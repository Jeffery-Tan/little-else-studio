"""Parent and child calls consume the same durable task budget."""
import pytest

from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.agents.budget import BudgetedClient
from studio.config import Settings
from studio.ledger import Ledger
from studio.models import BudgetExceeded
from studio.store import Store
from studio.pipeline import Pipeline
from studio import task_execution
from fakes import ScriptedModel, final_turn


def test_parent_and_role_share_limit_and_measurements(tmp_path):
    store = Store(tmp_path)
    store.create_run('budget', 'test', 'test')
    settings = Settings(data_root=tmp_path, base_url='https://example.invalid', task_max_calls=1)
    ledger = Ledger(store, settings)
    runtime = AgentRuntime(store, 'budget')
    runtime.create_task('parent', role='orchestrator', parent_job_id='parent')
    attempt = Pipeline(store).start_attempt('budget', 'intake')
    child = f'intake#{attempt.number}'
    runtime.create_task(child, role='orchestrator', stage='intake', parent_job_id='parent', parent_task_id='parent')
    task_execution.freeze(store, 'budget', 'parent', settings)
    model = ScriptedModel([final_turn('done'), final_turn('should not run')])
    client = BudgetedClient(model, ledger, run_id='budget', role='orchestrator', stage='')
    token = bind_task('parent')
    try:
        client.complete(model='test', system='s', messages=[{'role': 'user', 'content': 'hi'}], tools=[])
    finally:
        reset_task(token)
    token = bind_task(child)
    try:
        with pytest.raises(BudgetExceeded):
            client.complete(model='test', system='s', messages=[], tools=[])
    finally:
        reset_task(token)
    report = task_execution.summary(store, ledger, 'budget', 'parent')
    assert report['calls'] == 1 and report['input_bytes']['system'] == 1
    assert len(model.calls) == 1


def test_queue_wait_does_not_consume_execution_time(tmp_path):
    store = Store(tmp_path)
    store.create_run('queued', 'test', 'test')
    settings = Settings(data_root=tmp_path, base_url='https://example.invalid')
    task_execution.freeze(store, 'queued', 'parent', settings)
    store.append_event('queued', 'job_queued', job_id='parent', order=1)
    report = task_execution.summary(store, Ledger(store, settings), 'queued', 'parent')
    assert report['elapsed_seconds'] == 0
    assert report['calls'] == 0
    task_execution.check(store, 'queued', 'parent')
