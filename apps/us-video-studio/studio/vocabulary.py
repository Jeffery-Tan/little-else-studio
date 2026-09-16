"""The plain-language layer.

This module exists because the workbench is used by one person who is not a
programmer, and the internal names are not his language. ``intake`` is not a word
he uses; "需求整理" is. A review that came back as ``verdict: pass`` with four
findings is not "passed"; it is "通过了，但提了 4 点要注意".

Two rules, and they are the whole point of the module:

* **Say the consequence, not the state.** Not "stale" but "上游改过了，这份要重做".
  A state name tells him nothing about what it means for him.
* **Never hide a caveat behind a green light.** A pass with findings is shown as a
  pass *with the findings*. The reviewer writes those findings for a reason.

It lives in Python rather than JavaScript so there is exactly one copy of the
wording, and so it is testable. The frontend only draws what this returns.
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

#: Internal stage -> what he calls it. Order matters: it is the rail order.
STAGE_LABEL: dict[str, str] = {
    "intake": "需求整理",
    "direction": "创意方向",
    "material_plan": "素材计划",
    "writing": "脚本",
}

_CIRCLED = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨")

#: Roles as job titles. "material" is not a person he can picture; 素材统筹 is.
ROLE_LABEL: dict[str, str] = {
    "orchestrator": "主 Agent",
    "director": "导演",
    "material": "素材统筹",
    "writer": "编剧",
    "reviewer": "审核",
    "user": "你",
    "system": "系统",
}

STATUS_LABEL: dict[str, str] = {
    "empty": "还没开始",
    "done": "已写完",
    "awaiting_review": "审核中",
    "reviewed": "审核完了",
    "returned": "被打回",
    "approved": "你已通过",
}

#: How each agent tool reads as a sentence. ``{stage}`` is filled with the label.
TOOL_SENTENCE: dict[str, str] = {
    "read_project_state": "看了一下项目现在的情况",
    "read_artifact": "读了「{stage}」",
    "list_reviews": "翻了「{stage}」的历次审核",
    "write_stage": "写了「{stage}」",
    "dispatch": "把「{stage}」派给了{role}",
    "request_review": "把「{stage}」送去审核",
    "submit_stage": "交回了「{stage}」",
    "finish_review": "给出了审核结论",
}


def stage_label(stage: str) -> str:
    return STAGE_LABEL.get(stage, stage)


def stage_number(stage: str) -> str:
    """``direction`` -> ``②``. Empty string for a stage we do not know."""
    keys = list(STAGE_LABEL)
    if stage not in keys:
        return ""
    return _CIRCLED[keys.index(stage)]


def numbered_stage(stage: str) -> str:
    n = stage_number(stage)
    return f"{n}{stage_label(stage)}" if n else stage_label(stage)


def role_label(role: str) -> str:
    return ROLE_LABEL.get(role, role)


def status_label(status: str) -> str:
    return STATUS_LABEL.get(status, status)


def version_label(version: int | None) -> str:
    return "还没写" if not version else f"第 {version} 版"


# --------------------------------------------------------------------------- #
# Sentences about artifacts, reviews, approvals
# --------------------------------------------------------------------------- #


def stale_sentence(stage: str, reason: str = "") -> str:
    """Say what happened and what it costs him, not the word "stale"."""
    head = f"上游改过了：{reason}。" if reason else "上游改过了。"
    return f"{head}这份「{stage_label(stage)}」已经是旧的，要重做。"


def integrity_sentence(stage: str) -> str:
    return f"「{stage_label(stage)}」这份文件在保存之后被改动过，不能作为审核或批准的依据。"


def review_sentence(verdict: str, findings: list[dict[str, Any]]) -> str:
    """A pass with findings must never read as a bare pass.

    Observed on a real run: ``direction-v2`` was judged ``pass`` and carried four
    substantive findings. A UI that shows only the word 通过 would let him
    approve without ever seeing them.
    """
    return _review_count_sentence(verdict, len(findings))


def _review_count_sentence(verdict: str, n: int) -> str:
    """The sentence, from a count. Both callers funnel through here so the
    wording cannot drift between the live card and the reloaded timeline."""
    if verdict == "pass":
        if n:
            return f"审核通过了，但提了 {n} 点要注意——通过只代表它在自己声明的范围内成立。"
        return "审核通过了，没有提别的问题。"
    if n:
        return f"审核把它退回了，有 {n} 条问题。"
    return "审核把它退回了。"


def approval_sentence(decision: str, note: str = "") -> str:
    if decision == "approved_override":
        return f"你在审核没通过的情况下批准了它。理由：{note}" if note else "你在审核没通过的情况下批准了它。"
    if decision == "approved":
        return "你批准了这一版。"
    return "你把它退回了。" + (f" 理由：{note}" if note else "")


def rejection_sentence(role: str, stage: str, reason: str = "") -> str:
    """Humanize why an attempt produced nothing.

    ``max_tokens`` is a real observed failure — a director burned a whole attempt
    on it. "结尾被截断" says what happened; the raw enum does not.
    """
    who = role_label(role)
    what = stage_label(stage)
    if "max_tokens" in reason:
        return f"{who}这一次写得太长被截断了，没能交出「{what}」，要重来。"
    if "budget" in reason.lower() or "token" in reason.lower():
        return f"开发预算用完了，{who}没法继续写「{what}」。"
    if reason:
        return f"{who}这一次没能交出「{what}」：{reason}"
    return f"{who}这一次没能交出「{what}」。"


# --------------------------------------------------------------------------- #
# What a step says when there is nothing to draw
# --------------------------------------------------------------------------- #


def stage_blank(stage: str, st: dict[str, Any]) -> dict[str, str]:
    """A stage with no card to show — as one of four different situations.

    The old screen said 「还没有内容」 for all of them, which is wrong three times
    out of four. Nothing-has-happened is a reason to wait; work-in-progress is a
    reason to wait *differently*; a failed attempt is a reason to look; and a
    document that came out empty is a reason to open it. Collapsing them cost him
    the only signal that would have told him which.

    Takes the stage's own state dict, so it stays right on reload and on the live
    stream without either side recomputing anything.
    """
    label = stage_label(stage)
    if st.get("stale"):
        return {"state": "stale", "text": stale_sentence(stage, st.get("stale_reason", ""))}
    if st.get("latest") is None:
        if st.get("open_attempt"):
            who = role_label(
                st["open_attempt"].get("actor", "").split(":")[0]
                or owner_role(stage)
            )
            return {"state": "running", "text": f"{who}正在写「{label}」，还没交回来。"}
        if st.get("last_rejection"):
            return {
                "state": "failed",
                "text": rejection_sentence(owner_role(stage), stage, st["last_rejection"]),
            }
        if st.get("blocked_reason"):
            # The raw reason names stages and attempt keys; the panel says the
            # consequence and the machine-detail view keeps the original.
            return {"state": "waiting", "text": f"「{label}」还不能开始，要等上游先就绪。"}
        return {
            "state": "todo",
            "text": f"「{label}」还没开始，这一步由{role_label(owner_role(stage))}负责。",
        }
    if st.get("open_attempt"):
        who = role_label(st["open_attempt"].get("actor", "").split(":")[0] or owner_role(stage))
        return {"state": "running", "text": f"{who}正在写新的一版「{label}」，还没交回来。"}
    if st.get("review") is None and st.get("status") == "awaiting_review":
        return {"state": "reviewing", "text": f"「{label}」写完了，正在等审核。"}
    return {"state": "todo", "text": f"「{label}」已经写好了。"}


# --------------------------------------------------------------------------- #
# What the next step is likely to cost
# --------------------------------------------------------------------------- #


def cost_sentence(cost: dict[str, Any] | None) -> str:
    """What the next step cost the last time it ran.

    Sits on the button he is about to press, the way Higgsfield prints the price
    on ``Generate``. Tokens are the fact; dollars are labelled as an estimate and
    dropped entirely when the rate card has no rates, because a dollar figure
    computed from zero rates reads as free.

    Never a prediction: it is the last measured run, said as one.
    """
    if cost is None:
        return "这一步还从没跑过，花多少说不上来。"
    line = f"上次做这一步用了约 {human_tokens(cost['tokens'])} token（{cost['calls']} 次调用）"
    if cost.get("usd_is_real"):
        line += f"，约 ${cost['estimated_usd']:.2f}（估算）"
    return line + "。"


# --------------------------------------------------------------------------- #
# Live events (the SSE stream)
# --------------------------------------------------------------------------- #


def tool_args_stage(args: dict[str, Any]) -> str:
    """The stage a tool call was about, as a label. Empty when not applicable."""
    stage = args.get("stage") if isinstance(args, dict) else None
    return stage_label(stage) if stage else ""


def describe_tool_call(role: str, tool: str, args: dict[str, Any], ok: bool) -> str:
    """One agent tool call, as a sentence he can read.

    Unknown tools fall back to their raw name inside a readable frame rather than
    being dropped — a blank line would be worse than a slightly technical one.
    """
    who = role_label(role)
    template = TOOL_SENTENCE.get(tool)
    if template is None:
        sentence = f"用了 {tool}"
    elif tool == "dispatch":
        # ``{role}`` here is the worker being dispatched *to*, not the caller.
        # Filling it with the caller produced "把「创意方向」派给了主 Agent",
        # which names the one agent that definitely is not doing the work.
        target = args.get("role") if isinstance(args, dict) else None
        sentence = template.format(stage=tool_args_stage(args), role=role_label(target or ""))
    else:
        sentence = template.format(stage=tool_args_stage(args), role=who)
    if tool == "dispatch":
        # The sentence already names the worker; prefixing "主 Agent" would read
        # as the director dispatching to itself.
        return sentence if ok else f"{sentence}，但没成功"
    return f"{who}{sentence}" if ok else f"{who}想{sentence}，但没成功"


def describe_event(ev: dict[str, Any]) -> str:
    """A live SSE event -> a sentence, or "" when there is nothing worth saying.

    An ``agent_turn`` with no tool calls is not worth a line: the reply text
    itself is about to appear. A ``max_rounds`` stop is worth a line, because it
    means the agent was cut off mid-thought.
    """
    kind = ev.get("type")
    if kind == "agent_tool":
        return describe_tool_call(
            ev.get("role", ""), ev.get("tool", ""), ev.get("args") or {}, bool(ev.get("ok"))
        )
    if kind == "agent_turn":
        if ev.get("stop_reason") == "max_rounds":
            who = role_label(ev.get("role", ""))
            return f"{who}做的步骤太多，被中途叫停了，这一轮不完整。"
        return ""
    return ""


# --------------------------------------------------------------------------- #
# History (the persisted event log)
# --------------------------------------------------------------------------- #


def _finding_count(findings: Any) -> int:
    """How many findings, whether the caller holds the list or just the count.

    ``pipeline.record_review`` writes ``findings=len(...)`` into the event log —
    a count, not the findings themselves. The timeline normally pairs a review
    with its artifact card and never reads that field, but a run whose card is
    missing (or a legacy run) would fall through to here, and ``len(4)`` is a
    TypeError, not a degraded sentence. Accept both rather than crash.
    """
    if isinstance(findings, int):
        return findings
    return len(findings or [])


def failure_note(text: str) -> str:
    """A turn that died before answering.

    ``Orchestrator.send`` persists one of these on any exception, precisely
    because the SSE error is not persisted: without it, reloading after a
    failure shows his question followed by silence. The technical text is kept
    verbatim — it is the actual error, and he may need to quote it.
    """
    return f"这一轮没能做完：{text}"


def describe_history_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """A persisted pipeline event -> a timeline note, or None to skip it.

    ``dispatch_done`` is deliberately skipped: it duplicates the artifact card
    that the same dispatch produced, and two cards for one piece of work reads as
    two pieces of work. It is still used for the machine-detail panel.
    """
    kind = ev.get("kind")
    stage = ev.get("stage", "")
    at = ev.get("at", "")

    if kind == "attempt_started":
        return {
            "kind": "note",
            "at": at,
            "text": f"把「{stage_label(stage)}」交给了{role_label(ev.get('role', ''))}",
        }
    if kind == "attempt_submitted":
        return {
            "kind": "note",
            "at": at,
            "text": f"{role_label(owner_role(stage))}写完了「{stage_label(stage)}」",
        }
    if kind == "attempt_rejected":
        return {
            "kind": "note",
            "at": at,
            "text": rejection_sentence(owner_role(stage), stage, ev.get("reason", "")),
        }
    if kind == "review_recorded":
        return {
            "kind": "note",
            "at": at,
            "text": _review_count_sentence(ev.get("verdict", ""), _finding_count(ev.get("findings"))),
        }
    return None


def owner_role(stage: str) -> str:
    """Who owns a stage.

    ``attempt_submitted`` and ``attempt_rejected`` record no role — only the
    stage. The stage's owner is the honest answer, and it is the same fact the
    pipeline itself uses to route work.
    """
    from .models import STAGE_SPEC

    spec = STAGE_SPEC.get(stage)
    return spec.owner_role if spec else ""


# --------------------------------------------------------------------------- #
# Money, in units a person reads
# --------------------------------------------------------------------------- #


def human_tokens(n: int) -> str:
    """Tokens in units he reads, without rounding away the magnitude.

    One decimal is kept at every scale: rounding 16,000 to "2 万" overstates it
    by a quarter, and this is the number that says how much of his development
    budget is gone.
    """
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f} 千".replace(".0 千", " 千")
    return f"{n / 10_000:.1f} 万".replace(".0 万", " 万")


def spend_sentence(spend: Any, usd_enforceable: bool) -> str:
    """The header line. Tokens are facts; dollars are a guess and are labelled so.

    When the rate card has no rates the dollar figure means "cannot be computed",
    not "free" — and it must say that, or a zero reads as reassurance.
    """
    tokens = spend.input_tokens + spend.output_tokens
    line = f"已用约 {human_tokens(tokens)} token · {spend.calls} 次调用"
    if usd_enforceable:
        return f"{line} · 约 ${spend.estimated_usd:.2f}（估算）"
    return f"{line} · 花多少钱还没法算（价目表未确认）"


# --------------------------------------------------------------------------- #
# The step rail
# --------------------------------------------------------------------------- #


def build_rail(stages: list[dict[str, Any]], next_stage: str | None) -> list[dict[str, Any]]:
    """Top-of-screen progress, and the four stage views behind it.

    Each step reports one of three things: done (✓), the one being worked on now
    (●), or not started (○). A step that needs his click says so.

    Each step also carries everything the panel behind it draws: the blocks the
    stage's document was laid out into, and the questions it left open. Carried
    here rather than fetched per click because the timeline already has it all in
    memory, and a second request per step would only create a way for the rail
    and the panel to disagree.
    """
    from .structured import view

    rail: list[dict[str, Any]] = []
    for st in stages:
        stage = st["stage"]
        has_doc = st.get("latest") is not None
        if st.get("approval"):
            mark, state = "✓", "done"
        elif st.get("stale"):
            mark, state = "!", "stale"
        elif has_doc and st.get("status") in ("reviewed", "awaiting_review"):
            mark, state = "●", "active"
        elif has_doc:
            mark, state = "✓", "done"
        elif stage == next_stage:
            mark, state = "●", "active"
        else:
            mark, state = "○", "todo"

        structured = (st.get("latest") or {}).get("structured") or {}
        blank = stage_blank(stage, st)
        rail.append(
            {
                "stage": stage,
                "number": stage_number(stage),
                "label": stage_label(stage),
                "mark": mark,
                "state": state,
                "has_doc": has_doc,
                "version": (st.get("latest") or {}).get("version"),
                "version_label": version_label((st.get("latest") or {}).get("version")),
                "needs_you": bool(st.get("can_approve")),
                "blank": blank,
                "blocks": view(stage, structured),
                "questions": [
                    q
                    for q in (structured.get("open_questions") or [])
                    if isinstance(q, dict) and q.get("question")
                ],
            }
        )
    return rail


def build_questions(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every unanswered question, in stage order, each saying what it blocks.

    The screen's standing question to him: what is this waiting on you for. It
    has no dismiss button and no read flag — a question stops being open when the
    stage that raised it is rewritten without it, which is the only honest way to
    close one.
    """
    out: list[dict[str, Any]] = []
    for st in stages:
        stage = st["stage"]
        structured = (st.get("latest") or {}).get("structured") or {}
        for row in structured.get("open_questions") or []:
            if not isinstance(row, dict) or not row.get("question"):
                continue
            out.append(
                {
                    "stage": stage,
                    "stage_label": stage_label(stage),
                    "numbered": numbered_stage(stage),
                    "question": row["question"],
                    "blocks": row.get("blocks", ""),
                    "version": (st.get("latest") or {}).get("version"),
                }
            )
    return out
