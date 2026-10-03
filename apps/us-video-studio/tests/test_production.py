"""No network, real credentials or real project mutations in production tests."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from local_http import TestClient
from PIL import Image

from studio.agents.client import Turn, Usage
from studio.agents.orchestrator import OrchestratorTurn
from studio.agents.codex_image import GeneratedImage, ImageResult
from studio.agents.codex_client import CodexUnavailable
from studio.agents.wan_video import WanVideoError, WanVideoResult
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.models import Approval, PipelineError
from studio.production import DraftIn, assemble_videos, prepare_draft, read_image, source_snapshot
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for
from visual_fixtures import stage_structure

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
                    structured={'options': [{'key': 'A', 'claim': 'Synthetic claim',
                                             'satisfies': 'goal.primary'}]}
                    if stage == 'direction' else stage_structure(stage))
        if stage in {'direction', 'writing'}:
            pipe.record_review(RUN, stage, verdict='pass', findings=[])
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage), decision='approved',
                                 selected_option_id='A' if stage == 'direction' else None)
    return pipe


def test_old_approval_rows_keep_their_production_baseline(tmp_path, monkeypatch):
    store = Store(tmp_path)
    seed(store)
    original = Approval.to_dict
    with monkeypatch.context() as patch:
        patch.setattr(Approval, 'to_dict', lambda approval: {
            key: value for key, value in original(approval).items() if key != 'continue_after'})
        old_snapshot = source_snapshot(store, RUN)
    assert source_snapshot(store, RUN) == old_snapshot


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


def test_team_input_review_endpoint_is_retired_without_model_or_media_call(scene):
    http, store, pipe, reviewer, images, videos, settings = scene
    item = draft(scene, kind='image').json()
    before = list(store.list_approvals(RUN))
    assert action(scene, item, 'review').status_code == 410
    checked = http.app.state.production.workspace(RUN, for_agent=True)['drafts'][-1]
    assert checked['review'] is None
    assert not reviewer.calls
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


def video_goal(scene):
    scene[1].append_event(RUN, 'creative_context_selected',
        selection={'intent': 'new', 'format': 'video', 'tags': [], 'case_ids': None})


def capture_continuation(scene, *, finish_input=False):
    calls = []
    def prepare(run_id):
        calls.append(run_id)
        def run(text, emit, check_cancel):
            if finish_input:
                service = scene[0].app.state.production
                source = service.workspace(run_id, for_agent=True)['sources'][-1]
                reference = next(m for m in service.workspace(run_id, for_agent=True)['materials']
                                 if m['name'] == 'reference.png')
                payload = DraftIn(request_id='agent-video-input', kind='video',
                    prompt='Synthetic reviewed video input for the approved image.',
                    source_version=source['version'], source_sha256=source['artifact_sha256'],
                    reference={'name': reference['name'], 'sha256': reference['sha256']},
                    model='wan3.0-video', duration=5, resolution='720P')
                item = prepare_draft(scene[1], scene[2], run_id, payload, actor='agent')
            return OrchestratorTurn('Prepared offline.', 'done', 0)
        return run, {'source': 'offline-continuation'}
    scene[0].app.state.jobs.prepare = prepare
    return calls


def test_approved_image_continues_exact_video_work_once_without_generation(scene):
    video_goal(scene)
    calls = capture_continuation(scene, finish_input=True)
    source = workspace(scene)['sources'][-1]
    ref = workspace(scene)['materials'][0]

    first = approve(scene)
    assert first.status_code == 200, first.text
    continuation = first.json()['continuation']
    assert continuation['job_id']
    job = wait(scene, scene[0].get(f"/api/runs/{RUN}/jobs/{continuation['job_id']}"))
    assert job['status'] == 'succeeded'
    assert ref['name'] in job['text'] and ref['sha256'] in job['text']
    assert source['artifact_sha256'] in job['text']
    assert calls == [RUN]
    assert not scene[4].calls and not scene[5].calls
    prepared = workspace(scene)['drafts'][-1]
    assert prepared['actor'] == 'agent' and prepared['review'] is None
    assert prepared['reference']['sha256'] == ref['sha256']
    assert prepared['source_sha256'] == source['artifact_sha256']
    assert prepared['can_generate'] is True

    repeated = approve(scene)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['continuation'] == {'status': 'existing', 'draft_id': prepared['draft_id']}
    assert calls == [RUN]


@pytest.mark.parametrize('query_format', ['video', 'none'])
def test_status_query_does_not_disable_a_new_image_approval_video_continuation(scene, query_format):
    video_goal(scene)
    scene[1].append_event(RUN, 'creative_context_selected',
        selection={'intent': 'status', 'format': query_format, 'tags': [], 'case_ids': None})
    calls = capture_continuation(scene, finish_input=True)
    assert workspace(scene)['active'] is False and calls == []
    response = approve(scene)
    assert response.status_code == 200, response.text
    continuation = response.json()['continuation']
    assert continuation and continuation['job_id'], response.json()
    assert wait(scene, scene[0].get(f"/api/runs/{RUN}/jobs/{continuation['job_id']}"))['status'] == 'succeeded'
    assert calls == [RUN]
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('query_format', ['video', 'none'])
def test_explicit_batch_video_preparation_after_status_uses_the_same_approved_frames_once(scene, query_format):
    http, store, pipeline, _reviewer, images, videos, _settings = scene
    adopt_shot_count(scene, 2)
    frames = [draft(scene, kind='image', request_id=f'frame-{index}',
                    shot_id=f'shot-{index:02d}').json() for index in (1, 2)]
    assert wait(scene, http.post(BASE + '/batch-generate', json=batch_payload(scene, frames)))['status'] == 'succeeded'
    outputs = [next(d for d in workspace(scene)['drafts'] if d['draft_id'] == frame['draft_id'])
               ['attempts'][0]['output'] for frame in frames]
    refs = [{'name': output['name'], 'sha256': output['sha256']} for output in outputs]
    approval = http.post(BASE + '/material-approvals/batch', json={
        'request_id':'approve-before-video-route', 'nonce':workspace(scene)['nonce'], 'images':refs})
    assert approval.status_code == 200 and approval.json()['continuation'] is None
    video_goal(scene)
    store.append_event(RUN, 'creative_context_selected',
        selection={'intent':'status', 'format':query_format, 'tags':[], 'case_ids':None})
    calls = []
    def prepare(run_id):
        calls.append(run_id)
        def run(text, emit, check_cancel):
            source = workspace(scene)['sources'][-1]
            for index, ref in enumerate(refs, 1):
                prepare_draft(store, pipeline, run_id, DraftIn(request_id=f'clip-after-status-{index}',
                    kind='video', prompt=f'Offline approved clip {index}.',
                    source_version=source['version'], source_sha256=source['artifact_sha256'],
                    shot_id=f'shot-{index:02d}', reference=ref,
                    model='wan3.0-video', duration=3, resolution='720P'), actor='agent')
            return OrchestratorTurn('Prepared offline.', 'done', 1)
        return run, {'source':'offline-batch-after-status'}
    http.app.state.jobs.prepare = prepare
    assert workspace(scene)['active'] is False and calls == []
    payload = {'request_id':'explicit-continue-after-status', 'images':refs}
    response = http.post(BASE + '/material-approvals/batch/continue', json=payload)
    assert response.status_code == 200, response.text
    continuation = response.json()['continuation']
    assert wait(scene, http.get(f"/api/runs/{RUN}/jobs/{continuation['job_id']}"))['status'] == 'succeeded'
    assert calls == [RUN] and not videos.calls and len(images.calls) == 2
    assert [shot['video_draft_id'] for shot in workspace(scene)['video_sequence']] == [
        'clip-after-status-1', 'clip-after-status-2']
    assert http.post(BASE + '/material-approvals/batch/continue', json=payload).status_code == 200
    assert calls == [RUN] and not videos.calls and len(images.calls) == 2


def test_approved_image_continuation_can_resume_after_job_was_not_started(scene):
    video_goal(scene)
    calls = capture_continuation(scene)
    jobs = scene[0].app.state.jobs
    before_submit = jobs.before_submit
    jobs.before_submit = lambda: (_ for _ in ()).throw(PipelineError('fixture', '连接暂不可用'))
    try:
        response = approve(scene)
    finally:
        jobs.before_submit = before_submit
    assert response.status_code == 200, response.text
    assert response.json()['continuation']['status'] == 'not_started'
    assert not calls
    ref = workspace(scene)['materials'][0]
    assert ref['approved'] is True

    recovery = {'name': ref['name'], 'sha256': ref['sha256'], 'request_id': 'first-explicit-click'}
    recovered = scene[0].post(BASE + '/material-approvals/continue', json=recovery)
    assert recovered.status_code == 200, recovered.text
    job_id = recovered.json()['continuation']['job_id']
    assert wait(scene, scene[0].get(f"/api/runs/{RUN}/jobs/{job_id}"))['status'] == 'succeeded'
    assert calls == [RUN]
    again = scene[0].post(BASE + '/material-approvals/continue', json=recovery)
    assert again.json()['continuation']['job_id'] == job_id
    assert calls == [RUN]
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('first_status', ['failed', 'blocked'])
def test_approved_image_continuation_retries_only_after_explicit_new_click(scene, first_status):
    video_goal(scene)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def prepare(run_id):
        calls.append(run_id)
        ordinal = len(calls)
        def run(text, emit, check_cancel):
            if ordinal == 1:
                if first_status == 'failed':
                    raise PipelineError('fixture', 'Synthetic preparation failure')
                return OrchestratorTurn('Synthetic preparation blocked.', 'incomplete', 0)
            entered.set()
            assert release.wait(2), 'second preparation was not released'
            return OrchestratorTurn('Prepared offline.', 'done', 0)
        return run, {'source': 'offline-continuation'}
    scene[0].app.state.jobs.prepare = prepare
    data = workspace(scene)
    ref = data['materials'][0]
    approval = {'name': ref['name'], 'sha256': ref['sha256'], 'approved': True, 'nonce': data['nonce']}
    first = scene[0].post(BASE + '/material-approvals', json=approval)
    assert first.status_code == 200, first.text
    first_id = first.json()['continuation']['job_id']
    assert wait(scene, scene[0].get(f'/api/runs/{RUN}/jobs/{first_id}'))['status'] == first_status

    # A repeated approval after an uncertain HTTP response cannot spend on a
    # new model turn. The existing job, even though failed, is returned.
    duplicate_approval = scene[0].post(BASE + '/material-approvals', json=approval)
    assert duplicate_approval.status_code == 200, duplicate_approval.text
    assert duplicate_approval.json()['continuation']['job_id'] == first_id
    assert calls == [RUN]

    path = BASE + '/material-approvals/continue'
    retry = {'name': ref['name'], 'sha256': ref['sha256'], 'request_id': 'explicit-retry-1'}
    second = scene[0].post(path, json=retry)
    assert second.status_code == 200, second.text
    second_id = second.json()['continuation']['job_id']
    assert second_id != first_id and entered.wait(2)
    assert scene[0].post(path, json=retry).json()['continuation']['job_id'] == second_id
    assert scene[0].post(path, json={**retry, 'request_id': 'explicit-retry-2'}).json()['continuation']['job_id'] == second_id
    assert calls == [RUN, RUN]

    release.set()
    assert wait(scene, scene[0].get(f'/api/runs/{RUN}/jobs/{second_id}'))['status'] == 'succeeded'
    assert scene[0].post(path, json={**retry, 'request_id': 'explicit-retry-3'}).json()['continuation']['job_id'] == second_id
    assert calls == [RUN, RUN]
    assert not scene[4].calls and not scene[5].calls


def test_image_approval_skips_video_continuation_for_photo_and_existing_input(scene):
    calls = capture_continuation(scene)
    photo = approve(scene)
    assert photo.status_code == 200 and 'continuation' not in photo.json()
    assert not calls

    video_goal(scene)
    existing = draft(scene)
    assert existing.status_code == 200, existing.text
    approved = approve(scene)
    assert approved.status_code == 200, approved.text
    assert approved.json()['continuation'] == {'status': 'existing', 'draft_id': existing.json()['draft_id']}
    assert not calls


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


def batch_payload(scene, drafts, request_id='batch-1'):
    return {'request_id': request_id, 'nonce': workspace(scene)['nonce'], 'consent': True,
            'drafts': [{'draft_id': item['draft_id'], 'expected_sha256': item['draft_sha256']}
                       for item in drafts]}


def adopt_shot_count(scene, count):
    """Batch fixtures describe their intended shot count in the actual script."""
    structure = stage_structure('writing')
    first = structure['segments'][0]
    structure['segments'] = [{**first, 'tc': f'{index * 5}–{(index + 1) * 5}',
                              'picture': f'Synthetic product demonstration shot {index + 1}.'}
                             for index in range(count)]
    structure['segments'][0]['shot_id'] = scene[1].latest_artifact(RUN, 'writing').structured['segments'][0]['shot_id']
    pipe = scene[2]
    attempt = pipe.start_attempt(RUN, 'writing')
    pipe.submit(RUN, 'writing', attempt.token, content=f'Synthetic {count}-shot script',
                summary='Explicit offline batch storyboard', structured=structure)
    pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='approved')


def set_material_model(scene, model):
    saved = scene[0].get('/api/connections').json()
    saved['roles']['material'] = {'connection': 'codex', 'model': model}
    response = scene[0].put('/api/connections', json={
        'expected_revision': saved['revision'], 'connections': saved['connections'],
        'roles': saved['roles']})
    assert response.status_code == 200, response.text
    return response.json()['revision']


def test_saved_material_model_reaches_single_image_request_and_ledger(scene):
    revision = set_material_model(scene, 'gpt-6-sol')
    d = ready(scene, 'image')
    cap = workspace(scene)['capabilities']['image']
    assert cap['controller_model'] == 'gpt-6-sol'
    assert cap['connection_revision'] == revision
    assert wait(scene, action(scene, d, 'generate', expected_connection_revision=revision))['status'] == 'succeeded'
    assert scene[4].calls[0].model == 'gpt-6-sol'
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.model == 'gpt-6-sol' and row.actual_model == 'fixture-codex'


def test_image_generation_refuses_changed_model_before_submission(scene):
    old_revision = workspace(scene)['capabilities']['image']['connection_revision']
    d = ready(scene, 'image')
    set_material_model(scene, 'gpt-6-sol')
    response = action(scene, d, 'generate', expected_connection_revision=old_revision)
    assert response.status_code == 409
    assert not scene[4].calls
    assert not Ledger(scene[1], scene[-1]).entries()


def test_selected_image_model_requires_a_fresh_confirmation_page(scene):
    set_material_model(scene, 'gpt-6-sol')
    d = ready(scene, 'image')
    assert action(scene, d, 'generate').status_code == 409
    assert not scene[4].calls


def test_saved_material_model_reaches_every_image_in_batch(scene):
    adopt_shot_count(scene, 2)
    revision = set_material_model(scene, 'gpt-6-sol')
    first = draft(scene, kind='image', request_id='model-shot-1', shot_id='shot-01').json()
    second = draft(scene, kind='image', request_id='model-shot-2', shot_id='shot-02').json()
    payload = {**batch_payload(scene, [first, second], 'model-batch'),
               'expected_connection_revision': revision}
    assert scene[0].post(BASE + '/batch-generate', json={
        key: value for key, value in payload.items() if key != 'expected_connection_revision'}).status_code == 409
    assert not scene[4].calls
    assert wait(scene, scene[0].post(BASE + '/batch-generate', json=payload))['status'] == 'succeeded'
    assert [request.model for request in scene[4].calls] == ['gpt-6-sol', 'gpt-6-sol']


def test_one_batch_decision_generates_pinned_images_once_and_approves_exact_outputs(scene):
    http, store, _, _, images, videos, _ = scene
    adopt_shot_count(scene, 2)
    first = draft(scene, kind='image', request_id='shot-1', shot_id='shot-01').json()
    second = draft(scene, kind='image', request_id='shot-2', shot_id='shot-02', prompt='A different synthetic shot.').json()
    payload = batch_payload(scene, [first, second])
    assert http.post(BASE + '/batch-generate', json={**payload, 'consent': False}).status_code == 409
    assert http.post(BASE + '/batch-generate', json={**payload, 'drafts': [
        payload['drafts'][0], {**payload['drafts'][1], 'expected_sha256': '0' * 64}]}).status_code == 409
    assert not images.calls
    job = wait(scene, http.post(BASE + '/batch-generate', json=payload))
    assert job['status'] == 'succeeded'
    assert len(images.calls) == 2 and not videos.calls
    repeated = http.post(BASE + '/batch-generate', json=payload)
    assert repeated.status_code == 200 and repeated.json()['job_id'] == job['job_id']
    assert len(images.calls) == 2
    outputs = [next(item for item in workspace(scene)['drafts'] if item['draft_id'] == shot['draft_id'])
               ['attempts'][0]['output'] for shot in (first, second)]
    approval = {'request_id': 'approve-batch-1', 'nonce': workspace(scene)['nonce'],
                'images': [{'name': output['name'], 'sha256': output['sha256']} for output in outputs]}
    assert http.post(BASE + '/material-approvals/batch', json={**approval, 'images': [
        approval['images'][0], {'name': 'reference.png', 'sha256': workspace(scene)['materials'][0]['sha256']}]}).status_code == 409
    assert not any(item['approved'] for item in workspace(scene)['materials'] if item['name'] in {o['name'] for o in outputs})
    saved = http.post(BASE + '/material-approvals/batch', json=approval)
    assert saved.status_code == 200 and saved.json()['approved'] == 2
    assert all(item['approved'] for item in workspace(scene)['materials'] if item['name'] in {o['name'] for o in outputs})
    assert http.post(BASE + '/material-approvals/batch', json=approval).json()['already_saved'] is True
    assert len([event for event in store.read_events(RUN) if event.get('kind') == 'production_batch_approval']) == 1


def test_five_approved_frames_prepare_five_clips_then_generate_and_assemble_in_order(scene, monkeypatch):
    http, store, _, _, images, videos, _ = scene
    video_goal(scene)
    adopt_shot_count(scene, 5)
    image_drafts = [draft(scene, kind='image', request_id=f'frame-{index}',
                          shot_id=f'shot-{index:02d}', prompt=f'Synthetic frame {index}.').json() for index in range(1, 6)]
    source = workspace(scene)['sources'][-1]
    plan_payload = {'request_id': 'five-scene-edit', 'source_version': source['version'],
        'source_sha256': source['artifact_sha256'], 'confirmed_product': 'Small deep olive green bag',
        'cuts': [
            {'clip_index': 1, 'start_seconds': 0, 'audio_clip_index': 1,
             'audio_start_seconds': 0, 'duration_seconds': 1},
            {'clip_index': 2, 'start_seconds': 0, 'audio_clip_index': 1,
             'audio_start_seconds': 1, 'duration_seconds': 1},
            {'clip_index': 1, 'start_seconds': 1, 'audio_clip_index': 1,
             'audio_start_seconds': 2, 'duration_seconds': 1},
            *[{'clip_index': index, 'start_seconds': 0, 'audio_clip_index': index,
               'audio_start_seconds': 0, 'duration_seconds': 1} for index in (3, 4, 5)]],
        'overlays': [{'text': 'one bag.', 'start_seconds': 0, 'duration_seconds': 2}]}
    plan_response = http.post(BASE + '/edit-plan', json=plan_payload)
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    assert workspace(scene)['edit_plan']['plan_sha256'] == plan['plan_sha256']
    assert http.post(BASE + '/edit-plan', json={**plan_payload, 'confirmed_product': 'Other'}).status_code == 409
    assert wait(scene, http.post(BASE + '/batch-generate', json=batch_payload(scene, image_drafts)))['status'] == 'succeeded'
    outputs = [next(d for d in workspace(scene)['drafts'] if d['draft_id'] == image['draft_id'])
               ['attempts'][0]['output'] for image in image_drafts]
    assert all(not shot['approved'] for shot in workspace(scene)['video_sequence'])
    calls, prompts = [], []
    def prepare(run_id):
        calls.append(run_id)
        def run(text, emit, check_cancel):
            prompts.append(text)
            source = workspace(scene)['sources'][-1]
            for index, output in enumerate(outputs, 1):
                payload = DraftIn(request_id=f'clip-{index}', kind='video',
                    prompt=f'Synthetic segment {index}.', source_version=source['version'],
                    source_sha256=source['artifact_sha256'],
                    shot_id=f'shot-{index:02d}',
                    reference={'name': output['name'], 'sha256': output['sha256']},
                    model='wan3.0-video', duration=3, resolution='720P')
                prepare_draft(store, scene[2], run_id, payload, actor='agent')
            return OrchestratorTurn('Prepared five synthetic clips.', 'done', 1)
        return run, {'source': 'offline-five-clip-continuation'}
    http.app.state.jobs.prepare = prepare
    approval = {'request_id': 'approve-five', 'nonce': workspace(scene)['nonce'],
                'images': [{'name': output['name'], 'sha256': output['sha256']} for output in outputs]}
    saved = http.post(BASE + '/material-approvals/batch', json=approval)
    assert saved.status_code == 200, saved.text
    job_id = saved.json()['continuation']['job_id']
    assert wait(scene, http.get(f'/api/runs/{RUN}/jobs/{job_id}'))['status'] == 'succeeded'
    assert calls == [RUN] and not videos.calls
    assert '第 1 段原片至少 3 秒' in prompts[0]
    assert 'Small deep olive green bag' in prompts[0]
    assert http.post(BASE + '/material-approvals/batch/continue', json={
        'request_id': 'partial-video-prepare', 'images': approval['images'][:2]}).status_code == 409
    assert [shot['video_draft_id'] for shot in workspace(scene)['video_sequence']] == [f'clip-{i}' for i in range(1, 6)]
    repeated = http.post(BASE + '/material-approvals/batch', json=approval)
    assert repeated.json()['already_saved'] is True and calls == [RUN]
    clips = [next(d for d in workspace(scene)['drafts'] if d['draft_id'] == f'clip-{i}') for i in range(1, 6)]
    wrong_order = batch_payload(scene, clips[::-1], 'videos-wrong')
    assert http.post(BASE + '/batch-generate', json=wrong_order).status_code == 409
    assert not videos.calls
    generated = wait(scene, http.post(BASE + '/batch-generate', json=batch_payload(scene, clips, 'videos-five')))
    assert generated['status'] == 'succeeded' and len(videos.calls) == 5
    assert [call.image_sha256 for call in videos.calls] == [output['sha256'] for output in outputs]
    assembled_inputs = []
    def assemble_fixture(paths, cuts, overlays):
        assembled_inputs.extend(path.name for path in paths)
        assert cuts == plan_payload['cuts']
        assert overlays == plan_payload['overlays']
        return b'Fixture assembled video bytes only'
    monkeypatch.setattr('studio.production.assemble_videos', assemble_fixture)
    state = workspace(scene)
    selected = [next(d for d in state['drafts'] if d['draft_id'] == f'clip-{i}') for i in range(1, 6)]
    assembly_clips = [{'draft_id': d['draft_id'], 'draft_sha256': d['draft_sha256'],
                       'attempt_id': d['attempts'][0]['attempt_id'], **d['attempts'][0]['output']}
                      for d in selected]
    for clip in assembly_clips:
        clip.pop('url')
    assembly_payload = {'edit_plan_id': plan['request_id'], 'edit_plan_sha256': plan['plan_sha256']}
    assert http.post(BASE + '/assemble', json={'request_id': 'wrong-order',
        **assembly_payload, 'clips': assembly_clips[::-1]}).status_code == 409
    completed = wait(scene, http.post(BASE + '/assemble', json={'request_id': 'assemble-five',
        **assembly_payload, 'clips': assembly_clips}))
    assert completed['status'] == 'succeeded'
    assert assembled_inputs == [clip['name'] for clip in assembly_clips]
    assembly = workspace(scene)['assemblies'][-1]
    assert assembly['clips'] == assembly_clips
    assert assembly['edit_plan_sha256'] == plan['plan_sha256']
    assert http.get(assembly['output']['url']).content == b'Fixture assembled video bytes only'
    assert len(videos.calls) == 5


def test_local_assembly_normalizes_video_and_missing_audio(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and FFprobe are not installed')
    first, second = tmp_path / 'first.mp4', tmp_path / 'second.mp4'
    commands = [
        [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:r=30:d=0.4',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.4', '-shortest',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(first)],
        [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=96x64:r=24:d=0.4',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(second)],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    assembled = assemble_videos([first, second])
    output = tmp_path / 'full.mp4'
    output.write_bytes(assembled)
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(output)],
                           capture_output=True, timeout=60)
    assert probe.returncode == 0
    streams = json.loads(probe.stdout)['streams']
    assert {'video', 'audio'} <= {stream['codec_type'] for stream in streams}
    video = next(stream for stream in streams if stream['codec_type'] == 'video')
    assert (video['width'], video['height']) == (720, 1280)


def test_local_assembly_uses_explicit_picture_audio_cuts_and_overlay(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and FFprobe are not installed')
    first, second = tmp_path / 'first.mp4', tmp_path / 'second.mp4'
    for color, frequency, path in [('red', 440, first), ('blue', 660, second)]:
        result = subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            f'color=c={color}:s=64x64:r=30:d=0.8', '-f', 'lavfi', '-i',
            f'sine=frequency={frequency}:duration=0.8', '-shortest',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)],
            capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
    cuts = [{'clip_index': 1, 'start_seconds': 0, 'audio_clip_index': 1,
             'audio_start_seconds': 0, 'duration_seconds': .2},
            {'clip_index': 2, 'start_seconds': 0, 'audio_clip_index': 1,
             'audio_start_seconds': .2, 'duration_seconds': .2},
            {'clip_index': 1, 'start_seconds': .4, 'audio_clip_index': 1,
             'audio_start_seconds': .4, 'duration_seconds': .2}]
    output = tmp_path / 'edited.mp4'
    output.write_bytes(assemble_videos([first, second], cuts,
        [{'text': 'one bag.', 'start_seconds': 0, 'duration_seconds': .4}]))
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(output)], capture_output=True, timeout=60)
    assert probe.returncode == 0
    assert .55 <= float(probe.stdout.strip()) <= .8


def test_unknown_first_image_blocks_the_remaining_batch_until_user_accepts_uncertainty(scene, monkeypatch):
    http, store, _, _, images, _, settings = scene
    assert 'retry_with_unknown_cost' in workspace(scene)['recovery_modes']
    adopt_shot_count(scene, 2)
    first = draft(scene, kind='image', request_id='shot-1', shot_id='shot-01').json()
    second = draft(scene, kind='image', request_id='shot-2', shot_id='shot-02', prompt='Second synthetic shot.').json()
    original = images.generate
    def unknown_once(request):
        if not images.calls:
            images.calls.append(request)
            return ImageResult(status='unknown', images=(), usage=Usage(100, 20),
                observed_image_calls=0, provider_invoked=True, thread_id='fixture-thread',
                turn_id='fixture-turn', model='fixture-codex')
        return original(request)
    monkeypatch.setattr(images, 'generate', unknown_once)
    job = wait(scene, http.post(BASE + '/batch-generate', json=batch_payload(scene, [first, second])))
    assert job['status'] == 'blocked' and len(images.calls) == 1
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'unknown' and attempt['provider_outcome'] == 'unknown'
    assert http.post(BASE + '/batch-generate', json=batch_payload(scene, [second], 'skip-first')).status_code == 409
    assert len(images.calls) == 1
    assert http.post(BASE + f'/attempts/{attempt["attempt_id"]}/recover', json={
        'nonce': workspace(scene)['nonce'], 'retry_with_unknown_cost': True,
        'evidence_note': 'Synthetic user accepts old result and cost as unknown.'}).status_code == 200
    assert workspace(scene)['drafts'][0]['attempts'][0]['status'] == 'retry_authorized'
    assert Ledger(store, settings).entries()[-1].usage_status == 'unknown'
    assert len(images.calls) == 1, 'the recovery decision itself does not generate'
    resumed = wait(scene, http.post(BASE + '/batch-generate', json=batch_payload(scene, [first, second], 'new-batch')))
    assert resumed['status'] == 'succeeded' and len(images.calls) == 3


def test_completed_codex_turn_without_image_call_is_a_definite_failed_attempt(scene, monkeypatch):
    images = scene[4]
    def no_image(request):
        images.calls.append(request)
        return ImageResult(status='failed', images=(), usage=Usage(100, 20),
            observed_image_calls=0, provider_invoked=True, thread_id='fixture-thread',
            turn_id='fixture-turn', model='fixture-codex')
    monkeypatch.setattr(images, 'generate', no_image)
    shot = draft(scene, kind='image', request_id='no-image').json()
    job = wait(scene, action(scene, shot, 'generate'))
    assert job['status'] == 'blocked'
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'failed' and attempt['observed_image_calls'] == 0
    row = Ledger(scene[1], scene[-1]).entries()[-1]
    assert row.usage_status == 'known' and row.usage_quantity == 0


def ready(scene, kind='video'):
    d = draft(scene, kind).json()
    assert approve(scene).status_code == 200
    return d


def test_preparation_is_free_immutable_and_retry_safe(scene):
    response = draft(scene); assert response.status_code == 200, response.text
    assert draft(scene).json()['draft_sha256'] == response.json()['draft_sha256']
    assert draft(scene, prompt='Different').status_code == 409
    assert not scene[3].calls and not scene[4].calls and not scene[5].calls
    assert not Ledger(scene[1], scene[-1]).entries()


def test_generation_requires_exact_source_and_human_picture_approval(scene):
    d = draft(scene).json()
    assert action(scene, d, 'generate').status_code == 409
    assert action(scene, d, 'review').status_code == 410
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


def test_successful_output_never_stales_the_script_that_authorized_it(scene):
    """CLAUDE.md red line 7: a derived output is not a new source fact.

    Generation used to advance the evidence generation and enter the fingerprint
    while the production baseline excluded it, so one successful run left the
    approved script permanently stale and impossible to re-confirm.
    """
    from studio.evidence import evidence_generation

    store, pipe = scene[1], scene[2]
    d = ready(scene)
    frozen = int(pipe.stage_state(RUN, 'writing').latest.inputs['evidence:generation'])
    assert not pipe.stage_state(RUN, 'writing').stale
    assert wait(scene, action(scene, d, 'generate'))['status'] == 'succeeded'
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'succeeded'
    assert evidence_generation(store, RUN) == frozen
    state = pipe.stage_state(RUN, 'writing')
    assert not state.stale and state.status == 'approved' and not state.can_approve
    # The generated file is still offered as a usable material, not hidden.
    generated = next(m for m in workspace(scene)['materials'] if m['name'] == attempt['output']['name'])
    assert generated['url'] and generated['sha256'] == attempt['output']['sha256']


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


def test_a_failed_image_probe_is_retried_not_cached_for_the_process(scene, monkeypatch):
    """One hiccup must not disable image generation until the service restarts."""
    service, images = scene[0].app.state.production, scene[4]
    calls, real = [], images.capability

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise CodexUnavailable('fixture CLI is busy')
        return real()

    monkeypatch.setattr(images, 'capability', flaky)
    d = ready(scene, 'image')
    blocked = action(scene, d, 'generate')
    assert blocked.status_code == 409 and '图片连接暂未通过能力检查' in blocked.text
    assert 'CodexUnavailable' in blocked.text
    assert len(calls) == 1
    # The window stands between renders, then the probe runs again and works.
    monkeypatch.setattr('studio.production.IMAGE_PROBE_RETRY_SECONDS', 0)
    assert wait(scene, action(scene, d, 'generate'))['status'] == 'succeeded'
    assert len(calls) == 2 and images.calls


def test_workspace_probe_in_progress_returns_a_valid_capability(scene, monkeypatch):
    service, images = scene[0].app.state.production, scene[4]
    entered, release = threading.Event(), threading.Event()
    real = images.capability

    def slow_probe():
        entered.set()
        assert release.wait(2)
        return real()

    monkeypatch.setattr(images, 'capability', slow_probe)
    worker = threading.Thread(target=service.image_probe)
    worker.start()
    try:
        assert entered.wait(2)
        pending = service.capabilities()['image']
        assert pending['available'] is False
        assert pending['verified'] is False
        assert '正在检查' in pending['reason']
    finally:
        release.set()
        worker.join(2)
    assert service.capabilities()['image']['available'] is True


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


def test_retired_review_does_not_invoke_model_or_block_generation(scene):
    scene[1].append_chat(RUN, 'assistant', 'PRIVATE_PRODUCER_SELF_PRAISE')
    scene[3].verdict = 'revise'
    d = draft(scene).json(); assert action(scene, d, 'review').status_code == 410; approve(scene)
    assert not scene[3].calls
    assert wait(scene, action(scene, d, 'generate'))['status'] == 'succeeded'


def test_failed_provider_does_not_fabricate_zero_usage(scene):
    scene[5].status = 'FAILED'
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    a = workspace(scene)['drafts'][0]['attempts'][0]
    assert a['status'] == 'failed' and a['usage_status'] == 'unknown'


def done_text(item):
    return next(event for event in item['events'] if event['type'] == 'done')['text']


def test_failed_generation_is_not_reported_as_a_finished_round(scene):
    """CLAUDE.md red line 4: a round that ran is not a round that delivered."""
    scene[5].status = 'FAILED'
    d = ready(scene)
    item = wait(scene, action(scene, d, 'generate'))
    assert item['status'] == 'blocked' and 'incomplete' in item['reason']
    attempt = workspace(scene)['drafts'][0]['attempts'][0]
    assert attempt['status'] == 'failed'
    assert done_text(item) == attempt['message']


def test_partial_image_result_is_not_reported_as_a_finished_round(scene):
    scene[4].result_status = 'unknown'
    d = ready(scene, 'image')
    item = wait(scene, action(scene, d, 'generate'))
    assert item['status'] == 'blocked'
    assert workspace(scene)['drafts'][0]['attempts'][0]['status'] == 'unknown'


def test_unexpected_provider_error_records_only_its_class_name(scene):
    """The journal is served to the page; a provider error can carry a URL."""
    scene[5].error = ValueError('https://signed.example.invalid/?token=SECRET')
    d = ready(scene)
    item = wait(scene, action(scene, d, 'generate'))
    assert item['status'] == 'blocked'
    assert 'ValueError' in done_text(item)
    assert 'SECRET' not in json.dumps(item)


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
    # Latest writing version is not approved; production requires an approved script.
    attempt = pipe.start_attempt(RUN, 'writing')
    pipe.submit(RUN, 'writing', attempt.token, content='Unapproved rewrite', summary='rewrite',
                structured=stage_structure('writing'))
    assert pipe.stage_state(RUN, 'writing').approval is None
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


def test_frozen_video_parameters_match_submission(scene):
    d = ready(scene); wait(scene, action(scene, d, 'generate'))
    assert d['parameters'] == {'audio': True, 'ratio': 'adaptive', 'prompt_extend': False}
    assert not scene[3].calls
    request = scene[5].calls[0]
    assert request.audio is True and request.ratio == 'adaptive' and request.prompt_extend is False
