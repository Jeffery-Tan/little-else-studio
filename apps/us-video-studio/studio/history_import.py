"""Explicit offline import of a reviewed local migration plan; no providers.

The plan declares allowed roots and selected documents/materials. Whole roots are
archived byte-for-byte; only selected text documents have a read-only HTTP view.
Old scripts, approvals and ledgers are inert archive bytes, never active state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

from .jobs import WriterLease
from .materials import MAX_UPLOAD_BYTES, accept_upload, kind_of, sanitize
from .models import valid_run_id
from .pipeline import Pipeline
from .project_history import MAX_DOCUMENT_BYTES, sha_file
from .store import Store


def prepare(plan: dict) -> dict:
    if plan.get('market') != 'US' or not valid_run_id(plan.get('run_id', '')):
        raise ValueError('明确 US 线路和合法项目标识后才能迁移。')
    for key in ('title', 'brief', 'intake', 'summary', 'historical_cost_note'):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ValueError(f'迁移计划缺少 {key}')
    if not plan.get('sources'):
        raise ValueError('缺少明确的来源目录。')
    files, ids, roots = [], set(), []
    for source in plan['sources']:
        alias, root = source['id'], Path(source['root'])
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', alias) or alias in ids:
            raise ValueError('来源标识重复或不合法。')
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            raise ValueError('来源必须是现存的本地目录，不可使用链接。')
        root = root.resolve()
        if any(root.is_relative_to(r) or r.is_relative_to(root) for r in roots):
            raise ValueError('来源目录不能重叠。')
        roots.append(root)
        ids.add(alias)
        selected = source.get('files')
        if selected is not None and (not isinstance(selected, list) or not selected):
            raise ValueError('精确来源文件白名单不能为空。')
        paths = [root / name for name in selected] if selected is not None else sorted(root.rglob('*'))
        for path in paths:
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
                raise ValueError('来源含链接或越界路径；请先核对。')
            if not path.is_file():
                if selected is not None:
                    raise ValueError('白名单来源文件缺失。')
                continue
            relative = path.relative_to(root).as_posix()
            identity = f'{alias}/{relative}'
            if any(f['archive_path'] == identity for f in files):
                raise ValueError('白名单来源文件重复。')
            files.append({'id': hashlib.sha256(identity.encode()).hexdigest()[:24],
                          'source': alias, 'relative': relative, 'path': str(path),
                          'archive_path': identity, 'bytes': path.stat().st_size, 'sha256': sha_file(path)})
    lookup = {(f['source'], f['relative']): f for f in files}
    documents, materials, names = [], [], set()
    for doc in plan.get('documents', []):
        record = lookup.get((doc['source'], doc['path']))
        if record is None:
            raise ValueError('选定的历史文件不在来源目录中。')
        path = Path(record['path'])
        if path.suffix.lower() not in {'.md', '.txt', '.json'} or record['bytes'] > MAX_DOCUMENT_BYTES:
            raise ValueError('只允许有大小上限的文本历史资料。')
        path.read_text(encoding='utf-8-sig')
        if any(d['id'] == record['id'] for d in documents):
            raise ValueError('历史资料重复。')
        documents.append({k: record[k] for k in ('id', 'bytes', 'sha256')} | {'title': doc['title'], 'status': doc['status']})
    for media in plan.get('materials', []):
        record = lookup.get((media['source'], media['path']))
        if record is None:
            raise ValueError('选定素材不在来源目录中。')
        name = Path(record['path']).name
        if sanitize(name) is None or name.casefold() in names or kind_of(name) == 'other' or record['bytes'] > MAX_UPLOAD_BYTES:
            raise ValueError('素材名称、格式、大小或同名冲突需要处理。')
        if media['origin'] not in {'generated', 'product_source'}:
            raise ValueError('素材需注明生成来源或原商品来源。')
        if record['sha256'] != media['sha256']:
            raise ValueError('选定素材与已核对版本的 hash 不一致。')
        names.add(name.casefold())
        materials.append({'name': name, 'label': media['label'], 'origin': media['origin'],
                          'sha256': record['sha256'], 'archive_id': record['id']})
    return {'schema_version': 1, 'run_id': plan['run_id'], 'summary': plan['summary'],
            'historical_cost_note': plan['historical_cost_note'], 'files': files,
            'documents': documents, 'materials': materials,
            'archive': {'files': len(files), 'bytes': sum(f['bytes'] for f in files)},
            'sources': [{'id': s['id'], 'root': str(Path(s['root']).resolve()), 'files': s.get('files')}
                        for s in plan['sources']]}


def import_plan(data_root: Path, plan: dict) -> dict:
    prepared = prepare(plan)
    data_root = data_root.resolve()
    if any(data_root.is_relative_to(Path(s['root'])) or Path(s['root']).is_relative_to(data_root) for s in prepared['sources']):
        raise ValueError('来源和目标运行库必须分开。')
    with WriterLease(data_root):
        target = Store(data_root).run_dir(plan['run_id'])
        if target.exists():
            raise ValueError('目标项目已存在；拒绝覆盖或重复导入。')
        # A failure retains this isolated staging directory for investigation.
        staging = data_root / 'import-staging' / uuid.uuid4().hex
        store = Store(staging)
        store.create_run(plan['run_id'], plan['title'], plan['brief'])
        run = store.run_dir(plan['run_id'])
        for record in prepared['files']:
            destination = run / 'history' / 'archive' / record['archive_path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record['path'], destination)
            if sha_file(destination) != record['sha256'] or sha_file(Path(record['path'])) != record['sha256']:
                raise ValueError('迁移期间来源变化；未发布项目，请检查隔离暂存。')
        files = {r['id']: r for r in prepared['files']}
        for media in prepared['materials']:
            record = files[media['archive_id']]
            path = run / 'history' / 'archive' / record['archive_path']
            accept_upload(store, plan['run_id'], media['name'], path.read_bytes())
            store.append_event(plan['run_id'], 'material_imported', name=media['name'],
                               sha256=media['sha256'], origin=media['origin'], label=media['label'])
        manifest = run / 'history' / 'manifest.json'
        manifest.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        store.append_event(plan['run_id'], 'history_imported', manifest_sha256=sha_file(manifest),
                           actor='explicit-local-migration', approvals_imported=False, ledger_imported=False)
        pipeline = Pipeline(store)
        attempt = pipeline.start_attempt(plan['run_id'], 'intake', actor='explicit-local-migration')
        pipeline.submit(plan['run_id'], 'intake', attempt.token, content=plan['intake'],
                        summary='已迁入商品资料、历史版本与反馈，等待你在制片对话中继续。',
                        evidence=['history:manifest#' + sha_file(manifest)],
                        limitations=['仅迁移历史资料；未重新观察媒体，未通过本轮创意、图审或费用授权。'],
                        actor='explicit-local-migration')
        store.append_event(plan['run_id'], 'creative_context_selected',
                           selection={'intent': 'pause', 'format': 'video', 'tags': [], 'case_ids': None},
                           actor='explicit-local-migration')
        run.rename(target)
        return {'run_id': plan['run_id'], 'archive': prepared['archive'],
                'documents': len(prepared['documents']), 'materials': len(prepared['materials']),
                'paused': True, 'model_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    result = import_plan(args.data_root, plan) if args.apply else prepare(plan)
    print(json.dumps(result if args.apply else {'archive': result['archive'],
        'documents': len(result['documents']), 'materials': len(result['materials']), 'applied': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
