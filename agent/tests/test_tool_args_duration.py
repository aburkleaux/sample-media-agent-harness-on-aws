"""fal ``duration`` normalization — on the path Strands actually invokes.

Staging 9fb4e17b: gen_shot2/gen_shot8 sent ``"duration": "7s"`` to ``minimax/h3-max`` and failed
at result time with 422 ``body.duration: unable to parse string as an integer``; clips sent
``"7"`` succeeded. An earlier fix coerced numeric strings to int inside
``GatewayConnection.call_tool_sync`` — a path the LLM's tool calls never take (Strands calls
``MCPAgentTool.stream`` directly), and int would have broken Kling's string enum anyway.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from tool_args import normalize_duration as nd  # noqa: E402


@pytest.mark.parametrize("raw,want", [
    ("7s", "7"), ("7 s", "7"), ("7sec", "7"), ("7 secs", "7"), ("7 seconds", "7"),
    ("7.0", "7"), ("7.0s", "7"), (" 10 ", "10"), ("7", "7"),
])
def test_whole_second_strings_become_bare_digit_strings(raw, want):
    assert nd({"duration": raw}) == {"duration": want}


@pytest.mark.parametrize("raw", ["auto", "7.5s", "7m", "seven", "", "-5"])
def test_other_strings_untouched(raw):
    assert nd({"duration": raw}) == {"duration": raw}


def test_numbers_untouched_and_never_int_coerced():
    assert nd({"duration": 7}) == {"duration": 7}
    assert nd({"duration": 7.5}) == {"duration": 7.5}
    # Kling's duration is a STRING enum: a digit string must stay a string.
    assert nd({"duration": "5"}) == {"duration": "5"} and isinstance(nd({"duration": "5"})["duration"], str)


def test_nested_fal_submit_input_is_normalized_and_original_unmutated():
    src = {"endpoint_id": "minimax/h3-max", "input": {"prompt": "oil painting", "duration": "7s"}}
    out = nd(src)
    assert out == {"endpoint_id": "minimax/h3-max", "input": {"prompt": "oil painting", "duration": "7"}}
    assert src["input"]["duration"] == "7s"


def test_non_dict_passthrough():
    assert nd("x") == "x" and nd(None) is None


# ---- the real chokepoint ---------------------------------------------------------------------

def test_strands_stream_path_receives_normalized_input(monkeypatch):
    from jobs import strands_tool_wrap as stw

    captured = {}

    async def _fake_stream(self, tool_use, invocation_state, **kwargs):
        captured["tool_use"] = tool_use
        yield {"done": True}

    monkeypatch.setattr(stw.MCPAgentTool, "stream", _fake_stream)

    class _Tool(stw._ResilientMCPMixin):
        mcp_client = object()

    tool_use = {"toolUseId": "t1", "name": "usr-x___submit_job",
                "input": {"endpoint_id": "minimax/h3-max", "input": {"prompt": "p", "duration": "7s"}}}

    async def _run():
        return [ev async for ev in _Tool()._base_stream_once(tool_use, {}, {})]

    events = asyncio.run(_run())
    assert events == [{"done": True}]
    assert captured["tool_use"]["input"]["input"]["duration"] == "7"
    assert tool_use["input"]["input"]["duration"] == "7s"  # caller's dict untouched


def test_call_tool_sync_path_also_normalizes():
    import mcp_manager as mm

    sent = {}

    class _Client:
        def call_tool_sync(self, tool_use_id, name, arguments):
            sent["arguments"] = arguments
            return {"ok": True}

    gw = mm.GatewayConnection.__new__(mm.GatewayConnection)
    gw.name = "g"
    gw._client = _Client()
    gw.call_lease = lambda c: __import__("contextlib").nullcontext()
    gw.call_tool_sync("submit_job", {"input": {"duration": "6s"}})
    assert sent["arguments"] == {"input": {"duration": "6"}}
