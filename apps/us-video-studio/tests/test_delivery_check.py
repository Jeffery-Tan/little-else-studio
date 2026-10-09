"""Offline content-check control flow, not measured creative improvement."""
import copy
import json

import pytest

from studio.agents.client import NonRetryableStageError
from studio.models import PipelineError
from studio.delivery_check import validate_repair_scope
from test_finishing_phase import harness, TEXT, LOCAL


def finding(path, quote, *, verdict='repair'):
    return {'verdict': verdict, 'summary': 'A concrete fixture content issue remains.',
        'findings': [{'path': path, 'quote': quote,
                     'issue': 'This exact phrase does not match the intended fixture tone.',
                     'instruction': 'Make the wording more conversational without changing any facts.'}]}


def test_producer_check_repairs_once_under_original_source_then_rechecks(tmp_path, monkeypatch):
    check = finding(['segments', 0, 'line'], 'Pack a little room for your day.')
    orch, store, pipe, ledger, run, seen, _, _, _ = harness(tmp_path, monkeypatch,
        local=True, dispatch_round=3, check_results=[check], repair_value='A little room for whatever comes next.')
    before_receipts = [row for row in store.read_events(run) if row['kind'] == 'user_message_received']
    before_decisions = store.list_approvals(run)
    result = orch.send(LOCAL)
    assert result.stopped_because == 'done' and result.deliverable['producer_check']['check_index'] == 2
    assert store.latest_artifact(run, 'writing').version == 3
    assert store.latest_artifact(run, 'writing').structured['segments'][0]['line'] == 'A little room for whatever comes next.'
    rows = store.read_events(run)
    assert [row for row in rows if row['kind'] == 'user_message_received'] == before_receipts
    assert store.list_approvals(run) == before_decisions
    plans = [row for row in rows if row['kind'] == 'delivery_repair_planned']
    assert len(plans) == 1 and plans[0]['scope']['origin'] == 'delivery_self_check'
    assert 'quote' not in plans[0]['scope']
    checks = [row for row in rows if row['kind'] == 'delivery_self_check']
    assert [row['check_index'] for row in checks] == [1, 2]
    assert all(row['quality_verified'] is False and row['user_approval'] is False for row in checks)
    assert not seen[-1].get('tools') and 'phase=finishing' in seen[-1]['messages'][0]['content']
    assert len(ledger.entries(run)) == len(seen)


def test_check_outside_original_local_fields_stops_without_new_task_or_fix(tmp_path, monkeypatch):
    check = finding(['caption'], 'Pack a little room for your day.')
    orch, store, _, _, run, seen, _, _, _ = harness(tmp_path, monkeypatch,
        local=True, dispatch_round=3, check_results=[check])
    result = orch.send(LOCAL)
    assert result.stopped_because == 'incomplete'
    assert result.deliverable['status'] == 'incomplete'
    assert store.latest_artifact(run, 'writing').version == 2
    assert not any(row['kind'] == 'delivery_repair_planned' for row in store.read_events(run))
    assert not seen[-1].get('tools')


def test_fabricated_quote_cannot_trigger_repair(tmp_path, monkeypatch):
    check = finding(['caption'], 'THIS DOES NOT EXIST IN THE SAVED WORK')
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch, check_results=[check])
    with pytest.raises(NonRetryableStageError, match='准确文字'):
        orch.send(TEXT)
    assert store.latest_artifact(run, 'writing').version == 1
    assert not any(row['kind'] == 'delivery_repair_planned' for row in store.read_events(run))


def test_unknown_external_fact_is_incomplete_without_forced_field_or_repair(tmp_path, monkeypatch):
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch,
        check_results=[{'verdict': 'blocked', 'summary': '所需供应事实没有证据，当前稿件不能验证该要求。', 'findings': []}])
    result = orch.send(TEXT)
    assert result.stopped_because == 'incomplete'
    assert result.deliverable['producer_check']['verdict'] == 'blocked'
    assert store.latest_artifact(run, 'writing').version == 1
    assert not any(row['kind'] == 'delivery_repair_planned' for row in store.read_events(run))


def test_second_check_with_remaining_issue_cannot_start_another_repair(tmp_path, monkeypatch):
    text = 'A little room for whatever comes next.'
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch,
        local=True, dispatch_round=3, repair_value=text,
        check_results=[finding(['segments', 0, 'line'], 'Pack a little room for your day.'),
                       finding(['segments', 0, 'line'], text)])
    result = orch.send(LOCAL)
    assert result.stopped_because == 'incomplete'
    assert result.deliverable['producer_check']['check_index'] == 2
    assert store.latest_artifact(run, 'writing').version == 3
    assert len([row for row in store.read_events(run) if row['kind'] == 'delivery_repair_planned']) == 1


def test_a_model_origin_flag_does_not_create_repair_authority(tmp_path, monkeypatch):
    orch, store, _, _, run, _, _, _, _ = harness(tmp_path, monkeypatch)
    with pytest.raises(PipelineError, match='已保存的准确范围'):
        validate_repair_scope(store, run, {'origin': 'delivery_self_check', 'plan_id': 'invented'}, 'invented')


def test_distinct_receipts_share_one_repair_quota_within_parent_job(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from studio.agent_runtime import AgentRuntime, bind_task, reset_task
    from studio.conversation_input import (accept, receive, start_execution,
        record_task_interpretation, interpreted_task_constraints)
    from studio.delivery_check import repair_scope, _repair_already_planned
    from test_writing_tool_schema import seed, submission

    store, pipe, _, _, run = seed(tmp_path, monkeypatch)
    attempt = pipe.start_attempt(run, 'writing')
    artifact = pipe.submit(run, 'writing', attempt.token, **submission())
    runtime = AgentRuntime(store, run)
    scopes = []
    for index in range(2):
        text = f'Write the complete fixture script for annotation group {index}.'
        receipt = receive(store, run, text, f'annotation-group-{index}')
        accept(store, run, receipt)
        start_execution(store, run, receipt)
        meaning = record_task_interpretation(store, run, receipt, {'task': {
            'quote': text, 'mode': 'produce', 'deliverable': 'writing', 'format': 'video',
            'allowed_stages': ['writing'], 'pause_after': True}, 'constraints': []})
        orch = SimpleNamespace(store=store, pipeline=pipe, run_id=run,
            _task_user_input=receipt, _task_constraints=interpreted_task_constraints(receipt, meaning),
            _decision_context=None, _revision_scope=None, _turn_id=f'turn-{index}')
        assessment = finding(['segments', 0, 'line'], artifact.structured['segments'][0]['line'])
        evidence = store.append_event(run, 'delivery_self_check', source_message_id=receipt['message_id'],
            stage='writing', artifact={'version': artifact.version, 'sha256': artifact.sha256},
            assessment=assessment, quality_verified=False, user_approval=False)
        check = {**assessment,
                 'stage': 'writing', 'artifact': {'version': artifact.version, 'sha256': artifact.sha256},
                 'event_seq': evidence['seq']}
        task_id = f'annotation-task-{index}'
        runtime.create_task(task_id, role='orchestrator', parent_job_id='batch-parent',
                            source_message_id=receipt['message_id'])
        token = bind_task(task_id)
        try:
            if index == 0:
                scopes.append(repair_scope(orch, check))
                assert scopes[0]['root_job_id'] == 'batch-parent'
            else:
                assert _repair_already_planned(orch)
                with pytest.raises(PipelineError, match='已用过一次'):
                    repair_scope(orch, check)
        finally:
            reset_task(token)
        if index == 0:
            # Historical/unbound callers still cannot repeat the same receipt.
            with pytest.raises(PipelineError, match='已用过一次'):
                repair_scope(orch, check)
    plans = [row for row in store.read_events(run) if row['kind'] == 'delivery_repair_planned']
    assert len(plans) == 1 and plans[0]['job_id'] == 'batch-parent'
    # A separate explicitly started task has its own quota; the project is not
    # globally limited to one repair, and no new receipt was fabricated above.
    token = bind_task('separate-parent')
    try:
        assert not _repair_already_planned(orch)
        with pytest.raises(PipelineError, match='当前父任务'):
            validate_repair_scope(store, run, scopes[0], scopes[0]['source_message_id'])
        repair_scope(orch, check)
    finally:
        reset_task(token)


def test_ui_decision_successor_has_check_without_fabricated_user_message(tmp_path, monkeypatch):
    from studio.agents.orchestrator import Orchestrator
    from studio.agent_runtime import AgentRuntime, bind_task, reset_task
    from studio.decision import continuation_request_id
    from studio.decision_continuation import freeze
    from fakes import ScriptedModel, tool_turn, final_turn
    from test_writing_tool_schema import seed, submission
    store, pipe, settings, ledger, run = seed(tmp_path, monkeypatch)
    for stage in ('direction', 'material_plan'):
        old = store.latest_artifact(run, stage)
        attempt = pipe.start_attempt(run, stage)
        pipe.submit(run, stage, attempt.token, content='', summary=stage, structured=old.structured)
        if stage == 'direction':
            approval = pipe.record_approval(run, stage, nonce=pipe.issue_nonce(run, stage),
                decision='approved', selected_option_id='A', continue_after=True)
    context = freeze(store, pipe, run, continuation_request_id(approval))
    runtime = AgentRuntime(store, run)
    runtime.create_task('ui-successor', role='orchestrator', parent_job_id='ui-successor')
    runtime.transition('ui-successor', 'running')
    model = ScriptedModel([tool_turn('dispatch', 'dispatch', {'stage': 'writing', 'brief': '已采用方向的脚本'}),
        tool_turn('submit', 'submit_stage', submission()),
        tool_turn('check', 'submit_delivery_check', {'verdict': 'ready', 'summary': 'Synthetic only.', 'findings': []}),
        final_turn('完整脚本已保存，仍待用户采用。')])
    orch = Orchestrator(store, pipe, settings, model, ledger, run)
    token = bind_task('ui-successor')
    try:
        result = orch.send('按已保存UI决定继续准备', internal_instruction=True, decision_context=context)
    finally:
        reset_task(token)
    assert result.stopped_because == 'done' and result.deliverable['producer_check']['verdict'] == 'ready'
    rows = store.read_events(run)
    check = next(row for row in rows if row['kind'] == 'delivery_self_check')
    assert check['source_message_id'] is None and check['decision_request_id'] == context['request_id']
    assert not any(row['kind'] == 'user_message_received' for row in rows)
    final = json.loads(model.calls[-1]['messages'][0]['content'])
    assert final['user_text'] is None and final['decision_context'] == context
    assert model.calls[-1]['tool_names'] == []
