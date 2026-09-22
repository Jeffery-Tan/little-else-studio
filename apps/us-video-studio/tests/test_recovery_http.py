"""Recovery changes only isolated journals, never calls a provider."""
import pytest
from fastapi.testclient import TestClient

from studio.config import DEFAULT_PRICING
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for


def fixture(tmp_path, **settings_overrides):
    settings = settings_for(tmp_path, **settings_overrides)
    store = Store(tmp_path)
    store.create_run(RUN, "恢复测试", "隔离数据")
    pipe = Pipeline(store)
    first = pipe.start_attempt(RUN, "intake")
    artifact = pipe.submit(RUN, "intake", first.token, content="已保存内容", summary="需求")
    attempt = pipe.start_attempt(RUN, "direction")
    ledger = Ledger(store, settings)
    call = ledger.begin_call(RUN, role="director", stage="direction", model="synthetic")
    app = build_app(settings, client_factory=lambda: pytest.fail("recovery must not prepare a model"))
    return app, store, pipe, ledger, call, attempt, artifact


def snapshot(http):
    response = http.get(f"/api/runs/{RUN}/recovery")
    assert response.status_code == 200, response.text
    return response.json()


def test_recover_without_job_preserves_artifacts_costs_and_approval(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    original = (store.run_dir(RUN) / "ledger.jsonl").read_bytes()
    with TestClient(app) as http:
        before = snapshot(http)
        assert before["can_recover"] and not before["can_continue"]
        assert before["saved_artifacts"][0]["version"] == artifact.version
        close = http.post(f"/api/runs/{RUN}/recovery/abandon", json={
            "expected_revision": before["revision"], "stage": "direction",
            "reason": "服务中断后决定另开新尝试",
        })
        assert close.status_code == 200, close.text
        assert close.json()["open_attempts"] == []
        assert attempt.token not in close.text
        assert before["open_attempts"][0]["token"] is None
        assert attempt.token not in http.get(f"/api/runs/{RUN}/recovery").text
        assert close.json()["can_continue"]  # unknown usage remains diagnostic only
        assert close.json()["unresolved_calls"][0]["input_tokens"] is None
        assert (store.run_dir(RUN) / "ledger.jsonl").read_bytes() == original
        assert pipe.stage_state(RUN, "intake").latest.sha256 == artifact.sha256
        assert pipe.stage_state(RUN, "direction").approval is None
        settled = http.post(f"/api/runs/{RUN}/recovery/reconcile", json={
            "expected_revision": close.json()["revision"], "call_id": call,
            "input_tokens": 432, "output_tokens": 21, "evidence_note": "synthetic provider export #1",
        })
        assert settled.status_code == 200, settled.text
        assert settled.json()["can_continue"]
        row = ledger.entries(RUN)[0]
        assert row.input_tokens == 432 and row.output_tokens == 21
        assert row.call_status == "reconciled" and not row.ok
        assert (store.run_dir(RUN) / "ledger.jsonl").read_bytes().startswith(original)
        assert http.get(f"/api/runs/{RUN}/jobs/latest").json() is None


def test_exact_attempt_and_snapshot_reject_stale_recovery(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    with TestClient(app) as http:
        before = snapshot(http)
        # Wrong stage: there is no open attempt for writing yet.
        payload = {"expected_revision": before["revision"], "stage": "writing",
                   "reason": "test"}
        assert http.post(f"/api/runs/{RUN}/recovery/abandon", json=payload).status_code == 409
        pipe.reject(RUN, "direction", attempt.token, "another operator")
        next_attempt = pipe.start_attempt(RUN, "direction")
        # Stale revision after the attempt changed.
        payload = {"expected_revision": before["revision"], "stage": "direction",
                   "reason": "test"}
        assert http.post(f"/api/runs/{RUN}/recovery/abandon", json=payload).status_code == 409
        assert pipe.stage_state(RUN, "direction").open_attempt.token == next_attempt.token


@pytest.mark.parametrize("field,value", [("input_tokens", -1), ("input_tokens", True),
    ("output_tokens", 2.5), ("output_tokens", "2"), ("evidence_note", "")])
def test_reconciliation_requires_whole_counts_and_evidence(tmp_path, field, value):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    with TestClient(app) as http:
        payload = {"expected_revision": snapshot(http)["revision"], "call_id": call,
                   "input_tokens": 12, "output_tokens": 4, "evidence_note": "provider record"}
        payload[field] = value
        assert http.post(f"/api/runs/{RUN}/recovery/reconcile", json=payload).status_code == 422
        assert ledger.entries(RUN)[0].usage_status != "known"


def test_cross_project_settlement_and_blank_evidence_rejected(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    store.create_run("other-run", "Other", "brief")
    # A legacy imported unresolved entry can coexist with a pending intent.
    other = "other-call"
    ledger.record("other-run", role="writer", stage="writing", model="synthetic",
                  input_tokens=None, output_tokens=None, call_id=other, ok=False)
    with TestClient(app) as http:
        payload = {"expected_revision": snapshot(http)["revision"], "call_id": other,
                   "input_tokens": 0, "output_tokens": 0, "evidence_note": "provider record"}
        assert http.post(f"/api/runs/{RUN}/recovery/reconcile", json=payload).status_code == 409
        payload.update(call_id=call, evidence_note="   ")
        assert http.post(f"/api/runs/{RUN}/recovery/reconcile", json=payload).status_code == 409
        # Unresolved calls are limited to this run (not the other project's).
        assert len(snapshot(http)["unresolved_calls"]) == 1
        assert snapshot(http)["unresolved_calls"][0]["call_id"] == call


def test_any_active_job_and_inflight_call_block_recovery(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    with TestClient(app) as http:
        payload = {"expected_revision": snapshot(http)["revision"], "stage": "direction",
                   "reason": "test"}
        app.state.jobs.active = ("other-run", "active-job")
        try:
            assert not snapshot(http)["can_recover"]
            assert http.post(f"/api/runs/{RUN}/recovery/abandon", json=payload).status_code == 409
        finally:
            app.state.jobs.active = None
        with ledger.call_guard():
            assert http.post(f"/api/runs/{RUN}/recovery/abandon", json=payload).status_code == 409
        assert pipe.stage_state(RUN, "direction").open_attempt.token == attempt.token


def test_recovery_obeys_local_origin_boundary(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path)
    with TestClient(app) as http:
        response = http.post(f"/api/runs/{RUN}/recovery/abandon", headers={"origin": "https://evil.example"}, json={
            "expected_revision": snapshot(http)["revision"], "stage": "direction",
            "reason": "test"})
        assert response.status_code == 403
        assert pipe.stage_state(RUN, "direction").open_attempt is not None


def test_accounting_recovery_does_not_invent_a_price_or_accept_repeated_old_form(tmp_path):
    app, store, pipe, ledger, call, attempt, artifact = fixture(tmp_path, pricing=DEFAULT_PRICING)
    with TestClient(app) as http:
        snapshot_before = snapshot(http)
        payload = {"expected_revision": snapshot_before["revision"], "call_id": call,
                   "input_tokens": 12, "output_tokens": 4, "evidence_note": "provider record"}
        response = http.post(f"/api/runs/{RUN}/recovery/reconcile", json=payload)
        assert response.status_code == 200
        row = ledger.entries(RUN)[0]
        assert row.estimated_usd is None  # synthetic model has no configured price
        assert row.resolution_note == "provider record"
        settled_bytes = (store.run_dir(RUN) / "ledger.jsonl").read_bytes()
        assert http.post(f"/api/runs/{RUN}/recovery/reconcile", json=payload).status_code == 409
        assert (store.run_dir(RUN) / "ledger.jsonl").read_bytes() == settled_bytes
