"""Result dataclasses returned by the canonical lifecycle operations.

One small frozen dataclass per operation, mirroring the design's
"Result dataclass" naming (``Promoted``, ``Shared``, ``Deprecated``,
``Cloned``, ``RolledBack``). Task 3.1 populates :class:`Promoted`; Task
3.2 adds :class:`Shared`; Task 3.3 adds :class:`Deprecated`; the
remaining results land in Tasks 3.4–3.5.

The dataclasses are intentionally minimal — they carry just the fields a
caller needs to address the new row (``published_id``, ``version``) and
the prefix where its artifacts now live (``s3_prefix``). Round-tripping
the full :class:`~lab.models.AssetCommon` row is unnecessary because the
caller already issued the operation against a known draft and can re-read
the new published row via ``AssetIndex.get`` if required.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 6.1, 6.2, 6.3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import AssetCommon


@dataclass(frozen=True)
class Promoted:
    """Result of a successful :meth:`promote` call.

    Returned by every category CRUD's ``promote`` method when the new
    Published_Record is fully committed to both S3 and AssetIndex. The
    fields mirror the new row's identity:

    Attributes:
        published_id: The lineage id of the new published row. Newly
            generated for a new lineage; equal to the supplied
            ``published_id`` argument when promoting a subsequent
            version of an existing lineage.
        version: The positive integer version of the new published row
            within the lineage. ``1`` for a new lineage; ``max(existing)
            + 1`` for an existing lineage (Requirement 4.1).
        s3_prefix: The canonical S3 prefix where the new row's artifacts
            live, derived from ``(scope, owner, category, published_id,
            version)`` per the storage-layout grammar.
    """

    published_id: str
    version: int
    s3_prefix: str


@dataclass(frozen=True)
class Shared:
    """Result of a successful :meth:`share` call.

    Returned by every category CRUD's ``share`` method when the existing
    Published_Record has been moved across partitions and its new scope
    is fully committed to both S3 and AssetIndex. Share preserves the
    ``(published_id, version)`` identity — Share widens scope rather
    than creating a new version (Requirement 5.1) — so the returned
    fields address the same lineage row at its post-Share location.

    Attributes:
        published_id: Unchanged from the source row; the lineage
            identity is preserved across the move.
        version: Unchanged from the source row.
        s3_prefix: The canonical S3 prefix where the row's artifacts
            live AFTER the move, derived from the new scope/owner per
            the storage-layout grammar.
        scope: The new (wider) scope of the row — one of
            ``"private"``, ``"group"``, ``"org"``. Always strictly
            greater than the source scope under
            ``private < group < org`` (Requirement 5.5, Property 5).
    """

    published_id: str
    version: int
    s3_prefix: str
    scope: str


@dataclass(frozen=True)
class Deprecated:
    """Result of a successful :meth:`deprecate` call.

    Returned by every category CRUD's ``deprecate`` method when the
    target Published_Record has been marked ``status="deprecated"`` in
    AssetIndex (S3 artifacts are intentionally retained per Requirement
    6.4 so pinned references continue to resolve).

    Deprecate addresses an existing ``(published_id, version)`` and
    never moves it across partitions, so the result echoes the same
    identity. ``new_current_version`` reports the post-deprecate state
    of the lineage's current pointer:

    * If the deprecated row was the current one and at least one other
      non-deprecated version remained, that version (the highest
      remaining) is now ``current=true`` and its number is reported.
    * If the deprecated row was the current one and no other
      non-deprecated version remained, the lineage has no current and
      ``new_current_version`` is ``None`` (Requirement 6.3).
    * If the deprecated row was NOT the current one, the current
      pointer is unchanged; ``new_current_version`` echoes the existing
      current version's number.

    Attributes:
        published_id: Unchanged from the source row.
        version: Unchanged from the source row.
        new_current_version: The version now marked ``current=true`` for
            this lineage, or ``None`` when no non-deprecated version
            remains.

    Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
    Properties: 3 (Exactly one current per lineage), 8 (Authority gate)
    """

    published_id: str
    version: int
    new_current_version: Optional[int]


@dataclass(frozen=True)
class Cloned:
    """Result of a successful :meth:`clone` call.

    Returned by every category CRUD's ``clone`` method when a new Draft
    has been created from a Published_Record's source artifacts. Clone
    is the only canonical operation that *creates* a new ``draft_id``
    rather than addressing an existing ``(published_id, version)`` —
    the result therefore carries the new draft identity plus a
    ``cloned_from`` lineage pointer back to the source.

    Per Requirement 7.3 the new Draft has ``promoted_from = None`` and
    records the source ``(published_id, version)`` as ``cloned_from``;
    those values are persisted on the AssetIndex draft row and echoed
    here for the caller's convenience.

    Attributes:
        draft_id: The freshly generated draft identifier of the new
            Draft owned by ``ctx.actor_id``. Becomes the ``assetId`` of
            the new AssetIndex row and the ``<draftId>`` segment of its
            ``s3_prefix``.
        s3_prefix: The canonical S3 prefix where the cloned artifacts
            now live, of the form
            ``users/<actor>/drafts/<category>/<draft_id>/`` per the
            storage-layout grammar (Requirement 7.2).
        cloned_from: The ``(published_id, version)`` of the source
            Published_Record, recorded for lineage tracking
            (Requirement 7.3).

    Requirements: 7.1, 7.2, 7.3, 7.4
    """

    draft_id: str
    s3_prefix: str
    cloned_from: tuple[str, int]


@dataclass(frozen=True)
class RolledBack:
    """Result of a successful :meth:`rollback` call.

    Returned by every category CRUD's ``rollback`` method when the
    ``current`` pointer for a lineage has been moved from one version
    to another via a single atomic AssetIndex transaction. Rollback
    NEVER mutates ``status`` (Requirement 8.3) and never touches S3 —
    artifacts are retained so any pinned references continue to
    resolve. The result echoes the lineage identity plus before/after
    snapshots of the current pointer.

    Attributes:
        published_id: Lineage id whose current pointer was moved.
        new_current_version: The version now marked ``current=true``
            for this lineage; either the explicitly supplied
            ``to_version`` or the highest non-deprecated version
            strictly less than the prior current's version
            (Requirement 8.1, 8.2).
        prior_current_version: The version that was previously marked
            ``current=true`` and is now ``current=false``. Always
            populated since Rollback requires a current row to exist
            (a lineage with no current pointer raises
            :class:`NotFoundOrForbidden` rather than silently
            succeeding).

    Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
    Properties: 3 (Exactly one current per lineage), 8 (Authority gate)
    """

    published_id: str
    new_current_version: int
    prior_current_version: Optional[int]


@dataclass(frozen=True)
class ExternalAccessSet:
    """Result of a successful external-access toggle (Req 16).

    Returned by :func:`lab._canonical_ops.set_external_access_asset` (via the
    SpecialtyCRUD wrapper) when the Owner revokes or re-enables an agent's
    external access. The gate is written uniformly across every row of the
    lineage (working copy + all version snapshots) so it survives rollback:
    because rollback only flips the ``current`` pointer and never touches
    ``externalAccessEnabled``, a rolled-back snapshot cannot silently re-grant
    revoked access (Property 16).

    Attributes:
        published_id: Lineage id (== working-copy asset_id) of the agent whose
            gate was toggled.
        enabled: The new gate state — ``False`` after a revoke, ``True`` after
            a re-enable. Effective external access is still
            ``integration_config.enabled AND enabled`` at request time.
        rows_updated: How many lineage rows (working copy + snapshots) had the
            gate written in the atomic transaction.

    Requirements: 16.1, 16.2, 16.3
    Properties: 16 (Revocation precedence across rollback)
    """

    published_id: str
    enabled: bool
    rows_updated: int


@dataclass(frozen=True)
class VisibleAssetsPage:
    """Result of a successful :func:`list_visible_assets` call.

    Carries one merged page of records visible to the actor across the
    four-tier visibility union (drafts, private published, group
    published, org published) after mode-aware and deprecation
    post-filters and stable sort by ``updated_at desc``.

    Attributes:
        items: Decoded :class:`~lab.models.AssetCommon` rows for this
            page, already truncated to the caller-supplied ``limit``.
            Sorted by ``updated_at`` descending; ties are broken in the
            stable order the underlying tier queries produced them.
        next_cursor: An opaque base64-encoded JSON string carrying the
            per-tier ``LastEvaluatedKey`` values needed to fetch the
            next page, or ``None`` when every tier is exhausted.

    Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
    Properties: 7
    """

    items: list["AssetCommon"]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class AgentReferenceHit:
    """One thing that will break, or change meaning, when a lineage is purged.

    Reported by the purge pre-flight so the Owner sees the blast radius before
    confirming. Deliberately descriptive rather than blocking: a stale
    reference should not make an agent undeletable, but it must not be a
    surprise either.

    Attributes:
        kind: What holds the reference — ``"workflow-step"`` today.
        asset_id: The id of the REFERENCING asset (not the agent).
        name: Display name of the referencing asset.
        status: Its lifecycle status, so a reference from a deprecated
            workflow reads differently from a live one.
        detail: Where inside it, e.g. ``"step 'transcribe'"``.
    """

    kind: str
    asset_id: str
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class PurgePreflight:
    """What a purge would destroy, and what it would break.

    Returned by :func:`lab._canonical_ops.purge_asset_lineage` when
    ``dry_run=True``, and recorded on :class:`Purged` when it is not — so a
    purge that broke three workflows leaves evidence of which three.

    ``partial`` is load-bearing. The scan can only see what the calling actor
    can see, and :class:`~lab.context.LabRequestContext` carries no org id, so
    the org tier is usually skipped entirely. Consumers must present this as
    "references visible to you", never as "no references".

    Attributes:
        published_id: Lineage id under inspection.
        category: Asset category (``"specialty-agent"``).
        versions: Every immutable version a purge would delete, ascending.
        scopes: Distinct scopes across those versions. Anything other than
            ``{"private"}`` means the agent disappears for other people.
        working_copy_exists: Whether an editable working copy would also go.
        external_access_live: True when the current version both configures
            and enables external access — i.e. a live
            ``POST /invoke/<assetId>`` starts failing closed after the purge.
        prefixes: The S3 prefixes that would be deleted.
        hits: Discovered references (see :class:`AgentReferenceHit`).
        partial: Sources that could NOT be scanned, e.g. ``"org-tier"`` or
            ``"workflow:<id>"``.
    """

    published_id: str
    category: str
    versions: tuple[int, ...]
    scopes: tuple[str, ...]
    working_copy_exists: bool
    external_access_live: bool
    prefixes: tuple[str, ...]
    hits: tuple[AgentReferenceHit, ...]
    partial: tuple[str, ...]


@dataclass(frozen=True)
class Purged:
    """Result of a successful hard purge of a published lineage.

    A purge is the only operation in this layer that destroys immutable
    published state. It deletes every version row and every versioned S3
    prefix of a lineage, plus the working copy when one exists.

    ``prefixes_orphaned`` is not an error path. The operation deletes DynamoDB
    rows before S3 objects on purpose — the reverse of
    :func:`~lab._canonical_ops.promote_asset`, which copies S3 first so it can
    compensate. A row surviving over deleted objects would be a broken
    published version; orphaned objects are wasted storage with no correctness
    impact. So an S3 failure is reported here rather than raised.

    Attributes:
        published_id: The purged lineage id.
        category: Asset category.
        versions_deleted: Versions whose rows were removed, in the order the
            transactions committed (descending, highest first).
        working_copy_deleted: Whether a working copy was also removed.
        prefixes_deleted: S3 prefixes successfully swept.
        prefixes_orphaned: Prefixes whose delete failed; the rows are already
            gone, so these objects are unreferenced and need a manual sweep.
        outbox_events: How many ``Deprecated`` outbox rows were written — one
            per org-scoped version, which is what drives the Registry record
            to DEPRECATED via the projector.
        preflight: The blast-radius report as it stood at purge time.
    """

    published_id: str
    category: str
    versions_deleted: tuple[int, ...]
    working_copy_deleted: bool
    prefixes_deleted: tuple[str, ...]
    prefixes_orphaned: tuple[str, ...]
    outbox_events: int
    preflight: PurgePreflight
