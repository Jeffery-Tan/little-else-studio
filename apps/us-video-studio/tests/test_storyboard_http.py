"""End-to-end HTTP check of the storyboard delivery contract.

The review's first finding was that the controller read a thinner production
state than the page, so "why did the storyboard not come out" reached the chat
with no attempts in it. Unit tests on ``coverage`` cannot catch that: it is a
disagreement *between two readers*. These tests drive the real HTTP surface and
assert that the page and the controller see the same thing.
"""
from __future__ import annotations

import pytest

from local_http import TestClient
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for
from visual_fixtures import designed_script, stage_structure

BASE = f'/api/runs/{RUN}/production'

SCRIPT = {
    'segments': [
        {'tc': '0-2s', 'picture': 'Bag on the table', 'line': 'One.', 'sound': 'ambient', 'duration': 2},
        {'tc': '2-4s', 'picture': 'Zip opens', 'line': 'Two.', 'sound': 'zip', 'duration': 2},
        {'tc': '4-6s', 'picture': 'Lunch goes in', 'line': 'Three.', 'sound': 'ambient', 'duration': 2},
    ],
    'total_duration': 6,
}
SCRIPT = designed_script(SCRIPT)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'OFFLINE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    app = build_app(settings_for(tmp_path), client_factory=lambda: pytest.fail('no model call'))
    with TestClient(app) as http:
        created = http.post('/api/runs', json={'run_id': RUN, 'title': 'Storyboard fixture',
                                               'brief': 'Synthetic only; no generation'})
        assert created.status_code == 200, created.text
        store = Store(tmp_path)
        pipeline = Pipeline(store)
        for stage in ['intake', 'direction', 'material_plan', 'writing']:
            attempt = pipeline.start_attempt(RUN, stage)
            structured = SCRIPT if stage == 'writing' else (
                {'options': [{'key': 'A', 'claim': 'Synthetic', 'satisfies': 'goal.primary'}]}
                if stage == 'direction' else stage_structure(stage))
            pipeline.submit(RUN, stage, attempt.token, content='Synthetic ' + stage,
                            summary=stage, evidence=['Fixture only'],
                            limitations=['Not market evidence'], structured=structured)
            if stage in {'direction', 'writing'}:
                pipeline.record_approval(RUN, stage, nonce=pipeline.issue_nonce(RUN, stage),
                                         decision='approved',
                                         selected_option_id='A' if stage == 'direction' else None)
        yield http, store, pipeline


def save_shot(scene, request_id, shot_id=None, supersedes=None):
    http = scene[0]
    data = http.get(BASE).json()
    source = data['sources'][-1]
    payload = {'request_id': request_id, 'kind': 'image', 'prompt': f'Frame {request_id}',
               'source_version': source['version'], 'source_sha256': source['artifact_sha256'],
               'model': 'codex-image'}
    if shot_id:
        payload['shot_id'] = shot_id
    if supersedes:
        payload['supersedes'] = supersedes
    return http.post(BASE + '/drafts', json=payload)


def test_the_page_serves_the_target_shot_list_and_a_per_shot_state(scene):
    http = scene[0]
    data = http.get(BASE).json()
    assert data['targets']['image']['complete_list'] is True
    assert [shot['shot_id'] for shot in data['targets']['image']['shots']] == \
        ['shot-01', 'shot-02', 'shot-03']
    coverage = data['shots']['image']
    assert coverage['total'] == 3 and coverage['ready'] is False
    assert coverage['unready_shots'] == [1, 2, 3]
    assert [row['stage_label'] for row in coverage['shots']] == ['未准备', '未准备', '未准备']


def test_one_shot_of_three_does_not_read_as_a_finished_storyboard(scene):
    http = scene[0]
    assert save_shot(scene, 'shot-three', shot_id='shot-03').status_code == 200
    coverage = http.get(BASE).json()['shots']['image']
    assert coverage['ready'] is False
    assert coverage['unready_shots'] == [1, 2]
    assert [row['stage'] for row in coverage['shots']] == ['unprepared', 'unprepared', 'input_ready']


def test_all_three_inputs_are_ready_and_the_last_one_is_not_the_whole_project(scene):
    http = scene[0]
    for index in (1, 2, 3):
        assert save_shot(scene, f'frame-{index}', shot_id=f'shot-{index:02d}').status_code == 200
    coverage = http.get(BASE).json()['shots']['image']
    assert coverage['ready'] is True
    # Inputs ready is not results delivered: nothing has been generated.
    assert coverage['delivered'] is False
    assert [row['stage'] for row in coverage['shots']] == ['input_ready'] * 3


def test_a_revision_keeps_its_shot_and_the_replaced_input_is_not_a_fourth_shot(scene):
    http = scene[0]
    original = save_shot(scene, 'frame-2-v1', shot_id='shot-02').json()
    revised = save_shot(scene, 'frame-2-v2', supersedes=original['draft_id'])
    assert revised.status_code == 200, revised.text
    assert revised.json()['shot_id'] == original['shot_id']
    assert revised.json()['shot_revision'] == 2

    data = http.get(BASE).json()
    assert data['shots']['image']['total'] == 3
    row = next(item for item in data['shots']['image']['shots'] if item['ordinal'] == 2)
    assert row['draft_id'] == 'frame-2-v2'
    assert row['superseded_draft_ids'] == ['frame-2-v1']
    # The replaced input is still readable, and it is no longer generated by default.
    stored = {draft['draft_id']: draft for draft in data['drafts']}
    assert stored['frame-2-v1']['is_current'] is False
    assert stored['frame-2-v1']['can_generate'] is False
    assert stored['frame-2-v2']['can_generate'] is True


def test_replacing_another_shots_input_is_refused_with_its_own_reason(scene):
    http = scene[0]
    first = save_shot(scene, 'frame-1', shot_id='shot-01').json()
    refused = save_shot(scene, 'frame-2', shot_id='shot-02', supersedes=first['draft_id'])
    assert refused.status_code == 409
    assert '属于镜头' in refused.text


def test_replacing_an_already_replaced_input_is_refused(scene):
    http = scene[0]
    first = save_shot(scene, 'v1', shot_id='shot-01').json()
    assert save_shot(scene, 'v2', supersedes=first['draft_id']).status_code == 200
    again = save_shot(scene, 'v3', supersedes=first['draft_id'])
    assert again.status_code == 409
    assert '更新的版本' in again.text


def test_a_stale_script_version_is_named_as_the_reason_not_as_a_parameter_problem(scene):
    http, store, pipeline = scene
    attempt = pipeline.start_attempt(RUN, 'writing')
    pipeline.submit(RUN, 'writing', attempt.token, content='Revised', summary='v2', structured=SCRIPT)
    current = store.latest_artifact(RUN, 'writing')
    refused = http.post(BASE + '/drafts', json={
        'request_id': 'stale', 'kind': 'image', 'prompt': 'x',
        'source_version': current.version - 1, 'source_sha256': current.sha256,
        'model': 'codex-image'})
    assert refused.status_code == 409
    assert '版本' in refused.text
    assert '核对当前已批准脚本、参考图和参数' not in refused.text
