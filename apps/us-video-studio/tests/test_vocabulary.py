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


def test_a_stale_step_is_not_shown_as_done():
    rail = v.build_rail([_stage("direction", latest={"version": 1}, stale=True)], next_stage=None)
    assert rail[0]["mark"] == "!"
    assert rail[0]["state"] == "stale"


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
