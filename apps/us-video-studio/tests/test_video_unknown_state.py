"""A provider's unknown or expired task result never becomes a retry permit."""
import pytest

from studio.ledger import Ledger
from test_production import BASE, RUN, action, batch_payload, ready, scene, wait, workspace


def unknown_video(scene):
    scene[5].status = 'UNKNOWN'
    item = ready(scene)
    result = wait(scene, action(scene, item, 'generate'))
    assert result['status'] == 'blocked'
    return item


def test_unknown_provider_result_keeps_original_identity_and_blocks_generation(scene):
    item = unknown_video(scene)
    state = workspace(scene)
    attempt = state['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'unknown'
    assert attempt['task_id'] == 'fixture-task'
    assert '24 小时' in attempt['message']
    assert '无法通过此接口取回' in attempt['message']
    assert attempt['usage_status'] == 'unknown'
    assert state['unresolved_attempts'][0]['attempt_id'] == 'generate-1'
    assert action(scene, item, 'generate', request_id='new-generation').status_code == 409
    batch = scene[0].post(BASE + '/batch-generate', json=batch_payload(scene, [item]))
    assert batch.status_code == 409 and '未核实' in batch.text
    assert len(scene[5].calls) == 1
    assert scene[5].downloads == []
    entry = Ledger(scene[1], scene[-1]).entries(RUN)[-1]
    assert entry.usage_quantity is None and entry.estimated_cny is None


def test_explicit_refresh_can_resolve_same_unknown_task_without_resubmission(scene):
    unknown_video(scene)
    scene[5].status = 'SUCCEEDED'
    wait(scene, scene[0].post(BASE + '/attempts/generate-1/refresh',
                            json={'request_id': 'check-original'}))
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'succeeded'
    assert len(scene[5].calls) == 1
    assert scene[5].polls == ['fixture-task', 'fixture-task']


@pytest.mark.parametrize('recovery', [
    {'retry_with_unknown_cost': True},
    {'not_submitted': True},
])
def test_unknown_video_has_no_risk_retry_or_unsubmitted_escape(scene, recovery):
    item = unknown_video(scene)
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': workspace(scene)['nonce'], 'evidence_note': 'Synthetic only', **recovery})
    assert response.status_code == 409
    assert workspace(scene)['drafts'][0]['attempts'][0]['status'] == 'unknown'
    assert action(scene, item, 'generate', request_id='new-generation').status_code == 409
    assert len(scene[5].calls) == 1


def test_usage_reconciliation_does_not_resolve_unknown_video_result(scene):
    item = unknown_video(scene)
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': workspace(scene)['nonce'], 'quantity': 5,
        'evidence_note': 'Synthetic provider usage receipt; no result evidence'})
    assert response.status_code == 200, response.text
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'unknown'
    assert attempt['usage_status'] == 'known'
    assert '24 小时' in attempt['message'] and '不能重新提交' in attempt['message']
    assert action(scene, item, 'generate', request_id='new-generation').status_code == 409
    assert len(scene[5].calls) == 1
