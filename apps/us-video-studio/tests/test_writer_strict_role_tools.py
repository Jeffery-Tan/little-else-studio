"""New role tools keep logical boundaries through strict wire projections."""
import copy
import json

import pytest

from studio.agent_sessions import AgentSessions
from studio.agents.client import ModelError, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient, _matches_strict_schema
from studio.agents.mimo_client import MiMoClient
from studio.agents.requests import safe_response_diagnostic
from studio.conversation_input import (
    accept, receive, record_task_interpretation, start_execution,
)
from studio.models import PipelineError
from studio.store import Store
from studio.tools.registry import (
    REGISTRY, STRICT_ABSENT, strict_writer_execution_arguments, tools_for,
)
from test_deepseek_client import transport
from test_writing_tool_schema import seed, stream_reply, submission, wire


QUOTE = '只写脚本，台词自然一点'


def interpretation():
    return {
        'reuse_artifact': STRICT_ABSENT, 'video_use': STRICT_ABSENT, 'image_use': STRICT_ABSENT,
        'task': {'quote': QUOTE, 'mode': 'revise', 'deliverable': 'writing',
                 'format': 'video', 'allowed_stages': ['writing'], 'pause_after': False},
        'constraints': [],
    }


def tool_response(name, args):
    data = wire(submission())
    call = data['choices'][0]['message']['tool_calls'][0]
    call['function'] = {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}
    return data


def tool_schema(name):
    return next(tool['input_schema'] for tool in tools_for('writer') if tool['name'] == name)


def test_absence_branches_are_closed_strings_and_root_no_argument_tools_remain():
    schema = tool_schema('propose_task_interpretation')
    for field in ('reuse_artifact', 'video_use', 'image_use'):
        assert schema['properties'][field]['anyOf'][1] == {
            'type': 'string', 'enum': [STRICT_ABSENT]}
    for name in ('read_capabilities', 'list_materials'):
        assert tool_schema(name)['properties'] == {}
    from studio.agents.deepseek_client import _check_strict_schema
    for tool in tools_for('writer'):
        _check_strict_schema(tool['input_schema'], reject_nested_empty=True)


@pytest.mark.parametrize('placement', ['property', 'any_of', 'array_item'])
def test_deepseek_nested_empty_objects_stop_before_http_and_mimo_contract_is_preserved(monkeypatch, placement):
    empty = {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    child = {'property': empty,
             'any_of': {'anyOf': [empty, {'type': 'string', 'enum': [STRICT_ABSENT]}]},
             'array_item': {'type': 'array', 'items': empty}}[placement]
    tools = [{'name': 'synthetic_tool', 'description': 'Synthetic protocol only', 'strict': True,
              'input_schema': {'type': 'object', 'properties': {'value': child},
                               'required': ['value'], 'additionalProperties': False}}]
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(model='synthetic-model', system='', messages=[], tools=tools)
    assert seen == [] and caught.value.provider_invoked is False
    assert caught.value.usage_status == 'known' and caught.value.retryable is False
    assert MiMoClient('synthetic-only')._payload('synthetic-model', '', [], tools)['tools'][0]['function']['parameters'] == tools[0]['input_schema']


def test_absence_marker_only_decodes_our_owned_schema():
    arguments = interpretation()
    schema = copy.deepcopy(tool_schema('propose_task_interpretation'))
    schema['description'] = 'Synthetic foreign schema'
    assert strict_writer_execution_arguments('propose_task_interpretation', arguments, schema) is arguments
    assert arguments['reuse_artifact'] == arguments['video_use'] == STRICT_ABSENT


def trusted_input(tmp_path):
    store = Store(tmp_path)
    run_id = 'strict-role-tools'
    store.create_run(run_id, 'Synthetic only', QUOTE)
    receipt = receive(store, run_id, QUOTE, 'synthetic-request')
    accept(store, run_id, receipt)
    start_execution(store, run_id, receipt)
    return store, run_id, receipt


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('name, args, expected', [
    ('read_role_history', {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 12000},
     {'offset': 0, 'limit': 12000}),
    ('propose_task_interpretation', interpretation(),
     {key: value for key, value in interpretation().items() if key not in {'reuse_artifact', 'video_use', 'image_use'}}),
])
def test_strict_sentinels_only_change_detached_execution_copy(
        monkeypatch, client_class, streaming, name, args, expected):
    original = copy.deepcopy(args)
    data = tool_response(name, args)
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    client = client_class('synthetic-only')
    method = client.complete_with_progress if streaming else client.complete
    turn = method(model='synthetic-model', system='Synthetic only', messages=[], tools=tools_for('writer'))
    assert len(seen) == 1
    assert turn.tool_calls[0].input == expected
    raw_call = next(block for block in turn.raw_blocks if block['type'] == 'tool_use')
    assert raw_call['input'] == original and args == original
    assert raw_call['input'] is not turn.tool_calls[0].input
    assert turn.response_normalizations == []
    turn.tool_calls[0].input['synthetic_mutation'] = 'Detached only'
    assert raw_call['input'] == original


def test_complete_grounded_objects_and_exact_history_sequences_are_preserved():
    proposal = interpretation()
    proposal['video_use'] = {'source': 'fixture.mp4', 'purpose': 'reference', 'quote': QUOTE}
    proposal['reuse_artifact'] = {'stage': 'writing', 'version': 2, 'sha256': 'a' * 64, 'quote': QUOTE}
    proposal['image_use'] = {'quote': QUOTE, 'images': [
        {'name': 'synthetic.png', 'sha256': 'b' * 64, 'purpose': 'product'}]}
    schema = tool_schema('propose_task_interpretation')
    assert _matches_strict_schema(proposal, schema)
    result = strict_writer_execution_arguments('propose_task_interpretation', proposal, schema)
    assert result == proposal and result['video_use'] is not proposal['video_use']
    history = {'entry_seq': 9, 'before_seq': 8, 'offset': 7, 'limit': 1000}
    assert strict_writer_execution_arguments('read_role_history', history,
                                             tool_schema('read_role_history')) == history
    assert REGISTRY['read_role_history'].input_schema['properties']['entry_seq']['minimum'] == 1
    assert REGISTRY['propose_task_interpretation'].input_schema['required'] == ['task', 'constraints']


@pytest.mark.parametrize('name, args', [
    ('read_role_history', {'entry_seq': True, 'before_seq': 0, 'offset': 0, 'limit': 1000}),
    ('read_role_history', {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 12001}),
    ('read_role_history', {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 1000,
                           'PRIVATE_SESSION_ID': 'PRIVATE_VALUE'}),
    ('propose_task_interpretation', {**interpretation(), 'video_use': None}),
    ('propose_task_interpretation', {**interpretation(), 'video_use': {}}),
    ('propose_task_interpretation', {**interpretation(), 'reuse_artifact': {}}),
    ('propose_task_interpretation', {**interpretation(), 'video_use': 'PRIVATE_UNUSED_ALIAS'}),
    ('propose_task_interpretation', {**interpretation(), 'video_use': {'source': 'PRIVATE_VALUE'}}),
    ('propose_task_interpretation', {key: value for key, value in interpretation().items() if key != 'video_use'}),
    ('propose_task_interpretation', {**interpretation(), 'task': {**interpretation()['task'], 'pause_after': 0}}),
])
def test_invalid_new_role_tool_rejects_entire_batch_before_execution(monkeypatch, name, args):
    data = tool_response(name, args)
    data['choices'][0]['message']['tool_calls'].insert(0, {
        'id': 'read-first', 'type': 'function',
        'function': {'name': 'list_materials', 'arguments': '{}'},
    })
    seen = transport(monkeypatch, [data])
    executed = []
    with pytest.raises(ModelError) as failure:
        run_agent_loop(DeepSeekClient('synthetic-only'), model='synthetic-model',
            system='Synthetic only', messages=[], tools=tools_for('writer'),
            execute_tool=lambda *args: executed.append(args))
    assert len(seen) == 1 and executed == []
    assert failure.value.response_diagnostic['code'] == 'invalid_arguments_schema'
    assert failure.value.response_diagnostic['tool_call_index'] == 1
    assert failure.value.usage_status == 'known'
    assert 'PRIVATE' not in json.dumps(failure.value.response_diagnostic) + failure.value.public_message


def constraint():
    return {'quote': QUOTE, 'key': 'style.dialogue', 'value': 'Natural', 'operation': 'add',
            'polarity': 'require', 'intent': 'instruction', 'scope': {
                'lifetime': 'turn', 'stages': ['writing'], 'paths': [['segments', 0, 'line']], 'quote': ''},
            'supersedes': [], 'conflict_with': [], 'ambiguity': ''}


@pytest.mark.parametrize('change', ['too_many', 'duplicate_stages', 'empty_path', 'authorization', 'false_quote'])
def test_semantic_constraints_remain_enforced_by_original_handler(tmp_path, change):
    store, run_id, receipt = trusted_input(tmp_path)
    proposal = interpretation()
    proposal['constraints'] = [constraint()]
    if change == 'too_many':
        proposal['constraints'] *= 33
    elif change == 'duplicate_stages':
        proposal['constraints'][0]['scope']['stages'] *= 2
    elif change == 'empty_path':
        proposal['constraints'][0]['scope']['paths'] = [[]]
    elif change == 'authorization':
        proposal['constraints'][0]['key'] = 'authorization.generate'
    elif change == 'false_quote':
        proposal['task']['quote'] = 'PRIVATE_INVENTED_AUTHORIZATION'
    schema = tool_schema('propose_task_interpretation')
    assert _matches_strict_schema(proposal, schema)
    execution = strict_writer_execution_arguments('propose_task_interpretation', proposal, schema)
    with pytest.raises(PipelineError):
        record_task_interpretation(store, run_id, receipt, execution)
    assert not [event for event in store.read_events(run_id)
                if event['kind'] == 'user_task_interpretation_recorded']
    assert store.list_approvals(run_id) == []


def test_strict_key_schema_keeps_naming_description_and_original_handler_rejects_bad_name(tmp_path):
    from studio.agents.deepseek_client import _check_strict_schema
    from studio.conversation_input import TASK_INTERPRETATION_TOOL
    original = TASK_INTERPRETATION_TOOL['input_schema']['properties']['constraints']['items']['properties']['key']
    schema = tool_schema('propose_task_interpretation')
    projected = schema['properties']['constraints']['items']['properties']['key']
    assert 'pattern' in original and 'pattern' not in projected
    assert projected['description'] == original['description']
    _check_strict_schema(schema, reject_nested_empty=True)
    store, run_id, receipt = trusted_input(tmp_path)
    proposal = interpretation()
    proposal['constraints'] = [{**constraint(), 'key': 'generated_scenes_only'}]
    raw = copy.deepcopy(proposal)
    assert _matches_strict_schema(proposal, schema)
    execution = strict_writer_execution_arguments('propose_task_interpretation', proposal, schema)
    before = store.read_events(run_id)
    with pytest.raises(PipelineError, match='条件名称格式无效'):
        record_task_interpretation(store, run_id, receipt, execution)
    assert proposal == raw
    assert store.read_events(run_id) == before
    assert store.list_approvals(run_id) == []


def test_strict_decoding_executes_with_original_task_and_owned_history_readers(tmp_path):
    store, run_id, receipt = trusted_input(tmp_path)
    proposal = interpretation()
    # This fixture has no existing draft: it requests new writing, not revision.
    proposal['task']['mode'] = 'produce'
    proposal['constraints'] = [constraint()]
    execution = strict_writer_execution_arguments('propose_task_interpretation', proposal,
                                                 tool_schema('propose_task_interpretation'))
    result = record_task_interpretation(store, run_id, receipt, execution)
    assert result['task']['quote'] == QUOTE
    assert store.list_approvals(run_id) == []
    sessions = AgentSessions(store, run_id)
    sid = sessions.ensure(role='writer', stage='writing')['session_id']
    sessions.append(sid, 'model_request', system='PRIVATE_SYSTEM', messages=[], tools=[])
    public = sessions.append(sid, 'tool_result', name='read_artifact', step_id='one',
                             call_id='one', output='Original public result', is_error=False, skipped=False)
    args = {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 12000}
    page = sessions.read_role_history(sid, **strict_writer_execution_arguments(
        'read_role_history', args, tool_schema('read_role_history')))
    assert len(page['entries']) == 1 and 'PRIVATE' not in json.dumps(page)
    args['entry_seq'] = public['seq']
    full = sessions.read_role_history(sid, **strict_writer_execution_arguments(
        'read_role_history', args, tool_schema('read_role_history')))
    assert full['text'] == 'Original public result'


@pytest.mark.parametrize('path, rule, expected_type', [
    (['task', 'pause_after'], 'wrong_type', 'boolean'),
    (['video_use'], 'any_of_mismatch', 'anyOf'),
    (['constraints', 0, 'scope', 'paths', 0, 0], 'any_of_mismatch', 'anyOf'),
    (['limit'], 'maximum_mismatch', 'integer'),
    (['structured', 'segments', 0, 'shot_id'], 'missing_field', 'string'),
    (['structured', 'segments', 0, 'page_id'], 'missing_field', 'string'),
])
def test_new_diagnostics_keep_only_declared_keys_and_fixed_rules(path, rule, expected_type):
    issue = {'schema_path': path, 'schema_rule': rule, 'expected_type': expected_type,
             'raw_value': 'PRIVATE_VALUE', 'PRIVATE_FIELD': 'PRIVATE_VALUE'}
    assert safe_response_diagnostic(issue) == {
        'schema_path': path, 'schema_rule': rule, 'expected_type': expected_type}
    issue['schema_path'] = [*path, 'PRIVATE_FIELD']
    safe = safe_response_diagnostic(issue)
    assert 'schema_path' not in safe and 'PRIVATE' not in json.dumps(safe)


@pytest.mark.parametrize('format, field', [('video', 'shot_id'), ('photo', 'page_id'),
                                         ('video', 'character_ids'), ('photo', 'scene_id')])
def test_missing_identity_and_design_are_not_completed_as_auxiliary_defaults(monkeypatch, format, field):
    args = submission(format)
    args['structured'].pop('open_questions')
    args['structured']['segments'][0].pop(field)
    seen = transport(monkeypatch, [wire(args)])
    with pytest.raises(ModelError) as failure:
        DeepSeekClient('synthetic-only').complete(model='synthetic-model', system='Synthetic only',
                                                 messages=[], tools=tools_for('writer', format=format))
    assert len(seen) == 1
    assert failure.value.response_diagnostic['schema_path'] == ['structured', 'segments', 0, field]
    assert failure.value.response_diagnostic['schema_rule'] == 'missing_field'


@pytest.mark.parametrize('unknown_identity', [False, True])
def test_strict_full_script_preserves_supplied_shot_identity_and_server_rejects_invented_ids(
        tmp_path, monkeypatch, unknown_identity):
    store, pipeline, _, _, run_id = seed(tmp_path, monkeypatch)
    initial = submission()
    attempt = pipeline.start_attempt(run_id, 'writing')
    pipeline.submit(run_id, 'writing', attempt.token, **initial)
    first = store.latest_artifact(run_id, 'writing')
    args = copy.deepcopy(initial)
    args['structured']['segments'] = list(reversed(first.structured['segments']))
    args['structured']['segments'][0]['picture'] += ' Synthetic revised action.'
    if unknown_identity:
        args['structured']['segments'][0]['shot_id'] = 'shot-99999'
    seen = transport(monkeypatch, [wire(args)])
    turn = DeepSeekClient('synthetic-only').complete(model='synthetic-model', system='Synthetic only',
                                                   messages=[], tools=tools_for('writer'))
    assert turn.tool_calls[0].input == args and len(seen) == 1
    attempt = pipeline.start_attempt(run_id, 'writing')
    if unknown_identity:
        with pytest.raises(PipelineError, match='shot_id'):
            pipeline.submit(run_id, 'writing', attempt.token, **turn.tool_calls[0].input)
        assert store.latest_artifact(run_id, 'writing').version == 1
    else:
        pipeline.submit(run_id, 'writing', attempt.token, **turn.tool_calls[0].input)
        updated = store.latest_artifact(run_id, 'writing')
        assert [row['shot_id'] for row in updated.structured['segments']] == [
            row['shot_id'] for row in args['structured']['segments']]
        assert updated.structured['segments'][0]['picture'] == args['structured']['segments'][0]['picture']
