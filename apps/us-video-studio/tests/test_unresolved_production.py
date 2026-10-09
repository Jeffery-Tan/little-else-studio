"""Unresolved original tasks outrank invalid or replaced inputs; no providers."""
from copy import deepcopy

import pytest

from studio.production import coverage, digest
from test_production import (BASE, RUN, scene, adopt_shot_count, batch_payload, draft,
                            workspace)


@pytest.mark.parametrize('replaced', [False, True])
def test_coverage_keeps_original_unknown_identity_before_design_gap(replaced):
    original = {'draft_id': 'original', 'draft_sha256': 'a' * 64, 'kind': 'image',
                'shot_id': 'shot-01', 'source_sha256': 'b' * 64, 'source_snapshot': 'c' * 64,
                'source_ready': False, 'design_issue': 'Legacy binding missing',
                'is_current': not replaced,
                'attempts': [{'attempt_id': 'original-task', 'status': 'unknown'}]}
    rows = {'original': original}
    if replaced:
        rows['replacement'] = {**original, 'draft_id': 'replacement', 'draft_sha256': 'd' * 64,
                               'is_current': True, 'source_ready': True, 'design_issue': '', 'attempts': []}
    result = coverage(rows, 'image', baseline='c' * 64, targets={
        'source_sha256': 'b' * 64, 'complete_list': True,
        'shots': [{'shot_id': 'shot-01', 'ordinal': 1}]})['shots'][0]
    assert result['stage'] == 'unverified'
    assert result['next_action'] == 'check_original_task'
    assert result['attempt_id'] == 'original-task'
    assert '原任务' in result['reason'] and 'Legacy binding' not in result['reason']


def test_obsolete_unbound_task_blocks_a_new_batch_and_remains_queryable(scene):
    http, store, _, reviewer, images, videos, _ = scene
    adopt_shot_count(scene, 2)
    current = [draft(scene, kind='image', request_id=f'current-{index}', shot_id=f'shot-{index:02d}').json()
               for index in (1, 2)]
    legacy = deepcopy(next(row['draft'] for row in store.read_events(RUN)
                           if row['kind'] == 'production_draft'))
    legacy.update(draft_id='old-legacy-input', source_snapshot='0' * 64)
    legacy.pop('design_binding', None)
    legacy.pop('shot_binding', None)
    legacy['draft_sha256'] = digest({key: value for key, value in legacy.items() if key != 'draft_sha256'})
    store.append_event(RUN, 'production_draft', draft=legacy)
    store.append_event(RUN, 'production_attempt', attempt_id='original-unknown-task',
                       draft_id=legacy['draft_id'], draft_sha256=legacy['draft_sha256'],
                       media_kind='image', status='unknown')
    data = workspace(scene)
    original = next(row for row in data['drafts'] if row['draft_id'] == legacy['draft_id'])
    assert not original['source_ready']
    assert data['unresolved_attempts'][0]['attempt_id'] == 'original-unknown-task'
    assert data['unresolved_attempts'][0]['draft_id'] == legacy['draft_id']
    response = http.post(BASE + '/batch-generate', json=batch_payload(scene, current))
    assert response.status_code == 409 and '未核实' in response.text
    assert not reviewer.calls and not images.calls and not videos.calls
