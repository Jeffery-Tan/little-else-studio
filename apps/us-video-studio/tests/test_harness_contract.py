"""Business completion and capability boundaries, with no provider requests."""
import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.client import ToolCall, Turn, run_agent_loop
from studio.agents.orchestrator import Orchestrator
from studio.agents.reviewer import run_review
from studio.config import Settings
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher
from studio.tools.execution import ToolResult


def loop(model, execute, names):
    return run_agent_loop(model, model="fixture", system="", messages=[],
                          tools=[{"name": name} for name in names], execute_tool=execute)


def test_unavailable_tool_never_reaches_handler_even_if_handler_would_accept_it():
    model = ScriptedModel([tool_turn("one", "approve_stage", {}), final_turn("Cannot approve")])
    executed = []
    result = loop(model, lambda *args: executed.append(args), ["read_artifact"])
    assert executed == []
    assert result.tool_log[0]["ok"] is False
    assert model.calls[1]["messages"][-1]["content"][0]["is_error"] is True


@pytest.mark.parametrize("arguments", [None, [], "bad", 3])
def test_non_object_arguments_do_not_reach_handler(arguments):
    model = ScriptedModel([tool_turn("one", "submit_stage", arguments), final_turn("Repair needed")])
    executed = []
    result = loop(model, lambda *args: executed.append(args), ["submit_stage"])
    assert executed == []
    assert "JSON object" in result.tool_log[0]["result"]


def test_completion_pairs_every_call_and_skips_remaining_writes_without_another_model_call():
    calls = [ToolCall("save", "submit_stage", {}), ToolCall("late", "submit_stage", {})]
    model = ScriptedModel([Turn(tool_calls=calls, stop_reason="tool_use", raw_blocks=[
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls])])
    executed = []

    def execute(name, arguments):
        executed.append(name)
        return ToolResult("saved version", concludes_turn=True)

    result = loop(model, execute, ["submit_stage"])
    assert executed == ["submit_stage"]
    assert len(model.calls) == 1
    assert result.text == "" and result.stopped_because == "done"
    results = result.messages[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["save", "late"]
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True and "Not executed" in results[1]["content"]
    assert len(result.tool_log) == 2


def test_failed_submission_can_be_repaired_and_cannot_conclude_successfully():
    model = ScriptedModel([tool_turn("bad", "submit_stage", {}), tool_turn("fixed", "submit_stage", {})])
    outcomes = iter([ToolResult("missing evidence", is_error=True), ToolResult("saved", concludes_turn=True)])
    result = loop(model, lambda *_: next(outcomes), ["submit_stage"])
    assert [r["ok"] for r in result.tool_log] == [False, True]
    assert model.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    with pytest.raises(ValueError, match="failed tool"):
        ToolResult("failed", is_error=True, concludes_turn=True)


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path)
    store.create_run("harness-test", "Synthetic US video", "Offline fixture only")
    pipeline = Pipeline(store)
    settings = Settings(data_root=tmp_path, base_url="https://example.invalid")
    ledger = Ledger(store, settings)
    attempt = pipeline.start_attempt("harness-test", "intake")
    pipeline.submit("harness-test", "intake", attempt.token, content="Known fixture facts", summary="facts")
    return store, pipeline, settings, ledger


def test_director_commit_is_success_even_when_no_further_response_is_available(project):
    store, pipeline, settings, ledger = project
    model = ScriptedModel([tool_turn("save", "submit_stage", {"content": "Two fixture directions", "summary": "Two options"})])
    result = Dispatcher(store, pipeline, settings, model, ledger, "harness-test").dispatch("direction", "Compare")
    assert result.submitted == "direction-v1" and result.error == ""
    assert result.summary == "Two options"
    assert len(model.calls) == len(ledger.entries("harness-test")) == 1
    assert pipeline.stage_state("harness-test", "direction").status == "awaiting_decision"
    assert store.list_approvals("harness-test") == []


@pytest.mark.parametrize("verdict", ["pass", "return"])
def test_review_commit_ends_role_and_reuses_same_verdict_without_charge(project, verdict):
    store, pipeline, settings, ledger = project
    attempt = pipeline.start_attempt("harness-test", "direction")
    pipeline.submit("harness-test", "direction", attempt.token, content="Fixture direction", summary="direction")
    findings = [] if verdict == "pass" else [{"stage": "direction", "kind": "content", "issue": "Options are identical"}]
    model = ScriptedModel([tool_turn("review", "finish_review", {"verdict": verdict, "findings": findings})])
    for _ in range(2):
        result = run_review(store=store, pipeline=pipeline, settings=settings,
                            client=model, ledger=ledger, run_id="harness-test", stage="direction")
        assert result.verdict == verdict and result.error == ""
        assert result.findings == findings
    assert len(model.calls) == len(ledger.entries("harness-test")) == 1
    assert store.list_approvals("harness-test") == []


@pytest.mark.parametrize("tool_name", ["dispatch"])
def test_incomplete_work_is_visible_as_failed_tool_to_main_agent(project, tool_name):
    store, pipeline, settings, ledger = project
    if tool_name == "request_review":
        attempt = pipeline.start_attempt("harness-test", "direction")
        pipeline.submit("harness-test", "direction", attempt.token, content="Direction", summary="direction")
    model = ScriptedModel([
        tool_turn("route", "prepare_creative_context", {"intent": "edit", "format": "video"}),
        tool_turn("work", tool_name, {"stage": "direction"}),
        final_turn("No committed result"),  # Child ends without submitting a result.
        final_turn("This work has not completed."),
    ])
    result = Orchestrator(store, pipeline, settings, model, ledger, "harness-test").send("Continue planning")
    assert len(model.calls) == 4  # The child actually ran and failed to commit.
    assert result.failed_tools[0]["tool"] == tool_name
    assert model.calls[-1]["messages"][-1]["content"][0]["is_error"] is True
    assert store.list_approvals("harness-test") == []
