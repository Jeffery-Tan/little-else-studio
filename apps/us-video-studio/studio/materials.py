"""The files a run actually has, and the rules that keep them contained.

Layout::

    runs/<run-id>/materials/     real product photos and clips — copied in by
                                 hand, or dropped on the window

Two callers read this module. The server uses it to list, serve and accept
files. The material agent uses it — through the ``list_materials`` tool — to
find out which filenames it is allowed to write into an artifact's ``file``
field. The agent cannot see the filesystem; without that tool it would invent
filenames, and an artifact claiming a photo that does not exist is worse than
one that admits it has none.

SECURITY — this is a module rather than four lines in ``server.py`` because
every path in it is derived from a name the browser supplied, one of them by
drag-and-drop. Three rules, checked in this order:

1. The name must be a bare filename: no separators, no drive letter, no ``:``
   (on Windows a colon opens an alternate data stream — ``shot.jpg:x``), no
   NUL, no leading dot, not ``.`` or ``..``, no trailing dot or space (Windows
   strips those, so ``shot.jpg.`` and ``shot.jpg`` are the same file), and not
   one of the reserved device names (``CON``, ``NUL``, ``COM1``…).
2. The extension must be on the allowlist, and it is the extension that decides
   whether the page is told this is an image or a video.
3. The **resolved** path's direct parent must be the materials directory.

Rule 3 is the one that actually holds. Rules 1 and 2 exist so a rejected name
gets a reason instead of a bare 404; if rule 1 is ever wrong about some platform
quirk, ``..`` still cannot escape, because ``resolve()`` runs first and the
comparison is on the resolved path.

Nothing here touches the network, and nothing here writes outside
``materials/``. Every function is testable offline.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .store import Store

#: What the page may be handed. Kept small on purpose: this list is the
#: difference between "a folder of product photos" and "a folder of whatever
#: was dropped on the window, which a browser will then try to interpret".
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
)
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".webm", ".mov", ".m4v"})

#: Bytes, one file at a time. A phone clip is the realistic worst case and sits
#: well under this; anything past it is a mistake rather than a material.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: Windows refuses to create these names in any directory, extension or not.
_RESERVED: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

#: Long enough for a Chinese product name, short enough to stay clear of the
#: Windows path limit once the data root has been prefixed to it.
MAX_NAME_LENGTH = 120


class MaterialError(ValueError):
    """A rejected upload or lookup. The message is shown to Jeffery as written."""


def sanitize(name: str) -> str | None:
    """``name`` as a safe bare filename, or None if it cannot be one.

    Validation only — the name is returned **exactly as given**, never trimmed
    or normalised. That matters for two reasons. An artifact on disk holds the
    filename it was written with, so a rewrite here would break a reference that
    already exists. And a name that has to be repaired in order to be safe is a
    name we would rather not accept: ``shot.jpg `` and ``shot.jpg`` are the same
    file on Windows, so trimming one into the other would silently overwrite.
    """
    if not isinstance(name, str):
        return None
    if not name or len(name) > MAX_NAME_LENGTH:
        return None
    if name != name.strip() or name != name.rstrip(" ."):
        return None
    # `Path(name).name` would quietly drop a directory part. We refuse instead:
    # a client that sent `../shot.jpg` is not making a naming mistake.
    if name != Path(name).name:
        return None
    if "/" in name or "\\" in name or "\x00" in name or ":" in name:
        return None
    if name.startswith("."):
        return None
    if name.split(".")[0].lower() in _RESERVED:
        return None
    return name


def kind_of(name: str) -> str:
    """``"image"``, ``"video"`` or ``"other"`` — from the extension alone."""
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def materials_dir(store: Store, run_id: str) -> Path:
    """The one directory this module ever writes to.

    ``Store.run_dir`` validates the run id, so ``../`` cannot arrive by that
    route either.
    """
    return store.run_dir(run_id) / "materials"


def material_url(run_id: str, name: str) -> str:
    """Where the page fetches it.

    Quoted rather than trusted: these are filenames, and filenames contain
    spaces and Chinese.
    """
    return f"/api/runs/{quote(run_id, safe='')}/materials/{quote(name, safe='')}"


def resolve(store: Store, run_id: str, name: str) -> Path | None:
    """Absolute path to one material, or None if ``name`` may not name one.

    Rule 3: the resolved path's direct parent must be the materials directory.
    Checked after ``resolve()``, so symlinks and ``..`` are already gone.
    """
    safe = sanitize(name)
    if safe is None:
        return None
    base = materials_dir(store, run_id).resolve()
    candidate = (base / safe).resolve()
    if candidate.parent != base:
        return None
    if not candidate.is_file():
        return None
    return candidate


def list_materials(store: Store, run_id: str) -> list[dict[str, Any]]:
    """Every regular file in the directory, in a stable order.

    A file this module would refuse to serve is still listed, with
    ``usable: False`` and no URL, rather than hidden. Hiding it would make the
    shelf and the folder disagree — and the folder is the one Jeffery can open
    in Explorer.
    """
    base = materials_dir(store, run_id)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        safe = sanitize(path.name)
        kind = kind_of(path.name) if safe else "other"
        usable = safe is not None and kind != "other"
        out.append(
            {
                "name": path.name,
                "kind": kind,
                "bytes": path.stat().st_size,
                "url": material_url(run_id, path.name) if usable else None,
                "usable": usable,
            }
        )
    return out


def accept_upload(store: Store, run_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """Write one uploaded file into the run's materials directory.

    Refuses to overwrite, deliberately. A file already there may be the one an
    artifact's ``file`` field points at, and the artifact's fingerprint covers
    its *text* — replacing the picture would change what a signed-off document
    shows without changing anything that could be checked. Jeffery can delete
    the old file; this module will not do it for him.
    """
    safe = sanitize(filename)
    if safe is None:
        raise MaterialError(f"文件名不能用：{filename!r}")
    kind = kind_of(safe)
    if kind == "other":
        allowed = " ".join(sorted(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS))
        raise MaterialError(f"只收图片和视频。认得的格式：{allowed}")
    if not data:
        raise MaterialError("文件是空的。")
    if len(data) > MAX_UPLOAD_BYTES:
        limit = MAX_UPLOAD_BYTES // 1048576
        raise MaterialError(f"文件太大：{len(data) / 1048576:.1f} MB，上限 {limit} MB。")

    base = materials_dir(store, run_id)
    target = (base / safe).resolve()
    if target.parent != base.resolve():
        raise MaterialError(f"文件名不能用：{filename!r}")
    if target.exists():
        raise MaterialError(f"已经有同名文件了：{safe}。先删掉它，或者改个名再传。")

    base.mkdir(parents=True, exist_ok=True)
    # Written beside the target and moved into place, the same way
    # `store._atomic_write_text` does it: a half-written photo that the page
    # then renders is worse than a failed upload.
    partial = target.with_name(target.name + ".part")
    try:
        partial.write_bytes(data)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)

    # Advances the evidence generation (CLAUDE.md red line 7). Without this
    # event an A→B→A return of file bytes could silently revive an old
    # approval that only compares content hashes.
    with store.run_lock(run_id):
        store.append_event(
            run_id,
            "material_uploaded",
            name=safe,
            media_kind=kind,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    return {
        "name": safe,
        "kind": kind,
        "bytes": len(data),
        "url": material_url(run_id, safe),
        "usable": True,
    }
