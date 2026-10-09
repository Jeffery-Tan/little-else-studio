"""Offline full-script delivery gates against real temporary project files."""
import json

import pytest

from fakes import fixture_task_interpretation, final_turn, ScriptedModel, tool_turn
from studio.conversation_input import accept, receive, record_task_interpretation, start_execution
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store
from studio.task_contract import evaluate, writing_completeness
from studio.tools.dispatch import Dispatcher
from test_writing_task_route import RUN, seed, settings_for
from studio.ledger import Ledger


@pytest.fixture
def project(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic writing', 'Synthetic offline brief')
    pipe = Pipeline(store)
    seed(pipe)
    return store, pipe


def script(count):
    return {'segments': [{'tc': f'{n}–{n + 1}', 'picture': f'Synthetic action {n}',
                         'line': '', 'sound': '', 'duration': '1'} for n in range(count)],
            'total_duration': str(count), 'caption': 'Synthetic caption',
            'assumptions': [], 'open_questions': []}


def condition(store, text, *, value='4', quote=None, lifetime='turn', paths=None,
              polarity='require', intent='instruction', key='script.shot_count',
              operation='add', supersedes=(), prior=()):
    receipt = receive(store, RUN, text, f'count-{len(store.read_events(RUN))}')
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    proposal = fixture_task_interpretation(text)
    proposal['constraints'] = [{'quote': quote or text, 'key': key, 'value': value,
        'operation': operation, 'polarity': polarity, 'intent': intent,
        'scope': {'lifetime': lifetime, 'stages': ['writing'], 'paths': paths or [],
                  'quote': text if lifetime == 'project' else ''},
        'supersedes': list(supersedes), 'conflict_with': [], 'ambiguity': ''}]
    result = record_task_interpretation(store, RUN, receipt, proposal, prior_user_input_ids=prior)
    return receipt, result['constraints'][0]


def submit(pipe, data, **kwargs):
    attempt = pipe.start_attempt(RUN, 'writing')
    return pipe.submit(RUN, 'writing', attempt.token, content='', summary='Synthetic full draft',
                       structured=data, **kwargs)


@pytest.mark.parametrize('data', [{'segments': []}, {'caption': 'Only caption'}, {},
                                  {'segments': [{'tc': '0–3', 'line': 'No visual'}]}])
def test_empty_or_visual_missing_video_is_not_saved(project, data):
    store, pipe = project
    before = store.latest_artifact(RUN, 'writing')
    with pytest.raises(PipelineError) as err:
        submit(pipe, data)
    assert err.value.rule == 'writing_incomplete'
    assert store.latest_artifact(RUN, 'writing') == before
    assert pipe.stage_state(RUN, 'writing').open_attempt
    assert not (store.run_dir(RUN) / 'artifacts' / 'writing-v2.json').exists()


def test_prose_cannot_hide_explicit_empty_segments(project):
    store, pipe = project
    attempt = pipe.start_attempt(RUN, 'writing')
    with pytest.raises(PipelineError, match='非空镜头'):
        pipe.submit(RUN, 'writing', attempt.token, content='A prose draft', summary='Synthetic',
                    structured={'segments': []})
    assert store.latest_artifact(RUN, 'writing').version == 1


def test_nonempty_video_without_count_does_not_hardcode_a_number(project):
    store, pipe = project
    artifact = submit(pipe, script(3))
    assert len(artifact.structured['segments']) == 3
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'complete'


@pytest.mark.parametrize('text,value', [('完整稿做4个镜头', '4'), ('完整稿做四个镜头', '4'),
                                       ('Make 4 shots', '4'), ('完整稿做十六个镜头', '16'),
                                       ('15 秒、4 镜脚本', '4'), ('完整稿做4镜的脚本', '4')])
def test_exact_consumed_count_blocks_short_full_draft(project, text, value):
    store, pipe = project
    receipt, _ = condition(store, text, value=value)
    before = store.latest_artifact(RUN, 'writing')
    with pytest.raises(PipelineError, match=f'明确要求 {value} 个镜头'):
        submit(pipe, script(2), writing_source_message_ids=[receipt['message_id']])
    assert store.latest_artifact(RUN, 'writing') == before


def test_exact_count_is_preserved_as_source_identity_and_rechecked(project):
    store, pipe = project
    receipt, _ = condition(store, '本轮做4个镜头')
    artifact = submit(pipe, script(4), writing_source_message_ids=[receipt['message_id']])
    event = next(e for e in reversed(store.read_events(RUN)) if e['kind'] == 'attempt_submitted')
    assert event['writing_delivery'] == {'format': 'video', 'mode': 'full',
                                         'source_message_ids': [receipt['message_id']]}
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'complete'
    assert artifact.version == 2


@pytest.mark.parametrize('text,quote', [
    ('请写4镜头脚本且不要口播。', '4镜头'),
    ('不要口播，完整稿做4个镜头', '4个镜头'),
    ('完整稿做4镜脚本且不用宣称防水材质', '4镜'),
    ('不要改商品材质且完整稿做4个镜头', '4个镜头'),
    ('Make 4 shots and do not add voiceover.', '4 shots'),
    ('完整稿做4个镜头不要口播', '4个镜头'),
    ('Make 4 shots not using leather material', '4 shots'),
    ('写4镜头脚本，配乐可能用钢琴', '写4镜头脚本，配乐可能用钢琴'),
    ('材质可能不是皮革且完整稿做4个镜头', '4个镜头'),
    ('Write 4 shots and use piano music if available', 'Write 4 shots and use piano music if available'),
])
def test_unrelated_negative_condition_does_not_hide_exact_shot_count(project, text, quote):
    store, pipe = project
    receipt, _ = condition(store, text, quote=quote)
    result = writing_completeness(store, RUN, script(4), source_message_ids=[receipt['message_id']])
    assert result['expected_count'] == 4 and result['missing'] == []
    artifact = submit(pipe, script(4), writing_source_message_ids=[receipt['message_id']])
    assert artifact.version == 2


@pytest.mark.parametrize('text,quote', [
    ('改成6镜', '6镜'), ('把脚本改成六个镜头', '六个镜头'),
    ('本轮改为6个镜头', '6个镜头'), ('不要4镜，改成6镜', '改成6镜'),
    ('将镜头从4改成6镜', '6镜'),
    ('不要4镜，改成6镜', '不要4镜，改成6镜'),
    ('不做4个镜头，本轮改为六个镜头', '不做4个镜头，本轮改为六个镜头'),
])
def test_explicit_new_target_count_is_not_an_unresolved_change(project, text, quote):
    store, pipe = project
    receipt, _ = condition(store, text, quote=quote, value='6')
    result = writing_completeness(store, RUN, script(6), source_message_ids=[receipt['message_id']])
    assert result['expected_count'] == 6 and result['missing'] == []
    assert len(submit(pipe, script(6), writing_source_message_ids=[receipt['message_id']]).structured['segments']) == 6


@pytest.mark.parametrize('text,quote,value', [
    ('不要4镜', '4镜', '4'), ('不是4个镜头', '4个镜头', '4'),
    ('从4镜改6镜', '6镜', '6'), ('做4个方向，不是4镜', '4镜', '4'),
    ('至少4镜', '4镜', '4'), ('做4到6个镜头', '6个镜头', '6'),
    ('如果做4镜更好', '4镜', '4'), ('做4镜吗？', '4镜', '4'),
    ('做4镜', '4镜', '6'), ('做4个方向', '做4个方向', '4'),
    ('做到第4镜', '第4镜', '4'), ('做4.5镜', '4.5镜', '5'),
    ('做-4镜', '-4镜', '4'), ('做4组镜头', '4组镜头', '4'),
    ('做一百零二个镜头', '一百零二个镜头', '2'), ('做4镜面', '4镜面', '4'),
    ('不要写4镜脚本', '4镜脚本', '4'), ('4个镜头不要做', '4个镜头', '4'),
    ('不做4个镜头', '4个镜头', '4'),
    ('建议完整稿做4个镜头', '4个镜头', '4'), ('他说完整稿做4个镜头', '4个镜头', '4'),
    ('写4个镜头脚本可以吗？', '4个镜头', '4'),
    ('不用4镜而用6镜', '6镜', '6'), ('做4个镜头或者6个镜头', '6个镜头', '6'),
    ('做4镜，改成6镜', '做4镜，改成6镜', '6'),
    ('不要4镜，改成6镜或8镜', '不要4镜，改成6镜或8镜', '6'),
    ('Make no more than 4 shots', '4 shots', '4'),
    ('Do not make 4 shots', '4 shots', '4'), ('Make 4 shots instead of 6 shots', '4 shots', '4'),
])
def test_model_cannot_fabricate_exact_count_from_ambiguous_quote(project, text, quote, value):
    store, pipe = project
    receipt, _ = condition(store, text, value=value, quote=quote)
    result = writing_completeness(store, RUN, script(2), source_message_ids=[receipt['message_id']])
    assert result['expected_count'] is None
    assert any('无法与真实原话' in missing for missing in result['missing'])
    with pytest.raises(PipelineError, match='无法与真实原话'):
        submit(pipe, script(2), writing_source_message_ids=[receipt['message_id']])


@pytest.mark.parametrize('changes', [{'polarity': 'prefer'}, {'polarity': 'forbid'},
                                     {'intent': 'question'}, {'intent': 'hypothesis'},
                                     {'intent': 'reported'}, {'paths': [['segments', 0]]},
                                     {'key': 'script.direction_count'}])
def test_nonbinding_or_local_count_does_not_become_whole_script_quantity(project, changes):
    store, pipe = project
    receipt, _ = condition(store, '4个镜头', **changes)
    artifact = submit(pipe, script(2), writing_source_message_ids=[receipt['message_id']])
    assert len(artifact.structured['segments']) == 2
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'complete'


def test_unconsumed_turn_and_pending_future_count_are_not_revived(project):
    store, pipe = project
    condition(store, '本轮做4镜')  # Another turn was interpreted, but this task never consumed it.
    future = receive(store, RUN, '下一轮做8镜', 'future-count')
    accept(store, RUN, future)
    artifact = submit(pipe, script(2))
    assert len(artifact.structured['segments']) == 2
    with pytest.raises(PipelineError, match='已经执行'):
        writing_completeness(store, RUN, script(2), source_message_ids=[future['message_id']])


def test_project_count_release_and_replacement_use_existing_overlay(project):
    store, pipe = project
    old, row = condition(store, '全片做4镜', lifetime='project')
    changed, _ = condition(store, '全片做6镜', value='6', lifetime='project', operation='replace',
        supersedes=[row['id']], prior=[old['message_id']])
    result = writing_completeness(store, RUN, script(6), source_message_ids=[changed['message_id']])
    assert result['expected_count'] == 6 and result['missing'] == []
    released, _ = condition(store, '撤销全片镜头数量限制', value='6', lifetime='project', operation='release',
        supersedes=[next(r['constraint']['id'] for r in reversed(store.read_events(RUN))
                        if r['kind'] == 'user_constraint_recorded')],
        prior=[old['message_id'], changed['message_id']])
    assert writing_completeness(store, RUN, script(2), source_message_ids=[released['message_id']])['missing'] == []


def test_photo_text_plan_keeps_its_valid_contract(project):
    store, pipe = project
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'continue', 'format': 'photo'})
    attempt = pipe.start_attempt(RUN, 'writing')
    artifact = pipe.submit(RUN, 'writing', attempt.token, content='Synthetic full text photo planning',
        summary='Photo text draft', structured={'caption': 'Synthetic text planning', 'assumptions': ['No images yet']})
    assert 'caption' in artifact.structured and not artifact.structured.get('segments')
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'complete'


def test_legacy_prose_remains_readable_but_is_not_complete_video_delivery(project):
    store, pipe = project
    attempt = pipe.start_attempt(RUN, 'writing')
    artifact = pipe.submit(RUN, 'writing', attempt.token, content='Legacy prose', summary='Legacy import')
    assert store.read_artifact_text(RUN, 'writing', artifact.version) == 'Legacy prose'
    result = evaluate(store, pipe, RUN, 'writing')
    assert result['status'] == 'incomplete' and '非空镜头清单' in result['missing'][0]


def test_historical_empty_artifact_is_read_without_rewriting_or_false_completion(project):
    store, pipe = project
    prior = store.latest_artifact(RUN, 'writing')
    # Store is persistence, not the new full-script business submission gate.
    artifact = store.write_artifact(RUN, 'writing', 2, 'Historical empty full draft', summary='History',
        author_role='writer', attempt=prior.attempt, inputs=prior.inputs, structured={'segments': []})
    paths = list((store.run_dir(RUN) / 'artifacts').glob('writing-v2.*'))
    before = {path: path.read_bytes() for path in paths}
    assert store.read_artifact(RUN, 'writing', 2) == artifact
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'incomplete'
    assert all(path.read_bytes() == original for path, original in before.items())


def test_completion_gate_also_checks_known_count_on_legacy_short_saved_script(project):
    store, pipe = project
    receipt, _ = condition(store, '全片做4镜', lifetime='project')
    result = evaluate(store, pipe, RUN, 'writing')
    assert result['status'] == 'incomplete' and any('明确要求 4' in gap for gap in result['missing'])
    # This is a read-only gap, not metadata corruption or an automatic rewrite.
    assert store.latest_artifact(RUN, 'writing').version == 1


def test_exact_local_patch_is_not_treated_as_a_short_full_new_script(project):
    store, pipe = project
    receipt, _ = condition(store, '本轮做4镜')
    artifact = submit(pipe, script(2), writing_source_message_ids=[receipt['message_id']],
                      local_writing_revision=True)
    assert len(artifact.structured['segments']) == 2
    store.append_event(RUN, 'agent_revision_applied', plan_id='exact-local-plan', sha256=artifact.sha256)
    assert evaluate(store, pipe, RUN, 'revision', revision_scope={
        'plan_id': 'exact-local-plan', 'stage': 'writing'})['status'] == 'complete'


def test_dispatcher_does_not_replace_bound_user_count_with_model_brief(project, tmp_path, monkeypatch):
    store, pipe = project
    receipt, _ = condition(store, '本轮做4镜')
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'OFFLINE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'continue', 'format': 'video'})
    args = {'content': '', 'summary': 'Synthetic', 'structured': script(2), 'evidence': [], 'limitations': []}
    model = ScriptedModel([tool_turn('short', 'submit_stage', args), final_turn('Unable to deliver')])
    settings = settings_for(tmp_path, max_tool_rounds=2)
    result = Dispatcher(store, pipe, settings, model, Ledger(store, settings), RUN).dispatch(
        'writing', 'Model summary invents two shots', user_input_id=receipt['message_id'])
    assert result.submitted is None
    assert store.latest_artifact(RUN, 'writing').version == 1
    assert '明确要求 4' in json.dumps(model.calls[-1]['messages'], ensure_ascii=False)


def test_saved_contract_does_not_lose_turn_count_when_project_changes_format(project):
    store, pipe = project
    receipt, _ = condition(store, '做4镜')
    artifact = submit(pipe, script(4), writing_source_message_ids=[receipt['message_id']])
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'new', 'format': 'photo'})
    assert evaluate(store, pipe, RUN, 'writing')['status'] == 'complete'
    assert artifact.version == 2
