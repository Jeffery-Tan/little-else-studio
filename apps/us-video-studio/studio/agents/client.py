"""Model transport and the tool-use loop.

The transport speaks the Anthropic Messages wire format over SSE. It is written
against the *shape* the endpoint actually returns (verified by hand on
2026-09-17), not against a documented guarantee — this is a compatibility shim,
so the parser only consumes ``data:`` lines and classifies deltas by the
``content_block_start`` that opened them, rather than trusting event names.

The loop is deliberately small and boring. Everything interesting about this
application lives in the tools it is allowed to call, not in the loop.

Two properties the loop guarantees:

* every model call is reported through ``on_turn``, so the ledger sees each
  round of a multi-round turn — not just the last one;
* hitting the round cap is reported as ``stopped_because='max_rounds'`` rather
  than silently returning a half-finished turn.
"""
from __future__ import annotations

import copy
import hashlib
import json
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import httpx

from ..models import BudgetExceeded
from ..tools.execution import ToolResult, validate_call

ANTHROPIC_VERSION = "2023-06-01"

#: Generous on purpose. This endpoint returns a thinking block before every
#: answer and thinking tokens count as output, so an 8192 cap truncates a
#: document-writing role mid-sentence — observed on 2026-09-17, costing a
#: director attempt its whole turn.
DEFAULT_MAX_TOKENS = 16384


class ModelError(RuntimeError):
    def __init__(self, status: int, message: str, *, usage=None, provider_invoked: bool | None = None,
                 usage_final: bool = False, response_diagnostic=None, public_message: str | None = None,
                 retryable: bool = True):
        super().__init__(f"model endpoint returned {status}: {message}")
        self.status = status
        self.message = message
        self.provider_invoked = provider_invoked
        self.response_diagnostic = response_diagnostic
        self.transport_diagnostic = None
        self.public_message = public_message
        self.retryable = retryable
        # Only a transport that has not sent a model request may assert this.
        # HTTP errors/disconnects keep the default unknown invocation state.
        self.usage = Usage(0, 0) if provider_invoked is False else (usage or Usage(None, None))
        # A complete non-streaming response can have valid accounting even when
        # its content is invalid. Stream counters alone are not final evidence.
        confirmed = usage_final and all(type(v) is int and v >= 0 for v in
                                        (self.usage.input_tokens, self.usage.output_tokens))
        self.usage_status = ('known' if provider_invoked is False or confirmed else
            ('partial' if any(v is not None for v in (self.usage.input_tokens, self.usage.output_tokens)) else 'unknown'))


class JobCancelled(RuntimeError):
    """Cancellation observed at a safe model/tool boundary, never an API failure."""

    def __init__(self, message='', *, usage=None, usage_final=False):
        super().__init__(message)
        self.usage = usage
        self.usage_final = usage_final
        self.transport_diagnostic = None


class SafePreflightError(RuntimeError):
    """Non-retryable connection setup failure with safe user-facing text."""


class NonRetryableStageError(RuntimeError):
    """A stage protocol/setup failure must stop this turn before redispatch."""


class WriterRootClosureRejected(NonRetryableStageError):
    """Only an evidenced framing correction's business rejection is terminal."""


@dataclass
class Usage:
    input_tokens: int | None = 0
    output_tokens: int | None = 0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """One assistant response, fully assembled from the stream."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    #: Assistant content blocks, echoed back verbatim on the next request.
    raw_blocks: list[dict[str, Any]] = field(default_factory=list)
    #: Fixed schema paths and hashes only; raw assistant replay stays unchanged.
    response_normalizations: list[dict[str, Any]] = field(default_factory=list)
    #: Actual request options and bounded counters only, recorded under its call ID.
    transport_diagnostic: dict[str, Any] | None = None
    #: One source/execution framing pair; no tool may run before its audit is durable.
    writer_root_closure: Any | None = None


@dataclass(frozen=True)
class FinishingContext:
    """One read-only conclusion from exact saved sources, never another work loop."""

    system: str
    messages: list[dict[str, Any]]
    check_current: Callable[[], None]
    stage: str = 'writing'
    prepare: Callable[[], 'FinishingContext'] | None = None
    preparation_rounds: int = 0


class ModelClient(Protocol):
    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Turn: ...


# --------------------------------------------------------------------------- #
# Real transport
# --------------------------------------------------------------------------- #


class AnthropicCompatClient:
    """Streaming client for an Anthropic-compatible endpoint.

    The credential is held on the instance and never logged, never serialised,
    and never included in an exception message.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = 180.0,
        check_cancel=None,
        cancel_event=None,
        request_timeout: float = 600.0,
        progress_interval: float = 10.0,
    ):
        if not token:
            raise RuntimeError("no credential supplied to the model client")
        from ..connections import validate_base_url
        self.base_url = validate_base_url(base_url)
        self._token = token
        self.max_tokens = max_tokens
        self.timeout = timeout
        from .bounded_http import validate_wait
        validate_wait(request_timeout, progress_interval)
        self.check_cancel, self.cancel_event = check_cancel, cancel_event
        self.request_timeout, self.progress_interval = request_timeout, progress_interval

    # -- transport ---------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-api-key": self._token,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def _payload(
        self, model: str, system: str, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        return payload

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Turn:
        return self._complete(model=model, system=system, messages=messages, tools=tools)

    def complete_with_progress(self, *, model, system, messages, tools, on_progress=None):
        return self._complete(model=model, system=system, messages=messages, tools=tools, on_progress=on_progress)

    def _complete(self, *, model, system, messages, tools, on_progress=None):
        from .bounded_http import bounded_http, check_stopped
        check_stopped(self.check_cancel, self.cancel_event)
        url = self.base_url + ('/messages' if self.base_url.endswith('/v1') else '/v1/messages')
        payload = self._payload(model, system, messages, tools)

        # One invocation is exactly one HTTP attempt. Even connection failures
        # cannot prove that the provider did not start work; never auto-retry.
        async def exchange(model_data, model_usage):
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                    if resp.status_code != 200:
                        # Do not persist arbitrary response bodies: gateways can
                        # echo credentials or request content in errors.
                        raise ModelError(resp.status_code, "request rejected; usage unconfirmed", retryable=False)
                    return await _parse_async_stream(resp.aiter_lines(), model,
                        on_usage=model_usage, on_model_data=model_data)
        return asyncio.run(bounded_http(exchange, payload=payload, endpoint=url, provider='anthropic_compat',
            check_cancel=self.check_cancel, cancel_event=self.cancel_event,
            request_timeout=self.request_timeout, progress_interval=self.progress_interval, on_progress=on_progress))


_STREAM_EOF = object()


def _parse_stream(lines: Iterable[str], model: str) -> Turn:
    """Synchronous fixture/compatibility entry to the same incremental parser."""
    parser = _stream_parser(model)
    next(parser)
    try:
        try:
            for line in lines:
                parser.send(line)
            parser.send(_STREAM_EOF)
        except StopIteration as complete:
            return complete.value
        except ModelError:
            raise
        except Exception as error:
            # Preserve the synchronous iterator's already observed usage.
            parser.throw(error)
    finally:
        parser.close()


async def _parse_async_stream(lines, model, *, on_usage, on_model_data):
    parser = _stream_parser(model, on_usage=on_usage, on_model_data=on_model_data)
    next(parser)
    try:
        try:
            async for line in lines:
                parser.send(line)
            parser.send(_STREAM_EOF)
        except StopIteration as complete:
            return complete.value
        # HTTPX errors remain transport errors. The lifecycle owns cancellation
        # and retains incremental usage via on_usage before closing this parser.
    finally:
        parser.close()


def _stream_parser(model, *, on_usage=None, on_model_data=None):
    """Accept only a complete, closed Messages stream; preserve partial usage."""
    turn = Turn(model=model, usage=Usage(None, None))
    blocks: dict[int, dict[str, Any]] = {}
    partial_json: dict[int, str] = {}
    opened: set[int] = set()
    started = False
    stopped = False
    final_usage = False

    def fail(message: str):
        raise ModelError(200, message, usage=turn.usage)

    def update_usage(raw):
        for name in ("input_tokens", "output_tokens"):
            if name in raw:
                value = raw[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    fail("invalid usage in stream")
                setattr(turn.usage, name, value)
                if on_usage:
                    on_usage(turn.usage)

    try:
        while True:
            raw = yield
            if raw is _STREAM_EOF:
                break
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                fail("malformed stream event")
            if not isinstance(event, dict):
                fail("invalid stream event")
            kind = event.get("type")
            if stopped:
                fail("stream continued after message_stop")
            if kind == "ping":
                continue
            if kind == "error":
                fail("provider reported a stream error")
            if kind == "message_start":
                if started:
                    fail("duplicate message_start")
                started = True
                message = event.get("message") or {}
                turn.model = message.get("model") or model
                update_usage(message.get("usage") or {})
                continue
            if not started:
                fail("stream event before message_start")
            if kind == "content_block_start":
                idx = event.get("index")
                if not isinstance(idx, int) or idx in blocks:
                    fail("invalid or duplicate content block")
                block = dict(event.get("content_block") or {})
                blocks[idx] = block
                opened.add(idx)
                if block.get("type") == "tool_use":
                    partial_json[idx] = ""
                elif block.get("type") == "text":
                    turn.text += block.get("text", "")
                    if block.get("text") and on_model_data:
                        on_model_data()
                elif block.get("type") == "thinking":
                    turn.thinking += block.get("thinking", "")
                    if block.get("thinking") and on_model_data:
                        on_model_data()
            elif kind == "content_block_delta":
                idx = event.get("index")
                if idx not in opened:
                    fail("delta for unopened or closed block")
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                block = blocks[idx]
                if dtype == "text_delta" and block.get("type") == "text":
                    text = delta.get("text") or ""
                    turn.text += text
                    block["text"] = block.get("text", "") + text
                    if text and on_model_data:
                        on_model_data()
                elif dtype == "thinking_delta" and block.get("type") == "thinking":
                    thought = delta.get("thinking") or ""
                    turn.thinking += thought
                    block["thinking"] = block.get("thinking", "") + thought
                    if thought and on_model_data:
                        on_model_data()
                elif dtype == "input_json_delta" and block.get("type") == "tool_use":
                    partial_json[idx] += delta.get("partial_json") or ""
                    if delta.get("partial_json") and on_model_data:
                        on_model_data()
                elif dtype == "signature_delta" and block.get("type") == "thinking":
                    block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
                else:
                    fail("unsupported or mismatched content delta")
            elif kind == "content_block_stop":
                idx = event.get("index")
                if idx not in opened:
                    fail("stop for unopened block")
                opened.remove(idx)
                if blocks[idx].get("type") == "tool_use":
                    raw_json = partial_json[idx]
                    try:
                        value = json.loads(raw_json) if raw_json.strip() else blocks[idx].get("input", {})
                    except json.JSONDecodeError:
                        fail("incomplete tool input")
                    if not isinstance(value, dict):
                        fail("tool input must be an object")
                    blocks[idx]["input"] = value
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                if delta.get("stop_reason"):
                    turn.stop_reason = delta["stop_reason"]
                usage = event.get("usage") or {}
                update_usage(usage)
                final_usage = final_usage or "output_tokens" in usage
            elif kind == "message_stop":
                if opened or turn.stop_reason not in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
                    fail("incomplete message termination")
                stopped = True
                break
            else:
                fail("unsupported stream event")
    except ModelError:
        raise
    except Exception as exc:
        raise ModelError(200, f"stream interrupted ({type(exc).__name__})", usage=turn.usage) from exc
    if not started or not stopped:
        fail("stream ended before message_stop")
    if not final_usage or turn.usage.input_tokens is None or turn.usage.output_tokens is None:
        fail("complete usage missing from stream")
    for idx in sorted(blocks):
        block = blocks[idx]
        if block.get("type") == "tool_use":
            turn.tool_calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                                            input=dict(block.get("input") or {})))
        turn.raw_blocks.append(block)
    if (turn.stop_reason == "tool_use") != bool(turn.tool_calls):
        fail("tool calls and stop reason disagree")
    return turn


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


@dataclass
class LoopResult:
    text: str
    messages: list[dict[str, Any]]
    rounds: int
    stopped_because: str
    tool_log: list[dict[str, Any]] = field(default_factory=list)


#: The same tool failing the same way this many times cannot succeed by
#: repeating the identical call. At the limit the model is told to change
#: method (or report honestly); beyond it the loop stops with
#: ``stopped_because='repeated_failure'``. BudgetExceeded below is the
#: precedent — it ends the loop instead of becoming retryable fodder; this
#: generalizes that to every deterministic failure. Observed live 2026-09-26:
#: one rejected submit_patch burned every round while completion_check kept
#: saying "keep going" — zero progress for twenty minutes.
REPEATED_FAILURE_LIMIT = 3
FAILURE_ADVISORY = (
    "这是同一工具、同一错误的第 {n} 次失败——停止原样重试。"
    "换一种方法才继续（不同的输入、或拆成更小的步骤）；"
    "如果换不了方法，现在就收工：按三段给 Jeffery 汇报——"
    "结论、没成的事、需要他决定的——把卡住的原因写清楚，"
    "不要继续重复同一调用。"
)


_FAILURE_METADATA_KEYS = frozenset({
    'task_id', 'attempt_id', 'attempt_number', 'call_id', 'request_id', 'job_id',
    'timestamp', 'at', 'elapsed_seconds', 'nonce', 'token',
})


def _failure_key(output: str) -> str:
    """Identify a failure independently of its execution receipt identity.

    A dispatch creates a new attempt even if it fails for the same reason. Its
    task_id must not reset the convergence guard. Only structured transport
    metadata is excluded: plain-text errors and business values (shot, version,
    duration, numeric limits, hashes) remain exact. Compute before log truncation.
    """
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in _FAILURE_METADATA_KEYS
                    and not (key in {'attempt', 'call'} and isinstance(item, (str, int)))}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and {'stage', 'role', 'status', 'task_id'} <= payload.keys():
        # Dispatch receipts also contain summaries, read-more links and changing
        # progress. Their stable failure contract is the error + role/stage.
        payload = {key: payload.get(key) for key in
                   ('stage', 'role', 'status', 'error', 'stop_reason', 'artifact')}
    if isinstance(payload, dict) and any(key in payload for key in ('error', 'code', 'failure_key')):
        identity = json.dumps(stable(payload), ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'))
    else:
        identity = output
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def prepare_strict_writer_execution(turn: Turn, tools: list[dict[str, Any]]) -> Turn:
    """Validate original wire arguments before decoding a detached execution batch.

    Provider parsers may already have decoded these sentinels. Rechecking their
    original replay blocks keeps this boundary provider-independent and idempotent.
    Logical/history handlers still enforce scope, quotes and permissions.
    """
    from .writer_root_closure import verify_root_closure
    verify_root_closure(turn, tools)
    from ..tools.registry import is_strict_writer_wire_schema, strict_writer_execution_arguments
    schemas = {tool['name']: tool['input_schema'] for tool in tools
               if tool.get('strict') is True
               and is_strict_writer_wire_schema(tool['name'], tool['input_schema'])}
    if not schemas:
        return turn
    from .deepseek_client import _strict_schema_issue
    from .requests import safe_response_diagnostic
    replacements = []
    for index, call in enumerate(turn.tool_calls):
        if call.name not in schemas:
            continue
        original = next((block['input'] for block in turn.raw_blocks
                         if block.get('type') == 'tool_use' and block.get('id') == call.id
                         and block.get('name') == call.name and 'input' in block), call.input)
        issue = _strict_schema_issue(original, schemas[call.name])
        if issue:
            diagnostic = safe_response_diagnostic({
                'field': 'tool_arguments', 'code': 'invalid_arguments_schema',
                'tool_call_index': index, **issue})
            raise ModelError(200, 'invalid strict writer wire arguments; no tools dispatched',
                usage=turn.usage, usage_final=True, response_diagnostic=diagnostic,
                public_message='模型返回的工具参数不符合本次工具格式，本轮未执行返回工具。'
                               '已保留调用用量，不会自动重试。', retryable=False)
        replacements.append((call, strict_writer_execution_arguments(call.name, original, schemas[call.name])))
    # A later invalid call cannot leave an earlier call partially normalized or executed.
    for call, arguments in replacements:
        call.input = arguments
    return turn


def _request_tool_batch_error(turn: Turn, tools: list[dict[str, Any]], issue) -> ModelError:
    from .requests import safe_response_diagnostic
    return ModelError(200, 'invalid request tool manifest; no tools dispatched',
        usage=turn.usage, usage_final=True, response_diagnostic=safe_response_diagnostic(issue, tools=tools),
        public_message='模型返回的工具或参数不符合本次工作范围，本轮未执行返回工具。'
                       '已保留调用用量，不会自动重试。', retryable=False)


def _validate_tool_batch_identity(turn: Turn, tools: list[dict[str, Any]]) -> None:
    """Every role checks the whole batch before dispatch or parameter processing."""
    allowed = frozenset(tool['name'] for tool in tools)
    seen = set()
    for index, call in enumerate(turn.tool_calls):
        issue = None
        if not isinstance(call.name, str) or not call.name:
            issue = {'field': 'tool_call', 'code': 'invalid_identity', 'tool_call_index': index}
        elif call.name not in allowed:
            issue = {'field': 'tool_arguments', 'code': 'invalid_arguments_schema',
                     'schema_rule': 'unavailable_tool', 'tool_call_index': index}
        elif (not isinstance(call.id, str) or not call.id or call.id in seen
              or not isinstance(call.input, dict)):
            issue = {'field': 'tool_call', 'code': 'invalid_identity', 'tool_call_index': index}
        if issue:
            raise _request_tool_batch_error(turn, tools, issue)
        seen.add(call.id)


def _validate_request_tool_batch(turn: Turn, tools: list[dict[str, Any]]) -> None:
    """Check dynamic strict arguments after the shared identity/allowlist guard.

    Static writer requests retain their evidenced parameter-processing boundary;
    original blocks can legitimately precede the execution copy's empty fields.
    """
    from .deepseek_client import _strict_schemas, _strict_schema_issue
    schemas = _strict_schemas(tools)
    for index, call in enumerate(turn.tool_calls):
        if call.name in schemas:
            original = next((block['input'] for block in turn.raw_blocks
                if block.get('type') == 'tool_use' and block.get('id') == call.id
                and block.get('name') == call.name and 'input' in block), call.input)
            mismatch = _strict_schema_issue(original, schemas[call.name])
            if mismatch:
                issue = {'field': 'tool_arguments', 'code': 'invalid_arguments_schema',
                         'tool_call_index': index, 'tool_name': call.name, **mismatch}
                raise _request_tool_batch_error(turn, tools, issue)


def _final_reply_text(text: str, preserve_final_text=None):
    return text if preserve_final_text is not None and preserve_final_text() else text.strip()


def complete_finishing_turn(client: ModelClient, *, model: str, context: FinishingContext,
                            work_result: LoopResult, on_activity=None, on_turn=None,
                            check_cancel=None, preserve_final_text=None) -> LoopResult:
    """One tool-free call; no continuation, fallback, execution or second attempt."""
    if check_cancel:
        check_cancel()
    if context.prepare is not None:
        context = context.prepare()
    context.check_current()
    finishing_round = work_result.rounds + context.preparation_rounds + 1
    if on_activity:
        on_activity({'type': 'agent_activity', 'phase': 'finishing',
                     'stage': context.stage, 'round': finishing_round})
    previous_stage = getattr(client, 'stage', None)
    try:
        if hasattr(client, 'stage'):
            client.stage = context.stage
        final = client.complete(model=model, system=context.system,
                                messages=context.messages, tools=[])
    finally:
        if hasattr(client, 'stage'):
            client.stage = previous_stage
    if check_cancel:
        check_cancel()
    context.check_current()
    if final.tool_calls or final.stop_reason != 'end_turn' or not final.text.strip():
        raise ModelError(200, 'invalid read-only finishing response; no tools executed',
            usage=final.usage, usage_final=True, retryable=False,
            public_message='稿件已保存，但本轮收尾回答未完成；不会自动重试或修改稿件。')
    if on_turn:
        on_turn(final)
    return LoopResult(text=_final_reply_text(final.text, preserve_final_text),
                      messages=[*context.messages, {'role': 'assistant', 'content': final.raw_blocks or final.text}],
                      rounds=finishing_round, stopped_because='done', tool_log=work_result.tool_log)


def run_agent_loop(
    client: ModelClient,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str | ToolResult],
    max_rounds: int = 12,
    max_continuations: int = 1,
    on_turn: Callable[[Turn], None] | None = None,
    on_tool: Callable[[dict[str, Any]], None] | None = None,
    on_activity: Callable[[dict[str, Any]], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
    stop_when: Callable[[], bool] | None = None,
    completion_check: Callable[[], str | None] | None = None,
    session_journal=None,
    parallel_read_tools: frozenset[str] = frozenset(),
    pinned_user_input: str | None = None,
    preserve_initial_context: bool = True,
    finishing_context: Callable[[], FinishingContext | None] | None = None,
    tools_for_request: Callable[[], list[dict[str, Any]]] | None = None,
    system_for_request: Callable[[], str] | None = None,
    preserve_final_text: Callable[[], bool] | None = None,
    stage_checkpoint: Callable[[], str | None] | None = None,
    before_work_round: Callable[[], None] | None = None,
    settle_stage: Callable[[], None] | None = None,
) -> LoopResult:
    """Drive a tool-use conversation to completion.

    ``execute_tool`` returns text or a backend-owned ``ToolResult``. A successful
    concluding result ends this role after recording the batch; remaining calls
    are paired with explicit skips. Only tools in this request may execute.
    ``stop_when`` checks after every recorded tool result, including validation
    errors. A known prerequisite ends the batch without hiding its failures.
    Anything it raises is converted into an ``is_error`` tool result *and* recorded in
    ``tool_log`` — the failure is visible to the model and to the caller, never
    swallowed.

    The one exception is ``BudgetExceeded``, which ends the loop instead. See
    the comment on that handler: turning it into a tool result is what let the
    model retry against an exhausted budget.

    ``on_turn`` and ``on_tool`` are for observers (the ledger, the UI event
    stream). Neither can change what happens.
    """
    convo = list(messages)
    allowed_tools = frozenset(t["name"] for t in tools)
    tool_log: list[dict[str, Any]] = []
    text_seen: list[str] = []
    continuations_left = max_continuations
    completion_reminders = 1
    if hasattr(client, 'public_progress'):
        client.public_progress = on_activity

    round_index, phase_rounds = 0, 0
    completed_phases: set[str] = set()
    while phase_rounds < max_rounds:
        round_index += 1
        phase_rounds += 1
        if check_cancel is not None:
            check_cancel()
        if before_work_round is not None:
            before_work_round()
        request_tools = copy.deepcopy(tools_for_request() if tools_for_request is not None else tools)
        if (not isinstance(request_tools, list) or any(not isinstance(tool, dict)
                or not isinstance(tool.get('name'), str) or not tool['name'] for tool in request_tools)
                or len({tool['name'] for tool in request_tools}) != len(request_tools)):
            raise SafePreflightError('本次工具清单无法核对，没有发起模型请求。')
        allowed_tools = frozenset(tool['name'] for tool in request_tools)
        if session_journal is not None:
            session_journal.before_step(convo)
            session_journal.retain_current_input(convo)
            if 'read_role_history' in allowed_tools:
                session_journal.retain_history_index(convo)
        from ..context_projection import compact_message_history
        current_input = (session_journal.pinned['content'] if session_journal is not None
                         and session_journal.pinned else pinned_user_input)
        projected = compact_message_history(convo, pinned_user_input=current_input,
                                            preserve_initial=preserve_initial_context)
        convo = projected['messages']
        if session_journal is not None:
            session_journal.projection(projected['projection'])
            if 'read_role_history' in allowed_tools:
                session_journal.retain_history_index(convo)
        request_system = system_for_request() if system_for_request is not None else system
        if not isinstance(request_system, str):
            raise SafePreflightError('本次任务上下文无法核对，没有发起模型请求。')
        if on_activity is not None:
            on_activity({"type": "agent_activity", "phase": "thinking", "round": round_index})
        if session_journal is not None:
            session_journal.request(model, request_system, convo, request_tools)
        turn = client.complete(model=model, system=request_system, messages=convo, tools=copy.deepcopy(request_tools))
        _validate_tool_batch_identity(turn, request_tools)
        if tools_for_request is not None:
            _validate_request_tool_batch(turn, request_tools)
        prepare_strict_writer_execution(turn, request_tools)
        if turn.writer_root_closure is not None and not turn.writer_root_closure.evidence_saved:
            raise SafePreflightError('脚本格式处理证据尚未保存，本轮未执行返回工具。')
        if session_journal is not None:
            session_journal.response(turn)
        if on_turn is not None:
            on_turn(turn)
        # Cancellation may arrive while complete() is blocked. Its late final
        # text and tool batch belong to the old goal and must not be committed.
        if check_cancel is not None:
            check_cancel()
        if turn.tool_calls:
            # Operational narration belongs to the execution journal, not the
            # final answer (or the next conversational context window).
            text_seen.clear()
            if turn.text and on_activity is not None:
                on_activity({"type": "agent_narration", "text": turn.text, "round": round_index})
        elif turn.text:
            # Preserve continuation fragments of the final answer only.
            text_seen.append(turn.text)

        if not turn.tool_calls:
            if session_journal is not None and session_journal.has_pending():
                convo.append({'role': 'assistant', 'content': turn.text or '准备检查新消息。'})
                text_seen.clear()
                continue
            if turn.stop_reason == "max_tokens" and continuations_left > 0:
                # Truncated mid-thought. Picking up where it stopped is much
                # cheaper than discarding the call and starting the stage over.
                # Incomplete tool_use blocks are dropped rather than replayed —
                # their input never finished parsing.
                safe = [b for b in turn.raw_blocks if b.get("type") != "tool_use"]
                convo.append(
                    {"role": "assistant",
                     "content": safe or [{"type": "text", "text": turn.text}]}
                )
                convo.append({"role": "user", "content": (
                    "你的上一条输出被 token 上限截断了。从断掉的地方接着写，不要重来，"
                    "也不要重复已经写过的内容。写完后按原要求提交。"
                )})
                tool_log.append({"round": round_index, "tool": "__continuation__",
                                 "args": {}, "ok": True, "result": "truncated; asked to continue"})
                continuations_left -= 1
                continue
            incomplete = False
            # Always check the turn deliverable on end_turn — even after a
            # failed internal tool (e.g. request_review). Skipping here left the
            # model free to write a summary while the goal was still unfinished.
            if completion_check and turn.stop_reason == 'end_turn':
                reminder = completion_check()
                incomplete = bool(reminder)
                if reminder and completion_reminders and phase_rounds < max_rounds:
                    convo.append({'role': 'assistant', 'content': turn.text or '准备结束本轮。'})
                    convo.append({'role': 'user', 'content': reminder})
                    text_seen.clear()
                    completion_reminders -= 1
                    if on_activity:
                        on_activity({'type':'agent_activity','phase':'continuing','round':round_index})
                    continue
            return LoopResult(
                text=_final_reply_text("\n".join(text_seen), preserve_final_text),
                messages=convo,
                rounds=round_index,
                stopped_because="incomplete" if incomplete else "done" if turn.stop_reason != "max_tokens" else "max_tokens",
                tool_log=tool_log,
            )

        convo.append({"role": "assistant", "content": turn.raw_blocks})

        results: list[dict[str, Any]] = []
        concluded = False
        parallel_values = {}
        safe_reads = frozenset({'read_capabilities', 'read_artifact', 'list_materials'}) & parallel_read_tools
        if len(turn.tool_calls) > 1 and all(c.name in safe_reads for c in turn.tool_calls):
            from concurrent.futures import ThreadPoolExecutor
            from contextvars import copy_context
            def read(call):
                validate_call(call.name, call.input, allowed_tools)
                if check_cancel:
                    check_cancel()
                return execute_tool(call.name, call.input)
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='studio-read') as readers:
                futures = []
                for call in turn.tool_calls:
                    if session_journal is not None:
                        session_journal.tool_started(call)
                    futures.append(readers.submit(copy_context().run, read, call))
                for call, future in zip(turn.tool_calls, futures):
                    try:
                        parallel_values[call.id] = (True, future.result())
                    except Exception as exc:
                        parallel_values[call.id] = (False, exc)
        for call in turn.tool_calls:
            skipped = concluded
            if check_cancel is not None and not concluded:
                check_cancel()
            if on_activity is not None and not concluded:
                # Progress carries only a tool name and stage, never full model inputs.
                on_activity({"type": "agent_activity", "phase": "tool", "tool": call.name,
                             "args": {"stage": call.input.get("stage", "") if isinstance(call.input, dict) else ""}, "round": round_index})
            try:
                if concluded:
                    outcome = ToolResult("Not executed: this role's turn already concluded.", is_error=True)
                else:
                    validate_call(call.name, call.input, allowed_tools)
                    if call.id in parallel_values:
                        succeeded, value = parallel_values[call.id]
                        if not succeeded:
                            raise value
                    else:
                        if session_journal is not None:
                            session_journal.tool_started(call)
                        value = execute_tool(call.name, call.input)
                    outcome = value if isinstance(value, ToolResult) else ToolResult(value)
                output, is_error = outcome.content, outcome.is_error
                if not is_error:
                    concluded = concluded or outcome.concludes_turn
            except (BudgetExceeded, JobCancelled, SafePreflightError, NonRetryableStageError):
                # Deliberately *not* converted into a tool result. Doing so
                # handed the model the string "派工失败" and let it retry: each
                # retry is blocked pre-flight, so no money is lost, but every
                # one burns an orchestrator round until max_rounds runs out —
                # the model is never told to stop.
                #
                # An exhausted budget is a fact about the whole run, not a
                # failure of one tool, so it ends the loop. It propagates to
                # the orchestrator, which records it and re-raises; the server
                # turns that into a `blocked` event.
                raise
            except Exception as exc:  # surfaced, not hidden
                output = f"{type(exc).__name__}: {exc}"
                is_error = True
            if session_journal is not None:
                session_journal.tool_result(call, output, is_error, skipped)
            record = {"round": round_index, "tool": call.name,
                      "args": {} if turn.writer_root_closure is not None else call.input,
                      "ok": not is_error, "result": output[:2000]}
            if turn.writer_root_closure is not None:
                record['processing_method'] = 'writer_root_close_v1'
            if is_error and not skipped:
                record['failure_key'] = _failure_key(output)
            if skipped:
                record["skipped"] = True
            tool_log.append(record)
            if on_tool is not None:
                on_tool(record)
            if is_error and turn.writer_root_closure is not None:
                # The original and this business rejection are already in the
                # journal. Never turn a framing correction into another paid round.
                raise WriterRootClosureRejected('格式处理后的脚本提交未通过业务校验，本轮已停止；'
                                                 '已保存内容与用量保留，不会自动再调模型。')
            if not concluded and stop_when is not None:
                concluded = bool(stop_when())
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        # Non-convergence guard (see REPEATED_FAILURE_LIMIT): count identical
        # (tool, stable failure identity) pairs across the whole log — interleaved
        # successes do not reset a deterministic failure. At the limit, attach
        # ONE advisory telling the model to change method or report; if it does
        # the same thing anyway, stop the loop instead of feeding retries until
        # max_rounds. The transcript above is complete either way: the assistant
        # tool_use batch is paired with its results before we return.
        failures: dict[tuple[str, str], int] = {}
        for rec in tool_log:
            if not rec["ok"] and not rec.get("skipped"):
                key = (rec["tool"], rec.get('failure_key') or _failure_key(rec.get("result", "")))
                failures[key] = failures.get(key, 0) + 1
        if not concluded and any(count > REPEATED_FAILURE_LIMIT for count in failures.values()):
            convo.append({"role": "user", "content": results})
            return LoopResult(
                text=_final_reply_text("\n".join(text_seen), preserve_final_text),
                messages=convo,
                rounds=round_index,
                stopped_because="repeated_failure",
                tool_log=tool_log,
            )
        if any(count == REPEATED_FAILURE_LIMIT for count in failures.values()):
            results.append({"type": "text",
                            "text": FAILURE_ADVISORY.format(n=REPEATED_FAILURE_LIMIT)})
        convo.append({"role": "user", "content": results})
        if concluded:
            # Commit the terminal result and explicit skips for the remaining
            # batch before returning. No dangling tool_use blocks on resume.
            return LoopResult(text="", messages=convo, rounds=round_index,
                              stopped_because="done", tool_log=tool_log)
        if phase_rounds >= max_rounds and settle_stage is not None:
            settle_stage()
        if stage_checkpoint is not None:
            checkpoint = stage_checkpoint()
            # At most four distinct creative stages. Rewriting or polling the
            # same stage never renews its budget; root calls remain shared.
            if checkpoint is not None and checkpoint not in completed_phases and len(completed_phases) < 3:
                completed_phases.add(checkpoint)
                phase_rounds = 0
                completion_reminders = 1
        if finishing_context is not None:
            context = finishing_context()
            if context is not None:
                return complete_finishing_turn(client, model=model, context=context,
                    work_result=LoopResult(text='', messages=convo, rounds=round_index,
                                           stopped_because='work_complete', tool_log=tool_log),
                    on_activity=on_activity, on_turn=on_turn, check_cancel=check_cancel,
                    preserve_final_text=preserve_final_text)

    return LoopResult(
        text=_final_reply_text("\n".join(text_seen), preserve_final_text),
        messages=convo,
        rounds=round_index,
        stopped_because="max_rounds",
        tool_log=tool_log,
    )
