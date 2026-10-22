"""Tool results are capped so an oversized one cannot lose a completed turn.

AgentCore Memory rejects an oversized CreateEvent with a 413, and that failure
arrives AFTER the work is done. A turn that had already generated the video,
muxed it with the soundtrack and uploaded the artifact died with "Failed to create
message: An error occurred (413) when calling the CreateEvent operation" -- the
answer existed and was thrown away. The usual cause is a tool returning inline
base64 media.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks.provider import _MAX_TOOL_RESULT_CHARS, _cap_tool_result_size  # noqa: E402


class _Event:
    def __init__(self, result):
        self.result = result


def test_oversized_text_block_is_truncated():
    ev = _Event({"content": [{"text": "x" * (_MAX_TOOL_RESULT_CHARS + 1000)}]})
    _cap_tool_result_size(ev)
    text = ev.result["content"][0]["text"]
    assert len(text) < _MAX_TOOL_RESULT_CHARS + 1000
    assert "truncated" in text
    # The head is preserved so the model still sees the useful part.
    assert text.startswith("x" * 100)


def test_results_under_the_cap_are_byte_identical():
    original = {"content": [{"text": "a small result"}]}
    ev = _Event({"content": [{"text": "a small result"}]})
    _cap_tool_result_size(ev)
    assert ev.result == original


def test_only_oversized_blocks_are_touched():
    ev = _Event({"content": [
        {"text": "y" * (_MAX_TOOL_RESULT_CHARS + 5)},
        {"text": "keep me"},
    ]})
    _cap_tool_result_size(ev)
    assert "truncated" in ev.result["content"][0]["text"]
    assert ev.result["content"][1]["text"] == "keep me"


def test_non_text_blocks_are_left_alone():
    ev = _Event({"content": [{"json": {"url": "https://cdn/final.mp4"}}]})
    _cap_tool_result_size(ev)
    assert ev.result["content"][0] == {"json": {"url": "https://cdn/final.mp4"}}


def test_malformed_results_never_raise():
    # A failure here must not break tool execution, which is the point of the call.
    for bad in (None, {}, {"content": None}, {"content": ["not a dict"]}, "string"):
        _cap_tool_result_size(_Event(bad))


def test_missing_result_attribute_never_raises():
    class _Bare:
        pass

    _cap_tool_result_size(_Bare())
