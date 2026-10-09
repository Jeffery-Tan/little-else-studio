"""Missing video metadata can be completed without rewriting a frozen task."""
import copy
import hashlib

import pytest

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import (accept, interpretation_for, observation_admission,
    receive, record_task_interpretation, start_execution)
from studio.interactions import answer_from_receipt, ask, record_answer
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.store import Store
from test_reference_admission import contract, URL
from test_server import RUN, settings_for

TEXT = '当参考片拆解：拆它的表达方式'


def scene(tmp_path, *, text=TEXT, attachments=None, mode=None):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic reference', URL)
    accept_upload(store, RUN, 'reference.mp4', b'synthetic source')
    receipt = receive(store, RUN, text, 'purpose-source', reference_consent=True,
                      attachments=attachments)
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    proposal = contract(text)
    if mode:
        proposal['task']['mode'] = mode
    before = record_task_interpretation(store, RUN, receipt, proposal)
    return store, receipt, proposal, before


def supplement(proposal):
    result = copy.deepcopy(proposal)
    result['video_use'] = {'source': 'reference.mp4', 'purpose': 'reference', 'quote': TEXT}
    return result


def test_missing_video_use_is_filled_once_without_rewriting_original(tmp_path):
    store, receipt, proposal, before = scene(tmp_path)
    existing = copy.deepcopy(store.read_events(RUN))
    filled = record_task_interpretation(store, RUN, receipt, supplement(proposal))
    assert filled['id'] == before['id'] and filled['task'] == before['task']
    assert filled['proposal_sha256'] == before['proposal_sha256']
    assert filled['video_use']['source'] == 'reference.mp4'
    assert store.read_events(RUN)[:len(existing)] == existing
    count = len(store.read_events(RUN))
    assert record_task_interpretation(store, RUN, receipt, supplement(proposal)) == filled
    assert record_task_interpretation(store, RUN, receipt, proposal) == filled
    assert len(store.read_events(RUN)) == count
    observation_admission(store, RUN, receipt, interpretation_for(store, RUN, receipt['message_id']),
                          'reference.mp4', 'reference', TEXT)
    assert store.list_approvals(RUN) == []


@pytest.mark.parametrize('change', ['task', 'constraints', 'reuse', 'existing_purpose', 'existing_source', 'quote'])
def test_supplement_cannot_change_frozen_scope_or_bound_video(tmp_path, change):
    store, receipt, proposal, before = scene(tmp_path)
    filled = supplement(proposal)
    if change.startswith('existing_'):
        record_task_interpretation(store, RUN, receipt, filled)
        filled['video_use']['purpose' if change == 'existing_purpose' else 'source'] = (
            'product' if change == 'existing_purpose' else URL)
    elif change == 'task':
        filled['task']['deliverable'] = 'writing'
    elif change == 'constraints':
        filled['constraints'] = [{}]
    elif change == 'reuse':
        filled['reuse_artifact'] = {}
    else:
        filled['video_use']['quote'] = 'invented instruction'
    count = len(store.read_events(RUN))
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, receipt, filled)
    assert len(store.read_events(RUN)) == count
    assert interpretation_for(store, RUN, receipt['message_id'])['task'] == before['task']


def test_ui_full_answer_and_model_substring_are_one_immutable_answer(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic', 'Synthetic')
    question = ask(store, RUN, {'question': '参考怎么用？', 'options': [
        {'label': '表达方式'}, {'label': '商品信息'}]})
    text = '关于「参考怎么用？」：' + TEXT
    receipt = receive(store, RUN, text, 'ui-answer', question_id=question['id'])
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    original = answer_from_receipt(store, RUN, receipt)
    count = len(store.read_events(RUN))
    assert record_answer(store, RUN, receipt, question_id=question['id'], quote=TEXT) == original
    assert len(store.read_events(RUN)) == count
    assert original['text'] == text and store.list_approvals(RUN) == []
    with pytest.raises(PipelineError):
        record_answer(store, RUN, receipt, question_id=question['id'], quote='invented answer')


@pytest.mark.parametrize('source', ['invented.mp4', '../../outside.mp4',
    'https://www.tiktok.com/@invented/video/7250000000000000001', 'https://example.org/video'])
def test_invalid_source_does_not_consume_the_one_binding(tmp_path, source):
    store, receipt, proposal, _ = scene(tmp_path)
    store.append_chat(RUN, 'assistant', source)  # Model text cannot prove user provenance.
    bad = supplement(proposal)
    bad['video_use']['source'] = source
    with pytest.raises(PipelineError):
        record_task_interpretation(store, RUN, receipt, bad)
    assert not any(e['kind'] == 'user_video_use_bound' for e in store.read_events(RUN))
    assert record_task_interpretation(store, RUN, receipt, supplement(proposal))['video_use']['source'] == 'reference.mp4'


@pytest.mark.parametrize('mode', ['discuss', 'status', 'stop', 'clarify'])
def test_missing_video_use_cannot_authorize_observation_in_readonly_modes(tmp_path, mode):
    store, receipt, proposal, _ = scene(tmp_path, mode=mode)
    with pytest.raises(PipelineError, match='observation_scope'):
        record_task_interpretation(store, RUN, receipt, supplement(proposal))
    assert not any(e['kind'] == 'user_video_use_bound' for e in store.read_events(RUN))


def test_bare_link_cannot_be_completed_into_an_observation_request(tmp_path):
    store, receipt, proposal, _ = scene(tmp_path, text=URL)
    filled = supplement(proposal)
    filled['video_use'].update(source=URL, quote=URL)
    with pytest.raises(PipelineError, match='observation_purpose'):
        record_task_interpretation(store, RUN, receipt, filled)
    assert not any(e['kind'] == 'user_video_use_bound' for e in store.read_events(RUN))


def saved_source(store, *, name='reference.mp4', kind='reference'):
    source = dict(source_url=URL, canonical_url=URL, name=name,
                  sha256=hashlib.sha256(b'synthetic source').hexdigest())
    if kind == 'reference':
        store.append_event(RUN, 'reference_video_started', reference_id='reference-' + name,
                           request={'source_url': URL, 'intent': 'analyze', 'instruction': ''})
        store.append_event(RUN, 'reference_video_source', reference_id='reference-' + name, source=source)
    else:
        store.append_event(RUN, 'product_video_source', **source)


@pytest.mark.parametrize('kind', ['reference', 'product'])
def test_ui_product_purpose_cannot_be_changed_through_a_saved_url_alias(tmp_path, kind):
    store, receipt, proposal, _ = scene(tmp_path,
        attachments=[{'name': 'reference.mp4', 'purpose': 'product'}])
    saved_source(store, kind=kind)
    filled = supplement(proposal)
    filled['video_use']['source'] = URL
    with pytest.raises(PipelineError, match='observation_purpose'):
        record_task_interpretation(store, RUN, receipt, filled)
    assert not any(e['kind'] == 'user_video_use_bound' for e in store.read_events(RUN))


def test_any_conflicting_attachment_blocks_the_shared_source(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic', URL)
    for name in ['reference.mp4', 'product.mp4']:
        accept_upload(store, RUN, name, b'synthetic source')
        saved_source(store, name=name)
    receipt = receive(store, RUN, TEXT, 'conflicting-attachments', attachments=[
        {'name': 'reference.mp4', 'purpose': 'reference'}, {'name': 'product.mp4', 'purpose': 'product'}])
    accept(store, RUN, receipt)
    start_execution(store, RUN, receipt)
    proposal = contract(TEXT)
    record_task_interpretation(store, RUN, receipt, proposal)
    filled = supplement(proposal)
    filled['video_use']['source'] = URL
    with pytest.raises(PipelineError, match='observation_purpose'):
        record_task_interpretation(store, RUN, receipt, filled)


@pytest.mark.parametrize('changed', [False, True])
def test_saved_url_and_filename_match_only_current_original_bytes(tmp_path, changed):
    store, receipt, proposal, _ = scene(tmp_path)
    proposal = supplement(proposal)
    proposal['video_use']['source'] = URL
    store.append_event(RUN, 'reference_video_started', reference_id='synthetic-reference',
        request={'source_url': URL, 'intent': 'analyze', 'instruction': ''})
    store.append_event(RUN, 'reference_video_source', reference_id='synthetic-reference', source={
        'source_url': URL, 'name': 'reference.mp4', 'sha256': hashlib.sha256(b'synthetic source').hexdigest()})
    filled = record_task_interpretation(store, RUN, receipt, proposal)
    if changed:
        (store.run_dir(RUN) / 'materials/reference.mp4').write_bytes(b'changed source')
        with pytest.raises(PipelineError):
            observation_admission(store, RUN, receipt, filled, 'reference.mp4', 'reference', TEXT)
    else:
        observation_admission(store, RUN, receipt, filled, 'reference.mp4', 'reference', TEXT)


def test_producer_can_fill_missing_use_and_observe_in_the_same_turn(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic', URL)
    accept_upload(store, RUN, 'reference.mp4', b'synthetic source')
    proposal = contract(TEXT)
    model = ScriptedModel([
        tool_turn('task', 'propose_task_interpretation', proposal),
        tool_turn('observe', 'observe_reference', {'source': 'reference.mp4', 'action': 'analyze',
            'quote': TEXT, 'scope': '', 'task_interpretation': supplement(proposal)}),
        final_turn('Should already have completed.')], legacy_task_contracts=False)
    calls = []
    def observer(source, *_):
        calls.append(source)
        return {'reference_id': 'synthetic-reference', 'report': 'MOCK completed observation'}
    settings = settings_for(tmp_path)
    receipt = receive(store, RUN, TEXT, 'same-turn', reference_consent=True)
    result = Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings), RUN,
                          reference_observer=observer).send(TEXT, user_input_id=receipt['message_id'])
    assert calls == ['reference.mp4']
    assert result.text == 'MOCK completed observation' and result.deliverable['status'] == 'complete'
    assert len(model.calls) == 2
