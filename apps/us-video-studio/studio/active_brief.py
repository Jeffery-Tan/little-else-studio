"""Compile the ActiveBrief from records that already exist.

P0: turn intake structure + chat answers into one machine-checkable brief.
P2: apply user-approved ``amend_brief`` changes so the product goal can only
change through an explicit decision, never by prose drift.

The base compile is still pure and deterministic — no model call. Amendments
live in ``runs/<id>/briefs/amendments.json`` (atomic replace). ``version`` is
``1 + approved count``; ``fingerprint`` therefore moves when a brief is
amended, which is what invalidates downstream artifacts (pipeline stale).

Clause ids are content-stable (``must.3f2a91c0``). ``goal.primary`` keeps its
id across replacements so the gate can keep pointing at the same product goal.
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
    if len(head) > 200:
        head = head[:200].rstrip() + "…"
    if not head:
        return None
    return {
        "id": "goal.primary",
        "text": head,
        "kind": "product_goal",
        "source": "run.brief",
    }


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


def _apply_changes(brief: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply approved change ops in order. Unknown clause_id is a no-op miss."""
    goal = dict(brief["goal"]) if brief.get("goal") else None
    clauses = [dict(c) for c in brief.get("clauses") or []]
    by_id = {c.get("id"): c for c in clauses}

    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        clause_id = str(ch.get("clause_id") or "").strip()
        op = str(ch.get("op") or "replace").strip()
        text = str(ch.get("text") or "").strip()
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
                "scope": "global",
                "status": "confirmed",
            }
            if row["id"] not in by_id:
                clauses.append(row)
                by_id[row["id"]] = row
            if new_id == "goal.primary" and text:
                goal = {"id": "goal.primary", "text": text, "kind": "product_goal",
                        "source": "brief_amendment"}
            continue
        target = by_id.get(clause_id)
        if op == "retire":
            if target is not None:
                clauses = [c for c in clauses if c.get("id") != clause_id]
                by_id.pop(clause_id, None)
            if clause_id == "goal.primary":
                # Retiring the product goal is not allowed — keep the old text.
                pass
            continue
        # replace
        if not text:
            continue
        if target is not None:
            new_text_id = target["id"] if clause_id == "goal.primary" else _stable_clause_id(
                str(target.get("kind") or "must"), text)
            new_row = {**target, "text": text, "source": "brief_amendment"}
            if new_text_id != target["id"]:
                new_row["id"] = new_text_id
                by_id.pop(target["id"], None)
            # keep id stable for goal.primary
            if clause_id == "goal.primary":
                new_row["id"] = "goal.primary"
            clauses = [c for c in clauses if c.get("id") != target["id"]]
            clauses.append(new_row)
            by_id[new_row["id"]] = new_row
            if clause_id == "goal.primary" or new_row["id"] == "goal.primary":
                goal = {"id": "goal.primary", "text": text, "kind": "product_goal",
                        "source": "brief_amendment"}
        elif clause_id == "goal.primary" and text:
            goal = {"id": "goal.primary", "text": text, "kind": "product_goal",
                    "source": "brief_amendment"}
            clauses = [c for c in clauses if c.get("id") != "goal.primary"]
            row = {**goal, "check": "presence", "scope": "global", "status": "confirmed"}
            clauses.append(row)
            by_id["goal.primary"] = row

    clauses.sort(key=lambda c: (c["kind"], c["text"], c.get("id") or ""))
    return {**brief, "goal": goal, "clauses": clauses}


def compile_active_brief(store: Store, run_id: str, *, version: int | None = None) -> dict[str, Any]:
    """Build the ActiveBrief for ``run_id`` (base compile + approved amendments)."""
    run = store.read_run(run_id)
    intake = store.latest_artifact(run_id, "intake")
    structured: dict[str, Any] = dict(intake.structured or {}) if intake else {}
    source = intake.key if intake else "intake:missing"
    chat = store.read_chat(run_id)
    replies = _user_replies(chat)

    goal = _goal_from_intake(structured, source) or _goal_from_run(run.brief)
    clauses: list[dict[str, Any]] = []
    if goal is not None:
        clauses.append({**goal, "check": "presence", "scope": "global",
                        "status": "confirmed"})
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
    for row in approved:
        brief = _apply_changes(brief, row.get("changes") or [])
    brief["version"] = int(version) if version is not None else 1 + len(approved)
    brief["brief_id"] = f"brf-{content_sha({'goal': brief.get('goal'), 'clauses': brief.get('clauses')})[:12]}"
    # ``compiled_at`` must not enter content hashes / fingerprints.
    return brief


def propose_amendment(
    store: Store,
    run_id: str,
    *,
    changes: list[dict[str, Any]],
    note: str = "",
    source: str = "user",
    approve: bool = False,
) -> dict[str, Any]:
    """Record a brief amendment. ``approve=True`` applies it immediately
    (one explicit user click from the blocked gate); otherwise status=pending
    until ``decide_amendment``.
    """
    store.read_run(run_id)  # existence
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
        cleaned.append(row)
    if not cleaned:
        raise ValueError("amendment needs at least one change")

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


def brief_gate(store: Store, run_id: str,
               structured: dict[str, Any] | None) -> dict[str, Any]:
    """Read-only projection of option ↔ ActiveBrief alignment (P1).

    Never raises, never mutates, never blocks a click by itself — the screen
    and later the gate read this. ``status``:

    * ``absent`` — no direction structure (other stages / empty).
    * ``unmapped`` — options exist but carry no satisfies/violates (legacy v4).
    * ``ok`` — recommended option maps to the brief with no blockers.
    * ``blocked`` — recommended option has violates / amend / unknown goal.
    """
    structured = structured or {}
    brief = compile_active_brief(store, run_id)
    fp = fingerprint(brief)
    options = structured.get("options") or []
    base: dict[str, Any] = {
        "status": "absent",
        "fingerprint": fp,
        "brief_version": int(brief.get("version") or 1),
        "blockers": [],
        "recommended": None,
    }
    if not options:
        return base

    base["status"] = "unmapped"
    chosen = _chosen_option(structured)
    base["recommended"] = chosen or None
    index = clause_index(brief)
    goal_id = (brief.get("goal") or {}).get("id")

    blockers: list[dict[str, Any]] = []
    mapped_any = False
    for row in options:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").upper()
        satisfies = _split_ids(str(row.get("satisfies") or ""))
        violates = _split_ids(str(row.get("violates") or ""))
        amend = str(row.get("amend_clause") or "").strip()
        proposal = str(row.get("amend_proposal") or "").strip()
        if not (satisfies or violates or amend):
            continue
        mapped_any = True
        for cid in violates:
            blockers.append({
                "key": key, "kind": "violates", "clause_id": cid,
                "detail": index.get(cid, {}).get("text", cid),
            })
        if amend:
            blockers.append({
                "key": key, "kind": "needs_amendment", "clause_id": amend,
                "detail": proposal or index.get(amend, {}).get("text", amend),
            })
        # A recommended option must claim the product goal unless it amended it.
        if key and key == chosen and goal_id and goal_id not in satisfies and not amend:
            if not any(b["clause_id"] == goal_id for b in blockers if b["key"] == key):
                blockers.append({
                    "key": key, "kind": "violates", "clause_id": goal_id,
                    "detail": "推荐方案未声明满足 goal.primary，也未提出简报修订",
                })

    if not mapped_any:
        return base

    rec_blockers = [b for b in blockers if not chosen or b["key"] == chosen]
    base["blockers"] = blockers
    base["status"] = "blocked" if rec_blockers else "ok"
    return base
