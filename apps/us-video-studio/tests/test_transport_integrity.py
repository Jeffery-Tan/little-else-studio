"""Transport and accounting regressions; all HTTP is mocked, data is temporary."""
from __future__ import annotations

from contextlib import contextmanager
import json

import httpx
import pytest

from fakes import text_stream, sse, final_turn
from studio.agents.client import AnthropicCompatClient, ModelError, _parse_stream
from studio.agents.budget import BudgetedClient
from studio.config import Settings, Pricing, load_settings
from studio.ledger import Ledger
from studio.models import BudgetExceeded, LedgerEntry, utcnow
from studio.store import Store


@pytest.fixture
def ledger(tmp_path):
    store = Store(tmp_path)
    store.create_run("integrity", "test", "test only")
    return Ledger(store, Settings(data_root=tmp_path, base_url="https://example.invalid",
        pricing=Pricing("test-card", 1.0, 2.0, "2026-09-17")))


def test_eof_does_not_turn_a_partial_answer_into_success():
    lines = text_stream("partial")
    lines = [line for line in lines if 'message_stop' not in line]
    with pytest.raises(ModelError, match="message_stop") as error:
        _parse_stream(lines, "test")
    assert error.value.usage.input_tokens == 10
    assert error.value.usage.output_tokens == 5
    assert error.value.usage_status == "partial"


@pytest.mark.parametrize("remove", ["content_block_stop", "message_delta", "message_start"])
def test_terminal_marker_does_not_hide_missing_protocol_boundaries(remove):
    lines = [line for line in text_stream("x") if remove not in line]
    with pytest.raises(ModelError):
        _parse_stream(lines, "test")


def test_transport_disconnect_keeps_received_usage():
    def lines():
        yield from sse({"type": "message_start", "message": {"usage": {"input_tokens": 123}}})
        raise httpx.ReadError("private request information must not be logged")
    with pytest.raises(ModelError) as error:
        _parse_stream(lines(), "test")
    assert error.value.usage.input_tokens == 123
    assert error.value.usage.output_tokens is None
    assert "private" not in str(error.value)


def test_invalid_usage_cannot_be_treated_as_zero():
    lines = sse({"type": "message_start", "message": {"usage": {"input_tokens": -1}}})
    with pytest.raises(ModelError, match="invalid usage"):
        _parse_stream(lines, "test")


@pytest.mark.parametrize("failure", ["503", "disconnect"])
def test_http_attempt_is_never_automatically_retried(monkeypatch, failure):
    attempts = []
    class Response:
        status_code = 503 if failure == "503" else 200
        def iter_lines(self):
            yield from sse({"type": "message_start", "message": {"usage": {"input_tokens": 37}}})
            raise httpx.ReadError("broken stream")
    class HTTP:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        @contextmanager
        def stream(self, *args, **kwargs):
            attempts.append(1)
            yield Response()
    monkeypatch.setattr(httpx, "Client", HTTP)
    client = AnthropicCompatClient("https://example.invalid", "test-credential")
    with pytest.raises(ModelError) as error:
        client.complete(model="test", system="", messages=[], tools=[])
    assert len(attempts) == 1
    if failure == "disconnect":
        assert error.value.usage.input_tokens == 37


def test_intent_is_durable_before_network_and_collapses_after_success(ledger):
    class Fake:
        def complete(self, **kwargs):
            journal = ledger.store.read_ledger("integrity")
            assert len(journal) == 1
            assert journal[0].call_status == "pending"
            assert ledger.totals().pending_calls == 1
            return final_turn("done")
    client = BudgetedClient(Fake(), ledger, run_id="integrity", role="writer", stage="writing")
    client.complete(model="test", system="", messages=[], tools=[])
    assert len(ledger.store.read_ledger("integrity")) == 2
    assert len(ledger.entries()) == 1
    assert ledger.totals().calls == 1
    assert ledger.totals().input_tokens == 30
    assert ledger.totals().unknown_calls == 0


def test_partial_failure_keeps_usage_without_blocking_explicit_next_call(ledger):
    class Fake:
        def complete(self, **kwargs):
            from studio.agents.client import Usage
            raise ModelError(200, "stream interrupted", usage=Usage(23, None))
    client = BudgetedClient(Fake(), ledger, run_id="integrity", role="writer", stage="writing")
    with pytest.raises(ModelError):
        client.complete(model="test", system="", messages=[], tools=[])
    row = ledger.entries()[0]
    assert row.input_tokens == 23
    assert row.output_tokens is None
    assert row.estimated_usd is None
    assert row.usage_status == "partial"
    with pytest.raises(ModelError):
        client.complete(model="test", system="", messages=[], tools=[])
    assert ledger.totals().calls == 2
    assert ledger.totals().estimated_usd is None


def test_process_restart_sees_pending_intent_and_explicit_recovery_is_accounting_only(ledger):
    call_id = ledger.begin_call("integrity", role="director", stage="direction", model="test")
    restarted = Ledger(ledger.store, ledger.settings)
    restarted.ensure_within_budget()
    assert restarted.totals().unknown_calls == 1
    with pytest.raises(ValueError):
        restarted.reconcile("integrity", call_id, input_tokens=11, output_tokens=4, evidence_note="")
    row = restarted.reconcile("integrity", call_id, input_tokens=11, output_tokens=4,
                               evidence_note="Provider export request 123, verified locally")
    assert row.ok is False
    assert row.call_status == "reconciled"
    assert restarted.totals().calls == 1
    assert restarted.totals().unknown_calls == 0
    restarted.ensure_within_budget()
    with pytest.raises(ValueError):
        restarted.reconcile("integrity", call_id, input_tokens=11, output_tokens=4, evidence_note="duplicate")


def test_settlement_disk_failure_preserves_unknown_diagnostic_and_write_failure(ledger, monkeypatch):
    call_id = ledger.begin_call("integrity", role="director", stage="direction", model="test")
    def fail(*args): raise OSError("disk full")
    monkeypatch.setattr(ledger, "_append", fail)
    with pytest.raises(OSError):
        ledger.settle_call("integrity", call_id, input_tokens=9, output_tokens=4,
                           usage_status="known", ok=True)
    ledger.ensure_within_budget()
    assert ledger.totals().unknown_calls == 1
    with pytest.raises(OSError):
        ledger.begin_call("integrity", role="director", stage="direction", model="test")


def test_legacy_error_zero_is_unknown_and_reconcilable(ledger):
    ledger.store.append_ledger("integrity", LedgerEntry(at=utcnow(), run_id="integrity",
        role="writer", stage="writing", model="test", input_tokens=0, output_tokens=0,
        estimated_usd=0.0, pricing_source="old-test", ok=False))
    row = ledger.entries()[0]
    assert row.call_id == "legacy-1"
    assert row.usage_status == "unknown"
    assert row.estimated_usd is None
    ledger.reconcile("integrity", row.call_id, input_tokens=0, output_tokens=0,
                     evidence_note="Provider confirmed request not accepted")
    assert ledger.totals().calls == 1
    assert ledger.totals().unknown_calls == 0


def test_unknown_rates_and_unknown_calls_preserve_known_subtotal(ledger):
    ledger.record("integrity", role="writer", stage="writing", model="test",
                  input_tokens=1000, output_tokens=1000)
    ledger.record("integrity", role="writer", stage="writing", model="test",
                  input_tokens=5, output_tokens=None, ok=False, usage_status="partial")
    totals = ledger.totals().to_dict()
    assert totals["estimated_usd"] is None
    assert totals["known_estimated_usd"] == pytest.approx(0.003)
    assert totals["unknown_calls"] == 1
    assert totals["unknown_cost_calls"] == 1
    assert ledger.remaining_tokens() is None
    assert ledger.remaining_usd() is None


def test_default_pricing_is_unknown_not_free(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.pricing.input_usd_per_mtok is None
    assert settings.pricing.estimate_usd(10, 20) is None
    assert settings.usd_cap_is_enforceable is False


@pytest.mark.parametrize("bad", [-1, True, "1", float("nan"), float("inf")])
def test_invalid_pricing_rejected(tmp_path, bad):
    (tmp_path / "config.json").write_text(json.dumps({"pricing": {"input_usd_per_mtok": bad}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(tmp_path)


def test_recovery_cannot_clear_an_active_request(ledger):
    with ledger.call_guard():
        call_id = ledger.begin_call("integrity", role="writer", stage="writing", model="test")
        with pytest.raises(BudgetExceeded, match="active"):
            ledger.reconcile("integrity", call_id, input_tokens=1, output_tokens=2,
                             evidence_note="not yet settled")
    assert ledger.entries()[0].call_status == "pending"


def test_recovery_cli_lists_and_reconciles_only_explicit_counts(ledger, capsys):
    from studio.recovery import main
    call_id = ledger.begin_call("integrity", role="writer", stage="writing", model="test")
    root = str(ledger.settings.data_root)
    assert main(["--data-root", root, "pending"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["call_id"] == call_id
    assert rows[0]["input_tokens"] is None
    assert main(["--data-root", root, "reconcile", "integrity", call_id,
                 "--input-tokens", "0", "--output-tokens", "0",
                 "--evidence-note", "Provider confirms request never accepted"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["call_status"] == "reconciled"
    assert row["ok"] is False
    assert ledger.totals().calls == 1
    assert ledger.totals().unknown_calls == 0


def test_terminal_message_without_final_usage_is_not_complete():
    lines = text_stream("x")
    lines = [line.replace(', "usage": {"output_tokens": 5}', '') for line in lines]
    with pytest.raises(ModelError, match="usage missing"):
        _parse_stream(lines, "test")


def test_offline_abandon_closes_only_named_attempt_without_spending(ledger, capsys):
    from studio.recovery import main
    from studio.pipeline import Pipeline
    pipeline = Pipeline(ledger.store)
    attempt = pipeline.start_attempt("integrity", "intake")
    root = str(ledger.settings.data_root)
    main(["--data-root", root, "attempts", "integrity"])
    assert json.loads(capsys.readouterr().out)[0]["stage"] == "intake"
    main(["--data-root", root, "abandon", "integrity", "intake",
          f"--attempt-token={attempt.token}", "--reason", "test process crashed",
          "--server-stopped"])
    assert pipeline.stage_state("integrity", "intake").open_attempt is None
    assert not ledger.entries()
    assert not ledger.store.list_approvals("integrity")
