"""Task budgets and measurements projected from the existing project journal.

These limits cover one accepted task, including child roles and checks. They
grant no media authority. No in-memory counter can reset a durable call limit.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .agent_runtime import current_task_id
from .models import BudgetExceeded


def root_task(store, run_id):
    task_id = current_task_id()
    if not task_id:
        return None
    for row in reversed(store.read_events(run_id)):
        if row.get('kind') == 'agent_runtime_task_created' and row.get('task_id') == task_id:
            return row['task'].get('parent_job_id') or task_id
    return task_id


def freeze(store, run_id, job_id, settings):
    with store.run_lock(run_id):
        existing = next((row for row in store.read_events(run_id)
                         if row.get('kind') == 'task_execution_budget' and row.get('job_id') == job_id), None)
        if existing:
            return existing
        return store.append_event(run_id, 'task_execution_budget', job_id=job_id,
            max_calls=settings.task_max_calls, timeout_seconds=settings.task_timeout_seconds)


def check(store, run_id, job_id=None, *, reserve=False):
    job_id = job_id or root_task(store, run_id)
    if not job_id:
        return
    rows = store.read_events(run_id)
    budget = next((row for row in rows if row.get('kind') == 'task_execution_budget'
                   and row.get('job_id') == job_id), None)
    if not budget:
        return
    execution = next((row for row in rows if row.get('kind') == 'job_execution_started'
                      and row.get('job_id') == job_id), None)
    queued = not execution and any(row.get('kind') == 'job_queued' and row.get('job_id') == job_id for row in rows)
    started = datetime.fromisoformat((execution or budget)['at'].replace('Z', '+00:00'))
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if not queued and elapsed >= budget['timeout_seconds']:
        raise BudgetExceeded('本次任务的执行时间已用完；已保存进度，等待明确继续，不自动重试。')
    calls = sum(row.get('kind') == 'task_model_call' and row.get('job_id') == job_id for row in rows)
    if reserve and calls >= budget['max_calls']:
        raise BudgetExceeded('本次任务的文字调用额度已用完；已有作品和用量保留，不自动开启新额度。')


def record_call(store, run_id, call_id, *, role, stage, system, messages, tools):
    job_id = root_task(store, run_id)
    if not job_id:
        return None
    def size(value):
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return len(raw.encode('utf-8'))
    return store.append_event(run_id, 'task_model_call', job_id=job_id,
        task_id=current_task_id(), call_id=call_id, role=role, stage=stage,
        input_bytes={'system': size(system), 'messages': size(messages), 'tools': size(tools),
                     'tool_results': sum(size(message) if message.get('role') == 'tool' else
                         sum(size(block) for block in message.get('content', [])
                             if isinstance(block, dict) and block.get('type') == 'tool_result')
                         if isinstance(message.get('content'), list) else 0 for message in messages)})


def record_response(store, run_id, call_id, elapsed_seconds):
    job_id = root_task(store, run_id)
    if job_id:
        store.append_event(run_id, 'task_model_response', job_id=job_id, call_id=call_id,
                           elapsed_seconds=round(elapsed_seconds, 3))


def summary(store, ledger, run_id, job_id):
    rows = store.read_events(run_id)
    calls = [row for row in rows if row.get('kind') == 'task_model_call' and row.get('job_id') == job_id]
    ids = {row['call_id'] for row in calls}
    entries = [row for row in ledger.entries(run_id) if row.call_id in ids]
    budget = next((row for row in rows if row.get('kind') == 'task_execution_budget'
                   and row.get('job_id') == job_id), None)
    def count(field):
        values = [getattr(row, field, None) for row in entries]
        return sum(value for value in values if type(value) is int) if any(type(v) is int for v in values) else None
    costs = [row.estimated_usd for row in entries if row.estimated_usd is not None]
    execution = next((row for row in rows if row.get('kind') == 'job_execution_started'
                      and row.get('job_id') == job_id), None)
    queued = any(row.get('kind') == 'job_queued' and row.get('job_id') == job_id for row in rows)
    started = execution or (None if queued else budget)
    ended = next((row for row in reversed(rows) if row.get('kind') == 'job_status'
                  and row.get('job_id') == job_id and row.get('status') in
                  {'succeeded', 'blocked', 'failed', 'cancelled', 'interrupted'}), None)
    elapsed = 0 if queued and not execution else None
    if started:
        end = datetime.fromisoformat(ended['at'].replace('Z', '+00:00')) if ended else datetime.now(timezone.utc)
        elapsed = max(0, (end - datetime.fromisoformat(started['at'].replace('Z', '+00:00'))).total_seconds())
    return {'calls': len(calls), 'input_tokens': count('input_tokens'), 'output_tokens': count('output_tokens'),
            'estimated_usd': sum(costs) if costs else None, 'cost_complete': len(costs) == len(calls),
            'elapsed_seconds': elapsed,
            'model_elapsed_seconds': sum(row['elapsed_seconds'] for row in rows
                if row.get('kind') == 'task_model_response' and row.get('job_id') == job_id),
            'call_details': [{'call_id': row['call_id'], 'role': row['role'], 'stage': row['stage'],
                             'input_bytes': row['input_bytes']} for row in calls],
            'usage_complete': len(entries) == len(calls) and all(row.usage_status == 'known' for row in entries),
            'input_bytes': {key: sum(row['input_bytes'][key] for row in calls)
                            for key in ('system', 'messages', 'tools', 'tool_results')},
            'budget': {key: budget[key] for key in ('max_calls', 'timeout_seconds')} if budget else None}
