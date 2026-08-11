#!/usr/bin/env python3
"""Validate the project's Windows-safe text encoding contract."""

from __future__ import annotations

import argparse
import codecs
from pathlib import Path


HUMAN_READABLE_SUFFIXES = {".md", ".csv"}
MACHINE_READABLE_SUFFIXES = {
    ".json",
    ".ps1",
    ".psm1",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "node_modules",
}


def iter_project_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRECTORY_NAMES for part in relative_parts):
            continue
        if path.suffix.lower() in HUMAN_READABLE_SUFFIXES | MACHINE_READABLE_SUFFIXES:
            yield path


def validate_text_file(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    has_utf8_bom = raw.startswith(codecs.BOM_UTF8)
    errors: list[str] = []

    if suffix in HUMAN_READABLE_SUFFIXES and not has_utf8_bom:
        errors.append("human-readable Markdown/CSV must use UTF-8 with BOM")
    if suffix in MACHINE_READABLE_SUFFIXES and has_utf8_bom:
        errors.append("machine-readable source/config must use UTF-8 without BOM")

    encoding = "utf-8-sig" if has_utf8_bom else "utf-8"
    try:
        text = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        errors.append(f"invalid UTF-8 at byte {exc.start}")
        return errors

    if "\ufffd" in text:
        errors.append("contains Unicode replacement character U+FFFD")
    return errors


def validate_project(root: Path) -> tuple[int, list[str]]:
    checked = 0
    errors: list[str] = []
    for path in iter_project_text_files(root):
        checked += 1
        for error in validate_text_file(path):
            relative_path = path.relative_to(root).as_posix()
            errors.append(f"{relative_path}: {error}")
    return checked, errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    checked, errors = validate_project(root)
    if errors:
        for error in errors:
            print(f"ENCODING_ERROR: {error}")
        print(f"Text encoding validation: FAIL ({len(errors)} errors in {checked} files)")
        return 1
    print(f"Text encoding validation: PASS ({checked} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

