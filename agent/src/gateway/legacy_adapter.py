"""Legacy Strands-session transport adapter (Wave 2).

Implements the :class:`GatewayTransport` boundary over the existing persistent
``GatewayConnection`` (Strands ``MCPClient`` with a long-lived session). This is
the configuration-controlled **rollback path**: it preserves current behavior
exactly by delegating to the connection's proven methods.

It is introduced additively — nothing wires the manager through it yet, so no
runtime behavior changes. Wiring ``MCPManager`` to route through a transport
(legacy by default, stateless when a modern endpoint negotiates ``2026-07-28``)
is a later step guarded by the full suite.

Note on identity: the legacy persistent session snapshots its auth headers at
connect time (Strands runs the transport on a background thread seeded with a
contextvars snapshot at ``__enter__``), so the caller's identity is already
bound on the connection. ``RequestAuthContext`` is accepted for interface parity
with the stateless adapter but the legacy path continues to use the connection's
bound identity — it does not (and cannot) re-bind per operation.
"""

from __future__ import annotations

from typing import Any, Optional

from .transport import (
    GatewayTool,
    RequestAuthContext,
    ToolResult,
    TransportHealth,
)

# The protocol the current AgentCore Gateway advertises / negotiates on the
# legacy initialize path. Recorded so callers can gate stateless selection.
LEGACY_PROTOCOL = "2025-03-26"


class LegacyStrandsTransport:
    """``GatewayTransport`` over a persistent ``GatewayConnection`` (rollback path)."""

    def __init__(self, connection: Any) -> None:
        self._c = connection

    def negotiated_protocol(self) -> Optional[str]:
        return LEGACY_PROTOCOL

    def list_tools(self, context: RequestAuthContext) -> list[GatewayTool]:
        tools: list[GatewayTool] = []
        for meta in self._c.tool_metadata:
            tools.append(
                GatewayTool(
                    name=str(meta.get("name") or ""),
                    description=str(meta.get("description") or ""),
                    input_schema=meta.get("inputSchema"),
                    annotations=meta.get("annotations"),
                )
            )
        return tools

    def call_tool(
        self,
        tool_use_id: str,
        name: str,
        arguments: dict,
        context: RequestAuthContext,
    ) -> ToolResult:
        # GatewayConnection.call_tool_sync owns tool_use_id generation and the
        # Strands 3-arg forward; its public surface is (name, arguments).
        raw = self._c.call_tool_sync(name, arguments)
        return ToolResult(content=raw, is_error=False)

    def search_tools(self, query: str, context: RequestAuthContext) -> list[dict]:
        return self._c.search_tools(query)

    def health(self, context: RequestAuthContext) -> TransportHealth:
        return TransportHealth(
            connected=bool(self._c.is_connected),
            negotiated_protocol=LEGACY_PROTOCOL,
        )

    def close(self) -> None:
        self._c.disconnect()


__all__ = ["LegacyStrandsTransport", "LEGACY_PROTOCOL"]
