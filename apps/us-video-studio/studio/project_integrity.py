"""Read-only diagnosis of authoritative project files; never repair originals.

This is a projection of the existing files, not a quarantine database. Only
known persistence failures are classified here; unexpected program errors and
data-root/configuration failures continue to propagate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import MISSING, fields
from pathlib import Path

from .models import Artifact, Approval, LedgerEntry, PipelineError, Review, Run, STAGES, valid_run_id

_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


@contextmanager
def journal_lock(path: Path):
    # Do not acquire a run or JobManager lock here. Ledger already owns its
    # journal lock, and some callers own a run lock; reversing them deadlocks.
    key = os.path.normcase(str(path.resolve()))
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


class ProjectDataError(PipelineError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        issue = diagnostic['issues'][0]
        super().__init__('project_data', f"项目 {diagnostic['run_id']} 的资料无法读取："
                         f"{issue['file']}（{issue['category']}）。原件已保留，此项目暂停变更和调用。")


def _shape(record, model=None, required=()):
    if not isinstance(record, dict):
        return False
    if model:
        definitions = fields(model)
        names = {f.name for f in definitions}
        required = {f.name for f in definitions if f.default is MISSING and f.default_factory is MISSING}
        if set(record) - names:
            return False
        for definition in definitions:
            if definition.name not in record:
                continue
            value, annotation = record[definition.name], str(definition.type)
            if annotation == 'str' and not isinstance(value, str):
                # The prior Store API persisted positive integer attempt labels
                # and read them unchanged. Keep that one historical shape;
                # new Store writes require a string and never rewrite originals.
                legacy_attempt = (model is Artifact and definition.name == 'attempt'
                                  and type(value) is int and value > 0)
                if not legacy_attempt:
                    return False
            if annotation.startswith('list[') and not isinstance(value, list):
                return False
            if annotation.startswith('dict[') and not isinstance(value, dict):
                return False
    return set(required) <= record.keys()


def inspect_project(root: Path, run_id: str) -> dict:
    if not valid_run_id(run_id):
        raise PipelineError('store', 'invalid run id')
    base = Path(root) / 'runs' / run_id
    issues, metadata = [], None

    def issue(path, category, raw=None, line=None, column=None):
        item = {'file': path.relative_to(base).as_posix(), 'category': category,
                'sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None,
                'bytes': len(raw) if raw is not None else None}
        if line is not None:
            item['line'] = line
        if column is not None:
            item['column'] = column
        issues.append(item)

    def read(path, *, model=None, required=(), journal=False, mandatory=False, text_only=False):
        with journal_lock(path):
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                if mandatory:
                    issue(path, 'missing_file')
                return None
            except (PermissionError, IsADirectoryError):
                issue(path, 'unreadable_file')
                return None
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                issue(path, 'invalid_utf8', raw)
                return None
            if text_only:
                return text
            records = text.splitlines() if journal else [text]
            parsed = []
            for index, text_row in enumerate(records, 1):
                if journal and not text_row.strip():
                    continue
                try:
                    row = json.loads(text_row)
                except json.JSONDecodeError as exc:
                    issue(path, 'invalid_json', raw,
                          index if journal else exc.lineno, exc.colno)
                    return None
                if not _shape(row, model, required):
                    issue(path, 'invalid_record', raw, index if journal else None)
                    return None
                # These are persistence identities/types used directly by readers,
                # not a second creative-content or historical approval validator.
                valid = True
                if model is Run:
                    valid = (all(isinstance(row.get(k), str) for k in
                                 ('run_id', 'title', 'brief', 'market', 'created_at'))
                             and row['run_id'] == run_id and row.get('kind', 'creative') in {'creative', 'learning'})
                elif model in (Artifact, Approval, Review):
                    valid = row['stage'] in STAGES
                    if model is Artifact:
                        valid = valid and type(row['version']) is int and row['version'] >= 1
                        if valid:
                            valid = (path.name == f"{row['stage']}-v{row['version']}.json"
                                     and row['filename'] == f"{row['stage']}-v{row['version']}.md")
                elif model is LedgerEntry:
                    valid = (row['run_id'] == run_id and isinstance(row['pricing_source'], str)
                             and all(v is None or type(v) is int for v in
                                     (row['input_tokens'], row['output_tokens']))
                             and (row['estimated_usd'] is None or
                                  (type(row['estimated_usd']) in (int, float) and math.isfinite(row['estimated_usd']))))
                elif path.name == 'events.jsonl':
                    valid = (type(row['seq']) is int and row['seq'] == len(parsed) + 1
                             and isinstance(row['kind'], str) and isinstance(row['at'], str))
                elif path.name == 'chat.jsonl':
                    valid = all(isinstance(row[k], str) for k in ('at', 'role', 'text'))
                elif path.name == 'session.json':
                    valid = (row['run_id'] == run_id and row['session_id'] == path.parent.name
                             and isinstance(row['status'], str) and isinstance(row['contract'], dict)
                             and type(row['turn']) is int and type(row['stop_requested']) is bool)
                if not valid:
                    issue(path, 'invalid_record', raw, index if journal else None)
                    return None
                parsed.append(row)
            return parsed if journal else parsed[0]

    metadata = read(base / 'run.json', model=Run, mandatory=True)
    events = []
    for filename, model, required in (
            ('events.jsonl', None, ('seq', 'at', 'kind')),
            ('chat.jsonl', None, ('at', 'role', 'text')),
            ('ledger.jsonl', LedgerEntry, ()), ('approvals.jsonl', Approval, ())):
        rows = read(base / filename, model=model, required=required, journal=True)
        if filename == 'events.jsonl':
            events = rows or []
    for folder, model in (('artifacts', Artifact), ('reviews', Review)):
        for path in sorted((base / folder).glob('*.json')):
            record = read(path, model=model)
            if record and model is Artifact:
                read(path.with_suffix('.md'), mandatory=True, text_only=True)
    session_paths = set((base / 'agent-sessions').glob('*/session.json'))
    for event in events:
        if event['kind'] == 'agent_session_created':
            identity = event.get('session_id')
            if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
                raw = (base / 'events.jsonl').read_bytes()
                issue(base / 'events.jsonl', 'invalid_record', raw, event['seq'])
            else:
                session_paths.add(base / 'agent-sessions' / identity / 'session.json')
    for path in sorted(session_paths):
        read(path, required=('run_id', 'session_id', 'status', 'role', 'stage', 'task_id',
                             'stop_requested', 'turn', 'contract'), mandatory=True)
        read(path.parent / 'journal.jsonl', journal=True, required=('kind',))
    return {'run_id': run_id, 'available': not issues, 'issues': issues,
            'ledger_complete': not any(i['file'] == 'ledger.jsonl' for i in issues),
            'metadata': metadata}


def project_catalog(root: Path) -> list[dict]:
    directory = Path(root) / 'runs'
    if not directory.exists():
        return []
    out = []
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and valid_run_id(entry.name) and any(entry.iterdir()):
            out.append(inspect_project(root, entry.name))
    return out


def require_healthy(root: Path, run_id: str) -> dict:
    diagnostic = inspect_project(root, run_id)
    if not diagnostic['available']:
        raise ProjectDataError(public_diagnostic(diagnostic))
    return diagnostic


def public_diagnostic(diagnostic: dict) -> dict:
    return {key: value for key, value in diagnostic.items() if key != 'metadata'}


def guard_project_write(path: Path):
    """Cover shared atomic/journal helpers, including role-session writes."""
    for parent in path.parents:
        if parent.parent.name == 'runs' and valid_run_id(parent.name):
            # Initial creation has no previous metadata to diagnose. Existing
            # directories with other durable data must never be overwritten.
            if parent.exists() and ((parent / 'run.json').exists() or any(parent.iterdir())):
                require_healthy(parent.parent.parent, parent.name)
            return
