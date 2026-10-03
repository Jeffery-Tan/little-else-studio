"""Offline counterexamples: real boundaries, deliberately mistaken models.

These tests verify accepted/refused constructions, not general NL accuracy.
"""
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from studio.agents.orchestrator import Orchestrator
from studio.agents.client import JobCancelled
from studio.conversation_input import accept, receive
from studio.decision import candidates, commit, continuation_text, resolve
from studio.ledger import Ledger
from studio.models import PipelineError
from studio.revision_scope import plan_revision
from studio.tools.workspace import ToolError, Workspace
from studio.tools.dispatch import Dispatcher
from test_conversational_decision import RUN, seed_direction, settings_for
from test_decision import received_message
from test_revision_scope import env, plan_args, RUN as REVISION_RUN, QUOTE, patch, plan


@pytest.mark.parametrize('text', ['我现在在读 B 方案的详细内容', 'B 的开头很生活化',
                                  '我看到了方案 B', '方案 B 的说明已经读完了'])
def test_mention_is_not_a_decision_even_when_model_selects_it(tmp_path, text):
    store, pipe = seed_direction(tmp_path)
    receipt = received_message(store, pipe, RUN, text)
    result = resolve(candidates(store, pipe, RUN),
                     {'action': 'select_direction', 'option_id': 'B'}, receipt=receipt)
    assert result['status'] == 'clarify'
    assert result['reason'] == 'adoption_not_explicit'
    assert not store.list_approvals(RUN)


@pytest.mark.parametrize('note', ['', '完全改成广告', '开头生活化一点'])
def test_followup_carries_full_original_when_model_drops_or_rewrites_note(tmp_path, note):
    store, pipe = seed_direction(tmp_path)
    text = '采用 B，但开头生活化一点，然后继续。结尾保留，不要加促销。'
    receipt = received_message(store, pipe, RUN, text)
    choice = resolve(candidates(store, pipe, RUN),
                     {'action': 'select_direction', 'option_id': 'B'}, receipt=receipt)
    commit(store, pipe, RUN, choice['action'], source='conversation', note=note)
    event = next(e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_requested')
    assert text in event['text']
    assert event['source_user_input_id'] == receipt['message_id']
    assert continuation_text(store.list_approvals(RUN)[0], store=store, run_id=RUN) == event['text']
    assert '完全改成广告' not in event['text']


def main_for(env, text, **kwargs):
    store, pipe, settings, ledger = env
    main = Orchestrator(store, pipe, settings, ScriptedModel([final_turn('Read only.')]), ledger, REVISION_RUN)
    main.send(text, **kwargs)
    return main, Workspace(store, pipe, REVISION_RUN, 'orchestrator')


@pytest.mark.parametrize('text,args', [
    ('现在进度怎么样？', {'intent': 'new', 'format': 'video', 'deliverable': 'direction'}),
    ('只讨论一下，先不要改', {'intent': 'new', 'format': 'video', 'deliverable': 'direction'}),
    ('只分析当前脚本，不要创作', {'intent': 'edit', 'format': 'video', 'deliverable': 'writing'}),
    ('只要提示词', {'intent': 'new', 'format': 'video', 'deliverable': 'direction'}),
    ('只做图文，不要视频', {'intent': 'new', 'format': 'video', 'deliverable': 'direction'}),
    ('做视频', {'intent': 'new', 'format': 'video'}),
    ('做视频', {'intent': 'new', 'format': 'video', 'deliverable': 'follow_up'}),
    (QUOTE, {'intent': 'continue', 'format': 'video', 'deliverable': 'writing'}),
])
def test_first_route_respects_actual_limits_and_requires_endpoint(env, text, args):
    main, workspace = main_for(env, text)
    with pytest.raises(ToolError):
        main._execute('prepare_creative_context', args, workspace)
    assert main._scope_selected is False


def test_first_revision_cannot_widen_local_words(env):
    with pytest.raises(PipelineError, match='首次修订'):
        plan_revision(env[0], env[1], REVISION_RUN,
                      plan_args(env[0], mode='rework'), turn_id='first', user_message=QUOTE)


def test_field_target_binds_version_and_path_on_first_plan(env):
    store, pipe, _, _ = env
    current = store.latest_artifact(REVISION_RUN, 'writing')
    target = {'stage': 'writing', 'version': current.version, 'artifact_sha256': current.sha256,
              'path': ['segments', 0, 'line']}
    receipt = receive(store, REVISION_RUN, '改自然一点', 'field', feedback_target=target)
    from studio.conversation_input import task_constraints
    with pytest.raises(PipelineError, match='准确版本和字段'):
        plan_revision(store, pipe, REVISION_RUN,
                      plan_args(store, path=['segments', 1, 'line'], quote=receipt['text']),
                      turn_id='first', user_message=receipt['text'], constraints=task_constraints(receipt))


def test_selected_goal_cannot_be_removed_to_skip_delivery(env):
    main, workspace = main_for(env, '继续完成脚本')
    main._execute('prepare_creative_context', {'intent': 'continue', 'format': 'video',
        'deliverable': 'writing', 'task_interpretation': fixture_task_interpretation(
            '继续完成脚本', deliverable='writing')}, workspace)
    with pytest.raises(ToolError, match='交付.*绑定'):
        main._execute('prepare_creative_context', {'intent': 'status', 'format': 'none',
                                                  'deliverable': 'follow_up'}, workspace)


def test_format_and_downstream_scope_cannot_expand_after_selection(env):
    main, workspace = main_for(env, '重新构思方向')
    main._execute('prepare_creative_context', {'intent': 'new', 'format': 'video',
        'deliverable': 'direction', 'task_interpretation': fixture_task_interpretation(
            '重新构思方向', intent='new', deliverable='direction')}, workspace)
    with pytest.raises(ToolError, match='格式.*(?:绑定|明确要求)'):
        main._execute('prepare_creative_context', {'intent': 'new', 'format': 'photo',
                                                  'deliverable': 'direction'}, workspace)
    with pytest.raises(ToolError, match='超出.*范围|超出本轮'):
        main._execute('dispatch', {'stage': 'writing', 'brief': '未经要求继续写稿'}, workspace)


def test_role_gets_pinned_original_even_if_brief_omits_it_and_newer_input_is_accepted(env):
    store, pipe, settings, ledger = env
    text = QUOTE + '；口吻像朋友聊天，保留人物和场景设计。'
    receipt = receive(store, REVISION_RUN, text, 'current')
    accept(store, REVISION_RUN, receipt)
    newer = receive(store, REVISION_RUN, 'UNRELATED_NEWER_MESSAGE', 'future')
    accept(store, REVISION_RUN, newer)
    model = ScriptedModel([tool_turn('patch', 'submit_patch', patch())])
    outcome = Dispatcher(store, pipe, settings, model, ledger, REVISION_RUN).dispatch(
        'writing', '主制片遗漏了所有具体要求', revision_scope=plan(env),
        user_input_id=receipt['message_id'], turn_scope={'deliverable': 'revision'})
    assert outcome.submitted == 'writing-v2'
    assert text in model.calls[0]['messages'][0]['content']
    assert 'UNRELATED_NEWER_MESSAGE' not in model.requests_text()
    task = next(e for e in store.read_events(REVISION_RUN) if e['kind'] == 'agent_task_prepared')
    source = next(s for s in task['context']['sources'] if s['kind'] == 'user_message')
    assert source['message_id'] == receipt['message_id'] and source['scope']['stage'] == 'writing'


def test_internal_source_does_not_regain_decision_authority(env):
    receipt = receive(env[0], REVISION_RUN, '采用 B，但口吻保持自然', 'adopt')
    main, workspace = main_for(env, '服务端接续', internal_instruction=True,
                               source_user_input_id=receipt['message_id'])
    assert main._task_constraints['text'] == receipt['text']
    with pytest.raises(ToolError, match='没有新的真实用户决定'):
        main._execute('propose_user_action', {'action': 'select_direction', 'option_id': 'B'}, workspace)


def test_cancel_role_cannot_turn_a_quoted_stop_word_into_an_instruction(env):
    from studio.agent_runtime import AgentRuntime, bind_task, reset_task
    from studio.agent_sessions import AgentSessions
    store = env[0]
    runtime = AgentRuntime(store, REVISION_RUN)
    runtime.create_task('root-job', role='orchestrator', parent_job_id='root-job')
    runtime.transition('root-job', 'running')
    sessions = AgentSessions(store, REVISION_RUN)
    row = sessions.ensure(role='writer', stage='writing', parent_job_id='root-job')
    sessions.claim(row['session_id'])
    main, workspace = main_for(env, '不要停止，请继续写稿。')
    binding = bind_task('root-job')
    try:
        with pytest.raises(ToolError, match='停止意图'):
            main._execute('cancel_role', {'session_id': row['session_id'], 'quote': '停止'}, workspace)
        assert not sessions.snapshot(row['session_id'])['stop_requested']
    finally:
        reset_task(binding)
        sessions.finish(row['session_id'], 'waiting')


def role_seed(env):
    from studio.task_context import assemble_role_task
    from studio.learning import LearningService
    store, pipe, _, _ = env
    return assemble_role_task(store, pipe, LearningService(store), REVISION_RUN,
        Workspace(store, pipe, REVISION_RUN, 'writer', target_stage='writing'), 'writing', '继续写稿',
        method_context={'text': 'offline fixture'})


def test_project_conditions_survive_chat_window(env):
    from studio.agents.orchestrator import CHAT_WINDOW
    main_for(env, '整片在家里，不要街拍')
    for number in range(CHAT_WINDOW + 5):
        env[0].append_chat(REVISION_RUN, 'user' if number % 2 else 'assistant', f'Unrelated {number}')
    seed = role_seed(env)
    assert '整片在家里' in seed.seed and '不要街拍' in seed.seed
    assert 'Unrelated' not in seed.seed
    assert len([s for s in seed.sources if s['kind'] == 'user_constraint']) == 2


def test_explicit_same_key_replacement_supersedes_old_condition(env):
    from studio.conversation_input import project_constraints
    main_for(env, '整片在家里，不要街拍')
    main_for(env, '现在整片改在办公室')
    rows = project_constraints(env[0], REVISION_RUN)
    assert next(row for row in rows if row['quote'] == '整片在家里')['status'] == 'superseded'
    assert next(row for row in rows if row['quote'] == '现在整片改在办公室')['status'] == 'active'
    seed = role_seed(env).seed
    assert '整片在家里' not in seed and '现在整片改在办公室' in seed and '不要街拍' in seed


def test_accepted_future_input_cannot_change_project_constraints(env):
    main_for(env, '整片在家里')
    receipt = receive(env[0], REVISION_RUN, '现在整片改在办公室', 'future-constraint')
    accept(env[0], REVISION_RUN, receipt)
    seed = role_seed(env).seed
    assert '整片在家里' in seed and '现在整片改在办公室' not in seed


def test_ambiguous_project_replacement_requires_clarification(env):
    main_for(env, '整片在家里')
    main, workspace = main_for(env, '整片在办公室')
    with pytest.raises(ToolError, match='条件.*冲突'):
        main._execute('prepare_creative_context', {'intent': 'continue', 'format': 'video',
            'deliverable': 'writing', 'task_interpretation': fixture_task_interpretation(
                '整片在办公室', deliverable='writing')}, workspace)


def test_hypothetical_and_question_are_not_durable_constraints(env):
    from studio.conversation_input import project_constraints
    for text in ['整片在家里？', '如果整片在家里', '如果下雨，整片在家里', '不要街拍是否更好？', '同事说“不要街拍”']:
        main_for(env, text)
    assert project_constraints(env[0], REVISION_RUN) == []


def test_late_stop_preserves_saved_artifact_and_closes_role_as_cancelled(env, monkeypatch):
    from studio.agent_runtime import AgentRuntime
    store, pipe, settings, ledger = env
    original = pipe.submit
    runtime = AgentRuntime(store, REVISION_RUN)
    def save_then_stop(*args, **kwargs):
        artifact = original(*args, **kwargs)
        runtime.request_stop(artifact.attempt, reason='User stopped while save completed')
        return artifact
    monkeypatch.setattr(pipe, 'submit', save_then_stop)
    outcome = Dispatcher(store, pipe, settings, ScriptedModel([tool_turn('patch', 'submit_patch', patch())]),
                         ledger, REVISION_RUN).dispatch('writing', QUOTE, revision_scope=plan(env))
    assert outcome.stopped_because == 'cancelled'
    artifact = store.latest_artifact(REVISION_RUN, 'writing')
    assert artifact.version == 2
    assert runtime.task(artifact.attempt)['status'] == 'cancelled'
    assert pipe.stage_state(REVISION_RUN, 'writing').open_attempt is None


def test_runtime_creation_failure_does_not_leave_open_pipeline_attempt(env, monkeypatch):
    from studio.agent_runtime import AgentRuntime
    def fail(*args, **kwargs):
        raise PipelineError('agent_runtime', 'Synthetic initialization failure')
    monkeypatch.setattr(AgentRuntime, 'create_task', fail)
    store, pipe, settings, ledger = env
    model = ScriptedModel([])
    with pytest.raises(PipelineError, match='Synthetic'):
        Dispatcher(store, pipe, settings, model, ledger, REVISION_RUN).dispatch(
            'writing', QUOTE, revision_scope=plan(env))
    assert pipe.stage_state(REVISION_RUN, 'writing').open_attempt is None
    assert store.latest_artifact(REVISION_RUN, 'writing').version == 1
    assert not model.calls


def test_dispatch_runtime_owns_exact_parent_source_and_artifact_receipt(env):
    from studio.agent_runtime import AgentRuntime, bind_task, reset_task
    store, pipe, settings, ledger = env
    receipt = receive(store, REVISION_RUN, QUOTE, 'runtime-source')
    runtime = AgentRuntime(store, REVISION_RUN)
    runtime.create_task('parent-job', role='orchestrator', parent_job_id='parent-job',
                        source_message_id=receipt['message_id'])
    runtime.transition('parent-job', 'running')
    binding = bind_task('parent-job')
    try:
        outcome = Dispatcher(store, pipe, settings, ScriptedModel([tool_turn('patch', 'submit_patch', patch())]),
            ledger, REVISION_RUN).dispatch('writing', QUOTE, revision_scope=plan(env),
                                           user_input_id=receipt['message_id'])
    finally:
        reset_task(binding)
    task = runtime.task(outcome.task_id)
    assert task['status'] == 'completed'
    assert task['parent_task_id'] == task['parent_job_id'] == 'parent-job'
    assert task['source_message_id'] == receipt['message_id']
    assert task['artifact_receipt']['sha256'] == outcome.artifact_sha256


def test_future_accepted_input_is_hidden_from_prompt_and_history_tools(env):
    store, pipe, settings, ledger = env
    first = receive(store, REVISION_RUN, 'CURRENT_INPUT_A', 'a')
    future = receive(store, REVISION_RUN, 'FUTURE_INPUT_B', 'b')
    accept(store, REVISION_RUN, first)
    accept(store, REVISION_RUN, future)
    model = ScriptedModel([tool_turn('history', 'read_conversation_history', {}), final_turn('Answered A')])
    main = Orchestrator(store, pipe, settings, model, ledger, REVISION_RUN)
    main.send(first['text'], user_input_id=first['message_id'])
    assert model.calls[0]['messages'][-1] == {'role': 'user', 'content': first['text']}
    assert 'FUTURE_INPUT_B' not in model.requests_text()
    with pytest.raises(ValueError, match='future user turn'):
        main._execute('read_conversation_history', {'message_index': 1},
                      Workspace(store, pipe, REVISION_RUN, 'orchestrator'))
    later = ScriptedModel([final_turn('Answered B')])
    Orchestrator(store, pipe, settings, later, ledger, REVISION_RUN).send(
        future['text'], user_input_id=future['message_id'])
    assert later.calls[0]['messages'][-1] == {'role': 'user', 'content': future['text']}
    assert 'FUTURE_INPUT_B' in later.requests_text()
    assert [row['text'] for row in store.read_chat(REVISION_RUN) if row['role'] == 'user'] == [first['text'], future['text']]


def test_cancel_before_send_keeps_assigned_original_as_background_not_current_authority(env):
    from studio.conversation_input import visible_chat
    store, pipe, settings, ledger = env
    original = receive(store, REVISION_RUN, '原需求：为午餐包做一条十五秒美国站视频。', 'original')
    accept(store, REVISION_RUN, original)
    store.append_event(REVISION_RUN, 'job_created', job_id='original-job',
                       user_input_id=original['message_id'])
    store.append_event(REVISION_RUN, 'job_status', job_id='original-job', status='cancelled')
    assert not any(e['kind'] == 'user_message_execution_started' for e in store.read_events(REVISION_RUN))
    correction = receive(store, REVISION_RUN, '修改演员为成年人。', 'correction')
    future = receive(store, REVISION_RUN, 'FUTURE_UNASSIGNED_CORRECTION', 'future')
    accept(store, REVISION_RUN, correction)
    accept(store, REVISION_RUN, future)
    store.append_event(REVISION_RUN, 'job_created', job_id='child-job',
                       parent_job_id='original-job', user_input_id=correction['message_id'])
    model = ScriptedModel([final_turn('已理解当前纠正及原需求。')])
    main = Orchestrator(store, pipe, settings, model, ledger, REVISION_RUN)
    main.send('内部拼接背景不会冒充用户当前原话', user_input_id=correction['message_id'])
    prompt = model.requests_text()
    assert original['text'] in prompt and correction['text'] in prompt
    assert future['text'] not in prompt
    assert main._current_user_input['message_id'] == correction['message_id']
    assert main._current_user_message == correction['text']
    assert main._task_constraints['text'] == correction['text']
    assert [row['text'] for _, row in visible_chat(store, REVISION_RUN) if row['role'] == 'user'] == [
        original['text'], correction['text']]
    assert [row['text'] for row in store.read_chat(REVISION_RUN) if row['role'] == 'user'] == [
        original['text'], correction['text'], future['text']]
