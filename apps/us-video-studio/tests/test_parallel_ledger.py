"""Concurrent role calls retain intents and exclude live reconciliation."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from fakes import final_turn
from studio.agents.budget import BudgetedClient
from studio.ledger import Ledger
from studio.models import BudgetExceeded
from studio.store import Store
from test_server import RUN, settings_for


def test_parent_and_child_calls_can_overlap_and_both_settle(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline', 'Synthetic task')
    ledger = Ledger(store, settings)
    barrier = threading.Barrier(3)
    release = threading.Event()
    class Blocking:
        def complete(self, **_):
            barrier.wait(3)
            assert release.wait(3)
            return final_turn('synthetic result')
    def call(role):
        return BudgetedClient(Blocking(), ledger, run_id=RUN, role=role, stage='intake').complete(
            model=settings.model_for(role), system='test', messages=[{'role':'user','content':'test'}], tools=[])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, role) for role in ('orchestrator', 'director')]
        try:
            barrier.wait(3)
            assert ledger.totals(RUN).pending_calls == 2
            with pytest.raises(BudgetExceeded, match='active'):
                with ledger.call_guard():
                    pytest.fail('exclusive reconciliation entered during live calls')
        finally:
            release.set()
        assert all(future.result(3).text == 'synthetic result' for future in futures)
    totals = ledger.totals(RUN)
    assert totals.calls == 2 and totals.pending_calls == 0 and totals.unknown_calls == 0
    with ledger.call_guard():
        pass


def test_one_call_finishing_does_not_release_another_calls_lease(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(tmp_path)
    ledger = Ledger(store, settings)
    entered, release = threading.Event(), threading.Event()
    def worker():
        with ledger.call_guard(concurrent=True):
            entered.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert entered.wait(2)
        try:
            with ledger.call_guard(concurrent=True):
                pass
            with pytest.raises(BudgetExceeded):
                with ledger.call_guard():
                    pass
        finally:
            release.set()
        future.result(3)
