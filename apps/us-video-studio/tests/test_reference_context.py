"""Exact reference observations in actual agent requests; no providers or spend."""
import hashlib
import json

import pytest

from fakes import ScriptedModel, final_turn, tool_turn, fixture_task_interpretation
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import receive, accept, start_execution, record_task_interpretation
from studio.creation_mode import current, digest, select, context
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.tools.workspace import ToolError
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher
from test_server import RUN, settings_for

URL = 'https://www.tiktok.com/@fixture/video/7250000000000000000'


def seed(store, tag):
    name, raw, rid = tag + '.mp4', (tag + ' synthetic bytes').encode(), 'reference-' + tag
    accept_upload(store, RUN, name, raw)
    candidate = dict(name=name, sha256=hashlib.sha256(raw).hexdigest(),
        analysis_id='analysis-' + tag, reference_task_id=rid, purpose='reference',
        findings=[tag + '_SUMMARY'], limitations=['Synthetic observation'], reference_methods=[],
        timeline=[dict(start_seconds=0, end_seconds=2, visible=tag + '_EXCLUSIVE_TIMELINE',
                       audible=tag + '_EXCLUSIVE_AUDIO', relationship='Synthetic synchronous action')],
        audiovisual=dict(voice=tag + '_EXCLUSIVE_AUDIO'), observed_at='2026-10-04T00:00:00Z')
    store.append_event(RUN, 'reference_video_started', reference_id=rid,
                       request=dict(source_url=URL if tag == 'A' else URL + '1', intent='analyze', instruction=''))
    store.append_event(RUN, 'video_analysis_candidate', candidate=candidate)
    return dict(run_id=RUN, reference_id=rid, analysis_id=candidate['analysis_id'],
                candidate_sha256=digest(candidate))


def environment(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline context check', 'Synthetic product facts')
    return store, Pipeline(store), settings_for(tmp_path)


def test_historical_report_missing_segments_and_audio_does_not_claim_full_observation(tmp_path):
    store, _, _ = environment(tmp_path)
    seed(store, 'A')
    original = next(e['candidate'] for e in store.read_events(RUN) if e['kind'] == 'video_analysis_candidate')
    old_report = {key: value for key, value in original.items() if key not in {'timeline', 'audiovisual'}}
    store.append_event(RUN, 'video_analysis_candidate', candidate=old_report)
    select(store, RUN, 'inspiration', 'reference-A', 'Read saved report only')
    observation = context(store, RUN)['observation']
    assert observation['level'] == 'existence_only'
    assert 'timeline' not in observation and 'audiovisual' not in observation
    assert '不能宣称已完成完整声画观察' in observation['limitations'][-1]


def prompt_turn(tmp_path, *, prior=False, override=None, fresh=False):
    store, pipeline, settings = environment(tmp_path)
    target = None if fresh else seed(store, 'A')
    b = seed(store, 'B')
    before = select(store, RUN, 'inspiration', b['reference_id'], 'Only B opening') if prior else None
    text = '请适配我选中的参考，完成本品提示词 ' + URL
    proposal = fixture_task_interpretation(text, deliverable='reference_prompt')
    if fresh:
        proposal['video_use'] = dict(source=URL, purpose='reference', quote=text)
    route = dict(intent='new', format='video', deliverable='reference_prompt')
    if override == 'B':
        route.update(creation_mode='inspiration', reference_id=b['reference_id'], reference_scope='Only B')
    elif override == 'autonomous':
        route['creation_mode'] = 'autonomous'
    model = ScriptedModel([
        tool_turn('observe', 'observe_reference', dict(source=URL, action='prompt', quote=text,
            scope='Opening and evidence sequence', task_interpretation=proposal)),
        tool_turn('prepare', 'prepare_creative_context', route),
        tool_turn('save', 'save_reference_prompt', dict(prompt='Synthetic adapted product prompt')),
        final_turn('Cannot change the selected reference'),
    ], legacy_task_contracts=False)
    calls = []
    def observer(*args):
        calls.append(args[0])
        saved = seed(store, 'A')
        return dict(reference_id=saved['reference_id'], report='A summary report')
    receipt = receive(store, RUN, text, 'prompt-input', reference_target=target, reference_consent=fresh)
    result = Orchestrator(store, pipeline, settings, model, Ledger(store, settings), RUN,
                          reference_observer=observer).send(text, user_input_id=receipt['message_id'])
    return store, model, result, before, calls


@pytest.mark.parametrize('prior', [False, True])
@pytest.mark.parametrize('fresh', [False, True])
def test_prompt_requests_receive_full_selected_observation_without_changing_project_mode(tmp_path, prior, fresh):
    store, model, result, before, calls = prompt_turn(tmp_path, prior=prior, fresh=fresh)
    assert result.deliverable['status'] == 'complete'
    assert calls == ([URL] if fresh else [])
    assert current(store, RUN) == before
    actual = model.requests_text()
    assert 'A_EXCLUSIVE_TIMELINE' in actual and 'A_EXCLUSIVE_AUDIO' in actual
    assert 'B_EXCLUSIVE_TIMELINE' not in actual and 'B_EXCLUSIVE_AUDIO' not in actual
    saved = [event for event in store.read_events(RUN) if event['kind'] == 'reference_prompt_saved']
    assert saved[-1]['reference_id'] == 'reference-A'


@pytest.mark.parametrize('override', ['B', 'autonomous'])
def test_model_cannot_change_exact_reference_after_observation_or_save_a_mislabeled_prompt(tmp_path, override):
    store, model, result, before, _ = prompt_turn(tmp_path, prior=True, override=override)
    assert result.deliverable['status'] == 'incomplete'
    assert current(store, RUN) == before
    assert not any(e['kind'] == 'reference_prompt_saved' for e in store.read_events(RUN))
    assert '不能改为自主创作或换成其他参考' in model.requests_text()
    assert 'B_EXCLUSIVE_TIMELINE' not in model.requests_text()


def test_analysis_dependency_replaces_prior_source_for_the_actual_final_intake(tmp_path):
    store, pipeline, settings = environment(tmp_path)
    a, b = seed(store, 'A'), seed(store, 'B')
    select(store, RUN, 'inspiration', b['reference_id'], 'Only B opening')
    text = '先拆解所选参考，再完成本品需求方案'
    proposal = fixture_task_interpretation(text, deliverable='intake')
    model = ScriptedModel([
        tool_turn('observe', 'observe_reference', dict(source=URL, action='analyze', quote=text,
            scope='Opening expression', task_interpretation=proposal)),
        tool_turn('prepare', 'prepare_creative_context', dict(intent='new', format='video', deliverable='intake')),
        tool_turn('intake', 'write_stage', dict(stage='intake', content='Synthetic scheme based on A', summary='Synthetic')),
        final_turn('Original intake completed'),
    ], legacy_task_contracts=False)
    receipt = receive(store, RUN, text, 'intake-input', reference_target=a)
    result = Orchestrator(store, pipeline, settings, model, Ledger(store, settings), RUN).send(
        text, user_input_id=receipt['message_id'])
    assert result.deliverable['goal'] == 'intake' and result.deliverable['status'] == 'complete'
    assert current(store, RUN)['reference_id'] == 'reference-A'
    assert 'A_EXCLUSIVE_TIMELINE' in model.requests_text() and 'A_EXCLUSIVE_AUDIO' in model.requests_text()
    assert 'B_EXCLUSIVE_TIMELINE' not in model.requests_text()


def test_recreate_cannot_switch_reference_during_later_preparation(tmp_path):
    store, pipeline, settings = environment(tmp_path)
    a, b = seed(store, 'A'), seed(store, 'B')
    text = '按我选中的 A 参考完成本品需求方案'
    model = ScriptedModel([
        tool_turn('observe', 'observe_reference', dict(source=URL, action='recreate', quote=text,
            scope='Opening expression', task_interpretation=fixture_task_interpretation(text, deliverable='intake'))),
        tool_turn('switch', 'prepare_creative_context', dict(intent='new', format='video', deliverable='intake',
            creation_mode='inspiration', reference_id=b['reference_id'], reference_scope='Only B')),
        final_turn('Source switch rejected'),
    ], legacy_task_contracts=False)
    receipt = receive(store, RUN, text, 'recreate-input', reference_target=a)
    result = Orchestrator(store, pipeline, settings, model, Ledger(store, settings), RUN).send(
        text, user_input_id=receipt['message_id'])
    assert result.deliverable['status'] == 'incomplete'
    assert current(store, RUN)['reference_id'] == 'reference-A'
    assert not store.latest_artifact(RUN, 'intake')
    assert '不能改为自主创作或换成其他参考' in model.requests_text()
    assert 'B_EXCLUSIVE_TIMELINE' not in model.requests_text()


def test_new_reference_dependency_cannot_be_skipped_by_preparing_with_an_old_reference(tmp_path):
    store, pipeline, settings = environment(tmp_path)
    b = seed(store, 'B')
    before = select(store, RUN, 'inspiration', b['reference_id'], 'Only B opening')
    text = '请先拆解我提供的新参考，再完成商品需求方案 ' + URL
    proposal = fixture_task_interpretation(text, deliverable='intake')
    proposal['video_use'] = dict(source=URL, purpose='reference', quote=text)
    model = ScriptedModel([
        tool_turn('skip', 'prepare_creative_context', dict(intent='new', format='video', deliverable='intake',
            task_interpretation=proposal, creation_mode='inspiration', reference_id=b['reference_id'], reference_scope='Only B')),
        tool_turn('write', 'write_stage', dict(stage='intake', content='Synthetic scheme using old B', summary='Synthetic')),
        final_turn('Skipped dependency'),
    ], legacy_task_contracts=False)
    receipt = receive(store, RUN, text, 'new-reference-input', reference_consent=True)
    result = Orchestrator(store, pipeline, settings, model, Ledger(store, settings), RUN).send(
        text, user_input_id=receipt['message_id'])
    assert result.deliverable['status'] == 'incomplete'
    assert not store.latest_artifact(RUN, 'intake')
    assert current(store, RUN) == before
    assert '先读取指定参考' in model.requests_text()
    assert 'B_EXCLUSIVE_TIMELINE' not in model.requests_text()


@pytest.mark.parametrize('switched', [False, True])
def test_director_actual_input_uses_frozen_reference_or_rejects_changed_project_source(tmp_path, switched):
    store, pipeline, settings = environment(tmp_path)
    seed(store, 'A'); seed(store, 'B')
    reference = select(store, RUN, 'recreate', 'reference-A', 'Opening expression')
    store.append_event(RUN, 'creative_context_selected',
                       selection=dict(intent='new', format='video', tags=[], case_ids=None))
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='Synthetic product facts', summary='Synthetic')
    if switched:
        select(store, RUN, 'inspiration', 'reference-B', 'Only B opening')
    model = ScriptedModel([final_turn('Offline director')], legacy_task_contracts=False)
    dispatcher = Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN)
    if switched:
        with pytest.raises(ToolError, match='绑定的参考与当前创作依据不一致'):
            dispatcher.dispatch('direction', 'Propose directions', turn_scope={'reference_selection': reference})
        assert not model.calls
    else:
        original = receive(store, RUN, '请按参考给出商品创意方向', 'original-role-input')
        accept(store, RUN, original); start_execution(store, RUN, original)
        record_task_interpretation(store, RUN, original,
                                   fixture_task_interpretation(original['text'], deliverable='direction'))
        continuation = receive(store, RUN, '继续；本次补充：开场不要宣称自己亲身用过商品', 'role-recovery-input')
        accept(store, RUN, continuation); start_execution(store, RUN, continuation)
        dispatcher.dispatch('direction', 'Propose directions', user_input_id=original['message_id'],
            turn_scope={'reference_selection': reference,
                        'reference_recovery': {'recovery_message_id': continuation['message_id']}})
        actual = json.dumps(model.calls, ensure_ascii=False)
        assert 'A_EXCLUSIVE_TIMELINE' in actual and 'A_EXCLUSIVE_AUDIO' in actual
        assert 'B_EXCLUSIVE_TIMELINE' not in actual
        assert original['text'] in actual and continuation['text'] in actual
