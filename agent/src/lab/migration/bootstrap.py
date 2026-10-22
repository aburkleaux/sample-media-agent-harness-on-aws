"""Core_Task_Agent bootstrap — Task 8.4.

The migration creates exactly one org-scoped specialty-agent —
``Core_Task_Agent`` — at v1 the first time it runs, and reuses the same
``publishedId`` on every subsequent run via the ``core-task-agent``
marker. Workflow refs that previously named the legacy
``agent:{kind:'core', ref:'task-agent'}`` are rewritten to point at this
agent's ``publishedId@v1`` (see :mod:`rewrite`).

Idempotency contract
--------------------
On a fresh run the bootstrap commits, in this order:

1. ``s3.put_object`` of the agent.json body at
   ``org/<admin_org>/published/specialty-agents/<pid>/v1/agent.json``.
2. ``AssetIndex.put`` of the corresponding Published_Record row with
   ``status="published"``, ``scope="org"``, ``version=1``,
   ``current=true``, ``publishPolicy.ownerIds=[admin_user]``, and a
   single initial scopeHistory entry ``{from: None, to: "org", ...}``.
3. ``marker.mark_completed("core-task-agent", {"publishedId": pid,
   "version": 1})``.

Step 1 precedes step 2 so a transient AssetIndex failure compensates by
deleting the orphan agent.json (matching the migration design's "S3
first, AssetIndex second, compensate on AssetIndex failure" rule used
by ``copy.migrate_one``). The marker is written ONLY after both
underlying writes succeed (Requirement 14.7); this lets a re-run
distinguish "fully committed" from "S3-only orphan" by marker presence.

Dry-run
-------
``dry_run=True`` short-circuits at step 1: a fresh ``publishedId`` is
still generated (so the rewrite stage in :mod:`rewrite` and the report
in :mod:`report` have a stable target name in the dry-run plan) but
neither S3 nor AssetIndex is written and the marker is not persisted.

Requirements: 14.4
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models import PublishPolicy, ScopeHistoryEntry, SpecialtyAgent
from . import marker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Marker key that gates the bootstrap. Lives under the reserved
#: ``MIGRATION#MARKER`` partition (see :class:`AssetIndex`); the value
#: payload carries ``{"publishedId": ..., "version": 1}`` so resume
#: runs can return the existing id without having to re-query the
#: lineage.
_CORE_TASK_AGENT_MARKER_KEY: str = "core-task-agent"

#: AssetIndex category for the bootstrapped row. Constant so a typo
#: cannot drift it away from the rest of the platform's specialty
#: agent code path.
_CATEGORY: str = "specialty-agent"

#: Display name on the bootstrapped row. Surfaced in lists and the
#: workflow rewriter does not care about this — only the
#: ``publishedId`` it produces matters — but a human-friendly name
#: keeps the AssetIndex partition browsable.
_CORE_TASK_AGENT_NAME: str = "Core Task Agent"

#: Description embedded in the agent.json body. Kept short; the body
#: only needs to round-trip through ``from_index_item`` later, and the
#: AssetIndex row is the source of truth for everything else.
_CORE_TASK_AGENT_DESCRIPTION: str = (
    "Platform's built-in Task Agent runtime, exposed as an org-scoped "
    "specialty agent during the legacy-skill migration so workflow "
    "refs that previously named agent:{kind:'core', ref:'task-agent'} "
    "can be rewritten to a stable Asset_Ref."
)


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 form (with ``Z``).

    Matches the convention used by :func:`lab._canonical_ops._now_iso`
    so the scopeHistory ``timestamp`` field on the bootstrapped row is
    indistinguishable from one produced by Promote.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    """Return the current epoch in milliseconds."""
    return int(time.time() * 1000)


def ensure_core_task_agent(
    *,
    asset_index: Any,
    s3: Any,
    admin_user: str,
    admin_org: str,
    bucket: str,
    dry_run: bool = False,
) -> str:
    """Idempotently create the org-scoped Core_Task_Agent.

    The function is the single seam between the migration driver and the
    bootstrap concern: discovery / copy / rewrite all assume the
    Core_Task_Agent's ``publishedId`` exists, and they get it from this
    function's return value. Resume runs return the same id without
    issuing any S3 or DDB writes; first runs commit S3 → AssetIndex →
    marker in that order.

    Args:
        asset_index: The :class:`AssetIndex` instance. Only
            ``get_marker``, ``put`` (async), and the ``put_marker`` path
            via :func:`marker.mark_completed` are used.
        s3: A boto3 ``s3`` client (or test fake) capable of
            ``put_object`` and ``delete_object``. The migration uses a
            raw client (not :class:`S3UserFilesTool`) because the
            destination prefix lives outside any actor's namespace.
        admin_user: User id placed in
            ``publishPolicy.ownerIds`` and as the initial
            scopeHistory ``actorId``.
        admin_org: Org id rendered into the destination prefix.
        bucket: Artifacts S3 bucket. The destination key is
            ``org/<admin_org>/published/specialty-agents/<pid>/v1/agent.json``.
        dry_run: When ``True``, skip the S3 / AssetIndex / marker writes
            and return a freshly-generated ``publishedId``.

    Returns:
        The Core_Task_Agent's ``publishedId``. Stable across resume
        runs; freshly generated on each dry-run invocation (the dry-run
        id is intentionally not persisted, so the ids printed by two
        consecutive dry-runs will differ).
    """
    # ------------------------------------------------------------------
    # 1. Marker check — resume short-circuit.
    #
    # We do NOT use ``marker.is_completed`` here because we need the
    # stored ``publishedId`` payload, not just presence. The marker
    # module's helper only returns a boolean; reading the raw payload
    # via ``asset_index.get_marker`` keeps the resume path one round
    # trip instead of two.
    # ------------------------------------------------------------------
    existing = asyncio.run(asset_index.get_marker(_CORE_TASK_AGENT_MARKER_KEY))
    if existing is not None:
        published_id = existing.get("publishedId")
        if not published_id:
            # A marker without a publishedId would be a bootstrap bug
            # (we always write the id alongside the marker on the
            # commit path). Surface it loudly rather than silently
            # generating a new one and creating a duplicate row.
            raise RuntimeError(
                "core-task-agent marker is present but missing "
                "'publishedId' payload; refusing to bootstrap a new "
                "agent (would orphan the existing one)"
            )
        logger.info(
            "bootstrap: resuming Core_Task_Agent publishedId=%s",
            published_id,
        )
        return str(published_id)

    # ------------------------------------------------------------------
    # 2. Generate the new publishedId and render the destination prefix.
    # ------------------------------------------------------------------
    published_id = uuid.uuid4().hex
    target_prefix = (
        f"org/{admin_org}/published/specialty-agents/{published_id}/v1/"
    )
    agent_key = f"{target_prefix}agent.json"

    body = {
        "name": _CORE_TASK_AGENT_NAME,
        "description": _CORE_TASK_AGENT_DESCRIPTION,
    }
    body_bytes = json.dumps(body, indent=2).encode("utf-8")

    # ------------------------------------------------------------------
    # 3. Dry-run: log intent, return without writes.
    #
    # The id is fresh per call (no marker, no persistence) so a
    # dry-run report printed today will not match the publishedId
    # produced by a real run tomorrow. That is acceptable: the report
    # is descriptive of *that run's* plan, not a forecast of future
    # ids.
    # ------------------------------------------------------------------
    if dry_run:
        logger.info(
            "bootstrap DRY: would write %s and put org-scoped "
            "specialty-agent row publishedId=%s version=1",
            agent_key,
            published_id,
        )
        return published_id

    # ------------------------------------------------------------------
    # 4. S3 first.
    #
    # We write the artifact before the AssetIndex row so a failed put
    # can be compensated by a single delete_object. Reversing the
    # order would let consumers see an AssetIndex row pointing at an
    # empty prefix.
    # ------------------------------------------------------------------
    s3.put_object(Bucket=bucket, Key=agent_key, Body=body_bytes)

    # ------------------------------------------------------------------
    # 5. Build and put the AssetIndex row.
    #
    # Shape matches what Promote produces for an org-scoped first
    # publish: status=published, scope=org, version=1, current=true,
    # publishPolicy.ownerIds=[admin_user], a single scopeHistory entry
    # ``{from: None, to: "org", actorId, timestamp}``. We construct a
    # proper :class:`SpecialtyAgent` (not a raw dict) so
    # :func:`AssetIndex.put` can run its prefix validation and so the
    # round-trip via :func:`from_index_item` recovers an identical
    # object.
    # ------------------------------------------------------------------
    timestamp_iso = _now_iso()
    timestamp_ms = _now_ms()

    initial_entry = ScopeHistoryEntry(
        from_scope=None,
        to_scope="org",
        actor_id=admin_user,
        timestamp=timestamp_iso,
        group_ids=None,
    )
    publish_policy = PublishPolicy(
        owner_ids=[admin_user],
        contributor_ids=[],
    )

    asset = SpecialtyAgent(
        asset_id=published_id,
        category=_CATEGORY,
        name=_CORE_TASK_AGENT_NAME,
        status="published",
        scope="org",
        s3_prefix=target_prefix,
        created_at=timestamp_ms,
        updated_at=timestamp_ms,
        version=1,
        publish_policy=publish_policy,
        scope_history=[initial_entry],
        current=True,
        promoted_from=None,
        last_published_as=None,
        cloned_from=None,
        compatible_skills=[],
    )

    try:
        asyncio.run(asset_index.put(asset, owner_org_id=admin_org))
    except Exception as err:
        # Compensating delete: best-effort. If the delete itself fails
        # we still re-raise the original AssetIndex error so the
        # operator sees the root cause; the orphan agent.json will be
        # picked up by a re-run (the marker is absent so the next
        # invocation will re-attempt the put under a *new* publishedId,
        # leaving the orphan key behind for the janitor).
        try:
            s3.delete_object(Bucket=bucket, Key=agent_key)
        except Exception as comp_err:  # noqa: BLE001
            logger.error(
                "bootstrap: compensating delete failed for orphan "
                "key=%s original=%r compensation=%r",
                agent_key,
                err,
                comp_err,
            )
        raise

    # ------------------------------------------------------------------
    # 6. Marker last.
    #
    # Per Requirement 14.7, the marker is committed only after both
    # underlying writes durable. ``mark_completed`` is the documented
    # seam for this; using it (rather than calling ``put_marker``
    # directly) keeps the bootstrap symmetric with the per-skill copy
    # path.
    # ------------------------------------------------------------------
    marker.mark_completed(
        asset_index,
        _CORE_TASK_AGENT_MARKER_KEY,
        {"publishedId": published_id, "version": 1},
    )

    logger.info(
        "bootstrap: created Core_Task_Agent publishedId=%s prefix=%s",
        published_id,
        target_prefix,
    )
    return published_id
