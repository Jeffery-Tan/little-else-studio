"""Reviewable design supplements for adopted legacy stories; never approve them.

Preparing a proposal records one normal, metered material-role call. It cannot
write stage artifacts. Saving the user's reviewed proposal copies the original
story and adds only design and shot identities, using the original Pipeline.
"""
from __future__ import annotations

from copy import deepcopy
import json

from pydantic import BaseModel, ConfigDict, Field

from .models import PipelineError, STAGES
from .visual_design import (CHARACTER_FIELDS, SCENE_FIELDS, TEXT_FIELDS, character_ids,
                            fingerprint, normalize_visual_design, validate_segments)


class ProposeUpgradeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    expected_context_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_connection_revision: int = Field(ge=0, strict=True)
    consent: bool = Field(strict=True)


class SaveUpgradeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proposal_id: str = Field(min_length=8, max_length=128)
    expected_proposal_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    nonce: str = Field(min_length=1, max_length=128)
    consent: bool = Field(strict=True)


def _basis(store, pipeline, run_id, stage_memo=None):
    if store.read_run(run_id).kind != 'creative':
        raise PipelineError('design_upgrade', '只有创作项目可补充制作设计。')
    stage_memo = {} if stage_memo is None else stage_memo
    states = {stage: pipeline.stage_state(run_id, stage, stage_memo) for stage in STAGES}
    if any(state.open_attempt for state in states.values()) or not all(
            pipeline.is_complete(run_id, stage, stage_memo) for stage in STAGES):
        raise PipelineError('design_upgrade', '先完成当前创作步骤并采用准确脚本，再补充制作设计。')
    material, writing = states['material_plan'].latest, states['writing'].latest
    if not writing.structured.get('segments'):
        raise PipelineError('design_upgrade', '原脚本没有可核对的镜头清单，不能自动补绑定。')
    if material.structured.get('visual_design'):
        try:
            validate_segments(normalize_visual_design(material.structured['visual_design']),
                              writing.structured['segments'])
        except ValueError:
            pass
        else:
            raise PipelineError('design_upgrade', '当前设计与逐镜身份已存在，请核对或修订实际制作输入。')
    from .production import source_snapshot, target_shots
    targets = target_shots(store, run_id, 'image')
    refs = {stage: {'version': state.latest.version, 'sha256': state.latest.sha256}
            for stage, state in states.items()}
    basis = {'artifacts': refs, 'production_baseline': source_snapshot(store, run_id),
             'shot_ids': [row['shot_id'] for row in targets['shots']]}
    return basis


def available(store, pipeline, run_id, stage_memo=None):
    try:
        basis = _basis(store, pipeline, run_id, stage_memo)
    except (PipelineError, ValueError):
        return None
    return {'kind': 'design_upgrade', 'label': '补齐制作设计，保留原脚本',
            'expected_context_sha256': fingerprint(basis),
            'notice': '只准备共同设计和逐镜身份，原方向、画面、台词、声音与时长保留。准备需一次素材角色模型调用；保存新稿后仍需你采用，不生成媒体。'}


def _require_basis(store, pipeline, run_id, expected):
    basis = _basis(store, pipeline, run_id)
    if fingerprint(basis) != expected:
        raise PipelineError('design_upgrade_changed', '原方案或素材已变化，请刷新后重新准备设计补充。')
    return basis


def tool_schema(shot_ids):
    text = {'type': 'string'}
    def obj(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties),
                'additionalProperties': False}
    design = obj({'schema_version': {'type': 'integer', 'enum': [1]},
        'market': {'type': 'string', 'enum': ['US']}, **{key: text for key in TEXT_FIELDS},
        'characters': {'type': 'array', 'items': obj({key: text for key in CHARACTER_FIELDS})},
        'scenes': {'type': 'array', 'items': obj({key: text for key in SCENE_FIELDS})},
        'continuity': {'type': 'array', 'items': text},
        'assumptions': {'type': 'array', 'items': text}})
    return obj({'design': design, 'bindings': {'type': 'array', 'items': obj({
        'shot_id': {'type': 'string', 'enum': shot_ids},
        'character_ids': text, 'scene_id': text})}})


def _supplement(store, run_id, basis, arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'design', 'bindings'}:
        raise PipelineError('design_upgrade_result', '补充方案只接受共同设计与逐镜人物、场景身份。')
    design = normalize_visual_design(arguments['design'])
    bindings = arguments['bindings']
    if (not isinstance(bindings, list) or len(bindings) != len(basis['shot_ids']) or any(
            not isinstance(row, dict) or set(row) != {'shot_id', 'character_ids', 'scene_id'}
            for row in bindings) or [row['shot_id'] for row in bindings] != basis['shot_ids']):
        raise PipelineError('design_upgrade_result', '补充方案须按原顺序覆盖每个准确镜头，不能增加、删掉或换镜头。')
    material = store.read_artifact(run_id, 'material_plan', basis['artifacts']['material_plan']['version'])
    writing = store.read_artifact(run_id, 'writing', basis['artifacts']['writing']['version'])
    if material.structured.get('visual_design') and design != material.structured['visual_design']:
        raise PipelineError('design_upgrade_result', '原共同设计已存在，本次只能补逐镜身份，不能换设计。')
    story = deepcopy(writing.structured)
    for segment, binding in zip(story['segments'], bindings):
        character_ids(binding['character_ids'])
        for field in ('character_ids', 'scene_id'):
            bound = bool(segment.get(field)) or (field == 'character_ids' and bool(segment.get('scene_id')))
            if bound and segment[field] != binding[field]:
                raise PipelineError('design_upgrade_result', '原镜头已有的身份绑定必须保留，本次只能补缺失字段。')
        segment.update(character_ids=binding['character_ids'], scene_id=binding['scene_id'])
    validate_segments(design, story['segments'])
    return {'design': design, 'bindings': deepcopy(bindings),
            'material_plan': {**deepcopy(material.structured), 'visual_design': design},
            'writing': story}


def latest_proposal(store, run_id):
    rows = [row for row in store.read_events(run_id) if row.get('kind') == 'design_upgrade_proposed']
    if not rows:
        return None
    proposal = rows[-1]['proposal']
    if any(row.get('kind') == 'design_upgrade_applied' and row.get('proposal_id') == proposal['id']
           for row in store.read_events(run_id)):
        return None
    return proposal


def prepare_proposal(service, run_id, payload):
    if not payload.consent:
        raise PipelineError('design_upgrade_consent', '请确认这一次素材角色模型调用；不会提交媒体生成。')
    basis = _require_basis(service.store, service.pipeline, run_id, payload.expected_context_sha256)
    if service.connections.public()['revision'] != payload.expected_connection_revision:
        raise PipelineError('design_upgrade_connection', '模型设置已变化，请重新核对这一次准备调用。')
    if service.client_factory:
        settings = service.settings
        make_client = lambda check_cancel: service.client_factory()
    else:
        snapshot = service.connections.snapshot(expected_revision=payload.expected_connection_revision)
        settings = snapshot.settings(service.settings)
        make_client = lambda check_cancel: snapshot.client(check_cancel=check_cancel, max_tokens=8192)
    from .agents.budget import BudgetedClient
    from .ledger import Ledger
    from .project_brief import content_format
    format = content_format(service.store.read_events(run_id))
    context = service.learning.prepare_context(run_id, 'material', intent='edit', format=format,
                                               purpose='storyboard')
    source = {stage: service.store.read_artifact(run_id, stage, ref['version']).structured
              for stage, ref in basis['artifacts'].items()}
    from .evidence import evidence_snapshot
    seed = json.dumps({'adopted_content': source, 'shot_ids': basis['shot_ids'],
                       'evidence': evidence_snapshot(service.store, run_id)}, ensure_ascii=False)
    tools = [{'name': 'propose_design_supplement',
              'description': 'Propose only shared visual design and original-shot character/scene bindings. Does not submit artifacts, approve, or generate media.',
              'input_schema': tool_schema(basis['shot_ids'])}]

    def run(text, emit, check_cancel):
        check_cancel()
        _require_basis(service.store, service.pipeline, run_id, payload.expected_context_sha256)
        client = make_client(check_cancel)
        if hasattr(client, 'check_cancel'):
            client.check_cancel = check_cancel
        service.store.append_chat(run_id, 'user', text)
        emit({'type': 'status', 'message': '正在按原已采用内容准备设计补充；原脚本与图片保留。'})
        turn = BudgetedClient(client, Ledger(service.store, settings), run_id=run_id,
            role='material', stage='material_plan').complete(model=settings.model_for('material'),
            system=('你负责补齐旧项目制作设计，只调用 propose_design_supplement 一次。'
                    '原方向、故事、画面、台词、声音、时长、素材事实和镜头数全部保留。'
                    '只补人物/场景的创作设计与逐镜身份；把设计假设写入 assumptions，'
                    '不得把创作选择说成商品或市场事实。已有共同设计时原样保留，只补身份。'
                    '原镜头要求互相矛盾时如实说明，不能为填齐字段改写原故事。\n' + context['text']),
            messages=[{'role': 'user', 'content': seed}], tools=tools)
        check_cancel()
        if (turn.stop_reason != 'tool_use' or len(turn.tool_calls) != 1
                or turn.tool_calls[0].name != 'propose_design_supplement'):
            raise PipelineError('design_upgrade_result', '模型没有交回一份完整设计补充；原方案保留，本轮不会自动重试。')
        with service.store.run_lock(run_id):
            _require_basis(service.store, service.pipeline, run_id, payload.expected_context_sha256)
            supplement = _supplement(service.store, run_id, basis, turn.tool_calls[0].input)
            proposal = {'id': payload.request_id, 'basis': basis,
                        'context_sha256': payload.expected_context_sha256,
                        'supplement': supplement, 'method_sha256': context['snapshot_sha256']}
            proposal['sha256'] = fingerprint(proposal)
            service.store.append_event(run_id, 'design_upgrade_proposed', proposal=proposal)
        reply = '设计补充方案已准备好。请查看人物、场景和逐镜绑定；原脚本与批准尚未改变，保存后新版仍需你采用。'
        service.store.append_chat(run_id, 'assistant', reply)
        from .agents.orchestrator import OrchestratorTurn
        return OrchestratorTurn(reply, 'done', 1)
    return run, {'roles': {'material': {'model': settings.model_for('material')}},
                 'purpose': 'design_supplement', 'max_calls': 1, 'media_generation': False}


def _save_stage(store, pipeline, run_id, proposal, stage):
    from .structured import visual_design_document
    original = store.read_artifact(run_id, stage, proposal['basis']['artifacts'][stage]['version'])
    text = store.read_artifact_text(run_id, stage, original.version)
    supplement = proposal['supplement']
    extra = (visual_design_document(supplement['design']) if stage == 'material_plan' else
             '\n'.join(f"{row['shot_id']} · 人物：{row['character_ids'] or '无人'} · 场景：{row['scene_id']}"
                       for row in supplement['bindings']))
    attempt = pipeline.start_attempt(run_id, stage, actor='user-design-upgrade')
    try:
        artifact = pipeline.submit(run_id, stage, attempt.token,
            content=text + '\n\n## 制作设计补充\n\n' + extra,
            summary='保留原内容，仅补制作设计与逐镜身份', structured=supplement[stage],
            evidence=original.evidence, limitations=[*original.limitations,
                '仅补制作设计；旧批准不继承，尚未生成或重新确认任何媒体。'], actor='user-design-upgrade')
    except Exception:
        pipeline.reject(run_id, stage, attempt.token, '设计补充未保存完成，原版本保留。')
        raise
    return {'version': artifact.version, 'sha256': artifact.sha256}


def _preflight_save(service, run_id, proposal):
    """Exercise both Pipeline writes in a disposable copy before touching sources."""
    import shutil
    import tempfile
    from pathlib import Path
    from .store import Store
    from .pipeline import Pipeline
    with tempfile.TemporaryDirectory(prefix='studio-design-save-preflight-') as folder:
        root = Path(folder).resolve()
        destination = root / 'runs' / run_id
        if not destination.resolve().is_relative_to(root):
            raise PipelineError('design_upgrade', '临时校验路径不合法。')
        shutil.copytree(service.store.run_dir(run_id), destination)
        preview_store = Store(root)
        preview_pipeline = Pipeline(preview_store)
        return {stage: _save_stage(preview_store, preview_pipeline, run_id, proposal, stage)
                for stage in ('material_plan', 'writing')}


def _require_resume_state(store, pipeline, run_id, proposal, started):
    if (started['proposal_sha256'] != proposal['sha256']
            or pipeline.require_prerequisites(run_id, 'material_plan') != started['inputs']):
        raise PipelineError('design_upgrade_changed', '设计补充的来源已变化，不能继续保存旧方案。')
    for stage in STAGES:
        state = pipeline.stage_state(run_id, stage)
        allowed = [proposal['basis']['artifacts'][stage], started['artifacts'].get(stage)]
        current = {'version': state.latest.version, 'sha256': state.latest.sha256} if state.latest else None
        if state.open_attempt or not state.integrity_ok or current not in allowed:
            raise PipelineError('design_upgrade_changed', '保存期间已有其他修改，请核对当前版本。')


def save_proposal(service, run_id, payload):
    if not payload.consent:
        raise PipelineError('design_upgrade_consent', '先核对设计补充，再明确保存新版本。')
    rows = service.store.read_events(run_id)
    applied = next((row for row in reversed(rows) if row.get('kind') == 'design_upgrade_applied'
                    and row.get('proposal_id') == payload.proposal_id), None)
    if applied:
        if applied['proposal_sha256'] != payload.expected_proposal_sha256:
            raise PipelineError('design_upgrade_changed', '相同补充方案身份对应不同内容。')
        return {'saved': True, 'artifacts': applied['artifacts'], 'already_saved': True}
    service.require_nonce(run_id, payload.nonce)
    proposal = next((row['proposal'] for row in reversed(rows)
        if row.get('kind') == 'design_upgrade_proposed' and row['proposal']['id'] == payload.proposal_id), None)
    if (not proposal or proposal['sha256'] != payload.expected_proposal_sha256
            or fingerprint({key: value for key, value in proposal.items() if key != 'sha256'}) != proposal['sha256']):
        raise PipelineError('design_upgrade_changed', '设计补充已变化，请重新打开准确方案。')
    verified = _supplement(service.store, run_id, proposal['basis'], {
        key: proposal['supplement'][key] for key in ('design', 'bindings')})
    if verified != proposal['supplement']:
        raise PipelineError('design_upgrade_changed', '设计补充内容无法核验，原方案保留。')
    started = next((row for row in reversed(rows) if row.get('kind') == 'design_upgrade_apply_started'
                    and row.get('proposal_id') == proposal['id']), None)
    if started:
        _require_resume_state(service.store, service.pipeline, run_id, proposal, started)
        expected = started['artifacts']
    else:
        _require_basis(service.store, service.pipeline, run_id, proposal['context_sha256'])
        expected = _preflight_save(service, run_id, proposal)
        service.store.append_event(run_id, 'design_upgrade_apply_started', proposal_id=proposal['id'],
            proposal_sha256=proposal['sha256'], artifacts=expected,
            inputs=service.pipeline.require_prerequisites(run_id, 'material_plan'), actor='user')
    artifacts = {}
    for stage in ('material_plan', 'writing'):
        current = service.store.latest_artifact(run_id, stage)
        if current and (current.version, current.sha256) == (expected[stage]['version'], expected[stage]['sha256']):
            artifacts[stage] = expected[stage]
            continue
        if service.store.next_version(run_id, stage) != expected[stage]['version']:
            raise PipelineError('design_upgrade_changed', '保存版本已变化，请重新核对补充方案。')
        artifacts[stage] = _save_stage(service.store, service.pipeline, run_id, proposal, stage)
        if artifacts[stage] != expected[stage]:
            raise PipelineError('design_upgrade_changed', '保存结果与已核对的补充方案不一致，请检查当前版本。')
    service.store.append_event(run_id, 'design_upgrade_applied', proposal_id=proposal['id'],
        proposal_sha256=proposal['sha256'], basis=proposal['basis'], artifacts=artifacts, actor='user')
    return {'saved': True, 'artifacts': artifacts, 'already_saved': False,
            'notice': '新设计和原脚本已保存，请从原脚本决定入口采用准确新版；旧图片仍保留，未批准或生成媒体。'}


def install_routes(app, service, require_run):
    from fastapi import HTTPException
    from .agents.client import SafePreflightError
    base = '/api/runs/{run_id}/production/design-upgrade'

    @app.post(base + '/propose')
    def propose(run_id: str, payload: ProposeUpgradeIn):
        require_run(run_id)
        try:
            return service.jobs.submit(run_id, '补齐制作设计，保留已采用方向、脚本与旧图片；先交补充方案供我核对。',
                payload.request_id, kind='design_upgrade', payload=payload.model_dump(exclude={'request_id'}),
                prepare=lambda _: prepare_proposal(service, run_id, payload))
        except (PipelineError, ValueError, SafePreflightError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get(base + '/proposal')
    def proposal(run_id: str):
        require_run(run_id)
        result = latest_proposal(service.store, run_id)
        if result:
            result = deepcopy(result)
            try:
                _require_basis(service.store, service.pipeline, run_id, result['context_sha256'])
                result['current'] = True
            except PipelineError:
                result['current'] = False
                started = next((row for row in reversed(service.store.read_events(run_id))
                    if row.get('kind') == 'design_upgrade_apply_started' and row.get('proposal_id') == result['id']), None)
                if started:
                    try:
                        _require_resume_state(service.store, service.pipeline, run_id, result, started)
                        result.update(current=True, partial_save=True)
                    except PipelineError:
                        pass
        return result

    @app.post(base + '/save')
    def save(run_id: str, payload: SaveUpgradeIn):
        require_run(run_id)
        try:
            with service.jobs.lock, service.store.run_lock(run_id):
                if service.jobs.has_executions():
                    raise PipelineError('design_upgrade_busy', '团队正在执行，请本轮结束后保存设计补充。')
                return save_proposal(service, run_id, payload)
        except (PipelineError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, '设计补充未保存完成；原版本保留。请检查运行目录，再打开同一份补充方案继续保存，无需重新调用模型。') from exc
