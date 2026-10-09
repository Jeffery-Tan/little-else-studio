"""Decisions bind complete user clauses and titles from the exact offered view."""
from __future__ import annotations

import copy

import pytest

from studio.decision import DecisionError, candidates, commit, resolve
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.timeline import build_timeline
from test_conversational_decision import RUN, settings_for
from test_decision import received_message


TITLES = {'A': '前袋先开口', 'B': '门口交接', 'C': '俯看装包'}
ORIGINAL = ('采用当前方向 A「前袋先开口」，按工作日上班带饭继续。'
            '这条肩带是否随附我不确定，保留图中可见形态，不宣称随附。'
            '沿用 15 秒、4 个镜头，先完成素材方案和脚本，暂不生成图片或视频。')
PHOTO_FEEDBACK = ('采用方向 A 的收纳结构主线，继续做三页图文方案和英文 caption。'
                  '餐具固定位在商品细节图左上展示的前拉链袋，不是内盖；内盖是网眼袋。'
                  '不要写“无需另配收纳袋”，也不要断言普通袋子不能替代。'
                  '保留每页一个不同选购理由和全部未核实事实边界，不重新让我选 A/B/C。')


@pytest.fixture
def waiting(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Lunch bag', '为美国上班族带饭做一条 15 秒视频。')
    pipeline = Pipeline(store)
    for stage in ('intake', 'direction'):
        attempt = pipeline.start_attempt(RUN, stage)
        structure = ({'options': [{'key': key, 'title': title, 'claim': title,
                                  'satisfies': 'goal.primary'}
                                 for key, title in TITLES.items()]}
                     if stage == 'direction' else {})
        pipeline.submit(RUN, stage, attempt.token, content=f'Synthetic {stage}',
                        summary=stage, structured=structure)
    return store, pipeline


def proposed(waiting, receipt, *, option='A', quote=None):
    store, pipeline = waiting
    card = candidates(store, pipeline, RUN)[0]
    return {'action': 'select_direction',
            'target': {**card['target'], 'option_id': option},
            'message_id': receipt['message_id'],
            'display_context_id': receipt['display_context_id'],
            **({'evidence_quote': quote} if quote is not None else {})}


def decide(waiting, text, *, option='A', quote=None):
    store, pipeline = waiting
    receipt = received_message(store, pipeline, RUN, text)
    candidate = proposed(waiting, receipt, option=option, quote=quote)
    return receipt, candidate, resolve(candidates(store, pipeline, RUN), candidate, receipt=receipt)


def test_titles_come_from_same_exact_pending_artifact(waiting):
    store, pipeline = waiting
    timeline = build_timeline(store, pipeline, Ledger(store, settings_for(store.root)), RUN, None)
    pending = timeline['pending']
    card = candidates(store, pipeline, RUN)[0]
    assert card['target'] == {key: pending[key]
                              for key in ('stage', 'version', 'artifact_sha256')}
    assert {row['key']: row['title'] for row in pending['options']} == TITLES
    assert {row['id']: row['title'] for row in card['answers'][0]['options']} == TITLES


@pytest.mark.parametrize('quote', ['采用当前方向 A「前袋先开口」',
                                   '采用当前方向 A「前袋先开口」，按工作日上班带饭继续。',
                                   ORIGINAL, None])
def test_real_full_message_commits_without_reasking_and_preserves_source(waiting, quote):
    store, pipeline = waiting
    receipt, _, verdict = decide(waiting, ORIGINAL, quote=quote)
    assert verdict['status'] == 'commit', verdict
    if quote:
        assert verdict['action']['evidence_quote'] == quote
    result = commit(store, pipeline, RUN, verdict['action'], source='conversation',
                    user_message='model rewrite must not replace the receipt',
                    request_id=receipt['request_id'])
    assert result['selected_option_id'] == 'A'
    assert result['continuation_status'] == 'pending'
    approval = store.list_approvals(RUN)[0]
    assert approval.artifact_version == 1
    assert approval.artifact_sha256 == receipt['displayed']['decisions'][0]['artifact_sha256']
    assert approval.note == ''
    followup = next(e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_requested')
    assert ORIGINAL in followup['text']
    assert followup['source_user_input_id'] == receipt['message_id']
    with pytest.raises(DecisionError, match='message_consumed'):
        commit(store, pipeline, RUN, verdict['action'], source='conversation')
    assert len(store.list_approvals(RUN)) == 1


@pytest.mark.parametrize('left,right', [('「', '」'), ('“', '”'), ('"', '"'), ("'", "'")])
def test_exact_title_reference_is_not_quoted_other_speech(waiting, left, right):
    text = f'采用当前方向 A{left}前袋先开口{right}，先完成素材方案和脚本。'
    _, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert verdict['status'] == 'commit', verdict


@pytest.mark.parametrize('text,option', [
    ('采用当前方向 A「门口交接」', 'A'),
    ('采用当前方向 A「不存在的标题」', 'A'),
    ('采用当前方向 A「前袋先开口」', 'B'),
    ('采用「前袋先开口」', 'A'),
    ('「采用当前方向 A「前袋先开口」」', 'A'),
    ('同事说“采用当前方向 A「前袋先开口」”，你怎么看？', 'A'),
    ('采用方向 A 和 B', 'A'),
])
def test_title_cannot_invent_intent_or_substitute_another_option(waiting, text, option):
    _, _, verdict = decide(waiting, text, option=option, quote=text)
    assert verdict['status'] != 'commit'
    assert not waiting[0].list_approvals(RUN)


@pytest.mark.parametrize('text,quote', [
    ('不要采用方向 A', '采用方向 A'),
    ('如果采用方向 A，就继续', '采用方向 A'),
    ('同事说采用方向 A', '采用方向 A'),
    ('我还没决定采用方向 A', '采用方向 A'),
    ('采用当前方向 A「前袋先开口」', '方向 A'),
])
def test_model_cannot_slice_away_source_context(waiting, text, quote):
    _, _, verdict = decide(waiting, text, quote=quote)
    assert verdict['status'] == 'clarify'
    assert verdict['reason'] == 'evidence_not_decision_clause'
    assert not waiting[0].list_approvals(RUN)


@pytest.mark.parametrize('text', ['不要采用当前方向 A「前袋先开口」',
                                  '如果采用当前方向 A「前袋先开口」，就继续',
                                  '同事说采用当前方向 A「前袋先开口」',
                                  '是否采用当前方向 A「前袋先开口」？'])
def test_complete_evidence_does_not_turn_non_decisions_into_adoption(waiting, text):
    _, _, verdict = decide(waiting, text, quote=text)
    assert verdict['status'] != 'commit'
    assert not waiting[0].list_approvals(RUN)


def test_ancillary_question_and_downstream_stages_do_not_become_a_second_decision(waiting):
    text = '采用当前方向 A，肩带是否随附我不确定。先完成素材方案和脚本，不生成媒体。'
    _, _, verdict = decide(waiting, text, quote='采用当前方向 A')
    assert verdict['status'] == 'commit', verdict


@pytest.mark.parametrize('text', [
    PHOTO_FEEDBACK,
    '采用方向 A，文案不要写“无需另配收纳袋”；不重新让我选 A/B/C。',
    '采用方向 A，caption 不写 "perfect insulation"。',
    '采用方向 A，同事说“采用 B”，这是他的意见，不要改变我的选择。',
    '采用方向 A 的收纳结构路线，继续做逐页方案。',
    '采用方向 A 的分区思路，先准备素材方案和图文方案。',
])
def test_initial_adoption_is_not_invalidated_by_later_copy_feedback(waiting, text):
    store, pipeline = waiting
    receipt, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert verdict['status'] == 'commit', verdict
    original = store.read_artifact_text(RUN, 'direction', 1)
    result = commit(store, pipeline, RUN, verdict['action'], source='conversation',
                    request_id=receipt['request_id'])
    assert result['selected_option_id'] == 'A'
    assert len(store.list_approvals(RUN)) == 1
    assert store.read_artifact_text(RUN, 'direction', 1) == original
    followup = next(e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_requested')
    assert text in followup['text']
    assert followup['source_user_input_id'] == receipt['message_id']
    assert not store.list_approvals(RUN, 'writing')


@pytest.mark.parametrize('text', [
    'A 的收纳结构主线，继续做三页图文方案。',
    '采用方向 A 的收纳结构主线？',
    '不要采用方向 A 的收纳结构主线，继续讨论。',
    '如果采用方向 A 的收纳结构主线，就继续做逐页方案。',
    '同事说采用方向 A 的收纳结构主线，文案不要写“保温”。',
    '“采用方向 A 的收纳结构主线”，不重新让我选 A/B/C。',
    '采用方向 A 和 B 的收纳结构主线，继续做逐页方案。',
    '采用方向 A 的收纳结构主线，改用方向 B。',
    '采用方向 A 的收纳结构主线，换成方向 B。',
    '采用方向 A 的收纳结构主线，我改选 B。',
    '采用方向 A 的收纳结构主线，再采用方向 B 的拿法路线。',
    '采用方向 A 的收纳结构主线，但不要采用方向 A 的收纳结构主线。',
    '采用方向 A 的收纳结构主线，如果餐具位不存在就不要采用。',
    '采用方向 A 的收纳结构主线，但如果餐具不随附就先不要继续。',
])
def test_route_description_cannot_invent_or_hide_conflicting_decisions(waiting, text):
    _, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert verdict['status'] != 'commit', verdict
    assert not waiting[0].list_approvals(RUN)


def test_route_description_still_requires_the_whole_initial_clause(waiting):
    _, _, verdict = decide(waiting, PHOTO_FEEDBACK, quote='采用方向 A')
    assert verdict['status'] == 'clarify'
    assert verdict['reason'] == 'evidence_not_decision_clause'


def test_later_option_mentions_do_not_let_model_change_initial_choice(waiting):
    _, _, verdict = decide(waiting, PHOTO_FEEDBACK, option='B', quote=PHOTO_FEEDBACK.split('，')[0])
    assert verdict['status'] == 'clarify'
    assert verdict['reason'] == 'option_not_user_choice'


def test_mixed_photo_feedback_through_the_actual_action_handler(waiting):
    from test_conversation_ingress import perform
    store, pipeline = waiting
    receipt = received_message(store, pipeline, RUN, PHOTO_FEEDBACK)
    candidate = proposed(waiting, receipt, quote=PHOTO_FEEDBACK.split('，')[0])
    _, _, model = perform(store, pipeline, PHOTO_FEEDBACK, candidate, receipt=receipt)
    assert len(model.calls) == 2
    assert store.list_approvals(RUN)[0].selected_option_id == 'A'
    events = store.read_events(RUN)
    assert not any(e['kind'] == 'user_action_rejected' for e in events)
    assert len([e for e in events if e['kind'] == 'user_action_committed']) == 1
    followup = next(e for e in events if e['kind'] == 'decision_followup_requested')
    assert PHOTO_FEEDBACK in followup['text']


def test_title_adoption_can_pause_without_scheduling(waiting):
    store, pipeline = waiting
    text = '采用当前方向 A「前袋先开口」，暂时不要继续。'
    _, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert verdict['status'] == 'commit', verdict
    result = commit(store, pipeline, RUN, verdict['action'], source='conversation')
    assert result['continuation_status'] == 'paused'
    assert store.list_approvals(RUN)[0].continue_after is False
    assert not any(e['kind'] == 'decision_followup_requested' for e in store.read_events(RUN))


def test_title_identity_cannot_be_supplied_by_a_tampered_display(waiting):
    store, pipeline = waiting
    receipt, candidate, _ = decide(waiting, '采用当前方向 A「门口交接」')
    changed = copy.deepcopy(receipt)
    changed['displayed']['decisions'][0]['answers'][0]['options'][0]['title'] = '门口交接'
    verdict = resolve(candidates(store, pipeline, RUN), candidate, receipt=changed)
    assert verdict['status'] == 'reject'
    assert verdict['reason'] == 'target_not_displayed'
    assert not store.list_approvals(RUN)


def test_commit_rechecks_the_complete_original_quote(waiting):
    store, pipeline = waiting
    _, _, verdict = decide(waiting, ORIGINAL, quote='采用当前方向 A「前袋先开口」')
    assert verdict['status'] == 'commit'
    changed = {**verdict['action'], 'evidence_quote': '方向 A'}
    with pytest.raises(DecisionError, match='请明确'):
        commit(store, pipeline, RUN, changed, source='conversation')
    assert not store.list_approvals(RUN)


def test_new_version_cannot_rebase_an_old_title_receipt(waiting):
    store, pipeline = waiting
    receipt, candidate, _ = decide(waiting, ORIGINAL, quote='采用当前方向 A「前袋先开口」')
    old = store.latest_artifact(RUN, 'direction')
    pipeline.record_approval(RUN, 'direction', nonce=pipeline.issue_nonce(RUN, 'direction'),
                             decision='returned', note='change')
    attempt = pipeline.start_attempt(RUN, 'direction')
    pipeline.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=old.structured)
    verdict = resolve(candidates(store, pipeline, RUN), candidate, receipt=receipt)
    assert verdict['status'] == 'reject'
    assert verdict['reason'] == 'version_not_current'
    assert len(store.list_approvals(RUN)) == 1


@pytest.mark.parametrize('suffix', ['采用当前脚本', '同时采用完整方案',
                                   '再采用方向 A', '不要采用方向 A',
                                   '不要采用当前方向 A「前袋先开口」', '我还没决定'])
def test_later_actual_decisions_or_countermands_are_not_lost(waiting, suffix):
    text = f'采用当前方向 A「前袋先开口」，{suffix}。'
    _, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert verdict['status'] == 'clarify'
    assert verdict['reason'] in {'negative_message', 'several_targets_named'}
    assert not waiting[0].list_approvals(RUN)


def test_title_punctuation_is_part_of_the_exact_option_not_a_clause_boundary(waiting):
    store, pipeline = waiting
    old = store.latest_artifact(RUN, 'direction')
    pipeline.record_approval(RUN, 'direction', nonce=pipeline.issue_nonce(RUN, 'direction'),
                             decision='returned', note='new title')
    attempt = pipeline.start_attempt(RUN, 'direction')
    structure = copy.deepcopy(old.structured)
    structure['options'][0]['title'] = '前袋，先开口'
    pipeline.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=structure)
    text = '采用当前方向 A「前袋，先开口」，先完成素材方案和脚本。'
    _, _, verdict = decide(waiting, text, quote='采用当前方向 A「前袋，先开口」')
    assert verdict['status'] == 'commit', verdict



def test_original_message_through_actual_orchestrator_action_handler(waiting):
    from test_conversation_ingress import perform
    store, pipeline = waiting
    receipt = received_message(store, pipeline, RUN, ORIGINAL)
    candidate = proposed(waiting, receipt, quote='采用当前方向 A「前袋先开口」')
    _, _, model = perform(store, pipeline, ORIGINAL, candidate, receipt=receipt)
    approval = store.list_approvals(RUN)[0]
    assert approval.selected_option_id == 'A'
    assert approval.source == 'conversation'
    assert len(model.calls) == 2
    events = store.read_events(RUN)
    assert not any(e['kind'] == 'user_action_rejected' for e in events)
    assert len([e for e in events if e['kind'] == 'user_action_committed']) == 1
    followup = next(e for e in events if e['kind'] == 'decision_followup_requested')
    assert ORIGINAL in followup['text']


@pytest.mark.parametrize('title,accepted', [('Door Pickup', True), ('door pickup', False)])
def test_title_text_is_exact_even_when_option_id_is_case_insensitive(waiting, title, accepted):
    store, pipeline = waiting
    old = store.latest_artifact(RUN, 'direction')
    pipeline.record_approval(RUN, 'direction', nonce=pipeline.issue_nonce(RUN, 'direction'),
                             decision='returned', note='new title')
    attempt = pipeline.start_attempt(RUN, 'direction')
    structure = copy.deepcopy(old.structured)
    structure['options'][0]['title'] = 'Door Pickup'
    pipeline.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=structure)
    text = f'采用当前方向 a「{title}」，先完成素材方案和脚本。'
    _, _, verdict = decide(waiting, text, quote=text.split('，')[0])
    assert (verdict['status'] == 'commit') is accepted
