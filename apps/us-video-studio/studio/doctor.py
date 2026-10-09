"""Read-only local startup checks and a single checked launch entry."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from .paths import BUSINESS_ROOT, PROJECT_ROOT, is_source_path

APP_ROOT = Path(__file__).resolve().parents[1]

REPOSITORY = PROJECT_ROOT
REQUIREMENTS = APP_ROOT / 'requirements-runtime.txt'
ROOT_MARKER = '.studio-root.json'


def _source_label(path: Path) -> str:
    if path.is_relative_to(REPOSITORY):
        return 'app:' + path.relative_to(REPOSITORY).as_posix()
    return 'business:' + path.relative_to(BUSINESS_ROOT).as_posix()


def _fingerprint_paths() -> list[Path]:
    """All backend code and model-readable US methods, in stable order."""
    paths = list((APP_ROOT / 'studio').rglob('*.py'))
    shared_learning = REPOSITORY / 'scripts/creative_learning.py'
    if shared_learning.is_file():
        paths.extend((REPOSITORY / 'scripts').glob('*.py'))
    paths += list((APP_ROOT / 'docs/methods').glob('*.md'))
    for name in ('us-commerce-strategy', 'us-creator-video', 'us-photo-copy'):
        paths += list((BUSINESS_ROOT / '.agents/skills' / name).rglob('*.md'))
    return sorted(paths, key=lambda path: _source_label(path))


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in _fingerprint_paths():
        digest.update(_source_label(path).encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def source_version() -> dict:
    """Identify loaded source without network, raw paths or Git diff contents."""
    try:
        revision = subprocess.run(['git', 'rev-parse', '--short=12', 'HEAD'], cwd=REPOSITORY,
                                  capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        if not revision or any(c not in '0123456789abcdef' for c in revision):
            return {'source_revision': None, 'source_modified': None}
        changed = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal', '--', '.'],
                                 cwd=REPOSITORY, capture_output=True, timeout=2)
        return {'source_revision': revision,
                'source_modified': bool(changed.stdout.strip()) if changed.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {'source_revision': None, 'source_modified': None}


def runtime_identity(data_root: Path) -> dict:
    """Safe, frozen-on-call identity. No credential or project contents are read."""
    root = Path(data_root).expanduser().resolve()
    return {
        **source_version(),
        'started_at': datetime.now(timezone.utc).isoformat(),
        'process_id': os.getpid(),
        'hot_reload': {'enabled': False, 'state': 'disabled'},
        'code_fingerprint': code_fingerprint(),
        'data_root_id': hashlib.sha256(str(root).casefold().encode('utf-8')).hexdigest()[:12],
        'source_root_id': hashlib.sha256(str(PROJECT_ROOT).casefold().encode('utf-8')).hexdigest()[:12],
        'business_root_id': hashlib.sha256(str(BUSINESS_ROOT).casefold().encode('utf-8')).hexdigest()[:12],
        'python': sys.version.split()[0],
        'dependencies': {name: _version(name) for name in ('fastapi', 'uvicorn', 'pydantic', 'av')},
        'media_tools': {name: bool(shutil.which(name)) for name in ('ffmpeg', 'ffprobe')},
        'meaning': '仅表示当前进程加载的代码与本机依赖；未验证模型连接、创意质量或媒体生成。',
    }


def _version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _probe_lock(path: Path):
    """Probe an existing OS lock without creating or changing its contents."""
    with path.open('r+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _requirements() -> dict[str, str]:
    versions = {}
    for raw in REQUIREMENTS.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if '==' in line:
            name, version = line.split('==', 1)
            versions[name] = version
    return versions


def _has_root_marker(root: Path) -> bool:
    try:
        marker = json.loads((root / ROOT_MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get('schema_version') == 1


def _has_project_data(root: Path) -> bool:
    return _has_root_marker(root) or any((root / name).exists()
                                         for name in ('runs', 'connections.json', 'config.json'))


def mark_new_root(root: Path) -> None:
    """Persist an explicit first-start choice without treating a lock as data."""
    marker = root / ROOT_MARKER
    if _has_root_marker(root):
        return
    with marker.open('x', encoding='utf-8', newline='\n') as target:
        json.dump({'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat()}, target)
        target.write('\n')


def inspect(data_root: Path, port: int = 4760, *, host: str = '127.0.0.1', allow_new: bool = False) -> dict:
    """Inspect without creating the data root, opening connections or calling models."""
    root = Path(data_root).expanduser().resolve()
    checks = []

    def add(name, status, detail, remedy='', required=True):
        checks.append({'name': name, 'status': status, 'detail': detail,
                       'remedy': remedy, 'required': required})

    supported = sys.version_info[:2] == (3, 12)
    add('python', 'ok' if supported else 'error', sys.version.split()[0],
        '请用 Python 3.12 建立或修复运行 venv。' if not supported else '')
    for name, wanted in _requirements().items():
        actual = _version(name)
        add('dependency:' + name, 'ok' if actual == wanted else 'error',
            f'{actual or "未安装"}；需要 {wanted}',
            f'在所选解释器安装 requirements-runtime.txt 后重查。' if actual != wanted else '')

    safe_root = not is_source_path(root)
    exists = root.is_dir()
    add('data_root', 'ok' if safe_root and exists else 'error',
        '已找到所选 Git 外数据目录。' if safe_root and exists else '数据根不存在或位于 Git 仓库内。',
        '请显式选择已恢复的 Git 外数据根；不要启动一个空目录。' if not safe_root or not exists else '')
    if exists and safe_root:
        from .project_integrity import project_catalog, public_diagnostic
        projects = project_catalog(root)
        unavailable = [public_diagnostic(item) for item in projects if not item['available']]
        add('project_integrity', 'warning' if unavailable else 'ok',
            f'{len(projects) - len(unavailable)} 个项目可用；{len(unavailable)} 个项目资料损坏，隔离并保留原件。',
            '启动后在项目首页查看错误；不要自动截断或覆盖原文件。' if unavailable else '', required=False)
        # A lock file alone can be left by an aborted first start. It is not a
        # project identity and must not turn an empty folder into a restored one.
        known = _has_project_data(root)
        add('project_data', 'ok' if known or allow_new else 'error',
            '已找到 Studio 数据标识。' if known else '目录为空或不是已恢复的 Studio 数据根。',
            '先恢复原项目；若确实要新建空工作台，显式使用 --allow-new。' if not known and not allow_new else '')
        lock_file = root / '.studio-writer.lock'
        if lock_file.exists():
            try:
                _probe_lock(lock_file)
            except Exception:
                add('writer', 'error', '此数据根已有写入进程，或 writer 锁不可用。',
                    '请连接原服务；更新前先等待当前任务结束并停止原服务。')
            else:
                add('writer', 'ok', 'writer 锁可取得。')
        else:
            add('writer', 'ok', '尚无 writer 锁文件；启动时会创建并取得锁。')
        supervisor_lock = root / '.studio-supervisor.lock'
        try:
            if supervisor_lock.exists():
                _probe_lock(supervisor_lock)
        except OSError:
            add('supervisor', 'error', '此数据根已有热重载启动器，或监督锁不可用。',
                '请连接原服务；查看启动日志，修复代码后会自动恢复。不要另启第二个启动器。')
        else:
            add('supervisor', 'ok', '没有运行中的热重载启动器。')
        try:
            from .config import load_settings
            from .connections import ConnectionStore
            load_settings(root)
            public = ConnectionStore(root).public()
            add('connections', 'ok', f"配置可读取；{len(public['connections'])} 个服务，{len(public['roles'])} 个角色绑定。"
                '未验证登录、密钥、余额或模型生成。')
        except Exception:
            add('connections', 'error', '配置无法读取或校验。',
                '保留原 config.json / connections.json，按恢复说明核对，不要覆盖凭据。')

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError:
        add('port', 'error', f'{host}:{port} 无法监听。',
            '检查是否已有 Studio 服务占用该端口；连接原服务或选择空闲端口。')
    else:
        add('port', 'ok', f'{host}:{port} 可监听。')
    for name in ('ffmpeg', 'ffprobe'):
        path = shutil.which(name)
        add(name, 'ok' if path else 'warning', '已找到。' if path else '未找到；部分音视频分析不可用。',
            '安装 FFmpeg 并重新启动终端后再检查。' if not path else '', required=False)
    try:
        from .learning import validate_method_packages
        package_errors = validate_method_packages()
    except Exception:
        package_errors = ['方法包无法校验']
    add('method_packages', 'error' if package_errors else 'ok',
        '；'.join(package_errors) if package_errors else 'Skill 方法包、来源与工具映射均可读取。',
        '恢复仓库中的方法卡及来源文件后重查。' if package_errors else '')
    return {'ready_to_start': not any(c['required'] and c['status'] == 'error' for c in checks),
            'data_root': str(root), 'checks': checks,
            'unavailable_projects': unavailable if exists and safe_root else []}


def main() -> None:
    parser = argparse.ArgumentParser(description='US Studio 本地启动诊断（不调用模型）')
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--port', type=int, default=4760)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--allow-new', action='store_true', help='明确允许已存在的空数据根作为新工作台')
    parser.add_argument('--start', action='store_true', help='检查通过后启动本地服务')
    parser.add_argument('--reload', action='store_true', help='空闲时自动加载后端代码变更')
    parser.add_argument('--no-reload', action='store_true', help='关闭空闲时自动加载代码更新')
    parser.add_argument('--reload-worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = inspect(args.data_root, args.port, allow_new=args.allow_new)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report['checks']:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
            if check['remedy']:
                print('  ' + check['remedy'])
        print('可以启动。' if report['ready_to_start'] else '尚不能启动；请先修复上面的必需项。')
    if not report['ready_to_start']:
        raise SystemExit(2)
    if args.start:
        root = args.data_root.expanduser().resolve()
        if args.allow_new and not _has_project_data(root):
            mark_new_root(root)
        if args.reload and not args.no_reload:
            from .dev_reload import supervise
            raise SystemExit(supervise(root, args.port))
        import uvicorn
        from .config import load_settings
        from .server import build_app
        if args.reload_worker:
            from .runtime_reload import serve
            raise SystemExit(serve(build_app(load_settings(root)), args.port, auto_reload=not args.no_reload))
        uvicorn.run(build_app(load_settings(root)), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
