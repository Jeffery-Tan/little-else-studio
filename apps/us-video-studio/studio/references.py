"""User-selected creative references, derived solely from run events."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def selections(store, run_id):
    chosen = {}
    for event in store.read_events(run_id):
        if event['kind'] == 'reference_selected':
            chosen[event['selection']['case_id']] = event['selection']
        elif event['kind'] == 'reference_removed':
            chosen.pop(event['case_id'], None)
    return [chosen[key] for key in sorted(chosen)]


def reference_fingerprint(store, run_id):
    events = store.read_events(run_id)
    if not any(e['kind'] in ('reference_selected', 'reference_removed') for e in events):
        return None
    chosen = selections(store, run_id)
    service = getattr(store, 'reference_learning', None)
    if chosen and service is not None:
        try:
            _, doc = service._read()
            current = {c['id']: fingerprint(c) for c in doc['cases']}
        except (OSError, ValueError):
            current = {}
        # A changed or unavailable source invalidates approval of its old
        # dependents; the saved choice still binds the original exact content.
        return fingerprint([dict(s, current_source_sha256=current.get(s['case_id'])) for s in chosen])
    return fingerprint(chosen)


#: Append-only kinds that advance the reference generation (CLAUDE.md red line 7).
#: ``learning_applied`` is included because applying a proposal can change the
#: source case content folded into ``reference_fingerprint`` without a new
#: select/remove — same freeze-bump rule as hand-editing observations.
REFERENCE_GENERATION_KINDS = frozenset({'reference_selected', 'reference_removed', 'learning_applied'})


def reference_generation(store, run_id):
    """Highest event ``seq`` among reference choices, or 0 if none yet."""
    generation = 0
    for event in store.read_events(run_id):
        if event.get('kind') not in REFERENCE_GENERATION_KINDS:
            continue
        seq = event.get('seq')
        if isinstance(seq, int) and seq > generation:
            generation = seq
    return generation


class SelectIn(BaseModel):
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,100}$')
    case_id: str = Field(min_length=1, max_length=160)
    case_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    purpose: Literal['message', 'storyboard', 'performance']
    expression: str = Field(min_length=1, max_length=2000)
    expected_snapshot: str | None


class RemoveIn(BaseModel):
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,100}$')
    expected_snapshot: str | None


def reference_state(learning, run_id):
    _, doc = learning._read()
    current = {c['id']: c for c in doc['cases']}
    items = []
    for selection in selections(learning.store, run_id):
        case = current.get(selection['case_id'])
        status = ('missing' if case is None else
                  'changed' if fingerprint(case) != selection['case_sha256'] else 'ready')
        items.append({**selection, 'status': status})
    return {'snapshot': reference_fingerprint(learning.store, run_id), 'references': items}


def change_reference(learning, run_id, payload, *, remove_case=None):
    from .learning import core
    store = learning.store
    with store.run_lock(run_id):
        store.read_run(run_id)
        identity = fingerprint({'remove_case': remove_case, **payload.model_dump()})
        for event in store.read_events(run_id):
            if event['kind'] in ('reference_selected', 'reference_removed') and event.get('request_id') == payload.request_id:
                if event.get('request_sha256') != identity:
                    raise ValueError('这次保存编号已用于其他修改，请重新操作。')
                return reference_state(learning, run_id)
        if reference_fingerprint(store, run_id) != payload.expected_snapshot:
            raise ValueError('项目参考已被修改，请刷新后重新选择。')
        if remove_case is not None:
            if not any(s['case_id'] == remove_case for s in selections(store, run_id)):
                raise ValueError('这条参考已经不在当前项目中。')
            store.append_event(run_id, 'reference_removed', actor='user', case_id=remove_case,
                               request_id=payload.request_id, request_sha256=identity)
        else:
            _, doc = learning._read()
            case = next((c for c in doc['cases'] if c['id'] == payload.case_id), None)
            if case is None or fingerprint(case) != payload.case_sha256:
                raise ValueError('参考内容已变化，请重新打开案例，核对后再选择。')
            if case.get('partition') != 'calibration' or not case.get('eligible_for'):
                raise ValueError('这条参考目前仅供阅读，还没有用于创作的观察资格。')
            core.creator_library(doc, {case['id']})
            if payload.purpose not in case.get('eligible_for', []):
                raise ValueError('这条参考没有所选用途的证据资格。')
            if not payload.expression.strip():
                raise ValueError('请写明这次希望借鉴的具体表达。')
            selection = {key: getattr(payload, key) for key in ('case_id', 'case_sha256', 'purpose', 'expression')}
            selection['title'] = case['title']
            store.append_event(run_id, 'reference_selected', actor='user', selection=selection,
                               request_id=payload.request_id, request_sha256=identity)
        return reference_state(learning, run_id)


def install_reference_routes(app, jobs, require_run, learning):
    @app.get('/api/runs/{run_id}/references')
    def get_references(run_id: str):
        require_run(run_id)
        with jobs.store.run_lock(run_id):
            return reference_state(learning, run_id)

    def change(run_id, payload, remove_case=None):
        require_run(run_id)
        with jobs.lock, jobs.store.run_lock(run_id):
            if jobs.active is not None:
                raise HTTPException(409, '请等待当前任务结束，再调整项目参考。')
            if any(s.open_attempt for s in jobs.pipeline.state(run_id).stages.values()):
                raise HTTPException(409, '项目还有未完成的创作，请先完成或结束该轮任务。')
            try:
                return change_reference(learning, run_id, payload, remove_case=remove_case)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/references')
    def select_reference(run_id: str, payload: SelectIn):
        return change(run_id, payload)

    @app.post('/api/runs/{run_id}/references/{case_id}/remove')
    def remove_reference(run_id: str, case_id: str, payload: RemoveIn):
        return change(run_id, payload, remove_case=case_id)
