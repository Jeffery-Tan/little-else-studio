"""Knowledge ownership is independent of optional current business facts."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from studio.paths import APP_ROOT, PROJECT_ROOT
from test_learning_server import env


@pytest.mark.parametrize('connected', [False, True])
def test_production_default_uses_studio_knowledge_without_external_library(tmp_path, connected):
    business = tmp_path / 'business'
    business.mkdir()
    obsolete = business / 'tracks/us/creative/learning/library.json'
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text('invalid obsolete library', encoding='utf-8')
    skill = business / '.agents/skills/us-creator-video/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('obsolete skill', encoding='utf-8')
    before = {p.relative_to(business): p.read_bytes() for p in business.rglob('*') if p.is_file()}
    process_env = os.environ.copy()
    process_env.pop('STUDIO_BUSINESS_ROOT', None)
    if connected:
        process_env['STUDIO_BUSINESS_ROOT'] = str(business)
    script = '''
import sys
from pathlib import Path
from studio.paths import PROJECT_ROOT, KNOWLEDGE_ROOT
from studio.learning import LearningService, validate_method_packages
from studio.store import Store
from studio.doctor import code_fingerprint, _fingerprint_paths
from studio.config import Settings
from studio.server import build_app
from starlette.testclient import TestClient
assert KNOWLEDGE_ROOT == PROJECT_ROOT
service = LearningService(Store(Path(sys.argv[1])))
assert service.library_path == PROJECT_ROOT / 'tracks/us/creative/learning/library.json'
assert not validate_method_packages(), validate_method_packages()
assert all(p.is_relative_to(PROJECT_ROOT) for p in _fingerprint_paths())
first = code_fingerprint()
if len(sys.argv) > 2:
    (Path(sys.argv[2]) / '.agents/skills/us-creator-video/SKILL.md').write_text('external change')
    assert code_fingerprint() == first
app = build_app(Settings(data_root=Path(sys.argv[1]), base_url='http://127.0.0.1:4760'))
with TestClient(app, base_url='http://127.0.0.1:4760') as client:
    response = client.get('/api/learning')
    assert response.status_code == 200, response.text
    assert len(response.json()['cases']) == 8
    assert all('obsolete skill' not in s['text'] for s in response.json()['skills'])
'''
    args = [sys.executable, '-c', script, str(tmp_path / 'data')]
    if connected:
        args.append(str(business))
    result = subprocess.run(args, cwd=APP_ROOT, env=process_env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    after = {p.relative_to(business): p.read_bytes() for p in business.rglob('*') if p.is_file()}
    if connected:
        before[skill.relative_to(business)] = b'external change'
    assert after == before


def test_reviewed_apply_only_changes_isolated_studio_library(env, monkeypatch, tmp_path):
    from studio import paths
    client, repo, _, artifact, _ = env
    business = tmp_path / 'business'
    old = business / 'tracks/us/creative/learning/library.json'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old business library')
    monkeypatch.setattr(paths, 'BUSINESS_ROOT', business)
    current = repo / 'tracks/us/creative/learning/library.json'
    before = current.read_bytes()
    url = '/api/runs/learning-test/learning/proposals'
    response = client.post(url, json={'type': 'feedback', 'payload': {
        'stage': 'intake', 'version': 1, 'artifact_sha256': artifact.sha256,
        'quote': 'isolated ownership regression', 'scope': 'persona_dialogue', 'action': 'diagnose'}})
    assert response.status_code == 200, response.text
    proposal = response.json()
    assert current.read_bytes() == before
    applied = client.post(url + '/' + proposal['id'] + '/apply',
                          json={'expected_library_sha256': proposal['base_library_sha256']})
    assert applied.status_code == 200, applied.text
    assert current.read_bytes() != before
    assert old.read_bytes() == b'old business library'


def test_frozen_case_evidence_keeps_source_hashes():
    manifest = json.loads((PROJECT_ROOT / 'migration/knowledge-manifest.json').read_text(encoding='utf-8'))
    immutable = [f for f in manifest['files'] if f['ownership'] == 'frozen-case-evidence']
    assert len(immutable) == 9
    for item in immutable:
        assert hashlib.sha256((PROJECT_ROOT / item['path']).read_bytes()).hexdigest() == item['source_sha256'], item['path']
