"""Private, durable role conversations within the existing run Store.

Public events contain identities only. The private journal stores model-visible
steps, not client objects, credentials, wire responses or private reasoning.
Reading/reconstructing a session never runs a model or replays a tool.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid

from .models import PipelineError, utcnow
from .store import _append_jsonl, _atomic_write_text, _read_jsonl


TERMINAL = {'completed', 'cancelled', 'failed', 'interrupted', 'waiting'}
ROLE_RESUME_TEXT = '继续这项未完成的创作任务，保留已保存内容并先核对当前依据。'
_SECRET_KEYS = {'api_key', 'authorization', 'access_token', 'refresh_token',
                'protected_key', 'password', 'credentials', 'token', 'nonce'}
_PRIVATE_REASONING_BLOCKS = {'thinking', 'redacted_thinking', 'deepseek_reasoning'}


def _closed_input_reason(row, *, pending=False):
    receipt = row.get('artifact_receipt')
    if receipt:
        from .vocabulary import stage_label
        handling = '此前收到的补充尚未处理' if pending else '补充未投递'
        return (f"{stage_label(receipt['stage'])}第 {receipt['version']} 版已保存，{handling}。原话已保存。"
                '请阅读当前保存版本，在新稿上重新选择具体位置并发送修改意见；旧任务不会重放。')
    return '这项角色任务已经结束，补充未投递。原话已保存；请刷新后重新选择要处理的对象。'


def _public_blocks(blocks):
    """Copy logical blocks for persistence without changing the live protocol."""
    public = [copy.deepcopy(block) for block in blocks
              if block.get('type') not in _PRIVATE_REASONING_BLOCKS]
    return public, len(public) != len(blocks)


def _public_messages(messages):
    public = copy.deepcopy(messages)
    omitted = False
    for message in public:
        if isinstance(message.get('content'), list):
            message['content'], removed = _public_blocks(message['content'])
            omitted = omitted or removed
        if message.get('role') == 'assistant' and 'reasoning_content' in message:
            del message['reasoning_content']
            omitted = True
    return public, omitted


def _clean(value):
    if isinstance(value, dict):
        return {key: ('[redacted]' if key.lower() in _SECRET_KEYS else _clean(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'(?i)\bBearer\s+[^\s"\']+|\bsk-[A-Za-z0-9_-]{16,}', '[redacted]', value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError('Session journal only accepts logical JSON data')


class AgentSessions:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def _path(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[a-f0-9]{32}', session_id):
            raise PipelineError('agent_session', 'Invalid role session identity')
        root = self.store.run_dir(self.run_id).resolve()
        path = root / 'agent-sessions' / session_id
        if not path.resolve().is_relative_to(root):
            raise PipelineError('agent_session', 'Session directory is outside this run')
        return path

    def _read(self, session_id):
        path = self._path(session_id) / 'session.json'
        if not path.is_file():
            raise PipelineError('agent_session', 'Role session does not exist in this run')
        row = json.loads(path.read_text(encoding='utf-8'))
        if row['run_id'] != self.run_id or row['session_id'] != session_id:
            raise PipelineError('agent_session', 'Role session identity mismatch')
        return row

    def _save(self, row):
        row['updated_at'] = utcnow()
        _atomic_write_text(self._path(row['session_id']) / 'session.json',
                           json.dumps(_clean(row), ensure_ascii=False, sort_keys=True))

    def ensure(self, *, role, stage, session_id=None, source_message_id=None,
               parent_session_id=None, parent_job_id=None, contract=None):
        from .models import STAGE_SPEC
        if stage not in STAGE_SPEC or STAGE_SPEC[stage].owner_role != role or role == 'orchestrator':
            raise PipelineError('agent_session', 'Session must belong to its specialist stage')
        with self.store.run_lock(self.run_id):
            self.store.read_run(self.run_id)
            if session_id:
                row = self._read(session_id)
                if (row['role'], row['stage']) != (role, stage):
                    raise PipelineError('agent_session', 'A session cannot change its role or stage')
                return self.snapshot(session_id)
            session_id = uuid.uuid4().hex
            row = {'schema_version': 1, 'run_id': self.run_id, 'session_id': session_id,
                   'role': role, 'stage': stage, 'status': 'idle', 'turn': 0,
                   'task_id': None, 'source_message_id': source_message_id,
                   'parent_session_id': parent_session_id, 'parent_job_id': parent_job_id, 'contract': contract or {},
                   'stop_requested': False, 'input_closed': False, 'artifact_receipt': None,
                   'reason': '', 'created_at': utcnow()}
            self._path(session_id).mkdir(parents=True)
            self._save(row)
            self.store.append_event(self.run_id, 'agent_session_created', session_id=session_id,
                role=role, stage=stage, source_message_id=source_message_id)
            return self.snapshot(session_id)

    def history(self, session_id):
        """Private diagnostic/recovery API; do not expose this via public events."""
        with self.store.run_lock(self.run_id):
            self._read(session_id)
            return _read_jsonl(self._path(session_id) / 'journal.jsonl')

    def read_role_history(self, session_id, *, entry_seq=None, before_seq=None,
                          offset=0, limit=6000):
        """Bounded logical results from this role only, never requests/reasoning."""
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 12000
                or entry_seq is not None and (type(entry_seq) is not int or entry_seq < 1)
                or before_seq is not None and (type(before_seq) is not int or before_seq < 1)
                or entry_seq is not None and before_seq is not None):
            raise PipelineError('role_history', '历史游标无效，请使用返回的准确序号和偏移。')
        entries = []
        for event in self.history(session_id):
            if event['kind'] == 'tool_result':
                value = event['output']
                body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            elif event['kind'] == 'model_response' and event.get('text'):
                body = event['text']
            else:
                continue
            entries.append({'seq': event['seq'], 'kind': event['kind'],
                            'tool': event.get('name'), 'step_id': event.get('step_id'),
                            'is_error': bool(event.get('is_error')), 'text': body,
                            'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()})
        authority = 'historical_context_only_no_tool_replay_or_new_authorization'
        if entry_seq is not None:
            entry = next((row for row in entries if row['seq'] == entry_seq), None)
            if entry is None or offset > len(entry['text']):
                raise PipelineError('role_history', '该历史结果不存在或正文偏移无效。')
            body = entry['text'][offset:offset + limit]
            return {**{key: value for key, value in entry.items() if key != 'text'},
                    'text': body, 'offset': offset, 'total_chars': len(entry['text']),
                    'next_offset': offset + len(body) if offset + len(body) < len(entry['text']) else None,
                    'authority': authority}
        eligible = [row for row in entries if before_seq is None or row['seq'] < before_seq]
        page = eligible[-12:]
        return {'entries': [{**{key: value for key, value in row.items() if key != 'text'},
                             'excerpt': row['text'][:160], 'total_chars': len(row['text'])}
                            for row in page],
                'next_before': page[0]['seq'] if len(eligible) > len(page) else None,
                'authority': authority}

    def append(self, session_id, kind, **fields):
        with self.store.run_lock(self.run_id):
            self._read(session_id)
            row = _clean({'seq': len(self.history(session_id)) + 1, 'at': utcnow(), 'kind': kind, **fields})
            _append_jsonl(self._path(session_id) / 'journal.jsonl', row)
            self.store.append_event(self.run_id, 'agent_session_step', session_id=session_id,
                step=row['seq'], step_kind=kind,
                sha256=hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            return row

    def claim(self, session_id, *, resume=False, source_message_id=None, parent_job_id=None):
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            if row['status'] in {'queued', 'running'}:
                raise PipelineError('session_busy', 'This session already has an exclusive executor')
            if row['status'] == 'cancelled' or row['stop_requested']:
                raise PipelineError('session_cancelled', 'Cancelled sessions do not resume; start a new task')
            if row['status'] == 'completed':
                raise PipelineError('session_completed', 'This session already submitted its result')
            if row['turn'] and not resume:
                raise PipelineError('session_resume', 'Interrupted role work needs explicit continuation')
            row.update(status='queued', turn=row['turn'] + 1, reason='', task_id=None, input_closed=False)
            if source_message_id:
                row['source_message_id'] = source_message_id
            if parent_job_id:
                row['parent_job_id'] = parent_job_id
            self._save(row)
            self.append(session_id, 'turn_started', turn=row['turn'], explicit_resume=resume,
                        source_message_id=row['source_message_id'])
            return row

    def bind_attempt(self, session_id, task_id):
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            if row['stop_requested']:
                from .agents.client import JobCancelled
                raise JobCancelled('Role session stopped before attempt execution')
            row.update(task_id=task_id, status='running')
            self._save(row)
            self.append(session_id, 'attempt_bound', turn=row['turn'], task_id=task_id)

    def finish(self, session_id, status, *, reason='', artifact_receipt=None):
        if status not in TERMINAL:
            raise PipelineError('agent_session', 'Invalid session outcome')
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            row.update(status='cancelled' if row['stop_requested'] else status,
                       reason=reason, artifact_receipt=artifact_receipt or row.get('artifact_receipt'))
            self._save(row)
            self.append(session_id, 'turn_finished', turn=row['turn'], status=row['status'],
                        reason=reason, artifact_receipt=artifact_receipt)
            return self.snapshot(session_id)

    def commit_artifact(self, session_id, artifact_receipt):
        """Close input in the same run lock that persisted this attempt's output.

        The worker remains running until its transport and runtime settle. A
        saved artifact nevertheless cannot accept another message for the old
        submission, or silently consume that message on cached resume.
        """
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            artifact = self.store.read_artifact(self.run_id, row['stage'], artifact_receipt['version'])
            if (artifact.stage != row['stage'] or artifact.attempt != row['task_id']
                    or artifact.sha256 != artifact_receipt['sha256']
                    or not self.store.verify_artifact(self.run_id, row['stage'], artifact.version)):
                raise PipelineError('agent_session', 'Saved output does not match this role attempt')
            if self.pending(session_id):
                raise PipelineError('session_new_baseline', _closed_input_reason({**row,
                    'artifact_receipt': artifact_receipt}))
            if row.get('input_closed'):
                if row.get('artifact_receipt') != artifact_receipt:
                    raise PipelineError('agent_session', 'Role output receipt already belongs to another version')
                return self.snapshot(session_id)
            row.update(input_closed=True, artifact_receipt=copy.deepcopy(artifact_receipt))
            self._save(row)
            self.append(session_id, 'artifact_committed', turn=row['turn'], artifact_receipt=artifact_receipt)
            return self.snapshot(session_id)

    def closed_input_reason(self, session_id):
        with self.store.run_lock(self.run_id):
            return _closed_input_reason(self.snapshot(session_id))

    def bind_resume_action(self, session_id, receipt, *, expected_task_id):
        """Bind the exact public button action to its real receipt and task.

        Arbitrary text at the resume endpoint is not an execution decision. It
        needs the normal producer's semantic interpretation before resuming.
        """
        from .conversation_input import message
        with self.store.run_lock(self.run_id):
            trusted = message(self.store, self.run_id, receipt['message_id'])
            row = self.snapshot(session_id)
            if (trusted['text'] != ROLE_RESUME_TEXT or not expected_task_id
                    or row.get('task_id') != expected_task_id):
                raise PipelineError('session_input_scope',
                    '继续按钮须绑定当前任务和固定继续文案，不能代替自定义要求的解释。原话已保存；请通过普通对话发送。')
            if row.get('artifact_receipt'):
                raise PipelineError('session_new_baseline', _closed_input_reason(row,
                    pending=bool(self.pending(session_id))))
            if (row['status'] not in {'waiting', 'failed', 'interrupted'}
                    or row['stop_requested'] or row.get('input_closed') or row.get('artifact_receipt')):
                raise PipelineError('session_input_scope', '当前角色任务不能按旧继续按钮恢复，请刷新后核对当前稿。')
            fields = {'session_id': session_id, 'task_id': expected_task_id,
                      'source_message_id': trusted['message_id'], 'action': 'continue'}
            if not any(e['kind'] == 'role_resume_action_bound'
                       and all(e.get(key) == value for key, value in fields.items())
                       for e in self.store.read_events(self.run_id)):
                self.store.append_event(self.run_id, 'role_resume_action_bound', **fields)

    def require_resume_input(self, session_id, receipt, *, revision_scope=None):
        """An entry point is not permission to execute a read-only real input."""
        from .conversation_input import message, interpretation_for, interpreted_task_constraints
        trusted = message(self.store, self.run_id, receipt['message_id'])
        interpretation = interpretation_for(self.store, self.run_id, trusted['message_id'])
        limits = interpreted_task_constraints(trusted, interpretation)
        if limits['readonly']:
            raise PipelineError('session_input_scope',
                '本条消息要求停止、查询或只读讨论，未启动角色任务。原话已保存；请通过普通对话发送，由主制片按原话处理。')
        row = self.snapshot(session_id)
        if 'allowed_stages' in limits and row['stage'] not in limits['allowed_stages']:
            raise PipelineError('session_input_scope', '本条真实消息不允许恢复这一阶段，请通过普通对话核对任务范围。')
        if limits['local_only'] and (self.contract(session_id).get('revision_scope') or {}).get('mode') != 'local':
            raise PipelineError('session_input_scope', '本条消息只要求局部修改，不能恢复整稿任务；请在当前稿上选择准确修改位置。')
        if row.get('artifact_receipt'):
            raise PipelineError('session_new_baseline', _closed_input_reason(row,
                pending=bool(self.pending(session_id))))
        button_bound = trusted['text'] == ROLE_RESUME_TEXT and any(
            e['kind'] == 'role_resume_action_bound' and e.get('session_id') == session_id
            and e.get('task_id') == row.get('task_id')
            and e.get('source_message_id') == trusted['message_id'] and e.get('action') == 'continue'
            for e in self.store.read_events(self.run_id))
        if not interpretation and not button_bound:
            raise PipelineError('session_input_scope',
                '本条自定义消息尚未解释，未启动角色任务。原话已保存；请通过普通对话发送，由主制片核对任务与修改范围。')
        if interpretation and not button_bound:
            self._require_compatible_resume_scope(session_id, trusted, interpretation, revision_scope)
        return trusted

    def _require_compatible_resume_scope(self, session_id, receipt, interpretation, proof):
        """A new task cannot borrow an old field plan just by choosing resume."""
        old = self.contract(session_id).get('revision_scope')
        goal = interpretation['task']['deliverable']
        if old and old['mode'] == 'local' and goal != 'revision':
            raise PipelineError('session_input_scope',
                '旧任务仅修改固定局部字段，不能交付本轮完整新稿；请按本轮要求建立新任务。')
        if goal != 'revision':
            return
        if not old:
            raise PipelineError('session_input_scope', '本轮修订不能恢复旧整稿任务；请先建立准确的新修订计划。')

        def same_scope(candidate):
            return (all(candidate.get(key) == old.get(key) for key in ('stage', 'mode', 'version', 'sha256', 'inputs'))
                    and sorted(json.dumps(f['path']) for f in candidate.get('fields', []))
                    == sorted(json.dumps(f['path']) for f in old.get('fields', [])))

        rows = self.store.read_events(self.run_id)
        turns = {e['turn_id'] for e in rows if e['kind'] == 'agent_turn_scope_started'
                 and e.get('user_input_id') == receipt['message_id']}
        plans = [e['scope'] for e in rows if e['kind'] == 'agent_revision_planned'
                 and e['scope'].get('turn_id') in turns and e['scope'].get('quote')
                 and e['scope']['quote'] in receipt['text']]
        if proof is not None and proof not in plans:
            raise PipelineError('session_input_scope', '恢复所用修订计划没有绑定本条真实原话；请重新核对准确范围。')
        chosen = proof if proof is not None else plans[-1] if plans else None
        if chosen is not None and not same_scope(chosen):
            raise PipelineError('session_input_scope', '本轮修订计划与旧角色的版本或字段不同；请按新计划派工，不能恢复旧任务。')

        target = receipt.get('feedback_target') or {}
        if target.get('path'):
            from .pipeline import Pipeline
            from .revision_scope import validate_writing_target
            validate_writing_target(self.store, Pipeline(self.store), self.run_id, target)
            if (old['mode'] != 'local' or target.get('stage') != old['stage']
                    or target.get('version') != old['version'] or target.get('artifact_sha256') != old['sha256']
                    or [f['path'] for f in old['fields']] != [target['path']]):
                raise PipelineError('session_input_scope', '本条反馈所选版本或字段与旧角色任务不同；请按新位置建立修订任务。')
        elif chosen is None:
            raise PipelineError('session_input_scope',
                '本条修订尚未绑定准确版本和字段，不能从旧任务猜测范围；请在当前稿上选择位置，或由主制片建立新计划。')

    def _receipt_for_attempt(self, row):
        """Project saved output after an old crash without inventing events."""
        if row.get('artifact_receipt'):
            return row['artifact_receipt']
        if row.get('task_id'):
            artifact = next((a for a in self.store.list_artifacts(self.run_id, row['stage'])
                             if a.attempt == row['task_id']), None)
            if artifact and self.store.verify_artifact(self.run_id, artifact.stage, artifact.version):
                return {'stage': artifact.stage, 'version': artifact.version, 'sha256': artifact.sha256}
        return None

    def post_message(self, session_id, message_id, text, *, source_message_id=None):
        if not isinstance(message_id, str) or not message_id or not isinstance(text, str) or not text.strip():
            raise PipelineError('agent_session', 'Inbox message requires an identity and text')
        if len(text) > 12000:
            raise PipelineError('agent_session', 'Inbox message is too long')
        with self.store.run_lock(self.run_id):
            row = self.snapshot(session_id)
            if source_message_id:
                from .conversation_input import message
                if message(self.store, self.run_id, source_message_id)['text'] != text:
                    raise PipelineError('agent_session', 'Inbox text must match its trusted user receipt')
            prior = next((item for item in self.history(session_id) if item['kind'] == 'inbox_received'
                          and item['message_id'] == message_id), None)
            payload = {'message_id': message_id, 'text': text, 'source_message_id': source_message_id}
            if prior:
                if any(prior.get(key) != value for key, value in payload.items()):
                    raise PipelineError('request_conflict', 'Inbox identity already belongs to other content')
                return {'message_id': message_id, 'accepted': True}
            if row.get('input_closed') or row['status'] in {'completed', 'cancelled'} or row['stop_requested']:
                raise PipelineError('session_closed', _closed_input_reason(row))
            self.append(session_id, 'inbox_received', **payload)
            return {'message_id': message_id, 'accepted': True}

    def pending(self, session_id):
        history = self.history(session_id)
        consumed = {e['message_id'] for e in history if e['kind'] == 'inbox_consumed'}
        return [e for e in history if e['kind'] == 'inbox_received' and e['message_id'] not in consumed]

    def consume(self, session_id, message_id):
        if any(row['message_id'] == message_id for row in self.pending(session_id)):
            self.append(session_id, 'inbox_consumed', message_id=message_id)

    def cancel(self, session_id, *, reason='User stopped this role session'):
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            if row['status'] in {'completed', 'cancelled'}:
                return self.snapshot(session_id)
            row.update(stop_requested=True, reason=reason)
            if row['status'] not in {'queued', 'running'}:
                row['status'] = 'cancelled'
            self._save(row)
            self.append(session_id, 'stop_requested', reason=reason)
            if row['task_id']:
                from .agent_runtime import AgentRuntime
                AgentRuntime(self.store, self.run_id).request_stop(row['task_id'], reason=reason)
            return self.snapshot(session_id)

    def check_cancel(self, session_id):
        from .agents.client import JobCancelled
        row = self._read(session_id)
        stopped = row['stop_requested']
        if row['task_id']:
            from .agent_runtime import AgentRuntime
            stopped = stopped or AgentRuntime(self.store, self.run_id).task(row['task_id'])['stop_requested']
        if stopped:
            raise JobCancelled('Role session cancelled')

    def snapshot(self, session_id=None):
        if session_id is None:
            ids = [e['session_id'] for e in self.store.read_events(self.run_id) if e['kind'] == 'agent_session_created']
            return {'run_id': self.run_id, 'sessions': [self.snapshot(identity) for identity in ids]}
        row = self._read(session_id)
        row['artifact_receipt'] = self._receipt_for_attempt(row)
        from .conversation_input import interpretation_for
        interpretation = interpretation_for(self.store, self.run_id, row.get('source_message_id')) if row.get('source_message_id') else None
        pending_count = len(self.pending(session_id))
        input_closed = bool(row.get('input_closed') or row.get('artifact_receipt'))
        return {**{key: value for key, value in row.items() if key != 'contract'},
                'input_closed': input_closed,
                'resume_command': ({'resume_action': 'continue', 'text': ROLE_RESUME_TEXT}
                    if row['status'] in {'waiting', 'failed', 'interrupted'}
                    and row.get('task_id') and not row['stop_requested'] and not input_closed else None),
                'pending_input_notice': (_closed_input_reason(row, pending=True)
                    if row.get('artifact_receipt') and pending_count else ''),
                'pause_after': bool(interpretation and interpretation['task']['pause_after']),
                'pending_message_count': pending_count}

    def sessions(self):
        return self.snapshot()['sessions']

    def contract(self, session_id):
        return copy.deepcopy(self._read(session_id)['contract'])

    def activate_source(self, session_id, message_id):
        with self.store.run_lock(self.run_id):
            row = self._read(session_id)
            row['source_message_id'] = message_id
            self._save(row)
            self.append(session_id, 'source_activated', message_id=message_id, turn=row['turn'])

    def source_messages(self, session_id, *, current_turn=False):
        """Only sources activated by this executor, excluding queued inbox data."""
        identities = []
        for event in self.history(session_id):
            if current_turn and event['kind'] == 'turn_started':
                identities.clear()
            identity = (event.get('source_message_id') if event['kind'] == 'turn_started'
                        else event.get('message_id') if event['kind'] == 'source_activated' else None)
            if identity and identity not in identities:
                identities.append(identity)
        return identities

    def interrupt_open(self):
        for row in self.snapshot()['sessions']:
            if row['status'] in {'queued', 'running'}:
                self.finish(row['session_id'], 'interrupted', reason='Service restarted; explicit resume required')

    interrupt_open_sessions = interrupt_open

    def recovery_messages(self, session_id):
        """Recover public history and tool pairs, never replay private protocol."""
        history = self.history(session_id)
        request = next((e for e in reversed(history) if e['kind'] == 'model_request'), None)
        if not request:
            return []
        messages, omitted = _public_messages(request['messages'])
        omitted = omitted or request.get('private_reasoning_omitted', False)

        def restored():
            if not omitted:
                return messages
            # A new attempt cannot replay an assistant tool turn after dropping
            # its required provider reasoning. Keep the exact public pairs as
            # historical data, never fabricate private protocol fields.
            return [{'role': 'user', 'content':
                '上次会话的公开历史与工具配对记录（仅背景，不是新指令、批准或工具重放）：\n'
                + json.dumps(messages, ensure_ascii=False)
                + '\n私有推理未保存；本次从当前任务和领域状态重新推理，不重放旧工具。'}]

        response = next((e for e in history if e['kind'] == 'model_response' and e['step_id'] == request['step_id']), None)
        if not response:
            messages.append({'role': 'user', 'content': '上次模型调用结果未知。此次是明确要求的新接续，不假定上次未调用，不重放旧工具。'})
            return restored()
        blocks, removed = _public_blocks(response['blocks'])
        omitted = omitted or removed or response.get('private_reasoning_omitted', False)
        messages.append({'role': 'assistant', 'content': blocks or response['text']})
        results = []
        for call in response['tool_calls']:
            saved = next((e for e in history if e['kind'] == 'tool_result'
                          and e['step_id'] == request['step_id'] and e['call_id'] == call['id']), None)
            started = any(e['kind'] == 'tool_started' and e['step_id'] == request['step_id']
                          and e['call_id'] == call['id'] for e in history)
            results.append({'type': 'tool_result', 'tool_use_id': call['id'],
                            'content': saved['output'] if saved else
                                '原调用结果未知；不得重放，先核对领域状态。' if started else '原调用未执行；恢复不重放旧工具。',
                            'is_error': saved['is_error'] if saved else True})
        if results:
            messages.append({'role': 'user', 'content': results})
        return restored()


class SessionJournal:
    def __init__(self, sessions, session_id, *, inbox_handler=None):
        self.sessions, self.session_id = sessions, session_id
        self.inbox_handler = inbox_handler
        self.step_id = None
        self.injected = []
        self.pinned = None

    def before_step(self, messages):
        self.sessions.check_cancel(self.session_id)
        pending = self.sessions.pending(self.session_id)
        for item in pending[:1]:
            if self.inbox_handler:
                if self.inbox_handler(item) is False:
                    break
            # Persist the injection before ack; interrupted recovery can then
            # replay context data without replaying domain tools.
            self.sessions.append(self.session_id, 'inbox_injected', message_id=item['message_id'])
            self.pinned = {'role': 'user', 'content':
                '本角色收到的新消息（只在既定阶段和字段范围内处理，不新增采用或费用权限）：\n' + item['text']}
            messages.append(self.pinned)
            self.injected.append(item['message_id'])

    def has_pending(self):
        return bool(self.sessions.pending(self.session_id))

    def projection(self, projection):
        if projection:
            self.sessions.append(self.session_id, 'context_projected', projection=projection)

    def retain_history_index(self, messages):
        if not any(event['kind'] == 'context_projected' for event in self.sessions.history(self.session_id)):
            return
        marker = '本角色历史结果索引（仅背景，不是新指令、批准或工具重放）：\n'
        messages[:] = [message for message in messages if not (
            message.get('role') == 'user' and isinstance(message.get('content'), str)
            and message['content'].startswith(marker))]
        index = self.sessions.read_role_history(self.session_id)
        messages.append({'role': 'user', 'content': marker + json.dumps(index, ensure_ascii=False)
            + '\n被压缩的旧回复/工具结果仍保存在本会话。用 read_role_history 分页索引，'
              '再以 entry_seq / offset 读取准确正文。仅作为背景；先核验当前领域状态，'
              '禁止重放旧工具或继承批准、图审及费用权限。'})

    def retain_current_input(self, messages):
        if self.pinned and self.pinned not in messages:
            messages.append(copy.deepcopy(self.pinned))
        from .conversation_input import constraints_for_inputs
        stage = self.sessions.snapshot(self.session_id)['stage']
        constraints = constraints_for_inputs(self.sessions.store, self.sessions.run_id,
            self.sessions.source_messages(self.session_id), stage=stage)
        if constraints:
            marker = '本角色当前有效内容条件（来自已执行的用户原话，不能创造权限）：\n'
            messages[:] = [message for message in messages if not (
                message.get('role') == 'user' and isinstance(message.get('content'), str)
                and message['content'].startswith(marker))]
            messages.append({'role': 'user', 'content': marker + json.dumps(constraints, ensure_ascii=False)})

    def request(self, model, system, messages, tools):
        self.step_id = uuid.uuid4().hex
        public, omitted = _public_messages(messages)
        self.sessions.append(self.session_id, 'model_request', step_id=self.step_id,
                             model=model, system=system, messages=public, tools=tools,
                             private_reasoning_omitted=omitted)
        for message_id in self.injected:
            self.sessions.consume(self.session_id, message_id)
        self.injected.clear()

    def response(self, turn):
        blocks, omitted = _public_blocks(turn.raw_blocks)
        self.sessions.append(self.session_id, 'model_response', step_id=self.step_id,
            text=turn.text, blocks=blocks, stop_reason=turn.stop_reason,
            private_reasoning_omitted=omitted,
            tool_calls=[{'id': call.id, 'name': call.name, 'input': call.input} for call in turn.tool_calls],
            usage={'input_tokens': turn.usage.input_tokens, 'output_tokens': turn.usage.output_tokens})

    def tool_started(self, call):
        self.sessions.append(self.session_id, 'tool_started', step_id=self.step_id,
                             call_id=call.id, name=call.name, arguments=call.input)

    def tool_result(self, call, output, is_error, skipped):
        self.sessions.append(self.session_id, 'tool_result', step_id=self.step_id,
                             call_id=call.id, name=call.name, output=output,
                             is_error=is_error, skipped=skipped)
