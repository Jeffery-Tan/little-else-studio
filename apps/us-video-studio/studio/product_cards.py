"""Human-curated, immutable US creative-input cards kept in existing run events.

Cards are snapshots for new briefs, not a replacement product fact database.
They never copy assets, artifacts, approvals, or execution authorizations.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .models import PipelineError
from .store import sha256_text


def fingerprint(value):
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


class CardBinding(BaseModel):
    source_run_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,63}$')
    card_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class SourceBinding(BaseModel):
    kind: Literal['brief', 'intake']
    version: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class CardIn(BaseModel):
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    source: SourceBinding
    name: str = Field(min_length=1, max_length=120)
    facts: str = Field(min_length=1, max_length=6000)
    evidence: str = Field(min_length=1, max_length=3000)
    limitations: str = Field(min_length=1, max_length=3000)


def source_snapshot(store, pipeline, run_id):
    run = store.read_run(run_id)
    if run.market != 'US':
        raise ValueError('资料卡仅限 US 创作空间。')
    state = pipeline.stage_state(run_id, 'intake')
    artifact = state.latest
    if artifact and artifact.digest_algorithm == 'artifact-v2' and state.integrity_ok and not state.stale:
        facts = '\n'.join(f"{r.get('label', '')}：{r.get('value', '')}（来源：{r.get('source') or '尚未提供'}）"
                          for r in artifact.structured.get('facts', []))
        limits = [*artifact.limitations, *artifact.structured.get('constraints', []),
                  *[r.get('question', '') for r in artifact.structured.get('open_questions', [])]]
        return {'source': {'kind': 'intake', 'version': artifact.version, 'sha256': artifact.sha256},
                'source_title': run.title, 'source_label': f'商品与需求 · 版本 {artifact.version}',
                'name': run.title[:120], 'facts': facts,
                'evidence': '\n'.join(artifact.evidence), 'source_limits': list(dict.fromkeys(filter(None, limits))),
                'source_text': store.read_artifact_text(run_id, 'intake', artifact.version)}
    return {'source': {'kind': 'brief', 'version': 0, 'sha256': fingerprint(run.to_dict())},
            'source_title': run.title, 'source_label': '原始项目需求', 'name': run.title[:120],
            'facts': '', 'evidence': '本项目需求原话；商品信息需结合实际证据核对。',
            'source_limits': ['来源为原始需求，未自动核验商品事实或继承历史稿结论。'],
            'source_text': run.brief}


def cards_for_run(store, run_id):
    cards = []
    for event in store.read_events(run_id):
        if event.get('kind') != 'product_card_saved':
            continue
        card = event['card']
        if event.get('card_sha256') != fingerprint(card):
            raise ValueError('商品资料卡记录已变化，请检查来源项目。')
        cards.append({**card, 'sha256': event['card_sha256'], 'saved_at': event['at']})
    return cards


def save_card(store, pipeline, run_id, payload):
    with store.run_lock(run_id):
        identity = fingerprint(payload.model_dump())
        for card in cards_for_run(store, run_id):
            if card['card_id'] == payload.request_id:
                if card['request_sha256'] != identity:
                    raise ValueError('相同保存请求已用于不同资料，原卡片没有覆盖。')
                return card
        source = source_snapshot(store, pipeline, run_id)
        if payload.source.model_dump() != source['source']:
            raise ValueError('来源方案已更新，请重新打开并核对商品资料。')
        fields = {key: getattr(payload, key).strip() for key in ('name', 'facts', 'evidence', 'limitations')}
        if not all(fields.values()):
            raise ValueError('请填写商品名称、商品信息、证据来源以及未知项。')
        card = {'card_id': payload.request_id, 'source_run_id': run_id, 'market': 'US',
                'request_sha256': identity, **fields,
                **{key: source[key] for key in ('source', 'source_title', 'source_label', 'source_limits')}}
        event = store.append_event(run_id, 'product_card_saved', actor='user', card=card,
                                   card_sha256=fingerprint(card))
        return {**card, 'sha256': event['card_sha256'], 'saved_at': event['at']}


def resolve_card(store, binding):
    run = store.read_run(binding.source_run_id)
    if run.market != 'US':
        raise ValueError('不能跨线路复用商品资料。')
    card = next((c for c in cards_for_run(store, run.run_id) if c['card_id'] == binding.card_id), None)
    if not card or card['sha256'] != binding.sha256:
        raise ValueError('所选资料卡已不可核对，请重新选择。')
    source = card['source']
    if source['kind'] == 'brief':
        valid = source['sha256'] == fingerprint(run.to_dict())
    else:
        valid = (store.verify_artifact(run.run_id, 'intake', source['version']) and
                 store.read_artifact(run.run_id, 'intake', source['version']).sha256 == source['sha256'])
    if not valid:
        raise ValueError('资料卡的来源文件已变化，请核对原项目后另存资料。')
    return card


def compose_brief(brief, card):
    text = (brief + '\n\n---\n复用的商品创作资料（US）\n'
            '这是用户整理的来源快照，未自动重新核验，不代表当前售价、库存或性能已确认；'
            '不包含原项目的创意选择、审核、制作或费用授权。\n'
            f"商品：{card['name']}\n商品信息：\n{card['facts']}\n证据来源：\n{card['evidence']}\n"
            f"未知与限制：\n{card['limitations']}\n来源保留的边界：\n" + '\n'.join(card['source_limits']) +
            f"\n资料来源：{card['source_title']} · {card['source_label']} · 保存于 {card['saved_at']}\n")
    if len(text) > 20000:
        raise ValueError('资料卡与本次需求合计过长，请缩短需求或整理更精简的资料卡。')
    return text.strip()


def create_with_card(store, payload):
    """One locked, idempotent creation; frozen input is embedded in immutable run.json."""
    with store.run_lock(payload.run_id):
        card = resolve_card(store, payload.product_card)
        brief = compose_brief(payload.brief.strip(), card)
        if (store.run_dir(payload.run_id) / 'run.json').exists():
            existing = store.read_run(payload.run_id)
            if existing.title != payload.title.strip() or existing.brief != brief:
                raise ValueError('项目标识已用于不同内容，请重新新建项目。')
            run = existing
        else:
            run = store.create_run(payload.run_id, payload.title, brief)
        # Retry repairs an interrupted provenance append without duplicating a run.
        if not any(e.get('kind') == 'product_card_reused' for e in store.read_events(payload.run_id)):
            store.append_event(payload.run_id, 'product_card_reused', actor='user', card=card,
                               binding=payload.product_card.model_dump())
        return run


def install_product_card_routes(app, jobs, require_run):
    store, pipeline = jobs.store, jobs.pipeline

    @app.get('/api/product-cards')
    def catalog():
        try:
            cards = [card for run in store.list_runs() if run.market == 'US'
                     for card in cards_for_run(store, run.run_id)]
            return {'cards': sorted(cards, key=lambda c: c['saved_at'], reverse=True)}
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/product-cards')
    def workspace(run_id: str):
        require_run(run_id)
        try:
            with store.run_lock(run_id):
                return {**source_snapshot(store, pipeline, run_id), 'cards': cards_for_run(store, run_id)}
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/product-cards')
    def save(run_id: str, payload: CardIn):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.has_executions():
                    raise ValueError('请等待制作任务结束后再整理商品资料。')
                return save_card(store, pipeline, run_id, payload)
        except (ValueError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc
