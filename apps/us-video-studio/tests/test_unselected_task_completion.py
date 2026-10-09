"""A bound task still needs its original deliverable before the producer ends."""
import copy
from dataclasses import replace

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import display_context, interpretation_for, receive
from studio.ledger import Ledger
from studio.tools.registry import STRICT_ABSENT
from test_finishing_phase import mock_http, reply
from test_orchestration import env, make_orchestrator
from test_writing_tool_schema import seed, submission, wire


TEXT = '沿用已采用的方向 A 和素材计划，交付完整稿件；完成文字后暂停，不生成媒体。'


def proposal(text=TEXT, *, mode='produce', goal='writing', format='video', stages=None):
    return {'task': {'quote': text, 'mode': mode, 'deliverable': goal, 'format': format,
                     'allowed_stages': ['writing'] if stages is None else stages, 'pause_after': True},
            'constraints': []}


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_real_http_binding_then_early_end_reminds_scope_and_delivers_same_task(tmp_path, monkeypatch, format):
    store, pipe, settings, _, run = seed(tmp_path, monkeypatch)
    ledger = Ledger(store, settings)
    context = display_context(store, pipe, run, {'pending': None, 'questions': [], 'interaction': None})
    receipt = receive(store, run, TEXT, 'scope-completion', context)
    runtime = AgentRuntime(store, run)
    runtime.create_task('scope-job', role='orchestrator', parent_job_id='scope-job',
                        source_message_id=receipt['message_id'])
    runtime.transition('scope-job', 'running')
    upstream = {s: store.latest_artifact(run, s) for s in ('intake', 'direction', 'material_plan')}
    approvals = store.list_approvals(run)
    events, work = [], []
    declared = proposal(format=format)
    def handle(payload):
        names = {item['function']['name'] for item in payload.get('tools', [])}
        if 'submit_stage' in names:
            return wire(submission(format))
        if not names:
            return reply(text='完整稿件已保存，等待阅读；本轮没有生成媒体。')
        work.append(payload)
        if len(work) == 1:
            assert 'prepare_creative_context' not in names
            return reply(('propose_task_interpretation', {**declared,
                'reuse_artifact': STRICT_ABSENT, 'video_use': STRICT_ABSENT, 'image_use': STRICT_ABSENT}))
        if len(work) == 2:
            assert 'prepare_creative_context' in names
            return reply(text='先停在这里。')
        if len(work) == 3:
            assert any('任务已绑定' in str(row.get('content')) for row in payload['messages'])
            return reply(('prepare_creative_context', {'intent': 'continue', 'format': format,
                                                       'deliverable': 'writing'}))
        assert len(work) == 4
        return reply(('dispatch', {'stage': 'writing', 'brief': TEXT}))
    seen = mock_http(monkeypatch, handle)
    orch = Orchestrator(store, pipe, settings,
        DeepSeekClient('synthetic-only', reasoning_effort='low', progress_stream=False),
        ledger, run, on_event=events.append)
    token = bind_task('scope-job')
    try:
        result = orch.send(TEXT, user_input_id=receipt['message_id'])
    finally:
        reset_task(token)
    assert result.stopped_because == 'done' and result.deliverable['status'] == 'complete'
    assert len(seen) == 6 and len(work) == 4  # one writer and one tool-free conclusion
    assert sum(e.get('phase') == 'continuing' for e in events) == 1
    saved = interpretation_for(store, run, receipt['message_id'])
    assert saved['task'] == declared['task']
    assert all(store.latest_artifact(run, s) == a for s, a in upstream.items())
    assert store.list_approvals(run) == approvals
    sessions = orch.dispatcher.sessions()
    assert len(sessions) == 1 and sessions[0]['stage'] == 'writing'
    assert sessions[0]['source_message_id'] == receipt['message_id']
    assert sessions[0]['parent_job_id'] == 'scope-job'
    assert all(e['kind'] not in {'production_execution_authorized', 'production_generation_started'}
               for e in store.read_events(run))


def test_second_early_end_is_incomplete_without_unbounded_calls(env, tmp_path):
    store, pipe, ledger = env
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', proposal()),
                           final_turn('先结束。'), final_turn('仍不执行。')], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    result = orch.send(TEXT)
    assert len(model.calls) == 3 and result.stopped_because == 'incomplete'
    assert result.deliverable['status'] == 'incomplete'
    assert not orch._scope_selected and orch.selection['intent'] == 'status'
    assert not store.list_artifacts(orch.run_id) and not store.list_approvals(orch.run_id)
    assert sum('任务已绑定' in str(m.get('content')) for m in model.calls[-1]['messages']) == 1


def test_actual_user_question_concludes_without_completion_reminder(env, tmp_path):
    store, pipe, ledger = env
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', proposal()),
        tool_turn('ask', 'ask_user', {'question': '请补充需要改哪一页。',
            'options': [{'label': '第一页', 'description': '只改第一页'},
                        {'label': '第二页', 'description': '只改第二页'}],
            'recommended': 0})], legacy_task_contracts=False)
    result = make_orchestrator(store, pipe, ledger, model, tmp_path).send(TEXT)
    assert len(model.calls) == 2 and result.text == ''
    assert result.deliverable['status'] == 'incomplete'
    assert '任务已绑定' not in model.requests_text()


@pytest.mark.parametrize('goal, mode, stages', [('revision', 'revise', ['writing']),
                                             ('production_input', 'produce', [])])
def test_unselected_revision_or_input_only_reminds_without_inventing_scope(env, tmp_path, monkeypatch, goal, mode, stages):
    store, pipe, ledger = env
    declared = proposal(mode=mode, goal=goal, stages=stages)
    text = TEXT
    run = None
    if goal == 'revision':
        from test_revision_scope import env as revision_env, RUN
        from test_revision_interpretation_scope import meaning
        store, pipe, settings, ledger = revision_env.__wrapped__(tmp_path / 'revision', monkeypatch)
        run, text = RUN, '第二句只需要换个说法，整套方案照旧。'
        declared = meaning({'text': text})
        declared['task']['pause_after'] = True
    before = store.list_artifacts(run or 'lunch-bag-01')
    approvals = store.list_approvals(run or 'lunch-bag-01')
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', declared),
                           final_turn('暂停。'), final_turn('不执行。')], legacy_task_contracts=False)
    orch = (Orchestrator(store, pipe, settings, model, ledger, run) if run else
            make_orchestrator(store, pipe, ledger, model, tmp_path))
    result = orch.send(text)
    assert len(model.calls) == 3 and result.stopped_because == 'incomplete'
    assert '任务已绑定' in model.requests_text() and not orch._scope_selected
    assert orch._task_constraints['allowed_stages'] == stages
    assert store.list_artifacts(orch.run_id) == before and store.list_approvals(orch.run_id) == approvals


def test_complete_exact_reuse_has_no_unselected_completion_reminder(tmp_path, monkeypatch):
    store, pipe, settings, ledger, run = seed(tmp_path, monkeypatch)
    attempt = pipe.start_attempt(run, 'writing')
    artifact = pipe.submit(run, 'writing', attempt.token, **submission())
    declared = {**proposal(), 'reuse_artifact': {'stage': 'writing', 'version': artifact.version,
                                               'sha256': artifact.sha256, 'quote': TEXT}}
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', declared),
                           final_turn('沿用准确原稿。')], legacy_task_contracts=False)
    result = Orchestrator(store, pipe, settings, model, ledger, run).send(TEXT)
    assert len(model.calls) == 2 and result.deliverable['status'] == 'complete'
    assert store.latest_artifact(run, 'writing') == artifact
    assert '任务已绑定' not in model.requests_text()


@pytest.mark.parametrize('mode', ['status', 'stop', 'discuss', 'clarify'])
def test_readonly_interpretation_does_not_receive_creative_scope_reminder(env, tmp_path, mode):
    store, pipe, ledger = env
    text = '查看当前状态。'
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation',
        proposal(text, mode=mode, goal='follow_up', format='none', stages=[])), final_turn('当前记录已读取。')],
        legacy_task_contracts=False)
    result = make_orchestrator(store, pipe, ledger, model, tmp_path).send(text)
    assert len(model.calls) == 2 and result.stopped_because == 'done'
    assert '任务已绑定' not in model.requests_text()


def test_last_round_binding_keeps_original_round_limit(env, tmp_path):
    store, pipe, ledger = env
    model = ScriptedModel([tool_turn('read', 'read_capabilities', {}),
        tool_turn('bind', 'propose_task_interpretation', proposal())], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    orch.settings = replace(orch.settings, max_tool_rounds=2)
    result = orch.send(TEXT)
    assert len(model.calls) == 2 and result.stopped_because == 'max_rounds'
    assert result.deliverable['status'] == 'incomplete' and not orch._scope_selected


@pytest.mark.parametrize('change', ['unbound', 'identity', 'scope', 'readonly'])
def test_only_accurate_bound_current_task_can_get_unselected_reminder(env, tmp_path, change):
    store, pipe, ledger = env
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', proposal()),
                           final_turn('结束。'), final_turn('结束。')], legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    orch.send(TEXT)
    if change == 'unbound':
        orch._current_user_input = None
    else:
        orch._task_constraints = copy.deepcopy(orch._task_constraints)
        if change == 'identity':
            orch._task_constraints['interpretation_id'] = 'untrusted'
        elif change == 'scope':
            orch._task_constraints['allowed_stages'] = ['direction']
        else:
            orch._task_constraints['readonly'] = True
    assert orch._completion_check() is None


def natural_successor(tmp_path):
    from test_conversational_decision import seed_direction, settings_for, RUN
    from test_decision import received_message
    store, pipe = seed_direction(tmp_path)
    text = '采用 A，继续完成素材安排和完整脚本。'
    receipt = received_message(store, pipe, RUN, text)
    candidate = pipe.stage_state(RUN, 'direction').latest
    declared = proposal(text, stages=['material_plan', 'writing'])
    declared['task']['pause_after'] = False
    model = ScriptedModel([
        tool_turn('bind', 'propose_task_interpretation', declared),
        tool_turn('adopt', 'propose_user_action', {'action': 'select_direction',
            'target': {'stage': 'direction', 'version': candidate.version,
                       'artifact_sha256': candidate.sha256}, 'option_id': 'A'}),
        final_turn('已记录采用。')], legacy_task_contracts=False)
    settings = settings_for(tmp_path)
    ledger = Ledger(store, settings)
    Orchestrator(store, pipe, settings, model, ledger, RUN).send(text, user_input_id=receipt['message_id'])
    intent = next(e for e in store.read_events(RUN) if e['kind'] == 'decision_followup_requested')
    store.append_event(RUN, 'job_created', job_id='natural-successor',
        request_id=intent['request_id'], text=intent['text'],
        job_payload={'source_user_input_id': receipt['message_id']}, user_input_id=None)
    runtime = AgentRuntime(store, RUN)
    runtime.create_task('natural-successor', role='orchestrator', parent_job_id='natural-successor',
                        source_message_id=receipt['message_id'])
    runtime.transition('natural-successor', 'running')
    return store, pipe, settings, ledger, RUN, receipt, intent


def test_natural_adoption_successor_reminds_same_bound_task_without_readopting(tmp_path):
    store, pipe, settings, ledger, run, receipt, intent = natural_successor(tmp_path)
    approvals = store.list_approvals(run)
    bound = interpretation_for(store, run, receipt['message_id'])
    model = ScriptedModel([final_turn('先结束。'), final_turn('仍然结束。')], legacy_task_contracts=False)
    orch = Orchestrator(store, pipe, settings, model, ledger, run)
    token = bind_task('natural-successor')
    try:
        result = orch.send(intent['text'], internal_instruction=True,
                           source_user_input_id=receipt['message_id'])
    finally:
        reset_task(token)
    assert len(model.calls) == 2 and result.stopped_because == 'incomplete'
    assert '任务已绑定' in model.requests_text()
    assert not orch._scope_selected and orch._current_user_input is None
    assert orch._task_user_input == receipt and store.list_approvals(run) == approvals
    assert interpretation_for(store, run, receipt['message_id']) == bound


@pytest.mark.parametrize('invalid', ['unrelated_job', 'source', 'text', 'consumed'])
def test_unrelated_internal_instruction_cannot_inherit_completion_reminder(tmp_path, invalid):
    store, pipe, settings, ledger, run, receipt, intent = natural_successor(tmp_path)
    if invalid in {'text', 'consumed', 'source'}:
        path = store.run_dir(run) / 'events.jsonl'
        import json
        events = store.read_events(run)
        if invalid == 'text':
            next(e for e in events if e['kind'] == 'job_created')['text'] = 'Unrelated internal instruction'
        elif invalid == 'source':
            next(e for e in events if e['kind'] == 'job_created')['job_payload']['source_user_input_id'] = 'another-source'
        else:
            next(e for e in events if e['kind'] == 'user_message_consumed')['result']['continuation_request_id'] = 'other-decision'
        path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')
    orch = Orchestrator(store, pipe, settings, ScriptedModel([final_turn('结束。')]), ledger, run)
    token = bind_task('unrelated' if invalid == 'unrelated_job' else 'natural-successor')
    try:
        orch.send(intent['text'], internal_instruction=True, source_user_input_id=receipt['message_id'])
        assert orch._completion_check() is None
    finally:
        reset_task(token)
