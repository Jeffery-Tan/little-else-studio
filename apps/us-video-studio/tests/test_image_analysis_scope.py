"""Interpretation rejects incompatible image scope before it becomes immutable."""
from copy import deepcopy
import hashlib
import json

import pytest

from fakes import final_turn, tool_turn
from studio import conversation_input
from studio.agent_images import ImagePreflightError, bind_image_analysis, observe, saved_observations
from studio.ledger import Ledger
from studio.models import PipelineError, STAGES
from studio.project_integrity import ProjectDataError
from test_agent_images import tool_result_for
from test_bailian_omni import native
from test_deepseek_client import transport
from test_image_analysis import scene
from test_image_analysis_task_contract import proposal, six
from test_image_understanding import configure, frozen, answer, observer_reply
from test_natural_reference import chat, scripted
from test_server import RUN


TEXT = '只检查本项目商品图片的可见内容和未知信息，不出方向、脚本或生成媒体。'


def source(store, *, text=TEXT, request='interpret-image-scope'):
    receipt = conversation_input.receive(store, RUN, text, request)
    conversation_input.accept(store, RUN, receipt)
    conversation_input.start_execution(store, RUN, receipt)
    return receipt


@pytest.mark.parametrize('format', ['photo', 'none'])
@pytest.mark.parametrize('stage', STAGES)
def test_nonempty_image_analysis_stages_reject_before_binding_and_same_input_can_correct(native, format, stage):
    store = native[1]
    receipt = source(store)
    declared = proposal(TEXT, format=format, allowed_stages=[stage])
    unchanged = deepcopy(declared)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match=r'allowed_stages 必须为 \[\]'):
        conversation_input.record_task_interpretation(store, RUN, receipt, declared)
    assert declared == unchanged and store.read_events(RUN) == before
    assert conversation_input.interpretation_for(store, RUN, receipt['message_id']) is None
    corrected = proposal(TEXT, format=format)
    bound = conversation_input.record_task_interpretation(store, RUN, receipt, corrected)
    assert bound['source_message_id'] == receipt['message_id']
    assert bound['task'] == corrected['task'] and not bound['task']['allowed_stages']
    again = conversation_input.record_task_interpretation(store, RUN, receipt, corrected)
    assert again['id'] == bound['id']
    assert sum(e['kind'] == 'user_task_interpretation_recorded' for e in store.read_events(RUN)) == 1
    targets = bind_image_analysis(store, RUN, receipt, bound,
        [{'name': 'fixture.png', 'purpose': 'product'}], turn_id='scope-turn')
    assert targets[0]['sha256'] == hashlib.sha256(native[4]).hexdigest()
    assert not native[2].calls


def test_existing_bad_historical_binding_is_not_rewritten_or_silently_cleared(native):
    store = native[1]
    receipt = source(store)
    bad = proposal(TEXT, allowed_stages=['intake'])
    historical = {'id': 'synthetic-old-binding', 'source_message_id': receipt['message_id'],
                  'proposal_sha256': conversation_input._digest(bad),
                  'task': deepcopy(bad['task']), 'constraints': []}
    store.append_event(RUN, 'user_task_interpretation_recorded',
                       source_message_id=receipt['message_id'], interpretation=historical)
    before = store.read_events(RUN)
    with pytest.raises(PipelineError, match='解释已经绑定'):
        conversation_input.record_task_interpretation(store, RUN, receipt, proposal(TEXT))
    assert conversation_input.interpretation_for(store, RUN, receipt['message_id']) == historical
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(store, RUN, receipt, historical,
            [{'name': 'fixture.png', 'purpose': 'product'}], turn_id='scope-turn')
    assert store.read_events(RUN) == before and not native[2].calls


def test_unknown_source_still_rejects_before_binding_and_remains_correctable(native):
    store = native[1]
    receipt = source(store)
    bad = proposal(TEXT)
    bad['image_use'] = {'quote': TEXT, 'images': [
        {'name': 'unknown.png', 'sha256': 'f' * 64, 'purpose': 'product'}]}
    before = store.read_events(RUN)
    with pytest.raises(ImagePreflightError, match='本项目'):
        conversation_input.record_task_interpretation(store, RUN, receipt, bad)
    assert conversation_input.interpretation_for(store, RUN, receipt['message_id']) is None
    assert store.read_events(RUN) == before
    bound = conversation_input.record_task_interpretation(store, RUN, receipt, proposal(TEXT))
    with pytest.raises(ImagePreflightError, match='本项目'):
        bind_image_analysis(store, RUN, receipt, bound,
            [{'name': 'unknown.png', 'purpose': 'product'}], turn_id='scope-turn')
    assert not native[2].calls
    assert not any(e['kind'] == 'agent_image_analysis_requested' for e in store.read_events(RUN))


@pytest.mark.parametrize('mode', ['stop', 'status', 'discuss', 'clarify'])
def test_no_creative_stages_does_not_grant_observation_to_other_readonly_modes(native, mode):
    store = native[1]
    receipt = source(store)
    bound = conversation_input.record_task_interpretation(store, RUN, receipt,
        proposal(TEXT, mode=mode, deliverable='follow_up'))
    with pytest.raises(ImagePreflightError):
        bind_image_analysis(store, RUN, receipt, bound,
            [{'name': 'fixture.png', 'purpose': 'product'}], turn_id='scope-turn')
    assert not native[2].calls
    assert not any(e['kind'] == 'agent_image_analysis_requested' for e in store.read_events(RUN))


def test_corrected_shape_does_not_bypass_damaged_project_write_guard(native):
    store = native[1]
    receipt = source(store)
    path = store.run_dir(RUN) / 'run.json'
    damaged = b'{"synthetic_bad_record":'
    path.write_bytes(damaged)
    with pytest.raises(ProjectDataError):
        conversation_input.record_task_interpretation(store, RUN, receipt, proposal(TEXT))
    assert conversation_input.interpretation_for(store, RUN, receipt['message_id']) is None
    assert path.read_bytes() == damaged and not native[2].calls


@pytest.mark.parametrize('scene', [12], indirect=True)
def test_actual_handler_corrects_bad_shape_then_reuses_six_mock_http_reports_without_visual_call(scene, monkeypatch):
    items = six(scene)
    store = scene[1]
    connections, _ = configure(scene)
    route = frozen(scene, connections)
    seen = transport(monkeypatch, [observer_reply(answer(first=i), route[1].progress_stream)
                                   for i in range(1, 7)])
    observed = observe(store, scene[5], RUN, items, endpoint=None, emit=lambda _: None,
                       check_cancel=lambda: None, freeze_route=lambda: route)
    assert len(observed['observations']) == len(saved_observations(store, RUN)) == len(seen) == 6
    before_visual = [e for e in Ledger(store, scene[5]).entries(RUN) if e.stage == 'image_analysis']
    assert len(before_visual) == 6
    text = '只复用本项目已经保存的六张商品图片观察，用中文总结可见结构和未知项，不重新调用视觉模型，不创作。'
    scripted(scene, monkeypatch, [
        tool_turn('bad-scope', 'propose_task_interpretation', proposal(text, allowed_stages=['intake'])),
        tool_turn('correct-scope', 'propose_task_interpretation', proposal(text)),
        tool_turn('cached-look', 'observe_images', {'images': items}),
        final_turn('六张已保存观察均已核对，仅汇总可见内容，性能仍不能确认。'),
    ])
    job = chat(scene, text, request_id='corrected-cache-only-scenario')
    assert job['status'] == 'succeeded', job
    assert job['deliverable']['status'] == 'complete' and job['deliverable']['goal'] == 'analysis'
    failed = tool_result_for(scene[2], 'bad-scope')
    assert failed['is_error'] and 'allowed_stages 必须为 []' in failed['content']
    assert not tool_result_for(scene[2], 'correct-scope')['is_error']
    cached = tool_result_for(scene[2], 'cached-look')
    assert not cached['is_error']
    payload = json.loads(cached['content'].split('\n', 1)[0])
    assert payload['reused'] == len(payload['observations']) == 6
    interpretations = [e for e in store.read_events(RUN) if e['kind'] == 'user_task_interpretation_recorded']
    assert len(interpretations) == 1
    assert interpretations[0]['interpretation']['task'] == proposal(text)['task']
    assert len(seen) == 6  # Only the initial MockHTTP visual reports crossed transport.
    after_visual = [e for e in Ledger(store, scene[5]).entries(RUN) if e.stage == 'image_analysis']
    assert after_visual == before_visual
    assert len(scene[2].calls) == 4 and len(saved_observations(store, RUN)) == 6
    assert not store.list_approvals(RUN)
    assert all(store.latest_artifact(RUN, stage) is None for stage in STAGES)
