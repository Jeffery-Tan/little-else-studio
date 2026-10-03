"""Real role/job boundaries with disposable journals and blocked fake calls."""
import threading
import time
from types import SimpleNamespace

import pytest

from fakes import final_turn
from local_http import TestClient
from studio.agent_sessions import AgentSessions
from studio.agents.client import ModelError
from studio.jobs import JobManager, WriterLease
from studio.models import PipelineError
from studio.server import build_app
from studio.role_executor import RoleExecutor
from studio.tools.dispatch import Dispatcher
from test_jobs import wait_job
from test_revision_scope import env, plan, QUOTE, RUN as SCOPED_RUN
from test_server import make_run, settings_for, RUN


def eventually(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        threading.Event().wait(.01)
    raise AssertionError('Synthetic execution did not reach the expected boundary')


class BlockingRole:
    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def complete(self, **_):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10)
        return final_turn('Offline role response')


def result():
    return SimpleNamespace(stopped_because='done', text='saved', rounds=1, used_tools=[])


@pytest.mark.parametrize('stop_kind', ['failure', 'cancel'])
def test_root_keeps_busy_until_owned_fake_provider_actually_settles(env, stop_kind):
    store, pipe, settings, ledger = env
    model = BlockingRole()
    role = {}
    root_release = threading.Event()
    jobs = JobManager(store, pipe, ledger, lambda _: pytest.fail('no second task'))

    def prepare(_):
        def execute(_text, emit, check):
            dispatcher = Dispatcher(store, pipe, settings, model, ledger, SCOPED_RUN, on_event=emit)
            role.update(dispatcher.start_role('writing', QUOTE, revision_scope=plan(env)))
            assert model.entered.wait(5)
            if stop_kind == 'failure':
                raise ModelError('Offline simulated 503')
            assert root_release.wait(5)
            check()
            return result()
        return execute, {}

    original = jobs.submit(SCOPED_RUN, QUOTE, 'root-once', prepare=prepare)
    try:
        assert model.entered.wait(5)
        if stop_kind == 'cancel':
            jobs.cancel(SCOPED_RUN, original['job_id'])
            root_release.set()
        eventually(lambda: AgentSessions(store, SCOPED_RUN).snapshot(role['session_id'])['stop_requested'])
        assert jobs.busy(SCOPED_RUN)
        assert jobs.active == (SCOPED_RUN, original['job_id'])
        assert jobs.get(SCOPED_RUN, original['job_id'])['status'] in {'running', 'cancelling'}
        assert ledger.totals(SCOPED_RUN).pending_calls == 1
        assert pipe.stage_state(SCOPED_RUN, 'writing').open_attempt is not None
        assert not jobs.close_when_idle() and not jobs.closing
        with pytest.raises(PipelineError, match='job_busy'):
            jobs.submit(SCOPED_RUN, 'new task', 'second')
        other_lease = WriterLease(store.root)
        with pytest.raises(PipelineError, match='writer_busy'):
            other_lease.acquire()
    finally:
        model.release.set()
        root_release.set()
    eventually(lambda: jobs.active is None)
    assert jobs.get(SCOPED_RUN, original['job_id'])['status'] == (
        'failed' if stop_kind == 'failure' else 'cancelled')
    assert AgentSessions(store, SCOPED_RUN).snapshot(role['session_id'])['status'] == 'cancelled'
    assert ledger.totals(SCOPED_RUN).pending_calls == 0
    assert pipe.stage_state(SCOPED_RUN, 'writing').open_attempt is None
    assert model.calls == 1
    assert jobs.close_when_idle()
    jobs.close()


def test_orphan_role_blocks_new_jobs_reload_and_writer_lease_release(env):
    store, pipe, settings, ledger = env
    jobs = JobManager(store, pipe, ledger, lambda _: pytest.fail('no new model'))
    jobs.initialize()
    model = BlockingRole()
    dispatcher = Dispatcher(store, pipe, settings, model, ledger, SCOPED_RUN)
    row = dispatcher.start_role('writing', QUOTE, revision_scope=plan(env))
    try:
        assert model.entered.wait(5)
        assert jobs.active is None and jobs.busy(SCOPED_RUN)
        assert not jobs.close_when_idle()
        with pytest.raises(PipelineError, match='job_busy'):
            jobs.submit(SCOPED_RUN, 'new task', 'new')
        jobs.close()
        eventually(lambda: AgentSessions(store, SCOPED_RUN).snapshot(row['session_id'])['stop_requested'])
        other = WriterLease(store.root)
        with pytest.raises(PipelineError, match='writer_busy'):
            other.acquire()
        assert ledger.totals(SCOPED_RUN).pending_calls == 1
    finally:
        model.release.set()
    eventually(lambda: not jobs.busy(SCOPED_RUN))
    eventually(lambda: jobs.lease.handle is None)
    assert ledger.totals(SCOPED_RUN).pending_calls == 0
    assert model.calls == 1
    other.acquire()
    other.close()


def test_pool_submission_reservation_is_busy_until_binding_and_execution_settle(env, monkeypatch):
    store, pipe, settings, ledger = env
    jobs = JobManager(store, pipe, ledger, lambda _: pytest.fail('no new model'))
    jobs.initialize()
    pool = RoleExecutor(max_workers=1, max_pending=1)
    monkeypatch.setattr('studio.tools.dispatch.DEFAULT_EXECUTOR', pool)
    model = BlockingRole()
    dispatcher = Dispatcher(store, pipe, settings, model, ledger, SCOPED_RUN)
    submitting, release_submit, returned = threading.Event(), threading.Event(), threading.Event()
    original_submit = pool.pool.submit
    errors = []
    def pause_after_worker_started(*args, **kwargs):
        actual = original_submit(*args, **kwargs)
        submitting.set()
        assert release_submit.wait(10)
        return actual
    monkeypatch.setattr(pool.pool, 'submit', pause_after_worker_started)
    def start():
        try:
            dispatcher.start_role('writing', QUOTE, revision_scope=plan(env))
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()
    thread = threading.Thread(target=start)
    thread.start()
    other = WriterLease(store.root)
    try:
        assert submitting.wait(5) and model.entered.wait(5)
        assert not returned.is_set()
        assert jobs.active is None and jobs.busy(SCOPED_RUN)
        with pool.lock:
            completion, = pool.active.values()
        assert completion is not None and not completion.done()
        assert not completion.cancel(), 'Execution cancellation belongs to the role session'
        assert not jobs.close_when_idle() and not jobs.closing
        with pytest.raises(PipelineError, match='job_busy'):
            jobs.submit(SCOPED_RUN, 'late task', 'late')
        jobs.close()
        sessions = AgentSessions(store, SCOPED_RUN)
        eventually(lambda: sessions.sessions()[0]['stop_requested'])
        assert ledger.totals(SCOPED_RUN).pending_calls == 1
        with pytest.raises(PipelineError, match='writer_busy'):
            other.acquire()
        # Even after the real provider has settled, the unbound submission is
        # still owned. There is no None placeholder that reload can ignore.
        model.release.set()
        eventually(lambda: sessions.sessions()[0]['status'] == 'cancelled')
        assert ledger.totals(SCOPED_RUN).pending_calls == 0
        assert not returned.is_set() and jobs.busy(SCOPED_RUN)
        assert not completion.done()
        with pytest.raises(PipelineError, match='writer_busy'):
            other.acquire()
    finally:
        model.release.set()
        release_submit.set()
        thread.join(5)
        pool.shutdown()
    assert returned.is_set() and errors == []
    assert completion.done() and completion.result().stopped_because == 'cancelled'
    eventually(lambda: not jobs.busy(SCOPED_RUN))
    eventually(lambda: jobs.lease.handle is None)
    assert model.calls == 1
    other.acquire()
    other.close()


def test_failed_pool_submission_settles_reservation_and_releases_capacity(monkeypatch):
    pool = RoleExecutor(max_workers=1, max_pending=1)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = pool.pool.submit
    def reject(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('Offline executor rejection')
    monkeypatch.setattr(pool.pool, 'submit', reject)
    def start():
        try:
            pool.submit('reserved', lambda: pytest.fail('never run rejected work'))
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=start)
    thread.start()
    try:
        assert entered.wait(5)
        with pool.lock:
            completion = pool.active['reserved']
        assert not completion.done()
        with pytest.raises(PipelineError, match='role_capacity'):
            pool.submit('another', lambda: None)
    finally:
        release.set()
        thread.join(5)
        monkeypatch.setattr(pool.pool, 'submit', original)
    assert len(errors) == 1 and completion.exception() is errors[0]
    assert not pool.active
    assert pool.submit('next', lambda: 'next result').result(timeout=2) == 'next result'
    pool.shutdown()


@pytest.mark.parametrize('final_status', ['completed', 'failed', 'waiting', 'cancelled'])
def test_role_completion_during_receipt_has_durable_rejection_not_false_acceptance(
        tmp_path, monkeypatch, final_status):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        jobs = http.app.state.jobs
        entered, release = threading.Event(), threading.Event()
        def prepare(_):
            def execute(*_):
                entered.set()
                assert release.wait(5)
                return result()
            return execute, {}
        job = jobs.submit(RUN, 'original task', 'root', prepare=prepare)
        assert entered.wait(5)
        sessions = AgentSessions(jobs.store, RUN)
        role = sessions.ensure(role='director', stage='direction', parent_job_id=job['job_id'])
        sessions.claim(role['session_id'])
        from studio import conversation_input
        receive = conversation_input.receive
        def receive_and_finish(*args, **kwargs):
            receipt = receive(*args, **kwargs)
            sessions.finish(role['session_id'], final_status, reason='Synthetic completion window')
            return receipt
        monkeypatch.setattr(conversation_input, 'receive', receive_and_finish)
        path = f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer'
        payload = {'request_id': 'exact-role-message', 'text': '开头更直接，但不要改变其他内容',
                   'expected_goal_revision': 0, 'target_session_id': role['session_id']}
        try:
            first = http.post(path, json=payload)
            assert first.status_code == 409 and 'session_closed' in first.text
            retry = http.post(path, json=payload)
            assert retry.status_code == 409 and retry.json() == first.json()
            assert http.post(path, json={**payload, 'text': 'different text'}).status_code == 409
            assert sessions.pending(role['session_id']) == []
            rows = jobs.store.read_events(RUN)
            assert len([e for e in rows if e['kind'] == 'user_message_received'
                        and e['request_id'] == payload['request_id']]) == 1
            assert len([e for e in rows if e['kind'] == 'job_steer_delivery_failed']) == 1
            assert not any(e['kind'] == 'job_steer_delivered' for e in rows)
            receipt = http.get(f'/api/runs/{RUN}/request-receipts/{payload["request_id"]}',
                params={'kind': 'steer', 'job_id': job['job_id']}).json()
            assert receipt['received'] is False and receipt['rejected'] is True
            assert receipt['code'] == 'session_closed' and '未投递' in receipt['reason']
            assert jobs.get(RUN, job['job_id'])['steer_status'] == 'rejected'
            assert not jobs.cancelled.is_set()
        finally:
            release.set()
        finished = wait_job(http, job['job_id'])
        assert finished['status'] == 'succeeded' and not finished['successor_job_id']
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no replay'))) as http:
        assert http.get(f'/api/runs/{RUN}/jobs/{job["job_id"]}').json()['steer_status'] == 'rejected'
        assert http.post(path, json=payload).status_code == 409


def test_inbox_and_delivery_receipt_share_role_completion_lock(tmp_path, monkeypatch):
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model'))) as http:
        make_run(http)
        jobs = http.app.state.jobs
        release = threading.Event()
        def prepare(_):
            def execute(*_):
                assert release.wait(5)
                return result()
            return execute, {}
        job = jobs.submit(RUN, 'original', 'root', prepare=prepare)
        sessions = AgentSessions(jobs.store, RUN)
        role = sessions.ensure(role='director', stage='direction', parent_job_id=job['job_id'])
        sessions.claim(role['session_id'])
        ending, ended = threading.Event(), threading.Event()
        original = AgentSessions.post_message
        def finish():
            ending.set()
            sessions.finish(role['session_id'], 'waiting', reason='Explicitly incomplete fixture')
            ended.set()
        threads = []
        def inject_finish(self, *args, **kwargs):
            thread = threading.Thread(target=finish)
            threads.append(thread)
            thread.start()
            assert ending.wait(2)
            assert not ended.wait(.05), 'Role finish must not overtake locked inbox acceptance'
            return original(self, *args, **kwargs)
        monkeypatch.setattr(AgentSessions, 'post_message', inject_finish)
        try:
            response = http.post(f'/api/runs/{RUN}/jobs/{job["job_id"]}/steer', json={
                'text': '补充当前角色要求', 'request_id': 'locked-inbox',
                'expected_goal_revision': 0, 'target_session_id': role['session_id']})
            assert response.status_code == 200, response.text
            assert response.json()['steer_status'] == 'delivered'
            assert ended.wait(2)
            assert len(sessions.pending(role['session_id'])) == 1
            rows = jobs.store.read_events(RUN)
            assert len([e for e in rows if e['kind'] == 'job_steer_delivered']) == 1
            assert not any(e['kind'] == 'job_steer_delivery_failed' for e in rows)
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
        wait_job(http, job['job_id'])
