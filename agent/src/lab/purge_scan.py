"""Write-free blast-radius scan for a purge.

A purge is irreversible, so the only defence the user gets is being told what
will break before they type the confirmation. This module answers one question:
**what still points at this lineage?**

The answer depends on the category, so there is one scan per purgeable kind and
:func:`scan_references` picks between them:

* a specialty agent is referenced by **workflow steps**, which live only in
  ``workflow.json`` in S3 — one read per visible workflow;
* a skill is referenced by a specialty agent's **``compatible_skills``**, which
  *is* serialized onto the AssetIndex row, so that scan needs no S3 at all.

One scan here is not wired to :func:`scan_references`, deliberately.
:func:`scan_tool_collection_references` answers the same question for a flat tool
collection, but it is called from ``ToolCollectionCRUD.delete`` — a flat category
has no lineage to purge, so its ordinary delete is already the irreversible
operation, and the scan is used to *refuse* it rather than to describe it. Purge
must therefore keep rejecting ``tool-collection``: having a scan is necessary for
purgeability, not sufficient (the route and ``_PURGEABLE_CATEGORIES`` govern
that).

Pairing a category with its scan is mandatory, not optional. The purge route runs
whichever scan it is given and reports the result as the blast radius, so running
the wrong one — or none — renders a confident "nothing references this" over an
asset other things depend on.

It is deliberately read-only — no DynamoDB writes, no S3 writes — and
deliberately honest about what it could not see. Workflow steps live only in
``workflow.json`` in S3 (``models.Workflow.steps`` is not serialized onto the
AssetIndex row), so finding a reference means an S3 read per visible workflow.
Any read that fails, and any tier that could not be queried, is reported in
``partial`` rather than silently narrowing the answer. The caller must therefore
present the result as *"references visible to you"*, never as *"no references"*
— most notably because :class:`~lab.context.LabRequestContext` carries no
``org_id``, so the org tier is usually not scanned at all.

Kept out of ``_canonical_ops`` on purpose: that module is the transactional
core, while this is best-effort I/O whose failures are data, not exceptions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .asset_index import AssetIndex
from .context import LabRequestContext
from .operation_results import AgentReferenceHit
from .s3_user_files_tool import S3UserFilesTool

logger = logging.getLogger(__name__)

#: Steps are stored in this file at the workflow record's canonical prefix.
#: Same constant as ``workflow_crud._WORKFLOW_FILENAME``; duplicated rather than
#: imported so the scan does not drag the CRUD class in.
_WORKFLOW_FILENAME = "workflow.json"

#: Bound on concurrent S3 reads. An actor with 200 visible workflows would
#: otherwise open 200 sockets at once from a container sized for cheap reads.
_SCAN_CONCURRENCY = 8

#: How many workflow rows to consider. Beyond this the scan reports truncation
#: in ``partial`` instead of paging forever while a user waits on a dialog.
_SCAN_LIMIT = 200


async def scan_agent_references(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    published_id: str,
    org_id: Optional[str] = None,
    limit: int = _SCAN_LIMIT,
) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
    """Find what references ``published_id``, and report what could not be read.

    Args:
        ctx: Request context; its actor and groups define the visibility union.
        asset_index: Single-table AssetIndex wrapper (read-only here).
        s3: Artifact tool, used only for bucket-absolute reads.
        published_id: The specialty-agent lineage id about to be purged.
        org_id: Org identifier for the org tier. When ``None`` the org tier is
            skipped and that omission is reported in ``partial``.
        limit: Maximum workflow rows to inspect.

    Returns:
        ``(hits, partial)`` — findings, and human-readable descriptions of
        sources that were not or could not be scanned. Never raises; every
        failure becomes a ``partial`` entry.
    """
    partial: list[str] = []

    effective_org = org_id or getattr(ctx, "org_id", None)
    if not effective_org:
        partial.append(
            "org-shared workflows (no org id on this request; org-tier "
            "references cannot be listed)"
        )

    workflows = await _visible_workflows(
        ctx=ctx,
        asset_index=asset_index,
        org_id=effective_org,
        limit=limit,
        partial=partial,
    )

    semaphore = asyncio.Semaphore(_SCAN_CONCURRENCY)

    async def _scan(row: Any) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
        async with semaphore:
            return await _scan_one_workflow(s3, row, published_id)

    hits: list[AgentReferenceHit] = []
    for found, unreadable in await asyncio.gather(*(_scan(w) for w in workflows)):
        hits.extend(found)
        partial.extend(unreadable)

    logger.info(
        "purge reference scan: lineage=%s workflows=%d hits=%d partial=%d",
        published_id,
        len(workflows),
        len(hits),
        len(partial),
    )
    return tuple(hits), tuple(partial)


async def _visible_workflows(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    org_id: Optional[str],
    limit: int,
    partial: list[str],
) -> list[Any]:
    """Resolve the workflow rows this actor can see, tolerating failure.

    ``list_visible_assets`` is imported lazily: ``lab.service`` is the heavy
    module (it hosts the whole Lab service), and the Capability API should not
    pay for it at cold start just because a purge route exists.

    ``include_deprecated=True`` — a deprecated workflow still references the
    agent, and someone may un-deprecate it.
    """
    try:
        from .service import list_visible_assets

        page = await list_visible_assets(
            ctx,
            "workflow",
            "lab",
            asset_index=asset_index,
            org_id=org_id,
            include_deprecated=True,
            limit=limit,
        )
    except Exception as err:  # noqa: BLE001 — reported, never fatal
        logger.warning("purge reference scan: workflow listing failed: %r", err)
        partial.append(f"workflow references (listing failed: {type(err).__name__})")
        return []

    if page.next_cursor:
        partial.append(
            f"workflows beyond the first {limit} visible rows (not scanned)"
        )
    return list(page.items)


async def _scan_one_workflow(
    s3: S3UserFilesTool, row: Any, published_id: str
) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
    """Read one workflow's steps and return the steps naming ``published_id``.

    A missing or unparseable ``workflow.json`` is reported, not swallowed:
    unlike the read path — where "no steps" is a harmless empty view — here it
    means the scan genuinely does not know whether this workflow breaks.
    """
    key = f"{row.s3_prefix}{_WORKFLOW_FILENAME}"
    try:
        raw = await s3.read_absolute(key)
        doc = json.loads(raw.decode("utf-8"))
    except Exception as err:  # noqa: BLE001 — reported, never fatal
        logger.warning("purge reference scan: unreadable %s: %r", key, err)
        return (), (
            f"workflow {row.name!r} ({row.asset_id}) — steps unreadable "
            f"({type(err).__name__})",
        )

    if not isinstance(doc, dict):
        return (), (f"workflow {row.name!r} ({row.asset_id}) — malformed document",)
    # An absent or null `steps` is a legitimately empty workflow; a `steps` of
    # the wrong type is a document this scan cannot read. The two must not
    # collapse into the same "no references" answer.
    steps = doc.get("steps")
    if steps is not None and not isinstance(steps, list):
        return (), (f"workflow {row.name!r} ({row.asset_id}) — malformed steps",)

    hits: list[AgentReferenceHit] = []

    for step in steps or []:
        if not isinstance(step, dict):
            continue
        agent = step.get("agent") or {}
        if not isinstance(agent, dict):
            continue
        if agent.get("kind") != "specialty":
            continue
        if agent.get("assetId") != published_id:
            continue
        step_id = step.get("stepId") or "?"
        hits.append(
            AgentReferenceHit(
                kind="workflow-step",
                asset_id=row.asset_id,
                name=row.name,
                status=row.status,
                detail=f"step {step_id!r}",
            )
        )
    return tuple(hits), ()


async def scan_skill_references(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    published_id: str,
    org_id: Optional[str] = None,
    limit: int = _SCAN_LIMIT,
) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
    """Find the specialty agents whose ``compatible_skills`` names this skill.

    Cheaper than :func:`scan_agent_references`: ``compatible_skills`` is
    serialized onto the AssetIndex row as ``compatibleSkills``
    (:func:`lab.models.to_item` writes it, :func:`lab.models.from_index_item`
    reads it back), so this is a listing plus a field check — no S3 reads, and
    therefore no unreadable-document failure mode.

    Draft and published agents are both reported, and the distinction matters to
    the caller: a draft can be edited to drop the reference, an immutable
    published version can never be. ``status`` and the version in ``detail``
    carry that difference.

    Args:
        ctx: Request context; its actor and groups define the visibility union.
        asset_index: Single-table AssetIndex wrapper (read-only here).
        published_id: The skill lineage id about to be purged.
        org_id: Org identifier for the org tier. When ``None`` the org tier is
            skipped and that omission is reported in ``partial``.
        limit: Maximum agent rows to inspect.

    Returns:
        ``(hits, partial)`` — findings, and human-readable descriptions of
        sources that were not or could not be scanned. Never raises; every
        failure becomes a ``partial`` entry.
    """
    partial: list[str] = []

    effective_org = org_id or getattr(ctx, "org_id", None)
    if not effective_org:
        partial.append(
            "org-shared agents (no org id on this request; org-tier "
            "references cannot be listed)"
        )

    agents = await _visible_agents(
        ctx=ctx,
        asset_index=asset_index,
        org_id=effective_org,
        limit=limit,
        partial=partial,
    )

    hits: list[AgentReferenceHit] = []
    for row in agents:
        # Absent ≠ empty. A `SpecialtyAgent` always carries a list (possibly
        # empty), so a missing or wrongly-typed attribute means this row is not
        # one this scan can read — which must not be reported as "no reference".
        refs = getattr(row, "compatible_skills", None)
        if refs is None:
            partial.append(
                f"agent {row.name!r} ({row.asset_id}) — no compatible-skill "
                "list on the row"
            )
            continue
        if not isinstance(refs, (list, tuple)):
            partial.append(
                f"agent {row.name!r} ({row.asset_id}) — malformed "
                "compatible-skill list"
            )
            continue
        if published_id not in refs:
            continue

        version = getattr(row, "version", None)
        hits.append(
            AgentReferenceHit(
                kind="agent-compatible-skill",
                asset_id=row.asset_id,
                name=row.name,
                status=row.status,
                detail=(
                    f"compatible skill (v{version})"
                    if version
                    else "compatible skill"
                ),
            )
        )

    logger.info(
        "purge reference scan: skill=%s agents=%d hits=%d partial=%d",
        published_id,
        len(agents),
        len(hits),
        len(partial),
    )
    return tuple(hits), tuple(partial)


async def scan_tool_collection_references(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    collection_id: str,
    org_id: Optional[str] = None,
    limit: int = _SCAN_LIMIT,
) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
    """Find the specialty agents whose ``tool_collections`` names this collection.

    Same shape and cost as :func:`scan_skill_references` — ``tool_collections``
    is serialized onto the AssetIndex row as ``toolCollections``, so this is a
    listing plus a field check with no S3 reads.

    Unlike the skill and agent scans this is **not** a purge pre-flight. Tool
    collections are flat: the working copy IS the asset, so an ordinary delete is
    already the irreversible operation a purge is for other categories, and
    :meth:`lab.tool_collection_crud.ToolCollectionCRUD.delete` calls this to
    refuse rather than to describe. That difference matters to the caller:

      * A **draft** agent's reference can be edited away, so the hit is advice.
      * A **published** agent's reference is frozen in an immutable version and
        can never be edited. Before tool-collection snapshots existed, deleting
        the collection silently reduced that version to zero tools — the invoke
        still succeeded, in persona, with the tools missing. Published versions
        made since carry their own pinned copy of the tool refs
        (``SpecialtyAgent.tool_collection_snapshots``) and so survive the
        delete; the hit is still reported because the agent's *working copy*,
        and any future publish of it, would not.

    ``partial`` is as load-bearing here as in the purge scans, and for a blocking
    gate the direction of the error matters: an unscannable tier means references
    may exist that this returned no hit for, so a caller must present success as
    "no references *visible to you*". Blocking on ``partial`` alone would make a
    collection undeletable whenever the request carries no org id, which is
    almost always.

    Args:
        ctx: Request context; its actor and groups define the visibility union.
        asset_index: Single-table AssetIndex wrapper (read-only here).
        collection_id: The tool-collection asset id about to be deleted. Flat, so
            this is the row's ``assetId``, not a lineage id.
        org_id: Org identifier for the org tier. When ``None`` the org tier is
            skipped and that omission is reported in ``partial``.
        limit: Maximum agent rows to inspect.

    Returns:
        ``(hits, partial)`` — findings, and human-readable descriptions of
        sources that were not or could not be scanned. Never raises; every
        failure becomes a ``partial`` entry.
    """
    partial: list[str] = []

    effective_org = org_id or getattr(ctx, "org_id", None)
    if not effective_org:
        partial.append(
            "org-shared agents (no org id on this request; org-tier "
            "references cannot be listed)"
        )

    agents = await _visible_agents(
        ctx=ctx,
        asset_index=asset_index,
        org_id=effective_org,
        limit=limit,
        partial=partial,
    )

    hits: list[AgentReferenceHit] = []
    for row in agents:
        # Absent ≠ empty, exactly as in the skill scan: a `SpecialtyAgent` always
        # carries a list, so a missing or wrongly-typed attribute means this row
        # is not one this scan can read — which must not be reported as "no
        # reference" when the answer is used to allow a delete.
        refs = getattr(row, "tool_collections", None)
        if refs is None:
            partial.append(
                f"agent {row.name!r} ({row.asset_id}) — no tool-collection "
                "list on the row"
            )
            continue
        if not isinstance(refs, (list, tuple)):
            partial.append(
                f"agent {row.name!r} ({row.asset_id}) — malformed "
                "tool-collection list"
            )
            continue
        if collection_id not in refs:
            continue

        version = getattr(row, "version", None)
        hits.append(
            AgentReferenceHit(
                kind="agent-tool-collection",
                asset_id=row.asset_id,
                name=row.name,
                status=row.status,
                detail=(
                    f"tool collection (v{version})"
                    if version
                    else "tool collection (working copy)"
                ),
            )
        )

    logger.info(
        "tool-collection reference scan: collection=%s agents=%d hits=%d partial=%d",
        collection_id,
        len(agents),
        len(hits),
        len(partial),
    )
    return tuple(hits), tuple(partial)


async def _visible_agents(
    *,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    org_id: Optional[str],
    limit: int,
    partial: list[str],
) -> list[Any]:
    """Resolve the specialty-agent rows this actor can see, tolerating failure.

    Mirrors :func:`_visible_workflows`, including the lazy ``lab.service``
    import and ``include_deprecated=True`` — a deprecated agent still names the
    skill, and someone may un-deprecate it.
    """
    try:
        from .service import list_visible_assets

        page = await list_visible_assets(
            ctx,
            "specialty-agent",
            "lab",
            asset_index=asset_index,
            org_id=org_id,
            include_deprecated=True,
            limit=limit,
        )
    except Exception as err:  # noqa: BLE001 — reported, never fatal
        logger.warning("purge reference scan: agent listing failed: %r", err)
        partial.append(f"agent references (listing failed: {type(err).__name__})")
        return []

    if page.next_cursor:
        partial.append(f"agents beyond the first {limit} visible rows (not scanned)")
    return list(page.items)


async def scan_references(
    *,
    category: str,
    ctx: LabRequestContext,
    asset_index: AssetIndex,
    s3: S3UserFilesTool,
    published_id: str,
    org_id: Optional[str] = None,
    limit: int = _SCAN_LIMIT,
) -> tuple[tuple[AgentReferenceHit, ...], tuple[str, ...]]:
    """Run the PURGE reference scan that belongs to ``category``.

    Raises for a category with no scan rather than returning no hits. A purge
    presents this result as the blast radius, so an empty tuple is read by the
    user as "nothing references this" — which for an unscanned category is a
    claim nobody checked. Widening ``_PURGEABLE_CATEGORIES`` therefore has to
    add a scan here, and this raise is what enforces that pairing.

    ``tool-collection`` is intentionally absent even though
    :func:`scan_tool_collection_references` exists: that scan gates the flat
    category's ordinary delete, and purging one is still unsupported. Wiring it
    in here would advertise a purge nothing implements.

    Args:
        category: The asset category being purged.
        s3: Required by the specialty-agent scan, which reads ``workflow.json``.
            Unused by the skill scan, whose references live on the index row.

    Raises:
        ValueError: If ``category`` has no reference scan.
    """
    if category == "specialty-agent":
        return await scan_agent_references(
            ctx=ctx,
            asset_index=asset_index,
            s3=s3,
            published_id=published_id,
            org_id=org_id,
            limit=limit,
        )
    if category == "skill":
        return await scan_skill_references(
            ctx=ctx,
            asset_index=asset_index,
            published_id=published_id,
            org_id=org_id,
            limit=limit,
        )
    raise ValueError(
        f"no reference scan for category {category!r}; a purgeable category "
        "must have one, or its blast radius would be reported as empty"
    )


__all__ = [
    "scan_agent_references",
    "scan_references",
    "scan_skill_references",
    "scan_tool_collection_references",
]
