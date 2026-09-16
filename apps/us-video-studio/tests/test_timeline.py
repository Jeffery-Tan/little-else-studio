"""Turning a run directory into the conversation the screen shows.

The screen has one column, so the test that matters most is the ordering one:
what he said must appear above the work it caused, and the work must appear
above the answer. Everything else checks that a card is one card, and that the
numbers in the header come from the ledger rather than from a guess.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

import studio.ledger
import studio.pipeline
import studio.store
from studio.config import Pricing, Settings
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.timeline import build_timeline

RUN = "lunch-bag-01"


def settings_for(tmp_path):
    return Settings(
        data_root=tmp_path,
        base_url="https://example.invalid/anthropic",
        budget_limit_usd=10.0,
        pricing=Pricing(source="test", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
                        as_of="2026-09-17"),
    )


@pytest.fixture()
def clock(monkeypatch):
    """One distinct, increasing timestamp per write.

    ``utcnow()`` has one-second resolution, so a test that runs in milliseconds
    stamps a message and the document it produced with the *same* second. Real
    turns are seconds apart, and a timeline that only groups correctly when the
    clock happens to tick is not grouping correctly. Stepping the clock makes
    these tests look like a real run — ``test_items_sharing_a_timestamp_are_not_lost``
    covers the tie case on purpose.
    """
    ticks = itertools.count()
    base = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)

    def now() -> str:
        return (base + timedelta(seconds=next(ticks))).strftime("%Y-%m-%dT%H:%M:%SZ")

    for module in (studio.store, studio.pipeline, studio.ledger):
        monkeypatch.setattr(module, "utcnow", now)
    return now


@pytest.fixture()
def env(tmp_path, clock):
    store = Store(tmp_path)
    store.create_run(RUN, "保温午餐包 15 秒", "给上班族带饭的保温袋，做一条 15 秒的短视频。")
    pipeline = Pipeline(store)
    ledger = Ledger(store, settings_for(tmp_path))
    return store, pipeline, ledger


def write_intake(pipeline, text="需求：保温袋，15 秒短视频。"):
    attempt = pipeline.start_attempt(RUN, "intake")
    return pipeline.submit(RUN, "intake", attempt.token, content=text, summary="intake 整理完毕")


def write_direction(pipeline, text="# 三条方向\n\n- A\n- B", summary="三个方向"):
    attempt = pipeline.start_attempt(RUN, "direction")
    return pipeline.submit(RUN, "direction", attempt.token, content=text, summary=summary)


def timeline(store, pipeline, ledger):
    return build_timeline(store, pipeline, ledger, RUN)


def cards(turn):
    """The artifact cards in a turn, in order. A turn also carries notes."""
    return [i for i in turn["items"] if i["kind"] == "artifact"]


# --------------------------------------------------------------- ordering -- #


def test_a_message_comes_before_the_work_it_triggered(env):
    """The ordering bug, end to end.

    ``Orchestrator.send`` used to run the whole loop before persisting the user's
    message, so the question carried the timestamp of the moment its answer
    finished — the question sorted *below* the document it produced. The fix was
    to persist first; this checks the timeline reads in the order that produces.
    """
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "先把这个包的需求整理一下。")
    write_intake(pipeline)
    store.append_chat(RUN, "assistant", "整理好了，你看一眼。")

    turns = timeline(store, pipeline, ledger)["turns"]
    assert len(turns) == 1
    turn = turns[0]
    assert turn["user"]["text"] == "先把这个包的需求整理一下。"
    assert turn["reply"]["text"] == "整理好了，你看一眼。"
    # The announcement comes first, then the card it announces: he reads
    # "把「需求整理」交给了主 Agent" and *then* the document itself.
    assert [i["kind"] for i in turn["items"]] == ["note", "artifact"]
    assert "需求整理" in turn["items"][0]["text"]
    assert cards(turn)[0]["stage"] == "intake"


def test_each_message_starts_its_own_turn(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "先整理需求。")
    write_intake(pipeline)
    store.append_chat(RUN, "assistant", "好了。")
    store.append_chat(RUN, "user", "出三个方向。")
    write_direction(pipeline)
    store.append_chat(RUN, "assistant", "方向在这里。")

    turns = timeline(store, pipeline, ledger)["turns"]
    assert [t["user"]["text"] for t in turns] == ["先整理需求。", "出三个方向。"]
    assert [cards(t)[0]["stage"] for t in turns] == ["intake", "direction"]
    assert [t["reply"]["text"] for t in turns] == ["好了。", "方向在这里。"]


def test_work_done_before_he_said_anything_still_appears(env):
    """Legacy runs have this shape: the first message carries a late timestamp."""
    store, pipeline, ledger = env
    write_intake(pipeline)
    store.append_chat(RUN, "user", "继续。")

    turns = timeline(store, pipeline, ledger)["turns"]
    assert len(turns) == 2
    assert turns[0]["user"] is None, "the orphan turn holds the work"
    assert cards(turns[0])[0]["stage"] == "intake"
    assert turns[1]["user"]["text"] == "继续。"


# ----------------------------------------------------- one card per job -- #


def test_a_submitted_artifact_is_one_card_not_a_note_and_a_card(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)

    items = timeline(store, pipeline, ledger)["turns"][0]["items"]
    assert sum(1 for i in items if i["kind"] == "artifact") == 1
    assert not any(i["kind"] == "note" and "写完了" in i["text"] for i in items), \
        "the note duplicates the card"


def test_a_reviewed_artifact_carries_its_verdict_on_the_card(env):
    """A pass with findings must reach the card. See tests/test_vocabulary.py."""
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(
        RUN, "direction", verdict="pass",
        findings=[{"kind": "gap", "issue": f"问题 {i}"} for i in range(4)],
    )

    turns = timeline(store, pipeline, ledger)["turns"]
    card = [i for t in turns for i in t["items"] if i["kind"] == "artifact"
            and i["stage"] == "direction"][0]
    assert card["review"]["verdict"] == "pass"
    assert len(card["review"]["findings"]) == 4
    assert "4" in card["review"]["sentence"]
    assert card["author"] == "导演", "the card names the role in his language"


def test_the_review_event_does_not_appear_as_a_second_card(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(
        RUN, "direction", verdict="return", findings=[{"kind": "gap", "issue": "主推 SKU 没定"}]
    )

    items = [i for t in timeline(store, pipeline, ledger)["turns"] for i in t["items"]]
    notes = [i["text"] for i in items if i["kind"] == "note"]
    assert not any("退回" in n for n in notes), (
        "a reviewed artifact is skipped as a note because its card carries the verdict"
    )


def test_a_failed_turn_is_visible_after_a_reload(env):
    """The error event is not persisted; this record is what survives.

    Without a note here the timeline dropped it, so reloading after a failure
    showed the question followed by silence — which is exactly what the
    orchestrator writes the record to prevent.
    """
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "试试看")
    store.append_chat(RUN, "system", "RuntimeError: provider said no")

    items = timeline(store, pipeline, ledger)["turns"][0]["items"]
    text = " ".join(i["text"] for i in items if i["kind"] == "note")
    assert "没能做完" in text
    assert "provider said no" in text, "the real error is kept verbatim"


def test_a_rejected_attempt_survives_as_a_note_because_no_card_exists(env):
    """A rejection produces no artifact, so the note is the only trace of it."""
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    attempt = pipeline.start_attempt(RUN, "intake")
    pipeline.reject(RUN, "intake", attempt.token, "max_tokens reached")

    items = timeline(store, pipeline, ledger)["turns"][0]["items"]
    text = " ".join(i["text"] for i in items if i["kind"] == "note")
    assert "截断" in text


# ------------------------------------------------------ waiting on him -- #


def test_nothing_is_pending_until_a_gated_stage_is_written(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    assert timeline(store, pipeline, ledger)["pending"] is None


def test_the_pending_decision_names_the_stage_and_rides_the_last_turn(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    store.append_chat(RUN, "user", "出个方向。")
    write_direction(pipeline)
    pipeline.record_review(
        RUN, "direction", verdict="pass", findings=[{"kind": "gap", "issue": "素材没核实"}]
    )

    out = timeline(store, pipeline, ledger)
    pending = out["pending"]
    assert pending["kind"] == "decision_request"
    assert pending["stage"] == "direction"
    assert pending["passed"] is True
    assert "注意" in pending["headline"], "a pass with findings says so in the headline"
    assert len(pending["findings"]) == 1
    assert out["turns"][-1]["items"][-1] is pending


def test_a_returned_stage_asks_him_in_its_own_words(env):
    """A return reaches him as a decision, worded so the override is not free.

    The card must not read like the pass card with a different first line: what
    he is being offered here is *going against the reviewer*, and the body has to
    say that a reason will be demanded.
    """
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "出个方向。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(
        RUN, "direction", verdict="return", findings=[{"kind": "gap", "issue": "主推 SKU 没定"}]
    )

    state = pipeline.state(RUN).to_dict()
    direction = next(s for s in state["stages"] if s["stage"] == "direction")
    assert direction["status"] == "returned"

    pending = timeline(store, pipeline, ledger)["pending"]
    assert pending is not None
    assert pending["passed"] is False
    assert "没有通过" in pending["headline"]
    assert "仍然通过" in pending["body"]
    assert "理由" in pending["body"], "the cost of the override is stated up front"
    assert [f["issue"] for f in pending["findings"]] == ["主推 SKU 没定"]


def test_a_returned_version_still_reaches_him_as_a_card(env):
    """No decision to make, but he must still see *why* it was sent back."""
    store, pipeline, ledger = env
    write_intake(pipeline)
    store.append_chat(RUN, "user", "出个方向。")
    write_direction(pipeline)
    pipeline.record_review(
        RUN, "direction", verdict="return", findings=[{"kind": "gap", "issue": "主推 SKU 没定"}]
    )

    card = [i for t in timeline(store, pipeline, ledger)["turns"]
            for i in t["items"] if i["kind"] == "artifact"][-1]
    assert card["review"]["verdict"] == "return"
    assert card["review"]["findings"][0]["issue"] == "主推 SKU 没定"
    assert "退回" in card["review"]["sentence"]


# --------------------------------------------------- timestamps that tie -- #


def test_items_sharing_a_timestamp_are_not_lost(env, monkeypatch):
    """Every write stamps the same second — the shape a fast run has.

    The first version grouped with ``start <= at < end``. Over equal bounds that
    window is empty, so the middle turns came back with no items and the renderer
    fell over on the missing card. Bisecting cannot lose an item; the worst case
    is that it lands in the neighbouring turn.
    """
    frozen = "2026-09-17T12:00:00Z"
    for module in (studio.store, studio.pipeline, studio.ledger):
        monkeypatch.setattr(module, "utcnow", lambda: frozen)

    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "先整理需求。")
    write_intake(pipeline)
    store.append_chat(RUN, "assistant", "好了。")
    store.append_chat(RUN, "user", "出三个方向。")
    write_direction(pipeline)
    store.append_chat(RUN, "assistant", "方向在这里。")

    turns = timeline(store, pipeline, ledger)["turns"]
    assert [t["user"]["text"] for t in turns] == ["先整理需求。", "出三个方向。"]
    stages = [i["stage"] for t in turns for i in t["items"] if i["kind"] == "artifact"]
    assert stages == ["intake", "direction"], "no artifact may be dropped"
    assert all(t["detail"] for t in turns), "every turn still reports its cost"


# --------------------------------------------------------------- money -- #


def test_the_header_spend_comes_from_the_ledger_not_from_the_state(env):
    """Regression: the first version read spend out of ``pipeline.state()``.

    ``state()`` does not carry spend — the state *route* bolts it on afterwards.
    So the header read zero on every run, which is the one number on screen that
    must not be wrong.
    """
    store, pipeline, ledger = env
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=8000, output_tokens=3000)
    ledger.record(RUN, role="reviewer", stage="direction", model="deepseek-flash",
                  input_tokens=4000, output_tokens=1000)

    out = timeline(store, pipeline, ledger)
    assert out["spend"]["calls"] == 2
    assert out["spend"]["input_tokens"] == 12000
    assert "1.6 万" in out["spend_sentence"], "16,000 must not round to 2 万"
    assert "2 次调用" in out["spend_sentence"]


def test_the_header_says_the_dollar_cap_cannot_stop_anything_yet(env):
    """The test rate card has rates, so this asserts the other branch."""
    store, pipeline, ledger = env
    out = timeline(store, pipeline, ledger)
    assert out["budget"]["pricing"]["usd_cap_enforceable"] is True
    assert "估算" in out["spend_sentence"]


def test_cross_run_spend_is_reported_separately(env):
    """$10 is a development budget across every run, not a per-run allowance."""
    store, pipeline, ledger = env
    other = store.create_run("bag-02", "另一个包", "brief")
    ledger.record(RUN, role="director", stage="direction", model="m",
                  input_tokens=1000, output_tokens=0)
    ledger.record(other.run_id, role="director", stage="direction", model="m",
                  input_tokens=9000, output_tokens=0)

    out = timeline(store, pipeline, ledger)
    assert out["spend"]["input_tokens"] == 1000, "the run's own figure"
    assert "1 万" in out["all_runs_sentence"], "and the development total is separate"
    assert "跨所有项目" in out["all_runs_sentence"]


# ------------------------------------------------------ machine detail -- #


def test_machine_detail_aggregates_by_role_for_the_turn(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "出个方向。")
    write_intake(pipeline)
    write_direction(pipeline)
    ledger.record(RUN, role="director", stage="direction", model="m",
                  input_tokens=5000, output_tokens=1000)
    ledger.record(RUN, role="director", stage="direction", model="m",
                  input_tokens=3000, output_tokens=500)
    ledger.record(RUN, role="reviewer", stage="direction", model="m",
                  input_tokens=2000, output_tokens=500)

    detail = timeline(store, pipeline, ledger)["turns"][-1]["detail"]
    assert detail["calls"] == 3
    by_role = {r["role"]: r for r in detail["roles"]}
    assert by_role["director"]["calls"] == 2
    assert by_role["director"]["input_tokens"] == 8000
    assert by_role["director"]["label"] == "导演"
    assert detail["roles"][0]["role"] == "director", "busiest role first"


def test_a_turn_with_no_model_calls_reports_zero_rather_than_breaking(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "现在什么情况？")
    detail = timeline(store, pipeline, ledger)["turns"][0]["detail"]
    assert detail == {"calls": 0, "input_tokens": 0, "output_tokens": 0, "roles": []}


# ------------------------------------------------------------ the rail -- #


def test_the_rail_follows_the_run_state(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)

    rail = timeline(store, pipeline, ledger)["rail"]
    assert [s["stage"] for s in rail] == ["intake", "direction", "material_plan", "writing"]
    assert rail[0]["mark"] == "✓"
    assert all(s["label"] and not s["label"].isascii() for s in rail)


def test_an_empty_run_produces_an_empty_timeline_rather_than_an_error(env):
    store, pipeline, ledger = env
    out = timeline(store, pipeline, ledger)
    assert out["turns"] == []
    assert out["pending"] is None
    assert out["spend"]["calls"] == 0
    assert out["run"]["title"] == "保温午餐包 15 秒"


# ------------------------------------------------- four states, not one -- #


def test_the_four_blank_situations_read_differently(env):
    """「还没有内容」 was the old answer to all four. Only one of them means wait.

    A stage with nothing started, one being written, one whose attempt failed and
    one waiting on an upstream are four different things to do about it, and the
    screen used to say the same seven characters for every one.
    """
    store, pipeline, ledger = env

    def blank(stage):
        rail = timeline(store, pipeline, ledger)["rail"]
        return next(s for s in rail if s["stage"] == stage)["blank"]

    # Not started, and nothing in its way: intake requires nothing.
    assert blank("intake")["state"] == "todo"

    # Waiting: material_plan requires direction, which has no document yet. Said
    # as a consequence rather than as the pipeline's own English reason string.
    waiting = blank("material_plan")
    assert waiting["state"] == "waiting"
    assert "要等上游" in waiting["text"]

    # Failed: an attempt was opened and closed without a submission.
    attempt = pipeline.start_attempt(RUN, "intake")
    pipeline.reject(RUN, "intake", attempt.token, "stop_reason=max_tokens")
    failed = blank("intake")
    assert failed["state"] == "failed"
    assert "截断" in failed["text"]

    # Running: an attempt is open right now.
    pipeline.start_attempt(RUN, "intake", actor="orchestrator")
    running = blank("intake")
    assert running["state"] == "running"
    assert "正在写" in running["text"]


def test_a_stage_that_finished_empty_keeps_its_summary(env):
    """The fourth state: the document exists but the model produced no shape."""
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)  # no structured payload at all

    out = timeline(store, pipeline, ledger)
    intake_card = cards(out["turns"][-1])[0]
    assert intake_card["blocks"] == [], "nothing to lay out, so nothing is drawn"
    assert intake_card["summary"], "the card still carries something to read"
    rail = next(s for s in out["rail"] if s["stage"] == "intake")
    assert rail["has_doc"] is True
    assert rail["blank"]["state"] == "todo"


# ------------------------------------------- the layout rides with the card -- #


def test_a_card_carries_the_stage_laid_out(env):
    store, pipeline, ledger = env
    write_intake(pipeline)
    store.append_chat(RUN, "user", "出方向。")
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(
        RUN, "direction", attempt.token, content="正文", summary="三个方向",
        structured={
            "options": [
                {"key": "A", "claim": "外观", "hook": "特写"},
                {"key": "B", "claim": "容量", "hook": "装饭"},
            ],
            "recommended": "B",
            "dependency_matrix": [{"need": "主推 SKU", "options": {"A": True, "B": False}}],
            "open_questions": [{"question": "主推哪个 SKU？", "blocks": "依赖矩阵整行"}],
        },
    )

    card = cards(timeline(store, pipeline, ledger)["turns"][-1])[0]
    assert [b["type"] for b in card["blocks"]] == ["cards", "callout", "matrix"]
    assert [c["emphasis"] for c in card["blocks"][0]["cards"]] == ["", "chosen"]
    assert [q["question"] for q in card["questions"]] == ["主推哪个 SKU？"]


def test_the_payload_survives_the_sidecar(env):
    """The layout is read back off disk, not off the object just submitted.

    Every other test here reads a run written in the same process, so a field
    that never reached the sidecar would still look present.
    """
    store, pipeline, ledger = env
    write_intake(pipeline)
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(
        RUN, "direction", attempt.token, content="正文", summary="s",
        structured={"recommended": "B", "prerequisites": ["拿到官方图"]},
    )

    fresh = Pipeline(Store(store.root))
    artifact = fresh.store.latest_artifact(RUN, "direction")
    assert artifact.structured["recommended"] == "B"
    assert artifact.structured["prerequisites"] == ["拿到官方图"]


# ------------------------------------------ what the run is waiting on him for -- #


def test_unanswered_questions_from_every_stage_are_collected(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    attempt = pipeline.start_attempt(RUN, "intake")
    pipeline.submit(
        RUN, "intake", attempt.token, content="正文", summary="s",
        structured={
            "open_questions": [
                {"question": "这条片子投哪里？", "blocks": "交付形态"},
                {"question": "有没有实拍素材？", "blocks": "素材计划"},
            ]
        },
    )
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(
        RUN, "direction", attempt.token, content="正文", summary="s",
        structured={"open_questions": [{"question": "主推哪个 SKU？", "blocks": "方向 A"}]},
    )

    questions = timeline(store, pipeline, ledger)["questions"]
    assert [q["question"] for q in questions] == [
        "这条片子投哪里？", "有没有实拍素材？", "主推哪个 SKU？",
    ]
    assert questions[0]["numbered"] == "①需求整理"
    assert questions[2]["stage_label"] == "创意方向"
    assert questions[2]["blocks"] == "方向 A"


def test_a_question_row_with_no_question_is_not_listed(env):
    """The normaliser keeps a row whose only filled cell is `blocks`."""
    store, pipeline, ledger = env
    attempt = pipeline.start_attempt(RUN, "intake")
    pipeline.submit(
        RUN, "intake", attempt.token, content="正文", summary="s",
        structured={"open_questions": [{"blocks": "某件事"}]},
    )
    assert timeline(store, pipeline, ledger)["questions"] == []


# ------------------------------------------------ the price on the button -- #


def _record(store, role, stage, tokens, at="2026-09-17T11:00:00Z", run_id=RUN):
    store.append_ledger(
        run_id,
        studio.ledger.LedgerEntry(
            at=at, run_id=run_id, role=role, stage=stage, model="m",
            input_tokens=tokens, output_tokens=0, estimated_usd=0.0,
            pricing_source="test",
        ),
    )


def approve(pipeline, stage):
    """The click. A passing review is not an approval — the gate is his."""
    nonce = pipeline.issue_nonce(RUN, stage)
    return pipeline.record_approval(RUN, stage, nonce=nonce, decision="approved")


def test_the_gate_card_says_what_the_next_step_has_cost(env):
    """Higgsfield prints the price on Generate; this is the same idea.

    The figure is the last measured run of that role on that stage, not a
    forecast, and the sentence says so.
    """
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    _record(store, "material", "material_plan", 45_000)

    pending = timeline(store, pipeline, ledger)["pending"]
    assert pending["next_stage_label"] == "素材计划"
    assert pending["next_owner"] == "素材统筹"
    assert "4.5 万 token" in pending["next_cost_sentence"]
    assert "上次" in pending["next_cost_sentence"]


def test_a_step_that_has_never_run_promises_nothing(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])

    pending = timeline(store, pipeline, ledger)["pending"]
    assert "从没跑过" in pending["next_cost_sentence"]


def test_the_last_gate_says_there_is_nothing_after_it(env):
    store, pipeline, ledger = env
    store.append_chat(RUN, "user", "整理需求。")
    write_intake(pipeline)
    write_direction(pipeline)
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    approve(pipeline, "direction")
    attempt = pipeline.start_attempt(RUN, "material_plan")
    pipeline.submit(RUN, "material_plan", attempt.token, content="素材", summary="s")
    attempt = pipeline.start_attempt(RUN, "writing")
    pipeline.submit(RUN, "writing", attempt.token, content="脚本", summary="s")
    pipeline.record_review(RUN, "writing", verdict="pass", findings=[])

    pending = timeline(store, pipeline, ledger)["pending"]
    assert pending["stage"] == "writing"
    assert pending["next_stage_label"] == ""
    assert "走完了" in pending["next_cost_sentence"]


def test_last_stage_cost_reads_the_newest_run_of_that_step(env):
    """Across runs, because the run that needs the figure is the one that has not
    reached the step yet — the only runs that have it are older ones."""
    store, pipeline, ledger = env
    store.create_run("lunch-bag-02", "第二个案例", "brief")
    _record(store, "writer", "writing", 10_000, at="2026-09-17T10:00:00Z")
    _record(store, "writer", "writing", 40_000, at="2026-09-17T12:00:00Z", run_id="lunch-bag-02")

    cost = ledger.last_stage_cost("writer", "writing")
    assert cost["tokens"] == 40_000, "the newer run, not the sum of both"
    assert cost["at"] == "2026-09-17T12:00:00Z"
    assert cost["run_id"] == "lunch-bag-02"
    assert ledger.last_stage_cost("writer", "material_plan") is None


def test_the_cost_line_is_a_measurement_not_a_forecast(env):
    store, pipeline, ledger = env
    _record(store, "writer", "writing", 1_000)

    sentence = studio.vocabulary.cost_sentence(ledger.last_stage_cost("writer", "writing"))
    assert sentence.startswith("上次做这一步用了")
    assert studio.vocabulary.cost_sentence(None) == "这一步还从没跑过，花多少说不上来。"


def test_the_cost_line_drops_dollars_when_the_rate_card_has_no_rates(tmp_path):
    """A dollar figure computed from zero rates reads as free, not as unknown."""
    store = Store(tmp_path)
    store.create_run(RUN, "t", "b")
    ledger = Ledger(store, Settings(
        data_root=tmp_path, base_url="https://example.invalid",
        budget_limit_usd=10.0,
        pricing=Pricing(source="unverified-placeholder", input_usd_per_mtok=0.0,
                        output_usd_per_mtok=0.0, as_of="2026-09-17"),
    ))
    _record(store, "writer", "writing", 40_000)

    sentence = studio.vocabulary.cost_sentence(ledger.last_stage_cost("writer", "writing"))
    assert "token" in sentence
    assert "$" not in sentence
