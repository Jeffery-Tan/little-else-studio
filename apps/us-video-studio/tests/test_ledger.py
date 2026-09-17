"""Ledger rules: tokens are facts, dollars are labelled estimates, cap stops work."""
from __future__ import annotations

import pytest

from studio.config import Pricing, Settings, load_settings
from studio.ledger import Ledger
from studio.models import BudgetExceeded
from studio.store import Store

RUN = "lunch-bag-01"


def make_settings(tmp_path, **over):
    base = dict(
        data_root=tmp_path,
        base_url="https://api.deepseek.com/anthropic",
        budget_limit_usd=10.0,
        pricing=Pricing(
            source="test-rate-card", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
            as_of="2026-09-17",
        ),
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag ad", "brief")
    return store, Ledger(store, make_settings(tmp_path))


def test_one_row_per_call_with_tokens_and_labelled_estimate(env):
    store, ledger = env
    entry = ledger.record(
        RUN, role="director", stage="direction", model="deepseek-flash",
        input_tokens=1200, output_tokens=800, stop_reason="end_turn",
    )
    assert entry.input_tokens == 1200
    assert entry.output_tokens == 800
    # 1200/1e6*1.0 + 800/1e6*2.0
    assert entry.estimated_usd == pytest.approx(0.0028)
    assert entry.pricing_source == "test-rate-card"

    rows = store.read_ledger(RUN)
    assert len(rows) == 1
    assert rows[0].pricing_source == "test-rate-card"


def test_totals_accumulate(env):
    _, ledger = env
    for _ in range(3):
        ledger.record(
            RUN, role="writer", stage="writing", model="deepseek-flash",
            input_tokens=1000, output_tokens=0,
        )
    totals = ledger.totals(RUN)
    assert totals.calls == 3
    assert totals.input_tokens == 3000
    assert totals.estimated_usd == pytest.approx(0.003)


def test_failed_call_is_still_recorded(env):
    store, ledger = env
    ledger.record(
        RUN, role="director", stage="direction", model="deepseek-flash",
        input_tokens=0, output_tokens=0, ok=False, error="502 from upstream",
    )
    row = store.read_ledger(RUN)[0]
    assert row.ok is False
    assert "502" in row.error


def test_cap_blocks_the_next_call_once_reached(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "t", "b")
    ledger = Ledger(store, make_settings(tmp_path, budget_limit_usd=0.001))

    ledger.ensure_within_budget()  # under the cap: allowed

    ledger.record(
        RUN, role="director", stage="direction", model="deepseek-flash",
        input_tokens=1000, output_tokens=1000,
    )
    with pytest.raises(BudgetExceeded) as err:
        ledger.ensure_within_budget()
    assert "cap" in str(err.value)
    assert ledger.remaining_usd(RUN) == 0.0


def test_spend_carries_across_runs_so_the_cap_is_a_total(tmp_path):
    """The $10 is a development budget, not a per-run allowance."""
    store = Store(tmp_path)
    store.create_run("run-a", "a", "b")
    store.create_run("run-b", "b", "b")
    ledger = Ledger(store, make_settings(tmp_path, budget_limit_usd=0.001))

    ledger.record(
        "run-a", role="director", stage="direction", model="deepseek-flash",
        input_tokens=1000, output_tokens=1000,
    )
    with pytest.raises(BudgetExceeded):
        ledger.ensure_within_budget()


def test_the_token_cap_is_what_actually_bites(tmp_path):
    """With no confirmed rate card the USD cap cannot fire, so tokens must."""
    store = Store(tmp_path)
    store.create_run(RUN, "t", "b")
    ledger = Ledger(
        store,
        make_settings(tmp_path, budget_limit_usd=10.0, budget_limit_tokens=1000),
    )
    ledger.ensure_within_budget()
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=600, output_tokens=500)

    with pytest.raises(BudgetExceeded) as err:
        ledger.ensure_within_budget()
    assert "tokens" in str(err.value)
    assert ledger.remaining_tokens() == 0


def test_a_zero_rate_card_is_reported_as_not_enforceable(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "t", "b")
    settings = Settings(
        data_root=tmp_path, base_url="https://example.invalid",
        budget_limit_usd=10.0,
        pricing=Pricing(source="placeholder", input_usd_per_mtok=0.0,
                        output_usd_per_mtok=0.0, as_of="2026-09-17"),
    )
    ledger = Ledger(store, settings)
    note = ledger.price_note()
    assert note["usd_cap_enforceable"] is False
    assert "cannot stop anything yet" in note["note"]

    # Even after a large spend, the USD cap does not fire — and says so.
    ledger.record(RUN, role="director", stage="direction", model="deepseek-flash",
                  input_tokens=5_000_000, output_tokens=0)
    assert ledger.spent_usd(RUN) is None
    assert ledger.totals(RUN).unknown_cost_calls == 1
    assert ledger.totals(RUN).known_estimated_usd == 0.0
    assert settings.usd_cap_is_enforceable is False


def test_price_note_says_the_dollars_are_estimates(env):
    _, ledger = env
    note = ledger.price_note()
    assert note["source"] == "test-rate-card"
    assert note["as_of"] == "2026-09-17"
    assert "not the provider's bill" in note["note"]


# ------------------------------------------------------------ config rules --


def test_default_config_has_every_role_on_a_known_model(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.budget_limit_usd == 10.0
    for role in ("orchestrator", "director", "material", "writer", "reviewer"):
        assert settings.model_for(role) == "deepseek-flash"


def test_config_rejects_an_unknown_model_name(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"models": {"director": "deepseek-v4.1-flash"}}', encoding="utf-8"
    )
    with pytest.raises(ValueError) as err:
        load_settings(tmp_path)
    assert "deepseek-v4.1-flash" in str(err.value)


def test_config_rejects_a_negative_budget(tmp_path):
    (tmp_path / "config.json").write_text('{"budget_limit_usd": -1}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(tmp_path)


def test_config_can_move_one_role_to_the_stronger_model(tmp_path):
    (tmp_path / "config.json").write_text(
        '{"models": {"reviewer": "deepseek-v4-pro"}}', encoding="utf-8"
    )
    settings = load_settings(tmp_path)
    assert settings.model_for("reviewer") == "deepseek-v4-pro"
    assert settings.model_for("director") == "deepseek-flash"
