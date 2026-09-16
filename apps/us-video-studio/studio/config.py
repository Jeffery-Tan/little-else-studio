"""Studio configuration.

Two separate concerns, deliberately kept apart:

* **Settings** live in ``<data_root>/config.json`` (outside Git) and can be edited
  by Jeffery. Roles, models and the spend cap are policy, not code.
* **Credentials** are read from the environment at call time and never written
  anywhere. Nothing in this module returns a key into a log or an artifact.

The default model per role is ``deepseek-flash`` for every role in v1. The
endpoint exposes exactly two models, ``deepseek-flash`` and ``deepseek-v4-pro``;
switching a role is a config edit, not a code change.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .agents.client import DEFAULT_MAX_TOKENS

DEFAULT_DATA_ROOT = Path("J:/E-Business-Video-Data/us-video-studio")

#: Endpoint the studio talks to. It is DeepSeek's Anthropic-compatible surface.
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
API_VERSION = "2023-06-01"

#: Models observed on this endpoint on 2026-09-17. ``deepseek-v4.1-flash`` does
#: not exist here; do not add it without re-checking ``GET /models``.
KNOWN_MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEFAULT_MODEL = "deepseek-flash"

#: Roles that produce a stage artifact or a review verdict.
ROLES = ("orchestrator", "director", "material", "writer", "reviewer")

#: Development-period cap, in estimated USD. Jeffery set this on 2026-09-17.
DEFAULT_BUDGET_LIMIT_USD = 10.0

#: The cap that is actually enforceable today.
#:
#: A USD cap can only stop spending if the price is known, and DeepSeek's rate
#: card has not been confirmed — so ``DEFAULT_PRICING`` carries zero rates and
#: the USD figure is a report, not a brake. Token counts *are* reported by the
#: API, so they are the limit that can be enforced right now.
#:
#: 2,000,000 tokens for the whole development period. At any plausible rate for
#: this endpoint that is comfortably inside $10, so hitting it means something
#: has gone wrong rather than that the budget was spent as intended. Once the
#: real rate card is filled in, ``budget_limit_usd`` takes over.
DEFAULT_BUDGET_LIMIT_TOKENS = 2_000_000


@dataclass(frozen=True)
class Pricing:
    """A price book used only to *estimate* dollars.

    Dollars are never a fact here: the real bill comes from DeepSeek's own
    console. Token counts are facts and are recorded as such.
    """

    source: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    as_of: str

    def estimate_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_usd_per_mtok / 1_000_000
            + output_tokens * self.output_usd_per_mtok / 1_000_000
        )


#: Placeholder rate card. It is deliberately *not* presented as DeepSeek's
#: real pricing; every ledger row carries ``source`` so the number is auditable.
#: Replace once the real DeepSeek rate card is confirmed.
DEFAULT_PRICING = Pricing(
    source="unverified-placeholder: DeepSeek public rate card not confirmed",
    input_usd_per_mtok=0.0,
    output_usd_per_mtok=0.0,
    as_of="2026-09-17",
)


@dataclass(frozen=True)
class Settings:
    data_root: Path
    base_url: str
    model_by_role: dict[str, str] = field(default_factory=dict)
    budget_limit_usd: float = DEFAULT_BUDGET_LIMIT_USD
    budget_limit_tokens: int = DEFAULT_BUDGET_LIMIT_TOKENS
    pricing: Pricing = DEFAULT_PRICING
    max_tool_rounds: int = 12
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_continuations: int = 1

    def model_for(self, role: str) -> str:
        return self.model_by_role.get(role, DEFAULT_MODEL)

    @property
    def usd_cap_is_enforceable(self) -> bool:
        """A zero rate card means the dollar figure cannot stop anything."""
        return self.pricing.input_usd_per_mtok > 0 or self.pricing.output_usd_per_mtok > 0


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable config file: {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def load_settings(data_root: Path | str | None = None) -> Settings:
    """Load settings from ``<data_root>/config.json``, falling back to defaults.

    An unknown role or an unknown model name is a hard error: a typo must not
    silently route a role to the wrong model.
    """
    root = Path(
        data_root or os.environ.get("STUDIO_DATA_ROOT") or DEFAULT_DATA_ROOT
    ).expanduser()

    raw = _read_json(root / "config.json")

    model_by_role: dict[str, str] = {}
    for role, model in (raw.get("models") or {}).items():
        if role not in ROLES:
            raise ValueError(f"Unknown role in config.json: {role!r}")
        if model not in KNOWN_MODELS:
            raise ValueError(
                f"Unknown model for role {role!r}: {model!r}. "
                f"Known models on this endpoint: {', '.join(KNOWN_MODELS)}"
            )
        model_by_role[role] = model

    pricing_raw = raw.get("pricing") or {}
    pricing = Pricing(
        source=pricing_raw.get("source", DEFAULT_PRICING.source),
        input_usd_per_mtok=float(
            pricing_raw.get("input_usd_per_mtok", DEFAULT_PRICING.input_usd_per_mtok)
        ),
        output_usd_per_mtok=float(
            pricing_raw.get("output_usd_per_mtok", DEFAULT_PRICING.output_usd_per_mtok)
        ),
        as_of=pricing_raw.get("as_of", DEFAULT_PRICING.as_of),
    )

    limit = raw.get("budget_limit_usd", DEFAULT_BUDGET_LIMIT_USD)
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit < 0:
        raise ValueError("budget_limit_usd must be a non-negative number")

    token_limit = raw.get("budget_limit_tokens", DEFAULT_BUDGET_LIMIT_TOKENS)
    if not isinstance(token_limit, int) or isinstance(token_limit, bool) or token_limit < 0:
        raise ValueError("budget_limit_tokens must be a non-negative whole number")

    return Settings(
        data_root=root,
        base_url=raw.get("base_url") or os.environ.get("STUDIO_BASE_URL") or DEFAULT_BASE_URL,
        model_by_role=model_by_role,
        budget_limit_usd=float(limit),
        budget_limit_tokens=token_limit,
        pricing=pricing,
        max_tool_rounds=int(raw.get("max_tool_rounds", 12)),
        max_tokens=int(raw.get("max_tokens", DEFAULT_MAX_TOKENS)),
        max_continuations=int(raw.get("max_continuations", 1)),
    )


def credentials() -> tuple[str, str]:
    """Return ``(base_url, token)`` from the environment.

    Never persist, log or return these values anywhere else.
    """
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or ""
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
    if not token:
        raise RuntimeError(
            "No model credential in the environment. Set ANTHROPIC_AUTH_TOKEN "
            "or ANTHROPIC_API_KEY before starting the studio."
        )
    return base_url, token
