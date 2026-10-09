"""Three-page acceptance through real HTTP, with counted synthetic generation."""
from copy import deepcopy
import io
import json
import zipfile
import pytest

from test_visual_production import visual_scene, save, refs
from test_production import RUN, BASE, action, wait, workspace


@pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)
def test_three_pages_reference_revision_annotations_and_export(visual_scene):
    scene = visual_scene
    http, store, pipeline, model, images, videos, _ = scene
    structure = deepcopy(store.latest_artifact(RUN, 'writing').structured)
    structure['segments'].append({**structure['segments'][-1], 'shot_id': '', 'page_id': '',
                                  'picture': 'Ava closes the lunch bag.', 'line': 'Ready for the day.'})
    attempt = pipeline.start_attempt(RUN, 'writing')
    pipeline.submit(RUN, 'writing', attempt.token, content='Synthetic three-page photo plan',
                    summary='Offline three-page acceptance', structured=structure)
    pipeline.record_approval(RUN, 'writing', nonce=pipeline.issue_nonce(RUN, 'writing'), decision='approved')
    targets = workspace(scene)['targets']['image']['shots']
    assert len(targets) == 3
    drafts = []
    for shot in targets:
        draft = save(scene, request_id=f'page-{shot["ordinal"]}', shot_id=shot['shot_id'], references=refs(scene)).json()
        assert wait(scene, action(scene, draft, 'generate', f'generate-{shot["ordinal"]}'))['status'] == 'succeeded'
        drafts.append(draft)
    before = workspace(scene)
    outputs = [row['attempts'][-1]['output'] for row in before['drafts']]
    graph = http.get(f'/api/runs/{RUN}/canvas').json()
    assert len([n for n in graph['nodes'] if n['kind'] == 'shot']) == 3
    assert {n['target']['design_kind'] for n in graph['nodes'] if n['kind'] == 'design'} == {'character', 'scene'}
    second = next(n for n in graph['nodes'] if n['kind'] == 'media' and n['data']['draft']['draft_id'] == drafts[1]['draft_id'])
    annotation = http.post(f'/api/runs/{RUN}/annotations', json={'request_id': 'second-page-note',
        'target': second['target'], 'text': '只改第 2 页背景，商品与其他两页保持。',
        'region': {'x': .1, 'y': .1, 'width': .4, 'height': .4}})
    assert annotation.status_code == 200, annotation.text
    assert len(images.calls) == 3 and not model.calls and not videos.calls
    old = drafts[1]
    reference_change = {**old['request_payload'], 'request_id': 'second-new-reference', 'prompt': None,
        'preserve_prompt_from': {'draft_id': old['draft_id'], 'expected_sha256': old['draft_sha256']},
        'supersedes': old['draft_id'], 'references': refs(scene)[:1]}
    reference_change.pop('reference', None)
    revised = http.post(BASE + '/drafts', json=reference_change)
    assert revised.status_code == 200, revised.text
    assert len(images.calls) == 3
    changed = workspace(scene)
    assert changed['shots']['image']['shots'][1]['stage'] == 'input_ready'
    assert not changed['photo_delivery']['available']
    current = [d for d in changed['drafts'] if d['is_current']]
    assert current[0]['attempts'][-1]['output'] == outputs[0]
    assert next(d for d in current if d['draft_id'] == drafts[2]['draft_id'])['attempts'][-1]['output'] == outputs[2]
    assert wait(scene, action(scene, revised.json(), 'generate', 'second-explicit-generation'))['status'] == 'succeeded'
    delivery = workspace(scene)['photo_delivery']
    downloaded = http.get(f'/api/runs/{RUN}/delivery/photos/download', params={'expected_snapshot_sha256': delivery['snapshot_sha256']})
    assert downloaded.status_code == 200, downloaded.text
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert len(manifest['pages']) == 3
        assert [page['ordinal'] for page in manifest['pages']] == [1, 2, 3]
    assert len(images.calls) == 4 and not model.calls and not videos.calls
