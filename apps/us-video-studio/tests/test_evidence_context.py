"""Actual request facts/selection and honest byte-bound evidence, offline only."""
import hashlib
import json

import pytest

from fakes import ScriptedModel, final_turn
from studio.agents.reviewer import run_review
from studio.agents.roles import DIRECTOR, readable_stages
from studio.config import Settings
from studio.evidence import evidence_fingerprint, evidence_snapshot
from studio.input_import import import_content_input, prepare_content_input
import studio.input_import as input_import_module
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher
from studio.tools.workspace import Workspace

RUN = "evidence-case"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "data")
    store.create_run(RUN, "Synthetic", "Synthetic only")
    return store, Pipeline(store)


def submit(pipeline, stage, content, structured=None):
    attempt = pipeline.start_attempt(RUN, stage)
    return pipeline.submit(RUN, stage, attempt.token, content=content, summary=stage,
                           structured=structured)


def make_writing_ready(store, pipeline):
    submit(pipeline, "intake", "intake-only-marker; shipping remains UNKNOWN")
    submit(pipeline, "direction", "A speaks. B stays quiet.", {
        "options": [{"key": "A", "claim": "speaks", "satisfies": "goal.primary"},
                    {"key": "B", "claim": "quiet", "satisfies": "goal.primary"}],
        "recommended": "A",
    })
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="B")
    submit(pipeline, "material_plan", "plan keeps quiet")


def test_writer_actual_request_contains_intake_and_selected_b(env, tmp_path):
    store, pipeline = env
    make_writing_ready(store, pipeline)
    model = ScriptedModel([final_turn("offline complete")])
    settings = Settings(data_root=store.root, base_url="https://example.invalid")
    Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN).dispatch("writing", "write")
    seed = model.calls[0]["messages"][0]["content"]
    assert "intake-only-marker" in seed
    assert "shipping remains UNKNOWN" in seed
    assert "selected_option_id=B" in seed
    assert '"claim": "quiet"' in seed
    assert '"claim": "speaks"' not in seed
    assert "A speaks. B stays quiet." not in seed
    assert "intake" in readable_stages("writer")


def test_director_receives_effective_goal_without_full_chat(env):
    store, pipeline = env
    submit(pipeline, "intake", "Only product facts")
    model = ScriptedModel([final_turn("offline complete")])
    settings = Settings(data_root=store.root, base_url="https://example.invalid")
    Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN).dispatch(
        "direction", "propose options")
    seed = model.calls[0]["messages"][0]["content"]
    assert "goal.primary" in seed
    assert "Synthetic only" in seed
    assert "每个可供采用的方向都要在 satisfies 中标明" in seed


def test_material_planner_gets_selected_direction_without_other_options(env):
    store, pipeline = env
    submit(pipeline, "intake", "Product facts")
    submit(pipeline, "direction", "A unselected. B selected.", {
        "options": [{"key": "A", "claim": "unselected-claim", "satisfies": "goal.primary"},
                    {"key": "B", "claim": "selected-claim", "satisfies": "goal.primary"}],
        "prerequisites": ["shared-condition"],
        "dependency_matrix": [{"need": "only-A", "options": {"A": True, "B": False}},
                              {"need": "only-B", "options": {"A": False, "B": True}}],
    })
    pipeline.record_review(RUN, "direction", verdict="pass", findings=[])
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="B")
    model = ScriptedModel([final_turn("offline complete")])
    settings = Settings(data_root=store.root, base_url="https://example.invalid")
    Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN).dispatch(
        "material_plan", "plan the selected direction")
    seed = model.calls[0]["messages"][0]["content"]
    assert "selected-claim" in seed and "shared-condition" in seed and "only-B" in seed
    assert "unselected-claim" not in seed and "only-A" not in seed
    assert "A unselected. B selected." not in seed


def test_reviewer_actual_request_has_same_facts_and_choice(env):
    store, pipeline = env
    make_writing_ready(store, pipeline)
    submit(pipeline, "writing", "quiet script")
    model = ScriptedModel([final_turn("no verdict")])
    settings = Settings(data_root=store.root, base_url="https://example.invalid")
    run_review(store=store, pipeline=pipeline, settings=settings, client=model,
               ledger=Ledger(store, settings), run_id=RUN, stage="writing")
    seed = model.calls[0]["messages"][0]["content"]
    assert "intake-only-marker" in seed
    assert "selected_option_id=B" in seed
    assert '"claim": "speaks"' not in seed


def test_direction_reviewer_retains_product_observation_evidence(env):
    store, pipeline = env
    material(store, "only-on-demand.jpg")
    submit(pipeline, "intake", "Product facts")
    submit(pipeline, "direction", "Two directions")
    model = ScriptedModel([final_turn("no verdict")])
    settings = Settings(data_root=store.root, base_url="https://example.invalid")
    run_review(store=store, pipeline=pipeline, settings=settings, client=model,
               ledger=Ledger(store, settings), run_id=RUN, stage="direction")
    seed = model.calls[0]["messages"][0]["content"]
    assert "only-on-demand.jpg" in seed
    assert "本次请求未发送图片像素" in seed


def test_director_stops_at_comparable_directions_before_selection():
    assert "每个方向的推进概念控制在两三句话" in DIRECTOR.instructions
    assert "不要写逐镜头/逐页方案、时间段、完整台词或字幕样稿" in DIRECTOR.instructions


def material(store, name="still.jpg", content=b"synthetic image bytes"):
    path = store.run_dir(RUN) / "materials" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def manifest(store, record):
    path = store.run_dir(RUN) / "evidence" / "observations.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "observations": [record]}), encoding="utf-8")


def record(path, **override):
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "level": "image_observed", "observer": "human:test", "method": "manual inspection",
            "coverage": "whole still", "observed_at": "2026-09-17", "findings": ["synthetic"],
            "limitations": ["no audio"], **override}


def test_file_listing_never_claims_visual_observation(env):
    store, pipeline = env
    path = material(store)
    snapshot = evidence_snapshot(store, RUN)
    assert snapshot["materials"][0]["observation_level"] == "existence_only"
    assert snapshot["materials"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    context = Workspace(store, pipeline, RUN, "writer").render_materials()
    assert "existence_only" in context and "未发送图片像素" in context
    assert snapshot["reference_discovery"]["status"] == "unavailable"


def test_imported_observation_stays_attributed_and_invalidates_on_same_name_change(env):
    store, _ = env
    path = material(store)
    manifest(store, record(path))
    snapshot = evidence_snapshot(store, RUN)
    assert snapshot["materials"][0]["observations"][0]["observer"] == "human:test"
    before = evidence_fingerprint(store, RUN)
    path.write_bytes(b"different bytes")
    after = evidence_snapshot(store, RUN)
    assert after["materials"][0]["observation_level"] == "existence_only"
    assert after["errors"] and before != evidence_fingerprint(store, RUN)


def test_generated_output_is_derived_not_a_new_source_fact(env):
    """CLAUDE.md red line 7: only source facts advance generation or fingerprint."""
    from studio.evidence import evidence_generation

    store, _ = env
    material(store)
    generation = evidence_generation(store, RUN)
    before = evidence_fingerprint(store, RUN)
    output = material(store, "result.png", b"synthetic generated bytes")
    recorded = hashlib.sha256(output.read_bytes()).hexdigest()
    # The real flow: the file lands as a material, then the output is recorded.
    store.append_event(RUN, "material_uploaded", name=output.name, media_kind="image",
                       bytes=output.stat().st_size, sha256=recorded)
    store.append_event(RUN, "production_output", output={"name": output.name, "sha256": recorded})
    assert evidence_generation(store, RUN) == generation
    assert evidence_fingerprint(store, RUN) == before
    # It stays visible to agents, labelled as this run's own product.
    materials = {m["name"]: m for m in evidence_snapshot(store, RUN)["materials"]}
    assert materials[output.name]["origin"] == "generated"
    assert materials[output.name]["origin_sha256"] == recorded


def test_observing_own_generated_images_preserves_adopted_upstream(env):
    from studio.evidence import evidence_generation
    store, pipeline = env
    material(store)
    make_writing_ready(store, pipeline)
    writing = submit(pipeline, 'writing', 'Approved synthetic script')
    pipeline.record_review(RUN, 'writing', verdict='pass', findings=[])
    pipeline.record_approval(RUN, 'writing', nonce=pipeline.issue_nonce(RUN, 'writing'), decision='approved')
    before = evidence_fingerprint(store, RUN), evidence_generation(store, RUN)
    approvals = store.list_approvals(RUN)
    output = material(store, 'result.png', b'Synthetic generated image')
    observed = record(output, purpose='reference', observer='model:fixture')
    store.append_event(RUN, 'material_uploaded', name=output.name, sha256=observed['sha256'])
    store.append_event(RUN, 'production_output', output={'name': output.name, 'sha256': observed['sha256']})
    store.append_event(RUN, 'agent_images_observed', observations=[observed])
    assert (evidence_fingerprint(store, RUN), evidence_generation(store, RUN)) == before
    assert store.list_approvals(RUN) == approvals
    assert pipeline.stage_state(RUN, 'direction').status == 'approved'
    state = pipeline.stage_state(RUN, 'writing')
    assert state.status == 'approved' and state.latest.sha256 == writing.sha256 and not state.stale
    generated = next(m for m in evidence_snapshot(store, RUN)['materials'] if m['name'] == output.name)
    assert generated['origin'] == 'generated' and generated['observations'] == [observed]


@pytest.mark.parametrize('source', ['mixed', 'changed_bytes', 'unknown_history', 'empty', 'missing', 'non_object'])
def test_source_image_observations_still_advance_generation(env, source):
    from studio.evidence import evidence_generation
    store, _ = env
    original = material(store)
    output = material(store, 'result.png', b'Synthetic result')
    observed = record(output, purpose='reference')
    store.append_event(RUN, 'production_output', output={'name': output.name, 'sha256': observed['sha256']})
    before = evidence_generation(store, RUN)
    rows = [observed]
    if source == 'mixed':
        rows.append(record(original, purpose='product'))
    elif source == 'changed_bytes':
        output.write_bytes(b'Changed bytes')
        rows = [record(output, purpose='reference')]
    elif source == 'unknown_history':
        rows = [{'name': [], 'sha256': None}]
    elif source == 'empty':
        rows = []
    elif source == 'missing':
        rows = [{'name': output.name}]
    else:
        rows = [None]
    event = store.append_event(RUN, 'agent_images_observed', observations=rows)
    assert evidence_generation(store, RUN) == event['seq'] > before


def test_later_generation_registration_does_not_reclassify_prior_observation(env):
    from studio.evidence import evidence_generation
    store, _ = env
    output = material(store, 'result.png', b'Synthetic result')
    observed = record(output, purpose='reference')
    event = store.append_event(RUN, 'agent_images_observed', observations=[observed])
    store.append_event(RUN, 'production_output', output={'name': output.name, 'sha256': observed['sha256']})
    assert evidence_generation(store, RUN) == event['seq']


def test_real_source_return_to_prior_bytes_stays_stale_after_generated_observation(env):
    from studio.evidence import evidence_generation
    store, pipeline = env
    original = material(store)
    original_bytes = original.read_bytes()
    make_writing_ready(store, pipeline)
    before = evidence_fingerprint(store, RUN), evidence_generation(store, RUN)
    output = material(store, 'result.png', b'Synthetic result')
    observed = record(output, purpose='reference')
    store.append_event(RUN, 'production_output', output={'name': output.name, 'sha256': observed['sha256']})
    original.write_bytes(b'Changed product bytes')
    store.append_event(RUN, 'material_uploaded', name=original.name,
                       sha256=hashlib.sha256(original.read_bytes()).hexdigest())
    original.write_bytes(original_bytes)
    returned = store.append_event(RUN, 'material_uploaded', name=original.name,
                                  sha256=hashlib.sha256(original_bytes).hexdigest())
    store.append_event(RUN, 'agent_images_observed', observations=[observed])
    assert evidence_fingerprint(store, RUN) == before[0]
    assert evidence_generation(store, RUN) == returned['seq'] > before[1]
    assert pipeline.stage_state(RUN, 'direction').stale


def test_generated_file_changed_after_generation_is_a_source_change(env):
    """The exclusion is keyed on (name, sha256), not on the name alone."""
    store, _ = env
    path = material(store)
    store.append_event(RUN, "material_uploaded", name=path.name, media_kind="image",
                       bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    store.append_event(RUN, "production_output",
                       output={"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    before = evidence_fingerprint(store, RUN)
    path.write_bytes(b"tampered bytes")
    assert evidence_fingerprint(store, RUN) != before


@pytest.mark.parametrize("change", [{"name": "../outside.jpg"}, {"coverage": ""},
                                     {"level": "audiovisual_review"}, {"method": None}])
def test_unbound_or_insufficient_observation_is_not_usable(env, change):
    store, _ = env
    path = material(store)
    manifest(store, record(path, **change))
    snapshot = evidence_snapshot(store, RUN)
    assert snapshot["errors"]
    assert not snapshot["materials"][0]["observations"]


def test_symlink_outside_material_directory_is_not_evidence(env, tmp_path):
    store, _ = env
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    directory = store.run_dir(RUN) / "materials"
    directory.mkdir()
    try:
        (directory / "link.jpg").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    snapshot = evidence_snapshot(store, RUN)
    assert not snapshot["materials"] and snapshot["errors"]


def input_package(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    source = root / "source.md"
    source.write_text("shipping unknown", encoding="utf-8")
    data = {"schema_version": 1, "kind": "ContentInput", "track": "US", "input_id": "test-input",
            "created_on": "2026-09-17", "product": {"family": "synthetic"}, "goal": "test",
            "sources": [{"source_id": "source", "path": "source.md", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
            "facts": [{"source_id": "source", "value": None, "basis": "unknown shipping"}],
            "observations": [], "references": [], "preferences": [], "constraints": {"generation": False}}
    package = root / "input.json"
    package.write_text(json.dumps(data), encoding="utf-8")
    return root, package, source


def test_content_input_import_preserves_unknown_and_tracks_changed_sources(env, tmp_path):
    store, pipeline = env
    root, package, source = input_package(tmp_path)
    artifact = import_content_input(store, pipeline, RUN, path=package, repository_root=root, allowed_roots=[root])
    assert artifact.stage == "intake"
    body = store.read_artifact_text(RUN, "intake", artifact.version)
    assert '"value": null' in body and "unknown shipping" in body
    assert not pipeline.stage_state(RUN, "intake").stale
    source.write_text("shipping changed", encoding="utf-8")
    assert pipeline.stage_state(RUN, "intake").stale
    assert "input source changed" in str(evidence_snapshot(store, RUN)["errors"])
    with pytest.raises(ValueError, match="empty intake"):
        import_content_input(store, pipeline, RUN, path=package, repository_root=root, allowed_roots=[root])


def test_content_input_import_is_independent_of_sibling_workbench(tmp_path, monkeypatch):
    root, package, source = input_package(tmp_path)
    monkeypatch.setattr(input_import_module, '__file__', str(tmp_path / 'detached' / 'input_import.py'))
    prepared = prepare_content_input(package, root, [root])
    assert prepared['dependencies'][1]['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_content_input_import_rejects_missing_source_hash(tmp_path):
    root, package, _ = input_package(tmp_path)
    data = json.loads(package.read_text())
    del data['sources'][0]['sha256']
    package.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='sha256'):
        prepare_content_input(package, root, [root])


def test_content_input_import_rejects_outside_root_before_local_reader(tmp_path):
    root, package, source = input_package(tmp_path)
    data = json.loads(package.read_text())
    outside = tmp_path / "not-authorized.txt"
    outside.write_text("private", encoding="utf-8")
    data["sources"][0]["path"] = str(outside)
    package.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="outside explicitly allowed"):
        prepare_content_input(package, root, [root])
