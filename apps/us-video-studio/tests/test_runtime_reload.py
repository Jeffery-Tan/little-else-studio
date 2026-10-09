"""Free local update boundaries; no model or media provider requests."""
import threading
from types import SimpleNamespace

import pytest

from studio import runtime_reload
from studio.jobs import JobManager, WriterLease
from studio.ledger import Ledger
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store
from test_server import settings_for


def manager(tmp_path, *, create=True):
    store = Store(tmp_path)
    if create:
        store.create_run('reload-test', 'reload fixture', 'offline fixture')
    ledger = Ledger(store, settings_for(tmp_path))
    jobs = JobManager(store, Pipeline(store), ledger, None)
    jobs.initialize()
    return jobs


def test_update_waits_for_running_job_then_drains_admission_without_replay(tmp_path):
    jobs = manager(tmp_path)
    started, finish = threading.Event(), threading.Event()
    calls = []

    def runner(text, emit, check_cancel):
        calls.append(text)
        started.set()
        assert finish.wait(5)
        check_cancel()
        return SimpleNamespace(text='saved fixture', rounds=1, used_tools=[], stopped_because='done')

    job = jobs.submit('reload-test', 'one offline job', 'one', prepare=lambda _: (runner, {}))
    assert started.wait(5)
    runtime = {'code_fingerprint': 'old', 'data_root_id': 'same'}
    watch = runtime_reload.ReloadWatch(jobs, runtime, fingerprint=lambda: 'new', validate=lambda: True)
    try:
        assert not watch.tick(0)
        assert not watch.tick(3)
        assert runtime['auto_reload']['status'] == 'waiting_for_idle'
        assert not jobs.closing and not jobs.cancelled.is_set()
        assert not watch.tick(4)
        finish.set()
        with jobs.changed:
            assert jobs.changed.wait_for(lambda: jobs.active is None, timeout=5)
        assert watch.tick(5)
        assert jobs.closing and not jobs.cancelled.is_set()
        assert runtime['code_fingerprint'] == 'old' and runtime['data_root_id'] == 'same'
        assert jobs.get('reload-test', job['job_id'])['status'] == 'succeeded'
        with pytest.raises(PipelineError, match='服务正在停止|此服务正在停止'):
            jobs.submit('reload-test', 'must not run', 'two', prepare=lambda _: pytest.fail('no call'))
        with pytest.raises(PipelineError, match='写入|运行|持有'):
            WriterLease(tmp_path).acquire()
    finally:
        finish.set()
        jobs.close()
    with WriterLease(tmp_path):
        pass
    restarted = manager(tmp_path, create=False)
    try:
        assert restarted.get('reload-test', job['job_id'])['status'] == 'succeeded'
        assert calls == ['one offline job']
    finally:
        restarted.close()


def test_debounce_reverted_changes_and_syntax_errors_keep_old_service(tmp_path):
    jobs = manager(tmp_path)
    current = ['new-a']
    valid = [False]
    runtime = {'code_fingerprint': 'old'}
    watch = runtime_reload.ReloadWatch(jobs, runtime,
        fingerprint=lambda: current[0], validate=lambda: valid[0])
    try:
        assert not watch.tick(0)
        current[0] = 'new-b'
        assert not watch.tick(1)
        assert not watch.tick(2)
        assert not watch.tick(4)
        assert runtime['auto_reload']['status'] == 'invalid_code' and not jobs.closing
        current[0] = 'old'
        assert not watch.tick(5)
        assert runtime['auto_reload'] == {'enabled': True, 'status': 'watching'}
        current[0], valid[0] = 'fixed', True
        assert not watch.tick(6)
        assert watch.tick(9)
    finally:
        jobs.close()


def test_missing_source_during_save_defers_reload(tmp_path):
    jobs = manager(tmp_path)
    def unavailable():
        raise FileNotFoundError('temporary rename')
    watch = runtime_reload.ReloadWatch(jobs, {'code_fingerprint': 'old'}, fingerprint=unavailable)
    try:
        assert not watch.tick(1)
        assert watch.state['status'] == 'check_failed' and not jobs.closing
    finally:
        jobs.close()


def test_pending_login_also_defers_reload_and_new_login_is_blocked(tmp_path):
    from studio.codex_auth import CodexAuth
    from fastapi import HTTPException
    jobs = manager(tmp_path)
    auth = CodexAuth(jobs)
    auth.process = object()
    jobs.before_submit = auth.require_idle
    try:
        assert not jobs.begin_reload() and not jobs.closing
        auth.process = None
        assert jobs.begin_reload()
        with pytest.raises(HTTPException) as rejected:
            auth.start()
        assert rejected.value.status_code == 409
    finally:
        jobs.close()


def test_syntax_validation_only_parses_and_never_executes(tmp_path, monkeypatch):
    source = tmp_path / 'fixture.py'
    source.write_text('raise RuntimeError("must never execute")\n', encoding='utf-8')
    monkeypatch.setattr(runtime_reload, '_fingerprint_paths', lambda: [source])
    assert runtime_reload.validate_code()
    source.write_text('def broken(:\n', encoding='utf-8')
    assert not runtime_reload.validate_code()


@pytest.mark.parametrize('terminal_code', [0, 1, 2])
def test_supervisor_restarts_only_after_clean_reload(tmp_path, monkeypatch, terminal_code):
    results = iter([runtime_reload.RELOAD_EXIT_CODE, terminal_code])
    launched = []
    def launch(command, **kwargs):
        launched.append(command)
        return next(results)
    monkeypatch.setattr(runtime_reload.subprocess, 'call', launch)
    assert runtime_reload.supervise(tmp_path, 4760) == terminal_code
    assert len(launched) == 2 and launched[0] == launched[1]
    assert str(tmp_path) in launched[0] and '--reload-worker' in launched[0]


def test_normal_server_exit_never_requests_reload(monkeypatch):
    import uvicorn
    app = SimpleNamespace(state=SimpleNamespace(runtime_info={}, jobs=None))
    async def exit_normally(self):
        return
    monkeypatch.setattr(uvicorn.Server, 'serve', exit_normally)
    assert runtime_reload.serve(app, 0, auto_reload=False) == 0
    assert app.state.runtime_info['auto_reload']['enabled'] is False


def test_http_drain_is_bounded_only_after_idle_reload_admission_closes(monkeypatch):
    import asyncio
    import uvicorn
    configurations = []
    class Server:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, True, False
            assert config.timeout_graceful_shutdown is None
        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(.001)
            configurations.append(self.config)
    class IdleWatch:
        def __init__(self, jobs, runtime):
            self.reloading = False
        def tick(self, now):
            self.reloading = True
            return True  # The idle/admission boundary is covered above.
    monkeypatch.setattr(uvicorn, 'Server', Server)
    monkeypatch.setattr(runtime_reload, 'ReloadWatch', IdleWatch)
    app = SimpleNamespace(state=SimpleNamespace(runtime_info={}, jobs=None))
    assert runtime_reload.serve(app, 0, auto_reload=True) == runtime_reload.RELOAD_EXIT_CODE
    assert configurations[0].timeout_graceful_shutdown == 15
