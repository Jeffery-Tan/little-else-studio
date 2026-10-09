"""Current trusted scope remains available when successful tool pairs roll out."""
import hashlib
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.client import SafePreflightError, run_agent_loop
from studio.conversation_input import interpretation_for
from test_orchestration import env, make_orchestrator
from test_unselected_task_completion import TEXT, proposal


MARKER = '# 本轮绑定的真实用户来源与仍有效的项目条件\n'


def current(system):
    return json.JSONDecoder().raw_decode(system.split(MARKER, 1)[1])[0]


def test_original_binding_and_scope_survive_real_tool_pair_compaction(env, tmp_path, monkeypatch):
    store, pipe, ledger = env
    declared = proposal()
    declared['constraints'] = [{'quote': '不生成媒体', 'key': 'copy.claims', 'value': '不写未经核实的商品主张',
        'operation': 'add', 'polarity': 'forbid', 'intent': 'instruction',
        'scope': {'lifetime': 'project', 'stages': ['writing'], 'paths': [], 'quote': '不生成媒体'},
        'supersedes': [], 'conflict_with': [], 'ambiguity': ''}]
    model = ScriptedModel([
        tool_turn('bind', 'propose_task_interpretation', declared),
        tool_turn('route', 'prepare_creative_context',
                  {'intent': 'continue', 'format': 'video', 'deliverable': 'writing'}),
        tool_turn('big-1', 'read_project_state', {}),
        tool_turn('big-2', 'read_project_state', {}), final_turn('停止工作。'), final_turn('仍未交付。'),
    ], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    original = orch._execute
    def execute(name, args, workspace):
        return 'SYNTHETIC_LARGE_READ_' * 2000 if name == 'read_project_state' else original(name, args, workspace)
    monkeypatch.setattr(orch, '_execute', execute)
    result = orch.send(TEXT)
    assert result.stopped_because == 'incomplete'
    source = orch._current_user_input['message_id']
    binding = interpretation_for(store, orch.run_id, source)
    states = [current(call['system']) for call in model.calls]
    assert states[0]['current_input'].get('interpretation_id') is None
    assert states[0]['execution_scope']['selected'] is False
    assert states[1]['current_input']['interpretation_id'] == binding['id']
    assert states[1]['execution_scope']['selected'] is False
    for state, call in zip(states[2:], model.calls[2:]):
        assert state['current_input']['interpretation_id'] == binding['id']
        assert state['current_input']['allowed_stages'] == ['writing']
        assert state['current_input']['pause_after'] is True
        assert state['execution_scope']['selected'] is True
        assert state['execution_scope']['selection']['intent'] == 'continue'
        assert state['execution_scope']['source_message_id'] == source
        assert state['execution_scope']['source_sha256'] == hashlib.sha256(TEXT.encode()).hexdigest()
        assert state['project_constraints'][0]['quote'] == '不生成媒体'
        assert '本轮工作范围尚未选择。' not in call['system']
        assert call['system'].count(MARKER) == 1
    compacted = model.calls[4]['messages']
    names = [b.get('name') for m in compacted if isinstance(m.get('content'), list)
             for b in m['content'] if b.get('type') == 'tool_use']
    assert 'propose_task_interpretation' not in names and 'prepare_creative_context' not in names
    assert any(isinstance(m.get('content'), str) and TEXT in m['content'] for m in compacted)
    assert not store.list_artifacts(orch.run_id) and not store.list_approvals(orch.run_id)


def test_internal_continuation_keeps_saved_source_without_new_receipt(env, tmp_path):
    store, pipe, ledger = env
    first = make_orchestrator(store, pipe, ledger, ScriptedModel([
        tool_turn('bind', 'propose_task_interpretation', proposal()), final_turn('结束。'), final_turn('未完成。')],
        legacy_task_contracts=False), tmp_path)
    first.send(TEXT)
    source = first._current_user_input['message_id']
    model = ScriptedModel([final_turn('只核对已保存来源。')], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    orch.send('内部文字接续', internal_instruction=True, source_user_input_id=source)
    state = current(model.calls[0]['system'])
    assert state['input_authority'] == 'saved_source_only_no_new_user_decision'
    assert state['current_input']['interpretation_id'] == interpretation_for(store, orch.run_id, source)['id']
    assert state['execution_scope']['source_message_id'] == source
    assert state['execution_scope']['selected'] is False and orch._current_user_input is None
    assert len(model.calls) == 1


def test_dispatch_projection_labels_attempts_without_claiming_submission(env, tmp_path):
    store, pipe, ledger = env
    model = ScriptedModel([final_turn('只读。')], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    orch._dispatched_stages.add('writing')
    extra = orch._current_task_extra()
    scope = current(extra)['execution_scope']
    assert scope['dispatch_attempted_stages'] == ['writing']
    assert 'dispatched_stages' not in scope
    assert '包含失败尝试' in extra and '不能代表' in extra
    assert not store.list_artifacts(orch.run_id) and not model.calls


@pytest.mark.parametrize('mode', ['analyze', 'status', 'discuss', 'stop', 'clarify'])
def test_bound_readonly_task_does_not_get_creative_prepare_instruction(env, tmp_path, mode):
    store, pipe, ledger = env
    text = '只分析商品图片。' if mode == 'analyze' else '仅查询已保存状态。'
    declared = {'task': {'quote': text, 'mode': mode,
        'deliverable': 'analysis' if mode == 'analyze' else 'follow_up',
        'format': 'photo' if mode == 'analyze' else 'none', 'allowed_stages': [], 'pause_after': False},
        'constraints': []}
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', declared),
                           final_turn('未进行创作。'), final_turn('没有实际观察，分析未完成。')],
                           legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    result = orch.send(text)
    scope = current(model.calls[1]['system'])
    assert scope['current_input']['interpretation_id']
    assert scope['current_input']['readonly'] is True
    assert scope['execution_scope']['selected'] is False
    extra = orch._current_task_extra()
    assert '按同一任务用 prepare_creative_context' not in extra
    assert ('图片分析可直接 observe_images' in extra) == (mode == 'analyze')
    if mode == 'analyze':
        assert result.stopped_because == 'incomplete' and len(model.calls) == 3
    assert not store.list_artifacts(orch.run_id) and not store.list_approvals(orch.run_id)


@pytest.mark.parametrize('failure', ['raise', 'type'])
def test_bad_request_context_never_calls_provider_or_tools(failure):
    model = ScriptedModel([final_turn('Must not run')])
    def context():
        if failure == 'raise':
            raise SafePreflightError('Synthetic context failed')
        return {'not': 'a system string'}
    with pytest.raises(SafePreflightError):
        run_agent_loop(model, model='synthetic', system='original', messages=[], tools=[],
                       execute_tool=lambda *_: pytest.fail('No tool may run'), system_for_request=context)
    assert not model.calls


def test_default_loop_keeps_original_fixed_system():
    model = ScriptedModel([final_turn('Done')])
    result = run_agent_loop(model, model='synthetic', system='original fixed system', messages=[], tools=[],
                            execute_tool=lambda *_: pytest.fail('No tools'))
    assert result.stopped_because == 'done' and model.calls[0]['system'] == 'original fixed system'
