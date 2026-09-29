"""Isolated canonical-library workflow. No real learning data or external APIs."""
import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from studio.learning import (LearningService, LearningConflict, SKILLS, DOCUMENTS,
                             METHOD_CARDS, REPOSITORY, validate_method_packages, core, _json, _sha)
from studio.store import Store
from studio.models import PipelineError


def case(ident, *, partition="calibration", purposes=None, feedback=False):
    value = {"id": ident, "market": "US", "title": ident, "source_key": "fixture:" + ident,
             "source_url": None, "platform": "fixture", "origin": "user_reference",
             "comparison_group": ident, "partition": partition, "tags": ["proof"],
             "evidence": [{"id": "e1", "kind": "historical_report", "source": "fixture", "observation": ident + " evidence"}],
             "eligible_for": ["message"] if purposes is None else purposes,
             "strategy": {"proof": ident + " strategy"}, "limits": ["No product fact migration"],
             "user_feedback": None, "market_effect": None}
    if feedback:
        value["user_feedback"] = {"scope": "direction", "source": "fixture-user", "quote": "secret-preference"}
    return value


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    path = repo / "tracks/us/creative/learning/library.json"
    path.parent.mkdir(parents=True)
    doc = {"schema_version": 1, "market": "US", "library_version": 1,
           "cases": [case("seed-case", purposes=["message", "storyboard"]), case("dependency-case"),
                     case("preference-case", feedback=True), case("hidden-case", partition="holdout"),
                     case("pending-case", partition="pending", purposes=[])],
           "methods": [{"id": "method-one", "status": "user_calibrated", "when": "packing",
                        "decision": "show proof", "countercondition": "not demonstrated",
                        "supports": ["seed-case/e1"], "counterexamples": ["dependency-case/e1"]}]}
    assert core.validate(doc) == []
    path.write_text(_json(doc), encoding="utf-8")
    for skill in SKILLS:
        target = repo / ".agents/skills" / skill / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("FULL " + skill, encoding="utf-8")
    for title, relative in DOCUMENTS.values():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(title + " raw historical report", encoding="utf-8")
    for ident, spec in METHOD_CARDS.items():
        target = repo / spec['file']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('Fixture method card ' + ident, encoding='utf-8')
    store = Store(tmp_path / "external")
    store.create_run("run-one", "Fixture", "US brief")
    artifact = store.write_artifact("run-one", "direction", 1, "Exact fixture creative", summary="Fixture", author_role="director", attempt=1)
    return LearningService(store, repo), store, artifact


def feedback(artifact, **changes):
    return dict(stage=artifact.stage, version=artifact.version, artifact_sha256=artifact.sha256,
                quote="人物说话太像念说明", scope="persona_dialogue", action="local_change",
                observation="Dialogue feels read aloud", hypothesis="Try a grounded conversational response", **changes)


def apply(service, proposal):
    return service.apply("run-one", proposal["id"], proposal["base_library_sha256"])


def test_catalog_is_full_human_audit_but_model_directory_is_safe(env):
    service, store, _ = env
    catalog = service.catalog()
    assert len(catalog["skills"]) == 3
    assert all(s["text"] == "FULL " + s["id"] and s["sha256"] == _sha(s["text"]) for s in catalog["skills"])
    assert {"hidden-case", "pending-case"} <= {c["id"] for c in catalog["cases"]}
    assert "hidden-case" not in _json(service.skill_catalog())
    assert service.document("baseline-001")["text"].endswith("raw historical report")
    with pytest.raises(ValueError):
        service.document("../../library.json")
    with pytest.raises(ValueError):
        service.read_method_document("baseline-001", role="orchestrator")
    assert service.read_method_document(SKILLS[0])["text"] == "FULL " + SKILLS[0]


def test_method_packages_are_registered_and_role_scoped(env):
    service, store, _ = env
    assert validate_method_packages() == []
    assert set(METHOD_CARDS) <= {d['id'] for d in service.skill_catalog('orchestrator')['documents']}
    assert 'video-recovery' not in {d['id'] for d in service.skill_catalog('reviewer')['documents']}
    card = service.read_method_document('video-recovery', role='writer', run_id='run-one')
    assert card['source'] == 'tracks/us/docs/ai-video-recovery-policy.md'
    assert card['text'] == 'Fixture method card video-recovery'
    assert card['sha256'] and card['snapshot_sha256']
    assert store.read_events('run-one')[-1]['document_id'] == 'video-recovery'
    with pytest.raises(ValueError):
        service.read_method_document('video-recovery', role='reviewer')
    with pytest.raises(ValueError):
        service.read_method_document('batch-001', role='writer')


def test_context_persists_actual_text_and_dependency_purpose_without_holdout(env):
    service, store, _ = env
    context = service.context("run-one", "writer", case_ids=["seed-case"], purpose="storyboard")
    assert context["seed_case_ids"] == ["seed-case"]
    assert set(context["case_ids"]) == {"seed-case", "dependency-case"}
    assert "不具备该用途资格" in context["text"]
    assert "FULL us-commerce-strategy" not in context["text"] and "FULL us-creator-video" in context["text"]
    assert "hidden-case" not in _json(context) and "pending-case" not in _json(context)
    assert json.loads((store.run_dir("run-one") / context["evidence_ref"]).read_text(encoding="utf-8")) == context
    assert service.contexts("run-one")[-1]["snapshot_sha256"] == context["snapshot_sha256"]
    with pytest.raises(ValueError):
        service.context("run-one", "writer", case_ids=["hidden-case"])
    with pytest.raises(ValueError):
        service.context("run-one", "writer", case_ids=["dependency-case"], purpose="storyboard")


def test_material_role_uses_upstream_decision_without_full_creative_skills(env):
    service, _, _ = env
    context = service.context("run-one", "material", purpose="storyboard")
    assert context["skills"] == []
    assert "FULL us-commerce-strategy" not in context["text"]
    assert "FULL us-creator-video" not in context["text"]


def test_review_removes_feedback_anchors_and_own_output(env):
    service, store, artifact = env
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    apply(service, proposal)
    review = service.context("run-one", "reviewer", tags=["proof"], purpose="review")
    assert "secret-preference" not in _json(review)
    assert "preference-case" not in _json(review)
    assert review["run_feedback"] == []
    assert "人物说话" not in _json(review)
    assert review["skills"] == review["case_ids"] == []
    assert review["method_directory"] is None
    assert "FULL us-creator-video" not in review["text"]


@pytest.mark.parametrize("intent", ["pause", "status"])
def test_noncreative_intent_does_not_load_cases_or_skills(env, intent):
    service, _, _ = env
    result = service.context("run-one", "orchestrator", intent=intent, format="none", tags=["proof"])
    assert result["skills"] == result["case_ids"] == []


def test_edit_loads_skills_and_feedback_only_strategy(env):
    service, _, _ = env
    assert [s["id"] for s in service.context("run-one", "writer", intent="edit")["skills"]] == [SKILLS[1]]
    assert [s["id"] for s in service.context("run-one", "orchestrator", intent="feedback")["skills"]] == [SKILLS[0]]


def test_agent_quote_must_match_real_user_and_exact_artifact(env):
    service, store, artifact = env
    request = feedback(artifact)
    with pytest.raises(ValueError, match="real user"):
        service.propose("run-one", request)
    store.append_chat("run-one", "assistant", request["quote"])
    with pytest.raises(ValueError):
        service.propose("run-one", request)
    store.append_chat("run-one", "user", "我的反馈：" + request["quote"])
    with pytest.raises(LearningConflict):
        service.propose("run-one", dict(request, artifact_sha256="0" * 64))
    with pytest.raises(ValueError):
        service.propose("run-one", dict(request, source_chat=0))
    with pytest.raises(ValueError):
        service.propose_feedback("run-one", request, current_user_message="unrelated")
    proposal = service.propose("run-one", request)
    assert proposal["payload"]["source_identity"]["index"] == 1


def test_feedback_preview_apply_idempotency_and_scoped_next_context(env):
    service, store, artifact = env
    before = service.library_path.read_bytes()
    request = feedback(artifact)
    store.append_chat("run-one", "user", request["quote"])
    first = service.propose("run-one", request)
    assert service.propose("run-one", request)["id"] == first["id"]
    assert service.library_path.read_bytes() == before
    assert "feedback_history" in first["diff"]
    assert service.context("run-one", "writer")["run_feedback"] == []
    assert apply(service, first)["status"] == "applied"
    after = service.library_path.read_bytes()
    assert apply(service, first)["status"] == "applied" and service.library_path.read_bytes() == after
    doc = json.loads(after)
    own = next(c for c in doc["cases"] if c["origin"] == "own_output")
    assert own["partition"] == "pending" and own["eligible_for"] == []
    assert own["market_effect"] is None and own["feedback_history"][0]["improvement_effect"] is None
    assert own["studio_artifact"]["sha256"] == artifact.sha256
    result = service.context("run-one", "writer")
    assert result["run_feedback"][0]["scope"] == "persona_dialogue"
    assert own["id"] not in result["case_ids"]
    store.create_run("run-two", "Other", "Other brief")
    assert service.context("run-two", "writer")["run_feedback"] == []
    audit = store.run_dir("run-one") / "learning/proposals" / first["id"]
    assert (audit / "before.json").read_bytes() == before
    assert (audit / "after.json").read_bytes() == after
    assert len([e for e in store.read_events("run-one") if e["kind"] == "learning_applied"]) == 1


def test_user_explicit_quote_accepted_without_existing_chat_and_logged(env):
    service, store, artifact = env
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    assert store.read_chat("run-one") == []
    assert store.read_events("run-one")[-1]["user_quote"] == feedback(artifact)["quote"]
    assert proposal["payload"]["source_identity"]["kind"] == "explicit_user_input"


def test_wait_is_not_adoption_and_unsupported_action_rejected(env):
    service, _, artifact = env
    payload = feedback(artifact)
    payload.update(quote="先等一下，不继续", action="stop")
    apply(service, service.propose("run-one", payload, actor="user"))
    own = next(c for c in service.catalog()["cases"] if c["origin"] == "own_output")
    assert own["user_feedback"] is None and own["partition"] == "pending"
    assert own["feedback_history"][0]["action"] == "stop"
    with pytest.raises(ValueError, match="scope/action"):
        service.propose("run-one", dict(payload, action="wait"), actor="user")


def test_cas_prevents_lost_updates_between_two_previews(env):
    service, _, artifact = env
    first = service.propose("run-one", feedback(artifact), actor="user")
    second = service.propose_source("run-one", {"title": "New", "url": "https://www.tiktok.com/@fixture/video/12345", "tags": ["proof"]})
    apply(service, first)
    with pytest.raises(LearningConflict, match="Library changed"):
        apply(service, second)
    assert not any(c["title"] == "New" for c in service.catalog()["cases"])


@pytest.mark.parametrize("filename", ["before.json", "after.json", "proposal.json"])
def test_apply_refuses_tampered_reviewed_files(env, filename):
    service, store, artifact = env
    original = service.library_path.read_bytes()
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    file = store.run_dir("run-one") / "learning/proposals" / proposal["id"] / filename
    data = json.loads(file.read_text(encoding="utf-8"))
    data["tampered"] = True
    file.write_text(_json(data), encoding="utf-8")
    with pytest.raises(LearningConflict):
        apply(service, proposal)
    assert service.library_path.read_bytes() == original


def test_modified_artifact_refuses_apply(env):
    service, store, artifact = env
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    (store.run_dir("run-one") / "artifacts" / artifact.filename).write_text("tamper", encoding="utf-8")
    with pytest.raises(LearningConflict):
        apply(service, proposal)


def test_artifact_path_cannot_cross_runs_and_metadata_cannot_switch_versions(env):
    service, store, artifact = env
    store.create_run("run-two", "Other", "Other brief")
    other = store.write_artifact("run-two", "direction", 1, "Other creative", summary="Other", author_role="director", attempt=1)
    payload = feedback(other)
    payload["stage"] = "../../run-two/artifacts/direction"
    with pytest.raises(ValueError, match="stage"):
        service.propose("run-one", payload, actor="user")
    other_version = store.write_artifact("run-one", "direction", 2, "Revision", summary="Revision", author_role="director", attempt=1)
    metadata = store.run_dir("run-one") / "artifacts/direction-v1.json"
    metadata.write_text(_json(other_version.to_dict()), encoding="utf-8")
    with pytest.raises((LearningConflict, PipelineError), match="identity|metadata"):
        service.propose("run-one", dict(feedback(other_version), version=1), actor="user")


def test_review_diff_preserves_library_bom_newline_and_field_order(env):
    service, _, _ = env
    doc = json.loads(service.library_path.read_text(encoding="utf-8"))
    original = ("\ufeff" + json.dumps(doc, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8")
    service.library_path.write_bytes(original)
    proposal = service.propose_method("run-one", {"method_id": "method-one", "action": "keep", "rationale": "Still valid"})
    assert len(proposal["diff"].splitlines()) < 16
    apply(service, proposal)
    after = service.library_path.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf") and b"\r\n" in after
    assert json.loads(after.decode("utf-8-sig"))["library_version"] == 2


def test_first_feedback_diff_preserves_all_existing_real_case_fields(env):
    service, _, artifact = env
    # Read the shipped case fixture, then work exclusively on the temporary copy.
    original = (REPOSITORY / "tracks/us/creative/learning/library.json").read_bytes()
    service.library_path.write_bytes(original)
    doc = json.loads(original.decode("utf-8-sig"))
    assert len(doc["cases"]) >= 8
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    removed = [line for line in proposal["diff"].splitlines() if line.startswith("-") and not line.startswith("---")]
    assert len(removed) < 10
    assert not any(c["id"] in "\n".join(removed) for c in doc["cases"])
    assert service.library_path.read_bytes() == original


def test_parallel_apply_is_one_commit(env):
    service, store, artifact = env
    proposal = service.propose("run-one", feedback(artifact), actor="user")
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(lambda _: apply(service, proposal), range(2)))
    assert all(row["status"] == "applied" for row in rows)
    assert len([e for e in store.read_events("run-one") if e["kind"] == "learning_applied"]) == 1


def test_os_lock_prevents_lost_update_between_processes(env):
    service, store, artifact = env
    first = service.propose("run-one", feedback(artifact), actor="user")
    second = service.propose_source("run-one", {"title": "Parallel source", "url": "https://www.tiktok.com/@fixture/video/34567"})
    program = """
import sys
from studio.learning import LearningService, LearningConflict
from studio.store import Store
service = LearningService(Store(sys.argv[1]), sys.argv[2])
try:
    service.apply('run-one', sys.argv[3], sys.argv[4])
    print('applied')
except LearningConflict:
    print('conflict')
"""
    processes = [subprocess.Popen([sys.executable, "-c", program, str(store.root), str(service.repository_root),
                                   p["id"], p["base_library_sha256"]], cwd=Path(__file__).resolve().parents[1],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for p in (first, second)]
    results = [p.communicate(timeout=20) for p in processes]
    assert all(p.returncode == 0 for p in processes), results
    assert sorted(out.strip() for out, err in results) == ["applied", "conflict"]
    assert core.validate(service.catalog()) == []


def observation(cid, **updates):
    return dict(case_id=cid, observation="User observed opening demonstration", source="User watched source video",
                scope="opening 0-3 seconds", strategy={"proof": "Show use before explaining"},
                limits=["No full performance/audio verification", "Market effect unknown"], purpose="message", **updates)


def test_new_reference_to_reviewed_observation_to_real_recall(env):
    service, _, _ = env
    source = service.propose_source("run-one", {"title": "New reference", "url": "https://www.tiktok.com/@fixture/video/12345", "tags": ["new-tag"]})
    apply(service, source)
    item = next(c for c in service.catalog()["cases"] if c["title"] == "New reference")
    assert item["partition"] == "pending"
    assert service.context("run-one", "director", tags=["new-tag"])["case_ids"] == []
    proposal = service.propose_observation("run-one", observation(item["id"]))
    assert service.context("run-one", "director", tags=["new-tag"])["case_ids"] == []
    apply(service, proposal)
    context = service.context("run-one", "director", tags=["new-tag"])
    assert context["seed_case_ids"] == [item["id"]]
    assert "explicit_user_observation" in _json(context["library"])
    assert context["library"]["cases"][0]["market_effect"] is None


def test_observation_cannot_promote_holdout_or_performance_or_agent_claim(env):
    service, _, _ = env
    for payload in (observation("hidden-case"), dict(observation("pending-case"), purpose="performance")):
        with pytest.raises(ValueError):
            service.propose_observation("run-one", payload)
    with pytest.raises(ValueError):
        service.propose_observation("run-one", observation("pending-case"), actor="agent")


def test_method_revise_downgrades_to_hypothesis_and_retire_removes_recall(env):
    service, _, _ = env
    payload = {"method_id": "method-one", "action": "revise", "rationale": "New counterexample",
               "changes": {"decision": "Use only visible proof"}}
    proposal = service.propose_method("run-one", payload)
    assert service.catalog()["methods"][0]["status"] == "user_calibrated"
    apply(service, proposal)
    assert service.catalog()["methods"][0]["status"] == "hypothesis"
    with pytest.raises(ValueError, match="status"):
        service.propose_method("run-one", dict(payload, changes={"status": "user_calibrated"}))
    apply(service, service.propose_method("run-one", dict(payload, action="retire", changes={})))
    assert service.context("run-one", "writer", case_ids=["seed-case"])["library"]["methods"] == []


def test_source_registration_uses_core_identity_and_never_downloads(env):
    service, _, _ = env
    with pytest.raises(ValueError):
        service.propose_source("run-one", {"title": "Invalid", "url": "https://other.invalid/private"})
    with pytest.raises(ValueError):
        service.propose_source("run-one", {"title": "Missing", "media": "C:/private"})


@pytest.mark.parametrize("method,payload", [
    ("propose", []), ("propose", {}), ("propose_source", []), ("propose_source", {}),
    ("propose_source", {"url": [], "title": "bad"}),
    ("propose_source", {"url": "https://www.tiktok.com/@fixture/video/12345", "title": "bad", "id": {"bad": 1}}),
    ("propose_source", {"url": "https://www.tiktok.com/@fixture/video/12345", "title": "bad", "tags": "proof"}),
    ("propose_method", []), ("propose_method", {}),
    ("propose_method", {"method_id": "method-one", "action": "keep", "rationale": []}),
    ("propose_method", {"method_id": "method-one", "action": "revise", "rationale": "reason", "changes": {"supports": 3}}),
    ("propose_observation", []), ("propose_observation", {}),
    ("propose_observation", dict(observation("pending-case"), strategy={"proof": []})),
])
def test_malformed_user_payload_is_validation_error_not_server_error(env, method, payload):
    service, _, _ = env
    with pytest.raises(ValueError):
        getattr(service, method)("run-one", payload, actor="user")


def test_snapshot_reader_integrity_and_method_read_trace(env):
    service, store, _ = env
    result = service.context("run-one", "writer", case_ids=[])
    assert service.context_record("run-one", result["snapshot_sha256"]) == result
    path = store.run_dir("run-one") / result["evidence_ref"]
    changed = dict(result, text="tamper")
    path.write_text(_json(changed), encoding="utf-8")
    with pytest.raises(LearningConflict):
        service.context_record("run-one", result["snapshot_sha256"])
    document = service.read_method_document("creative-package-contract", role="orchestrator", run_id="run-one")
    assert (store.run_dir("run-one") / document["evidence_ref"]).exists()
    assert store.read_events("run-one")[-1]["kind"] == "learning_document_loaded"
