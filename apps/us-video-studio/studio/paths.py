"""Studio owns creative knowledge; an optional business root supplies product facts."""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]
KNOWLEDGE_ROOT = PROJECT_ROOT


def business_root() -> Path | None:
    value = os.environ.get('STUDIO_BUSINESS_ROOT')
    if not value:
        return None
    if not Path(value).is_absolute():
        raise ValueError('STUDIO_BUSINESS_ROOT must be an absolute business repository path.')
    root = Path(value).resolve()
    if not root.is_dir():
        raise ValueError('STUDIO_BUSINESS_ROOT must be an existing business directory.')
    return root


BUSINESS_ROOT = business_root()


def document_path(root: Path, relative: str) -> Path:
    """Read Studio knowledge or an explicitly injected isolated test repository."""
    base = PROJECT_ROOT if relative.startswith('apps/us-video-studio/') and Path(root).resolve() == KNOWLEDGE_ROOT else Path(root)
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError('Document path escapes its configured root')
    return result


def is_source_path(path: Path) -> bool:
    path = Path(path).resolve()
    return any(path.is_relative_to(root) for root in (PROJECT_ROOT, KNOWLEDGE_ROOT, BUSINESS_ROOT) if root is not None)
