"""Reload protection uses the real job manager, temporary data and no models."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from local_http import TestClient

from studio.dev_reload import IdleReload, RELOAD_EXIT, supervise
from studio.doctor import inspect, mark_new_root
from studio.jobs import WriterLease
from studio.models import PipelineError
from studio.server import build_app
from test_server import RUN, make_run, settings_for


def watcher(app, *, validate=lambda: None, server=None):
    server = server or SimpleNamespace(started=True, should_exit=False,
                                      config=SimpleNamespace(timeout_graceful_shutdown=None))
    state = {'fingerprint': app.state.runtime_info['code_fingerprint'], 'now': 0.0}
    reload = IdleReload(app, server, fingerprint=lambda: state['fingerprint'],
                        validate=validate, now=lambda: state['now'], settle=1)
    return reload, server, state


def changed(reload, state):
    state['fingerprint'] = 'new-code'
    reload.tick()
    state['now'] += 1.1
    reload.tick()


def test_reload_waits_for_actual_job_to_finish_and_does_not_replay_it(tmp_path):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        jobs = http.app.state.jobs
        entered, release = threading.Event(), threading.Event()
        calls = []

        def prepare(_run):
            def execute(text, emit, check_cancel):
                calls.append(text)
                entered.set()
                assert release.wait(3)
                check_cancel()
                return SimpleNamespace(stopped_because='done', text='saved', rounds=1, used_tools=[])
            return execute, {'source': 'synthetic'}

        original = jobs.submit(RUN, 'synthetic generation', 'once', prepare=prepare,
                               kind='production_batch_generation')
        assert entered.wait(2)
        reload, server, state = watcher(http.app)
        try:
            changed(reload, state)
            assert reload.info['state'] == 'waiting_for_idle'
            assert not server.should_exit and not jobs.cancelled.is_set() and not jobs.closing
            assert server.config.timeout_graceful_shutdown is None
            assert http.get('/api/runtime').status_code == 200
            assert http.get(f'/api/runs/{RUN}/jobs/{original["job_id"]}').status_code == 200
        finally:
            release.set()
        deadline = time.monotonic() + 3
        while jobs.active:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert jobs.get(RUN, original['job_id'])['status'] == 'succeeded'
        reload.tick()
        assert server.should_exit and reload.reloading and jobs.closing
        assert server.config.timeout_graceful_shutdown == 10.0
        assert not jobs.cancelled.is_set()
        with pytest.raises(PipelineError, match='停止'):
            jobs.submit(RUN, 'late new task', 'late', prepare=prepare)
        assert calls == ['synthetic generation']
        assert len(jobs.list(RUN)) == 1


def test_reload_debounces_saves_and_keeps_old_service_on_syntax_error(tmp_path):
    def invalid():
        raise SyntaxError('synthetic invalid code')
    with TestClient(build_app(settings_for(tmp_path))) as http:
        reload, server, state = watcher(http.app, validate=invalid)
        state['fingerprint'] = 'first-save'
        reload.tick()
        state['now'] = .8
        state['fingerprint'] = 'second-save'
        reload.tick()
        state['now'] = 1.1
        reload.tick()
        assert not server.should_exit and reload.info['state'] == 'settling'
        state['now'] = 2
        reload.tick()
        assert reload.info['state'] == 'invalid_code'
        assert not http.app.state.jobs.closing and not server.should_exit
        assert http.get('/api/runtime').json()['hot_reload']['state'] == 'invalid_code'
        # A transient file-read error must not overwrite the validation result.
        reload.info.update(state='waiting_for_files')
        reload.tick()
        assert not server.should_exit
        reload.validate = lambda: None
        state['fingerprint'] = 'fixed-code'
        reload.tick()
        state['now'] = 3.1
        reload.tick()
        assert server.should_exit


def test_reload_waits_for_login_then_atomically_closes_login_and_job_entry(tmp_path):
    with TestClient(build_app(settings_for(tmp_path))) as http:
        auth = http.app.state.codex_auth
        auth.process = object()
        reload, server, state = watcher(http.app)
        changed(reload, state)
        assert not server.should_exit and not auth.closed
        assert server.config.timeout_graceful_shutdown is None
        auth.process = None
        reload.tick()
        assert auth.closed and http.app.state.jobs.closing and server.should_exit
        assert http.post('/api/connections/codex/login', json={'consent': True}).status_code == 409


def test_idle_reload_bounds_uvicorn_wait_for_stranded_http_transport(tmp_path, monkeypatch):
    import uvicorn
    import studio.dev_reload as module
    monkeypatch.setattr(module, 'IDLE_HTTP_SHUTDOWN_TIMEOUT', .01)
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        server = uvicorn.Server(uvicorn.Config(http.app, log_level='error'))
        server.started = True
        events = []

        class StrandedHttpServer:
            def close(self):
                events.append('listener_closed')

            async def wait_closed(self):
                events.append('waiting_for_transport_detach')
                try:
                    # A failed Windows Proactor disconnect callback can leave
                    # asyncio.Server active_count nonzero forever.
                    await asyncio.Event().wait()
                finally:
                    events.append('http_wait_ended')

        class Lifespan:
            async def shutdown(self):
                events.append('application_shutdown')

        server.servers = [StrandedHttpServer()]
        server.lifespan = Lifespan()
        reload, server, state = watcher(http.app, server=server)
        changed(reload, state)
        assert http.app.state.jobs.closing and not http.app.state.jobs.cancelled.is_set()
        assert server.should_exit and reload.reloading
        assert not server.force_exit
        asyncio.run(asyncio.wait_for(server.shutdown(), timeout=2))
        assert events == ['listener_closed', 'waiting_for_transport_detach',
                          'http_wait_ended', 'application_shutdown']
        assert not http.app.state.jobs.cancelled.is_set()


def test_supervisor_lease_prevents_second_launcher_even_between_workers(tmp_path):
    mark_new_root(tmp_path)
    with WriterLease(tmp_path, name='.studio-supervisor.lock'):
        report = inspect(tmp_path, port=0)
        assert not report['ready_to_start']
        assert next(row for row in report['checks'] if row['name'] == 'supervisor')['status'] == 'error'
        assert supervise(tmp_path, 0) == 2
    assert next(row for row in inspect(tmp_path, port=0)['checks'] if row['name'] == 'supervisor')['status'] == 'ok'


def test_supervisor_waits_for_worker_exit_before_replacement(tmp_path, monkeypatch):
    import studio.dev_reload as module
    timeline = []
    exits = iter([RELOAD_EXIT, 0])

    class Child:
        def __init__(self, command, **kwargs):
            timeline.append('start')
            assert command[command.index('--data-root') + 1] == str(tmp_path)
            assert '--supervisor-id' in command
            assert '-B' in command and '-X' in command
            assert command[command.index('-X') + 1].startswith('pycache_prefix=')

        def wait(self):
            timeline.append('exit')
            return next(exits)

    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    assert supervise(tmp_path, 0) == 0
    assert timeline == ['start', 'exit', 'start', 'exit']
    with WriterLease(tmp_path, name='.studio-supervisor.lock'):
        pass
