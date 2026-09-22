"""Network policy and pinned offline parser; never contacts TikTok."""
import io
import json
from types import SimpleNamespace

import pytest

from studio.agents.client import JobCancelled
from studio import tiktok_reference as tk

VID = '7250000000000000000'
URL = f'https://www.tiktok.com/@fixture/video/{VID}'
CDN = 'https://v16.tiktokcdn.com/video.mp4?temporary=secret'


@pytest.mark.parametrize('url', ['http://www.tiktok.com/@a/video/'+VID,
    'https://www.tiktok.com.evil.test/@a/video/'+VID, 'https://user@www.tiktok.com/@a/video/'+VID,
    'https://www.tiktok.com:444/@a/video/'+VID, 'https://127.0.0.1/', 'https://www.tiktok.com/@a',
    'https://www.tiktok.com/../x', 'https://vm.tiktok.com/a\\b', 'https://vm.tiktok.com/a\n'])
def test_reject_nonvideo_or_unsafe_urls(url):
    with pytest.raises(tk.ReferenceFetchError): tk.checked_url(url)


def test_canonical_tracking_removed_short_link_retained_until_redirect():
    assert tk.checked_url(URL+'?tracking=yes#x') == URL
    assert tk.checked_url('https://vm.tiktok.com/ABC/?tracking=yes') == 'https://vm.tiktok.com/ABC/?tracking=yes'
    assert tk.checked_url('https://www.tiktok.com/t/ABC/')
    assert tk.checked_url(CDN, media=True) == CDN
    with pytest.raises(tk.ReferenceFetchError): tk.checked_url('https://tiktokcdn.com.evil.test/a', media=True)
    assert tk.checked_url('https://v16-webapp-prime.us.tiktok.com/video/tos/id?token=x', media=True)
    with pytest.raises(tk.ReferenceFetchError): tk.checked_url('https://v16-webapp-prime.us.tiktok.com/api/private', media=True)
    with pytest.raises(tk.ReferenceFetchError): tk.checked_url('https://v16-webapp-prime.tiktok.com.evil.test/video/id', media=True)


@pytest.mark.parametrize('ips', [['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'], ['::1'], ['fc00::1'], ['8.8.8.8', '192.168.1.1']])
def test_dns_rejects_any_nonpublic_address(monkeypatch, ips):
    monkeypatch.setattr(tk.socket, 'getaddrinfo', lambda *_a, **_kw: [(0, 0, 0, '', (ip, 443)) for ip in ips])
    with pytest.raises(tk.ReferenceFetchError): tk.public_addresses('www.tiktok.com')


def test_connect_uses_numeric_ip_and_original_tls_hostname(monkeypatch):
    calls = []
    raw = SimpleNamespace(getpeername=lambda: ('8.8.8.8', 443), close=lambda: None)
    monkeypatch.setattr(tk.socket, 'create_connection', lambda address, timeout: calls.append(address) or raw)
    connection = tk.PinnedHTTPS('www.tiktok.com', '8.8.8.8', 5)
    connection._context = SimpleNamespace(wrap_socket=lambda sock, server_hostname: calls.append(server_hostname) or raw)
    connection.connect()
    assert calls == [('8.8.8.8', 443), 'www.tiktok.com']
    raw.getpeername = lambda: ('127.0.0.1', 443)
    with pytest.raises(OSError): connection.connect()


def fake_network(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(tk, 'public_addresses', lambda host: ['8.8.8.8'])
    class Connection:
        sock = None
        def __init__(self, host, address, timeout): calls.append(host)
        def request(self, *_a, **_kw): pass
        def getresponse(self):
            status, headers, raw = responses.pop(0)
            return SimpleNamespace(status=status, getheader=headers.get, read=io.BytesIO(raw).read)
        def close(self): pass
    monkeypatch.setattr(tk, 'PinnedHTTPS', Connection)
    return calls


def test_redirect_revalidated_before_another_connection(monkeypatch):
    calls = fake_network(monkeypatch, [(302, {'Location': 'https://127.0.0.1/private'}, b'')])
    with pytest.raises(tk.ReferenceFetchError): tk.SafeDownload().get(URL)
    assert calls == ['www.tiktok.com']


@pytest.mark.parametrize('headers,raw', [({'Content-Length': '100'}, b'a'),
    ({'Content-Length': '-1'}, b'a'), ({'Content-Encoding': 'gzip'}, b'a'), ({}, b''), ({}, b'12345')])
def test_incomplete_encoded_or_oversized_body_rejected(monkeypatch, headers, raw):
    fake_network(monkeypatch, [(200, headers, raw)])
    monkeypatch.setattr(tk, 'MAX_UPLOAD_BYTES', 4)
    with pytest.raises(tk.ReferenceFetchError): tk.SafeDownload().get(CDN, media=True)


def test_cancellation_before_network(monkeypatch):
    calls = fake_network(monkeypatch, [])
    def cancel(): raise JobCancelled()
    with pytest.raises(JobCancelled): tk.SafeDownload(cancel).get(URL)
    assert not calls


def page(**changes):
    item = dict(id=VID, desc='Fixture only', author={'uniqueId': 'fixture'},
                video={'width': 720, 'height': 1280, 'duration': 4, 'playAddr': CDN, 'downloadAddr': CDN}, **changes)
    return ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'+json.dumps({
        '__DEFAULT_SCOPE__': {'webapp.video-detail': {'statusCode': 0, 'itemInfo': {'itemStruct': item}}}})+'</script>').encode()


def test_pinned_extractor_parses_public_page_without_own_network(monkeypatch):
    import yt_dlp
    def no_network(*_a, **_kw): raise AssertionError('extractor network forbidden')
    monkeypatch.setattr(yt_dlp.YoutubeDL, 'urlopen', no_network)
    info = tk.parse_page(page(), URL)
    assert info['video_id'] == VID and info['duration_seconds'] == 4
    assert info['url'] == CDN and info['extractor_version'] == tk.VERSION


@pytest.mark.parametrize('raw', [b'<html>login</html>', page(imagePost={'images': []}), page(isContentClassified=True)])
def test_unavailable_or_unsupported_page_fails(raw):
    with pytest.raises(tk.ReferenceFetchError): tk.parse_page(raw, URL)


def test_fetch_does_not_persist_signed_playback_url(monkeypatch):
    responses = [(200, {}, page()), (200, {}, b'video fixture')]
    fake_network(monkeypatch, responses)
    raw, source = tk.fetch_reference(URL+'?tracking=hidden')
    assert raw == b'video fixture' and source['source_url'] == URL
    assert 'secret' not in json.dumps(source) and 'url' not in source
