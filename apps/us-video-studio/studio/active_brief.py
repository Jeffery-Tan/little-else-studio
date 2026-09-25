"""Compile the ActiveBrief from records that already exist.

P0: turn intake structure + chat answers into one machine-checkable brief.
P2: apply user-approved ``amend_brief`` changes so the product goal can only
change through an explicit decision, never by prose drift.

**Who owns the goal.** ``run.brief`` (the user's own request, written once) and
approved amendments define the effective goal. Intake — the model's reading of
that request — supplies facts, constraints and answered questions, and may be
revised freely for those, but a revision cannot move the goal. Without that
split, rewriting intake was enough to redefine the objective and the pipeline
went on self-consistently against a goal nobody agreed to.

The base compile is still pure and deterministic — no model call. Amendments
live in ``runs/<id>/briefs/amendments.json`` (atomic replace). ``version`` is
``1 + approved count``; ``fingerprint`` therefore moves when a brief is
amended, which is what invalidates downstream artifacts (pipeline stale).

Clause ids are content-stable (``must.3f2a91c0``). ``goal.primary`` keeps its
id across replacements so the gate can keep pointing at the same product goal.

``brief_gate`` is the projection that keeps "not declared" from reading as
"符合约束": for an artifact written against the current brief it requires the
options that can be adopted to declare known clause ids. It is deliberately not
semantic verification — a self-reported ``satisfies`` still needs the
independent review and the user.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

from .models import utcnow
from .store import Store, _atomic_write_text

SCHEMA_VERSION = 1

#: Constraints that forbid rather than require. Only the *first* comma clause
#: decides: 「…用美式英语，不做中译英腔」 is a positive requirement with a
#: trailing ban, not a must_not — classifying the whole string would erase
#: the thing everyone must do.
_NEGATION_HEAD = re.compile(r"^(不得|禁止|不要|不能|不允许|不应|勿|不|无|禁)")
_ANY_BAN = re.compile(r"不得|禁止|不要|不允许")
_POSITIVE = re.compile(r"用|要|需|必须|保持|采用|使用|应当")

#: Intake fact labels that name the product goal rather than a detail.
_GOAL_LABEL = re.compile(r"交付形态|作品形态|视频类型|产品目标|创作目标|项目目标")

#: Length cap for the goal taken from ``run.brief`` (the user's own words).
_GOAL_CHARS = 300

#: Gate blocker kinds. Kept together because callers switch on them:
#: ``needs_amendment`` is the only kind that may project an approvable change.
BLOCKER_VIOLATES = "violates"
BLOCKER_NEEDS_AMENDMENT = "needs_amendment"
BLOCKER_UNKNOWN_CLAUSE = "unknown_clause"
BLOCKER_UNMAPPED = "unmapped"
#: The option declared something, but not the product goal and no amendment to
#: it — so the one clause every plan has to be measured against went unchecked.
BLOCKER_GOAL_UNCOVERED = "goal_uncovered"
#: An approved amendment could not be reapplied to the current brief. This one
#: is not about an option: it blocks the whole brief.
BLOCKER_AMENDMENT_UNRESOLVED = "amendment_unresolved"

#: ask_user replies written by the conversation path: 关于「问题」：答案
_ANSWER = re.compile(r"^关于「(.+?)」[：:]\s*(.+)$", re.S)

#: Shortest question excerpt still considered a match against a chat reply.
_MATCH_CHARS = 12


def _canonical(brief: dict[str, Any]) -> str:
    """JSON with sorted keys — the only input to content hashes.

    ``open_amendments`` (pending proposals) is deliberately excluded: a
    proposal that has not been approved must not move the fingerprint or
    mass-invalidate downstream artifacts.
    """
    return json.dumps(
        {
            "goal": brief.get("goal"),
            "clauses": brief.get("clauses"),
            "pending_questions": brief.get("pending_questions"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_sha(brief: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(brief).encode("utf-8")).hexdigest()


def fingerprint(brief: dict[str, Any]) -> str:
    """Stable token for ``inputs["brief:snapshot"]`` (P1). Content only."""
    return f"brief-v{int(brief.get('version') or 1)}-{content_sha(brief)[:16]}"


def clause_index(brief: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in brief.get("clauses") or [] if c.get("id")}


def _stable_clause_id(kind: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    prefix = {"product_goal": "goal", "must": "must", "must_not": "not",
              "asset": "asset", "decision": "dec", "boundary": "bnd"}.get(kind, kind)
    return f"{prefix}.{digest}"


def _clause_kind(text: str) -> str:
    head = re.split(r"[，,。；;]", text.strip(), maxsplit=1)[0].strip()
    if _NEGATION_HEAD.match(head) or re.search(r"不进|不出现|不声称|不作为|不做", head):
        return "must_not"
    if _POSITIVE.search(head):
        return "must"
    if _ANY_BAN.search(text):
        return "must_not"
    return "must"


def _check_for(kind: str, text: str) -> str:
    if kind == "must_not":
        return "absence"
    if kind == "asset":
        return "absence" if _ANY_BAN.search(text) else "presence"
    if re.search(r"美式英语|English|en[-_]?US", text, re.I):
        return "style"
    if kind == "decision":
        return "choice"
    return "presence"


def _question_excerpt(question: str) -> str:
    """First clause of a question — enough to spot an 关于「…」 reply."""
    cut = re.split(r"[？?。；;]", question.strip(), maxsplit=1)[0]
    return cut[:_MATCH_CHARS]


def _user_replies(chat: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """``(quoted_question, answer_text, raw)`` for explicit ask_user replies."""
    out: list[tuple[str, str, str]] = []
    for row in chat or []:
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        text = (row.get("text") or "").strip()
        m = _ANSWER.match(text)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip(), text))
    return out


def _match_answer(question: str, replies: list[tuple[str, str, str]]) -> str | None:
    excerpt = _question_excerpt(question)
    if len(excerpt) < 4:
        return None
    for quoted, answer, _raw in replies:
        if excerpt and excerpt in quoted:
            return answer
        if quoted and quoted[:_MATCH_CHARS] in question:
            return answer
    return None


def _goal_from_intake(structured: dict[str, Any], source: str) -> dict[str, Any] | None:
    for row in structured.get("facts") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        value = str(row.get("value") or "").strip()
        if value and _GOAL_LABEL.search(label):
            return {
                "id": "goal.primary",
                "text": value,
                "kind": "product_goal",
                "source": f"{source} facts[{label}]",
            }
    return None


def _goal_from_run(run_brief: str) -> dict[str, Any] | None:
    text = (run_brief or "").strip()
    if not text:
        return None
    # First sentence / first line — the ask, not the whole attachment dump.
    head = re.split(r"[。.\n]", text, maxsplit=1)[0].strip()
    if len(head) > _GOAL_CHARS:
        head = head[:_GOAL_CHARS].rstrip() + "…"
    if not head:
        return None
    return {
        "id": "goal.primary",
        "text": head,
        "kind": "product_goal",
        "source": "run.brief",
        "authority": "user",
    }


def _reading_clause(goal: dict[str, Any] | None,
                    structured: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """The intake's own reading of the delivery form, kept as *evidence*.

    The goal itself comes from ``run.brief`` (see ``compile_active_brief``), but
    the model's structured reading is real work and the roles need to see it —
    a direction that quietly narrows the delivery form should be visible as a
    proposal that disagrees with the user's goal, not silently discarded. It is
    a clause with its own id, explicitly not ``goal.primary``, so nothing can
    mistake it for the authorized objective.
    """
    reading = _goal_from_intake(structured, source)
    if reading is None or (goal is not None and reading["text"] == goal.get("text")):
        return []
    return [{
        "id": _stable_clause_id("decision", reading["text"]),
        "kind": "decision",
        "text": reading["text"],
        "check": "choice",
        "source": reading["source"],
        "scope": "global",
        "status": "proposed",
        "authority": "intake_proposal",
    }]


def _constraints(structured: dict[str, Any], source: str) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for raw in structured.get("constraints") or []:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        kind = _clause_kind(text)
        clauses.append({
            "id": _stable_clause_id(kind, text),
            "kind": kind,
            "text": text,
            "check": _check_for(kind, text),
            "source": f"{source} constraints",
            "scope": "global",
            "status": "confirmed",
        })
    return clauses


def _asset_facts(structured: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Facts that bound what footage/materials exist — asset clauses, not goals."""
    clauses: list[dict[str, Any]] = []
    pattern = re.compile(r"补不到|无法提供|没有.{0,6}素材|无视频|静图|不进画面")
    for row in structured.get("facts") or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("value") or "").strip()
        if not text or not pattern.search(text):
            continue
        kind = "asset"
        clauses.append({
            "id": _stable_clause_id(kind, text),
            "kind": kind,
            "text": text,
            "check": _check_for(kind, text),
            "source": f"{source} facts[{row.get('label') or ''}]",
            "scope": "global",
            "status": "confirmed",
        })
    return clauses


def _split_questions(structured: dict[str, Any], replies: list[tuple[str, str, str]],
                     source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """User-facing intake questions → (decision clauses, still-pending rows)."""
    decisions: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for row in structured.get("open_questions") or []:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        if not question:
            continue
        if row.get("owner") not in (None, "user"):
            continue
        if row.get("timing") not in (None, "now"):
            continue
        answer = _match_answer(question, replies)
        if answer is None:
            pending.append({
                "question": question,
                "blocks": str(row.get("blocks") or ""),
                "owner": "user",
                "timing": "now",
                "reason": str(row.get("reason") or ""),
                "source": f"{source} open_questions",
            })
            continue
        text = f"{_question_excerpt(question)} → {answer}"
        kind = "decision"
        decisions.append({
            "id": _stable_clause_id(kind, text),
            "kind": kind,
            "text": text,
            "check": "choice",
            "source": f"chat user_answer: {answer}",
            "scope": "global",
            "status": "confirmed",
            "question": question,
            "answer": answer,
        })
    return decisions, pending


def amendments_path(store: Store, run_id: str) -> Path:
    return store.run_dir(run_id) / "briefs" / "amendments.json"


def load_amendments(store: Store, run_id: str) -> list[dict[str, Any]]:
    path = amendments_path(store, run_id)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _save_amendments(store: Store, run_id: str, rows: list[dict[str, Any]]) -> None:
    path = amendments_path(store, run_id)
    _atomic_write_text(path, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def _amended_goal(text: str) -> dict[str, Any]:
    """The one shape an amendment-approved product goal takes."""
    return {"id": "goal.primary", "text": text, "kind": "product_goal",
            "source": "brief_amendment", "authority": "amendment"}


def validate_changes_against(brief: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    """Refuse an amendment that cannot mean anything (F4).

    Two ways a "change" is not a change, and both used to reach the write path:

    * a ``replace``/``retire`` whose clause id names nothing in the current
      brief, so applying it is a silent no-op that still bumps the version; and
    * the replacement text is the clause's existing text — the frontend fell
      back to the clause id when no proposal existed, and a block of diagnostic
      prose used to be offered as the *new goal*.

    ``add`` is validated the other way round (A2): its whole point is a clause
    the brief does not have yet, so requiring the id to exist rejected every
    legitimate addition. It needs a non-empty body and an id or a kind to
    derive one from — and an ``add`` that would duplicate an existing clause
    body is refused rather than silently dropped.

    Raising here keeps the ambiguity out of the audit trail: an amendment row
    exists only when someone decided something that differs from what is there.
    """
    index = clause_index(brief)
    goal_text = str((brief.get("goal") or {}).get("text") or "")
    known = set(index)
    if brief.get("goal"):
        known.add("goal.primary")
    bodies = {(str(c.get("text") or "")).strip() for c in index.values()}
    bodies.add(goal_text.strip())
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        clause_id = str(ch.get("clause_id") or "").strip()
        op = str(ch.get("op") or "replace").strip()
        text = str(ch.get("text") or "").strip()
        if op == "add":
            if not clause_id and not text:
                raise ValueError("新增条款需要 clause_id 或至少一段正文。")
            if text and text in bodies:
                raise ValueError("这条正文已在当前有效简报里，不需要新增。")
            continue
        if clause_id not in known:
            raise ValueError(
                f"修订条款 {clause_id or '(空)'} 不在当前有效简报中，不能作为修订目标。"
            )
        if op == "replace":
            current = goal_text if clause_id == "goal.primary" else str(
                index.get(clause_id, {}).get("text") or ""
            )
            if text and text == current.strip():
                raise ValueError("修订正文与原条款内容相同，没有实际变更；请写明要改成什么。")


def _text_key(value: Any) -> str:
    """Whitespace-insensitive identity for a clause body.

    Used only to recognise the *same* requirement after a model reworded it.
    It is deliberately not a fuzzy match: without an exact body there is nothing
    to claim the user's decision still applies to, and guessing would be worse
    than reporting the conflict.
    """
    return re.sub(r"\s+", "", str(value or ""))


def _apply_changes(brief: dict[str, Any], changes: list[dict[str, Any]],
                   collector: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply one approved change set in order, realigning it onto the live clauses.

    An amendment is a decision about a *requirement*, and clause ids here are
    derived from clause text. So a later intake revision that rewords the clause
    changes its id, and the approved change used to be dropped without a word —
    the record still said "approved" while the brief no longer carried it (R2).

    Each change therefore carries the body it was decided against (``base_text``,
    recorded at proposal time). Resolution order:

    1. the id, if the brief still has it;
    2. otherwise a live clause with the body the user approved — the model kept
       the decision but recompiled it under another id;
    3. otherwise a live clause with the body the user decided against — the model
       reworded the original and dropped the decision;
    4. otherwise a conflict: the approved body is kept under the id it was
       approved as, and the divergence is reported rather than silently skipped.

    Retiring a clause the brief no longer has is *not* a conflict: the
    requirement is already gone, which is what the user asked for.

    ``collector`` carries ``conflicts`` and the cross-change bookkeeping across
    the *whole* replay, because one amendment's doubt can be settled by the next
    amendment in a separate call — ``{"conflicts": [], "doubts": [], "voided":
    set(), "voided_bodies": set()}``. Tracking it per change set was the bug: the
    tables reset between records, so the second record could not see what the
    first had left behind (T1).
    """
    goal = dict(brief["goal"]) if brief.get("goal") else None
    clauses = [dict(c) for c in brief.get("clauses") or []]
    by_id = {c.get("id"): c for c in clauses}
    collector = collector if collector is not None else {}
    conflicts: list[dict[str, Any]] = collector.setdefault("conflicts", [])
    #: One entry per unresolved doubt: the clauses and bodies it is about. Later
    #: changes settle an entry by touching any of those bodies, because a body is
    #: what identifies a requirement — the clause id is derived from the text, so
    #: an amendment moves it and an id-based link would never match (T1).
    doubts: list[dict[str, Any]] = collector.setdefault("doubts", [])
    #: (clause id, body) pairs a later decision has already superseded. Clause
    #: objects are rebuilt on every change set, so this signature — not object
    #: identity — is what stays comparable across the replay.
    voided: set[tuple[str, str]] = collector.setdefault("voided", set())
    #: The bodies of those pairs, for the case where two records name the same
    #: requirement by different ids: the id differs, the requirement does not.
    voided_bodies: set[str] = collector.setdefault("voided_bodies", set())

    def _sign(clause_id: str, text: str) -> tuple[str, str]:
        return (str(clause_id or ""), _text_key(text))

    def _settle(*sources: str) -> None:
        """Retire the doubts this change speaks for.

        Called once per change, where the change *resolved* its target. A change
        that could not be resolved keeps its doubt, and a later change that also
        fails adds its own rather than replacing a real divergence.
        """
        keys = {_text_key(source) for source in sources if _text_key(source)}
        ids = {str(source or "") for source in sources}
        for doubt in list(doubts):
            if not (doubt["keys"] & keys or any(i in doubt["ids"] for i in ids)):
                continue
            doubts.remove(doubt)
            if doubt["row"] in conflicts:
                conflicts.remove(doubt["row"])
            for signature in doubt["signs"]:
                voided.add(signature)
                voided_bodies.add(signature[1])
                # A body this replay recovered for that requirement is history
                # now: the newer decision is what the requirement says.
                clauses[:] = [c for c in clauses
                              if _sign(c.get("id"), c.get("text")) != signature]
                by_id.pop(signature[0], None)

    def _doubt(clause_id: str, row: dict[str, Any], *sources: str) -> None:
        """Record one unresolved doubt about a requirement."""
        doubts.append({
            "ids": {str(clause_id)},
            "keys": {_text_key(source) for source in sources if _text_key(source)},
            "signs": {_sign(clause_id, sources[-1] if sources else "")},
            "row": row,
        })

    def align(clause_id: str, base_text: str, decided_text: str) -> str:
        """Which live clause this decision is about, or "" if none is.

        Three ways to recognise it, in order of confidence:

        1. the id — nothing has moved;
        2. a clause carrying the body the user *approved* — the model kept the
           decision but recompiled it under a different id, which is the normal
           case once a reworded intake carries the user's own wording forward;
        3. a clause carrying the body the user decided *against* — the model
           reworded the original and dropped the decision, so the change has to
           move onto the clause that still means the same thing.
        """
        if clause_id == "goal.primary" or clause_id in by_id:
            return clause_id
        for wanted in (decided_text, base_text):
            key = _text_key(wanted)
            if not key:
                continue
            for row in clauses:
                if _text_key(row.get("text")) == key:
                    return str(row.get("id") or "")
        return ""

    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        clause_id = str(ch.get("clause_id") or "").strip()
        op = str(ch.get("op") or "replace").strip()
        text = str(ch.get("text") or "").strip()
        base_text = str(ch.get("base_text") or "").strip()
        if not clause_id:
            continue
        if op == "add":
            kind = str(ch.get("kind") or "must")
            if not text:
                continue
            new_id = clause_id if clause_id.startswith(("goal.", "must.", "not.", "asset.", "dec.", "bnd.")) \
                else _stable_clause_id(kind, text)
            row = {
                "id": new_id,
                "kind": kind,
                "text": text,
                "check": _check_for(kind, text),
                "source": "brief_amendment",
                "authority": "amendment",
                "scope": "global",
                "status": "confirmed",
            }
            if new_id == "goal.primary" and text:
                goal = _amended_goal(text)
            existing = by_id.get(new_id)
            if existing is None:
                clauses.append(row)
                by_id[new_id] = row
            elif str(existing.get("text") or "").strip() != text:
                # Same identity, different body: an edit, not a duplicate. The
                # old code dropped this and reported nothing (A2).
                clauses = [c for c in clauses if c.get("id") != new_id]
                clauses.append(row)
                by_id[new_id] = row
            # An explicit add settles whatever this id used to be in doubt about.
            _settle(new_id)
            continue
        resolved = align(clause_id, base_text, text)
        # One change settles the whole requirement: the ids it names and the
        # bodies it is about. Done before acting, so a doubt cannot re-add a body
        # a later decision has already replaced (T1). The body this change
        # *moves away from* is voided here too, so a still-earlier record that
        # cannot be realigned does not resurrect it.
        voided.add(_sign(clause_id, base_text))
        voided_bodies.add(_text_key(base_text))
        _settle(clause_id, base_text, text)
        if op == "retire":
            if resolved and resolved != "goal.primary":
                clauses = [c for c in clauses if c.get("id") != resolved]
                by_id.pop(resolved, None)
            continue
        # replace
        if not text:
            continue
        if not resolved and clause_id != "goal.primary":
            # The clause the user decided about is no longer in the brief under
            # either its id or its body: the model replaced or dropped it
            # outright. Dropping the decision here is the defect this fixes, and
            # so is resurrecting the model's text as if the user had approved it.
            # What the user approved becomes the clause, under the id they
            # approved it as, and the divergence is reported — unless the same
            # requirement has already been settled in this replay: either this
            # exact (id, body), or that body under whatever id an earlier record
            # named it by. Two records about one requirement must not leave two
            # rows and two blocks behind (T1).
            if _sign(clause_id, text) in voided or (text and _text_key(text) in voided_bodies):
                continue
            kind = str(next((c.get("kind") for c in clauses if c.get("id") == clause_id),
                            "must") or "must")
            clauses = [c for c in clauses if c.get("id") != clause_id]
            kept = {
                "id": clause_id,
                "kind": kind,
                "text": text,
                "check": _check_for(kind, text),
                "source": "brief_amendment",
                "authority": "amendment",
                "scope": "global",
                "status": "confirmed",
            }
            clauses.append(kept)
            by_id[clause_id] = kept
            # Recorded as a doubt about *this requirement*, not appended blindly:
            # a later amendment about the same body supersedes both the doubt and
            # the body this path recovered, so one decision cannot leave a
            # permanent block — or a stale copy of the requirement — behind.
            conflicts.append({
                "amendment_clause_id": clause_id,
                "kind": BLOCKER_AMENDMENT_UNRESOLVED,
                "base_text": base_text,
                "detail": (
                    f"已批准的修订（针对条款 {clause_id}）把这条要求改成了「{text}」，"
                    "但模型重写 intake 时"
                    + (f"已经没有这一条（原文「{base_text}」）" if base_text else "已经把这一条换掉了")
                    + "。这份简报保留了你的版本，但两者不一致，采用前需要你确认："
                      "可以重新提出这条修订，或退回让团队按当前简报重做。"
                ),
            })
            _doubt(clause_id, conflicts[-1], base_text, text)
            continue
        target = by_id.get(resolved)
        if target is not None:
            new_text_id = resolved if resolved == "goal.primary" else _stable_clause_id(
                str(target.get("kind") or "must"), text)
            new_row = {**target, "text": text, "source": "brief_amendment",
                       "authority": "amendment"}
            if new_text_id != target["id"]:
                new_row["id"] = new_text_id
                by_id.pop(target["id"], None)
            # keep id stable for goal.primary
            if resolved == "goal.primary":
                new_row["id"] = "goal.primary"
            clauses = [c for c in clauses if c.get("id") != target["id"]]
            clauses.append(new_row)
            by_id[new_row["id"]] = new_row
            if resolved == "goal.primary" or new_row["id"] == "goal.primary":
                goal = _amended_goal(text)
        elif resolved and text:
            # The decision's own body is in the current brief — found by text
            # rather than by the id the record names. It is *already* the compiled
            # clause, so nothing is added; and because the id differs from the one
            # the record was decided against, the divergence is reported rather
            # than presented as an aligned replay.
            kind = str(next((c.get("kind") for c in clauses
                             if c.get("id") == resolved), "must") or "must")
            if resolved not in by_id:
                clauses.append({
                    "id": resolved, "kind": kind, "text": text,
                    "check": _check_for(kind, text), "source": "brief_amendment",
                    "authority": "amendment", "scope": "global",
                    "status": "confirmed",
                })
                by_id[resolved] = clauses[-1]
            if clause_id != resolved:
                conflicts.append({
                    "amendment_clause_id": clause_id,
                    "kind": BLOCKER_AMENDMENT_UNRESOLVED,
                    "base_text": base_text,
                    "detail": (
                        f"已批准的修订（针对条款 {clause_id}）把这条要求改成了「{text}」，"
                        f"当前简报里能找到这条正文但条款 id 已变为 {resolved}；"
                        "两者不一致，采用前需要你确认：可以重新提出这条修订，"
                        "或退回让团队按当前简报重做。"
                    ),
                })
            _settle(clause_id, base_text, text)
        elif resolved == "goal.primary" and text:
            goal = _amended_goal(text)
            clauses = [c for c in clauses if c.get("id") != "goal.primary"]
            row = {**goal, "check": "presence", "scope": "global", "status": "confirmed"}
            clauses.append(row)
            by_id["goal.primary"] = row

    clauses.sort(key=lambda c: (c["kind"], c["text"], c.get("id") or ""))
    return {**brief, "goal": goal, "clauses": clauses}


def compile_active_brief(store: Store, run_id: str, *, version: int | None = None) -> dict[str, Any]:
    """Build the ActiveBrief for ``run_id`` (base compile + approved amendments).

    **Who may define the product goal (see F3).** ``run.brief`` is written once,
    at project creation, from the user's own words; intake is the model's
    *reading* of that request, and every intake revision can rewrite its facts
    and constraints. So the goal comes from ``run.brief`` and from approved
    amendments — never from the latest intake. Otherwise a model rewrite of
    intake silently redefined the effective goal and the system went on
    self-consistently against a goal the user never agreed to, with only the
    old draft marked stale.

    The model's intake work is not discarded: its constraints, asset facts,
    answered questions and pending questions all still compile into the brief.
    A stage that needs to *change* the goal proposes an amendment, and
    ``brief_gate`` will not let any option adopt past one.
    """
    run = store.read_run(run_id)
    intake = store.latest_artifact(run_id, "intake")
    structured: dict[str, Any] = dict(intake.structured or {}) if intake else {}
    source = intake.key if intake else "intake:missing"
    chat = store.read_chat(run_id)
    replies = _user_replies(chat)

    # Authority first, interpretation second. ``_goal_from_intake`` stays as the
    # fallback for a run whose brief is empty (legacy/imported records) so those
    # still compile rather than losing their goal entirely.
    goal = _goal_from_run(run.brief) or _goal_from_intake(structured, source)
    # ``authority`` is stamped here, once, so every path into the goal says who
    # put it there. It sits outside ``_canonical``, so recording it does not
    # move the fingerprint and cannot mass-invalidate existing drafts.
    if goal is not None:
        goal["authority"] = "user" if goal.get("source") == "run.brief" else "intake_fallback"
    clauses: list[dict[str, Any]] = []
    if goal is not None:
        clauses.append({**goal, "check": "presence", "scope": "global",
                        "status": "confirmed"})
    clauses.extend(_reading_clause(goal, structured, source))
    clauses.extend(_constraints(structured, source))
    clauses.extend(_asset_facts(structured, source))
    decisions, pending = _split_questions(structured, replies, source)
    clauses.extend(decisions)

    clauses.sort(key=lambda c: (c["kind"], c["text"], c["id"]))
    pending.sort(key=lambda q: q["question"])

    amendments = load_amendments(store, run_id)
    approved = [a for a in amendments if a.get("status") == "approved"]
    pending_amendments = [a for a in amendments if a.get("status") == "pending"]

    brief: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "brief_id": f"brf-{content_sha({'goal': goal, 'clauses': clauses})[:12]}",
        "version": int(version) if version is not None else 1 + len(approved),
        "status": "active",
        "supersedes": None,
        "goal": goal,
        "clauses": clauses,
        "pending_questions": pending,
        "open_amendments": pending_amendments,
        "source": {
            "run_id": run_id,
            "intake": source,
            "chat_user_lines": sum(1 for r in chat if isinstance(r, dict) and r.get("role") == "user"),
            "approved_amendments": len(approved),
            "compiled_at": utcnow(),
        },
    }
    # Approved amendments are replayed in order. Anything that cannot be
    # realigned onto the current clauses becomes a reported conflict instead of
    # a silent skip (R2) — a brief that quietly dropped a user decision must not
    # look like a brief that carries it. The bookkeeping is shared across the
    # whole replay so a later amendment can settle an earlier one's doubt (T1).
    collector: dict[str, Any] = {"conflicts": [], "doubts": [], "voided": set(),
                                 "voided_bodies": set()}
    for row in approved:
        brief = _apply_changes(brief, row.get("changes") or [], collector)
    brief["version"] = int(version) if version is not None else 1 + len(approved)
    brief["conflicts"] = collector["conflicts"]
    brief["brief_id"] = f"brf-{content_sha({'goal': brief.get('goal'), 'clauses': brief.get('clauses')})[:12]}"
    # ``compiled_at`` must not enter content hashes / fingerprints.
    return brief


def _clean_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The normalised shape of an amendment's changes — write path and
    fingerprint path share it, so "the same payload" means the same thing
    in both (U3)."""
    cleaned: list[dict[str, Any]] = []
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        clause_id = str(ch.get("clause_id") or "").strip()
        op = str(ch.get("op") or "replace").strip()
        if op not in ("replace", "retire", "add") or not clause_id:
            raise ValueError(f"invalid amendment change: {ch!r}")
        text = str(ch.get("text") or "").strip()
        if op in ("replace", "add") and not text:
            raise ValueError("replace/add amendment requires text")
        if op == "retire" and clause_id == "goal.primary":
            raise ValueError("cannot retire goal.primary")
        row = {"clause_id": clause_id, "op": op}
        if text:
            row["text"] = text
        if ch.get("kind"):
            row["kind"] = str(ch.get("kind"))
        if ch.get("base_text") is not None:
            row["base_text"] = str(ch.get("base_text") or "").strip()
        cleaned.append(row)
    if not cleaned:
        raise ValueError("amendment needs at least one change")
    return cleaned


def amendment_request_fingerprint(*, changes: list[dict[str, Any]], note: str = "",
                                  approve: bool = False,
                                  expected_brief_fingerprint: str | None = None,
                                  expected_artifact_sha256: str | None = None,
                                  expected_version: int | None = None) -> str:
    """Canonical hash of the payload a submission identity is minted for (U3).

    Covers every field that can change what this decision means — the cleaned
    changes, the note, approve, and the declared brief / direction baselines —
    and nothing the server generates (timestamps, amendment ids, compile
    times), so a verbatim retry hashes the same. It is frozen onto the row at
    write time; a later request under the same id must hash to it, or it is a
    different request wearing a stolen identity.
    """
    payload: dict[str, Any] = {
        "approve": bool(approve),
        "changes": _clean_changes(changes),
        "note": (note or "").strip(),
    }
    if expected_brief_fingerprint is not None:
        payload["expected_brief_fingerprint"] = str(expected_brief_fingerprint)
    if expected_artifact_sha256 is not None:
        payload["expected_artifact_sha256"] = str(expected_artifact_sha256)
    if expected_version is not None:
        payload["expected_version"] = int(expected_version)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _legacy_changes_match(stored: Any, incoming: list[dict[str, Any]]) -> bool:
    """Compare against a row written before payloads were frozen.

    Only what the record actually kept can be checked — the changes (with
    ``base_text`` required only when this request declared one; the write path
    stamps it from the brief, and that stamp is not part of *this* request).
    Fields the row never stored are not claimed as verified.
    """
    old_rows = [r for r in (stored or []) if isinstance(r, dict)]
    try:
        new_rows = _clean_changes(incoming)
    except ValueError:
        return False
    if len(old_rows) != len(new_rows):
        return False
    for old, new in zip(old_rows, new_rows):
        for field in ("clause_id", "op", "text", "kind"):
            if str(old.get(field) or "") != str(new.get(field) or ""):
                return False
        if new.get("base_text") is not None and \
                str(old.get("base_text") or "") != str(new.get("base_text") or ""):
            return False
    return True


def resolve_amendment_by_request(
    store: Store, run_id: str, *, request_id: str,
    changes: list[dict[str, Any]], note: str = "", approve: bool = False,
    expected_brief_fingerprint: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any] | None:
    """The amendment this request_id already recorded — or None.

    One identity, one frozen payload: when the id is found but the incoming
    request carries a *different* payload, raises ``BriefRequestConflict``
    instead of resolving to the old decision's success — a changed request
    must never be answered "yes" by a record it does not describe (U3).
    """
    key = str(request_id or "").strip()
    if not key:
        return None
    row = next((r for r in load_amendments(store, run_id)
                if str(r.get("request_id") or "") == key), None)
    if row is None:
        return None
    frozen = row.get("request_fingerprint")
    if frozen:
        incoming = amendment_request_fingerprint(
            changes=changes, note=note, approve=approve,
            expected_brief_fingerprint=expected_brief_fingerprint,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_version=expected_version)
        if not secrets.compare_digest(str(frozen), incoming):
            raise BriefRequestConflict(key)
        return row
    # Legacy row (written before payloads were frozen): compare the fields it
    # does carry — changes and note — and nothing else. Absent history is not
    # invented into a claim that the baselines matched.
    if not _legacy_changes_match(row.get("changes"), changes) or \
            str(row.get("note") or "").strip() != (note or "").strip()[:2000]:
        raise BriefRequestConflict(key)
    return row


def propose_amendment(
    store: Store,
    run_id: str,
    *,
    changes: list[dict[str, Any]],
    note: str = "",
    source: str = "user",
    approve: bool = False,
    request_id: str = "",
    request_fingerprint: str = "",
) -> dict[str, Any]:
    """Record a brief amendment. ``approve=True`` applies it immediately
    (one explicit user click from the blocked gate); otherwise status=pending
    until ``decide_amendment``.

    Each change is stamped with ``base_text`` — the body of the clause as the
    *user saw it* when deciding — plus the brief fingerprint that body came
    from. That is what lets a later intake revision reword the clause without
    silently discarding the decision (R2); ``apply_brief_amendment`` does the
    validating, because only a new proposal has to still line up with the
    current brief.

    ``request_id`` is the caller's identity for the submission, stored on the row
    so a retry resolves to this amendment instead of recording a second one.
    ``request_fingerprint`` freezes the payload that identity was minted for,
    so the same id carrying a different decision later is a conflict, not a
    retry (U3).
    """
    store.read_run(run_id)  # existence
    cleaned = _clean_changes(changes)

    rows = load_amendments(store, run_id)
    amendment = {
        "id": f"amd-{secrets.token_hex(8)}",
        "status": "approved" if approve else "pending",
        "changes": cleaned,
        "note": (note or "").strip()[:2000],
        "source": source,
        "created_at": utcnow(),
        "decided_at": utcnow() if approve else None,
    }
    if request_id:
        amendment["request_id"] = str(request_id).strip()[:120]
        if request_fingerprint:
            amendment["request_fingerprint"] = str(request_fingerprint)
    rows.append(amendment)
    _save_amendments(store, run_id, rows)
    return amendment


def decide_amendment(
    store: Store,
    run_id: str,
    amendment_id: str,
    *,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    if decision not in ("approved", "rejected"):
        raise ValueError(f"unknown decision: {decision!r}")
    rows = load_amendments(store, run_id)
    row = next((r for r in rows if r.get("id") == amendment_id), None)
    if row is None:
        raise ValueError("no such amendment")
    if row.get("status") != "pending":
        raise ValueError(f"amendment is already {row.get('status')}")
    row["status"] = decision
    row["decided_at"] = utcnow()
    if note:
        row["note"] = ((row.get("note") or "") + "\n" + note.strip()).strip()
    _save_amendments(store, run_id, rows)
    return row


def apply_brief_amendment(
    store: Store,
    run_id: str,
    *,
    changes: list[dict[str, Any]],
    note: str = "",
    source: str = "user",
    approve: bool = False,
    expected_brief_fingerprint: str | None = None,
    request_id: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Record one user decision about the brief, validated against *that* brief.

    **One request, one amendment — and one payload.** ``request_id`` is the
    identity of *this* submission*, minted by the caller and reused when the
    same click is retried — the same shape the decision-continuation path uses.
    If an amendment already carries it **and still carries this request's
    payload**, that amendment is returned and nothing is written: a retried
    request after a lost response must not buy a second decision, and must not
    be refused either, since the baseline it was framed against is the one it
    already changed. If the id comes back wearing a *different* payload, that
    is a conflict (``BriefRequestConflict``) — resolving it to the recorded
    amendment would answer a request the server never saw with a decision it
    does not describe (U3). The payload is frozen on the row as a fingerprint
    when it is written; rows from before that only get compared on the fields
    they actually kept.

    It cannot be derived from the content. Two identical decisions made minutes
    apart are indistinguishable on the server, and the second one is legitimate;
    only the caller knows whether it is retrying. Without the key this route is
    therefore *not* idempotent, which is what the 409 asked the page to resolve.

    Two refusals live here rather than deeper down, because both are about the
    request being a *new* decision:

    * ``expected_brief_fingerprint`` must still be the current brief (A1). The
      direction guard already refuses a stale page, but the brief can move
      while the direction does not — a second amendment is enough — and then a
      request framed against the old brief would silently rebase onto the new
      one. Same-body validation answers "is this already the value"; this
      answers "is this still the decision the user made".
    * the change must actually change something, and must target a clause the
      current brief has (F4 + A2, via ``validate_changes_against``).

    The approved row also records ``base_text`` for each change, so replaying it
    onto a later intake revision can recognise the clause it was decided against
    instead of dropping it (R2).
    """
    key = str(request_id or "").strip()
    if key:
        # Resolved here as well as at the HTTP layer: this scan runs under the
        # write lock, so the concurrent twin that slipped past the earlier
        # check still meets the same one-identity-one-payload rule.
        existing = resolve_amendment_by_request(
            store, run_id, request_id=key, changes=changes, note=note,
            approve=approve,
            expected_brief_fingerprint=expected_brief_fingerprint,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_version=expected_version)
        if existing is not None:
            return existing
    brief = compile_active_brief(store, run_id)
    validate_changes_against(brief, changes)
    if expected_brief_fingerprint is not None:
        current = fingerprint(brief)
        if str(expected_brief_fingerprint) != current:
            raise BriefBaselineChanged(current)
    index = clause_index(brief)
    goal_text = str((brief.get("goal") or {}).get("text") or "")
    stamped: list[dict[str, Any]] = []
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        row = dict(ch)
        if not row.get("base_text"):
            clause_id = str(row.get("clause_id") or "").strip()
            row["base_text"] = (goal_text if clause_id == "goal.primary"
                                else str(index.get(clause_id, {}).get("text") or ""))
        stamped.append(row)
    frozen = amendment_request_fingerprint(
        changes=changes, note=note, approve=approve,
        expected_brief_fingerprint=expected_brief_fingerprint,
        expected_artifact_sha256=expected_artifact_sha256,
        expected_version=expected_version)
    return propose_amendment(store, run_id, changes=stamped, note=note,
                             source=source, approve=approve, request_id=key,
                             request_fingerprint=frozen if key else "")


class BriefBaselineChanged(ValueError):
    """The brief moved between the user's decision and the request.

    A ``ValueError`` so existing callers keep handling it; a distinct type so
    the route can answer "reload and decide again" rather than "invalid input".
    """

    def __init__(self, current_fingerprint: str) -> None:
        super().__init__(
            "有效简报已经变化，这次修订是相对旧简报做出的；"
            "请重新阅读当前简报后再决定。"
        )
        self.current_fingerprint = current_fingerprint


class BriefRequestConflict(ValueError):
    """The same request_id arrived carrying a different payload (U3).

    One identity stands for one frozen submission. Reusing it for another
    decision — a changed body, note, approve flag or baseline — must be an
    explicit conflict: never a write, never a generated task, and never the
    old record's success dressed up as the answer to the new request.
    """

    def __init__(self, request_id: str) -> None:
        super().__init__(
            f"提交标识「{request_id}」已用于另一份不同的修订请求；"
            "请核对当前决定后重新提交。"
        )
        self.request_id = request_id


def fingerprint_for(store: Store, run_id: str) -> str:
    return fingerprint(compile_active_brief(store, run_id))


def _split_ids(cell: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,，;；\s]+", cell or "") if p.strip()]


def _chosen_option(structured: dict[str, Any]) -> str:
    from .structured import direction_options
    for opt in direction_options(structured or {}):
        if opt.get("recommended"):
            return str(opt.get("key") or "").upper()
    return ""


def option_amend_proposal(structured: dict[str, Any] | None,
                          key: str) -> dict[str, str]:
    """The amendment *an option actually proposes*, or empty fields.

    A proposal is a change the author wrote down — ``amend_proposal`` naming how
    a clause should read instead. A blocker's ``detail`` is a **diagnosis**
    ("推荐方案未声明满足 goal.primary，也未提出简报修订") and the clause id is
    an identifier; neither is a replacement body. Conflating them is what let a
    diagnostic sentence be offered to the user as the new product goal (F4), so
    this function returns nothing rather than a fallback.
    """
    for row in (structured or {}).get("options") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("key") or "").upper() != str(key or "").upper():
            continue
        return {
            "clause_id": str(row.get("amend_clause") or "").strip(),
            "text": str(row.get("amend_proposal") or "").strip(),
        }
    return {"clause_id": "", "text": ""}


def brief_gate(store: Store, run_id: str,
               structured: dict[str, Any] | None,
               *, bound: bool = False,
               rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read-only projection of option ↔ ActiveBrief alignment.

    Never raises, never mutates, never blocks a click by itself — the screen
    and the approval path read this. ``status``:

    * ``absent`` — no direction structure (other stages / empty).
    * ``unmapped`` — options exist but carry no satisfies/violates (legacy v4).
    * ``ok`` — the recommended option maps to the brief with no blockers.
    * ``blocked`` — it has violates / amend / unknown clause / no mapping /
      no goal coverage, or the brief itself carries an unresolved amendment.

    ``bound`` says this artifact was written **for the current brief** (it
    carries ``brief:snapshot``). Only then is "the author declared nothing" a
    blocker: an old v4 draft that predates the mapping columns stays readable
    and unblocked, while a new draft cannot pass the gate by staying silent.
    Checking the declared ids is not semantic verification — it only refuses to
    read "not checked" as "satisfied"; whether a claim is *true* stays with the
    independent review and the user.

    **Admission is per option, and the recommendation is not part of it.** Every
    option's own coverage is computed the same way whether or not the director
    recommended it; ``recommended`` only decides which one the card leads with.
    Otherwise "the recommendation happens to be fine" would let an unchecked
    alternative through, and "the recommendation happens to be blocked" would
    take the compliant alternative's button away with it. ``options`` reports
    one entry per key so the screen and ``gate_blockers_for`` read the same
    answer the approval path enforces.

    ``rows`` lets a caller that already holds the option rows pass them in, so
    the gate and the rest of that projection are computed from one read of the
    artifact rather than two.
    """
    structured = structured or {}
    brief = compile_active_brief(store, run_id)
    fp = fingerprint(brief)
    options = structured.get("options") or []
    base: dict[str, Any] = {
        "status": "absent",
        "fingerprint": fp,
        "brief_version": int(brief.get("version") or 1),
        "bound": bool(bound),
        "blockers": [],
        "recommended": None,
        "options": [],
        "conflicts": list(brief.get("conflicts") or []),
    }
    if not options:
        return base

    base["status"] = "unmapped"
    chosen = _chosen_option(structured)
    base["recommended"] = chosen or None
    index = clause_index(brief)
    goal_id = (brief.get("goal") or {}).get("id")
    known = set(index)
    if goal_id:
        known.add(goal_id)

    blockers: list[dict[str, Any]] = []
    admitted: list[dict[str, Any]] = []
    mapped_any = False
    for row in rows if rows is not None else options:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").upper()
        satisfies = _split_ids(str(row.get("satisfies") or ""))
        violates = _split_ids(str(row.get("violates") or ""))
        amend = str(row.get("amend_clause") or "").strip()
        proposal = str(row.get("amend_proposal") or "").strip()
        own: list[dict[str, Any]] = []
        if not (satisfies or violates or amend):
            # No declaration at all. For a draft written against this brief that
            # is a blocker on *that option* — it is nobody's business until
            # someone tries to adopt it, which is why the status below keys off
            # the recommended option and the approval path keys off the selected
            # one. A half-mapped A/B/C must not read as a hard stop on the whole
            # stage, and an unmapped option must not read as satisfied either.
            if bound and key:
                own.append({
                    "key": key, "kind": BLOCKER_UNMAPPED, "clause_id": goal_id or "",
                    "detail": "这一版没有声明它满足哪条有效简报条款；"
                              "未声明不等于符合约束，不能直接采用。",
                })
        else:
            mapped_any = True
            for cid in violates:
                own.append({
                    "key": key, "kind": BLOCKER_VIOLATES, "clause_id": cid,
                    "detail": index.get(cid, {}).get("text", cid),
                })
            if amend:
                own.append({
                    "key": key, "kind": BLOCKER_NEEDS_AMENDMENT, "clause_id": amend,
                    # Diagnosis only — never a replacement body. ``amend_proposal``
                    # is the proposal; see ``option_amend_proposal``.
                    "detail": proposal or f"方案要求先修订 {amend}，但没有写出修订正文。",
                    "proposed_text": proposal,
                })
            if bound:
                # Ids the brief does not contain prove nothing about this brief.
                for cid in satisfies + violates + ([amend] if amend else []):
                    if cid not in known:
                        own.append({
                            "key": key, "kind": BLOCKER_UNKNOWN_CLAUSE, "clause_id": cid,
                            "detail": f"引用的简报条款 {cid} 不在当前有效简报中。",
                        })
            # Every adoptable option has to claim the product goal — not only the
            # one the director recommended. An option that proposes the goal's
            # amendment is covered by that amendment instead.
            amends_goal = bool(goal_id) and amend == goal_id
            if key and goal_id and goal_id not in satisfies and not amends_goal:
                own.append({
                    "key": key, "kind": BLOCKER_GOAL_UNCOVERED, "clause_id": goal_id,
                    "detail": f"方案 {key} 未声明满足 {goal_id}，也未提出该条款的简报修订；"
                              "未声明不等于符合约束，不能直接采用。",
                })
        # The brief's own unresolved amendments block every option: adopting
        # against a brief that failed to reapply a user decision would be
        # adopting against a brief nobody agreed to. They carry no `key` because
        # they are not about an option, so they are read as-is.
        own.extend(base["conflicts"])
        blockers.extend(own)
        admitted.append({
            "key": key,
            "admissible": bool(key) and not own,
            "blockers": own,
        })

    base["options"] = admitted
    if not mapped_any and not blockers:
        return base

    # ``status`` describes the recommended option, which is the one the card
    # leads with. It never decides whether another option is admissible.
    # Run-level blockers (an unresolved amendment) carry no ``key`` and apply to
    # every option, recommendation included.
    rec_blockers = [b for b in blockers
                    if not b.get("key") or not chosen or b["key"] == chosen]
    base["blockers"] = blockers
    base["status"] = "blocked" if rec_blockers else "ok"
    return base


def gate_admissible(gate: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The options the gate would let the user adopt, in the artifact's order."""
    return [row for row in ((gate or {}).get("options") or []) if row.get("admissible")]


def gate_blockers_for(gate: dict[str, Any] | None, option_id: str) -> list[dict[str, Any]]:
    """Blockers that stand between *this selection* and adoption.

    The selected option, not the recommendation: a recommendation is advice, and
    only the option the user actually adopts has to satisfy the brief. Blockers
    carrying no key are run-level and apply to every option.
    """
    sel = str(option_id or "").upper()
    return [
        b for b in ((gate or {}).get("blockers") or [])
        if not str(b.get("key") or "") or str(b.get("key") or "").upper() == sel
    ]
