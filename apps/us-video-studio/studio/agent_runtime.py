"""Durable role-task ownership and inboxes over the existing run event log.

This is bookkeeping, not a scheduler: reading or reconstructing it never starts
work. JobManager owns execution and cancellation; Pipeline owns stage attempts,
artifacts and user decisions. A stage task reuses its Pipeline attempt key.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from copy import deepcopy

from .models import PipelineError, STAGE_SPEC
from .tools.registry import tool_names_for


STATUSES = frozenset({'queued', 'running', 'waiting', 'completed', 'failed',
                      'cancelled', 'interrupted'})
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})
TRANSITIONS = {
    'queued': {'running', 'failed', 'cancelled', 'interrupted'},
    'running': {'waiting', 'completed', 'failed', 'cancelled', 'interrupted'},
    'waiting': {'running', 'completed', 'failed', 'cancelled', 'interrupted'},
}
_CURRENT_TASK = ContextVar('studio_agent_runtime_task', default=None)


def current_task_id():
    return _CURRENT_TASK.get()


def bind_task(task_id):
    return _CURRENT_TASK.set(task_id)


def reset_task(token):
    _CURRENT_TASK.reset(token)


_REF_FIELDS = frozenset({
    'kind', 'role', 'stage', 'version', 'sha256', 'artifact_sha256',
    'source_message_id', 'message_id', 'request_id', 'job_id', 'task_id',
    'parent_task_id', 'parent_job_id', 'evidence_ref', 'ref', 'source_ref',
    'sources', 'artifacts', 'inputs', 'context_refs', 'method_ref',
    'method_sha256', 'snapshot_sha256', 'system_sha256', 'messages_sha256',
    'seed_sha256', 'request_sha256', 'event_seq', 'chat_entries',
    'run_brief_sha256',
})


def _error(message):
    return PipelineError('agent_runtime', message)


def _identity(value, label, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise _error(f'{label} must be a nonempty identity of at most 200 characters')
    return value


def _references(value):
    """Retain identities/hashes, never raw prompts, tool arguments or tokens."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: _references(item) for key, item in value.items()
                if key in _REF_FIELDS or (
                    isinstance(key, str) and key in {
                        *STAGE_SPEC, 'evidence:snapshot', 'evidence:generation',
                        'references:snapshot', 'references:generation', 'brief:snapshot',
                        'direction:selection', 'writing:selection',
                    } and isinstance(item, str)
                    and re.fullmatch(r'(?:[a-f0-9]{64}|[0-9]+)', item))}
    if isinstance(value, (list, tuple)):
        return [_references(item) for item in value]
    if isinstance(value, (str, int, bool)):
        return value
    raise _error('context references must contain JSON metadata only')


class AgentRuntime:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def _tasks(self):
        tasks = {}
        for event in self.store.read_events(self.run_id):
            kind, task_id = event.get('kind'), event.get('task_id')
            if kind == 'agent_runtime_task_created':
                if task_id in tasks:
                    raise _error('duplicate task creation in execution history')
                tasks[task_id] = {
                    **deepcopy(event['task']), 'status': 'queued', 'reason': '',
                    'artifact_receipt': None, 'stop_requested': False,
                    'created_at': event['at'], 'updated_at': event['at'],
                    'event_seq': event['seq'], 'messages': [],
                }
            elif kind in {'agent_runtime_task_status', 'agent_runtime_stop_requested',
                          'agent_runtime_message_received', 'agent_runtime_message_acknowledged'}:
                if task_id not in tasks:
                    raise _error('task event has no owning task')
                task = tasks[task_id]
                task.update(updated_at=event['at'], event_seq=event['seq'])
                if kind == 'agent_runtime_task_status':
                    task.update(status=event['status'], reason=event.get('reason', ''),
                                artifact_receipt=deepcopy(event.get('artifact_receipt')))
                elif kind == 'agent_runtime_stop_requested':
                    task['stop_requested'] = True
                    task['stop_reason'] = event.get('reason', '')
                elif kind == 'agent_runtime_message_received':
                    task['messages'].append({**deepcopy(event['message']),
                                             'received_at': event['at'],
                                             'acknowledged': False})
                else:
                    message = next((m for m in task['messages']
                                    if m['message_id'] == event['message_id']), None)
                    if message is None:
                        raise _error('acknowledgement has no inbox message')
                    message.update(acknowledged=True, acknowledged_at=event['at'])
        return tasks

    def _require(self, task_id, tasks=None):
        task = (tasks if tasks is not None else self._tasks()).get(task_id)
        if task is None:
            raise _error('agent task does not exist in this run')
        return task

    def _append(self, kind, task_id, **fields):
        return self.store.append_event(self.run_id, kind, task_id=task_id, **fields)

    def create_task(self, task_id, *, role, stage=None, parent_task_id=None,
                    parent_job_id=None, source_message_id=None, context_refs=None,
                    allowed_tools=(), expected_result=None, session_id=None):
        """Create once with the original job/attempt identity, or return its receipt."""
        _identity(task_id, 'task_id')
        for name, value in (('parent_task_id', parent_task_id),
                            ('parent_job_id', parent_job_id),
                            ('source_message_id', source_message_id)):
            _identity(value, name, optional=True)
        if role not in {'orchestrator', 'director', 'material', 'writer'}:
            raise _error('unknown active agent role')
        if stage is not None and (stage not in STAGE_SPEC or STAGE_SPEC[stage].owner_role != role):
            raise _error('task role does not own its stage')
        if stage is None and role != 'orchestrator':
            raise _error('a specialist task must own a stage')
        if not isinstance(allowed_tools, (list, tuple, set, frozenset)) or any(
                not isinstance(name, str) for name in allowed_tools):
            raise _error('allowed_tools must be a collection of tool names')
        allowed = sorted(set(allowed_tools))
        # Local-revision tasks may expose submit_patch instead of submit_stage.
        capabilities = set(tool_names_for(role)) | set(tool_names_for(role, local_revision=True))
        if any(not isinstance(name, str) or name not in capabilities for name in allowed):
            raise _error('task tools exceed its role capabilities')
        expectation = dict(expected_result) if isinstance(expected_result, dict) else expected_result or {}
        if not isinstance(expectation, dict) or set(expectation) - {'artifact_stage', 'user_decision_required'}:
            raise _error('expected_result accepts only artifact-stage contract metadata')
        if expectation.get('artifact_stage', stage) != stage:
            raise _error('expected artifact must match the task stage')
        if stage is not None:
            gated = STAGE_SPEC[stage].gated
            if ('user_decision_required' in expectation
                    and expectation['user_decision_required'] is not gated):
                raise _error('task contract cannot change the stage user-decision requirement')
            expectation.update(artifact_stage=stage, user_decision_required=gated)
        record = {'task_id': task_id, 'role': role, 'stage': stage,
                  'parent_task_id': parent_task_id, 'parent_job_id': parent_job_id,
                  'source_message_id': source_message_id,
                  'context_refs': _references(context_refs or {}),
                  'allowed_tools': allowed, 'expected_result': deepcopy(expectation)}
        if session_id is not None:
            _identity(session_id, 'session_id')
            record['session_id'] = session_id
        with self.store.run_lock(self.run_id):
            self.store.read_run(self.run_id)
            tasks = self._tasks()
            parent = self._require(parent_task_id, tasks) if parent_task_id else None
            if parent:
                if parent_job_id is None:
                    record['parent_job_id'] = parent['parent_job_id']
                if source_message_id is None:
                    record['source_message_id'] = parent['source_message_id']
            if task_id in tasks:
                prior = tasks[task_id]
                if any(prior[key] != value for key, value in record.items()):
                    raise _error('task identity already belongs to another contract')
                return deepcopy(prior)
            if parent_task_id:
                if parent['status'] in TERMINAL or parent['stop_requested']:
                    raise _error('cannot delegate from a stopped or finished parent')
                if parent_job_id is not None and parent['parent_job_id'] != parent_job_id:
                    raise _error('child and parent must belong to the same job')
            if stage is not None:
                attempts = [e for e in self.store.read_events(self.run_id)
                            if e.get('kind') == 'attempt_started' and e.get('stage') == stage
                            and f"{stage}#{e.get('number')}" == task_id]
                if not attempts:
                    raise _error('stage task must reuse its existing Pipeline attempt identity')
            self._append('agent_runtime_task_created', task_id, task=record)
            return deepcopy(self._require(task_id))

    def _artifact(self, task, receipt):
        if receipt is None:
            if task['stage'] is not None:
                raise _error('completed stage task requires an exact saved artifact receipt')
            return None
        if not isinstance(receipt, dict):
            raise _error('artifact receipt must be an object')
        stage = receipt.get('stage', task['stage'])
        version, sha256 = receipt.get('version'), receipt.get('sha256')
        if (stage not in STAGE_SPEC or type(version) is not int or version < 1
                or not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256)
                or (task['stage'] is not None and stage != task['stage'])
                or receipt.get('key', f'{stage}-v{version}') != f'{stage}-v{version}'):
            raise _error('invalid artifact receipt identity')
        artifact = self.store.read_artifact(self.run_id, stage, version)
        if (artifact.sha256 != sha256 or not self.store.verify_artifact(self.run_id, stage, version)
                or (task['stage'] is not None and artifact.attempt != task['task_id'])):
            raise _error('artifact receipt does not match this task output')
        return {'stage': stage, 'version': version, 'sha256': sha256,
                'key': f'{stage}-v{version}'}

    def transition(self, task_id, status, *, reason='', artifact_receipt=None):
        if status not in STATUSES or not isinstance(reason, str):
            raise _error('invalid task status or reason')
        with self.store.run_lock(self.run_id):
            task = self._require(task_id)
            receipt = self._artifact(task, artifact_receipt) if status == 'completed' else None
            if status != 'completed' and artifact_receipt is not None:
                raise _error('only completed tasks carry an artifact receipt')
            if task['status'] == status:
                if task['reason'] != reason or task['artifact_receipt'] != receipt:
                    raise _error('repeated task transition has conflicting result')
                return deepcopy(task)
            if status not in TRANSITIONS.get(task['status'], set()):
                raise _error('task transition is not allowed; terminal tasks never restart')
            if task['stop_requested'] and status in {'running', 'waiting', 'completed'}:
                raise _error('stopped task cannot continue or commit a late completion')
            if status == 'completed' and any(
                    child['parent_task_id'] == task_id and child['status'] not in TERMINAL
                    for child in self._tasks().values()):
                raise _error('parent cannot complete while an owned child task is still open')
            self._append('agent_runtime_task_status', task_id, status=status,
                         reason=reason, artifact_receipt=receipt)
            return deepcopy(self._require(task_id))

    def request_stop(self, task_id, *, reason='User requested stop'):
        """Persist stop intent; the executor acknowledges actual cancellation later."""
        with self.store.run_lock(self.run_id):
            task = self._require(task_id)
            if task['status'] in TERMINAL or task['stop_requested']:
                return deepcopy(task)
            self._append('agent_runtime_stop_requested', task_id, reason=reason)
            for child in self._tasks().values():
                if child['parent_task_id'] == task_id:
                    self.request_stop(child['task_id'], reason=reason)
            return deepcopy(self._require(task_id))

    def post_message(self, task_id, message_id, text, *, source_message_id=None,
                     source_task_id=None, kind='user'):
        """Queue data for this task; messages never grant approval or execution rights."""
        _identity(message_id, 'message_id')
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise _error('inbox message must contain 1 to 12000 characters')
        if kind not in {'user', 'parent', 'child'}:
            raise _error('unknown inbox message kind')
        _identity(source_message_id, 'source_message_id', optional=True)
        _identity(source_task_id, 'source_task_id', optional=True)
        message = {'message_id': message_id, 'text': text, 'kind': kind,
                   'source_message_id': source_message_id, 'source_task_id': source_task_id}
        with self.store.run_lock(self.run_id):
            task = self._require(task_id)
            prior = next((m for m in task['messages'] if m['message_id'] == message_id), None)
            if prior:
                if any(prior[key] != value for key, value in message.items()):
                    raise _error('message identity already belongs to different input')
                return deepcopy(prior)
            if task['status'] in TERMINAL or task['stop_requested']:
                raise _error('finished or stopped tasks cannot accept new messages')
            if source_task_id:
                source = self._require(source_task_id)
                if not ((kind == 'parent' and task['parent_task_id'] == source_task_id)
                        or (kind == 'child' and source['parent_task_id'] == task_id)):
                    raise _error('agent messages must follow direct parent-child ownership')
            elif kind != 'user':
                raise _error('agent message requires its owning source task')
            self._append('agent_runtime_message_received', task_id, message=message)
            return deepcopy(self._require(task_id)['messages'][-1])

    def ack_message(self, task_id, message_id):
        with self.store.run_lock(self.run_id):
            task = self._require(task_id)
            message = next((m for m in task['messages'] if m['message_id'] == message_id), None)
            if message is None:
                raise _error('message does not belong to this task')
            if not message['acknowledged']:
                if task['status'] in TERMINAL or task['stop_requested']:
                    raise _error('stopped tasks cannot consume pending messages')
                self._append('agent_runtime_message_acknowledged', task_id, message_id=message_id)
            return deepcopy(next(m for m in self._require(task_id)['messages']
                                 if m['message_id'] == message_id))

    def pending_messages(self, task_id):
        with self.store.run_lock(self.run_id):
            return deepcopy([m for m in self._require(task_id)['messages'] if not m['acknowledged']])

    def task(self, task_id):
        with self.store.run_lock(self.run_id):
            return deepcopy(self._require(task_id))

    def tasks(self):
        with self.store.run_lock(self.run_id):
            return deepcopy(list(self._tasks().values()))

    def snapshot(self):
        """Public read projection omits inbox text and all private request bodies."""
        tasks = self.tasks()
        for task in tasks:
            task['pending_message_count'] = sum(not m['acknowledged'] for m in task['messages'])
            task['messages'] = [{k: v for k, v in message.items() if k != 'text'}
                                for message in task['messages']]
        return {'run_id': self.run_id, 'tasks': tasks}

    def interrupt_open_tasks(self, *, parent_job_id=None,
                             reason='Service restarted; explicit continuation required'):
        """Recovery records interruption only; it never replays a model or inbox."""
        with self.store.run_lock(self.run_id):
            for task in self._tasks().values():
                if (task['status'] not in TERMINAL
                        and (parent_job_id is None or task['parent_job_id'] == parent_job_id)):
                    self.transition(task['task_id'], 'interrupted', reason=reason)
            return self.snapshot()
