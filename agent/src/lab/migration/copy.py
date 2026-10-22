"""Per-skill migration — Task 8.5.

Migrates one legacy skill prefix (``s3://<bucket>/skills/<name>/``) into
the unified layout at
``s3://<bucket>/org/<adminOrg>/published/skills/<publishedId>/v1/``,
records the corresponding Published_Record row, and writes a
``legacy-skill::<key>`` marker so re-runs short-circuit on the resume
path.

The function mirrors :func:`lab.migration.bootstrap.ensure_core_task_agent`
in shape and ordering:

1. **Marker check first.** Reading the marker via ``get_marker``
   (rather than the boolean ``is_completed``) lets the resume path
   return the cached ``(publishedId, version)`` in one round trip.
2. **Generate ``publishedId`` and render** the destination prefix
   ``org/<adminOrg>/published/skills/<publishedId>/v1/``.
3. **Dry-run short-circuit.** Log intent, return ``(publishedId, 1)``;
   no S3 / DDB / marker writes (Requirement 14.2). The id is fresh per
   call (no marker, no persistence) — two consecutive dry-run reports
   will print different ids.
4. **Real run** commits in this order:

   - **S3 first**: copy every object under
     ``legacy_skill.legacy_key_prefix`` into ``target_prefix`` via
     ``copy_object``. Reversing this order would let consumers see an
     AssetIndex row pointing at an empty target prefix.
   - **AssetIndex second**: ``asset_index.put`` of a
     :class:`~lab.models.Skill` row with ``status="published"``,
     ``scope="org"``, ``version=1``, ``current=True``,
     ``publishPolicy.ownerIds=[admin_user]``, and a single initial
     ``scopeHistory`` entry ``{from: None, to: "org", ...}``. On failure
     the work is compensated with a best-effort
     ``ListObjectsV2`` / ``DeleteObjects`` sweep of the target prefix
     and the original error is re-raised; the marker is left absent so
     the next re-run can retry under a fresh ``publishedId``.
   - **Marker last**: ``marker.mark_completed`` with
     ``{"publishedId": ..., "version": 1}``. Per Requirement 14.7 the
     marker is committed only after both underlying writes are durable;
     this is what makes the resume path safe.

The legacy source prefix (``skills/<name>/``) is **never** read for
deletion or written to. The migration treats it as immutable — orphan
cleanup is an explicit follow-on (per design "Out of scope").

The function deliberately bypasses the per-category CRUD layer (and
therefore its authority gates) — the migration runs once with admin
credentials and writes via :func:`AssetIndex.put` directly (per
design's "Migration package" notes).

.. note::
   The design calls for a ``migrationMarker`` attribute on the
   AssetIndex row carrying the legacy source key, so the operator can
   audit which legacy prefix produced each migrated row. The
   :class:`~lab.models.Skill` dataclass does not yet expose that field
   (audit-only; not consumed by any read path); for now the audit
   trail is provided by the marker payload (``legacy-skill::<key> ->
   {publishedId, version}``) which carries the same association in
   the reserved ``MIGRATION#MARKER`` partition. Adding a typed
   ``Skill.migration_marker`` is a follow-on once the audit consumer
   exists.

Requirements: 14.1, 14.2, 14.5, 14.7
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models import PublishPolicy, ScopeHistoryEntry, Skill
from . import marker
from .discovery import LegacySkill

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 form (with ``Z``).

    Matches the convention used by ``_canonical_ops._now_iso`` so the
    migrated rows' ``scopeHistory`` timestamps are indistinguishable
    from those produced by an admin-driven Promote.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _now_ms() -> int:
    """Return the current epoch in milliseconds."""
    return int(time.time() * 1000)


def _copy_prefix(
    s3: Any,
    *,
    bucket: str,
    source_prefix: str,
    target_prefix: str,
) -> int:
    """Copy every object under ``source_prefix`` to ``target_prefix``.

    Paginates ``ListObjectsV2`` so very large legacy prefixes do not
    have to fit in memory, and issues one ``copy_object`` per source
    key. The destination key replaces the ``source_prefix`` portion
    of the source key with ``target_prefix`` so any nested suffixes
    are preserved.

    Returns the count of objects copied (used for the post-copy log
    line).
    """
    paginator = s3.get_paginator("list_objects_v2")
    copied = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=source_prefix):
        for entry in page.get("Contents", []) or []:
            src_key = entry.get("Key")
            if not isinstance(src_key, str):
                continue
            # The ``replace(..., 1)`` form is the simplest correct
            # rewrite when the prefix appears at offset 0 (which the
            # paginator guarantees because we filtered on it).
            dest_key = src_key.replace(source_prefix, target_prefix, 1)
            s3.copy_object(
                Bucket=bucket,
                Key=dest_key,
                CopySource={"Bucket": bucket, "Key": src_key},
            )
            copied += 1
    return copied


def _delete_prefix(s3: Any, *, bucket: str, prefix: str) -> int:
    """Best-effort delete of every object under ``prefix``.

    Used as the compensating action when the AssetIndex put fails
    after a successful S3 copy. Batches into 1000-key chunks (the
    ``DeleteObjects`` API limit). Returns the number of keys deleted.

    Any exception propagates so the caller can log it and still re-raise
    the *original* error — matching the bootstrap module's compensation
    contract.
    """
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents", []) or []:
            key = entry.get("Key")
            if isinstance(key, str):
                keys.append(key)

    if not keys:
        return 0

    batch_size = 1000
    deleted = 0
    for start in range(0, len(keys), batch_size):
        chunk = keys[start : start + batch_size]
        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in chunk]},
        )
        deleted += len(chunk)
    return deleted


def migrate_one(
    *,
    s3: Any,
    asset_index: Any,
    legacy_skill: LegacySkill,
    admin_user: str,
    admin_org: str,
    bucket: str,
    dry_run: bool = False,
) -> tuple[str, int]:
    """Migrate one legacy skill into the unified layout.

    Args:
        s3: A boto3 ``s3`` client (or test fake) supporting
            ``get_paginator("list_objects_v2")``, ``copy_object``,
            and ``delete_objects``. The migration uses a raw client
            (not :class:`S3UserFilesTool`) because the destination
            prefix lives outside any actor's namespace.
        asset_index: The :class:`AssetIndex` instance. ``get_marker``,
            ``put``, and ``put_marker`` (the last via
            :func:`marker.mark_completed`) are invoked through
            :func:`asyncio.run` to match the synchronous one-shot
            driver.
        legacy_skill: A :class:`LegacySkill` from
            :mod:`lab.migration.discovery`.
        admin_user: Publisher identity; populates
            ``publishPolicy.ownerIds`` and the initial scopeHistory
            ``actorId``.
        admin_org: Org id rendered into the destination prefix.
        bucket: Artifacts S3 bucket holding both the legacy and
            migrated trees.
        dry_run: When ``True``, no S3 / AssetIndex / marker writes.

    Returns:
        ``(publishedId, version)`` for the migrated row. ``version`` is
        always ``1`` — this one-shot migration does not extend a
        lineage.

    Raises:
        RuntimeError: When the marker payload exists but is malformed
            (missing ``publishedId`` / ``version``); also the wrapper
            error surfaced for any uncaught failure (Requirement 14.5),
            with ``__cause__`` carrying the underlying exception and
            the message naming ``legacy_skill.legacy_key_prefix``.
    """
    marker_key = f"legacy-skill::{legacy_skill.legacy_key_prefix}"

    # ------------------------------------------------------------------
    # 1. Marker check — resume short-circuit.
    #
    # We read the raw payload (not the boolean ``is_completed``) so
    # the resume path returns the cached ``(publishedId, version)`` in
    # one round trip. Mirrors ``bootstrap.ensure_core_task_agent``.
    # ------------------------------------------------------------------
    existing = asyncio.run(asset_index.get_marker(marker_key))
    if existing is not None:
        published_id = existing.get("publishedId")
        version = existing.get("version")
        if not published_id or version is None:
            # A marker without a publishedId / version is a migration
            # bug (we always write both alongside the marker on the
            # commit path). Surface it loudly rather than silently
            # generating a new id and creating a duplicate row that
            # would orphan the existing migration.
            raise RuntimeError(
                f"legacy-skill marker for {legacy_skill.legacy_key_prefix!r} "
                "is present but missing 'publishedId' / 'version' payload; "
                "refusing to migrate (would create a duplicate row)"
            )
        logger.info(
            "migrate_one: resume — legacy=%s already migrated to %s@v%s",
            legacy_skill.legacy_key_prefix,
            published_id,
            version,
        )
        return (str(published_id), int(version))

    # ------------------------------------------------------------------
    # 2. Generate the publishedId and render the destination prefix.
    # ------------------------------------------------------------------
    published_id = uuid.uuid4().hex
    target_prefix = (
        f"org/{admin_org}/published/skills/{published_id}/v1/"
    )

    # ------------------------------------------------------------------
    # 3. Dry-run: log intent, return without any writes.
    # ------------------------------------------------------------------
    if dry_run:
        logger.info(
            "migrate_one DRY: would copy %s -> %s and put org-scoped "
            "skill row publishedId=%s version=1",
            legacy_skill.legacy_key_prefix,
            target_prefix,
            published_id,
        )
        return (published_id, 1)

    # ------------------------------------------------------------------
    # 4a. S3 first — copy the legacy prefix to the target.
    #
    # We write the artifacts before the AssetIndex row so a failed put
    # can be compensated by deleting the destination prefix. Reversing
    # the order would let consumers see an AssetIndex row pointing at
    # an empty target prefix.
    # ------------------------------------------------------------------
    try:
        copied = _copy_prefix(
            s3,
            bucket=bucket,
            source_prefix=legacy_skill.legacy_key_prefix,
            target_prefix=target_prefix,
        )
        logger.info(
            "migrate_one: copied %d object(s) %s -> %s",
            copied,
            legacy_skill.legacy_key_prefix,
            target_prefix,
        )

        # ------------------------------------------------------------------
        # 4b. AssetIndex second — put the published row.
        #
        # Shape matches what Promote produces for an org-scoped first
        # publish: status=published, scope=org, version=1, current=true,
        # publishPolicy.ownerIds=[admin_user], a single scopeHistory
        # entry ``{from: None, to: "org", actorId, timestamp}``. We
        # construct a typed :class:`Skill` (not a raw dict) so
        # :func:`AssetIndex.put` runs its prefix validation.
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

        asset = Skill(
            asset_id=published_id,
            category="skill",
            name=legacy_skill.name,
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
        )

        try:
            asyncio.run(asset_index.put(asset, owner_org_id=admin_org))
        except Exception as err:
            # Compensating delete: best-effort. If the delete itself
            # fails we still re-raise the original AssetIndex error so
            # the operator sees the root cause; the orphan target
            # prefix is logged loudly so a janitor can reconcile it.
            try:
                _delete_prefix(s3, bucket=bucket, prefix=target_prefix)
            except Exception as comp_err:  # noqa: BLE001
                logger.error(
                    "migrate_one: compensating delete failed for "
                    "orphan prefix=%s original=%r compensation=%r",
                    target_prefix,
                    err,
                    comp_err,
                )
            raise

        # ------------------------------------------------------------------
        # 4c. Marker last — only after both underlying writes are durable.
        # ------------------------------------------------------------------
        marker.mark_completed(
            asset_index,
            marker_key,
            {"publishedId": published_id, "version": 1},
        )

        logger.info(
            "migrate_one: ok — legacy=%s -> %s@v1 (prefix=%s)",
            legacy_skill.legacy_key_prefix,
            published_id,
            target_prefix,
        )
        return (published_id, 1)

    except Exception as err:
        # Requirement 14.5: halt on any failure and surface the
        # offending source key. The original error is preserved as
        # ``__cause__`` so callers (and tests) can still inspect it
        # via ``excinfo.value.__cause__``.
        raise RuntimeError(
            f"lab.migration.copy.migrate_one halted for legacy source "
            f"{legacy_skill.legacy_key_prefix!r}: {err}"
        ) from err
