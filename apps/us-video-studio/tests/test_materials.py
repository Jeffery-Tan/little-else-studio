"""Material containment: the names we accept, and the ones we refuse.

`studio/materials.py` is the only place in the app that turns a
browser-supplied string into a filesystem path, and one of those strings
arrives by drag-and-drop. So most of what follows is an attempt to escape. The
tests are the argument that it cannot.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

import pytest

from studio.materials import (
    IMAGE_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    VIDEO_EXTENSIONS,
    MaterialError,
    accept_upload,
    kind_of,
    list_materials,
    materials_dir,
    resolve,
    sanitize,
)
from studio.models import PipelineError
from studio.store import Store

RUN = "lunch-bag-01"


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag", "brief")
    return store


# --------------------------------------------------------------------------- #
# sanitize — accept
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "shot.jpg",
        "O1CN01XrQX.jpg",
        "50233e7f-8c1a-4b2e.png",
        "多功能包 01.png",
        "a.b.c.webp",
        "UPPER.PNG",
        "shot-2 (1).mp4",
    ],
)
def test_sanitize_keeps_ordinary_names(name):
    """Returned exactly as given — an artifact on disk references this string."""
    assert sanitize(name) == name


# --------------------------------------------------------------------------- #
# sanitize — refuse
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        " shot.jpg",          # leading whitespace
        "shot.jpg ",          # trailing space: the same file as `shot.jpg`
        "shot.jpg.",          # trailing dot: also the same file
        "shot.jpg .",
        ".",
        "..",
        ".hidden",
        "../shot.jpg",
        "..\\shot.jpg",
        "a/b.jpg",
        "a\\b.jpg",
        "C:/shot.jpg",
        "C:\\shot.jpg",
        "/etc/passwd",
        "shot.jpg:x",         # a colon opens an alternate data stream
        "a\x00b.jpg",
        "con",
        "con.txt",
        "NUL.png",
        "COM1.mp4",
        "x" * 121,
    ],
)
def test_sanitize_refuses_dangerous_names(name):
    assert sanitize(name) is None


@pytest.mark.parametrize("name", [None, 123, b"shot.jpg", ["shot.jpg"]])
def test_sanitize_refuses_a_non_string(name):
    assert sanitize(name) is None


def test_sanitize_accepts_a_name_at_exactly_the_length_limit():
    name = "x" * 116 + ".jpg"          # 120
    assert sanitize(name) == name
    assert sanitize("x" * 117 + ".jpg") is None


# --------------------------------------------------------------------------- #
# kind_of
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,want",
    [
        ("shot.jpg", "image"),
        ("shot.JPEG", "image"),
        ("clip.mp4", "video"),
        ("clip.MOV", "video"),
        ("notes.txt", "other"),
        ("noext", "other"),
        ("archive.zip", "other"),
        ("shot.jpg.exe", "other"),     # the extension decides, and it is `.exe`
    ],
)
def test_kind_comes_from_the_extension(name, want):
    assert kind_of(name) == want


@pytest.mark.parametrize("ext", sorted(IMAGE_EXTENSIONS))
def test_every_allowed_image_extension_is_an_image(ext):
    """A drift guard: the allowlist and `kind_of` cannot disagree."""
    assert kind_of(f"shot{ext}") == "image"


@pytest.mark.parametrize("ext", sorted(VIDEO_EXTENSIONS))
def test_every_allowed_video_extension_is_a_video(ext):
    assert kind_of(f"shot{ext}") == "video"


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #


def test_resolve_finds_a_real_file(store):
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    (base / "shot.jpg").write_bytes(b"x")
    assert resolve(store, RUN, "shot.jpg") == (base / "shot.jpg").resolve()


def test_resolve_returns_none_for_a_missing_file(store):
    assert resolve(store, RUN, "nope.jpg") is None


@pytest.mark.parametrize(
    "name", ["../run.json", "../../config.json", "..\\..\\config.json", "a/../../run.json"]
)
def test_resolve_refuses_to_leave_the_directory(store, name):
    assert resolve(store, RUN, name) is None


def test_resolve_refuses_a_directory_that_looks_like_a_file(store):
    (materials_dir(store, RUN) / "shots.jpg").mkdir(parents=True)
    assert resolve(store, RUN, "shots.jpg") is None


def test_resolve_refuses_a_symlink_that_leaves_the_directory(store, tmp_path):
    """The reason rule 3 runs on the *resolved* path.

    A name with no separators, no dots and no colon can still reach outside if
    the file it names is a symlink. `resolve()` follows it, so the parent is no
    longer `materials/` and the lookup fails.
    """
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    secret = tmp_path / "outside-secret.jpg"       # outside the run entirely
    secret.write_bytes(b"not yours")
    try:
        os.symlink(secret, base / "innocent.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege this shell does not have")
    assert resolve(store, RUN, "innocent.jpg") is None


def test_resolve_refuses_a_candidate_that_resolves_outside(store, tmp_path, monkeypatch):
    """Rule 3's comparison, tested without needing the symlink privilege.

    The test above skips on a shell that cannot create symlinks — which is most
    Windows shells. So `Path.resolve` is replaced instead, making the candidate
    appear to land outside `materials/`, exactly as it would if the name were a
    symlink. The parent check is the code under test, and it is the only thing
    standing between a followed link and a served file.
    """
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    (base / "shot.jpg").write_bytes(b"x")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"x")

    real_resolve = Path.resolve

    def fake(self, *args, **kwargs):
        if self.name == "shot.jpg":
            return outside
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake)

    # Without the parent check this returns `outside`, which is a readable file.
    assert outside.is_file()
    assert resolve(store, RUN, "shot.jpg") is None


def test_a_bad_run_id_is_refused_before_any_path_is_built(store):
    with pytest.raises(PipelineError):
        materials_dir(store, "../outside")


# --------------------------------------------------------------------------- #
# list_materials
# --------------------------------------------------------------------------- #


def test_list_is_empty_before_anything_has_been_dropped_in(store):
    assert list_materials(store, RUN) == []


def test_list_reports_kind_size_and_url(store):
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    (base / "b.png").write_bytes(b"12345")
    (base / "a.mp4").write_bytes(b"12")

    got = list_materials(store, RUN)

    assert [m["name"] for m in got] == ["a.mp4", "b.png"]
    assert got[0]["kind"] == "video" and got[0]["bytes"] == 2
    assert got[1]["url"] == "/api/runs/lunch-bag-01/materials/b.png"
    assert all(m["usable"] for m in got)


def test_list_marks_what_it_cannot_serve_instead_of_hiding_it(store):
    """The shelf and the folder must not disagree — he can open the folder."""
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    (base / "notes.txt").write_bytes(b"x")
    (base / "real.jpg").write_bytes(b"x")

    got = {m["name"]: m for m in list_materials(store, RUN)}

    assert got["notes.txt"]["usable"] is False
    assert got["notes.txt"]["url"] is None
    assert got["notes.txt"]["kind"] == "other"
    assert got["real.jpg"]["usable"] is True


def test_list_ignores_subdirectories(store):
    (materials_dir(store, RUN) / "sub").mkdir(parents=True)
    assert list_materials(store, RUN) == []


def test_a_chinese_name_survives_the_journey_to_a_url(store):
    base = materials_dir(store, RUN)
    base.mkdir(parents=True)
    (base / "多功能包 01.png").write_bytes(b"x")

    url = list_materials(store, RUN)[0]["url"]

    assert " " not in url and "多功能包" not in url      # quoted, not raw
    assert unquote(url.rsplit("/", 1)[1]) == "多功能包 01.png"


# --------------------------------------------------------------------------- #
# accept_upload
# --------------------------------------------------------------------------- #


def test_upload_writes_the_file_and_creates_the_directory(store):
    assert not materials_dir(store, RUN).exists()

    got = accept_upload(store, RUN, "shot.jpg", b"jpegbytes")

    assert got["kind"] == "image" and got["usable"] is True
    assert got["bytes"] == 9
    assert (materials_dir(store, RUN) / "shot.jpg").read_bytes() == b"jpegbytes"
    assert list_materials(store, RUN)[0]["name"] == "shot.jpg"


def test_upload_is_case_insensitive_about_the_extension(store):
    assert accept_upload(store, RUN, "SHOT.JPG", b"x")["kind"] == "image"


@pytest.mark.parametrize(
    "name",
    ["../escape.jpg", "..\\escape.jpg", "a/b.jpg", ".hidden.jpg", "con.jpg", "", "   "],
)
def test_upload_refuses_a_bad_name(store, name):
    with pytest.raises(MaterialError):
        accept_upload(store, RUN, name, b"x")


def test_upload_cannot_write_outside_the_run(store, tmp_path):
    """The whole point of this module. A traversal must leave nothing anywhere."""
    before = set(tmp_path.rglob("*"))

    with pytest.raises(MaterialError):
        accept_upload(store, RUN, "../escaped.jpg", b"x")

    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("name", ["notes.txt", "archive.zip", "script.js", "noext"])
def test_upload_refuses_a_format_it_could_not_show(store, name):
    with pytest.raises(MaterialError) as err:
        accept_upload(store, RUN, name, b"x")
    assert "只收图片和视频" in str(err.value)


def test_upload_refuses_an_empty_file(store):
    with pytest.raises(MaterialError):
        accept_upload(store, RUN, "shot.jpg", b"")


def test_upload_refuses_anything_over_the_size_limit(store, monkeypatch):
    monkeypatch.setattr("studio.materials.MAX_UPLOAD_BYTES", 8)
    with pytest.raises(MaterialError) as err:
        accept_upload(store, RUN, "shot.jpg", b"x" * 9)
    assert "太大" in str(err.value)


def test_upload_accepts_a_file_at_exactly_the_limit(store, monkeypatch):
    monkeypatch.setattr("studio.materials.MAX_UPLOAD_BYTES", 8)
    assert accept_upload(store, RUN, "shot.jpg", b"x" * 8)["bytes"] == 8


def test_upload_refuses_to_overwrite(store):
    """Replacing a photo would change what a signed-off artifact shows, without
    changing anything the artifact's fingerprint covers."""
    accept_upload(store, RUN, "shot.jpg", b"first")

    with pytest.raises(MaterialError) as err:
        accept_upload(store, RUN, "shot.jpg", b"second")

    assert "同名" in str(err.value)
    assert (materials_dir(store, RUN) / "shot.jpg").read_bytes() == b"first"


def test_a_failed_write_leaves_nothing_behind(store, monkeypatch):
    """A half-written photo the page then renders is worse than a failed upload."""

    def boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_bytes", boom)

    with pytest.raises(OSError):
        accept_upload(store, RUN, "shot.jpg", b"x")

    base = materials_dir(store, RUN)
    assert list(base.glob("*.part")) == []
    assert list_materials(store, RUN) == []


def test_the_size_limit_is_the_one_the_readme_states():
    assert MAX_UPLOAD_BYTES == 64 * 1024 * 1024


def test_upload_writes_material_uploaded_event(store):
    """Evidence generation advances only when an event is on the log."""
    from studio.evidence import evidence_generation

    before = evidence_generation(store, RUN)
    accept_upload(store, RUN, "shot.jpg", b"jpeg-bytes")
    events = [e for e in store.read_events(RUN) if e["kind"] == "material_uploaded"]
    assert len(events) == 1
    assert events[0]["name"] == "shot.jpg"
    assert evidence_generation(store, RUN) > before


def test_evidence_return_to_prior_bytes_stays_stale(tmp_path):
    """A→B→A: hash equality alone must not revive the old approval path."""
    from studio.evidence import evidence_generation
    from studio.pipeline import Pipeline

    store = Store(tmp_path)
    store.create_run(RUN, "Lunch bag", "brief")
    pipeline = Pipeline(store)

    accept_upload(store, RUN, "shot.jpg", b"bytes-a")
    attempt = pipeline.start_attempt(RUN, "intake")
    pipeline.submit(RUN, "intake", attempt.token, content="brief", summary="brief")
    attempt = pipeline.start_attempt(RUN, "direction")
    pipeline.submit(RUN, "direction", attempt.token, content="direction", summary="direction")
    assert not pipeline.stage_state(RUN, "direction").stale
    frozen = int(pipeline.stage_state(RUN, "direction").latest.inputs["evidence:generation"])

    target = materials_dir(store, RUN) / "shot.jpg"
    target.unlink()
    accept_upload(store, RUN, "shot.jpg", b"bytes-b")
    assert pipeline.stage_state(RUN, "direction").stale

    target.unlink()
    accept_upload(store, RUN, "shot.jpg", b"bytes-a")

    state = pipeline.stage_state(RUN, "direction")
    assert state.stale
    assert evidence_generation(store, RUN) > frozen

