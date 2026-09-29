"""Bring a user-supplied TikTok video into product evidence, without reference adoption."""
from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlsplit, urlunsplit

from .agents import bailian_omni
from .audiovisual_observation import prepare_video
from .image_analysis import AnalysisIn, candidates, prepare_analysis
from .material_observations import ObservationError
from .materials import accept_upload, resolve
from .models import PipelineError, STAGES, utcnow
from .tiktok_reference import checked_url, fetch_reference


def observe(store, pipeline, connections, settings, run_id, source_url, emit, check_cancel,
            *, endpoint, revision, client_factory=None):
    """Download once, observe as product, and leave the candidate for user selection."""
    checked = checked_url(source_url)
    public_url = urlunsplit(urlsplit(checked)._replace(query=''))
    if revision != connections.public()['revision']:
        raise PipelineError('product_video_connection', '模型设置已变化，请重新发送商品视频链接。')
    if any(pipeline.stage_state(run_id, stage).open_attempt for stage in STAGES):
        raise PipelineError('product_video_stage', '请先处理当前未完成的策划步骤，再分析商品视频。')
    frozen = bailian_omni.freeze(settings, endpoint)
    previous = next((e for e in reversed(store.read_events(run_id))
                     if e['kind'] == 'product_video_source' and e.get('source_url') == public_url), None)
    name = previous.get('name') if previous else None
    sha = previous.get('sha256') if previous else None
    if not name or not sha or not (path := resolve(store, run_id, name)) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        emit({'type': 'status', 'role': 'material', 'message': '正在获取用户指定的商品视频。'})
        raw, source = fetch_reference(checked, check_cancel)
        try:
            prepare_video(raw, check_cancel)
        except ObservationError as exc:
            raise PipelineError('product_video_invalid', str(exc)) from None
        name = f"tiktok-product-{source['video_id']}-{source['sha256'][:12]}.mp4"
        sha = source['sha256']
        check_cancel()
        with store.run_lock(run_id):
            existing = resolve(store, run_id, name)
            if existing and hashlib.sha256(existing.read_bytes()).hexdigest() != sha:
                raise PipelineError('product_video_conflict', '同名商品视频文件已变化，未覆盖。')
            if not existing:
                accept_upload(store, run_id, name, raw)
            store.append_event(run_id, 'product_video_source',
                               source_url=public_url, canonical_url=source['canonical_url'],
                               video_id=source['video_id'], name=name, sha256=sha,
                               acquired_at=utcnow())
    candidate = next((c for c in reversed(candidates(store, run_id))
                      if c['name'] == name and c['sha256'] == sha
                      and c['purpose'] == 'product' and c['status'] != 'stale'), None)
    if candidate is None:
        payload = AnalysisIn(request_id=uuid.uuid4().hex, name=name, expected_sha256=sha,
                             expected_revision=revision, connection_id=bailian_omni.CONNECTION,
                             model=bailian_omni.MODEL, purpose='product', consent=True,
                             expected_endpoint=endpoint)
        runner, _ = prepare_analysis(store, pipeline, connections, settings, (run_id, payload),
                                     client_factory=client_factory, video=True, _native_frozen=frozen)
        result = runner('', emit, check_cancel)
        candidate = next(c for c in candidates(store, run_id)
                         if c['analysis_id'] == result.analysis_id)
    return {'name': name, 'analysis_id': candidate['analysis_id'],
            'findings': candidate['findings'], 'limitations': candidate['limitations'],
            'status': candidate['status'], 'reused': previous is not None,
            'source_url': public_url}
