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
from .structured import normalize, canonical_document, validate_canonical_input, material_blockers


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
    #: P1 projection only: option ↔ ActiveBrief alignment for this latest
    #: artifact. Never gates a click on its own — see active_brief.brief_gate.
    brief_gate: dict[str, Any] | None = None

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
            "brief_gate": self.brief_gate,
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
            from .evidence import evidence_fingerprint, evidence_generation
            if "__evidence_snapshot__" not in memo:
                memo["__evidence_snapshot__"] = evidence_fingerprint(self.store, run_id)
                memo["__evidence_generation__"] = evidence_generation(self.store, run_id)
            hash_stale = latest.inputs.get("evidence:snapshot") != memo["__evidence_snapshot__"]
            # Generation is frozen at write; once it advances the artifact is
            # permanently stale even if content later returns to the same hash
            # (A→B→A). Legacy inputs without the key keep hash-only checks.
            frozen_gen = latest.inputs.get("evidence:generation")
            gen_stale = (
                frozen_gen is not None
                and isinstance(memo["__evidence_generation__"], int)
                and memo["__evidence_generation__"] > int(frozen_gen)
            )
            if hash_stale or gen_stale:
                stale = True
                stale_reason = "source materials or observation records changed after this version was produced"

        if latest is not None and stage != 'intake':
            from .references import reference_fingerprint, reference_generation
            if '__references_snapshot__' not in memo:
                memo['__references_snapshot__'] = reference_fingerprint(self.store, run_id)
                memo['__references_generation__'] = reference_generation(self.store, run_id)
            hash_stale = latest.inputs.get('references:snapshot') != memo['__references_snapshot__']
            frozen_gen = latest.inputs.get('references:generation')
            gen_stale = (
                frozen_gen is not None
                and isinstance(memo['__references_generation__'], int)
                and memo['__references_generation__'] > int(frozen_gen)
            )
            if hash_stale or gen_stale:
                stale = True
                stale_reason = '项目选择的灵感参考已调整，请根据当前参考更新这一版。'

        # ActiveBrief binding (P1). Only artifacts that *carry* the key are
        # checked — pre-P1 versions without brief:snapshot stay readable and
        # are not mass-invalidated on deploy. New attempts always stamp it.
        brief_gate: dict[str, Any] | None = None
        brief_bound = False
        if latest is not None and stage != "intake" and "brief:snapshot" in latest.inputs:
            brief_bound = True
            from .active_brief import compile_active_brief, fingerprint as brief_fp
            if "__brief_fingerprint__" not in memo:
                memo["__brief_fingerprint__"] = brief_fp(compile_active_brief(self.store, run_id))
            if latest.inputs["brief:snapshot"] != memo["__brief_fingerprint__"]:
                stale = True
                stale_reason = (
                    "有效简报已变化（brief 指纹不一致）；"
                    f"本版写于 {latest.inputs['brief:snapshot']}，当前为 {memo['__brief_fingerprint__']}"
                )
        if latest is not None and stage == "direction":
            from .active_brief import brief_gate as compute_gate
            # ``bound`` is what makes "the author declared nothing" a blocker:
            # only for a draft written for this brief, never for an old one.
            brief_gate = compute_gate(self.store, run_id, latest.structured,
                                      bound=brief_bound)

        integrity_ok = True
        if latest is not None:
            integrity_ok = self.store.verify_artifact(run_id, stage, latest.version)
        material_gaps = (material_blockers(latest.structured)
                         if stage == 'material_plan' and latest and not stale and integrity_ok else [])

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
            and not material_gaps
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
            if material_gaps:
                blocked = "关键素材缺口尚未解决：" + "；".join(material_gaps[:3])

        can_submit = open_attempt is not None
        if open_attempt is not None and not upstream_ready:
            can_submit = False
            blocked = "; ".join(reasons)

        # Historical reviews remain readable but cannot start a new review or
        # affect a current user decision.
        can_review = False
        can_approve = (
            spec.gated
            and latest is not None
            and not stale
            and integrity_ok
            and latest.digest_algorithm == "artifact-v2"
            and not approved
            and approval is None
        )

        if not spec.gated:
            status = "empty"
            if latest is not None:
                status = "stale" if stale else "needs_resolution" if material_gaps else "done"
        else:
            if latest is None:
                status = "empty"
            elif stale:
                status = "stale"
            elif approved:
                status = "approved"
            elif approval is not None and approval.decision == "returned":
                status = "returned"
            else:
                status = "awaiting_decision"

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
            brief_gate=brief_gate,
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
        if self.store.read_run(run_id).kind == 'learning':
            raise PipelineError('run_kind', 'Independent learning runs cannot start production stages')
        spec = STAGE_SPEC[stage]
        memo: dict = {}
        inputs: dict[str, str] = {}
        from .evidence import evidence_fingerprint, evidence_generation
        inputs["evidence:snapshot"] = evidence_fingerprint(self.store, run_id)
        inputs["evidence:generation"] = str(evidence_generation(self.store, run_id))
        # Brief is compiled *from* intake, so only later stages bind to it.
        if stage != "intake":
            from .active_brief import compile_active_brief, fingerprint as brief_fp
            inputs["brief:snapshot"] = brief_fp(compile_active_brief(self.store, run_id))
        if stage != 'intake':
            from .references import reference_fingerprint, reference_generation
            selected_references = reference_fingerprint(self.store, run_id)
            if selected_references is not None:
                inputs['references:snapshot'] = selected_references
                inputs['references:generation'] = str(reference_generation(self.store, run_id))
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
        if not content.strip():
            if shape.dropped:
                raise PipelineError("structure", "结构化内容存在无法保存的字段，请修正后提交；原尝试仍保留。")
            try:
                validate_canonical_input(stage, structured or {})
                content = canonical_document(stage, shape.data)
            except ValueError as exc:
                raise PipelineError("structure", str(exc)) from exc
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
        if state.approval is not None:
            raise PipelineError("review", f"{stage}-v{state.latest.version} already has a user decision")
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

        # Clause binding (P3): any clause_ids the reviewer cites must exist on
        # the ActiveBrief. Direction returns against a blocked gate also get
        # stamped with the gate's clause ids when the model omitted them, so
        # the return stays machine-actionable.
        from .active_brief import clause_index as brief_clause_index, compile_active_brief as compile_brief
        brief = compile_brief(self.store, run_id)
        known = set(brief_clause_index(brief))
        findings = [dict(f) if isinstance(f, dict) else {"issue": str(f)} for f in findings]
        for f in findings:
            cids = f.get("clause_ids")
            if cids is None:
                continue
            if not isinstance(cids, list):
                raise PipelineError("review", "clause_ids must be a list of ActiveBrief clause ids")
            for cid in cids:
                if str(cid) not in known:
                    raise PipelineError(
                        "review",
                        f"unknown ActiveBrief clause_id {cid!r}; use an id from the brief checklist",
                    )
            f["clause_ids"] = [str(c) for c in cids]
        if stage == "direction" and verdict == "return" and state.brief_gate:
            gate_ids = {
                str(b.get("clause_id"))
                for b in (state.brief_gate.get("blockers") or [])
                if b.get("clause_id")
            }
            if gate_ids and not any(f.get("clause_ids") for f in findings) and findings:
                findings[0]["clause_ids"] = sorted(gate_ids)

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
        # One decision per version (approved, override, or returned).
        # A second nonce would let a new click overwrite the recorded decision
        # or `selected_option_id`. After return, produce a new version.
        if state.approval is not None:
            raise PipelineError(
                "approval",
                f"{stage}-v{state.latest.version} already has decision "
                f"{state.approval.decision!r}; a new link cannot replace it",
            )
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
        continue_after: bool = False,
    ) -> Approval:
        """Record a user decision. Only the server calls this, only after a click.

        Historical independent reviews are read-only evidence. A current,
        intact version needs Jeffery's decision, not a review verdict. Returning
        a version still requires a concrete note; approval does not.
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
                    and prior.continue_after == continue_after
                    and (expected_artifact_sha256 is None or prior.artifact_sha256 == expected_artifact_sha256)
                    and (expected_version is None or prior.artifact_version == expected_version)):
                return prior
            raise PipelineError("approval", "this nonce was already used for a different decision")

        if decision == "approved" and note.strip():
            raise PipelineError(
                "approval",
                "approving the current version cannot include requested changes; "
                "return it with those changes, review the new version, then approve",
            )

        state = self.stage_state(run_id, stage)
        if state.latest is None:
            raise PipelineError("approval", f"{stage} has nothing to approve")
        self._expect_version(state, expected_artifact_sha256, expected_version)
        # A different nonce cannot re-decide an already-decided version
        # (approved or returned); same-nonce idempotent retries returned above.
        if state.approval is not None:
            raise PipelineError(
                "approval",
                f"{stage}-v{state.latest.version} already has decision "
                f"{state.approval.decision!r}; this version cannot be decided again",
            )
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
            # Brief gate (P2): a selected option that still violates / needs an
            # amendment / declares nothing / cites an unknown clause cannot be
            # adopted — not even with override_reason. The product goal only
            # moves through amend_brief, never through a gate click.
            # Return-with-note remains available.
            if selected_option_id and state.brief_gate:
                from .active_brief import gate_blockers_for
                sel_blockers = gate_blockers_for(state.brief_gate, selected_option_id)
                if sel_blockers:
                    kinds = "、".join(sorted({str(b.get("kind") or "") for b in sel_blockers}))
                    raise PipelineError(
                        "brief",
                        f"方向 {str(selected_option_id).upper()} 未满足当前有效简报（{kinds}）。"
                        "请先批准简报修订并让团队按新简报重做，或退回按原简报重做；"
                        "不能用 override 直接采用。",
                    )
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
            continue_after=continue_after,
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
