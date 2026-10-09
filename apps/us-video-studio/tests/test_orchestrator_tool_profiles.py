"""Exact per-request tool manifests, isolated receipts and Mock HTTP only."""
import copy
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio import conversation_input
from studio.agent_images import observe, saved_observations
from studio.agents.client import JobCancelled, ModelError, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient, STRICT_ENDPOINT, _strict_schemas
from studio.agents.mimo_client import MiMoClient
from studio.agents.orchestrator import Orchestrator
from studio.agents.requests import read_request
from studio.interactions import ask
from studio.ledger import Ledger
from studio.materials import accept_upload, resolve
from studio.models import STAGES
from studio.pipeline import Pipeline
from studio.project_integrity import ProjectDataError
from studio.task_contract import READ_ONLY_TASK_GUIDANCE
from studio.tools.registry import REGISTRY, STRICT_ABSENT, orchestrator_profile_tools, tools_for
from test_agent_images import tool_result_for
from test_deepseek_client import transport
from test_image_analysis import scene, wait_job
from test_image_analysis_task_contract import proposal, six
from test_image_understanding import configure, frozen, answer, observer_reply
from test_natural_reference import scripted
from test_orchestration import env, make_orchestrator, seed_intake, RUN as LOCAL_RUN
from test_server import RUN
from test_writing_tool_schema import stream_reply, wire


TEXT = '只检查本项目商品图片的可见内容和未知信息，不出方向、脚本或生成媒体。'
BASE_NAMES = ['read_user_actions', 'read_capabilities', 'read_project_state', 'list_materials']
INITIAL_NAMES = [*BASE_NAMES, 'read_artifact', 'ask_user', 'propose_task_interpretation', 'observe_images']
BOUND_NAMES = [*BASE_NAMES, 'observe_images']


def declared(text=TEXT, **task):
    return {**proposal(text, **task), 'reuse_artifact': STRICT_ABSENT,
            'video_use': STRICT_ABSENT, 'image_use': STRICT_ABSENT}


def fallback():
    return [tool for tool in tools_for('orchestrator')
            if tool['name'] not in {'read_learning_session', 'propose_learning_delta'}]


def names(tools):
    return [tool['name'] for tool in tools]


def received(store, pipeline, run_id, *, execute=True, displayed=None, **fields):
    context_id = conversation_input.display_context(store, pipeline, run_id,
        displayed or {'pending': None, 'questions': [], 'interaction': None})
    receipt = conversation_input.receive(store, run_id, TEXT, 'profile-request',
                                          context_id, **fields)
    conversation_input.accept(store, run_id, receipt)
    if execute:
        conversation_input.start_execution(store, run_id, receipt)
    return receipt


def ready(env, tmp_path):
    store, pipeline, ledger = env
    orch = make_orchestrator(store, pipeline, ledger, ScriptedModel([]), tmp_path)
    orch._current_user_input = received(store, pipeline, LOCAL_RUN)
    return orch


def http_reply(*calls):
    data = wire({})
    data['choices'][0]['message']['tool_calls'] = [
        {'id': identity, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}
        for identity, name, args in calls]
    return data


def ui_chat(scene, text=TEXT, request='profile-ui'):
    context = scene[0].get(f'/api/runs/{RUN}/timeline')
    assert context.status_code == 200, context.text
    response = scene[0].post(f'/api/runs/{RUN}/jobs', json={
        'text': text, 'request_id': request, 'display_context_id': context.json()['display_context_id']})
    assert response.status_code == 200, response.text
    return wait_job(scene[0], response.json())


@pytest.mark.parametrize('profile, expected', [('initial', INITIAL_NAMES), ('image_analysis', BOUND_NAMES)])
def test_profiles_are_closed_provider_compatible_and_do_not_mutate_other_tools(profile, expected):
    before = copy.deepcopy({key: value.input_schema for key, value in REGISTRY.items()})
    tools = orchestrator_profile_tools(profile)
    assert names(tools) == expected and all(tool['strict'] is True for tool in tools)
    assert set(_strict_schemas(tools, reject_nested_empty=True)) == set(expected)
    for client in (DeepSeekClient('synthetic-only'), MiMoClient('synthetic-only')):
        payload = client._payload('synthetic-model', 'Synthetic only', [], tools)
        assert all(tool['function']['strict'] is True for tool in payload['tools'])
        assert [tool['function']['parameters'] for tool in payload['tools']] == [tool['input_schema'] for tool in tools]
    observe_schema = tools[-1]['input_schema']
    assert set(observe_schema['properties']) == ({'images', 'task_interpretation'} if profile == 'initial' else {'images'})
    if profile == 'initial':
        nested = observe_schema['properties']['task_interpretation']
        assert nested['properties']['task']['properties']['mode']['enum'] == ['analyze']
        assert nested['required'] == list(nested['properties'])
    tools[0]['input_schema']['properties']['synthetic'] = {'type': 'string'}
    assert {key: value.input_schema for key, value in REGISTRY.items()} == before
    assert all('strict' not in tool for tool in fallback())


def test_initial_descriptions_distinguish_creative_binding_from_readonly_combined_entry():
    tools = {tool['name']: tool for tool in orchestrator_profile_tools('initial')}
    bind = tools['propose_task_interpretation']
    observe = tools['observe_images']
    assert 'complete task and conditions' in bind['description']
    assert 'current deliverable and later creative requirements' in bind['description']
    assert 'NEXT model request' in bind['description'] and 'same batch' in bind['description']
    assert 'ONLY for a pure read-only image-analysis task' in observe['description']
    assert 'task.mode=analyze, task.deliverable=analysis, task.allowed_stages=[]' in observe['description']
    assert 'Never drop conditions or replace the creative deliverable' in observe['description']
    task = observe['input_schema']['properties']['task_interpretation']['properties']['task']['properties']
    assert task['mode']['enum'] == ['analyze'] and task['deliverable']['enum'] == ['analysis']
    assert task['format']['enum'] == ['photo', 'none']
    assert 'direction' in bind['input_schema']['properties']['task']['properties']['deliverable']['enum']
    assert set(bind['input_schema']['properties']) == {'task', 'constraints', 'reuse_artifact', 'video_use', 'image_use'}
    assert all(field['anyOf'][1] == {'type': 'string', 'enum': [STRICT_ABSENT]}
               for key, field in bind['input_schema']['properties'].items()
               if key in {'reuse_artifact', 'video_use', 'image_use'})


def test_readonly_request_builds_only_one_context_per_actual_model_call(env, tmp_path, monkeypatch):
    store, pipe, ledger = env
    receipt = received(store, pipe, LOCAL_RUN)
    args = declared(mode='status', deliverable='follow_up', format='none', allowed_stages=[])
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', args),
        tool_turn('read', 'read_capabilities', {}), final_turn('当前状态已核对。')],
        legacy_task_contracts=False)
    events = []
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    orch.on_event = events.append
    counts = {'profile': 0, 'limited': 0, 'full': 0}
    profile, limited, full = orch._request_profile, orch._limited_system, orch._system_extra
    def track_profile(**kwargs):
        counts['profile'] += 1
        return profile(**kwargs)
    def track_limited(*args):
        counts['limited'] += 1
        return limited(*args)
    def track_full():
        counts['full'] += 1
        return full()
    monkeypatch.setattr(orch, '_request_profile', track_profile)
    monkeypatch.setattr(orch, '_limited_system', track_limited)
    monkeypatch.setattr(orch, '_system_extra', track_full)
    result = orch.send(TEXT, user_input_id=receipt['message_id'])
    assert result.stopped_because == 'done'
    assert counts == {'profile': len(model.calls), 'limited': len(model.calls), 'full': 0}
    import hashlib
    ready = next(e for e in events if e['type'] == 'context_ready')
    assert ready['system_sha256'] == hashlib.sha256(model.calls[0]['system'].encode('utf-8')).hexdigest()


def test_initial_edit_can_read_exact_base_then_bind_and_apply_only_that_field(tmp_path, monkeypatch):
    from test_revision_scope import env as revision_env, RUN as EDIT_RUN, plan_args, PlannedModel, latest_plan
    from test_revision_interpretation_scope import meaning, TEXT as EDIT_TEXT, PATH
    store, pipe, settings, ledger = revision_env.__wrapped__(tmp_path, monkeypatch)
    artifact = store.latest_artifact(EDIT_RUN, 'writing')
    before = copy.deepcopy(artifact.structured)
    shown = conversation_input.display_context(store, pipe, EDIT_RUN,
        {'pending': None, 'questions': [], 'interaction': None})
    receipt = conversation_input.receive(store, EDIT_RUN, EDIT_TEXT, 'initial-edit-read', shown)
    proposal = {**meaning(receipt), 'reuse_artifact': STRICT_ABSENT,
                'video_use': STRICT_ABSENT, 'image_use': STRICT_ABSENT}
    model = PlannedModel([
        tool_turn('base', 'read_artifact', {'stage': 'writing', 'version': artifact.version}),
        tool_turn('bind', 'propose_task_interpretation', proposal),
        tool_turn('route', 'prepare_creative_context',
                  {'intent': 'edit', 'format': 'video', 'deliverable': 'revision'}),
        tool_turn('plan', 'plan_revision', plan_args(store, path=PATH, quote=EDIT_TEXT)),
        lambda: tool_turn('dispatch', 'dispatch', {'stage': 'writing', 'brief': EDIT_TEXT,
                                                  'plan_id': latest_plan(store)}),
        tool_turn('patch', 'submit_patch', {'changes': [{'path': PATH, 'value': 'A different second line'}],
                                          'summary': 'Changed only the second line'}),
        final_turn('只修改第二句，其余保留。')], legacy_task_contracts=False)
    orch = Orchestrator(store, pipe, settings, model, ledger, EDIT_RUN)
    result = orch.send(EDIT_TEXT, user_input_id=receipt['message_id'])
    assert result.deliverable['status'] == 'complete'
    assert model.calls[0]['tool_names'] == model.calls[1]['tool_names'] == INITIAL_NAMES
    assert '用于定位修改字段的原结构' in str(model.calls[1]['messages'])
    expected = copy.deepcopy(before)
    expected['segments'][1]['line'] = 'A different second line'
    assert store.latest_artifact(EDIT_RUN, 'writing').structured == expected
    assert store.verify_artifact(EDIT_RUN, 'writing', artifact.version)


def test_current_executing_receipt_selects_initial_then_only_bound_image_tools(env, tmp_path):
    orch = ready(env, tmp_path)
    assert names(orch._request_tools(fallback())) == INITIAL_NAMES
    conversation_input.record_task_interpretation(env[0], LOCAL_RUN, orch._current_user_input, proposal(TEXT))
    assert names(orch._request_tools(fallback())) == BOUND_NAMES
    assert not any('approve' in name or 'generate' in name for name in BOUND_NAMES)


@pytest.mark.parametrize('mode, deliverable, format, stages', [
    ('produce', 'writing', 'video', ['writing']), ('analyze', 'analysis', 'video', []),
    ('stop', 'follow_up', 'none', []),
])
def test_other_bound_tasks_keep_original_manifest(env, tmp_path, mode, deliverable, format, stages):
    orch = ready(env, tmp_path)
    interpretation = proposal(TEXT, mode=mode, deliverable=deliverable, format=format, allowed_stages=stages)
    # Explicit domain declaration; tools never classify the user text for routing.
    conversation_input.record_task_interpretation(env[0], LOCAL_RUN, orch._current_user_input, interpretation)
    assert orch._request_tools(fallback()) == fallback()


@pytest.mark.parametrize('mode', ['status', 'discuss', 'clarify'])
@pytest.mark.parametrize('format', ['none', 'photo', 'video'])
def test_trusted_bound_readonly_task_uses_original_read_schemas(env, tmp_path, mode, format):
    orch = ready(env, tmp_path)
    assert names(orch._request_tools(fallback())) == INITIAL_NAMES
    conversation_input.record_task_interpretation(env[0], LOCAL_RUN, orch._current_user_input,
        proposal(TEXT, mode=mode, deliverable='follow_up', format=format, allowed_stages=[]))
    tools = orch._request_tools(fallback())
    assert names(tools) == names(orchestrator_profile_tools('read_only'))
    assert {'read_project_history', 'read_conversation_history', 'read_role_sessions',
            'read_production', 'ask_user', 'retrieve_learning_methods'} <= set(names(tools))
    assert not any(name in names(tools) for name in ('observe_images', 'prepare_production',
        'prepare_creative_context', 'dispatch', 'wait_role', 'write_stage', 'record_method_usage'))
    assert tools == [REGISTRY[name].to_api() for name in names(tools)]


@pytest.mark.parametrize('case', ['attachment', 'scope', 'reference', 'forged', 'question'])
def test_bound_readonly_special_paths_do_not_enter_narrow_profile(env, tmp_path, case):
    orch = ready(env, tmp_path)
    conversation_input.record_task_interpretation(env[0], LOCAL_RUN, orch._current_user_input,
        proposal(TEXT, mode='status', deliverable='follow_up', format='none', allowed_stages=[]))
    if case == 'attachment':
        # Even an untrusted attachment cannot widen the interpretation or enter
        # the new branch; the receipt guard catches the changed envelope first.
        orch._current_user_input = {**orch._current_user_input, 'attachments': [{'name': 'source.png'}]}
    elif case == 'scope':
        orch._scope_selected = True
    elif case == 'reference':
        orch._reference_binding = {'id': 'original-reference'}
    elif case == 'forged':
        orch._current_user_input = {**orch._current_user_input, 'text': 'Forged status.'}
    else:
        ask(env[0], LOCAL_RUN, {'question': 'Which original?', 'options': [
            {'label': 'One', 'description': ''}, {'label': 'Two', 'description': ''}]})
    assert orch._request_tools(fallback()) == fallback()


def test_readonly_send_freezes_context_branch_and_preserves_full_grounded_conditions(env, tmp_path, monkeypatch):
    from test_reply_conditions import reply_row
    from studio.agents import orchestrator as module
    store, pipe, ledger = env
    text = '只查询已保存状态，全文不超过400字；不改方案、不生成。'
    shown = conversation_input.display_context(store, pipe, LOCAL_RUN,
        {'pending': None, 'questions': [], 'interaction': None})
    receipt = conversation_input.receive(store, LOCAL_RUN, text, 'readonly-send', shown)
    conversation_input.accept(store, LOCAL_RUN, receipt)
    conversation_input.start_execution(store, LOCAL_RUN, receipt)
    args = declared(text, mode='status', deliverable='follow_up', format='none', allowed_stages=[])
    args['constraints'] = [reply_row('全文不超过400字')]
    model = ScriptedModel([tool_turn('bind', 'propose_task_interpretation', args),
        tool_turn('state', 'read_project_state', {}), final_turn('已读取状态；没有生成或修改方案。')],
        legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    monkeypatch.setattr(orch, '_system_extra', lambda: 'CREATIVE_CONTEXT_NOT_FOR_READONLY\n' * 3000)
    monkeypatch.setattr(orch.learning, 'prepare_context', lambda *a, **k: {
        'text': 'Exact method\n\nOriginal method limits.', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    run_loop = module.run_agent_loop
    def inspect_loop(*a, **kwargs):
        original_factory = kwargs['system_for_request']
        def frozen_system():
            chosen = orch._request_profile()
            if chosen == 'read_only':
                orch._scope_selected = True  # Changed state must not select a different system branch.
                try:
                    system = original_factory()
                finally:
                    orch._scope_selected = False
                assert '本轮已绑定为只读' in system
                return system
            return original_factory()
        kwargs['system_for_request'] = frozen_system
        return run_loop(*a, **kwargs)
    monkeypatch.setattr(module, 'run_agent_loop', inspect_loop)
    outcome = orch.send(text, user_input_id=receipt['message_id'])
    assert outcome.stopped_because == 'done' and len(model.calls) == 3
    assert model.calls[0]['tool_names'] == INITIAL_NAMES
    assert '本轮尚未绑定任务解释' in model.calls[0]['system']
    assert READ_ONLY_TASK_GUIDANCE in model.calls[0]['system']
    canonical = conversation_input.TASK_INTERPRETATION_TOOL['input_schema']['properties']['task']['properties']
    initial_bind = next(tool for tool in orchestrator_profile_tools('initial')
                        if tool['name'] == 'propose_task_interpretation')
    strict_task = initial_bind['input_schema']['properties']['task']['properties']
    assert canonical['deliverable']['description'] == strict_task['deliverable']['description'] == READ_ONLY_TASK_GUIDANCE
    assert 'CREATIVE_CONTEXT_NOT_FOR_READONLY' not in model.calls[0]['system']
    for request in model.calls[1:]:
        system = request['system']
        assert request['tool_names'] == names(orchestrator_profile_tools('read_only'))
        assert 'CREATIVE_CONTEXT_NOT_FOR_READONLY' not in system and len(system) < 20000
        assert text in system and store.read_run(LOCAL_RUN).brief in system
        assert 'Exact method\n\nOriginal method limits.' in system
        marker = '# 本轮绑定的真实用户来源与仍有效的项目条件'
        context, _ = json.JSONDecoder().raw_decode(system.split(marker)[1].lstrip())
        assert context['current_input'] == orch._task_constraints
        assert context['project_constraints'] == orch._project_constraints
        assert context['reply_constraints'] == orch._reply_constraints
        assert context['execution_scope']['source_message_id'] == receipt['message_id']
    assert not store.list_artifacts(LOCAL_RUN) and not store.list_approvals(LOCAL_RUN)


def test_initial_capability_reads_keep_frozen_manifest_until_next_bound_request(env, tmp_path, monkeypatch):
    from studio.agents.client import ToolCall, Turn, Usage
    store, pipe, ledger = env
    receipt = received(store, pipe, LOCAL_RUN)
    args = declared(mode='status', deliverable='follow_up', format='none', allowed_stages=[])
    simultaneous = Turn(text='', tool_calls=[ToolCall(id='bind', name='propose_task_interpretation', input=args),
        ToolCall(id='same-batch-capabilities', name='read_capabilities', input={})], stop_reason='tool_use',
        usage=Usage(20, 15), raw_blocks=[
            {'type': 'tool_use', 'id': 'bind', 'name': 'propose_task_interpretation', 'input': args},
            {'type': 'tool_use', 'id': 'same-batch-capabilities', 'name': 'read_capabilities', 'input': {}}])
    model = ScriptedModel([tool_turn('initial-capabilities', 'read_capabilities', {}), simultaneous,
        tool_turn('bound-capabilities', 'read_capabilities', {}), final_turn('已核对状态。')],
        legacy_task_contracts=False)
    orch = make_orchestrator(store, pipe, ledger, model, tmp_path)
    monkeypatch.setattr(orch.learning, 'prepare_context', lambda *a, **k: {
        'text': 'Exact current method.', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture'})
    outcome = orch.send(TEXT, user_input_id=receipt['message_id'])
    assert outcome.stopped_because == 'done' and len(model.calls) == 4
    assert model.calls[0]['tool_names'] == model.calls[1]['tool_names'] == INITIAL_NAMES
    for request in model.calls[:2]:
        assert '本轮尚未绑定任务解释' in request['system']
        assert '最终 deliverable 必须是用户要的交付' in request['system']
    for call_id, expected in (('initial-capabilities', INITIAL_NAMES),
                              ('same-batch-capabilities', INITIAL_NAMES),
                              ('bound-capabilities', names(orchestrator_profile_tools('read_only')))):
        result = tool_result_for(model, call_id)
        payload = json.loads(result['content'])
        assert payload['tools'] == expected and payload['tools_scope'] == 'this_frozen_request'
        assert not payload['reference_video']['agent_can_execute']
        assert payload['image_analysis']['agent_can_execute'] == ('observe_images' in expected)
        assert payload['production']['agent_can_approve_or_generate'] is False
    assert not store.list_artifacts(LOCAL_RUN) and not store.list_approvals(LOCAL_RUN)


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
def test_readonly_manifest_rejects_whole_batch_with_undeclared_mutation(monkeypatch, client_class):
    seen = transport(monkeypatch, [http_reply(('read', 'read_project_state', {}),
                                              ('write', 'write_stage', {}))])
    executed = []
    with pytest.raises(ModelError):
        run_agent_loop(client_class('synthetic-only'), model='synthetic-model', system='Readonly',
            messages=[], tools=fallback(), tools_for_request=lambda: orchestrator_profile_tools('read_only'),
            execute_tool=lambda name, args: executed.append(name) or 'Done')
    assert not executed and len(seen) == 1
    payload = json.loads(seen[0].content)
    assert not any(tool['function'].get('strict') for tool in payload['tools'])


@pytest.mark.parametrize('attribute', [
    '_decision_context', '_reference_recovery_context', '_production_feedback',
    '_production_scope', '_decision_recorded',
])
def test_internal_feedback_reference_and_decision_paths_keep_original_tools(env, tmp_path, attribute):
    orch = ready(env, tmp_path)
    setattr(orch, attribute, {'synthetic': 'existing trusted path'})
    assert orch._request_tools(fallback()) == fallback()


@pytest.mark.parametrize('argument', ['internal_instruction', 'historical_readonly'])
def test_nonfresh_request_modes_keep_their_original_manifest(env, tmp_path, argument):
    orch = ready(env, tmp_path)
    assert orch._request_tools(fallback(), **{argument: True}) == fallback()


@pytest.mark.parametrize('case', ['no_display', 'queued', 'changed_receipt', 'displayed_question', 'current_question', 'current_pending', 'scope_already_selected'])
def test_unverified_or_actionable_state_never_enters_initial_profile(env, tmp_path, case):
    store, pipeline, ledger = env
    orch = make_orchestrator(store, pipeline, ledger, ScriptedModel([]), tmp_path)
    if case == 'no_display':
        receipt = conversation_input.receive(store, LOCAL_RUN, TEXT, 'without-display')
        conversation_input.accept(store, LOCAL_RUN, receipt)
        conversation_input.start_execution(store, LOCAL_RUN, receipt)
    else:
        receipt = received(store, pipeline, LOCAL_RUN, execute=case != 'queued',
                           displayed={'questions': [{'id': 'shown-question'}]} if case == 'displayed_question' else None)
    orch._current_user_input = receipt
    if case == 'changed_receipt':
        orch._current_user_input = {**receipt, 'text': 'Synthetic forged input'}
    if case == 'current_question':
        ask(store, LOCAL_RUN, {'question': '请明确用途', 'options': [
            {'label': '商品', 'description': ''}, {'label': '参考', 'description': ''}]})
    if case == 'current_pending':
        seed_intake(pipeline)
        attempt = pipeline.start_attempt(LOCAL_RUN, 'direction')
        pipeline.submit(LOCAL_RUN, 'direction', attempt.token, content='Synthetic directions',
                        summary='Two directions')
    if case == 'scope_already_selected':
        orch._scope_selected = True
    assert orch._request_tools(fallback()) == fallback()


def test_profile_cannot_hide_current_project_damage(env, tmp_path):
    orch = ready(env, tmp_path)
    path = env[0].run_dir(LOCAL_RUN) / 'run.json'
    path.write_bytes(b'{"synthetic-damaged":')
    with pytest.raises(ProjectDataError):
        orch._request_tools(fallback())


def test_actual_video_attachment_cannot_be_narrowed_by_photo_output_declaration(env, tmp_path):
    from test_bailian_omni import video_bytes
    store, pipeline, ledger = env
    accept_upload(store, LOCAL_RUN, 'source.mp4', video_bytes())
    orch = make_orchestrator(store, pipeline, ledger, ScriptedModel([]), tmp_path)
    receipt = received(store, pipeline, LOCAL_RUN,
                       attachments=[{'name': 'source.mp4', 'purpose': 'reference'}])
    orch._current_user_input = receipt
    conversation_input.record_task_interpretation(store, LOCAL_RUN, receipt, proposal(TEXT))
    assert orch._request_tools(fallback()) == fallback()


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('streaming', [False, True])
def test_combined_wire_is_exact_and_private_replay_keeps_original_sentinels(monkeypatch, client_class, streaming):
    args = {'images': [{'name': 'fixture.png', 'purpose': 'product'}], 'task_interpretation': declared()}
    original = copy.deepcopy(args)
    reply = http_reply(('combined', 'observe_images', args))
    seen = transport(monkeypatch, [stream_reply(reply) if streaming else reply])
    client = client_class('synthetic-only')
    complete = client.complete_with_progress if streaming else client.complete
    turn = complete(model='synthetic-model', system='Synthetic only', messages=[],
                    tools=orchestrator_profile_tools('initial'))
    assert len(seen) == 1
    if client_class is DeepSeekClient:
        assert str(seen[0].url) == STRICT_ENDPOINT
    assert turn.tool_calls[0].input['task_interpretation'] == proposal(TEXT)
    assert next(block['input'] for block in turn.raw_blocks if block['type'] == 'tool_use') == original
    assert args == original and turn.response_normalizations == []


@pytest.mark.parametrize('failure', ['unknown', 'duplicate_id', 'schema'])
def test_entire_batch_rejects_before_any_execution_even_when_later_call_is_bad(failure):
    first = tool_turn('read', 'list_materials', {})
    second = tool_turn('later', 'list_materials', {})
    if failure == 'unknown':
        second.tool_calls[0].name = second.raw_blocks[0]['name'] = 'write_stage'
    elif failure == 'duplicate_id':
        second.tool_calls[0].id = second.raw_blocks[0]['id'] = 'read'
    else:
        second.tool_calls[0].input = second.raw_blocks[0]['input'] = {'extra': 'wrong schema'}
    first.tool_calls += second.tool_calls
    first.raw_blocks += second.raw_blocks
    model = ScriptedModel([first])
    dispatched = []
    with pytest.raises(ModelError) as caught:
        run_agent_loop(model, model='synthetic-model', system='', messages=[], tools=fallback(),
            tools_for_request=lambda: orchestrator_profile_tools('initial'),
            execute_tool=lambda name, args: dispatched.append((name, args)) or 'done')
    assert not dispatched and len(model.calls) == 1
    assert caught.value.retryable is False and caught.value.usage_status == 'known'
    assert caught.value.response_diagnostic['code'] in {'invalid_identity', 'invalid_arguments_schema'}


def test_combined_schema_diagnostic_preserves_owned_field_path_without_input_values():
    arguments = {'images': [{'name': 'fixture.png', 'purpose': 'product'}],
                 'task_interpretation': declared()}
    arguments['task_interpretation']['task']['quote'] = 17
    model = ScriptedModel([tool_turn('look', 'observe_images', arguments)])
    with pytest.raises(ModelError) as caught:
        run_agent_loop(model, model='synthetic-model', system='', messages=[], tools=fallback(),
            tools_for_request=lambda: orchestrator_profile_tools('initial'),
            execute_tool=lambda name, args: pytest.fail('Invalid batch cannot execute'))
    diagnostic = caught.value.response_diagnostic
    assert diagnostic['schema_path'] == ['task_interpretation', 'task', 'quote']
    assert diagnostic['tool_name'] == 'observe_images' and diagnostic['schema_rule'] == 'wrong_type'
    assert 'fixture.png' not in json.dumps(diagnostic) and '17' not in json.dumps(diagnostic)


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('streaming', [False, True])
def test_native_enum_rejection_keeps_whole_batch_unexecuted_usage_and_generic_public_format(
        monkeypatch, client_class, streaming):
    creative = declared(mode='produce', deliverable='direction', format='photo', allowed_stages=['intake', 'direction'])
    analysis = declared(deliverable='PRIVATE_REJECTED_DELIVERABLE')
    response = http_reply(('bind', 'propose_task_interpretation', creative),
        ('look', 'observe_images', {'images': [{'name': 'fixture.png', 'purpose': 'product'}],
                                    'task_interpretation': analysis}))
    seen = transport(monkeypatch, [stream_reply(response) if streaming else response])
    native = client_class('synthetic-only')
    if streaming:
        class Streaming:
            def complete(self, **request):
                return native.complete_with_progress(**request)
        client = Streaming()
    else:
        client = native
    dispatched = []
    with pytest.raises(ModelError) as caught:
        run_agent_loop(client, model='synthetic-model', system='Synthetic only', messages=[],
            tools=fallback(), tools_for_request=lambda: orchestrator_profile_tools('initial'),
            execute_tool=lambda name, args: dispatched.append((name, args)) or 'done')
    error = caught.value
    assert not dispatched and len(seen) == 1
    assert error.usage_status == 'known'
    assert (error.usage.input_tokens, error.usage.output_tokens) == (44976, 13602)
    assert error.response_diagnostic['tool_name'] == 'observe_images'
    assert error.response_diagnostic['schema_path'] == ['task_interpretation', 'task', 'deliverable']
    assert error.response_diagnostic['schema_rule'] == 'enum_mismatch'
    assert error.response_diagnostic['expected_type'] == error.response_diagnostic['actual_type'] == 'string'
    assert '本次工具格式' in error.public_message and '脚本提交格式' not in error.public_message
    visible = str(error) + error.public_message + json.dumps(error.response_diagnostic)
    assert 'PRIVATE_REJECTED_DELIVERABLE' not in visible and 'SYNTHETIC_PRIVATE_REASONING' not in visible
    payload = json.loads(seen[0].content)
    assert all(tool['function']['strict'] is True for tool in payload['tools'])
    assert payload['tools'][-1]['function']['parameters']['properties']['task_interpretation']['properties'][
        'task']['properties']['deliverable']['enum'] == ['analysis']


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('streaming', [False, True])
def test_later_malformed_json_never_executes_first_read_or_retries(monkeypatch, client_class, streaming):
    reply = http_reply(('read', 'list_materials', {}), ('look', 'observe_images', {}))
    reply['choices'][0]['message']['tool_calls'][1]['function']['arguments'] = '{"images":['
    seen = transport(monkeypatch, [stream_reply(reply) if streaming else reply])
    client = client_class('synthetic-only')
    if streaming:
        class Streaming:
            def complete(self, **request):
                return client.complete_with_progress(**request)
        client_for_loop = Streaming()
    else:
        client_for_loop = client
    dispatched = []
    with pytest.raises(ModelError) as caught:
        run_agent_loop(client_for_loop, model='synthetic-model', system='Synthetic only', messages=[],
            tools=fallback(), tools_for_request=lambda: orchestrator_profile_tools('initial'),
            execute_tool=lambda name, args: dispatched.append((name, args)) or 'done')
    assert not dispatched and len(seen) == 1
    assert caught.value.response_diagnostic['code'] == 'invalid_arguments_json'


def test_callback_freezes_request_and_recomputes_only_after_completed_batch():
    current = orchestrator_profile_tools('initial')
    snapshot = copy.deepcopy(current)
    requests = []
    class Model:
        def complete(self, **request):
            requests.append(copy.deepcopy(request['tools']))
            if len(requests) == 1:
                current[:] = orchestrator_profile_tools('image_analysis')
                request['tools'].clear()  # A client cannot alter the frozen manifest.
                return tool_turn('read', 'list_materials', {})
            return final_turn('Done')
    executed = []
    result = run_agent_loop(Model(), model='synthetic-model', system='', messages=[], tools=fallback(),
        tools_for_request=lambda: current,
        execute_tool=lambda name, args: executed.append(name) or 'Synthetic read')
    assert requests[0] == snapshot and names(requests[1]) == BOUND_NAMES
    assert executed == ['list_materials'] and result.stopped_because == 'done'


def test_cancellation_before_request_and_after_provider_response_prevents_tools():
    for stop_after in (1, 2):
        model = ScriptedModel([tool_turn('read', 'list_materials', {})])
        count, dispatched = 0, []
        def cancel():
            nonlocal count
            count += 1
            if count == stop_after:
                raise JobCancelled()
        with pytest.raises(JobCancelled):
            run_agent_loop(model, model='synthetic-model', system='', messages=[], tools=fallback(),
                tools_for_request=lambda: orchestrator_profile_tools('initial'), check_cancel=cancel,
                execute_tool=lambda name, args: dispatched.append(name) or 'done')
        assert len(model.calls) == stop_after - 1 and not dispatched


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_real_combined_handler_saves_six_mock_http_observations_and_next_request_is_bound(scene, monkeypatch):
    items = six(scene)
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [observer_reply(answer(first=i), route[1].progress_stream)
                                   for i in range(1, 7)])
    scripted(scene, monkeypatch, [tool_turn('combined', 'observe_images', {
        'images': items, 'task_interpretation': declared()}), final_turn('六张图片均已核对，未知项保留。')])
    store, pipeline = scene[1], Pipeline(scene[1])
    receipt = received(store, pipeline, RUN)
    orch = Orchestrator(store, pipeline, scene[5], scene[2], Ledger(store, scene[5]), RUN,
        image_observer=lambda items, emit, cancel: observe(store, scene[5], RUN, items,
            endpoint=None, emit=emit, check_cancel=cancel, freeze_route=lambda: route))
    outcome = orch.send(TEXT, user_input_id=receipt['message_id'])
    assert outcome.stopped_because == 'done' and outcome.deliverable['status'] == 'complete'
    assert names(scene[2].calls[0]['tools']) == INITIAL_NAMES
    assert names(scene[2].calls[1]['tools']) == BOUND_NAMES
    assert len(seen) == len(saved_observations(scene[1], RUN)) == 6
    assert not tool_result_for(scene[2], 'combined')['is_error']
    assert not scene[1].list_approvals(RUN)
    assert all(scene[1].latest_artifact(RUN, stage) is None for stage in STAGES)
    snapshots = [read_request(scene[1], RUN, row['call_id']) for row in scene[1].read_events(RUN)
                 if row['kind'] == 'agent_request_snapshot' and row['role'] == 'orchestrator']
    assert names(snapshots[0]['request']['tools']) == INITIAL_NAMES
    assert names(snapshots[1]['request']['tools']) == BOUND_NAMES


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_handler_rejects_bad_scope_then_corrects_same_receipt_and_reuses_six_cached_reports(scene, monkeypatch):
    items = six(scene)
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [observer_reply(answer(first=i), route[1].progress_stream)
                                   for i in range(1, 7)])
    observe(scene[1], scene[5], RUN, items, endpoint=None, emit=lambda _: None,
            check_cancel=lambda: None, freeze_route=lambda: route)
    initial_usage = [entry for entry in Ledger(scene[1], scene[5]).entries(RUN) if entry.stage == 'image_analysis']
    scripted(scene, monkeypatch, [
        tool_turn('bad', 'propose_task_interpretation', declared(allowed_stages=['intake'])),
        tool_turn('corrected', 'propose_task_interpretation', declared()),
        tool_turn('cached', 'observe_images', {'images': items}),
        final_turn('六张已保存观察已核对，没有新视觉调用。')])
    job = ui_chat(scene)
    assert job['status'] == 'succeeded' and job['deliverable']['status'] == 'complete', job
    assert [names(call['tools']) for call in scene[2].calls] == [INITIAL_NAMES, INITIAL_NAMES, BOUND_NAMES, BOUND_NAMES]
    assert tool_result_for(scene[2], 'bad')['is_error']
    assert 'allowed_stages 必须为 []' in tool_result_for(scene[2], 'bad')['content']
    assert not tool_result_for(scene[2], 'corrected')['is_error']
    cached = json.loads(tool_result_for(scene[2], 'cached')['content'].split('\n', 1)[0])
    assert cached['reused'] == len(cached['observations']) == 6 and len(seen) == 6
    bindings = [row for row in scene[1].read_events(RUN) if row['kind'] == 'user_task_interpretation_recorded']
    assert len(bindings) == 1 and bindings[0]['interpretation']['task'] == proposal(TEXT)['task']
    assert [entry for entry in Ledger(scene[1], scene[5]).entries(RUN) if entry.stage == 'image_analysis'] == initial_usage


@pytest.mark.parametrize('failure', ['unknown_source', 'source_changed'])
def test_bound_strict_manifest_keeps_source_preflight_authoritative(scene, monkeypatch, failure):
    items = [{'name': 'fixture.png', 'purpose': 'product'}]
    second = tool_turn('look', 'observe_images', {'images': items})
    if failure == 'unknown_source':
        second.tool_calls[0].input['images'][0]['name'] = 'not-in-project.png'
    scripted(scene, monkeypatch, [tool_turn('bind', 'propose_task_interpretation', declared()), second,
                                final_turn('来源未能核对，本轮没有观察。'), final_turn('来源未能核对。')])
    if failure == 'source_changed':
        original = scene[2].complete
        calls = 0
        def complete(**request):
            nonlocal calls
            calls += 1
            if calls == 2:
                resolve(scene[1], RUN, 'fixture.png').write_bytes(b'Synthetic changed bytes')
            return original(**request)
        monkeypatch.setattr(scene[2], 'complete', complete)
    job = ui_chat(scene)
    assert job['status'] == 'blocked', job
    assert names(scene[2].calls[1]['tools']) == BOUND_NAMES
    assert tool_result_for(scene[2], 'look')['is_error']
    assert not saved_observations(scene[1], RUN)
    assert all(call['tools'] for call in scene[2].calls)  # No separate visual request.
    assert not any(entry.stage == 'image_analysis' for entry in Ledger(scene[1], scene[5]).entries(RUN))
