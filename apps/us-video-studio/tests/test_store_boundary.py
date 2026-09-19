"""Artifact addressing cannot cross runs or silently relabel a saved version."""
import json

import pytest

from studio.models import PipelineError
from studio.store import Store


@pytest.fixture()
def store(tmp_path):
    value = Store(tmp_path)
    for run in ("run-one", "run-two"):
        value.create_run(run, "Fixture", "No model calls")
        value.write_artifact(run, "writing", 1, "Synthetic " + run,
                             summary="Fixture", author_role="writer", attempt="test")
    return value


@pytest.mark.parametrize("stage", ["../../run-two/artifacts/writing", "../writing", "", "unknown", None, []])
def test_artifact_readers_reject_unregistered_stage_before_file_access(store, stage):
    for reader in (store.read_artifact, store.read_artifact_text, store.verify_artifact):
        with pytest.raises(PipelineError, match="stage"):
            reader("run-one", stage, 1)
    assert store.read_artifact_text("run-two", "writing", 1) == "Synthetic run-two"


@pytest.mark.parametrize("version", [0, -1, True, False, 1.0, "1", "1/../../run-two/artifacts/writing-v1", None])
def test_version_is_a_positive_integer_not_a_path_or_coerced_value(store, version):
    for reader in (store.read_artifact, store.read_artifact_text, store.verify_artifact):
        with pytest.raises(PipelineError, match="positive integer"):
            reader("run-one", "writing", version)


@pytest.mark.parametrize("changes", [{"stage": "direction"}, {"version": 2}, {"version": True}])
def test_sidecar_identity_must_match_its_requested_address(store, changes):
    path = store.run_dir("run-one") / "artifacts/writing-v1.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")
    for reader in (store.read_artifact, store.verify_artifact):
        with pytest.raises(PipelineError, match="metadata does not match"):
            reader("run-one", "writing", 1)
