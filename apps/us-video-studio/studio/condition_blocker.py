"""Project unresolved conditions from this executing input, without new authority."""
from __future__ import annotations

import copy
import hashlib

from .conversation_input import constraints_for_scope


def content_scope(goal, *, target=None, revision_scope=None):
    """The same content projection for saving a question and pausing its work."""
    target = target or {}
    stage = {'intake': 'intake', 'direction': 'direction', 'writing': 'writing'}.get(goal)
    paths = None
    if goal == 'revision':
        if revision_scope:
            stage = revision_scope['stage']
            if revision_scope.get('mode') == 'local':
                paths = [field['path'] for field in revision_scope['fields']]
        else:
            stage = target.get('stage')
            paths = [target['path']] if target.get('path') else None
    return stage, paths


def for_current_input(receipt, limits, rows, *, stage=None, paths=None):
    """Read the already saved source snapshot; never interpret or edit its text.

    The orchestrator supplies its trusted receipt, bound task and pinned condition
    snapshot. Scope selects applicable content, not permission to edit a stage.
    Historical conditions can explain a current conflict but cannot by themselves
    stop a different input, a discussion, a query or an internal continuation.
    """
    if (not receipt or limits.get('message_id') != receipt.get('message_id')
            or not limits.get('interpretation_id') or limits.get('readonly')
            or limits.get('task_mode') not in {'produce', 'revise'}
            or limits.get('deliverable') in {None, 'follow_up', 'analysis'}):
        return None

    source_id = receipt['message_id']
    source_hash = hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()
    applicable = constraints_for_scope(rows, stage=stage, paths=paths)
    unresolved = [row for row in applicable
                  if row['status'] == 'needs_clarification'
                  and row.get('intent') == 'instruction'
                  and row.get('origin') == 'grounded_model_interpretation'
                  and row.get('source_message_id') == source_id
                  and row.get('source_sha256') == source_hash]
    if not unresolved:
        return None

    by_id = {row['id']: row for row in rows}
    details, explanations = [], []
    for row in unresolved:
        conflicts = [{'id': identity,
                      **{key: by_id[identity][key] for key in ('quote', 'value') if identity in by_id}}
                     for identity in row['conflict_with']]
        details.append({key: copy.deepcopy(row[key]) for key in (
            'id', 'key', 'quote', 'value', 'scope', 'ambiguity', 'conflict_with')})
        details[-1]['conflicts'] = conflicts
        reasons = []
        if row['ambiguity']:
            reasons.append(row['ambiguity'])
        if conflicts:
            values = '、'.join(f'“{item["value"]}”' for item in conflicts if 'value' in item)
            reasons.append('与既有要求' + (values if values else '') + '存在冲突')
        if not reasons:
            # An explicit operation=clarify can have no further saved detail.
            reasons.append('该条件已记录为待澄清，尚无具体澄清内容')
        explanations.append(f'“{row["quote"]}”：' + '；'.join(reasons))

    note = '本轮工作已暂停，内容条件仍待澄清：' + '。'.join(explanations) + '。'
    return {'kind': 'content_condition', 'status': 'incomplete',
            'condition_status': 'needs_clarification',
            'source_message_id': source_id, 'interpretation_id': limits['interpretation_id'],
            'conditions': details, 'note': note,
            'next_action': {'action': 'clarify', 'reason': note}}
