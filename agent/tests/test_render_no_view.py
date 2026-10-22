"""'No view' is a terminal render outcome, and error records never become tabs (staging session a2d759c2).

A data-only result (conversation_analysis.json) was declined by the deterministic renderer ("JSON is data,
not a view"), but the decline looked like a failure, so it escalated to the LLM agent; that built a timeline
from JSON, failed validation, and its VALIDATION_FAILED error manifest -- carrying the whole prompt with a
presigned URL as `sourceUrl` -- was listed as a tab.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render_agent as ra  # noqa: E402
import render.artifacts as R  # noqa: E402
from orchestration.progress_channel import ProgressChannel, set_active_channel  # noqa: E402

PROMPT = ("Render these artifacts as one unified timeline. These are the CANDIDATE OUTPUT URLS:\n"
          "https://m2c-proxy.s3.amazonaws.com/micro-workflow/conversation-analysis/7e92/conversation_analysis.json"
          "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Security-Token=IQoJb3JpZ2lu&X-Amz-Signature=abc")


# ---------------------------------------------------------------------------------------- error records
def test_error_manifest_source_url_is_a_clean_url_never_the_prompt():
    m = ra._build_error_manifest("VALIDATION_FAILED", "duration must be >= 0.001", source_url=PROMPT)
    assert m["sourceUrl"] == ("https://m2c-proxy.s3.amazonaws.com/micro-workflow/conversation-analysis/7e92/"
                              "conversation_analysis.json")
    assert "X-Amz" not in json.dumps(m) and "CANDIDATE" not in json.dumps(m)
    assert "sourceUrl" not in ra._build_error_manifest("NO_URL", "x", source_url="no url in here")


def test_error_only_vs_a_view_that_carries_errors():
    a2d = {"id": "6921", "duration": 0.001, "tracks": [], "clips": [], "createdAt": "t",
           "errors": [{"code": "VALIDATION_FAILED", "message": "m"}], "sourceUrl": "https://x/y.json"}
    assert ra.is_error_only_manifest(a2d)
    assert not ra.is_error_only_manifest({**a2d, "template": "nle-timeline", "tracks": [{"src": "v.mp4"}]})
    assert not ra.is_error_only_manifest({"template": "nle-timeline", "tracks": [{"src": "v.mp4"}]})


class _S3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)

    def head_object(self, **kw):
        raise RuntimeError("absent")

    def get_object(self, **kw):
        raise RuntimeError("absent")


def test_error_only_manifest_is_written_under_errors_and_its_ref_is_flagged(monkeypatch):
    s3 = _S3()
    monkeypatch.setattr(ra, "_s3", s3)
    monkeypatch.setattr(ra, "S3_BUCKET", "bkt")
    m = ra._build_error_manifest("VALIDATION_FAILED", "bad", source_url=PROMPT)
    key = ra._write_manifest_to_s3(json.dumps(m), "sess", m["id"])
    assert key == f"artifacts/sess/render/_errors/{m['id']}.json" and s3.puts[0]["Key"] == key
    # A plain output_ref for it (the old agentic VALIDATION_FAILED shape) is flagged at the choke point.
    ev = ra._normalize_output_ref(json.dumps({"type": "output_ref", "s3_key": key, "manifest_id": m["id"]}))
    obj = json.loads(ev)
    assert obj["error"] is True and obj["error_code"] == "RENDER_ERROR"
    # A real render is untouched.
    good = json.dumps({"type": "output_ref", "s3_key": "artifacts/sess/render/m.json"})
    assert ra._normalize_output_ref(good) == good
    view = {"template": "nle-timeline", "tracks": [{"src": "v.mp4"}], "errors": [{"code": "X"}]}
    assert ra._write_manifest_to_s3(json.dumps(view), "sess", "v1") == "artifacts/sess/render/v1.json"


# ------------------------------------------------------------------------------------------- no_view
def _collect(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


def test_a_deliberate_decline_never_escalates_to_the_llm(monkeypatch):
    async def det(payload, context):
        yield json.dumps({"type": "step", "status": "done", "label": "JSON is data, not a view"})
        yield ra._no_view("data_only", "JSON-only input projected no view")

    called = []

    async def agentic(payload, context):
        called.append(True)
        yield json.dumps({"type": "output_ref", "s3_key": "artifacts/s/render/x.json"})

    monkeypatch.setattr(ra, "_render_deterministic", det)
    monkeypatch.setattr(ra, "_render_agentic", agentic)
    evs = [json.loads(e) for e in _collect(ra.invoke({"prompt": "render https://x/a.json"}, None))]
    assert not called, "no_view is terminal"
    assert any(e.get("type") == "no_view" and e["reason"] == "data_only" for e in evs)
    assert not any("Escalating" in str(e.get("label", "")) for e in evs)


def test_a_real_failure_still_escalates(monkeypatch):
    async def det(payload, context):
        yield json.dumps({"type": "output_ref", "s3_key": "artifacts/s/render/_errors/e.json"})  # error record

    called = []

    async def agentic(payload, context):
        called.append(True)
        yield json.dumps({"type": "output_ref", "s3_key": "artifacts/s/render/ok.json"})

    monkeypatch.setattr(ra, "_render_deterministic", det)
    monkeypatch.setattr(ra, "_render_agentic", agentic)
    evs = [json.loads(e) for e in _collect(ra.invoke({"prompt": "render https://x/a.mp4"}, None))]
    assert called
    assert evs[0]["error"] is True  # the deterministic error record reached the caller flagged


def test_json_only_decline_site_emits_no_view():
    src = open(ra.__file__).read()
    i = src.index("JSON is data, not a view — nothing to display")
    assert "_no_view(\"data_only\"" in src[i:i + 300]


# ---------------------------------------------------------------------------------------- dispatch_render
class _Resp:
    def __init__(self, events):
        self._lines = [f"data: {json.dumps(json.dumps(e))}" for e in events]

    def iter_lines(self):
        return iter(self._lines)


def test_dispatch_render_treats_no_view_as_a_clean_terminal_no_op(monkeypatch):
    monkeypatch.setattr(R, "_render_agent_arn", lambda: "arn:aws:bedrock-agentcore:us-west-2:1:runtime/render")
    monkeypatch.setattr(R, "_get_client", lambda: object())
    import jobs.context as jc
    monkeypatch.setattr(jc, "invoke_agent_runtime_traced",
                        lambda client, **kw: {"response": _Resp([{"type": "step", "label": "x"},
                                                                 {"type": "no_view", "reason": "data_only"}])})
    ch = ProgressChannel()
    set_active_channel(ch)
    urls = ["https://m2c/x/conversation_analysis_nv_test.json"]
    try:
        out = R.dispatch_render(urls, render_request_id="RIDNV")
        assert out is None
        evs = ch.drain()
        term = [e for e in evs if e.extra.get("render_request_id") == "RIDNV" and e.extra.get("render_failed")]
        assert len(term) == 1, "one terminal signal so the UI drops the placeholder"
        assert term[0].status == "done" and term[0].extra.get("render_no_view") is True
        assert not any("no viewable output" in (e.label or "") for e in evs), "not reported as a failure"
    finally:
        set_active_channel(None)
        R._rendered_url_sets.discard(frozenset(urls))
