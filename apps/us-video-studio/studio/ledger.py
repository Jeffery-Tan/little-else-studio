"""Append-only per-attempt intent and settlement journal.

Optional historical limits stop new calls after a recorded threshold; they are
soft limits, not a reservation or a guaranteed maximum bill. Usage remains
auditable when those limits are disabled.
"""
from __future__ import annotations

import os
import math
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from .config import Pricing, Settings
from .models import BudgetExceeded, LedgerEntry, utcnow
from .store import Store

_JOURNAL_LOCK = threading.RLock()
_CALL_CONDITION = threading.Condition(threading.RLock())
_CALL_LEASES: dict[str, dict[str, Any]] = {}
MAX_ACTIVE_MODEL_CALLS = 4


@dataclass(frozen=True)
class Totals:
    calls: int
    input_tokens: int
    output_tokens: int
    estimated_usd: float | None
    known_estimated_usd: float = 0.0
    unknown_calls: int = 0
    unknown_cost_calls: int = 0
    pending_calls: int = 0
    incomplete_projects: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": round(self.estimated_usd, 6) if self.estimated_usd is not None else None,
            "known_estimated_usd": round(self.known_estimated_usd, 6),
            "unknown_calls": self.unknown_calls, "unknown_cost_calls": self.unknown_cost_calls,
            "pending_calls": self.pending_calls,
            "summary_complete": not self.incomplete_projects,
            "incomplete_projects": list(self.incomplete_projects),
        }


def _totals(rows: list[LedgerEntry]) -> Totals:
    unknown = sum(r.usage_status != "known" or r.call_status == "pending" for r in rows)
    unknown_cost = sum(r.estimated_usd is None for r in rows)
    known_cost = sum(r.estimated_usd or 0.0 for r in rows)
    return Totals(len(rows), sum(r.input_tokens or 0 for r in rows),
                  sum(r.output_tokens or 0 for r in rows),
                  None if unknown_cost else known_cost, known_cost, unknown, unknown_cost,
                  sum(r.call_status == "pending" for r in rows))


class Ledger:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    @contextmanager
    def call_guard(self, *, concurrent=False, check_cancel=None):
        """Bounded calls share one process lease; reconciliation is exclusive.

        The OS lease is held until every local call has settled. A second
        process and reconciliation therefore cannot overwrite a live intent.
        Waiting for a slot does not create an intent or spend anything.
        """
        root = self.settings.data_root.resolve()
        key = os.path.normcase(str(root))
        with _CALL_CONDITION:
            while True:
                if check_cancel:
                    check_cancel()
                lease = _CALL_LEASES.get(key)
                if lease and (not concurrent or lease['exclusive']):
                    raise BudgetExceeded('a model call is still active; wait before reconciliation')
                if lease and lease['count'] >= MAX_ACTIVE_MODEL_CALLS:
                    _CALL_CONDITION.wait(.05)
                    continue
                if lease is None:
                    root.mkdir(parents=True, exist_ok=True)
                    handle = open(root / '.model-call.lock', 'a+b')
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b'0')
                        handle.flush()
                    handle.seek(0)
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        handle.close()
                        raise BudgetExceeded('another process has an active model call; await it before spending or reconciliation') from exc
                    lease = {'handle': handle, 'count': 0, 'exclusive': not concurrent}
                    _CALL_LEASES[key] = lease
                lease['count'] += 1
                break
        try:
            yield
        finally:
            with _CALL_CONDITION:
                lease['count'] -= 1
                if lease['count'] == 0:
                    handle = lease['handle']
                    try:
                        handle.seek(0)
                        if os.name == 'nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
                        _CALL_LEASES.pop(key, None)
                _CALL_CONDITION.notify_all()

    @contextmanager
    def _locked(self):
        # OS lock is released on process death. The durable pending journal row
        # survives, so restarting cannot silently repeat an interrupted call.
        with _JOURNAL_LOCK:
            root = self.settings.data_root
            root.mkdir(parents=True, exist_ok=True)
            with open(root / ".ledger.lock", "a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append(self, run_id: str, entry: LedgerEntry):
        self.store.append_ledger(run_id, entry)
        with open(self.store.run_dir(run_id) / "ledger.jsonl", "ab") as handle:
            os.fsync(handle.fileno())

    def entries(self, run_id: str | None = None) -> list[LedgerEntry]:
        """Effective calls, collapsing journal revisions without counting twice.

        Legacy failed calls have no trustworthy zero-token measurement. Their
        stable synthetic IDs let an operator reconcile them without rewriting.
        """
        if run_id is None:
            # A damaged event/chat/metadata file does not erase readable costs.
            # Unreadable ledgers are explicitly marked by totals(), never zero.
            return [r for item in self.store.project_catalog() if item['ledger_complete']
                    for r in self.entries(item['run_id'])]
        return self.project_entries(self.store, run_id)

    @staticmethod
    def project_entries(store, run_id: str) -> list[LedgerEntry]:
        """Read the same effective journal without a service or new settings."""
        effective: dict[str, LedgerEntry] = {}
        for index, row in enumerate(store.read_ledger(run_id)):
            key = row.call_id or f"legacy-{index + 1}"
            usage_status = row.usage_status
            if usage_status == "legacy":
                usage_status = "known" if row.ok and row.input_tokens is not None and row.output_tokens is not None else "unknown"
            cost = row.estimated_usd
            if usage_status != "known" or "placeholder" in row.pricing_source.lower() or (
                row.usage_status == "legacy" and cost == 0 and (row.input_tokens or row.output_tokens)
            ):
                cost = None
            effective[key] = replace(row, call_id=key, usage_status=usage_status, estimated_usd=cost)
        return list(effective.values())

    def record(self, run_id: str, *, role: str, stage: str, model: str,
               input_tokens: int | None, output_tokens: int | None,
               stop_reason: str = "", request_id: str = "", ok: bool = True,
               error: str = "", usage_status: str | None = None,
               call_id: str = "", call_status: str = "settled",
               resolution_note: str = "", pricing: Pricing | None = None,
               provider: str | None = None, connection_id: str | None = None,
               actual_model: str = "") -> LedgerEntry:
        for value in (input_tokens, output_tokens):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("token counts must be non-negative whole numbers or null")
        status = usage_status or ("known" if ok and input_tokens is not None and output_tokens is not None else "unknown")
        if status not in {"known", "partial", "unknown"}:
            raise ValueError("invalid usage_status")
        if status == "known" and (input_tokens is None or output_tokens is None):
            raise ValueError("known usage requires both counts")
        pricing = pricing or self.settings.pricing_for(role, model)
        cost = pricing.estimate_usd(input_tokens, output_tokens) if status == "known" else None
        entry = LedgerEntry(at=utcnow(), run_id=run_id, role=role, stage=stage, model=model,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            estimated_usd=cost, pricing_source=pricing.source,
                            estimated_cny=pricing.estimate_cny(input_tokens, output_tokens) if status == 'known' else None,
                            input_cny_per_mtok=pricing.input_cny_per_mtok,
                            output_cny_per_mtok=pricing.output_cny_per_mtok,
                            provider=provider if provider is not None else self.settings.provider_by_role.get(role, 'legacy'),
                            connection_id=connection_id if connection_id is not None else self.settings.connection_by_role.get(role, ''),
                            pricing_as_of=pricing.as_of, input_usd_per_mtok=pricing.input_usd_per_mtok,
                            output_usd_per_mtok=pricing.output_usd_per_mtok,
                            actual_model=actual_model,
                            stop_reason=stop_reason, request_id=request_id, ok=ok, error=error,
                            usage_status=status, call_id=call_id, call_status=call_status,
                            resolution_note=resolution_note)
        self._append(run_id, entry)
        return entry

    def begin_call(self, run_id: str, *, role: str, stage: str, model: str) -> str:
        self.store.require_healthy(run_id)
        with self._locked():
            self.store.read_run(run_id)
            self.ensure_within_budget()
            call_id = str(uuid.uuid4())
            self.record(run_id, role=role, stage=stage, model=model, input_tokens=None,
                        output_tokens=None, ok=False, usage_status="unknown",
                        call_id=call_id, call_status="pending")
            return call_id

    def settle_call(self, run_id: str, call_id: str, *, input_tokens: int | None,
                    output_tokens: int | None, usage_status: str, ok: bool,
                    stop_reason: str = "", error: str = "", actual_model: str = "") -> LedgerEntry:
        with self._locked():
            row = next((r for r in self.entries(run_id) if r.call_id == call_id), None)
            if row is None or row.call_status != "pending":
                raise ValueError("call has no unsettled intent")
            return self.record(run_id, role=row.role, stage=row.stage, model=row.model,
                               input_tokens=input_tokens, output_tokens=output_tokens,
                               usage_status=usage_status, call_id=call_id, ok=ok,
                               stop_reason=stop_reason, error=error,
                               pricing=self._row_pricing(row), provider=row.provider, connection_id=row.connection_id,
                               actual_model=actual_model)

    @staticmethod
    def _row_pricing(row):
        return Pricing(row.pricing_source, row.input_usd_per_mtok, row.output_usd_per_mtok, row.pricing_as_of,
                       row.input_cny_per_mtok, row.output_cny_per_mtok)

    def reconcile(self, run_id: str, call_id: str, *, input_tokens: int,
                  output_tokens: int, evidence_note: str) -> LedgerEntry:
        """Explicit local operator action; counts must come from provider evidence.

        This records accounting only. It never retries a request or turns a
        failed role execution into a successful creative artifact.
        """
        if not isinstance(evidence_note, str) or not evidence_note.strip():
            raise ValueError("provider evidence note is required")
        with self.call_guard(), self._locked():
            row = next((r for r in self.entries(run_id) if r.call_id == call_id), None)
            if row is None or row.usage_status == "known":
                raise ValueError("call is not awaiting reconciliation")
            if row.usage_unit != 'tokens':
                raise ValueError('图片或视频用量须在制作记录中核对，不能填写为 Tokens。')
            return self.record(run_id, role=row.role, stage=row.stage, model=row.model,
                               input_tokens=input_tokens, output_tokens=output_tokens,
                               usage_status="known", call_id=call_id, call_status="reconciled",
                               ok=False, stop_reason=row.stop_reason, error=row.error,
                               resolution_note=evidence_note.strip(), pricing=self._row_pricing(row),
                               provider=row.provider, connection_id=row.connection_id, actual_model=row.actual_model)

    def begin_media_call(self, run_id, *, model, provider, usage_unit, call_id=None):
        """Same journal and budget guard; media never invents token measurements."""
        if usage_unit not in {'images', 'video_seconds'}:
            raise ValueError('invalid media unit')
        self.store.require_healthy(run_id)
        with self._locked():
            self.ensure_within_budget()
            self.store.read_run(run_id)
            call_id = call_id or str(uuid.uuid4())
            if any(r.call_id == call_id for r in self.entries(run_id)):
                raise ValueError('media intent already exists')
            row = LedgerEntry(at=utcnow(), run_id=run_id, role='material',
                stage='image_generation' if usage_unit == 'images' else 'video_generation',
                model=model, provider=provider, input_tokens=None, output_tokens=None,
                estimated_usd=None, pricing_source='media: estimate is not the provider bill',
                usage_status='unknown', call_status='pending', call_id=call_id,
                usage_unit=usage_unit, ok=False)
            self._append(run_id, row)
            return call_id

    def settle_media_call(self, run_id, call_id, *, quantity, ok, estimated_cny=None,
                          request_id='', note='', pricing_source='media: price unknown',
                          input_tokens=None, output_tokens=None, actual_model=''):
        for count in (input_tokens, output_tokens):
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError('invalid measured token count')
        for value in (quantity, estimated_cny):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError('media usage and cost must be finite nonnegative values or null')
        with self._locked():
            row = next((r for r in self.entries(run_id) if r.call_id == call_id), None)
            if row is None or row.usage_unit == 'tokens':
                raise ValueError('no media intent')
            if row.usage_status == 'known':
                if row.usage_quantity == quantity and row.ok == ok:
                    return row
                raise ValueError('media call already settled')
            row = replace(row, at=utcnow(), usage_quantity=quantity,
                usage_status='known' if quantity is not None else 'unknown',
                call_status='settled', ok=ok, estimated_cny=estimated_cny if quantity is not None else None,
                request_id=request_id or row.request_id, resolution_note=note, pricing_source=pricing_source,
                input_tokens=input_tokens if input_tokens is not None else row.input_tokens,
                output_tokens=output_tokens if output_tokens is not None else row.output_tokens,
                actual_model=actual_model or row.actual_model)
            self._append(run_id, row)
            return row

    def totals(self, run_id: str | None = None) -> Totals:
        if run_id is None:
            catalog = self.store.project_catalog()
            rows = [row for item in catalog if item['ledger_complete']
                    for row in self.entries(item['run_id'])]
            totals = _totals(rows)
            incomplete = tuple(item['run_id'] for item in catalog
                               if not item['ledger_complete'])
            if incomplete:
                totals = replace(totals, estimated_usd=None, incomplete_projects=incomplete)
            return totals
        return _totals(self.entries(run_id))

    def spent_usd(self, run_id: str | None = None) -> float | None:
        return self.totals(run_id).estimated_usd

    def remaining_usd(self, run_id: str | None = None) -> float | None:
        limit = self.settings.budget_limit_usd
        if limit is None or not self.settings.usd_cap_is_enforceable:
            return None
        spent = self.spent_usd(run_id)
        return max(0.0, limit - spent) if spent is not None else None

    def remaining_tokens(self) -> int | None:
        limit = self.settings.budget_limit_tokens
        if limit is None:
            return None
        totals = self.totals()
        if totals.unknown_calls or totals.incomplete_projects:
            return None
        return max(0, limit - totals.input_tokens - totals.output_tokens)

    def last_stage_cost(self, role: str, stage: str) -> dict[str, Any] | None:
        groups: dict[str, list[LedgerEntry]] = {}
        for row in self.entries():
            if row.role == role and row.stage == stage:
                groups.setdefault(row.run_id, []).append(row)
        if not groups:
            return None
        run_id, rows = max(groups.items(), key=lambda kv: max(r.at for r in kv[1]))
        totals = _totals(rows)
        return {"role": role, "stage": stage, "run_id": run_id, "at": max(r.at for r in rows),
                **totals.to_dict(), "tokens": totals.input_tokens + totals.output_tokens,
                "usd_is_real": False}

    def price_note(self) -> dict[str, Any]:
        pricing = self.settings.pricing
        enforceable = self.settings.usd_cap_is_enforceable
        note = "USD figures are estimates from this rate card, not the provider's bill."
        if self.settings.budget_limit_usd is None and self.settings.budget_limit_tokens is None:
            note += " No spend limits are configured; usage is still recorded."
        elif self.settings.budget_limit_usd is not None and not enforceable:
            note += " Unverified rates mean the USD cap cannot stop anything yet; dollar totals remain unknown."
        if self.settings.budget_limit_usd is not None or self.settings.budget_limit_tokens is not None:
            note += " Configured limits stop new calls after recorded totals reach them; an in-flight call may exceed the limit."
        note += " Unknown usage is retained for diagnostics and does not block new calls."
        routes = {role: {'connection': cid, 'provider': self.settings.provider_by_role[role],
                        'model': self.settings.model_for(role),
                        'source': self.settings.pricing_for(role, self.settings.model_for(role)).source}
                  for role, cid in self.settings.connection_by_role.items()}
        return {"source": 'per-connection/model rate cards; unknown routes remain null' if routes else pricing.source,
                "as_of": '' if routes else pricing.as_of, 'routes': routes,
                "input_usd_per_mtok": pricing.input_usd_per_mtok if pricing.known and not routes else None,
                "output_usd_per_mtok": pricing.output_usd_per_mtok if pricing.known and not routes else None,
                "usd_estimate_available": self.settings.usd_estimate_available,
                "usd_cap_enforceable": enforceable,
                "limit_kind": "soft" if (self.settings.budget_limit_usd is not None or
                                          self.settings.budget_limit_tokens is not None) else "none",
                "note": note}

    def ensure_within_budget(self) -> None:
        token_limit = self.settings.budget_limit_tokens
        usd_limit = self.settings.budget_limit_usd
        if token_limit is None and usd_limit is None:
            return
        totals = self.totals()
        if totals.incomplete_projects:
            raise BudgetExceeded('部分项目的费用台账无法读取，无法核对已配置额度；原件保留，请先修复资料错误。')
        tokens = totals.input_tokens + totals.output_tokens
        if token_limit is not None and tokens >= token_limit:
            raise BudgetExceeded(f"{tokens} tokens reached the soft cap of {token_limit}; refusing a new call.")
        if usd_limit is not None and totals.known_estimated_usd >= usd_limit:
            raise BudgetExceeded(f"estimated known spend reached the soft cap of {usd_limit:.2f} USD; refusing a new call.")
