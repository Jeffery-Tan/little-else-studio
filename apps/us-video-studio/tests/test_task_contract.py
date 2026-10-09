"""Execution can finish while a concrete deliverable is still missing."""
from types import SimpleNamespace

from studio.task_contract import evaluate


def stage(version=1, sha='exact', verdict=None):
    latest = SimpleNamespace(version=version, sha256=sha,
                             structured={'segments': [{'picture': 'Synthetic complete shot'}]})
    review = SimpleNamespace(verdict=verdict, artifact_sha256=sha) if verdict else None
    return SimpleNamespace(latest=latest, stale=False, integrity_ok=True, open_attempt=None, review=review)


class FakePipeline:
    def __init__(self, **states):
        self.states = states
        self.approved = True

    def stage_state(self, run_id, name):
        return self.states[name]

    def is_complete(self, run_id, name):
        return self.approved


class FakeStore:
    def __init__(self, events=()):
        self.events = list(events)

    def read_events(self, run_id):
        return self.events


def test_saved_writing_is_a_deliverable_without_independent_review():
    pipeline = FakePipeline(writing=stage())
    assert evaluate(FakeStore(), pipeline, 'fixture', 'writing')['status'] == 'complete'
    pipeline.states['writing'] = stage(verdict='pass')
    assert evaluate(FakeStore(), pipeline, 'fixture', 'writing')['status'] == 'complete'


def test_revision_requires_this_turns_applied_plan():
    scope = {'plan_id': 'plan-1', 'stage': 'writing', 'version': 1, 'sha256': 'old'}
    pipeline = FakePipeline(writing=stage(version=2, sha='new', verdict='pass'))
    assert evaluate(FakeStore(), pipeline, 'fixture', 'revision', revision_scope=scope)['status'] == 'incomplete'
    events = [{'kind': 'agent_revision_applied', 'plan_id': 'plan-1', 'sha256': 'new'}]
    assert evaluate(FakeStore(events), pipeline, 'fixture', 'revision', revision_scope=scope)['status'] == 'complete'


def test_production_input_requires_an_adopted_script_before_any_shot_is_checked(monkeypatch):
    """The upstream reason is named first; a script nobody adopted has no inputs.

    This test used to monkeypatch ``draft_records`` and assert only that the word
    "已确认" appeared. The contract now reads the real production projection, so a
    fake store would be testing the fake. The shot-level rules are covered against
    a real store in ``test_shots.py``; what matters here is the ordering: an
    unadopted script is reported as such rather than as missing inputs.
    """
    from studio import production

    pipeline = FakePipeline(writing=stage(verdict='pass'))
    pipeline.approved = False
    result = evaluate(FakeStore(), pipeline, 'fixture', 'production_input',
                      production_kind='video')
    assert result['status'] == 'incomplete'
    assert result['missing'] == ['已确认的当前完整方案']
    assert '采用' in result['note']

    # Media has its own wording: the blocker is upstream, not a failed generation.
    media = evaluate(FakeStore(), pipeline, 'fixture', 'production_media', media_kind='image')
    assert media['status'] == 'incomplete'
    assert media['missing'] == ['当前脚本待用户采用']


def test_a_read_only_follow_up_promises_nothing_and_asks_for_nothing():
    result = evaluate(FakeStore(), FakePipeline(), 'fixture', 'follow_up')
    assert result['status'] == 'informational'
    assert result['missing'] == []
    assert evaluate(FakeStore(), FakePipeline(), 'fixture', None)['status'] == 'unspecified'


def test_analysis_requires_saved_report_signal():
    assert evaluate(FakeStore(), FakePipeline(), 'fixture', 'analysis')['status'] == 'incomplete'
    assert evaluate(FakeStore(), FakePipeline(), 'fixture', 'analysis', analysis_completed=True)['status'] == 'complete'
