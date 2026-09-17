"""Store invariants: atomicity, immutability, fingerprints, append-only logs."""
from __future__ import annotations

import json

import pytest

from studio.models import PipelineError
from studio.store import Store, sha256_text


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path)


def test_run_ids_are_restricted(store):
    store.create_run("lunch-bag-01", "Lunch bag", "brief")
    for bad in ("Bad-Id", "-leading", "has space", "", "有中文"):
        with pytest.raises(PipelineError):
            store.read_run(bad)


def test_market_is_us_only(store):
    with pytest.raises(PipelineError) as err:
        store.create_run("sea-thing", "SEA thing", "brief", market="SEA")
    assert err.value.rule == "market"


def test_creating_the_same_run_twice_fails(store):
    store.create_run("lunch-bag-01", "Lunch bag", "brief")
    with pytest.raises(PipelineError):
        store.create_run("lunch-bag-01", "Lunch bag", "brief")


def test_artifact_versions_are_immutable(store):
    store.create_run("r1", "t", "b")
    store.write_artifact(
        "r1", "intake", 1, "first", summary="s", author_role="orchestrator", attempt="intake#1"
    )
    with pytest.raises(PipelineError) as err:
        store.write_artifact(
            "r1", "intake", 1, "overwritten", summary="s",
            author_role="orchestrator", attempt="intake#1",
        )
    assert err.value.rule == "immutable"
    assert store.read_artifact_text("r1", "intake", 1) == "first"


def test_empty_artifact_is_refused(store):
    store.create_run("r1", "t", "b")
    with pytest.raises(PipelineError):
        store.write_artifact(
            "r1", "intake", 1, "   \n ", summary="s",
            author_role="orchestrator", attempt="intake#1",
        )


def test_fingerprint_matches_then_detects_tampering(store):
    store.create_run("r1", "t", "b")
    artifact = store.write_artifact(
        "r1", "direction", 1, "body", summary="s",
        author_role="director", attempt="direction#1", inputs={"intake": "abc"},
    )
    assert artifact.digest_algorithm == "artifact-v2"
    assert artifact.sha256 != sha256_text("body")
    assert store.verify_artifact("r1", "direction", 1) is True

    path = store.run_dir("r1") / "artifacts" / "direction-v1.md"
    path.write_text("body edited behind the store's back", encoding="utf-8")
    assert store.verify_artifact("r1", "direction", 1) is False


def test_no_artifact_file_is_left_without_its_sidecar(store):
    store.create_run("r1", "t", "b")
    store.write_artifact(
        "r1", "intake", 1, "x", summary="s", author_role="orchestrator", attempt="intake#1"
    )
    base = store.run_dir("r1") / "artifacts"
    assert (base / "intake-v1.md").is_file()
    assert (base / "intake-v1.json").is_file()


def test_artifacts_list_in_pipeline_order_not_alphabetical(store):
    store.create_run("r1", "t", "b")
    for stage in ("writing", "intake", "direction"):
        store.write_artifact(
            "r1", stage, 1, stage, summary="s",
            author_role="x", attempt=f"{stage}#1",
        )
    assert [a.stage for a in store.list_artifacts("r1")] == ["intake", "direction", "writing"]


def test_events_are_append_only_and_sequenced(store):
    store.create_run("r1", "t", "b")
    store.append_event("r1", "a")
    store.append_event("r1", "b")
    events = store.read_events("r1")
    assert [e["seq"] for e in events] == [1, 2]
    assert [e["kind"] for e in events] == ["a", "b"]


def test_corrupt_log_line_is_reported_not_skipped(store):
    store.create_run("r1", "t", "b")
    store.append_event("r1", "a")
    path = store.run_dir("r1") / "events.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    with pytest.raises(PipelineError) as err:
        store.read_events("r1")
    assert "not valid JSON" in str(err.value)


def test_archive_moves_but_does_not_delete(store, tmp_path):
    store.create_run("r1", "t", "b")
    dest = store.archive_run("r1")
    assert dest.is_dir()
    assert not store.run_dir("r1").exists()
    assert json.loads((dest / "run.json").read_text(encoding="utf-8"))["run_id"] == "r1"
