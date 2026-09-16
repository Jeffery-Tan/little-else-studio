"""The only path from an agent to the model endpoint.

Every call is checked against the cap before it starts and written to the ledger
after it returns — including the failed ones, because a call that errored after
the provider started generating was still a call.

Wrapping the client rather than hooking the loop means a new agent, a new role
or a new tool cannot accidentally spend money off the books: it would have to
deliberately bypass this class to do it.
"""
from __future__ import annotations

from typing import Any

from ..ledger import Ledger
from .client import ModelClient, Turn


class BudgetedClient:
    def __init__(
        self,
        inner: ModelClient,
        ledger: Ledger,
        *,
        run_id: str,
        role: str,
        stage: str,
    ):
        self.inner = inner
        self.ledger = ledger
        self.run_id = run_id
        self.role = role
        self.stage = stage

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Turn:
        self.ledger.ensure_within_budget()
        try:
            turn = self.inner.complete(
                model=model, system=system, messages=messages, tools=tools
            )
        except Exception as exc:
            self.ledger.record(
                self.run_id,
                role=self.role,
                stage=self.stage,
                model=model,
                input_tokens=0,
                output_tokens=0,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
            raise
        self.ledger.record(
            self.run_id,
            role=self.role,
            stage=self.stage,
            model=model,
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            stop_reason=turn.stop_reason,
        )
        return turn
