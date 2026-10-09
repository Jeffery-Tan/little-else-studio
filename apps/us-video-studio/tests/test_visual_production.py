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
from studio.visual_design import compile_visual_input, validate_visual_binding
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
def visual_scene(tmp_path, monkeypatch, request):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline visual production boundary', 'Synthetic US creator story only.')
    test_options = getattr(request, 'param', None)
    test_format = test_options.get('format') if isinstance(test_options, dict) else test_options
    if test_format:
        store.append_event(RUN, 'creative_context_selected', selection={'intent': 'new', 'format': test_format})
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
            if test_format == 'photo':
                first['line'] = 'A spot for each utensil.\nInside the front zipper pocket.\nUtensils shown for placement.'
                structure['caption'] = 'OFFLINE_PUBLISHING_CAPTION_DO_NOT_PRINT'
                if isinstance(test_options, dict):
                    first['line'] = test_options['line']
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


LEGACY_CONTINUITY_RULE = ('Preserve the approved frame identity, product, wardrobe, setting and composition; '
                          'animate only the specified action.')


def test_image_continuity_applies_still_edits_without_claiming_reference_approval(visual_scene, monkeypatch):
    scene = visual_scene
    original = save(scene).json()
    assert wait(scene, action(scene, original, 'generate', 'unapproved-original'))['status'] == 'succeeded'
    current = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == original['draft_id'])
    reference = {key: current['attempts'][-1]['output'][key] for key in ('name', 'sha256')}
    assert not scene[0].app.state.production.approvals(RUN).get((reference['name'], reference['sha256']))
    instructions = 'Keep Ava, the navy cardigan and the table. Change to a tighter overhead composition.'
    response = save(scene, 'still-edit', supersedes=original['draft_id'], prompt=instructions,
                    references=[{**reference, 'purpose': 'continuity'}])
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['shot_prompt'] == instructions
    assert item['compiled_prompt'].endswith('Additional shot instructions:\n' + instructions)
    assert 'preserve the specified retained elements' in item['compiled_prompt']
    assert 'including composition when requested' in item['compiled_prompt']
    assert 'Create a single still image.' in item['compiled_prompt']
    assert 'approved frame' not in item['compiled_prompt'] and 'animate' not in item['compiled_prompt']
    assert item['design_binding'] == original['design_binding']
    assert item['shot_binding'] == original['shot_binding']
    assert item['references'] == [{**reference, 'purpose': 'continuity'}]
    assert len(scene[4].calls) == 1 and not scene[5].calls  # Preparation did not submit.
    seen_hashes = []
    generate = scene[4].generate
    def capture(request):
        seen_hashes.extend(hashlib.sha256(path.read_bytes()).hexdigest() for path in request.reference_paths)
        return generate(request)
    monkeypatch.setattr(scene[4], 'generate', capture)
    job = wait(scene, action(scene, item, 'generate', 'explicit-still-edit'))
    assert job['status'] == 'succeeded', job
    request = scene[4].calls[-1]
    assert request.prompt == item['compiled_prompt']
    assert seen_hashes == [reference['sha256']]
    assert len(scene[4].calls) == 2 and not scene[5].calls
    assert not any(row['kind'] == 'production_material_approval' for row in scene[1].read_events(RUN))


def test_old_image_continuity_is_readable_but_requires_explicit_new_frozen_input(visual_scene, monkeypatch):
    scene = visual_scene
    reference = {**refs(scene)[0], 'purpose': 'continuity'}
    instructions = 'Keep the same product; change the composition to an overhead view.'
    # Persist through the actual HTTP boundary using the previous compiler rule.
    with monkeypatch.context() as patch:
        patch.setattr('studio.visual_design.IMAGE_CONTINUITY_RULE', LEGACY_CONTINUITY_RULE)
        response = save(scene, 'old-continuity', references=[reference], prompt=instructions)
        assert response.status_code == 200, response.text
        old = response.json()
        assert wait(scene, action(scene, old, 'generate', 'legacy-output'))['status'] == 'succeeded'
        output = scene[0].app.state.production.attempts(RUN)['legacy-output']['output']
    before = scene[1].read_events(RUN)
    projection = next(row for row in workspace(scene)['drafts'] if row['draft_id'] == old['draft_id'])
    assert projection['prompt'] == projection['compiled_prompt'] == old['compiled_prompt']
    assert projection['draft_sha256'] == old['draft_sha256']
    assert not projection['source_ready'] and not projection['can_generate']
    assert '制作输入说明已更新或不匹配' in projection['design_issue']
    assert '绑定已变化' not in projection['design_issue']
    assert scene[0].get(f'/api/runs/{RUN}/materials/{output["name"]}').status_code == 200
    assert scene[1].read_events(RUN) == before  # Reading did not migrate the frozen record.
    denied = action(scene, old, 'generate', 'old-rule-resubmit')
    assert denied.status_code == 409 and '重新保存本镜' in denied.text
    assert len(scene[4].calls) == 1 and not scene[5].calls
    # A repeated save receipt must retain its identity, rather than silently upgrading it.
    repeated = save(scene, 'old-continuity', references=[reference], prompt=instructions)
    assert repeated.status_code == 200 and repeated.json()['draft_sha256'] == old['draft_sha256']
    replacement = save(scene, 'current-continuity', references=[reference], prompt=instructions,
                       supersedes=old['draft_id'])
    assert replacement.status_code == 200, replacement.text
    new = replacement.json()
    assert new['draft_sha256'] != old['draft_sha256'] and new['compiled_prompt'] != old['compiled_prompt']
    assert new['shot_id'] == old['shot_id'] and new['design_binding'] == old['design_binding']
    assert new['supersedes'] == old['draft_id']
    assert len(scene[4].calls) == 1 and not scene[5].calls
    assert not any(row.get('attempt_id') == 'old-rule-resubmit' for row in scene[1].read_events(RUN))


@pytest.mark.parametrize('kind,purpose', [('image', None), ('image', 'product'), ('image', 'character'),
                                          ('image', 'scene'), ('video', 'continuity')])
def test_non_continuity_images_and_video_keep_their_frozen_compiler_bytes(visual_scene, monkeypatch, kind, purpose):
    scene = visual_scene
    reference_list = [] if purpose is None else [next(
        row for row in refs(scene) if row['purpose'] == ('product' if purpose == 'continuity' else purpose))]
    if purpose == 'continuity':
        reference_list[0]['purpose'] = 'continuity'
        approve_frame(scene, reference_list[0])
    with monkeypatch.context() as patch:
        patch.setattr('studio.visual_design.IMAGE_CONTINUITY_RULE', LEGACY_CONTINUITY_RULE)
        response = save(scene, 'unchanged-input', kind=kind, references=reference_list)
        assert response.status_code == 200, response.text
        old = response.json()
    compiled = compile_visual_input(scene[1], scene[2], RUN,
        source_version=old['source_version'], source_sha256=old['source_sha256'],
        shot_id=old['shot_id'], kind=kind, shot_prompt=old['shot_prompt'], references=reference_list)
    assert compiled['compiled_prompt'].encode('utf-8') == old['compiled_prompt'].encode('utf-8')
    assert compiled['design_binding'] == old['design_binding']
    assert compiled['shot_binding'] == old['shot_binding']
    assert digest({'prompt': compiled['compiled_prompt'], 'references': reference_list}) == old['estimate']['input_sha256']
    assert next(row for row in workspace(scene)['drafts'] if row['draft_id'] == old['draft_id'])['source_ready']
    if kind == 'video':
        assert LEGACY_CONTINUITY_RULE in compiled['compiled_prompt']
    assert not scene[4].calls and not scene[5].calls


def test_video_continuity_still_requires_accurate_reference_approval(visual_scene):
    scene = visual_scene
    reference = {**refs(scene)[0], 'purpose': 'continuity'}
    response = save(scene, 'unapproved-video', kind='video', references=[reference])
    assert response.status_code == 200, response.text
    video = response.json()
    assert LEGACY_CONTINUITY_RULE in video['compiled_prompt']
    denied = action(scene, video, 'generate', 'without-frame-approval')
    assert denied.status_code == 409 and '参考图' in denied.text
    assert not scene[4].calls and not scene[5].calls
    approve_frame(scene, reference)
    assert wait(scene, action(scene, video, 'generate', 'after-frame-approval'))['status'] == 'succeeded'
    assert len(scene[5].calls) == 1 and scene[5].calls[0].prompt == video['compiled_prompt']


@pytest.mark.parametrize('field', ['prompt', 'compiled_prompt'])
def test_prompt_mismatch_does_not_claim_shared_design_changed(visual_scene, field):
    scene = visual_scene
    item = save(scene).json()
    mismatched = {**item, field: item[field] + '\nDifferent frozen instructions.'}
    before = scene[1].read_events(RUN)
    with pytest.raises(ValueError, match='制作输入说明已更新或不匹配'):
        validate_visual_binding(scene[1], scene[2], RUN, mismatched)
    assert scene[1].read_events(RUN) == before
    changed_binding = deepcopy(item)
    changed_binding['shot_binding']['scene_id'] = 'other-scene'
    with pytest.raises(ValueError, match='人物、场景或分镜绑定已变化'):
        validate_visual_binding(scene[1], scene[2], RUN, changed_binding)
    assert not scene[4].calls and not scene[5].calls


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


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
def test_photo_page_copy_contract_reaches_image_provider_without_caption_or_video_call(visual_scene):
    import json
    scene = visual_scene
    source = scene[1].latest_artifact(RUN, 'writing')
    original = source.to_dict()
    draft = save(scene, prompt='Preserve product appearance; no supplier labels.').json()
    contract = draft['compiled_prompt'].split('FINAL PHOTO PAGE OUTPUT CONTRACT\n', 1)[1]
    assert json.dumps(source.structured['segments'][0]['line'], ensure_ascii=False) in contract
    assert 'not a text-free video reference frame' in contract
    assert source.structured['caption'] not in draft['compiled_prompt']
    assert draft['shot_binding']['line'] == source.structured['segments'][0]['line']
    result = wait(scene, action(scene, draft, 'generate', 'offline-photo-copy'))
    assert result['status'] == 'succeeded', result
    assert len(scene[4].calls) == 1 and not scene[5].calls
    assert scene[4].calls[0].prompt == draft['compiled_prompt']
    assert scene[1].latest_artifact(RUN, 'writing').to_dict() == original


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
def test_old_photo_input_requires_new_save_without_rewriting_the_old_draft(visual_scene):
    from copy import deepcopy
    from studio.visual_design import validate_visual_binding
    scene = visual_scene
    draft = save(scene).json()
    old = deepcopy(draft)
    old['prompt'] = old['compiled_prompt'] = old['compiled_prompt'].split('\nFINAL PHOTO PAGE OUTPUT CONTRACT', 1)[0]
    original = deepcopy(old)
    with pytest.raises(ValueError, match='重新保存'):
        validate_visual_binding(scene[1], scene[2], RUN, old)
    assert old == original
    validate_visual_binding(scene[1], scene[2], RUN, draft)


@pytest.mark.parametrize('visual_scene', [None, 'video'], indirect=True)
def test_video_reference_frames_do_not_force_spoken_lines_into_on_image_copy(visual_scene):
    draft = save(visual_scene).json()
    assert 'FINAL PHOTO PAGE OUTPUT CONTRACT' not in draft['compiled_prompt']
    assert 'Do not turn spoken lines into on-image text' in draft['compiled_prompt']


def test_photo_producer_keeps_full_role_instructions_and_adds_static_page_contract(visual_scene):
    from studio.agents.roles import ORCHESTRATOR
    run = visual_scene[1].read_run(RUN)
    text = ORCHESTRATOR.system(run, format='photo')
    assert ORCHESTRATOR.instructions in text
    assert '工作台尚无静态图后期叠字执行链' in text
    assert 'line 是图内文案，不是口播' in text
    assert '工作台尚无静态图后期叠字执行链' not in ORCHESTRATOR.system(run, format='video')


@pytest.mark.parametrize('visual_scene', [{'format': 'photo', 'line': ''}], indirect=True)
def test_explicit_empty_photo_copy_stays_empty_without_invented_caption(visual_scene):
    draft = save(visual_scene).json()
    assert draft['shot_binding']['line'] == ''
    assert 'Exact adopted on-image copy (JSON string; an empty string means no copy):\n""\n' in draft['compiled_prompt']
    assert 'OFFLINE_PUBLISHING_CAPTION_DO_NOT_PRINT' not in draft['compiled_prompt']


@pytest.mark.parametrize('visual_scene', ['photo', 'video', None], indirect=True)
@pytest.mark.parametrize('outcome', ['done', 'incomplete'])
@pytest.mark.parametrize('subset', [False, True])
def test_batch_photo_labels_are_frozen_and_do_not_propose_video(visual_scene, monkeypatch, outcome, subset):
    """Mock runners only; no model or provider generation is submitted."""
    from types import SimpleNamespace
    from studio.production import BatchGenerateIn
    scene = visual_scene
    service = scene[0].app.state.production
    rows = [save(scene, request_id=f'offline-page-{n}', shot_id=f'shot-0{n}').json()
            for n in (1, 2)]
    if subset:
        rows = rows[1:]
    payload = BatchGenerateIn(request_id='offline-page-batch', consent=True,
        nonce=workspace(scene)['nonce'],
        expected_connection_revision=service.connections.public()['revision'],
        drafts=[{'draft_id': row['draft_id'], 'expected_sha256': row['draft_sha256']} for row in rows])
    calls = []
    def fake_runner(*args):
        calls.append(True)
        return SimpleNamespace(stopped_because=outcome, text='Offline failure')
    monkeypatch.setattr(service, 'prepare_generation', lambda *a, **k: (fake_runner, {}))
    runner, config = service.prepare_batch_generation(RUN, payload)
    assert config['kind'] == 'image_batch'
    # Rendering a saved batch does not read a new format selection at execution.
    monkeypatch.setattr('studio.production.content_format', lambda *a: (_ for _ in ()).throw(
        AssertionError('batch display format must already be frozen')))
    events = []
    result = runner('', events.append, lambda: None)
    from studio.project_brief import content_format
    photo = content_format(scene[1].read_events(RUN)) == 'photo'
    messages = [event['message'] for event in events if event['type'] == 'status']
    indexes = tuple(range(1, len(rows) + 1)) if outcome == 'done' else (1,)
    assert len(calls) == len(indexes)
    assert messages == ([f'正在生成第 {n + int(subset):02d} 页图片（本次 {n}/{len(rows)} 张）。'
                         for n in indexes] if photo else
                        [f'正在生成第 {n}/{len(rows)} 个首帧。' for n in indexes])
    assert result.stopped_because == outcome
    if outcome == 'incomplete':
        assert (f'第 {1 + int(subset):02d} 页图片未完成' if photo else '第 1 个未完成') in result.text
        assert '没有提交' in result.text
    elif photo:
        assert f'本次 {len(rows)} 张图文图片已生成并保存' in result.text and '逐页查看图片' in result.text
        assert '视频' not in result.text and '首帧' not in result.text
    else:
        assert f'{len(rows)} 个首帧已生成并保存' in result.text
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
@pytest.mark.parametrize('projection_issue', ['missing', 'ambiguous'])
def test_photo_subset_missing_ordinal_reports_only_batch_count(visual_scene, monkeypatch, projection_issue):
    from types import SimpleNamespace
    from studio.production import BatchGenerateIn
    service = visual_scene[0].app.state.production
    row = save(visual_scene, request_id='offline-second-page', shot_id='shot-02').json()
    original_workspace = service.workspace
    def workspace_without_ordinal(*args, **kwargs):
        state = original_workspace(*args, **kwargs)
        shots = state['shots']['image']['shots']
        if projection_issue == 'missing':
            shots[1].pop('ordinal', None)
        else:
            shots.append(dict(shots[1]))
        return state
    monkeypatch.setattr(service, 'workspace', workspace_without_ordinal)
    calls = []
    def fake_runner(*args):
        calls.append(True)
        return SimpleNamespace(stopped_because='incomplete', text='Offline failure')
    monkeypatch.setattr(service, 'prepare_generation', lambda *a, **k: (fake_runner, {}))
    payload = BatchGenerateIn(request_id='offline-subset-no-ordinal', consent=True,
        nonce=workspace(visual_scene)['nonce'],
        expected_connection_revision=service.connections.public()['revision'],
        drafts=[{'draft_id': row['draft_id'], 'expected_sha256': row['draft_sha256']}])
    runner, _ = service.prepare_batch_generation(RUN, payload)
    events = []
    result = runner('', events.append, lambda: None)
    assert [e['message'] for e in events] == ['正在生成图文图片（本次 1/1 张）。']
    assert '图文图片未完成' in result.text
    assert '第 01 页' not in result.text and '第 02 页' not in result.text
    assert len(calls) == 1 and not visual_scene[4].calls and not visual_scene[5].calls


@pytest.mark.parametrize('visual_scene', ['photo', 'video', None], indirect=True)
def test_reference_only_revision_preserves_exact_description_and_provider_input(visual_scene, monkeypatch):
    """Exercise the HTTP save and image boundary; all pixels/providers are fakes."""
    from studio.production import draft_records
    scene = visual_scene
    description = '  原描述：保留台面。\nExact line with “quotes”.\n\nNo additional instructions.  '
    initial_refs = refs(scene)[:2]
    parent = save(scene, 'exact-description-parent', prompt=description, references=initial_refs).json()
    other = save(scene, 'untouched-second-shot', shot_id='shot-02').json()
    original_records = draft_records(scene[1], RUN)
    artifacts = {stage: scene[1].latest_artifact(RUN, stage).to_dict()
                 for stage in ('intake', 'direction', 'material_plan', 'writing')}
    approvals = scene[1].list_approvals(RUN)
    payload = dict(prompt=None, supersedes=parent['draft_id'], references=refs(scene),
                   preserve_prompt_from={'draft_id': parent['draft_id'],
                                         'expected_sha256': parent['draft_sha256']})
    result = save(scene, 'reference-only-revision', **payload)
    assert result.status_code == 200, result.text
    item = result.json()
    assert item['shot_prompt'].encode('utf-8') == description.encode('utf-8')
    assert item['request_payload']['prompt'] is None
    assert item['request_payload']['preserve_prompt_from'] == payload['preserve_prompt_from']
    assert item['references'] == refs(scene)
    for key in ('design_binding', 'shot_binding', 'source_snapshot', 'parameters'):
        assert item[key] == parent[key]
    repeated = save(scene, 'reference-only-revision', **payload)
    assert repeated.status_code == 200 and repeated.json()['draft_sha256'] == item['draft_sha256']
    records = draft_records(scene[1], RUN)
    assert len(records) == 3
    assert records[other['draft_id']] == original_records[other['draft_id']]
    assert records[parent['draft_id']]['shot_prompt'] == description
    assert records[parent['draft_id']]['draft_sha256'] == parent['draft_sha256']
    assert not scene[4].calls and not scene[5].calls
    assert scene[1].list_approvals(RUN) == approvals
    assert {stage: scene[1].latest_artifact(RUN, stage).to_dict() for stage in artifacts} == artifacts
    seen = []
    generate = scene[4].generate
    def capture(request):
        seen.extend((path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in request.reference_paths)
        return generate(request)
    monkeypatch.setattr(scene[4], 'generate', capture)
    job = wait(scene, action(scene, item, 'generate', 'explicit-reference-only-generation'))
    assert job['status'] == 'succeeded', job
    assert len(scene[4].calls) == 1 and not scene[5].calls
    assert scene[4].calls[0].prompt == item['compiled_prompt']
    assert 'Additional shot instructions:\n' + description in item['compiled_prompt']
    assert seen == [(f'reference-{n:02d}-{row["purpose"]}.png', row['sha256'])
                    for n, row in enumerate(refs(scene), 1)]
    assert scene[1].list_approvals(RUN) == approvals


def test_legacy_description_request_keeps_existing_payload_and_retry_identity(visual_scene):
    scene = visual_scene
    first = save(scene, 'legacy-description-request').json()
    assert 'preserve_prompt_from' not in first and 'preserve_prompt_from' not in first['request_payload']
    before = scene[1].read_events(RUN)
    again = save(scene, 'legacy-description-request', preserve_prompt_from=None)
    assert again.status_code == 200 and again.json()['draft_sha256'] == first['draft_sha256']
    assert scene[1].read_events(RUN) == before
    changed = save(scene, 'legacy-description-request', prompt='A different description')
    assert changed.status_code == 409
    assert scene[1].read_events(RUN) == before
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('change', [
    {'preserve_prompt_from': None}, {'prompt': 'Appended instructions'},
    {'supersedes': None}, {'supersedes': 'another-input'},
    {'preserve_prompt_from': {'draft_id': 'another-input', 'expected_sha256': 'a' * 64}},
    {'preserve_prompt_from': {'draft_id': 'exact-parent', 'expected_sha256': 'BAD'}},
    {'preserve_prompt_from': {'draft_id': 'exact-parent', 'expected_sha256': 'a' * 64, 'fallback': 'new'}},
])
def test_preserved_description_rejects_ambiguous_or_invalid_modes_before_saving(visual_scene, change):
    scene = visual_scene
    parent = save(scene, 'exact-parent').json()
    before = scene[1].read_events(RUN)
    payload = dict(prompt=None, supersedes=parent['draft_id'],
                   preserve_prompt_from={'draft_id': parent['draft_id'],
                                         'expected_sha256': parent['draft_sha256']})
    result = save(scene, 'invalid-preserved-description', **{**payload, **change})
    assert result.status_code == 422, result.text
    assert scene[1].read_events(RUN) == before
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('change,expected', [
    ('fingerprint', '指纹已变化'), ('missing_parent', '不在记录里'),
    ('replaced', '已经有更新的版本'), ('other_shot', '不能用来替换镜头'),
    ('other_kind', '同一媒体类型'), ('new_source', None),
])
def test_preserved_description_rechecks_exact_current_parent_inside_save(visual_scene, change, expected):
    scene = visual_scene
    parent = save(scene, 'exact-parent').json()
    payload = dict(prompt=None, supersedes=parent['draft_id'],
                   preserve_prompt_from={'draft_id': parent['draft_id'],
                                         'expected_sha256': parent['draft_sha256']})
    if change == 'fingerprint':
        payload['preserve_prompt_from']['expected_sha256'] = '0' * 64
    elif change == 'missing_parent':
        payload['supersedes'] = payload['preserve_prompt_from']['draft_id'] = 'unknown-parent'
    elif change == 'replaced':
        assert save(scene, 'concurrent-new-input', supersedes=parent['draft_id']).status_code == 200
    elif change == 'other_shot':
        payload['shot_id'] = 'shot-02'
    elif change == 'other_kind':
        payload.update(kind='video', model='wan3.0-video', duration=5, resolution='720P',
                       references=[{**refs(scene)[0], 'purpose': 'continuity'}])
    else:
        accept_upload(scene[1], RUN, 'changed-source.png', png())
    before = scene[1].read_events(RUN)
    result = save(scene, 'rejected-parent-copy', **payload)
    assert result.status_code == 409, result.text
    if expected:
        assert expected in result.text
    assert scene[1].read_events(RUN) == before
    assert not scene[4].calls and not scene[5].calls


@pytest.mark.parametrize('description', [None, '', '   ', 42, 'x' * 5001],
                         ids=['missing', 'empty', 'whitespace', 'wrong_type', 'oversized'])
def test_preserved_description_does_not_guess_from_legacy_compiled_prompt(visual_scene, description):
    scene = visual_scene
    parent = deepcopy(save(scene, 'historical-description-fixture').json())
    parent['draft_id'] = 'legacy-without-original-description'
    parent['shot_prompt'] = description
    parent['draft_sha256'] = digest({k: v for k, v in parent.items() if k != 'draft_sha256'})
    # Synthetic historical event, never a mutation of the earlier saved record.
    scene[1].append_event(RUN, 'production_draft', draft=parent)
    before = scene[1].read_events(RUN)
    result = save(scene, 'cannot-invent-original-description', prompt=None, supersedes=parent['draft_id'],
                  preserve_prompt_from={'draft_id': parent['draft_id'],
                                        'expected_sha256': parent['draft_sha256']})
    assert result.status_code == 409 and '没有可复用的准确原描述' in result.text
    assert scene[1].read_events(RUN) == before
    assert not scene[4].calls and not scene[5].calls
