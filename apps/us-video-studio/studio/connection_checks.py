"""Explicit credential checks; no generation, retries, configuration writes or prompts."""
from datetime import datetime, timezone
import re

import httpx


class ConnectionCheckError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def valid_deepseek_key(token: str) -> bool:
    """One local header boundary for saving, checking and opening a connection."""
    return (isinstance(token, str) and 0 < len(token) <= 8192 and token.isascii()
            and all(33 <= ord(character) < 127 for character in token))


def check_deepseek_models(token: str) -> dict:
    # Fixed official endpoint: a provider switch can never forward a saved key
    # to a caller-supplied URL. Neither error bodies nor request headers are echoed.
    if not valid_deepseek_key(token):
        raise ConnectionCheckError(400, '请填写有效的 DeepSeek API Key。')
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False) as client:
            response = client.get('https://api.deepseek.com/v1/models',
                                  headers={'Authorization': f'Bearer {token}'})
            if response.status_code in (401, 403):
                raise ConnectionCheckError(400, 'DeepSeek 未接受这个 Key，请检查是否填写正确或已失效。')
            if response.status_code == 402:
                raise ConnectionCheckError(400, 'DeepSeek 返回余额不足，请在提供方账户检查余额。')
            if response.status_code == 429:
                raise ConnectionCheckError(400, 'DeepSeek 请求过于频繁，请稍后手动重试。')
            if response.status_code != 200:
                raise ConnectionCheckError(502, 'DeepSeek 暂时无法完成验证，请稍后重试。')
            try:
                data = response.json()
                models = data['data']
                if not isinstance(models, list) or not models or len(models) > 200:
                    raise ValueError
                ids = []
                for model in models:
                    name = model['id']
                    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', name):
                        raise ValueError
                    if name not in ids:
                        ids.append(name)
            except (ValueError, TypeError, KeyError):
                raise ConnectionCheckError(502, '密钥请求已返回，但模型列表无法识别，请稍后重试。') from None
    except httpx.TimeoutException:
        raise ConnectionCheckError(504, '连接 DeepSeek 超时，请检查网络后重试。') from None
    except httpx.TransportError:
        raise ConnectionCheckError(502, '无法连接 DeepSeek，请检查网络后重试。') from None
    return {'status': 'verified', 'scope': 'credentials_and_models', 'model_ids': ids,
            'checked_at': datetime.now(timezone.utc).isoformat()}
