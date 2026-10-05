"""One reference task: local file or TikTok -> observation -> analysis/recreation."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from .agents import bailian_omni
from .agents.client import SafePreflightError, ModelError, JobCancelled
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


class PromptEditIn(BaseModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,100}$')
    analysis_id: str = Field(min_length=1, max_length=100)
    candidate_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_prompt_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    expected_prompt_revision: int | None = Field(default=None, ge=1, strict=True)
    prompt: str = Field(min_length=1, max_length=20000)


def task_state(store, run_id):
    items = {}
    events = store.read_events(run_id)
    latest_creative_seq = max((event['seq'] for event in events
        if event['kind'] in {'attempt_submitted', 'stage_decided', 'production_draft',
                             'production_attempt', 'production_material_approval'}
        or (event['kind'] == 'job_created' and event.get('job_kind') != 'reference_video')),
        default=0)
    for event in events:
        rid = event.get('reference_id')
        if event['kind'] == 'reference_video_started':
            items.setdefault(rid, {**event['request'], 'reference_id': rid,
                                   'started_at': event['at'],
                                   'after_creative_work': event['seq'] > latest_creative_seq,
                                   'status': 'started'})
        elif rid in items and event['kind'] == 'reference_video_source':
            items[rid].update(event['source'], status='downloaded')
        elif rid in items and event['kind'] == 'reference_video_ready':
            items[rid].update(analysis_id=event['analysis_id'], report=event['report'], status='analyzed')
        elif rid in items and event['kind'] == 'reference_video_completed':
            items[rid]['status'] = 'completed'
    owners, executions, failures = {}, {}, {}
    for event in events:
        if event['kind'] == 'job_created':
            executions[event['job_id']] = {'status': 'queued'}
            if event.get('job_kind') == 'reference_video':
                rid = (event.get('job_payload') or {}).get('reference_id')
                if rid in items:
                    owners[rid] = event['job_id']
        elif event['kind'] == 'reference_video_execution':
            if event.get('reference_id') in items:
                owners[event['reference_id']] = event['job_id']
        elif event['kind'] == 'reference_video_step_failed':
            failures[(event['reference_id'], event.get('job_id'))] = event['reason']
        elif event['kind'] == 'job_status':
            executions[event['job_id']] = {'status': event['status'],
                                            'reason': event.get('reason', '')}
    for rid, job_id in owners.items():
        execution = executions.get(job_id, {'status': 'unknown'})
        items[rid].update(job_id=job_id, execution_status=execution['status'])
        failure = failures.get((rid, job_id))
        if failure or execution['status'] in {'failed', 'blocked', 'cancelled', 'interrupted'}:
            items[rid]['failure_reason'] = failure or execution.get('reason', '')
    all_candidates = {c['analysis_id']: c for c in candidates(store, run_id)}
    for item in items.values():
        c = all_candidates.get(item.get('analysis_id'))
        if c:
            item['candidate'] = c
            if c['status'] == 'stale':
                item['status'] = 'stale'
    for event in events:
        if event['kind'] != 'reference_prompt_saved':
            continue
        item = items.get(event.get('reference_id'))
        if item and isinstance(event.get('prompt'), str):
            item['prompt'] = {'text': event['prompt'], 'sha256': event['sha256'],
                              'revision': event['seq'], 'saved_at': event['at'],
                              'actor': event.get('actor', 'model'),
                              'analysis_id': event.get('analysis_id')}
    for item in items.values():
        recovery = recovery_for(store, run_id, item, events=events)
        if recovery is not None:
            item['recovery'] = recovery
    return list(items.values())


def recovery_for(store, run_id, task, *, events=None):
    """Derive a conversation dependency's original task, never from its scope."""
    from .conversation_input import interpretation_for, message
    events = events if events is not None else store.read_events(run_id)
    jobs = {e['job_id']: e for e in events if e['kind'] == 'job_created'}
    owner = jobs.get(task.get('job_id'))
    if owner is None or owner.get('job_kind') == 'reference_video':
        return None
    payload = owner.get('job_payload') or {}
    saved = payload.get('reference_recovery') or {}
    source_id = saved.get('source_user_input_id') or owner.get('user_input_id')
    unavailable = {'allowed': False, 'reason': '原任务的真实消息与交付无法核对，请在对话中说明新的任务。'}
    if not source_id:
        return unavailable
    try:
        receipt = message(store, run_id, source_id)
    except PipelineError:
        return unavailable
    interpretation = interpretation_for(store, run_id, source_id)
    if not interpretation or interpretation['task']['mode'] not in {'produce', 'revise', 'analyze'}:
        return unavailable
    aliases = {task.get('source_url'), task.get('name')}
    requested = None
    cursor, visited = owner['job_id'], set()
    while cursor in jobs and cursor not in visited:
        visited.add(cursor)
        for event in reversed(events):
            if event.get('job_id') != cursor:
                continue
            if (event['kind'] == 'reference_observation_requested'
                    and event.get('source_user_input_id') == source_id
                    and event.get('source') in aliases):
                requested = event
                break
            # Historical conversation Jobs retained the actual tool arguments.
            progress = event.get('event') or {}
            args = progress.get('args') or {}
            if (event['kind'] == 'job_progress' and progress.get('type') == 'agent_tool'
                    and progress.get('tool') == 'observe_reference' and args.get('source') in aliases):
                requested = args
                break
        if requested:
            break
        prior = (jobs[cursor].get('job_payload') or {}).get('reference_recovery') or {}
        cursor = prior.get('parent_job_id')
    from .creation_mode import reference_candidate
    try:
        reference_candidate(store, run_id, task['reference_id'])
        requires_observation = False
    except ValueError:
        requires_observation = True
    return {'reference_id': task['reference_id'], 'parent_job_id': owner['job_id'],
            'source_user_input_id': source_id, 'interpretation_id': interpretation['id'],
            'deliverable': interpretation['task']['deliverable'], 'original_text': receipt['text'],
            'action': (requested or {}).get('action', 'analyze'),
            'reference_scope': (requested or {}).get('scope', task.get('instruction', '')),
            'requires_observation': requires_observation,
            'allowed': task.get('execution_status') in {'failed', 'blocked', 'cancelled', 'interrupted'}
                       and task['status'] != 'stale'}


def validate_recovery(store, run_id, receipt):
    """The explicit recovery selects an owner and original frozen interpretation."""
    from .conversation_input import task_constraints
    selected = receipt.get('reference_recovery') or {}
    task = next((t for t in task_state(store, run_id)
                 if t['reference_id'] == selected.get('reference_id')), None)
    recovery = (task or {}).get('recovery') or {}
    keys = ('reference_id', 'parent_job_id', 'source_user_input_id', 'interpretation_id')
    if not recovery.get('allowed') or any(selected.get(key) != recovery.get(key) for key in keys):
        raise PipelineError('reference_recovery', '参考所属任务或原要求已变化，请刷新后明确恢复准确任务。')
    limits = task_constraints(receipt)
    from .steer_intent import classify
    if (classify(receipt['text'])['class'] in {'stop', 'status', 'discussion'}
            or limits['analysis_only'] and recovery['deliverable'] != 'analysis'
            or limits['prompt_only'] and recovery['deliverable'] != 'reference_prompt'):
        raise PipelineError('reference_recovery', '本条消息要求停止、只读或改变交付，不能同时恢复旧任务；请作为新的对话发送。')
    if receipt.get('feedback_target') or receipt.get('question_id'):
        raise PipelineError('reference_recovery', '恢复原任务不能同时绑定其他作品修改或问题回答。')
    target = receipt.get('reference_target')
    if target and target['reference_id'] != selected['reference_id']:
        raise PipelineError('reference_recovery', '恢复任务与所选参考不一致，请重新核对。')
    matching = next((a for a in receipt.get('attachments') or [] if a['name'] == task.get('name')), None)
    if matching and matching.get('purpose') not in {None, 'reference'}:
        raise PipelineError('reference_recovery', '本条消息将原参考标为商品视频，请先澄清用途。')
    name, sha = selected.get('name'), selected.get('expected_sha256')
    if bool(name) != bool(sha):
        raise PipelineError('reference_recovery', '上传原视频须同时绑定准确文件与指纹。')
    if name:
        if task.get('sha256'):
            raise PipelineError('reference_recovery', '原片已保存，恢复不能替换来源。')
        path = resolve(store, run_id, name)
        attachment = next((a for a in receipt.get('attachments') or [] if a['name'] == name), None)
        if (path is None or kind_of(name) != 'video' or hashlib.sha256(path.read_bytes()).hexdigest() != sha
                or not attachment or attachment.get('purpose') != 'reference'):
            raise PipelineError('reference_recovery', '上传原视频的附件用途或字节已变化，请重新核对。')
    if recovery['requires_observation'] and not receipt.get('reference_consent', False):
        raise PipelineError('reference_recovery', '本次恢复尚需视频观察，请明确开启本次观察连接后发送。')
    return task, recovery


def _execution_job_id(store, run_id):
    """Use the active task's exact owner, never infer it from the latest Job."""
    from .agent_runtime import current_task_id
    task_id = current_task_id()
    if task_id is None:
        return None
    events = store.read_events(run_id)
    task = next((event['task'] for event in events
                 if event['kind'] == 'agent_runtime_task_created'
                 and event.get('task_id') == task_id), {})
    job_id = task.get('parent_job_id') or task_id
    return job_id if any(event['kind'] == 'job_created' and event['job_id'] == job_id
                         for event in events) else None


def reuse_analysis(store, run_id, reference_id):
    """Read one valid saved analysis and bind its use to the actual Job."""
    from .creation_mode import reference_candidate
    with store.run_lock(run_id):
        candidate = reference_candidate(store, run_id, reference_id)
        report = report_for(candidate)
        job_id = _execution_job_id(store, run_id)
        if job_id:
            store.append_event(run_id, 'reference_video_execution',
                               reference_id=reference_id, job_id=job_id)
        task = next(t for t in task_state(store, run_id) if t['reference_id'] == reference_id)
        needs_report = task.get('analysis_id') != candidate['analysis_id']
        if needs_report:
            store.append_event(run_id, 'reference_video_ready', reference_id=reference_id,
                               analysis_id=candidate['analysis_id'], report=report)
        if needs_report or task['status'] != 'completed':
            store.append_event(run_id, 'reference_video_completed', reference_id=reference_id)
    return {'reference_id': reference_id, 'report': report, 'reused': True}


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

    @app.post('/api/runs/{run_id}/reference-videos/{reference_id}/prompt')
    def save_prompt_edit(run_id: str, reference_id: str, payload: PromptEditIn):
        require_run(run_id)
        if store.read_run(run_id).kind == 'learning':
            raise HTTPException(409, '学习记录不保存商品适配提示词。')
        prompt = payload.prompt.strip()
        if not prompt:
            raise HTTPException(422, '提示词不能为空。')
        request_sha = hashlib.sha256(payload.model_dump_json().encode('utf-8')).hexdigest()
        with store.run_lock(run_id):
            for event in store.read_events(run_id):
                if event['kind'] == 'reference_prompt_saved' and event.get('request_id') == payload.request_id:
                    if event.get('request_sha256') != request_sha or event.get('reference_id') != reference_id:
                        raise HTTPException(409, '请求标识已用于另一份提示词。')
                    return {'prompt': {'text': event['prompt'], 'sha256': event['sha256'],
                                       'revision': event['seq'], 'saved_at': event['at'], 'actor': 'user',
                                       'analysis_id': event.get('analysis_id')}}
            task = next((t for t in task_state(store, run_id) if t['reference_id'] == reference_id), None)
            if not task or task['status'] == 'stale' or not task.get('candidate'):
                raise HTTPException(409, '参考分析已变化，请重新打开后再修改提示词。')
            candidate = task['candidate']
            if (candidate['analysis_id'] != payload.analysis_id or
                    candidate['candidate_sha256'] != payload.candidate_sha256):
                raise HTTPException(409, '参考分析版本已变化，请重新打开后再修改提示词。')
            current_sha = task.get('prompt', {}).get('sha256')
            current_revision = task.get('prompt', {}).get('revision')
            if (current_sha != payload.expected_prompt_sha256 or
                    current_revision != payload.expected_prompt_revision):
                raise HTTPException(409, '提示词已有更新，请重新打开后比较新版本。')
            digest = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
            event = store.append_event(run_id, 'reference_prompt_saved', reference_id=reference_id,
                analysis_id=payload.analysis_id, source_sha256=candidate['sha256'],
                prompt=prompt, sha256=digest, actor='user', request_id=payload.request_id,
                request_sha256=request_sha)
            return {'prompt': {'text': prompt, 'sha256': digest, 'saved_at': event['at'],
                               'revision': event['seq'], 'actor': 'user',
                               'analysis_id': payload.analysis_id}}

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

        def run_steps(_text, emit, check_cancel):
            check_cancel()
            current = next((t for t in task_state(store, run_id) if t['reference_id'] == payload.reference_id), None)
            if current and current['status'] == 'completed':
                reuse_analysis(store, run_id, payload.reference_id)
                return SimpleNamespace(text='该参考任务已完成，请查看已保存的分析和方案。', stopped_because='done', rounds=0, used_tools=[])
            request = payload.model_dump(exclude={'request_id', 'consent', 'expected_endpoint', 'expected_revision'})
            if not current:
                store.append_event(run_id, 'reference_video_started', reference_id=payload.reference_id, request=request)
            job_id = _execution_job_id(store, run_id)
            if job_id:
                store.append_event(run_id, 'reference_video_execution',
                                   reference_id=payload.reference_id, job_id=job_id)
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

        def run(_text, emit, check_cancel):
            try:
                return run_steps(_text, emit, check_cancel)
            except Exception as exc:
                # Preserve the actionable step error even if the conversation
                # catches a tool failure and its final Job reason is generic.
                reason = (str(exc) if isinstance(exc, (PipelineError, ModelError,
                           SafePreflightError, ObservationError, JobCancelled)) else
                          '参考处理失败；已保存的原片和观察保留，请查看当前任务记录。')
                if isinstance(exc, PipelineError):
                    reason = reason.removeprefix(f'[{exc.rule}] ')
                store.append_event(run_id, 'reference_video_step_failed',
                                   reference_id=payload.reference_id,
                                   job_id=_execution_job_id(store, run_id),
                                   reason=reason, error_kind=type(exc).__name__)
                raise

        return run, {'observer': frozen[2], 'intent': payload.intent, 'reference_id': payload.reference_id,
                     'creative': creative[1] if creative else None}

    @app.post('/api/runs/{run_id}/reference-videos')
    def start(run_id: str, payload: ReferenceIn):
        require_run(run_id)
        if store.read_run(run_id).kind == 'learning' and payload.intent != 'analyze':
            raise HTTPException(409, '学习记录只允许分析参考，不启动复刻策划。')
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
                return reuse_analysis(store, run_id, previous['reference_id'])
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

    def prepare_recovery(run_id, receipt):
        task, recovery = validate_recovery(store, run_id, receipt)
        hook = {'parent_job_id': recovery['parent_job_id'],
                'recovery_message_id': receipt['message_id'], 'action': recovery['action'],
                'reference_scope': recovery['reference_scope']}
        dependency, dependency_config = None, None
        if recovery['requires_observation']:
            connection = bailian_omni.connection_info()
            if not connection:
                raise PipelineError('reference_connection', '参考观察连接未就绪，请检查当前百炼连接。')
            selected = receipt['reference_recovery']
            if selected.get('name'):
                fields = {'name': selected['name'], 'expected_sha256': selected['expected_sha256']}
            elif task.get('name') and (task.get('sha256') or task.get('expected_sha256')):
                fields = {'name': task['name'], 'expected_sha256': task.get('sha256') or task['expected_sha256']}
            else:
                fields = {'source_url': task.get('source_url')}
            payload = ReferenceIn(request_id=receipt['message_id'], reference_id=task['reference_id'],
                intent=task['intent'], instruction=task.get('instruction', ''), consent=True,
                expected_revision=connections.public()['revision'], expected_endpoint=connection['base_url'], **fields)
            dependency, dependency_config = prepare(run_id, payload, observe_only=True)
        # Freeze the original team's routing before any observation is started.
        creative = prepare_creative_job(run_id, user_input_id=receipt['message_id'],
            source_user_input_id=recovery['source_user_input_id'],
            reference_consent=receipt.get('reference_consent', False),
            reference_recovery_context=hook)

        def run(text, emit, check_cancel):
            from .conversation_input import start_execution
            from .creation_mode import reference_candidate, digest
            start_execution(store, run_id, receipt)
            check_cancel()
            store.append_event(run_id, 'reference_recovery_started',
                reference_id=task['reference_id'], job_id=_execution_job_id(store, run_id),
                parent_job_id=recovery['parent_job_id'], recovery_message_id=receipt['message_id'],
                source_user_input_id=recovery['source_user_input_id'],
                interpretation_id=recovery['interpretation_id'], deliverable=recovery['deliverable'])
            if dependency:
                dependency('', emit, check_cancel)
            reuse_analysis(store, run_id, task['reference_id'])
            candidate = reference_candidate(store, run_id, task['reference_id'])
            hook['reference_target'] = {'run_id': run_id, 'reference_id': task['reference_id'],
                'analysis_id': candidate['analysis_id'], 'candidate_sha256': digest(candidate)}
            check_cancel()
            return creative[0](text, emit, check_cancel)

        return run, {'reference_recovery': {
            'reference_id': task['reference_id'], 'parent_job_id': recovery['parent_job_id'],
            'source_user_input_id': recovery['source_user_input_id'],
            'interpretation_id': recovery['interpretation_id'], 'deliverable': recovery['deliverable']},
            'observer': dependency_config, 'creative': creative[1]}

    app.state.prepare_reference_recovery = prepare_recovery
