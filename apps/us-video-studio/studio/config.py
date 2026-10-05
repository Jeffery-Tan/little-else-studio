"""Budget settings and compatibility loading for historical Studio config.json.

New workbench jobs use connections.ConnectionStore: each role binds a provider
and arbitrary model, defaults to Codex CLI, and freezes routing per job. API keys
entered in the workbench use Windows DPAPI outside Git; no environment keys are
imported into those connections. Pricing is keyed by connection and exact model.

The legacy loader and credentials() helper below remain for explicit historical
callers only. Loading that old format never rewrites it or silently migrates
existing keys. New server routing does not call credentials().
"""
from __future__ import annotations

import json
import math
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

#: Historical configuration fields retain the retired reviewer binding.
ROLES = ("orchestrator", "director", "material", "writer", "reviewer")
#: Roles eligible for ordinary new creative jobs.
ACTIVE_ROLES = ("orchestrator", "director", "material", "writer")

#: Historical optional soft limits. New data roots do not impose spend caps.
#: Existing numeric config.json values are still accepted; null disables a cap.
DEFAULT_BUDGET_LIMIT_USD = None

DEFAULT_BUDGET_LIMIT_TOKENS = None


@dataclass(frozen=True)
class Pricing:
    """A price book used only to *estimate* dollars.

    Dollars are never a fact here: the real bill comes from DeepSeek's own
    console. Token counts are facts and are recorded as such.
    """

    source: str
    input_usd_per_mtok: float | None
    output_usd_per_mtok: float | None
    as_of: str
    input_cny_per_mtok: float | None = None
    output_cny_per_mtok: float | None = None

    def estimate_cny(self, input_tokens: int, output_tokens: int) -> float | None:
        rates = (self.input_cny_per_mtok, self.output_cny_per_mtok)
        if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in rates):
            return None
        if not any(rates) or 'placeholder' in self.source.lower():
            return None
        return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000

    @property
    def known(self) -> bool:
        return (self.input_usd_per_mtok is not None and self.output_usd_per_mtok is not None
                and all(math.isfinite(rate) and rate >= 0 for rate in (
                    self.input_usd_per_mtok, self.output_usd_per_mtok))
                and (self.input_usd_per_mtok > 0 or self.output_usd_per_mtok > 0)
                and "placeholder" not in self.source.lower())

    def estimate_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        if not self.known:
            return None
        return (
            input_tokens * self.input_usd_per_mtok / 1_000_000
            + output_tokens * self.output_usd_per_mtok / 1_000_000
        )


#: Unknown rate card. It is deliberately *not* presented as DeepSeek's
#: real pricing; every ledger row carries ``source`` so the number is auditable.
#: Replace once the real DeepSeek rate card is confirmed.
DEFAULT_PRICING = Pricing(
    source="unverified-placeholder: DeepSeek public rate card not confirmed",
    input_usd_per_mtok=None,
    output_usd_per_mtok=None,
    as_of="2026-09-17",
)


@dataclass(frozen=True)
class Settings:
    data_root: Path
    base_url: str
    model_by_role: dict[str, str] = field(default_factory=dict)
    budget_limit_usd: float | None = DEFAULT_BUDGET_LIMIT_USD
    budget_limit_tokens: int | None = DEFAULT_BUDGET_LIMIT_TOKENS
    pricing: Pricing = DEFAULT_PRICING
    max_tool_rounds: int = 12
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_continuations: int = 1
    connection_by_role: dict[str, str] = field(default_factory=dict)
    provider_by_role: dict[str, str] = field(default_factory=dict)
    pricing_by_route: dict[tuple[str, str], Pricing] = field(default_factory=dict)

    def model_for(self, role: str) -> str:
        return self.model_by_role.get(role, DEFAULT_MODEL)

    def pricing_for(self, role: str, model: str) -> Pricing:
        if role not in self.connection_by_role:
            return self.pricing  # Existing explicitly loaded legacy configuration.
        connection = self.connection_by_role[role]
        if self.provider_by_role.get(role) == 'codex_cli':
            return Pricing('unknown: Codex CLI subscription cost is not an API rate', None, None, '')
        return self.pricing_by_route.get((connection, model),
            Pricing('unknown: no price for this connection/model', None, None, ''))

    @property
    def usd_estimate_available(self) -> bool:
        """Whether the configured rate cards can estimate every role's USD."""
        if self.connection_by_role:
            return all(self.pricing_for(role, self.model_for(role)).known
                       for role in self.connection_by_role)
        return self.pricing.known

    @property
    def usd_cap_is_enforceable(self) -> bool:
        return self.budget_limit_usd is not None and self.usd_estimate_available


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
    def rate(name):
        value = pricing_raw.get(name, getattr(DEFAULT_PRICING, name))
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number or null")
        return float(value)

    pricing = Pricing(
        source=pricing_raw.get("source", DEFAULT_PRICING.source),
        input_usd_per_mtok=rate("input_usd_per_mtok"),
        output_usd_per_mtok=rate("output_usd_per_mtok"),
        as_of=pricing_raw.get("as_of", DEFAULT_PRICING.as_of),
    )

    limit = raw.get("budget_limit_usd", DEFAULT_BUDGET_LIMIT_USD)
    if limit is not None and (not isinstance(limit, (int, float)) or isinstance(limit, bool)
                              or not math.isfinite(limit) or limit < 0):
        raise ValueError("budget_limit_usd must be a non-negative number or null")

    token_limit = raw.get("budget_limit_tokens", DEFAULT_BUDGET_LIMIT_TOKENS)
    if token_limit is not None and (not isinstance(token_limit, int)
                                    or isinstance(token_limit, bool) or token_limit < 0):
        raise ValueError("budget_limit_tokens must be a non-negative whole number or null")

    return Settings(
        data_root=root,
        base_url=raw.get("base_url") or os.environ.get("STUDIO_BASE_URL") or DEFAULT_BASE_URL,
        model_by_role=model_by_role,
        budget_limit_usd=float(limit) if limit is not None else None,
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
