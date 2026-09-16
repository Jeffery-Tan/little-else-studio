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
#: Statuses worth one retry: the request never produced tokens, so nothing was billed.
RETRYABLE = {429, 500, 502, 503, 504}


class ModelError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"model endpoint returned {status}: {message}")
        self.status = status
        self.message = message


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


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
        self.base_url = base_url.rstrip("/")
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
        url = f"{self.base_url}/v1/messages"
        payload = self._payload(model, system, messages, tools)

        last: Exception | None = None
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    with client.stream("POST", url, headers=self._headers(), json=payload) as resp:
                        if resp.status_code != 200:
                            body = resp.read().decode("utf-8", errors="replace")[:600]
                            if resp.status_code in RETRYABLE and attempt == 1:
                                last = ModelError(resp.status_code, body)
                                continue
                            raise ModelError(resp.status_code, body)
                        return _parse_stream(resp.iter_lines(), model)
            except httpx.TransportError as exc:
                # Connection-level failure: safe to retry, no tokens produced.
                last = ModelError(0, f"{type(exc).__name__}: {exc}")
                if attempt == 1:
                    continue
                raise last from exc
        raise last or ModelError(0, "request failed without a response")


def _parse_stream(lines: Iterable[str], model: str) -> Turn:
    """Assemble a Turn from an SSE byte stream of ``data:`` lines."""
    turn = Turn(model=model)
    blocks: dict[int, dict[str, Any]] = {}
    partial_json: dict[int, str] = {}
    order: list[int] = []

    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            # 'event:' and other fields are redundant here; the payload's own
            # 'type' is authoritative and is what we switch on.
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            turn.model = message.get("model") or model
            usage = message.get("usage") or {}
            turn.usage.input_tokens = int(usage.get("input_tokens") or 0)
            turn.usage.output_tokens = int(usage.get("output_tokens") or 0)
        elif kind == "content_block_start":
            idx = int(event.get("index", len(order)))
            block = dict(event.get("content_block") or {})
            blocks[idx] = block
            order.append(idx)
            if block.get("type") == "tool_use":
                partial_json[idx] = ""
        elif kind == "content_block_delta":
            idx = int(event.get("index", 0))
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            # Deltas are accumulated into the block *and* into the flat fields.
            # The block is what gets echoed back as the assistant message; if it
            # stayed empty, the next request would carry blank text blocks.
            if dtype == "text_delta":
                text = delta.get("text") or ""
                turn.text += text
                blocks.setdefault(idx, {"type": "text", "text": ""})
                blocks[idx]["text"] = blocks[idx].get("text", "") + text
                if idx not in order:
                    order.append(idx)
            elif dtype == "thinking_delta":
                thought = delta.get("thinking") or ""
                turn.thinking += thought
                blocks.setdefault(idx, {"type": "thinking", "thinking": ""})
                blocks[idx]["thinking"] = blocks[idx].get("thinking", "") + thought
                if idx not in order:
                    order.append(idx)
            elif dtype == "input_json_delta":
                partial_json[idx] = partial_json.get(idx, "") + (delta.get("partial_json") or "")
            elif dtype == "signature_delta":
                if idx in blocks:
                    blocks[idx]["signature"] = delta.get("signature") or ""
        elif kind == "content_block_stop":
            idx = int(event.get("index", 0))
            if idx in blocks and blocks[idx].get("type") == "tool_use":
                raw_json = partial_json.get(idx, "")
                try:
                    blocks[idx]["input"] = json.loads(raw_json) if raw_json.strip() else {}
                except json.JSONDecodeError:
                    # Keep the unparsable text so the caller can report it
                    # rather than pretending the tool was called with {}.
                    blocks[idx]["input"] = {}
                    blocks[idx]["_input_error"] = raw_json[:500]
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                turn.stop_reason = delta["stop_reason"]
            usage = event.get("usage") or {}
            if usage.get("output_tokens") is not None:
                turn.usage.output_tokens = int(usage["output_tokens"])
            if usage.get("input_tokens") is not None:
                turn.usage.input_tokens = int(usage["input_tokens"])
        elif kind == "error":
            err = event.get("error") or {}
            raise ModelError(200, f"stream error: {err.get('type')}: {err.get('message')}")

    for idx in sorted(order):
        block = blocks[idx]
        if block.get("type") == "tool_use":
            if "_input_error" in block:
                turn.tool_calls.append(
                    ToolCall(id=block.get("id", ""), name=block.get("name", ""),
                             input={"__unparsable_input__": block["_input_error"]})
                )
                continue
            turn.tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    input=dict(block.get("input") or {}),
                )
            )
        turn.raw_blocks.append(block)

    if not turn.raw_blocks and turn.text:
        turn.raw_blocks = [{"type": "text", "text": turn.text}]
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
            try:
                output = execute_tool(call.name, call.input)
                is_error = False
            except BudgetExceeded:
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
