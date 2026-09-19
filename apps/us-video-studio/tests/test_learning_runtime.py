"""Actual model-boundary tests; all models and libraries are isolated offline."""
import hashlib
import json
import re
from pathlib import Path
import shutil

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.orchestrator import Orchestrator
from studio.agents.reviewer import run_review
from studio.config import Settings, Pricing
from studio.learning import LearningService, SKILLS
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher

RUN = "learning-runtime"
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def runtime(tmp_path):
    repository = tmp_path / "repository"
    for skill in SKILLS:
        target = repository / ".agents/skills" / skill / "SKILL.md"
        target.parent.mkdir(parents=True)
        shutil.copyfile(REPO / ".agents/skills" / skill / "SKILL.md", target)
    library = repository / "tracks/us/creative/learning/library.json"
    library.parent.mkdir(parents=True)
    library.write_text(json.dumps({"schema_version": 1, "market": "US", "library_version": 1,
                                   "cases": [], "methods": []}), encoding="utf-8")
    store = Store(tmp_path / "data")
    store.create_run(RUN, "Independent creative runtime", "仅使用当前商品证据。")
    pipeline = Pipeline(store)
    settings = Settings(data_root=store.root, base_url="https://example.invalid", pricing=Pricing(source="offline test", as_of="2026-09-19",
                         input_usd_per_mtok=1, output_usd_per_mtok=2), max_tool_rounds=10)
    ledger = Ledger(store, settings)
    learning = LearningService(store, repository_root=repository)
    return store, pipeline, settings, ledger, learning


def orchestrator(env, model):
    store, pipeline, settings, ledger, learning = env
    return Orchestrator(store, pipeline, settings, model, ledger, RUN, learning_service=learning)


def submit(pipeline, stage, content="Current product evidence"):
    attempt = pipeline.start_attempt(RUN, stage)
    return pipeline.submit(RUN, stage, attempt.token, content=content, summary=stage)


def route(store, format="video", intent="new"):
    store.append_event(RUN, "creative_context_selected", selection={"intent": intent, "format": format,
                        "tags": [], "case_ids": None})


def skill_text(learning, name):
    return (learning.repository_root / ".agents/skills" / name / "SKILL.md").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("format,skill", [("video", "us-creator-video"), ("photo", "us-photo-copy")])
def test_full_applicable_skills_reach_role_and_snapshot_binds_artifact(runtime, format, skill):
    store, pipeline, settings, ledger, learning = runtime
    route(store, format)
    submit(pipeline, "intake")
    model = ScriptedModel([tool_turn("submit", "submit_stage", {"content": "方案正文", "summary": "s"}), final_turn("done")])
    outcome = Dispatcher(store, pipeline, settings, model, ledger, RUN, learning_service=learning).dispatch("direction", "比较方向")
    assert outcome.submitted == "direction-v1"
    assert skill_text(learning, skill) in model.calls[0]["system"]
    assert skill_text(learning, "us-commerce-strategy") in model.calls[0]["system"]
    artifact = store.latest_artifact(RUN, "direction")
    snapshot = json.loads((store.run_dir(RUN) / artifact.evidence[-1]).read_text(encoding="utf-8"))
    assert snapshot["role"] == "director"
    assert snapshot["format"] == format
    assert snapshot["library_sha256"] == hashlib.sha256(learning.library_path.read_bytes()).hexdigest()
    assert {s["id"] for s in snapshot["skills"]} == {skill, "us-commerce-strategy"}


def test_photo_writer_delivers_pages_and_caption_without_video_duration(runtime):
    store, pipeline, settings, ledger, learning = runtime
    route(store, "photo")
    submit(pipeline, "intake")
    direction = submit(pipeline, "direction")
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"), decision="approved",
                             expected_version=direction.version, expected_artifact_sha256=direction.sha256)
    submit(pipeline, "material_plan")
    model = ScriptedModel([tool_turn("submit", "submit_stage", {"content": "第1页：证据。\nCaption：带饭有自己的理由。", "summary": "图文"}), final_turn("done")])
    result = Dispatcher(store, pipeline, settings, model, ledger, RUN, learning_service=learning).dispatch("writing", "交付图文")
    assert result.submitted == "writing-v1"
    system = model.calls[0]["system"]
    assert skill_text(learning, "us-photo-copy") in system
    assert "写**带时码的完整脚本**" not in system
    assert "不要填 duration 或 total_duration" in system
    assert "Caption" in store.read_artifact_text(RUN, "writing", 1)


@pytest.mark.parametrize("intent", ["status", "pause", "feedback"])
def test_explicit_read_only_route_prevents_dispatch_and_survives_next_job(runtime, intent):
    store, pipeline, settings, ledger, learning = runtime
    submit(pipeline, "intake")
    model = ScriptedModel([
        tool_turn("route", "prepare_creative_context", {"intent": intent, "format": "none"}),
        tool_turn("dispatch", "dispatch", {"stage": "direction", "brief": "must not execute"}),
        tool_turn("write", "write_stage", {"stage": "intake", "content": "must not overwrite", "summary": "s"}),
        final_turn("只读取原状态。"),
    ])
    result = orchestrator(runtime, model).send("请先按我的意思处理，不要自动续做。")
    assert len(result.failed_tools) == 2
    assert len(model.calls) == 4
    assert store.latest_artifact(RUN, "direction") is None
    assert store.latest_artifact(RUN, "intake").version == 1
    next_model = ScriptedModel([tool_turn("dispatch", "dispatch", {"stage": "direction"}), final_turn("仍停留原状态。")])
    next_result = orchestrator(runtime, next_model).send("再看一下")
    assert next_result.failed_tools[0]["tool"] == "dispatch"
    assert len(next_model.calls) == 2


def test_main_explicit_photo_tool_returns_full_method_before_next_model_turn(runtime):
    model = ScriptedModel([
        tool_turn("prepare", "prepare_creative_context", {"intent": "new", "format": "photo", "tags": []}),
        tool_turn("write", "write_stage", {"stage": "intake", "content": "图文需求", "summary": "图文"}),
        final_turn("已整理"),
    ])
    orchestrator(runtime, model).send("我要图文策划与 caption。")
    learning = runtime[-1]
    assert skill_text(learning, "us-commerce-strategy") in model.calls[0]["system"]
    assert skill_text(learning, "us-photo-copy") in model.calls[1]["messages"][-1]["content"][0]["content"]
    artifact = runtime[0].latest_artifact(RUN, "intake")
    snap = json.loads((runtime[0].run_dir(RUN) / artifact.evidence[-1]).read_text(encoding="utf-8"))
    assert snap["format"] == "photo"


def test_feedback_is_pending_then_applied_feedback_enters_next_job_but_not_review(runtime):
    store, pipeline, settings, ledger, learning = runtime
    submit(pipeline, "intake")
    artifact = submit(pipeline, "direction")
    quote = "这版只修正角色关系，不代表采用整个方向。"
    arguments = {"stage": "direction", "version": artifact.version, "artifact_sha256": artifact.sha256,
                 "quote": quote, "scope": "persona_dialogue", "action": "diagnose"}
    before = learning.library_path.read_bytes()
    model = ScriptedModel([tool_turn("feedback", "propose_learning_feedback", arguments), final_turn("已提出待审差异。")])
    result = orchestrator(runtime, model).send(quote)
    assert not result.failed_tools
    assert learning.library_path.read_bytes() == before
    proposal = learning.proposals(RUN)[0]
    assert proposal["status"] == "pending"
    learning.apply(RUN, proposal["id"], proposal["base_library_sha256"])
    next_model = ScriptedModel([final_turn("收到局部反馈")])
    orchestrator(runtime, next_model).send("读取已保存的学习上下文。")
    assert quote in next_model.calls[0]["system"]
    review_model = ScriptedModel([tool_turn("review", "finish_review", {"verdict": "pass"}), final_turn("已审")])
    review = run_review(store=store, pipeline=pipeline, settings=settings, client=review_model, ledger=ledger,
                        run_id=RUN, stage="direction", learning_service=learning)
    assert review.verdict == "pass"
    assert quote not in review_model.requests_text()
    assert skill_text(learning, "us-creator-video") in review_model.calls[0]["system"]
    assert "propose_learning_feedback" not in review_model.calls[0]["tool_names"]


def test_feedback_tool_rejects_invented_quote_and_wrong_artifact(runtime):
    store, pipeline, settings, ledger, learning = runtime
    artifact = submit(pipeline, "intake")
    args = {"stage": "intake", "version": 1, "artifact_sha256": artifact.sha256,
            "quote": "用户没说过的认可", "scope": "direction"}
    model = ScriptedModel([tool_turn("false", "propose_learning_feedback", args),
                           tool_turn("wrong", "propose_learning_feedback", {**args, "quote": "真实反馈", "artifact_sha256": "0" * 64}),
                           final_turn("没有写入反馈")])
    result = orchestrator(runtime, model).send("真实反馈")
    assert len(result.failed_tools) == 2
    assert learning.proposals(RUN) == []
    assert all("apply" not in name for name in model.calls[0]["tool_names"])


def test_model_reads_contract_and_compiles_real_local_video_pack(runtime):
    store, pipeline, settings, ledger, learning = runtime
    artifact = submit(pipeline, "intake", "Observed product fact for this isolated test")
    contract = (REPO / "apps/us-video-studio/docs/creative-package-contract.md").read_text(encoding="utf-8-sig")
    manifest = json.loads(re.findall(r"```json\n(.*?)\n```", contract, re.S)[0])
    manifest["product_family_id"] = "offline-example"
    manifest["product_facts"][0].update(statement="Observed product fact for this isolated test",
        source=f"artifact:intake-v1#{artifact.sha256}")
    args = {"format": "video", "stage": "intake", "version": 1,
            "artifact_sha256": artifact.sha256, "manifest": manifest}
    model = ScriptedModel([
        tool_turn("read", "read_artifact", {"stage": "intake"}),
        tool_turn("contract", "read_method_document", {"name": "creative-package-contract"}),
        tool_turn("compile", "compile_creative_package", args), final_turn("编译包已保存，未生成媒体。"),
    ])
    result = orchestrator(runtime, model).send("把当前已保存策划编译成视频输入包。")
    assert not result.failed_tools
    assert artifact.sha256 in model.calls[1]["messages"][-1]["content"][0]["content"]
    method = json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])
    assert method["text"] == contract
    assert (store.run_dir(RUN) / method["evidence_ref"]).is_file()
    package = json.loads(model.calls[3]["messages"][-1]["content"][0]["content"])
    root = store.run_dir(RUN) / "creative-packages" / package["package_id"]
    compiled = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert compiled["generation_authorized"] is False
    assert compiled["references_approved"] is False
    assert package["artifact_sha256"] == artifact.sha256
    assert "Observed product fact" in (root / "prompt.txt").read_text(encoding="utf-8")


def test_photo_contract_example_compiles_through_agent_without_generating_images(runtime):
    from PIL import Image
    store, pipeline, settings, ledger, learning = runtime
    material = store.run_dir(RUN) / "materials" / "offline.png"
    material.parent.mkdir(exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(material)
    artifact = submit(pipeline, "intake", "Synthetic image fixture; no real product claims")
    contract = (REPO / "apps/us-video-studio/docs/creative-package-contract.md").read_text(encoding="utf-8-sig")
    manifest = json.loads(re.findall(r"```json\n(.*?)\n```", contract, re.S)[1])
    manifest["product"].update(product_family_id="offline-fixture", offer_id="offline-fixture-only", variant="fixture")
    manifest["product"]["sources"][0].update(path=material.name, sha256=hashlib.sha256(material.read_bytes()).hexdigest())
    manifest["publish_caption"] = "Offline fixture caption"
    for i, page in enumerate(manifest["pages"]):
        page["overlay_text"] = f"Offline page {i + 1}"
    model = ScriptedModel([
        tool_turn("route", "prepare_creative_context", {"intent": "new", "format": "photo"}),
        tool_turn("compile", "compile_creative_package", {"format": "photo", "stage": "intake", "version": 1,
            "artifact_sha256": artifact.sha256, "manifest": manifest}), final_turn("只有策划包。"),
    ])
    result = orchestrator(runtime, model).send("编译图文策划，不生成图片。")
    assert not result.failed_tools
    package = json.loads(model.calls[2]["messages"][-1]["content"][0]["content"])
    root = store.run_dir(RUN) / "creative-packages" / package["package_id"]
    pack = json.loads((root / "pack.json").read_text(encoding="utf-8"))
    assert pack["assessment"]["new_generation_calls"] == 0
    assert [page["status"] for page in pack["pages"]] == ["planned", "planned"]
    assert not list(root.glob("*.png"))


@pytest.mark.parametrize("manifest", [[], {"product_facts": [None]}, {"product_facts": [{"source": "../../outside"}]}])
def test_malformed_compile_inputs_are_tool_errors_and_do_not_crash_job(runtime, manifest):
    store, pipeline, settings, ledger, learning = runtime
    artifact = submit(pipeline, "intake")
    model = ScriptedModel([tool_turn("bad", "compile_creative_package", {
        "format": "video", "stage": "intake", "version": 1, "artifact_sha256": artifact.sha256,
        "manifest": manifest}), final_turn("输入不合格，保留原产物。")])
    result = orchestrator(runtime, model).send("校验这个待编译输入。")
    assert result.failed_tools[0]["tool"] == "compile_creative_package"
    assert len(model.calls) == 2
    assert not any(e["kind"] == "creative_package_created" for e in store.read_events(RUN))
