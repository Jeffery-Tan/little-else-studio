"""One cancellable HTTP attempt for the two compatibility protocols."""
import asyncio
import hashlib
import math
import time

import httpx

from .client import JobCancelled, ModelError, Usage
from .http_rejection import transport_failure_kind
from .requests import request_transport_diagnostic, safe_response_diagnostic


def validate_wait(request_timeout, progress_interval):
    for name, value in (('request_timeout', request_timeout), ('progress_interval', progress_interval)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be a positive finite number')


def check_stopped(check_cancel, cancel_event):
    if check_cancel:
        check_cancel()
    if cancel_event is not None and cancel_event.is_set():
        raise JobCancelled('用户已请求停止；未启动下一次模型调用或工具。')


async def bounded_http(exchange, *, payload, endpoint, provider, check_cancel, cancel_event,
                       request_timeout, progress_interval, on_progress):
    """Cancel the owned async task and await closure before releasing its call."""
    started = last_progress = time.monotonic()
    receiving = False
    observed = Usage(None, None)
    completed, failed = None, None
    tools = [{'name': tool.get('function', {}).get('name')} for tool in payload.get('tools', [])]
    wire = httpx.Request('POST', endpoint, json=payload).content
    metadata = {**request_transport_diagnostic(payload, provider),
                'request_bytes': min(len(wire), 2**31 - 1), 'request_sha256': hashlib.sha256(wire).hexdigest(),
                'request_timeout_ms': min(int(min(request_timeout, (2**31 - 1) / 1000) * 1000), 2**31 - 1)}

    def stop():
        check_stopped(check_cancel, cancel_event)

    def report():
        nonlocal last_progress
        last_progress = time.monotonic()
        if on_progress:
            elapsed = min(request_timeout, max(0, last_progress - started))
            on_progress({'type': 'agent_activity', 'phase': 'waiting_model',
                         'elapsed_seconds': round(elapsed, 1),
                         'remaining_seconds': round(max(0, request_timeout - elapsed), 1),
                         'timeout_seconds': request_timeout,
                         'transport_state': 'receiving' if receiving else 'waiting'})

    def model_data():
        nonlocal receiving
        if not receiving:
            receiving = True
            report()

    def model_usage(usage):
        nonlocal observed
        observed = Usage(usage.input_tokens if usage.input_tokens is not None else observed.input_tokens,
                         usage.output_tokens if usage.output_tokens is not None else observed.output_tokens)

    def deadline(usage, *, final=False):
        accounting = 'received usage retained' if final else 'usage unconfirmed'
        return ModelError(0, f'模型单次等待超过 {request_timeout:g} 秒，本轮已停止；未自动重试；{accounting}',
            usage=usage, usage_final=final, retryable=False, response_diagnostic={
                'code': 'request_deadline_exceeded', 'transport_failure': 'total_deadline'},
            public_message='模型本次等待超过总时限，已停止本轮且没有自动重试；已保存内容和已收到用量保留。')

    stop()
    report()
    task = asyncio.create_task(exchange(model_data, model_usage))
    try:
        while True:
            if task.done():
                try:
                    turn = task.result()
                except ModelError as error:
                    try:
                        stop()
                    except JobCancelled as cancelled:
                        raise JobCancelled(str(cancelled), usage=error.usage,
                                           usage_final=error.usage_status == 'known') from None
                    raise
                try:
                    stop()
                except JobCancelled as cancelled:
                    raise JobCancelled(str(cancelled), usage=turn.usage, usage_final=True) from None
                if time.monotonic() - started >= request_timeout:
                    raise deadline(turn.usage, final=True)
                completed = turn
                return turn
            stop()
            remaining = request_timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise deadline(observed)
            if time.monotonic() - last_progress >= progress_interval:
                report()
            await asyncio.wait({task}, timeout=min(.1, remaining, progress_interval))
    except JobCancelled as error:
        if error.usage is None:
            error.usage = observed
        failed = error
        raise
    except (httpx.TransportError, httpx.DecodingError) as error:
        try:
            stop()
        except JobCancelled as cancelled:
            failed = JobCancelled(str(cancelled), usage=observed)
            raise failed from None
        kind = transport_failure_kind(error)
        failed = ModelError(0, f'transport failed ({kind}); usage unconfirmed', usage=observed,
            retryable=False, response_diagnostic={'code': 'transport_error', 'transport_failure': kind},
            public_message='模型连接超时或中断，本次结果未完成；已保存内容和已收到用量保留，没有自动重试。')
        raise failed from None
    except ModelError as error:
        failed = error
        raise
    finally:
        if not task.done():
            task.cancel()
        # No background request survives stop, timeout or protocol failure.
        await asyncio.gather(task, return_exceptions=True)
        cancelled_after_close = False
        if failed is not None and not isinstance(failed, JobCancelled):
            try:
                stop()
            except JobCancelled as cancelled:
                failed = JobCancelled(str(cancelled), usage=failed.usage,
                                      usage_final=failed.usage_status == 'known')
                cancelled_after_close = True
        metadata['elapsed_ms'] = min(int((time.monotonic() - started) * 1000), 2**31 - 1)
        if failed is not None:
            prior = getattr(failed, 'response_diagnostic', None) or {}
            cancelled = isinstance(failed, JobCancelled)
            base = {'field': 'response', 'code': 'transport_error'}
            if isinstance(failed, ModelError) and 100 <= failed.status <= 599:
                metadata['provider_status'] = failed.status
                base['code'] = 'protocol_error' if failed.status == 200 else 'http_request_rejected'
            failure = {'transport_failure': 'cancelled'} if cancelled else {}
            failed.transport_diagnostic = safe_response_diagnostic({**metadata, **prior, **failure,
                'format': 'model_transport', 'code': 'transport_observed',
                'transport_outcome': 'cancelled' if cancelled else 'failed'}, tools=tools)
            if isinstance(failed, ModelError):
                failed.response_diagnostic = safe_response_diagnostic({**metadata, **base, **prior}, tools=tools)
        elif completed is not None:
            completed.transport_diagnostic = safe_response_diagnostic({**metadata, 'provider_status': 200,
                'format': 'model_transport', 'code': 'transport_observed', 'transport_outcome': 'completed'})
        if cancelled_after_close:
            raise failed from None
