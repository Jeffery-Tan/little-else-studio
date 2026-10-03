"""Server-owned shot identities and scoped, exact reuse dependencies."""
from __future__ import annotations

import hashlib
import json
import re


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _content(row):
    return {key: value for key, value in row.items()
            if key not in {'shot_id', 'page_id', 'ordinal', 'tc'}}


def validate_claims(rows, previous, *, photo=False):
    """Validate even claims on rows normalization would otherwise drop."""
    prior = {row.get('shot_id') or f'shot-{index:02d}': row
             for index, row in enumerate(previous, 1) if isinstance(row, dict)}
    pages = {row.get('page_id') or identity.replace('shot-', 'page-', 1): identity
             for identity, row in prior.items()}
    claims = []
    for row in rows:
        if not isinstance(row, dict):
            claims.append('')
            continue
        if any(key in row and row[key] not in (None, '') and not isinstance(row[key], str)
               for key in ('shot_id', 'page_id')):
            raise ValueError('shot_id/page_id 必须使用服务器提供的字符串身份。')
        identity, page = row.get('shot_id') or '', row.get('page_id') or ''
        if identity and identity not in prior:
            raise ValueError('shot_id 必须来自当前上一版脚本；新增镜头省略 ID，由服务器分配。')
        if page:
            if not photo or page not in pages:
                raise ValueError('page_id 必须来自当前上一版图文脚本；新增页面省略 ID。')
            if identity and identity != pages[page]:
                raise ValueError('shot_id 与 page_id 指向了不同页面。')
            identity = pages[page]
        if identity and identity in claims:
            raise ValueError('同一脚本中的 shot_id/page_id 不能重复；复制为新镜头时省略 ID。')
        claims.append(identity)
    return claims


def bind_rows(rows, previous, history, *, photo=False):
    """Retain valid prior identities; allocate new ones without recycling IDs.

    Unlabelled unchanged rows can retain an unambiguous exact-content match.
    Changed/new rows without a claim receive a new identity, never an ordinal
    guess. Explicit unknown or duplicate claims fail before an artifact saves.
    """
    previous = [row for row in previous if isinstance(row, dict)]
    prior = {row.get('shot_id') or f'shot-{index:02d}': row
             for index, row in enumerate(previous, 1)}
    pages = {row.get('page_id') or identity.replace('shot-', 'page-', 1): identity
             for identity, row in prior.items()}
    used = {row.get('shot_id') or f'shot-{index:02d}'
            for version in history for index, row in enumerate(version, 1) if isinstance(row, dict)}
    used.update(prior)
    next_number = max([int(value[5:]) for value in used if re.fullmatch(r'shot-\d+', value)] or [0])
    claims = validate_claims(rows, previous, photo=photo)
    reserved = set(claims) - {''}
    claimed = set()
    result = []
    for row, identity in zip(rows, claims):
        if not identity:
            matches = [key for key, old in prior.items()
                       if key not in claimed | reserved and fingerprint(_content(old)) == fingerprint(_content(row))]
            if len(matches) == 1:
                identity = matches[0]
            else:
                next_number += 1
                identity = f'shot-{next_number:02d}'
                while identity in used:
                    next_number += 1
                    identity = f'shot-{next_number:02d}'
        claimed.add(identity)
        used.add(identity)
        item = {**row, 'shot_id': identity}
        if photo:
            item['page_id'] = next((key for key, value in pages.items() if value == identity),
                                   identity.replace('shot-', 'page-', 1))
        result.append(item)
    return result


def dependency_values(*, segment, design, product, source_materials, prompt, references,
                      kind, model, duration, resolution):
    """Shot placement can move; its length must match. Page numbers are layout."""
    from .structured import _seconds

    seconds = _seconds(segment.get('tc', ''), segment.get('duration', ''))
    ids = {item.strip() for item in str(segment.get('character_ids', '')).split(',') if item.strip()}
    scene = str(segment.get('scene_id', '')).strip()
    return {
        'product': product,
        'source_materials': source_materials,
        'visual_rules': {key: value for key, value in design.items() if key not in {'characters', 'scenes'}},
        'characters': sorted((row for row in design.get('characters', []) if row.get('id') in ids),
                             key=lambda row: row['id']),
        'scene': [row for row in design.get('scenes', []) if row.get('id') == scene],
        'action': {key: value for key, value in segment.items()
                   if key not in {'shot_id', 'page_id', 'ordinal', 'tc', 'line', 'sound'}},
        'words': segment.get('line', ''),
        'sound': segment.get('sound', ''),
        # Photo tc is a page label. Unreadable video timing stays exact rather
        # than treating two unknown lengths as proof they are interchangeable.
        'timing': (None if segment.get('page_id') else seconds if seconds is not None
                   else {'unparsed_tc': segment.get('tc', '')}),
        'instructions': prompt,
        'references': [{key: value for key, value in row.items() if key != 'name'} for row in references],
        'parameters': {'kind': kind, 'model': model, 'duration': duration, 'resolution': resolution},
    }


def dependency_hashes(**kwargs):
    return {key: fingerprint(value) for key, value in dependency_values(**kwargs).items()}
