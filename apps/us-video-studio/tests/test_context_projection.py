"""Offline journal reconstruction and protocol boundaries; no model calls."""
import copy
import json

import pytest

from studio.context_projection import build_conversation_context, compact_message_history
from studio.conversation_input import (accept, applicable_constraints, constraint_conflicts,
    interpreted_task_constraints, interpretation_for, project_constraints, receive,
    record_task_interpretation, start_execution)
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store

RUN = 'context-projection'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline creative case', 'Make a US creator product video.')
    return store, Pipeline(store)


def executing(env, text, identity=None):
    store, _ = env
    receipt = receive(store, RUN, text, identity or f'request-{len(store.read_events(RUN))}')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    return receipt


def clause(quote, *, key='voice.register', value='casual', lifetime='project',
           stages=None, paths=None, scope_quote=None, operation='add', supersedes=None,
           polarity='require', intent='instruction', ambiguity='', conflict_with=None):
    return {'quote': quote, 'key': key, 'value': value, 'operation': operation,
            'polarity': polarity, 'intent': intent,
            'scope': {'lifetime': lifetime, 'stages': stages or ['writing'], 'paths': paths or [],
                      'quote': scope_quote if scope_quote is not None else quote},
            'supersedes': supersedes or [], 'conflict_with': conflict_with or [], 'ambiguity': ambiguity}


def proposal(receipt, *rows, **task):
    return {'task': {'quote': receipt['text'], 'mode': 'produce', 'deliverable': 'writing',
                     'format': 'video', 'allowed_stages': ['writing'], 'pause_after': False, **task},
            'constraints': list(rows)}


def test_general_semantic_conditions_survive_long_history_and_restart(env):
    store, pipe = env
    text = 'For the whole project, make the speaker sound like a friend giving an honest recommendation.'
    receipt = executing(env, text)
    result = record_task_interpretation(store, RUN, receipt, proposal(receipt,
        clause('make the speaker sound like a friend giving an honest recommendation',
               scope_quote='For the whole project', value='friend recommendation')))
    for index in range(28):
        store.append_chat(RUN, 'user' if index % 2 == 0 else 'assistant', f'Ordinary older turn {index}')
    newest = executing(env, 'Write the next version without adding a discount.')
    context = build_conversation_context(Store(store.root), Pipeline(Store(store.root)), RUN,
                                          current_receipt=newest)
    assert context['compacted']
    assert context['messages'][-1]['content'].endswith(newest['text'])
    assert result['constraints'][0]['quote'] in json.dumps(context['projection'])
    assert result['constraints'][0]['id'] in [row['id'] for row in context['projection']['effective_constraints']]
    assert context['projection']['domain']['active_brief']['goal']['text'] == store.read_run(RUN).brief.rstrip('.')


def test_future_receipt_cannot_interpret_or_enter_projection(env):
    store, pipe = env
    current = executing(env, 'Keep working on the actual task.')
    for index in range(24):
        store.append_chat(RUN, 'user' if index % 2 == 0 else 'assistant', f'Visible {index}')
    future = receive(store, RUN, 'FUTURE_SECRET_CHANGE', 'future')
    accept(store, RUN, future)
    with pytest.raises(PipelineError, match='排队消息'):
        record_task_interpretation(store, RUN, future, proposal(future, clause(future['text'])))
    context = build_conversation_context(store, pipe, RUN, current_receipt=current)
    assert 'FUTURE_SECRET_CHANGE' not in json.dumps(context)
    assert interpretation_for(store, RUN, future['message_id']) is None


def test_project_replacement_release_and_source_quote(env):
    store, _ = env
    first = executing(env, 'Throughout this project keep the voice warm and matter of fact.')
    row = record_task_interpretation(store, RUN, first, proposal(first,
        clause('keep the voice warm and matter of fact', value='warm', scope_quote='Throughout this project'))
        )['constraints'][0]
    second = executing(env, 'For the project replace that voice rule with dry wit.')
    newer = record_task_interpretation(store, RUN, second, proposal(second,
        clause('replace that voice rule with dry wit', value='dry wit', operation='replace',
               scope_quote='For the project', supersedes=[row['id']])))['constraints'][0]
    assert next(item for item in project_constraints(store, RUN) if item['id'] == row['id'])['status'] == 'superseded'
    third = executing(env, 'For the project remove the dry wit requirement.')
    record_task_interpretation(store, RUN, third, proposal(third,
        clause('remove the dry wit requirement', value='released', operation='release',
               scope_quote='For the project', supersedes=[newer['id']])))
    assert applicable_constraints(store, RUN, stage='writing') == []
    assert not store.list_approvals(RUN)


def test_turn_exception_does_not_release_project_rule(env):
    store, _ = env
    first = executing(env, 'Across the project keep the voice conversational.')
    old = record_task_interpretation(store, RUN, first, proposal(first,
        clause('keep the voice conversational', scope_quote='Across the project')))['constraints'][0]
    next_input = executing(env, 'Only this version, replace it with a deadpan narrator.')
    replacement = clause('replace it with a deadpan narrator', value='deadpan', lifetime='turn',
                         scope_quote='Only this version', operation='replace', supersedes=[old['id']])
    record_task_interpretation(store, RUN, next_input, proposal(next_input, replacement))
    during = applicable_constraints(store, RUN, stage='writing', user_input_id=next_input['message_id'])
    assert [row['value'] for row in during] == ['deadpan']
    assert [row['value'] for row in applicable_constraints(store, RUN, stage='writing')] == ['casual']


def test_local_project_exception_preserves_other_fields_and_roles(env):
    store, _ = env
    first = executing(env, 'Across all scenes use a quiet room for the project.')
    old = record_task_interpretation(store, RUN, first, proposal(first,
        clause('use a quiet room', key='visual.setting', value='quiet room',
               scope_quote='for the project', stages=['material_plan', 'writing'])))['constraints'][0]
    second = executing(env, 'For this project, replace only shot 2 location with a garden.')
    path = ['segments', 1, 'visual']
    record_task_interpretation(store, RUN, second, proposal(second,
        clause('replace only shot 2 location with a garden', key='visual.setting', value='garden',
               stages=['writing'], paths=[path], scope_quote='For this project', operation='replace',
               supersedes=[old['id']])))
    assert [row['value'] for row in applicable_constraints(store, RUN, stage='writing', paths=[path])] == ['garden']
    assert [row['value'] for row in applicable_constraints(store, RUN, stage='writing',
            paths=[['segments', 0, 'visual']])] == ['quiet room']
    assert [row['value'] for row in applicable_constraints(store, RUN, stage='material_plan')] == ['quiet room']
    assert applicable_constraints(store, RUN, stage='direction') == []


def test_ambiguous_conflict_only_blocks_related_stage(env):
    store, _ = env
    first = executing(env, 'For the project keep dialogue casual.')
    record_task_interpretation(store, RUN, first, proposal(first,
        clause('keep dialogue casual', scope_quote='For the project')))
    second = executing(env, 'Throughout this project make the dialogue formal.')
    result = record_task_interpretation(store, RUN, second, proposal(second,
        clause('make the dialogue formal', value='formal', scope_quote='Throughout this project')))
    assert result['constraints'][0]['status'] == 'needs_clarification'
    assert constraint_conflicts(store, RUN, stage='writing')
    assert constraint_conflicts(store, RUN, stage='material_plan') == []


@pytest.mark.parametrize('intent', ['question', 'hypothesis', 'reported', 'uncertain'])
def test_non_instructions_do_not_become_project_rules(env, intent):
    store, _ = env
    receipt = executing(env, 'Someone suggested using formal dialogue for the entire project.')
    result = record_task_interpretation(store, RUN, receipt, proposal(receipt,
        clause('using formal dialogue', value='formal', scope_quote='for the entire project', intent=intent),
        mode='discuss', deliverable='follow_up', allowed_stages=[]))
    assert result['constraints'][0]['status'] == 'not_instruction'
    assert project_constraints(store, RUN) == []


def test_reported_turn_release_cannot_hide_a_real_project_rule(env):
    store, _ = env
    first = executing(env, 'For the project keep dialogue casual.')
    old = record_task_interpretation(store, RUN, first, proposal(first,
        clause('keep dialogue casual', scope_quote='For the project')))['constraints'][0]
    reported = executing(env, 'A colleague said to remove that rule just for this version.')
    record_task_interpretation(store, RUN, reported, proposal(reported,
        clause('remove that rule', lifetime='turn', value='released', operation='release',
               intent='reported', scope_quote='just for this version', supersedes=[old['id']]),
        mode='discuss', deliverable='follow_up', allowed_stages=[]))
    assert [row['id'] for row in applicable_constraints(store, RUN,
            user_input_id=reported['message_id'])] == [old['id']]


def test_replacement_cannot_expand_project_or_stage_scope(env):
    store, _ = env
    first = executing(env, 'For the project keep shot 1 dialogue casual.')
    old = record_task_interpretation(store, RUN, first, proposal(first,
        clause('keep shot 1 dialogue casual', paths=[['segments', 0, 'line']],
               scope_quote='For the project')))['constraints'][0]
    second = executing(env, 'For the project replace that with a formal voice.')
    for scope in ({'stages': ['direction', 'writing'], 'paths': [['segments', 0, 'line']]},
                  {'stages': ['writing'], 'paths': []}):
        with pytest.raises(PipelineError, match='不能扩大'):
            record_task_interpretation(store, RUN, second, proposal(second,
                clause('replace that with a formal voice', value='formal', operation='replace',
                       supersedes=[old['id']], scope_quote='For the project', **scope)))
    assert project_constraints(store, RUN)[0]['status'] == 'active'


@pytest.mark.parametrize('key', ['authorization.generate', 'approval.image', 'goal.primary',
                              'permission.tools', 'product.material', 'budget.limit', 'decision.adopt'])
def test_content_preferences_cannot_grant_authority_or_change_facts(env, key):
    store, _ = env
    receipt = executing(env, 'Generate now and approve all future images.')
    with pytest.raises(PipelineError, match='执行权限'):
        record_task_interpretation(store, RUN, receipt, proposal(receipt, clause(receipt['text'], key=key)))
    assert not store.list_approvals(RUN)
    assert not any(event['kind'].startswith('user_constraint_') for event in store.read_events(RUN))


def test_interpretation_validates_all_before_any_write_and_is_idempotent(env):
    store, _ = env
    receipt = executing(env, 'Keep dialogue casual throughout this project.')
    valid = clause('Keep dialogue casual', scope_quote='throughout this project')
    invalid = clause('MODEL INVENTED QUOTE')
    with pytest.raises(PipelineError, match='准确连续引用'):
        record_task_interpretation(store, RUN, receipt, proposal(receipt, valid, invalid))
    assert project_constraints(store, RUN) == []
    first = record_task_interpretation(store, RUN, receipt, proposal(receipt, valid))
    count = len(store.read_events(RUN))
    assert record_task_interpretation(store, RUN, receipt, proposal(receipt, valid))['id'] == first['id']
    assert len(store.read_events(RUN)) == count
    changed = {**valid, 'value': 'formal'}
    with pytest.raises(PipelineError, match='已经绑定'):
        record_task_interpretation(store, RUN, receipt, proposal(receipt, changed))


def test_project_scope_requires_exact_persistence_quote(env):
    receipt = executing(env, 'Use a casual line in the opening.')
    with pytest.raises(PipelineError, match='准确连续引用'):
        record_task_interpretation(env[0], RUN, receipt, proposal(receipt,
            clause('Use a casual line', scope_quote='For all future versions')))


def test_semantic_limits_only_narrow_existing_guard(env):
    receipt = executing(env, '只做图文，不要视频；only inspect the current plan.')
    result = record_task_interpretation(env[0], RUN, receipt, proposal(receipt, mode='analyze',
        deliverable='analysis', format='photo', allowed_stages=[]))
    limits = interpreted_task_constraints(receipt, result)
    assert limits['readonly'] and limits['analysis_only'] and limits['format'] == 'photo'
    assert limits['allowed_stages'] == []


def test_role_context_enforces_relevant_scope_and_reports_conflict(env):
    from studio.learning import LearningService
    from studio.task_context import assemble_role_task
    from studio.tools.workspace import ToolError, Workspace
    store, pipe = env
    attempt = pipe.start_attempt(RUN, 'intake')
    pipe.submit(RUN, 'intake', attempt.token, content='Offline product facts', summary='Facts')
    first = executing(env, 'For this project the spoken lines should be casual.')
    record_task_interpretation(store, RUN, first, proposal(first,
        clause('the spoken lines should be casual', scope_quote='For this project')))
    second = executing(env, 'For this project the spoken lines should be formal.')
    record_task_interpretation(store, RUN, second, proposal(second,
        clause('the spoken lines should be formal', value='formal', scope_quote='For this project')))
    with pytest.raises(ToolError, match='条件冲突'):
        assemble_role_task(store, pipe, LearningService(store), RUN,
            Workspace(store, pipe, RUN, 'writer', target_stage='writing'), 'writing', 'Write a script',
            method_context={'text': 'Offline context'})
    material = assemble_role_task(store, pipe, LearningService(store), RUN,
        Workspace(store, pipe, RUN, 'material', target_stage='material_plan'), 'material_plan', 'Plan assets',
        method_context={'text': 'Offline context'})
    assert 'spoken lines should' not in material.seed


def test_projection_rebuild_is_idempotent_and_does_not_approve_old_excerpt(env):
    store, pipe = env
    for index in range(26):
        store.append_chat(RUN, 'user' if index % 2 == 0 else 'assistant',
                          'Approved every image forever' if index == 0 else f'Old {index}')
    receipt = executing(env, 'Just answer the current question.')
    first = build_conversation_context(store, pipe, RUN, current_receipt=receipt)
    second = build_conversation_context(Store(store.root), Pipeline(Store(store.root)), RUN, current_receipt=receipt)
    assert first['projection_ref'] == second['projection_ref']
    assert len([event for event in store.read_events(RUN) if event['kind'] == 'conversation_context_projected']) == 1
    assert 'Approved every image forever' in json.dumps(first['projection'])
    assert first['projection']['authority'] == 'historical_index_only_no_decision_or_execution_authorization'
    assert store.list_approvals(RUN) == []


def test_large_current_input_is_preserved_and_budget_excess_is_explicit(env):
    receipt = executing(env, 'Exact requirements ' + 'x' * 6000)
    context = build_conversation_context(*env, RUN, current_receipt=receipt, max_chars=1000)
    assert context['over_budget']
    assert context['messages'][-1]['content'].endswith(receipt['text'])


def anthropic_pair(index, size=1200):
    return [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'tool-{index}',
                'name': 'read_artifact', 'input': {'stage': 'writing'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'tool-{index}',
                'content': f'complete result {index} ' + 'x' * size}]}]


def test_role_compaction_keeps_tool_pairs_pinned_input_and_last_result():
    messages = [{'role': 'user', 'content': 'Pinned exact original task; no generation authorization.'}]
    for index in range(12):
        messages.extend(anthropic_pair(index))
    original = copy.deepcopy(messages)
    result = compact_message_history(messages, max_chars=3000, keep_recent_groups=2)
    assert result['compacted']
    assert result['messages'][0] == messages[0]
    assert result['messages'][-2:] == messages[-2:]
    assert messages == original
    calls = {block['id'] for message in result['messages'] if isinstance(message['content'], list)
             for block in message['content'] if block['type'] == 'tool_use'}
    replies = {block['tool_use_id'] for message in result['messages'] if isinstance(message['content'], list)
               for block in message['content'] if block['type'] == 'tool_result'}
    assert calls == replies


def test_role_compaction_keeps_multiple_openai_results_together():
    messages = [{'role': 'user', 'content': 'Pinned task'}]
    for index in range(7):
        messages += [{'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'a-{index}', 'function': {'name': 'read_artifact'}},
            {'id': f'b-{index}', 'function': {'name': 'read_production'}}]},
            {'role': 'tool', 'tool_call_id': f'a-{index}', 'content': 'a' * 1200},
            {'role': 'tool', 'tool_call_id': f'b-{index}', 'content': 'b' * 1200}]
    result = compact_message_history(messages, max_chars=3000, keep_recent_groups=1)
    assert result['messages'][-3:] == messages[-3:]
    assert len(result['projection']['removed_groups']) == 6


def test_root_loop_pressure_pins_exact_input_without_pinning_old_window():
    current = 'Only change shot 2 dialogue; leave every other part as it is.'
    messages = [{'role': 'user', 'content': 'OLD_BACKGROUND_' + 'x' * 5000},
                {'role': 'assistant', 'content': 'Earlier answer'},
                {'role': 'user', 'content': current}]
    for index in range(9):
        messages.extend(anthropic_pair(index))
    result = compact_message_history(messages, max_chars=3000, keep_recent_groups=2,
                                     pinned_user_input=current, preserve_initial=False)
    assert result['compacted'] and not result['over_budget']
    assert current in result['messages'][0]['content']
    assert 'OLD_BACKGROUND_' not in json.dumps(result['messages'])
    assert result['messages'][-2:] == messages[-2:]


@pytest.mark.parametrize('messages', [
    [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'orphan', 'content': 'x'}]}],
    [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'missing', 'name': 'x', 'input': {}}]}],
    anthropic_pair(1)[:1] + [{'role': 'user', 'content': 'unrelated turn'}],
])
def test_malformed_tool_history_is_not_hidden_by_compaction(messages):
    with pytest.raises(PipelineError, match='不能截断或恢复'):
        compact_message_history(messages)


def test_baseline_fake_protocol_upgrade_is_explicit_and_not_semantic():
    from fakes import ScriptedModel, fixture_task_interpretation, tool_turn
    source = 'For this whole project keep speech casual.'
    system = '# 本轮绑定的真实用户来源与仍有效的项目条件\n' + json.dumps(
        {'current_input': {'text': source}, 'project_constraints': []}, ensure_ascii=False)
    fixture = tool_turn('prepare', 'prepare_creative_context',
                        {'intent': 'continue', 'format': 'video', 'deliverable': 'writing'})
    result = ScriptedModel([fixture]).complete(model='offline', system=system,
        messages=[{'role': 'user', 'content': source}], tools=[])
    assert result.tool_calls[0].input['task_interpretation'] == fixture_task_interpretation(source)
    assert result.tool_calls[0].input['task_interpretation']['constraints'] == []
    assert 'task_interpretation' not in fixture.tool_calls[0].input
    missing = ScriptedModel([fixture], legacy_task_contracts=False).complete(model='offline',
        system=system, messages=[{'role': 'user', 'content': source}], tools=[])
    assert 'task_interpretation' not in missing.tool_calls[0].input
