"""Logical input durability and failure-before-spend at the model boundary."""
import json

import pytest

from fakes import final_turn
from studio.agents.budget import BudgetedClient
from studio.agents.client import JobCancelled, ModelError, SafePreflightError
from studio.agents.requests import read_request
from studio.config import Settings
from studio.ledger import Ledger
from studio.store import Store

RUN = "request-fixture"


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Offline request fixture", "Synthetic only")
    return store, Ledger(store, Settings(tmp_path, "https://example.invalid"))


def call(env, inner, messages=None):
    return BudgetedClient(inner, env[1], run_id=RUN, role="director", stage="direction").complete(
        model="fixture", system="rules", messages=messages or [],
        tools=[{"name": "read_artifact", "input_schema": {"type": "object"}}])


def saved(env):
    events = [e for e in env[0].read_events(RUN) if e["kind"] == "agent_request_snapshot"]
    return events, [read_request(env[0], RUN, e["call_id"]) for e in events]


def test_every_request_saved_before_invocation_with_detached_full_content_and_no_credentials(env):
    original = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "prior",
                 "content": "complete tool result " * 2000, "is_error": False}]}]
    before = json.loads(json.dumps(original))
    class Fake:
        api_key = "SYNTHETIC_SECRET_NEVER_SERIALIZE"
        def complete(self, **kwargs):
            events, records = saved(env)
            assert records[-1]["request"] == kwargs
            assert records[-1]["request"]["messages"] == before
            original[0]["content"][0]["content"] = "caller changed"
            assert kwargs["messages"] == before
            return final_turn("done")
    call(env, Fake(), original)
    call(env, Fake(), before)
    events, records = saved(env)
    assert len(events) == len(env[1].entries(RUN)) == 2
    assert records[0]["request"]["messages"] == before
    assert "complete tool result" not in json.dumps(events)
    assert Fake.api_key not in json.dumps(records)
    assert records[0]["role"] == "director" and records[0]["stage"] == "direction"


def test_provider_failure_preserves_input_and_unknown_usage(env):
    class Fake:
        def complete(self, **kwargs):
            raise ModelError(0, "sent then disconnected", provider_invoked=True)
    with pytest.raises(ModelError):
        call(env, Fake())
    assert len(saved(env)[1]) == 1
    assert env[1].entries(RUN)[0].usage_status == "unknown"


def test_snapshot_failure_prevents_invocation_and_settles_zero(env, monkeypatch):
    import studio.agents.budget as budget
    class Fake:
        def complete(self, **kwargs):
            pytest.fail("must not call provider without durable request")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(budget, "save_request", fail)
    with pytest.raises(SafePreflightError):
        call(env, Fake())
    entry = env[1].entries(RUN)[0]
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status, entry.ok) == (0, 0, "known", False)


def test_unserializable_input_fails_before_intent(env):
    with pytest.raises(SafePreflightError):
        call(env, object(), [{"role": "user", "content": object()}])
    assert env[1].entries(RUN) == []
    assert saved(env)[0] == []


def test_cancellation_during_snapshot_prevents_provider_invocation(env):
    class Fake:
        def check_cancel(self):
            if saved(env)[0]:
                raise JobCancelled("cancelled while recording")
        def complete(self, **kwargs):
            pytest.fail("cancelled before provider")
    with pytest.raises(JobCancelled):
        call(env, Fake())
    entry = env[1].entries(RUN)[0]
    assert (entry.input_tokens, entry.output_tokens, entry.usage_status) == (0, 0, "known")


def test_diagnostic_reader_rejects_tampering_and_path_traversal(env):
    class Fake:
        def complete(self, **kwargs):
            return final_turn("done")
    call(env, Fake())
    event = saved(env)[0][0]
    path = env[0].run_dir(RUN) / event["evidence_ref"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint"):
        read_request(env[0], RUN, event["call_id"])
    with pytest.raises(ValueError, match="identity"):
        read_request(env[0], RUN, "../../another-run")
