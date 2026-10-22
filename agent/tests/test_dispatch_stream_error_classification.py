"""Structured runtime errors must survive the SSE stream.

``task_agent`` yields ``{"error": ...}`` dicts with NO ``type`` field
(``tool_not_found``, and ``agent_config_unresolved`` from Task 9). These
matched no branch in ``_consume_stream`` and were dropped as "unknown
structured event", so the orchestrator reported "Sub-agent returned no output"
instead of the reason.

Ordering is load-bearing: ``output_ref`` events legitimately carry an ``error``
field, so known ``type`` values must be matched BEFORE the error-key fallback.

That ordering held only when a progress channel was active. The known-type
branches used to be conjoined with the channel check
(``if ev_type == "step" and channel is not None:``), so with no channel a
``step`` or ``output_ref`` fell straight through to the fallback and its
optional ``error`` field became a stream-level failure. Every ``error``-field
case here is therefore run BOTH with and without a channel, and with a TRUTHY
error value — the original guard used ``{"error": None}``, which is falsy and
would have passed even with the branch order fully reversed.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestration import dispatch_tool  # noqa: E402
from orchestration.progress_channel import ProgressChannel, set_active_channel  # noqa: E402


def _sse(*payloads):
    return [f"data: {json.dumps(p)}" for p in payloads]


@pytest.fixture(params=["no-channel", "with-channel"])
def channel(request):
    """Run a test under both progress-channel states.

    ``_active_channel`` is a module global, so it is restored on teardown to
    keep tests order-independent.
    """
    active = ProgressChannel() if request.param == "with-channel" else None
    set_active_channel(active)
    try:
        yield active
    finally:
        set_active_channel(None)


def test_typeless_error_dict_is_reported_as_an_error():
    text, had_error, _ = dispatch_tool._consume_stream(
        _sse({"error": "tool_not_found", "message": "no such tool"})
    )
    assert had_error is True
    assert "tool_not_found" in text or "no such tool" in text


def test_agent_config_unresolved_is_reported():
    text, had_error, _ = dispatch_tool._consume_stream(
        _sse({
            "error": "agent_config_unresolved",
            "message": "Custom Agent ag-1 (version 3) could not be resolved",
            "agent_id": "ag-1",
            "version": 3,
        })
    )
    assert had_error is True
    assert "agent_config_unresolved" in text or "could not be resolved" in text


def test_output_ref_carrying_an_error_field_is_not_an_error_event(channel):
    """A TRUTHY per-artifact error on an output_ref is not a stream failure."""
    _text, had_error, _ = dispatch_tool._consume_stream(
        _sse({
            "type": "output_ref",
            "s3_key": "artifacts/x.json",
            "error": "render failed: codec unsupported",
        })
    )
    assert had_error is False


def test_step_carrying_an_error_field_is_not_an_error_event(channel):
    """Same for a ``step`` — a recovered tool failure is progress, not a fault."""
    _text, had_error, _ = dispatch_tool._consume_stream(
        _sse({
            "type": "step",
            "label": "probe media",
            "status": "running",
            "error": "transient",
        })
    )
    assert had_error is False


def test_render_manifest_output_ref_with_an_error_is_still_not_a_failure(channel):
    """The render-manifest sub-branch is the one that reads ``error`` itself."""
    _text, had_error, _ = dispatch_tool._consume_stream(
        _sse({
            "type": "output_ref",
            "s3_key": "artifacts/job-1/render/manifest.json",
            "render_request_id": "rid-1",
            "error": "one clip failed to encode",
        })
    )
    assert had_error is False


def test_typeless_error_is_still_reported_with_no_channel(channel):
    """The fallback must keep working under both channel states.

    Guards against "fixing" the ordering by narrowing the fallback so far that
    genuine type-less errors stop being reported.
    """
    text, had_error, _ = dispatch_tool._consume_stream(
        _sse({"error": "tool_not_found", "message": "no such tool"})
    )
    assert had_error is True
    assert "tool_not_found" in text or "no such tool" in text


def test_explicit_error_type_still_works(channel):
    _text, had_error, _ = dispatch_tool._consume_stream(
        _sse({"type": "error", "error": "boom"})
    )
    assert had_error is True


def test_plain_text_still_accumulates():
    text, had_error, _ = dispatch_tool._consume_stream(["data: hello"])
    assert had_error is False
    assert "hello" in text


# ------------------------------------------------------------------ a render that ends on its manifest

_GALLERY_REF = {"type": "output_ref", "s3_key": "artifacts/sess-1/render/gal-1.json",
                "manifest_id": "gal-1", "template": "clip-gallery"}


def _dispatch_render(monkeypatch, frames):
    """Run dispatch_tool(target='render') over a canned SSE stream; no AWS."""
    monkeypatch.setattr(dispatch_tool, "RENDER_AGENT_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/r")
    monkeypatch.setattr(dispatch_tool, "_forward_request_context", lambda payload: None)

    def _stream(arn, payload, on_frame=None, rendered=None):
        return dispatch_tool._consume_stream(_sse(*frames), rendered=rendered)

    monkeypatch.setattr(dispatch_tool, "_invoke_runtime_stream", _stream)
    set_active_channel(ProgressChannel())
    try:
        return dispatch_tool.dispatch_tool({"toolUseId": "t1", "input": {"prompt": "render both videos",
                                                                          "target": "render"}})
    finally:
        set_active_channel(None)


def test_a_render_that_ends_on_its_output_ref_is_a_success(monkeypatch):
    """Session acb56629: the manifest was written and the tab opened, but the stream carried no text and
    no summary, so dispatch reported "Sub-agent returned no output" and the model re-dispatched."""
    out = _dispatch_render(monkeypatch, [_GALLERY_REF])
    assert out["status"] == "success"
    assert out["content"][0]["text"] == "Rendered: clip-gallery — the viewer tab is open. Do not re-dispatch."


def test_a_render_step_names_the_view_in_the_success(monkeypatch):
    step = {"type": "step", "status": "done", "label": "Rendered", "render_manifest_key": "artifacts/s/render/m.json",
            "render_request_id": "rid-1", "render_label": "Two videos"}
    out = _dispatch_render(monkeypatch, [step])
    assert out["status"] == "success"
    assert out["content"][0]["text"].startswith("Rendered: Two videos — the viewer tab is open.")


def test_no_output_and_no_render_is_still_an_error(monkeypatch):
    out = _dispatch_render(monkeypatch, [{"type": "step", "status": "running", "label": "thinking"}])
    assert out["status"] == "error" and out["content"][0]["text"] == "Sub-agent returned no output"
    failed = {**_GALLERY_REF, "error": True, "error_code": "NO_URL"}
    assert _dispatch_render(monkeypatch, [failed])["status"] == "error"


def test_a_forwarded_render_output_ref_records_the_render():
    set_active_channel(ProgressChannel())
    try:
        rendered = {}
        text, had_error, done = dispatch_tool._consume_stream(_sse(_GALLERY_REF), rendered=rendered)
    finally:
        set_active_channel(None)
    assert (text, had_error, done) == ("", False, False)
    assert rendered == {"forwarded": True, "template": "clip-gallery"}


def test_a_failed_render_output_ref_records_nothing():
    set_active_channel(ProgressChannel())
    try:
        rendered = {}
        dispatch_tool._consume_stream(_sse({**_GALLERY_REF, "error": True, "error_code": "NO_URL"}),
                                      rendered=rendered)
    finally:
        set_active_channel(None)
    assert rendered == {}
