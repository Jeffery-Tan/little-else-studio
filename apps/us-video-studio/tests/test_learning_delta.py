"""Learning library v2 and atomic delta tests; temporary libraries only."""
import copy
import hashlib
import json
import shutil

import pytest

from studio.learning import LearningConflict, LearningService, METHOD_CARDS, REPOSITORY, SKILLS, _json, _sha, core
from studio.learning_assets import run_material_resource_id
from studio.learning_session import model_finding_observation, session_snapshot
from studio.material_observations import material_workspace, save_human_observation
from studio.materials import accept_upload
from studio.store import Store


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    library = repo / "tracks/us/creative/learning/library.json"
    library.parent.mkdir(parents=True)
    doc = {"schema_version": 1, "market": "US", "library_version": 5,
           "cases": [{"id": "case-one", "market": "US", "title": "Observed source",
                      "source_key": "fixture:one", "source_url": None, "origin": "user_reference",
                      "platform": "fixture", "comparison_group": "fixture:one", "partition": "calibration",
                      "tags": ["speech"], "media": None,
                      "evidence": [{"id": "e1", "kind": "historical_report", "source": "fixture",
                                    "observation": "Spoken reason accompanies visible action"}],
                      "eligible_for": ["message"], "strategy": {"speech": "Give a reason"},
                      "limits": ["No transfer or market effect evidence"], "user_feedback": None,
                      "market_effect": None}],
           "methods": [{"id": "legacy-method", "status": "user_calibrated", "when": "Speech is needed",
                        "decision": "Explain one choice", "countercondition": "Silent montage",
                        "supports": ["case-one/e1"], "counterexamples": []}]}
    assert core.validate(doc) == []
    library.write_text(_json(doc), encoding="utf-8")
    for skill in SKILLS:
        path = repo / ".agents/skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Test skill " + skill, encoding="utf-8")
    for spec in METHOD_CARDS.values():
        path = repo / spec['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / spec['file'], path)
    store = Store(tmp_path / "data")
    store.create_run("learning-one", "Study", "Study reference", kind="learning")
    service = LearningService(store, repo)
    return service, store, library


def new_method():
    return {"id": "reasoned-dialogue", "kind": "expression_method", "name": "Give a reason",
            "retrieval_summary": "Use spoken choice reasoning when speech repeats visible action",
            "goal": "Keep a conversational reason", "use_when": ["The product action is visible"],
            "avoid_when": ["Pure visual montage"], "required_inputs": ["Verified product structure"],
            "procedure": ["Connect a visible action to a reason"],
            "must_preserve": ["Reason tied to product evidence"], "can_vary": ["Wording and pace"],
            "limitations": ["Transfer untested"], "checks": ["Speech adds meaning beyond the image"],
            "decision": "Connect visible action and reason", "support_refs": ["case-one/e1"],
            "counterexample_refs": []}


def apply(service, proposal, request_id="request-1234"):
    return service.apply("learning-one", proposal["id"], proposal["base_library_sha256"],
                         proposal["proposal_sha256"], request_id)


def migrate(service):
    proposal = service.propose_migration("learning-one")
    return apply(service, proposal)


def test_migration_is_previewed_then_backed_up_without_invented_market(env):
    service, store, library = env
    original = library.read_bytes()
    preview = service.migration_preview()
    assert library.read_bytes() == original
    assert preview["base_library_version"] == 5
    assert preview["unknown_source_markets"] == ["case-one"]
    proposal = service.propose_migration("learning-one")
    assert library.read_bytes() == original
    applied = apply(service, proposal)
    assert applied["status"] == "applied"
    doc = json.loads(library.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 2 and doc["library_version"] == 6
    assert doc["scope_market"] == "US" and doc["cases"][0]["source_market"] == "unknown"
    assert doc["methods"][0]["status"] == "user_calibrated"
    assert doc["methods"][0]["migration_pending"]
    directory = store.run_dir("learning-one") / "learning/proposals" / proposal["id"]
    assert (directory / "before.json").read_bytes() == original
    assert json.loads((directory / "migration-backup.json").read_text())["sha256"] == _sha(original)
    assert core.validate(doc) == []


def test_create_method_is_trial_with_versioned_evidence_and_explicit_gate(env):
    service, _, library = env
    migrate(service)
    before = library.read_bytes()
    payload = {"rationale": "The reference adds choice context", "summary": "Add a scoped method",
               "operations": [{"op": "method.create", "method": new_method()}]}
    proposal = service.propose_delta("learning-one", payload)
    assert proposal["proposal_sha256"] and proposal["summary"] == payload["summary"]
    assert library.read_bytes() == before
    assert service.propose_delta("learning-one", payload)["id"] == proposal["id"]
    with pytest.raises(LearningConflict, match="proposal hash"):
        service.apply("learning-one", proposal["id"], proposal["base_library_sha256"], "0" * 64, "request-5678")
    apply(service, proposal, "request-5678")
    doc = json.loads(library.read_text(encoding="utf-8"))
    method = next(m for m in doc["methods"] if m["id"] == "reasoned-dialogue")
    assert method["status"] == "trial" and method["revision"] == 1
    assert method["support_refs"] == ["case-one/e1"] and method["market_validation"] == "unknown"
    assert "reasoned-dialogue" not in {m["id"] for m in service.method_summaries(purpose="message")["methods"]}
    assert "reasoned-dialogue" in {m["id"] for m in service.method_summaries("learning-one", include_trial=True)["methods"]}
    assert apply(service, proposal, "request-5678")["status"] == "applied"
    with pytest.raises(LearningConflict, match="different request_id"):
        apply(service, proposal, "request-other")


def test_invalid_operation_rolls_back_whole_candidate(env):
    service, _, library = env
    migrate(service)
    before = library.read_bytes()
    payload = {"rationale": "Try two updates", "operations": [
        {"op": "method.create", "method": new_method()},
        {"op": "method.add_evidence", "method_id": "legacy-method", "expected_revision": 1,
         "support_refs": ["missing-case/e1"]}]}
    with pytest.raises(ValueError, match="unresolved evidence"):
        service.propose_delta("learning-one", payload)
    assert library.read_bytes() == before
    assert not any(m["id"] == "reasoned-dialogue" for m in service._read()[1]["methods"])


def test_no_change_with_operations_is_rejected_before_proposal(env):
    service, store, library = env
    migrate(service)
    before = library.read_bytes()
    for marker in ({'no_change': True}, {'outcome': 'no_change'}):
        with pytest.raises(ValueError, match='no_change cannot include'):
            service.propose_delta('learning-one', {
                'rationale': 'Create a method', 'operations': [
                    {'op': 'method.create', 'method': new_method()}], **marker})
    assert library.read_bytes() == before
    assert not any(p['type'] == 'delta' for p in service.proposals('learning-one'))


def test_preference_uses_exact_user_quote_and_only_matching_creative_scope(env):
    service, store, _ = env
    migrate(service)
    store.create_run('target-project', 'Target', 'Creative brief')
    store.create_run('other-project', 'Other', 'Different creative brief')
    text = '我喜欢短暂停顿。这个项目的结尾不要突然加速。品牌口吻先存档。'
    store.append_chat('learning-one', 'user', text)
    source_ref = 'user-chat:learning-one:0'
    preferences = [
        {'id': 'user-pause', 'scope': 'user', 'owner': 'workspace-user',
         'dimension': 'pace', 'quote': '我喜欢短暂停顿', 'source_ref': source_ref},
        {'id': 'project-ending', 'scope': 'project', 'owner': 'target-project',
         'dimension': 'ending', 'quote': '这个项目的结尾不要突然加速', 'source_ref': source_ref},
        {'id': 'brand-tone', 'scope': 'brand', 'owner': 'little-else',
         'dimension': 'tone', 'quote': '品牌口吻先存档', 'source_ref': source_ref},
    ]
    proposal = service.propose_delta('learning-one', {'rationale': 'Save exact scoped preferences',
        'operations': [{'op': 'preference.upsert', 'preference': item} for item in preferences]})
    apply(service, proposal, 'preference-save-1')
    target = service.context('target-project', 'orchestrator', intent='new', format='video')
    other = service.context('other-project', 'writer', intent='new', format='video')
    reviewer = service.context('target-project', 'reviewer', intent='new', format='video')
    assert {p['id'] for p in target['preferences']} == {'user-pause', 'project-ending'}
    assert {p['id'] for p in other['preferences']} == {'user-pause'}
    assert reviewer['preferences'] == []
    assert '品牌口吻先存档' not in target['text']
    assert next(p for p in service.catalog()['preferences'] if p['id'] == 'brand-tone')['application_status'] == 'stored_only'
    with pytest.raises(ValueError, match='exact user turn'):
        service.propose_delta('learning-one', {'rationale': 'Invent', 'operations': [
            {'op': 'preference.upsert', 'preference': dict(preferences[0], id='invented', quote='用户没说过')} ]})


def test_user_correction_stales_delta_but_agent_completion_does_not(env):
    service, store, _ = env
    migrate(service)
    store.append_chat("learning-one", "user", "Look at the speech and action")
    payload = {"rationale": "No new insight", "outcome": "no_change", "operations": []}
    proposal = service.propose_delta("learning-one", payload)
    store.append_chat("learning-one", "assistant", "I have prepared a proposal")
    assert apply(service, proposal, "request-nochange")["status"] == "applied"
    store.append_chat("learning-one", "user", "Actually I only liked the pacing")
    second = service.propose_delta("learning-one", payload)
    assert second["id"] != proposal["id"]
    store.append_chat("learning-one", "user", "Also, the opening is too long")
    with pytest.raises(LearningConflict, match="discussion changed"):
        apply(service, second, "request-corrected")


def test_reject_is_idempotent_and_prevents_apply(env):
    service, store, library = env
    migrate(service)
    before = library.read_bytes()
    proposal = service.propose_delta("learning-one", {"rationale": "No change", "outcome": "no_change", "operations": []})
    receipt = service.reject("learning-one", proposal["id"], proposal["proposal_sha256"], "reject-1234")
    assert receipt["status"] == "rejected"
    assert service.reject("learning-one", proposal["id"], proposal["proposal_sha256"], "reject-1234") == receipt
    assert library.read_bytes() == before
    assert len([e for e in store.read_events("learning-one") if e["kind"] == "learning_rejected"]) == 1
    with pytest.raises(LearningConflict, match="Rejected proposal"):
        apply(service, proposal, "request-later")


def test_v2_validation_rejects_unearned_market_and_performance_claims(env):
    service, _, _ = env
    doc = core.migrate_v1_to_v2(service._read()[1])
    changed = copy.deepcopy(doc)
    changed["cases"][0]["source_market"] = "US"  # Allowed only as a separately supported source fact.
    changed["cases"][0]["eligible_for"] = ["performance"]
    assert any("performance" in error for error in core.validate(changed))
    changed = copy.deepcopy(doc)
    changed["methods"][0]["status"] = "active"
    assert any("active requires" in error for error in core.validate(changed))


def test_case_and_method_commit_together_from_exact_same_run_finding(env, monkeypatch):
    service, store, library = env
    migrate(service)
    media_bytes = b'exact synthetic video fixture'
    media_sha = hashlib.sha256(media_bytes).hexdigest()
    accept_upload(store, 'learning-one', 'reference.mp4', media_bytes)
    finding = "The speaker gives a reason while opening the bag"
    coverage = {"mode": "native_audiovisual", "audio_sent": True,
                "audio_ranges": [[0.0, 5.0]], "duration_seconds": 5.0}
    source = {"analysis_id": "analysis-123", "sha256": media_sha, "status": "pending",
              "findings": [finding], "timeline": [{"finding_index": 0, "start_seconds": 1.0,
                                                     "end_seconds": 2.5}], "audiovisual": coverage}
    monkeypatch.setattr("studio.image_analysis.candidates", lambda store, run_id: [source])
    # Shape copied from read_learning_session; no v2 compatibility aliases.
    observation = {"id": "analysis-123/0", "source_ref": "analysis:analysis-123",
                   "media_sha256": media_sha, "time_range": [1.0, 2.5], "observer_type": "model",
                   "observation_kind": "model_audiovisual", "content": finding,
                   "coverage": coverage, "limitations": [], "review_state": "model_unreviewed"}
    case = {"id": "new-case", "source_key": "sha256:" + media_sha, "source_platform": "local",
            "source_market": "unknown", "title": "A specific observed moment", "comparison_group": media_sha,
            "partition": "calibration", "eligible_for": ["message"], "strategy": {"speech": "Add a reason"},
            "limits": ["Only 1–2.5 seconds observed; market effect unknown"], "evidence": [observation],
            "tags": ["speech"], "media": {"resource_id": run_material_resource_id('learning-one', 'reference.mp4'),
                                           "sha256": media_sha}}
    method = new_method()
    method["support_refs"] = ["new-case/analysis-123/0"]
    before = library.read_bytes()
    payload = {"rationale": "Reviewed observed speech supports a scoped method",
               "operations": [{"op": "case.upsert", "case": case}, {"op": "method.create", "method": method}]}
    proposal = service.propose_delta("learning-one", payload)
    assert library.read_bytes() == before
    apply(service, proposal, "request-case-method")
    doc = json.loads(library.read_text(encoding="utf-8"))
    assert any(c["id"] == "new-case" for c in doc["cases"])
    saved_observation = next(c for c in doc["cases"] if c["id"] == "new-case")["evidence"][0]
    assert saved_observation["observer_type"] == "model"
    assert saved_observation["source_review_state"] == "model_unreviewed"
    assert saved_observation["review_state"] == "user_accepted"
    assert any(m["id"] == "reasoned-dialogue" for m in doc["methods"])
    assert core.validate(doc) == []


def test_model_cannot_forge_or_rewrite_finding(env, monkeypatch):
    service, _, library = env
    migrate(service)
    monkeypatch.setattr("studio.image_analysis.candidates", lambda store, run_id: [
        {"analysis_id": "analysis-123", "sha256": "a" * 64, "status": "pending",
         "findings": ["Actually observed"], "timeline": []}])
    before = library.read_bytes()
    case = {"id": "forged-case", "source_key": "sha256:" + "a" * 64, "partition": "pending",
            "source_platform": "local", "source_market": "unknown", "comparison_group": "a" * 64,
            "title": "Forged", "evidence": [{"id": "analysis-123/0", "source_ref": "analysis:analysis-123",
                                            "media_sha256": "a" * 64, "content": "Invented", "observation": "Invented",
                                            "observer_type": "model", "kind": "model_av_observation"}]}
    with pytest.raises(ValueError, match="exact finding"):
        service.propose_delta("learning-one", {"rationale": "Try", "operations": [{"op": "case.upsert", "case": case}]})
    assert library.read_bytes() == before


def test_visual_only_finding_cannot_be_relabelled_audiovisual(env, monkeypatch):
    service, _, library = env
    migrate(service)
    sampling = {'duration_seconds': 3.0, 'frames': [{'id': 'f1', 'time_seconds': 1.0}]}
    monkeypatch.setattr('studio.image_analysis.candidates', lambda store, run_id: [{
        'analysis_id': 'visual-one', 'sha256': 'c' * 64, 'status': 'pending',
        'findings': ['画面中拉上包的拉链'], 'timeline': [], 'sampling': sampling}])
    base = {'id': 'visual-one/0', 'source_ref': 'analysis:visual-one',
            'media_sha256': 'c' * 64, 'content': '画面中拉上包的拉链',
            'observer_type': 'model', 'review_state': 'model_unreviewed', 'limitations': []}
    case = {'id': 'visual-case', 'source_key': 'sha256:' + 'c' * 64,
            'source_platform': 'local', 'source_market': 'unknown',
            'comparison_group': 'c' * 64, 'title': 'Visual only',
            'partition': 'pending', 'evidence': []}
    before = library.read_bytes()
    for forged in [dict(base, observation_kind='model_audiovisual',
                        coverage={'audio_sent': True, 'audio_ranges': [[0, 3]]}),
                   dict(base, observation_kind='model_visual_sample',
                        coverage={'mode': 'visual_sample', 'sampling': sampling,
                                  'audio_sent': True, 'audio_ranges': [[0, 3]]})]:
        with pytest.raises(ValueError, match='kind and coverage'):
            service.propose_delta('learning-one', {'rationale': 'Try', 'operations': [
                {'op': 'case.upsert', 'case': dict(case, evidence=[forged])}]})
    assert library.read_bytes() == before


def test_no_audio_and_partial_audio_coverage_remain_exact():
    base = {'analysis_id': 'audio-check', 'sha256': 'd' * 64,
            'findings': ['动作与声音关系待核对'], 'timeline': []}
    silent = model_finding_observation(dict(base, audiovisual={
        'mode': 'native_audiovisual', 'audio_sent': False,
        'audio_ranges': [], 'duration_seconds': 8}), 0)
    assert silent['observation_kind'] == 'model_visual_sample'
    assert silent['coverage']['audio_ranges'] == []
    partial = model_finding_observation(dict(base, audiovisual={
        'mode': 'native_audiovisual', 'audio_sent': True,
        'audio_ranges': [[2, 4]], 'duration_seconds': 8}), 0)
    assert partial['observation_kind'] == 'model_audiovisual'
    assert partial['coverage']['audio_ranges'] == [[2, 4]]


def test_exact_same_run_human_finding_can_support_scoped_method(env):
    service, store, library = env
    migrate(service)
    accept_upload(store, "learning-one", "reference.jpg", b"isolated image fixture")
    workspace = material_workspace(store, "learning-one")
    material = workspace["materials"][0]
    save_human_observation(store, "learning-one", name=material["name"],
        expected_sha256=material["sha256"], expected_manifest_sha256=workspace["expected_manifest_sha256"],
        purpose="reference", level="image_observed", coverage="one still image",
        findings=["The bag opens from the side"], limitations=["No audio observed"])
    observed = next(o for o in session_snapshot(store, service, "learning-one")["observations"]
                    if o["observer_type"] == "user")
    assert observed["review_state"] == "user_reported"
    case = {"id": "human-case", "source_key": "sha256:" + material["sha256"],
            "source_platform": "local", "source_market": "unknown", "title": "Side opening",
            "comparison_group": material["sha256"], "partition": "calibration",
            "eligible_for": ["message"], "tags": ["opening"],
            "strategy": {"proof": "Show how the opening works"},
            "limits": ["Only the still image was observed; no sound or market effect"],
            "evidence": [observed],
            "media": {"resource_id": run_material_resource_id('learning-one', material['name']),
                      "sha256": material['sha256']}}
    method = new_method()
    method["support_refs"] = ["human-case/" + observed["id"]]
    proposal = service.propose_delta("learning-one", {"rationale": "User's still image observation supports a scoped choice",
        "operations": [{"op": "case.upsert", "case": case}, {"op": "method.create", "method": method}]})
    apply(service, proposal, "request-human-source")
    doc = json.loads(library.read_text(encoding="utf-8"))
    saved = next(c for c in doc["cases"] if c["id"] == "human-case")["evidence"][0]
    assert saved["source_review_state"] == "user_reported"
    assert saved["observer_type"] == "user" and saved["kind"] == "historical_report"
    assert saved["kind"] != "full_av_review"
    assert core.validate(doc) == []


def test_method_revision_add_evidence_and_retirement_are_versioned(env):
    service, _, library = env
    migrate(service)
    revised = service.propose_delta("learning-one", {"rationale": "Narrow condition", "operations": [
        {"op": "method.revise", "method_id": "legacy-method", "expected_revision": 1,
         "changes": {"decision": "Explain a visible choice only"}}]})
    apply(service, revised, "request-revise")
    method = next(m for m in service._read()[1]["methods"] if m["id"] == "legacy-method")
    assert method["revision"] == 2 and method["status"] == "trial"
    added = service.propose_delta("learning-one", {"rationale": "Keep source explicit", "operations": [
        {"op": "method.add_evidence", "method_id": "legacy-method", "expected_revision": 2,
         "support_refs": ["case-one/e1"]}]})
    apply(service, added, "request-evidence")
    method = next(m for m in service._read()[1]["methods"] if m["id"] == "legacy-method")
    assert method["revision"] == 3 and method["support_refs"] == ["case-one/e1"]
    retired = service.propose_delta("learning-one", {"rationale": "Retire after review", "operations": [
        {"op": "method.retire", "method_id": "legacy-method", "expected_revision": 3}]})
    apply(service, retired, "request-retire")
    assert next(m for m in service._read()[1]["methods"] if m["id"] == "legacy-method")["status"] == "retired"
    assert "legacy-method" not in {m["id"] for m in service.method_summaries("learning-one", include_trial=True)["methods"]}
    assert core.validate(json.loads(library.read_text(encoding="utf-8"))) == []


def test_existing_source_observation_and_method_forms_remain_usable_after_migration(env):
    service, _, library = env
    migrate(service)
    source = service.propose_source("learning-one", {"title": "New URL",
        "url": "https://www.tiktok.com/@fixture/video/123456", "tags": ["speech"]})
    apply(service, source, "request-source")
    added = next(c for c in service._read()[1]["cases"] if c["title"] == "New URL")
    assert added["source_market"] == "unknown" and added["partition"] == "pending"
    observed = service.propose_observation("learning-one", {"case_id": added["id"],
        "observation": "User saw one spoken choice", "source": "User's own viewing note",
        "scope": "opening 0–3 seconds", "strategy": {"speech": "Explain the choice"},
        "limits": ["Audio tone not independently checked"], "purpose": "message"})
    apply(service, observed, "request-observed")
    method = service.propose_method("learning-one", {"method_id": "legacy-method", "action": "revise",
        "rationale": "Narrow the old rule", "changes": {"decision": "Explain a visible choice"}})
    apply(service, method, "request-old-form")
    doc = json.loads(library.read_text(encoding="utf-8"))
    assert core.validate(doc) == []
    assert next(c for c in doc["cases"] if c["id"] == added["id"])["eligible_for"] == ["message"]


def test_legacy_feedback_proposal_still_records_scoped_run_feedback_in_v2(env):
    service, store, library = env
    migrate(service)
    store.create_run("creative-one", "Creative", "Product brief")
    artifact = store.write_artifact("creative-one", "direction", 1, "Draft", summary="Draft",
                                    author_role="director", attempt='direction#1')
    proposal = service.propose_feedback("creative-one", {"stage": "direction", "version": 1,
        "artifact_sha256": artifact.sha256, "quote": "只改开场语气", "scope": "persona_dialogue",
        "action": "local_change", "observation": "开场太硬"}, actor="user")
    service.apply("creative-one", proposal["id"], proposal["base_library_sha256"],
                  proposal["proposal_sha256"], "creative-feedback-request")
    doc = json.loads(library.read_text(encoding="utf-8"))
    own = next(c for c in doc["cases"] if c.get("origin") == "own_output")
    assert own["source_market"] == "unknown" and own["partition"] == "pending"
    assert own["feedback_history"][0]["quote"] == "只改开场语气"
    assert core.validate(doc) == []


def test_creative_context_sees_qualified_method_without_case_tag_match(env):
    service, store, _ = env
    migrate(service)
    store.create_run("creative-one", "Creative", "Product brief")
    created = service.propose_delta("learning-one", {"rationale": "Observed speech can add a reason",
        "operations": [{"op": "method.create", "method": new_method()}]})
    assert "reasoned-dialogue" not in {m["id"] for m in service.context(
        "creative-one", "writer", tags=["unrelated-tag"])["method_directory"]["methods"]}
    apply(service, created, "request-trial-create")
    assert "reasoned-dialogue" not in {m["id"] for m in service.context(
        "creative-one", "writer", tags=["unrelated-tag"])["method_directory"]["methods"]}
    trial_sha = service.method_summaries("creative-one")["library_sha256"]
    with pytest.raises(ValueError, match="unavailable"):
        service.read_learning_method("creative-one", "reasoned-dialogue", 1, trial_sha)
    with pytest.raises(ValueError, match="previously saved trial"):
        service.propose_delta("learning-one", {"rationale": "Premature activation", "operations": [
            {"op": "method.create", "method": dict(new_method(), id="second-method")},
            {"op": "method.activate", "method_id": "second-method", "expected_revision": 1,
             "rationale": "Allow US use despite no transfer test", "unverified_use_approved": True}]})
    activation = service.propose_delta("learning-one", {"rationale": "Permit scoped use while unverified",
        "operations": [{"op": "method.activate", "method_id": "reasoned-dialogue", "expected_revision": 1,
                        "rationale": "Use for US message tasks while transfer is unverified",
                        "unverified_use_approved": True}]})
    assert "reasoned-dialogue" not in {m["id"] for m in service.context(
        "creative-one", "writer", tags=["unrelated-tag"])["method_directory"]["methods"]}
    apply(service, activation, "request-activate")
    assert "reasoned-dialogue" not in {m["id"] for m in service.context(
        "creative-one", "writer", tags=["unrelated-tag"])["method_directory"]["methods"]}
    store.create_run("creative-two", "Creative after learning", "Same product, new task")
    context = service.context("creative-two", "writer", tags=["unrelated-tag"])
    assert context["seed_case_ids"] == []
    assert "reasoned-dialogue" in {m["id"] for m in context["method_directory"]["methods"]}
    assert "reasoned-dialogue" in context["text"]
    assert "reasoned-dialogue" not in {m["id"] for m in service.method_summaries(purpose="storyboard")["methods"]}
    active = next(m for m in service._read()[1]["methods"] if m["id"] == "reasoned-dialogue")
    assert active["activation"]["effectiveness"] == "unverified"
    detail = service.read_learning_method("creative-two", "reasoned-dialogue", active["revision"],
                                          context["library_sha256"], purpose="message", role="writer")
    assert detail["method"]["must_preserve"] == ["Reason tied to product evidence"]
    assert detail["evidence"][0]["ref"] == "case-one/e1"
    assert (store.run_dir("creative-two") / detail["evidence_ref"]).is_file()
    with pytest.raises(ValueError, match="unavailable"):
        service.read_learning_method("creative-two", "reasoned-dialogue", active["revision"],
                                     context["library_sha256"], purpose="storyboard")
