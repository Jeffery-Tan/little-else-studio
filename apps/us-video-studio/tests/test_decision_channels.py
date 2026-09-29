"""Both input channels end at the same write point.

A click and a decision the user stated in conversation used to be two
implementations that happened to agree. They now differ in exactly two facts —
the ``source`` recorded, and where the nonce comes from — and everything that
decides whether the decision *counts* sits below that shared line. The first
property is what these tests check; the second is checked by the rules in
``test_decision.py``, which the shared line applies to both.
"""
from __future__ import annotations

import pytest

from local_http import TestClient
from studio import decision
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_server import RUN, settings_for


@pytest.fixture
def scene(tmp_path):
    app = build_app(settings_for(tmp_path), client_factory=lambda: None)
    with TestClient(app) as http:
        created = http.post("/api/runs", json={"run_id": RUN, "title": "Channels",
                                              "brief": "Synthetic only"})
        assert created.status_code == 200, created.text
        store = Store(tmp_path)
        pipeline = Pipeline(store)
        for stage in ("intake", "direction"):
            attempt = pipeline.start_attempt(RUN, stage)
            structured = ({"options": [{"key": key, "claim": f"claim {key}",
                                        "satisfies": "goal.primary"} for key in "ABC"]}
                          if stage == "direction" else {})
            pipeline.submit(RUN, stage, attempt.token, content=f"Synthetic {stage}",
                            summary=stage, structured=structured)
        yield http, store, pipeline


def decision_payload(store, nonce, **over):
    latest = store.latest_artifact(RUN, "direction")
    return {"stage": "direction", "nonce": nonce, "decision": "approved",
            "expected_version": latest.version, "expected_artifact_sha256": latest.sha256,
            "selected_option_id": "B", "continue_after": True, **over}


def test_a_click_reaches_the_shared_write_point_with_the_browsers_nonce(scene, monkeypatch):
    http, store, pipeline = scene
    seen = []
    original = decision.record

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(decision, "record", spy)
    latest = store.latest_artifact(RUN, "direction")
    issued = http.post(f"/api/runs/{RUN}/nonce?stage=direction"
                       f"&expected_version={latest.version}"
                       f"&expected_artifact_sha256={latest.sha256}")
    assert issued.status_code == 200, issued.text
    browser_nonce = issued.json()["nonce"]

    saved = http.post(f"/api/runs/{RUN}/decision", json=decision_payload(store, browser_nonce))
    assert saved.status_code == 200, saved.text

    assert len(seen) == 1
    assert seen[0]["source"] == "ui"
    assert seen[0]["nonce"] == browser_nonce, "a click must carry the nonce it was issued"
    assert seen[0]["stage"] == "direction"
    assert seen[0]["selected_option_id"] == "B"
    assert store.list_approvals(RUN, "direction")[-1].source == "ui"
    assert saved.json()["selected_option_id"] == "B"


def test_a_conversation_decision_reaches_the_same_write_point_with_a_server_nonce(
        scene, monkeypatch):
    """Same function, different holder of the nonce."""
    _http, store, pipeline = scene
    seen = []
    original = decision.record

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(decision, "record", spy)
    waiting = decision.candidates(store, pipeline, RUN)[0]
    selecting = next(a for a in waiting["answers"] if a["action"] == "select_direction")
    decision.commit(store, pipeline, RUN,
                    {**selecting, "target": dict(waiting["target"]), "option_id": "C"},
                    source="conversation", user_message="用 C，然后继续出分镜")

    assert len(seen) == 1
    assert seen[0]["source"] == "conversation"
    assert seen[0]["nonce"], "the server still mints one"
    # The browser was never given it: it is not in any HTTP-readable record.
    events = store.read_events(RUN)
    assert all("nonce" not in event for event in events)
    approval = store.list_approvals(RUN, "direction")[-1]
    assert approval.source == "conversation"
    assert approval.selected_option_id == "C"


def test_a_click_cannot_be_replayed_with_a_different_decision(scene):
    """The one-decision-per-version rule is below the shared line, so it holds.

    ``continue_after`` is off here on purpose: the follow-up job would make the run
    busy, and the busy guard would mask what this test is about. Idempotency of a
    retried decision is a property of the write point, not of the job it starts.
    """
    http, store, _ = scene
    latest = store.latest_artifact(RUN, "direction")
    issued = http.post(f"/api/runs/{RUN}/nonce?stage=direction"
                       f"&expected_version={latest.version}"
                       f"&expected_artifact_sha256={latest.sha256}").json()["nonce"]
    payload = decision_payload(store, issued, continue_after=False)
    first = http.post(f"/api/runs/{RUN}/decision", json=payload)
    assert first.status_code == 200, first.text

    again = http.post(f"/api/runs/{RUN}/decision",
                      json={**payload, "decision": "returned", "note": "改一下",
                            "selected_option_id": None})
    assert again.status_code == 409
    # The idempotent retry of the same decision still resolves, unchanged.
    replay = http.post(f"/api/runs/{RUN}/decision", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["selected_option_id"] == "B"
    assert len(store.list_approvals(RUN, "direction")) == 1


def test_the_button_channel_still_refuses_a_foreign_nonce(scene):
    """Converging the write point must not weaken what makes a click a click."""
    http, store, _ = scene
    forged = http.post(f"/api/runs/{RUN}/decision",
                       json=decision_payload(store, "not-a-real-nonce"))
    assert forged.status_code == 409
    assert store.list_approvals(RUN, "direction") == []
