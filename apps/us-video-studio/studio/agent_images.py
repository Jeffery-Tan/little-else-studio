"""Bounded visual observation inside the original conversation job."""
import base64
import hashlib
import json
import uuid

from .agents import bailian_omni
from .agents.budget import BudgetedClient
from .image_analysis import read_image, LIMITATION, SYSTEM, _lines
from .models import PipelineError, utcnow
from .ledger import Ledger
from .agents.client import SafePreflightError, ModelError
from .material_observations import ObservationError
from .observation_image_input import prepare as prepare_image_input, COMPRESSION_LIMITATION

MAX_IMAGES = 12


class ImagePreflightError(PipelineError):
    """Input/setup failed before any observation provider invocation."""

    def __init__(self, message):
        super().__init__('image_input', message)


def saved_observations(store, run_id):
    """Latest observation per immutable source and purpose, never a human adoption."""
    result = {}
    for event in store.read_events(run_id):
        if event['kind'] == 'agent_images_observed':
            for item in event['observations']:
                result[(item['name'], item['sha256'], item['purpose'])] = item
    return list(result.values())


def _image_sources(store, run_id):
    from .evidence import evidence_snapshot
    snapshot = evidence_snapshot(store, run_id)
    if snapshot['errors']:
        raise ImagePreflightError('图片来源记录无法核对，请先处理素材来源错误。')
    return {m['name']: m for m in snapshot['materials'] if m['kind'] == 'image'}


def validate_image_use(store, run_id, receipt, image_use):
    """Validate exact immutable sources; interpretation never adopts facts."""
    from .conversation_input import _quoted
    if (not isinstance(image_use, dict) or set(image_use) != {'quote', 'images'}
            or not isinstance(image_use['images'], list)
            or not 1 <= len(image_use['images']) <= MAX_IMAGES):
        raise ImagePreflightError('图片用途须绑定原话和 1 至 12 张准确图片。')
    _quoted(receipt['text'], image_use['quote'])
    sources = _image_sources(store, run_id)
    seen = set()
    attachments = receipt.get('attachments') or []
    attached = {a['name']: a.get('purpose') for a in attachments if a['name'] in sources}
    for item in image_use['images']:
        if (not isinstance(item, dict) or set(item) != {'name', 'sha256', 'purpose'}
                or not isinstance(item['name'], str) or item['name'] in seen
                or item['purpose'] not in {'product', 'reference'}
                or item['name'] not in sources
                or item['sha256'] != sources[item['name']]['sha256']):
            raise ImagePreflightError('图片必须来自本项目且不重复，名称、原图指纹与用途须准确匹配。')
        seen.add(item['name'])
        if attachments and (item['name'] not in attached or attached[item['name']] != item['purpose']):
            raise ImagePreflightError('本轮图片来源及用途须与真实附件一致。')
        try:
            read_image(store, run_id, item['name'], item['sha256'])
        except ObservationError as exc:
            raise ImagePreflightError(str(exc)) from None
    expected = set(attached) if attachments else {
        name for name, source in sources.items() if source.get('origin') != 'generated'}
    # No keywords/count guessing: an unspecified set is the whole supplied set.
    # A narrower selection requires literal resource identities in the receipt.
    if seen != expected and not all(name in receipt['text'] for name in seen):
        raise ImagePreflightError('不能用所选少量图片缩小本轮整组分析；子集须绑定原话中的准确图片名。')


def bind_image_analysis(store, run_id, receipt, interpretation, items, *, turn_id):
    """Freeze analysis targets in the existing receipt/interpretation journal."""
    from . import conversation_input
    with store.run_lock(run_id):
        trusted = conversation_input.message(store, run_id, (receipt or {}).get('message_id'))
        current = conversation_input.interpretation_for(store, run_id, trusted['message_id'])
        if (not current or current['id'] != (interpretation or {}).get('id')
                or trusted['text'] != receipt.get('text')
                or not any(e['kind'] == 'user_message_execution_started'
                           and e.get('message_id') == trusted['message_id'] for e in store.read_events(run_id))
                or current['task']['mode'] != 'analyze' or current['task']['deliverable'] != 'analysis'
                or current.get('video_use') or current['task']['allowed_stages']
                or current['task']['format'] == 'video'):
            raise ImagePreflightError('图片分析须绑定本轮已执行的只读分析原话，状态、讨论、暂停或视频任务不能启动图片观察。')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_IMAGES:
            raise ImagePreflightError('一次选择 1 至 12 张本项目图片。')
        sources = _image_sources(store, run_id)
        if any(not isinstance(item, dict) or item.get('name') not in sources
               or item.get('purpose') not in {'product', 'reference'} for item in items):
            raise ImagePreflightError('图片必须来自本项目且不重复，明确商品或参考用途。')
        use = current.get('image_use')
        if use is None:
            attachments = trusted.get('attachments') or []
            if attachments:
                targets = [{'name': a['name'], 'sha256': sources.get(a['name'], {}).get('sha256'),
                            'purpose': a.get('purpose')} for a in attachments if a['name'] in sources]
            else:
                purposes = {item['purpose'] for item in items}
                if len(purposes) != 1:
                    raise ImagePreflightError('不同图片用途须通过准确 image_use 来源绑定，不能推测未选图片的用途。')
                purpose = next(iter(purposes))
                targets = [{'name': name, 'sha256': source['sha256'], 'purpose': purpose}
                           for name, source in sources.items() if source.get('origin') != 'generated']
            use = {'quote': current['task']['quote'], 'images': targets}
        validate_image_use(store, run_id, trusted, use)
        by_name = {item['name']: item for item in use['images']}
        if (len({item['name'] for item in items}) != len(items)
                or any(item['name'] not in by_name or item['purpose'] != by_name[item['name']]['purpose']
                       for item in items)):
            raise ImagePreflightError('观察选择须属于本轮固定的图片目标，不能改名或改用途。')
        if {item['name'] for item in items} != set(by_name):
            raise ImagePreflightError('本轮要求整组图片分析，请一次选择全部固定目标；不能只观察少量图片并称整组完成。')
        if 'image_use' not in current:
            store.append_event(run_id, 'user_image_use_bound', source_message_id=trusted['message_id'],
                               interpretation_id=current['id'], image_use=use)
        # Explicit invocation, including cache reuse, connects this turn to the
        # fixed sources. Merely reading past observations cannot complete it.
        store.append_event(run_id, 'agent_image_analysis_requested', turn_id=turn_id,
                           source_message_id=trusted['message_id'], interpretation_id=current['id'])
        return [dict(by_name[item['name']]) for item in items]


def analysis_coverage(store, run_id, source_message_id, turn_id):
    """None means a non-image task. Coverage comes from saved source evidence."""
    from . import conversation_input
    interpretation = conversation_input.interpretation_for(store, run_id, source_message_id)
    if not interpretation or not interpretation.get('image_use') or interpretation.get('video_use'):
        return None
    targets = interpretation['image_use']['images']
    requested = any(e['kind'] == 'agent_image_analysis_requested'
                    and e.get('source_message_id') == source_message_id and e.get('turn_id') == turn_id
                    and e.get('interpretation_id') == interpretation['id'] for e in store.read_events(run_id))
    cache = {(r['name'], r['sha256'], r['purpose']): r for r in saved_observations(store, run_id)}
    try:
        _image_sources(store, run_id)
    except ImagePreflightError:
        return {'total': len(targets), 'observed': 0, 'missing': [t['name'] for t in targets],
                'requested': requested}
    covered, missing = [], []
    for target in targets:
        key = (target['name'], target['sha256'], target['purpose'])
        row = cache.get(key)
        valid = requested and row and row.get('analysis_id') and row.get('call_id')
        valid = valid and row.get('source_kind') == 'agent_model_observation'
        valid = valid and all(isinstance(row.get(field), list) and row[field]
                              and all(isinstance(line, str) and line.strip() for line in row[field])
                              for field in ('findings', 'limitations'))
        try:
            read_image(store, run_id, target['name'], target['sha256'])
        except ObservationError:
            valid = False
        (covered if valid else missing).append(target['name'])
    return {'total': len(targets), 'observed': len(covered), 'missing': missing,
            'requested': requested}


def _result_diagnostic(turn, batch, *, single_report=False):
    """Fixed structure only: never retain content, unknown keys or reasoning."""
    raw = turn.text.encode('utf-8')
    known_stop = {'end_turn', 'tool_use', 'max_tokens', 'stop_sequence'}
    diagnostic = {'category': 'invalid_image_result', 'body_bytes': len(raw),
                  'body_sha256': hashlib.sha256(raw).hexdigest(), 'expected_images': len(batch),
                  'has_tool_calls': bool(turn.tool_calls),
                  'expected_finish_matches': turn.stop_reason == 'end_turn',
                  'actual_stop_reason': turn.stop_reason if turn.stop_reason in known_stop else 'unknown'}
    if single_report:
        diagnostic['response_kind'] = 'single_image_json_report'
    if len(turn.text) > 45000:
        diagnostic['structure_inspected'] = False
        return diagnostic
    try:
        if single_report:
            from .image_understanding import decode_single_report
            value = decode_single_report(turn.text)
        else:
            text = turn.text.strip()
            if text.startswith('```json\n') and text.endswith('```'):
                text = text[8:-3]
            value = json.loads(text)
    except (ValueError, TypeError):
        diagnostic['json_valid'] = False
        return diagnostic
    diagnostic['json_valid'] = True
    kind = lambda value: ('object' if isinstance(value, dict) else 'array' if isinstance(value, list)
                         else 'string' if isinstance(value, str) else 'null' if value is None
                         else 'boolean' if isinstance(value, bool) else 'number')
    diagnostic['root_type'] = kind(value)
    if single_report:
        diagnostic['source_id_matches'] = isinstance(value, dict) and value.get('id') == batch[0][0]['id']
        diagnostic['report_fields'] = {field: {
            'present': isinstance(value, dict) and field in value,
            'type': kind(value.get(field)) if isinstance(value, dict) else 'null',
            **({'count': len(value[field])} if isinstance(value, dict) and isinstance(value.get(field), list) else {})}
            for field in ('id', 'findings', 'limitations')}
        return diagnostic
    rows = value.get('images') if isinstance(value, dict) else None
    diagnostic['images_present'] = isinstance(value, dict) and 'images' in value
    diagnostic['images_type'] = kind(rows)
    if isinstance(rows, list):
        diagnostic['images_count'] = len(rows)
        expected = {item['id'] for item, _, _ in batch}
        identifiers = [row.get('id') for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)]
        diagnostic['source_id_matches'] = len(set(identifiers) & expected)
        diagnostic['duplicate_id_count'] = len(identifiers) - len(set(identifiers))
        diagnostic['rows'] = [{field: {'present': isinstance(row, dict) and field in row,
            'type': kind(row.get(field)) if isinstance(row, dict) else 'null',
            **({'count': len(row[field])} if isinstance(row, dict) and isinstance(row.get(field), list) else {})}
            for field in ('id', 'findings', 'limitations')} for row in rows[:MAX_IMAGES]]
    return diagnostic


def observe(store, settings, run_id, items, *, endpoint, emit, check_cancel, client_factory=None,
            freeze_route=None, cancel_event=None):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_IMAGES:
        raise ImagePreflightError('一次选择 1 至 12 张本项目图片。')
    from .evidence import evidence_snapshot
    materials = {m['name']: m for m in evidence_snapshot(store, run_id)['materials']}
    cache = {(r['name'], r['sha256'], r['purpose']): r for r in saved_observations(store, run_id)}
    seen, reused, pending = set(), [], []
    for item in items:
        name, purpose = item.get('name'), item.get('purpose')
        if name in seen or purpose not in ('product', 'reference') or name not in materials:
            raise ImagePreflightError('图片必须来自本项目且不重复，明确商品或参考用途。')
        seen.add(name)
        material = materials[name]
        if material['kind'] != 'image' or item.get('sha256', material['sha256']) != material['sha256']:
            raise ImagePreflightError('图片来源的类型或原图指纹已改变，尚未调用模型。')
        try:
            raw, mime = read_image(store, run_id, name, material['sha256'])
        except ObservationError as exc:
            raise ImagePreflightError(str(exc)) from None
        existing = cache.get((name, material['sha256'], purpose))
        if existing:
            reused.append(existing)
            continue
        try:
            raw, mime, image_input = prepare_image_input(raw, mime)
        except ObservationError as exc:
            raise ImagePreflightError(str(exc)) from None
        ident = f'image-{len(pending) + 1}'
        pending.append((dict(id=ident, name=name, sha256=material['sha256'], purpose=purpose,
                             image_input=image_input), raw, mime))
    if not pending:
        return {'observations': reused, 'reused': len(reused)}
    check_cancel()
    try:
        frozen, native_client, connection = (freeze_route() if freeze_route is not None else
                                            bailian_omni.freeze(settings, endpoint, check_cancel))
    except SafePreflightError as exc:
        raise ImagePreflightError(str(exc)) from None
    native_client.check_cancel = check_cancel
    if hasattr(native_client, 'cancel_event'):
        native_client.cancel_event = cancel_event
    model = connection['suggested_model']
    # A real MiMo multi-image response swapped two image IDs despite correct
    # source/inline/wire hashes. One pixel source per call removes that binding
    # ambiguity; the existing Omni byte-bounded batches remain unchanged.
    single_image = connection['provider'] == 'mimo'
    batches, batch, total = [], [], 0
    for entry in pending:
        if len(entry[1]) > bailian_omni.MAX_INLINE_BYTES:
            raise ImagePreflightError('压缩后的单张图片仍超过本次模型输入上限，尚未调用模型。')
        if batch and (single_image or total + len(entry[1]) > bailian_omni.MAX_INLINE_BYTES):
            batches.append(batch)
            batch, total = [], 0
        batch.append(entry)
        total += len(entry[1])
    batches.append(batch)
    client = client_factory() if client_factory else native_client
    ledger = Ledger(store, frozen)
    system = SYSTEM.split('仅返回 JSON 对象：')[0] + '\n本次为多图观察，仅返回 {"images":[{"id":"image-1","findings":["可见内容"],"limitations":["限制"]}]}。每张输入各一项，准确对应 id，不混用不同图片的特征。每图简要列 2–6 条关键观察和具体限制。'
    completed = []
    for index, batch in enumerate(batches, 1):
        check_cancel()
        if freeze_route is not None:
            try:
                freeze_route()
            except SafePreflightError as exc:
                if not completed:
                    raise ImagePreflightError(str(exc)) from None
                raise PipelineError('image_connection', str(exc) +
                    f'此前已保存 {len(completed)} 张观察；后续图片尚未提交，不自动重试。') from None
        emit({'type': 'status', 'role': 'material',
              'message': (f"正在查看图片 {index}/{len(batches)}：{batch[0][0]['name']}（本轮已保存 {len(completed)} 张观察）。"
                          if single_image else
                          f'正在查看 {len(batch)} 张图片（第 {index}/{len(batches)} 批，已准备压缩或原图输入）。')})
        blocks = []
        for item, raw, mime in batch:
            blocks.extend([{'type': 'text', 'text': f"图片 {item['id']}；用途 {item['purpose']}；文件名只是索引，不是事实：{item['name']}"
                           + '\n只描述这张图可辨的形态；图中文字注明为文字声明，不能确认的功能、材质、数量或细节明确未知，不借其他图片或文件名补齐。'
                           + '\n实际图片输入：' + json.dumps(item['image_input'], ensure_ascii=False)},
                           {'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
                                                       'data': base64.b64encode(raw).decode('ascii')}}])
        before = {r.call_id for r in ledger.entries(run_id)}
        if single_image:
            from .image_understanding import single_report_prompt
            report_system = SYSTEM.split('仅返回 JSON 对象：')[0] + single_report_prompt(batch[0][0]['id'])
        try:
            budgeted = BudgetedClient(client, ledger, run_id=run_id, role='material', stage='image_analysis')
            budgeted.public_progress = lambda event: emit({**event, 'role': 'material'})
            turn = budgeted.complete(
                model=model, system=report_system if single_image else system,
                tools=[], messages=[{'role': 'user', 'content': blocks}])
        except SafePreflightError as exc:
            if not completed:
                raise ImagePreflightError(str(exc)) from None
            raise
        except ModelError as exc:
            if not completed and exc.provider_invoked is False:
                raise ImagePreflightError(str(exc)) from None
            issue = exc.response_diagnostic or {}
            if (single_image and issue.get('field') in {'tool_arguments', 'tool_call', 'tool_calls'}
                    and issue.get('code') in {'invalid_arguments_schema', 'invalid_arguments_json',
                        'expected_object', 'expected_json_string', 'invalid_identity',
                        'finish_reason_mismatch', 'expected_function', 'expected_array'}):
                call = next(r for r in ledger.entries(run_id) if r.call_id not in before)
                from .agents.requests import safe_response_diagnostic
                store.append_event(run_id, 'agent_image_result_invalid', call_id=call.call_id,
                    provider=call.provider, diagnostic={'category': 'invalid_image_result',
                        'response_kind': 'single_image_json_report',
                        'provider_diagnostic': safe_response_diagnostic(issue)})
                raise PipelineError('image_result', f'第 {index} 张模型图片观察格式不符合要求；此前已保存 {len(completed)} 张。'
                                    '本次调用已记录，不自动重试。') from None
            raise
        try:
            if len(turn.text) > 45000:
                raise ValueError()
            if single_image:
                from .image_understanding import decode_single_report, validate_single_report
                if turn.tool_calls or turn.stop_reason != 'end_turn':
                    raise ValueError()
                rows = [validate_single_report(decode_single_report(turn.text), batch[0][0]['id'])]
            else:
                if turn.tool_calls or turn.stop_reason != 'end_turn':
                    raise ValueError()
                text = turn.text.strip()
                if text.startswith('```json\n') and text.endswith('```'):
                    text = text[8:-3]
                rows = json.loads(text)['images']
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise ValueError()
            by_id = {r['id']: r for r in rows}
            if set(by_id) != {item['id'] for item, _, _ in batch}:
                raise ValueError()
            for item, _, _ in batch:
                row = by_id[item['id']]
                if single_image:
                    findings, limitations = list(row['findings']), list(row['limitations'])
                else:
                    findings, limitations = _lines(row['findings'], '观察'), _lines(row['limitations'], '限制')
                    if max(len(findings), len(limitations)) > 20 or any(len(x) > 1000 for x in findings + limitations):
                        raise ValueError()
                if item['image_input']['processing'] != 'original':
                    limitations.append(COMPRESSION_LIMITATION)
                item.update(findings=findings, limitations=limitations + [LIMITATION])
        except (KeyError, TypeError, ValueError):
            call = next(r for r in ledger.entries(run_id) if r.call_id not in before)
            store.append_event(run_id, 'agent_image_result_invalid', call_id=call.call_id,
                               provider=call.provider, diagnostic=_result_diagnostic(turn, batch, single_report=single_image))
            raise PipelineError('image_result', f'第 {index} 批模型图片观察格式不符合要求，未返回完整结果；此前已保存 {len(completed)} 张。'
                                '本批调用已记账，不自动重试。') from None
        check_cancel()
        call = next(r for r in ledger.entries(run_id) if r.call_id not in before)
        observations = []
        for item, _, _ in batch:
            # Bind observation to the unchanged source, not the compressed copy.
            read_image(store, run_id, item['name'], item['sha256'])
            item.update(analysis_id=uuid.uuid4().hex, call_id=call.call_id, actual_model=turn.model,
                        connection_id=call.connection_id, provider=call.provider,
                        connection_revision=connection.get('revision'),
                        observed_at=utcnow(), level='image_observed', source_kind='agent_model_observation',
                        observer='model:' + turn.model, coverage='whole still image',
                        method='主制片调用视觉模型读取绑定原图的观察输入，未经人工核实')
            item.pop('id')
            observations.append(item)
        store.append_event(run_id, 'agent_images_observed', observations=observations)
        completed.extend(observations)
    return {'observations': reused + completed, 'reused': len(reused), 'batches': len(batches),
            'notice': '可用于策划的模型观察，不是人工确认或性能证据。'}
