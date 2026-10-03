"""The shared design survives authoring, dependencies and specialist handoff."""
from copy import deepcopy

import pytest

from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.revision_scope import work_plan
from studio.store import Store
from studio.structured import canonical_document, normalize, view
from studio.visual_design import normalize_visual_design
from visual_fixtures import stage_structure, visual_design


RUN = 'visual-contract'


def material_attempt(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic design contract', 'Synthetic offline product demonstration')
    pipe = Pipeline(store)
    for stage, structured in (
        ('intake', {'facts': [{'label': 'Scope', 'value': 'Synthetic only', 'source': 'Fixture'}]}),
        ('direction', {'options': [{'key': 'A', 'claim': 'Synthetic', 'satisfies': 'goal.primary'}]}),
    ):
        attempt = pipe.start_attempt(RUN, stage)
        pipe.submit(RUN, stage, attempt.token, content='', summary=stage, structured=structured)
        if stage == 'direction':
            pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage),
                                 decision='approved', selected_option_id='A')
    return store, pipe, pipe.start_attempt(RUN, 'material_plan')


@pytest.mark.parametrize('content', ['', 'Legacy dual-written body'])
def test_shared_design_survives_canonical_and_dual_written_authoring(tmp_path, content):
    store, pipe, attempt = material_attempt(tmp_path)
    design = visual_design()
    structured = stage_structure('material_plan')
    artifact = pipe.submit(RUN, 'material_plan', attempt.token, content=content,
                           summary='Shared design', structured=structured)
    assert artifact.structured['visual_design'] == design
    assert store.verify_artifact(RUN, 'material_plan', artifact.version)
    blocks = view('material_plan', artifact.structured)
    serialized = str(blocks)
    for key in ('audience', 'everyday_context', 'localization', 'style', 'product_appearance'):
        assert design[key] in serialized
    if not content:
        body = store.read_artifact_text(RUN, 'material_plan', artifact.version)
        assert body == canonical_document('material_plan', artifact.structured)
        assert design['scenes'][0]['location'] in body


@pytest.mark.parametrize('content', ['', 'Fallback prose cannot hide a lost design'])
def test_unsupported_design_fields_do_not_get_silently_dropped(tmp_path, content):
    store, pipe, attempt = material_attempt(tmp_path)
    bad = stage_structure('material_plan')
    bad['visual_design']['unregistered_market_claim'] = 'Must not silently survive or disappear'
    with pytest.raises(PipelineError, match='未知字段'):
        pipe.submit(RUN, 'material_plan', attempt.token, content=content,
                    summary='Bad design', structured=bad)
    assert store.latest_artifact(RUN, 'material_plan') is None
    assert pipe.stage_state(RUN, 'material_plan').open_attempt.token == attempt.token
    assert normalize('material_plan', bad).dropped == 1


@pytest.mark.parametrize('bad_reference', ['unknown_scene', 'unknown_character', 'implicit_people'])
def test_writer_cannot_save_unmapped_design_identity_even_with_prose(tmp_path, bad_reference):
    store, pipe, attempt = material_attempt(tmp_path)
    pipe.submit(RUN, 'material_plan', attempt.token, content='', summary='Design',
                structured=stage_structure('material_plan'))
    writing = stage_structure('writing')
    row = writing['segments'][0]
    if bad_reference == 'unknown_scene':
        row['scene_id'] = 'another_place'
    elif bad_reference == 'unknown_character':
        row['character_ids'] = 'invented_person'
    else:
        del row['character_ids']
    attempt = pipe.start_attempt(RUN, 'writing')
    with pytest.raises(PipelineError):
        pipe.submit(RUN, 'writing', attempt.token, content='Prose with unbound visual choices',
                    summary='Invalid identities', structured=writing)
    assert store.latest_artifact(RUN, 'writing') is None


def test_design_revision_invalidates_downstream_without_rewriting_old_design(tmp_path):
    store, pipe, attempt = material_attempt(tmp_path)
    first = pipe.submit(RUN, 'material_plan', attempt.token, content='', summary='Design',
                        structured=stage_structure('material_plan'))
    old_bytes = store.read_artifact_text(RUN, 'material_plan', first.version)
    attempt = pipe.start_attempt(RUN, 'writing')
    script = pipe.submit(RUN, 'writing', attempt.token, content='', summary='Script',
                         structured=stage_structure('writing'))
    pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='approved')
    assert pipe.is_complete(RUN, 'writing')
    revised = stage_structure('material_plan')
    revised['visual_design']['scenes'][0]['lighting'] = 'Evening lamp light; intentionally changed design.'
    attempt = pipe.start_attempt(RUN, 'material_plan')
    second = pipe.submit(RUN, 'material_plan', attempt.token, content='', summary='Revised design', structured=revised)
    assert second.sha256 != first.sha256
    assert pipe.stage_state(RUN, 'writing').stale
    assert not pipe.is_complete(RUN, 'writing')
    assert store.read_artifact_text(RUN, 'material_plan', first.version) == old_bytes
    assert store.latest_artifact(RUN, 'writing').sha256 == script.sha256


def test_no_people_closeup_is_an_explicit_design_not_a_missing_character(tmp_path):
    store, pipe, attempt = material_attempt(tmp_path)
    design = visual_design()
    assert normalize_visual_design(design)['characters'] == []
    pipe.submit(RUN, 'material_plan', attempt.token, content='', summary='Product close-up',
                structured=stage_structure('material_plan'))
    attempt = pipe.start_attempt(RUN, 'writing')
    artifact = pipe.submit(RUN, 'writing', attempt.token, content='', summary='No people',
                           structured=stage_structure('writing'))
    assert artifact.structured['segments'][0]['character_ids'] == ''
    assert artifact.structured['segments'][0]['scene_id'] == 'table'


def test_legacy_missing_design_routes_production_back_to_material_without_migration(tmp_path):
    store, pipe, attempt = material_attempt(tmp_path)
    artifact = pipe.submit(RUN, 'material_plan', attempt.token, content='Historical prose plan', summary='Legacy')
    before = store.read_artifact_text(RUN, 'material_plan', artifact.version)
    plan = work_plan(pipe, RUN)
    assert not plan['visual_design_status']['ready']
    assert plan['production_next_step']['stage'] == 'material_plan'
    assert store.read_artifact_text(RUN, 'material_plan', artifact.version) == before


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(market='SEA'),
    lambda d: d.update(scenes=[]),
    lambda d: d['scenes'].append(deepcopy(d['scenes'][0])),
    lambda d: d.update(continuity=[]),
])
def test_design_rejects_wrong_market_or_unusable_shared_world(mutate):
    design = visual_design()
    mutate(design)
    with pytest.raises(ValueError):
        normalize_visual_design(design)
