"""No network, real credentials or real project mutations in production tests."""
import hashlib
import io
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from studio.agents.client import Turn, Usage
from studio.agents.codex_image import GeneratedImage, ImageResult
from studio.agents.codex_client import CodexUnavailable
from studio.agents.wan_video import WanVideoError, WanVideoResult
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.production import DraftIn, prepare_draft, read_image
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for

BASE = f'/api/runs/{RUN}/production'
ENDPOINT = 'https://fixture.cn-beijing.maas.aliyuncs.com/api/v1'


def png(size=(240, 240)):
    output = io.BytesIO()
    Image.new('RGB', size, (25, 80, 150)).save(output, 'PNG')
    return output.getvalue()


def seed(store):
    store.create_run(RUN, 'Isolated production fixture', 'Synthetic only; no actual generation')
    accept_upload(store, RUN, 'reference.png', png())
    pipe = Pipeline(store)
    for stage in ['intake', 'direction', 'material_plan', 'writing']:
        attempt = pipe.start_attempt(RUN, stage)
        pipe.submit(RUN, stage, attempt.token, content='Synthetic ' + stage, summary=stage,
                    evidence=['Fixture only'], limitations=['Not market evidence'],
                    structured={'options': [{'key': 'A', 'claim': 'Synthetic claim'}]} if stage == 'direction' else {})
        if stage in {'direction', 'writing'}:
            pipe.record_review(RUN, stage, verdict='pass', findings=[])
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage), decision='approved',
                                 selected_option_id='A' if stage == 'direction' else None)
    return pipe


class Reviewer:
    def __init__(self):
        self.calls = []
        self.verdict = 'ready'
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return Turn(text=json.dumps({'verdict': self.verdict, 'findings': ['Synthetic check only'],
            'checks': {k: 'Fixture evidence' for k in ['opening', 'progression', 'payoff', 'product', 'execution']}}),
            usage=Usage(50, 20), stop_reason='end_turn', model='fixture-reviewer')


class Images:
    def __init__(self):
        self.calls = []; self.error = None; self.result_status = 'completed'
    def capability(self):
        return {'available': True, 'verified': False, 'reason': 'Offline fixture'}
    def generate(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        raw = png()
        request.output_dir.mkdir()
        path = request.output_dir / 'result.png'
        path.write_bytes(raw)
        return ImageResult(status=self.result_status, images=(GeneratedImage(path, hashlib.sha256(raw).hexdigest(),
            'image/png', 240, 240),), usage=Usage(100, 30), observed_image_calls=1,
            provider_invoked=True, thread_id='fixture-thread', turn_id='fixture-turn', model='fixture-codex')


class Videos:
    endpoint = ENDPOINT
    def __init__(self):
        self.calls = []; self.polls = []; self.downloads = []; self.error = None
        self.poll_error = None; self.download_error = None; self.status = 'SUCCEEDED'
        self.usage = {'output_video_duration': 5}
    def submit(self, request, *, expected_endpoint):
        assert expected_endpoint == ENDPOINT
        self.calls.append(request)
        if self.error:
            raise self.error
        return WanVideoResult('fixture-task', 'PENDING', ENDPOINT)
    def poll(self, task_id, *, endpoint):
        assert endpoint == ENDPOINT
        self.polls.append(task_id)
        if self.poll_error:
            raise self.poll_error
        return WanVideoResult(task_id, self.status, ENDPOINT, 'https://fixture.invalid/not-fetched', usage=self.usage)
    def download(self, url, *, max_bytes):
        assert max_bytes == 64 * 1024 * 1024
        self.downloads.append(url)
        if self.download_error:
            raise self.download_error
        return b'Fixture video bytes only'


@pytest.fixture
def scene(tmp_path, monkeypatch):
    store = Store(tmp_path); pipe = seed(store); settings = settings_for(tmp_path)
    reviewer, images, videos = Reviewer(), Images(), Videos()
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'OFFLINE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    monkeypatch.setattr('studio.production.validate_video', lambda raw: None)
    app = build_app(settings, client_factory=lambda: reviewer, production_image_factory=lambda: images,
        production_video_factory=lambda: videos, production_poll_delay=0, production_poll_count=1)
    with TestClient(app) as http:
        yield http, store, pipe, reviewer, images, videos, settings


def workspace(scene):
    response = scene[0].get(BASE)
    assert response.status_code == 200, response.text
    return response.json()


def test_team_input_review_uses_frozen_client_without_media_authority(scene, monkeypatch):
    from studio.production import ActionIn
    http, store, pipe, reviewer, images, videos, settings = scene
    item = draft(scene, kind='image').json()
    service = http.app.state.production
    monkeypatch.setattr(service.connections, 'snapshot', lambda: pytest.fail('must reuse the current turn connection'))
    before = list(store.list_approvals(RUN))
    runner, _ = service.prepare_review(RUN, item['draft_id'], ActionIn(
        request_id='team-check', expected_sha256=item['draft_sha256'], consent=True),
        frozen=(settings, reviewer, {'source': 'offline-frozen'}))
    runner('check', lambda event: None, lambda: None)
    checked = service.workspace(RUN, for_agent=True)['drafts'][-1]
    assert checked['review']['verdict'] == 'ready'
    assert not images.calls and not videos.calls
    assert store.list_approvals(RUN) == before


def draft(scene, kind='video', request_id='draft-1', **over):
    data = workspace(scene); source = data['sources'][-1]; ref = next(m for m in data['materials'] if m['name'] == 'reference.png')
    payload = dict(request_id=request_id, kind=kind, prompt='Synthetic product on table; slowly move camera.',
        source_version=source['version'], source_sha256=source['artifact_sha256'],
        reference={'name': ref['name'], 'sha256': ref['sha256']},
        model='wan3.0-video' if kind == 'video' else 'codex-image',
        duration=5 if kind == 'video' else None, resolution='720P' if kind == 'video' else None)
    return scene[0].post(BASE + '/drafts', json={**payload, **over})


def approve(scene, approved=True):
    data = workspace(scene); ref = data['materials'][0]
    return scene[0].post(BASE + '/material-approvals', json={
        'name': ref['name'], 'sha256': ref['sha256'], 'approved': approved, 'nonce': data['nonce']})


def wait(scene, response):
    assert response.status_code == 200, response.text
    job = response.json()
    for _ in range(500):
        item = scene[0].get(f"/api/runs/{RUN}/jobs/{job['job_id']}").json()
        if item['status'] not in {'running', 'queued', 'cancelling'}:
            return item
        time.sleep(.01)
    pytest.fail('offline job did not finish')


def action(scene, d, kind='review', request_id=None, **over):
    return scene[0].post(BASE + f"/{d['draft_id']}/{kind}", json={
        'request_id': request_id or kind + '-1', 'expected_sha256': d['draft_sha256'],
        'consent': True, 'nonce': workspace(scene)['nonce'], **over})


def ready(scene, kind='video'):
    d = draft(scene, kind).json()
    assert approve(scene).status_code == 200
    assert wait(scene, action(scene, d))['status'] == 'succeeded'
    return d


def test_preparation_is_free_immutable_and_retry_safe(scene):
    response = draft(scene); assert response.status_code == 200, response.text
    assert draft(scene).json()['draft_sha256'] == response.json()['draft_sha256']
    assert draft(scene, prompt='Different').status_code == 409
    assert not scene[3].calls and not scene[4].calls and not scene[5].calls
    assert not Ledger(scene[1], scene[-1]).entries()


def test_generation_requires_exact_review_source_and_human_picture_approval(scene):
    d = draft(scene).json()
    assert action(scene, d, 'generate').status_code == 409
    wait(scene, action(scene, d))
    assert action(scene, d, 'generate').status_code == 409
    approve(scene)
    assert action(scene, d, 'generate', nonce='forged').status_code == 409
    assert action(scene, d, 'generate', consent=False).status_code == 409
    assert action(scene, d, 'generate', expected_sha256='0'*64).status_code == 409
    assert not scene[5].calls


def test_video_result_saved_media_units_cny_and_retry_does_not_resubmit(scene):
    d = ready(scene); first = action(scene, d, 'generate')
    assert wait(scene, first)['status'] == 'succeeded'
    assert action(scene, d, 'generate').json()['job_id'] == first.json()['job_id']
    assert len(scene[5].calls) == 1
    a = workspace(scene)['drafts'][0]['attempts'][0]
    assert a['status'] == 'succeeded' and a['usage_quantity'] == 5
    assert scene[0].get(a['output']['url']).content == b'Fixture video bytes only'
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.input_tokens is None and row.output_tokens is None
    assert row.usage_unit == 'video_seconds' and row.estimated_cny == 3 and row.estimated_usd is None
    assert workspace(scene)['drafts'][0]['source_ready'] is True
    assert draft(scene, request_id='draft-2').status_code == 200
    from studio.evidence import evidence_snapshot
    output = next(m for m in evidence_snapshot(scene[1], RUN)['materials'] if m['name'] == a['output']['name'])
    assert output['origin'] == 'generated' and '不是商品实拍' in output['origin_notice']


def test_image_preserves_unknown_bill_and_text_tokens_then_explicit_recovery(scene):
    d = ready(scene, 'image'); wait(scene, action(scene, d, 'generate'))
    data = workspace(scene); a = data['drafts'][0]['attempts'][0]
    assert a['status'] == 'succeeded' and a['usage_status'] == 'unknown' and a['output']
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.input_tokens == 100 and row.output_tokens == 30
    assert row.actual_model == 'fixture-codex' and row.usage_quantity is None
    next_response = action(scene, d, 'generate', request_id='generate-2')
    assert next_response.status_code == 200
    wait(scene, next_response)
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': data['nonce'], 'quantity': 1, 'evidence_note': 'Synthetic provider export'})
    assert response.status_code == 200, response.text
    assert Ledger(scene[1], scene[-1]).totals().unknown_calls == 1
    assert len(scene[4].calls) == 2


def test_image_partial_result_survives_and_does_not_claim_success(scene):
    scene[4].result_status = 'unknown'
    d = ready(scene, 'image'); wait(scene, action(scene, d, 'generate'))
    a = workspace(scene)['drafts'][0]['attempts'][0]
    assert a['status'] == 'unknown' and a['output'] and a['usage_status'] == 'unknown'


def test_image_preflight_has_no_phantom_usage_blocker(scene):
    scene[4].error = CodexUnavailable('fixture before invocation')
    d = ready(scene, 'image'); wait(scene, action(scene, d, 'generate'))
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.usage_status == 'known' and row.usage_quantity == 0 and not row.ok


def test_submit_timeout_retains_fixed_endpoint_and_manual_binding_only_queries(scene):
    scene[5].error = WanVideoError('timeout', submission_unknown=True)
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    data = workspace(scene); a = data['drafts'][0]['attempts'][0]
    assert a['status'] == 'unknown' and a['endpoint'] == ENDPOINT
    assert action(scene, d, 'generate', request_id='second-submit').status_code == 409
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': data['nonce'], 'task_id': 'fixture-recovered', 'evidence_note': 'Synthetic task export'})
    assert response.status_code == 200, response.text
    response = scene[0].post(BASE + '/attempts/generate-1/refresh', json={'request_id': 'poll-1'})
    wait(scene, response)
    assert len(scene[5].calls) == 1 and scene[5].polls == ['fixture-recovered']


def test_download_failure_manual_seconds_then_refresh_still_retrieves(scene):
    scene[5].usage = None; scene[5].download_error = ValueError('fixture unavailable')
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    data = workspace(scene)
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': data['nonce'], 'quantity': 5.2, 'evidence_note': 'Synthetic provider 5.2s'})
    assert response.status_code == 200, response.text
    scene[5].download_error = None
    wait(scene, scene[0].post(BASE + '/attempts/generate-1/refresh', json={'request_id': 'retrieve-1'}))
    assert workspace(scene)['drafts'][0]['attempts'][0]['status'] == 'succeeded'
    assert len(scene[5].calls) == 1
    assert Ledger(scene[1], scene[-1]).entries()[-1].usage_quantity == 5.2


@pytest.mark.parametrize('change', ['original', 'script', 'observation', 'revoke'])
def test_source_or_picture_change_blocks_generation(scene, change):
    d = ready(scene)
    if change == 'original':
        (scene[1].run_dir(RUN) / 'materials/reference.png').write_bytes(png((241, 240)))
    elif change == 'script':
        (scene[1].run_dir(RUN) / 'artifacts/writing-v1.md').write_text('tampered', encoding='utf-8')
    elif change == 'observation':
        path = scene[1].run_dir(RUN) / 'evidence/observations.json'; path.parent.mkdir(exist_ok=True)
        path.write_text('{"schema_version":1,"observations":[]}', encoding='utf-8')
    else:
        approve(scene, False)
    assert action(scene, d, 'generate').status_code == 409
    assert not scene[5].calls


def test_review_is_independent_and_bad_verdict_never_authorizes(scene):
    scene[1].append_chat(RUN, 'assistant', 'PRIVATE_PRODUCER_SELF_PRAISE')
    scene[3].verdict = 'revise'
    d = draft(scene).json(); wait(scene, action(scene, d)); approve(scene)
    assert 'PRIVATE_PRODUCER_SELF_PRAISE' not in json.dumps(scene[3].calls)
    assert 'OFFLINE_METHOD' in json.dumps(scene[3].calls)
    assert action(scene, d, 'generate').status_code == 409


def test_failed_provider_does_not_fabricate_zero_usage(scene):
    scene[5].status = 'FAILED'
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    a = workspace(scene)['drafts'][0]['attempts'][0]
    assert a['status'] == 'failed' and a['usage_status'] == 'unknown'


def test_provider_explicit_rejection_reconciles_no_invocation(scene):
    scene[5].error = WanVideoError('Synthetic HTTP 400', submission_unknown=False)
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.usage_quantity == 0 and row.usage_status == 'known'


def test_token_recovery_cannot_clear_media_uncertainty(scene):
    scene[5].error = WanVideoError('timeout', submission_unknown=True)
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    snapshot = scene[0].get(f'/api/runs/{RUN}/recovery').json()
    response = scene[0].post(f'/api/runs/{RUN}/recovery/reconcile', json={
        'expected_revision': snapshot['revision'], 'call_id': 'generate-1', 'input_tokens': 0,
        'output_tokens': 0, 'evidence_note': 'Cannot use tokens'})
    assert response.status_code == 409
    assert Ledger(scene[1], scene[-1]).totals().unknown_calls == 1


def test_approval_nonce_and_no_zero_after_real_task(scene):
    data = workspace(scene); ref = data['materials'][0]
    response = scene[0].post(BASE + '/material-approvals', json={
        'name': ref['name'], 'sha256': ref['sha256'], 'approved': True, 'nonce': 'forged'})
    assert response.status_code == 409
    scene[5].usage = None
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    response = scene[0].post(BASE + '/attempts/generate-1/recover', json={
        'nonce': data['nonce'], 'not_submitted': True, 'evidence_note': 'Wrong'})
    assert response.status_code == 409


def test_production_image_validation_accepts_over_analysis_size(scene):
    # Deterministic uncompressed pixels exceed 4 MiB, no generated model data.
    output = io.BytesIO(); Image.new('RGB', (1400, 1400)).save(output, 'PNG', compress_level=0)
    raw = output.getvalue(); assert len(raw) > 4 * 1024 * 1024
    accept_upload(scene[1], RUN, 'large.png', raw)
    assert read_image(scene[1], RUN, 'large.png', hashlib.sha256(raw).hexdigest())[0] == raw


def test_agent_registry_has_preparation_but_no_execution_approval_or_recovery():
    from studio.tools.registry import tool_names_for
    names = tool_names_for('orchestrator')
    assert 'prepare_production' in names and 'read_production' in names
    assert not any(x in names for x in ('generate', 'approve_material', 'recover_production'))


def test_unapproved_script_cannot_become_production_plan(scene):
    pipe = scene[2]
    pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='returned', note='Fixture return')
    assert draft(scene).status_code == 409


def test_cross_origin_cannot_approve_or_spend(scene):
    response = scene[0].post(BASE + '/drafts', headers={'origin': 'https://foreign.example'}, json={})
    assert response.status_code == 403


def test_media_units_validate_quantities(scene):
    ledger = Ledger(scene[1], scene[-1])
    cid = ledger.begin_media_call(RUN, model='fixture', provider='fixture', usage_unit='images')
    for bad in [True, -1, float('nan'), float('inf'), '3']:
        with pytest.raises(ValueError):
            ledger.settle_media_call(RUN, cid, quantity=bad, ok=True)
    ledger.settle_media_call(RUN, cid, quantity=1, ok=True)
    assert ledger.totals().input_tokens == 0


def test_interrupted_submission_becomes_recoverable_without_resubmit(scene):
    d = draft(scene).json()
    service = scene[0].app.state.production
    service.event(RUN, 'production_attempt', attempt_id='crashed', draft_id=d['draft_id'],
        draft_sha256=d['draft_sha256'], media_kind='video', status='submitting', output=None, endpoint=ENDPOINT)
    scene[0].app.state.jobs.ledger.begin_media_call(RUN, call_id='crashed', model='wan3.0-video',
        provider='dashscope', usage_unit='video_seconds')
    data = workspace(scene)
    assert data['drafts'][0]['attempts'][0]['status'] == 'unknown'
    response = scene[0].post(BASE + '/attempts/crashed/recover', json={
        'nonce': data['nonce'], 'not_submitted': True, 'evidence_note': 'Synthetic provider no submission'})
    assert response.status_code == 200 and not scene[5].calls


def test_cancel_callback_reaches_image_adapter(scene):
    class CancelAware(Images):
        def generate(self, request):
            assert callable(self.check_cancel)
            self.check_cancel()
            return super().generate(request)
    image = CancelAware()
    scene[0].app.state.production.image_factory = lambda: image
    d = ready(scene, 'image'); wait(scene, action(scene, d, 'generate'))
    assert len(image.calls) == 1


def test_provider_duration_alias_is_accounted(scene):
    scene[5].usage = {'duration': 5.5}
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.usage_quantity == 5.5 and row.estimated_cny == 3.3


def test_frozen_video_parameters_match_review_and_submission(scene):
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    assert d['parameters'] == {'audio': True, 'ratio': 'adaptive', 'prompt_extend': False}
    assert '"prompt_extend": false' in scene[3].calls[0]['messages'][0]['content']
    request = scene[5].calls[0]
    assert request.audio is True and request.ratio == 'adaptive' and request.prompt_extend is False
