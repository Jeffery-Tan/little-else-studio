"""Codex native image tool through one isolated app-server turn.

The local protocol advertises the tool; that is not a real generation test.
The agent can choose more than one tool call: prompt instructions are not a
spending cap. Extra calls stop observation and leave usage unknown. The CLI may
keep its own generated_images cache; only validated inline bytes are imported.
No auth files or server-reported savedPath are read by this adapter.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Callable
import warnings

from PIL import Image

from .client import JobCancelled, ModelError, Usage
from . import codex_client as transport


MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_LINE = 40 * 1024 * 1024
IMAGE_TYPES = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
               "WEBP": ("image/webp", ".webp")}


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    output_dir: Path
    reference_paths: tuple[Path, ...] = ()
    reference_root: Path | None = None
    model: str = ""


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    sha256: str
    mime: str
    width: int
    height: int


@dataclass
class ImageResult:
    status: str
    images: tuple[GeneratedImage, ...] = ()
    usage: Usage = field(default_factory=lambda: Usage(None, None))
    observed_image_calls: int = 0
    provider_invoked: bool = True
    # Text tokens do not establish the native image provider's usage.
    usage_status: str = "unknown"
    message: str = ""
    model: str = ""
    thread_id: str = ""
    turn_id: str = ""


class _ImageSession(transport._StdioSession):
    """The native image payload exceeds the text adapter's line-size bound."""

    def _pump(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    return
                try:
                    event = json.loads(line)
                except ValueError:
                    return
                if not isinstance(event, dict):
                    return
                self.messages.put(event)
        finally:
            self.messages.put(None)


def _image_info(data: bytes):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image size is outside the supported range")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                fmt = picture.format
                width, height = picture.size
                if (fmt not in IMAGE_TYPES or width * height > MAX_IMAGE_PIXELS
                        or getattr(picture, "n_frames", 1) != 1):
                    raise ValueError("Only bounded static PNG, JPEG or WebP images are supported")
                picture.verify()
            with Image.open(io.BytesIO(data)) as picture:
                picture.load()
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Image data is invalid") from exc
    return (*IMAGE_TYPES[fmt], width, height)


def _inputs(request: ImageRequest):
    if (not isinstance(request.prompt, str) or not request.prompt.strip()
            or len(request.prompt) > 30_000 or len(request.reference_paths) > 5):
        raise transport.CodexUnavailable("Supply an image prompt and at most five reference images")
    output = Path(request.output_dir)
    if not output.is_absolute() or output.is_symlink():
        raise transport.CodexUnavailable("Image output needs an absolute, non-symlink directory")
    # A task-specific empty output directory makes filenames deterministic and
    # prevents accidental reuse of outputs from a previous request.
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise transport.CodexUnavailable("Image output directory must be empty")
    content = [{"type": "text", "text": request.prompt}]
    reference_bytes = 0
    if request.reference_paths:
        if request.reference_root is None or not Path(request.reference_root).is_absolute():
            raise transport.CodexUnavailable("Reference images need an explicit allowed directory")
        root = Path(request.reference_root).resolve(strict=True)
        for given in request.reference_paths:
            path = Path(given)
            if not path.is_absolute() or not path.resolve(strict=True).is_relative_to(root):
                raise transport.CodexUnavailable("Reference image is outside the allowed directory")
            if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                raise transport.CodexUnavailable("Reference image is unavailable or too large")
            data = path.read_bytes()
            reference_bytes += len(data)
            if reference_bytes > MAX_IMAGE_BYTES:
                raise transport.CodexUnavailable("Combined reference images are too large")
            mime, _, _, _ = _image_info(data)
            content.append({"type": "image", "url": f"data:{mime};base64," + base64.b64encode(data).decode("ascii")})
    return content, output


def _verify_image_policy(metadata):
    if metadata.get("features", {}).get("image_generation") is not True:
        raise transport.CodexUnavailable("Codex image generation is not enabled for this isolated session")
    checked = {**metadata, "features": {**metadata["features"], "image_generation": False}}
    transport._verify_policy(checked)


def _verify_image_schema(command: str, directory: Path):
    output = directory / "image-schema"
    result = subprocess.run(
        [command, "app-server", "generate-json-schema", "--experimental", "--out", str(output)],
        capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise transport.CodexUnavailable("Codex image protocol could not be inspected")
    try:
        item = json.loads((output / "v2" / "ItemCompletedNotification.json").read_text("utf-8"))
        variants = item["definitions"]["ThreadItem"]["oneOf"]
        image = next(value for value in variants if value.get("properties", {}).get("type", {}).get("enum") == ["imageGeneration"])
        if not {"result", "status", "savedPath", "failure"} <= image["properties"].keys():
            raise ValueError()
        caps = json.loads((output / "v2" / "ModelProviderCapabilitiesReadResponse.json").read_text("utf-8"))
        if caps["properties"]["imageGeneration"] != {"type": "boolean"}:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise transport.CodexUnavailable("Codex lacks the required native-image protocol") from exc


class CodexImageClient:
    def __init__(self, *, command: str = "codex", timeout: float = 600,
                 check_cancel: Callable[[], None] | None = None):
        self.command, self.timeout, self.check_cancel = command, timeout, check_cancel
        self._mcp_names = None
        self._capability = None

    def capability(self):
        """Local-only probe. Does not log in, create a turn or call a model."""
        client = transport.CodexClient(run_id="image-probe", role="material", data_root=".",
                                       command=self.command, timeout=min(self.timeout, 30))
        info = client.preflight()
        with tempfile.TemporaryDirectory(prefix="studio-image-probe-") as directory:
            _verify_image_schema(client.command, Path(directory))
            config = transport._overrides(client._mcp_names or ())
            config["features.image_generation"] = True
            session = _ImageSession(client.command, Path(directory), config, min(self.timeout, 30))
            try:
                session.initialize()
                _verify_image_policy(session.policy_metadata())
                supported = session.request("modelProvider/capabilities/read", {}).get("imageGeneration") is True
            finally:
                session.close()
        self.command, self._mcp_names = client.command, client._mcp_names
        self._capability = {"backend": "codex_cli", "version": info["version"], "available": supported,
                            "verified": False, "model_invoked": False, "single_image_limit_enforced": False,
                            "image_usage_available": False,
                            "reason": ("Local native-image protocol is available; real generation has not been verified"
                                       if supported else "The selected Codex provider does not advertise native image generation")}
        return dict(self._capability)

    def generate(self, request: ImageRequest) -> ImageResult:
        """One explicitly authorized agent turn; caller owns intent and settlement.

        Preflight errors assert no invocation. Once turn/start is sent, failures
        return unknown usage and preserve any already validated output files.
        No request or tool failure is retried.
        """
        try:
            content, output = _inputs(request)
            if self.check_cancel:
                self.check_cancel()
            capability = self._capability or self.capability()
            if not capability["available"]:
                raise transport.CodexUnavailable(capability["reason"])
            output.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, JobCancelled) as exc:
            raise transport.CodexUnavailable("Image request or local reference could not be validated") from exc
        config = transport._overrides(self._mcp_names or ())
        config["features.image_generation"] = True
        result = ImageResult(status="unknown")
        images, seen, completed = [], set(), set()
        invoked = False
        with tempfile.TemporaryDirectory(prefix="studio-image-turn-") as directory:
            cwd = Path(directory)
            try:
                session = _ImageSession(self.command, cwd, config, self.timeout)
            except OSError as exc:
                raise transport.CodexUnavailable("Codex image session could not start") from exc
            try:
                initialized = session.initialize()
                _verify_image_policy(session.policy_metadata())
                setup = session.request("thread/start", {
                    **({"model": request.model} if request.model else {}), "modelProvider": "openai",
                    "allowProviderModelFallback": False, "cwd": str(cwd), "runtimeWorkspaceRoots": [str(cwd)],
                    "environments": [], "selectedCapabilityRoots": [], "ephemeral": True,
                    "sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user",
                    "baseInstructions": (
                        "Generate exactly one image using image_gen.imagegen. Do not retry or make another image call, "
                        "including after a failure. Use only the supplied prompt and inline reference images. "
                        "For editing use num_last_images_to_include equal to the number of supplied images; "
                        "never use referenced_image_paths. Do not use other tools. Return a short final message."),
                    "developerInstructions": "", "dynamicTools": [],
                })
                result.thread_id = transport._verify_thread(setup, cwd, request.model, transport._global_instruction_sources(initialized))
                result.model = setup["model"]
                if session.denied_request:
                    raise transport.CodexUnavailable("Codex requested an unsupported setup action")
                if self.check_cancel:
                    self.check_cancel()
                session.deadline = time.monotonic() + self.timeout
                invoked = True
                started = session.request("turn/start", {
                    "threadId": result.thread_id, "model": result.model, "cwd": str(cwd), "environments": [],
                    "runtimeWorkspaceRoots": [str(cwd)], "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "input": content,
                })
                result.turn_id = started.get("turn", {}).get("id") or ""
                if not result.turn_id:
                    raise ModelError(0, "Missing image turn identity")
                while True:
                    if self.check_cancel:
                        self.check_cancel()
                    event = session.event()
                    if session.denied_request:
                        raise ValueError("Unexpected native approval request")
                    params = event.get("params", {})
                    if (params.get("threadId") != result.thread_id
                            or params.get("turnId", params.get("turn", {}).get("id")) != result.turn_id):
                        continue
                    method = event.get("method")
                    if method == "thread/tokenUsage/updated":
                        total = params.get("tokenUsage", {}).get("total", {})
                        for key, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
                            if type(total.get(key)) is int and total[key] >= 0:
                                setattr(result.usage, target, total[key])
                    elif method == "model/rerouted":
                        if isinstance(params.get("toModel"), str) and params["toModel"]:
                            result.model = params["toModel"]
                    elif method in {"item/started", "item/completed"}:
                        item = params.get("item", {})
                        if item.get("type") == "imageGeneration":
                            item_id = item.get("id")
                            if not isinstance(item_id, str) or not item_id:
                                raise ValueError("Missing image identity")
                            seen.add(item_id)
                            result.observed_image_calls = len(seen)
                            if len(seen) > 1:
                                raise ValueError("Multiple native image calls observed")
                            if method == "item/completed" and item_id not in completed:
                                completed.add(item_id)
                                if item.get("status") != "completed" or item.get("failure"):
                                    result.status, result.message = "failed", "Codex image generation failed; usage requires verification"
                                    break
                                encoded = item.get("result", "")
                                if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                                    raise ValueError("Image payload is too large")
                                data = base64.b64decode(encoded, validate=True)
                                mime, extension, width, height = _image_info(data)
                                destination = output / ("image-1" + extension)
                                with destination.open("xb") as handle:
                                    handle.write(data)
                                images.append(GeneratedImage(destination, hashlib.sha256(data).hexdigest(), mime, width, height))
                        elif item.get("type") not in {"userMessage", "agentMessage", "reasoning"}:
                            raise ValueError("Unexpected native tool")
                    elif method == "turn/completed":
                        ok = params.get("turn", {}).get("status") == "completed" and len(images) == 1
                        result.status = "completed" if ok else "failed"
                        result.message = "Image saved; image usage requires verification" if ok else "No complete image result; usage requires verification"
                        break
            except (ModelError, OSError, ValueError, binascii.Error, JobCancelled):
                if not invoked:
                    raise transport.CodexUnavailable("Codex image setup failed; no model request sent") from None
                result.status, result.message = "unknown", "Image task interrupted or returned an unsupported result; verify usage before continuing"
            finally:
                session.close()
        result.images = tuple(images)
        return result
