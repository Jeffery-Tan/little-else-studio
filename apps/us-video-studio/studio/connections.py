"""Local role routing and Windows-user-encrypted credentials, outside Git.

No environment credentials are imported. Public configuration never decrypts keys;
job snapshots decrypt once into memory and freeze routing for that job.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import ipaddress
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from .config import ACTIVE_ROLES, ROLES, Pricing, Settings
from .agents.client import SafePreflightError
from .connection_checks import valid_deepseek_key

PROVIDERS = ('codex_cli', 'deepseek', 'mimo', 'anthropic_compat', 'openai_compat')
DEEPSEEK_BASE_URL = 'https://api.deepseek.com/v1'
MIMO_BASE_URL = 'https://api.xiaomimimo.com/v1'
_LOCK = threading.RLock()
_REPO = Path(__file__).resolve().parents[3]


class ConnectionConflict(ValueError):
    pass


class ConnectionUnavailable(SafePreflightError, ValueError):
    """Safe user-facing connection problem; never includes submitted key/URL."""


def validate_base_url(value: str) -> str:
    """Credentials go only to the exact HTTPS endpoint or explicit loopback HTTP."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError('A model connection needs a valid base URL')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        parsed.port  # Validate the port without printing the submitted URL.
        loopback = host.lower() == 'localhost'
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if (parsed.scheme not in ('https', 'http') or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(c.isspace() for c in value) or '\\' in value
                or (parsed.scheme == 'http' and not loopback)):
            raise ValueError
    except ValueError:
        raise ValueError('Use HTTPS or loopback HTTP without credentials, query, or fragment') from None
    return value.rstrip('/')


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise ValueError('Credential persistence requires Windows DPAPI; no plaintext fallback')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = crypt.CryptUnprotectData
    else:
        operation = crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('Windows credential protection failed; check the current Windows account')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def _default():
    return {'schema_version': 1, 'revision': 0,
            'connections': [{'id': 'codex', 'name': 'Codex CLI', 'provider': 'codex_cli',
                             'base_url': '', 'pricing': {}}],
            'roles': {role: {'connection': 'codex', 'model': ''} for role in ROLES}}


def _public(doc):
    return {'schema_version': 1, 'revision': doc['revision'],
            'connections': [{k: copy.deepcopy(c[k]) for k in ('id', 'name', 'provider', 'base_url', 'pricing')}
                            | {'has_key': bool(c.get('protected_key')),
                               'token_limit_parameter': c.get('token_limit_parameter', 'max_completion_tokens')}
                            for c in doc['connections']],
            'roles': copy.deepcopy(doc['roles']),
            'video_analysis': copy.deepcopy(doc.get('video_analysis')),
            'image_analysis': copy.deepcopy(doc.get('image_analysis'))}


def _role_projection(doc, roles):
    """Freeze required routes while preserving the complete saved configuration."""
    if not roles or any(role not in ROLES for role in roles):
        raise ValueError('A connection snapshot needs supported roles')
    public = _public(doc)
    public['roles'] = {role: public['roles'][role] for role in roles}
    return public


def _pricing(raw):
    if not isinstance(raw, dict):
        raise ValueError('Pricing must be keyed by exact model name')
    result = {}
    for model, card in raw.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(card, dict):
            raise ValueError('Each price needs an exact model and a rate card')
        for field_name in ('source', 'as_of'):
            if not isinstance(card.get(field_name), str):
                raise ValueError('Pricing needs source and as_of text')
        clean = {'source': card['source'], 'as_of': card['as_of']}
        for name in ('input_usd_per_mtok', 'output_usd_per_mtok'):
            rate = card.get(name)
            if rate is not None and (type(rate) not in (float, int) or not math.isfinite(rate) or rate < 0):
                raise ValueError('Price rates must be finite non-negative numbers or null')
            clean[name] = rate
        if any(clean[n] is not None for n in ('input_usd_per_mtok', 'output_usd_per_mtok')) and not clean['source'].strip():
            raise ValueError('Known prices need a source')
        result[model] = clean
    return result


class ConnectionStore:
    def __init__(self, data_root: Path | str):
        self.root = Path(data_root).resolve()
        if self.root.is_relative_to(_REPO):
            raise ValueError('Model connection credentials must remain outside the repository')
        self.path = self.root / 'connections.json'
        self._check_lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return _default()
        try:
            doc = json.loads(self.path.read_text(encoding='utf-8'))
            if doc['schema_version'] != 1 or type(doc['revision']) is not int:
                raise ValueError
            return doc
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('Local connection configuration is unreadable; preserve it for recovery') from None

    def public(self):
        with _LOCK:
            return _public(self._read())

    def settings(self, base: Settings) -> Settings:
        """Budget/UI projection without decrypting credentials or starting a job."""
        with _LOCK:
            public = _role_projection(self._read(), ACTIVE_ROLES)
        return ConnectionSnapshot(json.dumps(public, ensure_ascii=False), {}, self.root).settings(base)

    def save(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError('Connection update must be an object')
        with _LOCK:
            previous = self._read()
            if type(payload.get('expected_revision')) is not int or payload['expected_revision'] != previous['revision']:
                raise ConnectionConflict('Connection settings changed; reload before saving')
            entries, roles = payload.get('connections'), payload.get('roles')
            if not isinstance(entries, list) or not entries or len(entries) > 30 or not isinstance(roles, dict):
                raise ValueError('Provide connection entries and all role bindings')
            old = {c['id']: c for c in previous['connections']}
            clean, identifiers, warnings = [], set(), []
            for value in entries:
                if not isinstance(value, dict):
                    raise ValueError('Invalid connection entry')
                cid = value.get('id')
                if not isinstance(cid, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', cid) or cid in identifiers:
                    raise ValueError('Connection IDs must be unique lowercase names')
                identifiers.add(cid)
                provider = value.get('provider')
                if provider not in PROVIDERS:
                    raise ValueError('Unsupported connection provider')
                name = value.get('name', cid)
                if not isinstance(name, str) or not name.strip() or len(name) > 120:
                    raise ValueError('Connection name must be nonempty and at most 120 characters')
                base_url = (DEEPSEEK_BASE_URL if provider == 'deepseek' else
                            MIMO_BASE_URL if provider == 'mimo' else
                            validate_base_url(value.get('base_url')) if provider != 'codex_cli' else '')
                entry = {'id': cid, 'name': name, 'provider': provider, 'base_url': base_url,
                         'pricing': _pricing(value.get('pricing', {}))}
                if cid in old and (old[cid]['provider'], old[cid]['base_url']) != (provider, base_url):
                    # A UI round trip must not silently carry one provider's
                    # rate card to another endpoint. Old ledger rows retain it.
                    entry['pricing'] = {}
                    warnings.append(f'Connection {cid}: provider or endpoint changed; pricing cleared. Verify and save its rate card again.')
                token_parameter = ('max_tokens' if provider == 'deepseek' else
                                   'max_completion_tokens' if provider == 'mimo' else
                                   value.get('token_limit_parameter', 'max_completion_tokens'))
                if token_parameter not in ('max_tokens', 'max_completion_tokens'):
                    raise ValueError('Unsupported Chat Completions token limit parameter')
                entry['token_limit_parameter'] = token_parameter
                key = value.get('api_key')
                if key is not None and (not isinstance(key, str) or not key or len(key) > 8192 or any(c.isspace() for c in key)):
                    raise ValueError('API key must be nonempty text without whitespace')
                if provider == 'deepseek' and key and not valid_deepseek_key(key):
                    raise ValueError('DeepSeek API Key 只能包含可打印的英文字符，不能含空格或控制字符。')
                if provider == 'codex_cli' and key:
                    raise ValueError('Codex CLI uses its existing local login, not an API key')
                if key and value.get('clear_key'):
                    raise ValueError('Do not set and clear the API key together')
                if key:
                    entry['protected_key'] = base64.b64encode(_dpapi(key.encode('utf-8'))).decode('ascii')
                elif (not value.get('clear_key') and cid in old and
                      (old[cid]['provider'], old[cid]['base_url']) == (provider, base_url)):
                    if old[cid].get('protected_key'):
                        entry['protected_key'] = old[cid]['protected_key']
                clean.append(entry)
            if set(roles) != set(ROLES):
                raise ValueError('Bind exactly the five supported roles')
            providers = {c['id']: c['provider'] for c in clean}
            bindings = {}
            for role, value in roles.items():
                if not isinstance(value, dict) or value.get('connection') not in identifiers:
                    raise ValueError('Role refers to an unknown connection')
                model = value.get('model', '')
                if not isinstance(model, str) or len(model) > 200 or '\n' in model or '\r' in model:
                    raise ValueError('Model must be a single-line identifier')
                if providers[value['connection']] != 'codex_cli' and not model.strip():
                    raise ValueError('API connections require an explicit model')
                bindings[role] = {'connection': value['connection'], 'model': model.strip()}
            video = copy.deepcopy(payload.get('video_analysis', previous.get('video_analysis')))
            if video is not None:
                if (not isinstance(video, dict) or set(video) != {'connection', 'model'}
                        or not isinstance(video.get('connection'), str)
                        or providers.get(video.get('connection')) != 'mimo'
                        or video.get('model') != 'mimo-v2.6-flash'):
                    raise ValueError('视频理解请选择已添加的 MiMo 服务与 mimo-v2.6-flash；留空保留百炼。')
            image = copy.deepcopy(payload.get('image_analysis', previous.get('image_analysis')))
            if image is not None:
                if (not isinstance(image, dict) or set(image) != {'connection', 'model'}
                        or not isinstance(image.get('connection'), str)
                        or providers.get(image.get('connection')) != 'mimo'
                        or image.get('model') != 'mimo-v2.6-flash'):
                    raise ValueError('图片理解请选择已添加的 MiMo 服务与 mimo-v2.6-flash；留空保留百炼。')
            doc = {'schema_version': 1, 'revision': previous['revision'] + 1, 'connections': clean,
                   'roles': bindings, 'video_analysis': video, 'image_analysis': image}
            self.root.mkdir(parents=True, exist_ok=True)
            from .store import _atomic_write_text
            _atomic_write_text(self.path, json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
            public = _public(doc)
            public['warnings'] = warnings
            return public

    def check_deepseek(self, *, expected_revision: int, connection_id: str, api_key: str | None = None):
        """Authenticate a draft or saved DeepSeek key without saving it or generating text."""
        from .connection_checks import ConnectionCheckError, check_deepseek_models
        if not self._check_lock.acquire(blocking=False):
            raise ConnectionCheckError(409, '正在验证连接，请等待本次结果。')
        try:
            with _LOCK:
                doc = self._read()
                if type(expected_revision) is not int or expected_revision != doc['revision']:
                    raise ConnectionConflict('模型设置已在其他窗口更新，请重新打开设置后再验证。')
                if not isinstance(connection_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', connection_id):
                    raise ConnectionCheckError(400, '连接信息无效，请重新添加模型服务。')
                if api_key is not None:
                    if not valid_deepseek_key(api_key):
                        raise ConnectionCheckError(400, '请填写有效的 DeepSeek API Key，不要包含空格或控制字符。')
                    token = api_key
                else:
                    saved = next((c for c in doc['connections'] if c['id'] == connection_id), None)
                    if (not saved or saved['provider'] != 'deepseek' or
                            saved['base_url'] != DEEPSEEK_BASE_URL or not saved.get('protected_key')):
                        raise ConnectionCheckError(400, '请先输入 DeepSeek API Key；不会借用其他服务的密钥。')
                    try:
                        token = _dpapi(base64.b64decode(saved['protected_key'], validate=True), decrypt=True).decode('utf-8')
                    except (ValueError, UnicodeError):
                        raise ConnectionCheckError(400, '本机无法读取已保存的密钥，请重新填写。') from None
            result = check_deepseek_models(token)
            with _LOCK:
                if self._read()['revision'] != expected_revision:
                    raise ConnectionConflict('验证期间模型设置已改变，请重新打开设置后再验证。')
            return result
        finally:
            self._check_lock.release()

    def snapshot(self, *, roles=None, image_connection=None, image_model=None, expected_revision=None):
        """Only required roles may route/decrypt; retired roles require explicit scope."""
        with _LOCK:
            doc = self._read()
            if image_connection is not None:
                if type(expected_revision) is not int or expected_revision != doc['revision']:
                    raise ConnectionConflict('模型设置已更新，请重新打开图片分析后再确认。')
                connection = next((c for c in doc['connections'] if c['id'] == image_connection), None)
                if not connection or connection['provider'] not in {'openai_compat', 'anthropic_compat', 'mimo'}:
                    raise ConnectionUnavailable('这个连接尚未接入图片分析，请选择支持图片输入的 API 服务。')
                if not connection.get('protected_key'):
                    raise ConnectionUnavailable('请先在模型设置中保存此服务的 API Key。')
                if not isinstance(image_model, str) or not image_model.strip() or len(image_model) > 200 or any(c.isspace() for c in image_model):
                    raise ConnectionUnavailable('请填写支持图片输入的模型名称，不能包含空格。')
                # An ephemeral binding, never a change to the saved creative team.
                doc['roles']['material'] = {'connection': image_connection, 'model': image_model}
            selected_roles = (('material',) if image_connection is not None
                              else ACTIVE_ROLES if roles is None else tuple(roles))
            public = _role_projection(doc, selected_roles)
            tokens = {}
            used = {b['connection'] for b in public['roles'].values()}
            for c in doc['connections']:
                if c['id'] in used and c.get('protected_key'):
                    try:
                        tokens[c['id']] = _dpapi(base64.b64decode(c['protected_key'], validate=True), decrypt=True).decode('utf-8')
                    except (ValueError, UnicodeError):
                        raise ValueError('Stored credential could not be opened by this Windows account') from None
            return ConnectionSnapshot(json.dumps(public, ensure_ascii=False), MappingProxyType(tokens), self.root)


@dataclass(frozen=True)
class ConnectionSnapshot:
    _public_json: str
    _tokens: dict = field(repr=False, compare=False)
    _data_root: Path = field(repr=False)

    def public(self):
        return json.loads(self._public_json)

    def settings(self, base: Settings) -> Settings:
        doc = self.public()
        connections = {c['id']: c for c in doc['connections']}
        return replace(base,
            pricing=Pricing('per-connection/model pricing; no shared API rate', None, None, ''),
            model_by_role=MappingProxyType({r: b['model'] for r, b in doc['roles'].items()}),
            connection_by_role=MappingProxyType({r: b['connection'] for r, b in doc['roles'].items()}),
            provider_by_role=MappingProxyType({r: connections[b['connection']]['provider'] for r, b in doc['roles'].items()}),
            pricing_by_route=MappingProxyType({(c['id'], model): Pricing(**price)
                                              for c in connections.values() for model, price in c['pricing'].items()}))

    def client(self, check_cancel=None, *, max_tokens=16384, cancel_event=None):
        return RoutedClient(self, check_cancel=check_cancel, max_tokens=max_tokens,
                            cancel_event=cancel_event)


class RoutedClient:
    def __init__(self, snapshot: ConnectionSnapshot, *, check_cancel=None, max_tokens=16384,
                 cancel_event=None):
        self._snapshot = snapshot
        self.check_cancel = check_cancel or (lambda: None)
        self.max_tokens = max_tokens
        self.cancel_event = cancel_event

    def validate_model(self, role, model):
        binding = self._snapshot.public()['roles'].get(role)
        if binding is None:
            raise ConnectionUnavailable("Role is outside this job's frozen connections")
        if binding['model'] != model:
            raise ConnectionUnavailable('Model differs from the frozen role connection; start a new job to change it')

    def for_role(self, role: str, run_id: str):
        self.check_cancel()
        doc = self._snapshot.public()
        binding = doc['roles'].get(role)
        if binding is None:
            raise ConnectionUnavailable("Role is outside this job's frozen connections")
        connection = next(c for c in doc['connections'] if c['id'] == binding['connection'])
        provider = connection['provider']
        if provider == 'codex_cli':
            from .agents.codex_client import CodexClient
            return CodexClient(run_id=run_id, role=role, data_root=self._snapshot._data_root,
                               check_cancel=self.check_cancel)
        token = self._snapshot._tokens.get(connection['id'])
        if not token:
            raise ConnectionUnavailable('This model connection has no saved API key; configure it in the workbench')
        if provider == 'deepseek':
            from .agents.deepseek_client import DeepSeekClient
            try:
                if not valid_deepseek_key(token):
                    raise ValueError('Invalid local DeepSeek key')
                # DeepSeek's thinking-mode default has more room than the
                # generic compatibility client's 16K completion limit.
                # The writer's structured delivery completed at low effort in
                # the isolated live check; high exhausted the single-call deadline.
                # Keep other roles' existing creative deliberation setting.
                effort = 'low' if role == 'writer' else 'high'
                return DeepSeekClient(token, max_tokens=None, reasoning_effort=effort,
                                      progress_stream=role != 'writer',
                                      check_cancel=self.check_cancel,
                                      cancel_event=self.cancel_event)
            except (ValueError, TypeError):
                # This happens before the budget wrapper opens a call intent.
                # Stop a dispatched role without turning a local setup problem
                # into a retryable tool error or exposing the submitted value.
                raise ConnectionUnavailable('DeepSeek 本地连接配置无效，请检查密钥后重试。') from None
        if provider == 'mimo':
            from .agents.mimo_client import MiMoClient
            try:
                return MiMoClient(token, max_tokens=self.max_tokens, check_cancel=self.check_cancel,
                                  cancel_event=self.cancel_event)
            except (ValueError, TypeError):
                raise ConnectionUnavailable('MiMo 本地连接配置无效，请检查密钥与输出上限后重试。') from None
        try:
            base_url = validate_base_url(connection['base_url'])
        except ValueError:
            raise ConnectionUnavailable('This model connection has an invalid endpoint; correct it in the workbench') from None
        if provider == 'anthropic_compat':
            from .agents.client import AnthropicCompatClient
            return AnthropicCompatClient(base_url, token, max_tokens=self.max_tokens,
                                         check_cancel=self.check_cancel, cancel_event=self.cancel_event)
        from .agents.openai_compat import OpenAICompatClient
        return OpenAICompatClient(base_url, token, max_tokens=self.max_tokens,
                                  token_limit_parameter=connection['token_limit_parameter'],
                                  check_cancel=self.check_cancel, cancel_event=self.cancel_event)
