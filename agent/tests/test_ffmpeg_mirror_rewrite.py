"""A sandbox cell's static-ffmpeg download goes to the harness's STAGED build (presigned S3), never a third party.

Staging 2026-10-19, plan 7f882700 node "assemble": the cell's first command curl'ed the third-party static-ffmpeg
release with no timeout; the download stalled and held the node until its 600 s limit, on both attempts, so the
stitch never ran. The artifacts CDN's old public `tools/ffmpeg/` path is closed since 0.10.0, so it is rewritten too.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tool_input_normalize as tin  # noqa: E402

THIRD = "https://" + "john" + "vansickle.com" + "/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
CDN = "https://dexamplecdn002.cloudfront.net/tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz"
SIGNED = "https://bucket.s3.us-west-2.amazonaws.com/tools/ffmpeg/{}?X-Amz-Signature=x"


def _use(action: dict) -> dict:
    return {"toolUseId": "t1", "name": "code_interpreter", "input": {"code_interpreter_input": {"action": action}}}


def _action(tu: dict) -> dict:
    return tu["input"]["code_interpreter_input"]["action"]


def _sign(monkeypatch):
    monkeypatch.setattr(tin, "staged_ffmpeg_url", lambda tarball: SIGNED.format(tarball))


def test_a_third_party_shell_download_gets_the_staged_build(monkeypatch):
    _sign(monkeypatch)
    out = tin.mirror_ffmpeg_downloads_in_code(_use({"type": "executeCommand", "command": f"curl -sL -o ff.tar.xz {THIRD} && tar xf ff.tar.xz"}))
    assert _action(out)["command"] == "curl -sL -o ff.tar.xz " + SIGNED.format("ffmpeg-release-amd64-static.tar.xz") + " && tar xf ff.tar.xz"


def test_the_closed_cdn_path_python_cells_and_written_files_too(monkeypatch):
    _sign(monkeypatch)
    out = tin.mirror_ffmpeg_downloads_in_code(_use({"type": "executeCode", "code": f"urlretrieve('{CDN}', 'a')"}))
    assert SIGNED.format("ffmpeg-release-arm64-static.tar.xz") in _action(out)["code"]
    files = tin.mirror_ffmpeg_downloads_in_code(_use({"type": "writeFiles", "content": [{"path": "get.sh", "text": f"wget {THIRD}"}]}))
    assert "vansickle" not in _action(files)["content"][0]["text"]


def test_untouched_when_nothing_can_be_signed_or_nothing_matches(monkeypatch):
    monkeypatch.setattr(tin, "staged_ffmpeg_url", lambda tarball: "")
    tu = _use({"type": "executeCommand", "command": f"curl {THIRD}"})
    assert tin.mirror_ffmpeg_downloads_in_code(tu) is tu
    _sign(monkeypatch)
    plain = _use({"type": "executeCode", "code": "print(1)"})
    assert tin.mirror_ffmpeg_downloads_in_code(plain) is plain


def test_the_signed_url_is_for_the_staged_key(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    tin._ffmpeg_url_cache.clear()
    url = tin.staged_ffmpeg_url("ffmpeg-release-arm64-static.tar.xz")
    assert url.startswith("https://skills-bucket.s3.us-west-2.amazonaws.com/tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz?")
    monkeypatch.delenv("SKILLS_S3_BUCKET")
    tin._ffmpeg_url_cache.clear()
    assert tin.staged_ffmpeg_url("ffmpeg-release-arm64-static.tar.xz") == ""


def test_the_wrapper_applies_it_before_the_cell_runs():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "tool_input_normalize.py")).read()
    assert "presign_artifact_urls_in_code(mirror_ffmpeg_downloads_in_code(" in src
    assert "mirror_ffmpeg_downloads_in_text(skill_scripts.writer_code(files))" in src
