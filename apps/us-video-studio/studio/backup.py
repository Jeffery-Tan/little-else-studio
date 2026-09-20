"""Portable project snapshots; no credentials, overwrite, model calls, or auto sync.

Export reads a quiet running service and checks its revision and every source byte
again before publishing the ZIP. Restore only creates a new, separate data root.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit
import zipfile

MANIFEST = 'studio-backup.json'
TEXT = {'.json', '.jsonl', '.txt', '.md', '.py', '.html', '.log', '.ffgraph', '.yaml', '.yml'}
SECRET = re.compile(rb'(?i)(sk-[a-z0-9_-]{20,}|Bearer\s+[a-z0-9_.-]{20,}|'
                    rb'https?://[^/\s"\x27]+:[^/\s"\x27]+@|'
                    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|'
                    rb'[?&](?:Signature|X-Amz-Signature|x-oss-signature|security-token|access_token|api_key)=|'
                    rb'["\x27]?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|password|secret[_-]?key|protected_key)'
                    rb'["\x27]?\s*[:=]\s*["\x27][^"\x27\r\n]{12,})')
CONFIG_FIELDS = ('base_url', 'models', 'pricing', 'budget_limit_usd', 'budget_limit_tokens',
                 'max_tool_rounds', 'max_tokens', 'max_continuations')
CONNECTION_FIELDS = ('id', 'name', 'provider', 'base_url', 'pricing', 'token_limit_parameter')


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_name(name):
    parts = PurePosixPath(name).parts
    if (not parts or name != '/'.join(parts) or '\\' in name
            or any(p in {'.', '..'} or ':' in p or p.endswith((' ', '.')) for p in parts)
            or any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I)
                   or re.search(r'[<>"|?*\x00-\x1f]', p) for p in parts)
            or PurePosixPath(name).is_absolute()):
        raise ValueError('Unsafe archive path')
    if name not in {'config.json', 'connections.json', MANIFEST} and not (
            len(parts) >= 3 and parts[0] == 'runs' and re.fullmatch(r'[a-z0-9][a-z0-9-]*', parts[1])):
        raise ValueError('Unexpected backup path')
    return name


def inventory(root):
    paths = {}
    for path in sorted((root / 'runs').rglob('*')):
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
            raise ValueError('Linked source paths are not supported')
        if path.is_file():
            name = safe_name(path.relative_to(root).as_posix())
            if path.name.startswith('.env') or path.suffix.lower() in {'.key', '.pem', '.pfx', '.p12'}:
                raise ValueError(f'Credential file is not portable: {name}')
            paths[name] = path
    if not paths or not any(n.endswith('/run.json') and len(n.split('/')) == 3 for n in paths):
        raise ValueError('No Studio projects found')
    for name in ('config.json', 'connections.json'):
        path = root / name
        if path.exists():
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Linked configuration is not supported')
            paths[name] = path
    return paths


def portable_bytes(name, data):
    if name == 'config.json':
        raw = json.loads(data)
        data = encode({k: raw[k] for k in CONFIG_FIELDS if k in raw})
    elif name == 'connections.json':
        raw = json.loads(data)
        data = encode({'schema_version': raw['schema_version'], 'revision': raw['revision'],
                       'connections': [{k: c[k] for k in CONNECTION_FIELDS if k in c}
                                       for c in raw['connections']], 'roles': raw['roles']})
    if name in {'config.json', 'connections.json'}:
        clean = json.loads(data)
        urls = ([clean.get('base_url', '')] if name == 'config.json'
                else [c.get('base_url', '') for c in clean['connections']])
        for url in urls:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('Configured endpoint contains private URL components')
    if Path(name).suffix.lower() in TEXT and SECRET.search(data):
        raise ValueError(f'Potential credential or signed URL; not exported: {name}')
    return data


def omission_record(root, name):
    safe_name(name)
    parts = PurePosixPath(name).parts
    if len(parts) < 6 or parts[2:4] != ('history', 'archive') or Path(name).suffix != '.json':
        raise ValueError('Only explicitly selected, uncurated historical JSON can be omitted')
    history = json.loads((root / 'runs' / parts[1] / 'history' / 'manifest.json').read_bytes())
    record = next((r for r in history['files'] if r['archive_path'] == '/'.join(parts[4:])), None)
    protected = {d['id'] for d in history['documents']} | {m['archive_id'] for m in history['materials']}
    if record is None or record['id'] in protected:
        raise ValueError('Cannot omit curated documents or material sources')
    return {'path': name, 'reason': 'Uncurated provider response containing a signed download URL; original retained on source computer.'}


def live_state(url, run_ids):
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
        raise ValueError('Only the local Studio origin is allowed')
    def get(path):
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=15)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError('Could not verify local Studio state')
            return json.loads(response.read())
        finally:
            connection.close()
    if sorted(r['run_id'] for r in get('/api/runs')) != sorted(run_ids):
        raise ValueError('Service and source project lists differ')
    result = {}
    for run_id in run_ids:
        state = get(f'/api/runs/{run_id}/recovery')
        if state['active_run_id'] or state['open_attempts'] or state['unresolved_calls']:
            raise ValueError('Finish active work and resolve pending calls before backing up')
        result[run_id] = state['revision']
    return result


def export_backup(root, output, *, live_url=None, omit=(), code_commit='unknown'):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root) or output.exists():
        raise ValueError('Choose a new backup file outside the data root')
    if not root.is_dir():
        raise ValueError('Source data root does not exist')
    if live_url:
        lease = nullcontext()
    else:
        from .jobs import WriterLease
        lease = WriterLease(root)
    created = False
    try:
        with lease:
            paths = inventory(root)
            run_ids = sorted(n.split('/')[1] for n in paths if n.endswith('/run.json') and len(n.split('/')) == 3)
            before = live_state(live_url, run_ids) if live_url else None
            omissions = [omission_record(root, name) for name in sorted(set(omit))]
            if any(r['path'] not in paths for r in omissions):
                raise ValueError('Omitted file is missing')
            entries, source_hashes = [], {}
            with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                created = True
                for name, path in paths.items():
                    data = path.read_bytes()
                    source_hashes[name] = digest(data)
                    if name in omit:
                        continue
                    data = portable_bytes(name, data)
                    archive.writestr(name, data)
                    entries.append({'path': name, 'bytes': len(data), 'sha256': digest(data)})
                if set(inventory(root)) != set(paths) or any(file_digest(path) != source_hashes[name] for name, path in paths.items()):
                    raise ValueError('Source changed during backup; retry when idle')
                if live_url and live_state(live_url, run_ids) != before:
                    raise ValueError('Studio changed during backup; retry when idle')
                manifest = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                            'code_commit': code_commit, 'run_ids': run_ids, 'files': entries,
                            'omissions': omissions, 'credentials_included': False,
                            'connection_keys_removed': True, 'automatic_merge_supported': False}
                archive.writestr(MANIFEST, encode(manifest))
        return manifest
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [safe_name(i.filename) for i in infos]
        if len(set(n.casefold() for n in names)) != len(names) or any(
                stat.S_ISLNK(i.external_attr >> 16) or i.is_dir() for i in infos):
            raise ValueError('Duplicate or linked archive entries')
        if len(infos) > 100000 or sum(i.file_size for i in infos) > 10 * 1024**3 or any(i.file_size > 512 * 1024**2 for i in infos):
            raise ValueError('Archive exceeds supported size')
        if archive.getinfo(MANIFEST).file_size > 16 * 1024**2:
            raise ValueError('Manifest exceeds supported size')
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get('schema_version') != 1 or manifest.get('credentials_included') is not False:
            raise ValueError('Unsupported backup manifest')
        entries = manifest['files']
        expected = [safe_name(r['path']) for r in entries]
        if len(set(expected)) != len(expected) or set(names) != set(expected) | {MANIFEST}:
            raise ValueError('Archive does not match its manifest')
        for record in entries:
            data = archive.read(record['path'])
            if len(data) != record['bytes'] or digest(data) != record['sha256']:
                raise ValueError('Backup checksum mismatch')
            if portable_bytes(record['path'], data) != data and record['path'] in {'config.json', 'connections.json'}:
                raise ValueError('Backup contains nonportable configuration')
        return manifest


def restore_backup(path, target):
    target = Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise ValueError('Restore only into a new directory; existing projects are never overwritten')
    manifest = verify_backup(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.studio-restore-', dir=target.parent) as staging:
        staged = Path(staging) / 'data'
        staged.mkdir()
        with zipfile.ZipFile(path) as archive:
            for record in manifest['files']:
                destination = staged / record['path']
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(record['path']) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)
                if file_digest(destination) != record['sha256']:
                    raise ValueError('Backup changed during restore')
        (staged / MANIFEST).write_bytes(encode(manifest))
        if target.exists():
            raise ValueError('Restore target was created by another process')
        staged.rename(target)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    export = sub.add_parser('export')
    export.add_argument('--data-root', type=Path, required=True)
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--live-url')
    export.add_argument('--omit-history-file', action='append', default=[])
    export.add_argument('--code-commit', default='unknown')
    for action in ('verify', 'restore'):
        command = sub.add_parser(action)
        command.add_argument('archive', type=Path)
        if action == 'restore':
            command.add_argument('--data-root', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'export':
        result = export_backup(args.data_root, args.output, live_url=args.live_url,
                               omit=args.omit_history_file, code_commit=args.code_commit)
    elif args.action == 'restore':
        result = restore_backup(args.archive, args.data_root)
    else:
        result = verify_backup(args.archive)
    print(json.dumps({'action': args.action, 'runs': result['run_ids'], 'files': len(result['files']),
                      'omissions': result['omissions']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
