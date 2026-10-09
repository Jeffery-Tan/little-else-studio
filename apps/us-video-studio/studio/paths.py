"""Explicit ownership of application code and the external US business library."""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parents[1]


def business_root() -> Path:
    value = os.environ.get('STUDIO_BUSINESS_ROOT')
    if not value or not Path(value).is_absolute():
        raise ValueError('Set STUDIO_BUSINESS_ROOT to the absolute E-commerce business repository path.')
    root = Path(value).resolve()
    if not (root / 'tracks/us/creative/learning/library.json').is_file():
        raise ValueError('STUDIO_BUSINESS_ROOT must contain the US learning library; no empty library is created.')
    return root


BUSINESS_ROOT = business_root()


def document_path(root: Path, relative: str) -> Path:
    """Application method cards ship with this version; business evidence stays external."""
    base = PROJECT_ROOT if relative.startswith('apps/us-video-studio/') and Path(root).resolve() == BUSINESS_ROOT else Path(root)
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise ValueError('Document path escapes its configured root')
    return result


def is_source_path(path: Path) -> bool:
    path = Path(path).resolve()
    return any(path.is_relative_to(root) for root in (PROJECT_ROOT, BUSINESS_ROOT))
