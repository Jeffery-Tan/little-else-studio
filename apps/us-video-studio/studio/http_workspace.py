"""Canvas and annotation commands, sharing immutable objects and original jobs."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import PipelineError
from . import workspace_objects as objects


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Position(Input):
    x: float = Field(ge=-100000, le=100000)
    y: float = Field(ge=-100000, le=100000)


class Viewport(Position):
    zoom: float = Field(ge=.35, le=1.6)


class Group(Input):
    id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(min_length=1, max_length=100)
    node_ids: list[str] = Field(default_factory=list, max_length=500)


class Note(Input):
    id: str = Field(min_length=1, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    text: str = Field(min_length=1, max_length=2000)
    node_ids: list[str] = Field(default_factory=list, max_length=500)


class LayoutIn(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_revision: int = Field(ge=0, strict=True)
    view: Viewport
    positions: dict[str, Position] = Field(max_length=500)
    groups: list[Group] = Field(default_factory=list, max_length=100)
    notes: list[Note] = Field(default_factory=list, max_length=100)


class Region(Input):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode='after')
    def bounds(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError('批注区域超出图片。')
        return self


class AnnotationIn(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    target: dict[str, Any]
    text: str = Field(min_length=1, max_length=4000)
    region: Region | None = None
    at_seconds: float | None = Field(default=None, ge=0, le=3600)


class ExecuteIn(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    annotation_ids: list[str] = Field(min_length=1, max_length=24)
    expected_snapshot_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class CompareIn(Input):
    left: dict[str, Any]
    right: dict[str, Any]


def install_workspace_routes(app, store, pipeline, jobs, require_run, prepare_job):
    def guarded(run_id, action):
        require_run(run_id)
        try:
            with store.run_lock(run_id):
                return action()
        except FileNotFoundError as exc:
            raise HTTPException(409, '准确对象不存在，请重新选择。') from exc
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/canvas')
    def canvas(run_id: str):
        return guarded(run_id, lambda: objects.graph(store, pipeline, run_id))

    @app.get('/api/runs/{run_id}/tasks/{job_id}/usage')
    def task_usage(run_id: str, job_id: str):
        require_run(run_id)
        try:
            jobs.get(run_id, job_id)
            from .task_execution import summary
            return summary(store, jobs.ledger, run_id, job_id)
        except PipelineError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/canvas/layout')
    def save_layout(run_id: str, payload: LayoutIn):
        def save():
            body = payload.model_dump()
            if len(str(body)) > 200000:
                raise ValueError('画布布局过大，请减少笔记或分组。')
            prior = next((e for e in store.read_events(run_id) if e['kind'] == 'canvas_layout_saved'
                          and e['request_id'] == payload.request_id), None)
            fingerprint = objects.digest(body)
            if prior:
                if prior['payload_sha256'] != fingerprint:
                    raise ValueError('相同请求已经保存另一份布局，请保留原请求核对。')
                return prior['layout']
            current = objects.layout(store, run_id)
            if current['revision'] != payload.expected_revision:
                raise ValueError('画布布局已在其他页面更新；本页未保存的布局仍可保留，请重新读取后决定。')
            value = {key: body[key] for key in ('view', 'positions', 'groups', 'notes')}
            value['revision'] = current['revision'] + 1
            store.append_event(run_id, 'canvas_layout_saved', request_id=payload.request_id,
                               payload_sha256=fingerprint, layout=value)
            return value
        return guarded(run_id, save)

    @app.post('/api/runs/{run_id}/objects/compare')
    def compare(run_id: str, payload: CompareIn):
        return guarded(run_id, lambda: objects.compare(store, pipeline, run_id, payload.left, payload.right))

    @app.get('/api/runs/{run_id}/annotations')
    def annotations(run_id: str):
        def project():
            snapshot = objects.annotations(store, run_id)
            for annotation in snapshot['items']:
                try:
                    item = objects.read_object(store, pipeline, run_id, annotation['target'])
                    annotation['executable'] = annotation['state'] == 'saved' and 'revise' in item['allowed_actions']
                    if not annotation['executable']:
                        annotation['reason'] = ('已提交，请查询原任务。' if annotation['state'] != 'saved' else
                            '该对象只供阅读或已成为历史；修改请在当前方案或制作输入重新批注。')
                except (ValueError, PipelineError, FileNotFoundError):
                    annotation.update(executable=False, reason='准确来源已变化或缺失；原批注保留，请重新核对目标。')
            # The CAS token remains the journal snapshot, not this changing UI affordance.
            return snapshot
        return guarded(run_id, project)

    @app.post('/api/runs/{run_id}/annotations')
    def save_annotation(run_id: str, payload: AnnotationIn):
        def save():
            body = payload.model_dump(exclude_none=True)
            fingerprint = objects.digest(body)
            prior = next((e for e in store.read_events(run_id) if e['kind'] == 'workspace_annotation_saved'
                          and e['request_id'] == payload.request_id), None)
            if prior:
                if prior['payload_sha256'] != fingerprint:
                    raise ValueError('相同请求已保存另一条批注。')
                return next(item for item in objects.annotations(store, run_id)['items']
                            if item['annotation_id'] == prior['annotation']['annotation_id'])
            item = objects.read_object(store, pipeline, run_id, payload.target)
            material = item['data'].get('material', {})
            if payload.region and material.get('kind') != 'image':
                raise ValueError('区域批注只能绑定实际图片。')
            if payload.at_seconds is not None and material.get('kind') != 'video':
                raise ValueError('时间批注只能绑定实际视频。')
            if not payload.text.strip():
                raise ValueError('请填写批注内容。')
            annotation = {key: value for key, value in body.items() if key != 'request_id'}
            annotation['annotation_id'] = 'annotation-' + objects.digest([run_id, payload.request_id])[:24]
            row = store.append_event(run_id, 'workspace_annotation_saved', request_id=payload.request_id,
                                     payload_sha256=fingerprint, annotation=annotation)
            return {**annotation, 'at': row['at'], 'state': 'saved'}
        return guarded(run_id, save)

    @app.post('/api/runs/{run_id}/annotations/execute')
    def execute_annotations(run_id: str, payload: ExecuteIn):
        require_run(run_id)
        try:
            with jobs.lock, store.run_lock(run_id):
                submitted = next((e for e in store.read_events(run_id)
                                  if e['kind'] == 'workspace_annotations_submitted'
                                  and e['request_id'] == payload.request_id), None)
                fingerprint = objects.digest(payload.model_dump())
                if submitted:
                    if submitted['payload_sha256'] != fingerprint:
                        raise ValueError('相同请求已用于另一组批注。')
                    return jobs.get(run_id, submitted['job_id'])
                prior_job = next((e for e in store.read_events(run_id)
                                  if e['kind'] == 'job_created' and e.get('request_id') == payload.request_id), None)
                if prior_job:
                    expected = {'annotation_ids': payload.annotation_ids,
                                'snapshot_sha256': payload.expected_snapshot_sha256}
                    if prior_job.get('job_kind') != 'annotation_revision' or prior_job.get('job_payload') != expected:
                        raise ValueError('相同请求已用于其他任务。')
                    store.append_event(run_id, 'workspace_annotations_submitted', request_id=payload.request_id,
                        payload_sha256=fingerprint, annotation_ids=payload.annotation_ids, job_id=prior_job['job_id'])
                    return jobs.get(run_id, prior_job['job_id'])
                snapshot = objects.annotations(store, run_id)
                if snapshot['snapshot_sha256'] != payload.expected_snapshot_sha256:
                    raise ValueError('批注集合已更新，请重新核对本次选择。')
                if len(set(payload.annotation_ids)) != len(payload.annotation_ids):
                    raise ValueError('同一批注不能重复执行。')
                by_id = {a['annotation_id']: a for a in snapshot['items']}
                selected = [by_id.get(identity) for identity in payload.annotation_ids]
                if any(not a or a['state'] != 'saved' for a in selected):
                    raise ValueError('请选择尚未提交的准确批注；已提交项请查询原任务。')
                groups = {}
                for a in selected:
                    item = objects.read_object(store, pipeline, run_id, a['target'])
                    if 'revise' not in item['allowed_actions']:
                        raise ValueError('所选批注含历史、失效或仅可阅读对象；请在当前版本明确重新批注。')
                    target = dict(a['target'])
                    if target['kind'] == 'artifact':
                        target.update(kind='writing', quote=item['text'].strip()[:200])
                    # A media revision consumes every selected annotation for
                    # that immutable output once. Each timestamp stays with
                    # its own annotation, not in the grouping identity.
                    if target['kind'] == 'media':
                        target.pop('at_seconds', None)
                    key = objects.digest({k: target[k] for k in ('run_id', 'stage', 'version', 'artifact_sha256')}
                                         if target['kind'] == 'writing' else target)
                    if target['kind'] == 'writing':
                        target.pop('path', None)
                    groups.setdefault(key, {'target': target, 'annotations': []})['annotations'].append(a)
                from .conversation_input import receive
                originals = '\n\n'.join(a['text'] for a in selected)
                if len(originals) > 8000:
                    raise ValueError('本次批注合计超过 8000 字，请分批选择；所有批注已保留。')
                receipt = receive(store, run_id, originals, payload.request_id)
                store.append_event(run_id, 'workspace_annotations_selected', request_id=payload.request_id,
                    message_id=receipt['message_id'], annotation_ids=payload.annotation_ids,
                    targets=[a['target'] for a in selected], source='ui_annotation_selection')

                def prepare(rid):
                    prepared = []
                    for index, group in enumerate(groups.values()):
                        group_text = '\n\n'.join(a['text'] for a in group['annotations'])
                        sub = receive(store, rid, group_text, f'{payload.request_id}:part:{index}',
                                      feedback_target=group['target'])
                        store.append_event(rid, 'workspace_annotation_group', message_id=sub['message_id'],
                            annotation_ids=[a['annotation_id'] for a in group['annotations']],
                            request_id=payload.request_id)
                        runner, config = prepare_job(rid, feedback_target=group['target'], user_input_id=sub['message_id'])
                        prepared.append((group, sub, runner))
                    def run(text, emit, check_cancel):
                        from .agents.orchestrator import OrchestratorTurn
                        from .conversation_input import accept
                        replies, log, rounds = [], [], 0
                        for group, sub, runner in prepared:
                            check_cancel()
                            for annotation in group['annotations']:
                                current = objects.read_object(store, pipeline, rid, annotation['target'])
                                if 'revise' not in current['allowed_actions']:
                                    raise PipelineError('annotation_changed', '后续批注目标已变化；已完成修改保留，剩余批注未自动改绑。')
                            accept(store, rid, sub)
                            result = runner(sub['text'], emit, check_cancel)
                            replies.append(result.text)
                            log.extend(result.tool_log)
                            rounds += result.rounds
                            if result.stopped_because != 'done' or result.deliverable.get('status') not in {None, 'complete'}:
                                return OrchestratorTurn('\n\n'.join(replies), result.stopped_because, rounds,
                                                        tool_log=log, deliverable=result.deliverable)
                        return OrchestratorTurn('\n\n'.join(replies), 'done', rounds, tool_log=log)
                    return run, config
                job = jobs.submit(run_id, originals, payload.request_id, kind='annotation_revision',
                    payload={'annotation_ids': payload.annotation_ids, 'snapshot_sha256': payload.expected_snapshot_sha256},
                    user_input_id=receipt['message_id'], prepare=prepare)
                store.append_event(run_id, 'workspace_annotations_submitted', request_id=payload.request_id,
                    payload_sha256=fingerprint, annotation_ids=payload.annotation_ids, job_id=job['job_id'])
                return job
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
