"""One locked Job GET reuses projection work, never results from an older GET."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import builtins
import json
from pathlib import Path
import statistics
import threading
import time
from types import SimpleNamespace

import pytest
from local_http import TestClient

from studio.config import Settings
from studio.jobs import JobManager
from studio.ledger import Ledger
from studio.models import STAGES
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_revision_scope import env, RUN, write


def manager(store, pipe, settings, ledger):
    return JobManager(store, pipe, ledger, prepare=None)


def saved_job(store, job_id='parent', request_id='original', status='blocked', **fields):
    store.append_event(RUN, 'job_created', job_id=job_id, request_id=request_id,
                       text='Synthetic task', model_config={}, context_refs={},
                       job_kind='conversation', **fields)
    store.append_event(RUN, 'job_status', job_id=job_id, status=status, reason='Synthetic state')


@contextmanager
def traced_get(jobs, monkeypatch, *, legacy=False, count_io=True):
    """Reproduce the previous independent top-level memos, retaining recursion.

    No production read is mocked: the counters wrap actual temporary-file IO.
    Legacy discards only the memo supplied by the JobManager loop, while an
    individual stage's recursive calls still share their original local memo.
    """
    counts, cold, depth = Counter(), Counter(), 0
    original_stage = jobs.pipeline.stage_state
    original_events = jobs.store.read_events
    original_verify = jobs.store.verify_artifact
    original_path_open, original_open = Path.open, builtins.open
    root = jobs.store.root.resolve()

    def stage(run_id, name, _memo=None):
        nonlocal depth
        if legacy and depth == 0:
            _memo = None
        if _memo is None or name not in _memo:
            cold[name] += 1
        depth += 1
        try:
            return original_stage(run_id, name, _memo)
        finally:
            depth -= 1

    def events(*args, **kwargs):
        counts['read_events'] += 1
        return original_events(*args, **kwargs)

    def verify(*args, **kwargs):
        counts['verify_artifact'] += 1
        return original_verify(*args, **kwargs)

    def count_read(path, mode):
        if 'r' in mode and Path(path).resolve().is_relative_to(root):
            counts['file_read_opens'] += 1

    def path_open(path, mode='r', *args, **kwargs):
        handle = original_path_open(path, mode, *args, **kwargs)
        count_read(path, mode)
        return handle

    def file_open(path, mode='r', *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if not isinstance(path, int):
            count_read(path, mode)
        return handle

    with monkeypatch.context() as patch:
        patch.setattr(jobs.pipeline, 'stage_state', stage)
        if count_io:
            patch.setattr(jobs.store, 'read_events', events)
            patch.setattr(jobs.store, 'verify_artifact', verify)
            patch.setattr(Path, 'open', path_open)
            patch.setattr(builtins, 'open', file_open)
        yield counts, cold


def test_single_get_preserves_every_field_and_reads_each_stage_once(env, monkeypatch):
    store, pipe, settings, ledger = env
    saved_job(store)
    store.append_event(RUN, 'job_deliverable', job_id='parent',
                       deliverable={'goal': 'writing', 'status': 'complete', 'missing': []})
    store.append_event(RUN, 'job_progress', job_id='parent', event={
        'type': 'writing_revision_result', 'result': {'changed': ['segments.0.line']}})
    attempt = pipe.start_attempt(RUN, 'writing')
    jobs = manager(*env)
    with traced_get(jobs, monkeypatch, legacy=True) as (before, old_cold):
        previous = jobs.get(RUN, 'parent', after_seq=2)
    with traced_get(jobs, monkeypatch) as (after, new_cold):
        current = jobs.get(RUN, 'parent', after_seq=2)
    assert current == previous
    assert current['open_attempts'] == [{'stage': 'writing', 'attempt': attempt.key}]
    assert not current['can_continue']
    assert current['revision_result'] == {'changed': ['segments.0.line']}
    assert old_cold == {'intake': 4, 'direction': 3, 'material_plan': 2, 'writing': 1}
    assert new_cold == dict.fromkeys(STAGES, 1)
    assert before['verify_artifact'] == 10 and after['verify_artifact'] == 4
    assert after['read_events'] < before['read_events']
    assert after['file_read_opens'] < before['file_read_opens']
    print('\njob_get_read_counts', dict(before), '->', dict(after))


@pytest.mark.parametrize('endpoint', ['parent', 'latest'])
def test_http_reuses_locked_snapshot_preserves_response_and_refreshes_next_get(
        env, monkeypatch, endpoint):
    store, pipe, settings, _ = env
    saved_job(store)
    store.append_event(RUN, 'job_deliverable', job_id='parent',
                       deliverable={'goal': 'writing', 'status': 'complete', 'missing': []})
    store.append_event(RUN, 'job_progress', job_id='parent', event={
        'type': 'writing_revision_result', 'token': 'synthetic-private-token',
        'result': {'changed': ['segments.0.line']}})
    app = build_app(settings, client_factory=lambda: pytest.fail('No model permitted'))
    jobs = app.state.jobs
    original_get, original_state = jobs.get, jobs.pipeline.state
    snapshots = []

    def state(run_id, _memo=None):
        if _memo is not None:
            assert jobs.lock._is_owned() and jobs.store.run_lock(run_id)._is_owned()
            # JobManager already read each stage; state must use those exact rows.
            assert all(stage in _memo for stage in STAGES)
        result = original_state(run_id, _memo)
        if _memo is not None:
            assert all(result.stages[stage] is _memo[stage] for stage in STAGES)
            snapshots.append((_memo, result.to_dict()))
        return result

    def previous_http_get(run_id, job_id, after_seq=0, *, include_project_state=False):
        assert include_project_state
        # Reproduce the old HTTP handoff, retaining every real filesystem read.
        return original_get(run_id, job_id, after_seq, include_project_state=False)

    monkeypatch.setattr(jobs.pipeline, 'state', state)
    url = f'/api/runs/{RUN}/jobs/{endpoint}'
    with TestClient(app) as http:
        with monkeypatch.context() as legacy:
            legacy.setattr(jobs, 'get', previous_http_get)
            with traced_get(jobs, monkeypatch) as (before_io, before_cold):
                previous = http.get(url, params={'after_seq': 2})
        with traced_get(jobs, monkeypatch) as (after_io, after_cold):
            current = http.get(url, params={'after_seq': 2})
        assert previous.status_code == current.status_code == 200
        assert current.json() == previous.json()
        assert '_project_state' not in current.text
        assert 'synthetic-private-token' not in current.text
        assert current.json()['revision_result'] == {'changed': ['segments.0.line']}
        assert current.json()['events'][0]['token'] is None
        assert before_cold - after_cold == Counter(dict.fromkeys(STAGES, 1))
        assert after_io['verify_artifact'] < before_io['verify_artifact']
        assert after_io['file_read_opens'] < before_io['file_read_opens']
        assert len(snapshots) == 1
        assert '_project_state' not in original_get(RUN, 'parent')
        first_memo, first_state = snapshots[-1]

        # A new request must notice a changed upstream and a real open attempt.
        write(pipe, 'intake', {'facts': [{'label': 'Product', 'value': 'Updated HTTP fact'}]})
        attempt = pipe.start_attempt(RUN, 'direction')
        changed = http.get(url, params={'after_seq': 2})
        assert changed.status_code == 200, changed.text
        assert '_project_state' not in changed.text
        assert changed.json()['open_attempts'] == [{'stage': 'direction', 'attempt': attempt.key}]
        assert not changed.json()['can_continue']
        assert changed.json()['brief'] != current.json()['brief']
        assert len(snapshots) == 2 and snapshots[-1][0] is not first_memo
        stages = {stage['stage']: stage for stage in snapshots[-1][1]['stages']}
        assert stages['intake']['latest']['version'] == 2 and stages['writing']['stale']
        assert first_memo['intake'].latest.version == 1
        assert first_state['stages'][0]['latest']['version'] == 1
        assert next(row['event']['token'] for row in store.read_events(RUN)
                    if row['kind'] == 'job_progress') == 'synthetic-private-token'
        print('\nhttp_job_get_read_counts', endpoint, dict(before_io), '->', dict(after_io))


def test_next_get_revalidates_new_upstream_and_attempt_without_cache(env, monkeypatch):
    store, pipe, settings, ledger = env
    saved_job(store)
    jobs = manager(*env)
    original = pipe.stage_state
    memos, observed = [], []
    depth = 0

    def inspect(run_id, stage, _memo=None):
        nonlocal depth
        outer = depth == 0
        depth += 1
        try:
            state = original(run_id, stage, _memo)
            if outer:
                memos.append(_memo)
                if stage == 'writing':
                    observed.append(state)
            return state
        finally:
            depth -= 1

    monkeypatch.setattr(pipe, 'stage_state', inspect)
    assert jobs.get(RUN, 'parent')['can_continue']
    assert not observed[-1].stale
    old_memo = memos[-1]
    # The next request sees both a new immutable upstream and a real open stage.
    write(pipe, 'intake', {'facts': [{'label': 'Product', 'value': 'New synthetic fact'}]})
    attempt = pipe.start_attempt(RUN, 'direction')
    changed = jobs.get(RUN, 'parent')
    assert changed['open_attempts'] == [{'stage': 'direction', 'attempt': attempt.key}]
    assert not changed['can_continue'] and observed[-1].stale
    assert observed[-1].latest.version == 1
    assert memos[-1] is not old_memo
    assert memos[-1]['intake'].latest.version == 2
    pipe.reject(RUN, 'direction', attempt.token, 'Synthetic rejection')
    assert jobs.get(RUN, 'parent')['open_attempts'] == []
    assert jobs.get(RUN, 'parent')['can_continue']


def test_changed_body_is_rehashed_on_next_get_without_repair(env, monkeypatch):
    store, pipe, _, _ = env
    saved_job(store)
    jobs = manager(*env)
    original = store.verify_artifact
    verified = []

    def verify(run_id, stage, version):
        result = original(run_id, stage, version)
        verified.append((stage, version, result))
        return result

    monkeypatch.setattr(store, 'verify_artifact', verify)
    jobs.get(RUN, 'parent')
    assert ('writing', 1, True) in verified
    body = store.run_dir(RUN) / 'artifacts' / 'writing-v1.md'
    damaged = b'Changed synthetic body without corresponding fingerprint'
    body.write_bytes(damaged)
    verified.clear()
    jobs.get(RUN, 'parent')
    assert verified == [(stage, 1, stage != 'writing') for stage in STAGES]
    assert not pipe.stage_state(RUN, 'writing').integrity_ok
    assert body.read_bytes() == damaged


@pytest.mark.parametrize('file', ['events.jsonl', 'ledger.jsonl', 'artifacts/writing-v1.json'])
def test_damage_after_successful_get_still_blocks_http_and_preserves_original(env, file):
    store, _, settings, _ = env
    saved_job(store)
    app = build_app(settings, client_factory=lambda: pytest.fail('No model permitted'))
    with TestClient(app) as http:
        url = f'/api/runs/{RUN}/jobs/parent'
        assert http.get(url).status_code == 200
        path = store.run_dir(RUN) / file
        damaged = b'{"synthetic_broken_json":'
        path.write_bytes(damaged)
        response = http.get(url)
        assert response.status_code == 409, response.text
        assert not response.json()['integrity']['available']
        assert path.read_bytes() == damaged


def test_stage_read_holds_jobs_then_shared_run_lock_and_writer_waits(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic lock test', 'Offline only')
    settings = Settings(tmp_path, 'https://example.invalid')
    pipe = Pipeline(store)
    jobs = manager(store, pipe, settings, Ledger(store, settings))
    saved_job(store)
    second = Store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = pipe.stage_state

    def stage(run_id, name, _memo=None):
        assert jobs.lock._is_owned() and second.run_lock(RUN)._is_owned()
        if name == 'intake' and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(run_id, name, _memo)

    monkeypatch.setattr(pipe, 'stage_state', stage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(jobs.get, RUN, 'parent')
        try:
            assert entered.wait(3)
            writing = pool.submit(Pipeline(second).start_attempt, RUN, 'intake')
            with pytest.raises(FutureTimeout):
                writing.result(.1)
        finally:
            release.set()
        assert reading.result(3)['open_attempts'] == []
        attempt = writing.result(3)
    assert jobs.get(RUN, 'parent')['open_attempts'] == [{'stage': 'intake', 'attempt': attempt.key}]


def test_parent_successor_and_exact_request_receipts_refresh_on_next_get(env, monkeypatch):
    store, pipe, _, _ = env
    saved_job(store, status='cancelled')
    jobs = manager(*env)
    first = jobs.get(RUN, 'parent')
    assert first['successor_job_id'] is None and first['status'] == 'cancelled'
    # This is the same jobs -> run lock order used by actual continuation.
    with jobs.lock, store.run_lock(RUN):
        saved_job(store, 'child', 'followup', 'running', parent_job_id='parent')
    with traced_get(jobs, monkeypatch, legacy=True):
        before = jobs.get(RUN, 'parent')
    assert jobs.get(RUN, 'parent') == before
    assert before['successor_job_id'] == 'child' and before['steer_status'] == 'continued'
    assert before['status'] == 'cancelled' and jobs.get(RUN, 'child')['status'] == 'running'
    assert jobs.request_receipt(RUN, 'original')['job_id'] == 'parent'
    assert jobs.request_receipt(RUN, 'followup')['job_id'] == 'child'
    assert not jobs.request_receipt(RUN, 'not-an-accepted-request')['received']
    with jobs.lock, store.run_lock(RUN):
        store.append_event(RUN, 'job_status', job_id='child', status='blocked', reason='Exact child failure')
    assert jobs.get(RUN, 'child')['reason'] == 'Exact child failure'
    assert jobs.get(RUN, 'parent')['reason'] == 'Synthetic state'


def test_concurrent_stop_waits_for_read_and_cancels_original_once(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic cancellation test', 'Offline only')
    settings = Settings(tmp_path, 'https://example.invalid')
    pipe = Pipeline(store)
    entered, finish = threading.Event(), threading.Event()
    read_entered, release_read = threading.Event(), threading.Event()
    calls = []

    def prepare(_run_id):
        def run(_text, _emit, check):
            calls.append('original')
            entered.set()
            assert finish.wait(3)
            check()
            return SimpleNamespace(text='Should not finish', stopped_because='done', rounds=1, used_tools=[])
        return run, {'source': 'synthetic'}

    jobs = JobManager(store, pipe, Ledger(store, settings), prepare=prepare)
    job = jobs.submit(RUN, 'Synthetic controlled task', 'stop-test')
    assert entered.wait(3)
    original = pipe.stage_state

    def stage(run_id, name, _memo=None):
        if name == 'intake' and not read_entered.is_set():
            read_entered.set()
            assert release_read.wait(3)
        return original(run_id, name, _memo)

    monkeypatch.setattr(pipe, 'stage_state', stage)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading = pool.submit(jobs.get, RUN, job['job_id'])
            try:
                assert read_entered.wait(3)
                stopping = pool.submit(jobs.cancel, RUN, job['job_id'])
                with pytest.raises(FutureTimeout):
                    stopping.result(.1)
            finally:
                release_read.set()
            assert reading.result(3)['status'] == 'running'
            assert stopping.result(3)['status'] == 'cancelling'
        finish.set()
        deadline = time.monotonic() + 3
        with jobs.changed:
            while jobs.active is not None and time.monotonic() < deadline:
                jobs.changed.wait(timeout=max(0, deadline - time.monotonic()))
            assert jobs.active is None
        state = jobs.get(RUN, job['job_id'])
        assert state['status'] == 'cancelled' and state['successor_job_id'] is None
        assert calls == ['original']
    finally:
        release_read.set()
        finish.set()
        jobs.close()


def test_same_fixture_get_timing_is_recorded_without_relaxing_a_deadline(env, monkeypatch):
    store, _, _, _ = env
    saved_job(store)
    # A large valid journal reflects polling cost without starting a worker.
    path = store.run_dir(RUN) / 'events.jsonl'
    rows = store.read_events(RUN)
    for index in range(500):
        rows.append({'seq': len(rows) + 1, 'at': rows[-1]['at'], 'kind': 'fixture_history',
                     'detail': f'Synthetic history {index}'})
    path.write_bytes(''.join(json.dumps(row) + '\n' for row in rows).encode('utf-8'))
    jobs = manager(*env)
    samples = {'legacy': [], 'shared': []}
    for _ in range(5):
        for key, legacy in [('legacy', True), ('shared', False)]:
            with traced_get(jobs, monkeypatch, legacy=legacy, count_io=False):
                start = time.perf_counter()
                result = jobs.get(RUN, 'parent')
                samples[key].append(time.perf_counter() - start)
                assert result['status'] == 'blocked' and result['can_continue']
    print('\njob_get_seconds_same_fixture', {
        key: {'median': round(statistics.median(values), 4), 'min': round(min(values), 4),
              'max': round(max(values), 4)} for key, values in samples.items()})
