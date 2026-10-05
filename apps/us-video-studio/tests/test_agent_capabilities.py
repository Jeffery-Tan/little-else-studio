"""Exercise capabilities through real role dispatch with offline model fixtures."""
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from studio.agents.orchestrator import Orchestrator
from studio.agents.reviewer import run_review
from studio.config import Settings
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.context import capabilities, conversation
from studio.tools.dispatch import Dispatcher
from studio.tools.registry import tool_names_for

RUN = "capability-fixture"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Offline capability fixture", "Synthetic evidence only")
    pipeline = Pipeline(store)
    settings = Settings(tmp_path, "https://example.invalid", max_tool_rounds=10)
    return store, pipeline, settings, Ledger(store, settings)


def submit(pipeline, stage, text):
    attempt = pipeline.start_attempt(RUN, stage)
    return pipeline.submit(RUN, stage, attempt.token, content=text, summary=stage)


def route(intent="edit"):
    return tool_turn("route-" + intent, "prepare_creative_context", {"intent": intent, "format": "video",
        "deliverable": "follow_up" if intent in {"status", "pause", "feedback"} else "intake"})


@pytest.mark.parametrize("intent", ["status", "pause"])
def test_readonly_scope_cannot_escalate_but_next_user_turn_can_resume(env, intent):
    store, pipeline, settings, ledger = env
    model = ScriptedModel([route(intent), route(),
        tool_turn("write", "write_stage", {"stage": "intake", "content": "forbidden", "summary": "s"}),
        final_turn("No work"), route(),
        tool_turn("save", "write_stage", {"stage": "intake", "content": "resumed", "summary": "s"}),
        final_turn("Saved")])
    main = Orchestrator(store, pipeline, settings, model, ledger, RUN)
    first = main.send("只处理当前反馈，不续做")
    assert [t["tool"] for t in first.failed_tools] == ["prepare_creative_context", "write_stage"]
    assert store.latest_artifact(RUN, "intake") is None
    second = main.send("现在继续整理需求")
    assert not second.failed_tools
    assert store.read_artifact_text(RUN, "intake", 1) == "resumed"
    assert len({e["turn_id"] for e in store.read_events(RUN) if e["kind"] == "agent_turn_scope_started"}) == 2


def test_a_feedback_scope_can_widen_when_the_message_is_actually_a_correction(env):
    """F6: one wrong intent choice must not lock the turn out of the user's request.

    ``status`` and ``pause`` stay locked above because they say the user does not
    want work done right now. ``feedback`` is different: a correction the user
    sent while a task was running arrives as feedback about the running work, and
    the whole point is that it gets handled. The lock belongs on the user's
    actual intent, not on an enumeration the model filled in once.
    """
    store, pipeline, settings, ledger = env
    model = ScriptedModel([route("feedback"), route("edit"),
        tool_turn("write", "write_stage", {"stage": "intake", "content": "handled", "summary": "s"}),
        final_turn("Handled")])
    main = Orchestrator(store, pipeline, settings, model, ledger, RUN)
    result = main.send("修改本轮需求，让表达自然一点")
    assert not result.failed_tools
    assert store.read_artifact_text(RUN, "intake", 1) == "handled"


def test_even_same_orchestrator_cannot_inherit_previous_production_scope(env):
    store, pipeline, settings, ledger = env
    baseline = submit(pipeline, "intake", "Existing fixture intake")
    text = "沿用当前需求，不重新写，先准备继续。"
    interpretation = fixture_task_interpretation(text, intent='continue', deliverable='intake')
    interpretation['reuse_artifact'] = {'stage': 'intake', 'version': baseline.version,
        'sha256': baseline.sha256, 'quote': text}
    model = ScriptedModel([
        tool_turn('route', 'prepare_creative_context', {'intent': 'continue', 'format': 'video',
            'deliverable': 'intake', 'task_interpretation': interpretation}), final_turn("Ready"),
        tool_turn("write", "write_stage", {"stage": "intake", "content": "forbidden", "summary": "s"}),
        final_turn("Only status")])
    main = Orchestrator(store, pipeline, settings, model, ledger, RUN)
    assert main.send(text).deliverable['status'] == 'complete'
    result = main.send("目前进度怎样")
    assert "本轮工作范围" in result.failed_tools[0]["result"]
    assert store.latest_artifact(RUN, "intake").sha256 == baseline.sha256


def test_new_video_request_does_not_inherit_old_photo_system_constraint(env):
    store, pipeline, settings, ledger = env
    store.append_event(RUN, "creative_context_selected", selection={
        "intent": "new", "format": "photo", "tags": [], "case_ids": None})
    model = ScriptedModel([route(),
        tool_turn("save", "write_stage", {"stage": "intake", "content": "Video fixture", "summary": "s"}),
        final_turn("Video work")])
    Orchestrator(store, pipeline, settings, model, ledger, RUN).send("现在改做视频")
    assert "本次交付明确为图文" not in model.calls[0]["system"]
    assert '"format": "video"' in model.calls[1]["messages"][-1]["content"][0]["content"]


def test_revision_receives_exact_previous_work_without_retired_review_findings(env):
    store, pipeline, settings, ledger = env
    submit(pipeline, "intake", "Known fixture facts")
    previous = submit(pipeline, "direction", "Preserve this unique prior direction")
    pipeline.record_review(RUN, "direction", verdict="return", findings=[
        {"stage": "direction", "kind": "content", "issue": "Repair this specific relationship"},
        {"stage": "writing", "kind": "content", "issue": "UNRELATED_WRITER_ISSUE"}])
    model = ScriptedModel([
        tool_turn("old", "read_artifact", {"stage": "direction", "version": 1}),
        tool_turn("wrong", "read_artifact", {"stage": "direction", "version": 2}),
        tool_turn("method", "read_method_document", {"name": "creative-package-contract"}),
        tool_turn("raw", "read_method_document", {"name": "../../private"}),
        tool_turn("save", "submit_stage", {"content": "Repaired relationship", "summary": "fixed"})])
    result = Dispatcher(store, pipeline, settings, model, ledger, RUN).dispatch("direction", "Repair only the relationship")
    assert result.submitted == "direction-v2"
    seed = model.calls[0]["messages"][0]["content"]
    assert "Preserve this unique prior direction" in seed
    assert "Repair this specific relationship" not in seed
    assert "UNRELATED_WRITER_ISSUE" not in seed
    prepared = next(e for e in store.read_events(RUN) if e['kind'] == 'agent_task_prepared')
    assert previous.sha256 in [source['sha256'] for source in prepared['context']['sources']]
    assert model.calls[2]["messages"][-1]["content"][0]["is_error"] is True
    assert model.calls[4]["messages"][-1]["content"][0]["is_error"] is True
    artifact = store.latest_artifact(RUN, "direction")
    assert any(ref.startswith("learning/documents/") for ref in artifact.evidence)
    reviewer = ScriptedModel([tool_turn("review", "finish_review", {"verdict": "pass"})])
    run_review(store=store, pipeline=pipeline, settings=settings, client=reviewer,
               ledger=ledger, run_id=RUN, stage="direction")
    assert "Repair this specific relationship" not in reviewer.requests_text()
    assert "Preserve this unique prior direction" not in reviewer.requests_text()


def test_producer_seed_uses_one_copy_of_facts_and_does_not_anchor_fresh_direction(env):
    store, pipeline, settings, ledger = env
    intake = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', intake.token, content='', summary='Facts',
                    structured={'facts': [{'label': 'Evidence', 'value': 'UNIQUE_PRODUCT_FACT',
                                           'source': 'fixture'}]})
    submit(pipeline, 'direction', 'OLD_DIRECTION_REFERENCE')
    model = ScriptedModel([tool_turn('save', 'submit_stage',
                                     {'content': 'New concept', 'summary': 'New'})])
    Dispatcher(store, pipeline, settings, model, ledger, RUN).dispatch('direction', 'Explore again')
    seed = model.calls[0]['messages'][0]['content']
    assert seed.count('UNIQUE_PRODUCT_FACT') == 1
    assert 'OLD_DIRECTION_REFERENCE' in seed
    assert '不必沿用旧稿的创意或结构' in seed
    assert '未受影响的表达应保留' not in seed


def test_stale_review_is_not_revision_evidence(env):
    store, pipeline, settings, ledger = env
    submit(pipeline, "intake", "Old facts")
    submit(pipeline, "direction", "Old direction can still be edited")
    pipeline.record_review(RUN, "direction", verdict="return", findings=[
        {"stage": "direction", "kind": "content", "issue": "STALE_REVIEW_SENTINEL"}])
    submit(pipeline, "intake", "New facts change context")
    model = ScriptedModel([tool_turn("save", "submit_stage", {"content": "New direction", "summary": "new"})])
    result = Dispatcher(store, pipeline, settings, model, ledger, RUN).dispatch("direction", "Use new facts")
    assert result.submitted == "direction-v2"
    assert "Old direction can still be edited" in model.requests_text()
    assert "STALE_REVIEW_SENTINEL" not in model.requests_text()


def test_historical_review_is_not_sent_to_specialist_as_instruction(env):
    store, pipeline, settings, ledger = env
    submit(pipeline, "intake", "Facts")
    direction = submit(pipeline, "direction", "Chosen direction")
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
        decision="approved", expected_version=direction.version, expected_artifact_sha256=direction.sha256)
    previous = submit(pipeline, "material_plan", "Specific old material plan")
    writing = submit(pipeline, "writing", "Script depending on that plan")
    assert writing.inputs["material_plan"] == previous.sha256
    pipeline.record_review(RUN, "writing", verdict="return", findings=[
        {"stage": "material_plan", "kind": "evidence", "issue": "MISSING_SHOT_FOR_THIS_PLAN"},
        {"stage": "writing", "kind": "content", "issue": "WRITER_ONLY_LINE"}])
    model = ScriptedModel([tool_turn("save", "submit_stage", {"content": "Repaired plan", "summary": "fixed"})])
    result = Dispatcher(store, pipeline, settings, model, ledger, RUN).dispatch("material_plan", "Repair missing shot")
    assert result.submitted == "material_plan-v2"
    assert "MISSING_SHOT_FOR_THIS_PLAN" not in model.requests_text()
    assert "WRITER_ONLY_LINE" not in model.requests_text()
    event = next(e for e in store.read_events(RUN) if e["kind"] == "role_revision_context")
    assert event["artifact_sha256"] == previous.sha256


def test_tampered_revision_never_reaches_model_or_strands_attempt(env):
    store, pipeline, settings, ledger = env
    submit(pipeline, "intake", "Facts")
    submit(pipeline, "direction", "Prior")
    (store.run_dir(RUN) / "artifacts/direction-v1.md").write_text("tampered", encoding="utf-8")
    model = ScriptedModel([])
    result = Dispatcher(store, pipeline, settings, model, ledger, RUN).dispatch("direction", "Revise")
    assert result.error
    assert model.calls == []
    assert pipeline.stage_state(RUN, "direction").open_attempt is None


@pytest.mark.parametrize("role", ["orchestrator", "director", "material", "writer", "reviewer"])
def test_capabilities_match_actual_role_tools_and_observation_limits(env, role):
    value = capabilities(env[2], role)
    assert value["tools"] == list(tool_names_for(role))
    assert "read_method_document" in value["tools"]
    assert ("read_conversation_history" in value["tools"]) == (role == "orchestrator")
    assert value["image_analysis"]["agent_can_execute"] == (role == "orchestrator")
    assert ('observe_images' in value['tools']) == (role == 'orchestrator')
    assert not value["video_understanding"]["agent_can_execute"]
    assert not value["production"]["agent_can_approve_or_generate"]


def test_main_retrieves_old_full_conversation_without_diagnostic_or_cross_run_access(env):
    store, pipeline, settings, ledger = env
    text = "Original scoped preference. " * 1000
    store.append_chat(RUN, "user", text)
    store.append_chat(RUN, "system", "PRIVATE_DIAGNOSTIC")
    for index in range(26):
        store.append_chat(RUN, "assistant", f"later {index}")
    indexes, before = [], None
    while True:
        page = conversation(store, RUN, before=before, limit=10)
        indexes.extend(row["message_index"] for row in page["messages"])
        before = page["next_before"]
        if before is None:
            break
    assert len(indexes) == len(set(indexes)) == 27
    assert 0 in indexes and 1 not in indexes
    chunks, offset = [], 0
    while True:
        chunk = conversation(store, RUN, message_index=0, offset=offset)
        chunks.append(chunk["text"])
        offset = chunk["next_offset"]
        if offset is None:
            break
    assert "".join(chunks) == text
    with pytest.raises(ValueError, match="diagnostic"):
        conversation(store, RUN, message_index=1)
    model = ScriptedModel([
        tool_turn("read", "read_conversation_history", {"message_index": 0}),
        tool_turn("other", "read_conversation_history", {"run_id": "another-run"}),
        final_turn("Read old preference")])
    result = Orchestrator(store, pipeline, settings, model, ledger, RUN).send("查之前那句偏好")
    response = json.loads(model.calls[1]["messages"][-1]["content"][0]["content"])
    assert response["text"] == text[:8000] and response["next_offset"] == 8000
    assert result.failed_tools[0]["tool"] == "read_conversation_history"
    assert "PRIVATE_DIAGNOSTIC" not in model.requests_text()
