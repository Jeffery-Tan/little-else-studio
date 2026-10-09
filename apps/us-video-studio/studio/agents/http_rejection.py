"""Bounded, redacted diagnostics for rejected DeepSeek/MiMo requests."""
import asyncio
import hashlib
import json
import re

import httpx

from .client import ModelError
from .requests import HTTP_ERROR_TERMS, HTTP_ERROR_TOOLS, safe_response_diagnostic

ERROR_BODY_LIMIT = 65536
ERROR_BODY_TIMEOUT = 5.0
_PATH = re.compile(r'(?<!\w)(?:tools|messages|model|thinking|reasoning_effort|max_tokens|'
                   r'max_completion_tokens|tool_choice|response_format|stream)'
                   r'(?:\[\d+\]|\.[A-Za-z_][A-Za-z_0-9]*)*(?!\w)')


def transport_failure_kind(error):
    """Fixed HTTPX taxonomy; exception messages may contain private details."""
    for kind, label in (
        (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
        (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
        (httpx.RemoteProtocolError, 'remote_protocol_error'),
        (httpx.LocalProtocolError, 'local_protocol_error'),
        (httpx.ConnectError, 'connect_error'), (httpx.ReadError, 'read_error'),
        (httpx.WriteError, 'write_error'), (httpx.CloseError, 'close_error'),
        (httpx.ProxyError, 'proxy_error'), (httpx.UnsupportedProtocol, 'unsupported_protocol'),
        (httpx.DecodingError, 'decoding_error'),
    ):
        if isinstance(error, kind):
            return label
    return 'transport_error'


def _request_path(text, payload):
    """Only return a path that actually exists in this outgoing request."""
    found = []
    for match in _PATH.finditer(text):
        parts = re.findall(r'[A-Za-z_][A-Za-z_0-9]*|\d+', match.group())
        if len(parts) > 24 or any(part.isdecimal() and len(part) > 9 for part in parts):
            continue
        path = [int(part) if part.isdecimal() else part for part in parts]
        current = payload
        try:
            for part in path:
                current = current[part]
        except (KeyError, IndexError, TypeError):
            continue
        found.append(path)
    return max(found, key=len) if found else None


def _diagnostic(response, payload, provider, body, state):
    wire = response.request.content
    encoded = response.headers.get('content-encoding', '').strip().lower() not in ('', 'identity')
    issue = {
        'format': 'http_error', 'provider': provider.lower(), 'field': 'response',
        'code': 'http_request_rejected', 'provider_status': response.status_code,
        'request_mode': 'strict' if any(t.get('function', {}).get('strict') is True
                                      for t in payload.get('tools', [])) else 'standard',
        'request_bytes': len(wire), 'request_sha256': hashlib.sha256(wire).hexdigest(),
        'stream': payload.get('stream'), 'thinking_type': payload.get('thinking', {}).get('type'),
        'reasoning_effort': payload.get('reasoning_effort'),
        'output_token_limit': payload.get('max_tokens', payload.get('max_completion_tokens')),
        'error_body_bytes': len(body), 'error_body_sha256': hashlib.sha256(body).hexdigest(),
        'error_body_state': state,
        'error_body_encoding': 'encoded' if encoded else 'identity',
    }
    # A truncated/interrupted prefix cannot establish the provider's error.
    if state == 'complete' and not encoded:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            data = None
        error = data.get('error') if isinstance(data, dict) else None
        if isinstance(error, dict):
            issue['provider_error_type'] = error.get('type')
            issue['provider_error_code'] = error.get('code')
            message = error.get('message')
            issue['provider_message_type'] = ('missing' if 'message' not in error else
                'string' if isinstance(message, str) else 'object' if isinstance(message, dict) else
                'array' if isinstance(message, list) else 'boolean' if type(message) is bool else
                'number' if isinstance(message, (int, float)) else 'null')
            if isinstance(message, str):
                # Terms are a fixed set, never fragments of arbitrary text.
                # They describe mentions, not a claimed schema root cause.
                issue['provider_message_terms'] = [term for term in HTTP_ERROR_TERMS if re.search(
                        r'(?<!\w)' + re.escape(term) + r'(?!\w)', message, re.IGNORECASE)]
                outgoing = {tool.get('function', {}).get('name') for tool in payload.get('tools', [])}
                issue['provider_tools'] = [name for name in sorted(HTTP_ERROR_TOOLS & outgoing)
                    if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', message)]
            for key in ('param', 'message'):
                value = error.get(key)
                if isinstance(value, str) and value:
                    path = _request_path(value, payload)
                    if path:
                        issue['request_path'] = path
                        break
    return safe_response_diagnostic(issue)


def _failure(status, provider, diagnostic):
    if status == 401:
        reason, action = 'authentication_rejected', '认证未通过，请检查这个连接的 API Key。'
    elif status == 402:
        reason, action = 'balance_rejected', '余额或付费权限不足，请核对提供方账户。'
    elif status == 403:
        reason, action = 'permission_rejected', '接口访问被拒绝，请核对账户与模型权限。'
    elif status == 429:
        reason, action = 'rate_limited', '接口限流，请稍后再发起新一轮。'
    elif status >= 500:
        reason, action = 'provider_unavailable', '提供方服务暂时异常，请稍后检查连接。'
    else:
        reason, action = 'request_rejected', '接口拒绝了请求，请检查请求参数与接口兼容性。'
    diagnostic = safe_response_diagnostic({**diagnostic, 'http_failure': reason})
    return ModelError(status, f'{provider} {reason}; usage unconfirmed', retryable=False,
        response_diagnostic=diagnostic,
        public_message=f'{provider}（HTTP {status}）：{action}本轮已停止，已保存内容保留，没有自动重试。')


async def read_http_rejection(response, payload, *, provider, check_cancel):
    """Read one limited error body; never resend or retain its raw content."""
    body = bytearray()
    state = 'complete'
    failure = None

    async def read():
        nonlocal state
        # HTTPX aiter_bytes decompresses before chunking. Read raw network
        # bytes so a small compressed error cannot allocate an unbounded body.
        # Mock/pre-buffered responses have already been read by their owner.
        if response.is_stream_consumed:
            chunk = response.content[:ERROR_BODY_LIMIT + 1]
            check_cancel()
            body.extend(chunk[:ERROR_BODY_LIMIT])
            if len(chunk) > ERROR_BODY_LIMIT:
                state = 'truncated'
            return
        async for chunk in response.aiter_raw(chunk_size=4096):
            check_cancel()
            remaining = ERROR_BODY_LIMIT - len(body)
            body.extend(chunk[:remaining])
            if len(chunk) > remaining:
                state = 'truncated'
                return

    try:
        await asyncio.wait_for(read(), timeout=ERROR_BODY_TIMEOUT)
    except TimeoutError:
        state = 'timed_out'
        failure = 'body_read_deadline'
    except (httpx.TransportError, httpx.DecodingError) as exc:
        state = 'interrupted'
        failure = transport_failure_kind(exc)
    check_cancel()
    return _failure(response.status_code, provider,
                    {**_diagnostic(response, payload, provider, bytes(body), state),
                     'error_body_failure': failure})
