"""view_images returns real images as image blocks, and reads the skills bucket only under this session."""
from __future__ import annotations

import pytest

from tools import view_images as vi

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

ROOTS = ["projects/p1/sessions/s1/output/", "artifacts/s1/output/"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example")
    monkeypatch.setattr(vi, "_session_roots", lambda: ROOTS)
    reads: list = []
    store = {
        ("skills", "projects/p1/sessions/s1/output/inline/f1.jpg"): JPEG,
        ("skills", "artifacts/s1/output/f2.png"): PNG,
        ("skills", "artifacts/s1/output/notes.txt"): b"hello",
        ("proxy", "micro-workflow/shot-segmentation/w/shot_images/shot_000061.jpg"): JPEG,
    }

    def read_s3(bucket, key):
        reads.append((bucket, key))
        return store[(bucket, key)]

    monkeypatch.setattr(vi, "_read_s3", read_s3)
    import tools.result_files as rf
    monkeypatch.setattr(rf, "_presign_many", lambda uris: {u: None for u in uris})
    return reads


def _images(result):
    return [c["image"] for c in result["content"] if "image" in c]


def test_session_outputs_come_back_as_images():
    r = vi.view_images._tool_func(refs=[
        "https://cdn.example/projects/p1/sessions/s1/output/inline/f1.jpg",
        "s3://skills/artifacts/s1/output/f2.png",
    ], note="the street")
    assert r["status"] == "success"
    assert [i["format"] for i in _images(r)] == ["jpeg", "png"]
    assert _images(r)[0]["source"]["bytes"] == JPEG
    assert "Looking for: the street" in r["content"][0]["text"]


def test_other_sessions_and_projects_are_refused(_env):
    r = vi.view_images._tool_func(refs=[
        "https://cdn.example/projects/p2/assets/x.jpg",
        "s3://skills/artifacts/other-session/output/f.jpg",
    ])
    assert r["status"] == "error" and not _images(r)
    assert "only this session's own output files" in r["content"][-1]["text"]
    assert _env == []  # nothing was read


def test_job_outputs_fall_back_to_a_direct_read():
    r = vi.view_images._tool_func(refs=["s3://proxy/micro-workflow/shot-segmentation/w/shot_images/shot_000061.jpg"])
    assert [i["format"] for i in _images(r)] == ["jpeg"]


def test_non_images_and_bad_refs_are_reported_not_shown():
    r = vi.view_images._tool_func(refs=["s3://skills/artifacts/s1/output/notes.txt", "file:///etc/passwd", "f1.jpg"])
    assert r["status"] == "error"
    text = r["content"][-1]["text"]
    assert "not a jpeg, png, gif or webp image" in text and "not an https url or s3:// uri" in text


def test_at_most_eight_images():
    refs = [f"https://cdn.example/artifacts/s1/output/f{i}.png" for i in range(10)]
    r = vi.view_images._tool_func(refs=refs)
    assert sum(1 for line in r["content"][-1]["text"].splitlines() if "at most 8" in line) == 2


def test_signatures():
    assert vi.image_format(JPEG) == "jpeg" and vi.image_format(PNG) == "png"
    assert vi.image_format(b"GIF89a....") == "gif"
    assert vi.image_format(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert vi.image_format(b"<html>") is None


def test_the_model_reads_how_to_use_it():
    spec = vi.view_images.tool_spec
    assert spec["name"] == "view_images"
    assert "frames" in spec["description"] and "code sandbox" in spec["description"]
