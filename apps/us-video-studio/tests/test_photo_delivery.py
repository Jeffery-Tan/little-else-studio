"""Current photo archives over real HTTP/pipeline flow, with synthetic pixels only."""
import hashlib
import io
import json
import zipfile
import pytest
from studio.agents.codex_client import CodexUnavailable
from studio.delivery import photo_delivery_projection
from test_visual_production import visual_scene, save
from test_production import BASE, RUN, action, wait, workspace

ENDPOINT = f'/api/runs/{RUN}/delivery/photos/download'
pytestmark = pytest.mark.parametrize('visual_scene', ['photo'], indirect=True)


def complete(scene):
    items = []
    for target in workspace(scene)['targets']['image']['shots']:
        response = save(scene, request_id=f'archive-input-{target["ordinal"]}', shot_id=target['shot_id'])
        assert response.status_code == 200, response.text
        item = response.json()
        assert wait(scene, action(scene, item, 'generate', f'archive-image-{target["ordinal"]}'))['status'] == 'succeeded'
        items.append(item)
    return items


def download(scene, token=None):
    token = token or workspace(scene)['photo_delivery']['snapshot_sha256'] or '0' * 64
    return scene[0].get(ENDPOINT, params={'expected_snapshot_sha256': token})


def file_hashes(store):
    root = store.run_dir(RUN)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_missing_current_pages_are_specific_and_export_does_not_call_models(visual_scene):
    scene = visual_scene
    assert not workspace(scene)['photo_delivery']['available']
    before = file_hashes(scene[1])
    result = download(scene)
    assert result.status_code == 409
    assert '第 1、2 页' in result.json()['detail']
    assert result.headers['content-type'].startswith('application/json')
    assert file_hashes(scene[1]) == before
    assert not scene[3].calls and not scene[4].calls and not scene[5].calls


def test_archive_contains_only_current_sorted_original_pixels_and_verbatim_caption(visual_scene):
    scene = visual_scene
    complete(scene)
    delivery = workspace(scene)['photo_delivery']
    assert delivery['available']
    before = file_hashes(scene[1])
    calls = len(scene[4].calls)
    result = download(scene)
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'no-store'
    assert result.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert set(archive.namelist()) == {'images/page-01.png', 'images/page-02.png', 'caption.txt', 'page-copy.txt', 'README.txt', 'manifest.json'}
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['snapshot_sha256'] == delivery['snapshot_sha256']
        assert manifest['run_id'] == RUN
        assert manifest['source_sha256'] == scene[1].latest_artifact(RUN, 'writing').sha256
        assert [p['ordinal'] for p in manifest['pages']] == [1, 2]
        assert all(page['approved'] is False for page in manifest['pages'])
        assert '不代表图审' in archive.read('README.txt').decode('utf-8')
        for page in manifest['pages']:
            raw = archive.read(page['archive_file'])
            assert hashlib.sha256(raw).hexdigest() == page['sha256']
            assert raw == (scene[1].run_dir(RUN) / 'materials' / page['name']).read_bytes()
        source = scene[1].latest_artifact(RUN, 'writing')
        assert archive.read('caption.txt') == source.structured['caption'].encode('utf-8')
        assert source.structured['segments'][0]['line'].encode('utf-8') in archive.read('page-copy.txt')
    assert download(scene).status_code == 200
    assert file_hashes(scene[1]) == before
    assert len(scene[4].calls) == calls and not scene[3].calls and not scene[5].calls


def test_local_replacement_never_packages_previous_output_to_fill_missing_page(visual_scene):
    scene = visual_scene
    originals = complete(scene)
    token = workspace(scene)['photo_delivery']['snapshot_sha256']
    replacement = save(scene, request_id='archive-revised-second', shot_id='shot-02', supersedes=originals[1]['draft_id'])
    assert replacement.status_code == 200, replacement.text
    assert [r['stage'] for r in workspace(scene)['shots']['image']['shots']] == ['completed', 'input_ready']
    before = file_hashes(scene[1])
    result = download(scene, token)
    assert result.status_code == 409 and '第 2 页' in result.json()['detail']
    assert file_hashes(scene[1]) == before
    assert len(scene[4].calls) == 2


def test_old_view_token_refuses_changed_confirmation_and_new_archive_keeps_actual_flags(visual_scene):
    scene = visual_scene
    complete(scene)
    state = workspace(scene)
    token = state['photo_delivery']['snapshot_sha256']
    image = state['photo_delivery']['pages'][0]
    approved = scene[0].post(BASE + '/material-approvals', json={'name': image['name'], 'sha256': image['sha256'],
                                                              'approved': True, 'nonce': state['nonce']})
    assert approved.status_code == 200, approved.text
    result = download(scene, token)
    assert result.status_code == 409 and '已变化' in result.json()['detail']
    before = file_hashes(scene[1])
    result = download(scene)
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert [p['approved'] for p in json.loads(archive.read('manifest.json'))['pages']] == [True, False]
    assert file_hashes(scene[1]) == before


@pytest.mark.parametrize('error', [CodexUnavailable('Offline failed precheck'), RuntimeError('Offline unconfirmed result')])
def test_latest_failure_or_unknown_does_not_download_previous_success(visual_scene, error):
    scene = visual_scene
    items = complete(scene)
    token = workspace(scene)['photo_delivery']['snapshot_sha256']
    scene[4].error = error
    assert wait(scene, action(scene, items[0], 'generate', 'archive-new-unsettled-attempt'))['status'] == 'blocked'
    assert not workspace(scene)['photo_delivery']['available']
    before = file_hashes(scene[1])
    result = download(scene, token)
    assert result.status_code == 409 and '第 1 页' in result.json()['detail']
    assert file_hashes(scene[1]) == before


def test_tampered_output_refuses_archive_and_closes_temporary_storage(visual_scene, monkeypatch):
    import tempfile
    scene = visual_scene
    complete(scene)
    original = tempfile.SpooledTemporaryFile
    opened = []
    def spool(*args, **kwargs):
        assert kwargs['max_size'] == 8 * 1024 * 1024
        # Exercise disk rollover and closure without allocating real large media.
        kwargs['max_size'] = 1
        result = original(*args, **kwargs)
        opened.append(result)
        return result
    monkeypatch.setattr(tempfile, 'SpooledTemporaryFile', spool)
    token = workspace(scene)['photo_delivery']['snapshot_sha256']
    assert download(scene).status_code == 200
    assert opened[-1]._rolled
    assert all(f.closed for f in opened)
    name = workspace(scene)['photo_delivery']['pages'][0]['name']
    (scene[1].run_dir(RUN) / 'materials' / name).write_bytes(b'not an image')
    before = file_hashes(scene[1])
    assert download(scene, token).status_code == 409
    assert all(f.closed for f in opened)
    assert file_hashes(scene[1]) == before


def test_artifact_changes_and_invalid_tokens_are_not_silent_new_version_downloads(visual_scene):
    scene = visual_scene
    complete(scene)
    token = workspace(scene)['photo_delivery']['snapshot_sha256']
    assert download(scene, 'f' * 64).status_code == 409
    assert scene[0].get(ENDPOINT, params={'expected_snapshot_sha256': '../other'}).status_code == 422
    assert scene[0].get(ENDPOINT).status_code == 422
    source = scene[1].latest_artifact(RUN, 'writing')
    (scene[1].run_dir(RUN) / 'artifacts' / source.filename).write_text('Changed test artifact', encoding='utf-8')
    before = file_hashes(scene[1])
    assert download(scene, token).status_code == 409
    assert file_hashes(scene[1]) == before


@pytest.mark.parametrize('damage', ['duplicate-page', 'missing-copy-row', 'other-source', 'duplicate-material'])
def test_projection_rejects_ambiguous_export_identity(visual_scene, damage):
    scene = visual_scene
    complete(scene)
    state = workspace(scene)
    source = scene[1].latest_artifact(RUN, 'writing')
    if damage == 'duplicate-page':
        state['targets']['image']['shots'][1]['ordinal'] = 1
    elif damage == 'missing-copy-row':
        source.structured['segments'].pop()
    elif damage == 'other-source':
        state['targets']['image']['source_sha256'] = 'f' * 64
    else:
        material = next(m for m in state['materials'] if m['name'] == state['photo_delivery']['pages'][0]['name'])
        state['materials'].append(dict(material))
    result = photo_delivery_projection({**state, 'run_id': RUN}, source, writing_ready=True, format='photo')
    assert not result['available'] and result['reason'] and not result['snapshot_sha256']
