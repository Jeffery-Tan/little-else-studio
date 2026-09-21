"""Immutable logical model inputs in the existing run, not a second task store."""
from __future__ import annotations

import hashlib
import json
import os
import re


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def save_request(store, run_id, call_id, *, role, stage, request, route):
    # Only model-visible arguments and explicitly selected public route fields
    # cross this seam. Never serialize clients, settings or connection objects.
    if not re.fullmatch(r"[a-f0-9-]{36}", call_id):
        raise ValueError("invalid model call identity")
    record = {"schema_version": 1, "run_id": run_id, "call_id": call_id,
              "role": role, "stage": stage, "request": request, "route": route}
    raw = encode(record).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    with store.run_lock(run_id):
        root = store.run_dir(run_id).resolve()
        path = root / "requests" / (call_id + ".json")
        if not path.resolve().is_relative_to(root):
            raise ValueError("request directory is outside the run")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        store.append_event(run_id, "agent_request_snapshot", call_id=call_id,
                           role=role, stage=stage, sha256=digest,
                           evidence_ref=f"requests/{call_id}.json")
    return digest


def read_request(store, run_id, call_id):
    """Local diagnostic reader; not exposed to any Agent or arbitrary HTTP path."""
    if not isinstance(call_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", call_id):
        raise ValueError("invalid model call identity")
    event = next((e for e in store.read_events(run_id)
                  if e.get("kind") == "agent_request_snapshot" and e.get("call_id") == call_id), None)
    if event is None:
        raise ValueError("this call has no recorded request snapshot")
    root = store.run_dir(run_id).resolve()
    path = root / "requests" / (call_id + ".json")
    if not path.resolve().is_relative_to(root):
        raise ValueError("request directory is outside the run")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != event["sha256"]:
        raise ValueError("request snapshot fingerprint mismatch")
    record = json.loads(raw)
    if record.get("run_id") != run_id or record.get("call_id") != call_id:
        raise ValueError("request snapshot identity mismatch")
    return record
