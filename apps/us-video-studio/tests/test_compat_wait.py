"""Compatibility wait/stop behavior with mock HTTP and owned loopback sockets."""
import asyncio
import json
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from fakes import text_stream, tool_stream
from studio.agents.budget import BudgetedClient
from studio.agents.client import AnthropicCompatClient, JobCancelled, ModelError
from studio.agents.openai_compat import OpenAICompatClient
from studio.connections import ConnectionSnapshot
from studio.config import Settings
from studio.ledger import Ledger
from studio.store import Store
from test_deepseek_wait import install_transport

PARAMS = dict(model='synthetic', system='Synthetic', messages=[], tools=[])
PRIVATE = 'SYNTHETIC_PRIVATE_RESPONSE'
CLIENTS = [OpenAICompatClient, AnthropicCompatClient]


def stream(lines):
    return ('\n\n'.join(lines) + '\n\n').encode()


def final_body(client_class):
    if client_class is AnthropicCompatClient:
        return stream(text_stream('Observed.', input_tokens=12, output_tokens=18))
    return json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': 'Observed.'}}],
                      'usage': {'prompt_tokens': 12, 'completion_tokens': 18}}).encode()


def keepalive(client_class):
    return b'data: {"type":"ping"}\n\n' if client_class is AnthropicCompatClient else b' '


class Waiting(httpx.AsyncByteStream):
    def __init__(self, client_class, first=b'', cancel=None, failure=None):
        self.client_class, self.first, self.cancel, self.failure = client_class, first, cancel, failure
        self.closed = False

    async def __aiter__(self):
        if self.first:
            yield self.first
        if self.cancel:
            self.cancel.set()
        if self.failure:
            raise self.failure(PRIVATE)
        while True:
            await asyncio.sleep(.01)
            yield keepalive(self.client_class)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('client_class', CLIENTS)
@pytest.mark.parametrize('phase', ['headers', 'body'])
@pytest.mark.parametrize('cancelled', [False, True])
def test_stop_and_deadline_close_owned_request_without_retry(monkeypatch, client_class, phase, cancelled):
    cancel = threading.Event()
    body = Waiting(client_class, first=keepalive(client_class), cancel=cancel if cancelled else None)
    header_closed = []

    async def handler(_):
        if phase == 'body':
            return httpx.Response(200, stream=body)
        try:
            if cancelled:
                cancel.set()
            await asyncio.sleep(30)
        finally:
            header_closed.append(True)

    seen = install_transport(monkeypatch, handler)
    progress = []
    client = client_class('https://example.invalid', 'synthetic-only', cancel_event=cancel,
                          request_timeout=.08, progress_interval=.02)
    with pytest.raises(JobCancelled if cancelled else ModelError) as caught:
        client.complete_with_progress(**PARAMS, on_progress=progress.append)
    assert len(seen) == 1 and (body.closed if phase == 'body' else header_closed == [True])
    assert caught.value.transport_diagnostic['transport_failure'] == ('cancelled' if cancelled else 'total_deadline')
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (None, None)
    assert all(row['transport_state'] == 'waiting' for row in progress)
    assert all(set(row) == {'type', 'phase', 'elapsed_seconds', 'remaining_seconds',
                           'timeout_seconds', 'transport_state'} for row in progress)
    observed = len(progress)
    time.sleep(.025)
    assert len(progress) == observed  # No observer/request survives completion.


@pytest.mark.parametrize('ending', ['stop', 'deadline', 'disconnect'])
def test_anthropic_partial_usage_and_private_delta_survive_only_in_accounting(tmp_path, monkeypatch, ending):
    cancel = threading.Event()
    first = stream(tool_stream('one', 'read_artifact', '{"unfinished":', fragments=1)[:3])
    body = Waiting(AnthropicCompatClient, first=first, cancel=cancel if ending == 'stop' else None,
                   failure=httpx.ReadError if ending == 'disconnect' else None)

    async def handler(_):
        return httpx.Response(200, stream=body)

    seen = install_transport(monkeypatch, handler)
    store = Store(tmp_path)
    store.create_run('wait-fixture', 'Synthetic', 'Synthetic only')
    ledger = Ledger(store, Settings(tmp_path, 'https://example.invalid'))
    client = BudgetedClient(AnthropicCompatClient('https://example.invalid', 'synthetic-only',
        cancel_event=cancel, request_timeout=.08, progress_interval=.02), ledger,
        run_id='wait-fixture', role='writer', stage='writing')
    progress = []
    client.public_progress = progress.append
    with pytest.raises(JobCancelled if ending == 'stop' else ModelError):
        client.complete(**PARAMS)
    row = ledger.entries('wait-fixture')[0]
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (20, 0, 'partial', False)
    assert len(seen) == 1 and body.closed
    assert any(event['transport_state'] == 'receiving' for event in progress)
    assert 'unfinished' not in str(progress) and PRIVATE not in row.error
    event = next(row for row in store.read_events('wait-fixture') if row['kind'] == 'agent_model_transport')
    assert event['call_id'] == ledger.entries('wait-fixture')[0].call_id
    assert 'unfinished' not in json.dumps(event)


@pytest.mark.parametrize('client_class', CLIENTS)
@pytest.mark.parametrize('failure', [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_httpx_failure_has_fixed_private_safe_diagnostic(monkeypatch, client_class, failure):
    async def handler(_):
        raise failure(PRIVATE)
    seen = install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        client_class('https://example.invalid', 'synthetic-only').complete(**PARAMS)
    assert caught.value.retryable is False and len(seen) == 1
    assert caught.value.usage_status == 'unknown'
    assert caught.value.response_diagnostic['transport_failure'] in {
        'connect_timeout', 'read_timeout', 'remote_protocol_error'}
    assert PRIVATE not in str(caught.value) + json.dumps(caught.value.response_diagnostic)


@pytest.mark.parametrize('client_class', CLIENTS)
def test_complete_protocol_rejection_is_distinct_from_network_failure(monkeypatch, client_class):
    raw = b'not JSON' if client_class is OpenAICompatClient else b'data: not JSON\n\n'
    async def handler(_):
        return httpx.Response(200, content=raw)
    install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        client_class('https://example.invalid', 'synthetic-only').complete(**PARAMS)
    assert caught.value.response_diagnostic['code'] == 'protocol_error'
    assert caught.value.response_diagnostic['provider_status'] == 200
    assert 'transport_failure' not in caught.value.transport_diagnostic


@pytest.mark.parametrize('client_class', CLIENTS)
@pytest.mark.parametrize('malformed', [False, True])
def test_stop_after_response_wins_and_retains_complete_or_partial_usage(monkeypatch, client_class, malformed):
    cancel = threading.Event()
    raw = final_body(client_class)
    if malformed:
        if client_class is OpenAICompatClient:
            value = json.loads(raw)
            value['choices'][0]['message']['content'] = 42
            raw = json.dumps(value).encode()
        else:
            raw = raw.replace(b'"end_turn"', b'"invalid"')
    class ResponseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
        async def aclose(self):
            cancel.set()
    async def handler(_):
        return httpx.Response(200, stream=ResponseBody())
    seen = install_transport(monkeypatch, handler)
    with pytest.raises(JobCancelled) as caught:
        client_class('https://example.invalid', 'synthetic-only', cancel_event=cancel).complete(**PARAMS)
    assert len(seen) == 1 and (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (12, 18)
    assert caught.value.usage_final is (not malformed or client_class is OpenAICompatClient)
    assert caught.value.transport_diagnostic['transport_outcome'] == 'cancelled'


@pytest.mark.parametrize('client_class', CLIENTS)
def test_stop_during_deadline_close_wins_over_timeout(monkeypatch, client_class):
    cancel = threading.Event()
    body = Waiting(client_class)
    original_close = body.aclose
    async def close():
        await original_close()
        cancel.set()
    body.aclose = close
    async def handler(_):
        return httpx.Response(200, stream=body)
    install_transport(monkeypatch, handler)
    with pytest.raises(JobCancelled):
        client_class('https://example.invalid', 'synthetic-only', cancel_event=cancel,
                     request_timeout=.06).complete(**PARAMS)
    assert body.closed


@pytest.mark.parametrize('client_class', CLIENTS)
def test_completion_after_deadline_is_discarded_with_received_final_usage(monkeypatch, client_class):
    async def handler(_):
        # Simulate a synchronous response decoder occupying the event loop.
        # A late finished task must not bypass the absolute deadline check.
        time.sleep(.07)
        return httpx.Response(200, content=final_body(client_class))
    seen = install_transport(monkeypatch, handler)
    with pytest.raises(ModelError) as caught:
        client_class('https://example.invalid', 'synthetic-only', request_timeout=.04).complete(**PARAMS)
    assert len(seen) == 1 and caught.value.transport_diagnostic['transport_failure'] == 'total_deadline'
    assert caught.value.usage_status == 'known'
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (12, 18)


@pytest.mark.parametrize('provider,client_class', [('openai_compat', OpenAICompatClient),
                                                ('anthropic_compat', AnthropicCompatClient)])
def test_routing_preserves_stop_signal_and_default_limit(tmp_path, provider, client_class):
    cancel = threading.Event()
    def check():
        pass
    public = {'connections': [{'id': 'fixture', 'provider': provider,
        'base_url': 'https://example.invalid', 'token_limit_parameter': 'max_completion_tokens'}],
        'roles': {'writer': {'connection': 'fixture', 'model': 'synthetic'}}}
    client = ConnectionSnapshot(json.dumps(public), {'fixture': 'synthetic-only'}, tmp_path).client(
        check_cancel=check, cancel_event=cancel).for_role('writer', 'fixture')
    assert isinstance(client, client_class)
    assert client.check_cancel is check and client.cancel_event is cancel and client.request_timeout == 600


@pytest.mark.parametrize('client_class', CLIENTS)
@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), '600'])
def test_invalid_total_limit_rejected_before_network(client_class, value):
    with pytest.raises(ValueError, match='request_timeout'):
        client_class('https://example.invalid', 'synthetic-only', request_timeout=value)


@pytest.mark.parametrize('client_class', CLIENTS)
@pytest.mark.parametrize('phase', ['headers', 'silent_body', 'keepalive_body', 'partial_usage'])
@pytest.mark.parametrize('cancelled', [False, True])
def test_real_loopback_socket_closed_on_stop_or_absolute_deadline(monkeypatch, client_class, phase, cancelled):
    """No provider, external network, or private payload: one owned local socket."""
    received, peer_closed, server_stop, cancel = [threading.Event() for _ in range(4)]
    model_data_received = threading.Event()
    attempts = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            attempts.append(self.path)
            received.set()
            if phase != 'headers':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if client_class is AnthropicCompatClient else 'application/json')
                self.end_headers()
                self.wfile.flush()
                if phase == 'partial_usage':
                    raw = (stream([
                        'data: {"type":"message_start","message":{"usage":{"input_tokens":12}}}',
                        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}',
                        'data: ' + json.dumps({'type': 'content_block_delta', 'index': 0,
                                              'delta': {'type': 'thinking_delta', 'thinking': PRIVATE}}),
                    ]) if client_class is AnthropicCompatClient else b'{"usage":{"prompt_tokens":12},')
                    self.wfile.write(raw)
                    self.wfile.flush()
            while not server_stop.is_set():
                if select.select([self.connection], [], [], .01)[0]:
                    try:
                        if not self.connection.recv(1):
                            peer_closed.set()
                            return
                    except ConnectionError:
                        peer_closed.set()
                        return
                if phase in {'keepalive_body', 'partial_usage'}:
                    try:
                        self.wfile.write(keepalive(client_class))
                        self.wfile.flush()
                    except ConnectionError:
                        peer_closed.set()
                        return
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    client = client_class(f'http://127.0.0.1:{server.server_port}', 'synthetic-only',
                          cancel_event=cancel, timeout=3, request_timeout=.25, progress_interval=.02)
    # Prebuild the client so Windows certificate loading cannot consume the tiny
    # test deadline before the owned HTTP request reaches the loopback server.
    session = httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: session)
    def request_stop():
        if received.wait(2):
            if phase == 'partial_usage' and client_class is AnthropicCompatClient:
                assert model_data_received.wait(2)
            cancel.set()
    stopper = threading.Thread(target=request_stop, daemon=True) if cancelled else None
    if stopper:
        stopper.start()
    try:
        def progress(event):
            assert PRIVATE not in str(event)
            if event['transport_state'] == 'receiving':
                model_data_received.set()
        with pytest.raises(JobCancelled if cancelled else ModelError) as caught:
            client.complete_with_progress(**PARAMS, on_progress=progress)
        assert len(attempts) == 1 and peer_closed.wait(1) and session.is_closed
        assert caught.value.transport_diagnostic['transport_failure'] == ('cancelled' if cancelled else 'total_deadline')
        assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (
            (12, None) if phase == 'partial_usage' and client_class is AnthropicCompatClient else (None, None))
    finally:
        server_stop.set()
        server.shutdown()
        server.server_close()
        worker.join(1)
        if stopper:
            stopper.join(1)


@pytest.mark.parametrize('client_class', CLIENTS)
def test_real_loopback_complete_response_keeps_usage_and_closes_socket(tmp_path, monkeypatch, client_class):
    attempts = []
    raw = final_body(client_class)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            attempts.append(self.path)
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
            self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    session = httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: session)
    store = Store(tmp_path)
    store.create_run('complete-fixture', 'Synthetic', 'Synthetic only')
    ledger = Ledger(store, Settings(tmp_path, 'https://example.invalid'))
    inner = client_class(f'http://127.0.0.1:{server.server_port}', 'synthetic-only', request_timeout=.5)
    client = BudgetedClient(inner, ledger, run_id='complete-fixture', role='writer', stage='writing')
    try:
        turn = client.complete(**PARAMS)
        assert turn.text == 'Observed.' and session.is_closed and len(attempts) == 1
        row = ledger.entries('complete-fixture')[0]
        assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (12, 18, 'known', True)
        event = next(event for event in store.read_events('complete-fixture') if event['kind'] == 'agent_model_transport')
        assert event['call_id'] == row.call_id and event['diagnostic']['transport_outcome'] == 'completed'
        assert event['diagnostic']['provider_status'] == 200
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)
