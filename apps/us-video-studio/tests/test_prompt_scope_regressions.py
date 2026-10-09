"""Real receipt/decision/context boundaries for audit R2, R3 and R7.

These are offline protocol counterexamples, not claims of model understanding.
"""
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import (accept, applicable_constraints, continue_after,
    interpreted_task_constraints, receive, record_task_interpretation, start_execution)
from studio.decision import candidates, commit, continuation_intent, resolve
from studio.learning import LearningService
from studio.models import PipelineError
from studio.task_context import assemble_role_task
from studio.tools.dispatch import Dispatcher
from studio.tools.workspace import ToolError, Workspace
from test_context_projection import clause, proposal
from test_conversational_decision import (RUN as DECISION_RUN, orchestrator,
    seed_direction)
from test_decision import received_message
from test_revision_scope import RUN, QUOTE, env, patch, plan, plan_args, revision_meaning


def edit_proposal(receipt, *rows):
    # These fixtures explicitly edit the first line. This is not a classifier
    # or a compatibility bypass for missing/ambiguous scope.
    edit = revision_meaning(receipt['text'])['constraints'][0]
    return proposal(receipt, edit, *rows, mode='revise', deliverable='revision')


def begin(store, run_id, text, **kwargs):
    receipt = receive(store, run_id, text, f'scope-{len(store.read_events(run_id))}', **kwargs)
    accept(store, run_id, receipt)
    start_execution(store, run_id, receipt)
    return receipt


def paused_proposal(receipt):
    return proposal(receipt, deliverable='follow_up', allowed_stages=[], pause_after=True)


def test_semantic_pause_blocks_real_decision_continuation(tmp_path):
    store, pipe = seed_direction(tmp_path)
    text = '采用 B，这轮到此为止。'
    receipt = received_message(store, pipe, DECISION_RUN, text)
    accept(store, DECISION_RUN, receipt)
    start_execution(store, DECISION_RUN, receipt)
    interpretation = record_task_interpretation(
        store, DECISION_RUN, receipt, paused_proposal(receipt))
    verdict = resolve(candidates(store, pipe, DECISION_RUN),
        {'action': 'select_direction', 'option_id': 'B', 'continue_after': True},
        receipt=receipt, interpretation=interpretation)
    assert verdict['status'] == 'commit' and verdict['action']['continue_after'] is False

    # Even a stale/model-authored action that says continue must be rechecked
    # against the saved semantics at the actual write boundary.
    result = commit(store, pipe, DECISION_RUN,
        {**verdict['action'], 'continue_after': True, 'pause_after': False}, source='conversation')
    assert result['continuation_status'] == 'paused'
    approval = store.list_approvals(DECISION_RUN, 'direction')[0]
    assert approval.selected_option_id == 'B' and not approval.continue_after
    assert continuation_intent(store, pipe, DECISION_RUN) is None
    assert not any(event['kind'] == 'decision_followup_requested'
                   for event in store.read_events(DECISION_RUN))


def test_semantic_pause_through_real_orchestrator_tool_loop(tmp_path):
    store, pipe = seed_direction(tmp_path)
    text = '采用 B，这轮到此为止。'
    task = {'task': {'quote': text, 'mode': 'produce', 'deliverable': 'follow_up',
        'format': 'none', 'allowed_stages': [], 'pause_after': True}, 'constraints': []}
    orch, model = orchestrator(store, pipe, tmp_path, [
        tool_turn('interpret', 'propose_task_interpretation', task),
        tool_turn('choose', 'propose_user_action',
                  {'action': 'select_direction', 'option_id': 'B', 'continue_after': True}),
        final_turn('已采用 B，本轮停在这里。'),
    ])
    orch.send(text)
    assert store.list_approvals(DECISION_RUN, 'direction')[0].continue_after is False
    reply = next(block for message in model.calls[-1]['messages']
                 if isinstance(message.get('content'), list) for block in message['content']
                 if block.get('type') == 'tool_result' and block.get('tool_use_id') == 'choose')
    assert json.loads(reply['content'])['continuation_status'] == 'paused'
    assert not any(event['kind'] == 'decision_followup_requested'
                   for event in store.read_events(DECISION_RUN))


def test_pause_for_another_receipt_cannot_control_current_decision(tmp_path):
    store, pipe = seed_direction(tmp_path)
    old = begin(store, DECISION_RUN, 'This round ends here.')
    saved = record_task_interpretation(store, DECISION_RUN, old, paused_proposal(old))
    current = received_message(store, pipe, DECISION_RUN, '采用 B，继续。')
    with pytest.raises(PipelineError, match='真实用户消息'):
        continue_after(current, {'continue_after': True}, interpretation=saved)
    verdict = resolve(candidates(store, pipe, DECISION_RUN),
        {'action': 'select_direction', 'option_id': 'B'}, receipt=current)
    result = commit(store, pipe, DECISION_RUN,
        {**verdict['action'], 'interpretation': saved, 'pause_after': True}, source='conversation')
    assert result['continuation_status'] == 'pending'


def test_false_semantic_pause_never_overrides_explicit_pause(tmp_path):
    store, pipe = seed_direction(tmp_path)
    receipt = begin(store, DECISION_RUN, '采用 B，不要继续。')
    interpretation = record_task_interpretation(store, DECISION_RUN, receipt,
        proposal(receipt, mode='discuss', deliverable='follow_up',
                 allowed_stages=[], pause_after=False))
    assert not continue_after(receipt, {'continue_after': True}, interpretation=interpretation)
    ordinary = begin(store, DECISION_RUN, '采用 B。')
    allowed = record_task_interpretation(store, DECISION_RUN, ordinary,
        proposal(ordinary, deliverable='follow_up', allowed_stages=[]))
    assert not continue_after(ordinary, {'continue_after': False}, interpretation=allowed)
    assert not store.list_approvals(DECISION_RUN)


@pytest.mark.parametrize('intent', ['instruction', 'question', 'hypothesis', 'reported', 'uncertain'])
def test_field_content_condition_does_not_shrink_full_task(env, intent):
    store, pipe, settings, ledger = env
    approvals_before = store.list_approvals(RUN)
    text = 'Write a complete script. Keep the opening spoken line conversational.'
    receipt = begin(store, RUN, text)
    meaning = proposal(receipt, clause('Keep the opening spoken line conversational',
        lifetime='turn', scope_quote='', paths=[['segments', 0, 'line']], intent=intent))
    interpretation = record_task_interpretation(store, RUN, receipt, meaning)
    assert interpreted_task_constraints(receipt, interpretation)['local_only'] is False

    # A bound, unfulfilled full draft receives the existing one completion
    # reminder; the fake declines again rather than inventing a delivery.
    main = Orchestrator(store, pipe, settings, ScriptedModel([
        final_turn('Read only.'), final_turn('No full draft delivered.')]), ledger, RUN)
    main.send(text, user_input_id=receipt['message_id'])
    main._execute('prepare_creative_context', {'intent': 'continue', 'format': 'video',
        'deliverable': 'writing', 'task_interpretation': meaning},
        Workspace(store, pipe, RUN, 'orchestrator'))
    assert main._scope_selected and main._deliverable == 'writing'
    rows = applicable_constraints(store, RUN, stage='writing', user_input_id=receipt['message_id'])
    assert len(rows) == (1 if intent == 'instruction' else 0)
    assert store.list_approvals(RUN) == approvals_before


@pytest.mark.parametrize('targeted', [False, True])
def test_explicit_local_edit_guard_survives_content_scope_fix(env, targeted):
    store = env[0]
    text = 'Make this line more natural.' if targeted else '只把第一句改自然，其他保留。'
    target = {'stage': 'writing', 'path': ['segments', 0, 'line']} if targeted else None
    receipt = begin(store, RUN, text, feedback_target=target)
    interpretation = record_task_interpretation(store, RUN, receipt,
        proposal(receipt, mode='revise', deliverable='revision') if targeted else edit_proposal(receipt))
    assert interpreted_task_constraints(receipt, interpretation)['local_only']


def test_field_tone_condition_allows_full_script_submission(env):
    store, pipe, settings, ledger = env
    text = 'Write a complete script. Keep the opening spoken line conversational.'
    receipt = begin(store, RUN, text)
    interpretation = record_task_interpretation(store, RUN, receipt,
        proposal(receipt, clause('Keep the opening spoken line conversational',
                                lifetime='turn', scope_quote='', paths=[['segments', 0, 'line']])))
    structure = json.loads(json.dumps(store.latest_artifact(RUN, 'writing').structured))
    structure['segments'][0]['line'] = 'Hey, try this for lunch.'
    structure['segments'][1]['line'] = 'I also changed the second line as part of the full script.'
    model = ScriptedModel([tool_turn('submit', 'submit_stage',
        {'summary': 'Complete script', 'structured': structure})])
    outcome = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing',
        'Write the complete script.', user_input_id=receipt['message_id'],
        turn_scope={'deliverable': 'writing', 'project_constraints': interpretation['project_constraints']})
    assert outcome.submitted == 'writing-v2'
    assert pipe.stage_state(RUN, 'writing').approval is None
    current = store.latest_artifact(RUN, 'writing').structured
    assert current['segments'][0]['line'] == structure['segments'][0]['line']
    assert current['segments'][1]['line'] == structure['segments'][1]['line']
    assert 'Keep the opening spoken line conversational' in model.requests_text()


def conflicted_second_line(env):
    store = env[0]
    receipt = begin(store, RUN, 'For this project, clarify the tone of the second spoken line.')
    record_task_interpretation(store, RUN, receipt, proposal(receipt,
        clause('clarify the tone of the second spoken line', value='unknown', operation='clarify',
               scope_quote='For this project', paths=[['segments', 1, 'line']],
               ambiguity='The second line tone is undecided.')))
    return applicable_constraints(store, RUN)


def assemble_local(env, scope, turn_scope=None):
    store, pipe, _, _ = env
    return assemble_role_task(store, pipe, LearningService(store), RUN,
        Workspace(store, pipe, RUN, 'writer', target_stage='writing'), 'writing', 'Revise the first line.',
        previous=store.latest_artifact(RUN, 'writing'), revision_scope=scope,
        method_context={'text': 'Offline methods'}, turn_scope=turn_scope)


@pytest.mark.parametrize('pinned', [False, True])
def test_local_role_context_ignores_conflict_at_another_field(env, pinned):
    rows = conflicted_second_line(env)
    scope = plan(env)
    context = assemble_local(env, scope, {'project_constraints': rows} if pinned else None)
    assert 'The second line tone is undecided.' not in context.seed
    assert 'clarify the tone of the second spoken line' not in context.seed
    assert not any(source['kind'] == 'user_constraint' for source in context.sources)
    assert scope['fields'][0]['path'] == ['segments', 0, 'line']


def test_local_dispatch_can_save_despite_another_shot_condition_conflict(env):
    store, pipe, settings, ledger = env
    rows = conflicted_second_line(env)
    receipt = begin(store, RUN, QUOTE)
    meaning = record_task_interpretation(store, RUN, receipt,
        edit_proposal(receipt))
    model = ScriptedModel([tool_turn('patch', 'submit_patch', patch())])
    outcome = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing', QUOTE,
        revision_scope=plan(env), user_input_id=receipt['message_id'],
        turn_scope={'deliverable': 'revision', 'project_constraints': meaning['project_constraints']})
    assert outcome.submitted == 'writing-v2'
    current = store.latest_artifact(RUN, 'writing').structured
    assert current['segments'][0]['line'] == 'Try this line'
    assert current['segments'][1]['line'] == 'Keep second line'
    assert pipe.stage_state(RUN, 'writing').approval is None
    assert any(row['status'] == 'needs_clarification' for row in rows)


@pytest.mark.parametrize('targeted', [False, True])
def test_revision_entry_binds_exact_scope_before_related_conflict_check(env, targeted):
    store, pipe, settings, ledger = env
    conflicted_second_line(env)
    base = store.latest_artifact(RUN, 'writing')
    target = {'stage': 'writing', 'version': base.version, 'artifact_sha256': base.sha256,
              'path': ['segments', 0, 'line']} if targeted else None
    receipt = begin(store, RUN, QUOTE, feedback_target=target)
    model = ScriptedModel([final_turn('Ready for the exact edit.'),
                           tool_turn('patch', 'submit_patch', patch())])
    main = Orchestrator(store, pipe, settings, model, ledger, RUN)
    main.send(QUOTE, user_input_id=receipt['message_id'])
    workspace = Workspace(store, pipe, RUN, 'orchestrator')
    selection = {'intent': 'edit', 'format': 'video', 'deliverable': 'revision',
        'task_interpretation': edit_proposal(receipt)}
    main._execute('prepare_creative_context', selection, workspace)
    bound = json.loads(main._execute('plan_revision', plan_args(store), workspace))
    # Rechecking the route now uses the validated plan's precise field paths.
    main._execute('prepare_creative_context', selection, workspace)
    reply = main._execute('dispatch', {'stage': 'writing', 'brief': QUOTE,
                                      'plan_id': bound['plan_id']}, workspace)
    result = json.loads(reply.content)
    assert result['status'] == 'submitted' and result['artifact']['key'] == 'writing-v2'
    assert store.latest_artifact(RUN, 'writing').structured['segments'][1]['line'] == 'Keep second line'


def test_revision_entry_rejects_conflict_at_the_selected_field(env):
    store, pipe, settings, ledger = env
    conflicted_second_line(env)
    base = store.latest_artifact(RUN, 'writing')
    target = {'stage': 'writing', 'version': base.version, 'artifact_sha256': base.sha256,
              'path': ['segments', 1, 'line']}
    receipt = begin(store, RUN, '只把第二句改自然，其他保留。', feedback_target=target)
    model = ScriptedModel([final_turn('Checking the requested field.')])
    main = Orchestrator(store, pipe, settings, model, ledger, RUN)
    main.send(receipt['text'], user_input_id=receipt['message_id'])
    with pytest.raises(ToolError, match='条件.*冲突'):
        main._execute('prepare_creative_context', {'intent': 'edit', 'format': 'video',
            'deliverable': 'revision', 'task_interpretation': proposal(receipt, mode='revise',
                                                                     deliverable='revision')},
            Workspace(store, pipe, RUN, 'orchestrator'))
    assert store.latest_artifact(RUN, 'writing').version == 1 and len(model.calls) == 1


def test_reported_field_condition_does_not_redirect_local_submission(env):
    store, pipe, settings, ledger = env
    text = QUOTE + '。同事建议第二句用正式口吻。'
    receipt = begin(store, RUN, text)
    interpretation = record_task_interpretation(store, RUN, receipt, edit_proposal(receipt,
        clause('同事建议第二句用正式口吻', lifetime='turn', scope_quote='', intent='reported',
               paths=[['segments', 1, 'line']])))
    scope = plan(env, quote=text)
    model = ScriptedModel([tool_turn('patch', 'submit_patch', patch())])
    outcome = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing', text,
        revision_scope=scope, user_input_id=receipt['message_id'],
        turn_scope={'deliverable': 'revision', 'project_constraints': interpretation['project_constraints']})
    assert outcome.submitted == 'writing-v2'
    assert store.latest_artifact(RUN, 'writing').structured['segments'][1]['line'] == 'Keep second line'


def test_initial_local_task_with_another_field_project_condition_preserves_scope(env):
    store, pipe, settings, ledger = env
    text = QUOTE + '。以后本项目第二句保持日常口吻。'
    receipt = begin(store, RUN, text)
    interpretation = record_task_interpretation(store, RUN, receipt, edit_proposal(receipt,
        clause('第二句保持日常口吻', scope_quote='以后本项目', paths=[['segments', 1, 'line']])))
    scope = plan(env, quote=text)
    model = ScriptedModel([tool_turn('patch', 'submit_patch', patch())])
    outcome = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing', text,
        revision_scope=scope, user_input_id=receipt['message_id'],
        turn_scope={'deliverable': 'revision', 'project_constraints': interpretation['project_constraints']})
    assert outcome.submitted == 'writing-v2'
    assert store.latest_artifact(RUN, 'writing').structured['segments'][1]['line'] == 'Keep second line'
    assert [row['id'] for row in applicable_constraints(store, RUN, paths=[['segments', 1, 'line']])]


@pytest.mark.parametrize('change,reason', [
    ({'path': ['segments', 1, 'line']}, '固定字段范围'),
    ({'version': 999}, '新反馈版本'),
    ({'artifact_sha256': '0' * 64}, '新反馈版本'),
    ({'stage': 'direction'}, '另一阶段'),
])
def test_direct_feedback_binding_cannot_escape_exact_local_plan(env, change, reason):
    store, pipe, settings, ledger = env
    base = store.latest_artifact(RUN, 'writing')
    target = {'stage': 'writing', 'version': base.version, 'artifact_sha256': base.sha256,
              'path': ['segments', 0, 'line'], **change}
    receipt = begin(store, RUN, QUOTE, feedback_target=target)
    interpretation = record_task_interpretation(store, RUN, receipt,
        proposal(receipt, mode='revise', deliverable='revision'))
    model = ScriptedModel([tool_turn('patch', 'submit_patch', patch()),
                           final_turn('Cannot save against this mismatched target.')])
    outcome = Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch('writing', QUOTE,
        revision_scope=plan(env), user_input_id=receipt['message_id'],
        turn_scope={'deliverable': 'revision', 'project_constraints': interpretation['project_constraints']})
    assert outcome.submitted is None
    assert store.latest_artifact(RUN, 'writing').version == 1
    assert pipe.stage_state(RUN, 'writing').open_attempt is None
    assert reason in model.requests_text()


@pytest.mark.parametrize('path', [['segments', 1, 'line'], ['segments', 1], ['segments']])
def test_related_field_conflict_still_blocks_role_context(env, path):
    rows = conflicted_second_line(env)
    # The validated plan uses an editable line. Broader condition paths must
    # overlap it; the test varies the condition, never widens the edit plan.
    rows[0]['scope']['paths'] = [path]
    scope = plan(env, path=['segments', 1, 'line'])
    with pytest.raises(ToolError, match='条件冲突'):
        assemble_local(env, scope, {'project_constraints': rows})


def test_rework_context_keeps_stage_wide_conflict_check(env):
    rows = conflicted_second_line(env)
    with pytest.raises(ToolError, match='条件冲突'):
        assemble_local(env, plan(env, mode='rework'), {'project_constraints': rows})
