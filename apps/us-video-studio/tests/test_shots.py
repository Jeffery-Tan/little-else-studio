"""Shot identity, revisions and whole-storyboard coverage.

The review found that a storyboard was stored as a flat list of image drafts: no
shot identity, so a revised prompt for one shot read as a second shot, and no
coverage rule, so one draft counted as a whole storyboard. These tests pin the
replacement behaviour against a real store and pipeline, because the bug lived in
the interaction between the record and the deliverable check rather than in
either one alone.
"""
from __future__ import annotations

import pytest

from studio.pipeline import Pipeline
from studio.production import (
    DraftIn, apply_shots, coverage, current_draft, draft_records, prepare_draft, shot_id_for, target_shots,
)
from studio.store import Store
from visual_fixtures import designed_script, stage_structure

RUN = 'shots'

SCRIPT = {
    'segments': [
        {'tc': '0-2s', 'picture': '包出现在桌面', 'line': 'First line', 'sound': 'ambient', 'duration': 2},
        {'tc': '2-4s', 'picture': '拉链打开', 'line': 'Second line', 'sound': 'zip', 'duration': 2},
        {'tc': '4-6s', 'picture': '午餐装进包', 'line': 'Third line', 'sound': 'ambient', 'duration': 2},
    ],
    'total_duration': 6,
}
SCRIPT = designed_script(SCRIPT)


def prepared(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Shot identity fixture', 'Synthetic only; no generation')
    pipe = Pipeline(store)
    for stage in ['intake', 'direction', 'material_plan', 'writing']:
        attempt = pipe.start_attempt(RUN, stage)
        structured = SCRIPT if stage == 'writing' else (
            {'options': [{'key': 'A', 'claim': 'Synthetic', 'satisfies': 'goal.primary'}]}
            if stage == 'direction' else stage_structure(stage))
        pipe.submit(RUN, stage, attempt.token, content='Synthetic ' + stage, summary=stage,
                    evidence=['Fixture only'], limitations=['Not market evidence'],
                    structured=structured)
        if stage in {'direction', 'writing'}:
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage),
                                 decision='approved',
                                 selected_option_id='A' if stage == 'direction' else None)
    return store, pipe


def image_input(store, pipe, request_id, **over):
    source = store.latest_artifact(RUN, 'writing')
    payload = dict(request_id=request_id, kind='image', prompt='Synthetic frame description.',
                   source_version=source.version, source_sha256=source.sha256, model='codex-image')
    return prepare_draft(store, pipe, RUN, DraftIn(**{**payload, **over}), actor='agent')


def test_target_shots_come_from_the_script_and_are_not_a_fixed_number(tmp_path):
    store, _ = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    assert targets['complete_list'] is True
    assert targets['source'] == 'writing_segments'
    assert [shot['shot_id'] for shot in targets['shots']] == ['shot-01', 'shot-02', 'shot-03']
    assert targets['shots'][1]['label'] == '拉链打开'


def test_legacy_prose_script_reports_an_unreadable_shot_list_instead_of_guessing(tmp_path):
    store = Store(tmp_path)
    store.create_run('legacy', 'Legacy', 'Synthetic')
    pipe = Pipeline(store)
    for stage in ['intake', 'direction', 'material_plan', 'writing']:
        attempt = pipe.start_attempt('legacy', stage)
        pipe.submit('legacy', stage, attempt.token, content=f'Prose {stage}', summary=stage,
                    structured={'options': [{'key': 'A', 'claim': 'Synthetic',
                                             'satisfies': 'goal.primary'}]}
                    if stage == 'direction' else {})
        if stage == 'direction':
            pipe.record_approval('legacy', stage, nonce=pipe.issue_nonce('legacy', stage),
                                 decision='approved', selected_option_id='A')
    targets = target_shots(store, 'legacy', 'image')
    assert targets['complete_list'] is False
    assert targets['shots'] == []
    assert '不要假定' in targets['notice']


def test_new_inputs_bind_script_shots_and_never_guess_from_request_identity(tmp_path):
    store, pipe = prepared(tmp_path)
    with pytest.raises(ValueError, match='shot_id'):
        image_input(store, pipe, 'ambiguous')
    first = image_input(store, pipe, 'shot-a', shot_id='shot-01')
    second = image_input(store, pipe, 'shot-b', shot_id='shot-02')
    assert first['shot_id'] != second['shot_id']
    assert first['shot_id'] == 'shot-01'
    assert first['shot_id'] != shot_id_for('shot-a')
    assert first['shot_revision'] == 1 and second['shot_revision'] == 1
    assert first['is_current'] and second['is_current']


def test_a_revision_replaces_its_shot_and_the_old_input_is_not_a_second_shot(tmp_path):
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    original = image_input(store, pipe, 'shot-a', shot_id=targets['shots'][1]['shot_id'])
    revised = image_input(store, pipe, 'shot-b', supersedes=original['draft_id'])

    assert revised['shot_id'] == original['shot_id'], 'a revision must keep its shot'
    assert revised['shot_revision'] == 2
    assert revised['is_current'] is True
    # The replaced input stays readable, but it is no longer the input to generate.
    stored = draft_records(store, RUN)
    assert stored[original['draft_id']]['superseded_by'] == revised['draft_id']
    assert stored[original['draft_id']]['is_current'] is False
    assert stored[original['draft_id']]['prompt'] == original['prompt']


def test_two_current_inputs_for_one_shot_collapse_to_the_later_one(tmp_path):
    """Even without a declared successor, one shot does not become two."""
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    shot = targets['shots'][0]['shot_id']
    first = image_input(store, pipe, 'same-shot-1', shot_id=shot)
    second = image_input(store, pipe, 'same-shot-2', shot_id=shot)
    drafts = {row['draft_id']: dict(row) for row in draft_records(store, RUN).values()}
    apply_shots(drafts)
    current = [row for row in drafts.values() if row.get('is_current')]
    assert [row['draft_id'] for row in current] == [second['draft_id']]
    assert drafts[first['draft_id']]['superseded_by'] == second['draft_id']


def test_coverage_says_which_shots_are_still_missing_their_input(tmp_path):
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    empty = coverage(draft_records(store, RUN), 'image', targets=targets)
    assert empty['ready'] is False
    assert empty['unready_shots'] == [1, 2, 3]

    image_input(store, pipe, 'only-second', shot_id=targets['shots'][1]['shot_id'])
    partial = coverage(draft_records(store, RUN), 'image', targets=targets)
    assert partial['ready'] is False
    assert partial['unready_shots'] == [1, 3]
    assert [row['stage'] for row in partial['shots']] == ['unprepared', 'input_ready', 'unprepared']


def test_a_superseded_input_does_not_count_towards_coverage(tmp_path):
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    shot = targets['shots'][0]['shot_id']
    original = image_input(store, pipe, 'rev-1', shot_id=shot)
    assert coverage(draft_records(store, RUN), 'image', targets=targets)['ready'] is False

    image_input(store, pipe, 'rev-2', supersedes=original['draft_id'])
    after = coverage(draft_records(store, RUN), 'image', targets=targets)
    assert after['unready_shots'] == [2, 3], 'shot 1 is ready through its revision'
    assert after['shots'][0]['draft_id'] == 'rev-2'


def test_preparation_refuses_a_supersede_that_points_at_another_shot(tmp_path):
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    first = image_input(store, pipe, 'a', shot_id=targets['shots'][0]['shot_id'])
    try:
        image_input(store, pipe, 'b', shot_id=targets['shots'][1]['shot_id'],
                    supersedes=first['draft_id'])
    except ValueError as exc:
        assert getattr(exc, 'code', '') == 'supersedes_mismatch'
    else:
        raise AssertionError('replacing another shot must be refused')


def test_preparation_refuses_to_replace_an_already_replaced_input(tmp_path):
    store, pipe = prepared(tmp_path)
    targets = target_shots(store, RUN, 'image')
    shot = targets['shots'][0]['shot_id']
    original = image_input(store, pipe, 'a', shot_id=shot)
    image_input(store, pipe, 'b', supersedes=original['draft_id'])
    try:
        image_input(store, pipe, 'c', supersedes=original['draft_id'])
    except ValueError as exc:
        assert getattr(exc, 'code', '') == 'supersedes_already_replaced'
    else:
        raise AssertionError('replacing a历史稿 must be refused')


def test_preparation_names_the_actual_reason_instead_of_checking_parameters(tmp_path):
    """F2: the refusal has to say which cause it was, and what to do next."""
    store, pipe = prepared(tmp_path)
    source = store.latest_artifact(RUN, 'writing')
    base = dict(request_id='bad', kind='image', model='codex-image', prompt='x',
                source_sha256=source.sha256)

    for over, code, retryable, needs_decision in [
        ({'source_version': 99}, 'script_version_changed', False, True),
        ({'source_version': source.version, 'prompt': '   '}, 'empty_prompt', False, False),
        ({'source_version': source.version, 'duration': 5}, 'image_rejects_video_parameters', True, False),
    ]:
        try:
            prepare_draft(store, pipe, RUN, DraftIn(**{**base, **over}), actor='agent')
        except ValueError as exc:
            assert getattr(exc, 'code', '') == code, (over, exc)
            assert getattr(exc, 'retryable') is retryable
            assert getattr(exc, 'needs_user_decision') is needs_decision
            assert getattr(exc, 'next_action'), 'a refusal must name a next step'
            assert getattr(exc, 'target') is not None
        else:
            raise AssertionError(f'{over} should have been refused')


def test_preparation_reports_an_unadopted_script_as_its_own_reason(tmp_path):
    store, pipe = prepared(tmp_path)
    source = store.latest_artifact(RUN, 'writing')
    attempt = pipe.start_attempt(RUN, 'writing')
    pipe.submit(RUN, 'writing', attempt.token, content='Revised script', summary='revision',
                structured=SCRIPT)
    # New version saved, not yet adopted: preparation must not claim the inputs
    # are merely "not checked".
    current = store.latest_artifact(RUN, 'writing')
    try:
        prepare_draft(store, pipe, RUN, DraftIn(
            request_id='x', kind='image', prompt='x', source_version=current.version,
            source_sha256=current.sha256, model='codex-image'), actor='agent')
    except ValueError as exc:
        assert getattr(exc, 'code', '') in {'script_not_adopted', 'stage_attempt_open'}
        assert getattr(exc, 'needs_user_decision') is True
    else:
        raise AssertionError('preparation must refuse while the script is unadopted')
    assert source.version < current.version


def test_mixed_explicit_and_implicit_revisions_have_one_head_in_journal_order(tmp_path, monkeypatch):
    store, pipe = prepared(tmp_path)
    monkeypatch.setattr('studio.production.utcnow', lambda: '2026-09-29T12:00:00Z')
    original = image_input(store, pipe, 'z-original', shot_id='shot-02')
    explicit = image_input(store, pipe, 'y-explicit', supersedes=original['draft_id'])
    implicit = image_input(store, pipe, 'a-implicit', shot_id='shot-02')
    final = image_input(store, pipe, '0-final', supersedes=implicit['draft_id'])
    rows = draft_records(Store(store.root), RUN)
    apply_shots(rows)
    heads = [row for row in rows.values() if row['is_current']]
    assert [row['draft_id'] for row in heads] == [final['draft_id']]
    assert current_draft(rows, 'shot-02', 'image')['draft_id'] == final['draft_id']
    reordered = dict(reversed(list(rows.items())))
    apply_shots(reordered)
    assert current_draft(reordered, 'shot-02', 'image')['draft_id'] == final['draft_id']
    assert rows[explicit['draft_id']]['superseded_by'] == implicit['draft_id']
    assert [row['shot_revision'] for row in rows.values()] == [1, 2, 3, 4]
    assert [row['event_seq'] for row in rows.values()] == sorted(row['event_seq'] for row in rows.values())


def test_revision_heads_are_independent_per_baseline_and_media_kind():
    def row(key, baseline, kind='image', shot='shot-01'):
        return {'draft_id': key, 'kind': kind, 'source_sha256': 'script',
                'source_snapshot': baseline, 'shot_id': shot, 'attempts': []}
    rows = {item['draft_id']: item for item in [row('old', 'old'), row('new', 'new'),
            row('video', 'new', 'video'), row('newer-old', 'old')]}
    apply_shots(rows)
    assert {key for key, value in rows.items() if value['is_current']} == {'new', 'video', 'newer-old'}
    targets = {'complete_list': True, 'source_sha256': 'script', 'shots': [
        {'ordinal': 1, 'shot_id': 'shot-01'}]}
    assert coverage(rows, 'image', targets=targets, baseline='new')['shots'][0]['draft_id'] == 'new'
    assert rows['old']['superseded_by'] == 'newer-old'


def test_legacy_unlabelled_drafts_keep_readable_distinct_shots_after_reprojection():
    rows = {key: {'draft_id': key, 'kind': 'image', 'source_sha256': 'script',
                 'source_snapshot': 'baseline', 'attempts': []} for key in ['z-old', 'a-new']}
    apply_shots(rows)
    first = [(row['shot_id'], row['legacy_shot']) for row in rows.values()]
    apply_shots(rows)
    assert [(row['shot_id'], row['legacy_shot']) for row in rows.values()] == first
    assert len({row['shot_id'] for row in rows.values()}) == 2
    assert all(row['is_current'] for row in rows.values())


@pytest.mark.parametrize('status,output,expected', [
    ('succeeded', {'name': 'synthetic.png', 'sha256': 'a' * 64}, 'completed'),
    ('succeeded', None, 'unverified'), ('failed', None, 'failed'),
    ('unknown', None, 'unverified'), ('running', None, 'generating'),
])
def test_coverage_uses_actual_success_and_the_latest_attempt(status, output, expected):
    row = {'draft_id': 'input', 'kind': 'image', 'source_sha256': 'script',
           'source_snapshot': 'baseline', 'shot_id': 'shot-01', 'attempts': [
               {'attempt_id': 'original', 'status': 'succeeded',
                'output': {'name': 'previous.png', 'sha256': 'b' * 64}},
               {'attempt_id': 'latest', 'status': status, 'output': output,
                'message': 'Latest attempt reason', 'task_id': 'original-task'}]}
    rows = {'input': row}
    apply_shots(rows)
    targets = {'complete_list': True, 'shots': [{'ordinal': 1, 'shot_id': 'shot-01'}]}
    result = coverage(rows, 'image', targets=targets, baseline='baseline')
    shot = result['shots'][0]
    assert shot['stage'] == expected
    assert result['delivered'] is (expected == 'completed')
    assert shot['attempt_id'] == 'latest' and shot['reason'] == 'Latest attempt reason'
    assert shot['output'] == (output or row['attempts'][0]['output'])
