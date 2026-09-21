"""The plain-language layer.

The module's docstring makes two promises, so those are what is tested: it says
the consequence rather than the state name, and it never lets a caveat hide
behind a green light. Everything else here is the label tables staying honest.
"""
from __future__ import annotations

from studio import vocabulary as v


# --------------------------------------------------------------- labels -- #


def test_every_stage_and_role_has_a_name_in_his_language():
    from studio.models import STAGES

    for stage in STAGES:
        assert stage not in v.STAGE_LABEL.values(), "a label must not be the internal name"
        assert v.stage_label(stage) != stage, f"{stage} has no label"
    for role in ("orchestrator", "director", "material", "writer", "reviewer"):
        assert v.role_label(role) != role, f"{role} has no label"


def test_historical_accounting_stop_no_longer_asks_for_manual_reconciliation():
    note = v.failure_note('BudgetExceeded: unknown, partial or pending usage; reconcile provider evidence')
    assert '现已取消核账限制，可以继续' in note
    assert 'BudgetExceeded' not in note


def test_unknown_names_degrade_to_themselves_rather_than_blank():
    # A new stage added to the pipeline must still render as *something*.
    assert v.stage_label("storyboard") == "storyboard"
    assert v.role_label("composer") == "composer"
    assert v.stage_number("storyboard") == ""
    assert v.numbered_stage("storyboard") == "storyboard"


def test_stages_are_numbered_in_pipeline_order():
    assert v.numbered_stage("intake") == "①需求整理"
    assert v.numbered_stage("direction") == "②创意方向"
    assert v.numbered_stage("material_plan") == "③素材计划"
    assert v.numbered_stage("writing") == "④脚本"


def test_versions_read_as_versions_not_as_v_numbers():
    assert v.version_label(2) == "第 2 版"
    assert v.version_label(None) == "还没写"
    assert v.version_label(0) == "还没写"


# ------------------------------------------- the pass-with-findings case -- #


def test_a_failed_turn_keeps_the_real_error_alongside_the_plain_words():
    sentence = v.failure_note("RuntimeError: provider said no")
    assert "没能做完" in sentence
    assert "provider said no" in sentence, "he may need to quote this"


def test_a_pass_with_findings_does_not_read_as_a_bare_pass():
    """The bug this exists for.

    ``direction-v2`` was judged ``pass`` and carried four substantive findings.
    A screen that showed only 通过 would have let him approve without seeing them.
    """
    sentence = v.review_sentence("pass", [{"issue": f"问题 {i}"} for i in range(4)])
    assert "4" in sentence, "the count must appear"
    assert sentence != v.review_sentence("pass", [])
    assert "通过" in sentence, "it is still a pass — the findings are added, not traded away"


def test_a_clean_pass_says_it_was_clean():
    assert v.review_sentence("pass", []) == "审核通过了，没有提别的问题。"


def test_a_return_names_the_number_of_problems():
    sentence = v.review_sentence("return", [{"issue": "a"}, {"issue": "b"}])
    assert "退回" in sentence
    assert "2" in sentence


def test_the_history_reader_survives_a_review_event_that_carries_only_a_count():
    """``record_review`` writes ``findings=len(...)`` into the event log.

    The timeline normally skips that event because the artifact card says the
    same thing, but a run missing its card falls through to here, and ``len(4)``
    raises TypeError. A missing card must degrade, not crash the endpoint.
    """
    note = v.describe_history_event(
        {"kind": "review_recorded", "stage": "direction", "verdict": "pass", "findings": 4, "at": "t"}
    )
    assert note is not None
    assert "4" in note["text"]


def test_a_review_event_with_no_findings_field_at_all_also_survives():
    note = v.describe_history_event(
        {"kind": "review_recorded", "stage": "direction", "verdict": "pass", "at": "t"}
    )
    assert note["text"] == "审核通过了，没有提别的问题。"


# ------------------------------------------------------- consequences -- #


def test_stale_says_what_to_do_not_the_word_stale():
    sentence = v.stale_sentence("direction", "「需求整理」改过了")
    assert "stale" not in sentence
    assert "要重做" in sentence, "the consequence is the sentence"
    assert "创意方向" in sentence, "and the stage is in his words"


def test_a_broken_fingerprint_says_it_cannot_be_relied_on():
    sentence = v.integrity_sentence("direction")
    assert "改动过" in sentence
    assert "不能作为" in sentence


def test_a_truncated_attempt_says_what_happened():
    sentence = v.rejection_sentence("director", "direction", "max_tokens reached")
    assert "截断" in sentence
    assert "max_tokens" not in sentence, "the enum is not the explanation"
    assert "导演" in sentence


def test_an_exhausted_budget_is_named_as_a_budget_stop():
    sentence = v.rejection_sentence("director", "direction", "budget_exceeded")
    assert "预算" in sentence


def test_an_unrecognised_reason_is_still_quoted_rather_than_dropped():
    sentence = v.rejection_sentence("director", "direction", "provider returned 503")
    assert "503" in sentence


def test_an_override_approval_keeps_the_reason_he_gave():
    sentence = v.approval_sentence("approved_override", "这一版赶时间，先拍")
    assert "没通过" in sentence
    assert "这一版赶时间，先拍" in sentence


# --------------------------------------------------------- the money -- #


def test_tokens_are_written_in_units_a_person_reads():
    assert v.human_tokens(950) == "950"
    assert v.human_tokens(8300) == "8.3 千"
    assert v.human_tokens(101_267) == "10.1 万"
    # 16,000 must not round to "2 万": that overstates the spend by a quarter.
    assert v.human_tokens(16_000) == "1.6 万"
    assert v.human_tokens(10_000) == "1 万"
    assert v.human_tokens(100_000) == "10 万"


class _Spend:
    """The shape ``Ledger.totals()`` returns, which is all ``spend_sentence`` sees."""

    def __init__(self, i, o, calls, usd=0.0):
        self.input_tokens, self.output_tokens, self.calls = i, o, calls
        self.estimated_usd = usd


def test_an_unenforceable_rate_card_reads_as_unknown_not_as_free():
    """A zero that means "cannot compute" must not render as "cost nothing"."""
    sentence = v.spend_sentence(_Spend(80_000, 21_000, 14), usd_enforceable=False)
    assert "$0" not in sentence
    assert "没法算" in sentence
    assert "10.1 万" in sentence and "14" in sentence


def test_an_enforceable_rate_card_labels_the_number_as_an_estimate():
    sentence = v.spend_sentence(_Spend(80_000, 21_000, 14, usd=0.31), usd_enforceable=True)
    assert "$0.31" in sentence
    assert "估算" in sentence


# ------------------------------------------------------------ the rail -- #


def _stage(stage, **over):
    base = {
        "stage": stage,
        "latest": None,
        "status": "empty",
        "stale": False,
        "approval": None,
        "can_approve": False,
    }
    base.update(over)
    return base


def test_the_rail_marks_are_the_three_things_he_needs_to_tell_apart():
    rail = v.build_rail(
        [
            _stage("intake", latest={"version": 1}, status="approved", approval={"decision": "approved"}),
            _stage("direction", latest={"version": 2}, status="reviewed", can_approve=True),
            _stage("material_plan"),
            _stage("writing"),
        ],
        next_stage="material_plan",
    )
    marks = {step["stage"]: (step["mark"], step["state"]) for step in rail}
    assert marks["intake"] == ("✓", "done")
    assert marks["direction"] == ("●", "active")
    assert marks["material_plan"] == ("●", "active")
    assert marks["writing"] == ("○", "todo")


def test_a_step_waiting_on_him_is_flagged_even_when_it_is_marked_active():
    rail = v.build_rail([_stage("direction", latest={"version": 1}, status="reviewed",
                              can_approve=True)], next_stage=None)
    assert rail[0]["needs_you"] is True
    assert rail[0]["version"] == 1


def test_a_stage_the_reviewer_sent_back_is_not_marked_done():
    """It has a document, so it used to fall through to ✓.

    A returned stage is the one the reviewer just handed back, and it is the
    stage waiting on him — the exact case the amber 「this one is on you」 mark
    exists for. Marking it ✓ said the opposite.
    """
    rail = v.build_rail(
        [_stage("direction", latest={"version": 1}, status="returned", can_approve=True)],
        next_stage=None,
    )
    assert rail[0]["state"] == "active"
    assert rail[0]["mark"] == "●"


def test_a_step_that_needs_him_is_never_drawn_as_one_of_the_settled_states():
    """The stylesheet keys its request mark on `.step.active.needs`.

    That rule is only correct while "needs him" implies "active", so the
    implication is asserted rather than left as a fact about today's status
    vocabulary that the next status added would quietly break.

    Only three statuses can carry ``can_approve``, and the exclusions are the
    point rather than a convenience: ``pipeline.stage_state`` sets it only for a
    gated stage that has a document (so never ``empty``), that is not stale (so
    never ``stale``), has a review, and is not yet approved (so never
    ``approved``). Feeding those in would be asserting about a state the server
    cannot produce.
    """
    for status in ("returned", "reviewed", "awaiting_review"):
        rail = v.build_rail(
            [
                _stage(
                    "direction",
                    latest={"version": 1},
                    status=status,
                    review={"verdict": "return" if status == "returned" else "pass"},
                    can_approve=True,
                )
            ],
            next_stage=None,
        )
        assert rail[0]["needs_you"] is True, f"{status} should reach him as a request"
        assert rail[0]["state"] == "active", f"{status} needs him but is not active"


def test_a_stale_step_is_not_shown_as_done():
    rail = v.build_rail([_stage("direction", latest={"version": 1}, stale=True)], next_stage=None)
    assert rail[0]["mark"] == "!"
    assert rail[0]["state"] == "stale"


def test_a_step_carries_what_the_panel_draws_above_the_document():
    """The head of the stage panel: who wrote this version, when, and of what.

    Carried on the step rather than joined from `turns[].items` in the browser,
    which is the whole point of this dict having been built the way it was.
    """
    rail = v.build_rail(
        [
            _stage(
                "direction",
                latest={
                    "version": 2,
                    "summary": "三个方向，推荐 B。",
                    "author_role": "director",
                    "created_at": "2026-09-17T04:05:06",
                },
                status="reviewed",
            )
        ],
        next_stage=None,
    )
    step = rail[0]
    assert step["summary"] == "三个方向，推荐 B。"
    assert step["created_at"] == "2026-09-17T04:05:06"
    # His word for the role, not the wire name the file uses.
    assert step["author"] == "导演"


def test_an_empty_step_names_nobody_rather_than_a_blank_role():
    rail = v.build_rail([_stage("writing")], next_stage=None)
    assert rail[0]["author"] == ""
    assert rail[0]["summary"] == ""
    assert rail[0]["review"] is None


def test_the_review_on_a_step_is_the_whole_review_not_just_the_verdict():
    """A bare `pass` on the panel would let him approve without the four findings.

    Same failure `review_sentence` was written against — the sentence and the
    findings have to travel together, on both the card and the step.
    """
    rail = v.build_rail(
        [
            _stage(
                "direction",
                latest={"version": 1},
                status="reviewed",
                review={
                    "verdict": "pass",
                    "findings": [{"kind": "证据", "issue": "容量只有一条实测支撑"}],
                },
            )
        ],
        next_stage=None,
    )
    review = rail[0]["review"]
    assert review["verdict"] == "pass"
    assert review["sentence"] == v.review_sentence("pass", [{"kind": "证据"}])
    assert "1" in review["sentence"], "a pass with findings must not read as a bare pass"
    assert review["findings"] == [
        {"kind": "证据", "stage": "", "issue": "容量只有一条实测支撑"}
    ]


def test_a_file_changed_after_it_was_written_says_so_on_the_step():
    """The overlay reader used to print this. The panel is the reader now, so if
    the step does not carry it, nothing on the screen does."""
    rail = v.build_rail(
        [_stage("direction", latest={"version": 1}, integrity_ok=False)], next_stage=None
    )
    assert rail[0]["integrity_ok"] is False
    assert v.integrity_sentence("direction") == rail[0]["integrity_sentence"]
    assert rail[0]["integrity_sentence"]


def test_an_intact_step_carries_no_warning():
    rail = v.build_rail([_stage("direction", latest={"version": 1, "digest_algorithm": "artifact-v2"})], next_stage=None)
    assert rail[0]["integrity_ok"] is True
    assert rail[0]["integrity_sentence"] == ""
    assert rail[0]["stale_sentence"] == ""


def test_a_stale_step_says_why_in_the_panel():
    rail = v.build_rail(
        [
            _stage(
                "direction",
                latest={"version": 1},
                stale=True,
                stale_reason="an upstream stage was invalidated",
            )
        ],
        next_stage=None,
    )
    assert rail[0]["stale"] is True
    assert rail[0]["stale_sentence"] == v.stale_sentence(
        "direction", "an upstream stage was invalidated"
    )


# --------------------------------------------------------- live events -- #


def test_a_dispatch_names_the_worker_but_not_the_dispatcher():
    """'把「创意方向」派给了导演' already has the actor in it."""
    sentence = v.describe_tool_call("orchestrator", "dispatch",
                                    {"stage": "direction", "role": "director"}, ok=True)
    assert sentence == "把「创意方向」派给了导演"
    assert "主 Agent" not in sentence


def test_a_failed_tool_says_it_did_not_work():
    sentence = v.describe_tool_call("director", "write_stage", {"stage": "direction"}, ok=False)
    assert "没成功" in sentence
    assert "创意方向" in sentence


def test_an_unknown_tool_still_produces_a_readable_line():
    sentence = v.describe_tool_call("director", "frobnicate", {}, ok=True)
    assert "frobnicate" in sentence
    assert sentence.strip() != ""


def test_a_turn_with_no_tools_is_not_worth_a_line():
    assert v.describe_event({"type": "agent_turn", "role": "orchestrator", "tools": 0}) == ""


def test_being_cut_off_mid_thought_is_worth_a_line():
    sentence = v.describe_event(
        {"type": "agent_turn", "role": "orchestrator", "stop_reason": "max_rounds"}
    )
    assert "叫停" in sentence


# ------------------------------------------- what he can click -- #


def test_a_drafted_answer_carries_the_question_it_answers():
    """The answer arrives as prose, so it has to say which question it is.

    Without the question in it the orchestrator matches answer back to question
    by guessing, and a wrong guess is a stage built on the wrong fact.
    """
    sentence = v.answer_prefix("主推哪个 SKU？")
    assert "主推哪个 SKU？" in sentence
    assert sentence.endswith("："), "it is a prefix; it does not end the thought"


def test_the_answers_header_leaves_the_count_to_the_screen():
    assert "{n}" in v.ANSWERS_HEADER, "the count is substituted, never guessed"


def test_each_open_question_carries_the_line_its_answer_becomes():
    questions = v.build_questions([
        _stage("direction", latest={
            "version": 2, "digest_algorithm": "artifact-v2", "sha256": "a" * 64,
            "structured": {"open_questions": [
                {"question": "主推哪个 SKU？", "blocks": "素材计划定不了", "owner": "user", "timing": "now", "reason": "商品选择"},
                {"question": "要不要露脸", "blocks": "", "owner": "user", "timing": "now", "reason": "表达偏好"},
            ]},
        }),
    ])
    assert [q["send_prefix"] for q in questions] == [
        v.answer_prefix("主推哪个 SKU？"),
        v.answer_prefix("要不要露脸"),
    ]
    # The draft is keyed by version, so a rewritten stage drops the old answers
    # rather than letting one ride onto a document it was not written for.
    assert questions[0]["version"] == 2
    assert questions[0]["stage"] == "direction"


def test_the_two_option_buttons_differ_in_what_they_do_not_in_which_letter():
    """One keeps the director's recommendation, one sends the stage back.

    A pair reading 「A」 and 「B」 would leave him to remember which click means
    what — the exact failure this control exists to prevent.
    """
    keep = v.option_button_label("B", True)
    change = v.option_button_label("A", False)
    assert "B" in keep and "A" in change
    assert keep != change
    assert keep != v.option_button_label("B", False), "the letter alone is not the label"


def test_the_legend_on_a_gate_with_options_names_both_outcomes():
    assert "采用决定" in v.GATE_OPTION_HINT
    assert "退回" in v.GATE_OPTION_HINT


def test_the_reason_recorded_for_a_rework_keeps_the_recommendation_it_ignored():
    """An approval row is permanent. 「改用 A」 alone would lose the fact that a
    recommendation was passed over, which is the part worth reading later."""
    note = v.option_return_note("A", "B")
    assert "A" in note
    assert "B" in note


def test_returned_or_stale_approval_cannot_look_done():
    for stale, decision in ((False, "returned"), (True, "approved")):
        step = _stage("direction", latest={"version": 1, "digest_algorithm": "artifact-v2"},
                      status="reviewed", stale=stale,
                      approval={"decision": decision, "selected_option_id": "B"})
        row = v.build_rail([step], None)[0]
        assert row["state"] != "done"
        assert "B" in row["decision_sentence"]
