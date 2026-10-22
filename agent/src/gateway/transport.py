"""Transport boundary for Gateway MCP access (Wave 2 complexity reduction).

Separates MCP connection/session mechanics from authorization, catalog policy,
and tool side effects so the ~1.8k-line ``mcp_manager`` can be decomposed and,
later, adopt a stateless MCP 2.x adapter without weakening authorization.

Concrete adapters (introduced incrementally) implement :class:`GatewayTransport`:
- a legacy Strands-session adapter (current behavior / configuration-controlled
  rollback), and
- a future protocol-gated stateless MCP ``2026-07-28`` adapter.

These types are **additive**: importing them changes no runtime behavior. Wiring
the existing ``GatewayConnection`` behind this Protocol happens step by step with
the full agent test suite as the gate at each step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class RequestAuthContext:
    """Immutable per-operation identity/authorization.

    A stateless adapter must supply exactly this on each operation and never
    retain it globally, so concurrent actors/grants cannot bleed across
    requests. Fields mirror the credentials the current tenant/platform paths
    already use; ``None`` means "not applicable to this operation".
    """

    actor_id: str = ""
    user_token: Optional[str] = None
    machine_token: Optional[str] = None
    delegation_grant: Optional[str] = None
    correlation_id: Optional[str] = None


@dataclass(frozen=True)
class GatewayTool:
    """One tool as advertised by a Gateway target (normalized)."""

    name: str
    description: str = ""
    input_schema: Optional[dict] = None
    annotations: Optional[dict] = None


@dataclass(frozen=True)
class ToolResult:
    """Normalized terminal result of a tool call."""

    content: Any = None
    is_error: bool = False


@dataclass(frozen=True)
class TransportHealth:
    """Liveness snapshot for a transport, including the negotiated protocol."""

    connected: bool
    negotiated_protocol: Optional[str] = None
    detail: str = ""


@runtime_checkable
class GatewayTransport(Protocol):
    """MCP connection/session mechanics, isolated from auth/catalog/side effects.

    Adapters own only protocol negotiation, listing, calling, searching,
    liveness, and teardown. Authorization headers/identity arrive per operation
    via :class:`RequestAuthContext`; catalog policy, allowlists, job tracking,
    and media mirroring live above this boundary.
    """

    def negotiated_protocol(self) -> Optional[str]: ...

    def list_tools(self, context: RequestAuthContext) -> list[GatewayTool]: ...

    def call_tool(
        self,
        tool_use_id: str,
        name: str,
        arguments: dict,
        context: RequestAuthContext,
    ) -> ToolResult: ...

    def search_tools(self, query: str, context: RequestAuthContext) -> list[dict]: ...

    def health(self, context: RequestAuthContext) -> TransportHealth: ...

    def close(self) -> None: ...


__all__ = [
    "RequestAuthContext",
    "GatewayTool",
    "ToolResult",
    "TransportHealth",
    "GatewayTransport",
]
