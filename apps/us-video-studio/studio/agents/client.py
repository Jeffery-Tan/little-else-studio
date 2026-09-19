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

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

import httpx

from ..models import BudgetExceeded

ANTHROPIC_VERSION = "2023-06-01"

#: Generous on purpose. This endpoint returns a thinking block before every
#: answer and thinking tokens count as output, so an 8192 cap truncates a
#: document-writing role mid-sentence — observed on 2026-09-17, costing a
#: director attempt its whole turn.
DEFAULT_MAX_TOKENS = 16384


class ModelError(RuntimeError):
    def __init__(self, status: int, message: str, *, usage=None, provider_invoked: bool | None = None):
        super().__init__(f"model endpoint returned {status}: {message}")
        self.status = status
        self.message = message
        self.provider_invoked = provider_invoked
        # Only a transport that has not sent a model request may assert this.
        # HTTP errors/disconnects keep the default unknown invocation state.
        self.usage = Usage(0, 0) if provider_invoked is False else (usage or Usage(None, None))
        self.usage_status = ('known' if provider_invoked is False else
            ('partial' if any(v is not None for v in (self.usage.input_tokens, self.usage.output_tokens)) else 'unknown'))


class JobCancelled(RuntimeError):
    """Cancellation observed at a safe model/tool boundary, never an API failure."""


class SafePreflightError(RuntimeError):
    """Non-retryable connection setup failure with safe user-facing text."""


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
    ):
        if not token:
            raise RuntimeError("no credential supplied to the model client")
        from ..connections import validate_base_url
        self.base_url = validate_base_url(base_url)
        self._token = token
        self.max_tokens = max_tokens
        self.timeout = timeout

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
        url = self.base_url + ('/messages' if self.base_url.endswith('/v1') else '/v1/messages')
        payload = self._payload(model, system, messages, tools)

        # One invocation is exactly one HTTP attempt. Even connection failures
        # cannot prove that the provider did not start work; never auto-retry.
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                    if resp.status_code != 200:
                        # Do not persist arbitrary response bodies: gateways can
                        # echo credentials or request content in errors.
                        raise ModelError(resp.status_code, "request rejected; usage unconfirmed")
                    return _parse_stream(resp.iter_lines(), model)
        except httpx.TransportError as exc:
            raise ModelError(0, f"transport failed ({type(exc).__name__}); usage unconfirmed") from exc


def _parse_stream(lines: Iterable[str], model: str) -> Turn:
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

    try:
        for raw in lines:
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
                elif block.get("type") == "thinking":
                    turn.thinking += block.get("thinking", "")
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
                elif dtype == "thinking_delta" and block.get("type") == "thinking":
                    thought = delta.get("thinking") or ""
                    turn.thinking += thought
                    block["thinking"] = block.get("thinking", "") + thought
                elif dtype == "input_json_delta" and block.get("type") == "tool_use":
                    partial_json[idx] += delta.get("partial_json") or ""
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


def run_agent_loop(
    client: ModelClient,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str],
    max_rounds: int = 12,
    max_continuations: int = 1,
    on_turn: Callable[[Turn], None] | None = None,
    on_tool: Callable[[dict[str, Any]], None] | None = None,
    check_cancel: Callable[[], None] | None = None,
) -> LoopResult:
    """Drive a tool-use conversation to completion.

    ``execute_tool`` returns the text to hand back to the model. Anything it
    raises is converted into an ``is_error`` tool result *and* recorded in
    ``tool_log`` — the failure is visible to the model and to the caller, never
    swallowed.

    The one exception is ``BudgetExceeded``, which ends the loop instead. See
    the comment on that handler: turning it into a tool result is what let the
    model retry against an exhausted budget.

    ``on_turn`` and ``on_tool`` are for observers (the ledger, the UI event
    stream). Neither can change what happens.
    """
    convo = list(messages)
    tool_log: list[dict[str, Any]] = []
    text_seen: list[str] = []
    continuations_left = max_continuations

    for round_index in range(1, max_rounds + 1):
        if check_cancel is not None:
            check_cancel()
        turn = client.complete(model=model, system=system, messages=convo, tools=tools)
        if on_turn is not None:
            on_turn(turn)
        if turn.text:
            text_seen.append(turn.text)

        if not turn.tool_calls:
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
            return LoopResult(
                text="\n".join(text_seen).strip(),
                messages=convo,
                rounds=round_index,
                stopped_because="done" if turn.stop_reason != "max_tokens" else "max_tokens",
                tool_log=tool_log,
            )

        convo.append({"role": "assistant", "content": turn.raw_blocks})

        results: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            if check_cancel is not None:
                check_cancel()
            try:
                output = execute_tool(call.name, call.input)
                is_error = False
            except (BudgetExceeded, JobCancelled, SafePreflightError):
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
            record = {"round": round_index, "tool": call.name, "args": call.input,
                      "ok": not is_error, "result": output[:2000]}
            tool_log.append(record)
            if on_tool is not None:
                on_tool(record)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": output,
                    "is_error": is_error,
                }
            )
        convo.append({"role": "user", "content": results})

    return LoopResult(
        text="\n".join(text_seen).strip(),
        messages=convo,
        rounds=max_rounds,
        stopped_because="max_rounds",
        tool_log=tool_log,
    )
