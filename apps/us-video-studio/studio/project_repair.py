"""Explicit recovery of accidental, undecided upstream versions. No AI tools."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import PipelineError, STAGES
from .pipeline import Pipeline
from .production import agent_production_state, source_snapshot
from .store import Store


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    stage: Literal['intake', 'direction', 'material_plan', 'writing']
    version: int = Field(ge=1, strict=True)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class RepairPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{8,100}$')
    artifacts: list[ArtifactRef] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=2000)
    feedback_quote: str = Field(default='', max_length=8000)


class RepairAction(RepairPlan):
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class _PreviewStore(Store):
    def __init__(self, store, run_id, event):
        super().__init__(store.root)
        self._run_id, self._event = run_id, event

    def read_events(self, run_id):
        rows = super().read_events(run_id)
        return rows + [self._event] if run_id == self._run_id else rows

    def append_event(self, *args, **kwargs):
        raise PipelineError('repair_source_changed', '来源有变化，恢复预览不会修改记录，请先核对来源。')


def preview_repair(store, run_id, plan: RepairPlan):
    """Evaluate the same active projection without writing to the run."""
    if store.read_run(run_id).kind != 'creative':
        raise PipelineError('repair_kind', '只有创作项目可恢复阶段版本。')
    payload = plan.model_dump(exclude={'expected_revision'})
    if not plan.reason.strip() or len({row.stage for row in plan.artifacts}) != len(plan.artifacts):
        raise PipelineError('repair_scope', '请说明恢复原因，每个阶段只能停用当前一份版本。')
    for row in plan.artifacts:
        latest = store.latest_artifact(run_id, row.stage)
        if not latest or (latest.version, latest.sha256) != (row.version, row.sha256):
            raise PipelineError('repair_changed', '要停用的当前版本已变化，请重新核对。')
        if not store.verify_artifact(run_id, row.stage, row.version):
            raise PipelineError('repair_integrity', '要停用的版本无法核验，请保留原记录排查。')
        if any((a.artifact_version, a.artifact_sha256) == (row.version, row.sha256)
               for a in store.list_approvals(run_id, row.stage)):
            raise PipelineError('repair_decided', '已有用户决定的版本不能通过此入口停用。')
    if plan.feedback_quote and not any(
            row.get('role') == 'user' and row.get('text') == plan.feedback_quote
            for row in store.read_chat(run_id)):
        raise PipelineError('repair_feedback', '恢复须沿用已保存的用户原话，不能改写图审意见。')
    event = {'kind': 'artifact_versions_withdrawn', **payload}
    view = _PreviewStore(store, run_id, event)
    pipeline = Pipeline(view)
    state = pipeline.state(run_id)
    for stage in STAGES:
        row = state.stages[stage]
        if row.open_attempt or not pipeline.is_complete(run_id, stage):
            raise PipelineError('repair_incomplete',
                                f'恢复后 {stage} 仍有过期、未采用或未结束的内容；本次没有修改项目。')
    production = agent_production_state(view, pipeline, run_id)
    stages = [{'stage': row.stage, 'version': row.latest.version,
               'sha256': row.latest.sha256, 'status': row.status}
              for row in state.stages.values()]
    result = {'run_id': run_id, 'artifacts': payload['artifacts'], 'active_stages': stages,
              'feedback_quote': plan.feedback_quote,
              'current_inputs': [{'draft_id': d['draft_id'], 'shot_id': d['shot_id'],
                                  'source_ready': d['source_ready']}
                                 for d in production['drafts'] if d['is_current']]}
    result['revision'] = _digest({
        'plan': payload, 'events': store.read_events(run_id),
        'artifacts': [a.to_dict() for a in store.list_artifacts(run_id)],
        'approvals': [a.to_dict() for a in store.list_approvals(run_id)],
        'chat': store.read_chat(run_id), 'baseline': source_snapshot(view, run_id),
        'result': result,
    })
    return result


def apply_repair(store, run_id, action: RepairAction):
    """Caller holds jobs.lock then run_lock, keeping a single service writer."""
    payload = action.model_dump(exclude={'expected_revision'})
    for event in store.read_events(run_id):
        if event.get('kind') == 'artifact_versions_withdrawn' and event.get('request_id') == action.request_id:
            if event['request_sha256'] != _digest(payload):
                raise PipelineError('repair_request_reused', '恢复请求已保存另一份内容，请勿复用请求 ID。')
            return event['receipt']
    result = preview_repair(store, run_id, action)
    if result['revision'] != action.expected_revision:
        raise PipelineError('repair_changed', '恢复预览之后项目有变化，请重新核对；本次没有修改。')
    store.append_event(run_id, 'artifact_versions_withdrawn', actor='user',
                       **payload, request_sha256=_digest(payload), receipt=result)
    return result
