"""Content-versioned local page resources; isolated HTTP, no model or spend."""
from __future__ import annotations

import hashlib
import html
import os
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from local_http import TestClient

from studio import server
from studio.config import Settings


def resource_urls(page):
    return [html.unescape(url) for url in re.findall(r'''(?:src|href)=["'](/static/[^"']+)["']''', page)]


def client_for(tmp_path, monkeypatch=None, web_dir=None):
    if web_dir is not None:
        monkeypatch.setattr(server, 'WEB_DIR', web_dir)
    settings = Settings(data_root=tmp_path / 'data', base_url='https://example.invalid/model')
    app = server.build_app(settings, client_factory=lambda: pytest.fail('static requests must not call a model'))
    return TestClient(app)


def test_actual_index_versions_every_resource_by_the_served_bytes(tmp_path):
    with client_for(tmp_path) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert page.headers['cache-control'] == 'no-cache'
        assert page.headers['content-type'].startswith('text/html')
        assert page.headers['x-content-type-options'] == 'nosniff'
        assert page.headers['content-security-policy'] == "frame-ancestors 'none'"
        urls = resource_urls(page.text)
        assert len(urls) > 40
        by_path = {}
        for url in urls:
            parts = urlsplit(url)
            digest = hashlib.sha256((server.WEB_DIR / parts.path.removeprefix('/static/')).read_bytes()).hexdigest()
            assert parse_qs(parts.query) == {'v': [digest]}
            assert by_path.setdefault(parts.path, url) == url
        assert 'studio-current-' not in page.text
        assert page.text.index('/static/runtime.js?') < page.text.index('/static/app.js?')
        assert page.text.index('/static/codex-login.js?') < page.text.index('/static/connections.js?')
        current = client.get(by_path['/static/connections.js'])
        assert current.status_code == 200
        assert current.headers['cache-control'] == 'no-cache'
        assert hashlib.sha256(current.content).hexdigest() == parse_qs(urlsplit(by_path['/static/connections.js']).query)['v'][0]
        assert client.get('/').content == page.content, 'unchanged files do not get random or time-based URLs'
        assert 'cache-control' not in client.get('/api/runs').headers


def test_file_content_changes_version_without_restart_even_with_same_size_and_time(tmp_path, monkeypatch):
    web = tmp_path / 'web'
    web.mkdir()
    script, style = web / 'app.js', web / 'style.css'
    script.write_bytes(b'window.release = "old";')
    style.write_bytes(b'body { color: red; }')
    (web / 'index.html').write_text('<link href="/static/style.css"><script src="/static/app.js"></script>', encoding='utf-8')
    with client_for(tmp_path, monkeypatch, web) as client:
        before = {urlsplit(url).path: url for url in resource_urls(client.get('/').text)}
        previous = script.stat()
        script.write_bytes(b'window.release = "new";')
        os.utime(script, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        assert script.stat().st_size == previous.st_size
        assert script.stat().st_mtime_ns == previous.st_mtime_ns
        after = {urlsplit(url).path: url for url in resource_urls(client.get('/').text)}
        assert after['/static/app.js'] != before['/static/app.js']
        assert after['/static/style.css'] == before['/static/style.css']
        assert client.get(after['/static/app.js']).content == b'window.release = "new";'
        assert client.get('/static/app.js').headers['cache-control'] == 'no-cache'
        assert client.get('/').text == client.get('/').text


def test_replaces_legacy_versions_and_preserves_query_fragment_and_external_urls(tmp_path):
    (tmp_path / 'app.js').write_bytes(b'/* offline fixture */')
    original = '''<script src='/static/app.js?v=old&amp;language=en#entry'></script>
<script src="https://example.invalid/app.js?v=remote"></script><p>Offline text</p>'''
    (tmp_path / 'index.html').write_text(original, encoding='utf-8')
    page = server._versioned_index(tmp_path)
    urls = resource_urls(page)
    assert len(urls) == 1
    parts = urlsplit(urls[0])
    assert parse_qs(parts.query) == {'language': ['en'], 'v': [hashlib.sha256((tmp_path / 'app.js').read_bytes()).hexdigest()]}
    assert parts.fragment == 'entry'
    assert '&amp;v=' in page
    assert 'https://example.invalid/app.js?v=remote' in page
    assert '<p>Offline text</p>' in page


@pytest.mark.parametrize('resource', ['missing.js', '../outside.js'])
def test_invalid_local_resource_cannot_be_presented_as_a_valid_version(tmp_path, resource):
    web = tmp_path / 'web'
    web.mkdir()
    (tmp_path / 'outside.js').write_bytes(b'private fixture')
    (web / 'index.html').write_text(f'<script src="/static/{resource}"></script>', encoding='utf-8')
    expected = ValueError if resource.startswith('../') else FileNotFoundError
    with pytest.raises(expected):
        server._versioned_index(web)
