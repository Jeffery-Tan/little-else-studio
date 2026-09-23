"""Execution can finish while a concrete deliverable is still missing."""
from types import SimpleNamespace

from studio.task_contract import evaluate


def stage(version=1, sha='exact', verdict=None):
    latest = SimpleNamespace(version=version, sha256=sha)
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


def test_reviewed_writing_is_a_deliverable_but_unreviewed_writing_is_not():
    pipeline = FakePipeline(writing=stage())
    missing = evaluate(FakeStore(), pipeline, 'fixture', 'writing')
    assert missing['status'] == 'incomplete'
    assert '独立审核' in missing['missing'][0]
    pipeline.states['writing'] = stage(verdict='pass')
    assert evaluate(FakeStore(), pipeline, 'fixture', 'writing')['status'] == 'complete'


def test_revision_requires_this_turns_applied_plan_and_review():
    scope = {'plan_id': 'plan-1', 'stage': 'writing', 'version': 1, 'sha256': 'old'}
    pipeline = FakePipeline(writing=stage(version=2, sha='new', verdict='pass'))
    assert evaluate(FakeStore(), pipeline, 'fixture', 'revision', revision_scope=scope)['status'] == 'incomplete'
    events = [{'kind': 'agent_revision_applied', 'plan_id': 'plan-1', 'sha256': 'new'}]
    assert evaluate(FakeStore(events), pipeline, 'fixture', 'revision', revision_scope=scope)['status'] == 'complete'


def test_production_input_must_match_current_source_and_be_checked(monkeypatch):
    from studio import production
    pipeline = FakePipeline(writing=stage(verdict='pass'))
    monkeypatch.setattr(production, 'source_snapshot', lambda store, run: 'current-snapshot')
    drafts = {'first': {'source_sha256': 'exact', 'source_snapshot': 'old-snapshot', 'kind': 'video', 'review': {'verdict': 'ready'}}}
    monkeypatch.setattr(production, 'draft_records', lambda store, run: drafts)
    assert evaluate(FakeStore(), pipeline, 'fixture', 'production_input', production_kind='video')['status'] == 'incomplete'
    drafts['first']['source_snapshot'] = 'current-snapshot'
    drafts['first']['review'] = {'verdict': 'revise'}
    assert '独立文字检查' in evaluate(FakeStore(), pipeline, 'fixture', 'production_input', production_kind='video')['missing'][0]
    drafts['first']['review'] = {'verdict': 'ready'}
    assert evaluate(FakeStore(), pipeline, 'fixture', 'production_input', production_kind='video')['status'] == 'complete'
    pipeline.approved = False
    assert '已确认' in evaluate(FakeStore(), pipeline, 'fixture', 'production_input', production_kind='video')['missing'][0]


def test_analysis_requires_saved_report_signal():
    assert evaluate(FakeStore(), FakePipeline(), 'fixture', 'analysis')['status'] == 'incomplete'
    assert evaluate(FakeStore(), FakePipeline(), 'fixture', 'analysis', analysis_completed=True)['status'] == 'complete'
