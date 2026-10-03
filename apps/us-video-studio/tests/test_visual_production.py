"""Actual HTTP production boundaries, with synthetic pixels and counted fakes."""
import hashlib
import threading
from copy import deepcopy

import pytest
from local_http import TestClient

from studio.agents.codex_client import CodexUnavailable
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.production import DraftIn, digest, prepare_draft
from studio.server import build_app
from studio.store import Store
from test_production import BASE, RUN, Images, Reviewer, Videos, action, png, wait, workspace
from test_server import settings_for
from visual_fixtures import stage_structure, visual_design


def cast_design():
    design = visual_design()
    design['characters'] = [
        {'id': 'ava', 'label': 'Ava', 'relationship': 'A fictional US office commuter sharing a lunch routine.',
         'appearance': 'Adult with short curly hair; preserve the same face and build across shots.',
         'wardrobe': 'Plain navy cardigan and cream T-shirt throughout this story.',
         'performance': 'Relaxed direct address with small practical hand actions.'},
        {'id': 'jordan', 'label': 'Jordan', 'relationship': 'A fictional coworker in the same office.',
         'appearance': 'Adult with cropped hair; preserve a distinct face from Ava.',
         'wardrobe': 'Plain gray sweatshirt throughout this story.',
         'performance': 'Quiet background action, never speaks Ava’s lines.'},
    ]
    return design


@pytest.fixture
def visual_scene(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline visual production boundary', 'Synthetic US creator story only.')
    for name in ('product.png', 'character.png', 'scene.png'):
        accept_upload(store, RUN, name, png())
    pipe = Pipeline(store)
    for stage in ('intake', 'direction', 'material_plan', 'writing'):
        structure = stage_structure(stage)
        if stage == 'direction':
            structure = {'options': [{'key': 'A', 'claim': 'Synthetic creator story', 'satisfies': 'goal.primary'}]}
        elif stage == 'material_plan':
            structure['visual_design'] = cast_design()
        elif stage == 'writing':
            first = structure['segments'][0]
            first.update(character_ids='ava', scene_id='table',
                         picture='Ava places the lunch bag on the table.', line='This is my lunch setup.')
            second = {**first, 'tc': '0:05–0:10', 'picture': 'Ava opens the bag while Jordan sits nearby.',
                      'character_ids': 'ava,jordan', 'line': 'Everything goes in here.'}
            structure['segments'].append(second)
        attempt = pipe.start_attempt(RUN, stage)
        pipe.submit(RUN, stage, attempt.token, content='Synthetic ' + stage,
                    summary='Offline fixture', structured=structure)
        if stage in {'direction', 'writing'}:
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage), decision='approved',
                                 selected_option_id='A' if stage == 'direction' else None)
    settings = settings_for(tmp_path)
    reviewer, images, videos = Reviewer(), Images(), Videos()
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'OFFLINE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    monkeypatch.setattr('studio.production.validate_video', lambda raw: None)
    app = build_app(settings, client_factory=lambda: reviewer,
                    production_image_factory=lambda: images, production_video_factory=lambda: videos,
                    production_poll_delay=0, production_poll_count=1)
    with TestClient(app) as http:
        yield http, store, pipe, reviewer, images, videos, settings


def refs(scene):
    materials = {row['name']: row for row in workspace(scene)['materials']}
    return [{'name': name, 'sha256': materials[name]['sha256'], 'purpose': purpose,
             **({'design_id': identity} if identity else {})}
            for name, purpose, identity in (
                ('product.png', 'product', None), ('character.png', 'character', 'ava'),
                ('scene.png', 'scene', 'table'))]


def save(scene, request_id='visual-frame', kind='image', **over):
    source = workspace(scene)['sources'][-1]
    payload = {'request_id': request_id, 'kind': kind,
               'prompt': 'Hold a natural creator-camera angle and show the product clearly.',
               'source_version': source['version'], 'source_sha256': source['artifact_sha256'],
               'shot_id': 'shot-01', 'model': 'wan3.0-video' if kind == 'video' else 'codex-image',
               **({'duration': 5, 'resolution': '720P'} if kind == 'video' else {})}
    return scene[0].post(BASE + '/drafts', json={**payload, **over})


def approve_frame(scene, reference):
    result = scene[0].post(BASE + '/material-approvals', json={
        'name': reference['name'], 'sha256': reference['sha256'],
        'approved': True, 'nonce': workspace(scene)['nonce']})
    assert result.status_code == 200, result.text


def generated_frame(scene):
    saved = save(scene)
    assert saved.status_code == 200, saved.text
    item = saved.json()
    assert wait(scene, action(scene, item, 'generate', 'generate-current-frame'))['status'] == 'succeeded'
    current = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == item['draft_id'])
    reference = current['attempts'][-1]['output']
    approve_frame(scene, reference)
    return item, {key: reference[key] for key in ('name', 'sha256')}


def test_saved_input_binds_shared_design_and_only_the_selected_shot(visual_scene):
    scene = visual_scene
    reference_list = refs(scene)
    result = save(scene, references=reference_list)
    assert result.status_code == 200, result.text
    item = result.json()
    assert item['prompt'] == item['compiled_prompt'] != item['shot_prompt']
    assert item['request_payload']['prompt'] == item['shot_prompt']
    assert item['references'] == reference_list
    assert item['design_binding']['character_ids'] == ['ava']
    assert item['design_binding']['scene_id'] == 'table'
    assert item['design_binding']['material_plan'] == {
        'version': 1, 'sha256': scene[1].latest_artifact(RUN, 'material_plan').sha256}
    assert item['shot_binding']['character_ids'] == ['ava']
    assert item['shot_binding']['shot_id'] == 'shot-01'
    assert 'navy cardigan' in item['prompt'] and 'US apartment' in item['prompt']
    assert 'gray sweatshirt' not in item['prompt']
    assert 'Do not inherit the supplier model' in item['prompt']
    assert item['estimate']['input_sha256'] == digest({'prompt': item['prompt'], 'references': reference_list})
    state = workspace(scene)
    assert state['visual_design_status']['ready']
    assert state['drafts'][0]['design_labels']['characters'] == [{'id': 'ava', 'label': 'Ava'}]
    assert state['drafts'][0]['design_labels']['scene']['id'] == 'table'
    assert not scene[4].calls and not scene[5].calls


def test_multi_reference_pixels_and_frozen_prompt_reach_image_provider(visual_scene, monkeypatch):
    scene = visual_scene
    reference_list = refs(scene)
    item = save(scene, references=reference_list).json()
    seen = []
    original = scene[4].generate
    def capture(request):
        seen.append([(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                     for path in request.reference_paths])
        return original(request)
    monkeypatch.setattr(scene[4], 'generate', capture)
    job = wait(scene, action(scene, item, 'generate', 'generate-three-references'))
    assert job['status'] == 'succeeded', job
    assert len(scene[4].calls) == 1 and not scene[5].calls
    request = scene[4].calls[0]
    assert request.prompt == item['compiled_prompt']
    assert [row[1] for row in seen[0]] == [row['sha256'] for row in reference_list]
    assert [row[0] for row in seen[0]] == [
        'reference-01-product.png', 'reference-02-character.png', 'reference-03-scene.png']


def test_legacy_single_reference_normalizes_and_video_sends_one_continuity_frame(visual_scene):
    scene = visual_scene
    reference = {key: refs(scene)[0][key] for key in ('name', 'sha256')}
    image = save(scene, reference=reference).json()
    assert image['references'] == [{**reference, 'purpose': 'product'}]
    approve_frame(scene, reference)
    video = save(scene, 'visual-clip', kind='video', reference=reference).json()
    assert video['references'] == [{**reference, 'purpose': 'continuity'}]
    job = wait(scene, action(scene, video, 'generate', 'generate-one-first-frame'))
    assert job['status'] == 'succeeded', job
    assert len(scene[5].calls) == 1 and not scene[4].calls
    request = scene[5].calls[0]
    assert request.prompt == video['compiled_prompt']
    assert request.image_sha256 == reference['sha256']


@pytest.mark.parametrize('over,notice', [
    ({'shot_id': None}, 'shot_id'),
    ({'shot_id': 'shot-99'}, 'shot_id'),
    ({'references': [{'name': 'character.png', 'purpose': 'character', 'design_id': 'jordan'}]}, '身份'),
    ({'references': [{'name': 'scene.png', 'purpose': 'scene', 'design_id': 'different'}]}, '身份'),
])
def test_unresolved_shot_or_wrong_reference_identity_blocks_preparation(visual_scene, over, notice):
    scene = visual_scene
    over = deepcopy(over)
    for row in over.get('references', []):
        row['sha256'] = next(ref['sha256'] for ref in refs(scene) if ref['name'] == row['name'])
    response = save(scene, **over)
    assert response.status_code == 409 and notice in response.text
    assert not scene[4].calls and not scene[5].calls


def test_video_rejects_supplemental_design_pictures_instead_of_omitting_them(visual_scene):
    scene = visual_scene
    reference_list = refs(scene)
    reference_list[0]['purpose'] = 'continuity'
    response = save(scene, kind='video', references=reference_list)
    assert response.status_code == 409 and '一张' in response.text
    assert not scene[5].calls


def test_revised_image_head_invalidates_old_video_before_provider(visual_scene):
    scene = visual_scene
    image, reference = generated_frame(scene)
    video = save(scene, 'approved-clip', kind='video', reference=reference).json()
    replacement = save(scene, 'revised-image', supersedes=image['draft_id'], prompt='Change the table composition.')
    assert replacement.status_code == 200, replacement.text
    response = action(scene, video, 'generate', 'old-frame-video')
    assert response.status_code == 409 and '首帧' in response.text
    state = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == video['draft_id'])
    assert not state['source_ready'] and '首帧' in state['design_issue']
    assert len(scene[4].calls) == 1 and not scene[5].calls
    assert scene[0].get(f'/api/runs/{RUN}/materials/{reference["name"]}').status_code == 200


def test_first_frame_head_is_rechecked_at_actual_video_submit(visual_scene, monkeypatch):
    scene = visual_scene
    image, reference = generated_frame(scene)
    video = save(scene, 'approved-clip', kind='video', reference=reference).json()
    service = scene[0].app.state.production
    original = service.submit_video
    def revise_before_submit(*args, **kwargs):
        source = scene[1].latest_artifact(RUN, 'writing')
        prepare_draft(scene[1], scene[2], RUN, DraftIn(
            request_id='image-changed-at-boundary', kind='image', prompt='New current composition.',
            source_version=source.version, source_sha256=source.sha256,
            shot_id='shot-01', supersedes=image['draft_id']), actor='agent')
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'submit_video', revise_before_submit)
    job = wait(scene, action(scene, video, 'generate', 'frame-boundary-change'))
    assert job['status'] == 'failed' and '首帧' in job['reason']
    assert len(scene[4].calls) == 1 and not scene[5].calls
    attempt = service.attempts(RUN)['frame-boundary-change']
    assert attempt['status'] == 'failed' and attempt['usage_status'] == 'known'


@pytest.mark.parametrize('error', [CodexUnavailable('Synthetic definite failure'),
                                 RuntimeError('Synthetic unknown image attempt')])
def test_latest_image_retry_cannot_reuse_old_success_as_current_first_frame(visual_scene, error):
    scene = visual_scene
    image, reference = generated_frame(scene)
    video = save(scene, 'previously-approved-clip', kind='video', reference=reference).json()
    scene[4].error = error
    assert wait(scene, action(scene, image, 'generate', 'retry-current-image'))['status'] == 'blocked'
    response = action(scene, video, 'generate', 'video-from-previous-output')
    assert response.status_code == 409 and '原任务' in response.text
    assert len(scene[4].calls) == 2 and not scene[5].calls
    state = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == video['draft_id'])
    assert not state['can_generate'] and '首帧' in state['design_issue']


def test_legacy_unbound_draft_is_readable_without_inheriting_new_design(visual_scene):
    scene = visual_scene
    item = save(scene).json()
    old = {key: value for key, value in item.items() if key not in {
        'draft_sha256', 'references', 'compiled_prompt', 'design_binding', 'shot_binding',
        'shot_prompt', 'event_seq', 'attempts', 'review', 'is_current', 'superseded_by', 'shot_revision'}}
    old.update(draft_id='legacy-unbound', prompt='Old unbound human-readable draft.', shot_id='shot-02')
    old['draft_sha256'] = digest(old)
    scene[1].append_event(RUN, 'production_draft', draft=old)
    before = list(scene[1].read_events(RUN))
    projection = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == old['draft_id'])
    assert projection['prompt'] == old['prompt'] and '历史' in projection['design_issue']
    assert not projection['source_ready'] and not projection['can_generate']
    assert 'design_binding' not in projection
    assert scene[1].read_events(RUN) == before
    response = action(scene, old, 'generate', 'legacy-denied')
    assert response.status_code == 409 and '历史' in response.text
    assert not scene[4].calls and not scene[5].calls


def test_design_change_refuses_frozen_worker_without_media_calls(visual_scene, monkeypatch):
    scene = visual_scene
    item = save(scene, references=refs(scene)).json()
    service = scene[0].app.state.production
    entered, release = threading.Event(), threading.Event()
    original = service.prepare_generation
    def pause_worker(*args, **kwargs):
        runner, config = original(*args, **kwargs)
        def run(text, emit, check_cancel):
            entered.set()
            assert release.wait(5)
            return runner(text, emit, check_cancel)
        return run, config
    monkeypatch.setattr(service, 'prepare_generation', pause_worker)
    response = action(scene, item, 'generate', 'design-race')
    assert response.status_code == 200, response.text
    try:
        assert entered.wait(5)
        attempt = scene[2].start_attempt(RUN, 'material_plan')
        structure = stage_structure('material_plan')
        design = cast_design()
        design['characters'][0]['wardrobe'] = 'A plain forest-green cardigan in every shot.'
        structure['visual_design'] = design
        scene[2].submit(RUN, 'material_plan', attempt.token, content='Synthetic revised design',
                        summary='Changed wardrobe', structured=structure)
    finally:
        release.set()
    job = wait(scene, response)
    assert job['status'] == 'failed' and '依据' in job['reason']
    assert not scene[4].calls and not scene[5].calls


def test_missing_visual_design_blocks_new_input_and_explains_the_gap(visual_scene):
    scene = visual_scene
    material = scene[1].latest_artifact(RUN, 'material_plan')
    # Simulate an existing prose-only artifact; do not migrate or replace it.
    scene[1].write_artifact(RUN, 'material_plan', material.version + 1, 'Legacy readable material plan',
                           summary='Legacy plan without shared design', evidence=[], limitations=[],
                           author_role='material', attempt='legacy-fixture-only',
                           inputs=material.inputs, structured={})
    state = workspace(scene)
    assert not state['visual_design_status']['ready']
    response = save(scene)
    assert response.status_code == 409 and '人物与场景' in response.text
    assert not scene[4].calls and not scene[5].calls


def test_production_projection_reuses_one_stage_snapshot_for_all_shots(visual_scene, monkeypatch):
    scene = visual_scene
    assert save(scene, 'snapshot-shot-1').status_code == 200
    assert save(scene, 'snapshot-shot-2', shot_id='shot-02').status_code == 200
    import studio.evidence as evidence
    original = evidence.evidence_fingerprint
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(evidence, 'evidence_fingerprint', counted)
    first = workspace(scene)
    assert len(calls) == 1
    assert len(first['drafts']) == 2
    assert all(row['source_ready'] and row['can_generate'] for row in first['drafts'])
    second = workspace(scene)
    assert len(calls) == 2  # A new read must rebuild from actual state.
    assert second['drafts'] == first['drafts']
    assert second['targets'] == first['targets']
    assert second['shots'] == first['shots']
    assert not scene[4].calls and not scene[5].calls


def test_next_projection_rechecks_design_integrity_after_prior_ready_read(visual_scene):
    scene = visual_scene
    assert save(scene, 'snapshot-integrity').status_code == 200
    first = workspace(scene)
    assert first['visual_design_status']['ready']
    assert first['drafts'][0]['can_generate']
    path = scene[1].run_dir(RUN) / 'artifacts' / 'material_plan-v1.md'
    path.write_text(path.read_text(encoding='utf-8') + '\nFixture tampering\n', encoding='utf-8')
    second = workspace(scene)
    assert not second['visual_design_status']['ready']
    assert second['drafts'][0]['design_issue']
    assert not second['drafts'][0]['source_ready']
    assert not second['drafts'][0]['can_generate']
    assert not scene[4].calls and not scene[5].calls
