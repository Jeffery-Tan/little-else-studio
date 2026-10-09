import io
import json
import zipfile
import pytest

from studio.agents.client import Turn, Usage
from test_production import scene, draft, action, wait, workspace, RUN


def generated(scene):
    item = draft(scene, kind='image').json()
    assert wait(scene, action(scene, item, 'generate'))['status'] == 'succeeded'
    return next(n['target'] for n in scene[0].get(f'/api/runs/{RUN}/canvas').json()['nodes'] if n['kind'] == 'media')


def test_inspection_uses_actual_pixels_without_approving_or_regenerating(scene, monkeypatch):
    target = generated(scene)
    http, store, _, model, images, videos, _ = scene
    prior_approvals = store.list_approvals(RUN)
    def inspect(**kwargs):
        model.calls.append(kwargs)
        assert kwargs['tools'] == []
        blocks = kwargs['messages'][0]['content']
        assert sum(row['type'] == 'image' for row in blocks) == 2
        return Turn(text=json.dumps({'findings': [{'area': key, 'status': 'unknown',
            'evidence': 'Synthetic observation only'} for key in
            ['product', 'character', 'scene', 'copy', 'quality', 'requirements']],
            'limitations': ['Offline fixture; no visual quality claim']}), usage=Usage(20, 10),
            stop_reason='end_turn', model='fixture-observer')
    monkeypatch.setattr(model, 'complete', inspect)
    url = f'/api/runs/{RUN}/result-inspections'
    payload = {'request_id': 'inspect-one', 'target': target,
               'expected_revision': http.get(url).json()['revision'], 'consent': True}
    done = wait(scene, http.post(url, json=payload))
    assert done['status'] == 'succeeded', done
    assert wait(scene, http.post(url, json=payload))['job_id'] == done['job_id']
    report = http.get(url).json()['reports'][0]
    assert report['status'] == 'unknown' and report['human_approval'] is False
    assert report['target'] == target and report['input'] and report['references']
    assert len(model.calls) == 1 and len(images.calls) == 1 and not videos.calls
    assert store.list_approvals(RUN) == prior_approvals
    graph = http.get(f'/api/runs/{RUN}/canvas').json()
    assert next(n for n in graph['nodes'] if n['kind'] == 'media')['data']['inspection']['inspection_id'] == report['inspection_id']


def test_exact_export_preserves_bytes_and_rejects_tampering(scene):
    target = generated(scene)
    http, store, *_ = scene
    url = f'/api/runs/{RUN}/objects/export'
    payload = {'request_id': 'export-one', 'targets': [target]}
    response = http.post(url, json=payload)
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        item = manifest['objects'][0]
        assert item['target'] == target and item['inspection'] is None
        assert item['file_sha256'] == target['output_sha256']
    # A lost response can be recovered after unrelated metadata changes without
    # silently changing the archive originally bound to this request.
    assert http.post(f'/api/runs/{RUN}/annotations', json={'request_id': 'after-export',
        'target': target, 'text': 'A later note, outside the original export.'}).status_code == 200
    assert http.post(url, json=payload).content == response.content
    assert http.post(url, json={**payload, 'targets': []}).status_code == 422
    material = next(m for m in workspace(scene)['materials'] if m['sha256'] == target['output_sha256'] and m.get('origin') == 'generated')
    (store.run_dir(RUN) / 'materials' / material['name']).write_bytes(b'tampered')
    assert http.post(url, json=payload).status_code == 409
    assert len(scene[4].calls) == 1 and not scene[3].calls


def test_inspection_rejects_stale_identity_before_model(scene):
    target = generated(scene)
    url = f'/api/runs/{RUN}/result-inspections'
    payload = {'request_id': 'inspect-stale', 'target': {**target, 'output_sha256': '0' * 64},
               'expected_revision': scene[0].get(url).json()['revision'], 'consent': True}
    assert scene[0].post(url, json=payload).status_code == 409
    assert not scene[3].calls


@pytest.mark.parametrize('kind,provider', [('image', 'mimo'), ('video', 'bailian_omni')])
def test_single_media_routes_cannot_claim_reference_comparison(scene, monkeypatch, kind, provider):
    from fastapi import FastAPI
    from types import SimpleNamespace
    from local_http import TestClient
    from studio.result_inspection import install_routes
    from studio import image_understanding, video_understanding
    from test_production import approve
    http, store, pipeline, model, images, videos, settings = scene
    item = draft(scene, kind=kind).json()
    if kind == 'video':
        assert approve(scene).status_code == 200
    assert wait(scene, action(scene, item, 'generate'))['status'] == 'succeeded'
    target = next(n['target'] for n in http.get(f'/api/runs/{RUN}/canvas').json()['nodes'] if n['kind'] == 'media')
    info = {'provider': provider, 'suggested_model': 'fixture-observer', 'base_url': 'https://example.invalid'}
    route = video_understanding if kind == 'video' else image_understanding
    monkeypatch.setattr(route, 'connection_info', lambda *_: info)
    monkeypatch.setattr(route, 'freeze', lambda *args, **kwargs: (settings, model, info))
    monkeypatch.setattr(video_understanding, 'prepare_video', lambda raw, *_: (raw, {
        'input_media_type': 'video/mp4', 'video_fps': 2, 'media_resolution': 'default',
        'limitation': 'Synthetic transport packet only'}))
    def observe(**kwargs):
        model.calls.append(kwargs)
        blocks = kwargs['messages'][0]['content']
        assert [b['type'] for b in blocks if b['type'] != 'text'] == [kind]
        assert any('未发送参考图像素' in b.get('text', '') for b in blocks)
        areas = ['product', 'character', 'scene', 'copy', 'quality', 'requirements']
        if kind == 'video':
            areas.append('audio')
        return Turn(text=json.dumps({'findings': [{'area': area, 'status': 'observed_ok',
            'evidence': 'Synthetic response, cannot prove reference match'} for area in areas],
            'limitations': ['Offline fixture']}), usage=Usage(1, 1), stop_reason='end_turn', model='fixture')
    monkeypatch.setattr(model, 'complete', observe)
    app = FastAPI()
    install_routes(app, store, pipeline, http.app.state.jobs,
        SimpleNamespace(public=lambda: {'revision': 0}), settings, lambda rid: store.read_run(rid))
    with TestClient(app) as isolated:
        result = wait(scene, isolated.post(f'/api/runs/{RUN}/result-inspections', json={
            'request_id': 'one-medium-only', 'target': target, 'expected_revision': 0, 'consent': True}))
        assert result['status'] == 'succeeded', result
    report = http.get(f'/api/runs/{RUN}/result-inspections').json()['reports'][0]
    assert report['reference_pixels_compared'] is False
    assert report['status'] == 'unknown' and report['human_approval'] is False
    assert any('一致性未检查' in line for line in report['limitations'])
    assert len(model.calls) == 1
