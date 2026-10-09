"""Real provider parsers -> budget -> Dispatcher, using isolated mock transports."""
import copy
import json
from dataclasses import replace

import httpx
import pytest

from studio.agent_sessions import AgentSessions
from studio.agents import codex_client as codex
from studio.agents.client import (
    AnthropicCompatClient, NonRetryableStageError, Turn, ToolCall, Usage,
    prepare_strict_writer_execution, run_agent_loop,
)
from studio.agents.openai_compat import OpenAICompatClient
from studio.conversation_input import receive, accept, start_execution
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import tools_for
from test_codex_client import FakeSession, finished
from test_writer_strict_role_tools import interpretation, constraint, QUOTE
from test_writing_tool_schema import seed, submission


def model_client(monkeypatch, provider, root, run_id, batches):
    """Only transports are mocked; each provider's complete/parser is real."""
    seen = []
    if provider == 'codex':
        class Session(FakeSession):
            instances = []
            thread_change, metadata, denied_request = {}, None, False
            def __init__(self, *args, **kwargs):
                type(self).events = finished(calls=[{
                    'id': f'call-{i}', 'name': name, 'arguments_json': json.dumps(args)}
                    for i, (name, args) in enumerate(batches.pop(0))])
                super().__init__(*args, **kwargs)
                seen.append(self)
        monkeypatch.setattr(codex, '_StdioSession', Session)
        client = codex.CodexClient(run_id=run_id, role='writer', data_root=root)
        client._mcp_names = ()
        monkeypatch.setattr(client, 'preflight', lambda: None)
        return client, seen

    original = httpx.AsyncClient
    def handle(request):
        seen.append(json.loads(request.content))
        calls = batches.pop(0)
        if provider == 'openai':
            return httpx.Response(200, json={
                'model': 'synthetic-model', 'usage': {'prompt_tokens': 31, 'completion_tokens': 17},
                'choices': [{'finish_reason': 'tool_calls', 'message': {'content': '', 'tool_calls': [
                    {'id': f'call-{i}', 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(args)}}
                    for i, (name, args) in enumerate(calls)]}}]})
        events = [{'type': 'message_start', 'message': {
            'model': 'synthetic-model', 'usage': {'input_tokens': 31}}}]
        for i, (name, args) in enumerate(calls):
            events += [
                {'type': 'content_block_start', 'index': i, 'content_block': {
                    'type': 'tool_use', 'id': f'call-{i}', 'name': name, 'input': {}}},
                {'type': 'content_block_delta', 'index': i, 'delta': {
                    'type': 'input_json_delta', 'partial_json': json.dumps(args)}},
                {'type': 'content_block_stop', 'index': i},
            ]
        events += [{'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
                    'usage': {'output_tokens': 17}}, {'type': 'message_stop'}]
        return httpx.Response(200, text='\n\n'.join('data: ' + json.dumps(e) for e in events) + '\n\n',
                              headers={'content-type': 'text/event-stream'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(**kw, transport=httpx.MockTransport(handle)))
    cls = OpenAICompatClient if provider == 'openai' else AnthropicCompatClient
    return cls('https://synthetic.invalid/v1', 'synthetic-only'), seen


def dispatcher_scene(tmp_path, monkeypatch, provider, batches):
    store, pipeline, settings, ledger, rid = seed(tmp_path, monkeypatch)
    receipt = receive(store, rid, QUOTE, 'writer-real-input')
    accept(store, rid, receipt)
    start_execution(store, rid, receipt)
    client, seen = model_client(monkeypatch, provider, tmp_path, rid, batches)
    events = []
    dispatcher = Dispatcher(store, pipeline, settings, client, ledger, rid, on_event=events.append)
    return dispatcher, receipt, store, pipeline, ledger, rid, seen, events


@pytest.mark.parametrize('provider', ['openai', 'codex', 'anthropic'])
def test_real_parser_dispatcher_restores_sentinels_and_preserves_original_replay(tmp_path, monkeypatch, provider):
    proposal = interpretation()
    # The seeded project has no prior writing: this round creates writing-v1.
    proposal['task']['mode'] = 'produce'
    history = {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 12000}
    d, receipt, store, pipeline, ledger, rid, seen, events = dispatcher_scene(
        tmp_path, monkeypatch, provider,
        [[('read_role_history', history), ('propose_task_interpretation', proposal)],
         [('submit_stage', submission())]])
    result = d.dispatch('writing', QUOTE, user_input_id=receipt['message_id'])
    assert result.submitted == 'writing-v1' and len(seen) == 2
    tools = [e for e in events if e['type'] == 'agent_tool']
    assert [(e['tool'], e['ok']) for e in tools] == [
        ('read_role_history', True), ('propose_task_interpretation', True), ('submit_stage', True)]
    assert tools[0]['args'] == {'offset': 0, 'limit': 12000}
    assert set(tools[1]['args']) == {'task', 'constraints'}
    journal = AgentSessions(store, rid).history(result.session_id)
    response = next(e for e in journal if e['kind'] == 'model_response')
    blocks = [b for b in response['blocks'] if b['type'] == 'tool_use']
    assert blocks[0]['input'] == history and blocks[1]['input'] == proposal
    # Verify the actual second provider request also contains the original wire.
    if provider == 'codex':
        payload = next(p for method, p in seen[1].sent if method == 'turn/start')
        messages = json.loads(payload['input'][0]['text'])['messages']
    else:
        messages = seen[1]['messages']
    replay = next(m for m in messages if m['role'] == 'assistant')
    if provider == 'openai':
        assert json.loads(replay['tool_calls'][0]['function']['arguments']) == history
        assert json.loads(replay['tool_calls'][1]['function']['arguments']) == proposal
    else:
        blocks = [b for b in replay['content'] if b['type'] == 'tool_use']
        assert blocks[0]['input'] == history and blocks[1]['input'] == proposal
    assert len(ledger.entries(rid)) == 2 and all(e.usage_status == 'known' for e in ledger.entries(rid))
    assert pipeline.stage_state(rid, 'writing').open_attempt is None


@pytest.mark.parametrize('provider', ['openai', 'codex', 'anthropic'])
@pytest.mark.parametrize('bad', ['boolean_cursor', 'extra_field', 'missing_optional', 'partial_object'])
def test_invalid_wire_stops_whole_batch_with_original_usage_and_safe_diagnostic(
        tmp_path, monkeypatch, provider, bad):
    name, args = 'propose_task_interpretation', interpretation()
    if bad == 'boolean_cursor':
        name, args = 'read_role_history', {'entry_seq': False, 'before_seq': 0, 'offset': 0, 'limit': 100}
    elif bad == 'extra_field':
        args['PRIVATE_FIELD'] = 'PRIVATE_VALUE'
    elif bad == 'missing_optional':
        args.pop('video_use')
    else:
        args['video_use'] = {'source': 'PRIVATE_VALUE'}
    d, receipt, store, pipeline, ledger, rid, seen, events = dispatcher_scene(
        tmp_path, monkeypatch, provider, [[('list_materials', {}), (name, args)]])
    with pytest.raises(NonRetryableStageError):
        d.dispatch('writing', QUOTE, user_input_id=receipt['message_id'])
    assert len(seen) == 1 and not [e for e in events if e['type'] == 'agent_tool']
    assert store.latest_artifact(rid, 'writing') is None
    assert pipeline.stage_state(rid, 'writing').open_attempt is None
    rows = ledger.entries(rid)
    assert len(rows) == 1 and rows[0].usage_status == 'known'
    rejected = next(e for e in store.read_events(rid) if e['kind'] == 'agent_response_rejected')
    assert rejected['call_id'] == rows[0].call_id
    assert rejected['diagnostic']['code'] == 'invalid_arguments_schema'
    assert rejected['diagnostic']['tool_call_index'] == 1
    assert 'PRIVATE' not in json.dumps(rejected['diagnostic'])


@pytest.mark.parametrize('provider', ['openai', 'codex', 'anthropic'])
@pytest.mark.parametrize('guard', ['false_quote', 'authorization', 'invented_reuse'])
def test_decoded_wire_still_cannot_bypass_task_quotes_permissions_or_version_binding(
        tmp_path, monkeypatch, provider, guard):
    proposal = interpretation()
    if guard == 'false_quote':
        proposal['task']['quote'] = 'PRIVATE_INVENTED_REQUEST'
    elif guard == 'authorization':
        proposal['constraints'] = [{**constraint(), 'key': 'authorization.generate'}]
    else:
        proposal['reuse_artifact'] = {'stage': 'writing', 'version': 999, 'sha256': 'f' * 64, 'quote': QUOTE}
    d, receipt, store, _, _, rid, seen, events = dispatcher_scene(
        tmp_path, monkeypatch, provider, [[('propose_task_interpretation', proposal)]])
    d.settings = replace(d.settings, max_tool_rounds=1)
    approvals = store.list_approvals(rid)
    result = d.dispatch('writing', QUOTE, user_input_id=receipt['message_id'])
    assert result.submitted is None and len(seen) == 1
    assert [(e['tool'], e['ok']) for e in events if e['type'] == 'agent_tool'] == [
        ('propose_task_interpretation', False)]
    assert not [e for e in store.read_events(rid) if e['kind'] == 'user_task_interpretation_recorded']
    assert store.list_approvals(rid) == approvals and store.latest_artifact(rid, 'writing') is None


@pytest.mark.parametrize('mismatch', ['schema', 'not_strict'])
def test_exact_schema_match_and_direct_loop_boundary_do_not_mutate_raw_parameters(mismatch):
    args = {'entry_seq': 0, 'before_seq': 0, 'offset': 0, 'limit': 100}
    block = {'type': 'tool_use', 'id': 'one', 'name': 'read_role_history', 'input': copy.deepcopy(args)}
    turn = Turn(tool_calls=[ToolCall('one', 'read_role_history', block['input'])], raw_blocks=[block], usage=Usage(1, 1))
    changed = copy.deepcopy(tools_for('writer'))
    tool = next(t for t in changed if t['name'] == 'read_role_history')
    if mismatch == 'schema':
        tool['input_schema']['description'] = 'Changed contract'
    else:
        tool.pop('strict')
    assert prepare_strict_writer_execution(turn, changed).tool_calls[0].input == args
    assert block['input'] == args
    executed = []
    class Client:
        def complete(self, **kwargs):
            return turn
    run_agent_loop(Client(), model='synthetic-model', system='', messages=[], tools=tools_for('writer'),
                   execute_tool=lambda name, value: executed.append(value) or 'Read only', max_rounds=1)
    assert executed == [{'offset': 0, 'limit': 100}] and block['input'] == args
    assert executed[0] is not block['input']
