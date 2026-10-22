from __future__ import annotations

import pytest

from scraper.engine.http import looks_like_login, parse_content_disposition, sniff
from scraper.engine.manifest import Manifest
from scraper.engine.paths import PathAllocator, safe_component


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("Week 1: Intro?", "Week 1 - Intro_"),
        ("a/b\\c", "a_b_c"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("..", "untitled"),
        ("  trailing dots...  ", "trailing dots"),
        ("", "untitled"),
        (None, "untitled"),
    ],
)
def test_safe_component(raw, safe):
    assert safe_component(raw) == safe


def test_safe_component_caps_length():
    assert len(safe_component("x" * 500)) == 80


def test_allocator_is_stable_per_resource_and_unique_across(tmp_path):
    alloc = PathAllocator(tmp_path)
    a = alloc.claim(["Week 1"], "Lecture", key="a")
    assert alloc.claim(["Week 1"], "Lecture", key="a") == a
    assert alloc.claim(["Week 1"], "Lecture", key="b").name == "Lecture (2)"
    assert alloc.claim(["Week 1"], "lecture", key="c").name == "lecture (3)"  # case-insensitive on Windows


def test_allocator_avoids_files_it_does_not_own(tmp_path):
    (tmp_path / "Lecture.pdf").write_bytes(b"someone else's")
    alloc = PathAllocator(tmp_path)
    assert alloc.claim([], "Lecture", key="new").name == "Lecture (2)"


def test_allocator_reuses_files_recorded_for_the_same_resource(tmp_path):
    (tmp_path / "Lecture.pdf").write_bytes(b"%PDF")
    manifest = Manifest(tmp_path, tmp_path / ".state")
    manifest.record("key-1", "https://x/l.pdf", [tmp_path / "Lecture.pdf"])
    alloc = PathAllocator(tmp_path, owner_of=manifest.owner_of)
    assert alloc.claim([], "Lecture", key="key-1").name == "Lecture"
    assert alloc.claim([], "Lecture", key="key-2").name == "Lecture (2)"


def test_allocator_never_escapes_the_output_dir(tmp_path):
    alloc = PathAllocator(tmp_path)
    base = alloc.claim(["..", "..", "evil"], "../../x", key="k")
    assert tmp_path.resolve() in base.parents


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("attachment; filename=\"Lecture_92925_public.pdf\"; filename*=UTF-8''Lecture_9%2F29%2F25_public.pdf",
         "Lecture_9-29-25_public.pdf"),
        ('attachment; filename="[RESOLVE AI] Stanford CS146S.pdf"', "[RESOLVE AI] Stanford CS146S.pdf"),
        ("attachment; filename=notes.txt", "notes.txt"),
        ("inline", None),
        (None, None),
    ],
)
def test_content_disposition(header, name):
    assert parse_content_disposition(header) == name


def test_sniff():
    assert sniff(b"%PDF-1.7") == "pdf"
    assert sniff(b"PK\x03\x04rest") == "zip"
    assert sniff(b"  <!DOCTYPE html><html>") == "html"
    assert sniff(b"\x00\x00\x00\x18ftypmp42") == "mp4"
    assert sniff(b"plain text") == "unknown"


def test_login_detection():
    assert looks_like_login("<html></html>", "https://accounts.google.com/ServiceLogin?continue=x")
    assert looks_like_login("<form><input type=\"password\"></form>", "https://lms.edu/page")
    assert looks_like_login("<title>Sign in - SSO</title>", "https://x.edu/")
    assert not looks_like_login("<title>Lecture 3</title><p>slides</p>", "https://course.edu/l3")
