"""Parallel source fan-out, merge, and partial-error reporting (R6, R23).

One category load starts every relevant source concurrently, gives each its own
bounded timeout, merges what came back, and reports what did not. The shape of
the response never depends on which sources succeeded — a caller always gets
items plus an explicit list of degraded sources, so "empty because nothing
matched" and "empty because the only source that had results timed out" are
distinguishable (R12).

What this module deliberately does **not** do is fall back to the AgentCore
Runtime. That is the whole point of the migration: a Registry timeout returns a
partial error, and the only way back to the legacy path is an operator flipping
a category flag (Property 5, R14).

Which sources run per category:

===============  ==========================================================
skill            AssetIndex + Registry* + built-ins
specialty-agent  AssetIndex + Registry* + built-ins
tool             Gateway only — a Gateway tool is not an authored asset, and
                 internal native tools are excluded by policy (R10)
tool-collection  AssetIndex + Registry*
workflow         AssetIndex + Registry*
hook             AssetIndex only — actor-private, never harness-shared (R3)
===============  ==========================================================

\\* Registry is queried only in ``registry`` mode. In ``legacy``, ``shadow``, and
``canonical`` mode the served response comes from canonical state alone —
``canonical`` differs from the first two only in *who* answers (this service
rather than the Runtime), which is where the latency win lives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Sequence

from .config import CapabilityConfig
from .dto import CapabilitySummary, CategoryPage, SourceError, merge_summaries
from .sources import (
    SourceUnavailable,
    read_asset_index,
    read_builtins,
    read_gateway_catalog,
    read_registry,
)

logger = logging.getLogger(__name__)

#: Categories whose inventory includes shipped built-ins.
_BUILTIN_CATEGORIES: frozenset[str] = frozenset({"skill", "specialty-agent"})

#: Categories backed by authored AssetIndex rows.
_ASSET_CATEGORIES: frozenset[str] = frozenset(
    {"skill", "specialty-agent", "tool-collection", "workflow", "hook"}
)

#: Categories that can have a harness-shared Registry projection. ``hook`` is
#: absent by design (R3).
_REGISTRY_CATEGORIES: frozenset[str] = frozenset(
    {"skill", "specialty-agent", "tool-collection", "workflow"}
)


class ActorContext:
    """Trusted request identity, derived from the validated token only.

    Deliberately *not* built from a request body. Payload-supplied actor,
    group, organization, or owner fields are ignored everywhere downstream, so
    a caller cannot widen their own visibility by asserting membership (R5).
    """

    __slots__ = ("actor_id", "group_ids", "org_id", "session_id", "request_id")

    def __init__(
        self,
        *,
        actor_id: str,
        group_ids: Sequence[str] = (),
        org_id: Optional[str] = None,
        session_id: str = "",
        request_id: str = "",
    ) -> None:
        if not actor_id:
            raise ValueError("actor_id is required")
        self.actor_id = actor_id
        self.group_ids = list(group_ids)
        self.org_id = org_id
        self.session_id = session_id
        self.request_id = request_id

    def as_lab_context(self) -> Any:
        """Adapt to ``lab.context.LabRequestContext`` for the domain layer.

        ``org_id`` is attached as an extra attribute because the resolver reads
        it via ``getattr(ctx, "org_id", None)`` — the frozen dataclass does not
        declare it yet, and this keeps the adapter honest about that instead of
        silently dropping the org tier.
        """
        from lab.context import LabRequestContext

        ctx = LabRequestContext(
            actor_id=self.actor_id,
            session_id=self.session_id,
            request_id=self.request_id or "capability-api",
            group_ids=list(self.group_ids),
        )
        if self.org_id:
            object.__setattr__(ctx, "org_id", self.org_id)
        return ctx


async def _with_timeout(
    name: str, coro: Awaitable[Any], timeout: float
) -> tuple[str, Any]:
    """Run one source, converting failures into a classified error value.

    Nothing raises out of here: a source that fails must degrade this request,
    not abort it.
    """
    try:
        return name, await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("capability source timed out: source=%s limit=%ss", name, timeout)
        return name, SourceUnavailable("timeout", name)
    except SourceUnavailable as err:
        logger.warning("capability source unavailable: source=%s kind=%s", name, err.kind)
        return name, err
    except Exception as err:  # noqa: BLE001 — classified, never echoed raw
        logger.exception("capability source failed: source=%s", name)
        return name, SourceUnavailable("error", name)


def _plan(
    category: str,
    *,
    config: CapabilityConfig,
    actor: ActorContext,
    asset_index: Any,
    gateway_table: Any,
    cursor: Optional[str],
    limit: int,
    registry_data_client: Any = None,
    gateway_control: Any = None,
) -> dict[str, Callable[[], Awaitable[Any]]]:
    """Which source coroutines to start for ``category``."""
    plan: dict[str, Callable[[], Awaitable[Any]]] = {}

    if category in _ASSET_CATEGORIES and asset_index is not None:
        plan["asset-index"] = lambda: read_asset_index(
            category,
            ctx=actor.as_lab_context(),
            asset_index=asset_index,
            org_id=actor.org_id,
            cursor=cursor,
            limit=limit,
        )

    if category in _REGISTRY_CATEGORIES and config.registry_reads_enabled(category):
        plan["agent-registry"] = lambda: read_registry(
            category, config=config, client=registry_data_client
        )

    if category in _BUILTIN_CATEGORIES:
        # `gateway_table` because the built-in SKILL catalog is a DynamoDB row the
        # deploy writes (agents are a descriptor in the module and ignore it). Passing
        # it here rather than letting the source reach for its own client keeps every
        # source's dependencies visible in this plan.
        plan["builtin"] = lambda: read_builtins(
            category, gateway_table=gateway_table
        )

    if category == "tool":
        plan["gateway"] = lambda: read_gateway_catalog(
            config=config,
            actor_id=actor.actor_id,
            gateway_table=gateway_table,
            control=gateway_control,
        )

    return plan


async def load_category(
    category: str,
    *,
    config: CapabilityConfig,
    actor: ActorContext,
    asset_index: Any = None,
    gateway_table: Any = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    registry_data_client: Any = None,
    gateway_control: Any = None,
) -> CategoryPage:
    """Load one category from every relevant source, concurrently.

    Returns a :class:`CategoryPage` whose ``partial_errors`` name each degraded
    source. The AssetIndex is the one source whose failure is *not* partial for
    an asset-backed category: authorization cannot be reconstructed from the
    remaining sources, so its error is reported and the page carries only what
    the shared sources could offer — never a reconstructed guess (R2).
    """
    plan = _plan(
        category,
        config=config,
        actor=actor,
        asset_index=asset_index,
        gateway_table=gateway_table,
        cursor=cursor,
        limit=limit,
        registry_data_client=registry_data_client,
        gateway_control=gateway_control,
    )
    if not plan:
        return CategoryPage(category=category)

    timeout = config.source_timeout_seconds
    results = await asyncio.gather(
        *(_with_timeout(name, factory(), timeout) for name, factory in plan.items())
    )

    summaries: list[CapabilitySummary] = []
    errors: list[SourceError] = []
    succeeded: list[str] = []
    next_cursor: Optional[str] = None

    for name, value in results:
        if isinstance(value, SourceUnavailable):
            errors.append(SourceError(source=name, error=value.kind, detail=value.detail))
            continue
        succeeded.append(name)
        if name == "asset-index":
            items, next_cursor = value
            summaries.extend(items)
        else:
            summaries.extend(value)

    return CategoryPage(
        category=category,
        items=merge_summaries(summaries),
        next_cursor=next_cursor,
        partial_errors=tuple(errors),
        sources_queried=tuple(sorted(succeeded)),
    )


async def bootstrap(
    categories: Sequence[str],
    *,
    config: CapabilityConfig,
    actor: ActorContext,
    asset_index: Any = None,
    gateway_table: Any = None,
    limit: int = 100,
    registry_data_client: Any = None,
    gateway_control: Any = None,
) -> dict[str, CategoryPage]:
    """Load several categories in one round trip.

    Only the categories the caller asked for are loaded. A tab selection must
    not trigger hidden preloads of the other four (R10/R13): that requirement
    is enforced here by construction — there is no "load everything" path.
    """
    pages = await asyncio.gather(
        *(
            load_category(
                category,
                config=config,
                actor=actor,
                asset_index=asset_index,
                gateway_table=gateway_table,
                limit=limit,
                registry_data_client=registry_data_client,
                gateway_control=gateway_control,
            )
            for category in categories
        )
    )
    return {page.category: page for page in pages}


__all__ = ["ActorContext", "bootstrap", "load_category"]
