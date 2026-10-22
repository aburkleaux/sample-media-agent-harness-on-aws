"""Transactional outbox for AWS Agent Registry projection (R7).

Publishing to the harness-shared discovery catalog cannot be a second write in
the request path: a Registry call that fails after the canonical DynamoDB
transaction commits would leave the two permanently disagreeing, and one that
fails *before* would make a Registry outage look like a publish failure. So
lifecycle code appends an ``OUTBOX#`` row to the **same** ``TransactWriteItems``
call as the canonical mutation. Either both land or neither does.

The rows live in the AssetIndex table (single-table design) under a reserved
partition, outside the asset PK encoding, so listing or querying an asset
partition never returns one:

    PK = OUTBOX#<category>#<asset_id>
    SK = EVT#<occurredAt>#<random suffix>

A DynamoDB stream on the table feeds the projection Lambda, whose event-source
filter matches ``recordType = "outbox"`` — asset-row writes therefore never
invoke it.

Two deliberate properties:

* **Identifiers only.** An event carries no credentials, no package bytes, and
  no descriptor text. The projector *reloads* the canonical row and artifact
  manifest, so a stale or replayed event cannot publish stale data, and an
  ``org → private`` transition that happened after the event was written is
  caught by the projector's scope re-check (design "Outbox event").
* **Deterministic idempotency key.** ``<env>/<category>/<assetId>/v<n>/<eventType>/<revision>``
  is recomputable, which is what lets at-least-once delivery converge
  (Property 4).

Scope-change events are emitted in **both** directions. Narrowing looks like a
no-op from the shared catalog's perspective but is exactly when a record must
stop being discoverable, so the projector — not the writer — decides
eligibility.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

#: Reserved partition-key prefix. Outside the asset PK encoding documented in
#: :mod:`lab.asset_index` (``USER#``/``GROUP#``/``ORG#``/``MIGRATION#``).
OUTBOX_PK_PREFIX: str = "OUTBOX"

#: Discriminator the projector's DynamoDB-stream event-source filter matches.
#: Changing this string requires changing the CDK filter in the same commit or
#: projection silently stops.
OUTBOX_RECORD_TYPE: str = "outbox"

#: Event types (versioned by :data:`OUTBOX_EVENT_VERSION`).
EVENT_PUBLISHED: str = "CapabilityPublished"
EVENT_METADATA_UPDATED: str = "MetadataUpdated"
EVENT_SCOPE_CHANGED: str = "ScopeChanged"
EVENT_DEPRECATED: str = "Deprecated"
EVENT_REPLAY: str = "Replay"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_PUBLISHED,
        EVENT_METADATA_UPDATED,
        EVENT_SCOPE_CHANGED,
        EVENT_DEPRECATED,
        EVENT_REPLAY,
    }
)

#: Schema version of the outbox row itself. Bump when a field's meaning
#: changes so an in-flight queue of old events stays interpretable.
OUTBOX_EVENT_VERSION: int = 1

#: Canonical scopes that make an asset harness-shared (R3). Mirrors
#: ``capability.descriptors.ELIGIBLE_SCOPE`` — kept as a local constant so this
#: module stays importable by the lab package without a capability import.
ELIGIBLE_SCOPES: frozenset[str] = frozenset({"org"})

#: Categories with a Registry mapping (R4). ``hook`` is deliberately absent:
#: actor-private hooks stay management-only.
PROJECTABLE_CATEGORIES: frozenset[str] = frozenset(
    {"skill", "specialty-agent", "tool-collection", "workflow"}
)

#: Outbox rows are a delivery buffer, not a ledger. 30 days outlives any
#: realistic replay window while keeping the reserved partition from growing
#: without bound; the canonical rows remain the durable record.
OUTBOX_TTL_SECONDS: int = 30 * 86_400


def registry_eligible(scope: Optional[str], category: Optional[str] = None) -> bool:
    """Whether an asset's scope (and category) can enter the shared Registry.

    Note this is a *writer-side* pre-filter for whether an event is worth
    emitting at all. The projector re-checks eligibility against freshly
    reloaded canonical state before any Registry call, because scope can change
    between the two (Property 2).
    """
    if category is not None and category not in PROJECTABLE_CATEGORIES:
        return False
    return scope in ELIGIBLE_SCOPES


def should_emit(
    *,
    category: Optional[str],
    old_scope: Optional[str] = None,
    new_scope: Optional[str] = None,
) -> bool:
    """Whether a lifecycle change needs an outbox row.

    True when either side of the transition is eligible: widening to ``org``
    creates discoverability, narrowing away from it must remove it. A change
    that is eligible on neither side (``private → group``) is invisible to the
    shared catalog and emits nothing.
    """
    if category not in PROJECTABLE_CATEGORIES:
        return False
    return registry_eligible(old_scope) or registry_eligible(new_scope)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deploy_env() -> str:
    """Environment marker stamped on every event.

    The projector drops events whose ``environment`` does not match its own,
    so a table restored into the wrong account cannot project across
    environments (Property 7).
    """
    return (os.environ.get("DEPLOY_ENV") or "").strip() or "unknown"


def idempotency_key(
    *,
    environment: str,
    category: str,
    asset_id: str,
    version: int,
    event_type: str,
    canonical_revision: str,
) -> str:
    """``<env>/<category>/<assetId>/v<n>/<eventType>/<revision>``."""
    return "/".join(
        (
            environment,
            category,
            asset_id,
            f"v{version}",
            event_type,
            canonical_revision,
        )
    )


def canonical_revision(item: dict[str, Any]) -> str:
    """A stable revision marker for the canonical row this event describes.

    Prefers an explicit package digest when the row carries one; otherwise the
    sort key, which for a versioned row is immutable and unique
    (``<category>#<assetId>#v<NNNNNNNNNN>``). Either way it changes when the
    projected content should, which is all the idempotency key needs.
    """
    for key in ("packageDigest", "manifestDigest"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    sort_key = item.get("SK")
    if isinstance(sort_key, str) and sort_key:
        return sort_key
    updated = item.get("updatedAt")
    return str(updated) if updated is not None else "unknown"


def build_outbox_item(
    *,
    event_type: str,
    category: str,
    asset_id: str,
    version: int,
    scope: Optional[str],
    canonical_revision: str,
    previous_scope: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    """Build the outbox DynamoDB item (identifiers only).

    Raises:
        ValueError: Unknown ``event_type``, or a non-positive ``version``.
            Both indicate a programming error at the call site, and emitting a
            malformed event would poison the projector rather than fail here.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown outbox event type: {event_type!r}")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError(f"version must be a positive int; got {version!r}")
    if not category or not asset_id:
        raise ValueError("category and asset_id are required")

    env = environment or deploy_env()
    occurred_at = _now_iso()
    item: dict[str, Any] = {
        "PK": f"{OUTBOX_PK_PREFIX}#{category}#{asset_id}",
        # The random suffix keeps two events emitted in the same microsecond
        # from colliding — a transaction cannot contain two writes to one key,
        # and a collision would cancel an otherwise-valid publish.
        "SK": f"EVT#{occurred_at}#{uuid.uuid4().hex[:8]}",
        "recordType": OUTBOX_RECORD_TYPE,
        "eventType": event_type,
        "eventVersion": OUTBOX_EVENT_VERSION,
        "environment": env,
        "category": category,
        "assetId": asset_id,
        "version": version,
        "scope": scope,
        "canonicalRevision": canonical_revision,
        "idempotencyKey": idempotency_key(
            environment=env,
            category=category,
            asset_id=asset_id,
            version=version,
            event_type=event_type,
            canonical_revision=canonical_revision,
        ),
        "occurredAt": occurred_at,
        "ttl": int(time.time()) + OUTBOX_TTL_SECONDS,
    }
    if previous_scope is not None:
        item["previousScope"] = previous_scope
    return item


def build_outbox_put(
    *,
    table_name: str,
    event_type: str,
    category: str,
    asset_id: str,
    version: int,
    scope: Optional[str],
    canonical_revision: str,
    previous_scope: Optional[str] = None,
    environment: Optional[str] = None,
) -> dict[str, Any]:
    """A ``TransactWriteItems`` ``Put`` entry for one outbox event.

    Append the result to the ``operations`` list a canonical operation already
    builds, *before* ``AssetIndex.transact_write``. No condition expression:
    the sort key is unique per event, and a conditional check here could
    cancel the canonical mutation it rides with — the projector is idempotent,
    so a duplicate event is cheaper than a failed publish.

    DynamoDB caps a transaction at 100 actions; the canonical operations use
    at most three, so one more entry is never the constraint.
    """
    return {
        "Put": {
            "TableName": table_name,
            "Item": build_outbox_item(
                event_type=event_type,
                category=category,
                asset_id=asset_id,
                version=version,
                scope=scope,
                canonical_revision=canonical_revision,
                previous_scope=previous_scope,
                environment=environment,
            ),
        }
    }


__all__ = [
    "ELIGIBLE_SCOPES",
    "EVENT_DEPRECATED",
    "EVENT_METADATA_UPDATED",
    "EVENT_PUBLISHED",
    "EVENT_REPLAY",
    "EVENT_SCOPE_CHANGED",
    "EVENT_TYPES",
    "OUTBOX_EVENT_VERSION",
    "OUTBOX_PK_PREFIX",
    "OUTBOX_RECORD_TYPE",
    "OUTBOX_TTL_SECONDS",
    "PROJECTABLE_CATEGORIES",
    "build_outbox_item",
    "build_outbox_put",
    "canonical_revision",
    "deploy_env",
    "idempotency_key",
    "registry_eligible",
    "should_emit",
]
