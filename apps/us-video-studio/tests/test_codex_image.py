"""Synthetic native-image events only: no Codex process or provider requests."""
import base64
import copy
import io
from pathlib import Path

from PIL import Image
import pytest

from studio.agents import codex_image as adapter
from studio.agents import codex_client as transport
from studio.agents.client import ModelError


def png():
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(stream, format="PNG")
    return stream.getvalue()


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread-image", "turnId": "turn-image", **params}}


def image(item_id="image-call", **changes):
    return {"id": item_id, "type": "imageGeneration", "status": "completed",
            "result": base64.b64encode(png()).decode(), "savedPath": "C:/untrusted/private.png", **changes}


class Session:
    events = []
    instances = []
    denied_request = False
    setup_fails = False

    def __init__(self, command, cwd, config, timeout):
        self.cwd, self.config = cwd, config
        self.stream = iter(copy.deepcopy(self.events))
        self.sent, self.closed = [], False
        self.instances.append(self)

    def initialize(self):
        return {}

    def policy_metadata(self):
        return {"mcp_enabled": {}, "features": {**dict.fromkeys(transport.DISABLED_FEATURES, False),
                                                 "image_generation": True, "code_mode_host": True},
                "skip_host_skill_discovery": True, "web_search": "disabled", "sandbox_mode": "read-only",
                "approval_policy": "never", "model_provider": "openai", "forced_login_method": "chatgpt",
                "project_doc_max_bytes": 0, "developer_instructions_empty": True, "agents_disabled": True}

    def request(self, method, params):
        self.sent.append((method, params))
        if self.setup_fails:
            raise ModelError(0, "Synthetic setup failure")
        if method == "thread/start":
            return {"sandbox": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never",
                    "modelProvider": "openai", "model": params.get("model", "fixture-model"), "cwd": str(self.cwd),
                    "runtimeWorkspaceRoots": [str(self.cwd)], "instructionSources": [],
                    "thread": {"id": "thread-image", "ephemeral": True, "environments": [], "turns": []}}
        if method == "turn/start":
            return {"turn": {"id": "turn-image"}}
        raise AssertionError(method)

    def event(self):
        try:
            return next(self.stream)
        except StopIteration:
            raise ModelError(0, "Synthetic disconnect")

    def close(self):
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    Session.instances, Session.events, Session.setup_fails, Session.denied_request = [], [], False, False
    monkeypatch.setattr(adapter, "_ImageSession", Session)
    value = adapter.CodexImageClient()
    value._capability = {"available": True}
    value._mcp_names = ()
    return value


def complete():
    return [event("item/completed", item=image()),
            event("thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 101, "outputTokens": 35},
                                                           "last": {"inputTokens": 7, "outputTokens": 3}}),
            event("turn/completed", turn={"id": "turn-image", "status": "completed"})]


def test_image_bytes_imported_not_untrusted_saved_path(client, tmp_path):
    Session.events = complete()
    result = client.generate(adapter.ImageRequest("A blue product", tmp_path / "output"))
    assert result.status == "completed"
    assert len(result.images) == 1
    assert result.images[0].path.read_bytes() == png()
    assert result.images[0].mime == "image/png"
    assert (result.images[0].width, result.images[0].height) == (3, 2)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (101, 35)
    assert result.usage_status == "unknown"
    assert result.observed_image_calls == 1
    assert Session.instances[0].closed


def test_reference_input_is_inline_and_other_tools_disabled(client, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(png())
    Session.events = complete()
    client.generate(adapter.ImageRequest("Edit this reference", tmp_path / "out", (reference,), tmp_path))
    session = Session.instances[0]
    assert session.config["features.image_generation"] is True
    assert session.config["features.code_mode_host"] is True
    assert session.config["features.code_mode"] is False
    assert session.config["features.shell_tool"] is False
    assert session.config["features.view_image"] is False
    thread = next(params for method, params in session.sent if method == "thread/start")
    turn = next(params for method, params in session.sent if method == "turn/start")
    assert thread["environments"] == []
    assert thread["dynamicTools"] == []
    assert turn["input"][1]["url"].startswith("data:image/png;base64,")
    assert str(reference) not in str(turn)


def test_out_of_root_reference_rejected_before_invocation(client, tmp_path):
    reference = tmp_path / "reference.png"
    reference.write_bytes(png())
    root = tmp_path / "allowed"
    root.mkdir()
    with pytest.raises(transport.CodexUnavailable) as error:
        client.generate(adapter.ImageRequest("edit", tmp_path / "out", (reference,), root))
    assert error.value.provider_invoked is False
    assert not Session.instances


@pytest.mark.parametrize("data", [b"not an image", b"<svg></svg>", b""])
def test_bad_reference_rejected(client, tmp_path, data):
    reference = tmp_path / "reference.png"
    reference.write_bytes(data)
    with pytest.raises(transport.CodexUnavailable):
        client.generate(adapter.ImageRequest("edit", tmp_path / "out", (reference,), tmp_path))
    assert not Session.instances


def test_existing_output_never_overwritten(client, tmp_path):
    (tmp_path / "image-1.png").write_bytes(b"existing")
    with pytest.raises(transport.CodexUnavailable):
        client.generate(adapter.ImageRequest("image", tmp_path))
    assert (tmp_path / "image-1.png").read_bytes() == b"existing"


def test_second_image_call_unknown_preserves_first_output(client, tmp_path):
    Session.events = [event("item/completed", item=image()),
                      event("item/started", item=image("second", status="in_progress", result=""))]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "unknown"
    assert result.observed_image_calls == 2
    assert result.failure_code == 'protocol_or_payload'
    assert len(result.images) == 1
    assert result.images[0].path.exists()
    assert sum(method == "turn/start" for method, _ in Session.instances[0].sent) == 1


def test_duplicate_event_does_not_duplicate_image(client, tmp_path):
    Session.events = [event("item/started", item=image(status="in_progress", result="")),
                      event("item/completed", item=image()), *complete()]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "completed" and len(result.images) == 1
    assert result.observed_image_calls == 1


@pytest.mark.parametrize("item", [image(result="not-base64!!!"), image(result=base64.b64encode(b"not png").decode()),
                                 {"id": "shell", "type": "commandExecution"}])
def test_invalid_result_or_other_native_tool_leaves_usage_unknown(client, tmp_path, item):
    Session.events = [event("item/completed", item=item)]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "unknown"
    assert result.failure_code in {'protocol_or_payload', 'payload_decode'}
    assert not result.images
    assert result.usage.input_tokens is None


def test_code_mode_wrapper_is_allowed_but_other_wrappers_are_rejected(client, tmp_path):
    Session.events = [event("item/completed", item={"id": "wrapper", "type": "functionCallOutput",
                                                    "namespace": "functions", "name": "exec", "output": "done"}),
                      *complete()]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "completed" and result.observed_image_calls == 1

    Session.events = [event("item/completed", item={"id": "other", "type": "functionCallOutput",
                                                    "namespace": "functions", "name": "unknown", "output": ""})]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "another"))
    assert result.status == "unknown" and result.failure_code == "protocol_or_payload"


def test_disconnect_after_image_preserves_valid_file(client, tmp_path):
    Session.events = [event("item/completed", item=image())]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "unknown"
    assert result.failure_code == 'transport_or_timeout'
    assert len(result.images) == 1
    assert result.usage_status == "unknown"


def test_native_failure_not_retried(client, tmp_path):
    Session.events = [event("item/completed", item=image(status="failed", result="", failure={"type": "usageLimitExceeded"}))]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "failed"
    assert result.usage_status == "unknown"
    assert len(Session.instances) == 1


def test_setup_failure_asserts_no_model_invocation(client, tmp_path):
    Session.setup_fails = True
    with pytest.raises(transport.CodexUnavailable) as error:
        client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert error.value.provider_invoked is False
    assert Session.instances[0].closed
    assert not any(method == "turn/start" for method, _ in Session.instances[0].sent)


def test_no_image_is_failed_not_fake_success(client, tmp_path):
    Session.events = [event("turn/completed", turn={"id": "turn-image", "status": "completed"})]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "failed" and not result.images


def test_other_thread_events_ignored(client, tmp_path):
    unrelated = event("item/completed", item=image("unrelated"))
    unrelated["params"]["threadId"] = "other-thread"
    Session.events = [unrelated, *complete()]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "completed" and result.observed_image_calls == 1


def test_capability_is_local_only_and_does_not_overclaim(monkeypatch, tmp_path):
    class Probe:
        command = "synthetic-codex"
        _mcp_names = ()

        def __init__(self, **kwargs):
            pass

        def preflight(self):
            return {"version": "fixture-version"}

    class ProbeSession(Session):
        def request(self, method, params):
            assert method == "modelProvider/capabilities/read"
            return {"imageGeneration": True, "namespaceTools": True}

    monkeypatch.setattr(transport, "CodexClient", Probe)
    monkeypatch.setattr(adapter, "_verify_image_schema", lambda *args: None)
    monkeypatch.setattr(adapter, "_ImageSession", ProbeSession)
    result = adapter.CodexImageClient().capability()
    assert result["available"] is True
    assert result["verified"] is False
    assert result["model_invoked"] is False
    assert result["image_usage_available"] is False
    assert result["single_image_limit_enforced"] is False


def test_rerouted_model_reported(client, tmp_path):
    Session.events = [event("model/rerouted", toModel="actual-fixture-model"), *complete()]
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.model == "actual-fixture-model"


def test_image_policy_does_not_accept_shell_tool_enabled():
    metadata = Session("", Path("."), {}, 1).policy_metadata()
    metadata["features"]["shell_tool"] = True
    with pytest.raises(transport.CodexUnavailable):
        adapter._verify_image_policy(metadata)


def test_image_policy_requires_only_code_mode_host_exception():
    metadata = Session("", Path("."), {}, 1).policy_metadata()
    metadata["features"]["code_mode_host"] = False
    with pytest.raises(transport.CodexUnavailable):
        adapter._verify_image_policy(metadata)
    metadata["features"]["code_mode_host"] = True
    metadata["features"]["code_mode"] = True
    with pytest.raises(transport.CodexUnavailable):
        adapter._verify_image_policy(metadata)


def test_cancel_after_invocation_returns_unknown_and_closes(client, tmp_path):
    from studio.agents.client import JobCancelled
    checks = []

    def check():
        checks.append(True)
        if len(checks) == 3:
            raise JobCancelled()

    client.check_cancel = check
    result = client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert result.status == "unknown" and result.usage_status == "unknown"
    assert result.failure_code == 'cancelled'
    assert Session.instances[0].closed


def test_cancel_before_invocation_reports_safe_preflight(client, tmp_path):
    from studio.agents.client import JobCancelled

    def check():
        raise JobCancelled()

    client.check_cancel = check
    with pytest.raises(transport.CodexUnavailable) as error:
        client.generate(adapter.ImageRequest("image", tmp_path / "out"))
    assert error.value.provider_invoked is False
    assert not Session.instances
