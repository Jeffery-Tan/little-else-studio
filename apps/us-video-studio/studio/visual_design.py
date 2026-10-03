"""Shared visual production design, carried by the existing stage artifacts.

This is a content contract and deterministic compiler, not another workflow,
approval path or assertion that generated pixels match the design.
"""
from __future__ import annotations

import hashlib
import json
import re

TEXT_FIELDS = ('audience', 'everyday_context', 'localization', 'style', 'product_appearance')
CHARACTER_FIELDS = ('id', 'label', 'relationship', 'appearance', 'wardrobe', 'performance')
SCENE_FIELDS = ('id', 'label', 'location', 'layout', 'props', 'lighting', 'local_details')
LIST_FIELDS = ('continuity', 'assumptions')
MAX_TEXT = 4000
MAX_ENTITIES = 20
ID_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TEXT:
        raise ValueError(f'人物与场景设计的 {field} 需要明确文字，且不超过 {MAX_TEXT} 字符。')
    return value.strip()


def normalize_visual_design(value):
    """Validate without dropping content or inventing missing locale/casting."""
    keys = {'schema_version', 'market', *TEXT_FIELDS, 'characters', 'scenes', *LIST_FIELDS}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('人物与场景设计字段不完整或含未知字段，请按共同设计契约补全。')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['market'] != 'US':
        raise ValueError('人物与场景设计只接受 schema_version=1、market=US。')
    design = {'schema_version': 1, 'market': 'US'}
    design.update({key: _text(value[key], key) for key in TEXT_FIELDS})
    for key, fields in (('characters', CHARACTER_FIELDS), ('scenes', SCENE_FIELDS)):
        rows = value[key]
        if not isinstance(rows, list) or len(rows) > MAX_ENTITIES or (key == 'scenes' and not rows):
            raise ValueError('人物与场景各最多 20 项，至少需要一个场景；无人画面允许 characters=[]。')
        entities = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(fields):
                raise ValueError(f'{key} 中每项须完整包含 {", ".join(fields)}。')
            entity = {field: _text(row[field], f'{key}.{field}') for field in fields}
            entity_id = entity['id']
            if not ID_RE.fullmatch(entity_id) or entity_id in seen:
                raise ValueError(f'{key} 的身份须为不重复的英文标识，不能用画面描述代替身份。')
            seen.add(entity_id)
            entities.append(entity)
        design[key] = entities
    for key in LIST_FIELDS:
        values = value[key]
        if not isinstance(values, list) or len(values) > MAX_ENTITIES or (key == 'continuity' and not values):
            raise ValueError('连续性规则须至少一条；连续性规则与创作假设各最多 20 条。')
        design[key] = [_text(item, key) for item in values]
    return design


def character_ids(value):
    if not isinstance(value, str):
        raise ValueError('分镜 character_ids 使用逗号分隔的身份字符串，无人物时明确写空字符串。')
    ids = [part.strip() for part in value.split(',') if part.strip()]
    if len(ids) != len(set(ids)) or any(not ID_RE.fullmatch(item) for item in ids):
        raise ValueError('分镜的人物身份须为不重复的英文标识。')
    return ids


def validate_segments(design, segments):
    if not isinstance(segments, list) or not segments:
        raise ValueError('脚本需要可核对的分镜，才能引用共同人物与场景设计。')
    characters = {row['id'] for row in design['characters']}
    scenes = {row['id'] for row in design['scenes']}
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict) or 'character_ids' not in row or 'scene_id' not in row:
            raise ValueError(f'第 {index} 镜需明确 character_ids 与 scene_id，不能靠自由文字猜测。')
        if not set(character_ids(row['character_ids'])) <= characters:
            raise ValueError(f'第 {index} 镜引用了共同设计中不存在的人物。')
        if not isinstance(row['scene_id'], str) or row['scene_id'].strip() not in scenes:
            raise ValueError(f'第 {index} 镜引用了共同设计中不存在的场景。')


def design_status(store, pipeline, run_id, *, stage_memo=None):
    """Read-only readiness, including legacy gaps; never migrate old bytes."""
    state = pipeline.stage_state(run_id, 'material_plan', stage_memo)
    artifact = state.latest
    result = {'ready': False, 'issues': [], 'design': None,
              'version': artifact.version if artifact else None,
              'sha256': artifact.sha256 if artifact else None}
    if not artifact or not (artifact.structured or {}).get('visual_design'):
        result['issues'].append('素材计划还没有共同人物与场景设计；先补齐设计，再更新分镜与制作输入。')
        return result
    if state.stale or not state.integrity_ok or state.open_attempt:
        result['issues'].append('共同人物与场景设计已过期、无法校验或正在修改，请先完成当前素材计划。')
        return result
    try:
        result['design'] = normalize_visual_design(artifact.structured['visual_design'])
    except ValueError as exc:
        result['issues'].append(str(exc))
        return result
    writing = pipeline.stage_state(run_id, 'writing', stage_memo)
    if writing.latest:
        try:
            validate_segments(result['design'], writing.latest.structured.get('segments'))
        except ValueError as exc:
            result['issues'].append(str(exc))
    result['ready'] = not result['issues']
    return result


def compile_visual_input(store, pipeline, run_id, *, source_version, source_sha256,
                         shot_id, kind, shot_prompt, references, stage_memo=None):
    status = design_status(store, pipeline, run_id, stage_memo=stage_memo)
    if not status['ready']:
        raise ValueError(' '.join(status['issues']))
    writing = pipeline.stage_state(run_id, 'writing', stage_memo)
    source = writing.latest
    if (not source or writing.stale or not writing.integrity_ok or writing.open_attempt
            or (source.version, source.sha256) != (source_version, source_sha256)):
        raise ValueError('当前脚本依据已变化，请按共同设计更新并重新采用分镜。')
    # The authoritative target list is shared with UI/delivery checks.
    from .production import target_shots
    targets = target_shots(store, run_id, kind)['shots']
    if not shot_id and len(targets) == 1:
        shot_id = targets[0]['shot_id']
    target = next((row for row in targets if row['shot_id'] == shot_id), None)
    if not target:
        raise ValueError('请指定当前分镜的准确 shot_id；多个镜头不能自动猜测。')
    index = target['ordinal'] - 1
    segment = source.structured['segments'][index]
    ids = character_ids(segment['character_ids'])
    scene_id = segment['scene_id'].strip()
    design = status['design']
    cast = [row for row in design['characters'] if row['id'] in ids]
    scene = next(row for row in design['scenes'] if row['id'] == scene_id)
    if kind not in {'image', 'video'} or not isinstance(references, list) or len(references) > 5:
        raise ValueError('图片最多五张用途明确的参考图，视频使用单一获批首帧。')
    for ref in references:
        if not isinstance(ref, dict) or ref.get('purpose') not in {'product', 'character', 'scene', 'continuity'}:
            raise ValueError('每张参考图需明确 product/character/scene/continuity 用途。')
        if ref['purpose'] == 'character' and not ids:
            raise ValueError('无人分镜不能接收 character 用途的参考图。')
        entity_id = ref.get('design_id')
        if entity_id and not ((ref['purpose'] == 'character' and entity_id in ids)
                              or (ref['purpose'] == 'scene' and entity_id == scene_id)):
            raise ValueError('参考图的设计身份必须属于该镜采用的人物或场景。')
    if kind == 'video' and (len(references) != 1 or references[0].get('purpose') != 'continuity'):
        raise ValueError('视频只接受一张 continuity 用途的获批首帧。')
    binding = {'material_plan': {'version': status['version'], 'sha256': status['sha256']},
               'writing': {'version': source.version, 'sha256': source.sha256},
               'design_sha256': fingerprint(design), 'character_ids': ids, 'scene_id': scene_id}
    shot = {'shot_id': shot_id, 'index': index,
            **{key: segment.get(key, '') for key in ('picture', 'line', 'sound', 'tc', 'duration')},
            'character_ids': ids, 'scene_id': scene_id}
    purpose_rules = {
        'product': 'Use for product appearance only. Do not inherit the supplier model, setting or printed marketing claims.',
        'character': 'Use for the designed character appearance. Do not inherit product claims or unrelated surroundings.',
        'scene': 'Use for the designed space, layout and local details. Do not import unrelated people or product claims.',
        'continuity': 'Preserve the approved frame identity, product, wardrobe, setting and composition; animate only the specified action.',
    }
    lines = [
        'SHARED VISUAL PRODUCTION DESIGN — US / TikTok Shop',
        'Honor this shared design and the adopted shot. Additional shot instructions must not replace their identities or invent product facts.',
        'Casting, setting and lifestyle details below are creative design choices, not verified customer experiences or market results.',
        'Consumer-facing speech and designed copy use en-US. Do not turn spoken lines into on-image text unless the adopted shot explicitly requires designed copy.',
    ]
    for key in TEXT_FIELDS:
        lines.append(f'{key}: {design[key]}')
    lines += ['Characters in this shot:', json.dumps(cast, ensure_ascii=False, sort_keys=True),
              'Scene in this shot:', json.dumps(scene, ensure_ascii=False, sort_keys=True),
              'Continuity rules:', *[f'- {item}' for item in design['continuity']],
              'Creative assumptions:', *[f'- {item}' for item in design['assumptions']],
              'Adopted shot:', json.dumps(shot, ensure_ascii=False, sort_keys=True),
              'Inline reference images, in this exact order:']
    for number, ref in enumerate(references, 1):
        lines.append(f'{number}. {ref["purpose"]}' + (f' / {ref["design_id"]}' if ref.get('design_id') else '')
                     + ': ' + purpose_rules[ref['purpose']])
    lines += ['Additional shot instructions:', shot_prompt]
    prompt = '\n'.join(lines)
    if len(prompt) > 30_000:
        raise ValueError('共同设计与分镜输入合计超过 30000 字符，请精简相关设计；内容不会被自动截断。')
    return {'compiled_prompt': prompt, 'design_binding': binding, 'shot_binding': shot,
            'references': references}


def validate_visual_binding(store, pipeline, run_id, draft, *, stage_memo=None):
    if not draft.get('design_binding') or not isinstance(draft.get('shot_prompt'), str):
        raise ValueError('这份历史制作输入没有绑定共同人物与场景设计，请按当前设计重新准备；旧稿与结果仍保留。')
    compiled = compile_visual_input(store, pipeline, run_id,
        source_version=draft['source_version'], source_sha256=draft['source_sha256'],
        shot_id=draft['shot_id'], kind=draft['kind'], shot_prompt=draft['shot_prompt'],
        references=draft.get('references', []), stage_memo=stage_memo)
    if (draft['design_binding'] != compiled['design_binding']
            or draft.get('shot_binding') != compiled['shot_binding']
            or draft['prompt'] != compiled['compiled_prompt']
            or draft.get('compiled_prompt') != compiled['compiled_prompt']):
        raise ValueError('人物、场景或分镜绑定已变化，请重新准备准确输入与生成确认。')
