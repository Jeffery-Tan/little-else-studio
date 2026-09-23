"""Creator workflow contracts, using temporary data and scripted models only."""
import pytest
from local_http import TestClient

from fakes import ScriptedModel, final_turn, tool_turn
from studio.agents.client import ModelError, run_agent_loop
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.project_brief import project_brief
from studio.server import build_app
from studio.structured import is_user_question, open_questions
from studio.store import Store
from test_server import settings_for, RUN
from test_jobs import wait_job


def project(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, 'Fixture', 'Offline creator workflow')
    pipeline = Pipeline(store)
    a = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', a.token, content='Facts', summary='Facts')
    return store, pipeline


def direction(pipeline, questions=None, canonical=False):
    a = pipeline.start_attempt(RUN, 'direction')
    return pipeline.submit(RUN, 'direction', a.token, content='' if canonical else 'Original prose',
        summary='Two options', structured={'options': [{'key': 'A', 'claim': 'A', 'story': '开场→动作→回报'},
                                                     {'key': 'B', 'claim': 'B'}],
                                           'open_questions': questions or []})


def question(owner='user', timing='now', reason='Only the owner knows the tablet model'):
    return {'question': 'Which tablet?', 'blocks': 'Fit evidence', 'owner': owner, 'timing': timing, 'reason': reason}


def test_succeeded_execution_does_not_mean_direction_approved(tmp_path):
    store, pipeline = project(tmp_path)
    artifact = direction(pipeline)
    job = {'status': 'succeeded', 'job_kind': 'conversation', 'events': [
        {'type': 'agent_tool', 'tool': 'request_review', 'args': {'stage': 'direction'},
         'ok': False, 'at': '9999-01-01T00:00:00Z'}]}
    out = project_brief(pipeline.state(RUN).to_dict(), job)
    assert out['phase'] == 'review_failed'
    assert out['action']['kind'] == 'review_stage'
    assert out['action']['sha256'] == artifact.sha256
    assert out['completed'] == 1
    assert not store.list_approvals(RUN)


@pytest.mark.parametrize('row,expected', [
    (question(), 1), (question('team'), 0), (question(timing='later'), 0),
    (question(reason=''), 0), ({'question': 'Who reads the images?', 'blocks': 'Creative work'}, 0),
])
def test_only_explicit_current_user_questions_become_tasks(tmp_path, row, expected):
    _, pipeline = project(tmp_path)
    direction(pipeline, [row])
    out = project_brief(pipeline.state(RUN).to_dict())
    assert len(out['questions']) == expected
    assert len(out['issues']) == 1  # Nothing was silently discarded.


@pytest.mark.parametrize('row,expected', [
    (question(), True),
    (question('team'), False),
    (question(timing='later'), False),
    (question(reason=''), False),
    (question(reason=7), False),          # malformed reason: triage, never a crash
    ({'question': 'Who reads the images?', 'blocks': 'Creative work'}, False),
    ({'blocks': 'Only the consequence was filled in'}, False),
    ('not a row', False),
])
def test_the_card_and_the_standing_list_answer_from_one_rule(tmp_path, row, expected):
    """The stage card and the run-wide list must never disagree.

    Both surfaces used to spell the row-level rule out separately, so a change
    to it could land in one and leave the card and the standing list saying
    different things about the same document. They now share
    ``structured.is_user_question``; this pins the two together on the same
    payloads the live screens read.
    """
    _, pipeline = project(tmp_path)
    direction(pipeline, [row])
    state = pipeline.state(RUN).to_dict()
    stage = next(s for s in state['stages'] if s['stage'] == 'direction')

    card = open_questions(stage['latest']['structured'], 'direction')
    listed = project_brief(state)['questions']
    assert [q['question'] for q in card] == [q['question'] for q in listed]
    assert len(card) == int(expected)
    assert is_user_question(row) is expected


def test_stale_questions_are_not_current_user_work(tmp_path):
    _, pipeline = project(tmp_path)
    direction(pipeline, [question()])
    a = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', a.token, content='New facts', summary='New facts')
    out = project_brief(pipeline.state(RUN).to_dict())
    assert out['questions'] == []
    assert out['issues'][0]['active'] is False


def test_new_document_has_one_source_and_immutable_old_versions(tmp_path):
    store, pipeline = project(tmp_path)
    old = direction(pipeline)
    new = direction(pipeline, canonical=True)
    assert store.read_artifact_text(RUN, 'direction', old.version) == 'Original prose'
    text = store.read_artifact_text(RUN, 'direction', new.version)
    assert 'studio:structured-document-v1' in text and '开场→动作→回报' in text
    assert store.verify_artifact(RUN, 'direction', new.version)
    assert pipeline.stage_state(RUN, 'direction').status == 'awaiting_review'


def test_incomplete_canonical_payload_keeps_attempt_and_old_content(tmp_path):
    store, pipeline = project(tmp_path)
    old = direction(pipeline)
    attempt = pipeline.start_attempt(RUN, 'direction')
    with pytest.raises(PipelineError, match='核心内容'):
        pipeline.submit(RUN, 'direction', attempt.token, content='', summary='Incomplete', structured={'recommended': 'A'})
    assert store.latest_artifact(RUN, 'direction').sha256 == old.sha256
    assert pipeline.stage_state(RUN, 'direction').open_attempt.token == attempt.token


@pytest.mark.parametrize('extra', [{'unrecognized_narrative': 'Must not disappear'},
                                  {'dependency_matrix': [{'need': 'test', 'options': {'A' * 33: True}}]},
                                  {'recommended_because': 'x' * 4001}])
def test_canonical_source_cannot_silently_lose_content(tmp_path, extra):
    _, pipeline = project(tmp_path)
    attempt = pipeline.start_attempt(RUN, 'direction')
    with pytest.raises(PipelineError):
        pipeline.submit(RUN, 'direction', attempt.token, content='', summary='Fixture',
                        structured={'options': [{'key': 'A', 'claim': 'A'}], **extra})
    assert pipeline.stage_state(RUN, 'direction').latest is None


def test_canonical_edits_regenerate_prose_locally_without_any_model(tmp_path):
    store, pipeline = project(tmp_path)
    artifact = direction(pipeline, canonical=True)
    def no_model():
        pytest.fail('Canonical edits must not call a model to rewrite the same content')
    with TestClient(build_app(settings_for(tmp_path), client_factory=no_model)) as http:
        assert http.get(f'/api/runs/{RUN}/edits/direction').json()['revision_mode'] == 'local'
        saved = http.post(f'/api/runs/{RUN}/edits/direction', json={
            'request_id': 'edit-local', 'expected_version': artifact.version, 'expected_sha256': artifact.sha256,
            'edits': [{'row': 0, 'field': 'story', 'value': '新的开场和回报'}]})
        assert saved.status_code == 200, saved.text
        draft = saved.json()
        started = http.post(f'/api/runs/{RUN}/edits/direction/{draft["draft_id"]}/generate', json={
            'request_id': 'apply-local', 'expected_draft_sha256': draft['draft_sha256'],
            'expected_revision': 0, 'consent': True})
        assert started.status_code == 200, started.text
        job = wait_job(http, started.json()['job_id'])
        assert job['status'] == 'succeeded', job
        assert not store.read_ledger(RUN)
    latest = store.latest_artifact(RUN, 'direction')
    assert latest.version == artifact.version + 1
    assert '新的开场和回报' in store.read_artifact_text(RUN, 'direction', latest.version)
    assert '新的开场和回报' not in store.read_artifact_text(RUN, 'direction', artifact.version)
    assert store.list_approvals(RUN) == []


def test_narration_is_journaled_separately_and_final_continuations_survive():
    activity = []
    tool = tool_turn('one', 'read_project_state', {})
    tool.text = 'I will inspect the files now.'
    partial = final_turn('The new direction is saved.')
    partial.stop_reason = 'max_tokens'
    model = ScriptedModel([tool, partial, final_turn('The review has not completed.')])
    out = run_agent_loop(model, model='fixture', system='', messages=[], tools=[{'name': 'read_project_state'}],
                         execute_tool=lambda *_: '{}', on_activity=activity.append)
    assert out.text == 'The new direction is saved.\nThe review has not completed.'
    assert any(e.get('text') == tool.text for e in activity)


def test_review_command_is_bound_idempotent_and_uses_only_reviewer(tmp_path):
    store, pipeline = project(tmp_path)
    artifact = direction(pipeline)
    model = ScriptedModel([tool_turn('review', 'finish_review', {'verdict': 'pass', 'findings': []}), final_turn('Pass')])
    app = build_app(settings_for(tmp_path), client_factory=lambda: model)
    payload = {'request_id': 'review-once', 'stage': 'direction', 'expected_version': artifact.version,
               'expected_artifact_sha256': artifact.sha256}
    with TestClient(app) as http:
        invalid = {**payload, 'expected_artifact_sha256': 'f' * 64}
        assert http.post(f'/api/runs/{RUN}/actions/review', json=invalid).status_code == 409
        assert model.calls == []
        started = http.post(f'/api/runs/{RUN}/actions/review', json=payload)
        assert started.status_code == 200, started.text
        job = wait_job(http, started.json()['job_id'])
        assert job['status'] == 'succeeded', job
        assert job['brief']['phase'] == 'reviewed'
        assert http.post(f'/api/runs/{RUN}/actions/review', json=payload).json()['job_id'] == job['job_id']
        assert len(model.calls) == 1  # Recording the verdict completes the command.
        assert all('dispatch' not in call['tool_names'] for call in model.calls)
        assert http.get(f'/api/runs/{RUN}/timeline').json()['brief'] == job['brief']
        # Cursor-based polling cannot lose the authoritative project status.
        assert http.get(f'/api/runs/{RUN}/jobs/{job["job_id"]}?after_seq={job["cursor"]}').json()['brief'] == job['brief']
    assert len(store.list_artifacts(RUN)) == 2
    assert store.list_approvals(RUN) == []


def test_failed_review_keeps_exact_artifact_and_exposes_scoped_retry(tmp_path):
    store, pipeline = project(tmp_path)
    artifact = direction(pipeline)
    class Timeout:
        def complete(self, **_):
            raise ModelError('fixture timeout')
    with TestClient(build_app(settings_for(tmp_path), client_factory=Timeout)) as http:
        payload = {'request_id': 'timeout', 'stage': 'direction', 'expected_version': artifact.version,
                   'expected_artifact_sha256': artifact.sha256}
        started = http.post(f'/api/runs/{RUN}/actions/review', json=payload)
        assert started.status_code == 200, started.text
        job = wait_job(http, started.json()['job_id'])
        assert job['status'] == 'failed'
        assert job['brief']['phase'] == 'review_failed'
        assert job['brief']['action']['sha256'] == artifact.sha256
        assert http.get(f'/api/runs/{RUN}/timeline').json()['brief'] == job['brief']
    assert store.latest_artifact(RUN, 'direction').sha256 == artifact.sha256
    assert store.list_reviews(RUN) == []


def test_project_list_keeps_original_brief_for_creation_recovery(tmp_path):
    store, pipeline = project(tmp_path)
    direction(pipeline)
    original = store.read_run(RUN).brief
    with TestClient(build_app(settings_for(tmp_path))) as http:
        row = next(row for row in http.get('/api/runs').json() if row['run_id'] == RUN)
        assert row['brief'] == original
        assert row['project_brief']['phase'] == 'awaiting_review'
        assert row['progress']['title'] == row['project_brief']['title']
