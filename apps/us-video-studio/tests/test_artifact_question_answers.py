"""Exact artifact questions accept real natural answers without new authority.

Only local Store/Pipeline/actual handlers and a scripted text model are used.
No real project, visual model or generation provider is touched.
"""
import copy

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.active_brief import compile_active_brief, fingerprint
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import accept, receive, start_execution
from studio.decision import candidates, commit, resolve
from studio.interactions import artifact_responses, pending_question, record_answer
from studio.ledger import Ledger
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.project_brief import project_brief
from studio.store import Store
from studio.timeline import build_timeline
from test_conversational_decision import RUN, settings_for
from test_decision import received_message
from test_decision_title_clause import ORIGINAL


STRAP = '这条肩带是否随附？不确定时仅保留可见形态。'
SCENE = '主要用于工作日上班带饭还是周末野餐？'
STRAP_RESPONSE = '这条肩带是否随附我不确定，保留图中可见形态，不宣称随附'
SCENE_RESPONSE = '采用当前方向 A「前袋先开口」，按工作日上班带饭继续'


@pytest.fixture
def waiting(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Lunch bag', '为美国上班族带饭做约15秒4镜视频。')
    pipe = Pipeline(store)
    attempt = pipe.start_attempt(RUN, 'intake')
    pipe.submit(RUN, 'intake', attempt.token, content='Fixture intake', summary='intake')
    attempt = pipe.start_attempt(RUN, 'direction')
    pipe.submit(RUN, 'direction', attempt.token, content='Fixture direction', summary='direction',
        structured={'options': [{'key': 'A', 'title': '前袋先开口', 'claim': '动作演示',
                                 'satisfies': 'goal.primary'}],
            'open_questions': [
                {'question': STRAP, 'owner': 'user', 'timing': 'now', 'kind': 'fact',
                 'blocks': '不使用随附承诺', 'reason': '只有上架资料能核对'},
                {'question': SCENE, 'owner': 'user', 'timing': 'now', 'kind': 'local_design',
                 'blocks': '使用场景', 'reason': '由用户决定'}]})
    return store, pipe


def receipt_for(waiting, text=ORIGINAL):
    store, pipe = waiting
    receipt = received_message(store, pipe, RUN, text)
    accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    return receipt


def arguments(receipt, question=STRAP, quote=STRAP_RESPONSE, status='unknown'):
    row = next(row for row in receipt['displayed']['questions'] if row['question'] == question)
    return {'question_id': row['question_id'], 'quote': quote, 'answer_status': status,
            'artifact_target': {key: row[key] for key in ('stage', 'version', 'artifact_sha256')}}


def issue_for(waiting, question=STRAP):
    store, pipe = waiting
    brief = project_brief(pipe.state(RUN).to_dict(), chat=store.read_chat(RUN), store=store, run_id=RUN)
    return next(row for row in brief['issues'] if row['question'] == question)


def adoption(waiting, receipt):
    store, pipe = waiting
    return {'action': 'select_direction',
            'target': {**candidates(store, pipe, RUN)[0]['target'], 'option_id': 'A'},
            'message_id': receipt['message_id'], 'display_context_id': receipt['display_context_id'],
            'evidence_quote': '采用当前方向 A「前袋先开口」', 'continue_after': True}


def test_unknown_response_is_grounded_without_changing_fact_artifact_or_brief(waiting):
    store, pipe = waiting
    receipt = receipt_for(waiting)
    artifact = store.latest_artifact(RUN, 'direction')
    before = compile_active_brief(store, RUN)
    body = store.read_artifact_text(RUN, 'direction', 1)
    event = record_answer(store, RUN, receipt, **arguments(receipt))
    assert event['quote'] == STRAP_RESPONSE and event['answer_status'] == 'unknown'
    issue = issue_for(waiting)
    assert issue['answered'] and not issue['ask_user']
    assert issue['answer_status'] == 'unknown' and issue['answer_quote'] == STRAP_RESPONSE
    assert issue['answer_source_message_id'] == receipt['message_id']
    after = compile_active_brief(store, RUN)
    assert after['goal'] == before['goal'] and after['clauses'] == before['clauses']
    assert fingerprint(after) == fingerprint(before)
    assert after['source']['question_responses'][0]['quote'] == STRAP_RESPONSE
    assert store.latest_artifact(RUN, 'direction') == artifact
    assert store.read_artifact_text(RUN, 'direction', 1) == body
    assert not pipe.stage_state(RUN, 'direction').stale
    assert store.list_approvals(RUN) == []
    assert pending_question(store, RUN) is None
    settings = settings_for(store.root)
    timeline = build_timeline(store, pipe, Ledger(store, settings), RUN, None)
    assert [row['question'] for row in timeline['questions']] == [SCENE]
    rail = next(row for row in timeline['rail'] if row['stage'] == 'direction')
    assert not any(row['question'] == STRAP for row in rail['questions'] + rail['team_issues'])


@pytest.mark.parametrize('decision_first', [False, True])
def test_two_answers_and_adoption_in_one_actual_tool_batch(waiting, decision_first):
    store, pipe = waiting
    receipt = received_message(store, pipe, RUN, ORIGINAL)
    before_fp = fingerprint(compile_active_brief(store, RUN))
    calls = [tool_turn('strap', 'record_question_answer', arguments(receipt)),
             tool_turn('scene', 'record_question_answer', arguments(receipt, SCENE, SCENE_RESPONSE, 'answered'))]
    deciding = tool_turn('adopt', 'propose_user_action', adoption(waiting, receipt))
    calls.insert(0 if decision_first else len(calls), deciding)
    batch = copy.deepcopy(calls[0])
    batch.tool_calls = [call for turn in calls for call in turn.tool_calls]
    batch.raw_blocks = [block for turn in calls for block in turn.raw_blocks]
    model = ScriptedModel([batch, final_turn('按原话继续。')], legacy_task_contracts=False)
    settings = settings_for(store.root)
    turn = Orchestrator(store, pipe, settings, model, Ledger(store, settings), RUN).send(
        ORIGINAL, user_input_id=receipt['message_id'])
    assert all(row['ok'] for row in turn.tool_log), turn.tool_log
    responses = artifact_responses(store, RUN)
    assert len(responses) == 2 and {row['source_message_id'] for row in responses} == {receipt['message_id']}
    assert all(issue_for(waiting, q)['answered'] for q in (STRAP, SCENE))
    assert len(store.list_approvals(RUN)) == 1
    approved = store.list_approvals(RUN)[0]
    assert approved.selected_option_id == 'A' and approved.artifact_version == 1
    assert not pipe.stage_state(RUN, 'direction').stale
    assert fingerprint(compile_active_brief(store, RUN)) == before_fp
    events = store.read_events(RUN)
    assert len([e for e in events if e['kind'] == 'user_action_committed']) == 1
    followups = [e for e in events if e['kind'] == 'decision_followup_requested']
    assert len(followups) == 1 and ORIGINAL in followups[0]['text']
    assert not any(e['kind'] in {'production_attempt', 'execution_authorized', 'user_task_interpretation_recorded'}
                   for e in events)
    assert 'artifact_target' in model.requests_text() and STRAP in model.requests_text()


@pytest.mark.parametrize('quote', ['肩带是否随附我不确定', '保留图中可见形态', '不宣称随附'])
def test_response_cannot_be_sliced_to_hide_context(waiting, quote):
    store, _ = waiting
    receipt = receipt_for(waiting)
    with pytest.raises(PipelineError, match='完整连续回答'):
        record_answer(store, RUN, receipt, **arguments(receipt, quote=quote))
    assert artifact_responses(store, RUN) == []


@pytest.mark.parametrize('text,status,closed', [
    ('肩带不随附，不要写成随附。', 'answered', True),
    ('我不知道肩带是否随附。', 'unknown', True),
    ('如果肩带随附才使用，否则我还没决定。', 'needs_clarification', False),
    ('肩带随附吗？', 'needs_clarification', False),
])
def test_negative_unknown_and_ambiguous_response_preserve_literal_meaning(waiting, text, status, closed):
    store, _ = waiting
    receipt = receipt_for(waiting, text)
    record_answer(store, RUN, receipt, **arguments(receipt, quote=text, status=status))
    issue = issue_for(waiting)
    assert issue['answered'] is closed and issue['ask_user'] is not closed
    assert issue['answer_status'] == status and issue['answer_quote'] == text
    assert artifact_responses(store, RUN)[0]['quote'] == text
    assert artifact_responses(store, RUN)[0]['answer_status'] == status
    assert store.list_approvals(RUN) == []


def test_artifact_response_requires_an_explicit_uncertainty_status(waiting):
    store, _ = waiting
    receipt = receipt_for(waiting)
    args = arguments(receipt)
    del args['answer_status']
    with pytest.raises(PipelineError, match='回答状态'):
        record_answer(store, RUN, receipt, **args)
    assert artifact_responses(store, RUN) == []


@pytest.mark.parametrize('change', ['version', 'hash', 'question', 'fabricated_quote', 'source'])
def test_wrong_identity_or_source_is_rejected_before_writing(waiting, change):
    store, _ = waiting
    receipt = receipt_for(waiting)
    args = arguments(receipt)
    if change == 'version': args['artifact_target']['version'] += 1
    elif change == 'hash': args['artifact_target']['artifact_sha256'] = '0' * 64
    elif change == 'question': args['question_id'] = 'artifact-question:' + 'f' * 64
    elif change == 'fabricated_quote': args['quote'] = '肩带一定随附'
    else:
        receipt = receive(store, RUN, ORIGINAL, 'foreign-current-source')
        accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError): record_answer(store, RUN, receipt, **args)
    assert store.read_events(RUN) == before


@pytest.mark.parametrize('text', ['停止', '现在进度怎么样？', '先只讨论，不改稿'])
def test_stop_readonly_and_discussion_do_not_close_artifact_question(waiting, text):
    store, _ = waiting
    receipt = receipt_for(waiting, text)
    with pytest.raises(PipelineError): record_answer(store, RUN, receipt, **arguments(receipt, quote=text))
    assert not issue_for(waiting)['answered']


def test_internal_continuation_has_no_fresh_answer_authority(waiting):
    store, _ = waiting
    receipt = receipt_for(waiting)
    with pytest.raises(PipelineError, match='内部接续'):
        record_answer(store, RUN, None, **arguments(receipt))
    assert artifact_responses(store, RUN) == []


def test_queued_message_or_rewritten_receipt_is_not_answer_authority(waiting):
    store, pipe = waiting
    receipt = received_message(store, pipe, RUN, ORIGINAL)
    with pytest.raises(PipelineError, match='排队消息'):
        record_answer(store, RUN, receipt, **arguments(receipt))
    accept(store, RUN, receipt); start_execution(store, RUN, receipt)
    rewritten = {**receipt, 'text': 'model-authored replacement'}
    with pytest.raises(PipelineError, match='真实用户原话'):
        record_answer(store, RUN, rewritten, **arguments(receipt))
    assert artifact_responses(store, RUN) == []


def test_same_response_is_idempotent_but_cannot_be_rewritten(waiting):
    store, _ = waiting
    receipt = receipt_for(waiting)
    args = arguments(receipt)
    first = record_answer(store, RUN, receipt, **args)
    before = store.read_events(RUN)
    assert record_answer(store, RUN, receipt, **args) == first
    assert store.read_events(RUN) == before
    with pytest.raises(PipelineError, match='不能改写'):
        record_answer(store, RUN, receipt, **{**args, 'answer_status': 'answered'})
    assert store.read_events(RUN) == before


def test_new_artifact_cannot_be_answered_from_previous_display(waiting):
    store, pipe = waiting
    receipt = receipt_for(waiting)
    old = store.latest_artifact(RUN, 'direction')
    pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                         decision='returned', note='fixture new version')
    attempt = pipe.start_attempt(RUN, 'direction')
    pipe.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=old.structured)
    with pytest.raises(PipelineError, match='变更或失效'):
        record_answer(store, RUN, receipt, **arguments(receipt))
    assert artifact_responses(store, RUN) == []


def test_saved_answer_does_not_close_same_worded_new_version(waiting):
    store, pipe = waiting
    receipt = receipt_for(waiting)
    record_answer(store, RUN, receipt, **arguments(receipt))
    old = store.latest_artifact(RUN, 'direction')
    pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                         decision='returned', note='fixture new version')
    attempt = pipe.start_attempt(RUN, 'direction')
    pipe.submit(RUN, 'direction', attempt.token, content='new', summary='new', structured=old.structured)
    issue = issue_for(waiting)
    assert issue['version'] == 2 and not issue['answered'] and issue['ask_user']


def test_artifact_corruption_is_not_hidden_as_an_answer(waiting):
    store, _ = waiting
    receipt = receipt_for(waiting)
    args = arguments(receipt)
    path, _ = store._artifact_paths(RUN, 'direction', 1)
    path.write_bytes(path.read_bytes() + b'\nfixture corruption')
    with pytest.raises(PipelineError): record_answer(store, RUN, receipt, **args)
    assert not any(e['kind'] == 'artifact_question_response_recorded' for e in store.read_events(RUN))


def test_read_projection_rechecks_saved_answer_source_and_artifact_bytes(waiting):
    store, _ = waiting
    receipt = receipt_for(waiting)
    record_answer(store, RUN, receipt, **arguments(receipt))
    path, _ = store._artifact_paths(RUN, 'direction', 1)
    path.write_bytes(path.read_bytes() + b'\nfixture corruption')
    with pytest.raises(PipelineError): artifact_responses(store, RUN)


@pytest.mark.parametrize('later_draft', [False, True])
def test_durable_product_question_at_later_timing_uses_same_exact_answer_boundary(waiting, later_draft):
    store, pipe = waiting
    old = store.latest_artifact(RUN, 'direction')
    pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                         decision='returned', note='fixture product question')
    structured = copy.deepcopy(old.structured)
    structured['open_questions'] = [{'question': '主推哪一色？', 'owner': 'user', 'timing': 'later',
        'kind': 'product_choice', 'blocks': '色号身份', 'reason': '只有用户知道在售色号'}]
    attempt = pipe.start_attempt(RUN, 'direction')
    pipe.submit(RUN, 'direction', attempt.token, content='fixture product question',
                summary='fixture', structured=structured)
    receipt = receipt_for(waiting, '我还不知道主推色号，先保留未知。')
    if later_draft:
        pipe.record_approval(RUN, 'direction', nonce=pipe.issue_nonce(RUN, 'direction'),
                             decision='returned', note='fixture later version')
        attempt = pipe.start_attempt(RUN, 'direction')
        pipe.submit(RUN, 'direction', attempt.token, content='fixture later version',
                    summary='fixture', structured={'options': structured['options']})
    args = arguments(receipt, '主推哪一色？', receipt['text'], 'unknown')
    record_answer(store, RUN, receipt, **args)
    if not later_draft:
        issue = issue_for(waiting, '主推哪一色？')
        assert issue['answered'] and issue['answer_status'] == 'unknown' and not issue['ask_user']
    else:
        assert store.latest_artifact(RUN, 'direction').version == 3
        assert artifact_responses(store, RUN)[0]['target']['version'] == 2
    from studio.project_brief import durable_questions
    assert durable_questions(store, RUN, chat=store.read_chat(RUN)) == []


def test_unknown_response_then_commit_keeps_exact_adoption_scope(waiting):
    store, pipe = waiting
    receipt = receipt_for(waiting)
    proposed = adoption(waiting, receipt)
    verdict = resolve(candidates(store, pipe, RUN), proposed, receipt=receipt)
    assert verdict['status'] == 'commit'
    record_answer(store, RUN, receipt, **arguments(receipt))
    result = commit(store, pipe, RUN, verdict['action'], source='conversation')
    assert result['selected_option_id'] == 'A' and result['continuation_status'] == 'pending'
    assert store.list_approvals(RUN)[0].artifact_sha256 == proposed['target']['artifact_sha256']
