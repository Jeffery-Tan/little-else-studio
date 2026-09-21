"""Offline connection routing; all keys below are synthetic test strings."""
import copy
import json
import os
from dataclasses import replace

import pytest

from studio.config import Pricing, Settings, ROLES
from studio.connections import ConnectionConflict, ConnectionStore, ConnectionUnavailable, _dpapi, validate_base_url
from studio.agents.budget import BudgetedClient
from studio.agents.client import JobCancelled, ModelError, Turn, Usage
from studio.ledger import Ledger
from studio.models import BudgetExceeded
from studio.store import Store


def update_payload(store):
    public = store.public()
    return dict(expected_revision=public['revision'], connections=public['connections'], roles=public['roles'])


def api_payload(store):
    payload = update_payload(store)
    payload['connections'].append({'id': 'test-api', 'name': 'Test API', 'provider': 'openai_compat',
        'base_url': 'https://fixture.invalid/v1', 'api_key': 'synthetic-credential-only',
        'pricing': {'custom-model': {'source': 'fixture-rate', 'as_of': '2026-09-19',
                                   'input_usd_per_mtok': 1, 'output_usd_per_mtok': 2}}})
    payload['roles']['writer'] = {'connection': 'test-api', 'model': 'custom-model'}
    return payload


def test_default_is_codex_for_every_role_without_reading_environment_or_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'do-not-import-this')
    legacy = tmp_path / 'config.json'
    legacy.write_text('{"models":{"writer":"deepseek-v4-pro"}}')
    original = legacy.read_bytes()
    store = ConnectionStore(tmp_path)
    public = store.public()
    assert all(b == {'connection': 'codex', 'model': ''} for b in public['roles'].values())
    snapshot = store.snapshot()
    settings = snapshot.settings(Settings(tmp_path, 'https://legacy.invalid'))
    assert all(settings.model_for(role) == '' for role in ROLES)
    assert not (tmp_path / 'connections.json').exists()
    assert legacy.read_bytes() == original
    assert 'do-not-import-this' not in repr(snapshot)


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows DPAPI round trip')
def test_actual_dpapi_round_trip_keeps_plaintext_out_of_disk_and_public(tmp_path):
    secret = b'synthetic-dpapi-test-only'
    encrypted = _dpapi(secret)
    assert secret not in encrypted
    assert _dpapi(encrypted, decrypt=True) == secret
    store = ConnectionStore(tmp_path)
    public = store.save(api_payload(store))
    assert public['revision'] == 1
    assert public['connections'][1]['has_key'] is True
    assert 'synthetic-credential-only' not in json.dumps(public)
    assert b'synthetic-credential-only' not in store.path.read_bytes()
    snapshot = store.snapshot()
    client = snapshot.client().for_role('writer', 'test-run')
    assert client._token == 'synthetic-credential-only'
    assert 'synthetic-credential-only' not in repr(snapshot)
    assert 'protected_key' not in json.dumps(public)


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows encrypted credential persistence')
def test_update_preserves_same_endpoint_key_but_never_reuses_key_for_changed_endpoint(tmp_path):
    store = ConnectionStore(tmp_path)
    store.save(api_payload(store))
    original_snapshot = store.snapshot()
    payload = update_payload(store)
    payload['connections'][1]['name'] = 'Renamed'
    assert store.save(payload)['connections'][1]['has_key']
    payload = update_payload(store)
    payload['connections'][1]['base_url'] = 'https://other-fixture.invalid'
    saved = store.save(payload)
    changed_connection = saved['connections'][1]
    assert not changed_connection['has_key']
    assert changed_connection['pricing'] == {}
    assert saved['warnings'] and 'pricing cleared' in saved['warnings'][0]
    with pytest.raises(ValueError, match='no saved API key'):
        store.snapshot().client().for_role('writer', 'test-run')
    # Already-running job continues using the captured endpoint/key.
    assert original_snapshot.client().for_role('writer', 'test-run').base_url == 'https://fixture.invalid/v1'
    assert original_snapshot.settings(Settings(tmp_path, '')).pricing_for('writer', 'custom-model').known


def test_revision_and_models_are_frozen_and_invalid_updates_leave_file_untouched(tmp_path):
    store = ConnectionStore(tmp_path)
    stale = update_payload(store)
    snapshot = store.snapshot()
    payload = copy.deepcopy(stale)
    payload['roles']['director']['model'] = 'custom-codex-model'
    store.save(payload)
    before = store.path.read_bytes()
    with pytest.raises(ConnectionConflict):
        store.save(stale)
    bad = update_payload(store)
    bad['roles']['writer']['connection'] = 'missing'
    with pytest.raises(ValueError):
        store.save(bad)
    assert store.path.read_bytes() == before
    settings = snapshot.settings(Settings(tmp_path, 'https://legacy.invalid'))
    assert settings.model_for('director') == ''
    with pytest.raises(TypeError):
        settings.model_by_role['director'] = 'changed'


def test_role_factory_routes_independently_and_does_not_require_unassigned_keys(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    store = ConnectionStore(tmp_path)
    payload = update_payload(store)
    payload['connections'].append({'id': 'unused-api', 'name': 'Unassigned', 'provider': 'anthropic_compat',
                                  'base_url': 'https://fixture.invalid', 'pricing': {}})
    payload['roles']['reviewer']['model'] = 'reviewer-specific-model'
    store.save(payload)
    seen = []
    class FakeCodex:
        def __init__(self, **kwargs):
            seen.append(kwargs)
    monkeypatch.setitem(sys.modules, 'studio.agents.codex_client', SimpleNamespace(CodexClient=FakeCodex))
    snapshot = store.snapshot()
    router = snapshot.client()
    for role in ROLES:
        router.for_role(role, 'run-fixture')
    assert [item['role'] for item in seen] == list(ROLES)
    assert all(item['run_id'] == 'run-fixture' and item['data_root'] == tmp_path for item in seen)
    assert snapshot.settings(Settings(tmp_path, '')).model_for('reviewer') == 'reviewer-specific-model'
    assert store.settings(Settings(tmp_path, '')).pricing.source.startswith('per-connection')


@pytest.mark.parametrize('url', ['http://api.example.com', 'https://key@example.com',
    'https://example.com?api_key=secret', 'https://example.com/#secret', 'file:///path',
    'https://example.com:bad', 'https://example.com\\@evil.invalid', 'https://exa mple.com'])
def test_unsafe_urls_rejected_without_echoing_credentials(url):
    with pytest.raises(ValueError) as failure:
        validate_base_url(url)
    assert url not in str(failure.value)


@pytest.mark.parametrize('url', ['https://api.example.com/v1', 'http://127.0.0.1:8080/v1',
                                'http://[::1]:8000', 'http://localhost:1234'])
def test_custom_https_and_loopback_connections_are_accepted(url):
    assert validate_base_url(url) == url


def test_missing_key_and_model_drift_block_before_creating_a_budget_intent(tmp_path):
    connections = ConnectionStore(tmp_path)
    payload = api_payload(connections)
    del payload['connections'][1]['api_key']
    connections.save(payload)
    snapshot = connections.snapshot()
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    ledger = Ledger(store, snapshot.settings(Settings(tmp_path, 'https://legacy.invalid')))
    client = BudgetedClient(snapshot.client(), ledger, run_id='test-run', role='writer', stage='writing')
    for model in ('drifted', 'custom-model'):
        with pytest.raises(ValueError):
            client.complete(model=model, system='', messages=[], tools=[])
    assert store.read_ledger('test-run') == []


def test_pricing_is_route_specific_codex_unknown_and_frozen_through_reconciliation(tmp_path):
    connections = ConnectionStore(tmp_path)
    payload = api_payload(connections)
    del payload['connections'][1]['api_key']
    connections.save(payload)
    old = Pricing('legacy-deepseek-only', 100, 200, '2026-09-17')
    settings = connections.snapshot().settings(Settings(tmp_path, 'https://legacy.invalid', pricing=old))
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    ledger = Ledger(store, settings)
    codex = ledger.record('test-run', role='orchestrator', stage='intake', model='', input_tokens=20, output_tokens=30)
    assert codex.estimated_usd is None and codex.provider == 'codex_cli'
    assert 'DeepSeek' not in codex.pricing_source
    call = ledger.begin_call('test-run', role='writer', stage='writing', model='custom-model')
    ledger.ensure_within_budget()
    assert ledger.totals().unknown_calls == 1
    ledger.settle_call('test-run', call, input_tokens=100, output_tokens=None, usage_status='partial', ok=False)
    changed = Ledger(store, replace(settings, pricing_by_route={('test-api', 'custom-model'): old}))
    recovered = changed.reconcile('test-run', call, input_tokens=100, output_tokens=200, evidence_note='Synthetic provider receipt')
    assert recovered.estimated_usd == pytest.approx(0.0005)
    assert recovered.pricing_source == 'fixture-rate'
    assert recovered.provider == 'openai_compat' and recovered.connection_id == 'test-api'
    assert recovered.ok is False


def test_cancel_and_client_preflight_stop_before_intent(tmp_path):
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    ledger = Ledger(store, Settings(tmp_path, 'https://fixture.invalid'))
    class Cancelled:
        def check_cancel(self):
            raise JobCancelled('cancelled')
    with pytest.raises(JobCancelled):
        BudgetedClient(Cancelled(), ledger, run_id='test-run', role='writer', stage='writing').complete(
            model='test', system='', messages=[], tools=[])
    class Unsupported:
        def preflight(self):
            raise ValueError('unsupported safe runtime')
    with pytest.raises(ValueError):
        BudgetedClient(Unsupported(), ledger, run_id='test-run', role='writer', stage='writing').complete(
            model='test', system='', messages=[], tools=[])
    assert store.read_ledger('test-run') == []


def test_proven_uninvoked_setup_failure_is_failed_zero_usage_but_sent_failure_stays_unknown(tmp_path):
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    ledger = Ledger(store, Settings(tmp_path, 'https://fixture.invalid'))
    class SetupFailure:
        def complete(self, **kwargs):
            raise ModelError(0, 'local setup failed before model request', provider_invoked=False)
    with pytest.raises(ModelError):
        BudgetedClient(SetupFailure(), ledger, run_id='test-run', role='writer', stage='writing').complete(
            model='test', system='', messages=[], tools=[])
    row = ledger.entries()[0]
    assert (row.input_tokens, row.output_tokens, row.usage_status, row.ok) == (0, 0, 'known', False)
    ledger.ensure_within_budget()
    class SentFailure:
        def complete(self, **kwargs):
            raise ModelError(0, 'connection interrupted after request', provider_invoked=True)
    with pytest.raises(ModelError):
        BudgetedClient(SentFailure(), ledger, run_id='test-run', role='writer', stage='writing').complete(
            model='test', system='', messages=[], tools=[])
    assert ledger.entries()[-1].usage_status == 'unknown'
    ledger.ensure_within_budget()


@pytest.mark.parametrize('tool', ['dispatch', 'request_review'])
def test_subrole_connection_preflight_stops_orchestrator_without_retry_or_open_attempt(tmp_path, tool):
    from fakes import ScriptedModel, tool_turn
    from studio.agents.orchestrator import Orchestrator
    from studio.pipeline import Pipeline
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt('test-run', 'intake')
    pipeline.submit('test-run', 'intake', attempt.token, content='Synthetic intake', summary='fixture')
    if tool == 'request_review':
        attempt = pipeline.start_attempt('test-run', 'direction')
        pipeline.submit('test-run', 'direction', attempt.token, content='Synthetic directions', summary='fixture')
    model = ScriptedModel([tool_turn("route", "prepare_creative_context", {"intent": "edit", "format": "video"}), tool_turn('call-1', tool, {'stage': 'direction', 'brief': 'Synthetic task'})])
    class Router:
        def for_role(self, role, run_id):
            if role != 'orchestrator':
                raise ConnectionUnavailable('Role API key has not been configured')
            return model
    settings = Settings(tmp_path, 'https://fixture.invalid')
    ledger = Ledger(store, settings)
    with pytest.raises(ConnectionUnavailable):
        Orchestrator(store, pipeline, settings, Router(), ledger, 'test-run').send('Proceed with synthetic task')
    assert len(model.calls) == 2
    assert ledger.totals().calls == 2 and ledger.totals().unknown_calls == 0
    assert pipeline.stage_state('test-run', 'direction').open_attempt is None
    assert not store.list_reviews('test-run', 'direction')


def test_default_requested_model_and_actual_response_model_are_both_preserved(tmp_path):
    store = Store(tmp_path)
    store.create_run('test-run', 'Test', 'Synthetic only')
    settings = ConnectionStore(tmp_path).settings(Settings(tmp_path, 'https://legacy.invalid'))
    ledger = Ledger(store, settings)
    class Fake:
        def complete(self, **kwargs):
            assert kwargs['model'] == ''
            return Turn(text='Fixture done', stop_reason='end_turn', model='observed-codex-model', usage=Usage(2, 3))
    BudgetedClient(Fake(), ledger, run_id='test-run', role='orchestrator', stage='intake').complete(
        model='', system='', messages=[], tools=[])
    row = ledger.entries()[0]
    assert row.model == '' and row.actual_model == 'observed-codex-model'
    assert row.to_dict()['actual_model'] == 'observed-codex-model'
    assert row.estimated_usd is None
