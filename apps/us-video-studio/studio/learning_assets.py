"""Allowlisted historical learning artifacts; never an arbitrary file server."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import quote, unquote

from .materials import resolve
from .models import PipelineError
from .paths import KNOWLEDGE_ROOT


REPOSITORY = KNOWLEDGE_ROOT
US_MEDIA = Path('J:/E-Business-Video-Data/US')
ARCHIVES = {
    'brief-review': ('视频输入编译演练 · 审阅（非正式候选）', 'creative-learning-20260918/brief-exercise-v2/review.md'),
    'brief-prompt': ('视频输入编译演练 · 最终文字', 'creative-learning-20260918/brief-exercise-v2/prompt.txt'),
    'brief-manifest': ('视频输入编译演练 · 绑定记录', 'creative-learning-20260918/brief-exercise-v2/manifest.json'),
    'photo-review': ('图片文案演练 · 待生成审阅包', 'photo-copy-methods-exercise-20260918/review-v1/index.html'),
    'photo-pack': ('图片文案演练 · 逐页计划', 'photo-copy-methods-exercise-20260918/review-v1/pack.json'),
    'photo-caption': ('图片文案演练 · 发布文案', 'photo-copy-methods-exercise-20260918/review-v1/caption.txt'),
    'photo-prompts': ('图片文案演练 · 逐页制作提示', 'photo-copy-methods-exercise-20260918/review-v1/prompts.md'),
    'comparison-report': ('Oumomo 旧片 · 受限比较报告', 'creative-comparison-20260918/baseline-v2/comparison.md'),
    'comparison-record': ('Oumomo 旧片 · 材料与缺口', 'creative-comparison-20260918/baseline-v2/comparison.json'),
}
MEDIA_SUFFIXES = {'.mp4', '.webm', '.mov', '.m4v', '.jpg', '.jpeg', '.png', '.webp', '.gif'}
RESOURCE_ID = re.compile(r'^run-material:([a-z0-9][a-z0-9-]*):([^:/]+)$')


def run_material_resource_id(run_id, name):
    return f'run-material:{run_id}:{quote(name, safe="")}'


def verified_run_material(store, resource_id, expected_sha256):
    match = RESOURCE_ID.fullmatch(resource_id) if isinstance(resource_id, str) else None
    if match is None or not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
        raise ValueError('学习媒体资源 ID 或指纹无效。')
    run_id, encoded_name = match.groups()
    name = unquote(encoded_name)
    try:
        run = store.read_run(run_id)
    except PipelineError as exc:
        raise ValueError('学习媒体来源记录不存在。') from exc
    if quote(name, safe='') != encoded_name or run.kind != 'learning':
        raise ValueError('学习媒体资源身份无法核对。')
    try:
        events = store.read_events(run_id)
    except (PipelineError, OSError) as exc:
        raise ValueError('学习媒体来源记录不可读取。') from exc
    if not any(event.get('kind') == 'material_uploaded' and event.get('name') == name and
               event.get('sha256') == expected_sha256 for event in events):
        raise ValueError('学习媒体没有准确的来源记录。')
    try:
        path = resolve(store, run_id, name)
        valid = path is not None and path.suffix.lower() in MEDIA_SUFFIXES and digest(path) == expected_sha256
    except OSError as exc:
        raise ValueError('学习媒体字节不可读取。') from exc
    if not valid:
        raise ValueError('学习媒体字节已变化或不可访问。')
    return path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class LearningAssets:
    def __init__(self, repository_root=REPOSITORY, media_root=US_MEDIA, store=None):
        self.repository_root = Path(repository_root).resolve()
        self.media_root = Path(media_root).resolve()
        self.store = store

    def archives(self):
        return [{'id': key, 'title': title, 'kind': Path(relative).suffix.lstrip('.'),
                 'available': (self.media_root / relative).is_file(),
                 'url': ('/api/learning/photo-archive/index.html' if key == 'photo-review' else f'/api/learning/archive/{key}')}
                for key, (title, relative) in ARCHIVES.items()]

    def archive(self, key):
        if key not in ARCHIVES:
            raise ValueError('没有登记这份历史资料。')
        path = (self.media_root / ARCHIVES[key][1]).resolve()
        if not path.is_relative_to(self.media_root) or not path.is_file():
            raise ValueError('历史资料不可用；未以替代文件冒充。')
        return path

    def photo_archive_file(self, filename):
        keys = {'index.html': 'photo-review', 'pack.json': 'photo-pack',
                'caption.txt': 'photo-caption', 'prompts.md': 'photo-prompts'}
        if filename not in keys:
            raise ValueError('没有登记这份历史图文资料。')
        return self.archive(keys[filename])

    def case_media(self, catalog, case_id):
        case = next((case for case in catalog['cases'] if case['id'] == case_id), None)
        media = case.get('media') if case else None
        if isinstance(media, dict) and media.get('resource_id'):
            if self.store is None:
                raise ValueError('此案例的媒体资源当前不可核对。')
            return verified_run_material(self.store, media['resource_id'], media.get('sha256'))
        if not isinstance(media, dict) or not media.get('path') or not media.get('sha256'):
            raise ValueError('此案例尚未登记本地媒体。')
        source = Path(media['path'])
        path = (source if source.is_absolute() else self.repository_root / source).resolve()
        allowed = (self.media_root, (self.repository_root / 'tracks/us').resolve(),
                   Path('J:/E-Business-Video-Data/workbench-discovery/projects').resolve())
        if (not any(path.is_relative_to(root) for root in allowed)
                or path.suffix.lower() not in MEDIA_SUFFIXES or not path.is_file()):
            raise ValueError('登记媒体不在允许的 US 资料目录内或已经不可用。')
        if digest(path) != media['sha256']:
            raise ValueError('媒体字节已变化，不能按原观察版本展示。')
        return path
