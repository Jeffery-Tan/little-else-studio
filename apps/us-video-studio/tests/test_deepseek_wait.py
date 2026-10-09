"""Bounded model waits with synthetic async HTTP; no provider requests."""
import asyncio
import json
import threading
import time
import select
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from studio.agents.client import JobCancelled, ModelError
from studio.agents.deepseek_client import DeepSeekClient
from studio.agents.mimo_client import MiMoClient

PARAMS = dict(model='deepseek-flash', system='fixture',
              messages=[{'role': 'user', 'content': 'fixture'}], tools=[])


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    requests = []

    async def handle(request):
        requests.append(request)
        return await handler(request)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw:
                        original(**kw, transport=httpx.MockTransport(handle)))
    return requests


def delta(value, *, usage=None, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': value,
              'finish_reason': finish}], 'usage': usage}) + '\n\n').encode()


class WaitingBody(httpx.AsyncByteStream):
    def __init__(self, first=b': keep-alive\n\n', cancel=None):
        self.first, self.cancel = first, cancel
        self.closed = False

    async def __aiter__(self):
        if self.first:
            yield self.first
        if self.cancel:
            self.cancel.set()
        while True:
            await asyncio.sleep(.01)
            yield b': keep-alive\n\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('stream', [False, True])
def test_deadline_covers_headers_and_cancels_request_without_retry(monkeypatch, stream):
    closed = []

    async def handler(_):
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)

    requests = install_transport(monkeypatch, handler)
    client = DeepSeekClient('synthetic-only', request_timeout=.08, progress_interval=.02)
    started = time.monotonic()
    with pytest.raises(ModelError) as caught:
        (client.complete_with_progress if stream else client.complete)(**PARAMS)
    assert time.monotonic() - started < 1
    assert closed == [True] and len(requests) == 1
    assert caught.value.status == 0 and caught.value.usage_status == 'unknown'
    assert 'usage unconfirmed' in caught.value.message and '未自动重试' in caught.value.message


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_nonstream_progress_closes_incomplete_body_and_keeps_unknown_usage(monkeypatch, stop):
    cancel = threading.Event()
    body = WaitingBody(first=b'{"choices": [', cancel=cancel if stop == 'cancel' else None)

    async def handler(_):
        return httpx.Response(200, stream=body, headers={'content-type': 'application/json'})

    requests = install_transport(monkeypatch, handler)
    events = []
    client = DeepSeekClient('synthetic-only', progress_stream=False, cancel_event=cancel,
        request_timeout=.12, progress_interval=.025)
    with pytest.raises(JobCancelled if stop == 'cancel' else ModelError) as caught:
        client.complete_with_progress(**PARAMS, on_progress=events.append)
    assert len(requests) == 1 and json.loads(requests[0].content)['stream'] is False
    assert body.closed
    assert caught.value.usage.input_tokens is None and caught.value.usage.output_tokens is None
    if stop == 'deadline':
        waiting = [event for event in events if event['type'] == 'agent_activity']
        assert len(waiting) >= 2 and all(event['transport_state'] == 'waiting' for event in waiting)


@pytest.mark.parametrize('first,state', [
    (b': keep-alive\n\n', 'waiting'),
    (delta({'reasoning_content': 'private-secret'}), 'receiving'),
    (delta({'tool_calls': [{'index': 0, 'id': 'fixture', 'function': {
        'name': 'read_artifact', 'arguments': '{"secret-fragment":'}}]}), 'receiving'),
    (delta({'content': 'partial'}, usage={'prompt_tokens': 4, 'completion_tokens': 6}, finish='stop'), 'receiving'),
])
def test_keepalive_does_not_extend_deadline_or_expose_private_data(monkeypatch, first, state):
    body = WaitingBody(first)

    async def handler(_):
        return httpx.Response(200, stream=body)

    requests = install_transport(monkeypatch, handler)
    events = []
    with pytest.raises(ModelError) as caught:
        DeepSeekClient('synthetic-only', request_timeout=.12, progress_interval=.025
            ).complete_with_progress(**PARAMS, on_progress=events.append)
    assert len(requests) == 1 and body.closed
    has_usage = b'"prompt_tokens"' in first
    assert caught.value.usage_status == ('partial' if has_usage else 'unknown')
    if has_usage:
        assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (4, 6)
    waiting = [row for row in events if row['type'] == 'agent_activity']
    assert len(waiting) >= 2 and waiting[0]['transport_state'] == 'waiting'
    assert waiting[-1]['transport_state'] == state
    assert waiting[-1]['elapsed_seconds'] >= waiting[0]['elapsed_seconds']
    assert waiting[-1]['remaining_seconds'] <= waiting[0]['remaining_seconds']
    assert all(set(row) == {'type', 'phase', 'elapsed_seconds', 'remaining_seconds',
                           'timeout_seconds', 'transport_state'} for row in waiting)
    assert 'private-secret' not in str(events) and 'secret-fragment' not in str(events)
    observed = len(events)
    time.sleep(.04)
    assert len(events) == observed  # No orphan monitor after settlement.


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_stop_interrupts_headers_and_body_before_deadline(monkeypatch, phase):
    cancel = threading.Event()
    body = WaitingBody(cancel=cancel)
    closed = []

    async def handler(_):
        if phase == 'body':
            return httpx.Response(200, stream=body)
        try:
            cancel.set()
            await asyncio.sleep(30)
        finally:
            closed.append(True)

    requests = install_transport(monkeypatch, handler)
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        DeepSeekClient('synthetic-only', cancel_event=cancel, request_timeout=2
            ).complete_with_progress(**PARAMS)
    assert time.monotonic() - started < 1 and len(requests) == 1
    assert body.closed if phase == 'body' else closed == [True]


def test_late_stop_retains_complete_final_usage(monkeypatch):
    cancel = threading.Event()

    class CompleteBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield delta({'content': 'complete'}, finish='stop',
                        usage={'prompt_tokens': 4, 'completion_tokens': 6}) + b'data: [DONE]\n\n'

        async def aclose(self):
            cancel.set()  # Final turn has parsed; stop arrives during HTTP cleanup.

    async def handler(_):
        return httpx.Response(200, stream=CompleteBody())

    requests = install_transport(monkeypatch, handler)
    with pytest.raises(JobCancelled) as caught:
        DeepSeekClient('synthetic-only', cancel_event=cancel).complete_with_progress(**PARAMS)
    assert len(requests) == 1
    assert caught.value.usage_final
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (4, 6)


@pytest.mark.parametrize('client_class', [DeepSeekClient, MiMoClient])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('stopped', [False, True])
def test_late_complete_response_respects_deadline_and_stop_with_final_usage(
        monkeypatch, client_class, stream, stopped):
    cancel = threading.Event()
    if stream:
        raw = delta({'content': 'synthetic complete'}, finish='stop',
                    usage={'prompt_tokens': 12, 'completion_tokens': 18}) + b'data: [DONE]\n\n'
    else:
        raw = json.dumps({'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': 'synthetic complete'}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 18}}).encode()

    class CompleteBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield raw

        async def aclose(self):
            self.closed = True
            if stopped:
                cancel.set()  # Stop wins even when the completed call is late.

    body = CompleteBody()

    async def handler(_):
        # Synchronous decoding/cleanup can occupy the event loop past its wait.
        time.sleep(.07)
        return httpx.Response(200, stream=body, headers={
            'content-type': 'text/event-stream' if stream else 'application/json'})

    requests = install_transport(monkeypatch, handler)
    client = client_class('synthetic-only', cancel_event=cancel)
    client.request_timeout = .04
    with pytest.raises(JobCancelled if stopped else ModelError) as caught:
        (client.complete_with_progress if stream else client.complete)(**PARAMS)
    assert len(requests) == 1 and body.closed
    assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (12, 18)
    if stopped:
        assert caught.value.usage_final
        assert caught.value.transport_diagnostic['transport_failure'] == 'cancelled'
    else:
        assert caught.value.usage_status == 'known' and caught.value.retryable is False
        assert caught.value.response_diagnostic['code'] == 'request_deadline_exceeded'
        assert caught.value.transport_diagnostic['transport_failure'] == 'total_deadline'


def test_stop_before_request_has_no_provider_attempt(monkeypatch):
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: pytest.fail('No HTTP client after stop'))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        DeepSeekClient('synthetic-only', cancel_event=cancel).complete_with_progress(**PARAMS)


def test_nonstream_body_deadline_settles_one_unknown_call(monkeypatch, tmp_path):
    from studio.agents.budget import BudgetedClient
    from studio.config import Settings
    from studio.ledger import Ledger
    from studio.store import Store

    body = WaitingBody(b'\n')

    async def handler(_):
        return httpx.Response(200, stream=body)

    requests = install_transport(monkeypatch, handler)
    store = Store(tmp_path)
    store.create_run('wait-fixture', 'Offline wait fixture', 'fixture')
    ledger = Ledger(store, Settings(tmp_path, ''))
    transport = DeepSeekClient('synthetic-only', request_timeout=.08)

    class NonStreaming:
        def complete(self, **params):
            return transport.complete(**params)

    client = BudgetedClient(NonStreaming(), ledger,
                           run_id='wait-fixture', role='orchestrator', stage='intake')
    with pytest.raises(ModelError):
        client.complete(**PARAMS)
    rows = ledger.entries('wait-fixture')
    assert len(requests) == len(rows) == 1 and body.closed
    assert rows[0].usage_status == 'unknown' and rows[0].input_tokens is None
    assert rows[0].output_tokens is None and rows[0].ok is False


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_real_loopback_socket_is_closed_on_deadline(monkeypatch, phase):
    """Exercise actual HTTPX cancellation, using only an owned local socket."""
    requests, peer_closed = [], threading.Event()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            if phase == 'body':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.flush()
            while not stop.is_set():
                if select.select([self.connection], [], [], .01)[0]:
                    try:
                        if not self.connection.recv(1):
                            peer_closed.set()
                            return
                    except ConnectionError:
                        peer_closed.set()
                        return
                if phase == 'body':
                    try:
                        self.wfile.write(b': keep-alive\n\n')
                        self.wfile.flush()
                    except ConnectionError:
                        peer_closed.set()
                        return

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    worker.start()
    monkeypatch.setattr('studio.agents.deepseek_client.ENDPOINT',
                        f'http://127.0.0.1:{server.server_port}/fixture')
    try:
        transport = DeepSeekClient('synthetic-only', request_timeout=.8)
        # This case checks cancellation of a connected socket. Build HTTPX's
        # TLS context before its short deadline; slow Windows certificate
        # loading must not consume the budget before the request reaches us.
        client = httpx.AsyncClient(timeout=transport.timeout,
                                  follow_redirects=False, trust_env=False)
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **_: client)
        with pytest.raises(ModelError, match='未自动重试'):
            transport.complete_with_progress(**PARAMS)
        assert requests == ['/fixture']
        assert peer_closed.wait(1)
        assert client.is_closed
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


@pytest.mark.parametrize('failure', ['rejected', 'incomplete', 'malformed_final'])
def test_stop_wins_over_simultaneous_response_error(monkeypatch, failure):
    cancel = threading.Event()

    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            if failure == 'malformed_final':
                # Complete counters remain known even if reasoning is invalid.
                yield json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'content': 'fixture', 'reasoning_content': 123}}],
                    'usage': {'prompt_tokens': 4, 'completion_tokens': 6}}).encode()
            else:
                yield delta({'content': 'incomplete'})

        async def aclose(self):
            cancel.set()

    async def handler(_):
        return httpx.Response(401 if failure == 'rejected' else 200, stream=ErrorBody())

    requests = install_transport(monkeypatch, handler)
    with pytest.raises(JobCancelled) as caught:
        client = DeepSeekClient('synthetic-only', cancel_event=cancel)
        (client.complete if failure == 'malformed_final' else client.complete_with_progress)(**PARAMS)
    assert len(requests) == 1
    assert caught.value.usage_final is (failure == 'malformed_final')
    if failure == 'malformed_final':
        assert (caught.value.usage.input_tokens, caught.value.usage.output_tokens) == (4, 6)


@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), '600'])
def test_invalid_total_limit_rejected(value):
    with pytest.raises(ValueError, match='request_timeout'):
        DeepSeekClient('synthetic-only', request_timeout=value)
