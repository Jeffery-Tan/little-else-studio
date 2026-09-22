"""One-attempt Beijing Omni SSE transport using the existing Wan credentials."""
from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace

import httpx

from .client import ModelError, SafePreflightError, Turn, Usage
from ..config import Pricing
from . import wan_video

MODEL = 'qwen3.8-omni-flash'
CONNECTION = 'bailian_omni'
PRICE = Pricing('https://help.aliyun.com/zh/model-studio/model-pricing (Beijing; cache discounts excluded)',
                None, None, '2026-09-21', .8, 2.7)
MAX_INLINE_BYTES = 7_000_000  # Encoded video stays below the provider's 10 MB limit.


def connection_info():
    """No secret reads, decryption, network, or configuration writes."""
    if not wan_video.existing_credentials_available():
        return None
    try:
        base = wan_video.existing_endpoint().removesuffix('/api/v1') + '/compatible-mode/v1'
    except wan_video.WanVideoError:
        return None
    return {'id': CONNECTION, 'name': '阿里百炼北京 · 与 Wan 共用连接', 'provider': 'bailian_omni',
            'base_url': base, 'has_key': True, 'suggested_model': MODEL, 'native_video': True,
            'pricing': {MODEL: asdict(PRICE)}}


def freeze(settings, expected_endpoint, check_cancel=lambda: None):
    info = connection_info()
    if info is None or not expected_endpoint or info['base_url'] != expected_endpoint:
        raise SafePreflightError('百炼北京连接已变化或尚未配置，请重新打开素材分析。')
    try:
        credential = wan_video.load_existing_credentials()
        base = credential.endpoint.removesuffix('/api/v1') + '/compatible-mode/v1'
    except wan_video.WanVideoError:
        raise SafePreflightError('无法读取现有百炼北京凭据，请检查 Wan 的本机连接配置。') from None
    if base != expected_endpoint:
        raise SafePreflightError('百炼业务空间已变化，请重新确认分析服务。')
    frozen = replace(settings,
        model_by_role={**settings.model_by_role, 'material': MODEL},
        connection_by_role={**settings.connection_by_role, 'material': CONNECTION},
        provider_by_role={**settings.provider_by_role, 'material': 'bailian_omni'},
        pricing_by_route={**settings.pricing_by_route, (CONNECTION, MODEL): PRICE})
    return frozen, OmniClient(base, credential.api_key, check_cancel=check_cancel), info


class OmniClient:
    def __init__(self, base_url, token, *, check_cancel=lambda: None):
        self.base_url, self._token = base_url, token
        self.check_cancel = check_cancel

    def complete(self, *, model, system, messages, tools):
        # This adapter accepts only the explicit observation request shape.
        try:
            if model != MODEL or tools or len(messages) != 1 or messages[0]['role'] != 'user':
                raise ValueError()
            parts = []
            video_count = image_count = image_bytes = 0
            for block in messages[0]['content']:
                if block['type'] == 'text':
                    parts.append({'type': 'text', 'text': block['text']})
                elif block['type'] == 'video':
                    source = block['source']
                    raw = base64.b64decode(source['data'], validate=True)
                    if source['type'] != 'base64' or not 0 < len(raw) <= MAX_INLINE_BYTES:
                        raise ValueError()
                    parts.append({'type': 'video_url', 'video_url': {'url': 'data:;base64,' + source['data']}, 'fps': 2})
                    video_count += 1
                elif block['type'] == 'image':
                    source = block['source']
                    raw = base64.b64decode(source['data'], validate=True)
                    mime = source['media_type']
                    if source['type'] != 'base64' or mime not in ('image/png', 'image/jpeg', 'image/webp') or not raw:
                        raise ValueError()
                    image_count += 1
                    image_bytes += len(raw)
                    parts.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,' + source['data']}})
                else:
                    raise ValueError()
            if not ((video_count == 1 and image_count == 0) or
                    (video_count == 0 and 1 <= image_count <= 12 and image_bytes <= MAX_INLINE_BYTES)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ModelError(0, 'invalid local Omni input; not sent', provider_invoked=False) from None
        payload = {'model': MODEL, 'messages': [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': parts}], 'modalities': ['text'],
                   'stream': True, 'stream_options': {'include_usage': True},
                   'reasoning_effort': 'low', 'max_tokens': 4096}
        self.check_cancel()
        turn = Turn(model=MODEL, usage=Usage(None, None))
        done, count, finished = False, 0, False
        try:
            with httpx.Client(timeout=180, follow_redirects=False, trust_env=False) as client:
                with client.stream('POST', self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self._token}, json=payload) as response:
                    if response.status_code != 200:
                        raise ModelError(response.status_code, 'Omni request rejected; usage unconfirmed')
                    for line in response.iter_lines():
                        self.check_cancel()
                        count += len(line)
                        if count > 2_000_000:
                            raise ValueError()
                        if not line.startswith('data:'):
                            continue
                        data = line[5:].strip()
                        if data == '[DONE]':
                            done = True
                            break
                        item = json.loads(data)
                        if not isinstance(item, dict) or item.get('error'):
                            raise ValueError()
                        if item.get('usage') is not None:
                            usage = item['usage']
                            for key, attr in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
                                value = usage.get(key)
                                if value is not None and (type(value) is not int or value < 0):
                                    raise ValueError()
                                setattr(turn.usage, attr, value)
                        if isinstance(item.get('model'), str):
                            turn.model = item['model']
                        choices = item.get('choices', [])
                        if not isinstance(choices, list) or len(choices) > 1:
                            raise ValueError()
                        for choice in choices:
                            delta = choice.get('delta', {})
                            if delta.get('tool_calls') or (finished and (delta.get('content') or delta.get('reasoning_content'))):
                                raise ValueError()
                            for key, attr in [('content', 'text'), ('reasoning_content', 'thinking')]:
                                value = delta.get(key) or ''
                                if not isinstance(value, str):
                                    raise ValueError()
                                setattr(turn, attr, getattr(turn, attr) + value)
                            reason = choice.get('finish_reason')
                            if reason is not None:
                                if reason not in ('stop', 'length') or finished:
                                    raise ValueError()
                                turn.stop_reason = 'end_turn' if reason == 'stop' else 'max_tokens'
                                finished = True
            if not done or not finished:
                raise ValueError()
        except httpx.TransportError:
            raise ModelError(0, 'Omni transport interrupted; usage unconfirmed', usage=turn.usage) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ModelError(200, 'invalid or incomplete Omni stream; no automatic retry', usage=turn.usage) from None
        turn.raw_blocks = [{'type': 'text', 'text': turn.text}]
        return turn
