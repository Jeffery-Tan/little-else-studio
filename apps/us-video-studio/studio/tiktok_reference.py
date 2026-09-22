"""Single public TikTok reference: pinned HTTPS I/O and offline yt-dlp parsing."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
from urllib.parse import urlsplit, urlunsplit, urljoin

from .models import PipelineError
from .materials import MAX_UPLOAD_BYTES

VERSION = '2026.08.19'
PAGE_HOSTS = {'www.tiktok.com', 'tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'}
CDN_DOMAINS = ('tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com', 'tiktokv.com',
               'byteoversea.com', 'ibytedtos.com', 'muscdn.com', 'akamaized.net', 'tiktokcdn-in.com')
VIDEO_PATH = re.compile(r'/@[\w.-]+/video/([0-9]{10,24})/?\Z', re.ASCII)
SHORT_PATH = re.compile(r'/(?:t/)?[A-Za-z0-9]+/?\Z')
WEB_MEDIA_HOST = re.compile(r'v[0-9]+-web(?:app)?(?:-[a-z0-9]+)*(?:\.(?:us|eu))?\.tiktok\.com\Z')


class ReferenceFetchError(PipelineError):
    def __init__(self, message, code='reference_fetch'):
        super().__init__(code, message)


def checked_url(url, *, media=False):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url) or '\\' in url:
        raise ReferenceFetchError('链接格式无效，请粘贴单条TikTok视频链接。')
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        host = p.hostname or ''
        if media:
            if not (any(host == d or host.endswith('.' + d) for d in CDN_DOMAINS)
                    or (WEB_MEDIA_HOST.fullmatch(host) and p.path.startswith('/video/'))):
                raise ValueError()
        elif host not in PAGE_HOSTS or not (VIDEO_PATH.fullmatch(p.path) or
                (host in {'vm.tiktok.com', 'vt.tiktok.com'} and SHORT_PATH.fullmatch(p.path)) or
                (host in {'www.tiktok.com', 'tiktok.com'} and p.path.startswith('/t/') and SHORT_PATH.fullmatch(p.path))):
            raise ValueError()
        # Tracking queries are not needed for canonical long links. Short-link
        # queries are kept in memory for redirects, but not persisted in events.
        return urlunsplit(('https', host, p.path, p.query if media or not VIDEO_PATH.fullmatch(p.path) else '', ''))
    except ValueError:
        raise ReferenceFetchError('只支持公开的单条TikTok视频链接；该地址或跳转不在支持范围。') from None


def public_addresses(host):
    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(not ipaddress.ip_address(ip).is_global or '%' in ip for ip in addresses):
            raise ValueError()
        return addresses
    except (OSError, ValueError):
        raise ReferenceFetchError('无法解析为允许的公开网络地址。') from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect directly to the checked numeric address; no second DNS lookup.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            if ipaddress.ip_address(raw.getpeername()[0]) != ipaddress.ip_address(self.address):
                raise OSError('peer mismatch')
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SafeDownload:
    def __init__(self, check_cancel=lambda: None):
        self.check_cancel = check_cancel
        self.deadline = time.monotonic() + 120
        self.requests = 0

    def check(self):
        self.check_cancel()
        if time.monotonic() >= self.deadline or self.requests >= 12:
            raise ReferenceFetchError('获取视频超时，请上传原视频继续。')

    def get(self, url, *, media=False, referer=None):
        limit = MAX_UPLOAD_BYTES if media else 4 * 1024 * 1024
        for _ in range(6):
            self.check()
            url = checked_url(url, media=media)
            p = urlsplit(url)
            addresses = public_addresses(p.hostname)
            self.check()
            self.requests += 1
            connection = PinnedHTTPS(p.hostname, addresses[0], min(15, max(1, self.deadline - time.monotonic())))
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36',
                           'Accept-Encoding': 'identity', 'Accept': '*/*'}
                if referer:
                    headers['Referer'] = checked_url(referer)
                connection.request('GET', urlunsplit(('', '', p.path or '/', p.query, '')), headers=headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location:
                        raise ReferenceFetchError('短链接没有可用的目标地址。')
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ReferenceFetchError(f'TikTok获取失败（HTTP {response.status}），可以上传原视频继续。')
                size = response.getheader('Content-Length')
                if size and (not size.isdigit() or int(size) > limit):
                    raise ReferenceFetchError('参考视频或页面超过当前大小上限。')
                if (response.getheader('Content-Encoding') or 'identity').lower() != 'identity':
                    raise ReferenceFetchError('页面采用不支持的传输编码，请上传原视频继续。')
                chunks, total = [], 0
                while True:
                    self.check()
                    if connection.sock:
                        connection.sock.settimeout(min(15, max(1, self.deadline - time.monotonic())))
                    data = response.read(min(65536, limit + 1 - total))
                    if not data:
                        break
                    chunks.append(data)
                    total += len(data)
                    if total > limit:
                        raise ReferenceFetchError('参考视频超过64 MiB，请上传较小原视频继续。')
                if not total or (size and total != int(size)):
                    raise ReferenceFetchError('下载文件不完整，请上传原视频继续。')
                return b''.join(chunks), url
            except (OSError, http.client.HTTPException):
                raise ReferenceFetchError('无法连接TikTok或视频下载中断，请上传原视频继续。') from None
            finally:
                connection.close()
        raise ReferenceFetchError('TikTok链接跳转过多，无法获取参考视频。')


def parse_page(raw, url):
    """No extractor network, plugins, cookies, subprocess, or JS execution."""
    try:
        import yt_dlp
        from yt_dlp.extractor.tiktok import TikTokIE
        from yt_dlp.version import __version__
    except ImportError:
        raise ReferenceFetchError('链接解析组件尚未安装，请更新工作台运行依赖。') from None
    if __version__ != VERSION:
        raise ReferenceFetchError('链接解析组件版本不匹配，请更新工作台运行依赖。')
    match = VIDEO_PATH.fullmatch(urlsplit(checked_url(url)).path)
    if not match:
        raise ReferenceFetchError('短链接未指向可识别的视频，请上传原视频继续。')
    video_id = match[1]

    class OfflineTikTok(TikTokIE):
        def _request_webpage(self, *args, **kwargs):
            raise ReferenceFetchError('解析器请求了未支持的网络操作。')
        def _get_subtitles(self, *args, **kwargs):
            return {}

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'cachedir': False,
                              'writesubtitles': False, 'writeautomaticsub': False}, auto_init=False) as ydl:
            parser = OfflineTikTok(ydl)
            html = raw.decode('utf-8')
            scope = parser._get_universal_data(html, video_id)
            detail = scope.get('webapp.video-detail', {})
            item = detail.get('itemInfo', {}).get('itemStruct')
            if not item:
                item = parser._get_sigi_state(html, video_id).get('ItemModule', {}).get(video_id)
            if detail.get('statusCode', 0) != 0 or not isinstance(item, dict) or str(item.get('id')) != video_id:
                raise ReferenceFetchError('页面无法公开读取或视频已失效，请上传原视频继续。')
            if item.get('imagePost') or item.get('isContentClassified') or not item.get('video'):
                raise ReferenceFetchError('当前仅支持可公开访问的视频，不能处理图集或受限内容。')
            info = parser._parse_aweme_video_web(item, url, video_id)
    except ReferenceFetchError:
        raise
    except Exception:
        raise ReferenceFetchError('未能解析TikTok视频页面，请上传原视频继续。') from None
    if not isinstance(info.get('duration'), (int, float)) or not 0 < info['duration'] <= 90:
        raise ReferenceFetchError('参考视频需不超过90秒。')
    formats = [f for f in info.get('formats', []) if f.get('vcodec') not in ('none', 'bytevc2')
               and f.get('acodec') != 'none' and f.get('url') and f.get('ext') == 'mp4'
               and (f.get('filesize') or 0) <= MAX_UPLOAD_BYTES]
    # Do not substitute the music track: it may omit speech or use a different clock.
    formats.sort(key=lambda f: (f.get('format_id') == 'download', f.get('height') or 0), reverse=True)
    if not formats:
        raise ReferenceFetchError('没有可用的声画视频文件，请上传原视频继续。')
    selected_url = None
    for item in formats:
        try:
            selected_url = checked_url(item['url'], media=True)
            break
        except ReferenceFetchError:
            continue
    if selected_url is None:
        raise ReferenceFetchError('没有支持的公开视频下载地址，请上传原视频继续。')
    return {'video_id': video_id, 'title': str(info.get('title') or '')[:300],
            'author': str(info.get('uploader') or '')[:100], 'duration_seconds': info['duration'],
            'url': selected_url, 'extractor_version': __version__}


def fetch_reference(url, check_cancel=lambda: None):
    source = checked_url(url)
    download = SafeDownload(check_cancel)
    page, canonical = download.get(source)
    info = parse_page(page, canonical)
    check_cancel()
    raw, _ = download.get(info.pop('url'), media=True, referer=canonical)
    return raw, {**info, 'source_url': urlunsplit(urlsplit(source)._replace(query='')),
                 'canonical_url': checked_url(canonical), 'sha256': hashlib.sha256(raw).hexdigest()}
