"""Durable creative-stage progress; no user decision or budget is renewed."""
from __future__ import annotations

import hashlib
import json

from .agent_runtime import current_task_id
from .models import BudgetExceeded


def _binding(orch):
    source = orch._task_user_input
    if not orch._scope_selected or orch._decision_recorded or orch._turn_blocker():
        return None
    task = orch._task_constraints
    decision = orch._decision_context
    if not source and decision:
        from .decision_continuation import validate
        validate(orch.store, orch.pipeline, orch.run_id, decision)
    if (not source and not decision or task.get('readonly')
            or task.get('task_mode') not in {'produce', 'revise', 'create'}):
        return None
    return {'source_message_id': source['message_id'] if source else None,
            'decision_request_id': decision['request_id'] if decision else None,
            'task_sha256': hashlib.sha256(json.dumps(task, ensure_ascii=False,
                sort_keys=True).encode('utf-8')).hexdigest()}


def checkpoint(orch, budgeted):
    binding = _binding(orch)
    job = current_task_id()
    if not binding or not job:
        return None
    with orch.store.run_lock(orch.run_id):
        rows = orch.store.read_events(orch.run_id)
        seen = {row['stage'] for row in rows if row['kind'] == 'agent_stage_checkpoint'
                and row.get('task_id') == job}
        stages = ('intake', 'direction', 'material_plan', 'writing')
        allowed = orch._task_constraints.get('allowed_stages', [])
        for stage in stages:
            if stage in seen or stage not in allowed:
                continue
            state = orch.pipeline.stage_state(orch.run_id, stage)
            item = state.latest
            baseline = orch._artifact_baseline.get(stage)
            if (not item or state.stale or not state.integrity_ok or state.open_attempt
                    or baseline and item.version <= baseline['version']):
                continue
            if stage != 'intake' and not any(row.get('status') == 'completed'
                    and row.get('source_message_id') == binding['source_message_id']
                    and row.get('parent_job_id') == job and row.get('task_id') == item.attempt
                    and (row.get('artifact_receipt') or {}).get('sha256') == item.sha256
                    for row in orch.dispatcher.sessions()):
                continue
            if stage == 'intake' and not item.author_role.startswith('orchestrator'):
                continue
            if not orch.store.verify_artifact(orch.run_id, stage, item.version):
                continue
            successor = next((candidate for candidate in stages[stages.index(stage) + 1:]
                if candidate in allowed and orch.pipeline.stage_state(orch.run_id, candidate).can_start), None)
            if successor is None:
                continue
            event = orch.store.append_event(orch.run_id, 'agent_stage_checkpoint', task_id=job,
                turn_id=orch._turn_id, **binding, stage=stage, next_stage=successor,
                artifact={'version': item.version, 'sha256': item.sha256},
                methods_sha256=(orch.method_context or {}).get('methods_sha256'),
                max_work_rounds=orch.settings.max_tool_rounds,
                grants_decision=False, grants_execution=False)
            budgeted.stage = successor
            orch._emit({'type': 'agent_activity', 'phase': 'stage_checkpoint',
                        'stage': successor, 'completed_stage': stage, 'checkpoint_seq': event['seq']})
            return stage
    return None


def reserve_work_round(orch):
    """A restart cannot erase the rounds already spent after its checkpoint."""
    job = current_task_id()
    if not job:
        return
    with orch.store.run_lock(orch.run_id):
        rows = orch.store.read_events(orch.run_id)
        checkpoint = next((row for row in reversed(rows) if row['kind'] == 'agent_stage_checkpoint'
                           and row.get('task_id') == job), None)
        phase = checkpoint['seq'] if checkpoint else None
        count = sum(row['kind'] == 'agent_work_round' and row.get('task_id') == job
                    and row.get('checkpoint_seq') == phase for row in rows)
        if count >= orch.settings.max_tool_rounds:
            raise BudgetExceeded('本阶段的文字工作轮次已用完；已保存当前进度，不自动重开额度。')
        orch.store.append_event(orch.run_id, 'agent_work_round', task_id=job,
            checkpoint_seq=phase, round=count + 1, source_message_id=(orch._task_user_input or {}).get('message_id'))
