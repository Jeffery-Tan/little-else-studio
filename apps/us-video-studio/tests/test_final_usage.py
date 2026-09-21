"""Content validity and provider accounting are independent; no live requests."""
import pytest

from studio.agents.budget import BudgetedClient
from studio.agents.client import ModelError, Usage
from studio.agents.deepseek_client import _parse
from studio.config import Settings
from studio.ledger import Ledger
from studio.models import BudgetExceeded
from studio.store import Store


@pytest.mark.parametrize('counts,known', [
    ({'prompt_tokens': 10458, 'completion_tokens': 16384}, True),
    ({'prompt_tokens': 0, 'completion_tokens': 0}, True),
    ({'prompt_tokens': 10458}, False),
    ({'completion_tokens': 16384}, False),
    ({'prompt_tokens': 10458, 'completion_tokens': True}, False),
    ({'prompt_tokens': 10458, 'completion_tokens': -1}, False),
    ({}, False),
])
def test_invalid_content_records_final_usage_without_dispatching_tools(tmp_path, counts, known):
    store = Store(tmp_path)
    store.create_run('usage-test', 'Fixture', 'Offline')
    ledger = Ledger(store, Settings(tmp_path, ''))

    class InvalidContent:
        def complete(self, **kwargs):
            return _parse({'usage': counts, 'choices': [{
                'finish_reason': 'length', 'message': {
                    'reasoning_content': 'private fixture', 'tool_calls': [{
                        'id': 'unfinished', 'type': 'function', 'function': {
                            'name': 'submit_stage', 'arguments': '{"incomplete":'}}]}}]},
                'deepseek-flash')

    client = BudgetedClient(InvalidContent(), ledger, run_id='usage-test', role='director', stage='direction')
    with pytest.raises(ModelError):
        client.complete(model='deepseek-flash', system='', messages=[], tools=[])
    row, = ledger.entries('usage-test')
    assert row.ok is False
    assert store.list_artifacts('usage-test') == []
    assert (row.usage_status == 'known') is known
    if known:
        assert row.input_tokens == counts['prompt_tokens']
        assert row.output_tokens == counts['completion_tokens']
        ledger.ensure_within_budget()
    else:
        assert row.estimated_usd is None
        ledger.ensure_within_budget()


def test_stream_counters_without_final_evidence_stay_partial():
    error = ModelError(200, 'stream interrupted', usage=Usage(31, 12))
    assert error.usage_status == 'partial'
    assert ModelError(0, 'timeout').usage_status == 'unknown'
