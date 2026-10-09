"""The only path from an agent to the model endpoint.

Every attempt records a durable intent and logical request before it starts,
then a settlement. Failure keeps received usage; missing usage remains unknown
without requiring manual reconciliation or blocking future calls.

Wrapping the client rather than hooking the loop means a new agent, a new role
or a new tool cannot accidentally spend money off the books: it would have to
deliberately bypass this class to do it.
"""
from __future__ import annotations

import json
from typing import Any

from ..ledger import Ledger
from .client import (JobCancelled, ModelClient, ModelError, SafePreflightError, Turn, Usage,
                     prepare_strict_writer_execution)
from .requests import (encode, save_request, record_response_rejection,
                       record_response_normalization, record_model_transport, save_writer_root_closure)


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
        self.public_progress = None

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Turn:
        with self.ledger.call_guard(concurrent=True,
                                    check_cancel=getattr(self.inner, 'check_cancel', None)):
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
        # Detach exactly the logical input supplied to the provider adapter.
        # Snapshot serialization happens before intent; failure cannot spend.
        try:
            request = json.loads(encode(dict(model=model, system=system, messages=messages, tools=tools)))
        except (TypeError, ValueError):
            raise SafePreflightError('模型输入无法完整记录，尚未发起请求。') from None
        call_id = self.ledger.begin_call(self.run_id, role=self.role, stage=self.stage, model=model)
        try:
            save_request(self.ledger.store, self.run_id, call_id, role=self.role, stage=self.stage,
                         request=request, route={
                             'connection': self.ledger.settings.connection_by_role.get(self.role),
                             'provider': self.ledger.settings.provider_by_role.get(self.role),
                         })
        except Exception:
            self.ledger.settle_call(self.run_id, call_id, input_tokens=0, output_tokens=0,
                                   usage_status='known', ok=False, error='request snapshot failed before provider invocation')
            raise SafePreflightError('本次模型输入记录失败，没有调用模型；请检查运行目录。') from None
        try:
            if cancel:
                cancel()
        except Exception:
            self.ledger.settle_call(self.run_id, call_id, input_tokens=0, output_tokens=0,
                                   usage_status='known', ok=False, error='cancelled before provider invocation')
            raise
        turn = None
        try:
            progress_call = getattr(target, 'complete_with_progress', None)
            turn = (progress_call(**request, on_progress=self.public_progress)
                    if callable(progress_call) else target.complete(**request))
            prepare_strict_writer_execution(turn, request['tools'])
        except Exception as exc:
            if isinstance(exc, ModelError):
                usage, usage_status = exc.usage, exc.usage_status
            elif isinstance(exc, JobCancelled):
                usage = exc.usage or Usage(None, None)
                observed = (usage.input_tokens, usage.output_tokens)
                usage_status = ('known' if exc.usage_final and all(
                    type(value) is int and value >= 0 for value in observed) else
                    'partial' if any(value is not None for value in observed) else 'unknown')
            else:
                usage, usage_status = Usage(None, None), 'unknown'
            self.ledger.settle_call(
                self.run_id, call_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                usage_status=usage_status,
                ok=False,
                error=str(exc)[:300] if isinstance(exc, ModelError) else f"{type(exc).__name__}: call failed; usage unconfirmed",
            )
            if isinstance(exc, ModelError) and isinstance(exc.response_diagnostic, dict):
                try:
                    record_response_rejection(self.ledger.store, self.run_id, call_id,
                                              role=self.role, stage=self.stage, diagnostic=exc.response_diagnostic,
                                              tools=request['tools'])
                except (OSError, ValueError):
                    # Accounting is already durable. Preserve the actual model
                    # failure, but make a diagnostic-write failure inspectable.
                    exc.add_note('Response diagnostic could not be saved; call settlement was retained.')
            diagnostic = getattr(exc, 'transport_diagnostic', None)
            if diagnostic is None and turn is not None:
                diagnostic = turn.transport_diagnostic
            if isinstance(diagnostic, dict):
                try:
                    record_model_transport(self.ledger.store, self.run_id, call_id,
                                           role=self.role, stage=self.stage, diagnostic=diagnostic,
                                           tools=request['tools'])
                except (OSError, ValueError):
                    exc.add_note('Transport diagnostic could not be saved; call settlement was retained.')
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
        # A cancellation arriving with a complete response retains real usage,
        # but must not persist/execute this exceptional format-processing path.
        if turn.writer_root_closure is not None and cancel:
            cancel()
        try:
            if isinstance(turn.transport_diagnostic, dict):
                record_model_transport(self.ledger.store, self.run_id, call_id,
                                       role=self.role, stage=self.stage, diagnostic=turn.transport_diagnostic,
                                       tools=request['tools'])
        except (OSError, ValueError):
            raise SafePreflightError('模型诊断记录未能保存，本轮未执行返回工具；已收到的模型用量保留。') from None
        try:
            for diagnostic in turn.response_normalizations:
                if diagnostic.get('code') == 'root_object_closed':
                    save_writer_root_closure(self.ledger.store, self.run_id, call_id,
                                            role=self.role, stage=self.stage, closure=turn.writer_root_closure)
                else:
                    record_response_normalization(self.ledger.store, self.run_id, call_id,
                                                  role=self.role, stage=self.stage, diagnostic=diagnostic)
        except (OSError, ValueError):
            raise SafePreflightError('提交格式处理记录未能保存，本轮未执行返回工具；已收到的模型用量保留。') from None
        if turn.response_normalizations and self.public_progress:
            self.public_progress({'type': 'agent_submission_normalized'})
        return turn
