"""Graceful recovery from a dead MCP client session.

Session d9844770: a fal edit died with ``MCPClientInitializationError: the client session is not
running`` — the transport had idle-closed, so the request never reached fal (no request_id,
nothing billed). These tests pin the two halves of the fix:

1. ``_is_session_down_error`` classifies ONLY pre-request session/transport failures as
   recoverable — a real tool/model error or 4xx/5xx (which reached the upstream and may carry a
   request_id) is NOT matched, so we never double-submit.
2. The resilient wrapper reconnects + retries EXACTLY once on a session-down error, rebinds the
   fresh client, and yields the retried result; on any other error it propagates without
   reconnecting.
"""
from __future__ import annotations

import asyncio

import pytest

import jobs.strands_tool_wrap as stw
import types
from media import persist_media as _pm
from strands.types._events import ToolResultEvent


class MCPClientInitializationError(Exception):
    """Stand-in matched by class name (mirrors Strands' real exception)."""


def _drain(agen):
    async def _c():
        out = []
        async for x in agen:
            out.append(x)
        return out
    return asyncio.run(_c())


def _make_tool(reconnect, behaviors, monkeypatch):
    """A minimal object carrying the resilient mixin, with MCPAgentTool.stream faked to a
    scripted sequence of behaviors (an Exception raises; a list yields its items)."""
    calls = {"n": 0}

    class FakeBase:
        async def stream(self, tool_use, invocation_state, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            beh = behaviors[i]
            if isinstance(beh, Exception):
                raise beh
            for ev in beh:
                yield ev

    monkeypatch.setattr(stw, "MCPAgentTool", FakeBase)

    class Tool(stw._ResilientMCPMixin):
        pass

    t = Tool()
    t._reconnect = reconnect
    t.mcp_client = "old-client"
    t.tool_name = "fal___run_model"
    return t, calls


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------

def test_is_session_down_error_matches_only_pre_request_failures():
    # Recoverable — the request never left the client.
    assert stw._is_session_down_error(MCPClientInitializationError("boom"))
    assert stw._is_session_down_error(RuntimeError("the client session is not running"))
    assert stw._is_session_down_error(
        RuntimeError("Ensure the agent is used within the MCP client context manager")
    )
    # NOT recoverable — reached the upstream / ambiguous; retrying could double-submit or loop.
    assert not stw._is_session_down_error(RuntimeError("fal model error: content policy violation"))
    assert not stw._is_session_down_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not stw._is_session_down_error(RuntimeError("HTTP 500 Internal Server Error"))
    assert not stw._is_session_down_error(ValueError("missing required field endpoint_id"))
    # A read timeout is deliberately NOT auto-retried (a submit may have reached fal's queue).
    assert not stw._is_session_down_error(RuntimeError("Read timeout on endpoint URL: None"))


# ---------------------------------------------------------------------------
# reconnect + retry-once
# ---------------------------------------------------------------------------

def test_reconnects_and_retries_once_on_session_down(monkeypatch):
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    behaviors = [MCPClientInitializationError("the client session is not running"), ["OK_EVENT"]]
    t, calls = _make_tool(reconnect, behaviors, monkeypatch)

    out = _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))

    assert out == ["OK_EVENT"]          # the retried call's result reached the agent
    assert rc["n"] == 1                 # reconnected exactly once
    assert t.mcp_client == "fresh-client"  # rebound to the fresh session
    assert calls["n"] == 2              # base stream invoked twice (fail, then retry)


def test_does_not_retry_a_real_tool_error(monkeypatch):
    # A model/tool error DID reach fal — reconnecting + retrying could double-bill, so it must
    # propagate untouched and NOT reconnect.
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    behaviors = [RuntimeError("fal model error: content policy violation")]
    t, calls = _make_tool(reconnect, behaviors, monkeypatch)

    with pytest.raises(RuntimeError, match="content policy"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))

    assert rc["n"] == 0     # never reconnected
    assert calls["n"] == 1  # never re-issued


def test_no_reconnect_on_first_try_success(monkeypatch):
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    t, calls = _make_tool(reconnect, [["A", "B"]], monkeypatch)
    out = _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))

    assert out == ["A", "B"]
    assert rc["n"] == 0
    assert calls["n"] == 1


def test_session_down_without_reconnect_propagates(monkeypatch):
    # No reconnect callable available -> surface the original error rather than loop.
    behaviors = [MCPClientInitializationError("the client session is not running")]
    t, calls = _make_tool(None, behaviors, monkeypatch)

    with pytest.raises(MCPClientInitializationError):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert calls["n"] == 1


def test_only_retries_once_then_gives_up(monkeypatch):
    # Two consecutive session-down failures: reconnect once, retry, and if the retry ALSO fails
    # the error surfaces (no infinite loop).
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    behaviors = [
        MCPClientInitializationError("the client session is not running"),
        MCPClientInitializationError("the client session is not running"),
    ]
    t, calls = _make_tool(reconnect, behaviors, monkeypatch)

    with pytest.raises(MCPClientInitializationError):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert rc["n"] == 1     # reconnected exactly once
    assert calls["n"] == 2  # original + one retry, then gave up


# ---------------------------------------------------------------------------
# Fix 3: self-heal on an auth/scope rejection a fresh session can clear.
# Session 6c69afb4 / 252dee7c: a warm fal session degraded to `insufficient_scope`
# and never reconnected, so every call failed for ~45 min until the microVM recycled.
# A rejected auth call executed nothing upstream, so reconnect+retry-once is idempotent.
# ---------------------------------------------------------------------------

def test_is_auth_refresh_error_matches_scope_and_401_403():
    assert stw._is_auth_refresh_error(
        RuntimeError("insufficient_scope - The request requires higher privileges"))
    assert stw._is_auth_refresh_error(RuntimeError("MCPError: -32002 insufficient_scope"))
    assert stw._is_auth_refresh_error(RuntimeError("HTTP 401 Unauthorized"))
    assert stw._is_auth_refresh_error(RuntimeError("403 Forbidden"))
    # NOT an auth-refresh condition — a real result / generic status must propagate.
    assert not stw._is_auth_refresh_error(RuntimeError("Result fetch failed (404)"))
    assert not stw._is_auth_refresh_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not stw._is_auth_refresh_error(RuntimeError("HTTP 500 Internal Server Error"))
    assert not stw._is_auth_refresh_error(ValueError("missing required field endpoint_id"))
    assert not stw._is_auth_refresh_error(RuntimeError("content policy violation"))


def test_reconnects_and_retries_once_on_insufficient_scope(monkeypatch):
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    behaviors = [
        RuntimeError("insufficient_scope - The request requires higher privileges than "
                     "provided by the access token."),
        ["OK_EVENT"],
    ]
    t, calls = _make_tool(reconnect, behaviors, monkeypatch)
    out = _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert out == ["OK_EVENT"]              # the fresh-auth retry reached the agent
    assert rc["n"] == 1                     # reconnected once (re-auth)
    assert t.mcp_client == "fresh-client"
    assert calls["n"] == 2


def test_insufficient_scope_retry_is_bounded(monkeypatch):
    # A persistent (non-transient) scope failure: reconnect once, retry, then surface — no loop.
    rc = {"n": 0}

    def reconnect():
        rc["n"] += 1
        return "fresh-client"

    err = RuntimeError("insufficient_scope - higher privileges required")
    t, calls = _make_tool(reconnect, [err, err], monkeypatch)
    with pytest.raises(RuntimeError, match="insufficient_scope"):
        _drain(t._resilient_base_stream({"toolUseId": "u"}, {}, {}))
    assert rc["n"] == 1     # reconnected exactly once
    assert calls["n"] == 2  # original + one retry, then gave up


# ---------------------------------------------------------------------------
# wrap_resilient passthrough safety
# ---------------------------------------------------------------------------

def test_wrap_resilient_skips_non_mcp_and_already_wrapped():
    class NotMcp:
        pass

    class AlreadyMixin(stw._ResilientMCPMixin):
        pass

    n = NotMcp()
    a = AlreadyMixin()
    out = stw.wrap_resilient([n, a], reconnect=lambda: None)
    assert out[0] is n      # non-MCP tool untouched
    assert out[1] is a      # already-resilient tool untouched (idempotent)


# ---------------------------------------------------------------------------
# results source-mirror (fal output durability): _mirror_result_media
# ---------------------------------------------------------------------------
#
# The results-role wrap copies external (fal.media) output into our durable store and
# rewrites the URLs BEFORE the agent sees the result, so the chat text and the render
# manifest reference the permanent copy instead of an ephemeral URL that later 404s.
# Called with a stub self (the method only reads self.tool_name).

FAL_URL = "https://v3b.fal.media/files/b/x.png"
DURABLE_URL = "https://cdn/artifacts/external/abc.png"


def _run_mirror(event):
    stub = types.SimpleNamespace(tool_name="fal___get_job_result")
    return asyncio.run(stw.JobTrackingMCPAgentTool._mirror_result_media(stub, event))


def test_results_mirror_swaps_media_urls(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "b")
    monkeypatch.setattr(stw, "_MIRROR_S3_CLIENT", object())  # skip boto3 client creation
    swapped = {"status": "success", "content": [{"text": f"frame {DURABLE_URL}"}]}
    monkeypatch.setattr(
        _pm, "swap_result_media",
        lambda result, **k: (swapped, [{"source_url": FAL_URL, "url": DURABLE_URL}]),
    )
    out = _run_mirror(ToolResultEvent({"status": "success", "content": [{"text": f"frame {FAL_URL}"}]}))
    assert out.tool_result is swapped  # agent now sees the durable URL


def test_results_mirror_passthrough_when_nothing_copied(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "b")
    monkeypatch.setattr(stw, "_MIRROR_S3_CLIENT", object())
    ev = ToolResultEvent({"status": "success", "content": [{"text": "no media"}]})
    monkeypatch.setattr(_pm, "swap_result_media", lambda result, **k: (result, []))
    assert _run_mirror(ev) is ev  # unchanged event when nothing was mirrored


def test_results_mirror_never_raises(monkeypatch):
    monkeypatch.setenv("SKILLS_S3_BUCKET", "b")
    monkeypatch.setattr(stw, "_MIRROR_S3_CLIENT", object())
    ev = ToolResultEvent({"status": "success", "content": [{"text": "x"}]})

    def boom(result, **k):
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(_pm, "swap_result_media", boom)
    assert _run_mirror(ev) is ev  # best-effort: a working tool is never failed by the mirror


def test_results_mirror_skips_without_bucket(monkeypatch):
    monkeypatch.delenv("SKILLS_S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    ev = ToolResultEvent({"status": "success", "content": [{"text": f"frame {FAL_URL}"}]})
    assert _run_mirror(ev) is ev  # no durable store configured -> passthrough


# ---------------------------------------------------------------------------
# NON-TRIO stream mirror: the fix that covers SYNCHRONOUS tools (fal run_model)
#
# The mirror used to be gated to the trio "results" role, so a synchronous
# single-call image tool (run_model) — wrapped by ResilientMCPAgentTool, not
# JobTrackingMCPAgentTool — never had its fal.media URL swapped, and the chat
# text kept an ephemeral URL that Slack won't inline. These pin that the
# non-trio wrapper's stream now mirrors its ToolResultEvent (and only that).
# ---------------------------------------------------------------------------

def _resilient_stub(base_events, mirror_fn):
    """A ResilientMCPAgentTool with __init__/base-stream/mirror stubbed for stream testing."""

    class T(stw.ResilientMCPAgentTool):
        def __init__(self):  # bypass MCPAgentTool.__init__ (needs a real MCP tool)
            self._reconnect = None

        async def _resilient_base_stream(self, tool_use, invocation_state, kwargs):
            for ev in base_events:
                yield ev

        async def _mirror_result_media(self, event):
            return mirror_fn(event)

    return T()


def test_non_trio_stream_mirrors_tool_result():
    orig = ToolResultEvent({"status": "success", "content": [{"text": f"img {FAL_URL}"}]})
    swapped = ToolResultEvent({"status": "success", "content": [{"text": f"img {DURABLE_URL}"}]})
    seen = {"n": 0}

    def mirror(ev):
        seen["n"] += 1
        assert ev is orig            # the raw fal result is what gets mirrored
        return swapped

    t = _resilient_stub([orig], mirror)
    out = _drain(t.stream({"toolUseId": "u"}, {}))
    assert out == [swapped]          # the agent sees the durable (swapped) result
    assert seen["n"] == 1            # mirror invoked exactly once on the result


def test_non_trio_stream_passes_non_result_events_untouched():
    """Non-ToolResultEvent items (e.g. progress/text) must not be routed through the mirror."""
    text_ev = "PROGRESS"
    result_ev = ToolResultEvent({"status": "success", "content": [{"text": "done"}]})
    mirrored = ToolResultEvent({"status": "success", "content": [{"text": "MIRRORED"}]})
    calls = {"n": 0}

    def mirror(ev):
        calls["n"] += 1
        return mirrored

    t = _resilient_stub([text_ev, result_ev], mirror)
    out = _drain(t.stream({"toolUseId": "u"}, {}))
    assert out == [text_ev, mirrored]  # non-result passed through; result mirrored
    assert calls["n"] == 1             # mirror called only for the ToolResultEvent
