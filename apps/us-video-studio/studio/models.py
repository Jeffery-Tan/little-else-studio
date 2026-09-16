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

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "title": self.title,
            "brief": self.brief,
            "market": self.market,
            "created_at": self.created_at,
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
    #: The stage's own shape — options, a dependency matrix, material rows —
    #: normalised by ``structured.normalize``. Optional and additive: an empty
    #: dict means the model did not produce the shape and the interface falls
    #: back to prose. Persisted in the sidecar, so it is **not covered by
    #: `sha256`**, which digests the Markdown body alone — the same standing as
    #: `evidence` and `limitations`.
    structured: dict[str, Any] = field(default_factory=dict)

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "artifact_sha256": self.artifact_sha256,
            "verdict": self.verdict,
            "reviewer": self.reviewer,
            "findings": [dict(f) for f in self.findings],
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Approval:
    """A user decision.

    Only the *hash* of the approval nonce is persisted. The plaintext is minted
    by the server, handed to the browser, and never written to disk — otherwise
    an agent that can read the run directory could replay it. No agent tool has
    a path to writing this record, and none can produce a nonce that verifies.
    """

    stage: str
    artifact_sha256: str
    decision: str  # approved | approved_override | returned
    note: str
    decided_at: str
    nonce_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "artifact_sha256": self.artifact_sha256,
            "decision": self.decision,
            "note": self.note,
            "decided_at": self.decided_at,
            "nonce_sha256": self.nonce_sha256,
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
    input_tokens: int
    output_tokens: int
    estimated_usd: float
    pricing_source: str
    stop_reason: str = ""
    request_id: str = ""
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "run_id": self.run_id,
            "role": self.role,
            "stage": self.stage,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": round(self.estimated_usd, 6),
            "pricing_source": self.pricing_source,
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
