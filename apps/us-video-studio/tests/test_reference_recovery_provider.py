"""Selected observer recovery: HTTP contract with synthetic local media/models."""
import hashlib

import pytest

from fakes import final_turn
from studio.agents.client import Turn, Usage
from studio.connections import ConnectionStore
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.reference_video import task_state
from test_bailian_omni import native
from test_image_analysis import scene
from test_mimo_video import configure, observation, MIMO_MODEL
from test_reference_recovery import original_task, recovery_input, strict_turns, post_recovery
from test_server import RUN
from test_video_observation import video_bytes


def failed_reference(native, monkeypatch, *, key=True):
    configure(native, key=key)
    task, receipt = original_task(native, goal='analysis', saved=False, action='analyze')
    raw = video_bytes(audio=True)
    accept_upload(native[1], RUN, 'fallback.mp4', raw)
    payload = recovery_input(task, attachments=[{'name': 'fallback.mp4', 'purpose': 'reference'}])
    payload['reference_recovery'].update(name='fallback.mp4', expected_sha256=hashlib.sha256(raw).hexdigest())
    return task, receipt, payload


def test_explicit_failed_reference_recovery_uses_selected_mimo_once_and_original_goal(native, monkeypatch):
    task, receipt, payload = failed_reference(native, monkeypatch)
    # The legacy observer is unavailable: the chosen MiMo route must suffice.
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    strict_turns(monkeypatch, native[2], [observation(), final_turn('Mock recovered original analysis')])
    job = post_recovery(native, payload)
    assert job['status'] == 'succeeded' and job['deliverable']['goal'] == 'analysis'
    assert len(native[2].calls) == 2 and native[2].calls[0]['model'] == MIMO_MODEL
    assert any(block['type'] == 'video' for block in native[2].calls[0]['messages'][0]['content'])
    current = task_state(native[1], RUN)[0]
    assert current['reference_id'] == task['reference_id'] and current['candidate']['provider'] == 'mimo'
    assert current['recovery']['source_user_input_id'] == receipt['message_id']
    assert native[0].post(f'/api/runs/{RUN}/jobs', json=payload).json()['job_id'] == job['job_id']
    assert len(native[2].calls) == 2


@pytest.mark.parametrize('fault', ['missing_key', 'changed_config', 'changed_before_info'])
def test_recovery_free_preflight_is_business_rejection_without_fallback_or_job(native, monkeypatch, fault):
    _, receipt, payload = failed_reference(native, monkeypatch, key=fault != 'missing_key')
    if fault == 'changed_config':
        from studio.video_understanding import freeze
        def change_before_freeze(*args, **kwargs):
            connections = ConnectionStore(native[1].root)
            current = connections.public()
            connections.save({**current, 'expected_revision': current['revision'], 'video_analysis': None})
            return freeze(*args, **kwargs)
        monkeypatch.setattr('studio.video_understanding.freeze', change_before_freeze)
    elif fault == 'changed_before_info':
        from studio.video_understanding import connection_info
        def change_before_info(connections):
            previous = connection_info(connections)
            current = connections.public()
            connections.save({**current, 'expected_revision': current['revision'], 'video_analysis': None})
            return previous
        monkeypatch.setattr('studio.video_understanding.connection_info', change_before_info)
    before = len([e for e in native[1].read_events(RUN) if e['kind'] == 'job_created'])
    result = native[0].post(f'/api/runs/{RUN}/jobs', json=payload)
    assert result.status_code == 409 and result.json()['detail']
    assert native[2].calls == []
    assert len([e for e in native[1].read_events(RUN) if e['kind'] == 'job_created']) == before
    assert task_state(native[1], RUN)[0]['recovery']['source_user_input_id'] == receipt['message_id']
    assert any(e['kind'] == 'user_message_received' and e.get('request_id') == payload['request_id']
               for e in native[1].read_events(RUN))


def test_cached_recovery_skips_observer_freeze_even_after_selected_mimo_key_is_missing(native, monkeypatch):
    task, _ = original_task(native, goal='analysis', action='analyze')
    configure(native, key=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Cached observations must not freeze or invoke an observer')
    monkeypatch.setattr('studio.video_understanding.freeze', forbidden)
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    before = len(native[2].calls)
    strict_turns(monkeypatch, native[2], [final_turn('Mock explanation of the saved observation')])
    job = post_recovery(native, recovery_input(task, reference_consent=False))
    assert job['status'] == 'succeeded' and job['deliverable']['goal'] == 'analysis'
    assert len(native[2].calls) == before + 1


def test_same_recovery_receipt_can_be_explicitly_retried_after_free_missing_key_rejection(native, monkeypatch):
    _, _, payload = failed_reference(native, monkeypatch, key=False)
    first = native[0].post(f'/api/runs/{RUN}/jobs', json=payload)
    assert first.status_code == 409 and native[2].calls == []
    connections = ConnectionStore(native[1].root)
    current = connections.public()
    next(c for c in current['connections'] if c['id'] == 'mimo-video')['api_key'] = 'synthetic-only'
    connections.save({**current, 'expected_revision': current['revision']})
    strict_turns(monkeypatch, native[2], [observation(), final_turn('Mock recovered original analysis')])
    recovered = post_recovery(native, payload)
    assert recovered['status'] == 'succeeded', [
        {key: e.get(key) for key in ('type', 'error', 'reason', 'text')}
        for e in recovered['events'] if e.get('type') in {'error', 'blocked'}]
    assert len(native[2].calls) == 2


def test_paid_invalid_recovery_observation_stops_without_fallback_or_creative_call(native, monkeypatch):
    task, _, payload = failed_reference(native, monkeypatch)
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info', lambda: None)
    strict_turns(monkeypatch, native[2], [Turn(text='Mock malformed observation',
        stop_reason='end_turn', model=MIMO_MODEL, usage=Usage(100, 20))])
    failed = post_recovery(native, payload)
    assert failed['status'] == 'failed' and len(native[2].calls) == 1
    current = task_state(native[1], RUN)[0]
    assert current['reference_id'] == task['reference_id'] and not current.get('candidate')
    entries = Ledger(native[1], native[5]).entries(RUN)
    assert len(entries) == 1 and entries[0].usage_status == 'known'
