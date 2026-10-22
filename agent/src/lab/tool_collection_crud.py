"""ToolCollectionCRUD — CRUD operations for Tool_Collection assets in the MediaWorks Lab.

Tool collections are named, curated sets of references to tools exposed by the
shared AgentCore_Gateway. Each tool reference is a (name, version) pair that must
match a GatewayToolDescriptor from the current GatewayProjection at create/update time.

Tool collections live at the flat ``USER#<u>#TOOL_COLLECTIONS`` partition in
AssetIndex (no version sub-tier — lifecycle stage is carried on the row's
``status`` field). Their S3 prefix uses the design's flat
``tool-collections/<id>/`` form rendered by :func:`derive_record_prefix`.

Delete is a hard-delete: a single ``transact_write`` removes the AssetIndex
row, then ``delete_prefix`` removes the S3 artifacts. Cross-actor access
returns not_found (no existence disclosure).

The five canonical lifecycle operations (``promote``, ``share``,
``deprecate``, ``clone``, ``rollback``) are intentionally
``NotImplementedError`` per the design's "Out of scope: tool-collection
visibility tiers" note — they are exposed only for interface symmetry
with the other three category CRUDs.

Requirements: 4.3, 4.4, 4.5, 4.7
Properties: 5, 6
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from botocore.exceptions import ClientError

from .asset_index import (
    AssetIndex,
    make_pk_user_owned,
    make_sk_owned,
)
from .context import LabRequestContext
from .errors import StillReferencedError
from .gateway_browser import GatewayBrowser
from .models import ToolCollection, ToolRef
from .path_validator import derive_record_prefix
from .purge_scan import scan_tool_collection_references
from .s3_user_files_tool import S3UserFilesTool

if TYPE_CHECKING:
    from .models import Scope
    from .operation_results import Cloned, Deprecated, Promoted, RolledBack, Shared

logger = logging.getLogger(__name__)

# Constants
_CATEGORY = "tool-collection"
_COLLECTION_FILENAME = "collection.json"
# S3 layout. `collection.json` lives at the row's CANONICAL bucket-absolute prefix
# (`record.s3_prefix`, rendered by `derive_record_prefix`), written with `write_absolute`
# like skills, agents and workflows. It used to be written ACTOR-RELATIVE through the
# S3UserFilesTool, which prepends `{actor_id}/`, so the file landed at
# `{actor_id}/drafts/tool-collections/{id}/` while the row advertised a different prefix —
# anything that trusted `s3_prefix` (as every other lab category is read) found nothing.
# Reads fall back to that legacy location so collections written before the fix still
# resolve, and an update rewrites them at the canonical prefix.
_DRAFTS_PREFIX = "drafts/tool-collections"


def _canonical_collection_key(s3_prefix: str) -> str:
    return f"{s3_prefix.rstrip('/')}/{_COLLECTION_FILENAME}"


def _legacy_collection_path(asset_id: str) -> str:
    """The pre-fix actor-relative path (the S3 tool prepends `{actor_id}/`)."""
    return f"{_DRAFTS_PREFIX}/{asset_id}/{_COLLECTION_FILENAME}"


@dataclass
class ToolCollectionReadResult:
    """Result of reading a Tool_Collection."""

    asset_id: str
    name: str
    tool_refs: list[ToolRef]
    metadata: dict


@dataclass
class ToolCollectionCreateResult:
    """Result of creating a Tool_Collection."""

    asset_id: str
    s3_prefix: str


class ToolCollectionCRUD:
    """CRUD operations for Tool_Collection assets.

    Uses S3UserFilesTool for file operations, AssetIndex for metadata, and
    GatewayBrowser for validating tool references against the current
    GatewayProjection.

    All operations are scoped to the actor_id bound in the provided context.

    Args:
        s3: The S3UserFilesTool instance (already bound to actor_id).
        index: The AssetIndex single-table wrapper.
        gateway_browser: The GatewayBrowser instance for tool reference validation.
    """

    def __init__(
        self,
        s3: S3UserFilesTool,
        index: AssetIndex,
        gateway_browser: GatewayBrowser,
    ) -> None:
        self._s3 = s3
        self._index = index
        self._gateway_browser = gateway_browser

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_tool_refs(
        self, ctx: LabRequestContext, tool_refs: list[ToolRef]
    ) -> None:
        """Validate that each tool reference resolves against the current GatewayProjection.

        Requirement 4.4: If any tool reference cannot be matched to a
        GatewayToolDescriptor, reject the request and identify each unresolved ref.

        Built-in tools (those without ``___`` in the name) are always available
        and skip Gateway validation — this is a deliberate product decision
        and is intentionally preserved as-is.

        First attempts to use the cached projection (get_projection). If no
        projection is cached, triggers a browse() to populate it.

        Args:
            ctx: The request context (provides actor_id and session_id).
            tool_refs: List of ToolRef objects to validate.

        Raises:
            ValueError: If any tool reference does not resolve, with details
                identifying each unresolved reference.
        """
        if not tool_refs:
            return

        # Separate built-in tools from Gateway tools. Built-in tools
        # (file_read, shell, etc.) don't have '___' in their name and
        # are always available — no Gateway validation needed.
        gateway_refs = [ref for ref in tool_refs if "___" in ref.name]

        if not gateway_refs:
            # All tools are built-ins; nothing to verify against the Gateway.
            return

        # Get the current projection (cached or freshly populated).
        projection = self._gateway_browser.get_projection(ctx)
        if projection is None:
            browse_result = self._gateway_browser.browse(ctx)
            projection = browse_result.projection

        # Match gateway refs by NAME only. Gateway tools do not carry a real
        # version end to end: the catalog (mcp_manager) exposes no version, the
        # UI defaults it to "1.0" (lab-api.ts), and the browser projection also
        # fabricates a version. Matching on the (name, version) tuple therefore
        # only ever worked by that shared coincidence and broke outright once
        # the projection was sourced differently from the catalog (BUG-3). The
        # fully-qualified ``gateway___tool`` name is the stable, authoritative
        # identity, so resolve on that alone.
        available_names: set[str] = {d.name for d in projection.tools}

        # Check each Gateway tool ref against the projection (by name).
        unresolved: list[ToolRef] = []
        for ref in gateway_refs:
            if ref.name not in available_names:
                unresolved.append(ref)

        if unresolved:
            unresolved_descriptions = [
                f"({ref.name}, version={ref.version})" for ref in unresolved
            ]
            raise ValueError(
                "The following tool references do not resolve to any "
                "Gateway tool for the current actor: "
                f"{', '.join(unresolved_descriptions)}"
            )

    def _build_collection_json(self, name: str, tool_refs: list[ToolRef]) -> bytes:
        """Serialize a collection.json document from name and tool_refs."""
        doc = {
            "name": name,
            "tools": [
                {"name": ref.name, "version": ref.version}
                for ref in tool_refs
            ],
        }
        return json.dumps(doc, indent=2).encode("utf-8")

    async def _read_collection_json(self, s3_prefix: str, asset_id: str) -> bytes:
        """`collection.json` at the row's canonical prefix, else at the pre-fix location.

        Raises FileNotFoundError when neither exists (callers fall back to the row's refs).
        """
        try:
            return await self._s3.read_absolute(_canonical_collection_key(s3_prefix))
        except FileNotFoundError:
            return await self._s3.read(_legacy_collection_path(asset_id))

    def _parse_collection_json(self, data: bytes) -> tuple[str, list[ToolRef]]:
        """Parse a collection.json document into ``(name, tool_refs)``."""
        doc = json.loads(data.decode("utf-8"))
        name = doc.get("name", "")
        tools = doc.get("tools", [])
        tool_refs = [
            ToolRef(name=t["name"], version=t["version"])
            for t in tools
        ]
        return name, tool_refs

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self, ctx: LabRequestContext, name: str, tool_refs: list[ToolRef]
    ) -> ToolCollectionCreateResult:
        """Create a new Tool_Collection.

        Validates each tool reference against the current GatewayProjection,
        writes collection.json to S3, and records the row in AssetIndex
        under ``USER#<actor>#TOOL_COLLECTIONS`` with status ``draft``.

        Args:
            ctx: The request context (provides actor_id).
            name: User-supplied collection name.
            tool_refs: List of ToolRef objects (name, version pairs).

        Returns:
            ToolCollectionCreateResult with the assigned asset_id and s3_prefix.

        Raises:
            ValueError: If any tool reference does not resolve against the
                GatewayProjection.
            GatewayUnavailableError: If the Gateway is unreachable and no
                cached projection is available.
        """
        # Requirement 4.4: validate tool refs against Gateway projection.
        self._validate_tool_refs(ctx, tool_refs)

        # Assign unique asset_id.
        asset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Canonical S3 prefix recorded in the AssetIndex row. Tool
        # collections render to the flat `tool-collections/<id>/`
        # form per design grammar; the actor-scoped S3UserFilesTool
        # used below auto-prepends the actor_id when resolving its
        # relative path, so the artifact still lands under the
        # actor's namespace at `<actor_id>/drafts/tool-collections/<id>/`.
        s3_prefix = derive_record_prefix(
            scope="private",
            owner_user_id=ctx.actor_id,
            owner_group_id=None,
            owner_org_id=None,
            category=_CATEGORY,
            asset_id=asset_id,
            version=None,
            status="draft",
        )
        # Write collection.json to S3 first; on a transactional failure
        # later we would compensate by deleting the prefix, but at this
        # entry point there is no transactional writer to fail.
        collection_data = self._build_collection_json(name, tool_refs)
        await self._s3.write_absolute(_canonical_collection_key(s3_prefix), collection_data)

        # Record in AssetIndex (Requirement 4.3) as a typed
        # ToolCollection. AssetIndex.put computes
        # PK = USER#<actor>#TOOL_COLLECTIONS,
        # SK = tool-collection#<asset_id>, and validates the prefix.
        tc = ToolCollection(
            asset_id=asset_id,
            category=_CATEGORY,
            name=name,
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
            tool_refs=list(tool_refs) if tool_refs else [],
        )
        await self._index.put(tc, owner_user_id=ctx.actor_id)

        logger.info(
            "Created tool collection: asset_id=%s, name=%s, "
            "tool_count=%d, actor=%s",
            asset_id,
            name,
            len(tool_refs),
            ctx.actor_id,
        )

        return ToolCollectionCreateResult(asset_id=asset_id, s3_prefix=s3_prefix)

    async def read(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[ToolCollectionReadResult]:
        """Read a Tool_Collection by asset_id.

        Returns the collection name, tool references, and metadata.
        Returns None if the row does not exist or belongs to a different
        actor (no existence disclosure per Requirement 4.7).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.

        Returns:
            ToolCollectionReadResult if found and owned by this actor, else None.
        """
        # Requirement 4.7: cross-actor access returns not_found. The row
        # only exists under the requesting actor's flat tool-collections
        # partition.
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None:
            return None

        # Only return drafts (the only published lifecycle stage tool
        # collections support in this sprint). Anything else surfaces
        # as not-found.
        if record.status != "draft":
            return None

        # Read collection.json from S3 (canonical prefix, then the legacy location).
        try:
            collection_bytes = await self._read_collection_json(record.s3_prefix, asset_id)
            collection_name, tool_refs = self._parse_collection_json(
                collection_bytes
            )
        except FileNotFoundError:
            # collection.json missing — shouldn't happen for a valid row
            # but degrade gracefully so the caller still gets the index
            # metadata.
            collection_name = record.name
            tool_refs = list(getattr(record, "tool_refs", []))

        return ToolCollectionReadResult(
            asset_id=asset_id,
            name=collection_name,
            tool_refs=tool_refs,
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
            },
        )

    async def update(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        name: Optional[str] = None,
        tool_refs: Optional[list[ToolRef]] = None,
    ) -> Optional[dict]:
        """Update an existing Tool_Collection.

        Updates the collection name and/or tool references. Re-validates
        tool refs against the GatewayProjection if provided. The previous
        collection.json is overwritten only after validation succeeds
        (Requirement 4.5).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.
            name: Optional new collection name.
            tool_refs: Optional new list of ToolRef objects. If provided,
                each ref is re-validated against the GatewayProjection.

        Returns:
            Dict with updatedAt if successful, None if not found.

        Raises:
            ValueError: If any tool reference does not resolve against the
                GatewayProjection.
            GatewayUnavailableError: If the Gateway is unreachable and no
                cached projection is available.
        """
        # Verify ownership (Requirement 4.7).
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Requirement 4.4: re-validate tool refs if provided. Validation
        # runs BEFORE any S3 write so a malformed update leaves both
        # stores untouched.
        if tool_refs is not None:
            self._validate_tool_refs(ctx, tool_refs)

        # Determine effective name and tool_refs for the collection.json.
        effective_name = name if name is not None else record.name

        if tool_refs is None:
            # Read current collection.json to preserve existing refs.
            try:
                collection_bytes = await self._read_collection_json(record.s3_prefix, asset_id)
                _, current_tool_refs = self._parse_collection_json(
                    collection_bytes
                )
            except FileNotFoundError:
                current_tool_refs = list(getattr(record, "tool_refs", []))
            effective_tool_refs = current_tool_refs
        else:
            effective_tool_refs = tool_refs

        # Write updated collection.json to S3 at the canonical prefix (this also migrates a
        # collection written before the path fix).
        collection_data = self._build_collection_json(
            effective_name, effective_tool_refs
        )
        await self._s3.write_absolute(_canonical_collection_key(record.s3_prefix), collection_data)

        # Build the AssetIndex update expression. updatedAt is always
        # bumped; name and toolRefs are updated only when supplied.
        now = int(time.time() * 1000)
        update_parts: list[str] = ["updatedAt = :updatedAt"]
        attr_values: dict[str, object] = {":updatedAt": now}
        attr_names: dict[str, str] = {}
        if name is not None:
            update_parts.append("#name = :name")
            attr_values[":name"] = name
            # `name` is a DynamoDB reserved word; use a placeholder.
            attr_names["#name"] = "name"
        if tool_refs is not None:
            update_parts.append("toolRefs = :toolRefs")
            attr_values[":toolRefs"] = [
                {"name": ref.name, "version": ref.version}
                for ref in tool_refs
            ]

        update_kwargs: dict[str, object] = {
            "update_expression": "SET " + ", ".join(update_parts),
            "expression_attribute_values": attr_values,
        }
        if attr_names:
            update_kwargs["expression_attribute_names"] = attr_names

        try:
            await self._index.update(pk, sk, **update_kwargs)
        except ClientError as err:
            # If the row was concurrently deleted between the get above
            # and this update, surface as not-found rather than 5xx.
            if (
                err.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return None
            raise

        logger.info(
            "Updated tool collection: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return {"updatedAt": now}

    async def delete(
        self, ctx: LabRequestContext, asset_id: str, *, force: bool = False
    ) -> Optional[bool]:
        """Delete a Tool_Collection using a hard-delete in two phases.

        Phase 0: refuse if a specialty agent still selects this collection,
        unless ``force``. A flat collection has no lineage and therefore no
        purge pre-flight to warn through, so this delete has always been the
        irreversible operation — and until this gate existed it had no
        referential-integrity check at all. Deleting a referenced collection
        left the agent resolving it to nothing: the invoke still succeeded, in
        persona, with the tools silently absent (``task_agent`` appends an
        unresolvable ``{"id": cid}`` marker and drops it with a log line the
        caller never sees). Refusing is the cheap fix; the scan is a listing
        plus a field check because ``toolCollections`` lives on the AssetIndex
        row.

        Phase 1: Hard-delete the AssetIndex row via ``transact_write``
        with an ``attribute_exists(PK)`` precondition so a missing row
        (or one owned by a different actor) surfaces as not-found
        rather than a 5xx.
        Phase 2: ``delete_prefix`` the S3 artifacts under the tool
        collection's draft prefix.

        Returns None if the row does not exist or belongs to a different
        actor (no existence disclosure per Requirement 4.7).

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.
            force: Skip the reference check. For cleaning up after the
                referencing agents are gone, or when the caller has decided the
                references are stale. The scan can only see what this actor can
                see, so it is advice with teeth, not proof.

        Returns:
            True if successfully deleted, None if not found.

        Raises:
            StillReferencedError: A visible specialty agent still names this
                collection and ``force`` is False.
        """
        # Phase 0: reference gate. Ordered before the row delete so a refusal
        # changes nothing; a not-found row is left to Phase 1 to report so the
        # gate cannot become a back door for probing other actors' ids.
        if not force:
            hits, partial = await scan_tool_collection_references(
                ctx=ctx,
                asset_index=self._index,
                collection_id=asset_id,
            )
            if hits:
                raise StillReferencedError(
                    category=_CATEGORY,
                    asset_id=asset_id,
                    hits=hits,
                    partial=partial,
                )
            if partial:
                # No hits, but the answer is known to be incomplete. Allowing the
                # delete is deliberate — blocking on `partial` would make every
                # collection undeletable, since a LabRequestContext usually
                # carries no org id — but it must not read as "nothing
                # referenced it".
                logger.warning(
                    "Tool collection delete: no references found but the scan "
                    "was incomplete; asset=%s actor=%s unscanned=%s",
                    asset_id,
                    ctx.actor_id,
                    list(partial),
                )

        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)

        # Phase 1: hard-delete the AssetIndex row. The condition ensures
        # we only return success when a row actually existed; a
        # concurrent delete or a cross-actor lookup surfaces as
        # not-found. The Key uses scalar Python values (not the typed
        # {"S": …} form) — the boto3 Table resource layer marshals
        # them, matching the shape used in skill_crud / specialty_crud.
        ops: list[dict[str, Any]] = [
            {
                "Delete": {
                    "TableName": self._index.table_name,
                    "Key": {"PK": pk, "SK": sk},
                    "ConditionExpression": "attribute_exists(PK)",
                }
            }
        ]
        try:
            await self._index.transact_write(ops)
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            # TransactionCanceledException with a ConditionalCheckFailed
            # reason is the canonical signal that the row didn't exist.
            # We also tolerate a bare ConditionalCheckFailedException in
            # case the underlying mock surfaces it directly.
            if code == "TransactionCanceledException":
                reasons = err.response.get("CancellationReasons", [])
                if any(
                    isinstance(r, dict)
                    and r.get("Code") == "ConditionalCheckFailed"
                    for r in reasons
                ):
                    return None
                raise
            if code == "ConditionalCheckFailedException":
                return None
            raise

        # Phase 2: delete all S3 objects of the collection, at BOTH locations: the canonical
        # prefix the row advertised (derived exactly as create() derived it — the row is
        # already gone) and the legacy actor-relative one written before the path fix.
        # Best-effort: an S3 failure leaves an orphan prefix but does not compromise
        # correctness — the index row is already gone.
        canonical_prefix = derive_record_prefix(
            scope="private",
            owner_user_id=ctx.actor_id,
            owner_group_id=None,
            owner_org_id=None,
            category=_CATEGORY,
            asset_id=asset_id,
            version=None,
            status="draft",
        )
        s3_prefix = f"{_DRAFTS_PREFIX}/{asset_id}/"
        try:
            await self._s3.delete_prefix_absolute(canonical_prefix)
            await self._s3.delete_prefix(s3_prefix)
        except Exception as s3_err:  # noqa: BLE001
            logger.warning(
                "Tool collection delete: index removed but S3 prefix "
                "cleanup failed; orphan_prefix=%s actor=%s asset=%s "
                "error=%r",
                s3_prefix,
                ctx.actor_id,
                asset_id,
                s3_err,
            )

        logger.info(
            "Deleted tool collection: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )

        return True

    async def list(self, ctx: LabRequestContext) -> list[ToolCollection]:
        """List all Tool_Collections owned by the requesting actor.

        Queries the actor's flat tool-collections partition with an SK
        prefix scoped to the tool-collection category, returning every
        typed :class:`ToolCollection`. Pagination is intentionally hidden;
        if a single user accumulates more than one page of tool
        collections in practice, expose ``last_evaluated_key`` here.

        Args:
            ctx: The request context (provides actor_id).

        Returns:
            List of typed :class:`ToolCollection` rows.
        """
        # Requirement 4.7: return only rows owned by requesting actor.
        pk = make_pk_user_owned(ctx.actor_id)
        items, _ = await self._index.query_pk(
            pk, sk_prefix=f"{_CATEGORY}#"
        )
        return items

    # ------------------------------------------------------------------
    # Canonical lifecycle operations — deferred per design "Out of scope:
    # tool-collection visibility tiers". Method bodies intentionally raise
    # NotImplementedError; the canonical helpers reject the
    # `tool-collection` category up-front. Do not implement here.
    # ------------------------------------------------------------------

    async def promote(
        self,
        ctx: LabRequestContext,
        draft_id: str,
        scope: "Scope",
        group_ids: Optional[list[str]] = None,
        published_id: Optional[str] = None,
        *,
        org_id: Optional[str] = None,
        publish_policy_owner_ids: Optional[list[str]] = None,
    ) -> "Promoted":
        """Promote a Tool_Collection (not implemented in this sprint).

        Tool collections live at the flat ``USER#<u>#TOOL_COLLECTIONS``
        partition with no version sub-tier; lifecycle stage is carried
        on the row itself via the ``status`` field. Versioned promotion
        therefore does not apply, and the design explicitly defers
        widening tool-collection visibility to a follow-on sprint
        (see "AssetIndex schema → Partition-key encoding").

        Method is exposed for interface symmetry with the other three
        category CRUDs and to satisfy the task plan's requirement that
        every CRUD module declare a ``promote`` method. Calling it
        raises :class:`NotImplementedError`; the canonical Promote
        helper rejects the ``tool-collection`` category up-front.
        """
        raise NotImplementedError(
            "tool-collection promote is out of scope: tool collections "
            "are flat (no version sub-tier) and remain private in this "
            "sprint; lifecycle is carried on the status field"
        )

    async def share(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
        new_scope: "Scope",
        group_ids: Optional[list[str]] = None,
        *,
        org_id: Optional[str] = None,
    ) -> "Shared":
        """Widen a Tool_Collection's scope (not implemented in this sprint).

        Tool collections are flat (no version sub-tier) and remain
        private in this sprint; the design defers widening
        tool-collection visibility to a follow-on. Method exists for
        interface symmetry with the other three category CRUDs.
        Calling it raises :class:`NotImplementedError`; the canonical
        Share helper rejects the ``tool-collection`` category up-front.
        """
        raise NotImplementedError(
            "tool-collection share is out of scope: tool collections "
            "are flat (no version sub-tier) and remain private in this "
            "sprint; widening tool-collection visibility is a follow-on"
        )

    async def deprecate(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Deprecated":
        """Deprecate a Tool_Collection (not implemented in this sprint).

        Tool collections are flat (no version sub-tier) and remain
        private in this sprint; the design defers
        deprecate-by-version semantics to a follow-on. Method exists
        for interface symmetry with the other three category CRUDs.
        Calling it raises :class:`NotImplementedError`; the canonical
        Deprecate helper rejects the ``tool-collection`` category
        up-front.
        """
        raise NotImplementedError(
            "tool-collection deprecate is out of scope: tool "
            "collections are flat (no version sub-tier) and remain "
            "private in this sprint; per-version deprecate is a "
            "follow-on"
        )

    async def clone(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Cloned":
        """Clone a Tool_Collection (not implemented in this sprint).

        Tool collections are flat (no version sub-tier) and remain
        private in this sprint; the design defers cloning a published
        tool collection into a new draft to a follow-on, since the
        flat partition has no ``(publishedId, version)`` to address.
        Method exists for interface symmetry with the other three
        category CRUDs. Calling it raises :class:`NotImplementedError`;
        the canonical Clone helper rejects the ``tool-collection``
        category up-front.
        """
        raise NotImplementedError(
            "tool-collection clone is out of scope: tool collections "
            "are flat (no version sub-tier) and remain private in this "
            "sprint; per-version clone is a follow-on"
        )

    async def rollback(
        self,
        ctx: LabRequestContext,
        published_id: str,
        to_version: Optional[int] = None,
    ) -> "RolledBack":
        """Roll back a Tool_Collection (not implemented in this sprint).

        Tool collections are flat (no version sub-tier) and remain
        private in this sprint; the design defers per-version
        rollback semantics to a follow-on, since the flat partition
        has no ``(publishedId, version)`` lineage to roll back to.
        Method exists for interface symmetry with the other three
        category CRUDs. Calling it raises :class:`NotImplementedError`;
        the canonical Rollback helper rejects the ``tool-collection``
        category up-front.
        """
        raise NotImplementedError(
            "tool-collection rollback is out of scope: tool collections "
            "are flat (no version sub-tier) and remain private in this "
            "sprint; per-version rollback is a follow-on"
        )
