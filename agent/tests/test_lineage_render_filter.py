"""Cross-session render filter vs this turn's own writes (asset-lineage R3.4, R7.4).

Staging session 833a079f: the main agent's code_interpreter wrote the user's clip to
``artifacts/e34ebb5c-.../output/`` (an invented uuid -- main.py never told the model its session
id). The auto-render filter reads the session from the PATH, so it dropped the clip from the
render AND from ``source_urls``: no clip tab, and later analyses had no source video to fold onto.

Now: a key this runtime wrote this turn is ours wherever it sits; source media is never
session-filtered; a genuinely foreign URL (another session's prior work merely QUOTED) is still
dropped exactly as before.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks import actions  # noqa: E402
from hooks.engine import HookEngine  # noqa: E402
from hooks.defaults import default_hook_specs  # noqa: E402
from hooks.provider import HarnessHookProvider, reset_dispatched_artifacts  # noqa: E402
from media import artifact_fetch, turn_outputs  # noqa: E402

BUCKET = "skills-bucket"
CDN = "d123.cloudfront.net"
SID = "833a079f-1111-4222-8333-444455556666"
INVENTED = "e34ebb5c-aaaa-4bbb-8ccc-ddddeeeeffff"
PRIOR = "e27e5945-ba47-45d7-bb0b-3f33feb2be03"
CLIP_KEY = f"artifacts/{INVENTED}/output/clip_00_12.mp4"
CLIP = f"https://{CDN}/{CLIP_KEY}"


class _Head:
    def __init__(self, modified: dict):
        self.modified = modified

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.modified:
            raise RuntimeError("404")
        return {"LastModified": datetime.fromtimestamp(self.modified[Key], tz=timezone.utc)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import time
    monkeypatch.setenv("SKILLS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("ARTIFACTS_DOMAIN", CDN)
    monkeypatch.delenv("ARTIFACTS_PREFIX", raising=False)
    import jobs.context as jc
    tok = jc._current_session_id.set(SID)
    reset_dispatched_artifacts()
    # The clip was written just now; nothing else exists.
    monkeypatch.setattr(artifact_fetch, "_client", lambda: _Head({CLIP_KEY: time.time()}))
    # No external mirroring, no render dispatch in a unit test.
    monkeypatch.setattr(actions, "_mirror_external_media", lambda urls: urls)
    monkeypatch.setattr(actions, "_emit_composing_marker", lambda rid, label: None)
    yield
    jc._current_session_id.reset(tok)
    actions._render_batch.clear()
    turn_outputs.open_turn()
    reset_dispatched_artifacts()


def _ci_event(code: str, stdout: str):
    return types.SimpleNamespace(
        tool_use={"name": "code_interpreter", "toolUseId": "tu-ci",
                  "input": {"code_interpreter_input": {"action": {
                      "type": "executeCode", "language": "python", "code": code}}}},
        result={"toolUseId": "tu-ci", "status": "success", "content": [{"text": stdout}]},
    )


def _buffered_urls() -> list:
    return [u for slot in actions._render_batch.values() for u in slot["urls"]]


def test_833a079f_invented_uuid_clip_written_this_turn_auto_renders():
    """Regression: the code tool's clip under an invented-uuid prefix opens its tab."""
    provider = HarnessHookProvider(HookEngine(default_hook_specs()))
    provider._on_session_start(types.SimpleNamespace())
    code = ("import uuid, boto3\nkey = f'artifacts/{uuid.uuid4()}/output/clip_00_12.mp4'\n"
            f"urllib.request.urlretrieve('https://{CDN}/artifacts/{SID}/input/source.mp4', 'in.mp4')\n")
    provider._on_after_tool(_ci_event(code, f"Saved the clip: {CLIP}"))
    assert CLIP in _buffered_urls(), "the user's own clip was dropped as 'another session's'"
    # ...and it is recorded as lineage from the source it was cut from.
    assert turn_outputs.lineage() == [{
        "output": f"s3://{BUCKET}/{CLIP_KEY}",
        "inputs": [f"s3://{BUCKET}/artifacts/{SID}/input/source.mp4"],
        "tool": "code_interpreter"}]


def test_a_foreign_session_url_this_turn_did_not_write_is_still_dropped():
    foreign = f"https://{CDN}/artifacts/{PRIOR}/output/final_reel_crossfade.mp4"
    assert actions._drop_foreign_session_urls([foreign]) == []


def test_an_own_write_under_another_prefix_passes_the_deliverable_filter():
    turn_outputs.note_write(CLIP_KEY, tool="code_interpreter")
    assert actions._drop_foreign_session_urls([CLIP]) == [CLIP]


def test_833a079f_the_clip_is_used_as_source_for_a_later_analysis(monkeypatch):
    """R7.4: an analysis whose SOURCE is the invented-uuid clip keeps it as its canvas, even in a
    LATER turn (the ledger has been reset) -- source media is never session-filtered."""
    turn_outputs.open_turn()  # a later turn: the clip is no longer a write of THIS turn
    actions.open_render_batch()
    shots = "https://m2c-proxy.s3.amazonaws.com/micro-workflow/shot/abc/shots.json?X-Amz-Signature=s"
    actions.render_action(
        {"urls": "result.urls", "source_urls": "result.source_urls"},
        {"tool": {"name": "shot-segmentation___get_job_status"},
         "result": {"urls": [shots], "source_urls": [CLIP]}})
    slots = list(actions._render_batch.values())
    assert slots and slots[0]["source_urls"] == [CLIP]


def test_plan_groups_keep_their_source_urls_and_filter_only_deliverables(monkeypatch):
    actions.open_render_batch()
    foreign = f"https://{CDN}/artifacts/{PRIOR}/output/old.mp4"
    diar = f"https://{CDN}/artifacts/{SID}/output/plan/n2/final_transcript.json"
    actions.render_action(
        {"urls": "result.urls", "source_urls": "result.source_urls"},
        {"tool": {"name": "get_plan_results"},
         "result": {"urls": [diar, foreign], "render_groups": [
             {"node_id": "n2", "tool": "speech-diarization", "label": "Speakers",
              "urls": [diar, foreign], "source_urls": [CLIP]}]}})
    slots = list(actions._render_batch.values())
    assert [s["urls"] for s in slots] == [[diar]]
    assert slots[0]["source_urls"] == [CLIP]


def test_ledger_is_filled_before_the_render_rule_fires(monkeypatch):
    """Ordering: the render rule for a result must see the writes of that very result."""
    seen = {}
    real = turn_outputs.is_own_write

    def spy(url):
        seen.setdefault("own", real(url))
        return seen["own"]

    monkeypatch.setattr(turn_outputs, "is_own_write", spy)
    provider = HarnessHookProvider(HookEngine(default_hook_specs()))
    provider._on_session_start(types.SimpleNamespace())
    provider._on_after_tool(_ci_event("key = f'artifacts/{uuid.uuid4()}/output/x.mp4'",
                                      json.dumps({"url": CLIP})))
    assert seen.get("own") is True
