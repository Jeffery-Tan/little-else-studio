import json
import shutil

import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn
from studio.learning import LearningService
from studio.learning_assets import REPOSITORY
from studio.server import build_app
from studio.store import Store
from test_server import settings_for


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / 'repo'
    for name in ('.agents/skills', 'apps/us-video-studio/docs', 'tracks/us/creative/learning', 'tracks/us/creative/photo-copy', 'tracks/us/creative/comparison'):
        if (REPOSITORY / name).exists():
            shutil.copytree(REPOSITORY / name, repo / name)
    (repo / 'tracks/us/docs').mkdir(parents=True)
    shutil.copyfile(REPOSITORY / 'tracks/us/docs/ai-video-recovery-policy.md', repo / 'tracks/us/docs/ai-video-recovery-policy.md')
    settings = settings_for(tmp_path / 'data')
    app = build_app(settings, client_factory=lambda: ScriptedModel([final_turn('离线回复')]),
                    learning_repository_root=repo, learning_media_root=tmp_path / 'media')
    with TestClient(app) as client:
        client.post('/api/runs', json={'run_id': 'learning-test', 'title': '隔离验收', 'brief': '不调用模型'})
        store = Store(settings.data_root)
        artifact = store.write_artifact('learning-test', 'intake', 1, '仅测试的稿件',
                                        summary='测试', author_role='orchestrator', attempt='test')
        yield client, repo, store, artifact, app


def test_catalog_full_skill_and_context_snapshot(env):
    client, repo, store, _, _ = env
    catalog = client.get('/api/learning').json()
    assert len(catalog['skills']) == 3
    assert len(catalog['cases']) == 8
    assert len(catalog['methods']) == 6
    assert all(s['text'] and len(s['sha256']) == 64 for s in catalog['skills'])
    for doc in catalog['documents']:
        response = client.get('/api/learning/documents/' + doc['id'])
        assert response.status_code == 200, (doc, response.text)
    assert client.get('/api/learning/documents/secrets').status_code == 404
    assert client.get('/api/learning/archive/secret').status_code == 404
    assert client.get('/api/runs/learning-test/creative-packages').json() == {'packages': []}
    context = LearningService(store, repo).context('learning-test', 'writer', tags=['lunch'])
    response = client.get('/api/runs/learning-test/learning')
    assert response.status_code == 200, response.text
    assert response.json()['contexts'][0]['snapshot']['text'] == context['text']


def test_feedback_preview_apply_cas_and_same_run_context(env):
    client, repo, store, artifact, app = env
    path = repo / 'tracks/us/creative/learning/library.json'
    before = path.read_bytes()
    base = '/api/runs/learning-test/learning/proposals'
    payload = {'type': 'feedback', 'payload': {'stage': 'intake', 'version': 1, 'artifact_sha256': artifact.sha256,
                                            'quote': '只改开场语气，其他先保留', 'scope': 'persona_dialogue', 'action': 'diagnose'}}
    response = client.post(base, json=payload)
    assert response.status_code == 200, response.text
    proposal = response.json()
    assert path.read_bytes() == before
    assert proposal['status'] == 'pending' and proposal['diff']
    url = base + '/' + proposal['id'] + '/apply'
    assert client.post(url, json={'expected_library_sha256': '0'*64}).status_code == 409
    assert path.read_bytes() == before
    app.state.jobs.active = ('different-run', 'job-test')
    assert client.post(url, json={'expected_library_sha256': proposal['base_library_sha256']}).status_code == 409
    app.state.jobs.active = None
    response = client.post(url, json={'expected_library_sha256': proposal['base_library_sha256']})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'applied'
    after = path.read_bytes()
    assert after != before
    assert client.post(url, json={'expected_library_sha256': proposal['base_library_sha256']}).status_code == 200
    assert path.read_bytes() == after
    context = LearningService(store, repo).context('learning-test', 'writer')
    assert context['run_feedback'][0]['quote'] == payload['payload']['quote']
    assert context['run_feedback'][0]['scope'] == 'persona_dialogue'
    assert not any(c.startswith('studio-') for c in context['case_ids'])
    assert not LearningService(store, repo).context('learning-test', 'reviewer')['run_feedback']


def test_cross_origin_and_stale_artifact_cannot_propose(env):
    client, _, _, artifact, _ = env
    url = '/api/runs/learning-test/learning/proposals'
    payload = {'type': 'feedback', 'payload': {'stage': 'intake', 'version': 1,
                                            'artifact_sha256': artifact.sha256, 'quote': 'test'}}
    assert client.post(url, json=payload, headers={'origin': 'https://evil.example'}).status_code == 403
    payload['payload']['artifact_sha256'] = '0'*64
    assert client.post(url, json=payload).status_code == 409


def test_method_upgrade_http_requires_reviewed_fingerprints_and_idle_job(env):
    client, repo, store, _, app = env
    learning = LearningService(store, repo)
    original = learning.context('learning-test', 'writer')
    skill = repo / '.agents/skills/us-creator-video/SKILL.md'
    skill.write_text(skill.read_text(encoding='utf-8-sig') + '\nHTTP reviewed change\n', encoding='utf-8')
    status = client.get('/api/runs/learning-test/learning').json()['pin_status']
    assert status['methods_refresh_available']
    assert any('HTTP reviewed change' in item['diff'] for item in status['method_changes'])
    payload = {'expected_run_sha256': status['run_library_sha256'],
               'expected_current_sha256': status['current_library_sha256'],
               'expected_run_methods_sha256': status['run_methods_sha256'],
               'expected_current_methods_sha256': status['current_methods_sha256'],
               'request_id': 'reviewed-method-upgrade'}
    url = '/api/runs/learning-test/learning/refresh'
    assert client.post(url, json={**payload, 'expected_current_methods_sha256': '0' * 64}).status_code == 409
    assert learning.context('learning-test', 'writer')['methods_sha256'] == original['methods_sha256']
    app.state.jobs.active = ('learning-test', 'busy')
    assert client.post(url, json=payload).status_code == 409
    app.state.jobs.active = None
    response = client.post(url, json=payload)
    assert response.status_code == 200, response.text
    assert learning.context('learning-test', 'writer')['methods_sha256'] == status['current_methods_sha256']
    assert client.post(url, json=payload).status_code == 200


def test_method_result_http_is_readonly_and_rejects_unknown_comparison(env):
    client, _, store, _, _ = env
    before = store.read_events('learning-test')
    assert client.get('/api/runs/learning-test/learning/method-results').json() == {'results': []}
    result = client.get('/api/runs/learning-test/learning/method-comparison?baseline=missing&candidate=other')
    assert result.status_code == 409
    assert client.get('/api/runs/missing/learning/method-results').status_code == 404
    assert store.read_events('learning-test') == before
