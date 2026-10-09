"""Rejected HTTP boundary: synthetic bodies only, no provider or credentials."""
import asyncio
import hashlib
import gzip
import json
import threading

import httpx
import pytest

from studio.agents.client import JobCancelled, ModelError, run_agent_loop
from studio.agents.deepseek_client import DeepSeekClient, STRICT_ENDPOINT
from studio.agents.mimo_client import MiMoClient
from studio.agents.requests import safe_response_diagnostic
from studio.tools.registry import tools_for
from test_deepseek_client import transport


PARAMS = dict(model='deepseek-flash', system='SYNTHETIC_PRIVATE_SYSTEM',
              messages=[{'role': 'user', 'content': 'SYNTHETIC_PRIVATE_REQUEST'}],
              tools=tools_for('writer'))


@pytest.mark.parametrize('client,provider', [(DeepSeekClient, 'deepseek'), (MiMoClient, 'mimo')])
@pytest.mark.parametrize('status,category,public', [
    (400, 'request_rejected', '请求'), (401, 'authentication_rejected', '认证'),
    (402, 'balance_rejected', '余额'), (403, 'permission_rejected', '权限'),
    (429, 'rate_limited', '限流'), (503, 'provider_unavailable', '服务'),
    (302, 'request_rejected', '请求'),
])
def test_status_classification_never_retries_or_executes(monkeypatch, client, provider, status, category, public):
    seen = transport(monkeypatch, [httpx.Response(status, json={'error': {
        'message': 'SYNTHETIC_PRIVATE_ERROR', 'type': 'invalid_request_error'}})])
    executed = []
    with pytest.raises(ModelError) as caught:
        run_agent_loop(client('synthetic-only'), **PARAMS,
            execute_tool=lambda *args: executed.append(args), max_rounds=3)
    error = caught.value
    assert len(seen) == 1 and executed == [] and error.retryable is False
    assert error.status == status and error.usage_status == 'unknown'
    diagnostic = error.response_diagnostic
    assert diagnostic['provider'] == provider and diagnostic['provider_status'] == status
    assert diagnostic['http_failure'] == category and diagnostic['request_mode'] == 'strict'
    assert diagnostic['request_bytes'] == len(seen[0].content)
    assert seen[0].headers['Accept-Encoding'] == 'identity'
    assert diagnostic['request_sha256'] == hashlib.sha256(seen[0].content).hexdigest()
    assert diagnostic['thinking_type'] == 'enabled' and diagnostic['stream'] is False
    assert diagnostic.get('reasoning_effort') == ('high' if provider == 'deepseek' else None)
    assert diagnostic.get('output_token_limit') == (None if provider == 'deepseek' else 16384)
    assert public in error.public_message and 'HTTP ' + str(status) in error.public_message
    assert '严格格式' not in error.public_message
    assert 'SYNTHETIC_PRIVATE' not in str(error) + error.public_message + json.dumps(diagnostic)
    if provider == 'deepseek':
        assert str(seen[0].url) == STRICT_ENDPOINT


@pytest.mark.parametrize('streaming', [False, True])
def test_error_path_metadata_is_bound_to_actual_request_and_no_raw_error_saved(monkeypatch, streaming):
    raw = json.dumps({'error': {'type': 'invalid_request_error', 'code': 'invalid_json',
        'param': 'tools[0].function.parameters',
        'message': 'Invalid strict schema: enum is unsupported. SYNTHETIC_PRIVATE_ERROR',
        'secret': 'SYNTHETIC_PRIVATE_KEY'}}).encode()
    seen = transport(monkeypatch, [httpx.Response(400, content=raw)])
    client = DeepSeekClient('synthetic-only', max_tokens=24576, reasoning_effort='none')
    with pytest.raises(ModelError) as caught:
        (client.complete_with_progress if streaming else client.complete)(**PARAMS)
    diagnostic = caught.value.response_diagnostic
    assert diagnostic['request_path'] == ['tools', 0, 'function', 'parameters']
    assert diagnostic['provider_error_code'] == 'invalid_json'
    assert diagnostic['provider_error_type'] == 'invalid_request_error'
    assert diagnostic['provider_message_terms'] == ['enum', 'invalid', 'schema', 'strict', 'unsupported']
    assert diagnostic['provider_message_type'] == 'string'
    assert diagnostic['stream'] == streaming and diagnostic['thinking_type'] == 'disabled'
    assert diagnostic['output_token_limit'] == 24576 and 'reasoning_effort' not in diagnostic
    assert diagnostic['error_body_bytes'] == len(raw)
    assert diagnostic['error_body_sha256'] == hashlib.sha256(raw).hexdigest()
    assert diagnostic['error_body_state'] == 'complete' and len(seen) == 1
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(diagnostic)


@pytest.mark.parametrize('raw', [b'', b'PRIVATE_HTML', b'{"error":', b'{"error":"PRIVATE"}',
                              b'{"error":{"code":"PRIVATE","type":"PRIVATE","param":"tools[99].PRIVATE"}}'])
def test_malformed_or_unrecognized_body_preserves_http_failure_without_leaking(monkeypatch, raw):
    seen = transport(monkeypatch, [httpx.Response(400, content=raw)])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    diagnostic = caught.value.response_diagnostic
    assert len(seen) == 1 and caught.value.status == 400
    assert diagnostic['http_failure'] == 'request_rejected'
    assert 'provider_error_code' not in diagnostic and 'provider_error_type' not in diagnostic
    assert 'request_path' not in diagnostic and 'PRIVATE' not in json.dumps(diagnostic)


def test_diagnostic_cannot_inject_arbitrary_keys_or_values():
    safe = safe_response_diagnostic({'format': 'http_error', 'provider_status': 400,
        'request_path': ['tools', 0, 'function', 'parameters', 'PRIVATE'],
        'provider_message_terms': ['strict', 'PRIVATE'], 'http_failure': 'PRIVATE',
        'provider_error_code': 'PRIVATE', 'provider_error_type': 'PRIVATE',
        'request_sha256': 'PRIVATE', 'error_body_bytes': True, 'stream': 'PRIVATE',
        'output_token_limit': -1, 'body': 'PRIVATE'})
    assert safe == {'format': 'http_error', 'provider_status': 400}


@pytest.mark.parametrize('message,terms', [
    ('Function read_capabilities parameters object needs at least one property. PRIVATE',
     ['at least', 'function', 'object', 'parameters']),
    ('The description of submit_stage is invalid. PRIVATE', ['description', 'invalid']),
])
def test_provider_can_name_owned_tool_without_a_dotted_parameter_path(monkeypatch, message, terms):
    transport(monkeypatch, [httpx.Response(400, json={'error': {'message': message}})])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    issue = caught.value.response_diagnostic
    assert issue['provider_message_terms'] == terms
    assert issue['provider_tools'] == ['read_capabilities' if 'read_capabilities' in message else 'submit_stage']
    assert 'PRIVATE' not in json.dumps(issue)


class ErrorBody(httpx.AsyncByteStream):
    def __init__(self, mode, cancel):
        self.mode, self.cancel, self.closed = mode, cancel, False

    async def __aiter__(self):
        yield b'x' * 4096
        if self.mode == 'interrupt':
            raise httpx.ReadError('PRIVATE_TRANSPORT_DETAIL')
        if self.mode == 'cancel':
            self.cancel.set()
        if self.mode == 'oversize':
            for _ in range(100):
                yield b'x' * 4096
        else:
            await asyncio.sleep(30)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('mode,state', [('oversize', 'truncated'), ('interrupt', 'interrupted'),
                                      ('timeout', 'timed_out'), ('cancel', None), ('deadline', None)])
def test_error_read_is_bounded_and_cancellable_and_closed(monkeypatch, mode, state):
    from studio.agents import http_rejection
    monkeypatch.setattr(http_rejection, 'ERROR_BODY_TIMEOUT', .04 if mode != 'deadline' else 1)
    cancel = threading.Event()
    body = ErrorBody(mode, cancel)
    seen = transport(monkeypatch, [httpx.Response(400, stream=body)])
    client = DeepSeekClient('synthetic-only', cancel_event=cancel,
                            request_timeout=.08 if mode == 'deadline' else 1)
    with pytest.raises(JobCancelled if mode == 'cancel' else ModelError) as caught:
        client.complete_with_progress(**PARAMS)
    assert body.closed and len(seen) == 1
    if state:
        error = caught.value
        assert error.status == 400 and error.usage_status == 'unknown'
        assert error.response_diagnostic['error_body_state'] == state
        assert error.response_diagnostic['error_body_bytes'] <= http_rejection.ERROR_BODY_LIMIT
        assert error.response_diagnostic.get('error_body_failure') == {
            'interrupt': 'read_error', 'timeout': 'body_read_deadline'}.get(mode)
        assert 'PRIVATE' not in str(error) + json.dumps(error.response_diagnostic)
    elif mode == 'deadline':
        assert caught.value.status == 0 and caught.value.usage_status == 'unknown'


def test_error_message_path_must_exist_and_prefers_detailed_path(monkeypatch):
    seen = transport(monkeypatch, [httpx.Response(400, json={'error': {'message':
        'model rejected tools[0].function.parameters and tools[99].PRIVATE'}})])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    assert caught.value.response_diagnostic['request_path'] == ['tools', 0, 'function', 'parameters']
    assert len(seen) == 1


def test_oversized_untrusted_index_cannot_break_http_classification(monkeypatch):
    seen = transport(monkeypatch, [httpx.Response(400, json={'error': {
        'message': 'tools[' + '9' * 5000 + '].PRIVATE'}})])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    assert caught.value.status == 400 and len(seen) == 1
    assert 'request_path' not in caught.value.response_diagnostic


@pytest.mark.parametrize('message', ['invalid_model_name: SYNTHETIC', 'tools_secret_key: SYNTHETIC'])
def test_words_containing_parameter_names_are_not_paths(monkeypatch, message):
    transport(monkeypatch, [httpx.Response(400, json={'error': {'message': message}})])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    assert 'request_path' not in caught.value.response_diagnostic


def test_compressed_error_is_not_decompressed_or_parsed(monkeypatch):
    raw = gzip.compress(b'PRIVATE' * 1_000_000)
    class CompressedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
        async def aclose(self):
            pass
    seen = transport(monkeypatch, [httpx.Response(400, stream=CompressedBody(),
                                               headers={'content-encoding': 'gzip'})])
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only').complete(**PARAMS)
    issue = caught.value.response_diagnostic
    assert issue['error_body_encoding'] == 'encoded' and issue['error_body_state'] == 'complete'
    assert issue['error_body_bytes'] == len(raw)
    assert issue['error_body_sha256'] == hashlib.sha256(raw).hexdigest()
    assert 'provider_message_terms' not in issue and 'request_path' not in issue
    assert len(seen) == 1 and 'PRIVATE' not in json.dumps(issue)
