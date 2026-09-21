"""Execution outcomes owned by backend tools, never by model-authored prose.

Domain handlers still own artifact validation, approval and evidence checks.
The loop only understands whether a tool succeeded and whether its committed
result completes this role's work. This module has no store or provider access.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    concludes_turn: bool = False

    def __post_init__(self):
        if not isinstance(self.content, str):
            raise TypeError("tool result content must be text")
        if self.is_error and self.concludes_turn:
            raise ValueError("a failed tool cannot complete a turn")


def validate_call(name: str, arguments: Any, allowed: frozenset[str]) -> None:
    """Deny unavailable capabilities before invoking any domain handler."""
    if name not in allowed:
        raise ValueError(f"tool {name!r} is not available in this role's request")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")
