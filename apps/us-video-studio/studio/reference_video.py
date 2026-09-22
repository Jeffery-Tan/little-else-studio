"""One reference task: local file or TikTok -> observation -> analysis/recreation."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from .agents import bailian_omni
from .agents.client import SafePreflightError
from .audiovisual_observation import prepare_video
from .image_analysis import AnalysisIn, prepare_analysis, candidates, confirm_candidate
from .material_observations import _manifest, ObservationError
from .materials import accept_upload, resolve, kind_of
from .models import PipelineError, STAGES, utcnow
from .tiktok_reference import checked_url, fetch_reference


class ReferenceIn(BaseModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    reference_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    intent: Literal['analyze', 'recreate']
    source_url: str | None = Field(default=None, max_length=8192)
    name: str | None = Field(default=None, max_length=120)
    expected_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    instruction: str = Field(default='', max_length=2000)
    consent: bool = Field(strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    expected_endpoint: str = Field(max_length=300)

    @model_validator(mode='after')
    def source(self):
        if bool(self.source_url) == bool(self.name) or bool(self.name) != bool(self.expected_sha256):
            raise ValueError('请选择本地视频或TikTok链接中的一种来源。')
        if self.name and kind_of(self.name) != 'video':
            raise ValueError('参考任务需要视频文件。')
        return self


def task_state(store, run_id):
    items = {}
    for event in store.read_events(run_id):
        rid = event.get('reference_id')
        if event['kind'] == 'reference_video_started':
            items.setdefault(rid, {**event['request'], 'reference_id': rid, 'status': 'started'})
        elif rid in items and event['kind'] == 'reference_video_source':
            items[rid].update(event['source'], status='downloaded')
        elif rid in items and event['kind'] == 'reference_video_ready':
            items[rid].update(analysis_id=event['analysis_id'], report=event['report'], status='analyzed')
        elif rid in items and event['kind'] == 'reference_video_completed':
            items[rid]['status'] = 'completed'
    all_candidates = {c['analysis_id']: c for c in candidates(store, run_id)}
    for item in items.values():
        c = all_candidates.get(item.get('analysis_id'))
        if c:
            item['candidate'] = c
            if c['status'] == 'stale':
                item['status'] = 'stale'
    return list(items.values())


def report_for(candidate):
    lines = ['参考视频分析', '', *[f'{i+1}. {v}' for i, v in enumerate(candidate['findings'])]]
    for m in candidate['reference_methods']:
        lines.extend(['', '可借鉴方法：' + m['mechanism'], '不能直接照搬：' + m['non_transferable'], '可尝试：' + m['test_change']])
    lines.extend(['', '未确定与限制：', *['- ' + v for v in candidate['limitations']], '',
                  '以上为本次指定参考的模型观察，未经人工核对；不作为本款商品性能事实。'])
    return '\n'.join(lines)


def install_reference_video_routes(app, jobs, require_run, connections, settings, prepare_creative_job, *, client_factory=None):
    store = jobs.store

    @app.get('/api/runs/{run_id}/reference-videos')
    def workspace(run_id: str):
        require_run(run_id)
        return {'tasks': task_state(store, run_id), 'connection': bailian_omni.connection_info(),
                'revision': connections.public()['revision']}

    def prepare(run_id, payload, *, observe_only=False):
        if not payload.consent:
            raise ValueError('开始参考任务需要确认发送至百炼及可能产生的分析费用。')
        prior = next((t for t in task_state(store, run_id) if t['reference_id'] == payload.reference_id), None)
        if prior:
            if prior['intent'] != payload.intent or prior.get('instruction', '') != payload.instruction:
                raise ValueError('继续参考任务不能改变原目标；请新建参考任务。')
            if payload.source_url and payload.source_url != prior.get('source_url'):
                raise ValueError('继续参考任务不能换成其他链接。')
            if prior.get('name') and payload.name and (payload.name != prior['name'] or payload.expected_sha256 != prior.get('sha256', prior.get('expected_sha256'))):
                raise ValueError('已有参考文件不能在继续任务时替换。')
        if payload.source_url:
            checked_url(payload.source_url)
        if payload.expected_revision != connections.public()['revision']:
            raise ValueError('模型设置已变化，请重新打开参考视频入口。')
        if any(jobs.pipeline.stage_state(run_id, s).open_attempt for s in STAGES):
            raise ValueError('请先处理当前未完成的策划步骤，再开始参考任务。')
        frozen = bailian_omni.freeze(settings, payload.expected_endpoint)
        # Freeze creative routing before the download; no new role credential is
        # read later if the user changes connections while the reference runs.
        creative = prepare_creative_job(run_id) if payload.intent == 'recreate' and not observe_only else None

        def run(_text, emit, check_cancel):
            check_cancel()
            current = next((t for t in task_state(store, run_id) if t['reference_id'] == payload.reference_id), None)
            if current and current['status'] == 'completed':
                return SimpleNamespace(text='该参考任务已完成，请查看已保存的分析和方案。', stopped_because='done', rounds=0, used_tools=[])
            request = payload.model_dump(exclude={'request_id', 'consent', 'expected_endpoint', 'expected_revision'})
            if not current:
                store.append_event(run_id, 'reference_video_started', reference_id=payload.reference_id, request=request)
            name = current.get('name') if current else None
            sha = current.get('sha256') if current else None
            if not sha:
                if payload.name:
                    path = resolve(store, run_id, payload.name)
                    if path is None:
                        raise PipelineError('reference_source', '本地参考视频不存在，请重新上传。')
                    from .video_observation import read_video
                    raw = read_video(store, run_id, payload.name, payload.expected_sha256)
                    name, sha = payload.name, payload.expected_sha256
                    source = {'name': name, 'sha256': sha, 'origin': 'user_upload',
                              'source_url': current.get('source_url') if current else None}
                else:
                    emit({'type': 'status', 'role': 'material', 'message': '正在获取TikTok参考视频。'})
                    raw, source = fetch_reference(payload.source_url, check_cancel)
                    name, sha = f"tiktok-{source['video_id']}-{source['sha256'][:12]}.mp4", source['sha256']
                    source.update(name=name, origin='tiktok_public')
                # Validate before registering remote bytes as project material.
                try:
                    _, info = prepare_video(raw, check_cancel)
                except ObservationError as exc:
                    raise PipelineError('reference_invalid', str(exc)) from None
                if source['origin'] == 'tiktok_public' and not info['audio_sent']:
                    raise PipelineError('reference_audio_missing', '下载文件没有音轨，无法核实原视频声音。请上传带声音的原视频继续。')
                check_cancel()
                with store.run_lock(run_id):
                    if source['origin'] == 'tiktok_public':
                        existing = resolve(store, run_id, name)
                        if existing and hashlib.sha256(existing.read_bytes()).hexdigest() != sha:
                            raise PipelineError('reference_conflict', '同名参考文件已经变化，未覆盖。')
                        if not existing:
                            accept_upload(store, run_id, name, raw)
                    store.append_event(run_id, 'reference_video_source', reference_id=payload.reference_id,
                                       source={**source, 'acquired_at': utcnow()})
            path = resolve(store, run_id, name)
            if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise PipelineError('reference_changed', '参考文件已变化，请重新指定参考。')
            existing_candidate = next((c for c in candidates(store, run_id)
                if c.get('reference_task_id') == payload.reference_id and c['sha256'] == sha and c['status'] != 'stale'), None)
            if existing_candidate is None:
                analysis = AnalysisIn(request_id=payload.request_id, name=name, expected_sha256=sha,
                    expected_revision=payload.expected_revision, connection_id=bailian_omni.CONNECTION,
                    model=bailian_omni.MODEL, purpose='reference', consent=True, expected_endpoint=payload.expected_endpoint)
                observer, _ = prepare_analysis(store, jobs.pipeline, connections, settings, (run_id, analysis),
                    client_factory=client_factory, video=True, _native_frozen=frozen, _reference_id=payload.reference_id,
                    _reference_instruction=payload.instruction)
                result = observer('', emit, check_cancel)
                existing_candidate = next(c for c in candidates(store, run_id) if c['analysis_id'] == result.analysis_id)
            c = existing_candidate
            check_cancel()
            report = report_for(c)
            if not current or not current.get('analysis_id'):
                store.append_event(run_id, 'reference_video_ready', reference_id=payload.reference_id,
                                   analysis_id=c['analysis_id'], report=report)
                if not observe_only:
                    store.append_chat(run_id, 'assistant', report)
            if payload.intent == 'recreate' and not observe_only:
                from .creation_mode import select
                select(store, run_id, 'recreate', payload.reference_id, payload.instruction or '保留参考的注意力节奏、证据顺序与镜头功能，改编为本款商品表达')
                check_cancel()
                emit({'type': 'status', 'role': 'orchestrator', 'message': '参考已分析，主制片正在结合当前商品形成复刻方案。'})
                prompt = ('请按我指定的参考视频继续复刻策划：' + name + '。当前参考观察已保存，'
                          '请读取当前创作证据，结合本项目商品和已选方向形成方案、脚本与分镜。'
                          '模型观察未经人工核对；参考中的性能和经历不能变成本款事实。'
                          '不要重复分析参考；不得自行批准或提交图片、视频生成。\n我的补充要求：' + payload.instruction)
                result = creative[0](prompt, emit, check_cancel)
                if result.stopped_because != 'done':
                    return result
            else:
                result = SimpleNamespace(text=report, stopped_because='done', rounds=1, used_tools=[])
            store.append_event(run_id, 'reference_video_completed', reference_id=payload.reference_id)
            return result

        return run, {'observer': frozen[2], 'intent': payload.intent, 'reference_id': payload.reference_id,
                     'creative': creative[1] if creative else None}

    @app.post('/api/runs/{run_id}/reference-videos')
    def start(run_id: str, payload: ReferenceIn):
        require_run(run_id)
        try:
            return jobs.submit(run_id, '分析参考视频' if payload.intent == 'analyze' else '按参考视频复刻',
                payload.request_id, kind='reference_video', payload=payload.model_dump(exclude={'request_id'}),
                prepare=lambda rid: prepare(rid, payload))
        except (ValueError, PipelineError, SafePreflightError) as exc:
            raise HTTPException(409, str(exc)) from None

    def observe(run_id, source, instruction, emit, check_cancel, *, endpoint, revision):
        """Run inside the current conversation Job; never create a nested Job."""
        import uuid
        from .creation_mode import reference_candidate
        fields = {}
        if source.startswith('https://'):
            fields['source_url'] = checked_url(source)
        else:
            path = resolve(store, run_id, source)
            if path is None or kind_of(source) != 'video':
                raise ValueError('请使用当前项目实际上传的视频文件名或用户提供的TK链接。')
            fields.update(name=source, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        previous = next((t for t in reversed(task_state(store, run_id))
            if (fields.get('source_url') and t.get('source_url') == fields['source_url']) or
               (fields.get('name') and t.get('name') == source and t.get('sha256') == fields['expected_sha256'])), None)
        if previous and previous['status'] == 'stale':
            previous = None
        if previous:
            try:
                c = reference_candidate(store, run_id, previous['reference_id'])
                return {'reference_id': previous['reference_id'], 'report': report_for(c), 'reused': True}
            except ValueError:
                pass
        # An explicit new turn may retry a failed download/analysis. Keep the old
        # source identity so downloaded bytes survive; do not retry in this turn.
        rid = previous['reference_id'] if previous else uuid.uuid4().hex
        payload = ReferenceIn(request_id=uuid.uuid4().hex, reference_id=rid,
            intent=previous['intent'] if previous else 'analyze', instruction=previous.get('instruction', '') if previous else instruction,
            consent=True, expected_revision=revision, expected_endpoint=endpoint, **fields)
        runner, _ = prepare(run_id, payload, observe_only=True)
        runner('', emit, check_cancel)
        c = reference_candidate(store, run_id, rid)
        return {'reference_id': rid, 'report': report_for(c), 'reused': False}

    app.state.observe_reference = observe
