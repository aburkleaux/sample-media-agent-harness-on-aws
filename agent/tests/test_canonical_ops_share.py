"""Tests for the canonical Share operation across all four CRUDs.

Covers Task 3.2's behavior:

* Happy-path Share per scope transition (private → group, private → org,
  group → org). For each:
    - the S3 ``copy_prefix_absolute`` is called with (source → target)
    - the AssetIndex transaction is a Delete-source + Put-target pair
      with the same SK on both rows (only the PK differs)
    - the new row's ``scopeHistory`` is the source's history with one
      appended entry recording (from, to, actor_id, timestamp, group_ids?)
    - ``publishPolicy``, ``current``, ``promotedFrom``, and the
      category-specific payload survive the move unchanged
    - the post-transaction ``delete_prefix_absolute(source_prefix)`` runs
* Scope-ordering rejection (Property 5):
    - private → private, group → group, group → private, org → group all
      raise :class:`ScopeNarrowingError` BEFORE any S3 or AssetIndex
      mutation
* Validation error: ``new_scope='group'`` without ``group_ids`` ⇒
  :class:`ValueError`
* Authz: actor not in source.publish_policy.{owner_ids ∪ contributor_ids}
  ⇒ :class:`AuthorityDenied`
* Source row missing ⇒ :class:`NotFoundOrForbidden`
* DDB ``TransactionCanceledException`` ⇒ :class:`ConflictError` and the
  compensating ``delete_prefix_absolute(target_prefix)`` runs
* Compensating delete itself fails ⇒ :class:`CompensationFailedError`
  carrying the orphan target prefix
* Post-transaction source-prefix delete failure does NOT fail the
  operation; the source orphan is logged but Share returns successfully
* ToolCollection share raises :class:`NotImplementedError`

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.1, 13.2, 13.3,
           13.4
Properties: 5 (Share monotonically widens), 6 (ScopeHistory
            append-only), 8 (Authority gate independent of visibility)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.errors import (  # noqa: E402
    AuthorityDenied,
    CompensationFailedError,
    ConflictError,
    NotFoundOrForbidden,
    ScopeNarrowingError,
)
from lab.gateway_browser import GatewayBrowser  # noqa: E402
from lab.models import (  # noqa: E402
    PublishPolicy,
    Scope,
    ScopeHistoryEntry,
    Skill,
    SpecialtyAgent,
    Workflow,
)
from lab.operation_results import Shared  # noqa: E402
from lab.skill_crud import SkillCRUD  # noqa: E402
from lab.specialty_crud import SpecialtyCRUD  # noqa: E402
from lab.tool_collection_crud import ToolCollectionCRUD  # noqa: E402
from lab.workflow_crud import WorkflowCRUD  # noqa: E402


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(actor_id: str = "u-jane") -> LabRequestContext:
    """Build a LabRequestContext with the given actor."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
    )


def _initial_history(
    *,
    to_scope: Scope,
    actor_id: str = "u-jane",
    group_ids: Optional[list[str]] = None,
) -> list[ScopeHistoryEntry]:
    """Render the single-entry history a Promote would have produced."""
    return [
        ScopeHistoryEntry(
            from_scope=None,
            to_scope=to_scope,
            actor_id=actor_id,
            timestamp="2024-01-01T00:00:00.000000Z",
            group_ids=list(group_ids) if to_scope == "group" and group_ids else None,
        )
    ]


def _make_published_skill(
    *,
    scope: Scope = "private",
    user_id: str = "u-jane",
    group_id: str = "g-marketing",
    org_id: str = "o-acme",
    published_id: str = "p-skill-1",
    version: int = 1,
    actor_id: str = "u-jane",
    contributor_ids: Optional[list[str]] = None,
    history: Optional[list[ScopeHistoryEntry]] = None,
) -> Skill:
    """Build a published Skill row at the given scope.

    The S3 prefix is rendered to match the canonical grammar so the
    helper's prefix-derivation can recover the owner from it.
    """
    now = int(time.time() * 1000)
    if scope == "private":
        prefix = f"users/{user_id}/published/skills/{published_id}/v{version}/"
    elif scope == "group":
        prefix = f"groups/{group_id}/published/skills/{published_id}/v{version}/"
    elif scope == "org":
        prefix = f"org/{org_id}/published/skills/{published_id}/v{version}/"
    else:  # pragma: no cover
        raise ValueError(scope)

    return Skill(
        asset_id=published_id,
        category="skill",
        name="my-skill",
        status="published",
        scope=scope,
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(
            owner_ids=[actor_id],
            contributor_ids=list(contributor_ids) if contributor_ids else [],
        ),
        scope_history=history if history is not None else _initial_history(
            to_scope=scope,
            actor_id=actor_id,
            group_ids=[group_id] if scope == "group" else None,
        ),
        current=True,
        promoted_from="d-original",
    )


def _make_published_specialty(
    *,
    scope: Scope = "private",
    user_id: str = "u-jane",
    actor_id: str = "u-jane",
    published_id: str = "p-spec-1",
    compatible_skills: Optional[list[str]] = None,
    tool_collections: Optional[list[str]] = None,
    tool_collection_snapshots=None,
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/specialty-agents/{published_id}/v1/"
    return SpecialtyAgent(
        asset_id=published_id,
        category="specialty-agent",
        name="my-specialty",
        status="published",
        scope=scope,
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=1,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(to_scope=scope, actor_id=actor_id),
        current=True,
        promoted_from="d-spec",
        compatible_skills=list(compatible_skills) if compatible_skills else ["sk-001"],
        tool_collections=list(tool_collections) if tool_collections else [],
        tool_collection_snapshots=tool_collection_snapshots,
    )


def _make_published_workflow(
    *,
    scope: Scope = "private",
    user_id: str = "u-jane",
    actor_id: str = "u-jane",
    published_id: str = "p-wf-1",
) -> Workflow:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/workflows/{published_id}/v1/"
    return Workflow(
        asset_id=published_id,
        category="workflow",
        name="my-workflow",
        status="published",
        scope=scope,
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=1,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(to_scope=scope, actor_id=actor_id),
        current=True,
        promoted_from="d-wf",
        steps=[],
    )


def _make_index_with_source(source) -> MagicMock:
    """Build a MagicMock AssetIndex whose query_lineage returns ``source``."""
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.query_lineage = AsyncMock(return_value=[source])
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock() -> MagicMock:
    """Build a MagicMock S3UserFilesTool that succeeds at copy and delete."""
    s3 = MagicMock()
    s3.copy_prefix_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    return s3


def _txn_canceled_error() -> ClientError:
    """Build a botocore ClientError that mimics a TransactWriteItems cancel."""
    response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled, please refer to "
            "cancellation reasons for specific reasons",
        },
        "CancellationReasons": [
            {"Code": "ConditionalCheckFailed"},
            {"Code": "None"},
        ],
    }
    return ClientError(response, "TransactWriteItems")


# ---------------------------------------------------------------------------
# Happy-path scope transitions
# ---------------------------------------------------------------------------


def test_share_skill_private_to_group_happy_path():
    """Sharing a private Skill to group performs the cross-PK move.

    Verifies:
      * ``Shared`` result carries the unchanged ``(published_id,
        version)`` plus the new group-scoped prefix and ``scope='group'``
      * S3 copy uses (source → target)
      * Transaction has Delete source + Put target
      * Both ops carry the same SK; PKs differ (USER vs GROUP)
      * Delete carries a ``scope = :old_scope`` precondition
      * Put carries ``attribute_not_exists(PK)`` and the new row has
        scope=group, s3Prefix=target, scopeHistory appended with
        from=private/to=group/groupIds=[g-marketing]
      * publish_policy, current, promotedFrom survive unchanged
      * Post-transaction source-prefix delete runs
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(
        scope="private", user_id="u-jane", published_id="p-skill-1"
    )
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.share(
            ctx=ctx,
            published_id="p-skill-1",
            version=1,
            new_scope="group",
            group_ids=["g-marketing"],
        )
    )

    # Result
    assert isinstance(result, Shared)
    assert result.published_id == "p-skill-1"
    assert result.version == 1
    assert result.scope == "group"
    assert result.s3_prefix == (
        "groups/g-marketing/published/skills/p-skill-1/v1/"
    )

    # Source row was looked up via the lineage query.
    index.query_lineage.assert_awaited_once_with("p-skill-1")

    # S3 copy: source → target.
    s3.copy_prefix_absolute.assert_awaited_once_with(
        source.s3_prefix, result.s3_prefix
    )

    # Transaction: Delete + Put with same SK, different PKs.
    index.transact_write.assert_awaited_once()
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    delete_op = operations[0]["Delete"]
    put_op = operations[1]["Put"]

    expected_sk = "skill#p-skill-1#v0000000001"
    assert delete_op["Key"] == {
        "PK": "USER#u-jane#VERSIONS",
        "SK": expected_sk,
    }
    assert delete_op["ConditionExpression"] == "#scope = :old_scope"
    assert delete_op["ExpressionAttributeNames"] == {"#scope": "scope"}
    assert delete_op["ExpressionAttributeValues"] == {":old_scope": "private"}

    new_item = put_op["Item"]
    assert put_op["ConditionExpression"] == "attribute_not_exists(PK)"
    assert new_item["PK"] == "GROUP#g-marketing#SHARED"
    assert new_item["SK"] == expected_sk
    assert new_item["GSI1PK"] == "PUB#p-skill-1"
    assert new_item["GSI1SK"] == "v0000000001"
    assert new_item["scope"] == "group"
    assert new_item["s3Prefix"] == result.s3_prefix
    assert new_item["ownerGroupId"] == "g-marketing"
    assert "ownerUserId" not in new_item

    # ScopeHistory: original entry + appended group entry.
    history = new_item["scopeHistory"]
    assert len(history) == 2
    assert history[0]["from"] is None
    assert history[0]["to"] == "private"
    assert history[1]["from"] == "private"
    assert history[1]["to"] == "group"
    assert history[1]["actorId"] == "u-jane"
    assert history[1]["groupIds"] == ["g-marketing"]
    assert "timestamp" in history[1]

    # publish_policy, current, promotedFrom survive.
    assert new_item["publishPolicy"] == {
        "ownerIds": ["u-jane"],
        "contributorIds": [],
    }
    assert new_item["current"] is True
    assert new_item["promotedFrom"] == "d-original"

    # Post-transaction source-prefix delete.
    s3.delete_prefix_absolute.assert_awaited_once_with(source.s3_prefix)


def test_share_specialty_private_to_org_happy_path():
    """Sharing a private SpecialtyAgent to org moves to the org partition.

    Verifies the org-partition routing, the appended history entry, and
    that the category-specific payload (``compatibleSkills``) survives.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_specialty(
        scope="private",
        user_id="u-jane",
        published_id="p-spec-1",
        compatible_skills=["sk-001", "sk-002"],
    )
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    result = _run(
        crud.share(
            ctx=ctx,
            published_id="p-spec-1",
            version=1,
            new_scope="org",
            org_id="o-acme",
        )
    )

    assert result.scope == "org"
    assert result.s3_prefix == (
        "org/o-acme/published/specialty-agents/p-spec-1/v1/"
    )

    operations = index.transact_write.await_args.args[0]
    new_item = operations[1]["Put"]["Item"]
    assert new_item["PK"] == "ORG#o-acme#SHARED"
    assert new_item["ownerOrgId"] == "o-acme"
    assert "ownerUserId" not in new_item
    assert "ownerGroupId" not in new_item

    # Specialty-specific payload survives.
    assert new_item["compatibleSkills"] == ["sk-001", "sk-002"]

    # History appended; group_ids omitted because new scope is org.
    history = new_item["scopeHistory"]
    assert len(history) == 2
    assert history[1]["from"] == "private"
    assert history[1]["to"] == "org"
    assert "groupIds" not in history[1]


def test_share_carries_the_versions_pinned_tool_collections():
    """Load-bearing on this path specifically, not just tidy.

    A shared version becomes reachable by actors who cannot see the owner's
    private tool-collection rows at all. Dropping the pin here would leave every
    one of them holding ids nothing can resolve — the agent would answer in
    persona with no tools, for everyone but the owner.
    """
    from lab.models import ToolCollectionSnapshot, ToolRef

    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_specialty(
        scope="private",
        user_id="u-jane",
        published_id="p-spec-1",
        tool_collections=["tc-1"],
        tool_collection_snapshots=[
            ToolCollectionSnapshot(
                asset_id="tc-1",
                name="media-tools",
                tool_refs=[ToolRef(name="get_weather", version="1.0")],
            )
        ],
    )
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    _run(
        crud.share(
            ctx=ctx,
            published_id="p-spec-1",
            version=1,
            new_scope="org",
            org_id="o-acme",
        )
    )

    operations = index.transact_write.await_args.args[0]
    new_item = operations[1]["Put"]["Item"]
    assert new_item["toolCollections"] == ["tc-1"]
    assert new_item["toolCollectionSnapshots"] == [
        {
            "assetId": "tc-1",
            "name": "media-tools",
            "toolRefs": [{"name": "get_weather", "version": "1.0"}],
        }
    ]


def test_share_workflow_group_to_org_happy_path():
    """Sharing a group-scoped Workflow to org widens correctly.

    Verifies group → org transition: the source PK is GROUP#..., the
    target PK is ORG#..., the Delete precondition checks
    ``scope = "group"``.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_workflow(scope="private", actor_id="u-jane")
    # Override to simulate a group-scoped source.
    source = Workflow(
        asset_id="p-wf-1",
        category="workflow",
        name="my-workflow",
        status="published",
        scope="group",
        s3_prefix="groups/g-marketing/published/workflows/p-wf-1/v1/",
        created_at=source.created_at,
        updated_at=source.updated_at,
        version=1,
        publish_policy=PublishPolicy(owner_ids=["u-jane"]),
        scope_history=_initial_history(
            to_scope="group", group_ids=["g-marketing"]
        ),
        current=True,
        promoted_from="d-wf",
        steps=[],
    )
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.share(
            ctx=ctx,
            published_id="p-wf-1",
            version=1,
            new_scope="org",
            org_id="o-acme",
        )
    )

    assert result.s3_prefix == "org/o-acme/published/workflows/p-wf-1/v1/"

    operations = index.transact_write.await_args.args[0]
    delete_op = operations[0]["Delete"]
    put_op = operations[1]["Put"]

    assert delete_op["Key"]["PK"] == "GROUP#g-marketing#SHARED"
    assert delete_op["ExpressionAttributeValues"] == {":old_scope": "group"}
    assert put_op["Item"]["PK"] == "ORG#o-acme#SHARED"

    history = put_op["Item"]["scopeHistory"]
    assert history[1]["from"] == "group"
    assert history[1]["to"] == "org"


# ---------------------------------------------------------------------------
# Scope-narrowing rejection (Property 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_scope,new_scope",
    [
        ("private", "private"),  # no-op same scope
        ("group", "group"),  # no-op same scope
        ("org", "org"),  # no-op same scope
        ("group", "private"),  # narrowing
        ("org", "group"),  # narrowing
        ("org", "private"),  # narrowing
    ],
)
def test_share_rejects_non_widening_with_scope_narrowing_error(
    source_scope: Scope, new_scope: Scope
):
    """Any non-widening Share raises :class:`ScopeNarrowingError`.

    Covers Property 5 — the strict ordering ``private < group < org``
    rejects equal-scope (no-op) shares as well as narrower targets.
    Verifies no S3 or AssetIndex mutation is attempted.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope=source_scope, actor_id="u-jane")
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ScopeNarrowingError) as exc_info:
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=1,
                new_scope=new_scope,
                # Provide group_ids/org_id so the validation reaches the
                # scope-ordering check rather than failing on missing args.
                group_ids=["g-marketing"] if new_scope == "group" else None,
                org_id="o-acme" if new_scope == "org" else None,
            )
        )
    assert exc_info.value.from_scope == source_scope
    assert exc_info.value.to_scope == new_scope

    # No S3 copy, no transaction issued.
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_share_group_scope_without_group_ids_raises_value_error():
    """``new_scope='group'`` without ``group_ids`` is a validation error.

    Verified BEFORE any S3 or AssetIndex mutation. Note: the helper
    validates scope ordering first, so the source must be ``private`` so
    the narrowing check passes and we reach the group-ids check.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope="private", actor_id="u-jane")
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError):
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=1,
                new_scope="group",
                group_ids=None,
            )
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Authz / not-found
# ---------------------------------------------------------------------------


def test_share_actor_not_in_publish_policy_raises_authority_denied():
    """An actor not in publishPolicy.{owner_ids ∪ contributor_ids} is denied.

    Property 8: authority is independent of visibility — the actor may
    be able to read the row at its current scope but is still denied a
    Share. Verifies no S3 or AssetIndex mutation.
    """
    ctx = _make_ctx(actor_id="u-bob")
    # Source's publish_policy lists u-jane only; u-bob is not authorized.
    source = _make_published_skill(scope="private", actor_id="u-jane")
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(AuthorityDenied):
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=1,
                new_scope="group",
                group_ids=["g-marketing"],
            )
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_share_actor_in_contributor_ids_is_authorized():
    """A contributor (not owner) may Share — covers Requirement 5.6.

    Verifies the union ``owner_ids ∪ contributor_ids`` is honoured.
    """
    ctx = _make_ctx(actor_id="u-bob")
    source = _make_published_skill(
        scope="private",
        actor_id="u-jane",
        contributor_ids=["u-bob"],
    )
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.share(
            ctx=ctx,
            published_id=source.asset_id,
            version=1,
            new_scope="org",
            org_id="o-acme",
        )
    )
    assert result.scope == "org"
    operations = index.transact_write.await_args.args[0]
    new_item = operations[1]["Put"]["Item"]
    # The appended scopeHistory entry records u-bob as the actor.
    assert new_item["scopeHistory"][1]["actorId"] == "u-bob"


def test_share_source_not_found_raises_not_found_or_forbidden():
    """When ``query_lineage`` returns nothing, surface NotFoundOrForbidden."""
    ctx = _make_ctx(actor_id="u-jane")
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.query_lineage = AsyncMock(return_value=[])
    index.transact_write = AsyncMock()
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.share(
                ctx=ctx,
                published_id="p-nope",
                version=1,
                new_scope="org",
                org_id="o-acme",
            )
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_share_version_not_in_lineage_raises_not_found_or_forbidden():
    """Lineage exists but the requested version is missing ⇒ not-found."""
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope="private", version=1, actor_id="u-jane")
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=2,  # only v1 exists
                new_scope="org",
                org_id="o-acme",
            )
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compensating-action edge cases
# ---------------------------------------------------------------------------


def test_share_transaction_canceled_runs_compensating_delete_of_target():
    """When the DDB transaction cancels, the target prefix is cleaned up
    and ``ConflictError`` surfaces. The source prefix is left intact
    (AssetIndex still resolves it).
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope="private", actor_id="u-jane")
    index = _make_index_with_source(source)
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ConflictError) as exc_info:
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=1,
                new_scope="org",
                org_id="o-acme",
            )
        )

    assert exc_info.value.cancellation_reasons
    s3.copy_prefix_absolute.assert_awaited_once()
    # Compensating delete targets the freshly-copied target prefix.
    target_prefix = s3.copy_prefix_absolute.await_args.args[1]
    s3.delete_prefix_absolute.assert_awaited_once_with(target_prefix)


def test_share_compensation_failure_raises_compensation_failed_error():
    """When the txn cancels AND the compensating delete fails, surface
    :class:`CompensationFailedError` with the orphan target prefix.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope="private", actor_id="u-jane")
    index = _make_index_with_source(source)
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())

    s3 = _make_s3_mock()
    s3.delete_prefix_absolute = AsyncMock(
        side_effect=RuntimeError("S3 unreachable")
    )

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(CompensationFailedError) as exc_info:
        _run(
            crud.share(
                ctx=ctx,
                published_id=source.asset_id,
                version=1,
                new_scope="org",
                org_id="o-acme",
            )
        )

    err = exc_info.value
    assert err.orphan_prefix.startswith("org/o-acme/published/skills/")
    assert err.orphan_prefix.endswith("/v1/")
    assert isinstance(err.compensation_error, RuntimeError)
    assert isinstance(err.original_error, ClientError)


def test_share_post_txn_source_delete_failure_does_not_fail_operation():
    """Per the design's "Failure modes" section: if AssetIndex commit
    succeeds but the post-transaction source-prefix delete fails, the
    operation still returns successfully. The orphan source prefix is
    logged and reclaimed by a janitor follow-on.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_published_skill(scope="private", actor_id="u-jane")
    index = _make_index_with_source(source)
    s3 = _make_s3_mock()

    # First call (target copy) succeeds; second call (source delete) fails.
    s3.delete_prefix_absolute = AsyncMock(
        side_effect=RuntimeError("source delete failed")
    )

    crud = SkillCRUD(s3=s3, index=index)
    # Operation should NOT raise.
    result = _run(
        crud.share(
            ctx=ctx,
            published_id=source.asset_id,
            version=1,
            new_scope="org",
            org_id="o-acme",
        )
    )

    assert isinstance(result, Shared)
    assert result.scope == "org"
    # The transaction did commit (this is the success branch).
    index.transact_write.assert_awaited_once()
    # And the helper attempted the source delete (which failed but was
    # swallowed).
    s3.delete_prefix_absolute.assert_awaited_once_with(source.s3_prefix)


# ---------------------------------------------------------------------------
# Tool collections — out of scope in this sprint
# ---------------------------------------------------------------------------


def test_tool_collection_share_raises_not_implemented():
    """Tool collections have no cross-scope Share in this sprint."""
    ctx = _make_ctx(actor_id="u-jane")
    s3 = _make_s3_mock()
    index = MagicMock()
    gateway = MagicMock(spec=GatewayBrowser)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=gateway)
    with pytest.raises(NotImplementedError):
        _run(
            crud.share(
                ctx=ctx,
                published_id="p-tc",
                version=1,
                new_scope="org",
                org_id="o-acme",
            )
        )
