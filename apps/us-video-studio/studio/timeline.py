"""The conversation, reconstructed from disk.

A chat log and an event log are two different things, and the screen has to show
one thing. This module merges them into **turns**: one thing Jeffery said, plus
everything the system did because of it, plus the reply.

Why turns rather than a timestamp-sorted log: a bare event stream makes the
causal link invisible. He asks for a direction, four cards appear, and nothing
on screen says those four cards were the answer. Grouping by turn makes "I said
this, and here is what came back" the shape of the page.

Ordering depends on the user's message being timestamped *before* the work it
triggers. That was not true until ``Orchestrator.send`` was fixed to persist the
message before running the loop — before that, a question carried the timestamp
of the moment its answer finished. ``build_timeline`` therefore reads correctly
for every run created after that fix, and reads jumbled for runs created before
it. That is a deliberate trade: the alternative is guessing order from content.
"""
from __future__ import annotations

from bisect import bisect_left
from typing import Any

from . import vocabulary as v
from .pipeline import Pipeline
from .materials import list_materials
from .store import Store
from .structured import direction_options, open_questions, view
from .progress import project_progress

#: Sort tiebreak when a note and a card share a timestamp: the note announced the
#: work, so it goes above the work.
_PRIORITY = {"note": 0, "artifact": 1, "approval": 2, "decision_request": 3}


def build_timeline(
    store: Store, pipeline: Pipeline, ledger: Any, run_id: str
) -> dict[str, Any]:
    """``ledger`` is passed in rather than reached through ``pipeline.state``.

    ``state()`` does not carry spend — the state *route* bolts it on afterwards.
    Building the sentence from ``state`` therefore read zero on every run, which
    is the one number in the header that must not be wrong.
    """
    state = pipeline.state(run_id).to_dict()
    chat = store.read_chat(run_id)
    events = store.read_events(run_id)
    artifacts = store.list_artifacts(run_id)
    reviews = store.list_reviews(run_id)
    approvals = store.list_approvals(run_id)
    rows = ledger.entries(run_id)

    items = _collect_items(events, artifacts, reviews, approvals, chat)
    turns = _group_into_turns(chat, items, events, rows)

    pending = _pending_action(state, ledger)
    if pending is not None:
        if turns:
            turns[-1]["items"].append(pending)
        else:
            turns.append({"at": pending["at"], "user": None, "items": [pending],
                          "reply": None, "detail": _empty_detail()})

    mine = ledger.totals(run_id)
    everything = ledger.totals()
    pricing = ledger.price_note()

    return {
        "run": state.get("run"),
        "progress": project_progress(state),
        "turns": turns,
        "rail": v.build_rail(state["stages"], state.get("next_stage")),
        # The standing list of what the run is waiting on him for. Separate from
        # the cards, which are a record of what happened; this is the only part
        # of the page that is a request.
        "questions": v.build_questions(state["stages"]),
        # How drafted answers are joined into the one message they are sent as.
        # Carried, not hard-coded in the screen, for the same reason every other
        # sentence is: see `vocabulary.ANSWERS_HEADER`.
        "answers_header": v.ANSWERS_HEADER,
        "pending": pending,
        "spend": mine.to_dict(),
        "budget": {
            "limit_usd": ledger.settings.budget_limit_usd,
            "remaining_usd": ledger.remaining_usd(),
            "limit_mode": "soft_threshold",
            "pricing": pricing,
        },
        "spend_sentence": v.spend_sentence(mine, pricing["usd_cap_enforceable"]),
        "all_runs_sentence": (
            f"开发期已知用量：{v.human_tokens(everything.input_tokens + everything.output_tokens)}"
            f" token · {everything.calls} 次调用 · {everything.unknown_calls} 次用量待核实（跨所有项目）"
        ),
        # The shelf rides along with the timeline rather than being fetched
        # separately, because the screen needs it to answer a question the
        # artifacts cannot: an artifact names a file, and only this list says
        # whether that file is really there. Two requests that could disagree
        # is one request too many.
        "materials": list_materials(store, run_id),
    }


# --------------------------------------------------------------------------- #
# Items: the things that appear in the stream
# --------------------------------------------------------------------------- #


def _collect_items(
    events: list[dict[str, Any]],
    artifacts: list[Any],
    reviews: list[Any],
    approvals: list[Any],
    chat: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cards and notes, minus the notes the cards already say.

    An ``attempt_submitted`` event and the artifact it produced are one piece of
    work, not two. Rendering both gives "导演写完了「创意方向」" immediately
    followed by a card saying the same thing in more detail.
    """
    #: sha256 -> review, so each artifact card can carry its own verdict.
    review_by_sha = {r.artifact_sha256: r for r in reviews}
    card_shas = {a.sha256 for a in artifacts}

    items: list[dict[str, Any]] = []

    for ev in events:
        # These two are the card's own story; skip them when the card is present.
        if ev.get("kind") in ("attempt_submitted", "review_recorded"):
            if ev.get("sha256") in card_shas or _sha_of(ev, events) in card_shas:
                continue
        note = v.describe_history_event(ev)
        if note is not None:
            items.append(note)

    # Failures. The orchestrator writes these so a dead turn is not silent on
    # reload; without a card here they were silent anyway, because the timeline
    # only rendered user, assistant and event-log lines.
    for record in chat:
        if record.get("role") == "system" and record.get("text"):
            items.append(
                {
                    "kind": "note",
                    "at": record.get("at", ""),
                    "text": v.failure_note(record["text"]),
                }
            )

    for a in artifacts:
        review = review_by_sha.get(a.sha256)
        items.append(
            {
                "kind": "artifact",
                "at": a.created_at,
                "stage": a.stage,
                "stage_label": v.stage_label(a.stage),
                "numbered": v.numbered_stage(a.stage),
                "version": a.version,
                "version_label": v.version_label(a.version),
                "summary": a.summary,
                "author": v.role_label(a.author_role),
                # The document, laid out. Empty means the model did not produce
                # the stage's shape — the card then shows the summary and the
                # link to the text, which is what it always did.
                "blocks": view(a.stage, a.structured),
                # The consequence, said on the card rather than left in the
                # document: this stage asked something it could not answer.
                "questions": open_questions(a.structured, a.stage),
                # One shape for a review, shared with the rail's step: this card
                # and the stage panel are looking at the same review, and two
                # assemblies of it is how one of them loses the sentence.
                "review": None
                if review is None
                else v.review_view(review.verdict, review.findings),
            }
        )

    for ap in approvals:
        items.append(
            {
                "kind": "approval",
                "at": ap.decided_at,
                "stage": ap.stage,
                "stage_label": v.stage_label(ap.stage),
                "decision": ap.decision,
                "sentence": v.approval_sentence(ap.decision, ap.note) + (f" 已选方向 {ap.selected_option_id}。" if ap.selected_option_id else ""),
            }
        )

    items.sort(key=lambda i: (i["at"], _PRIORITY.get(i["kind"], 9)))
    return items


def _sha_of(ev: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """``review_recorded`` carries the artifact *key*, not the sha.

    Resolve it against the submission that produced that key so a reviewed
    artifact is still recognised as having a card.
    """
    key = ev.get("artifact")
    if not key:
        return ""
    for other in events:
        if other.get("kind") == "attempt_submitted" and other.get("artifact") == key:
            return other.get("sha256", "")
    return ""


# --------------------------------------------------------------------------- #
# Turns
# --------------------------------------------------------------------------- #


def _ranges(times: list[str], bounds: list[str]) -> list[tuple[int, int]]:
    """Index ranges, one per user message, over an ``at``-sorted list.

    Bisect, rather than a ``start <= at < end`` predicate. ``utcnow()`` has
    one-second resolution, so an item and the following message routinely carry
    the *same* timestamp — in a fast run, all of them do. A half-open window over
    two equal bounds is empty, which produced turns with no items and crashed the
    renderer on the missing card. Bisecting can only ever move an item into the
    neighbouring turn; it cannot lose one, and no item lands in two turns.
    """
    starts = [bisect_left(times, b) for b in bounds]
    return [
        (start, starts[i + 1] if i + 1 < len(starts) else len(times))
        for i, start in enumerate(starts)
    ]


def _group_into_turns(
    chat: list[dict[str, Any]],
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    ledger: list[Any],
) -> list[dict[str, Any]]:
    users = [c for c in chat if c.get("role") == "user"]
    replies = [c for c in chat if c.get("role") == "assistant"]

    # Boundaries come from the user's own messages, in the order he wrote them.
    # File order, not timestamp order: the log is append-only, so its order is
    # the truth about what he said even when a timestamp is wrong.
    bounds = [c["at"] for c in users]

    item_ranges = _ranges([i["at"] for i in items], bounds)
    rows = sorted(ledger, key=lambda r: r.at)
    row_ranges = _ranges([r.at for r in rows], bounds)
    ev_ranges = _ranges([e.get("at", "") for e in events], bounds)
    reply_ranges = _ranges([r["at"] for r in replies], bounds)

    turns: list[dict[str, Any]] = []

    # Anything the system did before he said anything. Real on legacy runs where
    # the first message carries a late timestamp; harmless when empty.
    head_end = item_ranges[0][0] if item_ranges else len(items)
    if head_end:
        head = items[:head_end]
        turns.append(
            {
                "at": head[0]["at"],
                "user": None,
                "items": head,
                "reply": None,
                "detail": _detail(
                    rows[: row_ranges[0][0]] if row_ranges else rows,
                    events[: ev_ranges[0][0]] if ev_ranges else events,
                ),
            }
        )

    for idx, msg in enumerate(users):
        start = bounds[idx]
        i0, i1 = item_ranges[idx]
        r0, r1 = row_ranges[idx]
        e0, e1 = ev_ranges[idx]
        # Same index ranges as the items, so a turn cannot claim the next turn's
        # reply when its own turn produced none.
        j0, j1 = reply_ranges[idx]
        answer = replies[j0] if j0 < j1 else None
        turns.append(
            {
                "at": start,
                "user": {"at": start, "text": msg.get("text", "")},
                "items": items[i0:i1],
                "reply": None if answer is None else {"at": answer["at"], "text": answer["text"]},
                "detail": _detail(rows[r0:r1], events[e0:e1]),
            }
        )
    return turns


def _empty_detail() -> dict[str, Any]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "roles": []}


def _detail(rows: list[Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The "机器细节" panel: what this turn cost, per role.

    Given this turn's slices of the ledger and the event log rather than a time
    window — the windows overlapped when timestamps tied, which double-counted
    the same call into two turns.

    Deliberately about *cost and effort*, not tool names. "导演 · 8 轮 · 5 次调用 ·
    42k token" answers the question a person actually has — did that take a lot of
    work? — where a list of tool names does not.
    """
    rounds: dict[str, int] = {}
    for ev in events:
        if ev.get("kind") == "dispatch_done":
            role = ev.get("role", "")
            rounds[role] = rounds.get(role, 0) + int(ev.get("rounds", 0) or 0)

    by_role: dict[str, dict[str, Any]] = {}
    for r in rows:
        agg = by_role.setdefault(
            r.role, {"role": r.role, "label": v.role_label(r.role), "calls": 0,
                     "input_tokens": 0, "output_tokens": 0, "rounds": 0}
        )
        agg["calls"] += 1
        agg["input_tokens"] += r.input_tokens or 0
        agg["output_tokens"] += r.output_tokens or 0
    for role, n in rounds.items():
        if role in by_role:
            by_role[role]["rounds"] = n

    return {
        "calls": len(rows),
        "input_tokens": sum(r.input_tokens or 0 for r in rows),
        "output_tokens": sum(r.output_tokens or 0 for r in rows),
        "roles": sorted(by_role.values(), key=lambda a: -a["calls"]),
    }


# --------------------------------------------------------------------------- #
# What he is being asked to do
# --------------------------------------------------------------------------- #


def _pending_action(state: dict[str, Any], ledger: Any) -> dict[str, Any] | None:
    """The one thing waiting on him, or None.

    Only approvals wait on him. Reviews are the orchestrator's to request; a
    stage being ready to start is the orchestrator's to start. The single action
    that no agent can take is this one, so it is the single thing the screen
    should ask for.

    Both review outcomes reach this card. ``Pipeline.stage_state`` sets
    ``can_approve`` for a returned stage as well as a passed one, because the
    reviewer is an agent on the producing side of the table and its ``return``
    must not be a veto he cannot lift. Approving past one goes through
    ``record_approval``, which demands an explicit reason and records
    ``approved_override`` — so the card that offers it must also say what it
    costs him, which is why the wording differs between the two branches.
    """
    from .models import STAGES, STAGE_SPEC, utcnow

    for st in state["stages"]:
        if not st.get("can_approve"):
            continue
        stage = st["stage"]
        latest = st.get("latest") or {}
        review = st.get("review") or {}
        passed = review.get("verdict") == "pass"
        findings = review.get("findings") or []
        if passed:
            head = f"「{v.stage_label(stage)}」写好了，审核也通过了。"
            if findings:
                head += f"但审核提了 {len(findings)} 点要注意。"
            body = "你看一遍，觉得可以就通过；要改就退回重做。"
        else:
            head = f"「{v.stage_label(stage)}」写好了，但审核没有通过它。"
            body = "你可以退回重做，或者看完之后明知有问题仍然通过——那样要写明理由。"

        # What approving this one opens, and what that has cost before. The
        # price sits on the button rather than in a budget page, because the
        # moment he needs it is the moment he is deciding.
        nxt = next((s for s in STAGES if stage in STAGE_SPEC[s].requires), None)
        if nxt is None:
            next_stage_label = ""
            next_owner = ""
            cost = "批准之后这条链路就走完了，没有下一步要花的钱。"
        else:
            next_stage_label = v.stage_label(nxt)
            next_owner = v.role_label(v.owner_role(nxt))
            cost = v.cost_sentence(ledger.last_stage_cost(v.owner_role(nxt), nxt))

        # Recommendation is advisory. Every actual option is selectable, while
        # an override still requires its explicit reason in the approval API.
        options = []
        if stage == "direction":
            options = [{"key": o["key"], "recommended": o["recommended"],
                        "label": v.option_button_label(o["key"], o["recommended"])}
                       for o in direction_options(latest.get("structured") or {})]

        return {
            "kind": "decision_request",
            "at": latest.get("created_at", utcnow()),
            "stage": stage,
            "stage_label": v.stage_label(stage),
            "version": latest.get("version"),
            "artifact_sha256": latest.get("sha256"),
            "passed": passed,
            "headline": head,
            "body": body,
            "findings": [
                {"kind": f.get("kind", ""), "issue": f.get("issue", "")} for f in findings
            ],
            "next_stage_label": next_stage_label,
            "next_owner": next_owner,
            "next_cost_sentence": cost,
            "options": options,
            "options_hint": v.GATE_OPTION_HINT if options else "",
        }
    return None
