"""Bounded visual observation inside the original conversation job."""
import base64
import json
import uuid

from .agents import bailian_omni
from .agents.budget import BudgetedClient
from .image_analysis import read_image, LIMITATION, SYSTEM, _lines
from .models import PipelineError, utcnow
from .ledger import Ledger

MAX_IMAGES = 12


def saved_observations(store, run_id):
    """Latest observation per immutable source and purpose, never a human adoption."""
    result = {}
    for event in store.read_events(run_id):
        if event['kind'] == 'agent_images_observed':
            for item in event['observations']:
                result[(item['name'], item['sha256'], item['purpose'])] = item
    return list(result.values())


def observe(store, settings, run_id, items, *, endpoint, emit, check_cancel, client_factory=None):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_IMAGES:
        raise PipelineError('image_input', '一次选择 1 至 12 张本项目图片。')
    from .evidence import evidence_snapshot
    materials = {m['name']: m for m in evidence_snapshot(store, run_id)['materials']}
    cache = {(r['name'], r['sha256'], r['purpose']): r for r in saved_observations(store, run_id)}
    seen, reused, pending, blocks, total = set(), [], [], [], 0
    for item in items:
        name, purpose = item.get('name'), item.get('purpose')
        if name in seen or purpose not in ('product', 'reference') or name not in materials:
            raise PipelineError('image_input', '图片必须来自本项目且不重复，明确商品或参考用途。')
        seen.add(name)
        material = materials[name]
        raw, mime = read_image(store, run_id, name, material['sha256'])
        existing = cache.get((name, material['sha256'], purpose))
        if existing:
            reused.append(existing)
            continue
        total += len(raw)
        ident = f'image-{len(pending) + 1}'
        pending.append(dict(id=ident, name=name, sha256=material['sha256'], purpose=purpose))
        blocks.extend([{'type': 'text', 'text': f'图片 {ident}；用途 {purpose}；文件名只是索引，不是事实：{name}'},
                       {'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                                                   'data': base64.b64encode(raw).decode('ascii')}}])
    if not pending:
        return {'observations': reused, 'reused': len(reused)}
    if total > bailian_omni.MAX_INLINE_BYTES:
        raise PipelineError('image_input', '本次未发送：所选图片合计超过 7 MB，请缩小观察范围或使用较小图片。')
    check_cancel()
    frozen, native_client, _ = bailian_omni.freeze(settings, endpoint, check_cancel)
    client = client_factory() if client_factory else native_client
    ledger = Ledger(store, frozen)
    emit({'type': 'status', 'role': 'material', 'message': f'正在查看 {len(pending)} 张图片。'})
    system = SYSTEM.split('仅返回 JSON 对象：')[0] + '\n本次为多图观察，仅返回 {"images":[{"id":"image-1","findings":["可见内容"],"limitations":["限制"]}]}。每张输入各一项，准确对应 id，不混用不同图片的特征。每图简要列 2–6 条关键观察和具体限制。'
    before = {r.call_id for r in ledger.entries(run_id)}
    turn = BudgetedClient(client, ledger, run_id=run_id, role='material', stage='image_analysis').complete(
        model=bailian_omni.MODEL, system=system, tools=[], messages=[{'role': 'user', 'content': blocks}])
    try:
        if turn.tool_calls or turn.stop_reason != 'end_turn' or len(turn.text) > 45000:
            raise ValueError()
        text = turn.text.strip()
        if text.startswith('```json\n') and text.endswith('```'):
            text = text[8:-3]
        rows = json.loads(text)['images']
        if not isinstance(rows, list) or len(rows) != len(pending):
            raise ValueError()
        by_id = {r['id']: r for r in rows}
        if set(by_id) != {r['id'] for r in pending}:
            raise ValueError()
        for item in pending:
            row = by_id[item['id']]
            findings, limitations = _lines(row['findings'], '观察'), _lines(row['limitations'], '限制')
            if max(len(findings), len(limitations)) > 20 or any(len(x) > 1000 for x in findings + limitations):
                raise ValueError()
            item.update(findings=findings, limitations=limitations + [LIMITATION])
    except (KeyError, TypeError, ValueError):
        raise PipelineError('image_result', '图片观察未返回完整结果，未写入依据；本次调用已记账，不自动重试。') from None
    check_cancel()
    call = next(r for r in ledger.entries(run_id) if r.call_id not in before)
    for item in pending:
        # Recheck source bytes before making the observation usable.
        read_image(store, run_id, item['name'], item['sha256'])
        item.update(analysis_id=uuid.uuid4().hex, call_id=call.call_id, actual_model=turn.model,
                    observed_at=utcnow(), level='image_observed', source_kind='agent_model_observation',
                    observer='model:' + turn.model, coverage='whole still image',
                    method='主制片调用视觉模型读取实际图片，未经人工核实')
        item.pop('id')
    store.append_event(run_id, 'agent_images_observed', observations=pending)
    return {'observations': reused + pending, 'reused': len(reused),
            'notice': '可用于策划的模型观察，不是人工确认或性能证据。'}
