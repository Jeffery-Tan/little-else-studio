"""Immutable local migration archive. Reading it never resumes production."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException

from .store import Store

MAX_DOCUMENT_BYTES = 512 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def archive_file(run: Path, relative: str) -> Path:
    base = run / 'history' / 'archive'
    path = base / relative
    if not base.resolve().is_relative_to(run.resolve()) or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError('历史文件路径不在本项目内。')
    if not path.is_file():
        raise ValueError('历史文件缺失，请检查迁移归档。')
    return path


def manifest(store: Store, run_id: str) -> dict | None:
    run = store.run_dir(run_id)
    event = next((e for e in reversed(store.read_events(run_id)) if e.get('kind') == 'history_imported'), None)
    if event is None:
        return None
    path = run / 'history' / 'manifest.json'
    if not path.resolve().is_relative_to(run.resolve()) or not path.is_file():
        raise ValueError('迁移记录缺失。')
    if path.stat().st_size > MAX_MANIFEST_BYTES or sha_file(path) != event['manifest_sha256']:
        raise ValueError('迁移记录指纹不一致。')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('不支持的迁移记录版本。')
    return data


def snapshot(store: Store, run_id: str) -> dict:
    data = manifest(store, run_id)
    if data is None:
        return {'available': False, 'documents': [], 'materials': []}
    return {key: data[key] for key in ('summary', 'documents', 'materials', 'archive', 'historical_cost_note')} | {'available': True}


def read_document(store: Store, run_id: str, document_id: str) -> dict:
    data = manifest(store, run_id)
    doc = next((d for d in (data or {}).get('documents', []) if d['id'] == document_id), None)
    if doc is None:
        raise ValueError('没有登记这份历史资料。')
    record = next(r for r in data['files'] if r['id'] == document_id)
    path = archive_file(store.run_dir(run_id), record['archive_path'])
    if path.suffix.lower() not in {'.md', '.txt', '.json'} or path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError('这份历史资料不支持文本阅读。')
    if sha_file(path) != doc['sha256']:
        raise ValueError('历史资料字节已变化，不能沿用原来的版本依据。')
    content = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.json':
        content = '```json\n' + content + '\n```'
    return dict(doc, content=content)


def evidence_binding(store: Store, run_id: str) -> dict | None:
    """Bind curated source documents without injecting old producer conversations."""
    data = manifest(store, run_id)
    if data is None:
        return None
    for doc in data['documents']:
        read_document(store, run_id, doc['id'])
    return {'manifest_sha256': sha_file(store.run_dir(run_id) / 'history' / 'manifest.json')}


def install_history_routes(app, store: Store, require_run):
    @app.get('/api/runs/{run_id}/history')
    def history_index(run_id: str):
        require_run(run_id)
        try:
            return snapshot(store, run_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/history/{document_id}')
    def history_document(run_id: str, document_id: str):
        require_run(run_id)
        try:
            return read_document(store, run_id, document_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
