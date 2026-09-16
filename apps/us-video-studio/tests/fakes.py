"""Shared test doubles and SSE fixtures.

Nothing here touches the network. ``ScriptedModel`` records every request it
receives, which is how the tests assert on what an agent was *given* — the
isolation claims in the README are only worth anything if they are checked.
"""
from __future__ import annotations

import json

from studio.agents.client import LoopResult, ToolCall, Turn, Usage, _parse_stream


def sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


def text_stream(text: str, *, input_tokens=10, output_tokens=5) -> list[str]:
    return sse(
        {"type": "message_start",
         "message": {"model": "deepseek-flash",
                     "usage": {"input_tokens": input_tokens, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    )


def tool_stream(call_id: str, name: str, arguments: str, *, fragments: int = 1) -> list[str]:
    """A tool_use block whose input arrives as one or more JSON fragments."""
    size = max(1, -(-len(arguments) // fragments))
    pieces = [arguments[i:i + size] for i in range(0, len(arguments), size)]
    events = [
        {"type": "message_start",
         "message": {"model": "deepseek-flash",
                     "usage": {"input_tokens": 20, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
    ]
    for piece in pieces:
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": piece}})
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 15}},
        {"type": "message_stop"},
    ]
    return sse(*events)


class ScriptedModel:
    """Returns pre-built turns in order and records every request."""

    def __init__(self, turns: list[Turn]):
        self.turns = list(turns)
        self.calls: list[dict] = []

    def complete(self, *, model, system, messages, tools):
        self.calls.append(
            {"model": model, "system": system,
             "messages": json.loads(json.dumps(messages)),
             "tool_names": [t["name"] for t in tools]}
        )
        if not self.turns:
            raise AssertionError("scripted model ran out of turns")
        return self.turns.pop(0)

    def requests_text(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)


def tool_turn(call_id: str, name: str, args: dict, text: str = "") -> Turn:
    return Turn(
        text=text,
        tool_calls=[ToolCall(id=call_id, name=name, input=args)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=20, output_tokens=15),
        raw_blocks=(
            ([{"type": "text", "text": text}] if text else [])
            + [{"type": "tool_use", "id": call_id, "name": name, "input": args}]
        ),
    )


def final_turn(text: str) -> Turn:
    return Turn(text=text, stop_reason="end_turn",
                usage=Usage(input_tokens=30, output_tokens=8),
                raw_blocks=[{"type": "text", "text": text}])


def parse(lines: list[str], model: str = "deepseek-flash") -> Turn:
    return _parse_stream(lines, model)


__all__ = [
    "sse", "text_stream", "tool_stream", "ScriptedModel", "tool_turn",
    "final_turn", "parse", "LoopResult",
]
