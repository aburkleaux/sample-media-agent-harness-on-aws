"""GatewayBrowser — read-only projection of AgentCore Gateway tool listings.

Wraps AgentCore_Gateway calls with the Lab's per-session caching and
stale-labeling rules. Lists tools authorized for the current actorId;
caches results per session.

The Lab's gateway browser does NOT call add_tool / trust_tool on the
agent's tool registry; it is a catalog read projection plus a tool-test
invocation path.

Requirements: 4.1, 4.2, 4.8, 4.9
Properties: 22
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .context import LabRequestContext
from .errors import GatewayUnavailableError
from .models import GatewayProjection, GatewayToolDescriptor

logger = logging.getLogger(__name__)

# Default timeout for Gateway calls (seconds)
GATEWAY_TIMEOUT_SECONDS = 30


@dataclass
class ToolInvocationResult:
    """Result of a single Gateway tool invocation.

    Attributes:
        response_body: The raw response body returned by the Gateway.
        status: The HTTP status code or equivalent status from the Gateway.
        duration_ms: The duration of the invocation in milliseconds.
    """

    response_body: Any
    status: int
    duration_ms: int


class GatewayClient(Protocol):
    """Protocol for the underlying Gateway connection used by GatewayBrowser.

    This abstracts the actual HTTP/MCP call to the AgentCore Gateway,
    allowing the browser to be tested with fakes.
    """

    def list_tools(self, actor_id: str, timeout: float) -> list[GatewayToolDescriptor]:
        """Query the Gateway for tools authorized for the given actor.

        Args:
            actor_id: The actor identity to query tools for.
            timeout: Maximum seconds to wait for a response.

        Returns:
            List of GatewayToolDescriptor objects.

        Raises:
            GatewayUnavailableError: On timeout, network error, or non-2xx response.
        """
        ...

    def call_tool(
        self, tool_name: str, tool_version: str, input_data: Any, timeout: float
    ) -> ToolInvocationResult:
        """Invoke a single Gateway tool with the given input.

        Args:
            tool_name: The name of the tool to invoke.
            tool_version: The version of the tool to invoke.
            input_data: The input payload to send to the tool.
            timeout: Maximum seconds to wait for a response.

        Returns:
            ToolInvocationResult with response body, status, and duration.

        Raises:
            GatewayUnavailableError: On timeout, network error, or non-2xx response.
        """
        ...


@dataclass
class BrowseResult:
    """Result of a browse or refresh operation.

    Attributes:
        projection: The GatewayProjection containing tool descriptors.
        stale: Whether the projection is stale (returned from cache after
               a failed Gateway call).
    """

    projection: GatewayProjection
    stale: bool


@dataclass
class GatewayEndpointConfig:
    """Configuration for a single Gateway endpoint loaded at startup.

    Attributes:
        endpoint_id: Unique identifier for this endpoint.
        url: The Gateway URL.
        name: Human-readable name for the endpoint.
    """

    endpoint_id: str
    url: str
    name: str = ""


class GatewayBrowser:
    """Read-only browser for AgentCore Gateway tool listings.

    Provides per-session caching keyed by (actor_id, session_id) with
    lazy population on first access and explicit refresh via refresh().

    Stale-labeling rules (Requirement 4.2):
    - On Gateway failure with a prior projection available: return the
      cached projection labeled stale with its retrieval timestamp.
    - On Gateway failure with no prior projection: raise
      GatewayUnavailableError.

    Startup behavior (Requirement 4.9):
    - Loads Configured_Gateway_Endpoints from config only.
    - Does NOT populate per-actor projections at startup.
    - Projections are populated lazily on first browse() call or on
      refresh().
    """

    def __init__(
        self,
        gateway_client: GatewayClient,
        configured_endpoints: list[GatewayEndpointConfig] | None = None,
    ) -> None:
        """Initialize the GatewayBrowser.

        Args:
            gateway_client: The underlying client used to query the Gateway.
            configured_endpoints: Gateway endpoints loaded from platform
                configuration at startup (Requirement 4.9). These define
                the universe of Gateways the Lab is authorized to query.
        """
        self._client = gateway_client
        self._configured_endpoints: list[GatewayEndpointConfig] = (
            configured_endpoints or []
        )
        # Per-session cache: (actor_id, session_id) -> GatewayProjection
        self._cache: dict[tuple[str, str], GatewayProjection] = {}

    @property
    def configured_endpoints(self) -> list[GatewayEndpointConfig]:
        """Return the set of Configured_Gateway_Endpoints loaded at startup."""
        return list(self._configured_endpoints)

    def browse(self, ctx: LabRequestContext) -> BrowseResult:
        """Browse Gateway tools for the current actor/session.

        Lazy-populates the per-session cache on first call. Subsequent
        calls within the same session return the cached projection.

        On Gateway failure during lazy population:
        - If a prior projection exists for this (actor_id, session_id):
          returns it labeled stale.
        - If no prior projection exists: raises GatewayUnavailableError.

        Args:
            ctx: The current request context providing actor_id and session_id.

        Returns:
            BrowseResult with the projection and stale flag.

        Raises:
            GatewayUnavailableError: When the Gateway is unreachable and
                no prior projection is cached for this session.
        """
        cache_key = (ctx.actor_id, ctx.session_id)

        # Cache hit — return existing projection as fresh (within session)
        existing = self._cache.get(cache_key)
        if existing is not None:
            return BrowseResult(projection=existing, stale=False)

        # Cache miss — query Gateway (lazy populate)
        try:
            tools = self._client.list_tools(
                actor_id=ctx.actor_id,
                timeout=GATEWAY_TIMEOUT_SECONDS,
            )
            projection = GatewayProjection(
                retrieved_at=_now_epoch_ms(),
                actor_id=ctx.actor_id,
                tools=tools,
            )
            self._cache[cache_key] = projection
            return BrowseResult(projection=projection, stale=False)

        except (GatewayUnavailableError, Exception) as exc:
            # No prior projection — cannot serve stale data
            logger.warning(
                "Gateway unavailable for actor=%s session=%s: %s",
                ctx.actor_id,
                ctx.session_id,
                exc,
            )
            raise GatewayUnavailableError(
                "Tool discovery failed: Gateway is unavailable and no "
                "prior projection is cached for this session."
            ) from exc

    def refresh(self, ctx: LabRequestContext) -> BrowseResult:
        """Re-query the Gateway and replace the cached projection.

        User-initiated operation (RefreshGatewayTools). On success,
        replaces the per-session projection and stamps it with
        retrieved_at = now.

        On Gateway failure:
        - If a prior projection exists: returns it labeled stale with
          its original retrieved_at timestamp.
        - If no prior projection exists: raises GatewayUnavailableError.

        Args:
            ctx: The current request context providing actor_id and session_id.

        Returns:
            BrowseResult with the (possibly stale) projection.

        Raises:
            GatewayUnavailableError: When the Gateway is unreachable and
                no prior projection is cached for this session.
        """
        cache_key = (ctx.actor_id, ctx.session_id)

        try:
            tools = self._client.list_tools(
                actor_id=ctx.actor_id,
                timeout=GATEWAY_TIMEOUT_SECONDS,
            )
            projection = GatewayProjection(
                retrieved_at=_now_epoch_ms(),
                actor_id=ctx.actor_id,
                tools=tools,
            )
            self._cache[cache_key] = projection
            return BrowseResult(projection=projection, stale=False)

        except (GatewayUnavailableError, Exception) as exc:
            # Stale-labeling: if prior projection exists, return it as stale
            existing = self._cache.get(cache_key)
            if existing is not None:
                logger.info(
                    "Gateway refresh failed for actor=%s session=%s; "
                    "returning stale projection (retrieved_at=%d): %s",
                    ctx.actor_id,
                    ctx.session_id,
                    existing.retrieved_at,
                    exc,
                )
                return BrowseResult(projection=existing, stale=True)

            # No prior projection — cannot serve stale data
            logger.warning(
                "Gateway refresh failed for actor=%s session=%s "
                "with no prior projection: %s",
                ctx.actor_id,
                ctx.session_id,
                exc,
            )
            raise GatewayUnavailableError(
                "Tool discovery failed: Gateway is unavailable and no "
                "prior projection is cached for this session."
            ) from exc

    def get_projection(self, ctx: LabRequestContext) -> Optional[GatewayProjection]:
        """Return the cached projection for the given context, or None.

        This is a read-only accessor that does not trigger any Gateway call.
        Useful for internal consumers (e.g., ToolCollectionCRUD validation)
        that need to check the current projection without side effects.

        Args:
            ctx: The current request context.

        Returns:
            The cached GatewayProjection or None if not yet populated.
        """
        cache_key = (ctx.actor_id, ctx.session_id)
        return self._cache.get(cache_key)

    def invalidate(self, ctx: LabRequestContext) -> None:
        """Remove the cached projection for the given context.

        Primarily for testing or session cleanup.

        Args:
            ctx: The current request context.
        """
        cache_key = (ctx.actor_id, ctx.session_id)
        self._cache.pop(cache_key, None)

    def call_tool(
        self,
        tool_name: str,
        tool_version: str,
        input_data: Any,
        timeout: float = GATEWAY_TIMEOUT_SECONDS,
    ) -> ToolInvocationResult:
        """Invoke a single Gateway tool directly.

        This is the tool-test invocation path used by the Test_Harness
        (Requirement 4.6). It calls the Gateway's call_tool method directly
        without any Task Agent involvement.

        Args:
            tool_name: The name of the tool to invoke.
            tool_version: The version of the tool to invoke.
            input_data: The input payload to send to the tool.
            timeout: Maximum seconds to wait for a response.

        Returns:
            ToolInvocationResult with response body, status, and duration.

        Raises:
            GatewayUnavailableError: On timeout, network error, or non-2xx response.
        """
        return self._client.call_tool(
            tool_name=tool_name,
            tool_version=tool_version,
            input_data=input_data,
            timeout=timeout,
        )


def _now_epoch_ms() -> int:
    """Return the current time as epoch milliseconds."""
    return int(time.time() * 1000)
