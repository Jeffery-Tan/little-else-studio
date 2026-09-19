"""DeepSeek setup boundaries. Synthetic credentials and offline HTTP fixtures only."""
import copy
import json
import threading
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from studio.config import Settings
from studio.connections import ConnectionConflict, ConnectionStore, ConnectionUnavailable, DEEPSEEK_BASE_URL
from studio.connection_checks import ConnectionCheckError, check_deepseek_models
from studio.server import build_app


@pytest.fixture
def fake_protection(monkeypatch):
    # Isolate the credentials tests on any OS; real DPAPI is covered separately.
    monkeypatch.setattr('studio.connections._dpapi', lambda value, **kw: value[::-1])


def payload(store, key='synthetic-key'):
    public = store.public()
    result = {'expected_revision': public['revision'], 'connections': public['connections'], 'roles': public['roles']}
    result['connections'].append({'id': 'deepseek-fixture', 'name': 'DeepSeek', 'provider': 'deepseek',
                                  'api_key': key, 'base_url': 'https://ignored.invalid',
                                  'token_limit_parameter': 'max_completion_tokens', 'pricing': {}})
    result['roles']['writer'] = {'connection': 'deepseek-fixture', 'model': 'deepseek-flash'}
    return result


def mock_models(monkeypatch, handler):
    real_client = httpx.Client
    def factory(**kwargs):
        assert kwargs['trust_env'] is False
        assert kwargs['follow_redirects'] is False
        assert kwargs['timeout'] == 15.0
        return real_client(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, 'Client', factory)


def test_provider_presets_and_frozen_role_route(tmp_path, fake_protection):
    store = ConnectionStore(tmp_path)
    public = store.save(payload(store))
    entry = public['connections'][1]
    assert entry['base_url'] == DEEPSEEK_BASE_URL
    assert entry['token_limit_parameter'] == 'max_tokens'
    assert entry['has_key'] is True
    assert 'synthetic-key' not in json.dumps(public)
    assert b'synthetic-key' not in store.path.read_bytes()
    snapshot = store.snapshot()
    from studio.agents.deepseek_client import DeepSeekClient
    assert isinstance(snapshot.client().for_role('writer', 'fixture-run'), DeepSeekClient)
    assert snapshot.settings(Settings(tmp_path, '')).provider_by_role['writer'] == 'deepseek'
    assert public['roles']['orchestrator'] == {'connection': 'codex', 'model': ''}
    revised = {'expected_revision': 1, 'connections': public['connections'], 'roles': copy.deepcopy(public['roles'])}
    revised['roles']['writer']['model'] = 'deepseek-v4-pro'
    store.save(revised)
    assert snapshot.public()['roles']['writer']['model'] == 'deepseek-flash'


def test_new_draft_check_authenticates_without_saving_or_generating(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path)
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'https://api.deepseek.com/v1/models'
        assert request.method == 'GET' and not request.content
        assert request.headers['authorization'] == 'Bearer synthetic-key'
        return httpx.Response(200, json={'data': [{'id': 'deepseek-flash'}, {'id': 'deepseek-v4-pro'}]})
    mock_models(monkeypatch, handler)
    result = store.check_deepseek(expected_revision=0, connection_id='draft', api_key='synthetic-key')
    assert result['status'] == 'verified' and result['scope'] == 'credentials_and_models'
    assert result['model_ids'] == ['deepseek-flash', 'deepseek-v4-pro'] and result['checked_at']
    assert len(requests) == 1
    assert not store.path.exists() and not list(tmp_path.glob('runs/*'))
    assert 'synthetic-key' not in json.dumps(result)


def test_saved_key_check_only_uses_exact_deepseek_connection(tmp_path, fake_protection, monkeypatch):
    store = ConnectionStore(tmp_path)
    store.save(payload(store))
    before = store.path.read_bytes()
    seen = []
    monkeypatch.setattr('studio.connection_checks.check_deepseek_models', lambda token: seen.append(token) or {'status': 'verified'})
    store.check_deepseek(expected_revision=1, connection_id='deepseek-fixture')
    assert seen == ['synthetic-key'] and store.path.read_bytes() == before
    with pytest.raises(ConnectionCheckError, match='先输入'):
        store.check_deepseek(expected_revision=1, connection_id='codex')
    with pytest.raises(ConnectionConflict):
        store.check_deepseek(expected_revision=0, connection_id='deepseek-fixture')
    assert seen == ['synthetic-key']


@pytest.mark.parametrize('status,expected', [(401,400),(403,400),(402,400),(429,400),(302,502),(500,502)])
def test_check_failures_are_safe_single_attempts(monkeypatch, status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='echo synthetic-secret prompt-private', headers={'location':'https://unapproved.invalid'})
    mock_models(monkeypatch, handler)
    with pytest.raises(ConnectionCheckError) as failure:
        check_deepseek_models('synthetic-secret')
    assert failure.value.status == expected and len(calls) == 1
    assert 'synthetic-secret' not in str(failure.value) and 'prompt-private' not in str(failure.value)


@pytest.mark.parametrize('response', [{'data':[]}, {'data':[{'id':'<script>bad</script>'}]}, {'data':[None]}, {}, []])
def test_malformed_or_empty_model_list_never_reports_verified(monkeypatch, response):
    mock_models(monkeypatch, lambda request: httpx.Response(200, json=response))
    with pytest.raises(ConnectionCheckError) as failure:
        check_deepseek_models('synthetic-key')
    assert failure.value.status == 502


def test_timeout_explanation_and_retry_releases_lock(tmp_path, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout('contains synthetic-secret')
    mock_models(monkeypatch, timeout)
    store = ConnectionStore(tmp_path)
    for _ in range(2):
        with pytest.raises(ConnectionCheckError) as failure:
            store.check_deepseek(expected_revision=0, connection_id='draft', api_key='synthetic-key')
        assert failure.value.status == 504
        assert 'synthetic-secret' not in str(failure.value)


def test_revision_changed_during_check_invalidates_success(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path)
    def checked(token):
        public = store.public()
        store.save({'expected_revision':0,'connections':public['connections'],'roles':public['roles']})
        return {'status':'verified'}
    monkeypatch.setattr('studio.connection_checks.check_deepseek_models', checked)
    with pytest.raises(ConnectionConflict, match='验证期间'):
        store.check_deepseek(expected_revision=0, connection_id='draft', api_key='synthetic-key')


def test_concurrent_check_is_rejected_without_second_provider_request(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def checked(token):
        entered.set()
        assert release.wait(5)
        return {'status':'verified'}
    monkeypatch.setattr('studio.connection_checks.check_deepseek_models', checked)
    thread = threading.Thread(target=lambda: store.check_deepseek(expected_revision=0, connection_id='draft', api_key='synthetic-key'))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ConnectionCheckError) as failure:
            store.check_deepseek(expected_revision=0, connection_id='draft', api_key='synthetic-key')
        assert failure.value.status == 409
    finally:
        release.set()
        thread.join(5)


def test_http_boundary_same_origin_redacted_validation_and_no_configuration_mutation(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr('studio.connection_checks.check_deepseek_models', lambda token: seen.append(token) or {
        'status':'verified','scope':'credentials_and_models','model_ids':['deepseek-flash'],'checked_at':'fixture'})
    app = build_app(Settings(tmp_path, ''), client_factory=lambda: pytest.fail('No model allowed'))
    with TestClient(app) as browser:
        body = {'expected_revision':0,'connection_id':'draft','api_key':'synthetic-key'}
        response = browser.post('/api/connections/deepseek/check', json=body)
        assert response.status_code == 200 and response.json()['scope'] == 'credentials_and_models'
        assert not (tmp_path/'connections.json').exists()
        assert browser.post('/api/connections/deepseek/check', json=body, headers={'origin':'https://foreign.invalid'}).status_code == 403
        bad = browser.post('/api/connections/deepseek/check', json={**body, 'api_key':['private-invalid-secret']})
        assert bad.status_code == 422 and 'private-invalid-secret' not in bad.text
        assert browser.post('/api/connections/deepseek/check', json={**body,'expected_revision':1}).status_code == 409
        assert seen == ['synthetic-key']


def test_routed_orchestrator_two_jobs_preserves_only_current_private_protocol(tmp_path, fake_protection, monkeypatch):
    from studio.agents.orchestrator import Orchestrator
    from studio.pipeline import Pipeline
    from studio.store import Store
    from studio.ledger import Ledger
    connections = ConnectionStore(tmp_path)
    draft = payload(connections)
    draft['roles']['orchestrator'] = {'connection':'deepseek-fixture','model':'deepseek-flash'}
    connections.save(draft)
    snapshot = connections.snapshot()
    settings = snapshot.settings(Settings(tmp_path, ''))
    store = Store(tmp_path)
    store.create_run('deepseek-integration', 'Offline fixture', '仅检查工具往返和跨轮聊天。')
    real_client = httpx.Client
    seen = []
    def handler(request):
        incoming = json.loads(request.content)
        seen.append(incoming)
        if len(seen) == 1:
            msg = {'content':'先查看项目状态。','reasoning_content':'private-first',
                   'tool_calls':[{'id':'call-state','type':'function','function':{'name':'read_project_state','arguments':'{}'}}]}
            reason = 'tool_calls'
        else:
            msg = {'content':'资料已保存，请确认下一步。','reasoning_content':'private-final'}
            reason = 'stop'
        return httpx.Response(200, json={'model':'deepseek-flash','choices':[{'finish_reason':reason,'message':msg}],
                                         'usage':{'prompt_tokens':50,'completion_tokens':20}})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    events = []
    def run(message):
        return Orchestrator(store, Pipeline(store), settings, snapshot.client(), Ledger(store, settings),
                            'deepseek-integration', on_event=events.append).send(message)
    first = run('先检查状态，不制作素材。')
    second = run('延续上次讨论。')
    assert first.stopped_because == second.stopped_because == 'done'
    assert not first.failed_tools
    assert len(seen) == 3
    assert any(m.get('reasoning_content') == 'private-first' for m in seen[1]['messages'])
    assert all(m['role'] != 'assistant' for m in seen[2]['messages'])
    assert any('saved_assistant_text' in str(m) and '资料已保存' in str(m) for m in seen[2]['messages'])
    assert 'private-first' not in json.dumps(events) and 'private-final' not in json.dumps(store.read_chat('deepseek-integration'))
    rows = Ledger(store, settings).entries('deepseek-integration')
    assert len(rows) == 3 and all(row.provider == 'deepseek' for row in rows)
    assert all(row.input_tokens == 50 and row.output_tokens == 20 for row in rows)


@pytest.mark.parametrize('character', ['\x00', '\x01', '\x1f', '\x7f', ' ', '\n', '密'])
def test_invalid_key_is_rejected_consistently_before_save_check_or_http(tmp_path, monkeypatch, character):
    token = 'synthetic' + character + 'key'
    monkeypatch.setattr(httpx, 'Client', lambda **_: pytest.fail('Invalid key must never create HTTP client'))
    monkeypatch.setattr('studio.connections._dpapi', lambda *_, **kw: pytest.fail('Invalid key must never be encrypted'))
    store = ConnectionStore(tmp_path)
    with pytest.raises(ValueError) as saved:
        store.save(payload(store, token))
    with pytest.raises(ConnectionCheckError) as checked:
        store.check_deepseek(expected_revision=0, connection_id='draft', api_key=token)
    with pytest.raises(ConnectionCheckError) as direct:
        check_deepseek_models(token)
    assert checked.value.status == direct.value.status == 400
    assert all(token not in str(error.value) for error in (saved, checked, direct))
    assert not store.path.exists() and store.public()['revision'] == 0


@pytest.mark.parametrize('invalid_setting', ['legacy_key', 'output_limit'])
def test_local_deepseek_construction_failure_stops_subrole_without_retry_or_ledger_intent(
        tmp_path, fake_protection, monkeypatch, invalid_setting):
    from fakes import ScriptedModel, tool_turn
    from studio.agents.budget import BudgetedClient
    from studio.agents.client import run_agent_loop
    from studio.ledger import Ledger
    from studio.store import Store

    monkeypatch.setattr(httpx, 'Client', lambda **_: pytest.fail('Local setup failure must never create HTTP client'))
    connections = ConnectionStore(tmp_path)
    connections.save(payload(connections))
    snapshot = connections.snapshot()
    if invalid_setting == 'legacy_key':
        # Simulate an already-loaded legacy credential; do not weaken save to
        # produce it. The snapshot remains isolated from the saved configuration.
        snapshot = replace(snapshot, _tokens={'deepseek-fixture': 'synthetic\x00key'})
    router = snapshot.client(max_tokens=0 if invalid_setting == 'output_limit' else 16384)
    store = Store(tmp_path)
    store.create_run('preflight-test', 'Offline', 'Synthetic data only')
    ledger = Ledger(store, snapshot.settings(Settings(tmp_path, '')))
    child = BudgetedClient(router, ledger, run_id='preflight-test', role='writer', stage='writing')
    main = ScriptedModel([tool_turn('dispatch-1', 'dispatch', {'stage': 'writing'})])

    def dispatch(*_):
        return child.complete(model='deepseek-flash', system='writer scope',
                              messages=[{'role': 'user', 'content': 'Synthetic task'}], tools=[])

    with pytest.raises(ConnectionUnavailable) as failure:
        run_agent_loop(main, model='synthetic-main', system='main scope',
                       messages=[{'role': 'user', 'content': 'Synthetic dispatch'}],
                       tools=[], execute_tool=dispatch)
    assert len(main.calls) == 1
    assert ledger.entries('preflight-test') == []
    assert 'synthetic' not in str(failure.value)
    assert connections.snapshot()._tokens['deepseek-fixture'] == 'synthetic-key'
