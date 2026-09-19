"""Local accounting recovery; never contacts a provider or retries a model.

Usage (from the app directory)::

    python -m studio.recovery --data-root /approved/data pending
    python -m studio.recovery --data-root /approved/data reconcile RUN CALL_ID \
        --input-tokens 123 --output-tokens 45 --evidence-note "provider export reference"

Do not guess counts from partial output. Reconcile only after checking provider
usage or written confirmation that the request was not accepted. This command
never approves creative content. Offline abandon closes only the named open
attempt after stopping the Studio server; it does not erase costs or rerun work.
"""
from __future__ import annotations

import argparse
import hashlib
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import load_settings
from .ledger import Ledger
from .jobs import WriterLease
from .models import PipelineError, STAGES
from .pipeline import Pipeline
from .store import Store


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReconcileAction(RecoveryAction):
    call_id: str = Field(min_length=1, max_length=200)
    input_tokens: int = Field(ge=0, le=9007199254740991, strict=True)
    output_tokens: int = Field(ge=0, le=9007199254740991, strict=True)
    evidence_note: str = Field(min_length=1, max_length=4000)


class AbandonAction(RecoveryAction):
    stage: str = Field(min_length=1, max_length=40)
    attempt_token: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


def recovery_snapshot(jobs, run_id):
    """Current root-wide accounting blockers and this project's open work.

    The revision binds a human action to exactly the state they saw. Other
    projects' pending calls are shown for navigation, never settled here.
    """
    with jobs.lock, jobs.store.run_lock(run_id):
        calls = [row.to_dict() for row in jobs.ledger.entries()
                 if row.usage_status != "known" or row.call_status == "pending"]
        attempts = [state.open_attempt.to_dict() for stage in STAGES
                    if (state := jobs.pipeline.stage_state(run_id, stage)).open_attempt]
        saved = [{"stage": item.stage, "version": item.version, "summary": item.summary}
                 for item in jobs.store.list_artifacts(run_id)]
        basis = {"run_id": run_id, "calls": calls, "attempts": attempts,
                 "active": jobs.active, "jobs": jobs.list(run_id)}
        revision = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"run_id": run_id, "revision": revision, "unresolved_calls": calls,
                "open_attempts": attempts, "saved_artifacts": saved,
                "active_run_id": jobs.active[0] if jobs.active else None,
                "can_recover": jobs.active is None,
                "can_continue": jobs.active is None and not calls and not attempts}


def install_recovery_routes(app, jobs, require_run):
    """Explicit human-only HTTP actions; never part of the Agent tool registry."""
    def validate_action(run_id, revision):
        jobs.initialize()
        if jobs.active is not None:
            raise PipelineError("job_busy", "制作团队仍在执行。请等待本轮结束，再处理恢复事项。")
        snapshot = recovery_snapshot(jobs, run_id)
        if snapshot["revision"] != revision:
            raise PipelineError("recovery_changed", "恢复状态已经变化，请刷新后重新核对。")
        return snapshot

    @app.get("/api/runs/{run_id}/recovery")
    def read_recovery(run_id: str):
        require_run(run_id)
        return recovery_snapshot(jobs, run_id)

    @app.post("/api/runs/{run_id}/recovery/reconcile")
    def reconcile_recovery(run_id: str, payload: ReconcileAction):
        require_run(run_id)
        try:
            with jobs.lock:
                snapshot = validate_action(run_id, payload.expected_revision)
                if not any(row["run_id"] == run_id and row["call_id"] == payload.call_id
                           for row in snapshot["unresolved_calls"]):
                    raise ValueError("这次调用不属于当前项目的待核实记录。")
                jobs.ledger.reconcile(run_id, payload.call_id, input_tokens=payload.input_tokens,
                                      output_tokens=payload.output_tokens, evidence_note=payload.evidence_note)
                return recovery_snapshot(jobs, run_id)
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/recovery/abandon")
    def abandon_recovery(run_id: str, payload: AbandonAction):
        require_run(run_id)
        try:
            with jobs.lock:
                snapshot = validate_action(run_id, payload.expected_revision)
                if not any(row["stage"] == payload.stage and row["token"] == payload.attempt_token
                           for row in snapshot["open_attempts"]):
                    raise ValueError("此制作步骤已变化，请刷新后重新核对。")
                reason = payload.reason.strip()
                if not reason:
                    raise ValueError("请填写关闭原因，方便之后核对恢复记录。")
                with jobs.ledger.call_guard():
                    jobs.pipeline.reject(run_id, payload.stage, payload.attempt_token,
                                         "站内恢复：" + reason)
                return recovery_snapshot(jobs, run_id)
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc


def nonnegative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative whole number")
    return parsed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, help="Explicit approved Studio data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending", help="List calls awaiting usage reconciliation; read-only")
    reconcile = commands.add_parser("reconcile", help="Append provider-confirmed usage, never rerun")
    reconcile.add_argument("run_id")
    reconcile.add_argument("call_id")
    reconcile.add_argument("--input-tokens", required=True, type=nonnegative)
    reconcile.add_argument("--output-tokens", required=True, type=nonnegative)
    reconcile.add_argument("--evidence-note", required=True)
    attempts = commands.add_parser("attempts", help="List open stage attempts; read-only")
    attempts.add_argument("run_id")
    abandon = commands.add_parser("abandon", help="Close a crashed attempt without rerunning or clearing costs")
    abandon.add_argument("run_id")
    abandon.add_argument("stage", choices=STAGES)
    abandon.add_argument("--attempt-token", required=True)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--server-stopped", required=True, action="store_true",
                         help="Confirm the Studio writer process has been stopped before offline recovery")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.data_root)
        ledger = Ledger(Store(settings.data_root), settings)
        if args.command == "pending":
            rows = [entry.to_dict() for entry in ledger.entries() if entry.usage_status != "known"]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif args.command == "attempts":
            state = Pipeline(ledger.store).state(args.run_id).to_dict()
            print(json.dumps([{ "stage": row["stage"], "attempt": row["open_attempt"]}
                              for row in state["stages"] if row.get("open_attempt")],
                             ensure_ascii=False, indent=2))
        elif args.command == "abandon":
            if not args.reason.strip():
                raise ValueError("a specific recovery reason is required")
            with WriterLease(settings.data_root), ledger.call_guard():
                Pipeline(ledger.store).reject(args.run_id, args.stage, args.attempt_token,
                                              "Offline recovery: " + args.reason.strip())
            print(json.dumps({"run_id": args.run_id, "stage": args.stage,
                              "status": "abandoned", "rerun": False}))
        else:
            with WriterLease(settings.data_root):
                row = ledger.reconcile(args.run_id, args.call_id, input_tokens=args.input_tokens,
                                       output_tokens=args.output_tokens, evidence_note=args.evidence_note)
            print(json.dumps(row.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, PipelineError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
