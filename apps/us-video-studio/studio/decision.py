"""What the user can decide right now, and how a message becomes that decision.

Three paths used to write a user decision three different ways: the browser
clicked a button, the conversation asked the user to go click one, and a
continuation replayed what a click had already recorded. The conversation had no
way to *make* a decision at all, so "采用 B，继续出分镜" was a sentence the system
could not act on.

This module is the one place that answers two questions:

* What can the user decide right now? (``candidates`` — derived from run state,
  never from the message)
* Is this proposed action the one the user actually expressed? (``resolve``)

The split matters. ``candidates`` and ``resolve`` are **pure**: no store, no
pipeline, no I/O, so every rule is exhaustively testable. Writing happens only in
``commit``, which derives the candidates *again* under the run lock and re-runs
the same check, so a decision can never be recorded against a state that moved
while it was being validated.

Two classes of action, and the class is a property of the action rather than of
the words used to ask for it:

``creative``
    No external side effect, reversible, re-decidable through a new version.
    Adopting a direction, adopting or returning a script, amending the brief.
    A conversation may commit these directly.

``execution``
    Produces a provider request or a cost. A conversation may only prepare it;
    submitting stays behind the workbench's own one-time authorisation, bound to
    exact inputs. Nothing here can submit anything.

What this module deliberately does **not** do: read the user's message to guess
intent. Understanding the sentence is the agent's job. This module's job is to
refuse an action that the run's current state does not actually offer, and to ask
instead of guessing when more than one decision is open.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import PipelineError, STAGE_SPEC

#: Action classes. ``execution`` never commits from conversation.
CREATIVE = "creative"
EXECUTION = "execution"

#: Actions that state a target outright. They may resolve inside a set of
#: alternatives, because the user named which one they meant.
SELECTING = frozenset({"select_direction"})

#: Actions that accept a target the run already offers. These may only resolve
#: from a message when exactly one decision is open.
CONFIRMING = frozenset({"approve_stage", "return_stage"})


class DecisionError(PipelineError):
    """A decision cannot be recorded, and the reason is specific."""


#: Which actions adopt the version rather than send it back. The mapping lives in
#: one place so the two channels cannot disagree about what a decision means.
ADOPTING = frozenset({"select_direction", "approve_stage", "amend_brief"})


def record(store, run_id, pipeline, *, stage: str, artifact_sha256: str, version: int,
           decision: str, source: str, nonce: str, note: str = "",
           override_reason: str = "", selected_option_id: str | None = None,
           continue_after: bool = True, continuation_note: str = "",
           action_id: str = "", action_class: str = "", request_id: str = "") -> Any:
    """The one place a user decision is written down.

    Both channels end here — the browser's click and a decision the user stated
    in conversation. They differ in two facts and nothing else:

    * ``source``, which is recorded for the audit trail; and
    * where the ``nonce`` comes from. A click carries the one the browser was
      issued and never leaves the browser's hands. A conversation decision gets
      one issued inside this call, so no agent ever holds a usable value and the
      decision cannot be replayed.

    Everything that decides whether the decision *counts* lives below this line
    and is therefore identical for both: the exact artifact version and
    fingerprint, the version's staleness and integrity, one decision per version,
    and the requirement that an adoption names a real option.
    """
    if source not in ("ui", "conversation"):
        raise DecisionError("decision", f"unknown decision source: {source!r}")
    if decision not in ("approved", "returned"):
        raise DecisionError("decision", f"unknown decision: {decision!r}")
    with store.run_lock(run_id):
        approval = pipeline.record_approval(
            run_id, stage,
            nonce=nonce,
            decision=decision,
            note=note,
            override_reason=override_reason,
            selected_option_id=selected_option_id,
            expected_artifact_sha256=artifact_sha256,
            expected_version=int(version),
            continue_after=continue_after,
            source=source,
            continuation_note=continuation_note,
        )
        if action_id:
            store.append_event(run_id, "user_action_resolved",
                               action_id=action_id, action_class=action_class,
                               target={"stage": stage, "version": int(version),
                                       "artifact_sha256": artifact_sha256,
                                       **({"option_id": selected_option_id}
                                          if selected_option_id else {})},
                               source=source, request_id=request_id or "",
                               selected_option_id=approval.selected_option_id,
                               has_continuation_note=bool(approval.continuation_note))
    return approval


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def pending_decisions(store, pipeline, run_id, ledger=None) -> list[dict[str, Any]]:
    """Every decision the user is actually being asked for.

    Read through ``timeline._pending_action`` rather than re-deriving it. That
    projection is already the single answer to "what is waiting on him", and a
    second derivation here is how the screen and the chat would drift apart.

    ``ledger`` is only used by that projection to put a cost sentence on the card.
    A caller that has one should pass it; otherwise a read-only ledger is built
    over the same store, which reads and never writes.
    """
    if store.read_run(run_id).kind != "creative":
        return []
    from .project_brief import latest_execution
    from .timeline import build_timeline

    if ledger is None:
        from .config import load_settings
        from .ledger import Ledger
        ledger = Ledger(store, load_settings(store.root))
    timeline = build_timeline(store, pipeline, ledger, run_id,
                              latest_execution(store, run_id))
    pending = timeline.get("pending")
    return [pending] if pending else []


def candidates(store, pipeline, run_id) -> list[dict[str, Any]]:
    """The decisions this run is waiting on, each with the answers it allows.

    A **question**, not a flat list of actions. The distinction is what makes the
    short-reply rule correct: "行" may answer a question that has one obvious
    answer, but it can never choose between adopting a version and returning it —
    those are opposite outcomes, and guessing which one the user meant is how a
    short reply records an irreversible thing nobody asked for.

    Each entry carries:

    ``question``
        The one thing the user is being asked, and how many answers it has.
    ``answers``
        The legitimate answers. The agent points at one of their ``action`` ids;
        it cannot invent one, and it cannot substitute a recommendation for the
        user's choice.
    """
    out: list[dict[str, Any]] = []
    for row in pending_decisions(store, pipeline, run_id):
        stage = str(row.get("stage") or "")
        version = row.get("version")
        sha = str(row.get("artifact_sha256") or "")
        if not stage or version is None or not sha:
            continue
        label = str(row.get("stage_label") or stage)
        kind = str(row.get("kind") or "decision_request")
        target = {"stage": stage, "version": int(version), "artifact_sha256": sha}
        options = [{"id": str(o.get("key")), "label": str(o.get("label") or o.get("key"))}
                   for o in (row.get("options") or [])
                   if o.get("key") and bool(o.get("admissible", True))]
        answers: list[dict[str, Any]] = []

        if kind == "brief_amendment":
            amend = row.get("amend") or {}
            if amend.get("valid"):
                answers.append({
                    "action": "amend_brief", "class": CREATIVE,
                    "label": f"按这一版修订有效简报，本版作废后重做",
                    "effect": "本版作废，团队按新简报重做；不产生媒体费用。",
                    "payload": {"clause_id": amend.get("clause_id"),
                                "op": amend.get("op") or "replace", "text": amend.get("text")},
                })
            answers.append({
                "action": "return_stage", "class": CREATIVE,
                "label": f"退回{label}并按意见修改",
                "effect": "责任角色按你的意见出新版；旧版保留。",
                "requires": ["note"],
            })
            out.append({"question": f"这份{label}没有可以采用的方向，你想怎么改？",
                        "kind": kind, "stage": stage, "version": int(version),
                        "artifact_sha256": sha, "label": label, "target": target,
                        "answers": answers})
            continue

        if options:
            # Adopting a version that proposes options *is* naming one of them.
            # There is no separate "adopt as-is": the option is the decision.
            answers.append({
                "action": "select_direction", "class": CREATIVE,
                "label": f"采用{label}的某个方向",
                "effect": "记录你选的方向，团队按它继续；不产生媒体费用。",
                "options": options, "requires": ["option_id"],
            })
        else:
            answers.append({
                "action": "approve_stage", "class": CREATIVE,
                "label": f"采用当前{label}",
                "effect": "团队按它继续下一步；不产生媒体费用。",
            })
        answers.append({
            "action": "return_stage", "class": CREATIVE,
            "label": f"退回{label}并按意见修改",
            "effect": "责任角色按你的意见出新版；旧版保留。",
            "requires": ["note"],
        })
        out.append({"question": f"这一版{label}要采用还是退回？",
                    "kind": kind, "stage": stage, "version": int(version),
                    "artifact_sha256": sha, "label": label, "target": target,
                    "answers": answers})
    return out


def actions(decisions: list[dict]) -> list[dict]:
    """Flatten decisions into the actions an agent may point at.

    Each carries its decision's ``target``, so an action can never be detached
    from the exact version it answers.
    """
    out = []
    for decision in decisions:
        for answer in decision["answers"]:
            out.append({**answer, "target": dict(decision["target"]),
                        "stage": decision["stage"], "version": decision["version"],
                        "artifact_sha256": decision["artifact_sha256"],
                        "label": answer["label"]})
    return out


def prepare_generation_candidate(store, pipeline, run_id, kind="image") -> dict[str, Any] | None:
    """The one execution authorisation this project could open next, or None.

    Read-only. It says what a confirmation *would* cover; it does not create one
    and cannot submit anything. The numbers come from the same production
    projection the workbench page shows, so the card in the conversation and the
    page cannot describe different sets.
    """
    from .production import _read_state_without_service

    state = _read_state_without_service(store, pipeline, run_id)
    coverage = state["shots"].get(kind) or {}
    if not coverage.get("complete_list") or not coverage.get("ready"):
        return None
    draft_ids = []
    for row in coverage.get("shots") or []:
        if row.get("draft_id") and row.get("is_current", True) is not False:
            draft_ids.append(row["draft_id"])
    if not draft_ids:
        return None
    drafts = {d["draft_id"]: d for d in state["drafts"]}
    frozen = [{"draft_id": d, "draft_sha256": drafts[d].get("draft_sha256")}
              for d in sorted(set(draft_ids))]
    known = [drafts[f["draft_id"]].get("estimate", {}).get("amount") for f in frozen]
    total = sum(a for a in known if isinstance(a, (int, float)))
    return {
        "id": "authorise_generation", "class": EXECUTION, "kind": kind,
        "target": {"kind": kind, "source_sha256": (state["targets"].get(kind) or {}).get("source_sha256"),
                   "drafts": frozen, "snapshot": _sha(frozen)},
        "label": f"准备生成 {len(frozen)} 项",
        "effect": "这一步只准备；真正的提交仍需你在制作界面确认一次。",
        "count": len(frozen),
        "estimate": {"known": all(isinstance(a, (int, float)) for a in known), "amount": total or None},
    }


def action_id_of(action: dict | None) -> str:
    """The action an answer names, under either spelling.

    A resolved answer uses ``action``; a proposal on its way in may use ``id``.
    Accepting both in one place keeps the two shapes from drifting apart.
    """
    if not isinstance(action, dict):
        return ""
    return str(action.get("action") or action.get("id") or "").strip()


def resolve(decisions: list[dict], proposed: dict | None, *, source: str = "conversation") -> dict:
    """Decide what a proposed answer means. Pure; no state, no I/O.

    Returns one of:

    ``commit``
        The proposal is one of the legitimate answers to a question the run is
        actually asking, bound to the exact version the user was shown.
    ``clarify``
        More than one question is open, or the proposal names something the run
        does not offer. Guessing here is what turns a short reply into the wrong
        irreversible record.
    ``reject``
        The proposal is internally inconsistent: an option that does not exist,
        a version that is not the current one, an answer whose required detail
        is missing.
    """
    proposed = proposed or {}
    action_id = action_id_of(proposed)
    if not action_id:
        return {"status": "clarify", "reason": "no_action_proposed",
                "question": _clarify_question(decisions)}

    target = proposed.get("target") or {}
    stage = str(target.get("stage") or proposed.get("stage") or "")
    version = target.get("version", proposed.get("version"))
    sha = str(target.get("sha256") or target.get("artifact_sha256")
              or proposed.get("artifact_sha256") or "")

    pool = list(decisions)
    if stage:
        pool = [d for d in pool if d["stage"] == stage]
    if version is not None:
        pool = [d for d in pool if int(version) == int(d["version"])]
    if sha:
        pool = [d for d in pool if sha == d["artifact_sha256"]]
    if not pool:
        # Either nothing is open, or the proposal names a version that is no
        # longer the one waiting. Both are "reject" only if something *is* open.
        if not decisions:
            return {"status": "clarify", "reason": "nothing_open",
                    "question": _clarify_question(decisions)}
        return {"status": "reject", "reason": "version_not_current",
                "message": "这个动作指向的版本不是当前等待你决定的版本。"}

    if len(pool) > 1:
        return {"status": "clarify", "reason": "several_decisions_open",
                "question": _clarify_question(pool)}

    decision = pool[0]
    answer = next((a for a in decision["answers"] if a["action"] == action_id), None)
    if answer is None:
        return {"status": "clarify", "reason": "action_not_available",
                "question": _clarify_question([decision])}

    action = {**answer, "target": dict(decision["target"]),
              "stage": decision["stage"], "version": decision["version"],
              "artifact_sha256": decision["artifact_sha256"]}

    if answer["action"] == "select_direction":
        option = str(target.get("option_id") or proposed.get("option_id") or "").strip().upper()
        offered = {o["id"].upper() for o in answer.get("options") or []}
        if not option:
            # "采用 B" needs a B. Without one the choice belongs to the user, and
            # the recommended option is not a substitute for their answer.
            return {"status": "clarify", "reason": "option_not_named", "action": action,
                    "question": "你想采用哪个方向？" +
                                "／".join(o["id"] for o in answer.get("options") or []) + "。"}
        if option not in offered:
            return {"status": "reject", "reason": "option_not_available",
                    "message": f"当前{decision['label']}这一版里没有可采用的方向 {option}。"}
        action["target"] = {**action["target"], "option_id": option}
        return {"status": "commit", "action": action}

    if answer["action"] == "return_stage":
        note = str(proposed.get("note") or "").strip()
        if not note:
            return {"status": "clarify", "reason": "return_needs_note", "action": action,
                    "question": "退回需要说明要改什么，否则责任角色只能猜。"}
        action["note"] = note
        return {"status": "commit", "action": action}

    # A plain adoption. It answers the run's one open question, so a bare
    # confirmation is enough — but only when that question is the *only* one.
    if len(decisions) != 1:
        return {"status": "clarify", "reason": "several_decisions_open",
                "question": _clarify_question(decisions)}
    return {"status": "commit", "action": action}


def _clarify_question(decisions: list[dict]) -> str:
    if not decisions:
        return "现在没有等你决定的事项。你想让我做什么？"
    if len(decisions) == 1:
        return str(decisions[0].get("question") or "你在回答哪一件事？")
    return "现在有几件事等你决定，你指的是哪一件？" + "".join(
        f"{index}. {decision.get('question') or decision.get('label')}"
        for index, decision in enumerate(decisions, 1))


def accepting_user_words(note: str, user_message: str) -> str:
    """The user's own words, or nothing.

    A decision is adopted as it stands; what follows it may carry an instruction
    from the user ("B, but make the opening less like an ad"). That instruction is
    the *user's* sentence, so it has to be quoted from their message rather than
    rewritten by the model — otherwise the model is authoring a requirement the
    user never stated, which is the same failure as deciding for them, one step
    later.

    This is not keyword matching and does not decide whether a decision exists:
    the decision was already verified against the run's own state. It only checks
    that an instruction attributed to the user really is theirs.
    """
    value = " ".join(str(note or "").split()).strip()
    if not value:
        return ""
    haystack = " ".join(str(user_message or "").split())
    return value[:2000] if value in haystack else ""


def commit(store, pipeline, run_id, action: dict, *, source: str, user_message: str = "",
           note: str = "", request_id: str = "") -> dict:
    """Record one verified creative decision, and say what it opens next.

    The candidates are derived again here, under the run lock. That second
    derivation is not redundant: it is what makes "verified" mean anything when
    the state can move between checking and writing.
    """
    stage = str((action.get("target") or {}).get("stage") or "")
    if stage not in STAGE_SPEC or not STAGE_SPEC[stage].gated:
        raise DecisionError("decision", f"{stage or '这个动作'} 不是由你直接决定的阶段。")
    if source == "ui":
        raise DecisionError("decision", "这个入口只记录对话中表达的决定；界面点击走原确认入口。")
    wanted = action_id_of(action)
    if not wanted:
        raise DecisionError("decision", "这个动作没有说明是哪一件事。")

    with store.run_lock(run_id):
        fresh = candidates(store, pipeline, run_id)
        match = None
        for decision in fresh:
            if decision["stage"] != stage:
                continue
            if decision["artifact_sha256"] != action["target"]["artifact_sha256"]:
                continue
            answer = next((a for a in decision["answers"] if a["action"] == wanted), None)
            if answer is not None:
                match = {**answer, "target": dict(decision["target"])}
                break
        if match is None:
            raise DecisionError("decision_conflict",
                                "等待你决定的内容已经变化，请查看当前版本后再决定。")
        target = match["target"]
        decision = "approved" if match["action"] in ADOPTING else "returned"
        option = action.get("option_id") or (action.get("target") or {}).get("option_id")
        continuation = accepting_user_words(note or action.get("continuation_note") or "",
                                           user_message)
        # The nonce is issued by the pipeline and only ever written down as a
        # hash. Its plaintext stays inside this call and is returned to nobody, so
        # a conversation decision cannot be replayed by an agent — no agent ever
        # sees a usable value.
        nonce = pipeline.issue_nonce(run_id, stage,
                                     expected_artifact_sha256=target["artifact_sha256"],
                                     expected_version=int(target["version"]))

    approval = record(
        store, run_id, pipeline,
        stage=stage, artifact_sha256=target["artifact_sha256"], version=int(target["version"]),
        decision=decision, source=source, nonce=nonce,
        note=str(action.get("note") or ""),
        selected_option_id=str(option).upper() if option else None,
        continue_after=True, continuation_note=continuation,
        action_id=match["action"], action_class=match["class"], request_id=request_id,
    )
    result = {"status": "recorded", "action": match["action"], "stage": stage,
              "version": approval.artifact_version, "decision": approval.decision,
              "selected_option_id": approval.selected_option_id,
              "continuation_note": approval.continuation_note}
    if match["action"] == "amend_brief":
        from .active_brief import apply_brief_amendment

        payload = action.get("payload") or match.get("payload") or {}
        amendment = apply_brief_amendment(
            store, run_id,
            changes=[{"op": payload.get("op") or "replace",
                      "clause_id": payload.get("clause_id"),
                      "text": payload.get("text")}],
            note=user_message[:2000], source="conversation", approve=True,
            expected_artifact_sha256=target["artifact_sha256"],
            expected_version=int(target["version"]),
        )
        result["amendment"] = {"id": amendment.get("id"), "status": amendment.get("status")}
    store.append_event(run_id, "user_action_committed", action_id=match["action"],
                       stage=stage, source=source, request_id=request_id or "")
    return result


def reject_event(store, run_id, proposed: dict | None, resolution: dict, *, source: str) -> None:
    """Record a proposal that was not acted on. Trace only; changes no state."""
    store.append_event(run_id, "user_action_rejected",
                       action_id=str((proposed or {}).get("action") or ""),
                       target=(proposed or {}).get("target") or {},
                       source=source, reason=str(resolution.get("reason") or ""))


def continuation_intent(store, pipeline, run_id, request_id: str = "") -> dict[str, Any] | None:
    """The decision whose follow-up work has not started yet, if any.

    Shared by the workbench button and the conversation path so both derive the
    same request identity from the same approval record.
    """
    rows = store.read_events(run_id)
    created = {row.get("request_id") for row in rows if row.get("kind") == "job_created"}
    for approval in reversed(store.list_approvals(run_id)):
        if not approval.continue_after:
            continue
        rid = continuation_request_id(approval)
        if rid in created:
            continue
        current = pipeline.stage_state(run_id, approval.stage).latest
        if current is None:
            continue
        if (current.version != approval.artifact_version
                or current.sha256 != approval.artifact_sha256):
            continue
        return {"stage": approval.stage, "approval": approval.to_dict(),
                "request_id": rid, "source": approval.source}
    return None


def continuation_request_id(approval) -> str:
    """One identity for one decision's follow-up work, whatever the channel."""
    return f"decision:{approval.nonce_sha256}"


def continuation_text(approval) -> str:
    """The work the user's decision asks for next.

    Written once, here, so a decision made by clicking and the same decision made
    in conversation start the *same* follow-up. Two texts for one decision is how
    the two channels would drift into behaving differently.

    ``continuation_note`` is the user's own instruction for this next round — it
    rides along without being part of what was adopted.
    """
    stage_label = {"direction": "创意方向", "writing": "完整方案"}.get(
        approval.stage, approval.stage)
    extra = (approval.continuation_note or "").strip()
    if approval.decision == "returned":
        return (f'请按我的补充意见调整当前{stage_label}：{approval.note}。'
                '完成内部检查后让我看方案。' + (f'另外：{extra}' if extra else ''))
    if approval.stage == "direction":
        chosen = f'我选择方向 {approval.selected_option_id}。' if approval.selected_option_id else ''
        return (f'我已采用当前的{stage_label}。{chosen}{extra} '
                '请继续完成素材安排和完整方案，完成内部检查后展示给我确认。')
    return (f'我已采用当前的{stage_label}。{extra} '
            '请准备对应的分镜参考图制作方案，在工作区展示实际输入供我确认。'
            '先不提交图片或视频生成。')
