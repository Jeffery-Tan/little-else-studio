import pytest

from studio.evidence import evidence_fingerprint
from studio.material_observations import (
    ObservationConflict, ObservationError, material_workspace, save_human_observation,
)
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.workspace import Workspace


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path)
    instance.create_run("material-test", "Material test", "brief")
    accept_upload(instance, "material-test", "商品.jpg", b"isolated test fixture")
    return instance


def payload(store, **overrides):
    state = material_workspace(store, "material-test")
    item = state["materials"][0]
    return {"name": item["name"], "expected_sha256": item["sha256"],
            "expected_manifest_sha256": state["expected_manifest_sha256"],
            "purpose": "product", "level": "image_observed", "coverage": "front only",
            "findings": ["Handle visible"], "limitations": ["Capacity unknown"], **overrides}


def save(store, **overrides):
    return save_human_observation(store, "material-test", **payload(store, **overrides))


def test_uploaded_is_not_observed_and_saved_human_record_is_byte_bound(store):
    initial = material_workspace(store, "material-test")
    assert initial["automatic_analysis_available"] is False
    assert initial["materials"][0]["status"] == "uploaded"
    before = evidence_fingerprint(store, "material-test")
    state = save(store, purpose="reference")
    item = state["materials"][0]
    assert item["status"] == "has_observations"
    assert item["observations"][0]["observer"] == "human:workspace-user"
    assert item["observations"][0]["purpose"] == "reference"
    assert item["observations"][0]["source_kind"] == "human_observation"
    assert before != evidence_fingerprint(store, "material-test")


def test_revision_conflict_preserves_existing_and_append_keeps_history(store):
    stale = payload(store)
    save(store)
    with pytest.raises(ObservationConflict):
        save_human_observation(store, "material-test", **stale)
    state = save(store, findings=["Correction: only one side visible"])
    assert len(state["materials"][0]["observations"]) == 2
    assert state["materials"][0]["observations"][0]["findings"] == ["Handle visible"]


def test_changed_file_rejects_old_observation_and_hides_stale_report(store):
    save(store)
    stale = payload(store)
    (store.run_dir("material-test") / "materials" / "商品.jpg").write_bytes(b"changed")
    with pytest.raises(ObservationConflict):
        save_human_observation(store, "material-test", **stale)
    state = material_workspace(store, "material-test")
    assert state["materials"][0]["status"] == "uploaded"
    assert state["errors"]


@pytest.mark.parametrize("overrides", [
    {"name": "../outside.jpg"}, {"level": "audiovisual_review"},
    {"purpose": "verified_fact"}, {"coverage": ""}, {"findings": []},
    {"limitations": []}, {"findings": ["x" * 2001]},
])
def test_invalid_records_do_not_write(store, overrides):
    with pytest.raises(ObservationError):
        save(store, **overrides)
    assert not (store.run_dir("material-test") / "evidence" / "observations.json").exists()


def test_invalid_manifest_is_not_overwritten(store):
    path = store.run_dir("material-test") / "evidence" / "observations.json"
    path.parent.mkdir()
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ObservationError):
        save(store)
    assert path.read_text() == "broken"


def test_observation_change_invalidates_existing_stage(store):
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt("material-test", "intake", actor="test")
    pipeline.submit("material-test", "intake", attempt.token, content="known input", summary="Input", actor="test")
    assert not pipeline.stage_state("material-test", "intake").stale
    save(store)
    assert pipeline.stage_state("material-test", "intake").stale


def test_video_does_not_promote_transcript_to_audiovisual_review(store):
    accept_upload(store, "material-test", "clip.mp4", b"video fixture")
    item = next(item for item in material_workspace(store, "material-test")["materials"] if item["name"] == "clip.mp4")
    state = save(store, name="clip.mp4", expected_sha256=item["sha256"], level="transcript_only", coverage="00:01-00:02 dialogue")
    clip = next(item for item in state["materials"] if item["name"] == "clip.mp4")
    assert clip["observations"][0]["level"] == "transcript_only"


def test_role_context_keeps_source_and_reference_boundary(store):
    save(store, purpose="reference", findings=["Reference has fast cuts"])
    context = Workspace(store, Pipeline(store), "material-test", role="writer").render_materials()
    assert "Reference has fast cuts" in context
    assert '"purpose": "reference"' in context
    assert "human:workspace-user" in context
    assert "不得当成本商品事实" in context
