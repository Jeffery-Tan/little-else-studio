"""Durable queue admission and isolated query slots; all workers are synthetic."""
import io
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from studio.jobs import JobManager
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store
from test_server import settings_for


def done(text='done'):
    return SimpleNamespace(stopped_because='done', text=text, rounds=1, used_tools=[])


def wait(jobs, run_id, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = jobs.get(run_id, job_id)
        if current['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
            return current
        threading.Event().wait(.01)
    pytest.fail('synthetic worker did not finish')


@pytest.fixture
def queue(tmp_path):
    store = Store(tmp_path)
    for rid in ('first', 'second', 'third', 'fourth'):
        store.create_run(rid, rid, 'Synthetic brief')
    settings = settings_for(tmp_path)
    called = []
    config = {'source': 'offline', 'revision': 1}
    def prepare(rid):
        def run(text, emit, check):
            check()
            called.append((rid, text))
            return done(text)
        return run, dict(config)
    jobs = JobManager(store, Pipeline(store), Ledger(store, settings), prepare)
    yield jobs, store, called, config
    jobs.close()
    deadline = time.monotonic() + 5
    while jobs._controls and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert not jobs._controls


def blocking(jobs, run_id='first', *, query=False, attempt='original-task'):
    entered, released = threading.Event(), threading.Event()
    def prepare(_rid):
        def run(_text, _emit, check):
            entered.set()
            while not released.wait(.01):
                check()
            check()
            return done()
        return run, {'task_id': attempt, 'new_generation': False} if query else {'source': 'blocking'}
    result = jobs.submit(run_id, 'original user text', 'block-' + attempt,
        kind='production_refresh' if query else 'conversation',
        payload={'attempt_id': attempt} if query else None, query_only=query, prepare=prepare)
    assert entered.wait(2)
    return result, released


def test_fifo_and_cancelling_one_queued_does_not_stop_active(queue):
    jobs, store, called, _ = queue
    active, release = blocking(jobs)
    queued = [jobs.submit(rid, '原话 ' + rid, rid, enqueue=True)
              for rid in ('second', 'third', 'fourth')]
    assert [row['position'] for row in jobs.queue()['items'] if row['status'] == 'queued'] == [1, 2, 3]
    assert all(item['status'] == 'queued' for item in queued)
    events = store.read_events('second')
    assert next(row for row in events if row['kind'] == 'job_created')['text'] == '原话 second'
    assert {'job_created', 'job_queued', 'task_execution_budget'} <= {row['kind'] for row in events}
    assert not any(row['kind'] == 'job_execution_started' for row in events)
    assert queued[0]['queue_snapshot']['brief']
    jobs.cancel('third', queued[1]['job_id'])
    assert jobs.get('first', active['job_id'])['status'] == 'running'
    assert not jobs.cancelled.is_set()
    assert [item['position'] for item in jobs.queue()['items'] if item['status'] == 'queued'] == [1, 2]
    release.set()
    assert wait(jobs, 'fourth', queued[2]['job_id'])['status'] == 'succeeded'
    assert called == [('second', '原话 second'), ('fourth', '原话 fourth')]
    assert jobs.get('third', queued[1]['job_id'])['status'] == 'cancelled'


@pytest.mark.parametrize('change', ['context', 'connection'])
def test_changed_frozen_inputs_block_before_any_call(queue, change):
    jobs, store, called, config = queue
    active, release = blocking(jobs)
    queued = jobs.submit('second', 'keep exact target', 'queued', enqueue=True)
    if change == 'context':
        image = io.BytesIO()
        Image.new('RGB', (20, 20)).save(image, format='PNG')
        accept_upload(store, 'second', 'changed-source.png', image.getvalue())
    else:
        config['revision'] = 2
    release.set()
    result = wait(jobs, 'second', queued['job_id'])
    assert result['status'] == 'blocked' and '排队期间' in result['reason']
    assert called == []
    assert not any(row['kind'] == 'job_execution_started' for row in store.read_events('second'))


def test_restart_never_replays_queued_text(queue):
    jobs, store, called, _ = queue
    active, release = blocking(jobs)
    queued = jobs.submit('second', 'saved but not run', 'queued', enqueue=True)
    jobs.close()
    release.set()
    wait(jobs, 'first', active['job_id'])
    other = JobManager(store, jobs.pipeline, jobs.ledger, jobs.prepare)
    try:
        other.initialize()
        assert other.get('second', queued['job_id'])['status'] == 'interrupted'
        assert other.queue()['items'] == []
        assert called == []
    finally:
        other.close()


def test_media_cannot_enter_text_queue_and_legacy_busy_stays_explicit(queue):
    jobs, _, _, _ = queue
    active, release = blocking(jobs)
    with pytest.raises(PipelineError, match='未排队'):
        jobs.submit('second', 'legacy', 'legacy')
    with pytest.raises(PipelineError, match='只有文字'):
        jobs.submit('second', 'generate', 'media', kind='production_generation', enqueue=True)
    release.set()
    wait(jobs, 'first', active['job_id'])


def test_query_slots_are_bounded_and_cancellation_does_not_cross_lanes(queue):
    jobs, _, called, _ = queue
    query1, release1 = blocking(jobs, 'first', query=True, attempt='remote-one')
    query2, release2 = blocking(jobs, 'second', query=True, attempt='remote-two')
    with pytest.raises(PipelineError, match='两个视频'):
        blocking(jobs, 'third', query=True, attempt='remote-three')
    creative, release_text = blocking(jobs, 'third', attempt='creative')
    jobs.cancel('first', query1['job_id'])
    assert wait(jobs, 'first', query1['job_id'])['status'] == 'cancelled'
    assert jobs.get('third', creative['job_id'])['status'] == 'running'
    assert not jobs.cancelled.is_set()
    jobs.cancel('third', creative['job_id'])
    assert wait(jobs, 'third', creative['job_id'])['status'] == 'cancelled'
    assert jobs.get('second', query2['job_id'])['status'] == 'running'
    release2.set()
    assert wait(jobs, 'second', query2['job_id'])['status'] == 'succeeded'


def test_query_identity_and_duplicate_attempt_are_checked(queue):
    jobs, _, _, _ = queue
    query, release = blocking(jobs, query=True)
    with pytest.raises(PipelineError, match='已经在查询'):
        jobs.submit('first', 'same task', 'second-query', kind='production_refresh',
                    payload={'attempt_id': 'original-task'}, query_only=True)
    with pytest.raises(PipelineError, match='没有核验'):
        jobs.submit('second', 'not a task', 'bad-query', kind='production_refresh', query_only=True)
    release.set()
    wait(jobs, 'first', query['job_id'])


def test_queue_freezes_budget_but_starts_timeout_at_claim(queue):
    jobs, store, called, _ = queue
    active, release = blocking(jobs)
    queued = jobs.submit('second', 'bounded work', 'queued', enqueue=True)
    before = next(row for row in store.read_events('second') if row['kind'] == 'task_execution_budget')
    jobs.ledger.settings = replace(jobs.ledger.settings, task_max_calls=1, task_timeout_seconds=1)
    release.set()
    assert wait(jobs, 'second', queued['job_id'])['status'] == 'succeeded'
    events = store.read_events('second')
    assert len([row for row in events if row['kind'] == 'task_execution_budget']) == 1
    assert next(row for row in events if row['kind'] == 'task_execution_budget') == before
    assert next(row for row in events if row['kind'] == 'job_execution_started')['seq'] > before['seq']


def test_deadlines_are_owned_by_one_worker(queue):
    jobs, _, _, _ = queue
    query, release_query = blocking(jobs, 'first', query=True)
    creative, release_text = blocking(jobs, 'second')
    jobs._controls[('second', creative['job_id'])]['deadline'] = 0
    assert wait(jobs, 'second', creative['job_id'])['status'] == 'blocked'
    assert jobs.get('first', query['job_id'])['status'] == 'running'
    release_query.set()
    assert wait(jobs, 'first', query['job_id'])['status'] == 'succeeded'


def test_startup_marks_an_unclaimed_durable_job_interrupted(queue):
    from studio.agent_runtime import AgentRuntime
    jobs, store, called, _ = queue
    store.append_event('second', 'job_created', job_id='saved-job', request_id='saved-request',
                       text='exact text', job_kind='conversation', job_payload=None,
                       model_config={'source': 'offline'}, context_refs={})
    store.append_event('second', 'job_queued', job_id='saved-job', order=1)
    AgentRuntime(store, 'second').create_task('saved-job', role='orchestrator', parent_job_id='saved-job')
    jobs.initialize()
    assert jobs.get('second', 'saved-job')['status'] == 'interrupted'
    assert jobs.queue()['items'] == []
    assert not called


def test_queue_wakes_when_a_detached_role_finishes(queue, monkeypatch):
    from concurrent.futures import Future
    jobs, _, called, _ = queue
    future = Future()
    monkeypatch.setattr(jobs, '_role_futures', lambda *args: () if future.done() else (future,))
    queued = jobs.submit('second', 'after role settlement', 'role-queue', enqueue=True)
    assert queued['status'] == 'queued'
    assert not called
    future.set_result(None)
    assert wait(jobs, 'second', queued['job_id'])['status'] == 'succeeded'


def test_http_queue_keeps_original_receipt_and_duplicate_is_one_job(tmp_path):
    from local_http import TestClient
    from fakes import ScriptedModel, final_turn
    from studio.server import build_app
    from test_server import RUN, make_run
    model = ScriptedModel([final_turn('仅讨论')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        make_run(http)
        jobs = http.app.state.jobs
        active, release = blocking(jobs, RUN)
        data = {'text': '保留这句原话，仅讨论我的新想法。', 'request_id': 'queued-http'}
        url = f'/api/runs/{RUN}/jobs?enqueue=true'
        response = http.post(url, json=data)
        assert response.status_code == 200, response.text
        queued = response.json()
        assert queued['status'] == 'queued' and queued['queue_position'] == 1
        assert http.post(url, json=data).json()['job_id'] == queued['job_id']
        listing = http.get('/api/jobs/queue').json()['items']
        assert next(row for row in listing if row['job_id'] == queued['job_id'])['position'] == 1
        rows = Store(tmp_path).read_events(RUN)
        originals = [row for row in rows if row['kind'] == 'user_message_received' and row['request_id'] == data['request_id']]
        assert len(originals) == 1 and originals[0]['text'] == data['text']
        cancelled = http.post(f'/api/runs/{RUN}/jobs/{queued["job_id"]}/cancel').json()
        assert cancelled['status'] == 'cancelled'
        assert jobs.get(RUN, active['job_id'])['status'] == 'running'
        release.set()
        wait(jobs, RUN, active['job_id'])
        assert model.calls == []
