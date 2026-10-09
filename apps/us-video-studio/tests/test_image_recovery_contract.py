"""Disposable FastAPI image recovery; providers are explicit offline fixtures."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from studio.ledger import Ledger
from studio.materials import resolve
from test_production import BASE, RUN, action, draft, ready, scene, wait, workspace


def partial(scene):
    scene[4].result_status = 'unknown'
    item = ready(scene, 'image')
    result = wait(scene, action(scene, item, 'generate'))
    assert result['status'] == 'blocked'
    service = scene[0].app.state.production
    service.status(RUN, 'generate-1', failure_code='cancelled')
    data = workspace(scene)
    attempt = next(row for row in data['drafts'][0]['attempts'] if row['attempt_id'] == 'generate-1')
    assert attempt['status'] == 'unknown' and attempt['provider_outcome'] == 'unknown'
    assert attempt['output_available'] and attempt['observed_image_calls'] == 1
    assert attempt['recovery']['modes'] == ['retry_with_unknown_cost']
    return item, attempt


def request(scene, attempt, **over):
    return {'nonce': workspace(scene)['nonce'], 'retry_with_unknown_cost': True,
            'request_id': 'exact-image-recovery',
            'expected_attempt_sha256': attempt['recovery']['attempt_sha256'],
            'evidence_note': 'Synthetic user retains the file and accepts old result and cost as unknown.',
            **over}


def post(scene, attempt, payload):
    return scene[0].post(BASE + f'/attempts/{attempt["attempt_id"]}/recover', json=payload)


def test_retained_unknown_image_records_only_the_explicit_recovery_and_preserves_all_evidence(scene):
    item, attempt = partial(scene)
    store, service = scene[1], scene[0].app.state.production
    before_events = store.read_events(RUN)
    before_ledger = (store.run_dir(RUN) / 'ledger.jsonl').read_bytes()
    before_approvals = store.list_approvals(RUN)
    output_path = resolve(store, RUN, attempt['output']['name'])
    before_output = output_path.read_bytes()
    body = request(scene, attempt)
    response = post(scene, attempt, body)
    assert response.status_code == 200, response.text
    assert response.json() == {'recovered': True, 'new_generation': False, 'usage_still_unknown': True}
    assert len(store.read_events(RUN)) == len(before_events) + 1
    recovered = service.attempts(RUN)[attempt['attempt_id']]
    assert recovered['status'] == 'retry_authorized' and recovered['recovery_actor'] == 'user'
    assert recovered['provider_outcome'] == 'unknown' and recovered['failure_code'] == 'cancelled'
    assert recovered['observed_image_calls'] == 1 and recovered['output'] == attempt['output']
    assert output_path.read_bytes() == before_output
    assert (store.run_dir(RUN) / 'ledger.jsonl').read_bytes() == before_ledger
    assert store.list_approvals(RUN) == before_approvals
    assert len(scene[4].calls) == 1 and not scene[3].calls and not scene[5].calls
    data = workspace(scene)
    assert not data['unresolved_attempts']
    assert data['shots']['image']['shots'][0]['stage'] != 'completed'
    assert data['drafts'][0]['draft_sha256'] == item['draft_sha256']
    # Preparing a separate explicit authorization still revalidates the exact
    # current input. Do not execute the returned provider runner in this test.
    from studio.production import ActionIn
    service.prepare_generation(RUN, item['draft_id'], ActionIn(request_id='new-independent-generation',
        expected_sha256=item['draft_sha256'], consent=True, nonce=data['nonce']))
    assert len(scene[4].calls) == 1


def test_same_recovery_request_is_idempotent_and_changed_payload_is_rejected(scene):
    _, attempt = partial(scene)
    body = request(scene, attempt)
    first = post(scene, attempt, body)
    assert first.status_code == 200, first.text
    before = scene[1].read_events(RUN)
    assert post(scene, attempt, body).json() == first.json()
    assert scene[1].read_events(RUN) == before
    for changed in [dict(body, evidence_note='Different user decision'),
                    dict(body, expected_attempt_sha256='0' * 64)]:
        assert post(scene, attempt, changed).status_code == 409
        assert scene[1].read_events(RUN) == before
    assert len(scene[4].calls) == 1


def test_concurrent_same_recovery_request_records_one_user_decision(scene):
    _, attempt = partial(scene)
    body = request(scene, attempt)
    before = len(scene[1].read_events(RUN))
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: post(scene, attempt, body), range(2)))
    assert [response.status_code for response in replies] == [200, 200]
    assert replies[0].json() == replies[1].json()
    assert len(scene[1].read_events(RUN)) == before + 1
    assert len(scene[4].calls) == 1


@pytest.mark.parametrize('fault', ['missing-request', 'missing-hash', 'wrong-hash', 'wrong-nonce',
                                   'changed-status', 'wrong-original-input', 'provider-complete',
                                   'missing-file', 'changed-bytes'])
def test_retained_image_recovery_rejects_unbound_changed_or_unverifiable_records(scene, fault):
    _, attempt = partial(scene)
    body = request(scene, attempt)
    if fault == 'missing-request':
        body.pop('request_id')
    elif fault == 'missing-hash':
        body.pop('expected_attempt_sha256')
    elif fault == 'wrong-hash':
        body['expected_attempt_sha256'] = '0' * 64
    elif fault == 'wrong-nonce':
        body['nonce'] = 'not-the-page-nonce'
    elif fault == 'changed-status':
        scene[0].app.state.production.status(RUN, attempt['attempt_id'], status='succeeded')
    elif fault == 'wrong-original-input':
        scene[0].app.state.production.status(RUN, attempt['attempt_id'], draft_sha256='0' * 64)
    elif fault == 'provider-complete':
        scene[0].app.state.production.status(RUN, attempt['attempt_id'], provider_outcome='completed')
    elif fault == 'missing-file':
        resolve(scene[1], RUN, attempt['output']['name']).unlink()
    elif fault == 'changed-bytes':
        resolve(scene[1], RUN, attempt['output']['name']).write_bytes(b'Invalid synthetic image bytes')
    before = scene[1].read_events(RUN)
    assert post(scene, attempt, body).status_code == 409
    assert scene[1].read_events(RUN) == before
    assert len(scene[4].calls) == 1


def test_retained_output_cannot_be_declared_unsubmitted_and_is_not_a_completed_or_approved_image(scene):
    _, attempt = partial(scene)
    before = scene[1].read_events(RUN)
    response = post(scene, attempt, {'nonce': workspace(scene)['nonce'], 'not_submitted': True,
                                   'evidence_note': 'Synthetic invalid no-submission claim.'})
    assert response.status_code == 409
    assert scene[1].read_events(RUN) == before
    data = workspace(scene)
    assert data['unresolved_attempts'][0]['attempt_id'] == attempt['attempt_id']
    assert data['shots']['image']['shots'][0]['stage'] == 'unverified'
    output = next(row for row in data['materials'] if row['name'] == attempt['output']['name'])
    assert not output['approved']


def test_a_replaced_input_can_resolve_its_own_old_unknown_attempt_without_authorizing_new_generation(scene):
    item, attempt = partial(scene)
    replacement = draft(scene, kind='image', request_id='explicit-new-input',
                        shot_id=item['shot_id'], supersedes=item['draft_id'],
                        prompt='Synthetic independent replacement input.').json()
    assert replacement['draft_id'] == 'explicit-new-input'
    data = workspace(scene)
    original = next(row for row in data['drafts'] if row['draft_id'] == item['draft_id'])
    assert not original['is_current']
    assert data['unresolved_attempts'][0]['attempt_id'] == attempt['attempt_id']
    response = post(scene, attempt, request(scene, original['attempts'][0]))
    assert response.status_code == 200, response.text
    assert not workspace(scene)['unresolved_attempts']
    assert len(scene[4].calls) == 1
    # The historical input remains forbidden at the fresh execution boundary.
    assert action(scene, item, 'generate', request_id='old-input-again').status_code == 409
    assert len(scene[4].calls) == 1


def test_even_a_matching_recorded_hash_cannot_authorize_invalid_image_bytes(scene):
    _, attempt = partial(scene)
    raw = b'Not an image, with a correct synthetic SHA256.'
    path = resolve(scene[1], RUN, attempt['output']['name'])
    path.write_bytes(raw)
    output = {**attempt['output'], 'sha256': hashlib.sha256(raw).hexdigest()}
    service = scene[0].app.state.production
    service.event(RUN, 'production_output', attempt_id=attempt['attempt_id'], output=output)
    current = next(row for row in workspace(scene)['drafts'][0]['attempts']
                   if row['attempt_id'] == attempt['attempt_id'])
    assert current['output_available']
    before = scene[1].read_events(RUN)
    assert post(scene, current, request(scene, current)).status_code == 409
    assert scene[1].read_events(RUN) == before
    assert Ledger(scene[1], scene[-1]).entries()[-1].usage_status == 'unknown'


def test_durable_recovery_ack_is_read_only_after_service_nonce_change_and_later_missing_file(scene):
    _, attempt = partial(scene)
    body = request(scene, attempt)
    first = post(scene, attempt, body)
    assert first.status_code == 200, first.text
    service = scene[0].app.state.production
    service.nonces.pop(RUN)
    assert workspace(scene)['nonce'] != body['nonce']
    resolve(scene[1], RUN, attempt['output']['name']).unlink()
    before = scene[1].read_events(RUN)
    before_ledger = (scene[1].run_dir(RUN) / 'ledger.jsonl').read_bytes()
    replay = post(scene, attempt, body)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert scene[1].read_events(RUN) == before
    assert (scene[1].run_dir(RUN) / 'ledger.jsonl').read_bytes() == before_ledger
    assert not resolve(scene[1], RUN, attempt['output']['name'])
    assert len(scene[4].calls) == 1
    # New decisions do not inherit the old receipt's read-only exception.
    changed = {**body, 'request_id':'another-recovery-request'}
    assert post(scene, attempt, changed).status_code == 409
    assert scene[1].read_events(RUN) == before
