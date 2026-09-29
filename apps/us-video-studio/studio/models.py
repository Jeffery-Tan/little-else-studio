"""Domain types for the studio.

Everything here is plain data with an explicit ``to_dict``. The pipeline is a
state machine over an append-only event log (see ``pipeline.py``), so these
types describe *records*, not mutable state.

Two rules that show up in the type shapes:

* An artifact carries the fingerprint of the inputs it was produced from
  (``inputs``). That is what makes invalidation possible without guessing.
* An approval carries the fingerprint of the artifact it approved
  (``artifact_sha256``). An approval therefore cannot silently survive a
  revision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: True for the whole of v1. A run is US or it is not opened.
MARKET = "US"


def utcnow() -> str:
    """Timestamp as an ISO-8601 UTC string. Sortable, no local-time ambiguity."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id or ""))


# --------------------------------------------------------------------------- #
# Pipeline definition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageSpec:
    name: str
    requires: tuple[str, ...]
    owner_role: str
    #: Gated stages need an explicit user click before anything downstream opens.
    gated: bool = False


#: The v1 chain. ``layout_preview`` / ``ad_preview`` from the old manifest need
#: local rendering tools and are deliberately absent.
PIPELINE: tuple[StageSpec, ...] = (
    StageSpec("intake", (), "orchestrator", gated=False),
    StageSpec("direction", ("intake",), "director", gated=True),
    StageSpec("material_plan", ("direction",), "material", gated=False),
    StageSpec("writing", ("material_plan",), "writer", gated=True),
)

STAGES: tuple[str, ...] = tuple(s.name for s in PIPELINE)
STAGE_SPEC: dict[str, StageSpec] = {s.name: s for s in PIPELINE}

#: Stage -> the role that reviews it. Reviewer is never the owner.
REVIEWER_FOR: dict[str, str] = {s.name: "reviewer" for s in PIPELINE if s.gated}


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Run:
    run_id: str
    title: str
    brief: str
    market: str
    created_at: str
    kind: str = "creative"  # creative | learning; absent on historical run.json

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "title": self.title,
            "brief": self.brief,
            "market": self.market,
            "created_at": self.created_at,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Attempt:
    """One unit of assigned work. A rejected attempt cannot submit late."""

    stage: str
    number: int
    role: str
    token: str
    started_at: str
    #: stage -> sha256 of the upstream artifact this attempt was briefed on.
    inputs: dict[str, str] = field(default_factory=dict)
    status: str = "open"  # open | submitted | rejected
    submitted_at: str | None = None
    reason: str | None = None

    @property
    def key(self) -> str:
        return f"{self.stage}#{self.number}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "number": self.number,
            "role": self.role,
            "token": self.token,
            "started_at": self.started_at,
            "inputs": dict(self.inputs),
            "status": self.status,
            "submitted_at": self.submitted_at,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Artifact:
    """An immutable file plus the record of what it was made from."""

    stage: str
    version: int
    filename: str
    sha256: str
    summary: str
    evidence: list[str]
    limitations: list[str]
    author_role: str
    attempt: str
    created_at: str
    inputs: dict[str, str] = field(default_factory=dict)
    #: Structured presentation. artifact-v2 fingerprints cover this alongside
    #: body and dependencies; body-v1 is retained solely for legacy replay.
    structured: dict[str, Any] = field(default_factory=dict)
    digest_algorithm: str = "body-v1"

    @property
    def key(self) -> str:
        return f"{self.stage}-v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "version": self.version,
            "filename": self.filename,
            "sha256": self.sha256,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
            "author_role": self.author_role,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "inputs": dict(self.inputs),
            "structured": dict(self.structured),
            "digest_algorithm": self.digest_algorithm,
        }


@dataclass(frozen=True)
class Review:
    """A reviewer verdict on one exact artifact fingerprint."""

    stage: str
    artifact_sha256: str
    verdict: str  # pass | return
    reviewer: str
    findings: list[dict[str, Any]]
    created_at: str
    artifact_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "artifact_version": self.artifact_version,
            "artifact_sha256": self.artifact_sha256,
            "verdict": self.verdict,
            "reviewer": self.reviewer,
            "findings": [dict(f) for f in self.findings],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Approval:
    """A user decision.

    The record is the same whether the user clicked or said it: a decision is a
    decision, and only the *input channel* differs. ``source`` says which one,
    purely for the audit trail — no code branches on it to decide whether the
    decision counts.

    ``continuation_note`` is separate from ``note`` on purpose. ``note`` is a
    return's requested changes, recorded against the version being judged.
    ``continuation_note`` is an instruction for the *next* round ("B, but make
    the opening less like a family ad") and leaves the adopted version's bytes
    and fingerprint untouched. Folding the two together would either forbid
    "adopt B and, next, do X" or quietly rewrite what was adopted.

    Only the *hash* of the approval nonce is persisted. The plaintext is minted
    by the server, handed to the browser, and never written to disk — otherwise
    an agent that can read the run directory could replay it. A conversation
    decision carries no nonce at all: it is verified against the run's own
    derived action candidates instead, so there is nothing to replay.
    """

    stage: str
    artifact_sha256: str
    decision: str  # approved | approved_override | returned
    note: str
    decided_at: str
    nonce_sha256: str
    selected_option_id: str | None = None
    artifact_version: int | None = None
    continue_after: bool = False
    #: 'ui' when the browser clicked, 'conversation' when the user said it.
    source: str = "ui"
    #: Instruction for the work that follows this decision; never part of it.
    continuation_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "artifact_sha256": self.artifact_sha256,
            "decision": self.decision,
            "note": self.note,
            "decided_at": self.decided_at,
            "nonce_sha256": self.nonce_sha256,
            "selected_option_id": self.selected_option_id,
            "artifact_version": self.artifact_version,
            "continue_after": self.continue_after,
            "source": self.source,
            "continuation_note": self.continuation_note,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """One model call.

    Tokens are facts reported by the API. ``estimated_usd`` is an estimate and
    is always paired with ``pricing_source`` so nobody mistakes it for a bill.
    """

    at: str
    run_id: str
    role: str
    stage: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    estimated_usd: float | None
    pricing_source: str
    stop_reason: str = ""
    request_id: str = ""
    ok: bool = True
    error: str = ""
    usage_status: str = "legacy"
    call_status: str = "settled"
    call_id: str = ""
    resolution_note: str = ""
    provider: str = "legacy"
    connection_id: str = ""
    pricing_as_of: str = ""
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    actual_model: str = ""
    usage_unit: str = "tokens"
    usage_quantity: float | None = None
    estimated_cny: float | None = None
    input_cny_per_mtok: float | None = None
    output_cny_per_mtok: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "run_id": self.run_id,
            "role": self.role,
            "stage": self.stage,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": round(self.estimated_usd, 6) if self.estimated_usd is not None else None,
            "usage_status": self.usage_status,
            "call_status": self.call_status,
            "call_id": self.call_id,
            "resolution_note": self.resolution_note,
            "pricing_source": self.pricing_source,
            "provider": self.provider,
            "connection_id": self.connection_id,
            "pricing_as_of": self.pricing_as_of,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "actual_model": self.actual_model,
            "usage_unit": self.usage_unit,
            "usage_quantity": self.usage_quantity,
            "estimated_cny": self.estimated_cny,
            "input_cny_per_mtok": self.input_cny_per_mtok,
            "output_cny_per_mtok": self.output_cny_per_mtok,
            "stop_reason": self.stop_reason,
            "request_id": self.request_id,
            "ok": self.ok,
            "error": self.error,
        }


class PipelineError(RuntimeError):
    """A rule in the state machine was violated. Always carries the rule name."""

    def __init__(self, rule: str, message: str):
        super().__init__(f"[{rule}] {message}")
        self.rule = rule


class BudgetExceeded(PipelineError):
    def __init__(self, message: str):
        super().__init__("budget", message)
