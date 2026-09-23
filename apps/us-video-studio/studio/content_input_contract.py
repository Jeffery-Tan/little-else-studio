"""Local, versioned reader for the US ContentInput v1 interchange format.

The importer intentionally does not depend on the other workbench's runtime.
This validates an input package; it never imports a plan or grants permissions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_UNSET = object()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')
    return value


def _local_path(root: Path, value: Any) -> Path:
    path = Path(_text(value, 'path'))
    if not path.is_absolute():
        path = root / path
        if not path.resolve().is_relative_to(root):
            raise ValueError('Relative source escapes repository')
    return path.resolve()


def _binding(path: Path, expected: Any = _UNSET) -> dict[str, str]:
    if expected is not _UNSET and (not isinstance(expected, str) or len(expected) != 64
                                  or any(char not in '0123456789abcdef' for char in expected)):
        raise ValueError('Source sha256 must be lowercase hexadecimal')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected is not _UNSET and digest != expected:
        raise ValueError(f'Changed source: {path}; create a new input version')
    return {'path': str(path), 'sha256': digest}


def read_content_input(path: Path, repository_root: Path) -> tuple[bytes, dict[str, Any], list[dict[str, str]]]:
    """Return exact package bytes, parsed data and verified source dependencies."""
    path = path.resolve()
    root = repository_root.resolve()
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('kind') != 'ContentInput' or data.get('track') != 'US':
        raise ValueError('Expected US ContentInput v1')
    for key in ('input_id', 'created_on', 'goal'):
        _text(data.get(key), key)
    if not isinstance(data.get('product'), dict) or not data['product']:
        raise ValueError('Product identity required')
    if not isinstance(data.get('constraints'), dict):
        raise ValueError('Constraints required')
    for key in ('sources', 'facts', 'observations', 'references', 'preferences'):
        if not isinstance(data.get(key), list):
            raise ValueError(f'{key} must be a list')

    sources: set[str] = set()
    dependencies = [_binding(path)]
    for source in data['sources']:
        if not isinstance(source, dict):
            raise ValueError('Source must be an object')
        source_id = _text(source.get('source_id'), 'source_id')
        if source_id in sources:
            raise ValueError('Duplicate source ID')
        sources.add(source_id)
        dependencies.append(_binding(_local_path(root, source.get('path')), source.get('sha256')))
    for fact in data['facts']:
        if not isinstance(fact, dict) or fact.get('source_id') not in sources or 'value' not in fact:
            raise ValueError('Fact must reference a known source and preserve unknown value')
        _text(fact.get('basis'), 'fact basis')

    assets: set[str] = set()
    for observation in data['observations']:
        if not isinstance(observation, dict):
            raise ValueError('Observation must be an object')
        asset_id = _text(observation.get('asset_id'), 'asset_id')
        if asset_id in assets:
            raise ValueError('Duplicate asset ID')
        assets.add(asset_id)
        if observation.get('method') not in {
            'file_exists', 'image_observed', 'frames_observed', 'transcript_only', 'full_av_observed'
        }:
            raise ValueError('Explicit observation method required')
        for key in ('coverage', 'limitations', 'observed_on'):
            _text(observation.get(key), key)
        dependencies.append(_binding(_local_path(root, observation.get('path')), observation.get('sha256')))
    return raw, data, dependencies
