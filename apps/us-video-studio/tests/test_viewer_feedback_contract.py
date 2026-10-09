"""Actual temporary HTTP projections for preview feedback; no provider calls."""
import hashlib

from studio.materials import accept_upload, material_url
from studio.production import DraftIn, prepare_draft
from test_production import scene, adopt_shot_count, png
from test_server import RUN


def test_raw_material_preview_uses_verified_production_hash_and_changed_bytes_still_reject_feedback(scene):
    http, store, pipeline, reviewer, images, videos, _ = scene
    adopt_shot_count(scene, 1)
    source = store.latest_artifact(RUN, 'writing')
    reference = store.run_dir(RUN) / 'materials/reference.png'
    draft = prepare_draft(store, pipeline, RUN, DraftIn(
        request_id='preview-contract-image', kind='image', prompt='Synthetic preview contract only.',
        source_version=source.version, source_sha256=source.sha256,
        shot_id=source.structured['segments'][0]['shot_id'], model='codex-image',
        references=[{'name':reference.name, 'sha256':hashlib.sha256(reference.read_bytes()).hexdigest(),
                     'purpose':'product'}]))
    name, raw, attempt_id = 'preview-generated.png', png(), 'preview-contract-attempt'
    sha = hashlib.sha256(raw).hexdigest()
    accept_upload(store, RUN, name, raw)
    store.append_event(RUN, 'production_attempt', attempt_id=attempt_id,
        draft_id=draft['draft_id'], draft_sha256=draft['draft_sha256'], media_kind='image', status='succeeded')
    store.append_event(RUN, 'production_output', attempt_id=attempt_id,
        output={'name':name, 'sha256':sha, 'url':material_url(RUN, name)})
    approvals = store.list_approvals(RUN)

    materials = http.get(f'/api/runs/{RUN}/materials')
    assert materials.status_code == 200, materials.text
    material = next(row for row in materials.json() if row['name'] == name)
    assert set(material) == {'name', 'kind', 'bytes', 'url', 'usable'}
    assert material['kind'] == 'image' and material['usable'] is True
    assert 'sha256' not in material
    response = http.get(f'/api/runs/{RUN}/production')
    assert response.status_code == 200, response.text
    projected = next(row for row in response.json()['drafts'] if row['draft_id'] == draft['draft_id'])
    assert projected['source_ready'] is True and projected['is_current'] is True
    assert projected['draft_sha256'] == draft['draft_sha256']
    assert projected['source_version'] == source.version and projected['source_sha256'] == source.sha256
    attempt = next(row for row in projected['attempts'] if row['attempt_id'] == attempt_id)
    assert attempt['status'] == 'succeeded' and attempt['output_available'] is True
    assert attempt['output']['name'] == material['name'] and attempt['output']['sha256'] == sha
    timeline = http.get(f'/api/runs/{RUN}/timeline').json()
    writing = next(row for row in timeline['rail'] if row['stage'] == 'writing')
    assert writing['has_doc'] and not writing['stale'] and writing['integrity_ok']
    assert writing['version'] == source.version and writing['artifact_sha256'] == source.sha256

    # A stale raw listing cannot authorize the old bytes: production computes
    # availability and the existing feedback admission reads the physical file.
    (store.run_dir(RUN) / 'materials' / name).write_bytes(png((241, 240)))
    response = http.get(f'/api/runs/{RUN}/production')
    assert response.status_code == 200, response.text
    projected = next(row for row in response.json()['drafts'] if row['draft_id'] == draft['draft_id'])
    assert projected['attempts'][0]['output_available'] is False
    rejected = http.post(f'/api/runs/{RUN}/jobs', json={
        'text':'Only describe a correction to this selected image.', 'request_id':'stale-preview-feedback',
        'feedback_target':{'kind':'media', 'run_id':RUN, 'attempt_id':attempt_id, 'output_sha256':sha}})
    assert rejected.status_code == 409, rejected.text
    assert not reviewer.calls and not images.calls and not videos.calls
    assert not http.app.state.jobs.ledger.entries(RUN)
    assert store.list_approvals(RUN) == approvals
