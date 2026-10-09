"""Legacy design supplements exercised through real HTTP; synthetic models only."""
from copy import deepcopy

import pytest
from local_http import TestClient

from fakes import ScriptedModel, tool_turn
from studio.design_upgrade import available
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.models import STAGES
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_jobs import wait_job
from test_production import png
from test_server import RUN, settings_for
from visual_fixtures import stage_structure, visual_design


BASE = f'/api/runs/{RUN}/production/design-upgrade'


def seed_legacy(store, *, designed=False):
    store.create_run(RUN, 'Legacy synthetic story', 'No real project or model calls.')
    pipe = Pipeline(store)
    for stage in STAGES:
        shape = stage_structure(stage)
        if stage == 'direction':
            shape = {'options': [{'key': 'A', 'claim': 'Synthetic adopted choice',
                                  'satisfies': 'goal.primary'}]}
        if not designed:
            shape.pop('visual_design', None)
            for row in shape.get('segments', []):
                row.pop('character_ids', None)
                row.pop('scene_id', None)
        attempt = pipe.start_attempt(RUN, stage)
        pipe.submit(RUN, stage, attempt.token, content=f'Original {stage} prose\r\nKept exactly.',
                    summary='Historical fixture', structured=shape,
                    evidence=['Synthetic fixture only'], limitations=['No observed customers'])
        if stage in {'direction', 'writing'}:
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage),
                                 decision='approved', selected_option_id='A' if stage == 'direction' else None)
    return pipe


@pytest.fixture
def scene(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pipe = seed_legacy(store)
    model = ScriptedModel([], legacy_task_contracts=False)
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'OFFLINE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    settings = settings_for(tmp_path)
    app = build_app(settings, client_factory=lambda: model)
    with TestClient(app) as http:
        yield http, store, pipe, model, settings


def originals(store):
    return ({stage: store.latest_artifact(RUN, stage) for stage in STAGES}, store.list_approvals(RUN))


def arguments(store):
    from studio.production import target_shots
    ids = [row['shot_id'] for row in target_shots(store, RUN, 'image')['shots']]
    return {'design': visual_design(), 'bindings': [
        {'shot_id': shot_id, 'character_ids': '', 'scene_id': 'table'} for shot_id in ids]}


def propose(scene, args=None, *, turn=None):
    http, store, pipe, model, _ = scene
    model.turns.append(turn or tool_turn('supplement', 'propose_design_supplement', args or arguments(store)))
    payload = {'request_id': 'design-upgrade-one', 'expected_context_sha256': available(store, pipe, RUN)['expected_context_sha256'],
               'expected_connection_revision': 0, 'consent': True}
    response = http.post(BASE + '/propose', json=payload)
    assert response.status_code == 200, response.text
    job = wait_job(http, response.json()['job_id'])
    return job, http.get(BASE + '/proposal').json(), payload


def save_payload(scene, proposal):
    return {'proposal_id': proposal['id'], 'expected_proposal_sha256': proposal['sha256'],
            'nonce': scene[0].app.state.production.nonce(RUN), 'consent': True}


def test_workspace_exposes_reviewable_legacy_upgrade_without_mutating_versions(scene):
    http, store, pipe, model, _ = scene
    before = originals(store)
    result = http.get(f'/api/runs/{RUN}/production').json()
    assert result['design_upgrade_action']['expected_context_sha256']
    assert not result['visual_design_status']['ready']
    assert originals(store) == before and not model.calls


def test_proposal_is_one_metered_call_not_an_artifact_or_approval(scene):
    _, store, _, model, settings = scene
    before = originals(store)
    job, proposal, payload = propose(scene)
    assert job['status'] == 'succeeded', job.get('reason')
    assert proposal['current'] and proposal['sha256']
    assert len(model.calls) == 1
    entries = Ledger(store, settings).entries(RUN)
    assert len(entries) == 1 and entries[0].role == 'material' and entries[0].usage_status == 'known'
    assert originals(store) == before
    assert not any(row['kind'] in {'execution_authorized', 'production_attempt'} for row in store.read_events(RUN))
    repeated = scene[0].post(BASE + '/propose', json=payload)
    assert repeated.status_code == 200 and repeated.json()['job_id'] == job['job_id']
    assert len(model.calls) == 1


def test_user_save_preserves_story_and_original_files_without_inheriting_approval(scene):
    http, store, pipe, model, _ = scene
    before, approvals = originals(store)
    old_bytes = {stage: store.read_artifact_text(RUN, stage, row.version) for stage, row in before.items()}
    job, proposal, _ = propose(scene)
    assert job['status'] == 'succeeded', job.get('reason')
    payload = save_payload(scene, proposal)
    response = http.post(BASE + '/save', json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['saved']
    for stage in STAGES:
        assert store.read_artifact(RUN, stage, before[stage].version) == before[stage]
        assert store.read_artifact_text(RUN, stage, before[stage].version) == old_bytes[stage]
    assert store.latest_artifact(RUN, 'direction') == before['direction']
    assert store.list_approvals(RUN) == approvals
    assert pipe.stage_state(RUN, 'writing').approval is None
    assert not pipe.is_complete(RUN, 'writing')
    new_plan = store.latest_artifact(RUN, 'material_plan')
    new_story = store.latest_artifact(RUN, 'writing')
    assert new_plan.version == new_story.version == 2
    restored_plan = deepcopy(new_plan.structured)
    restored_plan.pop('visual_design')
    restored_story = deepcopy(new_story.structured)
    for row in restored_story['segments']:
        row.pop('character_ids'); row.pop('scene_id')
    assert restored_plan == before['material_plan'].structured
    old_story = deepcopy(before['writing'].structured)
    for row in old_story['segments']:
        row.pop('character_ids', None); row.pop('scene_id', None)
    assert restored_story == old_story
    assert store.read_artifact_text(RUN, 'writing', 2).startswith(old_bytes['writing'])
    assert http.get(BASE + '/proposal').json() is None
    repeated = http.post(BASE + '/save', json={**payload, 'nonce': 'old-nonce-after-restart'})
    assert repeated.status_code == 200 and repeated.json()['already_saved']
    assert len(model.calls) == 1 and len(store.list_artifacts(RUN)) == 6


@pytest.mark.parametrize('change', ['consent', 'connection', 'context'])
def test_preflight_rejection_spends_nothing(scene, change):
    http, store, pipe, model, _ = scene
    payload = {'request_id': 'design-preflight', 'expected_context_sha256': available(store, pipe, RUN)['expected_context_sha256'],
               'expected_connection_revision': 0, 'consent': True}
    payload[{'consent': 'consent', 'connection': 'expected_connection_revision',
             'context': 'expected_context_sha256'}[change]] = {'consent': False, 'connection': 1, 'context': '0' * 64}[change]
    before = originals(store)
    response = http.post(BASE + '/propose', json=payload)
    assert response.status_code == 409, response.text
    assert not model.calls and originals(store) == before


@pytest.mark.parametrize('change', ['extra_story', 'wrong_shot', 'missing_binding', 'unknown_scene', 'truncated'])
def test_bad_model_proposal_cannot_change_the_original_story(scene, change):
    _, store, _, model, _ = scene
    args = arguments(store)
    if change == 'extra_story': args['writing'] = {'segments': []}
    if change == 'wrong_shot': args['bindings'][0]['shot_id'] = 'not-an-original-shot'
    if change == 'missing_binding': args['bindings'] = []
    if change == 'unknown_scene': args['bindings'][0]['scene_id'] = 'not-designed'
    turn = tool_turn('proposal', 'propose_design_supplement', args)
    if change == 'truncated': turn.stop_reason = 'max_tokens'
    before = originals(store)
    job, proposal, _ = propose(scene, turn=turn)
    assert job['status'] == 'failed', job
    assert proposal is None and originals(store) == before and len(model.calls) == 1


def test_changed_source_refuses_save_without_new_versions(scene):
    http, store, _, _, _ = scene
    job, proposal, _ = propose(scene)
    assert job['status'] == 'succeeded', job.get('reason')
    before = originals(store)
    accept_upload(store, RUN, 'changed.png', png())
    assert http.get(BASE + '/proposal').json()['current'] is False
    response = http.post(BASE + '/save', json=save_payload(scene, proposal))
    assert response.status_code == 409 and originals(store) == before


def test_partial_save_resumes_the_same_reviewed_proposal_without_new_model_call(scene, monkeypatch):
    http, store, pipe, model, _ = scene
    job, proposal, _ = propose(scene)
    assert job['status'] == 'succeeded', job.get('reason')
    live_pipe = http.app.state.production.pipeline
    original = live_pipe.submit
    def fail_writing(run_id, stage, *args, **kwargs):
        if stage == 'writing': raise OSError('Synthetic disk interruption')
        return original(run_id, stage, *args, **kwargs)
    monkeypatch.setattr(live_pipe, 'submit', fail_writing)
    payload = save_payload(scene, proposal)
    interrupted = http.post(BASE + '/save', json=payload)
    assert interrupted.status_code == 503 and '同一份' in interrupted.text
    assert store.latest_artifact(RUN, 'material_plan').version == 2
    assert store.latest_artifact(RUN, 'writing').version == 1
    assert pipe.stage_state(RUN, 'writing').open_attempt is None
    current = http.get(BASE + '/proposal').json()
    assert current['current'] and current['partial_save']
    monkeypatch.setattr(live_pipe, 'submit', original)
    response = http.post(BASE + '/save', json=payload)
    assert response.status_code == 200, response.text
    assert store.latest_artifact(RUN, 'material_plan').version == 2
    assert store.latest_artifact(RUN, 'writing').version == 2
    assert len(model.calls) == 1


def test_current_designed_project_has_no_upgrade_entry(tmp_path):
    store = Store(tmp_path)
    pipe = seed_legacy(store, designed=True)
    assert available(store, pipe, RUN) is None
