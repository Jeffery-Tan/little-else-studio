"""Review R1-R3 through real HTTP jobs and offline counted media providers."""
import threading

import pytest

from studio.agents.codex_client import CodexUnavailable
from studio.production import DraftIn, prepare_draft, target_shots
from studio.task_contract import evaluate, media_progress
from studio.decision import prepare_generation_candidate
from test_production import BASE, RUN, action, batch_payload, draft, scene, wait, workspace
from test_storyboard_http import SCRIPT


@pytest.fixture
def storyboard(scene):
    pipe, store = scene[2], scene[1]
    attempt = pipe.start_attempt(RUN, 'writing')
    source = pipe.submit(RUN, 'writing', attempt.token, content='Synthetic storyboard',
                         summary='Three shots', structured=SCRIPT)
    pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='approved')
    return scene


def inputs(scene):
    result = []
    for index, target in enumerate(target_shots(scene[1], RUN)['shots'], start=1):
        response = draft(scene, kind='image', request_id=f'frame-{index}', shot_id=target['shot_id'])
        assert response.status_code == 200, response.text
        result.append(response.json())
    return result


def replace_second(scene, old):
    source = scene[1].latest_artifact(RUN, 'writing')
    return prepare_draft(scene[1], scene[2], RUN, DraftIn(
        request_id='second-revised', kind='image', prompt='Revised second shot only',
        source_version=source.version, source_sha256=source.sha256,
        model='codex-image', supersedes=old['draft_id']), actor='agent')


def test_input_readiness_is_not_media_delivery_or_a_saved_confirmation(storyboard):
    scene = storyboard
    inputs(scene)
    assert prepare_generation_candidate(scene[1], scene[2], RUN, 'image')
    delivery = evaluate(scene[1], scene[2], RUN, 'production_media')
    assert delivery['status'] == 'input_ready' and delivery['missing']
    assert evaluate(scene[1], scene[2], RUN, 'production_input')['status'] == 'complete'
    assert not scene[4].calls and not scene[5].calls


def test_real_succeeded_attempts_and_saved_files_complete_the_storyboard(storyboard):
    scene = storyboard
    items = inputs(scene)
    for index, item in enumerate(items):
        assert wait(scene, action(scene, item, 'generate', f'generate-{index}'))['status'] == 'succeeded'
    projection = workspace(scene)['shots']['image']
    assert projection['state'] == 'complete' and projection['delivered']
    assert [row['stage'] for row in projection['shots']] == ['completed'] * 3
    assert all(row['output'] for row in projection['shots'])
    assert evaluate(scene[1], scene[2], RUN, 'production_media')['status'] == 'complete'
    assert media_progress(scene[1], RUN, 'image')['delivered']
    assert len(scene[4].calls) == 3 and not scene[5].calls


def test_revising_one_delivered_shot_keeps_order_and_only_offers_that_shot(storyboard):
    scene = storyboard
    items = inputs(scene)
    for index, item in enumerate(items):
        assert wait(scene, action(scene, item, 'generate', f'generate-{index}'))['status'] == 'succeeded'
    replace_second(scene, items[1])
    state = workspace(scene)
    assert [row['stage'] for row in state['shots']['image']['shots']] == ['completed', 'input_ready', 'completed']
    assert [row['image_draft_id'] for row in state['video_sequence']] == ['frame-1', 'second-revised', 'frame-3']
    candidate = prepare_generation_candidate(scene[1], scene[2], RUN, 'image')
    assert candidate['count'] == 1
    assert [row['draft_id'] for row in candidate['target']['drafts']] == ['second-revised']
    assert len(scene[4].calls) == 3


@pytest.mark.parametrize('error,expected', [
    (CodexUnavailable('Offline synthetic precheck failure'), 'failed'),
    (RuntimeError('Offline synthetic interruption'), 'unknown'),
])
def test_latest_failure_or_unknown_cannot_reuse_prior_success_as_delivery(storyboard, error, expected):
    scene = storyboard
    items = inputs(scene)
    for index, item in enumerate(items):
        assert wait(scene, action(scene, item, 'generate', f'original-{index}'))['status'] == 'succeeded'
    old_output = workspace(scene)['shots']['image']['shots'][1]['output']
    scene[4].error = error
    retried = wait(scene, action(scene, items[1], 'generate', 'retry-second'))
    assert retried['status'] == 'blocked'
    projection = workspace(scene)['shots']['image']
    assert projection['state'] == expected and not projection['delivered']
    shot = projection['shots'][1]
    assert shot['stage'] == ('failed' if expected == 'failed' else 'unverified')
    assert shot['output'] == old_output
    assert shot['attempt_id'] == 'retry-second' and shot['reason']
    assert evaluate(scene[1], scene[2], RUN, 'production_media')['status'] == expected
    assert prepare_generation_candidate(scene[1], scene[2], RUN, 'image') is None
    assert len(scene[4].calls) == 4 and not scene[5].calls


@pytest.mark.parametrize('batch', [False, True])
def test_old_confirmation_is_refused_after_second_shot_replacement_before_provider(storyboard, batch):
    scene = storyboard
    items = inputs(scene)
    confirmation = batch_payload(scene, items, 'old-batch') if batch else {
        'request_id': 'old-single', 'expected_sha256': items[1]['draft_sha256'],
        'nonce': workspace(scene)['nonce'], 'consent': True}
    replace_second(scene, items[1])
    endpoint = '/batch-generate' if batch else f"/{items[1]['draft_id']}/generate"
    response = scene[0].post(BASE + endpoint, json=confirmation)
    assert response.status_code == 409 and '替换' in response.text
    assert not scene[4].calls and not scene[5].calls
    assert not any(row['kind'] == 'production_attempt' for row in scene[1].read_events(RUN))
    projection = workspace(scene)['shots']['image']['shots']
    assert [row['draft_id'] for row in projection] == ['frame-1', 'second-revised', 'frame-3']


@pytest.mark.parametrize('batch', [False, True])
def test_http_worker_revalidates_the_entire_confirmation_after_preparation(storyboard, monkeypatch, batch):
    scene = storyboard
    items = inputs(scene)
    entered, release = threading.Event(), threading.Event()
    service = scene[0].app.state.production
    method = 'prepare_batch_generation' if batch else 'prepare_generation'
    original = getattr(service, method)

    def delayed(*args, **kwargs):
        runner, config = original(*args, **kwargs)
        def run(text, emit, check_cancel):
            entered.set()
            assert release.wait(5), 'Synthetic worker was not released'
            return runner(text, emit, check_cancel)
        return run, config

    monkeypatch.setattr(service, method, delayed)
    response = scene[0].post(BASE + '/batch-generate', json=batch_payload(scene, items)) if batch else \
        action(scene, items[1], 'generate', 'delayed-single')
    assert response.status_code == 200, response.text
    try:
        assert entered.wait(5)
        replace_second(scene, items[1])
    finally:
        release.set()
    result = wait(scene, response)
    assert result['status'] == 'failed' and '替换' in result['reason']
    assert not scene[4].calls and not scene[5].calls
    assert not any(row['kind'] == 'production_attempt' for row in scene[1].read_events(RUN))


def test_batch_rechecks_remaining_revision_before_each_actual_submit(storyboard, monkeypatch):
    scene = storyboard
    items = inputs(scene)
    provider = scene[4]
    original = provider.generate

    def replace_after_first(request):
        result = original(request)
        if len(provider.calls) == 1:
            replace_second(scene, items[1])
        return result

    monkeypatch.setattr(provider, 'generate', replace_after_first)
    result = wait(scene, scene[0].post(BASE + '/batch-generate', json=batch_payload(scene, items)))
    assert result['status'] == 'failed' and '替换' in result['reason']
    assert len(provider.calls) == 1 and not scene[5].calls
    projection = workspace(scene)['shots']['image']['shots']
    assert projection[0]['stage'] == 'completed'
    assert projection[1]['draft_id'] == 'second-revised' and projection[1]['stage'] == 'input_ready'
    assert projection[2]['stage'] == 'input_ready'


def test_actual_provider_boundary_rechecks_changes_after_intent_was_recorded(storyboard, monkeypatch):
    scene = storyboard
    items = inputs(scene)
    service = scene[0].app.state.production
    original = service.status
    replaced = False

    def change_before_submit(run_id, attempt_id, **update):
        nonlocal replaced
        original(run_id, attempt_id, **update)
        if update.get('status') == 'submitting' and not replaced:
            replaced = True
            replace_second(scene, items[1])

    monkeypatch.setattr(service, 'status', change_before_submit)
    result = wait(scene, scene[0].post(BASE + '/batch-generate', json=batch_payload(scene, items)))
    assert result['status'] == 'failed' and '替换' in result['reason']
    assert not scene[4].calls and not scene[5].calls
    attempts = list(service.attempts(RUN).values())
    assert len(attempts) == 1 and attempts[0]['status'] == 'failed'
    assert attempts[0]['usage_quantity'] == 0


def test_deleted_generated_file_is_not_a_readable_deliverable(storyboard):
    scene = storyboard
    items = inputs(scene)
    for index, item in enumerate(items):
        assert wait(scene, action(scene, item, 'generate', f'generate-{index}'))['status'] == 'succeeded'
    output = workspace(scene)['shots']['image']['shots'][1]['output']
    # This path belongs only to this pytest temporary synthetic data root.
    (scene[1].run_dir(RUN) / 'materials' / output['name']).unlink()
    projection = workspace(scene)['shots']['image']
    assert projection['state'] == 'unknown' and not projection['delivered']
    assert projection['shots'][1]['stage'] == 'unverified'


def test_new_revision_cannot_hide_an_unknown_original_task_or_submit_again(storyboard):
    scene = storyboard
    items = inputs(scene)
    scene[4].error = RuntimeError('Synthetic provider uncertainty')
    assert wait(scene, action(scene, items[1], 'generate', 'original-unknown'))['status'] == 'blocked'
    assert len(scene[4].calls) == 1
    replacement = replace_second(scene, items[1])
    projection = workspace(scene)['shots']['image']
    assert projection['state'] == 'unknown'
    assert projection['shots'][1]['draft_id'] == replacement['draft_id']
    assert projection['shots'][1]['attempt_id'] == 'original-unknown'
    assert prepare_generation_candidate(scene[1], scene[2], RUN, 'image') is None
    response = action(scene, replacement, 'generate', 'cannot-bypass-unknown')
    assert response.status_code == 409 and '原任务' in response.text
    assert len(scene[4].calls) == 1 and not scene[5].calls
