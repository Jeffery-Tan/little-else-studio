"""HTTP surface, driven with a scripted model. No network, no spend."""
from __future__ import annotations

import json
import threading

import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio.config import Pricing, Settings
from studio.models import PipelineError, STAGES
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store

RUN = "lunch-bag-01"


def settings_for(tmp_path, **over):
    base = dict(
        data_root=tmp_path,
        base_url="https://example.invalid/anthropic",
        budget_limit_usd=10.0,
        pricing=Pricing(source="test", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
                        as_of="2026-09-17"),
        max_tool_rounds=6,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def client(tmp_path):
    settings = settings_for(tmp_path)
    app = build_app(settings, client_factory=lambda: ScriptedModel([final_turn("你好。")]))
    return TestClient(app)


def make_run(client):
    res = client.post("/api/runs", json={
        "run_id": RUN, "title": "保温袋 15 秒", "brief": "给上班族带饭的保温袋。",
    })
    assert res.status_code == 200, res.text
    return res.json()


def stream_events(res) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in res.text.splitlines()
        if line.startswith("data: ")
    ]


# ------------------------------------------------------------------- read -- #


def test_index_and_static_assets_are_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "US 视频制片室" in index.text
    assert index.headers['content-security-policy'] == "frame-ancestors 'none'"
    assert index.headers['cache-control'] == 'no-store'
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_core_route_domains_remain_installed(client):
    routes = {route.path for route in client.app.routes}
    assert {
        '/api/learning', '/api/runs', '/api/runs/{run_id}/state',
        '/api/runs/{run_id}/materials', '/api/runs/{run_id}/decision',
        '/api/connections', '/api/runs/{run_id}/jobs',
    } <= routes


def test_settings_and_agent_share_declared_media_boundaries(client):
    data = client.get('/api/capabilities').json()
    assert data['video_understanding']['agent_can_execute'] is False
    assert '百炼北京' in data['video_understanding']['ui_summary']
    assert data['image_analysis']['agent_can_execute'] is True
    assert data['production']['agent_can_approve_or_generate'] is False


def test_runtime_reports_loaded_process_without_data_root_path(client, tmp_path):
    first = client.get('/api/runtime')
    second = client.get('/api/runtime')
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()['code_fingerprint']
    assert str(tmp_path) not in first.text


def test_state_exposes_the_pipeline_and_the_budget(client):
    make_run(client)
    data = client.get(f"/api/runs/{RUN}/state").json()
    assert [s["stage"] for s in data["stages"]] == list(STAGES)
    assert data["next_stage"] == "intake"
    assert data["budget"]["limit_usd"] == 10.0
    assert "not the provider's bill" in data["budget"]["pricing"]["note"]
    gated = {p["stage"]: p["gated"] for p in data["pipeline"]}
    assert gated == {"intake": False, "direction": True,
                     "material_plan": False, "writing": True}


def test_project_list_uses_existing_events_for_format_and_activity(client):
    make_run(client)
    store = client.app.state.jobs.store
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'new', 'format': 'photo'})
    row = next(run for run in client.get('/api/runs').json() if run['run_id'] == RUN)
    assert row['content_format'] == 'photo'
    assert row['updated_at'] == store.read_events(RUN)[-1]['at']
    assert row['progress']['title'] == row['project_brief']['title']


def test_timeline_and_project_list_project_the_latest_format_without_reopening(client):
    make_run(client)
    store = client.app.state.jobs.store

    def formats():
        row = next(run for run in client.get('/api/runs').json() if run['run_id'] == RUN)
        current = client.get(f'/api/runs/{RUN}/timeline')
        assert current.status_code == 200, current.text
        return row['content_format'], current.json()['run']['content_format']

    assert formats() == (None, None)
    for selected in ('video', 'photo', 'video', 'photo'):
        store.append_event(RUN, 'creative_context_selected', selection={'intent': 'edit', 'format': selected})
        assert formats() == (selected, selected)
    for intent in ('status', 'pause', 'discuss'):
        store.append_event(RUN, 'creative_context_selected', selection={'intent': intent, 'format': 'none'})
        assert formats() == ('photo', 'photo')
    assert not any(row.get('kind') == 'job_created' for row in store.read_events(RUN))


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope/state").status_code == 404


def test_a_second_run_with_the_same_id_is_refused(client):
    make_run(client)
    res = client.post("/api/runs", json={"run_id": RUN, "title": "x", "brief": "y"})
    assert res.status_code == 400


# -------------------------------------------------------------- approvals -- #


@pytest.fixture()
def client_factory(tmp_path):
    """A client whose app can swap the scripted model between calls."""
    box = {}

    def factory():
        return box["model"]

    app = build_app(settings_for(tmp_path), client_factory=factory)
    box["app"] = app
    box["http"] = TestClient(app)
    box["tmp_path"] = tmp_path
    return box


def drive_intake_and_direction(box, structured=None):
    http = box["http"]
    make_run(http)
    box["model"] = ScriptedModel([
        tool_turn("route", "prepare_creative_context", {"intent": "edit", "format": "video", "deliverable": "direction"}),
        tool_turn("t1", "write_stage", {
            "stage": "intake", "content": "需求正文", "summary": "需求整理",
        }),
        tool_turn("t2", "dispatch", {"stage": "direction", "brief": "出两个方向"}),
        tool_turn("t3", "submit_stage", {
            "content": "方向正文", "summary": "两个方向", "structured": structured,
        }),
        final_turn("intake 和 direction 都好了，等审核。"),
    ])
    res = http.post(f"/api/runs/{RUN}/message", json={"text": "做个保温袋视频"})
    assert res.status_code == 200
    events = stream_events(res)
    assert events[-1]["type"] == "done"


def shown(http, stage="direction"):
    latest = next(s for s in http.get(f"/api/runs/{RUN}/state").json()["stages"]
                  if s["stage"] == stage)["latest"]
    return {"expected_artifact_sha256": latest["sha256"], "expected_version": latest["version"]}


def nonce_for(http):
    response = http.post(f"/api/runs/{RUN}/nonce", params={"stage": "direction", **shown(http)})
    assert response.status_code == 200, response.text
    return response.json()["nonce"]


def test_approval_requires_a_nonce_issued_for_that_stage(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    # No nonce at all.
    res = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": "", "decision": "approved",
    })
    assert res.status_code == 409
    assert "not valid" in res.json()["detail"]

    # A nonce minted for the other gated stage.
    other = nonce_for(http)
    res = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "writing", "nonce": other, "decision": "approved",
    })
    assert res.status_code == 409

    assert http.get(f"/api/runs/{RUN}/state").json()["stages"][1]["approval"] is None


def test_approval_without_optional_review_is_an_ordinary_user_decision(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    nonce = nonce_for(http)
    res = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": nonce, "decision": "approved",
    })
    assert res.status_code == 200
    assert res.json()["decision"] == "approved"

    # And the gate is now open.
    state = http.get(f"/api/runs/{RUN}/state").json()
    direction = state["stages"][1]
    assert direction["status"] == "approved"
    assert state["next_stage"] == "material_plan"


def test_a_nonce_cannot_be_spent_twice(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    nonce = nonce_for(http)
    first = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": nonce, "decision": "returned", "note": "再看看",
    })
    assert first.status_code == 200
    second = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": nonce, "decision": "approved",
        "override_reason": "same nonce again",
    })
    assert second.status_code == 409


def test_decision_continuation_can_resume_without_repeating_approval_or_job(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box['http']
    box['model'] = ScriptedModel([final_turn('继续完成方案。')])
    jobs = box['app'].state.jobs
    original_check = jobs.before_submit

    def blocked():
        raise PipelineError('test_connection', '连接暂时不可用')

    jobs.before_submit = blocked
    payload = {**shown(http), 'stage': 'direction', 'nonce': nonce_for(http),
               'decision': 'returned', 'note': '请把开场改得更具体', 'continue_after': True}
    first = http.post(f'/api/runs/{RUN}/decision', json=payload)
    assert first.status_code == 200
    assert first.json()['continuation']['status'] == 'not_started'
    assert http.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation']['stage'] == 'direction'
    assert len(Store(box['tmp_path']).list_approvals(RUN, 'direction')) == 1

    jobs.before_submit = original_check
    retry = {**shown(http), 'stage': 'direction'}
    resumed = http.post(f'/api/runs/{RUN}/decision-continuation', json=retry)
    assert resumed.status_code == 200, resumed.text
    again = http.post(f'/api/runs/{RUN}/decision-continuation', json=retry)
    assert again.status_code == 200
    assert again.json()['job_id'] == resumed.json()['job_id']
    events = Store(box['tmp_path']).read_events(RUN)
    assert sum(e['kind'] == 'job_created' and e.get('request_id', '').startswith('decision:') for e in events) == 1
    assert http.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation'] is None
    assert len(Store(box['tmp_path']).list_approvals(RUN, 'direction')) == 1


def test_a_running_job_is_never_announced_as_a_continuation_that_has_not_started(client_factory):
    """Observed 2026-09-29: the card flashed under the user's cursor.

    ``pending_continuation`` only asked whether the continuation job had ever
    been *created*. Until that job row landed, "not created yet" was still true —
    so the card for a step that was already under way appeared and vanished again.
    """
    box = client_factory
    drive_intake_and_direction(box)
    http = box['http']
    box['model'] = ScriptedModel([final_turn('继续完成方案。')])
    jobs = box['app'].state.jobs
    store = Store(box['tmp_path'])

    original_check = jobs.before_submit

    def blocked():
        raise PipelineError('test_connection', '连接暂时不可用')

    # Write a decision whose continuation job cannot start: this is the window in
    # which the card is legitimate — the user has decided and nothing is running.
    jobs.before_submit = blocked
    payload = {**shown(http), 'stage': 'direction', 'nonce': nonce_for(http),
               'decision': 'returned', 'note': '请把开场改得更具体', 'continue_after': True}
    assert http.post(f'/api/runs/{RUN}/decision', json=payload).status_code == 200
    assert len(store.list_approvals(RUN, 'direction')) == 1
    def continuation_jobs():
        return [e for e in store.read_events(RUN) if e['kind'] == 'job_created'
                and str(e.get('request_id', '')).startswith('decision:')]
    assert continuation_jobs() == []
    assert http.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation'] is not None

    # Same stored state, but a job now holds the run: the step has started, so
    # there is nothing left to prompt for.
    jobs.active = (RUN, 'in-flight')
    try:
        assert jobs.busy(RUN) is True
        assert http.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation'] is None
    finally:
        jobs.active = None
        jobs.before_submit = original_check
    assert http.get(f'/api/runs/{RUN}/timeline').json()['pending_continuation'] is not None


def test_a_return_with_no_note_is_refused_by_the_route(client_factory):
    """The rule reaches the screen as a 400, not only as a Python exception.

    The screen asks for the note first (see `wireGate` in app.js), but a stale
    page or a replayed request goes straight to the route — so the route is
    where the rule has to hold, and this is the test that says it does. A note
    of spaces is not a note: that is what gets typed into a required field when
    the field is the only thing in the way.
    """
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    for note in ("", "   "):
        nonce = nonce_for(http)
        res = http.post(f"/api/runs/{RUN}/decision", json={
            **shown(http), "stage": "direction", "nonce": nonce, "decision": "returned", "note": note,
        })
        assert res.status_code == 409, f"{note!r} should not be an acceptable return"
        assert "note" in res.json()["detail"]

    nonce = nonce_for(http)
    ok = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": nonce, "decision": "returned",
        "note": "开场缺了容量对比",
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["note"] == "开场缺了容量对比"


def test_an_ungated_stage_has_no_nonce(client_factory):
    box = client_factory
    http = box["http"]
    make_run(http)
    assert http.post(f"/api/runs/{RUN}/nonce", params={"stage": "intake", "expected_artifact_sha256": "0" * 64, "expected_version": 1}).status_code == 400


def test_decision_rejects_non_hex_hash_without_consuming_nonce(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box['http']
    nonce = nonce_for(http)
    request = {**shown(http), 'stage': 'direction', 'nonce': nonce,
               'decision': 'returned', 'note': '需要重写开场'}
    bad = http.post(f'/api/runs/{RUN}/decision', json={**request, 'expected_artifact_sha256': 'z' * 64})
    assert bad.status_code == 422
    assert http.post(f'/api/runs/{RUN}/decision', json=request).status_code == 200


#: ② the shape the gate card grows buttons from: three options, one recommended.
#: Each option declares which ActiveBrief clause it satisfies — a draft written
#: against the current brief has to say so, or the gate refuses adoption (F2).
DIRECTION_PAYLOAD = {
    "options": [
        {"key": "A", "claim": "通勤路上八秒看懂", "satisfies": "goal.primary"},
        {"key": "B", "claim": "办公室午饭场景", "satisfies": "goal.primary"},
        {"key": "C", "claim": "容量实测", "satisfies": "goal.primary"},
    ],
    "recommended": "推荐 B",
}


def test_the_note_the_gate_card_offers_is_the_decision_the_route_records(client_factory):
    """②'s button and the approval row, held together end to end.

    The note is read off the timeline the screen draws and posted back verbatim.
    The card and the decision route are different layers, so a note the card
    offers but the route refuses would be a button that silently does nothing —
    the only way to catch that is to run both ends against each other.
    """
    box = client_factory
    drive_intake_and_direction(box, structured=DIRECTION_PAYLOAD)
    http = box["http"]
    # The review is its own agent turn with its own scripted model; recording
    # the verdict directly is what `request_review` ends up doing, and it keeps
    # this test about the gate rather than about the reviewer.
    Pipeline(Store(box["tmp_path"])).record_review(
        RUN, "direction", verdict="pass", findings=[]
    )

    pending = http.get(f"/api/runs/{RUN}/timeline").json()["pending"]
    assert pending["stage"] == "direction", "the gate is open on the stage with a choice"
    assert [o["key"] for o in pending["options"]] == ["A", "B", "C"]
    option = next(o for o in pending["options"] if not o["recommended"])

    nonce = nonce_for(http)
    res = http.post(f"/api/runs/{RUN}/decision", json={
        **shown(http), "stage": "direction", "nonce": nonce,
        "decision": "approved", "selected_option_id": option["key"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["decision"] == "approved"
    state = http.get(f"/api/runs/{RUN}/state").json()
    assert state["stages"][1]["approval"]["selected_option_id"] == option["key"]
    assert state["next_stage"] == "material_plan"

    # And the choice is legible afterwards, on the record he scrolls past.
    turns = http.get(f"/api/runs/{RUN}/timeline").json()["turns"]
    approvals = [i for t in turns for i in t["items"] if i["kind"] == "approval"]
    assert option["key"] in approvals[-1]["sentence"]


# ----------------------------------------------------------- conversation -- #


def test_message_streams_tool_activity_then_a_final_answer(client_factory):
    box = client_factory
    http = box["http"]
    make_run(http)
    box["model"] = ScriptedModel([
        tool_turn("t1", "read_project_state", {}),
        final_turn("现在什么都没有，先聊需求。"),
    ])
    res = http.post(f"/api/runs/{RUN}/message", json={"text": "现在什么情况"})
    events = stream_events(res)

    kinds = [e["type"] for e in events]
    assert "agent_tool" in kinds
    done = events[-1]
    assert done["type"] == "done"
    assert done["text"] == "现在什么都没有，先聊需求。"
    assert "read_project_state" in done["tools"]

    chat = http.get(f"/api/runs/{RUN}/chat").json()
    assert chat[0]["role"] == "user"
    assert chat[-1]["text"] == "现在什么都没有，先聊需求。"


def test_a_model_failure_surfaces_as_an_error_event_not_a_hang(client_factory):
    box = client_factory
    http = box["http"]
    make_run(http)

    class Broken:
        def complete(self, **kwargs):
            raise RuntimeError("endpoint unreachable")

    box["model"] = Broken()
    res = http.post(f"/api/runs/{RUN}/message", json={"text": "在吗"})
    events = stream_events(res)
    assert events[-1]["type"] == "error"
    assert "RuntimeError" in events[-1]["message"]
    assert "台账" in events[-1]["message"]

    # The failure was recorded on the ledger as a failed call.
    rows = http.get(f"/api/runs/{RUN}/ledger").json()["rows"]
    assert rows and rows[0]["ok"] is False


def test_budget_exhaustion_blocks_before_any_request(client_factory):
    box = client_factory
    http = box["http"]
    make_run(http)

    # Push spend past the cap by hand, then talk.
    store = Store(box["tmp_path"])
    from studio.ledger import Ledger
    ledger = Ledger(store, settings_for(box["tmp_path"], budget_limit_usd=0.001))
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=10_000_000, output_tokens=0)

    box["model"] = ScriptedModel([final_turn("不该被调用")])
    res = http.post(f"/api/runs/{RUN}/message", json={"text": "继续"})
    events = stream_events(res)
    assert events[-1]["type"] == "blocked"
    assert "cap" in events[-1]["message"]


def test_a_second_message_is_refused_while_one_is_still_running(client_factory):
    """One turn at a time per run.

    ``Store.append_event`` numbers a record by reading the whole log and adding
    one, and ``POST /message`` runs the turn on a bare daemon thread. Two turns
    interleaving there write duplicate ``seq`` values, and
    ``Pipeline._attempts`` rebuilds attempt state by scanning that log for
    ``{stage}#{number}`` — so the duplicates corrupt state rather than merely
    looking untidy.

    The first turn is held open by a model that blocks, which is the only way to
    get two requests genuinely in flight at once.
    """
    box = client_factory
    http = box["http"]
    make_run(http)

    started, release = threading.Event(), threading.Event()

    class SlowModel(ScriptedModel):
        def complete(self, **kwargs):
            started.set()
            release.wait(timeout=5)
            return super().complete(**kwargs)

    box["model"] = SlowModel([final_turn("好。")])
    first: dict = {}

    def send_first():
        first["res"] = http.post(f"/api/runs/{RUN}/message", json={"text": "第一句"})

    thread = threading.Thread(target=send_first)
    thread.start()
    try:
        assert started.wait(timeout=5), "the first turn should have started"

        second = http.post(f"/api/runs/{RUN}/message", json={"text": "第二句"})
        assert second.status_code == 409, "a concurrent turn must be refused"
        assert "未调用模型" in second.json()["detail"]
    finally:
        release.set()
        thread.join(timeout=10)

    assert first["res"].status_code == 200
    assert first["res"].text, "the refused request must not have disturbed the first"

    # And the guard releases: a message sent after the turn ends is accepted.
    box["model"] = ScriptedModel([final_turn("好。")])
    third = http.post(f"/api/runs/{RUN}/message", json={"text": "第三句"})
    assert third.status_code == 200


def test_artifact_endpoint_returns_content_and_fingerprint_check(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    listed = http.get(f"/api/runs/{RUN}/artifacts?stage=direction").json()
    assert [a["version"] for a in listed] == [1]

    data = http.get(f"/api/runs/{RUN}/artifacts/direction/1").json()
    assert data["content"] == "方向正文"
    assert data["integrity_ok"] is True
    assert data["author_role"] == "director"

    assert http.get(f"/api/runs/{RUN}/artifacts/direction/9").status_code == 404


def test_the_reader_endpoint_names_the_stage_and_author_in_his_words(client_factory):
    """The reader must not print ``director`` or ``direction-v1`` at him.

    The wording lives in ``vocabulary``; this checks it actually reaches the two
    endpoints the reader draws from, rather than only the timeline.
    """
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    listed = http.get(f"/api/runs/{RUN}/artifacts?stage=direction").json()
    assert listed[0]["version_label"] == "第 1 版"

    data = http.get(f"/api/runs/{RUN}/artifacts/direction/1").json()
    assert data["stage_label"] == "创意方向"
    assert data["numbered"] == "②创意方向"
    assert data["author"] == "导演"
    assert data["author_role"] == "director", "the raw role still travels for the ledger"


def test_the_timeline_endpoint_returns_turns_a_rail_and_the_cost(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    out = http.get(f"/api/runs/{RUN}/timeline").json()
    assert [s["stage"] for s in out["rail"]] == list(STAGES)
    assert out["turns"], "the message he sent produced a turn"
    assert out["turns"][0]["user"]["text"] == "做个保温袋视频"
    assert [i["stage"] for i in out["turns"][0]["items"] if i["kind"] == "artifact"] == [
        "intake", "direction",
    ]
    assert out["spend"]["calls"] > 0, "the header figure comes from the ledger"
    assert "$0.00" not in out["spend_sentence"] or "估算" in out["spend_sentence"]


def test_the_timeline_is_read_only(client_factory):
    """It is a view. Nothing that reaches it may change the run."""
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]

    before = http.get(f"/api/runs/{RUN}/state").json()
    http.get(f"/api/runs/{RUN}/timeline")
    assert http.get(f"/api/runs/{RUN}/state").json() == before


def test_events_http_never_returns_attempt_token(client, tmp_path):
    """Attempt tokens stay on disk for submit; HTTP must not leak them."""
    make_run(client)
    store = Store(tmp_path)
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt(RUN, "intake")
    body = client.get(f"/api/runs/{RUN}/events")
    assert body.status_code == 200
    assert attempt.token not in body.text
    started = next(e for e in body.json() if e.get("kind") == "attempt_started")
    assert started.get("token") is None
    disk = next(e for e in store.read_events(RUN) if e.get("kind") == "attempt_started")
    assert disk["token"] == attempt.token
    # /state must not leak the same token either.
    state = client.get(f"/api/runs/{RUN}/state")
    assert attempt.token not in state.text
    open_attempt = next(
        (s.get("open_attempt") for s in state.json()["stages"] if s.get("open_attempt")),
        None,
    )
    assert open_attempt is not None
    assert open_attempt.get("token") is None


# --------------------------------------------------------------------------- #
# Materials — the third write entry point
#
# The upload is the only route in this app that stores a file the browser sent,
# so what is checked here is mostly what it refuses.
# --------------------------------------------------------------------------- #


def test_the_materials_list_is_empty_before_anything_is_dropped_in(client):
    make_run(client)
    assert client.get(f"/api/runs/{RUN}/materials").json() == []


def test_uploading_a_photo_puts_it_on_the_shelf(client, tmp_path):
    make_run(client)

    res = client.post(
        f"/api/runs/{RUN}/materials",
        files={"file": ("shot.jpg", b"jpegbytes", "image/jpeg")},
    )

    assert res.status_code == 200, res.text
    assert res.json()["kind"] == "image"
    landed = Store(tmp_path).run_dir(RUN) / "materials" / "shot.jpg"
    assert landed.read_bytes() == b"jpegbytes"
    assert [m["name"] for m in client.get(f"/api/runs/{RUN}/materials").json()] == ["shot.jpg"]


def test_uploading_does_not_change_the_run(client, tmp_path):
    """What makes this a third *entry point* rather than a third *gate*.

    It writes a file and a ``material_uploaded`` event (so the evidence
    generation advances and A→B→A cannot revive an old approval), but it
    still creates no artifact and cannot approve anything.
    """
    make_run(client)
    store = Store(tmp_path)
    events_before = store.read_events(RUN)
    artifacts_before = len(store.list_artifacts(RUN))

    client.post(f"/api/runs/{RUN}/materials",
                files={"file": ("shot.jpg", b"x", "image/jpeg")})

    events_after = store.read_events(RUN)
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1]["kind"] == "material_uploaded"
    assert events_after[-1]["name"] == "shot.jpg"
    assert len(store.list_artifacts(RUN)) == artifacts_before


def test_a_served_photo_is_rendered_inline(client):
    make_run(client)
    client.post(f"/api/runs/{RUN}/materials",
                files={"file": ("shot.png", b"\x89PNG\r\n", "image/png")})

    res = client.get(f"/api/runs/{RUN}/materials/shot.png")

    assert res.status_code == 200
    assert res.content == b"\x89PNG\r\n"
    assert res.headers["content-type"].startswith("image/png")
    # No content-disposition, so clicking a photo shows it instead of saving it.
    assert "content-disposition" not in res.headers


def test_a_video_is_served_as_a_video(client):
    make_run(client)
    client.post(f"/api/runs/{RUN}/materials",
                files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftyp", "video/mp4")})

    res = client.get(f"/api/runs/{RUN}/materials/clip.mp4")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("video/mp4")


def test_a_missing_material_is_a_404(client):
    make_run(client)
    assert client.get(f"/api/runs/{RUN}/materials/nope.jpg").status_code == 404


def test_a_local_html_file_cannot_be_served_as_a_material(client, tmp_path):
    make_run(client)
    folder = Store(tmp_path).run_dir(RUN) / 'materials'
    folder.mkdir(exist_ok=True)
    (folder / 'audit.html').write_text('<p>local file</p>', encoding='utf-8')
    assert client.get(f'/api/runs/{RUN}/materials/audit.html').status_code == 404
    (folder / 'photo.png').write_bytes(b'\x89PNG\r\n')
    response = client.get(f'/api/runs/{RUN}/materials/photo.png')
    assert response.status_code == 200
    assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize(
    "name", ["..%2Frun.json", "..%5Crun.json", "%2E%2E%2Frun.json", "run.json"]
)
def test_a_traversal_in_the_url_never_serves_something_else(client, tmp_path, name):
    """This URL is the one string in the app that arrives from a browser."""
    make_run(client)
    (Store(tmp_path).run_dir(RUN) / "materials").mkdir(parents=True, exist_ok=True)

    res = client.get(f"/api/runs/{RUN}/materials/{name}")

    assert res.status_code == 404
    # run.json sits one level up and holds the brief; it must not come back.
    assert "给上班族带饭".encode() not in res.content


def test_upload_refuses_a_bad_name_over_http(client):
    make_run(client)
    res = client.post(f"/api/runs/{RUN}/materials",
                      files={"file": ("../escape.jpg", b"x", "image/jpeg")})
    assert res.status_code == 400
    assert "文件名不能用" in res.json()["detail"]


def test_upload_refuses_a_format_it_could_not_show(client):
    make_run(client)
    res = client.post(f"/api/runs/{RUN}/materials",
                      files={"file": ("notes.txt", b"x", "text/plain")})
    assert res.status_code == 400
    assert "只收图片和视频" in res.json()["detail"]


def test_upload_refuses_to_overwrite_over_http(client):
    make_run(client)
    for body in (b"first", b"second"):
        res = client.post(f"/api/runs/{RUN}/materials",
                          files={"file": ("shot.jpg", body, "image/jpeg")})
    assert res.status_code == 400
    assert "同名" in res.json()["detail"]


def test_a_chinese_filename_survives_upload_and_download(client):
    """His product photos are named like this."""
    make_run(client)
    client.post(f"/api/runs/{RUN}/materials",
                files={"file": ("多功能包 01.png", b"png", "image/png")})

    listed = client.get(f"/api/runs/{RUN}/materials").json()[0]
    res = client.get(listed["url"])

    assert listed["name"] == "多功能包 01.png"
    assert res.status_code == 200 and res.content == b"png"


def test_materials_need_a_real_run(client):
    assert client.get("/api/runs/no-such-run/materials").status_code == 404


def test_decision_routes_require_the_displayed_version(client_factory):
    box = client_factory
    drive_intake_and_direction(box)
    http = box["http"]
    assert http.post(f"/api/runs/{RUN}/nonce?stage=direction").status_code == 422
    assert http.post(f"/api/runs/{RUN}/decision", json={
        "stage": "direction", "nonce": "missing", "decision": "approved",
    }).status_code == 422
    expected = shown(http)
    nonce = nonce_for(http)
    pipeline = Pipeline(Store(box["tmp_path"]))
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="new version", summary="new")
    res = http.post(f"/api/runs/{RUN}/decision", json={
        **expected, "stage": "direction", "nonce": nonce, "decision": "approved",
        "override_reason": "read the old version",
    })
    assert res.status_code == 409
    assert "version_conflict" in res.text
    assert not pipeline.store.list_approvals(RUN, "direction")


def test_configured_endpoint_is_used_for_actual_client_construction(tmp_path, monkeypatch):
    import studio.agents.client as transport
    from studio.connections import ConnectionStore
    seen = []
    def client(base_url, token, **kwargs):
        seen.append((base_url, token))
        return ScriptedModel([final_turn("done")])
    monkeypatch.setattr(transport, "AnthropicCompatClient", client)
    config = ConnectionStore(tmp_path)
    config.save({"expected_revision": 0, "connections": [{"id": "synthetic",
        "provider": "anthropic_compat", "base_url": "https://example.invalid/anthropic",
        "api_key": "synthetic-test-only"}], "roles": {r: {"connection": "synthetic", "model": "test-model"}
            for r in ("orchestrator", "director", "material", "writer", "reviewer")}})
    with TestClient(build_app(settings_for(tmp_path))) as http:
        make_run(http)
        res = http.post(f"/api/runs/{RUN}/message", json={"text": "test"})
        assert stream_events(res)[-1]["type"] == "done"
        assert seen == [("https://example.invalid/anthropic", "synthetic-test-only")]
