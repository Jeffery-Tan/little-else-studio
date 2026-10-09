"""Durable progress may extend a phase; repeated reads and versions may not."""
from types import SimpleNamespace

import pytest

from fakes import ScriptedModel, tool_turn
from studio.agents.client import run_agent_loop
from studio.agent_runtime import AgentRuntime, bind_task, reset_task
from studio.config import Settings
from studio.models import BudgetExceeded
from studio.stage_checkpoints import reserve_work_round
from studio.store import Store


def test_only_owned_exact_new_stage_receipt_can_advance_budget(tmp_path, monkeypatch):
    from test_writing_tool_schema import seed
    from studio.stage_checkpoints import checkpoint
    store, pipeline, settings, _, run = seed(tmp_path, monkeypatch)
    artifact = store.latest_artifact(run, 'material_plan')
    sessions = [{'status': 'completed', 'source_message_id': 'source', 'parent_job_id': 'other-job',
        'task_id': artifact.attempt, 'artifact_receipt': {'sha256': artifact.sha256}}]
    orch = SimpleNamespace(store=store, pipeline=pipeline, run_id=run, settings=settings,
        _task_user_input={'message_id': 'source'}, _scope_selected=True, _decision_recorded=None,
        _decision_context=None, _turn_blocker=lambda: None, _artifact_baseline={'material_plan': None},
        _task_constraints={'task_mode': 'produce', 'allowed_stages': ['material_plan', 'writing']},
        dispatcher=SimpleNamespace(sessions=lambda: sessions), _turn_id='turn', method_context={}, _emit=lambda event: None)
    budgeted = SimpleNamespace(stage='material_plan')
    token = bind_task('job')
    try:
        assert checkpoint(orch, budgeted) is None
        sessions[0]['parent_job_id'] = 'job'
        orch._artifact_baseline['material_plan'] = {'version': artifact.version}
        assert checkpoint(orch, budgeted) is None
        orch._artifact_baseline['material_plan'] = None
        assert checkpoint(orch, budgeted) == 'material_plan'
        assert checkpoint(orch, budgeted) is None
    finally:
        reset_task(token)
    saved = [row for row in store.read_events(run) if row['kind'] == 'agent_stage_checkpoint']
    assert len(saved) == 1 and saved[0]['artifact']['sha256'] == artifact.sha256
    assert saved[0]['next_stage'] == budgeted.stage == 'writing'
    assert saved[0]['grants_execution'] is False and saved[0]['grants_decision'] is False


def test_distinct_checkpoint_starts_next_stage_allowance_not_same_stage_retry():
    model = ScriptedModel([tool_turn(str(index), 'read_capabilities', {}) for index in range(5)])
    count = []
    def progress():
        count.append(1)
        return 'intake' if len(count) >= 2 else None
    result = run_agent_loop(model, model='fixture', system='fixture', messages=[],
        tools=[{'name': 'read_capabilities'}], execute_tool=lambda *args: 'ok',
        max_rounds=2, stage_checkpoint=progress)
    assert result.rounds == 4 and result.stopped_because == 'max_rounds'
    assert len(model.calls) == 4


def test_phase_allowance_survives_resume_and_only_saved_checkpoint_opens_next(tmp_path):
    store = Store(tmp_path)
    store.create_run('phase-test', 'Fixture', 'Original task')
    runtime = AgentRuntime(store, 'phase-test')
    runtime.create_task('job', role='orchestrator', parent_job_id='job')
    orch = SimpleNamespace(store=store, run_id='phase-test', settings=Settings(tmp_path,
        'https://example.invalid', max_tool_rounds=2), _task_user_input={'message_id': 'original'})
    token = bind_task('job')
    try:
        reserve_work_round(orch)
        reserve_work_round(orch)
        with pytest.raises(BudgetExceeded):
            reserve_work_round(orch)
        store.append_event('phase-test', 'agent_stage_checkpoint', task_id='job', stage='intake')
        reserve_work_round(orch)
        reserve_work_round(orch)
        with pytest.raises(BudgetExceeded):
            reserve_work_round(orch)
    finally:
        reset_task(token)
    assert len([row for row in store.read_events('phase-test') if row['kind'] == 'agent_work_round']) == 4
