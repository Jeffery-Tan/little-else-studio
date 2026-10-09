"""Per-call wire options and bounded statistics, using synthetic HTTP only."""
import hashlib
import json
import threading
import time

import httpx
import pytest

from studio.agents.budget import BudgetedClient
from studio.agents.client import JobCancelled, ModelError, SafePreflightError, Turn, Usage
from studio.agents.deepseek_client import DeepSeekClient, _TransportObservation
from studio.agents.http_rejection import transport_failure_kind
from studio.agents.mimo_client import MiMoClient
from studio.agents.requests import safe_response_diagnostic
from studio.config import Settings
from studio.ledger import Ledger
from studio.store import Store
from studio.tools.registry import tools_for
from test_deepseek_wait import WaitingBody, delta, install_transport

PRIVATE = 'SYNTHETIC_PRIVATE_TEXT'
PARAMS = dict(model='deepseek-flash', system=PRIVATE,
              messages=[{'role': 'user', 'content': PRIVATE}], tools=[])
USAGE = {'prompt_tokens': 12, 'completion_tokens': 18}
RUN = 'transport-fixture'


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline transport fixture', 'Synthetic only')
    return store, Ledger(store, Settings(tmp_path, 'https://example.invalid'))


def budget(env, inner):
    return BudgetedClient(inner, env[1], run_id=RUN, role='writer', stage='script')


def events(env, kind):
    return [event for event in env[0].read_events(RUN) if event['kind'] == kind]


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure, self.closed = chunks, failure, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure(PRIVATE)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('streaming', [False, True])
def test_success_records_actual_options_and_original_identity(env, monkeypatch, client_class, streaming):
    if streaming:
        chunks = [delta({'reasoning_content': PRIVATE}),
                  delta({'content': 'Observed.'}, usage=USAGE, finish='stop'), b'data: [DONE]\n\n']
    else:
        chunks = [json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': 'Observed.', 'reasoning_content': PRIVATE}}], 'usage': USAGE}).encode()]
    body = Body(chunks)

    async def handler(_):
        return httpx.Response(200, stream=body, headers={'content-type':
            'text/event-stream' if streaming else 'application/json'})

    seen = install_transport(monkeypatch, handler)
    client = (DeepSeekClient('synthetic-only', reasoning_effort='low') if client_class is DeepSeekClient
              else MiMoClient('synthetic-only', max_tokens=24576))
    # BudgetedClient prefers the streaming path. Hide it for full JSON coverage.
    if not streaming:
        client.complete_with_progress = None
    result = budget(env, client).complete(**{**PARAMS, 'tools': tools_for('writer')})
    row = env[1].entries(RUN)[0]
    event = events(env, 'agent_model_transport')[0]
    diagnostic = event['diagnostic']
    payload = json.loads(seen[0].content)
    assert len(seen) == len(env[1].entries(RUN)) == 1 and body.closed
    assert event['call_id'] == row.call_id == events(env, 'agent_request_snapshot')[0]['call_id']
    assert event['role'] == 'writer' and event['stage'] == 'script'
    assert diagnostic == result.transport_diagnostic
    assert diagnostic['transport_outcome'] == 'completed' and 'transport_failure' not in diagnostic
    assert diagnostic['provider'] == ('deepseek' if client_class is DeepSeekClient else 'mimo')
    assert diagnostic['request_mode'] == 'strict' and len(payload['tools']) == 7
    assert all(tool['function']['strict'] is True for tool in payload['tools'])
    assert diagnostic['stream'] is streaming and diagnostic['thinking_type'] == 'enabled'
    assert diagnostic.get('reasoning_effort') == payload.get('reasoning_effort')
    assert diagnostic['output_limit_mode'] == ('provider_default' if client_class is DeepSeekClient else 'explicit')
    assert diagnostic.get('output_token_limit') == payload.get('max_tokens', payload.get('max_completion_tokens'))
    assert diagnostic['request_bytes'] == len(seen[0].content)
    assert diagnostic['request_sha256'] == hashlib.sha256(seen[0].content).hexdigest()
    assert diagnostic['request_timeout_ms'] == 600000 and diagnostic['http_read_timeout_ms'] == 180000
    assert diagnostic['received_bytes'] == sum(map(len, chunks))
    assert diagnostic['received_frames'] == (3 if streaming else 0)
    assert diagnostic['reasoning_bytes'] == (len(PRIVATE) if streaming else 0)
    assert diagnostic['public_content_bytes'] == (len('Observed.') if streaming else 0)
    assert diagnostic['elapsed_ms'] >= 0 and diagnostic['last_data_age_ms'] >= 0
    assert result.raw_blocks[0]['reasoning_content'] == PRIVATE and PRIVATE not in json.dumps(event)
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (12, 18, 'known', True)


@pytest.mark.parametrize('failure,label', [
    (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
    (httpx.RemoteProtocolError, 'remote_protocol_error'), (httpx.ReadError, 'read_error'),
    (httpx.DecodingError, 'decoding_error'),
])
def test_interruption_records_partial_counters_and_actual_usage_without_retry(env, monkeypatch, failure, label):
    public, private, arguments = '观察', PRIVATE, '{"partial":'
    first = delta({'content': public, 'reasoning_content': private, 'tool_calls': [{
        'index': 0, 'id': 'fixture', 'function': {'name': 'read_artifact', 'arguments': arguments}}]},
        usage=USAGE)
    body = Body([first, b': keep-alive\n\n'], failure)

    async def handler(_):
        return httpx.Response(200, stream=body)

    seen = install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        budget(env, DeepSeekClient('synthetic-only', reasoning_effort='low')).complete(**PARAMS)
    error = caught.value
    row = env[1].entries(RUN)[0]
    event = events(env, 'agent_model_transport')[0]
    issue = event['diagnostic']
    assert len(seen) == 1 and body.closed and error.retryable is False
    assert issue['transport_failure'] == label and issue['transport_outcome'] == 'failed'
    assert error.response_diagnostic['transport_failure'] == label
    assert issue['received_bytes'] == len(first) + len(b': keep-alive\n\n')
    assert issue['received_frames'] == 1
    assert issue['public_content_bytes'] == len(public.encode())
    assert issue['reasoning_bytes'] == len(private.encode()) and issue['tool_argument_bytes'] == len(arguments)
    assert issue['last_model_data_age_ms'] >= issue['last_data_age_ms'] >= 0
    assert issue['reasoning_effort'] == 'low'
    assert row.call_id == event['call_id'] == events(env, 'agent_response_rejected')[0]['call_id']
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (12, 18, 'partial', False)
    assert PRIVATE not in json.dumps(event) + str(error) + row.error
    assert public not in json.dumps(event) and arguments not in json.dumps(event)


@pytest.mark.parametrize('streaming', [False, True])
def test_connect_timeout_before_headers_records_zero_receipt_and_unknown_usage(monkeypatch, streaming):
    async def handler(_):
        raise httpx.ConnectTimeout(PRIVATE)
    seen = install_transport(monkeypatch, handler)
    client = DeepSeekClient('synthetic-only', reasoning_effort='none', max_tokens=24576)
    with pytest.raises(ModelError) as caught:
        (client.complete_with_progress if streaming else client.complete)(**PARAMS)
    error = caught.value
    issue = error.transport_diagnostic
    assert len(seen) == 1 and error.usage_status == 'unknown'
    assert issue['transport_failure'] == 'connect_timeout' and issue['received_bytes'] == 0
    assert issue['received_frames'] == 0 and 'provider_status' not in issue
    assert 'last_data_age_ms' not in issue and 'last_model_data_age_ms' not in issue
    assert issue['thinking_type'] == 'disabled' and 'reasoning_effort' not in issue
    assert issue['output_token_limit'] == 24576 and issue['output_limit_mode'] == 'explicit'
    assert '连接超时' in error.public_message and PRIVATE not in str(error) + json.dumps(issue)


@pytest.mark.parametrize('cancelled', [False, True])
def test_deadline_and_stop_have_distinct_identity_without_inventing_keepalive_usage(env, monkeypatch, cancelled):
    cancel = threading.Event()
    body = WaitingBody(cancel=cancel if cancelled else None)

    async def handler(_):
        return httpx.Response(200, stream=body)

    seen = install_transport(monkeypatch, handler)
    with pytest.raises(JobCancelled if cancelled else ModelError) as caught:
        budget(env, DeepSeekClient('synthetic-only', cancel_event=cancel,
                                  request_timeout=.08, progress_interval=.02)).complete(**PARAMS)
    issue = events(env, 'agent_model_transport')[0]['diagnostic']
    assert issue['transport_failure'] == ('cancelled' if cancelled else 'total_deadline')
    assert issue['transport_outcome'] == ('cancelled' if cancelled else 'failed')
    assert issue['received_bytes'] > 0 and issue['received_frames'] == 0
    assert issue['public_content_bytes'] == issue['reasoning_bytes'] == issue['tool_argument_bytes'] == 0
    assert issue['request_timeout_ms'] == 80 and len(seen) == 1 and body.closed
    assert env[1].entries(RUN)[0].usage_status == 'unknown'
    if not cancelled:
        assert caught.value.response_diagnostic['code'] == 'request_deadline_exceeded'


def test_clean_eof_does_not_claim_httpx_failure(monkeypatch):
    first = delta({'content': 'partial'})

    async def handler(_):
        return httpx.Response(200, stream=Body([first]))

    install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete_with_progress(**PARAMS)
    assert caught.value.transport_diagnostic['transport_failure'] == 'stream_eof'
    assert caught.value.response_diagnostic['code'] == 'incomplete_stream'
    assert caught.value.usage_status == 'unknown'


def test_invalid_complete_response_keeps_known_usage_and_actual_wire_options(env, monkeypatch):
    raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
        'content': 42, 'reasoning_content': PRIVATE}}], 'usage': USAGE}).encode()
    body = Body([raw])

    async def handler(_):
        return httpx.Response(200, stream=body, headers={'content-type': 'application/json'})

    seen = install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        budget(env, DeepSeekClient('synthetic-only', reasoning_effort='low')).complete(**PARAMS)
    issue = caught.value.response_diagnostic
    transport = events(env, 'agent_model_transport')[0]['diagnostic']
    assert issue['code'] == 'expected_text' and issue['field'] == 'content'
    assert issue['reasoning_effort'] == 'low' and issue['stream'] is True
    assert transport['transport_outcome'] == 'failed' and 'transport_failure' not in transport
    assert transport['received_frames'] == 0 and transport['received_bytes'] == len(raw)
    assert caught.value.usage_status == 'known' and len(seen) == 1 and body.closed
    assert env[1].entries(RUN)[0].usage_status == 'known'
    assert PRIVATE not in json.dumps(transport) + json.dumps(issue)


def test_bounded_observation_caps_without_losing_private_data_boundary():
    observation = _TransportObservation({'stream': False}, 'DeepSeek', 'https://example.invalid',
                                        1e308, time.monotonic() - 1e10)
    observation.count('received_bytes', 2**50)
    issue = observation.snapshot()
    assert issue['received_bytes'] == issue['request_timeout_ms'] == issue['elapsed_ms'] == 2**31 - 1
    assert issue['transport_statistics_capped'] is True
    assert safe_response_diagnostic({**issue, 'received_frames': True, 'reasoning_bytes': -1,
        'tool_argument_bytes': 2**31, 'transport_failure': PRIVATE, 'error_body_failure': PRIVATE,
        'raw_body': PRIVATE, 'api_key': PRIVATE, 'last_data_age_ms': 1.5}) == {
            key: value for key, value in issue.items() if key not in {
                'received_frames', 'reasoning_bytes', 'tool_argument_bytes'}}


def test_transport_audit_write_failure_blocks_returned_tools_and_keeps_accounting(env, monkeypatch):
    from studio.agents import budget as module

    class Synthetic:
        def complete(self, **_):
            return Turn(text='Observed.', usage=Usage(12, 18), transport_diagnostic={
                'format': 'model_transport', 'code': 'transport_observed', 'transport_outcome': 'completed'})

    def fail(*_, **__):
        raise OSError(PRIVATE)

    monkeypatch.setattr(module, 'record_model_transport', fail)
    with pytest.raises(SafePreflightError, match='诊断记录未能保存') as caught:
        budget(env, Synthetic()).complete(**PARAMS)
    row = env[1].entries(RUN)[0]
    assert (row.input_tokens, row.output_tokens, row.usage_status) == (12, 18, 'known')
    assert len(env[1].entries(RUN)) == 1 and PRIVATE not in str(caught.value)


def test_failure_audit_write_failure_preserves_original_failure_and_usage(env, monkeypatch):
    from studio.agents import budget as module

    class Synthetic:
        def complete(self, **_):
            failure = ModelError(0, 'synthetic disconnect', usage=Usage(12, None), retryable=False)
            failure.transport_diagnostic = {'format': 'model_transport', 'code': 'transport_observed'}
            raise failure

    def fail(*_, **__):
        raise OSError(PRIVATE)

    monkeypatch.setattr(module, 'record_model_transport', fail)
    with pytest.raises(ModelError, match='synthetic disconnect') as caught:
        budget(env, Synthetic()).complete(**PARAMS)
    assert caught.value.retryable is False and 'settlement was retained' in caught.value.__notes__[0]
    row = env[1].entries(RUN)[0]
    assert (row.input_tokens, row.output_tokens, row.usage_status) == (12, None, 'partial')
    assert PRIVATE not in str(caught.value) + str(caught.value.__notes__)


def test_post_transport_execution_guard_keeps_successful_transport_on_failed_call(env, monkeypatch):
    from studio.agents import budget as module
    diagnostic = {'format': 'model_transport', 'code': 'transport_observed',
                  'transport_outcome': 'completed', 'provider': 'deepseek', 'reasoning_effort': 'low'}

    class Synthetic:
        def complete(self, **_):
            return Turn(usage=Usage(12, 18), transport_diagnostic=diagnostic)

    def reject(turn, _):
        raise ModelError(200, 'synthetic execution guard rejected', usage=turn.usage, usage_final=True,
                         response_diagnostic={'field': 'tool_arguments', 'code': 'invalid_arguments_schema'})

    monkeypatch.setattr(module, 'prepare_strict_writer_execution', reject)
    with pytest.raises(ModelError):
        budget(env, Synthetic()).complete(**PARAMS)
    row = env[1].entries(RUN)[0]
    event = events(env, 'agent_model_transport')[0]
    assert event['diagnostic'] == diagnostic and event['call_id'] == row.call_id
    assert event['call_id'] == events(env, 'agent_response_rejected')[0]['call_id']
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (12, 18, 'known', False)


@pytest.mark.parametrize('failure,label', [
    (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
    (httpx.LocalProtocolError, 'local_protocol_error'), (httpx.ConnectError, 'connect_error'),
    (httpx.WriteError, 'write_error'), (httpx.CloseError, 'close_error'),
    (httpx.ProxyError, 'proxy_error'), (httpx.UnsupportedProtocol, 'unsupported_protocol'),
    (httpx.TransportError, 'transport_error'),
])
def test_other_httpx_failures_use_only_fixed_labels(failure, label):
    assert transport_failure_kind(failure(PRIVATE)) == label
