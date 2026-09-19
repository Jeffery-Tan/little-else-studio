"""Append-only, user-reported publication outcomes; never publishes or fetches URLs."""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import math
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator, model_validator

from .models import PipelineError
from .store import sha256_text


def fingerprint(value):
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('请填写带时区的日期时间。')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('日期时间格式无效。') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('日期时间必须包含时区。')
    if parsed > datetime.now(timezone.utc):
        raise ValueError('日期时间不能在未来。')
    return parsed


class PublicationIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    title: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    published_at: str
    version: int = Field(strict=True, ge=1)
    artifact_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    differences: str = Field(min_length=1, max_length=4000)

    @field_validator('title', 'differences')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('请填写作品名称和实际作品与稿件的差异或未知项。')
        return value.strip()

    @field_validator('published_at')
    @classmethod
    def valid_time(cls, value):
        timestamp(value)
        return value

    @field_validator('url')
    @classmethod
    def public_url(cls, value):
        value = value.strip()
        if any(ord(c) < 33 for c in value) or '\\' in value:
            raise ValueError('请使用公开作品的 HTTPS 链接。')
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or '').rstrip('.').lower()
            port = parsed.port
        except ValueError as exc:
            raise ValueError('作品链接格式无效。') from exc
        if parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError('请使用不含账号密码的 HTTPS 公开作品链接。')
        if port not in (None, 443) or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
            raise ValueError('请使用公开作品链接。')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.encode('idna').decode('ascii').split('.')
            if len(labels) < 2 or labels[-1].isdigit() or any(not label or len(label) > 63 or label[0] == '-' or label[-1] == '-'
                                     or any(not (c.isalnum() or c == '-') for c in label) for label in labels):
                raise ValueError('请使用公开作品链接。')
        else:
            if not address.is_global:
                raise ValueError('请使用公开作品链接。')
        return value


Count = Annotated[int, Field(strict=True, ge=0)]
Amount = StrictFloat | StrictInt
METRICS = ('views', 'likes', 'clicks', 'orders', 'revenue_usd', 'spend_usd')


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')
    expected_latest_id: str | None
    observed_at: str
    window_start: str
    window_end: str
    traffic: Literal['organic', 'paid', 'mixed', 'unknown']
    views: Count | None = None
    likes: Count | None = None
    clicks: Count | None = None
    orders: Count | None = None
    revenue_usd: Amount | None = None
    spend_usd: Amount | None = None
    source: str = Field(min_length=1, max_length=2000)
    notes: str = Field(min_length=1, max_length=4000)

    @field_validator('source', 'notes')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('请填写数据来源与口径说明，未知项请明确记录。')
        return value.strip()

    @field_validator('revenue_usd', 'spend_usd')
    @classmethod
    def finite_money(cls, value):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError('金额必须是有限的非负数；未知金额请留空。')
        return value

    @model_validator(mode='after')
    def valid_window(self):
        observed, start, end = (timestamp(getattr(self, key)) for key in ('observed_at', 'window_start', 'window_end'))
        if not start <= end <= observed:
            raise ValueError('统计开始时间不得晚于结束时间，结束时间不得晚于观察时间。')
        if all(getattr(self, key) is None for key in METRICS):
            raise ValueError('请至少填写一项已知指标；未知值留空，不能补零。')
        return self


def _require_us(store, run_id):
    if store.read_run(run_id).market != 'US':
        raise ValueError('作品表现仅限当前 US 创作空间。')


def publications(store, run_id):
    _require_us(store, run_id)
    result = {}
    for event in store.read_events(run_id):
        if event.get('kind') not in ('publication_recorded', 'outcome_snapshot_recorded'):
            continue
        record = event.get('record')
        if not isinstance(record, dict) or event.get('record_sha256') != fingerprint(record):
            raise ValueError('作品表现记录已变化，请核对原始记录。')
        if event['kind'] == 'publication_recorded':
            if record['publication_id'] in result:
                raise ValueError('作品记录重复，请核对原始记录。')
            result[record['publication_id']] = {**record, 'recorded_at': event['at'], 'snapshots': [], 'latest': None}
        else:
            parent = result.get(record['publication_id'])
            if parent is None or record['expected_latest_id'] != (parent['latest'] or {}).get('snapshot_id'):
                raise ValueError('作品表现历史不连续，请核对原始记录。')
            snapshot = {**record, 'recorded_at': event['at']}
            parent['snapshots'].append(snapshot)
            parent['latest'] = snapshot
    return list(result.values())


def _source_valid(store, run_id, version, digest):
    return (store.verify_artifact(run_id, 'writing', version) and
            store.read_artifact(run_id, 'writing', version).sha256 == digest)


def outcome_state(store, run_id):
    items = publications(store, run_id)
    artifacts = []
    for artifact in store.list_artifacts(run_id, 'writing'):
        try:
            valid = _source_valid(store, run_id, artifact.version, artifact.sha256)
        except (ValueError, OSError, PipelineError):
            valid = False
        artifacts.append({'version': artifact.version, 'artifact_sha256': artifact.sha256,
                          'summary': artifact.summary,
                          'created_at': artifact.created_at, 'digest_algorithm': artifact.digest_algorithm,
                          'integrity_ok': valid})
    valid_sources = {(a['version'], a['artifact_sha256']): a['integrity_ok'] for a in artifacts}
    for item in items:
        item['source_integrity_ok'] = valid_sources.get((item['version'], item['artifact_sha256']), False)
    return {'artifacts': artifacts, 'publications': items}


def record_publication(store, run_id, payload):
    with store.run_lock(run_id):
        identity = fingerprint(payload.model_dump())
        for item in publications(store, run_id):
            if item['publication_id'] == payload.request_id:
                if item['request_sha256'] != identity:
                    raise ValueError('相同保存请求已用于不同作品，原记录未覆盖。')
                return {k: v for k, v in item.items() if k not in ('snapshots', 'latest')}
            if item['url'] == payload.url:
                raise ValueError('这个作品链接已经登记，请在已有作品中追加表现记录。')
        if not _source_valid(store, run_id, payload.version, payload.artifact_sha256):
            raise ValueError('所选稿件无法核对，请重新选择准确版本。')
        record = {**payload.model_dump(exclude={'request_id'}), 'publication_id': payload.request_id,
                  'request_sha256': identity, 'verification': 'user_reported'}
        store.append_event(run_id, 'publication_recorded', actor='user', record=record, record_sha256=fingerprint(record))
        item = next(p for p in publications(store, run_id) if p['publication_id'] == payload.request_id)
        return {k: v for k, v in item.items() if k not in ('snapshots', 'latest')}


def record_snapshot(store, run_id, publication_id, payload):
    with store.run_lock(run_id):
        items = publications(store, run_id)
        parent = next((p for p in items if p['publication_id'] == publication_id), None)
        if parent is None:
            raise ValueError('作品记录不存在，请刷新后重试。')
        identity = fingerprint(payload.model_dump())
        for item in parent['snapshots']:
            if item['snapshot_id'] == payload.request_id:
                if item['request_sha256'] != identity:
                    raise ValueError('相同保存请求已用于不同数据，原记录未覆盖。')
                return item
        if payload.expected_latest_id != (parent['latest'] or {}).get('snapshot_id'):
            raise ValueError('作品已有新的表现记录，请刷新后核对再保存。')
        if not _source_valid(store, run_id, parent['version'], parent['artifact_sha256']):
            raise ValueError('作品关联稿件已无法核对，请先核对原始记录。')
        if timestamp(payload.window_start) < timestamp(parent['published_at']):
            raise ValueError('统计时间范围不能早于作品发布时间。')
        record = {**payload.model_dump(exclude={'request_id'}), 'snapshot_id': payload.request_id,
                  'publication_id': publication_id, 'request_sha256': identity, 'verification': 'user_reported'}
        event = store.append_event(run_id, 'outcome_snapshot_recorded', actor='user', record=record,
                                   record_sha256=fingerprint(record))
        return {**record, 'recorded_at': event['at']}


def install_outcome_routes(app, jobs, require_run):
    class OutcomeRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()
            async def handler(request):
                try:
                    return await original(request)
                except RequestValidationError as exc:
                    # Never echo user inputs. In particular, non-JSON numeric
                    # literals parsed by Python must not break error encoding.
                    errors = [{'loc': e['loc'], 'msg': e['msg'], 'type': e['type']} for e in exc.errors()]
                    return JSONResponse(status_code=422, content={'detail': errors})
            return handler

    router = APIRouter(route_class=OutcomeRoute)

    @router.get('/api/runs/{run_id}/outcomes')
    def workspace(run_id: str):
        require_run(run_id)
        try:
            with jobs.store.run_lock(run_id):
                return outcome_state(jobs.store, run_id)
        except (ValueError, OSError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def change(run_id, action):
        require_run(run_id)
        try:
            with jobs.lock:
                if jobs.active is not None:
                    raise ValueError('请等待当前创作任务结束，再记录作品表现。')
                return action()
        except (ValueError, OSError, PipelineError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post('/api/runs/{run_id}/outcomes')
    def publish_record(run_id: str, payload: PublicationIn):
        return change(run_id, lambda: record_publication(jobs.store, run_id, payload))

    @router.post('/api/runs/{run_id}/outcomes/{publication_id}/snapshots')
    def snapshot_record(run_id: str, publication_id: str, payload: SnapshotIn):
        return change(run_id, lambda: record_snapshot(jobs.store, run_id, publication_id, payload))

    app.include_router(router)
