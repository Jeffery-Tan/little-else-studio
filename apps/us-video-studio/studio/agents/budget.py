"""The only path from an agent to the model endpoint.

Every attempt records a durable intent before it starts and a settlement after
it returns. Failure keeps received usage; missing usage remains unknown and
blocks further calls until the operator reconciles provider evidence.

Wrapping the client rather than hooking the loop means a new agent, a new role
or a new tool cannot accidentally spend money off the books: it would have to
deliberately bypass this class to do it.
"""
from __future__ import annotations

from typing import Any

from ..ledger import Ledger
from .client import ModelClient, ModelError, Turn, Usage


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
        with self.ledger.call_guard():
            return self._complete_guarded(model=model, system=system, messages=messages, tools=tools)

    def _complete_guarded(self, *, model: str, system: str,
                          messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Turn:
        cancel = getattr(self.inner, 'check_cancel', None)
        if cancel:
            cancel()
        if hasattr(self.inner, 'validate_model'):
            self.inner.validate_model(self.role, model)
        target = self.inner.for_role(self.role, self.run_id) if hasattr(self.inner, 'for_role') else self.inner
        preflight = getattr(target, 'preflight', None)
        if preflight:
            preflight()
        if cancel:
            cancel()
        call_id = self.ledger.begin_call(self.run_id, role=self.role, stage=self.stage, model=model)
        try:
            turn = target.complete(
                model=model, system=system, messages=messages, tools=tools
            )
        except Exception as exc:
            usage = exc.usage if isinstance(exc, ModelError) else Usage(None, None)
            self.ledger.settle_call(
                self.run_id, call_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                usage_status=exc.usage_status if isinstance(exc, ModelError) else "unknown",
                ok=False,
                error=str(exc)[:300] if isinstance(exc, ModelError) else f"{type(exc).__name__}: call failed; usage unconfirmed",
            )
            raise
        self.ledger.settle_call(
            self.run_id, call_id,
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            stop_reason=turn.stop_reason,
            actual_model=turn.model,
            usage_status="known" if turn.usage.input_tokens is not None and turn.usage.output_tokens is not None else "unknown",
            ok=True,
        )
        return turn
