"""Tests for the Wave 2 transport boundary types (additive)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.transport import (  # noqa: E402
    GatewayTool,
    GatewayTransport,
    RequestAuthContext,
    ToolResult,
    TransportHealth,
)


def test_request_auth_context_is_immutable():
    ctx = RequestAuthContext(actor_id="USER#alice", user_token="tok")
    with pytest.raises(Exception):
        ctx.actor_id = "USER#bob"  # frozen dataclass
    assert ctx.actor_id == "USER#alice"
    assert ctx.machine_token is None and ctx.delegation_grant is None


def test_value_types_construct():
    t = GatewayTool(name="target___tool", description="d", input_schema={"type": "object"})
    assert t.name == "target___tool"
    assert ToolResult(content="x").is_error is False
    h = TransportHealth(connected=True, negotiated_protocol="2025-03-26")
    assert h.connected and h.negotiated_protocol == "2025-03-26"


def test_conforming_adapter_satisfies_protocol():
    class _Stub:
        def negotiated_protocol(self):
            return None

        def list_tools(self, context):
            return []

        def call_tool(self, tool_use_id, name, arguments, context):
            return ToolResult()

        def search_tools(self, query, context):
            return []

        def health(self, context):
            return TransportHealth(connected=False)

        def close(self):
            pass

    assert isinstance(_Stub(), GatewayTransport)


def test_incomplete_adapter_fails_protocol_check():
    class _Partial:
        def list_tools(self, context):
            return []

    assert not isinstance(_Partial(), GatewayTransport)
