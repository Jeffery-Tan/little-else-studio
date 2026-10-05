"""Trusted media input and immutable task goals; fake observers, no network."""
import hashlib
import json
import threading

import pytest

from fakes import ScriptedModel, tool_turn, final_turn, fixture_task_interpretation
from local_http import TestClient
from studio.agents.orchestrator import Orchestrator
from studio.conversation_input import receive
from studio.creation_mode import digest
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store
from test_conversation_runtime_control import wait_idle
from test_server import RUN, settings_for, make_run

URL = 'https://www.tiktok.com/@fixture/video/7250000000000000000'


def contract(text, goal='analysis', *, source=None, purpose='reference', quote=None):
    proposal = fixture_task_interpretation(text, deliverable=goal)
    if goal == 'analysis':
        proposal['task'].update(mode='analyze', allowed_stages=[])
    if source:
        proposal['video_use'] = dict(source=source, purpose=purpose, quote=quote or text)
    return proposal


def run_turn(tmp_path, text, *, source=URL, goal='analysis', action='analyze',
             interpretation=True, quote=None, attachments=None, target=None, store=None):
    store = store or Store(tmp_path)
    if not store.list_runs():
        store.create_run(RUN, 'Offline reference', 'Synthetic product facts')
    settings = settings_for(tmp_path)
    args = dict(source=source, action=action, quote=quote or text, scope='Opening expression')
    if interpretation:
        args['task_interpretation'] = contract(text, goal, source=source, quote=quote)
    turns = [tool_turn('observe', 'observe_reference', args), final_turn('Original task still applies')]
    model = ScriptedModel(turns, legacy_task_contracts=False)
    calls = []
    def observe(*args):
        calls.append(args[0])
        return {'reference_id': 'fixture-reference', 'report': 'MOCK saved report'}
    receipt = receive(store, RUN, text, 'reference-input', reference_consent=True,
                      attachments=attachments, reference_target=target)
    result = Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings),
                          RUN, reference_observer=observe).send(text, user_input_id=receipt['message_id'])
    return result, calls, store, model


def test_bare_link_cannot_be_observed_even_with_model_purpose_and_connection_admission(tmp_path):
    result, calls, _, model = run_turn(tmp_path, URL)
    assert calls == []
    assert result.deliverable == {**result.deliverable, 'goal': 'analysis', 'status': 'incomplete'}
    assert '只有链接' in model.requests_text()


def test_action_and_quote_without_bound_task_never_authorize_observation(tmp_path):
    _, calls, _, model = run_turn(tmp_path, '请拆解这个参考 ' + URL, interpretation=False)
    assert calls == []
    assert '先按本条真实原话解释任务' in model.requests_text()


@pytest.mark.parametrize('goal', ['writing', 'direction', 'production_input'])
def test_analysis_is_dependency_and_does_not_replace_frozen_goal(tmp_path, goal):
    text = '请先看参考，再完成本轮约定方案 ' + URL
    result, calls, store, model = run_turn(tmp_path, text, goal=goal)
    assert calls == [URL]
    assert result.deliverable['goal'] == goal
    assert result.deliverable['status'] == 'incomplete'
    assert not store.latest_artifact(RUN, 'writing')
    assert len(model.calls) == 2  # The analysis did not conclude the model loop.


def test_prompt_action_cannot_downgrade_frozen_writing_goal(tmp_path):
    result, calls, _, model = run_turn(tmp_path, '按参考做完整方案 ' + URL,
                                     goal='writing', action='prompt')
    assert calls == []
    assert result.deliverable['goal'] == 'writing'
    assert result.deliverable['status'] == 'incomplete'
    assert '不能用 prompt 改写' in model.requests_text()


def test_attachment_purpose_is_trusted_and_cannot_be_switched_by_the_model(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline', 'Synthetic facts')
    accept_upload(store, RUN, 'product.mp4', b'fixture bytes, not a codec test')
    _, calls, _, model = run_turn(tmp_path, '读取我附上的视频', source='product.mp4',
        store=store, attachments=[{'name': 'product.mp4', 'purpose': 'product'}])
    assert calls == []
    assert '本条输入已标明另一种视频用途' in model.requests_text()


def test_receipt_retries_compare_purpose_target_and_connection_admission(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline', 'Synthetic facts')
    accept_upload(store, RUN, 'reference.mp4', b'fixture')
    fields = dict(attachments=[{'name': 'reference.mp4', 'purpose': 'reference'}],
                  reference_consent=True)
    first = receive(store, RUN, '读取素材', 'same-input', **fields)
    assert receive(store, RUN, '读取素材', 'same-input', **fields) == first
    with pytest.raises(PipelineError, match='request_conflict'):
        receive(store, RUN, '读取素材', 'same-input', **{**fields, 'reference_consent': False})
    with pytest.raises(PipelineError, match='request_conflict'):
        receive(store, RUN, '读取素材', 'same-input', **{**fields,
            'attachments': [{'name': 'reference.mp4', 'purpose': 'product'}]})


def selected_reference(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline reference target', 'Synthetic facts')
    raw = b'fixture original bytes; observation already saved'
    accept_upload(store, RUN, 'saved-reference.mp4', raw)
    candidate = dict(name='saved-reference.mp4', sha256=hashlib.sha256(raw).hexdigest(),
        analysis_id='analysis-fixture', reference_task_id='reference-fixture',
        purpose='reference', findings=['Mock visible expression'], limitations=['Mock limitation'],
        reference_methods=[])
    store.append_event(RUN, 'reference_video_started', reference_id='reference-fixture',
                       request={'source_url': URL, 'intent': 'analyze', 'instruction': ''})
    store.append_event(RUN, 'video_analysis_candidate', candidate=candidate)
    return store, dict(run_id=RUN, reference_id='reference-fixture',
        analysis_id=candidate['analysis_id'], candidate_sha256=digest(candidate))


@pytest.mark.parametrize('source', [URL, 'saved-reference.mp4'])
def test_exact_reference_target_reuses_its_analysis_for_url_or_filename(tmp_path, source):
    store, target = selected_reference(tmp_path)
    result, calls, _, _ = run_turn(tmp_path, '拆解这条选中的参考', source=source,
                                  target=target, store=store)
    assert result.deliverable['status'] == 'complete'
    assert calls == []  # No source-based lookup or observer request can pick another task.


def test_other_reference_attachment_cannot_bypass_exact_target(tmp_path):
    store, target = selected_reference(tmp_path)
    accept_upload(store, RUN, 'other.mp4', b'other fixture')
    _, calls, _, model = run_turn(tmp_path, '拆解选中的参考', source='other.mp4',
        target=target, store=store, attachments=[{'name': 'other.mp4', 'purpose': 'reference'}])
    assert calls == []
    assert '不能换来源或用途' in model.requests_text()


def test_analysis_dependency_can_finish_exact_prompt_contract_without_role_dispatch(tmp_path):
    store, target = selected_reference(tmp_path)
    text = '请从所选参考适配本品提示词'
    proposal = contract(text, 'reference_prompt')  # UI target supplies purpose; no model video_use needed.
    model = ScriptedModel([
        tool_turn('analysis', 'observe_reference', dict(source=URL, action='analyze', quote=text,
            scope='', task_interpretation=proposal)),
        tool_turn('scope', 'prepare_creative_context', dict(intent='new', format='video',
                                                         deliverable='reference_prompt')),
        tool_turn('save', 'save_reference_prompt', dict(prompt='Synthetic product prompt with known evidence')),
    ], legacy_task_contracts=False)
    settings = settings_for(tmp_path)
    receipt = receive(store, RUN, text, 'prompt-input', reference_target=target,
                      reference_consent=False)  # Cached exact candidate has no new observation fee.
    def forbidden_observer(*args):
        raise AssertionError('Exact target must reuse its saved analysis')
    result = Orchestrator(store, Pipeline(store), settings, model, Ledger(store, settings), RUN,
                          reference_observer=forbidden_observer).send(text, user_input_id=receipt['message_id'])
    assert result.deliverable['goal'] == 'reference_prompt'
    assert result.deliverable['status'] == 'complete'
    assert result.text == 'Synthetic product prompt with known evidence'
    assert not any(e['kind'] == 'agent_task_prepared' for e in store.read_events(RUN))


@pytest.mark.parametrize('admitted', [True, False])
def test_busy_reference_input_keeps_its_own_admission_not_the_original_job(tmp_path, monkeypatch, admitted):
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info',
                        lambda: {'base_url': 'https://fixture.invalid/compatible-mode/v1'})
    entered, release = threading.Event(), threading.Event()
    text = '请拆解这条参考 ' + URL
    next_model = ScriptedModel([tool_turn('observe', 'observe_reference', dict(
        source=URL, action='analyze', quote='请拆解这条参考', scope='',
        task_interpretation=contract(text, source=URL, quote='请拆解这条参考'))), final_turn('没有观察请求可用')],
        legacy_task_contracts=False)
    class Block:
        def complete(self, **kwargs):
            entered.set()
            assert release.wait(8)
            return final_turn('Original fixture')
    models = iter([Block(), next_model])
    app = build_app(settings_for(tmp_path), client_factory=lambda: next(models))
    calls = []
    app.state.observe_reference = lambda *args, **kwargs: (calls.append(args[1]) or {
        'reference_id': 'fixture-reference', 'report': 'MOCK saved report'})
    with TestClient(app) as http:
        make_run(http)
        original = http.post(f'/api/runs/{RUN}/jobs', json=dict(text='继续准备方案',
            request_id='original', reference_consent=not admitted)).json()
        assert entered.wait(3)
        try:
            payload = dict(text=text, request_id='reference-steer', expected_goal_revision=0,
                           reference_consent=admitted)
            response = http.post(f'/api/runs/{RUN}/jobs/{original["job_id"]}/steer', json=payload)
            assert response.status_code == 200, response.text
        finally:
            release.set()
            wait_idle(http)
        successor = http.app.state.jobs.latest(RUN)
        receipt = next(e for e in http.app.state.jobs.store.read_events(RUN)
                       if e['kind'] == 'user_message_received' and e['request_id'] == 'reference-steer')
        assert receipt['reference_consent'] is admitted
        assert bool(successor['model_config'].get('reference_analysis')) is admitted
        assert calls == ([URL] if admitted else [])
        assert successor['status'] == ('succeeded' if admitted else 'blocked')


def test_restart_requires_explicit_recovery_and_keeps_pending_reference_admission(tmp_path, monkeypatch):
    from studio.agent_runtime import AgentRuntime
    monkeypatch.setattr('studio.agents.bailian_omni.connection_info',
                        lambda: {'base_url': 'https://fixture.invalid/compatible-mode/v1'})
    store = Store(tmp_path)
    store.create_run(RUN, 'Offline recovery', 'Synthetic facts')
    receipt = receive(store, RUN, '拆解参考 ' + URL, 'pending-reference', reference_consent=True)
    store.append_event(RUN, 'job_created', job_id='old-job', request_id='old-request',
        text='Previous task', job_kind='conversation', job_payload={'pending_user_inputs': [receipt['message_id']]})
    store.append_event(RUN, 'job_status', job_id='old-job', status='running')
    runtime = AgentRuntime(store, RUN)
    runtime.create_task('old-job', role='orchestrator', parent_job_id='old-job')
    runtime.transition('old-job', 'running')
    models = []
    def factory():
        model = ScriptedModel([final_turn('Recovery fixture')])
        models.append(model)
        return model
    app = build_app(settings_for(tmp_path), client_factory=factory)
    with TestClient(app) as http:
        assert models == []
        recovered = http.get(f'/api/runs/{RUN}/jobs/old-job').json()
        assert recovered['status'] == 'interrupted'
        assert models == []
        response = http.post(f'/api/runs/{RUN}/jobs/old-job/resume-steer', json={})
        assert response.status_code == 200, response.text
        wait_idle(http)
        successor = http.app.state.jobs.latest(RUN)
        assert successor['user_input_id'] == receipt['message_id']
        assert successor['model_config']['reference_analysis']
        assert len(models) == 1
