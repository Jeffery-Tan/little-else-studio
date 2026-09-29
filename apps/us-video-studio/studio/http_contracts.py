"""HTTP request contracts and one shared event redaction rule."""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from .product_cards import CardBinding


def redact_http_event(event: dict[str, Any]) -> dict[str, Any]:
    """Strip attempt tokens before any HTTP/SSE/timeline payload (red line 11).

    One rule, one function: /events, SSE, job events and state snapshots all
    call this so a new read surface cannot reintroduce the plaintext token.
    Nested open_attempt rows (state/recovery style) are redacted in place and
    the same dict is returned so callers can use either style.
    """
    if not isinstance(event, dict):
        return event
    if "token" in event:
        event["token"] = None
    attempt = event.get("open_attempt")
    if isinstance(attempt, dict) and "token" in attempt:
        attempt["token"] = None
    return event


def redact_http_events(events: Any) -> list[dict[str, Any]]:
    return [redact_http_event(e) if isinstance(e, dict) else e for e in events]


class RunIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=20000)
    kind: Literal['creative', 'learning'] = 'creative'
    product_card: CardBinding | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class FeedbackTarget(BaseModel):
    kind: Literal['writing'] = 'writing'
    run_id: str = Field(min_length=1, max_length=64)
    stage: str = Field(pattern=r'^writing$')
    version: int = Field(ge=1, strict=True)
    artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    quote: str = Field(min_length=1, max_length=240)
    path: list[str | int] | None = None


class WritingRevisionTarget(FeedbackTarget):
    path: list[str | int] = Field(min_length=1, max_length=3)


class WritingRevisionIn(MessageIn):
    text: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    feedback_target: WritingRevisionTarget


class MediaFeedbackTarget(BaseModel):
    kind: Literal['media']
    run_id: str = Field(min_length=1, max_length=64)
    attempt_id: str = Field(min_length=1, max_length=160)
    output_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    at_seconds: float | None = Field(default=None, ge=0, le=3600)


class ProductionDraftFeedbackTarget(BaseModel):
    kind: Literal['production_draft']
    run_id: str = Field(min_length=1, max_length=64)
    draft_id: str = Field(min_length=1, max_length=160)
    draft_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    attempt_id: str | None = Field(default=None, min_length=1, max_length=160)


class JobIn(MessageIn):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    question_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    reference_consent: bool = Field(default=False, strict=True)
    feedback_target: FeedbackTarget | MediaFeedbackTarget | ProductionDraftFeedbackTarget | None = None


class DirectionResumeIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_context_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class SteerIn(MessageIn):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_goal_revision: int = Field(ge=0, strict=True)
    feedback_target: FeedbackTarget | MediaFeedbackTarget | ProductionDraftFeedbackTarget | None = None


class ReviewActionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    stage: str = Field(pattern=r'^(direction|writing)$')
    expected_version: int = Field(ge=1)
    expected_artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class DecisionIn(BaseModel):
    stage: str
    nonce: str
    decision: str
    note: str = ""
    override_reason: str = ""
    expected_artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_version: int = Field(ge=1)
    selected_option_id: str | None = None
    continue_after: bool = Field(default=False, strict=True)


class DecisionContinuationIn(BaseModel):
    stage: str = Field(pattern=r'^(direction|writing)$')
    expected_version: int = Field(ge=1)
    expected_artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class BriefAmendmentChange(BaseModel):
    clause_id: str = Field(min_length=1, max_length=80)
    op: Literal["replace", "retire", "add"] = "replace"
    text: str = Field(default="", max_length=4000)
    kind: str | None = Field(default=None, max_length=32)
    #: The clause body this change was decided against. The server fills it in
    #: when a client omits it; sending it is how a replayed amendment finds its
    #: clause again after the model reworded the intake (R2).
    base_text: str | None = Field(default=None, max_length=4000)


class BriefAmendmentIn(BaseModel):
    changes: list[BriefAmendmentChange] = Field(min_length=1, max_length=12)
    note: str = Field(default="", max_length=2000)
    approve: bool = Field(default=True, strict=True)
    expected_artifact_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    expected_version: int | None = Field(default=None, ge=1)
    #: The effective brief the user was reading. Optional so an older page keeps
    #: working, but a page that sends it cannot rebase a decision onto a brief
    #: that moved while the direction version stood still (A1).
    expected_brief_fingerprint: str | None = Field(default=None, max_length=120)
    #: Identity of *this submission*, minted by the page and reused when the same
    #: click is retried. An amendment already carrying it *and this payload* is
    #: returned instead of recording a second decision; the same id wearing a
    #: different payload is a 409 ``request_conflict`` — one identity, one
    #: frozen payload (U3). Optional, because only the caller knows whether a
    #: request is a retry — two identical decisions minutes apart are
    #: indistinguishable here, and the second one is legitimate.
    request_id: str | None = Field(default=None, max_length=120)


class BriefAmendmentDecisionIn(BaseModel):
    amendment_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2000)


class LearningProposalIn(BaseModel):
    type: str
    payload: dict[str, Any]


class LearningApplyIn(BaseModel):
    expected_library_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_proposal_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    request_id: str | None = Field(default=None, min_length=1, max_length=128,
                                   pattern=r'^[A-Za-z0-9_.:-]+$')


class LearningRejectIn(BaseModel):
    expected_proposal_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class LearningRefreshIn(BaseModel):
    expected_run_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_current_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class DeepSeekCheckIn(BaseModel):
    expected_revision: int = Field(ge=0, strict=True)
    connection_id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9-]*$')
    api_key: str | None = Field(default=None, min_length=1, max_length=8192)


class MaterialObservationIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_manifest_sha256: str | None = None
    purpose: str
    level: str
    coverage: str = Field(min_length=1, max_length=2000)
    findings: list[str] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(min_length=1, max_length=30)
