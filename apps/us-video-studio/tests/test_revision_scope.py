"""Real dispatcher and conversation loop; no network, credentials or real runs."""
import copy
import hashlib
import json

import pytest

from fakes import ScriptedModel, tool_turn, final_turn
from studio.agents.client import JobCancelled
from studio.agents.orchestrator import Orchestrator
from studio.config import Settings
from studio.ledger import Ledger
from studio.models import PipelineError, STAGES
from studio.pipeline import Pipeline
from studio.revision_scope import plan_revision, patch_payload, work_plan
from studio.store import Store
from studio.tools.dispatch import Dispatcher

RUN = 'scoped-revision'
QUOTE = '只把第一句改得自然一点，其他内容保留'


def write(pipe, stage, data, *, body=''):
    attempt = pipe.start_attempt(RUN, stage)
    return pipe.submit(RUN, stage, attempt.token, content=body, summary=stage,
                       structured=data, evidence=['fixture-source'], limitations=['actual audio unverified'])


def approve(pipe, stage, option=None):
    pipe.record_review(RUN, stage, verdict='pass', findings=[])
    pipe.record_approval(RUN, stage, nonce=pipe.issue_nonce(RUN, stage),
                         decision='approved', selected_option_id=option)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic product', 'Offline only')
    pipe = Pipeline(store)
    settings = Settings(tmp_path, 'https://example.invalid', max_tool_rounds=12)
    monkeypatch.setattr('studio.learning.LearningService.prepare_context', lambda *a, **k: {
        'text': 'FIXTURE_METHOD', 'snapshot_sha256': 'f' * 64, 'evidence_ref': 'fixture-method'})
    write(pipe, 'intake', {'facts': [{'label': 'Product', 'value': 'Bag', 'source': 'fixture'}]})
    write(pipe, 'direction', {'options': [{'key': 'A', 'claim': 'Lunch', 'hook': 'Desk', 'satisfies': 'goal.primary'},
                                        {'key': 'B', 'claim': 'Picnic', 'hook': 'Park', 'satisfies': 'goal.primary'}],
                              'recommended': 'B'})
    approve(pipe, 'direction', 'A')
    write(pipe, 'material_plan', {'items': [{'shot': 'Desk', 'status': '不确定', 'note': 'Keep this'}]})
    write(pipe, 'writing', {'segments': [
        {'tc': '0–3', 'picture': 'Open bag', 'line': 'Original line', 'sound': 'Room', 'duration': '3'},
        {'tc': '3–6', 'picture': 'Lunch', 'line': 'Keep second line', 'sound': 'Room', 'duration': '3'}],
        'total_duration': '6秒', 'assumptions': ['Unverified pacing']})
    approve(pipe, 'writing')
    return store, pipe, settings, Ledger(store, settings)


def plan_args(store, stage='writing', path=None, mode='local', quote=QUOTE):
    base = store.latest_artifact(RUN, stage)
    return {'stage': stage, 'version': base.version, 'sha256': base.sha256, 'mode': mode,
            'quote': quote, 'reason': '只修改对应内容，保留其他表达',
            'fields': [{'path': path or ['segments', 0, 'line'], 'instruction': 'Make it conversational'}]
                      if mode == 'local' else []}


def plan(env, **kwargs):
    return plan_revision(env[0], env[1], RUN, plan_args(env[0], **kwargs), turn_id='turn-1', user_message=QUOTE)


def patch(path=None, value='Try this line'):
    return {'changes': [{'path': path or ['segments', 0, 'line'], 'value': value}], 'summary': 'Changed one line'}


def dispatch(env, model, scope):
    store, pipe, settings, ledger = env
    return Dispatcher(store, pipe, settings, model, ledger, RUN).dispatch(scope['stage'], QUOTE, revision_scope=scope)


def test_writer_receives_patch_only_and_preserves_chosen_direction_and_other_shots(env):
    store, pipe, _, _ = env
    before = {s: store.latest_artifact(RUN, s) for s in STAGES}
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch())])
    outcome = dispatch(env, model, plan(env))
    assert outcome.submitted == 'writing-v2' and len(model.calls) == 1
    assert 'submit_patch' in model.calls[0]['tool_names']
    assert 'submit_stage' not in model.calls[0]['tool_names']
    assert 'Original line' in model.requests_text() and 'Keep second line' in model.requests_text()
    after = store.latest_artifact(RUN, 'writing')
    expected = copy.deepcopy(before['writing'].structured)
    expected['segments'][0]['line'] = 'Try this line'
    assert after.structured == expected
    assert after.limitations == before['writing'].limitations
    assert set(before['writing'].evidence) <= set(after.evidence)
    assert store.verify_artifact(RUN, 'writing', 1)
    assert pipe.stage_state(RUN, 'writing').approval is None
    assert pipe.stage_state(RUN, 'writing').review is None
    for stage in STAGES[:-1]:
        assert store.latest_artifact(RUN, stage) == before[stage]
        assert not pipe.stage_state(RUN, stage).stale
    assert pipe.stage_state(RUN, 'direction').approval.selected_option_id == 'A'
    assert outcome.impact['next_step']['stage'] == 'writing'
    assert outcome.impact['next_step']['action'] == 'review'


def test_writing_revision_task_records_exact_role_tools_and_context_sources(env):
    store, _, _, _ = env
    scope = plan(env)
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch())])
    outcome = dispatch(env, model, scope)
    assert outcome.submitted == 'writing-v2'

    events = store.read_events(RUN)
    task = next(e for e in events if e['kind'] == 'agent_task_prepared')
    finished = next(e for e in events if e['kind'] == 'dispatch_done')
    assert task['task_id'] == finished['task_id']
    assert (task['task_kind'], task['stage'], task['role']) == ('stage_revision', 'writing', 'writer')
    assert (task['plan_id'], task['mode']) == (scope['plan_id'], 'local')
    assert task['base'] == {'version': scope['version'], 'sha256': scope['sha256']}
    assert task['inputs'] == scope['inputs']
    assert task['expected_result'] == {'artifact_stage': 'writing', 'review_required': True}
    assert 'submit_patch' in task['allowed_tools'] and 'submit_stage' not in task['allowed_tools']
    sources = {row['stage']: row for row in task['context']['sources']}
    for stage in ('intake', 'direction', 'material_plan', 'writing'):
        artifact = store.read_artifact(RUN, stage, 1)
        assert sources[stage] == {'kind': 'artifact', 'stage': stage,
                                  'version': 1, 'sha256': artifact.sha256}
    assert task['context']['method_ref'] == 'fixture-method'
    assert task['context']['method_sha256'] == 'f' * 64
    call = model.calls[0]
    assert task['context']['system_sha256'] == hashlib.sha256(call['system'].encode()).hexdigest()
    assert task['context']['seed_sha256'] == hashlib.sha256(
        call['messages'][0]['content'].encode()).hexdigest()
    assert 'Keep second line' in call['messages'][0]['content']
    assert 'selected_option_id=A' in call['messages'][0]['content']


def test_out_of_scope_and_full_submit_rejected_then_valid_patch_completes(env):
    model = ScriptedModel([
        tool_turn('full', 'submit_stage', {'content': 'Replace everything', 'summary': 'wrong'}),
        tool_turn('wide', 'submit_patch', patch(['segments', 1, 'picture'], 'Wrong shot')),
        tool_turn('caps', 'read_capabilities', {}),
        tool_turn('save', 'submit_patch', patch())])
    result = dispatch(env, model, plan(env))
    assert result.submitted == 'writing-v2'
    for index in (1, 2):
        assert model.calls[index]['messages'][-1]['content'][0]['is_error']
    caps = json.loads(model.calls[3]['messages'][-1]['content'][0]['content'])
    assert caps['tools'] == model.calls[3]['tool_names']
    assert len(env[0].list_artifacts(RUN, 'writing')) == 2


@pytest.mark.parametrize('path', [['segments', -1, 'line'], ['segments', True, 'line'],
                                 ['segments'], ['segments', 99, 'line'], ['segments', 0, 'absent']])
def test_invalid_scope_paths_fail_without_model_or_attempt(env, path):
    with pytest.raises(PipelineError):
        plan(env, path=path)
    assert env[1].stage_state(RUN, 'writing').open_attempt is None


@pytest.mark.parametrize('changes', [
    [{'path': ['segments', 0, 'line'], 'value': 'Original line'}],
    [{'path': ['segments', 0, 'line'], 'value': 'a'}, {'path': ['segments', 0, 'line'], 'value': 'b'}],
    [{'path': ['segments', 0, 'line'], 'value': {'injection': 'object'}}],
])
def test_invalid_or_noop_patch_creates_no_version(env, changes):
    model = ScriptedModel([tool_turn('bad', 'submit_patch', {'changes': changes, 'summary': 'bad'}), final_turn('Cannot submit')])
    result = dispatch(env, model, plan(env))
    assert result.submitted is None
    assert env[0].latest_artifact(RUN, 'writing').version == 1
    assert env[1].stage_state(RUN, 'writing').open_attempt is None


def test_timing_patch_marks_duration_for_full_review(env):
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch(['segments', 0, 'duration'], '4秒'))])
    assert dispatch(env, model, plan(env, path=['segments', 0, 'duration'])).submitted
    data = env[0].latest_artifact(RUN, 'writing').structured
    assert data['total_duration'].startswith('待复核')
    assert data['segments'][1]['duration'] == '3'
    assert data['assumptions'][0] == 'Unverified pacing'


def test_stale_and_legacy_work_are_not_silently_rewritten_as_local(env):
    store, pipe, _, _ = env
    base = store.latest_artifact(RUN, 'writing')
    write(pipe, 'writing', base.structured, body='Extra legacy prose that must not disappear')
    with pytest.raises(PipelineError, match='正文与卡片'):
        plan(env)
    write(pipe, 'intake', {'facts': [{'label': 'Product', 'value': 'New fact', 'source': 'fixture'}]})
    with pytest.raises(PipelineError):
        plan(env)
    workflow = work_plan(pipe, RUN, 'writing')
    assert workflow['next_step']['stage'] == 'direction'
    assert workflow['next_step']['action'] == 'update'
    assert workflow['stages'][0]['action'] == 'preserve'


def test_direction_patch_invalidates_only_existing_downstream_and_never_carries_approval(env):
    store, pipe, _, _ = env
    scope = plan(env, stage='direction', path=['options', 0, 'hook'])
    assert scope['affected_stages'] == ['material_plan', 'writing']
    model = ScriptedModel([tool_turn('save', 'submit_patch', patch(['options', 0, 'hook'], 'New desk opening'))])
    assert dispatch(env, model, scope).submitted == 'direction-v2'
    assert not pipe.stage_state(RUN, 'intake').stale
    assert pipe.stage_state(RUN, 'direction').approval is None
    assert pipe.stage_state(RUN, 'material_plan').stale and pipe.stage_state(RUN, 'writing').stale
    assert store.latest_artifact(RUN, 'direction').structured['options'][1]['hook'] == 'Park'
    assert len(store.list_approvals(RUN, 'direction')) == 1


def test_version_changes_before_dispatch_never_call_model(env):
    scope = plan(env)
    write(env[1], 'writing', env[0].latest_artifact(RUN, 'writing').structured)
    model = ScriptedModel([])
    with pytest.raises(PipelineError, match='基线'):
        dispatch(env, model, scope)
    assert not model.calls


def test_input_change_during_model_call_rejects_patch_and_closes_attempt(env):
    scope = plan(env)
    class ChangingModel(ScriptedModel):
        def complete(self, **kwargs):
            write(env[1], 'intake', {'facts': [{'label': 'Changed', 'value': 'new', 'source': 'fixture'}]})
            return super().complete(**kwargs)
    model = ChangingModel([tool_turn('save', 'submit_patch', patch()), final_turn('Stale')])
    assert dispatch(env, model, scope).submitted is None
    assert env[0].latest_artifact(RUN, 'writing').version == 1
    assert env[1].stage_state(RUN, 'writing').open_attempt is None


def test_plan_quote_identity_and_local_scope_cannot_expand(env):
    store, pipe, _, _ = env
    args = plan_args(store, quote='Not in current user message')
    with pytest.raises(PipelineError, match='原话'):
        plan_revision(store, pipe, RUN, args, turn_id='t', user_message=QUOTE)
    scope = plan(env)
    for args in (plan_args(store, mode='rework'), plan_args(store, path=['segments', 1, 'line'])):
        with pytest.raises(PipelineError, match='扩大'):
            plan_revision(store, pipe, RUN, args, turn_id='t', user_message=QUOTE, previous=scope)


class PlannedModel(ScriptedModel):
    def complete(self, **kwargs):
        if self.turns and callable(self.turns[0]):
            self.turns[0] = self.turns[0]()
        return super().complete(**kwargs)


def latest_plan(store):
    return next(e['scope']['plan_id'] for e in reversed(store.read_events(RUN)) if e['kind'] == 'agent_revision_planned')


def route(intent='edit'):
    return tool_turn('route', 'prepare_creative_context', {'intent': intent, 'format': 'video'})


def test_chat_routes_one_line_to_writer_and_blocks_whole_pipeline_rewrite(env):
    store, pipe, settings, ledger = env
    model = PlannedModel([
        route(), tool_turn('unbound', 'dispatch', {'stage': 'writing', 'brief': QUOTE}),
        tool_turn('plan', 'plan_revision', plan_args(store)),
        tool_turn('other', 'write_stage', {'stage': 'intake', 'content': 'Rewrite facts', 'summary': 'bad'}),
        lambda: tool_turn('dispatch', 'dispatch', {'stage': 'writing', 'brief': QUOTE, 'plan_id': latest_plan(store)}),
        tool_turn('patch', 'submit_patch', patch()),
        tool_turn('review', 'request_review', {'stage': 'writing'}),
        tool_turn('verdict', 'finish_review', {'verdict': 'pass', 'findings': []}),
        final_turn('已修改一句，等待查看新版')])
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(QUOTE)
    assert [t['tool'] for t in result.failed_tools] == ['dispatch', 'write_stage']
    assert store.latest_artifact(RUN, 'writing').version == 2
    assert store.latest_artifact(RUN, 'intake').version == 1
    assert pipe.stage_state(RUN, 'writing').review.verdict == 'pass'
    assert pipe.stage_state(RUN, 'writing').approval is None
    assert work_plan(pipe, RUN)['next_step']['action'] == 'user_decision'
    assert sum('submit_patch' in c['tool_names'] and 'dispatch' not in c['tool_names'] for c in model.calls) == 1
    tasks = [e for e in store.read_events(RUN) if e['kind'] == 'agent_task_prepared']
    assert len(tasks) == 1
    assert (tasks[0]['stage'], tasks[0]['role'], tasks[0]['plan_id']) == (
        'writing', 'writer', latest_plan(store))


def test_next_turn_cannot_reuse_old_plan_but_can_choose_new_scope(env):
    store, pipe, settings, ledger = env
    model = PlannedModel([route(), tool_turn('plan', 'plan_revision', plan_args(store)), final_turn('Planned'),
        route(), lambda: tool_turn('old', 'dispatch', {'stage': 'writing', 'brief': QUOTE, 'plan_id': latest_plan(store)}),
        tool_turn('new', 'plan_revision', plan_args(store, mode='rework')), final_turn('New scope')])
    main = Orchestrator(store, pipe, settings, model, ledger, RUN)
    main.send(QUOTE)
    result = main.send(QUOTE)
    assert [t['tool'] for t in result.failed_tools] == ['dispatch']
    assert main._revision_scope['mode'] == 'rework'


def test_intake_patch_uses_no_worker_and_preserves_other_facts(env):
    store, pipe, settings, ledger = env
    model = PlannedModel([route(), tool_turn('plan', 'plan_revision', plan_args(store, stage='intake', path=['facts', 0, 'value'])),
        lambda: tool_turn('patch', 'submit_patch', {**patch(['facts', 0, 'value'], 'Lunch bag'), 'plan_id': latest_plan(store)}),
        final_turn('Fact updated')])
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send(QUOTE)
    assert not result.failed_tools and len(model.calls) == 4
    assert store.latest_artifact(RUN, 'intake').structured['facts'][0]['source'] == 'fixture'
    assert store.latest_artifact(RUN, 'intake').version == 2
    assert pipe.stage_state(RUN, 'direction').stale


def test_readonly_scope_cannot_plan_or_patch(env):
    store, pipe, settings, ledger = env
    model = ScriptedModel([route('status'), tool_turn('plan', 'plan_revision', plan_args(store)),
                           tool_turn('patch', 'submit_patch', patch()), final_turn('Status only')])
    result = Orchestrator(store, pipe, settings, model, ledger, RUN).send('查看进度')
    assert [t['tool'] for t in result.failed_tools] == ['plan_revision', 'submit_patch']
    assert not any(e['kind'] == 'agent_revision_planned' for e in store.read_events(RUN))


def test_rework_receives_input_changes_and_updates_only_responsible_stage(env):
    store, pipe, _, _ = env
    old = store.latest_artifact(RUN, 'direction')
    intake = write(pipe, 'intake', {'facts': [{'label': 'Product', 'value': 'New supplied fact', 'source': 'fixture'}]})
    scope = plan(env, stage='direction', mode='rework')
    assert any(c['source'] == 'intake' and c['current'] == intake.sha256 for c in scope['input_changes'])
    model = ScriptedModel([tool_turn('save', 'submit_stage', {'content': '', 'summary': 'Rechecked direction',
        'structured': old.structured, 'evidence': ['fixture-source'], 'limitations': ['new fact considered']})])
    result = dispatch(env, model, scope)
    assert result.submitted == 'direction-v2' and len(model.calls) == 1
    assert 'New supplied fact' in model.requests_text()
    assert 'input_changes' in model.requests_text()
    assert 'submit_patch' not in model.calls[0]['tool_names']
    assert not pipe.stage_state(RUN, 'direction').stale
    assert store.latest_artifact(RUN, 'material_plan').version == 1
    assert pipe.stage_state(RUN, 'material_plan').stale
    assert result.impact['next_step']['action'] == 'review'


def test_cancelled_patch_closes_attempt_without_new_artifact(env):
    store, pipe, settings, ledger = env
    cancelled = False
    class CancellingModel(ScriptedModel):
        def complete(self, **kwargs):
            nonlocal cancelled
            cancelled = True
            return super().complete(**kwargs)
    def check_cancel():
        if cancelled:
            raise JobCancelled('fixture cancel')
    model = CancellingModel([tool_turn('save', 'submit_patch', patch())])
    scope = plan(env)
    with pytest.raises(JobCancelled):
        Dispatcher(store, pipe, settings, model, ledger, RUN, check_cancel=check_cancel).dispatch(
            'writing', QUOTE, revision_scope=scope)
    assert store.latest_artifact(RUN, 'writing').version == 1
    assert pipe.stage_state(RUN, 'writing').open_attempt is None
    assert len(ledger.entries()) == 1


def test_reference_change_between_plan_and_dispatch_is_not_overlooked(env):
    scope = plan(env)
    # Replace the original source snapshot: a genuine changed dependency,
    # independent of author-written text or stage version.
    from studio.evidence import evidence_fingerprint
    original = evidence_fingerprint(env[0], RUN)
    material_dir = env[0].run_dir(RUN) / 'materials'
    material_dir.mkdir(exist_ok=True)
    from PIL import Image
    Image.new('RGB', (8, 8), 'blue').save(material_dir / 'new.jpg')
    assert evidence_fingerprint(env[0], RUN) != original
    model = ScriptedModel([])
    with pytest.raises(PipelineError):
        dispatch(env, model, scope)
    assert not model.calls


def test_work_plan_returns_recovery_before_new_model_work(env):
    env[1].start_attempt(RUN, 'writing')
    result = work_plan(env[1], RUN, 'writing')
    assert result['next_step']['action'] == 'recover'
    assert all(r['action'] == 'preserve' for r in result['stages'][:-1])


def test_work_plan_routes_bound_review_problem_to_responsible_upstream(env):
    store, pipe, _, _ = env
    write(pipe, 'writing', store.latest_artifact(RUN, 'writing').structured)
    pipe.record_review(RUN, 'writing', verdict='return', findings=[
        {'stage': 'material_plan', 'issue': 'Missing proof shot', 'kind': 'evidence'}])
    result = work_plan(pipe, RUN, 'writing')
    assert result['next_step']['stage'] == 'material_plan'
    assert result['next_step']['action'] == 'revise'
    assert result['next_step']['responsible_findings'][0]['source_version'] == 2
    assert result['stages'][0]['action'] == result['stages'][1]['action'] == 'preserve'
    # Once this responsibility is updated, the old downstream review cannot
    # keep requesting revisions to the replacement material plan.
    write(pipe, 'material_plan', store.latest_artifact(RUN, 'material_plan').structured)
    result = work_plan(pipe, RUN, 'writing')
    assert result['stages'][2]['action'] == 'preserve'
    assert result['next_step']['stage'] == 'writing'
    assert result['next_step']['action'] == 'update'
