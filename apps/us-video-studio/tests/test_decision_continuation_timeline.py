"""Receipt projection from persisted identities; no provider calls or run edits."""
from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from studio.conversation_input import accept, receive
from studio.store import Store
from studio.timeline import _group_into_turns, _project_reply_sources

RUN = 'continuation-fixture'
ACK = '已采用方向 A。下一步已记录，待本轮结束后启动；实际进展会显示在任务状态。'
FAILURE = 'DeepSeek 连接中断，本次结果未完成。已保存内容保留，没有自动重试。'


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    ticks = itertools.count()
    base = datetime(2026, 10, 8, tzinfo=timezone.utc)
    monkeypatch.setattr('studio.store.utcnow', lambda: (
        base + timedelta(seconds=next(ticks))).strftime('%Y-%m-%dT%H:%M:%SZ'))
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline continuation', 'Offline US fixture.')
    source = receive(store, RUN, '采用方向 A，先完成素材方案和脚本。', 'parent-request')
    accept(store, RUN, source)
    store.append_event(RUN, 'job_created', job_id='parent', request_id='parent-request',
                       user_input_id=source['message_id'])
    store.append_event(RUN, 'job_status', job_id='parent', status='running')
    store.append_event(RUN, 'decision_followup_requested', request_id='decision:exact',
        source_request_id='parent-request', message_id=source['message_id'],
        source_user_input_id=source['message_id'])
    store.append_event(RUN, 'user_message_consumed', request_id='parent-request',
        message_id=source['message_id'], result={
            'status': 'recorded', 'action': 'select_direction', 'selected_option_id': 'A',
            'continuation_status': 'pending', 'continuation_request_id': 'decision:exact'})
    store.append_chat(RUN, 'assistant', ACK)
    store.append_event(RUN, 'job_status', job_id='parent', status='succeeded')
    return store, source


def child(store, source, **overrides):
    return store.append_event(RUN, 'job_created', **{
        'job_id': 'child', 'request_id': 'decision:exact', 'parent_job_id': 'parent',
        'job_payload': {'source_user_input_id': source['message_id']}, **overrides})


def project(store):
    chat, events = store.read_chat(RUN), store.read_events(RUN)
    original = copy.deepcopy(chat)
    turns = _group_into_turns(chat, [], events, [])
    _project_reply_sources(turns, chat, events)
    assert chat == original
    assert store.read_chat(RUN) == original
    return turns


@pytest.mark.parametrize('status,phrase', [
    ('pending', '尚未启动'), ('queued', '已排队'), ('running', '继续处理'),
    ('cancelling', '正在停止'), ('succeeded', '处理已结束'), ('blocked', FAILURE),
    ('failed', FAILURE), ('cancelled', '已停止'), ('interrupted', '被中断'),
])
def test_exact_successor_receipt_tracks_durable_job_state(recorded, status, phrase):
    store, source = recorded
    if status != 'pending':
        child(store, source)
        if status != 'queued':
            store.append_event(RUN, 'job_status', job_id='child', status=status,
                               reason=FAILURE if status in {'blocked', 'failed'} else '')
    turn = project(store)[0]
    assert turn['reply']['text'] == ACK
    assert turn['decision_continuation']['status'] == status
    assert phrase in turn['replies'][0]['display_text']
    assert '待本轮结束后启动' not in turn['replies'][0]['display_text']
    if status in {'blocked', 'failed', 'cancelled', 'interrupted'}:
        assert '处理已结束' not in turn['replies'][0]['display_text']


def test_runtime_interrupt_does_not_override_original_child_job_failure(recorded):
    store, source = recorded
    child(store, source)
    store.append_event(RUN, 'job_status', job_id='child', status='blocked', reason=FAILURE)
    store.append_event(RUN, 'agent_runtime_task_status', task_id='child', status='interrupted',
                       reason='Service reloaded')
    projection = project(store)[0]['decision_continuation']
    assert projection['status'] == 'blocked'
    assert projection['reason'] == FAILURE


@pytest.mark.parametrize('kind,status', [('decision_followup_blocked', 'blocked'),
                                       ('decision_followup_suppressed', 'suppressed')])
def test_unstarted_failure_and_suppression_are_visible(recorded, kind, status):
    store, _ = recorded
    store.append_event(RUN, kind, job_id='parent', request_id='decision:exact', reason='Exact reason')
    turn = project(store)[0]
    assert turn['decision_continuation']['status'] == status
    assert 'Exact reason' in turn['replies'][0]['display_text']


def test_later_actual_successor_outranks_old_unstarted_failure(recorded):
    store, source = recorded
    store.append_event(RUN, 'decision_followup_blocked', job_id='parent',
                       request_id='decision:exact', reason='Earlier admission failure')
    child(store, source)
    store.append_event(RUN, 'job_status', job_id='child', status='running')
    assert project(store)[0]['decision_continuation']['status'] == 'running'


@pytest.mark.parametrize('changes', [
    {'request_id': 'another-request'}, {'parent_job_id': 'another-parent'},
    {'job_payload': {'source_user_input_id': 'another-message'}},
])
def test_foreign_job_is_never_used_as_this_continuation(recorded, changes):
    store, source = recorded
    child(store, source, **changes)
    store.append_event(RUN, 'job_status', job_id='child', status='succeeded')
    projection = project(store)[0]['decision_continuation']
    assert projection['status'] == 'pending'
    assert projection['job_id'] is None


def test_exact_reply_source_survives_next_user_message(recorded):
    store, source = recorded
    child(store, source)
    later = receive(store, RUN, '我只问进度', 'later-request')
    accept(store, RUN, later)
    index = len(store.read_chat(RUN))
    store.append_chat(RUN, 'assistant', '素材方案已保存，脚本待完成。')
    store.append_event(RUN, 'agent_reply_recorded', chat_index=index,
        source_message_id=source['message_id'], job_id='child', decision_receipt=False)
    turns = project(store)
    assert [r['text'] for r in turns[0]['replies']] == [ACK, '素材方案已保存，脚本待完成。']
    assert turns[1]['reply'] is None


def test_uncontested_legacy_successor_conclusion_is_visible(recorded):
    store, source = recorded
    child(store, source)
    store.append_chat(RUN, 'assistant', 'Legacy successor conclusion')
    turn = project(store)[0]
    assert [r['text'] for r in turn['replies']] == [ACK, 'Legacy successor conclusion']


def test_unbound_legacy_reply_after_another_request_is_not_claimed(recorded):
    store, source = recorded
    child(store, source)
    store.append_event(RUN, 'job_created', job_id='foreign', request_id='unrelated')
    store.append_chat(RUN, 'assistant', 'Foreign conclusion')
    turn = project(store)[0]
    assert [r['text'] for r in turn['replies']] == [ACK]


def test_pause_receipt_does_not_schedule_or_claim_completed_work(recorded):
    store, _ = recorded
    chat, events = store.read_chat(RUN), store.read_events(RUN)
    events = [e for e in events if e['kind'] != 'decision_followup_requested']
    consumed = next(e for e in events if e['kind'] == 'user_message_consumed')
    consumed['result']['continuation_status'] = 'paused'
    consumed['result'].pop('continuation_request_id')
    turns = _group_into_turns(chat, [], events, [])
    _project_reply_sources(turns, chat, events)
    assert turns[0]['decision_continuation']['status'] == 'paused'
    assert '暂停推进' in turns[0]['replies'][0]['display_text']
    assert '处理已结束' not in turns[0]['replies'][0]['display_text']
    assert 'job_id' not in turns[0]['decision_continuation']


def test_legacy_ack_cannot_replace_another_request_reply_within_parent_interval(recorded):
    store, _ = recorded
    chat, events = store.read_chat(RUN), store.read_events(RUN)
    consumed = next(e for e in events if e['kind'] == 'user_message_consumed')
    events.insert(events.index(consumed) + 1, {'kind':'job_created', 'job_id':'foreign',
        'request_id':'foreign-request', 'at':consumed['at']})
    turns = _group_into_turns(chat, [], events, [])
    _project_reply_sources(turns, chat, events)
    assert 'display_text' not in turns[0]['replies'][0]
    assert turns[0]['decision_continuation']['separate'] is True


def test_no_continuation_identity_retains_legacy_first_reply_only(recorded):
    store, _ = recorded
    chat, events = store.read_chat(RUN), store.read_events(RUN)
    events = [e for e in events if e['kind'] not in {'user_message_consumed', 'decision_followup_requested'}]
    chat.append({'role': 'assistant', 'at': '2026-10-08T01:00:00Z', 'text': 'Later unbound reply'})
    turns = _group_into_turns(chat, [], events, [])
    _project_reply_sources(turns, chat, events)
    assert [r['text'] for r in turns[0]['replies']] == [ACK]
    assert 'decision_continuation' not in turns[0]


def test_reply_metadata_cannot_attach_foreign_job_to_source(recorded):
    store, source = recorded
    child(store, source, job_payload={'source_user_input_id': 'another-message'})
    index = len(store.read_chat(RUN))
    store.append_chat(RUN, 'assistant', 'Foreign conclusion')
    store.append_event(RUN, 'agent_reply_recorded', chat_index=index,
        source_message_id=source['message_id'], job_id='child', decision_receipt=False)
    assert [r['text'] for r in project(store)[0]['replies']] == [ACK]


def test_projection_is_repeatable_and_only_changes_display_text(recorded):
    store, source = recorded
    child(store, source)
    store.append_event(RUN, 'job_status', job_id='child', status='running')
    assert project(store) == project(store)


def test_real_orchestrator_handler_records_original_reply_identity(tmp_path):
    from studio.agent_runtime import bind_task, reset_task
    from test_conversation_ingress import perform, proposal
    from test_conversational_decision import RUN as actual_run, seed_direction
    from test_decision import received_message

    store, pipeline = seed_direction(tmp_path)
    receipt = received_message(store, pipeline, actual_run, '采用 B')
    store.append_event(actual_run, 'job_created', job_id='actual-parent',
        request_id=receipt['request_id'], user_input_id=receipt['message_id'])
    activation = bind_task('actual-parent')
    try:
        result, _, model = perform(store, pipeline, '采用 B', proposal(store, pipeline), receipt=receipt)
    finally:
        reset_task(activation)
    associations = [e for e in store.read_events(actual_run) if e['kind'] == 'agent_reply_recorded']
    assert len(associations) == 1
    event = associations[0]
    assert event['source_message_id'] == receipt['message_id']
    assert event['job_id'] == 'actual-parent'
    assert event['decision_receipt'] is True
    assert store.read_chat(actual_run)[event['chat_index']]['text'] == result.text
    assert len(model.calls) == 2  # Scripted offline model, never a provider call.
