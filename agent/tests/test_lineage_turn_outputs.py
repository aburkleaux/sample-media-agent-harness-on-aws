"""Per-turn own-writes ledger + lineage (asset-lineage design §4, R3.4, R6.1).

The ledger records what THIS runtime wrote during a turn (artifact upload/adopt/import, crop_video
outputs, code_interpreter results) and what each output was made from, in canonical
``s3://bucket/key`` form. It feeds the render filter (an invented-uuid output written this turn is
still ours) and the turn-end ``lineage`` field.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from media import artifact_fetch, turn_outputs  # noqa: E402

BUCKET = "skills-bucket"
CDN = "d123.cloudfront.net"
SID = "833a079f-1111-4222-8333-444455556666"
INVENTED = "e34ebb5c-aaaa-4bbb-8ccc-ddddeeeeffff"
OTHER = "5ced0ead-90dd-4ff1-bb3d-e1e0c067e98c"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    monkeypatch.delenv("ARTIFACTS_PREFIX", raising=False)
    import jobs.context as jc
    tok = jc._current_session_id.set(SID)
    turn_outputs.open_turn(now=1_000_000.0)
    yield
    jc._current_session_id.reset(tok)
    turn_outputs.open_turn()


class _Head:
    """A fake S3 client whose head_object reports a LastModified per key."""

    def __init__(self, modified: dict):
        self.modified = modified
        self.calls: list[str] = []

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3 casing
        self.calls.append(Key)
        if Key not in self.modified:
            raise RuntimeError("404")
        return {"LastModified": datetime.fromtimestamp(self.modified[Key], tz=timezone.utc)}


def _ci_result(stdout: str) -> dict:
    return {"status": "success", "content": [{"text": stdout}]}


def _ci_input(code: str) -> dict:
    return {"code_interpreter_input": {"action": {"type": "executeCode", "language": "python",
                                                  "code": code}}}


# --------------------------------------------------------------------- canonical_location

@pytest.mark.parametrize("ref,expected", [
    (f"https://{CDN}/artifacts/{SID}/output/clip.mp4", f"s3://{BUCKET}/artifacts/{SID}/output/clip.mp4"),
    (f"/api/artifact?key=artifacts/{SID}/output/clip.mp4", f"s3://{BUCKET}/artifacts/{SID}/output/clip.mp4"),
    (f"https://ui.example/api/artifact?key=projects/p1/a1/v.mp4", f"s3://{BUCKET}/projects/p1/a1/v.mp4"),
    (f"https://{BUCKET}.s3.us-west-2.amazonaws.com/projects/p1/a1/v.mp4?X-Amz-Signature=x",
     f"s3://{BUCKET}/projects/p1/a1/v.mp4"),
    # The M2C proxy bucket's presigned GET (another bucket entirely) keeps ITS bucket.
    ("https://m2c-proxy.s3.amazonaws.com/micro-workflow/x/shots.json?X-Amz-Expires=60",
     "s3://m2c-proxy/micro-workflow/x/shots.json"),
    ("https://s3.us-west-2.amazonaws.com/m2c-proxy/a/b.mp4?sig=1", "s3://m2c-proxy/a/b.mp4"),
    ("https://s3-media.s3.amazonaws.com/a/b.mp4", "s3://s3-media/a/b.mp4"),
    ("s3://m2c-proxy/a/b.mp4", "s3://m2c-proxy/a/b.mp4"),
    (f"artifacts/{SID}/output/clip.mp4", f"s3://{BUCKET}/artifacts/{SID}/output/clip.mp4"),
    ("projects/p1/a1/v.mp4", f"s3://{BUCKET}/projects/p1/a1/v.mp4"),
])
def test_canonical_location_normalises_every_form(ref, expected):
    assert artifact_fetch.canonical_location(ref) == expected


@pytest.mark.parametrize("ref", [
    "https://v3.fal.media/files/x.mp4", "https://other.example/artifacts/x.mp4",
    "artifacts/", f"artifacts/{SID}/output/", "artifacts/../etc/passwd", "s3://b/", "", None,
    "notes.txt",
])
def test_canonical_location_rejects_foreign_and_partial_refs(ref):
    assert artifact_fetch.canonical_location(ref) is None


def test_refs_in_text_finds_urls_and_bare_keys_once():
    text = (f"saved https://{CDN}/artifacts/{SID}/output/a.mp4 and artifacts/{SID}/output/a.mp4.\n"
            f"also s3://{BUCKET}/projects/p1/a1/v.mp4, key = f\"artifacts/{{sid}}/output/{{n}}\"")
    assert turn_outputs.refs_in_text(text) == [
        f"s3://{BUCKET}/artifacts/{SID}/output/a.mp4",
        f"s3://{BUCKET}/projects/p1/a1/v.mp4",
    ]


# --------------------------------------------------------------------- artifact / crop_video

def test_artifact_adopt_and_import_are_own_writes_with_import_lineage():
    turn_outputs.note_tool_result(
        "artifact", {"operation": "adopt", "key": "report.pdf"},
        {"status": "success", "content": [{"text": json.dumps(
            {"status": "adopted", "key": f"artifacts/{SID}/output/report.pdf", "url": "u"})}]})
    turn_outputs.note_tool_result(
        "artifact", {"operation": "import", "source_uri": "s3://m2c-proxy/job/shots.json"},
        {"status": "success", "content": [{"text": json.dumps(
            {"status": "imported", "key": f"artifacts/{SID}/output/shots.json"})}]})
    assert turn_outputs.is_own_write(f"https://{CDN}/artifacts/{SID}/output/report.pdf")
    assert turn_outputs.lineage() == [
        {"output": f"s3://{BUCKET}/artifacts/{SID}/output/report.pdf", "inputs": [], "tool": "artifact"},
        {"output": f"s3://{BUCKET}/artifacts/{SID}/output/shots.json",
         "inputs": ["s3://m2c-proxy/job/shots.json"], "tool": "artifact"},
    ]


def test_an_artifact_error_records_nothing():
    turn_outputs.note_tool_result("artifact", {"operation": "adopt"},
                                  {"status": "success", "content": [{"text": json.dumps({"error": "x"})}]})
    assert turn_outputs.lineage() == [] and not turn_outputs.own_writes()


def test_crop_video_records_source_to_output_lineage():
    src = f"https://{CDN}/projects/p1/a1/source.mp4"
    turn_outputs.note_tool_result(
        "crop_video", {"source_url": src, "box": "0,0,0.5,1"},
        {"status": "success", "content": [{"text": json.dumps(
            {"status": "cropped", "key": f"artifacts/{SID}/output/crop_ab12.mp4"})}]})
    assert turn_outputs.lineage() == [{
        "output": f"s3://{BUCKET}/artifacts/{SID}/output/crop_ab12.mp4",
        "inputs": [f"s3://{BUCKET}/projects/p1/a1/source.mp4"], "tool": "crop_video"}]


# --------------------------------------------------------------------- code_interpreter

def test_code_interpreter_result_ref_in_this_session_is_an_output_without_a_head(monkeypatch):
    head = _Head({})
    monkeypatch.setattr(artifact_fetch, "_client", lambda: head)
    src = f"https://{CDN}/projects/p1/a1/source.mp4"
    code = f"import boto3\nurllib.request.urlretrieve('{src}', 'in.mp4')\n# ffmpeg ...\n"
    turn_outputs.note_tool_result(
        "code_interpreter", _ci_input(code),
        _ci_result(f"wrote https://{CDN}/artifacts/{SID}/output/frame_05.jpg"))
    assert head.calls == []
    assert turn_outputs.lineage() == [{
        "output": f"s3://{BUCKET}/artifacts/{SID}/output/frame_05.jpg",
        "inputs": [f"s3://{BUCKET}/projects/p1/a1/source.mp4"], "tool": "code_interpreter"}]


def test_833a079f_invented_uuid_output_written_this_turn_is_ours(monkeypatch):
    """The code tool computed its own folder (`uuid.uuid4()`), so the key first appears in the
    RESULT under another session-shaped prefix. One HEAD proves it was written this turn."""
    key = f"artifacts/{INVENTED}/output/clip.mp4"
    head = _Head({key: 1_000_030.0})
    monkeypatch.setattr(artifact_fetch, "_client", lambda: head)
    src = f"https://{CDN}/artifacts/{SID}/input/source.mp4"
    code = (f"import uuid, boto3\nkey = f'artifacts/{{uuid.uuid4()}}/output/clip.mp4'\n"
            f"urllib.request.urlretrieve('{src}', 'in.mp4')\n")
    turn_outputs.note_tool_result("code_interpreter", _ci_input(code),
                                  _ci_result(f"done: https://{CDN}/{key}"))
    assert head.calls == [key]
    assert turn_outputs.is_own_write(f"https://{CDN}/{key}")
    assert turn_outputs.lineage()[0]["inputs"] == [f"s3://{BUCKET}/artifacts/{SID}/input/source.mp4"]


def test_a_printed_listing_of_another_sessions_old_files_is_not_ours(monkeypatch):
    key = f"artifacts/{OTHER}/output/final_reel.mp4"
    head = _Head({key: 10.0})  # written long before this turn
    monkeypatch.setattr(artifact_fetch, "_client", lambda: head)
    turn_outputs.note_tool_result("code_interpreter", _ci_input("print(s3.list_objects_v2(...))"),
                                  _ci_result(f"['{key}']"))
    assert not turn_outputs.is_own_write(key)
    assert turn_outputs.lineage() == []


def test_a_literal_output_key_in_the_code_is_an_output_when_written_this_turn(monkeypatch):
    out_key = f"artifacts/{INVENTED}/output/clip.mp4"
    prior = f"artifacts/{SID}/output/earlier_clip.mp4"
    head = _Head({out_key: 1_000_010.0, prior: 500.0})
    monkeypatch.setattr(artifact_fetch, "_client", lambda: head)
    code = (f"s3.download_file('{BUCKET}', '{prior}', 'in.mp4')\n"
            f"s3.upload_file('out.mp4', '{BUCKET}', '{out_key}')\nprint('{out_key}')")
    turn_outputs.note_tool_result("code_interpreter", _ci_input(code), _ci_result(out_key))
    assert turn_outputs.lineage() == [{
        "output": f"s3://{BUCKET}/{out_key}",
        "inputs": [f"s3://{BUCKET}/{prior}"], "tool": "code_interpreter"}]


def test_an_earlier_write_this_turn_is_an_input_to_the_next_call(monkeypatch):
    head = _Head({})
    monkeypatch.setattr(artifact_fetch, "_client", lambda: head)
    crop = f"artifacts/{SID}/output/crop_1.mp4"
    turn_outputs.note_tool_result("crop_video", {"source_url": f"https://{CDN}/projects/p/a/s.mp4"},
                                  {"content": [{"text": json.dumps({"status": "cropped", "key": crop})}]})
    code = f"urlretrieve('https://{CDN}/{crop}', 'c.mp4')"
    turn_outputs.note_tool_result("code_interpreter", _ci_input(code),
                                  _ci_result(f"https://{CDN}/artifacts/{SID}/output/frame.jpg"))
    assert head.calls == []  # the crop is a known write -> input, no HEAD
    assert turn_outputs.lineage()[-1] == {
        "output": f"s3://{BUCKET}/artifacts/{SID}/output/frame.jpg",
        "inputs": [f"s3://{BUCKET}/{crop}"], "tool": "code_interpreter"}


def test_input_uploads_and_render_manifests_are_never_outputs(monkeypatch):
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({}))
    turn_outputs.note_tool_result(
        "code_interpreter", _ci_input("print('x')"),
        _ci_result(f"artifacts/{SID}/input/u.mp4 artifacts/{SID}/render/m.json"))
    assert turn_outputs.lineage() == []


def test_open_turn_forgets_the_previous_turn():
    turn_outputs.note_write(f"artifacts/{SID}/output/a.mp4", tool="artifact")
    assert turn_outputs.lineage()
    turn_outputs.open_turn()
    assert turn_outputs.lineage() == [] and not turn_outputs.own_writes()


def test_note_tool_result_never_raises():
    turn_outputs.note_tool_result("code_interpreter", object(), object())
    turn_outputs.note_tool_result("crop_video", None, {"content": [{"text": "{not json"}]})
