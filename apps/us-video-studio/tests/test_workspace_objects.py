"""Real local HTTP commands with immutable fixtures; no provider calls."""
from copy import deepcopy

from test_production import scene, draft, workspace, RUN, approve, action, wait


def test_canvas_relations_and_exact_compare(scene):
    http, store, *_ = scene
    original = draft(scene, kind='image', request_id='canvas-original').json()
    url = f'/api/runs/{RUN}'
    graph = http.get(url + '/canvas')
    assert graph.status_code == 200, graph.text
    data = graph.json()
    node = next(n for n in data['nodes'] if n['target'].get('draft_id') == original['draft_id'])
    assert 'edit_references' in node['allowed_actions']
    assert any(e['to'] == node['id'] and e['kind'] == 'production_input' for e in data['edges'])
    compared = http.post(url + '/objects/compare', json={'left': node['target'], 'right': node['target']})
    assert compared.status_code == 200, compared.text
    assert compared.json()['identical'] and compared.json()['diff'] == ''
    bad = {**node['target'], 'draft_sha256': '0' * 64}
    assert http.post(url + '/objects/compare', json={'left': bad, 'right': node['target']}).status_code == 409


def test_layout_persists_with_revision_and_idempotency(scene):
    http, *_ = scene
    url = f'/api/runs/{RUN}/canvas/layout'
    payload = {'request_id': 'layout-one', 'expected_revision': 0,
               'view': {'zoom': 1, 'x': 32, 'y': 32}, 'positions': {'card': {'x': 25, 'y': 40}}}
    response = http.post(url, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 1
    assert http.post(url, json=payload).json() == response.json()
    assert http.post(url, json={**payload, 'request_id': 'layout-stale'}).status_code == 409
    assert http.get(f'/api/runs/{RUN}/canvas').json()['layout']['positions']['card']['x'] == 25


def test_script_design_edges_keep_the_design_version_it_used(scene):
    http, store, pipeline, *_ = scene
    original = store.latest_artifact(RUN, 'material_plan')
    updated = deepcopy(original.structured)
    updated['visual_design']['scenes'][0]['label'] = 'New scene, same identity'
    attempt = pipeline.start_attempt(RUN, 'material_plan')
    pipeline.submit(RUN, 'material_plan', attempt.token, content='Updated scene',
                    summary='Changed design', structured=updated)
    response = http.get(f'/api/runs/{RUN}/canvas')
    assert response.status_code == 200, response.text
    graph = response.json()
    nodes = {n['id']: n for n in graph['nodes']}
    edges = [e for e in graph['edges'] if e['kind'] == 'uses_design']
    assert edges
    assert all(nodes[e['from']]['target']['artifact_sha256'] == original.sha256 for e in edges)
    assert any(n['kind'] == 'design' and n['target']['version'] == 2 for n in nodes.values())


def test_annotation_saves_without_model_and_is_exact(scene):
    http, _, _, model, images, videos, _ = scene
    original = draft(scene, kind='image', request_id='annotation-original').json()
    target = {'kind': 'production_draft', 'run_id': RUN, 'draft_id': original['draft_id'],
              'draft_sha256': original['draft_sha256']}
    url = f'/api/runs/{RUN}/annotations'
    payload = {'request_id': 'annotation-one', 'target': target, 'text': '只调整背景。'}
    result = http.post(url, json=payload)
    assert result.status_code == 200, result.text
    assert http.post(url, json=payload).json()['annotation_id'] == result.json()['annotation_id']
    assert http.post(url, json={**payload, 'text': '另一个要求'}).status_code == 409
    snapshot = http.get(url).json()
    assert snapshot['items'][0]['state'] == 'saved'
    assert not model.calls and not images.calls and not videos.calls

    invalid = deepcopy(payload)
    invalid.update(request_id='region-wrong-object', region={'x': 0, 'y': 0, 'width': .5, 'height': .5})
    assert http.post(url, json=invalid).status_code == 409
    assert http.post(url + '/execute', json={'request_id': 'exec-stale',
        'annotation_ids': [result.json()['annotation_id']], 'expected_snapshot_sha256': '0' * 64}).status_code == 409
    assert not model.calls and not images.calls and not videos.calls


def test_multiple_video_timestamps_share_one_revision_and_keep_each_note(scene):
    from fastapi import FastAPI
    from local_http import TestClient
    from studio.http_workspace import install_workspace_routes
    from studio.agents.orchestrator import OrchestratorTurn
    from studio.workspace_objects import annotation_context
    http, store, pipeline, model, images, videos, _ = scene
    item = draft(scene, kind='video').json()
    assert approve(scene).status_code == 200
    assert wait(scene, action(scene, item, 'generate'))['status'] == 'succeeded'
    target = next(n['target'] for n in http.get(f'/api/runs/{RUN}/canvas').json()['nodes'] if n['kind'] == 'media')
    url = f'/api/runs/{RUN}/annotations'
    identities = [http.post(url, json={'request_id': f'time-{index}', 'target': target,
        'text': text, 'at_seconds': at}).json()['annotation_id']
        for index, (text, at) in enumerate([('这里动作太急。', .5), ('这里保留停顿。', 2.5)])]
    preparations = []
    def prepare(run_id, **kwargs):
        notes = annotation_context(store, run_id, kwargs['user_input_id'])
        preparations.append((kwargs['feedback_target'], notes))
        return (lambda *_: OrchestratorTurn('Explicit offline runner, no creative claim.', 'done', 0)), {'source': 'test'}
    app = FastAPI()
    install_workspace_routes(app, store, pipeline, http.app.state.jobs, lambda rid: store.read_run(rid), prepare)
    snapshot = http.get(url).json()
    assert all(a['executable'] for a in snapshot['items'])
    with TestClient(app) as isolated:
        job = wait(scene, isolated.post(url + '/execute', json={'request_id': 'batch-times',
            'annotation_ids': identities, 'expected_snapshot_sha256': snapshot['snapshot_sha256']}))
        assert job['status'] == 'succeeded', job
    assert len(preparations) == 1
    assert 'at_seconds' not in preparations[0][0]
    assert [a['at_seconds'] for a in preparations[0][1]] == [.5, 2.5]
    assert len(videos.calls) == 1 and not images.calls and not model.calls
    retry = http.post(url, json={'request_id': 'time-0', 'target': target,
        'text': '这里动作太急。', 'at_seconds': .5})
    assert retry.status_code == 200 and retry.json()['state'] == 'submitted'
