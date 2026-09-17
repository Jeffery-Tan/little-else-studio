"""Stream parsing and the tool-use loop, with no network anywhere.

The SSE fixtures below are hand-written to the shape the endpoint actually
returns. They are the regression net for the transport: if the shim changes its
framing, these fail before anything gets billed.
"""
from __future__ import annotations

import pytest

from fakes import final_turn, sse, ScriptedModel, text_stream, tool_stream, tool_turn
from studio.agents.client import (
    LoopResult,
    ModelError,
    ToolCall,
    Turn,
    Usage,
    _parse_stream,
    run_agent_loop,
)


# ------------------------------------------------------------ stream parse -- #


def test_parses_a_plain_text_answer():
    turn = _parse_stream(text_stream("hello there"), "deepseek-flash")
    assert turn.text == "hello there"
    assert turn.stop_reason == "end_turn"
    assert turn.usage.input_tokens == 10
    assert turn.usage.output_tokens == 5
    assert turn.tool_calls == []
    assert turn.model == "deepseek-flash"


def test_text_blocks_are_not_echoed_back_empty():
    """Regression: deltas must land in the block, not only in the flat field.

    Otherwise the next request sends blank assistant text blocks.
    """
    turn = _parse_stream(text_stream("hello there"), "deepseek-flash")
    assert turn.raw_blocks == [{"type": "text", "text": "hello there"}]


def test_thinking_deltas_are_kept_separate_from_the_answer():
    lines = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "weighing options"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig-abc"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "the answer"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    )
    turn = _parse_stream(lines, "deepseek-flash")
    assert turn.thinking == "weighing options"
    assert turn.text == "the answer"
    assert turn.raw_blocks[0]["signature"] == "sig-abc"


def test_parses_a_tool_call_split_across_fragments():
    turn = _parse_stream(
        tool_stream("toolu_1", "read_project_state", '{"stage": "intake"}', fragments=4),
        "deepseek-flash",
    )
    assert turn.stop_reason == "tool_use"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "read_project_state"
    assert call.input == {"stage": "intake"}


def test_unparsable_tool_input_is_flagged_not_treated_as_empty_args():
    with pytest.raises(ModelError, match="incomplete tool input"):
        _parse_stream(tool_stream("toolu_2", "submit_stage", "{not json"), "deepseek-flash")


def test_unknown_event_fields_are_ignored_rather_than_crashing():
    lines = text_stream("ok")
    lines.insert(1, "event: content_block_delta")
    lines.insert(2, ": a comment line")
    lines.append("data: [DONE]")
    turn = _parse_stream(lines, "deepseek-flash")
    assert turn.text == "ok"


def test_stream_level_error_is_raised():
    from studio.agents.client import ModelError

    lines = sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    with pytest.raises(ModelError):
        _parse_stream(lines, "deepseek-flash")


# ------------------------------------------------------------------- loop -- #


def test_a_plain_answer_finishes_in_one_round():
    model = ScriptedModel([final_turn("nothing to do")])
    seen: list[Turn] = []
    result = run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "hi"}],
        tools=[], execute_tool=lambda n, a: "", on_turn=seen.append,
    )
    assert result.stopped_because == "done"
    assert result.rounds == 1
    assert result.text == "nothing to do"
    assert len(seen) == 1


def test_tool_result_is_fed_back_and_the_loop_continues():
    model = ScriptedModel([
        tool_turn("t1", "read_project_state", {"stage": "intake"}),
        final_turn("intake has no work yet"),
    ])
    executed: list[tuple[str, dict]] = []

    def execute(name, args):
        executed.append((name, args))
        return "intake: status=empty"

    result = run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "status?"}],
        tools=[{"name": "read_project_state"}], execute_tool=execute,
    )
    assert executed == [("read_project_state", {"stage": "intake"})]
    assert result.rounds == 2
    assert result.text == "intake has no work yet"

    # The second request must carry the assistant turn and a matching tool_result.
    second = model.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user"
    block = second[-1]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "t1"
    assert block["is_error"] is False
    assert "status=empty" in block["content"]


def test_every_round_is_reported_so_the_ledger_sees_each_call():
    model = ScriptedModel([
        tool_turn("t1", "a", {}),
        tool_turn("t2", "b", {}),
        final_turn("done"),
    ])
    turns: list[Turn] = []
    run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "a"}, {"name": "b"}], execute_tool=lambda n, a: "ok",
        on_turn=turns.append,
    )
    assert len(turns) == 3


def test_a_failing_tool_becomes_an_error_result_and_is_logged():
    model = ScriptedModel([tool_turn("t1", "submit_stage", {}), final_turn("I will retry")])

    def execute(name, args):
        raise ValueError("prereq: direction is not approved")

    result = run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "submit_stage"}], execute_tool=execute,
    )
    assert result.tool_log[0]["ok"] is False
    assert "direction is not approved" in result.tool_log[0]["result"]
    block = model.calls[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True
    assert "prereq" in block["content"]


def test_unparsable_input_never_reaches_the_executor():
    """An invalid tool message is not an executable assistant turn."""
    class InvalidToolModel:
        def complete(self, **kwargs):
            return _parse_stream(tool_stream("t1", "submit_stage", "{broken"), "deepseek-flash")

    executed = []
    with pytest.raises(ModelError, match="incomplete tool input"):
        run_agent_loop(
            InvalidToolModel(), model="deepseek-flash", system="s",
            messages=[{"role": "user", "content": "go"}],
            tools=[{"name": "submit_stage"}],
            execute_tool=lambda name, args: executed.append((name, args)),
        )
    assert executed == []


def test_a_truncated_turn_is_continued_rather_than_thrown_away():
    """Observed live on 2026-09-17: a director burned a whole attempt at the
    token cap and submitted nothing. One continuation is much cheaper."""
    truncated = Turn(
        text="方案 A 的主张是……（写到一半）",
        stop_reason="max_tokens",
        usage=Usage(input_tokens=100, output_tokens=8192),
        raw_blocks=[
            {"type": "text", "text": "方案 A 的主张是……（写到一半）"},
            # A tool call that never finished parsing must not be replayed.
            {"type": "tool_use", "id": "broken", "name": "submit_stage",
             "input": {"__unparsable_input__": '{"content": "half'}},
        ],
    )
    model = ScriptedModel([
        truncated,
        tool_turn("t1", "submit_stage", {"content": "完整正文", "summary": "s"}),
        final_turn("done"),
    ])
    result = run_agent_loop(
        model, model="deepseek-flash", system="s",
        messages=[{"role": "user", "content": "写方向"}],
        tools=[{"name": "submit_stage"}], execute_tool=lambda n, a: "已提交",
    )

    assert result.stopped_because == "done"
    assert [t["tool"] for t in result.tool_log] == ["__continuation__", "submit_stage"]

    # The nudge went back to the model, and the broken tool_use was dropped.
    second = model.calls[1]["messages"]
    assert "被 token 上限截断" in second[-1]["content"]
    assistant_blocks = second[-2]["content"]
    assert all(b.get("type") != "tool_use" for b in assistant_blocks)


def test_only_one_continuation_then_it_gives_up_honestly():
    truncated = Turn(text="又断了", stop_reason="max_tokens",
                     raw_blocks=[{"type": "text", "text": "又断了"}])
    model = ScriptedModel([truncated, Turn(text="还是断", stop_reason="max_tokens",
                                           raw_blocks=[{"type": "text", "text": "还是断"}])])
    result = run_agent_loop(
        model, model="deepseek-flash", system="s",
        messages=[{"role": "user", "content": "go"}], tools=[],
        execute_tool=lambda n, a: "", max_continuations=1,
    )
    assert result.stopped_because == "max_tokens"
    assert len(model.calls) == 2


def test_the_round_cap_stops_a_looping_agent():
    model = ScriptedModel([tool_turn(f"t{i}", "spin", {}) for i in range(10)])
    result = run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "spin"}], execute_tool=lambda n, a: "again", max_rounds=3,
    )
    assert result.stopped_because == "max_rounds"
    assert result.rounds == 3
    assert len(result.tool_log) == 3


def test_parallel_tool_calls_in_one_turn_all_execute():
    """Unverified against the live shim, but the loop must handle the shape."""
    turn = Turn(
        stop_reason="tool_use",
        tool_calls=[
            ToolCall(id="a", name="read_project_state", input={}),
            ToolCall(id="b", name="list_artifacts", input={"stage": "intake"}),
        ],
        raw_blocks=[
            {"type": "tool_use", "id": "a", "name": "read_project_state", "input": {}},
            {"type": "tool_use", "id": "b", "name": "list_artifacts", "input": {"stage": "intake"}},
        ],
    )
    model = ScriptedModel([turn, final_turn("both done")])
    result: LoopResult = run_agent_loop(
        model, model="deepseek-flash", system="s", messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "read_project_state"}, {"name": "list_artifacts"}],
        execute_tool=lambda n, a: f"{n} ok",
    )
    assert [t["tool"] for t in result.tool_log] == ["read_project_state", "list_artifacts"]
    assert len(model.calls[1]["messages"][-1]["content"]) == 2
