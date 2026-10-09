"""Safe tool JSON location metadata, using synthetic HTTP and temporary runs."""
import json

import httpx
import pytest

from studio.agents.budget import BudgetedClient
from studio.agents.client import ModelError, Usage, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.mimo_client import MiMoClient
from studio.agents.openai_compat import OpenAICompatClient, _parse, _response_error
from studio.agents.requests import safe_response_diagnostic
from studio.config import Settings
from studio.ledger import Ledger
from studio.store import Store


TOOLS = [{'name': 'read_artifact', 'input_schema': {'type': 'object'}}]
PRIVATE = 'SYNTHETIC_PRIVATE_REASONING_ARGUMENT_CONTENT'
USAGE = {'prompt_tokens': 31, 'completion_tokens': 12}


def response(arguments, name='read_artifact', *, second=False):
    calls = [{'id': 'bad', 'type': 'function',
              'function': {'name': name, 'arguments': arguments}}]
    if second:
        calls.insert(0, {'id': 'first', 'type': 'function',
                         'function': {'name': 'read_artifact', 'arguments': '{}'}})
    return {'model': 'fixture', 'usage': USAGE,
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'content': PRIVATE, 'reasoning_content': PRIVATE, 'tool_calls': calls}}]}


@pytest.mark.parametrize('arguments,category,at_eof', [
    ('', 'expected_value', True),
    ('{', 'expected_property_name', True),
    ('{"x":', 'expected_value', True),
    ('{"产品":"红"', 'expected_comma', True),
    ('{"产品":"红",PRIVATE}', 'expected_property_name', False),
    ('{"x" 1}', 'expected_colon', False),
    ('{} PRIVATE', 'extra_data', False),
    ('{"x":"PRIVATE', 'unterminated_string', False),
    ('{"x":"\\qPRIVATE"}', 'invalid_escape', False),
    ('{"x":"\x01PRIVATE"}', 'invalid_control_character', False),
    ('{"x":"\\uXXXXPRIVATE"}', 'invalid_unicode_escape', False),
    ('\ufeff{}', 'unexpected_bom', False),
])
def test_fixed_json_categories_and_character_byte_locations(arguments, category, at_eof):
    with pytest.raises(ModelError) as caught:
        _parse(response(arguments), 'fixture', tools=TOOLS)
    issue = caught.value.response_diagnostic
    assert issue['json_error_category'] == category
    assert issue['arguments_chars'] == len(arguments)
    assert issue['arguments_bytes'] == len(arguments.encode('utf-8'))
    assert (issue['json_position'] == issue['arguments_chars']) is at_eof
    assert issue['tool_name'] == 'read_artifact'
    assert caught.value.usage_status == 'known'
    assert PRIVATE not in str(caught.value) + str(issue) + caught.value.public_message
    assert 'json_error_msg' not in issue


@pytest.mark.parametrize('message', ["Expecting ',' delimiter " + PRIVATE, {'private': PRIVATE}])
def test_arbitrary_json_error_message_only_maps_to_other_without_echo(message):
    arguments = '{"产品":"' + PRIVATE
    error = json.JSONDecodeError(message, arguments, 3)
    result = _response_error({}, Usage(1, 2), 'tool_arguments', 'invalid_arguments_json',
                             arguments=arguments, json_error=error,
                             tool_name='read_artifact', tools=TOOLS)
    assert result.response_diagnostic['json_error_category'] == 'other_json_error'
    assert result.response_diagnostic['arguments_chars'] == len(arguments)
    assert result.response_diagnostic['arguments_bytes'] > len(arguments)
    assert PRIVATE not in str(result) + str(result.response_diagnostic) + result.public_message


@pytest.mark.parametrize('name', ['list_materials', PRIVATE, 'read_artifact\nPRIVATE'])
def test_undeclared_or_malicious_tool_name_never_leaves_parser(name):
    with pytest.raises(ModelError) as caught:
        _parse(response('{', name), 'fixture', tools=TOOLS)
    assert 'tool_name' not in caught.value.response_diagnostic
    assert name not in str(caught.value) + str(caught.value.response_diagnostic)


def test_diagnostic_whitelist_requires_this_request_declarations_and_fixed_values():
    issue = {'tool_name': 'read_artifact', 'arguments_chars': 8,
             'json_error_category': 'expected_comma', 'json_error_msg': PRIVATE}
    assert safe_response_diagnostic(issue, tools=TOOLS) == {
        'tool_name': 'read_artifact', 'arguments_chars': 8, 'json_error_category': 'expected_comma'}
    assert 'tool_name' not in safe_response_diagnostic(issue)
    assert 'tool_name' not in safe_response_diagnostic(issue, tools=[{'name': 'list_materials'}])
    assert safe_response_diagnostic({'arguments_chars': True, 'json_error_category': PRIVATE}) == {}
    assert safe_response_diagnostic({'arguments_chars': -1, 'tool_name': PRIVATE}) == {}


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def sse_chunks(arguments, name):
    def chunk(delta, finish=None):
        return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta,
            'finish_reason': finish}]}, ensure_ascii=False) + '\n\n').encode('utf-8')
    middle = len(arguments) // 2
    return [chunk({'content': PRIVATE, 'reasoning_content': PRIVATE, 'tool_calls': [
                {'index': 0, 'id': 'first', 'type': 'function',
                 'function': {'name': 'read_artifact', 'arguments': '{}'}},
                {'index': 1, 'id': 'bad', 'type': 'function',
                 'function': {'name': name, 'arguments': arguments[:middle]}}]}),
            chunk({'tool_calls': [{'index': 1, 'function': {'arguments': arguments[middle:]}}]}),
            chunk({}, 'tool_calls'),
            ('data: ' + json.dumps({'choices': [], 'usage': USAGE}) + '\n\n').encode(),
            b'data: [DONE]\n\n']


@pytest.mark.parametrize('provider,streaming', [
    ('openai', False), ('deepseek', False), ('deepseek', True), ('mimo', False), ('mimo', True)])
@pytest.mark.parametrize('name', ['read_artifact', 'list_materials', PRIVATE])
def test_complete_http_bad_second_tool_records_exact_call_usage_and_executes_nothing(
        tmp_path, monkeypatch, provider, streaming, name):
    store = Store(tmp_path)
    run_id = 'json-diagnostics'
    store.create_run(run_id, 'Offline JSON diagnostic fixture', 'Synthetic only')
    ledger = Ledger(store, Settings(tmp_path, 'https://example.invalid'))
    arguments = '{"产品":"红"'
    chunks = (sse_chunks(arguments, name) if streaming else
              [json.dumps(response(arguments, name, second=True), ensure_ascii=False).encode('utf-8')])
    body = Body(chunks)
    seen, executed = [], []
    real_client = httpx.AsyncClient

    def handle(request):
        seen.append(request)
        wire = json.loads(request.content)
        assert wire['tools'][0]['function']['name'] == 'read_artifact'
        return httpx.Response(200, stream=body, headers={
            'content-type': 'text/event-stream' if streaming else 'application/json'})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw:
                        real_client(**kw, transport=httpx.MockTransport(handle)))
    if provider == 'openai':
        client = OpenAICompatClient('https://fixture.invalid', 'synthetic-only')
    elif provider == 'deepseek':
        client = DeepSeekClient('synthetic-only', progress_stream=streaming)
    else:
        client = MiMoClient('synthetic-only')
        if not streaming:
            client.complete_with_progress = None
    budget = BudgetedClient(client, ledger, run_id=run_id, role='orchestrator', stage='intake')
    with pytest.raises(ModelError) as caught:
        run_agent_loop(budget, model='fixture', system='', messages=[], tools=TOOLS,
                       execute_tool=lambda *args: executed.append(args))

    events = store.read_events(run_id)
    rejection = next(e for e in events if e['kind'] == 'agent_response_rejected')
    transport = next(e for e in events if e['kind'] == 'agent_model_transport')
    snapshot = next(e for e in events if e['kind'] == 'agent_request_snapshot')
    entry = ledger.entries(run_id)[0]
    issue = rejection['diagnostic']
    assert rejection['call_id'] == transport['call_id'] == snapshot['call_id'] == entry.call_id
    assert len(seen) == len(ledger.entries(run_id)) == 1 and body.closed and executed == []
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status, entry.ok) == (31, 12, 'known', False)
    assert issue['tool_call_index'] == 1 and issue['finish_reason'] == 'tool_calls'
    assert issue['code'] == 'invalid_arguments_json' and issue['json_error_category'] == 'expected_comma'
    assert issue['arguments_chars'] == issue['json_position'] == len(arguments)
    assert issue['arguments_bytes'] > issue['arguments_chars']
    for diagnostic in (issue, transport['diagnostic'], caught.value.response_diagnostic):
        assert diagnostic.get('tool_name') == ('read_artifact' if name == 'read_artifact' else None)
        assert diagnostic['arguments_chars'] == len(arguments)
        assert diagnostic['json_error_category'] == 'expected_comma'
    assert PRIVATE not in json.dumps(events) + str(caught.value)
    assert 'synthetic-only' not in json.dumps(events)
