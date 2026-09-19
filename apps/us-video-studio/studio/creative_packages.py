"""Compile reviewable packs with the existing US compilers; never call a model."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import materials
from .learning_assets import REPOSITORY, digest
from .models import STAGES


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


class CreativePackages:
    def __init__(self, store, repository_root=REPOSITORY):
        self.store = store
        self.repository_root = Path(repository_root)

    def _root(self, run_id):
        self.store.read_run(run_id)
        return self.store.run_dir(run_id) / 'creative-packages'

    def _material(self, run_id, name):
        path = materials.resolve(self.store, run_id, name)
        if path is None or not path.is_file():
            raise ValueError('编译只能引用本任务已上传的素材文件名。')
        return path

    def compile(self, run_id, *, format, stage, version, artifact_sha256, manifest):
        if format not in {'video', 'photo'} or not isinstance(manifest, dict):
            raise ValueError('需要 video/photo 格式和结构化 manifest。')
        if not isinstance(stage, str) or stage not in STAGES or type(version) is not int or version < 1:
            raise ValueError('需要本任务内的有效阶段与准确版本。')
        with self.store.run_lock(run_id):
            artifact = self.store.read_artifact(run_id, stage, version)
            if artifact.sha256 != artifact_sha256 or not self.store.verify_artifact(run_id, stage, version):
                raise ValueError('产物版本或指纹已变化，请重新读取该版本。')
            doc = copy.deepcopy(manifest)
            source_hashes = {}
            if format == 'video':
                if not isinstance(doc.get('product_facts'), list) or not all(isinstance(f, dict) for f in doc['product_facts']):
                    raise ValueError('product_facts 须为事实对象列表。')
                for fact in doc.get('product_facts', []):
                    ref = fact.get('source', '')
                    if not isinstance(ref, str):
                        raise ValueError('事实 source 须为文本引用。')
                    if ref.startswith('material:'):
                        source_hashes[ref] = digest(self._material(run_id, ref.removeprefix('material:')))
                    elif ref.startswith('artifact:'):
                        match = re.fullmatch(r'artifact:([a-z_]+)-v([1-9][0-9]*)#([a-f0-9]{64})', ref)
                        if not match:
                            raise ValueError('商品事实须引用实际产物版本与指纹。')
                        source = self.store.read_artifact(run_id, match[1], int(match[2]))
                        if source.sha256 != match[3] or not self.store.verify_artifact(run_id, match[1], int(match[2])):
                            raise ValueError('商品事实引用的产物已变化。')
                    else:
                        raise ValueError('商品事实 source 须为 material:文件名 或 artifact:阶段-v版本#指纹。')
            else:
                if (not isinstance(doc.get('product'), dict)
                        or not isinstance(doc['product'].get('sources'), list)
                        or not isinstance(doc.get('pages'), list)
                        or not all(isinstance(page, dict) for page in doc['pages'])):
                    raise ValueError('图文 product.sources 与 pages 须为对象列表。')
                refs = list(doc.get('product', {}).get('sources', []))
                refs += [page['image'] for page in doc.get('pages', []) if page.get('image')]
                for ref in refs:
                    if not isinstance(ref, dict):
                        raise ValueError('图片来源须为对象。')
                    path = self._material(run_id, ref.get('path', ''))
                    if ref.get('sha256') != digest(path):
                        raise ValueError('图文来源素材指纹不匹配。')
                    source_hashes['material:' + path.name] = ref['sha256']
                    ref['path'] = str(path)
            library = (self.repository_root / 'tracks/us/creative/learning/library.json').read_bytes()
            identity = {'format': format, 'stage': stage, 'version': version,
                        'artifact_sha256': artifact_sha256, 'manifest': doc,
                        'source_hashes': source_hashes,
                        'library_sha256': hashlib.sha256(library).hexdigest()}
            package_id = hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
            root = self._root(run_id)
            root.mkdir(parents=True, exist_ok=True)
            final = root / package_id
            if final.exists():
                return self._read(run_id, package_id)
            staging = Path(tempfile.mkdtemp(prefix='.compile-', dir=root))
            try:
                input_path = staging / 'input.json'
                input_path.write_text(_json(doc), encoding='utf-8')
                library_path = staging / 'library.json'
                library_path.write_bytes(library)
                output = staging / 'compiled'
                # Only fixed repository code is executed. Paths and manifest text
                # are argv/data; neither is interpreted by a shell.
                script = REPOSITORY / 'scripts' / ('creative_brief.py' if format == 'video' else 'photo_copy_pack.py')
                args = [sys.executable, str(script), 'compile']
                args += (['--brief', str(input_path), '--library', str(library_path), '--output-dir', str(output)]
                         if format == 'video' else ['--manifest', str(input_path), '--out', str(output)])
                result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                                        errors='replace', timeout=30, shell=False)
                if result.returncode:
                    # Core validators emit concise field errors on the last line.
                    reason = (result.stderr or result.stdout).strip().splitlines()
                    raise ValueError('编译未通过：' + (reason[-1][:1000] if reason else '输入结构不完整'))
                if any(digest(self._material(run_id, ref.removeprefix('material:'))) != expected
                       for ref, expected in source_hashes.items()):
                    raise ValueError('编译过程中来源素材发生变化，请重新读取素材。')
                files = [{'name': p.relative_to(output).as_posix(), 'sha256': digest(p)}
                         for p in sorted(output.rglob('*')) if p.is_file()]
                record = {key: identity[key] for key in ('format', 'stage', 'version', 'artifact_sha256', 'library_sha256', 'source_hashes')}
                record.update(package_id=package_id, files=files,
                              created_at=datetime.now(timezone.utc).isoformat(),
                              notice='结构编译不等于内容通过、图片获批或生成授权。')
                record_path = output / 'package-record.json'
                record_path.write_text(_json(record), encoding='utf-8')
                record_sha256 = digest(record_path)
                output.rename(final)
                self.store.append_event(run_id, 'creative_package_created', package_record_sha256=record_sha256,
                                        **{k: v for k, v in record.items() if k != 'files'})
            finally:
                # This directory was created by this call beneath this run only.
                shutil.rmtree(staging)
            return self._read(run_id, package_id)

    def _read(self, run_id, package_id):
        if not re.fullmatch(r'[a-f0-9]{24}', package_id):
            raise ValueError('未知编译包。')
        root = self._root(run_id).resolve()
        base = (root / package_id).resolve()
        if base.parent != root or not (base / 'package-record.json').is_file():
            raise ValueError('未知编译包。')
        record_path = base / 'package-record.json'
        recorded_hashes = {event.get('package_record_sha256') for event in self.store.read_events(run_id)
                           if event['kind'] == 'creative_package_created' and event.get('package_id') == package_id}
        if digest(record_path) not in recorded_hashes:
            raise ValueError('编译包绑定记录已变化或缺少创建凭据。')
        record = json.loads(record_path.read_text(encoding='utf-8'))
        for file in record['files']:
            path = (base / file['name']).resolve()
            if not path.is_relative_to(base) or not path.is_file() or digest(path) != file['sha256']:
                raise ValueError('编译包文件缺失或已变化。')
            file['url'] = f'/api/runs/{quote(run_id)}/creative-packages/{package_id}/files/{quote(file["name"])}'
        return record

    def packages(self, run_id):
        root = self._root(run_id)
        return [self._read(run_id, p.name) for p in sorted(root.iterdir())
                if re.fullmatch(r'[a-f0-9]{24}', p.name)] if root.exists() else []

    def file(self, run_id, package_id, filename):
        record = self._read(run_id, package_id)
        if not any(file['name'] == filename for file in record['files']):
            raise ValueError('未知编译包文件。')
        return self._root(run_id) / package_id / filename
