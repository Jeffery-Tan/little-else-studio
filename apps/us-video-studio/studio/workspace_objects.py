"""Exact creative objects and canvas relationships, projected from original facts."""
from __future__ import annotations

import difflib
import hashlib
import json

from .models import PipelineError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def object_id(target):
    return target['kind'] + ':' + digest(target)[:24]


def read_object(store, pipeline, run_id, target, *, production=None):
    if not isinstance(target, dict) or target.get('run_id') != run_id:
        raise PipelineError('object_target', '对象不属于当前项目，请重新选择。')
    kind = target.get('kind')
    if kind in {'artifact', 'writing', 'design', 'shot'}:
        stage, version = target.get('stage'), target.get('version')
        artifact = store.read_artifact(run_id, stage, version)
        if artifact.sha256 != target.get('artifact_sha256') or not store.verify_artifact(run_id, stage, version):
            raise PipelineError('object_changed', '作品字节或版本已变化，请重新打开准确版本。')
        state = pipeline.stage_state(run_id, stage)
        current = bool(state.latest and state.latest.version == version and not state.stale and state.integrity_ok)
        from .vocabulary import stage_label
        if kind in {'design', 'shot'}:
            if kind == 'design':
                plural = {'character': 'characters', 'scene': 'scenes'}.get(target.get('design_kind'))
                values = ((artifact.structured or {}).get('visual_design') or {}).get(plural, []) if plural else []
                value = next((row for row in values if row.get('id') == target.get('design_id')), None)
                title = (value or {}).get('label') or target.get('design_id')
            else:
                from .production import _artifact_target_shots
                shot = next((row for row in _artifact_target_shots(artifact, 'image')['shots']
                             if row['shot_id'] == target.get('shot_id')), None)
                value = (artifact.structured or {})['segments'][shot['ordinal'] - 1] if shot else None
                from .project_brief import content_format
                unit = '页' if content_format(store.read_events(run_id)) == 'photo' else '镜'
                title = f"第 {shot['ordinal']} {unit} · {shot['label']}" if shot else ''
            if value is None:
                raise PipelineError('object_missing', '准确作品中没有这个设计或镜头。')
            return {'target': target, 'title': title, 'text': json.dumps(value, ensure_ascii=False, indent=2),
                    'data': {'entity': value}, 'current': current, 'status': 'current' if current else 'history',
                    'allowed_actions': ['read', 'compare', 'annotate']}
        return {'target': target, 'title': f'{stage_label(stage)} · 第 {version} 版',
                'text': store.read_artifact_text(run_id, stage, version),
                'data': {'artifact': artifact.to_dict()}, 'current': current,
                'status': 'current' if current else 'history',
                'allowed_actions': ['read', 'compare', 'annotate'] + (['revise'] if current and stage == 'writing' else [])}
    from .production import _read_state_without_service
    production = production if production is not None else _read_state_without_service(store, pipeline, run_id)
    if kind == 'material':
        material = next((m for m in production['materials']
                         if m['name'] == target.get('name') and m['sha256'] == target.get('sha256')), None)
        if not material:
            raise PipelineError('object_changed', '素材已变化或缺失，请重新选择。')
        return {'target': target, 'title': material['name'], 'text': '', 'data': {'material': material},
                'current': True, 'status': 'available', 'allowed_actions': ['read', 'annotate']}
    if kind not in {'production_draft', 'media'}:
        raise PipelineError('object_target', '此对象类型不支持该操作。')
    draft, attempt = None, None
    for item in production['drafts']:
        if kind == 'production_draft' and item['draft_id'] == target.get('draft_id'):
            if item['draft_sha256'] != target.get('draft_sha256'):
                raise PipelineError('object_changed', '制作输入指纹已变化。')
            draft = item
        if kind == 'media':
            found = next((a for a in item['attempts'] if a['attempt_id'] == target.get('attempt_id')), None)
            if found:
                draft, attempt = item, found
    if not draft:
        raise PipelineError('object_missing', '未找到这份准确制作输入。')
    current = bool(draft.get('is_current') and draft.get('source_ready'))
    actions = ['read', 'compare', 'annotate']
    data = {'draft': draft}
    if kind == 'media':
        output = (attempt or {}).get('output') or {}
        if output.get('sha256') != target.get('output_sha256') or not attempt.get('output_available'):
            raise PipelineError('object_changed', '媒体文件已变化或缺失。')
        data.update(attempt=attempt, material=next(m for m in production['materials']
                    if m['name'] == output['name'] and m['sha256'] == output['sha256']))
        from .result_inspection import latest_report
        data['inspection'] = latest_report(store, run_id, target)
        actions.extend(['inspect', 'export'])
        current = current and attempt['status'] in {'succeeded', 'completed'}
        if current:
            actions.append('revise')
    elif current and not draft.get('reuse'):
        actions.extend(['edit_references', 'revise'])
    shot = next((s for s in production['targets'][draft['kind']]['shots']
                 if s['shot_id'] == draft.get('shot_id')), None)
    ordinal = shot.get('ordinal') if shot else None
    from .project_brief import content_format
    unit = '页' if content_format(store.read_events(run_id)) == 'photo' else '镜'
    label = f'第 {ordinal} {unit}' if ordinal else '历史制作'
    return {'target': target, 'title': f'{label} · {"结果" if attempt else "输入"}',
            'text': draft.get('shot_prompt', draft.get('prompt', '')), 'data': data,
            'current': current, 'status': attempt['status'] if attempt else 'current' if current else 'history',
            'allowed_actions': actions}


def layout(store, run_id):
    event = next((e for e in reversed(store.read_events(run_id)) if e['kind'] == 'canvas_layout_saved'), None)
    return event['layout'] if event else {'revision': 0, 'view': {'zoom': 1, 'x': 32, 'y': 32},
                                         'positions': {}, 'groups': [], 'notes': []}


def graph(store, pipeline, run_id):
    from .production import _read_state_without_service
    with store.run_lock(run_id):
        production = _read_state_without_service(store, pipeline, run_id)
        nodes, edges, seen = [], [], set()
        def add(target):
            identity = object_id(target)
            if identity not in seen:
                value = read_object(store, pipeline, run_id, target, production=production)
                nodes.append({'id': identity, 'kind': target['kind'], **value})
                seen.add(identity)
            return identity
        def edge(left, right, kind, *, purpose=None, editable=False):
            value = {'from': left, 'to': right, 'kind': kind, 'editable': editable}
            if purpose:
                value['purpose'] = purpose
            edges.append({'id': digest(value)[:24], **value})
        artifacts = {}
        saved_artifacts = store.list_artifacts(run_id)
        for a in saved_artifacts:
            target = {'kind': 'artifact', 'run_id': run_id, 'stage': a.stage,
                      'version': a.version, 'artifact_sha256': a.sha256}
            artifacts[(a.stage, a.version, a.sha256)] = add(target)
        entities, shots = {}, {}
        def add_designs(material):
            if material is None:
                return
            for singular, plural in (('character', 'characters'), ('scene', 'scenes')):
                for entity in ((material.structured or {}).get('visual_design') or {}).get(plural, []):
                    target = {'kind': 'design', 'run_id': run_id, 'stage': material.stage,
                              'version': material.version, 'artifact_sha256': material.sha256,
                              'design_kind': singular, 'design_id': entity['id']}
                    key = (material.sha256, singular, entity['id'])
                    if key in entities:
                        continue
                    node = entities[key] = add(target)
                    edge(artifacts[(material.stage, material.version, material.sha256)], node, 'design')
        add_designs(store.latest_artifact(run_id, 'material_plan'))
        writing = store.latest_artifact(run_id, 'writing')
        if writing:
            bound_design_hash = writing.inputs.get('material_plan')
            bound_material = next((a for a in saved_artifacts
                if a.stage == 'material_plan' and a.sha256 == bound_design_hash), None)
            add_designs(bound_material)
            for shot in production['targets']['image']['shots']:
                target = {'kind': 'shot', 'run_id': run_id, 'stage': 'writing', 'version': writing.version,
                          'artifact_sha256': writing.sha256, 'shot_id': shot['shot_id']}
                node = shots[shot['shot_id']] = add(target)
                edge(artifacts[('writing', writing.version, writing.sha256)], node, 'contains')
                segment = writing.structured['segments'][shot['ordinal'] - 1]
                character_ids = segment.get('character_ids') or []
                if isinstance(character_ids, str):
                    character_ids = [identity.strip() for identity in character_ids.split(',') if identity.strip()]
                for key in [(bound_design_hash, 'scene', segment.get('scene_id')),
                            *[(bound_design_hash, 'character', identity) for identity in character_ids]]:
                    if key in entities:
                        edge(entities[key], node, 'uses_design')
        materials = {}
        for m in production['materials']:
            materials[(m['name'], m['sha256'])] = add({'kind': 'material', 'run_id': run_id,
                                                      'name': m['name'], 'sha256': m['sha256']})
        drafts = {}
        for d in production['drafts']:
            drafts[d['draft_id']] = add({'kind': 'production_draft', 'run_id': run_id,
                'draft_id': d['draft_id'], 'draft_sha256': d['draft_sha256']})
        for d in production['drafts']:
            node = drafts[d['draft_id']]
            source = artifacts.get(('writing', d.get('source_version'), d.get('source_sha256')))
            if source:
                edge(source, node, 'production_input')
            if writing and d.get('source_sha256') == writing.sha256 and d.get('shot_id') in shots:
                edge(shots[d['shot_id']], node, 'shot_input')
            if d.get('supersedes') in drafts:
                edge(drafts[d['supersedes']], node, 'supersedes')
            for ref in d['references']:
                source = materials.get((ref['name'], ref['sha256']))
                if source:
                    edge(source, node, 'reference', purpose=ref['purpose'],
                         editable=bool(d.get('is_current') and d.get('source_ready') and not d.get('reuse')))
            for a in d['attempts']:
                if not a.get('output_available'):
                    continue
                target = {'kind': 'media', 'run_id': run_id, 'attempt_id': a['attempt_id'],
                          'output_sha256': a['output']['sha256']}
                result = add(target)
                edge(node, result, 'generated_from')
        snapshot = digest({'nodes': [{k: n[k] for k in ('id', 'target', 'status', 'allowed_actions')} for n in nodes],
                           'edges': edges, 'baseline': production['baseline']})
        return {'schema_version': 1, 'run_id': run_id, 'snapshot_sha256': snapshot,
                'nodes': nodes, 'edges': edges, 'layout': layout(store, run_id)}


def compare(store, pipeline, run_id, left, right):
    with store.run_lock(run_id):
        a, b = (read_object(store, pipeline, run_id, value) for value in (left, right))
        if not a['text'] or not b['text']:
            raise PipelineError('object_compare', '逐字比较需要两份文字作品或制作输入；媒体请并排查看。')
        return {'left': a, 'right': b, 'identical': a['text'] == b['text'],
                'diff': '\n'.join(difflib.unified_diff(a['text'].splitlines(), b['text'].splitlines(),
                                                    fromfile=a['title'], tofile=b['title'], lineterm=''))}


def annotations(store, run_id):
    items = {}
    for row in store.read_events(run_id):
        if row['kind'] == 'workspace_annotation_saved':
            items[row['annotation']['annotation_id']] = {**row['annotation'], 'at': row['at'], 'state': 'saved'}
        elif row['kind'] == 'workspace_annotations_submitted':
            for identity in row['annotation_ids']:
                if identity in items:
                    items[identity].update(state='submitted', job_id=row['job_id'])
    values = list(items.values())
    return {'run_id': run_id, 'snapshot_sha256': digest(values), 'items': values}


def annotation_context(store, run_id, message_id):
    groups = [e for e in store.read_events(run_id) if e['kind'] == 'workspace_annotation_group'
              and e['message_id'] == message_id]
    if not groups:
        return []
    ids = groups[-1]['annotation_ids']
    saved = {a['annotation_id']: a for a in annotations(store, run_id)['items']}
    if any(identity not in saved for identity in ids):
        raise PipelineError('annotation_missing', '本轮批注来源不完整，不能继续修改。')
    return [{key: saved[identity][key] for key in ('annotation_id', 'target', 'text', 'region', 'at_seconds')
             if key in saved[identity]} for identity in ids]
