"""The stage state machine.

State is *derived* from the append-only event log plus the artifact sidecars,
never stored as a mutable blob. Two consequences worth stating plainly:

* Nothing is ever deleted or overwritten to express a change of mind. A
  revision is a new version; an invalidation is a recomputed fact.
* Staleness cannot drift out of sync with the data, because it is recomputed
  from fingerprints every time it is asked for.

Four rules are enforced here rather than in prompts:

1. **Prerequisites.** A stage cannot start until every upstream stage is
   complete. "Complete" means: has an artifact, the file still matches its
   fingerprint, it is not stale, and — if the stage is gated — it is approved.
2. **A rejected attempt is dead.** Its token cannot submit afterwards.
3. **An approval binds to one fingerprint.** Revise the artifact and the old
   approval no longer counts, with no code path that "carries it forward".
4. **An approval needs a valid unconsumed nonce**, which the server only ever
   hands to the browser. No agent tool can reach this method with a nonce.
"""
from __future__ import annotations

import secrets
import json
from dataclasses import dataclass, field, replace
from typing import Any

from .models import (
    Approval,
    Artifact,
    Attempt,
    PipelineError,
    Review,
    Run,
    STAGE_SPEC,
    STAGES,
    utcnow,
)
from .store import Store, serialized_run, sha256_text
from .structured import normalize


@dataclass(frozen=True)
class StageState:
    stage: str
    status: str
    latest: Artifact | None = None
    stale: bool = False
    stale_reason: str = ""
    integrity_ok: bool = True
    review: Review | None = None
    approval: Approval | None = None
    open_attempt: Attempt | None = None
    last_rejection: str = ""
    can_start: bool = False
    can_submit: bool = False
    can_review: bool = False
    can_approve: bool = False
    blocked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "latest": self.latest.to_dict() if self.latest else None,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "integrity_ok": self.integrity_ok,
            "review": self.review.to_dict() if self.review else None,
            "approval": self.approval.to_dict() if self.approval else None,
            "open_attempt": self.open_attempt.to_dict() if self.open_attempt else None,
            "last_rejection": self.last_rejection,
            "can_start": self.can_start,
            "can_submit": self.can_submit,
            "can_review": self.can_review,
            "can_approve": self.can_approve,
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True)
class RunState:
    run: Run
    stages: dict[str, StageState] = field(default_factory=dict)
    next_stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.to_dict(),
            "stages": [self.stages[s].to_dict() for s in STAGES],
            "next_stage": self.next_stage,
        }


class Pipeline:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def upstream_stages(stage: str) -> tuple[str, ...]:
        """All facts used by a stage, in pipeline order (not just its parent)."""
        found: set[str] = set()
        def visit(name: str) -> None:
            for up in STAGE_SPEC[name].requires:
                found.add(up)
                visit(up)
        visit(stage)
        return tuple(s for s in STAGES if s in found)

    @staticmethod
    def selection_fingerprint(state: StageState) -> str | None:
        approval = state.approval
        if not approval or not approval.decision.startswith("approved") or not approval.selected_option_id:
            return None
        return sha256_text(json.dumps({
            "artifact_sha256": approval.artifact_sha256,
            "artifact_version": approval.artifact_version,
            "selected_option_id": approval.selected_option_id,
        }, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _expect_version(state: StageState, expected_artifact_sha256: str | None,
                        expected_version: int | None) -> None:
        if state.latest is None:
            raise PipelineError("version_conflict", "there is no current artifact")
        if ((expected_artifact_sha256 is not None and state.latest.sha256 != expected_artifact_sha256)
                or (expected_version is not None and state.latest.version != expected_version)):
            raise PipelineError("version_conflict", "the displayed artifact was replaced; reload before deciding")

    # ----------------------------------------------------------- derivation --

    def _attempts(self, run_id: str, stage: str) -> list[Attempt]:
        """Rebuild every attempt for a stage from the event log, in order."""
        order: list[str] = []
        by_key: dict[str, Attempt] = {}
        for ev in self.store.read_events(run_id):
            if ev.get("stage") != stage:
                continue
            kind = ev.get("kind")
            key = f"{stage}#{ev.get('number')}"
            if kind == "attempt_started":
                attempt = Attempt(
                    stage=stage,
                    number=ev["number"],
                    role=ev["role"],
                    token=ev["token"],
                    started_at=ev["at"],
                    inputs=dict(ev.get("inputs") or {}),
                )
                by_key[attempt.key] = attempt
                order.append(attempt.key)
            elif kind == "attempt_submitted" and key in by_key:
                by_key[key] = replace(by_key[key], status="submitted", submitted_at=ev["at"])
            elif kind == "attempt_rejected" and key in by_key:
                by_key[key] = replace(
                    by_key[key], status="rejected", reason=ev.get("reason", "")
                )
        return [by_key[k] for k in order]

    def stage_state(self, run_id: str, stage: str, _memo: dict | None = None) -> StageState:
        if stage not in STAGE_SPEC:
            raise PipelineError("pipeline", f"unknown stage: {stage!r}")
        memo = {} if _memo is None else _memo
        if stage in memo:
            return memo[stage]

        spec = STAGE_SPEC[stage]
        artifacts = self.store.list_artifacts(run_id, stage)
        latest = artifacts[-1] if artifacts else None

        # --- upstream fingerprints and staleness --------------------------- #
        upstream: dict[str, str] = {}
        reasons: list[str] = []
        upstream_ready = True
        required = self.upstream_stages(stage)
        for up in required:
            up_state = self.stage_state(run_id, up, memo)
            if up_state.latest is None or up_state.stale or not up_state.integrity_ok:
                upstream_ready = False
                if up_state.latest is None:
                    reasons.append(f"{up} has no work yet")
                elif up_state.stale:
                    reasons.append(f"upstream {up} is itself stale")
                else:
                    reasons.append(f"upstream {up} failed its fingerprint check")
                continue
            upstream[up] = up_state.latest.sha256
            selected = self.selection_fingerprint(up_state)
            if selected is not None:
                upstream[f"{up}:selection"] = selected
            if not memo.get(f"__complete__{up}"):
                upstream_ready = False
                reasons.append(f"upstream {up} is not complete or its decision was returned")

        stale = False
        stale_reason = ""
        if latest is not None and spec.requires:
            for up, sha in upstream.items():
                # Legacy records only promised direct body dependencies. They
                # remain readable; no new dependency coverage is invented.
                if latest.digest_algorithm == "body-v1" and up not in spec.requires:
                    continue
                if latest.inputs.get(up) != sha:
                    stale = True
                    was = latest.inputs.get(up, "")
                    source_stage = up.split(":", 1)[0]
                    old_v = next(
                        (a.version for a in self.store.list_artifacts(run_id, source_stage) if a.sha256 == was),
                        None,
                    )
                    new_v = self.stage_state(run_id, source_stage, memo).latest.version
                    stale_reason = (
                        f"{up}-v{old_v or '?'} was replaced by {up}-v{new_v}; "
                        f"{stage}-v{latest.version} was written against the older one"
                    )
                    break
            if not stale and any(
                self.stage_state(run_id, up, memo).stale for up in required
            ):
                stale = True
                stale_reason = "an upstream stage was invalidated"
            if not upstream_ready:
                stale = True
                stale_reason = "; ".join(reasons)
        if latest is not None and latest.digest_algorithm == "artifact-v2":
            from .evidence import evidence_fingerprint
            if "__evidence_snapshot__" not in memo:
                memo["__evidence_snapshot__"] = evidence_fingerprint(self.store, run_id)
            if latest.inputs.get("evidence:snapshot") != memo["__evidence_snapshot__"]:
                stale = True
                stale_reason = "source materials or observation records changed after this version was produced"

        integrity_ok = True
        if latest is not None:
            integrity_ok = self.store.verify_artifact(run_id, stage, latest.version)

        # --- review and approval, bound to the exact fingerprint ----------- #
        review = None
        approval = None
        if latest is not None:
            for r in reversed(self.store.list_reviews(run_id, stage)):
                if r.artifact_sha256 == latest.sha256 and r.artifact_version in (None, latest.version):
                    review = r
                    break
            for a in reversed(self.store.list_approvals(run_id, stage)):
                if a.artifact_sha256 == latest.sha256 and a.artifact_version in (None, latest.version):
                    approval = a
                    break

        approved = approval is not None and approval.decision.startswith("approved")
        complete = (
            latest is not None
            and not stale
            and integrity_ok
            and (approved or not spec.gated)
        )
        attempts = self._attempts(run_id, stage)
        open_attempt = next((a for a in attempts if a.status == "open"), None)
        last_rejection = next(
            (a.reason or "" for a in reversed(attempts) if a.status == "rejected"), ""
        )

        # --- what is allowed right now ------------------------------------- #
        blocked = ""
        can_start = False
        if not upstream_ready:
            blocked = "; ".join(reasons)
        elif open_attempt is not None:
            blocked = f"{stage}#{open_attempt.number} is already open — submit or reject it first"
        else:
            # An open attempt is fine to leave behind only if it was rejected;
            # otherwise the guard above catches it.
            can_start = True

        can_submit = open_attempt is not None
        if open_attempt is not None and not upstream_ready:
            can_submit = False
            blocked = "; ".join(reasons)

        can_review = (
            spec.gated
            and latest is not None
            and not stale
            and integrity_ok
            and latest.digest_algorithm == "artifact-v2"
            and (review is None or review.artifact_sha256 != latest.sha256)
        )
        # Both verdicts reach him, not only ``pass``.
        #
        # The gate exists so that approval is *his click* rather than an agent's;
        # it does not exist to give the reviewer a veto he cannot lift. The
        # reviewer is a same-model agent sitting on the same side of the table as
        # the producers, and on the first real run it returned a direction that
        # carried four substantive findings — a decision worth seeing, not a
        # dead end. Requiring ``pass`` here made ``record_approval``'s override
        # branch unreachable from the interface: the card that would have offered
        # it was never drawn, so the only way to override was to hand-craft an
        # HTTP request.
        #
        # Approving past a ``return`` is *not* free — ``record_approval`` refuses
        # it without an explicit ``override_reason`` and records
        # ``approved_override`` instead of ``approved``, so it stays legible in
        # the history as the deliberate decision it is.
        #
        # ``review is not None`` still holds: with no review recorded there is
        # nothing on screen to weigh, and approving the unreviewed thing is a
        # different feature.
        can_approve = (
            spec.gated
            and latest is not None
            and not stale
            and integrity_ok
            and latest.digest_algorithm == "artifact-v2"
            and not approved
            and review is not None
        )

        if not spec.gated:
            status = "empty"
            if latest is not None:
                status = "stale" if stale else "done"
        else:
            if latest is None:
                status = "empty"
            elif stale:
                status = "stale"
            elif approved:
                status = "approved"
            elif review is not None and review.verdict == "return":
                status = "returned"
            elif review is not None:
                status = "reviewed"
            else:
                status = "awaiting_review"

        state = StageState(
            stage=stage,
            status=status,
            latest=latest,
            stale=stale,
            stale_reason=stale_reason,
            integrity_ok=integrity_ok,
            review=review,
            approval=approval,
            open_attempt=open_attempt,
            last_rejection=last_rejection,
            can_start=can_start,
            can_submit=can_submit,
            can_review=can_review,
            can_approve=can_approve,
            blocked_reason=blocked,
        )
        memo[stage] = state
        # ``complete`` is used by dependents; stash it alongside.
        memo[f"__complete__{stage}"] = complete
        return state

    def is_complete(self, run_id: str, stage: str, memo: dict | None = None) -> bool:
        """Has this stage produced something a downstream stage may build on?"""
        own_memo = memo if memo is not None else {}
        self.stage_state(run_id, stage, own_memo)
        return bool(own_memo.get(f"__complete__{stage}"))

    def state(self, run_id: str) -> RunState:
        run = self.store.read_run(run_id)
        memo: dict = {}
        stages = {s: self.stage_state(run_id, s, memo) for s in STAGES}
        nxt = next((s for s in STAGES if not memo.get(f"__complete__{s}")), None)
        return RunState(run=run, stages=stages, next_stage=nxt)

    # -------------------------------------------------------------- actions --

    def require_prerequisites(self, run_id: str, stage: str) -> dict[str, str]:
        spec = STAGE_SPEC[stage]
        memo: dict = {}
        inputs: dict[str, str] = {}
        from .evidence import evidence_fingerprint
        inputs["evidence:snapshot"] = evidence_fingerprint(self.store, run_id)
        for up in self.upstream_stages(stage):
            self.stage_state(run_id, up, memo)
            if not memo.get(f"__complete__{up}"):
                up_state = memo[up]
                why = up_state.stale_reason or up_state.blocked_reason or "not complete"
                raise PipelineError(
                    "prereq",
                    f"{stage} cannot proceed: upstream {up} is not complete ({why})",
                )
            inputs[up] = memo[up].latest.sha256
            if memo[up].latest.digest_algorithm != "artifact-v2":
                raise PipelineError("prereq", f"{up} is legacy body-only history; produce a fully bound new version before starting {stage}")
            selected = self.selection_fingerprint(memo[up])
            if selected is not None:
                inputs[f"{up}:selection"] = selected
        return inputs

    @serialized_run
    def start_attempt(self, run_id: str, stage: str, *, actor: str = "user") -> Attempt:
        spec = STAGE_SPEC[stage]
        self.store.read_run(run_id)  # existence check
        inputs = self.require_prerequisites(run_id, stage)

        current = self.stage_state(run_id, stage)
        if current.open_attempt is not None:
            raise PipelineError(
                "attempt",
                f"{stage}#{current.open_attempt.number} is already open; "
                f"submit or reject it before starting another",
            )

        attempts = self._attempts(run_id, stage)
        number = (max((a.number for a in attempts), default=0)) + 1
        token = secrets.token_urlsafe(24)
        attempt = Attempt(
            stage=stage,
            number=number,
            role=spec.owner_role,
            token=token,
            started_at=utcnow(),
            inputs=inputs,
        )
        self.store.append_event(
            run_id,
            "attempt_started",
            stage=stage,
            number=number,
            role=spec.owner_role,
            token=token,
            inputs=inputs,
            actor=actor,
        )
        return attempt

    @serialized_run
    def submit(
        self,
        run_id: str,
        stage: str,
        token: str,
        *,
        content: str,
        summary: str,
        evidence: list[str] | None = None,
        limitations: list[str] | None = None,
        structured: dict[str, Any] | None = None,
        actor: str = "agent",
    ) -> Artifact:
        """Turn an open attempt into an immutable artifact version."""
        spec = STAGE_SPEC[stage]
        attempts = self._attempts(run_id, stage)
        attempt = next((a for a in attempts if a.token == token), None)
        if attempt is None:
            raise PipelineError("attempt", f"no attempt for {stage} carries this token")
        if attempt.status == "rejected":
            raise PipelineError(
                "attempt",
                f"{stage}#{attempt.number} was rejected and cannot submit late results",
            )
        if attempt.status != "open":
            raise PipelineError("attempt", f"{stage}#{attempt.number} is already {attempt.status}")

        # Re-check upstream *now*: it may have moved while this attempt was in
        # flight. Completeness is not enough — the work must have been briefed
        # on the versions that are current at submit time.
        inputs = self.require_prerequisites(run_id, stage)
        for up, sha in inputs.items():
            if attempt.inputs.get(up) != sha:
                if ":" in up:
                    raise PipelineError("stale_attempt", f"{up} changed after {stage}#{attempt.number} was briefed; start a new attempt")
                was_v = next(
                    (a.version for a in self.store.list_artifacts(run_id, up)
                     if a.sha256 == attempt.inputs.get(up)),
                    None,
                )
                now_v = next(
                    (a.version for a in self.store.list_artifacts(run_id, up) if a.sha256 == sha),
                    None,
                )
                raise PipelineError(
                    "stale_attempt",
                    f"{up} moved from v{was_v or '?'} to v{now_v or '?'} after "
                    f"{stage}#{attempt.number} was briefed. This work was done against the "
                    f"older version; start a new attempt so it is written against the current one.",
                )

        version = self.store.next_version(run_id, stage)
        # Coerced here rather than in the store: the shape is stage semantics,
        # and normalising is what makes a bad payload cost a rendering instead
        # of an attempt. `normalize` never raises; see `studio/structured.py`.
        shape = normalize(stage, structured or {})
        artifact = self.store.write_artifact(
            run_id,
            stage,
            version,
            content,
            summary=summary,
            author_role=attempt.role,
            attempt=attempt.key,
            evidence=evidence or [],
            limitations=limitations or [],
            inputs=inputs,
            structured=shape.data,
        )
        self.store.append_event(
            run_id,
            "attempt_submitted",
            stage=stage,
            number=attempt.number,
            artifact=f"{stage}-v{version}",
            sha256=artifact.sha256,
            actor=actor,
        )
        if structured and shape.dropped:
            # Recorded, not swallowed. A prompt that is quietly failing to
            # produce its shape should be visible in the log, because the only
            # other symptom is a screen that looks emptier than it should.
            self.store.append_event(
                run_id,
                "structure_dropped",
                stage=stage,
                artifact=f"{stage}-v{version}",
                dropped=shape.dropped,
                at=utcnow(),
            )
        return artifact

    @serialized_run
    def reject(self, run_id: str, stage: str, token: str, reason: str) -> None:
        if not reason.strip():
            raise PipelineError("attempt", "rejection needs a specific reason")
        attempts = self._attempts(run_id, stage)
        attempt = next((a for a in attempts if a.token == token), None)
        if attempt is None:
            raise PipelineError("attempt", f"no attempt for {stage} carries this token")
        if attempt.status != "open":
            raise PipelineError("attempt", f"{stage}#{attempt.number} is already {attempt.status}")
        self.store.append_event(
            run_id,
            "attempt_rejected",
            stage=stage,
            number=attempt.number,
            reason=reason.strip(),
        )

    @serialized_run
    def record_review(
        self,
        run_id: str,
        stage: str,
        *,
        verdict: str,
        findings: list[dict[str, Any]],
        expected_artifact_sha256: str | None = None,
        expected_version: int | None = None,
    ) -> Review:
        """Record the reviewer's verdict on the *current* artifact fingerprint."""
        if verdict not in ("pass", "return"):
            raise PipelineError("review", f"verdict must be 'pass' or 'return', got {verdict!r}")
        spec = STAGE_SPEC[stage]
        if not spec.gated:
            raise PipelineError("review", f"{stage} has no review gate in v1")
        state = self.stage_state(run_id, stage)
        if state.latest is None:
            raise PipelineError("review", f"{stage} has nothing to review")
        if state.latest.digest_algorithm != "artifact-v2":
            raise PipelineError("review", "legacy body-only history cannot receive a new full-content review; produce a new version")
        self._expect_version(state, expected_artifact_sha256, expected_version)
        if state.stale:
            raise PipelineError(
                "review", f"{stage}-v{state.latest.version} is stale and must be redone: {state.stale_reason}"
            )
        if not state.integrity_ok:
            raise PipelineError(
                "review", f"{stage}-v{state.latest.version} fails its fingerprint check"
            )
        if verdict == "return" and not findings:
            raise PipelineError("review", "a return needs at least one specific finding")

        review = Review(
            stage=stage,
            artifact_sha256=state.latest.sha256,
            verdict=verdict,
            reviewer="reviewer",
            findings=findings,
            created_at=utcnow(),
            artifact_version=state.latest.version,
        )
        self.store.write_review(run_id, review)
        self.store.append_event(
            run_id,
            "review_recorded",
            stage=stage,
            artifact=f"{stage}-v{state.latest.version}",
            verdict=verdict,
            findings=len(findings),
        )
        return review

    # ------------------------------------------------------------ approvals --

    @serialized_run
    def issue_nonce(self, run_id: str, stage: str, *,
                    expected_artifact_sha256: str | None = None,
                    expected_version: int | None = None) -> str:
        """Mint a single-use approval nonce. The server hands it only to the UI.

        Only ``sha256(nonce)`` is written down. The plaintext lives in the HTTP
        response and the browser, so an agent able to read the run directory
        still cannot forge a click. The audit trail records *that* a nonce was
        issued for a stage, and which one was spent, without recording a value
        that could be replayed.
        """
        if not STAGE_SPEC[stage].gated:
            raise PipelineError("approval", f"{stage} is not a gated stage")
        state = self.stage_state(run_id, stage)
        self._expect_version(state, expected_artifact_sha256, expected_version)
        if state.latest.digest_algorithm != "artifact-v2":
            raise PipelineError("approval", "legacy body-only artifacts are read-only; produce a fully bound new version before approving")
        nonce = secrets.token_urlsafe(24)
        self.store.append_event(
            run_id, "nonce_issued", stage=stage, nonce_sha256=sha256_text(nonce),
            artifact_sha256=state.latest.sha256, artifact_version=state.latest.version,
        )
        return nonce

    def _nonce_usable(self, run_id: str, stage: str, nonce: str, artifact: Artifact) -> bool:
        digest = sha256_text(nonce or "")
        issued = False
        for ev in self.store.read_events(run_id):
            if ev.get("kind") != "nonce_issued":
                continue
            if ev.get("nonce_sha256") != digest:
                continue
            if ev.get("stage") != stage:
                return False
            if (ev.get("artifact_sha256") != artifact.sha256
                    or ev.get("artifact_version") != artifact.version):
                return False
            issued = True
        if not issued:
            return False
        return not any(
            a.nonce_sha256 == digest for a in self.store.list_approvals(run_id, stage)
        )

    @serialized_run
    def record_approval(
        self,
        run_id: str,
        stage: str,
        *,
        nonce: str,
        decision: str,
        note: str = "",
        override_reason: str = "",
        selected_option_id: str | None = None,
        expected_artifact_sha256: str | None = None,
        expected_version: int | None = None,
    ) -> Approval:
        """Record a user decision. Only the server calls this, only after a click.

        A pass review on the current fingerprint is required. If Jeffery wants to
        go against a reviewer's ``return``, that is allowed but must be *named* —
        the override is recorded in the approval row and shows up in the history.

        Both decisions that send something onward have to carry the content that
        makes them actionable, and the two rules sit next to each other because
        they are one rule: ``approved`` against a failing review needs an
        ``override_reason``, and ``returned`` needs a ``note``. The plain path —
        approving a version the reviewer passed — needs neither, so nothing is
        demanded of him for doing the normal thing.
        """
        spec = STAGE_SPEC[stage]
        if not spec.gated:
            raise PipelineError("approval", f"{stage} is not a gated stage")
        if decision not in ("approved", "returned"):
            raise PipelineError("approval", f"unknown decision: {decision!r}")
        # A lost HTTP response can be retried without creating a second vote.
        # The same nonce must never mean a different decision or option.
        prior = next((a for a in self.store.list_approvals(run_id, stage)
                      if a.nonce_sha256 == sha256_text(nonce or "")), None)
        final_note = note.strip()
        if prior is not None and prior.decision == "approved_override" and override_reason.strip():
            final_note = (note + f"\n[override] {override_reason.strip()}").strip()
        if prior is not None:
            if (prior.decision in (decision, "approved_override" if decision == "approved" else decision)
                    and prior.note == final_note
                    and prior.selected_option_id == selected_option_id
                    and (expected_artifact_sha256 is None or prior.artifact_sha256 == expected_artifact_sha256)
                    and (expected_version is None or prior.artifact_version == expected_version)):
                return prior
            raise PipelineError("approval", "this nonce was already used for a different decision")

        state = self.stage_state(run_id, stage)
        if state.latest is None:
            raise PipelineError("approval", f"{stage} has nothing to approve")
        self._expect_version(state, expected_artifact_sha256, expected_version)
        if not self._nonce_usable(run_id, stage, nonce, state.latest):
            raise PipelineError(
                "approval",
                "this approval link is not valid, is for another stage, or was already used. "
                "Reload the page to get a fresh one.",
            )

        if state.stale:
            raise PipelineError(
                "approval",
                f"{stage}-v{state.latest.version} is stale: {state.stale_reason}. "
                f"Redo it before approving.",
            )
        if not state.integrity_ok:
            raise PipelineError(
                "approval",
                f"{stage}-v{state.latest.version} fails its fingerprint check; "
                f"the file changed after it was written",
            )

        recorded = decision
        if decision == "approved":
            options = state.latest.structured.get("options", []) if stage == "direction" else []
            keys = [option.get("key") for option in options]
            if options and (not selected_option_id or selected_option_id not in keys
                            or keys.count(selected_option_id) != 1):
                raise PipelineError("approval", "choose one valid direction option explicitly before approving")
            if selected_option_id is not None and (stage != "direction" or not options):
                raise PipelineError("approval", "this artifact has no selectable direction options")
            passed = state.review is not None and state.review.verdict == "pass"
            if not passed:
                if not override_reason.strip():
                    raise PipelineError(
                        "approval",
                        "no passing review on the current version. Approving anyway requires "
                        "an explicit override reason, which is recorded.",
                    )
                recorded = "approved_override"
                note = (note + f"\n[override] {override_reason.strip()}").strip()
        elif decision == "returned" and not note.strip():
            # The mirror of the rule above, and it is here for the same reason:
            # a decision that *sends something onward* has to carry the content
            # that makes it actionable.
            #
            # A return goes to the agent that wrote the document, and that agent
            # acts on this text and nothing else. An empty one tells it 「do it
            # again」 without saying what to do differently, so its only options
            # are to guess or to redo the work unchanged — one is a wrong fix,
            # the other is a wasted turn, and both cost real tokens. Gitea and
            # Forgejo refuse this server-side for exactly this reason
            # (`models/issues/review.go`: "You need to leave a comment indicating
            # the requested change(s).").
            #
            # The screen also asks for the note before it sends (see `wireGate`),
            # but that is a courtesy: the rule is the server's, so a replayed
            # request or a stale page cannot get around it.
            raise PipelineError(
                "approval",
                "returning a stage requires a note saying what to change; "
                "the writer is an agent and this note is what it acts on.",
            )
        if decision == "returned" and selected_option_id is not None:
            raise PipelineError("approval", "returning a proposal is not selecting an option")

        approval = Approval(
            stage=stage,
            artifact_sha256=state.latest.sha256,
            decision=recorded,
            note=note.strip(),
            decided_at=utcnow(),
            nonce_sha256=sha256_text(nonce),
            artifact_version=state.latest.version,
            selected_option_id=selected_option_id,
        )
        self.store.append_approval(run_id, approval)
        self.store.append_event(
            run_id,
            "stage_decided",
            stage=stage,
            artifact=f"{stage}-v{state.latest.version}",
            decision=recorded,
            selected_option_id=selected_option_id,
        )
        return approval
