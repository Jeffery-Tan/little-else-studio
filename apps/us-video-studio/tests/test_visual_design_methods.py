"""Offline method and role request assembly; not a test of model creative quality."""
import hashlib
import json
import shutil

import pytest

from fakes import ScriptedModel, final_turn
from studio.agents.roles import ROLES
from studio.config import Settings
from studio.learning import LearningService, METHOD_CARDS, REPOSITORY, SKILLS, validate_method_packages
from studio.ledger import Ledger
from studio.models import STAGES
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher


RUN = "visual-method-fixture"


@pytest.fixture
def visual_env(tmp_path):
    repo = tmp_path / "repo"
    library = repo / "tracks/us/creative/learning/library.json"
    library.parent.mkdir(parents=True)
    library.write_text(json.dumps({"schema_version": 1, "market": "US", "library_version": 1,
                                   "cases": [], "methods": []}), encoding="utf-8")
    for skill in SKILLS:
        relative = f".agents/skills/{skill}/SKILL.md"
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target)
    for spec in METHOD_CARDS.values():
        target = repo / spec['file']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / spec['file'], target)
    store = Store(tmp_path / "data")
    store.create_run(RUN, "Synthetic visual design", "Synthetic product only; no real market claim")
    return LearningService(store, repo), store, Pipeline(store)


def submit(pipeline, stage, structured=None):
    attempt = pipeline.start_attempt(RUN, stage)
    return pipeline.submit(RUN, stage, attempt.token, content=f"Synthetic {stage}",
                           summary=stage, structured=structured)


def design():
    return {"schema_version": 1, "market": "US", "audience": "Fixture lunch-at-desk hypothesis",
            "everyday_context": "Fixture office lunch", "localization": "Fixture neutral workspace hypothesis",
            "style": "Fixture product demonstration", "product_appearance": "Fixture shape; unseen details unknown",
            "characters": [], "scenes": [{"id": "desk-01", "label": "Fixture desk", "location": "Fixture office",
                "layout": "Fixture desk and chair", "props": "Fixture plate", "lighting": "Fixture daylight",
                "local_details": "Fixture neutral desk chosen for lunch-at-desk demonstration"}],
            "continuity": ["Fixture product identity stays constant"],
            "assumptions": ["Fixture audience and environment are creative assumptions"]}


def ready_for(service, store, pipeline, stage, format):
    store.append_event(RUN, "creative_context_selected",
                       selection={"intent": "new", "format": format, "tags": [], "case_ids": None})
    submit(pipeline, "intake")
    if stage == 'direction':
        return
    submit(pipeline, "direction", {"options": [
        {"key": "A", "claim": "Unselected fixture", "satisfies": "goal.primary"},
        {"key": "B", "claim": "Selected fixture", "satisfies": "goal.primary",
         "audience_moment": "Fixture lunch-at-desk purchase moment",
         "story": "Fixture brand demonstrator relationship; neutral office environment hypothesis"}],
        "recommended": "A"})
    pipeline.record_approval(RUN, "direction", nonce=pipeline.issue_nonce(RUN, "direction"),
                             decision="approved", selected_option_id="B")
    if stage == 'writing':
        submit(pipeline, "material_plan", {"items": [{"shot": "Fixture desk view", "purpose": "Fixture proof",
              "status": "需要生成", "note": "Use desk-01", "file": ""}], "visual_design": design()})


def test_visual_design_card_is_registered_and_links_are_valid():
    assert validate_method_packages() == []
    assert set(METHOD_CARDS['visual-design']['default_formats']) == {'video', 'photo'}
    assert set(METHOD_CARDS['visual-design']['roles']) == {'orchestrator', 'director', 'material', 'writer'}
    assert STAGES == ('intake', 'direction', 'material_plan', 'writing')


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('role', ['orchestrator', 'director', 'material', 'writer'])
def test_actual_default_method_text_and_hash_are_in_context(visual_env, role, format):
    service, store, _ = visual_env
    result = service.context(RUN, role, format=format, purpose='storyboard')
    card, = result['method_cards']
    raw = (service.repository_root / METHOD_CARDS['visual-design']['file']).read_bytes()
    assert card['id'] == 'visual-design'
    assert card['text'] == raw.decode('utf-8-sig')
    assert card['sha256'] == hashlib.sha256(raw).hexdigest()
    assert card['text'] in result['text']
    assert '创作假设' in card['text'] and '不默认白人' in card['text']
    if role == 'material':
        # The default is a task-specific stable card, not every creative Skill.
        assert result['skills'] == []
    saved = store.run_dir(RUN) / result['evidence_ref']
    assert json.loads(saved.read_text(encoding='utf-8')) == result
    event = store.read_events(RUN)[-1]
    assert event['kind'] == 'learning_context_loaded'
    assert event['method_cards'] == [{'id': card['id'], 'sha256': card['sha256']}]


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('intent,role', [('status', 'orchestrator'), ('pause', 'orchestrator'), ('continue', 'reviewer')])
def test_nonproduction_context_does_not_inject_design_method(visual_env, format, intent, role):
    service, _, _ = visual_env
    result = service.context(RUN, role, format=format, intent=intent)
    assert result['method_cards'] == []
    assert '共同设计的单一来源' not in result['text']


def test_new_method_revision_preserves_saved_context_bytes(visual_env):
    service, store, _ = visual_env
    first = service.context(RUN, 'material', purpose='storyboard')
    old_path = store.run_dir(RUN) / first['evidence_ref']
    original = old_path.read_bytes()
    card_path = service.repository_root / METHOD_CARDS['visual-design']['file']
    card_path.write_bytes(card_path.read_bytes() + b'\nFixture-only later revision\n')
    second = service.context(RUN, 'material', purpose='storyboard')
    assert second['snapshot_sha256'] != first['snapshot_sha256']
    assert second['method_cards'][0]['sha256'] != first['method_cards'][0]['sha256']
    assert old_path.read_bytes() == original
    assert 'Fixture-only later revision' not in json.loads(original)['text']


@pytest.mark.parametrize('format', ['video', 'photo'])
@pytest.mark.parametrize('stage,role', [('direction', 'director'), ('material_plan', 'material'), ('writing', 'writer')])
def test_professional_request_receives_design_responsibility_and_default_card(visual_env, format, stage, role):
    service, store, pipeline = visual_env
    ready_for(service, store, pipeline, stage, format)
    model = ScriptedModel([final_turn('Fixture request inspection only')])
    settings = Settings(data_root=store.root, base_url='https://example.invalid')
    Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN, learning_service=service).dispatch(stage, 'Fixture task')
    assert len(model.calls) == 1
    request = model.calls[0]
    assert '# US 人物、场景与共同视觉设计方法卡' in request['system']
    assert '创作假设' in request['system']
    assert ROLES[role].owns == stage
    if stage == 'direction':
        assert 'audience_moment' in request['system'] and '说话者' in request['system']
    else:
        seed = request['messages'][0]['content']
        assert 'selected_option_id=B' in seed and 'Selected fixture' in seed
        assert 'Unselected fixture' not in seed
        if stage == 'material_plan':
            assert 'visual_design' in request['system'] and '服装道具' in request['system']
        else:
            assert 'character_ids' in request['system'] and 'scene_id' in request['system']
            assert 'desk-01' in seed


@pytest.mark.parametrize('format', ['video', 'photo'])
def test_orchestrator_request_routes_missing_design_to_existing_material_role(visual_env, format):
    service, store, _ = visual_env
    context = service.context(RUN, 'orchestrator', format=format)
    system = ROLES['orchestrator'].system(store.read_run(RUN), extra=context['text'], format=format)
    assert '先 dispatch 素材统筹补齐，再更新脚本' in system
    assert '真正要发送的完整输入' in system
    assert '不增加新角色、阶段或用户逐项审批' in system
