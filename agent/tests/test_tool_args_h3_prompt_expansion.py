"""``minimax/h3-max/*`` requires a valid ``prompt_expansion_mode`` — normalized on the way in.

Staging dcd11946: the user's fal MCP server began schema-validating submits (2026-10-07) and
rejected a missing ``prompt_expansion_mode`` as ``required``; the schema lists the values only as
examples, so node agents guessed ``"off"`` / ``"auto"`` / ``"expand"`` and 422'd at result time
(``Input should be 'disabled', 'fast', 'balanced' or 'quality'``). Plan 1 lost 7 of 8 clips.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from tool_args import normalize_minimax_h3_args as nh  # noqa: E402

H3 = "minimax/h3-max/image-to-video"


def _submit(**model_input):
    return {"endpoint_id": H3, "input": {"prompt": "p", "image_url": "https://x/a.png", **model_input}}


def test_missing_becomes_disabled_and_original_unmutated():
    src = _submit()
    out = nh(src)
    assert out["input"]["prompt_expansion_mode"] == "disabled"
    assert out["input"]["prompt"] == "p" and out["endpoint_id"] == H3
    assert "prompt_expansion_mode" not in src["input"]


@pytest.mark.parametrize("raw", ["off", "none", "false", "no", "disable", "OFF", " None ", False, None, ""])
def test_off_aliases_become_disabled(raw):
    assert nh(_submit(prompt_expansion_mode=raw))["input"]["prompt_expansion_mode"] == "disabled"


@pytest.mark.parametrize("raw", ["auto", "default", "true", "on", "expand", "Auto", True])
def test_on_aliases_become_balanced(raw):
    assert nh(_submit(prompt_expansion_mode=raw))["input"]["prompt_expansion_mode"] == "balanced"


@pytest.mark.parametrize("raw", ["disabled", "fast", "balanced", "quality"])
def test_valid_values_untouched_and_same_object(raw):
    src = _submit(prompt_expansion_mode=raw)
    assert nh(src) is src


@pytest.mark.parametrize("raw,want", [("Fast", "fast"), ("QUALITY", "quality"), (" balanced ", "balanced")])
def test_valid_values_case_insensitive(raw, want):
    assert nh(_submit(prompt_expansion_mode=raw))["input"]["prompt_expansion_mode"] == want


@pytest.mark.parametrize("key", ["endpoint_id", "model_id", "application"])
def test_every_endpoint_key_and_text_to_video(key):
    src = {key: "minimax/h3-max/text-to-video", "input": {"prompt": "p"}}
    assert nh(src)["input"]["prompt_expansion_mode"] == "disabled"


def test_wrong_fal_ai_prefix_is_still_recognised():
    src = {"endpoint_id": "fal-ai/minimax/h3-max/image-to-video", "input": {"prompt": "p"}}
    assert nh(src)["input"]["prompt_expansion_mode"] == "disabled"


@pytest.mark.parametrize("endpoint", [
    "fal-ai/kling-video/v3/pro/image-to-video",
    "fal-ai/minimax/hailuo-02/standard/image-to-video",
    "minimax/h3/image-to-video",       # the distinct base family
    "minimax/h3-max",                  # bare app (a read), not a model path
])
def test_other_endpoints_untouched(endpoint):
    src = {"endpoint_id": endpoint, "input": {"prompt": "p", "prompt_expansion_mode": "off"}}
    assert nh(src) is src


def test_reads_without_an_input_dict_are_untouched():
    for src in ({"endpoint_id": H3, "request_id": "r1"}, {"endpoint_id": H3}, {"input": {"prompt": "p"}}):
        assert nh(src) is src


def test_top_level_field_is_normalized_but_never_added():
    src = {"endpoint_id": H3, "prompt": "p", "prompt_expansion_mode": "auto"}
    out = nh(src)
    assert out == {"endpoint_id": H3, "prompt": "p", "prompt_expansion_mode": "balanced"}
    assert src["prompt_expansion_mode"] == "auto"
    # Present at the top level: nothing is added under `input` beside it.
    both = {"endpoint_id": H3, "prompt_expansion_mode": "fast", "input": {"prompt": "p"}}
    assert nh(both) is both


def test_unrecognised_value_passes_through_for_fal_to_report():
    src = _submit(prompt_expansion_mode="turbo")
    assert nh(src) is src


def test_non_dict_passthrough():
    assert nh("x") == "x" and nh(None) is None


# ---- the real chokepoints ----------------------------------------------------------------------

def test_normalized_input_reaches_mcp_agent_tool_stream(monkeypatch):
    """Modelled on test_plan_node_endpoint_guard._drive: inside a plan node planned for h3-max."""
    import test_plan_node_endpoint_guard as guard

    raw = {"endpoint_id": "fal-ai/minimax/h3-max/image-to-video",
           "input": {"prompt": "p", "duration": "6s", "prompt_expansion_mode": "off"}}
    results, sent = guard._drive(guard._tool("usr-1___submit_job"), raw, planned=[H3],
                                 monkeypatch=monkeypatch)
    assert results[0]["status"] == "success"
    assert sent == [{"endpoint_id": H3,
                     "input": {"prompt": "p", "duration": "6", "prompt_expansion_mode": "disabled"}}]
    assert raw["input"]["prompt_expansion_mode"] == "off"  # caller's dict untouched


def test_call_tool_sync_path_also_normalizes():
    import contextlib

    import mcp_manager as mm

    sent = {}

    class _Client:
        def call_tool_sync(self, tool_use_id, name, arguments):
            sent["arguments"] = arguments
            return {"ok": True}

    gw = mm.GatewayConnection.__new__(mm.GatewayConnection)
    gw.name = "g"
    gw._client = _Client()
    gw.call_lease = lambda c: contextlib.nullcontext()
    gw.call_tool_sync("submit_job", {"endpoint_id": H3, "input": {"prompt": "p"}})
    assert sent["arguments"]["input"]["prompt_expansion_mode"] == "disabled"
