"""Tests for the legacy Strands-session transport adapter (Wave 2)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.legacy_adapter import LEGACY_PROTOCOL, LegacyStrandsTransport  # noqa: E402
from gateway.transport import GatewayTransport, RequestAuthContext, ToolResult  # noqa: E402


class _FakeConn:
    def __init__(self):
        self._tools = [
            {"name": "target___tool", "description": "does a thing", "inputSchema": {"type": "object"}},
        ]
        self._connected = True
        self.calls: list[tuple[str, dict]] = []
        self.searches: list[str] = []
        self.disconnected = 0

    @property
    def tool_metadata(self):
        return list(self._tools)

    @property
    def is_connected(self):
        return self._connected

    def call_tool_sync(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"content": [{"type": "text", "text": "ok"}]}

    def search_tools(self, query):
        self.searches.append(query)
        return [{"name": "target___tool"}]

    def disconnect(self):
        self.disconnected += 1
        self._connected = False
        return True


_CTX = RequestAuthContext(actor_id="USER#alice")


def test_adapter_conforms_to_protocol():
    assert isinstance(LegacyStrandsTransport(_FakeConn()), GatewayTransport)


def test_negotiated_protocol_is_legacy():
    assert LegacyStrandsTransport(_FakeConn()).negotiated_protocol() == LEGACY_PROTOCOL == "2025-03-26"


def test_list_tools_maps_metadata():
    tools = LegacyStrandsTransport(_FakeConn()).list_tools(_CTX)
    assert len(tools) == 1
    assert tools[0].name == "target___tool"
    assert tools[0].description == "does a thing"
    assert tools[0].input_schema == {"type": "object"}


def test_call_tool_delegates_and_wraps_result():
    conn = _FakeConn()
    result = LegacyStrandsTransport(conn).call_tool("tuid-1", "target___tool", {"q": 1}, _CTX)
    assert isinstance(result, ToolResult) and result.is_error is False
    # GatewayConnection's public surface is (name, arguments); it owns tool_use_id.
    assert conn.calls == [("target___tool", {"q": 1})]


def test_search_and_health_and_close_delegate():
    conn = _FakeConn()
    t = LegacyStrandsTransport(conn)
    assert t.search_tools("hi", _CTX) == [{"name": "target___tool"}]
    assert conn.searches == ["hi"]
    h = t.health(_CTX)
    assert h.connected is True and h.negotiated_protocol == "2025-03-26"
    t.close()
    assert conn.disconnected == 1
    assert t.health(_CTX).connected is False
