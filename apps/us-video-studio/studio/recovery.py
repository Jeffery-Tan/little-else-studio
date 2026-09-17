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
import json

from .config import load_settings
from .ledger import Ledger
from .models import PipelineError, STAGES
from .pipeline import Pipeline
from .store import Store


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
            with ledger.call_guard():
                Pipeline(ledger.store).reject(args.run_id, args.stage, args.attempt_token,
                                              "Offline recovery: " + args.reason.strip())
            print(json.dumps({"run_id": args.run_id, "stage": args.stage,
                              "status": "abandoned", "rerun": False}))
        else:
            row = ledger.reconcile(args.run_id, args.call_id, input_tokens=args.input_tokens,
                                   output_tokens=args.output_tokens, evidence_note=args.evidence_note)
            print(json.dumps(row.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, PipelineError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
