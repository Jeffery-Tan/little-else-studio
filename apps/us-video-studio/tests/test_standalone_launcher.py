"""Launcher reuse checks use a disposable local HTTP stub, never a real service."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from studio.paths import BUSINESS_ROOT, PROJECT_ROOT


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
@pytest.mark.parametrize('identity', ['matching', 'legacy', 'other_business', 'other_code'])
@pytest.mark.parametrize('connected', [True, False])
def test_launcher_only_reuses_matching_code_business_and_data(tmp_path, identity, connected):
    executable = shutil.which('powershell') or shutil.which('pwsh')
    if not executable:
        pytest.skip('PowerShell unavailable')
    def fingerprint(path):
        return hashlib.sha256(str(Path(path).resolve()).casefold().encode()).hexdigest()[:12]
    business = tmp_path / 'business'
    if connected:
        business.mkdir()
    runtime = {
        'data_root_id': fingerprint(tmp_path),
        'source_root_id': fingerprint(PROJECT_ROOT),
        'business_root_id': fingerprint(business) if connected else 'not-configured',
        'hot_reload': {'enabled': True}, 'process_id': 1,
    }
    if identity == 'legacy':
        runtime.pop('source_root_id')
        runtime.pop('business_root_id')
    elif identity == 'other_business':
        runtime['business_root_id'] = 'other'
    elif identity == 'other_code':
        runtime['source_root_id'] = 'other'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(runtime).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([executable, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(PROJECT_ROOT / 'scripts/start.ps1'),
            *(['-BusinessRoot', str(business)] if connected else []),
            '-DataRoot', str(tmp_path), '-Python', sys.executable,
            '-Port', str(server.server_port), '-NoReload'], capture_output=True, timeout=30)
        assert (result.returncode == 0) == (identity == 'matching'), result.stderr.decode(errors='replace')
        if identity != 'matching':
            assert b'another code or business root' in result.stderr
        assert list(tmp_path.iterdir()) == ([business] if connected else [])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
