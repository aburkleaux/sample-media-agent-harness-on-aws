"""Example test: Custom Agent extended fields survive publish + rollback.

Task 4.3 (custom-agents): a focused, example-based check that the
extended ``SpecialtyAgent`` fields introduced for Custom Agents —
``model``, ``tool_collections``, and ``integration_config`` — travel
through the canonical publish (Promote) and Rollback operations, and
that the lineage's version numbers stay monotonic with exactly one
``current`` row at every step.

Reuses the existing ``_canonical_ops`` test conventions (``LabRequestContext``
builder, ``SpecialtyCRUD``, the ``sys.path`` insert). Rather than the
``MagicMock`` AssetIndex used by ``test_canonical_ops_promote.py`` /
``test_canonical_ops_rollback.py`` — which returns a fixed lineage and
never persists the serialized rows — this module wires a **thin stateful
in-memory AssetIndex fake**. That fake stores the exact DDB items written
by Promote (via ``to_index_item``) and re-reads them via ``from_index_item``
on ``query_lineage``, so the extended fields genuinely round-trip through
the same serialization path the real store uses. This is the strongest
equivalent to end-to-end publish/rollback while staying at the unit level
(no AWS), and it is what lets us assert Req 8.6 (the rolled-back snapshot
carries the config it was published with).

Task 4.1 confirmed the extended fields are snapshotted on publish
(``_build_published_asset``) and that Rollback only flips the ``current``
pointer between immutable snapshots. This test locks that behavior in
with a concrete scenario.

Validates: Requirements 8.3, 8.5, 8.6
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import time
from typing import Any, Optional

import pytest

# Add agent/src to sys.path so imports resolve (repo convention).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab._lifecycle_ops import _clone_to_working_copy  # noqa: E402
from lab.asset_index import (  # noqa: E402
    make_pk_user_owned,
    make_sk_owned,
)
from lab.context import LabRequestContext  # noqa: E402
from lab.models import (  # noqa: E402
    IntegrationConfig,
    SpecialtyAgent,
    ToolCollection,
    ToolRef,
    from_index_item,
    to_index_item,
)
from lab.operation_results import Promoted, RolledBack  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Thin stateful in-memory AssetIndex fake
# ---------------------------------------------------------------------------


class _InMemoryAssetIndex:
    """A minimal stateful stand-in for :class:`lab.asset_index.AssetIndex`.

    Stores DDB items keyed by ``(PK, SK)`` and supports exactly the three
    methods the canonical Promote / Rollback ops touch:

      * ``get(pk, sk)``        → ``from_index_item`` of the stored row, or None
      * ``query_lineage(pid)`` → every stored row whose ``GSI1PK == PUB#<pid>``,
                                 decoded and sorted ascending by version
      * ``transact_write(ops)``→ apply Put / Update entries to the store

    The Put path stores the fully serialized published row that Promote
    builds (via ``to_index_item``), and ``query_lineage`` re-reads it via
    ``from_index_item`` — so the extended Custom Agent fields make a real
    serialization round-trip on every publish and every rollback read.
    """

    def __init__(self) -> None:
        self.table_name = "test-asset-index"
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    # -- seeding helper (test-only) ------------------------------------
    def seed_item(self, item: dict[str, Any]) -> None:
        self._items[(item["PK"], item["SK"])] = copy.deepcopy(item)

    # -- AssetIndex surface used by the canonical ops ------------------
    async def get(self, pk: str, sk: str):
        raw = self._items.get((pk, sk))
        if raw is None:
            return None
        return from_index_item(raw)

    async def query_lineage(self, published_id: str):
        gsi1pk = f"PUB#{published_id}"
        rows = [
            from_index_item(raw)
            for raw in self._items.values()
            if raw.get("GSI1PK") == gsi1pk
        ]
        rows.sort(key=lambda r: r.version or 0)
        return rows

    async def transact_write(self, operations: list[dict[str, Any]]) -> None:
        # Apply in order. Happy-path only: conditions are not enforced
        # (the scenarios here never contend), matching what the ops need
        # to observe a consistent post-state.
        for op in operations:
            if "Put" in op:
                item = copy.deepcopy(op["Put"]["Item"])
                self._items[(item["PK"], item["SK"])] = item
            elif "Update" in op:
                self._apply_update(op["Update"])
            elif "Delete" in op:
                key = op["Delete"]["Key"]
                self._items.pop((key["PK"], key["SK"]), None)

    def _apply_update(self, update: dict[str, Any]) -> None:
        key = update["Key"]
        item = self._items.get((key["PK"], key["SK"]))
        if item is None:
            # A missing target would surface as a cancelled transaction in
            # DynamoDB; in these happy-path scenarios every target exists.
            raise KeyError((key["PK"], key["SK"]))
        expr = update["UpdateExpression"]
        names = update.get("ExpressionAttributeNames", {})
        values = update.get("ExpressionAttributeValues", {})
        assert expr.startswith("SET "), f"unexpected expression: {expr!r}"
        for assignment in expr[len("SET "):].split(","):
            lhs, rhs = (part.strip() for part in assignment.split("="))
            attr = names.get(lhs, lhs)
            item[attr] = copy.deepcopy(values[rhs])


class _NoopS3:
    """S3 stand-in. Promote copies the draft prefix; Rollback never uses S3.

    Both prefix operations are recorded but do nothing — there is no
    artifact content to move for an index-level publish/rollback check.
    """

    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    async def copy_prefix_absolute(self, src: str, dst: str) -> None:
        self.copies.append((src, dst))

    async def delete_prefix_absolute(self, prefix: str) -> None:
        self.deletes.append(prefix)

    async def list_absolute(self, prefix: str) -> list[str]:
        # Promote's empty-source guard (BUG-2) lists the draft prefix before
        # copying; report a non-empty listing so the publish proceeds.
        return [f"{prefix}artifact"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_ctx(actor_id: str = "u-jane") -> LabRequestContext:
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
    )


def _seed_specialty_draft(
    index: _InMemoryAssetIndex,
    *,
    user_id: str,
    draft_id: str,
    model: Optional[str],
    tool_collections: list[str],
    integration_config: Optional[IntegrationConfig],
) -> SpecialtyAgent:
    """Seed a SpecialtyAgent draft (working copy) into the fake index.

    Returns the dataclass for convenience. The row is stored as a real
    serialized DDB item under the actor's OWNED partition so Promote's
    ``asset_index.get`` reads it back through ``from_index_item``.
    """
    now = int(time.time() * 1000)
    draft = SpecialtyAgent(
        asset_id=draft_id,
        category="specialty-agent",
        name="my-custom-agent",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/specialty-agents/{draft_id}/",
        created_at=now,
        updated_at=now,
        compatible_skills=["sk-001"],
        model=model,
        tool_collections=list(tool_collections),
        integration_config=integration_config,
    )
    item = to_index_item(
        draft,
        owner_user_id=user_id,
        owner_group_id=None,
        owner_org_id=None,
    )
    item["PK"] = make_pk_user_owned(user_id)
    item["SK"] = make_sk_owned("specialty-agent", draft_id)
    index.seed_item(item)
    return draft


def _seed_tool_collections(
    index: _InMemoryAssetIndex,
    *,
    user_id: str,
    collection_ids: list[str],
) -> None:
    """Seed one flat tool-collection row per id into the actor's OWNED partition.

    Required, not decorative: publish resolves each selected collection to freeze
    its tool refs onto the version, and refuses when an id has no row. Giving each
    collection a distinct tool name also lets the assertions tell the pinned refs
    apart.
    """
    now = int(time.time() * 1000)
    for collection_id in collection_ids:
        record = ToolCollection(
            asset_id=collection_id,
            category="tool-collection",
            name=f"collection-{collection_id}",
            status="draft",
            scope="private",
            s3_prefix=f"tool-collections/{collection_id}/",
            created_at=now,
            updated_at=now,
            tool_refs=[ToolRef(name=f"tool-for-{collection_id}", version="1.0")],
        )
        item = to_index_item(
            record,
            owner_user_id=user_id,
            owner_group_id=None,
            owner_org_id=None,
        )
        item["PK"] = make_pk_user_owned(user_id)
        item["SK"] = make_sk_owned("tool-collection", collection_id)
        index.seed_item(item)


def _pinned_tool_names(snapshot: SpecialtyAgent) -> list[str]:
    """Flatten a version's pinned tool refs to names, in order."""
    assert snapshot.tool_collection_snapshots is not None, (
        "a published version must pin its tool collections"
    )
    return [
        ref.name
        for snap in snapshot.tool_collection_snapshots
        for ref in snap.tool_refs
    ]


def _current_rows(lineage: list[SpecialtyAgent]) -> list[SpecialtyAgent]:
    return [r for r in lineage if r.current]


def _assert_monotonic_single_current(lineage: list[SpecialtyAgent]) -> None:
    """Versions strictly increase and exactly one row is ``current``."""
    versions = [r.version for r in lineage]
    assert all(v is not None for v in versions), lineage
    assert versions == sorted(versions), f"versions not ascending: {versions}"
    assert len(set(versions)) == len(versions), f"duplicate versions: {versions}"
    current = _current_rows(lineage)
    assert len(current) == 1, (
        f"expected exactly one current row, got {len(current)}: "
        f"{[(r.version, r.current) for r in lineage]}"
    )


# ---------------------------------------------------------------------------
# The example test
# ---------------------------------------------------------------------------


def test_extended_fields_travel_through_publish_and_rollback():
    """model / tool_collections / integration_config survive publish+rollback.

    Scenario:
      1. Draft a Custom Agent with a concrete ``model``, ``tool_collections``,
         and an enabled ``integration_config`` (issuer/audience/clientIds).
      2. Publish (v1). Assert the persisted v1 snapshot carries those exact
         extended fields (Req 8.6 — settings travel with the version).
      3. Edit the draft (different model / tools / integration config) and
         publish again (v2). Assert versions stay monotonic (1, 2) with
         exactly one ``current`` row (v2), and v2 carries the new config.
      4. Roll back (implicit → v1). Assert ``current`` points at v1 again,
         still exactly one current, and the now-current snapshot carries
         *v1's* extended fields — including the full integration_config —
         not v2's. Rollback flips only the pointer between immutable
         snapshots (Req 8.3/8.5/8.6); status is never mutated.
    """
    actor = "u-jane"
    ctx = _make_ctx(actor)
    index = _InMemoryAssetIndex()
    s3 = _NoopS3()
    crud = SpecialtyCRUD(s3=s3, index=index)

    # --- 1. Draft with v1 config --------------------------------------
    v1_model = "us.anthropic.claude-3-5-sonnet-20240620-v1:0"
    v1_tools = ["tc-alpha", "tc-beta"]
    v1_integration = IntegrationConfig(
        enabled=True,
        issuer="https://issuer-v1.example.com",
        audience="aud-v1",
        allowed_client_ids=["client-a", "client-b"],
    )
    _seed_tool_collections(index, user_id=actor, collection_ids=v1_tools)
    _seed_specialty_draft(
        index,
        user_id=actor,
        draft_id="d-agent",
        model=v1_model,
        tool_collections=v1_tools,
        integration_config=v1_integration,
    )

    # --- 2. Publish v1 -------------------------------------------------
    promoted_v1 = _run(
        crud.promote(ctx=ctx, draft_id="d-agent", scope="private")
    )
    assert isinstance(promoted_v1, Promoted)
    assert promoted_v1.version == 1
    pid = promoted_v1.published_id

    lineage = _run(index.query_lineage(pid))
    _assert_monotonic_single_current(lineage)
    v1_snapshot = next(r for r in lineage if r.version == 1)
    # The v1 snapshot carries the exact extended fields it was published with.
    assert v1_snapshot.model == v1_model
    assert v1_snapshot.tool_collections == v1_tools
    assert v1_snapshot.integration_config == v1_integration
    assert v1_snapshot.current is True
    assert v1_snapshot.status == "published"
    # The ids alone were never a snapshot — they point at mutable collections.
    # The version also pins the tool refs behind them, per collection and in
    # order, so editing or deleting a collection cannot change what v1 binds.
    assert [
        s.asset_id for s in v1_snapshot.tool_collection_snapshots
    ] == v1_tools
    assert _pinned_tool_names(v1_snapshot) == [
        "tool-for-tc-alpha", "tool-for-tc-beta",
    ]

    # --- 3. Edit the draft and publish v2 with a DIFFERENT config -----
    v2_model = "us.anthropic.claude-3-haiku-20240307-v1:0"
    v2_tools = ["tc-gamma"]
    v2_integration = IntegrationConfig(
        enabled=True,
        issuer="https://issuer-v2.example.com",
        audience="aud-v2",
        allowed_client_ids=["client-c"],
    )
    _seed_tool_collections(index, user_id=actor, collection_ids=v2_tools)
    _seed_specialty_draft(
        index,
        user_id=actor,
        draft_id="d-agent",
        model=v2_model,
        tool_collections=v2_tools,
        integration_config=v2_integration,
    )
    promoted_v2 = _run(
        crud.promote(
            ctx=ctx,
            draft_id="d-agent",
            scope="private",
            published_id=pid,
        )
    )
    assert promoted_v2.version == 2
    assert promoted_v2.published_id == pid

    lineage = _run(index.query_lineage(pid))
    _assert_monotonic_single_current(lineage)
    assert [r.version for r in lineage] == [1, 2]
    # v2 is now current and carries the new config; v1 is untouched.
    v2_snapshot = next(r for r in lineage if r.version == 2)
    v1_snapshot = next(r for r in lineage if r.version == 1)
    assert v2_snapshot.current is True
    assert v1_snapshot.current is False
    assert v2_snapshot.model == v2_model
    assert v2_snapshot.tool_collections == v2_tools
    assert v2_snapshot.integration_config == v2_integration
    assert _pinned_tool_names(v2_snapshot) == ["tool-for-tc-gamma"]
    # A later publish must never mutate the earlier immutable snapshot.
    assert v1_snapshot.model == v1_model
    assert v1_snapshot.tool_collections == v1_tools
    assert v1_snapshot.integration_config == v1_integration
    assert _pinned_tool_names(v1_snapshot) == [
        "tool-for-tc-alpha", "tool-for-tc-beta",
    ]

    # --- 4. Rollback (implicit → v1) ----------------------------------
    rolled_back = _run(crud.rollback(ctx=ctx, published_id=pid))
    assert isinstance(rolled_back, RolledBack)
    assert rolled_back.new_current_version == 1
    assert rolled_back.prior_current_version == 2

    lineage = _run(index.query_lineage(pid))
    _assert_monotonic_single_current(lineage)
    current = _current_rows(lineage)[0]
    # Current points back at v1, and the restored/current snapshot carries
    # v1's extended fields — integration_config included (Req 8.6).
    assert current.version == 1
    assert current.model == v1_model
    assert current.tool_collections == v1_tools
    assert current.integration_config == v1_integration
    # And v1's PINNED tools come back with it. Rollback only flips the current
    # pointer, so this follows from the pin being on the row — which is the
    # point: without it a rolled-back version would resolve today's collection
    # contents rather than the ones it was published with.
    assert _pinned_tool_names(current) == [
        "tool-for-tc-alpha", "tool-for-tc-beta",
    ]
    # Rollback never mutates status (Req 8.3): both snapshots stay published.
    assert current.status == "published"
    assert next(r for r in lineage if r.version == 2).status == "published"


# ---------------------------------------------------------------------------
# The pin's edges: refusal, and what must stay unpinned
# ---------------------------------------------------------------------------


def test_publish_refuses_an_agent_whose_tool_collection_is_gone():
    """Freezing "no tools" into an immutable version is worse than refusing.

    A publish cannot resolve a collection that no longer exists. Skipping it would
    write a version permanently missing those tools, which no later fix can
    reach — and the agent would answer in persona as if nothing were wrong. So the
    author is told at publish time instead, the same choice ``promote_asset``
    already makes for a draft whose artifact prefix is empty.
    """
    actor = "u-jane"
    ctx = _make_ctx(actor)
    index = _InMemoryAssetIndex()
    crud = SpecialtyCRUD(s3=_NoopS3(), index=index)

    # Only tc-present is seeded; tc-vanished names a collection that is not there.
    _seed_tool_collections(index, user_id=actor, collection_ids=["tc-present"])
    _seed_specialty_draft(
        index,
        user_id=actor,
        draft_id="d-agent",
        model=None,
        tool_collections=["tc-present", "tc-vanished"],
        integration_config=None,
    )

    with pytest.raises(ValueError, match="tc-vanished"):
        _run(crud.promote(ctx=ctx, draft_id="d-agent", scope="private"))

    # And it refused cleanly: no version row was written.
    assert _run(index.query_lineage("d-agent")) == []


def test_a_working_copy_forked_from_a_version_is_not_pinned():
    """A fork must track the live collections, not the version's frozen copy.

    ``edit_asset`` carries every other extended field forward. This one must not
    travel: a working copy exists to be edited against the collections as they are
    now, and its next publish re-resolves them. Inheriting the pin would freeze
    the draft against contents the owner can no longer change.
    """
    actor = "u-jane"
    ctx = _make_ctx(actor)
    index = _InMemoryAssetIndex()
    crud = SpecialtyCRUD(s3=_NoopS3(), index=index)

    _seed_tool_collections(index, user_id=actor, collection_ids=["tc-alpha"])
    _seed_specialty_draft(
        index,
        user_id=actor,
        draft_id="d-agent",
        model=None,
        tool_collections=["tc-alpha"],
        integration_config=None,
    )
    promoted = _run(crud.promote(ctx=ctx, draft_id="d-agent", scope="private"))

    published = next(
        r for r in _run(index.query_lineage(promoted.published_id))
        if r.version == 1
    )
    assert published.tool_collection_snapshots is not None, "version is pinned"

    # Fork the published row the way ``edit_asset`` does. Calling the builder
    # rather than the whole operation keeps the in-memory index fake honest — it
    # implements only what Promote/Rollback touch, and teaching it `put` would
    # mean reimplementing the real key derivation inside a test double.
    forked = _clone_to_working_copy(
        source=published,
        actor_id=actor,
        category="specialty-agent",
        asset_id="wc-1",
        forked_from_version=1,
        timestamp_ms=1_700_000_000_000,
    )

    # The ids travel; the pin does not.
    assert forked.tool_collections == ["tc-alpha"]
    assert forked.tool_collection_snapshots is None
