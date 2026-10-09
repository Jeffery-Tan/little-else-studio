"""Logical input durability and failure-before-spend at the model boundary."""
import json
import hashlib

import pytest

from fakes import final_turn
from studio.agents.budget import BudgetedClient
from studio.agents.client import JobCancelled, ModelError, SafePreflightError
from studio.agents.requests import read_request, encode
from studio.config import Settings
from studio.ledger import Ledger
from studio.store import Store

RUN = "request-fixture"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Offline request fixture", "Synthetic only")
    return store, Ledger(store, Settings(tmp_path, "https://example.invalid"))


def call(env, inner, messages=None):
    return BudgetedClient(inner, env[1], run_id=RUN, role="director", stage="direction").complete(
        model="fixture", system="rules", messages=messages or [],
        tools=[{"name": "read_artifact", "input_schema": {"type": "object"}}])


def saved(env):
    events = [e for e in env[0].read_events(RUN) if e["kind"] == "agent_request_snapshot"]
    return events, [read_request(env[0], RUN, e["call_id"]) for e in events]


def test_every_request_saved_before_invocation_with_detached_full_content_and_no_credentials(env):
    original = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "prior",
                 "content": "complete tool result " * 2000, "is_error": False}]}]
    before = json.loads(json.dumps(original))
    class Fake:
        api_key = "SYNTHETIC_SECRET_NEVER_SERIALIZE"
        def complete(self, **kwargs):
            events, records = saved(env)
            assert records[-1]["request"] == kwargs
            assert records[-1]["request"]["messages"] == before
            original[0]["content"][0]["content"] = "caller changed"
            assert kwargs["messages"] == before
            return final_turn("done")
    call(env, Fake(), original)
    call(env, Fake(), before)
    events, records = saved(env)
    assert len(events) == len(env[1].entries(RUN)) == 2
    assert records[0]["request"]["messages"] == before
    assert "complete tool result" not in json.dumps(events)
    assert Fake.api_key not in json.dumps(records)
    assert records[0]["role"] == "director" and records[0]["stage"] == "direction"
    assert records[0]['schema_version'] == 1 and 'capture' not in records[0]


@pytest.mark.parametrize('private_block', [
    {'type': 'deepseek_reasoning', 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'},
    {'type': 'mimo_reasoning', 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'},
    {'type': 'thinking', 'thinking': 'PRIVATE_REASONING_FIXTURE', 'signature': 'PRIVATE_SIGNATURE'},
    {'type': 'redacted_thinking', 'data': 'PRIVATE_REASONING_FIXTURE'},
])
def test_private_protocol_is_live_only_and_snapshot_is_marked_diagnostic_copy(env, private_block):
    messages = [{'role': 'assistant', 'content': [private_block,
        {'type': 'text', 'text': 'Public answer'},
        {'type': 'tool_use', 'id': 'public-call', 'name': 'read_artifact',
         'input': {'reasoning_content': 'public tool argument'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'public-call',
                                     'content': 'public result'}]}]
    original = json.loads(json.dumps(messages))
    received = []
    class Fake:
        def complete(self, **kwargs):
            received.append(kwargs)
            record = saved(env)[1][-1]
            assert record['request']['messages'][0]['content'] == original[0]['content'][1:]
            assert record['request']['messages'][1] == original[1]
            assert record['schema_version'] == 2
            assert record['capture'] == {
                'mode': 'diagnostic_copy', 'private_protocol_omitted': True,
                'full_protocol_replay_available': False,
                'original_input_sha256': hashlib.sha256(encode(kwargs).encode('utf-8')).hexdigest()}
            assert 'PRIVATE_' not in json.dumps(record) + json.dumps(env[0].read_events(RUN))
            return final_turn('done')
    call(env, Fake(), messages)
    assert received[0]['messages'] == messages == original
    assert private_block in received[0]['messages'][0]['content']


@pytest.mark.parametrize('role', ['assistant', 'user'])
def test_message_level_reasoning_is_omitted_without_censoring_logical_tool_data(env, role):
    messages = [{'role': role, 'content': 'The words thinking and reasoning_content are public text.',
                 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'public-call',
            'content': {'reasoning_content': 'public result field', 'type': 'thinking'}}]}]
    before = json.loads(json.dumps(messages))
    class Fake:
        def complete(self, **kwargs):
            assert kwargs['messages'] == before
            return final_turn('done')
    call(env, Fake(), messages)
    record = saved(env)[1][0]
    assert messages == before
    assert 'reasoning_content' not in record['request']['messages'][0]
    assert record['request']['messages'][1] == before[1]
    assert record['capture']['full_protocol_replay_available'] is False
    assert 'PRIVATE_REASONING_FIXTURE' not in json.dumps(record)


def test_marked_diagnostic_copy_does_not_store_exceptional_original_arguments(env):
    messages = [{'role': 'assistant', 'content': [{'type': 'tool_use',
        'id': 'processed-call', 'name': 'submit_stage', 'arguments_origin': 'writer_root_close_v1',
        'raw_arguments': 'PRIVATE_ORIGINAL_ARGUMENTS', 'execution_input': {'content': 'Public copy'}}]}]
    original = json.loads(json.dumps(messages))
    class Fake:
        def complete(self, **kwargs):
            assert kwargs['messages'] == original
            return final_turn('done')
    call(env, Fake(), messages)
    record = saved(env)[1][0]
    assert messages == original
    assert 'raw_arguments' not in record['request']['messages'][0]['content'][0]
    assert record['request']['messages'][0]['content'][0]['execution_input'] == {'content': 'Public copy'}
    assert record['capture']['full_protocol_replay_available'] is False
    assert 'PRIVATE_ORIGINAL_ARGUMENTS' not in json.dumps(record)


@pytest.mark.parametrize('provider', ['deepseek', 'mimo', 'anthropic'])
def test_budgeted_actual_adapter_keeps_live_private_protocol_out_of_saved_input(env, monkeypatch, provider):
    import httpx
    from studio.agents.client import AnthropicCompatClient
    from studio.agents.deepseek_client import DeepSeekClient
    from studio.agents.mimo_client import MiMoClient
    from fakes import text_stream
    from test_deepseek_client import response, transport
    private = {'type': 'thinking', 'thinking': 'PRIVATE_REASONING_FIXTURE', 'signature': 'PRIVATE_SIGNATURE'}
    if provider != 'anthropic':
        private = {'type': provider + '_reasoning', 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'}
    messages = [{'role': 'assistant', 'content': [private,
        {'type': 'tool_use', 'id': 'public-call', 'name': 'read_artifact', 'input': {'stage': 'intake'}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'public-call',
                                     'content': 'public result'}]}]
    original = json.loads(json.dumps(messages))
    reply = (httpx.Response(200, text='\n\n'.join(text_stream('Done')) + '\n\n')
             if provider == 'anthropic' else response(reasoning='SYNTHETIC_NEW_RESPONSE'))
    seen = transport(monkeypatch, [reply])
    inner = {'deepseek': lambda: DeepSeekClient('synthetic-only'),
             'mimo': lambda: MiMoClient('synthetic-only'),
             'anthropic': lambda: AnthropicCompatClient('https://example.invalid', 'synthetic-only')}[provider]()
    call(env, inner, messages)
    assert len(seen) == 1 and messages == original
    wire = json.loads(seen[0].content)
    assistant = next(m for m in wire['messages'] if m['role'] == 'assistant')
    if provider == 'anthropic':
        assert assistant['content'] == original[0]['content']
    else:
        assert assistant['reasoning_content'] == private['reasoning_content']
    record = saved(env)[1][0]
    assert 'PRIVATE_' not in json.dumps(record) + json.dumps(env[0].read_events(RUN))
    assert record['capture']['full_protocol_replay_available'] is False
    # No runtime path consumes read_request to replay a model. The thinking
    # adapters also reject this diagnostic copy before any second HTTP request.
    if provider != 'anthropic':
        with pytest.raises(ModelError) as error:
            inner.complete(**record['request'])
        assert error.value.provider_invoked is False and len(seen) == 1


def test_historical_schema_one_private_snapshot_is_read_only_and_not_rewritten(env):
    call_id = '00000000-0000-0000-0000-000000000001'
    record = {'schema_version': 1, 'run_id': RUN, 'call_id': call_id,
              'role': 'director', 'stage': 'direction', 'route': {},
              'request': {'model': 'fixture', 'system': '', 'tools': [],
                  'messages': [{'role': 'assistant', 'content': [
                      {'type': 'deepseek_reasoning', 'reasoning_content': 'PRIVATE_LEGACY_FIXTURE'}]}]}}
    raw = encode(record).encode('utf-8')
    path = env[0].run_dir(RUN) / 'requests' / (call_id + '.json')
    path.parent.mkdir()
    path.write_bytes(raw)
    env[0].append_event(RUN, 'agent_request_snapshot', call_id=call_id,
        role='director', stage='direction', sha256=hashlib.sha256(raw).hexdigest(),
        evidence_ref=f'requests/{call_id}.json')
    before_events = env[0].read_events(RUN)
    assert read_request(env[0], RUN, call_id) == record
    assert path.read_bytes() == raw and env[0].read_events(RUN) == before_events


def test_private_projection_failure_is_before_provider_and_settles_known_zero(env, monkeypatch):
    import studio.agents.requests as requests
    def fail(*args):
        raise OSError('SYNTHETIC_PRIVATE_DETAIL')
    monkeypatch.setattr(requests, 'persistable_messages', fail)
    class Fake:
        def complete(self, **kwargs):
            pytest.fail('private input must not be sent without a durable diagnostic capture')
    with pytest.raises(SafePreflightError) as error:
        call(env, Fake(), [{'role': 'assistant', 'content': [
            {'type': 'deepseek_reasoning', 'reasoning_content': 'PRIVATE_REASONING_FIXTURE'}]}])
    entry = env[1].entries(RUN)[0]
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status, entry.ok) == (0, 0, 'known', False)
    assert saved(env)[0] == []
    assert 'PRIVATE' not in str(error.value)


def test_provider_failure_preserves_input_and_unknown_usage(env):
    class Fake:
        def complete(self, **kwargs):
            raise ModelError(0, "sent then disconnected", provider_invoked=True)
    with pytest.raises(ModelError):
        call(env, Fake())
    assert len(saved(env)[1]) == 1
    assert env[1].entries(RUN)[0].usage_status == "unknown"


def test_snapshot_failure_prevents_invocation_and_settles_zero(env, monkeypatch):
    import studio.agents.budget as budget
    class Fake:
        def complete(self, **kwargs):
            pytest.fail("must not call provider without durable request")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(budget, "save_request", fail)
    with pytest.raises(SafePreflightError):
        call(env, Fake())
    entry = env[1].entries(RUN)[0]
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status, entry.ok) == (0, 0, "known", False)


def test_unserializable_input_fails_before_intent(env):
    with pytest.raises(SafePreflightError):
        call(env, object(), [{"role": "user", "content": object()}])
    assert env[1].entries(RUN) == []
    assert saved(env)[0] == []


def test_cancellation_during_snapshot_prevents_provider_invocation(env):
    class Fake:
        def check_cancel(self):
            if saved(env)[0]:
                raise JobCancelled("cancelled while recording")
        def complete(self, **kwargs):
            pytest.fail("cancelled before provider")
    with pytest.raises(JobCancelled):
        call(env, Fake())
    entry = env[1].entries(RUN)[0]
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status) == (0, 0, "known")


def test_diagnostic_reader_rejects_tampering_and_path_traversal(env):
    class Fake:
        def complete(self, **kwargs):
            return final_turn("done")
    call(env, Fake())
    event = saved(env)[0][0]
    path = env[0].run_dir(RUN) / event["evidence_ref"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        read_request(env[0], RUN, event["call_id"])
    with pytest.raises(ValueError, match="identity"):
        read_request(env[0], RUN, "../../another-run")


def test_response_rejection_records_exact_call_and_safe_field_after_settlement(env):
    from studio.agents.openai_compat import _parse
    invoked = []
    class Fake:
        def complete(self, **kwargs):
            invoked.append(kwargs)
            return _parse({'usage': {'prompt_tokens': 7, 'completion_tokens': 9},
                'choices': [{'finish_reason': 'tool_calls', 'message': {
                    'content': 'PRIVATE_CONTENT', 'reasoning_content': 'PRIVATE_REASONING',
                    'tool_calls': [{'id': 'one', 'type': 'function', 'function': {
                        'name': 'read_artifact', 'arguments': '{"x":"PRIVATE_ARGUMENTS"'}}]}}]}, 'fixture')
    with pytest.raises(ModelError):
        call(env, Fake())
    assert len(invoked) == 1
    row = env[1].entries(RUN)[0]
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (7, 9, 'known', False)
    event = next(e for e in env[0].read_events(RUN) if e['kind'] == 'agent_response_rejected')
    assert event['call_id'] == row.call_id == saved(env)[0][0]['call_id']
    assert event['role'] == 'director' and event['stage'] == 'direction'
    assert event['diagnostic']['field'] == 'tool_arguments'
    assert event['diagnostic']['json_position'] > 0
    assert 'PRIVATE' not in json.dumps(event) + row.error
    assert not (env[0].run_dir(RUN) / 'responses').exists()


def test_http_rejection_diagnostic_and_unknown_usage_keep_original_call_identity(env, monkeypatch):
    import httpx
    from studio.agents.deepseek_client import DeepSeekClient
    from test_deepseek_client import transport
    raw = {'error': {'type': 'invalid_request_error', 'code': 'invalid_json',
                    'message': 'SYNTHETIC_PRIVATE_BODY', 'param': 'tools[0].function.parameters'}}
    seen = transport(monkeypatch, [httpx.Response(400, json=raw)])
    with pytest.raises(ModelError):
        call(env, DeepSeekClient('synthetic-only'))
    row = env[1].entries(RUN)[0]
    assert row.usage_status == 'unknown' and row.input_tokens is None and row.output_tokens is None
    assert row.ok is False and len(seen) == 1
    event = next(e for e in env[0].read_events(RUN) if e['kind'] == 'agent_response_rejected')
    assert event['call_id'] == row.call_id == saved(env)[0][0]['call_id']
    assert event['diagnostic']['http_failure'] == 'request_rejected'
    assert event['diagnostic']['provider_status'] == 400
    assert event['diagnostic']['request_path'] == ['tools', 0, 'function', 'parameters']
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(event) + row.error
    assert not (env[0].run_dir(RUN) / 'responses').exists()


def test_response_diagnostic_whitelist_drops_untrusted_labels_and_extra_content(env):
    from studio.agents.requests import record_response_rejection
    class Fake:
        def complete(self, **kwargs):
            return final_turn('done')
    call(env, Fake())
    call_id = saved(env)[0][0]['call_id']
    record_response_rejection(env[0], RUN, call_id, role='director', stage='direction', diagnostic={
        'format': 'chat_completion', 'field': 'tool_arguments', 'code': 'PRIVATE_REASON',
        'finish_reason': 'PRIVATE_FINISH', 'body': 'PRIVATE_CONTENT', 'arguments': 'PRIVATE_ARGUMENTS',
        'json_position': True, 'json_column': -1, 'tool_call_index': 0,
        'arguments_sha256': 'PRIVATE_DIGEST',
    })
    event = next(e for e in env[0].read_events(RUN) if e['kind'] == 'agent_response_rejected')
    assert event['diagnostic'] == {'format': 'chat_completion', 'field': 'tool_arguments', 'tool_call_index': 0}
    assert 'PRIVATE' not in json.dumps(event)


def test_diagnostic_write_failure_preserves_original_error_and_durable_usage(env, monkeypatch):
    import studio.agents.budget as budget
    from studio.agents.openai_compat import _parse
    def fail(*args, **kwargs):
        raise OSError('SYNTHETIC_PRIVATE_DISK_DETAIL')
    monkeypatch.setattr(budget, 'record_response_rejection', fail)
    class Fake:
        def complete(self, **kwargs):
            return _parse({'usage': {'prompt_tokens': 7, 'completion_tokens': 9}, 'choices': []}, 'fixture')
    with pytest.raises(ModelError) as caught:
        call(env, Fake())
    assert caught.value.response_diagnostic['field'] == 'choices'
    assert 'could not be saved' in caught.value.__notes__[0]
    assert 'PRIVATE' not in str(caught.value) + str(caught.value.__notes__)
    assert env[1].entries(RUN)[0].usage_status == 'known'


@pytest.mark.parametrize('path', [['structured', 'segments', 0, 'PRIVATE_FIELD'],
                                ['PRIVATE_FIELD'], ['structured', True], ['structured', -1]])
def test_schema_diagnostic_path_rejects_untrusted_names_and_indices(path):
    from studio.agents.requests import safe_response_diagnostic
    from studio.agents.openai_compat import _response_error
    from studio.agents.client import Usage
    issue = {'schema_path': path, 'schema_rule': 'extra_field',
             'expected_type': 'object', 'actual_type': 'PRIVATE_TYPE', 'raw_value': 'PRIVATE_VALUE'}
    safe = safe_response_diagnostic(issue)
    assert safe == {'schema_rule': 'extra_field', 'expected_type': 'object'}
    error = _response_error({}, Usage(1, 2), 'tool_arguments', 'invalid_arguments_schema', schema_issue=issue)
    assert 'PRIVATE' not in error.public_message + json.dumps(error.response_diagnostic)


@pytest.mark.parametrize('path', [['structured', 'PRIVATE_FIELD'], ['structured', True], ['structured', -1]])
def test_normalization_diagnostic_does_not_echo_untrusted_paths(path):
    from studio.agents.requests import safe_response_diagnostic
    safe = safe_response_diagnostic({'code': 'empty_defaults_applied',
        'defaulted_paths': [['structured', 'open_questions'], path], 'raw_value': 'PRIVATE_VALUE'})
    assert safe == {'code': 'empty_defaults_applied'}
