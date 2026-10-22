"""Installed-SDK contract tests for Strands ``MCPClient`` + ``GatewayConnection``.

Wave 0 / Requirement 12.2-12.3. The harness invokes the REAL
``MCPClient.call_tool_sync``, whose signature is ``(tool_use_id, name,
arguments)``. The existing ``test_manager_call_tool`` fakes model
``GatewayConnection``'s OWN 2-arg public API (legitimately), so they cannot
catch a mismatch in the ``GatewayConnection -> raw client`` forward. These tests
pin the real installed signature and exercise the forwarding contract directly,
so a 2-arg call (which silently misbinds the arguments dict into ``name``) is
caught here rather than only in production.
"""

from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import GatewayConnection  # noqa: E402


def test_installed_mcpclient_call_tool_sync_is_three_positional():
    """Pin the real installed contract: (self, tool_use_id, name, arguments, ...)."""
    from strands.tools.mcp.mcp_client import MCPClient

    params = list(inspect.signature(MCPClient.call_tool_sync).parameters)
    assert params[1:4] == ["tool_use_id", "name", "arguments"], params


class _RealSigClient:
    """Fake carrying the REAL installed ``call_tool_sync`` positional order."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def call_tool_sync(
        self,
        tool_use_id,
        name,
        arguments=None,
        read_timeout_seconds=None,
        meta=None,
        progress_callback=None,
        *,
        cancel_signal=None,
    ):
        self.calls.append((tool_use_id, name, arguments))

        class _Block:
            text = "{}"

        class _Result:
            content = [_Block()]

        return _Result()


def _conn_with(client: _RealSigClient) -> GatewayConnection:
    gw = GatewayConnection("gw-test", {"url": "https://gw.example/mcp"})
    gw._client = client
    gw._connected = True
    gw._search_available = True
    return gw


def test_call_tool_sync_forwards_three_arg_contract():
    c = _RealSigClient()
    gw = _conn_with(c)

    gw.call_tool_sync("target___tool", {"q": 1})

    assert len(c.calls) == 1
    tool_use_id, name, arguments = c.calls[0]
    assert name == "target___tool", f"tool name must land in 'name', got {name!r}"
    assert arguments == {"q": 1}, f"arguments must land in 'arguments', got {arguments!r}"
    assert isinstance(tool_use_id, str) and tool_use_id, "a non-empty tool_use_id is required"


def test_search_tools_forwards_three_arg_contract():
    c = _RealSigClient()
    gw = _conn_with(c)

    gw.search_tools("hello world")

    assert c.calls, "search_tools must invoke the client"
    tool_use_id, name, arguments = c.calls[0]
    assert name == "x_amz_bedrock_agentcore_search", f"got {name!r}"
    assert arguments == {"query": "hello world"}, f"got {arguments!r}"
    assert isinstance(tool_use_id, str) and tool_use_id


# --- Wave 6: protocol-gated stateless MCP 2.x is provided by the SDK, not hand-written ---------
# The original Wave 6 plan envisioned manually "protocol-gating" a stateless adapter for the
# 2026-07-28 revision. The MCP 2.x SDK (adopted in Wave 1) obviates that: it auto-negotiates the
# modern era at connect time (mode='auto', mcp.client._probe.negotiate_auto) and the streamable-HTTP
# transport runs statelessly whenever the server issues no session id. So the runtime already speaks
# 2026-07-28 statelessly with any gateway that advertises it (the Platform gateway does) with no
# adapter-selection code of our own. These guards fail loudly if an SDK bump drops that support.

def test_sdk_negotiates_modern_2026_07_28_protocol():
    import mcp_types.version as v
    assert "2026-07-28" in v.SUPPORTED_PROTOCOL_VERSIONS, v.SUPPORTED_PROTOCOL_VERSIONS
    assert v.MODERN_PROTOCOL_VERSIONS == ("2026-07-28",), v.MODERN_PROTOCOL_VERSIONS
    assert v.LATEST_PROTOCOL_VERSION == "2026-07-28", v.LATEST_PROTOCOL_VERSION


def test_sdk_provides_auto_era_negotiation_and_stateless_transport():
    # Connect-time era negotiation (mode='auto') — the "protocol gate" is the SDK's job.
    from mcp.client import _probe
    assert hasattr(_probe, "negotiate_auto")
    # Streamable-HTTP tracks an OPTIONAL session id; absent id == stateless operation.
    import inspect
    from mcp.client import streamable_http as sh
    src = inspect.getsource(sh)
    assert "mcp-session-id" in src.lower(), "streamable-HTTP must handle the optional session id"
