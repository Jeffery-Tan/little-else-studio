"""Read-only, integrity-checked planning handoff. No model or persistent writes."""
import io
import json
import zipfile

from .materials import list_materials
from .models import PipelineError
from .vocabulary import stage_label


def delivery_snapshot(store, pipeline, packages, run_id):
    with store.run_lock(run_id):
        state = pipeline.state(run_id).to_dict()
        documents = []
        for stage in state['stages']:
            artifact = stage['latest']
            if not artifact:
                continue
            current = (stage['integrity_ok'] and not stage['stale']
                       and artifact.get('digest_algorithm') == 'artifact-v2')
            approved = bool(current and stage['status'] == 'approved')
            documents.append({'stage': stage['stage'], 'label': stage_label(stage['stage']),
                              'version': artifact['version'], 'sha256': artifact['sha256'],
                              'integrity_ok': stage['integrity_ok'], 'current': current,
                              'adopted': approved, 'status': '已采用' if approved else
                              '已整理' if current and stage['status'] == 'done' else
                              '待审阅' if current else '历史或需更新',
                              'summary': artifact.get('summary', ''),
                              'limitations': artifact.get('limitations', [])})
        return {'run': state['run'], 'ready': bool(documents) and all(d['current'] and d['status'] in {'已整理', '已采用'} for d in documents) and len(documents) == len(state['stages']),
                'documents': documents, 'materials': list_materials(store, run_id),
                'packages': packages.packages(run_id),
                'missing': [stage_label(s['stage']) for s in state['stages']
                            if not s['latest'] or s['stale'] or not s['integrity_ok']
                            or s['latest'].get('digest_algorithm') != 'artifact-v2'
                            or s['status'] not in {'done', 'approved'}],
                'notice': '这是前期策划资料，不包含新生成的图片或视频。历史及待审阅版本会明确标注，导出不会改变采用状态。'}


def delivery_zip(store, pipeline, packages, run_id):
    with store.run_lock(run_id):
        snapshot = delivery_snapshot(store, pipeline, packages, run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('delivery-manifest.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
            notes = [f'# {snapshot["run"]["title"]}', '', snapshot['notice'], '',
                     '## 项目要求', snapshot['run']['brief'], '', '## 版本清单']
            for doc in snapshot['documents']:
                stage, version = doc['stage'], doc['version']
                if not doc['integrity_ok'] or not store.verify_artifact(run_id, stage, version):
                    raise PipelineError('delivery_integrity', '有文件与保存记录不一致，暂不能导出。')
                text = store.read_artifact_text(run_id, stage, version)
                # Recheck after reading, so a changed file is never knowingly packaged.
                if not store.verify_artifact(run_id, stage, version):
                    raise PipelineError('delivery_integrity', '导出时文件发生变化，请重新检查。')
                filename = f'{stage}-v{version}.md'
                archive.writestr(filename, '\ufeff' + text)
                notes.append(f'- {doc["label"]} v{version} · {doc["status"]} · {filename}')
            notes += ['', '## 仍需完成', *(f'- {label}' for label in snapshot['missing']),
                      '', '## 素材', '素材仅列入清单，不复制媒体文件；文件存在不代表已分析。']
            archive.writestr('README.md', '\ufeff' + '\n'.join(notes))
        return buffer.getvalue()


def photo_delivery_projection(production, writing, *, writing_ready, format):
    """Archive availability over the existing target/coverage projection."""
    import hashlib

    source = {'source_version': writing.version if writing else None,
              'source_sha256': writing.sha256 if writing else None}
    def unavailable(reason):
        return {**source, 'available': False, 'reason': reason, 'snapshot_sha256': '', 'pages': []}
    if format != 'photo':
        return unavailable('只有当前图文项目提供逐页图片 ZIP。')
    if not writing or not writing_ready or writing.digest_algorithm != 'artifact-v2':
        return unavailable('当前图文方案还需核对或采用，暂不能下载整组图片。')
    caption = writing.structured.get('caption') if isinstance(writing.structured, dict) else None
    if not isinstance(caption, str):
        return unavailable('本版发布文案尚未保存，暂不能下载整组图文。')
    target = production.get('targets', {}).get('image', {})
    covered = production.get('shots', {}).get('image', {})
    shots, rows = target.get('shots', []), covered.get('shots', [])
    if (target.get('complete_list') is not True or covered.get('complete_list') is not True
            or target.get('source_version') != writing.version or target.get('source_sha256') != writing.sha256
            or not shots or len(shots) != len(rows)
            or any(not isinstance(s.get('ordinal'), int) or isinstance(s.get('ordinal'), bool)
                   or s['ordinal'] < 1 or not s.get('shot_id') for s in shots)
            or len({s['ordinal'] for s in shots}) != len(shots)
            or len({s['shot_id'] for s in shots}) != len(shots)):
        return unavailable('当前逐页结果清单尚未核对，暂不能下载整组图片。')
    missing = [s['ordinal'] for s in shots if len(matched := [r for r in rows if r.get('shot_id') == s['shot_id']]) != 1
               or matched[0].get('stage') != 'completed' or matched[0].get('source_ready') is not True]
    if missing or covered.get('delivered') is not True:
        detail = '、'.join(str(n) for n in sorted(missing))
        return unavailable(f'整组下载还缺第 {detail} 页当前可核对图片，可以先下载已有单页。' if detail
                           else '当前整组结果还需核实，可以先查看已有单页。')
    segments = writing.structured.get('segments')
    if (not isinstance(segments, list) or len(segments) != len(shots)
            or {s['ordinal'] for s in shots} != set(range(1, len(shots) + 1))
            or any(not isinstance(segment, dict) or not isinstance(segment.get('line'), str) for segment in segments)):
        return unavailable('本版逐页文案与结果清单还需核对，暂不能下载整组图文。')
    pages = []
    for shot in sorted(shots, key=lambda s: s['ordinal']):
        row = next(r for r in rows if r['shot_id'] == shot['shot_id'])
        drafts = [d for d in production.get('drafts', []) if d.get('draft_id') == row.get('draft_id')
                  and d.get('kind') == 'image' and d.get('shot_id') == shot['shot_id']
                  and d.get('is_current') is True and d.get('source_ready') is True
                  and d.get('source_version') == writing.version and d.get('source_sha256') == writing.sha256]
        draft = drafts[0] if len(drafts) == 1 else {}
        latest = (draft.get('attempts') or [{}])[-1]
        output = row.get('output') or {}
        materials = [m for m in production.get('materials', []) if m.get('kind') == 'image'
                     and m.get('name') == output.get('name') and m.get('sha256') == output.get('sha256')]
        if (not draft or latest.get('status') != 'succeeded' or latest.get('output_available') is not True
                or latest.get('attempt_id') != row.get('attempt_id') or latest.get('output') != output
                or draft.get('draft_sha256') != row.get('draft_sha256') or len(materials) != 1):
            return unavailable(f'第 {shot["ordinal"]} 页当前图片身份还需核对，暂不能下载整组。')
        pages.append({'ordinal': shot['ordinal'], 'shot_id': shot['shot_id'], 'draft_id': draft['draft_id'],
                      'draft_sha256': draft['draft_sha256'], 'attempt_id': latest['attempt_id'],
                      'name': output['name'], 'sha256': output['sha256'], 'approved': materials[0].get('approved') is True})
    manifest = {'schema_version': 1, 'run_id': production['run_id'], 'kind': 'photo', **source,
                'caption_sha256': hashlib.sha256(caption.encode('utf-8')).hexdigest(), 'pages': pages}
    token = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return {**manifest, 'available': True, 'reason': '', 'snapshot_sha256': token}


def photo_delivery_zip(store, pipeline, run_id, expected_snapshot_sha256, *, ledger=None, execution_active=False):
    """Verify the viewed snapshot and original bytes before exposing any ZIP."""
    import tempfile
    from .production import _read_state_without_service, read_image

    archive_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        with store.run_lock(run_id):
            state = _read_state_without_service(store, pipeline, run_id, ledger=ledger, execution_active=execution_active)
            delivery = state['photo_delivery']
            if not delivery['available']:
                raise ValueError(delivery['reason'])
            if delivery['snapshot_sha256'] != expected_snapshot_sha256:
                raise ValueError('当前图文或图片已变化，请关闭后重新打开整组图文，再核对下载。')
            source = store.latest_artifact(run_id, 'writing')
            if not store.verify_artifact(run_id, 'writing', source.version):
                raise ValueError('当前图文方案原文件无法校验，暂不能打包。')
            caption = source.structured['caption']
            copy = source.structured.get('segments') or []
            manifest = {k: v for k, v in delivery.items() if k not in {'available', 'reason'}}
            manifest['notice'] = '这是当前原图与文案快照；下载不代表图审、内容验收、执行授权或发布。'
            manifest['pages'] = [dict(page) for page in manifest['pages']]
            with zipfile.ZipFile(archive_file, 'w', zipfile.ZIP_DEFLATED) as archive:
                for page in manifest['pages']:
                    try:
                        raw, mime = read_image(store, run_id, page['name'], page['sha256'])
                    except (OSError, ValueError) as exc:
                        raise ValueError(f'第 {page["ordinal"]} 页当前图片未通过字节或静态图校验，本次 ZIP 未交付。请核对原图。') from exc
                    extension = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[mime]
                    filename = f'images/page-{page["ordinal"]:02d}{extension}'
                    # Images are already compressed; avoid re-encoding or wasting CPU.
                    archive.writestr(filename, raw, compress_type=zipfile.ZIP_STORED)
                    page['archive_file'] = filename
                archive.writestr('caption.txt', caption.encode('utf-8'))
                copies = [f'Page {page["ordinal"]:02d}\n{copy[page["ordinal"] - 1].get("line", "")}' for page in manifest['pages']]
                archive.writestr('page-copy.txt', '\n\n'.join(copies).encode('utf-8'))
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
                confirmed = sum(page['approved'] for page in manifest['pages'])
                notice = (f'当前图文方案第 {source.version} 版；{len(manifest["pages"])} 页原图，{confirmed} 页已确认。\n'
                          + manifest['notice'] + '\nimages/ 按本版页序排列；caption.txt 是本版发布文案原文；page-copy.txt 供逐页对照。\n')
                archive.writestr('README.txt', notice.encode('utf-8'))
            if not store.verify_artifact(run_id, 'writing', source.version):
                raise ValueError('打包时图文方案原文件发生变化，本次 ZIP 未交付。请重新核对。')
        archive_file.seek(0)
        return archive_file, source.version
    except BaseException:
        archive_file.close()
        raise
