"""MiMo protocol and model routing; synthetic HTTP only, no real credentials."""
import base64
import json

import httpx
import pytest

from studio.agents.client import JobCancelled, ModelError, run_agent_loop
from studio.agents.mimo_client import MiMoClient, ENDPOINT, REASONING_BLOCK
from studio.connections import ConnectionSnapshot, ConnectionStore, ConnectionUnavailable
from studio.tools.registry import tools_for
from test_connections import update_payload
from test_deepseek_client import TOOLS, response, transport
from test_writing_tool_schema import stream_reply, submission, wire

MODEL = 'mimo-v2.6-flash'


@pytest.mark.parametrize('streaming', [False, True])
def test_official_writer_endpoint_strict_schema_and_private_replay(monkeypatch, streaming):
    args = submission()
    args['structured'].pop('open_questions')
    data = wire(args)
    data['model'] = MODEL
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    progress = []
    client = MiMoClient('synthetic-only', max_tokens=16000)
    kwargs = dict(model=MODEL, system='Synthetic', messages=[], tools=tools_for('writer'))
    turn = (client.complete_with_progress(**kwargs, on_progress=progress.append) if streaming
            else client.complete(**kwargs))
    assert len(seen) == 1 and str(seen[0].url) == ENDPOINT
    assert seen[0].headers['Authorization'] == 'Bearer synthetic-only'
    payload = json.loads(seen[0].content)
    assert payload['model'] == MODEL and payload['max_completion_tokens'] == 16000
    assert payload['thinking'] == {'type': 'enabled'}
    assert 'reasoning_effort' not in payload and 'max_tokens' not in payload and 'temperature' not in payload
    assert all(t['function']['strict'] is True for t in payload['tools'])
    assert turn.tool_calls[0].input['structured']['open_questions'] == []
    assert turn.raw_blocks[0] == {'type': REASONING_BLOCK, 'reasoning_content': 'SYNTHETIC_PRIVATE_REASONING'}
    assert 'open_questions' not in turn.raw_blocks[1]['input']['structured']
    assert len(turn.response_normalizations) == 1
    assert turn.usage.output_tokens == 13602 and turn.model == MODEL
    assert turn.thinking == '' and 'SYNTHETIC_PRIVATE_REASONING' not in json.dumps(progress)


def test_actual_two_turn_tool_protocol_replays_only_mimo_private_history(monkeypatch):
    replies = [response('Checking the artifact', calls=[('read-1', 'intake')]), response('Done')]
    for reply in replies:
        reply['model'] = MODEL
    seen = transport(monkeypatch, replies)
    result = run_agent_loop(MiMoClient('synthetic-only'), model=MODEL, system='Synthetic',
        messages=[{'role': 'user', 'content': 'Synthetic task'}], tools=TOOLS,
        execute_tool=lambda *args: 'Synthetic artifact', max_rounds=3)
    assert result.text == 'Done' and len(seen) == 2
    second = json.loads(seen[1].content)
    assistant = next(m for m in second['messages'] if m['role'] == 'assistant')
    assert assistant['reasoning_content'] == 'private reasoning'
    assert assistant['tool_calls'][0]['id'] == 'read-1'
    assert any(m['role'] == 'tool' and m['tool_call_id'] == 'read-1' for m in second['messages'])
    assert 'private reasoning' not in result.text


def test_image_analysis_sends_base64_pixels_to_the_official_endpoint(monkeypatch):
    data = response('Synthetic observation')
    data['model'] = MODEL
    seen = transport(monkeypatch, [data])
    encoded = base64.b64encode(b'synthetic-pixels').decode()
    MiMoClient('synthetic-only').complete(model=MODEL, system='Synthetic', tools=[], messages=[{
        'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64',
            'media_type': 'image/png', 'data': encoded}}, {'type': 'text', 'text': 'Observe'}]}])
    parts = json.loads(seen[0].content)['messages'][1]['content']
    assert parts[0] == {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + encoded}}
    assert str(seen[0].url) == ENDPOINT


@pytest.mark.parametrize('failure', ['missing_core', 'repetition', 'http_rejection'])
def test_failures_never_execute_tools_or_retry_and_preserve_actual_usage(monkeypatch, failure):
    args = submission()
    data = wire(args)
    data['model'] = MODEL
    if failure == 'missing_core':
        args['structured']['segments'][0].pop('sound')
        data = wire(args)
    elif failure == 'repetition':
        data['choices'][0]['finish_reason'] = 'repetition_truncation'
    else:
        data = httpx.Response(401, text='SYNTHETIC_PRIVATE_HTTP_BODY')
    seen = transport(monkeypatch, [data])
    executed = []
    with pytest.raises(ModelError) as caught:
        run_agent_loop(MiMoClient('synthetic-only'), model=MODEL, system='Synthetic', messages=[],
            tools=tools_for('writer'), execute_tool=lambda *args: executed.append(args), max_rounds=3)
    error = caught.value
    assert len(seen) == 1 and executed == []
    assert 'SYNTHETIC_PRIVATE' not in str(error) + str(error.public_message) + json.dumps(error.response_diagnostic)
    if failure == 'http_rejection':
        assert error.retryable is False and error.usage.input_tokens is None
    else:
        assert error.usage.output_tokens == 13602
        if failure == 'repetition':
            assert error.response_diagnostic['finish_reason'] == 'repetition_truncation'


def test_cancel_before_request_never_sends_http(monkeypatch):
    seen = transport(monkeypatch, [])
    def cancel():
        raise JobCancelled('Synthetic stop')
    with pytest.raises(JobCancelled):
        MiMoClient('synthetic-only', check_cancel=cancel).complete_with_progress(
            model=MODEL, system='Synthetic', messages=[], tools=[])
    assert seen == []


def test_unconfigured_entry_keeps_roles_and_official_parameters(tmp_path):
    connections = ConnectionStore(tmp_path)
    before = connections.public()
    payload = update_payload(connections)
    payload['connections'].append({'id': 'mimo', 'name': 'MiMo', 'provider': 'mimo',
        'base_url': 'https://unapproved.invalid/v1', 'token_limit_parameter': 'max_tokens'})
    saved = connections.save(payload)
    mimo = next(c for c in saved['connections'] if c['id'] == 'mimo')
    assert mimo['base_url'] == 'https://api.xiaomimimo.com/v1'
    assert mimo['token_limit_parameter'] == 'max_completion_tokens'
    assert mimo['has_key'] is False and saved['roles'] == before['roles']
    snapshot = ConnectionSnapshot(json.dumps({'connections': [mimo], 'roles': {
        'writer': {'connection': 'mimo', 'model': MODEL}}}), {}, tmp_path)
    with pytest.raises(ConnectionUnavailable, match='no saved API key'):
        snapshot.client().for_role('writer', 'fixture')


def test_frozen_mimo_routing_uses_its_own_credential_and_cancellation(tmp_path):
    public = {'connections': [{'id': 'mimo', 'provider': 'mimo'}], 'roles': {
        'writer': {'connection': 'mimo', 'model': MODEL}}}
    snapshot = ConnectionSnapshot(json.dumps(public), {'mimo': 'synthetic-mimo', 'ds': 'synthetic-ds'}, tmp_path)
    cancel = lambda: None
    routed = snapshot.client(max_tokens=12000, check_cancel=cancel)
    client = routed.for_role('writer', 'fixture')
    assert isinstance(client, MiMoClient) and client._token == 'synthetic-mimo'
    assert client.max_tokens == 12000 and client.check_cancel is cancel


@pytest.mark.parametrize('limit', [0, True, 131073])
def test_invalid_limits_stop_before_any_provider_invocation(limit):
    with pytest.raises(ValueError, match='MiMo max_completion_tokens'):
        MiMoClient('synthetic-only', max_tokens=limit)


@pytest.mark.parametrize('streaming', [False, True])
def test_general_json_mode_is_first_request_only_and_keeps_normal_role_client(monkeypatch, streaming):
    data = response('{"images":[{"id":"image-1","findings":["Synthetic pixels"],"limitations":["Not verified"]}]}')
    data['model'] = MODEL
    reply = data
    if streaming:
        content = data['choices'][0]['message']['content']
        chunks = [{'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': content[pos:pos + 8]},
                                                'finish_reason': None}]} for pos in range(0, len(content), 8)]
        chunks.append({'model': MODEL, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                       'usage': data['usage']})
        reply = httpx.Response(200, text='\n\n'.join('data: ' + json.dumps(row) for row in chunks)
            + '\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
    seen = transport(monkeypatch, [reply])
    original = MiMoClient('synthetic-only', max_tokens=4096)
    observer = original.with_json_output()
    kwargs = dict(model=MODEL, system='Return only JSON', messages=[{'role': 'user', 'content': 'Synthetic image task'}], tools=[])
    turn = (observer.complete_with_progress(**kwargs) if streaming else observer.complete(**kwargs))
    assert len(seen) == 1 and str(seen[0].url) == ENDPOINT
    payload = json.loads(seen[0].content)
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['stream'] is streaming and payload['max_completion_tokens'] == 4096
    assert payload['thinking'] == {'type': 'enabled'}
    assert json.loads(turn.text)['images'][0]['id'] == 'image-1'
    assert turn.usage.input_tokens == data['usage']['prompt_tokens']
    assert original.json_output is False and observer.json_output is True
    assert 'response_format' not in original._payload(**kwargs)


def test_observer_json_mode_never_becomes_a_writer_tool_or_retries(monkeypatch):
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError) as error:
        MiMoClient('synthetic-only').with_json_output().complete(
            model=MODEL, system='Synthetic', messages=[], tools=TOOLS)
    assert error.value.provider_invoked is False and error.value.retryable is False
    assert seen == []


@pytest.mark.parametrize('streaming', [False, True])
def test_structured_observer_first_request_strict_data_function_isolated_from_roles(monkeypatch, streaming):
    from studio.image_understanding import structured_report_tool, REPORT_FUNCTION
    from test_image_understanding import strict_answer
    data = strict_answer()
    seen = transport(monkeypatch, [stream_reply(data) if streaming else data])
    original = MiMoClient('synthetic-only', max_tokens=4096)
    observer = original.with_structured_observation()
    tool = structured_report_tool('image-1')
    kwargs = dict(model=MODEL, system='Synthetic image data report', messages=[], tools=[tool])
    turn = (observer.complete_with_progress(**kwargs) if streaming else observer.complete(**kwargs))
    payload = json.loads(seen[0].content)
    assert len(seen) == 1 and 'response_format' not in payload
    assert payload['tool_choice'] == 'auto' and payload['tools'][0]['function']['strict'] is True
    assert payload['tools'][0]['function']['parameters'] == tool['input_schema']
    assert turn.stop_reason == 'tool_use' and len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == REPORT_FUNCTION
    assert set(turn.tool_calls[0].input) == {'id', 'findings', 'limitations'}
    assert observer.structured_observation and not observer.json_output
    assert not original.structured_observation and not original.json_output
    role = original._payload(model=MODEL, system='', messages=[], tools=[])
    assert 'tool_choice' not in role and 'response_format' not in role and 'tools' not in role


@pytest.mark.parametrize('tools', [[], TOOLS, tools_for('writer')])
def test_structured_observer_cannot_send_any_studio_execution_schema(monkeypatch, tools):
    seen = transport(monkeypatch, [])
    with pytest.raises(ModelError) as error:
        MiMoClient('synthetic-only').with_structured_observation().complete(
            model=MODEL, system='Synthetic', messages=[], tools=tools)
    assert error.value.provider_invoked is False and not error.value.retryable and not seen
