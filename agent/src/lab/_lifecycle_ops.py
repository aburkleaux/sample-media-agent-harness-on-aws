"""Working-copy lifecycle operations (asset-lifecycle-redesign Stage B+).

Implements the publish / edit / discard semantics agreed with the user:

    * **edit_asset(asset_id, from_version=None)** — fork into a working
      copy. Refuses if a working copy already exists. Default fork
      source = the version flagged ``current``; pass ``from_version``
      to fork from a specific historical version.

    * **publish_asset(asset_id, scope, ...)** — turn the current working
      copy into the next immutable version. Delegates to
      :func:`promote_asset` for the heavy lifting (writes the version
      row, flips ``current``, updates lastPublishedAs). After the
      transaction succeeds, removes the working copy from the OWNED
      partition so the canonical state is the published version.

    * **discard_changes(asset_id)** — drop an unpublished working copy.
      The latest published version is unaffected.

This module sits one layer above the per-category CRUDs. The CRUDs still
own ``create`` / ``read`` / ``update`` / ``delete`` for the working
copy. Lifecycle transitions are routed here so the rules are in one
place.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
    make_sk_owned,
)
from .context import LabRequestContext
from .errors import NotFoundOrForbidden
from .models import AssetCommon, Scope
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# edit_asset — fork a version into a working copy
# ---------------------------------------------------------------------------


async def edit_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    asset_id: str,
    from_version: Optional[int] = None,
) -> AssetCommon:
    """Fork an immutable version into a working copy in the owner's OWNED partition.

    Behavior:

      1. If a working copy already exists at ``USER#<actor>#OWNED /
         <category>#<asset_id>`` → raise ``WorkingCopyExists`` so the
         caller can prompt the user to discard or publish first.
      2. Look up the source version. If ``from_version`` is None,
         picks the version with ``current=True``. If no version has
         ``current=True``, picks the highest non-deprecated version.
      3. Build a new working copy row from the version's metadata,
         setting ``forked_from = (asset_id, from_version)``.
      4. Copy the version's S3 prefix to ``users/<actor>/owned/<category>/<asset_id>/``.
      5. Write the working copy row.

    Returns the new working copy AssetCommon.
    """
    _reject_flat_category(category, "edit_asset")

    # 1. Check for existing working copy.
    owned_pk = make_pk_user_owned(ctx.actor_id)
    owned_sk = make_sk_owned(category, asset_id)
    existing = await asset_index.get(owned_pk, owned_sk)
    if existing is not None:
        raise WorkingCopyExists(
            f"Working copy already exists for {category}#{asset_id}. "
            "Discard or publish it before forking another version."
        )

    # 2. Resolve source version.
    lineage = await asset_index.query_lineage(asset_id)
    if not lineage:
        raise NotFoundOrForbidden(
            f"No published versions found for {category}#{asset_id}."
        )

    if from_version is not None:
        candidates = [r for r in lineage if r.version == from_version]
        if not candidates:
            raise NotFoundOrForbidden(
                f"Version v{from_version} of {category}#{asset_id} does not exist."
            )
        source = candidates[0]
    else:
        # Prefer the row marked current; fall back to the highest non-deprecated.
        current_rows = [r for r in lineage if r.current]
        if current_rows:
            source = current_rows[0]
        else:
            non_deprecated = [r for r in lineage if not r.deprecated]
            if not non_deprecated:
                raise NotFoundOrForbidden(
                    f"All versions of {category}#{asset_id} are deprecated."
                )
            source = max(non_deprecated, key=lambda r: r.version or 0)

    if source.version is None:
        raise ValueError(
            "Source row has version=None; cannot fork a working copy "
            "from another working copy"
        )

    source_version: int = source.version
    timestamp_ms = int(time.time() * 1000)

    # 3. Build the working-copy row.
    working_copy = _clone_to_working_copy(
        source=source,
        actor_id=ctx.actor_id,
        category=category,
        asset_id=asset_id,
        forked_from_version=source_version,
        timestamp_ms=timestamp_ms,
    )

    # 4. Copy S3 bytes (best effort — owners may have versions with no S3 content).
    try:
        await s3.copy_prefix_absolute(source.s3_prefix, working_copy.s3_prefix)
    except Exception as exc:
        # Don't fail the fork if S3 has nothing to copy; the working
        # copy row is the source of truth for what exists.
        logger.warning(
            "edit_asset: S3 copy %s -> %s failed (continuing): %s",
            source.s3_prefix, working_copy.s3_prefix, exc,
        )

    # 5. Write the working copy row.
    await asset_index.put(working_copy, owner_user_id=ctx.actor_id)

    logger.info(
        "edit_asset: forked %s#%s from v%d into working copy",
        category, asset_id, source_version,
    )
    return working_copy


# ---------------------------------------------------------------------------
# publish_asset — promote working copy to a new version
# ---------------------------------------------------------------------------


async def publish_asset(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    asset_id: str,
    scope: Scope = "private",
    group_ids: Optional[list[str]] = None,
    org_id: Optional[str] = None,
) -> dict:
    """Publish the working copy as the next immutable version, then remove the working copy.

    Wraps :func:`promote_asset` to write the version row + flip current
    + update ``lastPublishedAs``. Once the canonical-op transaction
    commits, this function then deletes the working copy row from
    OWNED. After publish, the canonical state is the new version; the
    user must call :func:`edit_asset` again to make further changes.

    For "first publish of a brand-new skill" (no prior versions), the
    asset_id is reused as the published_id so the working copy and
    version live in the same lineage.

    Returns a dict with keys ``assetId``, ``version``, ``s3Prefix``.
    """
    from ._canonical_ops import promote_asset

    # Sanity: working copy must exist.
    owned_pk = make_pk_user_owned(ctx.actor_id)
    owned_sk = make_sk_owned(category, asset_id)
    working = await asset_index.get(owned_pk, owned_sk)
    if working is None:
        raise NotFoundOrForbidden(
            f"No working copy found for {category}#{asset_id}; "
            "nothing to publish."
        )

    # asset-lifecycle-redesign anchors every version's lineage at the
    # working-copy's asset_id. On first publish there are no existing
    # versions; we still pass asset_id as published_id so the new
    # lineage is queryable by the working-copy id forever after.
    published_id_arg: Optional[str] = asset_id

    # promote_asset reads the working copy via the same (PK, SK), uses
    # it as the source, writes the version, and updates the working
    # copy's lastPublishedAs. It does NOT delete the working copy.
    result = await promote_asset(
        ctx=ctx,
        asset_index=asset_index,
        s3=s3,
        category=category,
        draft_id=asset_id,  # working-copy id == lineage id
        scope=scope,
        group_ids=group_ids,
        published_id=published_id_arg,
        org_id=org_id,
    )

    # Remove the working copy now that the version is durable.
    try:
        await _delete_owned_row_and_prefix(
            asset_index=asset_index,
            s3=s3,
            actor_id=ctx.actor_id,
            category=category,
            asset_id=asset_id,
            s3_prefix=working.s3_prefix,
        )
    except Exception as exc:
        # Don't fail the publish if cleanup hiccups — the version
        # exists and is the canonical state. Log so a janitor can
        # follow up.
        logger.warning(
            "publish_asset: post-publish working-copy cleanup failed "
            "for %s#%s: %s",
            category, asset_id, exc,
        )

    return {
        "assetId": result.published_id,
        "version": result.version,
        "s3Prefix": result.s3_prefix,
    }


# ---------------------------------------------------------------------------
# discard_changes — drop an unpublished working copy
# ---------------------------------------------------------------------------


async def discard_changes(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    category: str,
    asset_id: str,
) -> dict:
    """Delete the working copy. The latest published version is unaffected.

    Idempotent: returns ``{"discarded": False}`` if no working copy
    exists.
    """
    # A flat category has no version to fall back to, so "discard the working
    # copy" is an outright delete of the only copy that exists — and this path
    # does no reference scan, which would have made it a silent bypass of the
    # gate on ``ToolCollectionCRUD.delete``.
    _reject_flat_category(category, "discard_changes")

    owned_pk = make_pk_user_owned(ctx.actor_id)
    owned_sk = make_sk_owned(category, asset_id)
    working = await asset_index.get(owned_pk, owned_sk)
    if working is None:
        return {"discarded": False}

    await _delete_owned_row_and_prefix(
        asset_index=asset_index,
        s3=s3,
        actor_id=ctx.actor_id,
        category=category,
        asset_id=asset_id,
        s3_prefix=working.s3_prefix,
    )

    return {"discarded": True, "assetId": asset_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reject_flat_category(category: str, operation: str) -> None:
    """Refuse a versioned-lifecycle operation on a flat (unversioned) category.

    ``tool-collection`` is flat: no version sub-tier, no published form. Every
    transition here presupposes a version to fork from, publish to, or fall back
    to, so running one against a collection could only produce a confusing
    not-found or an unintended delete. Mirrors
    :data:`lab._canonical_ops._VERSIONED_CATEGORIES`, which ``promote_asset``
    enforces for the publish path.
    """
    # Imported here, not at module scope, for the same reason ``publish_asset``
    # imports ``promote_asset`` locally.
    from ._canonical_ops import _VERSIONED_CATEGORIES

    if category not in _VERSIONED_CATEGORIES:
        raise ValueError(
            f"{operation} only supports versioned categories "
            f"({sorted(_VERSIONED_CATEGORIES)}); got {category!r}. "
            "Tool collections are flat — they have no versions."
        )


def _clone_to_working_copy(
    *,
    source: AssetCommon,
    actor_id: str,
    category: str,
    asset_id: str,
    forked_from_version: int,
    timestamp_ms: int,
) -> AssetCommon:
    """Construct a new working-copy AssetCommon from a source version.

    Uses the source's class so the category-specific fields
    (compatible_skills, tool_refs, steps) carry over. Resets the
    fields that don't apply to a working copy.
    """
    cls = type(source)
    # Working copies use the existing drafts/ path scheme so path_validator
    # accepts them without changes. ("drafts" = working copies in v2.)
    s3_prefix = (
        f"users/{actor_id}/drafts/{_pluralize_category(category)}/{asset_id}/"
    )

    # Fields shared by every AssetCommon subclass.
    base_kwargs = dict(
        asset_id=asset_id,
        category=source.category,
        name=source.name,
        # Carried explicitly: this dict is field-by-field, so a new
        # AssetCommon field is dropped unless named here. If ``description``
        # were dropped, editing a published Custom Agent would load an empty
        # description into the editor and the next publish would snapshot the
        # blank — silently erasing it with no error. (Same failure mode as the
        # ``model`` / ``tool_collections`` / ``external_access_enabled``
        # carries below; the three sibling builders in ``_canonical_ops`` name
        # it too.)
        description=source.description,
        status="draft",  # legacy field kept derived; working copy = draft
        scope="private",
        s3_prefix=s3_prefix,
        created_at=timestamp_ms,
        updated_at=timestamp_ms,
        version=None,
        deprecated=False,
        publish_policy=None,
        scope_history=[],
        current=False,
        promoted_from=None,
        last_published_as=None,
        cloned_from=(asset_id, forked_from_version),
    )

    # Carry category-specific fields forward.
    extra: dict[str, Any] = {}
    if hasattr(source, "compatible_skills"):
        extra["compatible_skills"] = list(source.compatible_skills)
    # Custom Agents (Req 8.4/8.6/8.7): forking a published version into a
    # working copy must carry the FULL config, not just compatible_skills.
    # If the extended fields (model, tool_collections, integration_config)
    # were dropped here, editing a published Custom Agent would silently
    # lose the model / tool bindings / integration (OIDC) settings and the
    # next publish would snapshot a blank config — so the integration
    # settings would no longer travel with the version they were published
    # alongside. ``model`` is an immutable str; the list is copied;
    # ``integration_config`` is carried by reference (working copies are not
    # mutated in place — the CRUD ``update`` path replaces the field).
    if hasattr(source, "model"):
        extra["model"] = source.model
    if hasattr(source, "tool_collections"):
        extra["tool_collections"] = list(source.tool_collections)
    # The one extended field that must NOT travel. `tool_collection_snapshots`
    # pins an immutable version's tool set; a working copy has to track the live
    # collections the Owner is editing, and its next publish re-resolves them.
    # Set explicitly rather than left to the dataclass default so this reads as a
    # decision — the `hasattr` carries above are otherwise the pattern here, and
    # adding it to them would silently freeze every fork.
    if hasattr(source, "tool_collection_snapshots"):
        extra["tool_collection_snapshots"] = None
    if hasattr(source, "integration_config"):
        extra["integration_config"] = source.integration_config
    # Req 16: forking a published version into a working copy must carry the
    # external-access gate forward too. If it were dropped here it would reset
    # to the ``True`` default, and the next publish would snapshot an ENABLED
    # gate — silently re-granting access the Owner had revoked. Carrying it
    # keeps the revocation record stable across the edit/publish cycle.
    if hasattr(source, "external_access_enabled"):
        extra["external_access_enabled"] = source.external_access_enabled
    if hasattr(source, "tool_refs"):
        extra["tool_refs"] = list(source.tool_refs)
    if hasattr(source, "steps"):
        extra["steps"] = list(source.steps)

    return cls(**base_kwargs, **extra)


def _pluralize_category(category: str) -> str:
    """Match the s3_prefix path conventions used elsewhere in the codebase."""
    return {
        "skill": "skills",
        "specialty-agent": "specialty-agents",
        "tool-collection": "tool-collections",
        "workflow": "workflows",
    }.get(category, category)


async def _delete_owned_row_and_prefix(
    *,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    actor_id: str,
    category: str,
    asset_id: str,
    s3_prefix: str,
) -> None:
    """Hard-delete the working copy DDB row + S3 prefix."""
    owned_pk = make_pk_user_owned(actor_id)
    owned_sk = make_sk_owned(category, asset_id)
    await asset_index.transact_write([
        {
            "Delete": {
                "TableName": asset_index.table_name,
                "Key": {"PK": owned_pk, "SK": owned_sk},
                "ConditionExpression": "attribute_exists(PK)",
            }
        }
    ])
    try:
        await s3.delete_prefix_absolute(s3_prefix)
    except Exception as exc:
        logger.warning(
            "_delete_owned_row_and_prefix: S3 prefix delete %s failed: %s",
            s3_prefix, exc,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkingCopyExists(Exception):
    """Raised by edit_asset when a working copy already exists.

    Caller surfaces this to the user so they can choose to discard
    or publish before forking another version.
    """
