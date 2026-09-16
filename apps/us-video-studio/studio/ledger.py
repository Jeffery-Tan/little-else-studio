"""Cost ledger.

The one thing this module refuses to do is pretend a dollar figure is a fact.

* ``input_tokens`` / ``output_tokens`` come from the API response. They are facts.
* ``estimated_usd`` is computed from a ``Pricing`` record and is always stored
  next to ``pricing_source``, so any reader can see which rate card produced it.

The spend cap is checked *before* a call is made. That is the only moment the
cap can actually prevent spending; after a call returns the money is already
gone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .models import BudgetExceeded, LedgerEntry, utcnow
from .store import Store


@dataclass(frozen=True)
class Totals:
    calls: int
    input_tokens: int
    output_tokens: int
    estimated_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": round(self.estimated_usd, 6),
        }


class Ledger:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    # ---------------------------------------------------------------- write --

    def record(
        self,
        run_id: str,
        *,
        role: str,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str = "",
        request_id: str = "",
        ok: bool = True,
        error: str = "",
    ) -> LedgerEntry:
        pricing = self.settings.pricing
        entry = LedgerEntry(
            at=utcnow(),
            run_id=run_id,
            role=role,
            stage=stage,
            model=model,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            estimated_usd=pricing.estimate_usd(int(input_tokens), int(output_tokens)),
            pricing_source=pricing.source,
            stop_reason=stop_reason,
            request_id=request_id,
            ok=ok,
            error=error,
        )
        self.store.append_ledger(run_id, entry)
        return entry

    # ----------------------------------------------------------------- read --

    def totals(self, run_id: str | None = None) -> Totals:
        """Totals for one run, or across every run when ``run_id`` is None."""
        if run_id is not None:
            rows = self.store.read_ledger(run_id)
        else:
            rows = [
                row
                for run in self.store.list_runs()
                for row in self.store.read_ledger(run.run_id)
            ]
        return Totals(
            calls=len(rows),
            input_tokens=sum(r.input_tokens for r in rows),
            output_tokens=sum(r.output_tokens for r in rows),
            estimated_usd=sum(r.estimated_usd for r in rows),
        )

    def spent_usd(self, run_id: str | None = None) -> float:
        return self.totals(run_id).estimated_usd

    def remaining_usd(self, run_id: str | None = None) -> float:
        return max(0.0, self.settings.budget_limit_usd - self.spent_usd(run_id))

    def remaining_tokens(self) -> int:
        totals = self.totals()
        return max(0, self.settings.budget_limit_tokens - totals.input_tokens - totals.output_tokens)

    def last_stage_cost(self, role: str, stage: str) -> dict[str, Any] | None:
        """What this role's work on this stage cost the last time it ran.

        Across every run, not just this one: a run that has not reached a stage
        yet is exactly the case where the figure is wanted, and the only runs
        that have the figure are older ones.

        Returns ``None`` when it has never run, rather than zero or an estimate.
        The number goes on a button he is about to press, and an invented one
        there is worse than a missing one — "还没做过这一步" is a true sentence,
        "预计 0 token" is not.

        A "group" is one run's calls as that role on that stage, which is one
        dispatch in practice. A stage redone within the same run merges into one
        group, so the figure is roughly right rather than exact, and it is
        presented as a past measurement rather than a prediction.
        """
        groups: dict[str, list[LedgerEntry]] = {}
        for run in self.store.list_runs():
            for row in self.store.read_ledger(run.run_id):
                if row.role == role and row.stage == stage:
                    groups.setdefault(run.run_id, []).append(row)
        if not groups:
            return None

        # Newest group by its last call, not by dict order — the run list carries
        # no promise about ordering.
        run_id, rows = max(groups.items(), key=lambda kv: max(r.at for r in kv[1]))
        rows.sort(key=lambda r: r.at)
        totals = Totals(
            calls=len(rows),
            input_tokens=sum(r.input_tokens for r in rows),
            output_tokens=sum(r.output_tokens for r in rows),
            estimated_usd=sum(r.estimated_usd for r in rows),
        )
        return {
            "role": role,
            "stage": stage,
            "run_id": run_id,
            "at": rows[-1].at,
            "calls": totals.calls,
            "tokens": totals.input_tokens + totals.output_tokens,
            "estimated_usd": round(totals.estimated_usd, 6),
            "usd_is_real": self.settings.usd_cap_is_enforceable,
        }

    def price_note(self) -> dict[str, Any]:
        """What to show in the UI so the dollar number is never read as a bill."""
        pricing = self.settings.pricing
        enforceable = self.settings.usd_cap_is_enforceable
        note = "USD figures are estimates from this rate card, not the provider's bill."
        if not enforceable:
            note += (
                " This rate card has zero rates, so the USD cap cannot stop anything yet; "
                "the token cap is what is actually enforced."
            )
        return {
            "source": pricing.source,
            "as_of": pricing.as_of,
            "input_usd_per_mtok": pricing.input_usd_per_mtok,
            "output_usd_per_mtok": pricing.output_usd_per_mtok,
            "usd_cap_enforceable": enforceable,
            "note": note,
        }

    # --------------------------------------------------------------- budget --

    def ensure_within_budget(self) -> None:
        """Raise before a call if a development cap has been reached.

        Both caps are **totals across every run**, not per-run allowances: $10 was
        set as a development budget, and it would mean nothing if a new run reset
        it.

        The token cap is the one that actually bites. It is checked always; the
        USD cap is checked only when the rate card has non-zero rates, because a
        zero rate card would make it a limit that can never be reached — a fake
        brake is worse than no brake, since it looks like protection.

        Counting the in-flight call is impossible — tokens are unknown until the
        response arrives — so the rule is: once a total is at or above its cap,
        no new call starts. The last call before the cap may overshoot it.
        """
        totals = self.totals()
        tokens = totals.input_tokens + totals.output_tokens
        token_limit = self.settings.budget_limit_tokens
        if tokens >= token_limit:
            raise BudgetExceeded(
                f"{tokens} tokens have been used across all runs, at or above the cap of "
                f"{token_limit}; refusing to start another model call. Raise "
                f"budget_limit_tokens in config.json to continue."
            )

        if not self.settings.usd_cap_is_enforceable:
            return

        spent = totals.estimated_usd
        limit = self.settings.budget_limit_usd
        if spent >= limit:
            raise BudgetExceeded(
                f"estimated spend across all runs is {spent:.4f} USD, at or above the "
                f"cap of {limit:.2f} USD; refusing to start another model call. "
                f"Raise budget_limit_usd in config.json to continue."
            )
