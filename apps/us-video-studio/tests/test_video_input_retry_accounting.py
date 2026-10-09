"""Real disposable Store/HTTP preparation; all runners/providers are offline fixtures."""
import threading

import pytest

from studio.agents.client import JobCancelled
from studio.agents.orchestrator import OrchestratorTurn
from studio.models import PipelineError
from test_production import (
    BASE, RUN, action, adopt_shot_count, batch_payload, draft, scene,
    video_goal, wait, workspace,
)


def setup_retry(scene, kind, *, stopped=False, blocked=False):
    http, store, _pipe, _reviewer, _images, _videos, _settings = scene
    count = 2 if kind == 'batch' else 1
    if count == 2:
        adopt_shot_count(scene, count)
    frames = [draft(scene, kind='image', request_id=f'frame-{index}',
                    shot_id=f'shot-{index:02d}').json() for index in range(1, count + 1)]
    generated = (http.post(BASE + '/batch-generate', json=batch_payload(scene, frames))
                 if kind == 'batch' else action(scene, frames[0], 'generate'))
    assert wait(scene, generated)['status'] == 'succeeded'
    data = workspace(scene)
    attempts = [next(d for d in data['drafts'] if d['draft_id'] == frame['draft_id'])
                ['attempts'][0] for frame in frames]
    assert all(row['status'] == 'succeeded' and row['usage_status'] == 'unknown' for row in attempts)
    refs = [{'name': row['output']['name'], 'sha256': row['output']['sha256']} for row in attempts]
    if kind == 'batch':
        approved = http.post(BASE + '/material-approvals/batch', json={
            'request_id': 'fixture-approved-frames', 'nonce': data['nonce'], 'images': refs})
        assert approved.status_code == 200 and approved.json()['continuation'] is None
        path, payload = BASE + '/material-approvals/batch/continue', {'images': refs}
    else:
        approved = http.post(BASE + '/material-approvals', json={
            **refs[0], 'approved': True, 'nonce': data['nonce']})
        assert approved.status_code == 200 and 'continuation' not in approved.json()
        path, payload = BASE + '/material-approvals/continue', refs[0]
    video_goal(scene)
    calls = []

    def prepare(run_id):
        calls.append(run_id)

        def run(text, emit, cancel):
            if stopped:
                raise JobCancelled('Synthetic explicit stop; no model invocation')
            if blocked:
                return OrchestratorTurn('Synthetic input preparation blocked.', 'incomplete', 0)
            raise PipelineError('fixture_prepare_failure', 'Synthetic input preparation failure')

        return run, {'source': 'offline-input-preparation'}

    http.app.state.jobs.prepare = prepare
    first = http.post(path, json={**payload, 'request_id': 'first-prepare-click'})
    assert first.status_code == 200, first.text
    first_id = first.json()['continuation']['job_id']
    job = wait(scene, http.get(f'/api/runs/{RUN}/jobs/{first_id}'))
    assert job['status'] == ('cancelled' if stopped else 'blocked' if blocked else 'failed')
    assert job['unresolved_calls'] and not job['open_attempts']
    return path, {**payload, 'request_id': 'fresh-explicit-prepare-click'}, job, calls, refs


def frozen_records(scene):
    http, store, *_ = scene
    return {
        'events': len(store.read_events(RUN)),
        'ledger': (store.run_dir(RUN) / 'ledger.jsonl').read_bytes(),
        'unknown': http.app.state.jobs.ledger.totals(RUN).unknown_calls,
        'approvals': [row for row in store.read_events(RUN)
                      if row['kind'] in {'production_material_approval', 'production_batch_approval'}],
        'provider_calls': (len(scene[3].calls), len(scene[4].calls), len(scene[5].calls)),
    }


def assert_no_paid_or_approval_effect(scene, before):
    after = frozen_records(scene)
    for key in ('ledger', 'unknown', 'approvals', 'provider_calls'):
        assert after[key] == before[key], key


@pytest.mark.parametrize('kind', ['single', 'batch'])
@pytest.mark.parametrize('historical_bill', [False, True], ids=['completed-frame-unknown-quantity', 'historical-image-bill'])
def test_explicit_input_retry_preserves_unknown_image_usage_and_exact_request(scene, kind, historical_bill):
    path, payload, first, calls, _refs = setup_retry(scene, kind, blocked=historical_bill)
    jobs = scene[0].app.state.jobs
    if historical_bill:
        jobs.ledger.begin_media_call(RUN, model='codex-image', provider='codex_cli',
                                     usage_unit='images', call_id='historical-image-unknown')
        jobs.ledger.settle_media_call(RUN, 'historical-image-unknown', quantity=None, ok=False,
                                     note='Synthetic historical quantity unknown; not zero')
    before = frozen_records(scene)
    response = scene[0].post(path, json=payload)
    assert response.status_code == 200, response.text
    second_id = response.json()['continuation']['job_id']
    assert second_id != first['job_id']
    assert wait(scene, scene[0].get(f'/api/runs/{RUN}/jobs/{second_id}'))['status'] == first['status']
    assert calls == [RUN, RUN]
    assert_no_paid_or_approval_effect(scene, before)
    settled = frozen_records(scene)
    repeated = scene[0].post(path, json=payload)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['continuation']['job_id'] == second_id
    assert calls == [RUN, RUN]
    assert frozen_records(scene) == settled
    assert len(jobs.list(RUN)) == 3


@pytest.mark.parametrize('kind', ['single', 'batch'])
@pytest.mark.parametrize('obstruction', ['active', 'open_stage', 'stopped', 'source_change', 'approval_change'])
def test_unknown_usage_does_not_relax_real_preparation_gates(scene, kind, obstruction):
    http, store, pipe, *_ = scene
    path, payload, first, calls, refs = setup_retry(scene, kind, stopped=obstruction == 'stopped')
    jobs = http.app.state.jobs
    release = threading.Event()
    active_id = None
    if obstruction == 'active':
        entered = threading.Event()

        def prepare_active(_run_id):
            def run(_text, _emit, _cancel):
                entered.set()
                assert release.wait(10), 'offline active task was not released'
                return OrchestratorTurn('Synthetic active task finished.', 'done', 0)
            return run, {'source': 'offline-active-task'}

        active = jobs.submit(RUN, 'Synthetic independent active task', 'active-preparation-gate',
                             prepare=prepare_active)
        active_id = active['job_id']
        assert entered.wait(1)
    elif obstruction == 'open_stage':
        pipe.start_attempt(RUN, 'writing')
    elif obstruction == 'source_change':
        jobs.before_submit = lambda: adopt_shot_count(scene, 2 if kind == 'batch' else 1)
    elif obstruction == 'approval_change':
        jobs.before_submit = lambda: http.app.state.production.event(
            RUN, 'production_material_approval', **refs[0], approved=False, actor='user')
    before = frozen_records(scene)
    job_count = len(jobs.list(RUN))
    try:
        response = http.post(path, json=payload)
        if obstruction == 'open_stage' and kind == 'batch':
            assert response.status_code == 409, response.text
            assert '当前整组分镜顺序' in response.json()['detail']
        else:
            assert response.status_code == 200, response.text
            continuation = response.json()['continuation']
            assert continuation.get('job_id', first['job_id']) == first['job_id']
            assert continuation['status'] in {'not_started', 'existing'}
        assert calls == [RUN]
        assert len(jobs.list(RUN)) == job_count
        # The deliberate approval-change hook writes a fixture-only revocation;
        # the rejected preparation must not write another approval or call.
        after = frozen_records(scene)
        for key in ('ledger', 'unknown', 'provider_calls'):
            assert after[key] == before[key], key
        if obstruction != 'approval_change':
            assert after['approvals'] == before['approvals']
    finally:
        jobs.before_submit = None
        release.set()
        if active_id:
            assert wait(scene, http.get(f'/api/runs/{RUN}/jobs/{active_id}'))['status'] == 'succeeded'
