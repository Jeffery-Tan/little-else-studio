"""Mock transport tests; never calls a provider or uses a real key."""
import json

import httpx
import pytest

from studio.agents.client import AnthropicCompatClient, JobCancelled, ModelError, Turn, Usage, ToolCall, run_agent_loop
from studio.agents.openai_compat import OpenAICompatClient, _messages, _parse


def response(**overrides):
    data = {'model': 'custom-model', 'choices': [{'finish_reason': 'stop', 'message': {'content': 'Done'}}],
            'usage': {'prompt_tokens': 31, 'completion_tokens': 12}}
    return dict(data, **overrides)


def test_tool_call_and_result_round_trip_preserves_studio_tool_boundary():
    data = response(choices=[{'finish_reason': 'tool_calls', 'message': {'content': 'Inspecting', 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_artifact', 'arguments': '{"stage":"intake"}'}}]}}])
    turn = _parse(data, 'custom-model')
    assert turn.tool_calls == [ToolCall('call-1', 'read_artifact', {'stage': 'intake'})]
    wire = _messages('Role scope', [{'role': 'assistant', 'content': turn.raw_blocks},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'Observed only', 'is_error': False}]}])
    assert wire[0] == {'role': 'system', 'content': 'Role scope'}
    assert json.loads(wire[1]['tool_calls'][0]['function']['arguments']) == {'stage': 'intake'}
    assert wire[-1] == {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'Observed only'}
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (31, 12)


@pytest.mark.parametrize('base_url,expected', [('https://fixture.invalid', 'https://fixture.invalid/v1/chat/completions'),
    ('https://fixture.invalid/v1', 'https://fixture.invalid/v1/chat/completions')])
@pytest.mark.parametrize('parameter', ['max_tokens', 'max_completion_tokens'])
def test_exact_endpoint_one_request_no_redirects_or_env_proxy(monkeypatch, base_url, expected, parameter):
    real_client = httpx.AsyncClient
    seen = []
    def handle(request):
        seen.append(request)
        assert str(request.url) == expected
        assert request.headers['Authorization'] == 'Bearer synthetic-only'
        payload = json.loads(request.content)
        assert payload[parameter] == 123
        assert payload['stream'] is False
        assert payload['tools'][0]['function']['parameters'] == {'type': 'object'}
        return httpx.Response(200, json=response())
    def factory(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real_client(**kwargs, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    client = OpenAICompatClient(base_url, 'synthetic-only', max_tokens=123, token_limit_parameter=parameter)
    turn = client.complete(model='custom-model', system='Role', messages=[{'role': 'user', 'content': 'Task'}],
                           tools=[{'name': 'read_artifact', 'input_schema': {'type': 'object'}}])
    assert turn.text == 'Done' and len(seen) == 1


@pytest.mark.parametrize('status', [302, 307, 401, 429, 500])
def test_gateway_errors_do_not_echo_credentials_or_follow_redirect(monkeypatch, status):
    real_client = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://other.invalid'},
                              text='echo synthetic-only prompt-private')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError) as failure:
        OpenAICompatClient('https://fixture.invalid', 'synthetic-only').complete(
            model='custom', system='', messages=[], tools=[])
    assert len(calls) == 1
    assert 'synthetic-only' not in str(failure.value) and 'prompt-private' not in str(failure.value)
    assert failure.value.usage.input_tokens is None


def test_anthropic_versioned_base_rejects_redirect_without_leaking_key(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []
    def handle(request):
        seen.append(request)
        assert str(request.url) == 'https://fixture.invalid/v1/messages'
        assert request.headers['x-api-key'] == 'synthetic-only'
        return httpx.Response(307, headers={'Location': 'https://other.invalid'}, text='echo synthetic-only')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle)))
    with pytest.raises(ModelError) as failure:
        AnthropicCompatClient('https://fixture.invalid/v1', 'synthetic-only').complete(
            model='custom', system='', messages=[], tools=[])
    assert len(seen) == 1 and 'synthetic-only' not in str(failure.value)


def test_malformed_tool_response_retains_known_usage_without_echoing_arguments():
    with pytest.raises(ModelError) as failure:
        _parse(response(choices=[{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'id': 'x', 'type': 'function', 'function': {'name': 'read_artifact', 'arguments': 'private-invalid'}}]}}]), 'custom')
    assert failure.value.usage.input_tokens == 31
    assert failure.value.usage_status == 'known'
    assert 'private-invalid' not in str(failure.value)


def test_missing_usage_remains_unknown_and_invalid_counts_rejected():
    turn = _parse(response(usage={}), 'custom')
    assert turn.usage.input_tokens is None and turn.usage.output_tokens is None
    with pytest.raises(ModelError):
        _parse(response(usage={'prompt_tokens': True}), 'custom')


@pytest.mark.parametrize('finish', ['content_filter', 'insufficient_system_resource', 'aborted'])
def test_documented_provider_interruptions_stop_without_executing_even_valid_tools(finish):
    data = response(choices=[{'finish_reason': finish, 'message': {'content': 'PRIVATE_CONTENT',
        'tool_calls': [{'id': 'one', 'type': 'function', 'function': {
            'name': 'read_artifact', 'arguments': '{"private":"PRIVATE_ARGUMENTS"}'}}]}}])
    with pytest.raises(ModelError) as caught:
        _parse(data, 'custom')
    error = caught.value
    assert error.response_diagnostic['code'] == 'provider_interrupted'
    assert error.response_diagnostic['finish_reason'] == finish
    assert error.usage_status == 'known'
    assert error.usage.input_tokens == 31 and error.usage.output_tokens == 12
    assert 'invalid Chat' not in str(error)
    assert 'PRIVATE' not in str(error) + str(error.response_diagnostic) + error.public_message


@pytest.mark.parametrize('data,field,code', [
    ([], 'response', 'expected_object'),
    (response(usage=['PRIVATE_USAGE']), 'usage', 'expected_object'),
    (response(choices=[]), 'choices', 'expected_single_choice'),
    (response(choices=[None]), 'choice', 'expected_object'),
    (response(choices=[{'finish_reason': ['PRIVATE_REASON']}]), 'finish_reason', 'unsupported_finish_reason'),
    (response(choices=[{'finish_reason': 'PRIVATE_REASON'}]), 'finish_reason', 'unsupported_finish_reason'),
    (response(choices=[{'finish_reason': 'stop', 'message': []}]), 'message', 'expected_object'),
    (response(choices=[{'finish_reason': 'stop', 'message': {'content': False}}]), 'content', 'expected_text'),
    (response(choices=[{'finish_reason': 'stop', 'message': {'tool_calls': {}}}]), 'tool_calls', 'expected_array'),
    (response(choices=[{'finish_reason': 'tool_calls', 'message': {}}]), 'tool_calls', 'finish_reason_mismatch'),
])
def test_invalid_response_identifies_field_without_echoing_untrusted_values(data, field, code):
    with pytest.raises(ModelError) as caught:
        _parse(data, 'custom')
    assert caught.value.response_diagnostic['field'] == field
    assert caught.value.response_diagnostic['code'] == code
    assert 'PRIVATE' not in str(caught.value) + str(caught.value.response_diagnostic)


def test_malformed_second_tool_keeps_json_location_and_prevents_execution_of_first():
    data = response(choices=[{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
        {'id': 'one', 'type': 'function', 'function': {'name': 'read_artifact', 'arguments': '{}'}},
        {'id': 'two', 'type': 'function', 'function': {'name': 'read_artifact', 'arguments': '{\n"private": "PRIVATE_ARGUMENTS"'}},
    ]}}])
    executed = []
    class Fake:
        def complete(self, **_):
            return _parse(data, 'custom')
    with pytest.raises(ModelError) as caught:
        run_agent_loop(Fake(), model='custom', system='', messages=[], tools=[],
                       execute_tool=lambda *args: executed.append(args))
    diagnostic = caught.value.response_diagnostic
    assert diagnostic['field'] == 'tool_arguments' and diagnostic['code'] == 'invalid_arguments_json'
    assert diagnostic['tool_call_index'] == 1
    assert diagnostic['json_line'] == 2 and diagnostic['json_column'] > 0
    assert len(diagnostic['arguments_sha256']) == 64
    assert 'PRIVATE_ARGUMENTS' not in str(diagnostic) + str(caught.value)
    assert caught.value.usage_status == 'known' and executed == []


def test_argument_objects_are_never_guessed_or_repaired_and_length_stops_stay_truncated():
    for arguments, code in [({'stage': 'intake'}, 'expected_json_string'), ('[]', 'expected_object'),
                            ('', 'invalid_arguments_json')]:
        data = response(choices=[{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'id': 'one', 'type': 'function', 'function': {'name': 'read_artifact', 'arguments': arguments}}]}}])
        with pytest.raises(ModelError) as caught:
            _parse(data, 'custom')
        assert caught.value.response_diagnostic['code'] == code
    data['choices'][0]['finish_reason'] = 'length'
    with pytest.raises(ModelError, match='output truncated at max_tokens') as caught:
        _parse(data, 'custom')
    assert caught.value.response_diagnostic['finish_reason'] == 'length'
    assert caught.value.usage_status == 'known'


def test_invalid_usage_retains_other_valid_count_without_claiming_known_usage():
    with pytest.raises(ModelError) as caught:
        _parse(response(usage={'prompt_tokens': True, 'completion_tokens': 12}), 'custom')
    assert caught.value.usage.input_tokens is None
    assert caught.value.usage.output_tokens == 12
    assert caught.value.usage_status == 'partial'


def test_cancel_after_completed_model_prevents_tool_execution_without_erasing_turn():
    from studio.tools.registry import REGISTRY
    calls = []
    cancelled = False
    class Fake:
        def complete(self, **kwargs):
            nonlocal cancelled
            cancelled = True
            return Turn(tool_calls=[ToolCall('one', 'submit_stage', {})], stop_reason='tool_use', usage=Usage(10, 20))
    def check_cancel():
        if cancelled:
            raise JobCancelled('cancelled at boundary')
    with pytest.raises(JobCancelled):
        run_agent_loop(Fake(), model='fixture', system='', messages=[],
                       tools=[REGISTRY['submit_stage'].to_api()],
                       execute_tool=lambda *args: calls.append(args),
                       on_turn=lambda turn: calls.append(('usage', turn.usage.input_tokens)), check_cancel=check_cancel)
    assert calls == [('usage', 10)]
