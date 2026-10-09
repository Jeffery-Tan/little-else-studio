"""Every role checks a whole returned batch before a test handler can run."""
import json

import pytest

from studio.agents.client import ModelError, ToolCall, Turn, Usage, _parse_stream, run_agent_loop
from studio.tools.registry import tools_for


def anthropic_turn(calls):
    events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 12}}}]
    for index, (identity, name, arguments) in enumerate(calls):
        events += [
            {'type': 'content_block_start', 'index': index, 'content_block': {
                'type': 'tool_use', 'id': identity, 'name': name, 'input': arguments}},
            {'type': 'content_block_stop', 'index': index},
        ]
    events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
                'usage': {'output_tokens': 18}}, {'type': 'message_stop'}]
    return _parse_stream(['data: ' + json.dumps(event) for event in events], 'synthetic')


@pytest.mark.parametrize('dynamic', [False, True])
@pytest.mark.parametrize('bad', ['duplicate', 'empty', 'nonstring', 'unavailable', 'not_object'])
def test_invalid_second_call_stops_whole_batch_before_handlers(dynamic, bad):
    identity, name, arguments = 'second', 'list_materials', {}
    if bad == 'duplicate':
        identity = 'first'
    elif bad == 'empty':
        identity = ''
    elif bad == 'nonstring':
        identity = 123
    elif bad == 'unavailable':
        name = 'dispatch_stage'
    elif bad == 'not_object':
        arguments = []
    if bad == 'not_object':
        # Exercise the shared boundary independently of a provider's decoder.
        turn = Turn(tool_calls=[ToolCall('first', 'read_capabilities', {}),
                                ToolCall(identity, name, arguments)], usage=Usage(12, 18))
    else:
        turn = anthropic_turn([('first', 'read_capabilities', {}), (identity, name, arguments)])
    executed = []

    class Client:
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return turn

    client = Client()
    options = {'tools_for_request': lambda: tools_for('writer')} if dynamic else {}
    with pytest.raises(ModelError) as caught:
        run_agent_loop(client, model='synthetic', system='fixture', messages=[],
            tools=tools_for('writer'), max_rounds=1,
            execute_tool=lambda tool, args: executed.append((tool, args)) or 'synthetic', **options)
    assert executed == [] and client.calls == 1
    assert caught.value.usage_status == 'known' and caught.value.retryable is False
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (12, 18)
    assert caught.value.response_diagnostic['code'] == (
        'invalid_arguments_schema' if bad == 'unavailable' else 'invalid_identity')
