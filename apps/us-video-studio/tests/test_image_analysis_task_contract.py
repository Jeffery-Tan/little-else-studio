"""Read-only image task receipts, actual synthetic pixels and fake transports."""
import hashlib
import json

import pytest

from fakes import final_turn, tool_turn
from studio import conversation_input
from studio.agent_images import (
    ImagePreflightError, analysis_coverage, bind_image_analysis, observe, saved_observations,
)
from studio.agents.client import JobCancelled
from studio.ledger import Ledger
from studio.materials import accept_upload, resolve
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.project_brief import content_format
from studio.task_contract import evaluate
from studio.tools.registry import REGISTRY, tools_for
from test_agent_images import answering_observer, call, result, tool_result_for
from test_bailian_omni import BASE, native
from test_image_analysis import scene
from test_natural_reference import chat, scripted
from test_server import RUN


TEXT = '这次只检查商品图片。请实际查看这六张图，用中文总结可见外观和结构，以及目前不能确认的信息。不要出创意方向、脚本或生成图片/视频。'


def proposal(text=TEXT, **task):
    return {'task': {'quote': text, 'mode': 'analyze', 'deliverable': 'analysis',
                     'format': 'photo', 'allowed_stages': [], 'pause_after': False, **task},
            'constraints': []}


def six(native):
    for number in range(2, 7):
        accept_upload(native[1], RUN, f'product-{number}.png', native[4])
    return [dict(name=name, purpose='product') for name in
            ['fixture.png', *[f'product-{n}.png' for n in range(2, 7)]]]


def executing(store, *, text=TEXT, request='image-analysis', declared=None, attachments=None):
    receipt = conversation_input.receive(store, RUN, text, request, attachments=attachments)
    conversation_input.accept(store, RUN, receipt)
    conversation_input.start_execution(store, RUN, receipt)
    interpretation = conversation_input.record_task_interpretation(
        store, RUN, receipt, declared or proposal(text))
    return receipt, interpretation


def status(store, source, turn='image-turn', **kwargs):
    return evaluate(store, Pipeline(store), RUN, 'analysis', turn_id=turn,
                    analysis_source_message_id=source['message_id'], **kwargs)


@pytest.mark.parametrize('scene', [12], indirect=True)
@pytest.mark.parametrize('use_prepare', [False, True])
def test_real_image_only_job_does_not_switch_video_project_or_create_decisions(native, monkeypatch, use_prepare):
    items = six(native)
    store = native[1]
    store.append_event(RUN, 'creative_context_selected', selection={'intent': 'new', 'format': 'video'})
    before = store.read_events(RUN)
    route = tool_turn('route', 'prepare_creative_context', {
        'intent': 'new', 'format': 'photo', 'deliverable': 'analysis', 'task_interpretation': proposal()})
    look = tool_turn('look', 'observe_images', {'images': items,
        **({} if use_prepare else {'task_interpretation': proposal()})})
    scripted(native, monkeypatch, ([route] if use_prepare else []) + [look, result(6), final_turn('六张图片均已查看，性能仍不能确认。')])
    job = chat(native, TEXT)
    assert job['status'] == 'succeeded', job
    events = store.read_events(RUN)
    assert content_format(events) == 'video'
    assert sum(e['kind'] == 'creative_context_selected' for e in events) == 1
    assert len(saved_observations(store, RUN)) == 6
    assert not any(e['kind'] in {'agent_task_prepared', 'approval_recorded'} for e in events[len(before):])
    assert not store.list_approvals(RUN)
    assert all(store.latest_artifact(RUN, stage) is None for stage in ('intake', 'direction', 'material_plan', 'writing'))
    assert not tool_result_for(native[2], 'look')['is_error']
    scope = next(e for e in events if e['kind'] == 'agent_turn_scope_started')
    source = conversation_input.message(store, RUN, scope['user_input_id'])
    delivery = status(store, source, turn=scope['turn_id'], analysis_completed=False)
    assert delivery['status'] == 'complete' and '固定 6 张' in delivery['note']
    assert len([r for r in Ledger(store, native[5]).entries(RUN) if r.stage == 'image_analysis']) == 1


def test_whole_set_cannot_be_reduced_by_selecting_three_or_proposing_three(native):
    items = six(native)
    receipt, interpretation = executing(native[1])
    with pytest.raises(ImagePreflightError, match='全部固定目标'):
        bind_image_analysis(native[1], RUN, receipt, interpretation, items[:3], turn_id='image-turn')
    assert not native[2].calls
    assert not any(e['kind'] == 'agent_image_analysis_requested' for e in native[1].read_events(RUN))
    receipt2 = conversation_input.receive(native[1], RUN, TEXT, 'smaller-scope')
    conversation_input.accept(native[1], RUN, receipt2)
    conversation_input.start_execution(native[1], RUN, receipt2)
    declared = proposal()
    declared['image_use'] = {'quote': TEXT, 'images': [{**i, 'sha256': hashlib.sha256(native[4]).hexdigest()} for i in items[:3]]}
    with pytest.raises(ImagePreflightError, match='缩小本轮整组'):
        conversation_input.record_task_interpretation(native[1], RUN, receipt2, declared)
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'incomplete'


def test_partial_fourth_failure_keeps_three_and_actual_coverage_then_explicit_reuse(native, monkeypatch):
    items = six(native)
    store = native[1]
    receipt, interpretation = executing(store)
    frozen = bind_image_analysis(store, RUN, receipt, interpretation, items, turn_id='image-turn')
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch, fail_batch=4)
    with pytest.raises(PipelineError, match='此前已保存 3 张'):
        call(native, frozen)
    assert len(native[2].calls) == 4 and len(saved_observations(store, RUN)) == 3
    delivery = status(store, receipt, analysis_completed=True)
    assert delivery['status'] == 'incomplete' and len(delivery['missing']) == 3
    assert '固定 6 张图片，已核对 3 张' in delivery['note']
    assert all('参考分析报告' not in note for note in delivery['missing'])
    # Cached rows alone do not complete a new task without an explicit invocation.
    receipt2, interpretation2 = executing(store, request='explicit-recheck')
    declared_coverage = status(store, receipt2, turn='next-turn', analysis_completed=True)
    assert declared_coverage['status'] == 'incomplete'
    frozen2 = bind_image_analysis(store, RUN, receipt2, interpretation2, items, turn_id='next-turn')
    answering_observer(native, monkeypatch)
    observed = call(native, frozen2)
    assert observed['reused'] == 3 and len(native[2].calls) == 7
    assert status(store, receipt2, turn='next-turn')['status'] == 'complete'
    receipt3, interpretation3 = executing(store, request='cached-recheck')
    frozen3 = bind_image_analysis(store, RUN, receipt3, interpretation3, items, turn_id='cache-turn')
    assert call(native, frozen3)['reused'] == 6 and len(native[2].calls) == 7
    assert status(store, receipt3, turn='cache-turn')['status'] == 'complete'


def test_exact_attachment_subset_and_purpose_are_frozen(native):
    six(native)
    source, interpretation = executing(native[1], text='只看本条附上的图片',
        attachments=[{'name': 'fixture.png', 'purpose': 'product'}])
    frozen = bind_image_analysis(native[1], RUN, source, interpretation,
        [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    assert len(frozen) == 1 and frozen[0]['sha256'] == hashlib.sha256(native[4]).hexdigest()
    with pytest.raises(ImagePreflightError, match='改用途'):
        bind_image_analysis(native[1], RUN, source, interpretation,
            [dict(name='fixture.png', purpose='reference')], turn_id='image-turn')
    assert not native[2].calls


def test_explicit_named_subset_preserves_original_task_and_hash_changes_block(native):
    items = six(native)
    text = '只看 fixture.png 的外观，不创作。'
    declared = proposal(text)
    declared['image_use'] = {'quote': text, 'images': [
        {**items[0], 'sha256': hashlib.sha256(native[4]).hexdigest()}]}
    receipt, interpretation = executing(native[1], text=text, declared=declared)
    frozen = bind_image_analysis(native[1], RUN, receipt, interpretation, items[:1], turn_id='image-turn')
    native[2].turn = result()
    call(native, frozen)
    assert status(native[1], receipt)['status'] == 'complete'
    original_task = conversation_input.interpretation_for(native[1], RUN, receipt['message_id'])['task']
    resolve(native[1], RUN, 'fixture.png').write_bytes(b'changed source')
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'incomplete'
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(native[1], RUN, receipt, interpretation, items[:1], turn_id='image-turn')
    with pytest.raises(ImagePreflightError, match='指纹已改变'):
        call(native, frozen)
    assert len(native[2].calls) == 1
    assert conversation_input.interpretation_for(native[1], RUN, receipt['message_id'])['task'] == original_task


@pytest.mark.parametrize('mode', ['status', 'discuss', 'stop', 'clarify'])
def test_prior_images_do_not_grant_new_readonly_status_or_pause_observation(native, mode):
    native[2].turn = result()
    call(native, [dict(name='fixture.png', purpose='product')])
    receipt, interpretation = executing(native[1], declared=proposal(mode=mode, deliverable='follow_up'))
    with pytest.raises(ImagePreflightError, match='状态、讨论、暂停或视频'):
        bind_image_analysis(native[1], RUN, receipt, interpretation,
                            [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    assert len(native[2].calls) == 1 and len(saved_observations(native[1], RUN)) == 1


def test_video_binding_with_photo_output_is_not_an_image_analysis(native, monkeypatch):
    # True video admission remains the video source authority; a future photo
    # output declaration cannot cause image completion or observation.
    from test_bailian_omni import video_bytes
    accept_upload(native[1], RUN, 'source.mp4', video_bytes())
    text = '只拆解 source.mp4 的表达方式'
    declared = proposal(text)
    declared['video_use'] = {'source': 'source.mp4', 'purpose': 'reference', 'quote': text}
    receipt, interpretation = executing(native[1], text=text, declared=declared)
    assert analysis_coverage(native[1], RUN, receipt['message_id'], 'image-turn') is None
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'complete'
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(native[1], RUN, receipt, interpretation,
                            [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    assert not native[2].calls


def test_actual_video_attachment_with_photo_declaration_keeps_original_video_completion(native):
    from test_bailian_omni import video_bytes
    accept_upload(native[1], RUN, 'source.mp4', video_bytes())
    text = '只拆解本条附上的参考片'
    receipt, interpretation = executing(native[1], text=text,
        attachments=[{'name': 'source.mp4', 'purpose': 'reference'}])
    assert not interpretation.get('video_use')
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'complete'
    assert status(native[1], receipt, analysis_completed=False)['missing'] == ['已保存的参考分析报告']
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(native[1], RUN, receipt, interpretation,
                            [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    assert not native[2].calls


def test_bound_image_task_cannot_add_video_use_or_video_observation(native):
    from test_bailian_omni import video_bytes
    accept_upload(native[1], RUN, 'source.mp4', video_bytes())
    receipt, interpretation = executing(native[1])
    bind_image_analysis(native[1], RUN, receipt, interpretation,
                        [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    bound = conversation_input.interpretation_for(native[1], RUN, receipt['message_id'])
    changed = proposal()
    changed['video_use'] = {'source': 'source.mp4', 'purpose': 'reference', 'quote': TEXT}
    with pytest.raises(PipelineError, match='解释已经绑定'):
        conversation_input.record_task_interpretation(native[1], RUN, receipt, changed)
    with pytest.raises(PipelineError, match='固定为只读图片'):
        conversation_input.observation_admission(native[1], RUN, receipt, bound,
                                                 'source.mp4', 'reference', TEXT)
    assert not native[2].calls


def test_missing_frozen_source_is_incomplete_and_cannot_call_provider(native):
    receipt, interpretation = executing(native[1], text='只检查 fixture.png')
    frozen = bind_image_analysis(native[1], RUN, receipt, interpretation,
        [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    resolve(native[1], RUN, 'fixture.png').unlink()  # This test's own synthetic file.
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'incomplete'
    with pytest.raises(ImagePreflightError):
        call(native, frozen)
    assert not native[2].calls


def test_cancellation_after_three_stops_remaining_sources_and_keeps_exact_coverage(native, monkeypatch):
    items = six(native)
    receipt, interpretation = executing(native[1])
    frozen = bind_image_analysis(native[1], RUN, receipt, interpretation, items, turn_id='image-turn')
    monkeypatch.setattr('studio.agents.bailian_omni.MAX_INLINE_BYTES', len(native[4]) * 2 - 1)
    answering_observer(native, monkeypatch)
    def check_cancel():
        if len(saved_observations(native[1], RUN)) == 3:
            raise JobCancelled('Synthetic user stop')
    with pytest.raises(JobCancelled):
        observe(native[1], native[5], RUN, frozen, endpoint=BASE, emit=lambda _: None,
                check_cancel=check_cancel, client_factory=lambda: native[2])
    assert len(native[2].calls) == 3 and len(saved_observations(native[1], RUN)) == 3
    coverage = analysis_coverage(native[1], RUN, receipt['message_id'], 'image-turn')
    assert coverage['observed'] == 3 and len(coverage['missing']) == 3
    assert status(native[1], receipt, analysis_completed=True)['status'] == 'incomplete'
    assert len(Ledger(native[1], native[5]).entries(RUN)) == 3


def test_a_queued_receipt_cannot_borrow_the_previous_interpretation(native):
    _, interpretation = executing(native[1])
    queued = conversation_input.receive(native[1], RUN, TEXT, 'not-executing')
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(native[1], RUN, queued, interpretation,
            [dict(name='fixture.png', purpose='product')], turn_id='image-turn')
    assert not native[2].calls
    assert not any(e['kind'] == 'user_image_use_bound' for e in native[1].read_events(RUN))


@pytest.mark.parametrize('data', [
    {'SECRET_UNKNOWN_KEY': 'PRIVATE_RESPONSE'},
    {'images': [{'id': 'PRIVATE_WRONG_ID', 'findings': 'PRIVATE_FINDINGS', 'limitations': []}]},
    {'images': [{'id': 'image-1', 'findings': ['PRIVATE_FINDINGS'], 'limitations': []}]},
    ['PRIVATE_ARRAY'],
])
def test_invalid_result_diagnostic_records_structure_only_and_usage_without_content(native, data):
    turn = result()
    turn.text = json.dumps(data)
    native[2].turn = turn
    with pytest.raises(PipelineError, match='未返回完整结果'):
        call(native, [dict(name='fixture.png', purpose='product')])
    event = next(e for e in native[1].read_events(RUN) if e['kind'] == 'agent_image_result_invalid')
    diagnostic = event['diagnostic']
    assert diagnostic['body_sha256'] == hashlib.sha256(turn.text.encode()).hexdigest()
    assert diagnostic['body_bytes'] == len(turn.text.encode()) and diagnostic['json_valid'] is True
    serialized = json.dumps(event)
    assert not any(secret in serialized for secret in ('PRIVATE_RESPONSE', 'PRIVATE_WRONG_ID', 'PRIVATE_FINDINGS', 'SECRET_UNKNOWN_KEY', 'PRIVATE_ARRAY'))
    entry = Ledger(native[1], native[5]).entries(RUN)[0]
    assert event['call_id'] == entry.call_id and entry.usage_status == 'known'
    assert len(native[2].calls) == 1 and not saved_observations(native[1], RUN)


def test_image_use_is_optional_for_producer_and_has_exact_strict_writer_absence_branch():
    schema = REGISTRY['propose_task_interpretation'].input_schema
    assert 'image_use' not in schema['required']
    assert 'task_interpretation' not in REGISTRY['observe_images'].input_schema['required']
    writer = next(t for t in tools_for('writer') if t['name'] == 'propose_task_interpretation')
    assert writer['input_schema']['properties']['image_use']['anyOf'][1] == {
        'type': 'string', 'enum': ['not_applicable']}
