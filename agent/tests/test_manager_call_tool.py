"""Tests for invoking a gateway tool through the registry-backed manager.

Listing was moved onto the registry manager to fix BUG-3: federated tools
appeared in the catalog but failed validation, because the validator only knew
the single GATEWAY_ENDPOINTS URL. Invocation never got the same treatment, so a
federated tool call still went to the wrong gateway — and once that gateway
started authenticating end users instead of a machine identity, it would have
stopped working at all.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import MCPManager  # noqa: E402


class _FakeConn:
    def __init__(self, name, tools, connected=True):
        self.name = name
        self._tools = [{"name": t, "description": ""} for t in tools]
        self._connected = connected
        self.calls: list[tuple[str, dict]] = []
        self.connects = 0

    @property
    def tool_metadata(self):
        return list(self._tools)

    @property
    def is_connected(self):
        return self._connected

    def connect(self):
        self.connects += 1
        self._connected = True
        return {"status": "connected"}

    def call_tool_sync(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"content": [{"type": "text", "text": f"{tool_name} ok"}]}


def _mgr(**conns) -> MCPManager:
    m = MCPManager()
    m._gateways = dict(conns)
    return m


def test_resolves_the_gateway_that_exposes_the_tool():
    media = _FakeConn("media", ["speech___diarize", "video___probe"])
    aws = _FakeConn("aws", ["awsmcp___search_docs"])
    m = _mgr(media=media, aws=aws)

    m.call_tool("speech___diarize", {"x": 1})
    assert media.calls == [("speech___diarize", {"x": 1})]
    assert aws.calls == []


def test_federated_tool_reaches_its_own_gateway():
    """The behaviour BUG-3's listing fix had, which invocation lacked."""
    media = _FakeConn("media", ["speech___diarize"])
    aws = _FakeConn("aws", ["awsmcp___search_docs"])
    m = _mgr(media=media, aws=aws)

    m.call_tool("awsmcp___search_docs", {})
    assert aws.calls and not media.calls


def test_unknown_tool_fails_clearly():
    """Clearer than sending it to whichever endpoint happens to be configured."""
    m = _mgr(media=_FakeConn("media", ["a___b"]))
    with pytest.raises(RuntimeError, match="No connected gateway exposes"):
        m.call_tool("nope___missing", {})


def test_connects_on_demand():
    cold = _FakeConn("cold", ["t___x"], connected=False)
    m = _mgr(cold=cold)
    m.call_tool("t___x", {})
    assert cold.connects == 1
    assert cold.calls


def test_resolution_uses_reported_tools_not_the_name_prefix():
    """The prefix names a TARGET, not a gateway, so two gateways could collide."""
    a = _FakeConn("a", ["shared___one"])
    b = _FakeConn("b", ["shared___two"])
    m = _mgr(a=a, b=b)
    m.call_tool("shared___two", {})
    assert b.calls and not a.calls


def test_find_returns_none_when_absent():
    m = _mgr(a=_FakeConn("a", ["x___y"]))
    assert m.find_gateway_for_tool("x___y") is not None
    assert m.find_gateway_for_tool("q___z") is None


def test_exact_name_collision_across_gateways_fails_closed():
    """Property 13 / Req 13.10: two gateways exposing the SAME exact tool name
    must NOT resolve to whichever is first — routing fails closed."""
    a = _FakeConn("a", ["shared___dup"])
    b = _FakeConn("b", ["shared___dup"])
    m = _mgr(a=a, b=b)

    assert m.find_gateway_for_tool("shared___dup") is None
    with pytest.raises(RuntimeError, match="No connected gateway exposes"):
        m.call_tool("shared___dup", {})
    assert not a.calls and not b.calls, "an ambiguous tool must not be dispatched to any gateway"


def test_single_exposer_still_resolves_after_collision_hardening():
    """A tool exposed by exactly one gateway resolves normally (no false collision)."""
    a = _FakeConn("a", ["only___here", "shared___x"])
    b = _FakeConn("b", ["other___tool"])
    m = _mgr(a=a, b=b)
    assert m.find_gateway_for_tool("only___here") is a
    m.call_tool("only___here", {})
    assert a.calls == [("only___here", {})]
