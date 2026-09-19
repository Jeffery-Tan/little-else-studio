"""One-attempt Wan 3.0 first-frame video transport; no approvals or auto retries.

The caller owns approval, intent persistence, idempotency, job locks, settlement,
and final media decoding. Merely constructing a client never reads credentials.
"""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

MODEL = 'wan3.0-video'
API_REFERENCE = 'https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference'
PRICE_REFERENCE = 'https://help.aliyun.com/zh/model-studio/wan3-0-video'
PRICE_CHECKED_AT = '2026-09-19'
RATES_CNY = {'480P': Decimal('0.3'), '720P': Decimal('0.6'), '1080P': Decimal('1.2')}
RATIOS = {'adaptive', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'}
STATUSES = {'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED', 'UNKNOWN'}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 150 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_ENDPOINT = re.compile(r'https://[A-Za-z0-9][A-Za-z0-9-]{0,62}\.cn-beijing\.maas\.aliyuncs\.com/api/v1\Z')
_RESULT_HOST = re.compile(r'dashscope-result-[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com\Z')


class WanVideoError(Exception):
    """Safe UI error; an unknown submission must never be blindly resubmitted."""

    def __init__(self, message: str, *, submission_unknown: bool = False,
                 code: str = 'wan_video_error', http_status: int | None = None):
        super().__init__(message)
        self.submission_unknown = submission_unknown
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class WanCredentials:
    api_key: str = field(repr=False)
    workspace_id: str
    region: str = 'cn-beijing'

    @property
    def endpoint(self) -> str:
        endpoint = _configured_endpoint(self.workspace_id, self.region)
        if not isinstance(self.api_key, str) or not self.api_key or any(c in self.api_key for c in '\r\n'):
            raise WanVideoError('百炼凭据不可用。', code='credential_invalid')
        return endpoint


def _configured_endpoint(workspace_id: str, region: str) -> str:
    endpoint = f'https://{workspace_id}.{region}.maas.aliyuncs.com/api/v1'
    if region != 'cn-beijing' or not _ENDPOINT.fullmatch(endpoint):
        raise WanVideoError('百炼视频需要北京地域的有效业务空间配置。', code='configuration_invalid')
    return endpoint


def _credential_directory() -> Path:
    value = os.environ.get('LOCALAPPDATA')
    if not value:
        raise WanVideoError('未找到本机百炼凭据目录。', code='credential_missing')
    return Path(value) / 'E-commerce' / 'credentials'


def existing_credentials_available() -> bool:
    """Presence check only: no decryption, key reads, API calls, or settings writes."""
    try:
        directory = _credential_directory()
        return (directory / 'dashscope.dpapi').is_file() and (directory / 'dashscope.json').is_file()
    except WanVideoError:
        return False


def existing_endpoint() -> str:
    """Read only non-secret connection metadata before persisting an intent.

    Freeze this endpoint with the attempt. Passing it to submit prevents a
    connection change between consent/intent and the actual POST from silently
    sending the generation into another workspace.
    """
    try:
        path = _credential_directory() / 'dashscope.json'
        if path.stat().st_size > 65536:
            raise ValueError('metadata size')
        metadata = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(metadata, dict):
            raise ValueError('metadata type')
        return _configured_endpoint(metadata.get('workspace_id', ''), metadata.get('region', ''))
    except WanVideoError:
        raise
    except (OSError, ValueError, TypeError):
        raise WanVideoError('无法读取本机百炼业务空间配置。', code='configuration_unavailable') from None


def load_existing_credentials() -> WanCredentials:
    """Explicitly decrypt the already-configured Windows DPAPI credential.

    Called only for a user-submitted generation or querying its existing task.
    This neither imports old run approvals nor modifies account configuration.
    """
    if os.name != 'nt':
        raise WanVideoError('当前百炼凭据仅支持原 Windows 用户。', code='credential_unavailable')
    try:
        directory = _credential_directory()
        metadata = json.loads((directory / 'dashscope.json').read_text(encoding='utf-8-sig'))
        encrypted_file = directory / 'dashscope.dpapi'
        if encrypted_file.stat().st_size > 65536:
            raise ValueError('size')
        encrypted = bytes.fromhex(encrypted_file.read_text(encoding='utf-8').strip())

        class Blob(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        source, target = Blob(len(encrypted), buffer), Blob()
        crypt = ctypes.windll.crypt32
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
            raise ValueError('decrypt')
        try:
            key = ctypes.string_at(target.data, target.length).decode('utf-16-le')
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(target.data)
        credentials = WanCredentials(key, metadata.get('workspace_id', ''), metadata.get('region', ''))
        credentials.endpoint
        return credentials
    except WanVideoError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise WanVideoError('无法读取本机已配置的百炼凭据。', code='credential_unavailable') from None


@dataclass(frozen=True)
class WanVideoRequest:
    prompt: str
    image_bytes: bytes = field(repr=False)
    image_sha256: str
    duration: int = 5
    resolution: str = '720P'
    ratio: str = 'adaptive'
    audio: bool = True
    prompt_extend: bool = False


@dataclass(frozen=True)
class WanVideoResult:
    task_id: str
    status: str
    endpoint: str
    video_url: str | None = field(default=None, repr=False)
    request_id: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None


def estimate_cost(duration: int, resolution: str) -> dict:
    """Beijing list-price estimate, not a bill, discount, or USD conversion."""
    if type(duration) is not int or not 2 <= duration <= 30 or not isinstance(resolution, str) or resolution not in RATES_CNY:
        raise WanVideoError('请选择 2–30 秒及支持的清晰度。', code='invalid_parameters')
    return {'amount': float(RATES_CNY[resolution] * duration), 'currency': 'CNY',
            'per_second': float(RATES_CNY[resolution]), 'region': 'cn-beijing',
            'model': MODEL, 'source': PRICE_REFERENCE, 'checked_at': PRICE_CHECKED_AT,
            'kind': 'list_price_estimate'}


def build_payload(request: WanVideoRequest) -> dict:
    estimate_cost(request.duration, request.resolution)
    if not isinstance(request.prompt, str) or not request.prompt.strip() or len(request.prompt) > 20000:
        raise WanVideoError('视频提示词需为 1–20000 个字符。', code='invalid_prompt')
    if not isinstance(request.ratio, str) or request.ratio not in RATIOS or type(request.audio) is not bool or type(request.prompt_extend) is not bool:
        raise WanVideoError('视频比例或声音参数无效。', code='invalid_parameters')
    data = request.image_bytes
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise WanVideoError('参考图需小于 20 MB。', code='invalid_image')
    if hashlib.sha256(data).hexdigest() != request.image_sha256:
        raise WanVideoError('参考图内容已变化，请重新审核当前图片。', code='image_changed')
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            mime = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp', 'BMP': 'image/bmp'}.get(image.format)
            if not mime or min(width, height) < 240 or max(width, height) > 8000 or max(width, height) / min(width, height) > 8:
                raise ValueError('image limits')
            if getattr(image, 'n_frames', 1) != 1 or 'A' in image.getbands() or 'transparency' in image.info:
                raise ValueError('animation/transparency')
            image.verify()
        # Decoder verification is required; format headers alone are insufficient.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise WanVideoError('参考图需为有效静态不透明图片，单边 240–8000 像素，宽高比不超过 8:1。', code='invalid_image') from None
    return {'model': MODEL,
            'input': {'prompt': request.prompt, 'media': [{'type': 'first_frame',
                     'url': f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')}]},
            'parameters': {'duration': request.duration, 'resolution': request.resolution,
                           'ratio': request.ratio, 'audio': request.audio,
                           'prompt_extend': request.prompt_extend}}


def validate_video_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (len(url) > 8192 or parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not _RESULT_HOST.fullmatch(parsed.hostname or '')
                or not parsed.path.lower().endswith('.mp4') or parsed.fragment):
            raise ValueError('url')
    except (ValueError, TypeError, AttributeError):
        raise WanVideoError('视频下载地址不在已验证的百炼结果域名内。', code='invalid_video_url') from None
    return url


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for key in ('video_count', 'duration', 'input_video_duration', 'output_video_duration', 'fps', 'SR'):
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            result[key] = number
    if isinstance(value.get('ratio'), str) and value['ratio'] in RATIOS:
        result['ratio'] = value['ratio']
    return result or None


class WanVideoClient:
    def __init__(self, credential_loader: Callable[[], WanCredentials] = load_existing_credentials,
                 *, client: httpx.Client | None = None):
        self._credential_loader = credential_loader
        self._client = client

    def _json(self, method: str, url: str, credentials: WanCredentials, payload: dict | None = None) -> dict:
        submission = method == 'POST'
        headers = {'Authorization': 'Bearer ' + credentials.api_key}
        if submission:
            headers['X-DashScope-Async'] = 'enable'
        client = self._client or httpx.Client(timeout=90, follow_redirects=False, trust_env=False)
        try:
            with client.stream(method, url, headers=headers, json=payload, follow_redirects=False) as response:
                if not 200 <= response.status_code < 300:
                    unknown = submission and (response.status_code >= 500 or response.status_code == 408 or 300 <= response.status_code < 400)
                    raise WanVideoError('百炼请求未获确认，请检查已有任务后再处理。' if unknown else '百炼未接受本次请求，请检查连接与参数。',
                                        submission_unknown=unknown, code='provider_http_error', http_status=response.status_code)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise ValueError('response too large')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
                if not isinstance(result, dict):
                    raise ValueError('response type')
                return result
        except WanVideoError:
            raise
        except (httpx.HTTPError, OSError, ValueError):
            raise WanVideoError('未取得可靠的百炼返回；请勿重复提交生成。' if submission else '暂时无法查询任务，请稍后查询同一任务。',
                                submission_unknown=submission, code='submission_unknown' if submission else 'poll_unavailable') from None
        finally:
            if self._client is None:
                client.close()

    def _result(self, value: dict, endpoint: str, *, submission: bool, expected_task_id: str | None = None) -> WanVideoResult:
        output = value.get('output')
        if not isinstance(output, dict):
            output = {}
        task_id = output.get('task_id')
        status = output.get('task_status')
        if (not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id)
                or expected_task_id is not None and task_id != expected_task_id
                or not isinstance(status, str) or status not in STATUSES):
            raise WanVideoError('百炼任务返回不完整，需核对提供方记录。', submission_unknown=submission, code='invalid_response')
        url = output.get('video_url')
        # A valid task ID remains recoverable even if the provider returns an
        # unrecognized download host. Download validates separately before I/O.
        if not isinstance(url, str) or len(url) > 8192:
            url = None
        request_id = value.get('request_id')
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            request_id = None
        return WanVideoResult(task_id, status, endpoint, url, request_id, _usage(value.get('usage')))

    def submit(self, request: WanVideoRequest, *, expected_endpoint: str | None = None) -> WanVideoResult:
        """POST exactly once. Caller must first persist intent and approval."""
        payload = build_payload(request)
        credentials = self._credential_loader()
        endpoint = credentials.endpoint
        if expected_endpoint is not None and endpoint != expected_endpoint:
            raise WanVideoError('百炼业务空间已变化，本次没有发送生成请求，请重新核对制作方案。', code='connection_changed')
        response = self._json('POST', endpoint + '/services/aigc/video-generation/video-synthesis', credentials, payload)
        return self._result(response, endpoint, submission=True)

    def poll(self, task_id: str, *, endpoint: str) -> WanVideoResult:
        """One GET for an already-persisted task; never creates or resubmits."""
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id) or not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):
            raise WanVideoError('已保存的视频任务标识无效。', code='invalid_task')
        credentials = self._credential_loader()
        if credentials.endpoint != endpoint:
            raise WanVideoError('百炼业务空间已变化，请恢复原连接后查询此任务。', code='connection_changed')
        response = self._json('GET', endpoint + '/tasks/' + task_id, credentials)
        return self._result(response, endpoint, submission=False, expected_task_id=task_id)

    def download(self, url: str, *, max_bytes: int = MAX_VIDEO_BYTES) -> bytes:
        """Download result bytes only, without API credentials or redirects.

        MP4 container sniffing is not playback validation. The caller must decode
        or probe the output before publishing it as an available video material.
        """
        validate_video_url(url)
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_VIDEO_BYTES:
            raise WanVideoError('视频下载大小限制无效。', code='invalid_download_limit')
        client = self._client or httpx.Client(timeout=90, follow_redirects=False, trust_env=False)
        try:
            with client.stream('GET', url, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise WanVideoError('视频暂时无法下载，可重试下载同一任务结果。', code='download_failed')
                length = response.headers.get('content-length')
                if length and (not length.isdigit() or int(length) > max_bytes):
                    raise WanVideoError('返回的视频超过允许大小。', code='video_too_large')
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise WanVideoError('返回的视频超过允许大小。', code='video_too_large')
                    chunks.append(chunk)
                data = b''.join(chunks)
                if len(data) < 24 or data[4:8] != b'ftyp' or int.from_bytes(data[:4], 'big') not in range(16, min(len(data), 4096) + 1):
                    raise WanVideoError('提供方返回的文件不是可识别的 MP4 视频。', code='invalid_video')
                return data
        except WanVideoError:
            raise
        except (httpx.HTTPError, OSError, ValueError):
            raise WanVideoError('视频下载中断，可重试下载同一任务结果。', code='download_failed') from None
        finally:
            if self._client is None:
                client.close()
