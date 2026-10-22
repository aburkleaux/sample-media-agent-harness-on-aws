"""SkillCRUD — CRUD operations for Skill drafts in the MediaWorks Lab.

Implements the two-tier skill structure: a primary SKILL.md file (required)
plus zero or more supporting files under subdirectories within the skill prefix.

S3 key layout — objects live AT the prefix the AssetIndex row records::

    users/<actor>/drafts/skills/{assetId}/SKILL.md        — tier 1 (required)
    users/<actor>/drafts/skills/{assetId}/**/*            — tier 2 (optional)

This module used to write via the actor-scoped ``S3UserFilesTool.write``, which
prepends only ``{actor_id}/`` — so the bytes landed at
``{actor_id}/drafts/skills/<id>/SKILL.md`` while the row advertised
``users/<actor>/drafts/skills/<id>/``. That was believed harmless ("both paths
refer to the same logical record"), and it is not: consumers outside this module
trust ``record.s3_prefix`` literally. The Task Agent reads
``{row.s3_prefix}SKILL.md`` with a plain ``get_object``, so **every skill bound
to a Custom Agent silently failed to load** — logged once as
``external config: skill <id> unresolvable; skipping`` and then dropped, leaving
an agent that runs with fewer skills than the owner selected. ``promote`` copies
from the same recorded prefix, so published skills were empty too.

So all object access here goes through the ``*_absolute`` helpers at the
canonical prefix, matching :mod:`lab.specialty_crud` (whose ``persona.md`` loads
correctly for exactly this reason). Pre-existing objects at the old translated
key are still readable: ``read_absolute``/``delete_absolute``/``list_absolute``
resolve both conventions. They are NOT reachable by the runtime's direct
``get_object``, though — an old skill must be re-saved (or its objects copied)
before an agent can load it.

Delete is a hard delete: a single-op ``transact_write`` removes the index
row, then ``delete_prefix`` reclaims the S3 artifacts (Requirement 2.7).
The legacy ``status="archived"`` soft-delete is gone — the new schema
exposes only ``draft | published | deprecated``.

Cross-actor access returns ``None`` (no existence disclosure): a draft
owned by a different user lives under a different ``PK`` so the get
returns nothing.

Mirrors the patterns in :mod:`lab._canonical_ops` for transact_write op
shape and ``make_pk_*`` / ``make_sk_*`` helper usage.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9
Properties: 5, 6, 7, 8, 25
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from botocore.exceptions import ClientError

from .asset_index import AssetIndex, make_pk_user_owned, make_sk_owned
from .context import LabRequestContext
from .models import Skill
from .path_validator import validate_path
from .skill_package_validator import extract_skill_description, validate_skill_package
# The lab package's single implementation of published-version selection
# (explicit version → current → highest non-deprecated). Imported rather than
# re-copied; see ``read_published`` below.
from .specialty_crud import _pick_published_row
from .s3_user_files_tool import S3UserFilesTool

if TYPE_CHECKING:
    from .specialty_crud import SpecialtyCRUD
    from .models import Scope
    from .operation_results import Promoted, Shared, Deprecated, Cloned, RolledBack

logger = logging.getLogger(__name__)

# Constants
_CATEGORY = "skill"
_SKILL_MD_FILENAME = "SKILL.md"
_DRAFTS_PREFIX = "drafts/skills"


def _canonical_draft_prefix(actor_id: str, asset_id: str) -> str:
    """Render the canonical draft S3 prefix matching ``derive_record_prefix``.

    This is BOTH what ``AssetIndex.put`` validates ``s3_prefix`` against and
    where the objects are actually written — the two must not diverge, because
    every consumer outside this module (the Task Agent's skill loader, the
    canonical-op ``copy_prefix_absolute`` behind publish) addresses artifacts by
    the recorded prefix. See the module docstring for what divergence cost.
    """
    return f"users/{actor_id}/drafts/skills/{asset_id}/"


@dataclass
class SkillReadResult:
    """Result of reading a Skill draft."""

    asset_id: str
    name: str
    skill_md: str
    supporting_files: list[str]
    metadata: dict


@dataclass
class SkillCreateResult:
    """Result of creating a Skill draft."""

    asset_id: str
    s3_prefix: str


class SkillCRUD:
    """CRUD operations for Skill drafts.

    Uses S3UserFilesTool for file operations and AssetIndex for metadata.
    The AssetIndex is no longer actor-bound; this class derives the PK
    from ``ctx.actor_id`` on every call via :func:`make_pk_user_owned`.

    Args:
        s3: The S3UserFilesTool instance (already bound to actor_id).
        index: The single-table AssetIndex instance.
        specialty_crud: Optional SpecialtyCRUD instance for cascade
            operations. When provided, deleting a skill will cascade-
            remove it from all specialty compatible-skill lists
            (Requirement 3.4). The cascade call is wrapped in a
            try/except so a transitional failure in the specialty
            module's own migration does not break delete.
    """

    def __init__(
        self,
        s3: S3UserFilesTool,
        index: AssetIndex,
        specialty_crud: Optional["SpecialtyCRUD"] = None,
    ) -> None:
        self._s3 = s3
        self._index = index
        self._specialty_crud = specialty_crud

    async def create(
        self, ctx: LabRequestContext, name: str, skill_md: str
    ) -> SkillCreateResult:
        """Create a new Skill draft.

        Validates that ``skill_md`` is non-empty, assigns a UUID, writes
        SKILL.md to S3, and records the draft in the AssetIndex.

        Args:
            ctx: The request context (provides actor_id).
            name: User-supplied skill name.
            skill_md: The SKILL.md content (must be non-empty).

        Returns:
            SkillCreateResult with the assigned asset_id and canonical s3_prefix.

        Raises:
            ValueError: If skill_md is empty or whitespace-only.
        """
        # Requirement 2.3: reject empty SKILL.md body
        if not skill_md or not skill_md.strip():
            raise ValueError("SKILL.md body must not be empty")

        asset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)

        # Canonical record prefix: recorded on the row AND written to, so the
        # runtime's `{row.s3_prefix}SKILL.md` read resolves.
        s3_prefix = _canonical_draft_prefix(ctx.actor_id, asset_id)

        # Write SKILL.md to S3 first so a failed AssetIndex.put leaves a
        # stray file but no orphan index row (matches the create-then-record
        # ordering used by the canonical ops layer's S3-first pattern).
        await self._s3.write_absolute(
            f"{s3_prefix}{_SKILL_MD_FILENAME}", skill_md.encode("utf-8")
        )

        # Record in AssetIndex as a typed Skill draft.
        #
        # `description` is projected from the SKILL.md frontmatter onto the row. It is
        # the ONLY category whose description lives inside a file rather than in a form
        # field, so nothing used to copy it here — every inventory row therefore listed
        # a name and no statement of what the skill was FOR, and no amount of UI work
        # could fix that because the value was not in the record being listed. SKILL.md
        # in S3 stays the source of truth; this is a display projection of it, kept in
        # step by `update_skill_md`.
        skill = Skill(
            asset_id=asset_id,
            category=_CATEGORY,
            name=name,
            description=extract_skill_description(skill_md),
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
        )
        await self._index.put(skill, owner_user_id=ctx.actor_id)

        logger.info(
            "Created skill draft: asset_id=%s, name=%s, actor=%s",
            asset_id,
            name,
            ctx.actor_id,
        )

        return SkillCreateResult(asset_id=asset_id, s3_prefix=s3_prefix)

    async def import_package(
        self,
        ctx: LabRequestContext,
        name: str,
        skill_md: str,
        supporting_files: list[tuple[str, bytes]],
    ) -> SkillCreateResult:
        """Validate and atomically expose a complete uploaded Skill package.

        Every object is written below a fresh, unindexed prefix first. The
        AssetIndex row is created only after all writes succeed, so readers
        never observe a partial package. Any write or index failure triggers a
        best-effort prefix deletion before the original error is re-raised.
        """
        normalized_files = validate_skill_package(
            name=name,
            skill_md=skill_md,
            supporting_files=supporting_files,
        )

        existing = await self.list(ctx)
        if any(skill.name.casefold() == name.casefold() for skill in existing):
            raise ValueError(
                f"A Skill named '{name}' already exists. Choose a different name."
            )

        asset_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        s3_prefix = _canonical_draft_prefix(ctx.actor_id, asset_id)
        skill = Skill(
            asset_id=asset_id,
            category=_CATEGORY,
            name=name,
            # Same projection as `create`. An imported package has already been through
            # the strict validator, so this always finds one — extracted the same way
            # regardless, so the two paths cannot record it differently.
            description=extract_skill_description(skill_md),
            status="draft",
            scope="private",
            s3_prefix=s3_prefix,
            created_at=now,
            updated_at=now,
        )

        try:
            await self._s3.write_absolute(
                f"{s3_prefix}{_SKILL_MD_FILENAME}",
                skill_md.encode("utf-8"),
            )
            for path, content in normalized_files:
                await self._s3.write_absolute(f"{s3_prefix}{path}", content)
            await self._index.put(skill, owner_user_id=ctx.actor_id)
        except Exception:
            try:
                await self._s3.delete_prefix_absolute(s3_prefix)
            except Exception as cleanup_error:  # noqa: BLE001
                logger.warning(
                    "Skill package import compensation failed: actor=%s "
                    "asset=%s prefix=%s error=%r",
                    ctx.actor_id,
                    asset_id,
                    s3_prefix,
                    cleanup_error,
                )
            raise

        logger.info(
            "Imported skill package: asset_id=%s, name=%s, files=%d, actor=%s",
            asset_id,
            name,
            len(normalized_files) + 1,
            ctx.actor_id,
        )
        return SkillCreateResult(asset_id=asset_id, s3_prefix=s3_prefix)

    async def read(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[SkillReadResult]:
        """Read a Skill draft by asset_id.

        Returns the SKILL.md content, list of supporting file paths
        relative to the skill prefix, and draft metadata. Returns ``None``
        if the draft does not exist or belongs to a different actor.
        Cross-actor access returns ``None`` because the get is keyed on
        the requesting actor's drafts partition (Requirement 2.8).
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Read SKILL.md from the prefix the row records. ``read_absolute`` also
        # resolves objects left at the old translated key, so drafts written
        # before this module was corrected still open in the editor.
        list_prefix = record.s3_prefix
        skill_md_path = f"{list_prefix}{_SKILL_MD_FILENAME}"
        try:
            skill_md_bytes = await self._s3.read_absolute(skill_md_path)
            skill_md = skill_md_bytes.decode("utf-8")
        except FileNotFoundError:
            # SKILL.md missing — shouldn't happen for a valid draft, but
            # surface an empty body rather than crashing the read.
            skill_md = ""

        # List supporting files (everything under the skill prefix except SKILL.md).
        # ``list_absolute`` searches both conventions and normalizes what it
        # returns to CANONICAL keys, so relativizing against the recorded
        # prefix is correct for either storage layout.
        all_files = await self._s3.list_absolute(list_prefix)
        supporting_files: list[str] = []
        for file_path in all_files:
            if file_path == skill_md_path:
                continue
            if file_path.startswith(list_prefix):
                supporting_files.append(file_path[len(list_prefix):])

        return SkillReadResult(
            asset_id=record.asset_id,
            name=record.name,
            skill_md=skill_md,
            supporting_files=supporting_files,
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
            },
        )

    async def read_published(
        self,
        ctx: LabRequestContext,
        asset_id: str,
        version: Optional[int] = None,
    ) -> Optional[SkillReadResult]:
        """Read a *published* Skill version by published id.

        A sibling of :meth:`read` rather than a relaxation of it, for the same
        reason ``specialty_crud.read_published`` is: :meth:`read` is
        contractually draft-only — it keys the actor's OWNED partition and
        rejects any non-``draft`` status — and callers depend on that. Since
        publishing *consumes* the working copy, that partition holds nothing for
        a published lineage, so a published version is unreadable without this.

        Version selection is delegated to the one implementation in this
        package (explicit ``version`` → ``current`` → highest non-deprecated)
        rather than copied. There are already two copies of that rule
        (``task_agent._pick_lineage_row`` is deliberately separate to keep the
        Strands import graph out of Lab CRUD); a third would be one more thing
        to keep in lockstep.

        Owner scoping mirrors :meth:`read`: the resolved row's recorded prefix
        must belong to the requesting actor when it is user-scoped. A
        cross-actor or missing lineage surfaces as ``None`` — no existence
        disclosure.

        Returns:
            A :class:`SkillReadResult` whose ``metadata`` additionally carries
            ``version`` and ``current``, or ``None`` when no published version
            is resolvable.
        """
        if not asset_id:
            return None

        try:
            lineage = await self._index.query_lineage(asset_id)
        except Exception:  # noqa: BLE001 — a lookup failure is a read miss here
            logger.warning(
                "read_published: lineage query failed for asset_id=%s",
                asset_id,
                exc_info=True,
            )
            return None

        record = _pick_published_row(lineage, version)
        if record is None or getattr(record, "category", "") != _CATEGORY:
            return None

        prefix = getattr(record, "s3_prefix", "") or ""
        if prefix.startswith("users/") and not prefix.startswith(
            f"users/{ctx.actor_id}/"
        ):
            logger.warning(
                "read_published: owner mismatch for asset_id=%s "
                "(actor=%s, row prefix=%s)",
                asset_id,
                ctx.actor_id,
                prefix,
            )
            return None

        # Published rows are immutable, so a missing SKILL.md means the publish
        # was corrupt rather than mid-edit. Surface an empty body (as ``read``
        # does) instead of crashing the caller.
        skill_md = ""
        supporting_files: list[str] = []
        if prefix:
            skill_md_path = f"{prefix}{_SKILL_MD_FILENAME}"
            try:
                skill_md_bytes = await self._s3.read_absolute(skill_md_path)
                skill_md = skill_md_bytes.decode("utf-8")
            except FileNotFoundError:
                logger.warning(
                    "read_published: SKILL.md missing under %s", prefix
                )

            # Same relativization as ``read``: ``list_absolute`` normalizes to
            # canonical keys, so stripping the recorded prefix is correct.
            for file_path in await self._s3.list_absolute(prefix):
                if file_path == skill_md_path:
                    continue
                if file_path.startswith(prefix):
                    supporting_files.append(file_path[len(prefix):])

        return SkillReadResult(
            asset_id=record.asset_id,
            name=record.name,
            skill_md=skill_md,
            supporting_files=supporting_files,
            metadata={
                "category": record.category,
                "status": record.status,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "s3Prefix": record.s3_prefix,
                "version": getattr(record, "version", None),
                "current": bool(getattr(record, "current", False)),
            },
        )

    async def update_skill_md(
        self, ctx: LabRequestContext, asset_id: str, skill_md: str
    ) -> Optional[dict]:
        """Update the SKILL.md of an existing Skill draft.

        Overwrites the SKILL.md object and bumps ``updatedAt``. Supporting
        files are left unchanged.
        """
        if not skill_md or not skill_md.strip():
            raise ValueError("SKILL.md body must not be empty")

        # Verify ownership and existence (Requirement 2.8).
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Overwrite SKILL.md in S3 (Requirement 2.5). Writes at the recorded
        # prefix, which is also how an old translated-key draft gets migrated:
        # re-saving it puts the body where the runtime looks.
        await self._s3.write_absolute(
            f"{record.s3_prefix}{_SKILL_MD_FILENAME}", skill_md.encode("utf-8")
        )

        # Bump updatedAt in the index, and re-project the description.
        #
        # Both, not just the timestamp: the description is a projection of the body
        # that was just overwritten, so a save that refreshed only `updatedAt` would
        # leave the row describing the PREVIOUS version of the skill — a staleness
        # that is invisible until someone reads the list and the file and finds them
        # disagreeing. Removing the frontmatter description clears the row's, which is
        # why this is a SET of the extracted value rather than a conditional write.
        now = int(time.time() * 1000)
        description = extract_skill_description(skill_md)
        try:
            await self._index.update(
                pk,
                sk,
                update_expression="SET updatedAt = :now, description = :description",
                expression_attribute_values={
                    ":now": now,
                    ":description": description,
                },
                condition_expression="attribute_exists(PK)",
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Row vanished between the get and the update; treat as
                # not-found rather than surfacing the DDB error.
                return None
            raise

        logger.info(
            "Updated SKILL.md: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )
        return {"updatedAt": now}

    async def put_supporting_file(
        self, ctx: LabRequestContext, asset_id: str, path: str, content: bytes
    ) -> Optional[dict]:
        """Add or replace a supporting file in a Skill draft.

        Validates the path, writes under the skill prefix, and bumps
        ``updatedAt``. The SKILL.md is left unchanged.

        Args:
            ctx: The request context (provides actor_id).
            asset_id: The unique asset identifier.
            path: Relative path within the skill prefix (e.g.
                ``"references/guide.md"``). Must not be ``"SKILL.md"``
                (that's the primary tier).
            content: The file content as bytes.

        Returns:
            Dict with the S3 key if successful, ``None`` if draft not
            found.

        Raises:
            ValueError: If ``path`` is ``"SKILL.md"`` (use update_skill_md).
            PathTraversalError: If ``path`` fails validation.
        """
        if path == _SKILL_MD_FILENAME or path.rstrip("/") == _SKILL_MD_FILENAME:
            raise ValueError(
                "Cannot write SKILL.md via put_supporting_file; "
                "use update_skill_md instead"
            )

        validate_path(path)

        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Write supporting file under the skill prefix (Requirements 2.2, 2.6).
        file_path = f"{record.s3_prefix}{path}"
        await self._s3.write_absolute(file_path, content)

        # Bump updatedAt (Requirement 2.6).
        now = int(time.time() * 1000)
        try:
            await self._index.update(
                pk,
                sk,
                update_expression="SET updatedAt = :now",
                expression_attribute_values={":now": now},
                condition_expression="attribute_exists(PK)",
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return None
            raise

        logger.info(
            "Put supporting file: asset_id=%s, path=%s, actor=%s",
            asset_id,
            path,
            ctx.actor_id,
        )
        return {"key": file_path}

    async def delete_supporting_file(
        self, ctx: LabRequestContext, asset_id: str, path: str
    ) -> Optional[bool]:
        """Delete a single supporting file from a Skill draft.

        Removes the file from S3 and bumps ``updatedAt``. The SKILL.md
        is unchanged.
        """
        if path == _SKILL_MD_FILENAME or path.rstrip("/") == _SKILL_MD_FILENAME:
            raise ValueError(
                "Cannot delete SKILL.md via delete_supporting_file; "
                "use delete to remove the entire skill draft"
            )

        validate_path(path)

        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)
        record = await self._index.get(pk, sk)
        if record is None or record.status != "draft":
            return None

        # Delete the supporting file (Requirement 2.6). ``delete_absolute``
        # resolves the old translated key too, so this cannot leave an orphan
        # for a draft written before the path fix.
        file_path = f"{record.s3_prefix}{path}"
        await self._s3.delete_absolute(file_path)

        # Bump updatedAt (Requirement 2.6).
        now = int(time.time() * 1000)
        try:
            await self._index.update(
                pk,
                sk,
                update_expression="SET updatedAt = :now",
                expression_attribute_values={":now": now},
                condition_expression="attribute_exists(PK)",
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return None
            raise

        logger.info(
            "Deleted supporting file: asset_id=%s, path=%s, actor=%s",
            asset_id,
            path,
            ctx.actor_id,
        )
        return True

    async def delete(
        self, ctx: LabRequestContext, asset_id: str
    ) -> Optional[bool]:
        """Hard-delete a Skill draft.

        The new schema has no ``archived`` status (Requirement 2.7); the
        draft row is removed from the AssetIndex via a single-op
        ``transact_write`` with an ``attribute_exists(PK)`` precondition,
        then the S3 prefix is reclaimed via ``delete_prefix``. Cascade
        Requirement 3.4 is best-effort: failures in the specialty
        cascade do not roll back the delete (the cascade is wrapped in a
        try/except so the canonical ``delete`` semantics survive a
        transitionally-broken ``cascade_remove_skill``).

        Returns:
            ``True`` on success, ``None`` when the draft does not exist or
            is owned by a different actor (no existence disclosure).
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk = make_sk_owned(_CATEGORY, asset_id)

        # Phase 1: hard-delete the AssetIndex row. Mirrors the
        # transact_write op shape used in ``_canonical_ops.py`` (single
        # Delete entry with explicit TableName + Key).
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
            # A cancelled transaction whose only condition was
            # ``attribute_exists(PK)`` means the row is gone. Surface as
            # not-found rather than as an error.
            if code in (
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            ):
                return None
            raise

        # Phase 2: reclaim S3 prefix (Requirement 2.7). The canonical prefix,
        # matching what create/import wrote and what the row recorded;
        # ``delete_prefix_absolute`` also sweeps the old translated location so
        # deleting a pre-fix draft does not leave its objects behind.
        s3_prefix = _canonical_draft_prefix(ctx.actor_id, asset_id)
        try:
            await self._s3.delete_prefix_absolute(s3_prefix)
        except Exception as s3_err:  # noqa: BLE001
            # Index is already gone; an S3-prefix failure leaves an
            # orphan but does not compromise correctness. Log and
            # proceed — the janitor reclaims the prefix later.
            logger.warning(
                "Skill delete: index removed but S3 prefix cleanup failed; "
                "orphan_prefix=%s actor=%s asset=%s error=%r",
                s3_prefix,
                ctx.actor_id,
                asset_id,
                s3_err,
            )

        # Phase 3: cascade — remove the deleted skill from specialty
        # compatible-skill lists (Requirement 3.4, Property 9). The
        # cascade module may itself be in a transitional state during
        # the unified-platform rollout, so we tolerate failures here.
        if self._specialty_crud is not None:
            try:
                cascade_count = self._specialty_crud.cascade_remove_skill(
                    asset_id
                )
                if cascade_count > 0:
                    logger.info(
                        "Cascade removed skill '%s' from %d specialty agent(s), "
                        "actor=%s",
                        asset_id,
                        cascade_count,
                        ctx.actor_id,
                    )
            except Exception as cascade_err:  # noqa: BLE001
                logger.warning(
                    "Skill delete: cascade_remove_skill failed; "
                    "specialty references may be stale until the next "
                    "specialty migration. actor=%s asset=%s error=%r",
                    ctx.actor_id,
                    asset_id,
                    cascade_err,
                )

        logger.info(
            "Deleted skill draft: asset_id=%s, actor=%s",
            asset_id,
            ctx.actor_id,
        )
        return True

    async def list(self, ctx: LabRequestContext) -> list[Skill]:
        """List all Skill drafts owned by the requesting actor.

        Queries the actor's drafts partition with a category-prefixed SK
        filter so only ``skill`` drafts are returned. The result is a
        list of typed :class:`Skill` records; the service-layer handler
        converts them to dicts for the wire response.

        Pagination is not exposed today: the underlying ``query_pk``
        returns a single page (default 100 items). When per-actor draft
        counts grow past a page, the handler can switch to cursor-based
        listing without changing this method's signature.
        """
        pk = make_pk_user_owned(ctx.actor_id)
        sk_prefix = f"{_CATEGORY}#"
        items, _next = await self._index.query_pk(pk, sk_prefix=sk_prefix)
        # The drafts partition only contains drafts, but defend against a
        # malformed published row leaking in via a future migration.
        return [item for item in items if isinstance(item, Skill)]

    async def list_supporting_files(
        self, asset_ids: list[str]
    ) -> dict[str, list[str]]:
        """Return supporting package paths for many drafts with one S3 listing."""
        result = {asset_id: [] for asset_id in asset_ids}
        if not result:
            return result

        # Canonical drafts root for this actor. No ``ctx`` here, so take the
        # actor from the tool it was constructed with. ``list_absolute``
        # normalizes both storage conventions to canonical keys, so one listing
        # still covers drafts written before the path fix.
        prefix = f"users/{self._s3.actor_id}/{_DRAFTS_PREFIX}/"
        for file_path in await self._s3.list_absolute(prefix):
            relative = file_path[len(prefix):] if file_path.startswith(prefix) else ""
            asset_id, separator, package_path = relative.partition("/")
            if not separator or asset_id not in result or package_path == _SKILL_MD_FILENAME:
                continue
            result[asset_id].append(package_path)

        for paths in result.values():
            paths.sort()
        return result

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
        """Promote a Skill draft into a new Published_Record.

        Thin wrapper over :func:`lab._canonical_ops.promote_asset` with
        the skill category constant. See that helper for the full
        sequence and error semantics.

        Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
        Properties: 3, 4
        """
        from ._canonical_ops import promote_asset

        return await promote_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            draft_id=draft_id,
            scope=scope,
            group_ids=group_ids,
            published_id=published_id,
            org_id=org_id,
            publish_policy_owner_ids=publish_policy_owner_ids,
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
        """Widen a published Skill_Record's scope.

        Thin wrapper over :func:`lab._canonical_ops.share_asset` with
        the skill category constant. See that helper for the full
        sequence and error semantics (Authz, scope-narrowing rejection,
        cross-PK move, compensating-delete on transaction cancel,
        post-txn source-prefix cleanup).

        Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.1, 13.2, 13.3, 13.4
        Properties: 5, 6, 8
        """
        from ._canonical_ops import share_asset

        return await share_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
            new_scope=new_scope,
            group_ids=group_ids,
            org_id=org_id,
        )

    async def deprecate(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Deprecated":
        """Mark a published Skill_Record as deprecated.

        Thin wrapper over :func:`lab._canonical_ops.deprecate_asset`
        with the skill category constant. See that helper for the full
        sequence and error semantics (authority gate, current-pointer
        flip in the same transaction, no S3 mutation).

        Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
        Properties: 3, 8
        """
        from ._canonical_ops import deprecate_asset

        return await deprecate_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )

    async def clone(
        self,
        ctx: LabRequestContext,
        published_id: str,
        version: int,
    ) -> "Cloned":
        """Clone a published Skill_Record into a new Draft owned by the actor.

        Thin wrapper over :func:`lab._canonical_ops.clone_asset` with
        the skill category constant. See that helper for the full
        sequence and error semantics (visibility delegated to handler;
        S3-first copy with compensating delete on AssetIndex put
        failure; new draft has ``cloned_from=(publishedId, version)``
        and ``promoted_from=None``).

        Requirements: 7.1, 7.2, 7.3, 7.4
        """
        from ._canonical_ops import clone_asset

        return await clone_asset(
            ctx=ctx,
            asset_index=self._index,
            s3=self._s3,
            category=_CATEGORY,
            published_id=published_id,
            version=version,
        )

    async def rollback(
        self,
        ctx: LabRequestContext,
        published_id: str,
        to_version: Optional[int] = None,
    ) -> "RolledBack":
        """Roll back the ``current`` pointer of a Skill lineage.

        Thin wrapper over :func:`lab._canonical_ops.rollback_asset`
        with the skill category constant. Rollback NEVER mutates
        ``status`` (Requirement 8.3) and does not touch S3 — it
        toggles the ``current`` flag between the prior current row
        and the resolved target version in a single
        ``TransactWriteItems``.

        Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
        Properties: 3, 8
        """
        from ._canonical_ops import rollback_asset

        return await rollback_asset(
            ctx=ctx,
            asset_index=self._index,
            category=_CATEGORY,
            published_id=published_id,
            to_version=to_version,
        )
