"""Normalized capability DTOs shared by every Capability API source.

One shape describes a capability regardless of where it came from: a working
copy in the AssetIndex, an approved record in the harness-shared Agent
Registry, a tool exposed by a shared MCP Gateway target, or a built-in that
ships with the harness. The browser never sees an AWS-shaped payload — the
API resolves each source into :class:`CapabilitySummary` (R6).

snake_case on the Python side, camelCase at the JSON boundary. ``None`` and
empty-tuple fields are omitted from the JSON so a Gateway tool summary does
not carry a wall of null asset fields (and vice versa).

The dedupe key everywhere is ``(category, asset_id, version)``; when the same
identity arrives from more than one source the AssetIndex copy wins for
owner-visible lifecycle fields because it is canonical (R2).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Categories the Capability API accepts. Mirrors ``lab.models.Category``
#: plus the Gateway-only ``tool`` pseudo-category, which has no AssetIndex
#: row (a Gateway tool is not an authored asset).
CATEGORIES: frozenset[str] = frozenset(
    {"skill", "specialty-agent", "tool", "tool-collection", "workflow", "hook"}
)

#: ``capabilityClass`` distinguishes *how* a capability is realized within a
#: category, which the UI needs in order to render (and gate) correctly:
#:   asset            authored, versioned AssetIndex row
#:   gateway-mcp      a tool exposed by a shared MCP Gateway target
#:   builtin          ships with the harness; no canonical row
#:   tool-collection  a curated selection of gateway tools
CAPABILITY_CLASSES: frozenset[str] = frozenset(
    {"asset", "gateway-mcp", "builtin", "tool-collection"}
)

#: ``source`` records which reader produced the summary. Surfaced so partial
#: failures and parity mismatches can be attributed without guessing.
SOURCES: frozenset[str] = frozenset(
    {"asset-index", "agent-registry", "gateway", "builtin"}
)

#: Projection health as seen by an authorized owner. ``ineligible`` means the
#: asset is deliberately outside the shared Registry (private/group/working
#: copy) — not an error (R12).
PROJECTION_STATUSES: frozenset[str] = frozenset(
    {"in_sync", "pending", "failed", "ineligible"}
)


def _camel(name: str) -> str:
    """``schema_digest`` -> ``schemaDigest``."""
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilitySummary:
    """One capability, normalized across sources.

    Args:
        id: Stable display/react key. ``"skill:asset-123:v2"`` for assets,
            ``"tool:<gatewayId>:<targetId>:<bareToolName>"`` for Gateway
            tools. Distinct targets therefore never collide even when their
            bare tool names repeat (R4/R10).
        category: One of :data:`CATEGORIES`.
        capability_class: One of :data:`CAPABILITY_CLASSES`.
        display_name: Human label. Never used for identity — a rename must
            not fork a Registry lineage (R4).
        description: The author's own description of the capability, shown on
            inventory rows so a list of names says what each thing is FOR.
            ``None`` means the source cannot answer (Gateway tools, built-ins
            and Registry records carry no description), which is NOT the same
            as an author who left it blank — the row renders no description
            line in either case, but a client must not turn the first into
            "this has no description".
        scope: ``private`` / ``group`` / ``org`` for assets;
            ``harness-shared`` for anything every authenticated user of the
            environment can see (Gateway tools, built-ins, Registry records).
        status: ``draft`` / ``published`` / ``deprecated``.
        source: One of :data:`SOURCES`.
        asset_id: Canonical lineage id. ``None`` for Gateway tools.
        version: Exact canonical version. ``None`` on a working copy.
        registry_record_id: Set when this summary came from, or has been
            projected into, the shared Registry.
        gateway_id, target_id, target_name: Gateway provenance. Retained even
            when the UI visually consolidates repeated bare names.
        qualified_tool_name: Target-qualified invocation identity, e.g.
            ``media-processing___process_media``. This — not
            ``bare_tool_name`` — is what a Runtime actually calls.
        bare_tool_name: The tool name as the target declares it. Repeats
            across targets by design (``get_job_status``).
        schema_digest: ``sha256:...`` over the tool's input schema, so a
            silent upstream schema change is detectable.
        agent_availability: Which agents can bind this capability
            (``("main", "task")``).
        invoked_via: How a built-in sub-agent is reached (``dispatch_tool``,
            ``plan_and_execute_tool``). Built-ins only — the harness has more
            than one sub-agent invocation path and the Agents panel says which,
            rather than implying every runtime is a dispatch target.
        configured: Whether a built-in's runtime is actually wired in this
            deployment, from its ARN environment variable. ``None`` means the
            question does not apply (anything that is not a built-in sub-agent);
            ``False`` means it applies and the answer is no. Keep those apart —
            collapsing them renders "we did not look" as "not deployed".
        runtime_id: The runtime's id (an ARN's last path segment) when
            ``configured``.
        projection_status: One of :data:`PROJECTION_STATUSES`.
        updated_at: ISO-8601 UTC.
    """

    id: str
    category: str
    capability_class: str
    display_name: str
    scope: str
    status: str
    source: str
    description: Optional[str] = None
    asset_id: Optional[str] = None
    version: Optional[int | str] = None
    registry_record_id: Optional[str] = None
    gateway_id: Optional[str] = None
    target_id: Optional[str] = None
    target_name: Optional[str] = None
    qualified_tool_name: Optional[str] = None
    bare_tool_name: Optional[str] = None
    schema_digest: Optional[str] = None
    agent_availability: tuple[str, ...] = ()
    invoked_via: Optional[str] = None
    configured: Optional[bool] = None
    runtime_id: Optional[str] = None
    projection_status: Optional[str] = None
    updated_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category: {self.category!r}")
        if self.capability_class not in CAPABILITY_CLASSES:
            raise ValueError(
                f"unknown capabilityClass: {self.capability_class!r}"
            )
        if self.source not in SOURCES:
            raise ValueError(f"unknown source: {self.source!r}")
        if (
            self.projection_status is not None
            and self.projection_status not in PROJECTION_STATUSES
        ):
            raise ValueError(
                f"unknown projectionStatus: {self.projection_status!r}"
            )

    @property
    def dedupe_key(self) -> tuple[str, Optional[str], Optional[int | str]]:
        """``(category, assetId, version)`` — the cross-source identity.

        Gateway tools have no ``asset_id``, so they fall back to their
        target-qualified id, which is already unique per (gateway, target,
        tool).
        """
        if self.asset_id is None:
            return (self.category, self.id, None)
        return (self.category, self.asset_id, self.version)

    def to_json(self) -> dict[str, Any]:
        """camelCase dict with empty fields dropped."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if value is None or value == ():
                continue
            out[_camel(key)] = list(value) if isinstance(value, tuple) else value
        return out


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceError:
    """A single source failing without failing the whole request (R12).

    ``error`` is a *class* (``timeout`` / ``throttled`` / ``unavailable`` /
    ``config`` / ``error``), never a raw exception string — descriptor bodies
    and private paths must not reach logs or the browser.
    """

    source: str
    error: str
    detail: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        out = {"source": self.source, "error": self.error}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class CategoryPage:
    """One page of one category, plus which sources degraded producing it."""

    category: str
    items: tuple[CapabilitySummary, ...] = ()
    next_cursor: Optional[str] = None
    partial_errors: tuple[SourceError, ...] = ()
    #: Which sources answered successfully — lets the UI distinguish "no
    #: results" from "the only source that would have had results failed".
    sources_queried: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "category": self.category,
            "items": [item.to_json() for item in self.items],
        }
        if self.next_cursor:
            out["nextCursor"] = self.next_cursor
        if self.partial_errors:
            out["partialErrors"] = [e.to_json() for e in self.partial_errors]
        if self.sources_queried:
            out["sourcesQueried"] = list(self.sources_queried)
        return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

#: Source precedence when the same ``dedupe_key`` arrives twice. AssetIndex is
#: canonical for lifecycle fields (R2), so it outranks the eventually
#: consistent Registry projection; ``builtin`` is last because a built-in that
#: has been projected should render with its Registry identity.
_SOURCE_RANK: dict[str, int] = {
    "asset-index": 0,
    "gateway": 1,
    "agent-registry": 2,
    "builtin": 3,
}


def merge_summaries(
    summaries: Iterable[CapabilitySummary],
) -> tuple[CapabilitySummary, ...]:
    """Dedupe by ``(category, assetId, version)`` preferring canonical state.

    When an asset shows up both as an AssetIndex row and as an approved
    Registry record, the AssetIndex row wins but keeps the Registry record id
    and ``in_sync`` projection status from its twin — that pairing is exactly
    what tells an owner their publish landed.
    """
    winners: dict[tuple[str, Optional[str], Optional[int | str]], CapabilitySummary] = {}
    for item in summaries:
        key = item.dedupe_key
        prior = winners.get(key)
        if prior is None:
            winners[key] = item
            continue
        best, other = (
            (prior, item)
            if _SOURCE_RANK.get(prior.source, 99) <= _SOURCE_RANK.get(item.source, 99)
            else (item, prior)
        )
        # Carry the Registry-side identity onto the canonical winner so the
        # owner sees "published AND discoverable" rather than one or the other.
        if best.registry_record_id is None and other.registry_record_id:
            best = CapabilitySummary(
                **{
                    **asdict(best),
                    "agent_availability": best.agent_availability,
                    "registry_record_id": other.registry_record_id,
                    "projection_status": best.projection_status or "in_sync",
                }
            )
        winners[key] = best
    return tuple(sorted(winners.values(), key=sort_key))


def sort_key(item: CapabilitySummary) -> tuple[Any, ...]:
    """Deterministic ordering so pagination and shadow parity are stable.

    Newest-updated first within a category, then by display name and id to
    break ties that timestamps alone leave ambiguous.
    """
    return (
        item.category,
        item.updated_at is None,
        _descending(item.updated_at),
        item.display_name.lower(),
        item.id,
    )


def _descending(value: Optional[str]) -> str:
    """Invert an ISO timestamp for descending sort inside an ascending key."""
    if not value:
        return ""
    # ISO-8601 is lexicographically ordered; complement each character's code
    # point against a fixed ceiling to reverse it without a reverse= flag
    # (which would flip every other component of the tuple too).
    return "".join(chr(0x7E - min(ord(c), 0x7D)) for c in value)


__all__ = [
    "CAPABILITY_CLASSES",
    "CATEGORIES",
    "CapabilitySummary",
    "CategoryPage",
    "PROJECTION_STATUSES",
    "SOURCES",
    "SourceError",
    "merge_summaries",
    "sort_key",
]
