"""Tests for the canonical Rollback operation across all four CRUDs.

Covers Task 3.5's behavior:

* Implicit rollback (``to_version`` is None):
    - Lineage v1, v2, v3 (current) → rollback selects v2 (highest
      non-deprecated v < current.version).
    - Lineage v1, v2 (deprecated), v3 (current) → rollback selects v1
      (skips deprecated).
    - Lineage v1 (current) only / no prior version available →
      NotFoundOrForbidden.
* Explicit rollback (``to_version`` supplied):
    - Lineage v1, v2, v3 (current) → rollback to v1; verifies the
      transaction targets v1.
    - Rollback to deprecated version raises ValueError (Requirement
      8.4 — deprecated targets are a validation error, distinct from
      missing).
    - Rollback to missing version raises NotFoundOrForbidden.
    - Rollback to current version raises ValueError (no-op rejected).
* Lineage shape errors:
    - Empty lineage → NotFoundOrForbidden.
    - No current row in lineage → NotFoundOrForbidden.
* Authz (Property 8):
    - Actor not in publish_policy → AuthorityDenied; no transaction
      issued.
    - Contributor (not owner) is allowed.
* Status invariant (Requirement 8.3):
    - Verify the UpdateExpressions never reference the ``status``
      attribute, on either the target or the prior current row.
* TransactionCanceled → ConflictError; no S3 calls (Rollback never
  mutates S3).
* ToolCollection.rollback raises NotImplementedError (out of scope).

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5
Properties: 3 (Exactly one current per lineage), 8 (Authority gate
            independent of visibility)
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
    ConflictError,
    NotFoundOrForbidden,
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
from lab.operation_results import RolledBack  # noqa: E402
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
    to_scope: Scope = "private",
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


def _make_skill(
    *,
    scope: Scope = "private",
    user_id: str = "u-jane",
    actor_id: str = "u-jane",
    contributor_ids: Optional[list[str]] = None,
    published_id: str = "p-skill-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> Skill:
    """Build a Skill row at the requested scope.

    The S3 prefix is rendered to match the canonical grammar so the
    helper's prefix-derivation can recover the owner from it.
    """
    now = int(time.time() * 1000)
    if scope == "private":
        prefix = f"users/{user_id}/published/skills/{published_id}/v{version}/"
    elif scope == "group":
        prefix = f"groups/g-marketing/published/skills/{published_id}/v{version}/"
    else:  # org
        prefix = f"org/o-acme/published/skills/{published_id}/v{version}/"

    return Skill(
        asset_id=published_id,
        category="skill",
        name="my-skill",
        status=status,
        scope=scope,
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(
            owner_ids=[actor_id],
            contributor_ids=list(contributor_ids) if contributor_ids else [],
        ),
        scope_history=_initial_history(to_scope=scope, actor_id=actor_id),
        current=current,
        promoted_from="d-original",
    )


def _make_specialty(
    *,
    actor_id: str = "u-jane",
    user_id: str = "u-jane",
    published_id: str = "p-spec-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    prefix = (
        f"users/{user_id}/published/specialty-agents/{published_id}/v{version}/"
    )
    return SpecialtyAgent(
        asset_id=published_id,
        category="specialty-agent",
        name="my-specialty",
        status=status,
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=current,
        promoted_from="d-spec",
        compatible_skills=["sk-001"],
    )


def _make_workflow(
    *,
    actor_id: str = "u-jane",
    user_id: str = "u-jane",
    published_id: str = "p-wf-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> Workflow:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/workflows/{published_id}/v{version}/"
    return Workflow(
        asset_id=published_id,
        category="workflow",
        name="my-workflow",
        status=status,
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=current,
        promoted_from="d-wf",
        steps=[],
    )


def _make_index(lineage: list) -> MagicMock:
    """Build a MagicMock AssetIndex whose query_lineage returns ``lineage``."""
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.query_lineage = AsyncMock(return_value=lineage)
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock() -> MagicMock:
    """Build a MagicMock S3UserFilesTool. Rollback must NEVER call it."""
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


def _assert_no_status_mutation(operations: list[dict]) -> None:
    """Assert no Update operation references the ``status`` attribute.

    Requirement 8.3: Rollback never modifies status. Both the target
    Update and the prior-current Update must SET only ``current`` and
    ``updatedAt`` (and condition only on ``current``).
    """
    for op in operations:
        update = op.get("Update")
        if update is None:
            continue
        expr = update.get("UpdateExpression", "")
        cond = update.get("ConditionExpression", "")
        names = update.get("ExpressionAttributeNames", {}) or {}
        # The literal 'status' should not appear anywhere — neither in
        # the SET expression nor as a placeholder name. ``current`` and
        # ``updatedAt`` are allowed.
        assert "status" not in expr, (
            f"Rollback UpdateExpression must not touch status: {expr!r}"
        )
        assert "status" not in cond, (
            f"Rollback ConditionExpression must not touch status: {cond!r}"
        )
        for placeholder, attr in names.items():
            assert attr != "status", (
                f"Rollback ExpressionAttributeNames must not bind status: "
                f"{placeholder} -> {attr!r}"
            )


# ---------------------------------------------------------------------------
# Implicit rollback (to_version=None)
# ---------------------------------------------------------------------------


def test_implicit_rollback_selects_highest_non_deprecated_below_current():
    """Implicit rollback picks max(v < current.version ∧ status != deprecated).

    Lineage: v1, v2, v3 (current). Implicit rollback selects v2 — the
    highest non-deprecated version strictly less than the current
    version. Verifies the transaction toggles current on (target=v2,
    prior=v3) and never touches status.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=False)
    v3 = _make_skill(version=3, status="published", current=True)
    index = _make_index([v1, v2, v3])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    assert isinstance(result, RolledBack)
    assert result.published_id == "p-skill-1"
    assert result.new_current_version == 2
    assert result.prior_current_version == 3

    # Two updates: target → current=true, prior → current=false.
    index.transact_write.assert_awaited_once()
    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2

    target_update = operations[0]["Update"]
    prior_update = operations[1]["Update"]

    # Target row is v2.
    assert target_update["Key"]["SK"] == "skill#p-skill-1#v0000000002"
    assert target_update["ExpressionAttributeValues"][":true"] is True
    assert target_update["ConditionExpression"] == "#current = :false"

    # Prior current row is v3.
    assert prior_update["Key"]["SK"] == "skill#p-skill-1#v0000000003"
    assert prior_update["ExpressionAttributeValues"][":false"] is False
    assert prior_update["ConditionExpression"] == "#current = :true"

    # Status invariant (Requirement 8.3).
    _assert_no_status_mutation(operations)

    # No S3 mutations (Rollback never touches S3).
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_implicit_rollback_skips_deprecated_versions():
    """Implicit rollback skips deprecated versions when selecting the target.

    Lineage: v1 (published), v2 (deprecated), v3 (current). Implicit
    rollback should select v1, not v2 (Requirement 8.1's "highest
    non-deprecated v < current.version").
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="deprecated", current=False)
    v3 = _make_skill(version=3, status="published", current=True)
    index = _make_index([v1, v2, v3])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    assert result.new_current_version == 1
    assert result.prior_current_version == 3

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    assert operations[0]["Update"]["Key"]["SK"] == "skill#p-skill-1#v0000000001"
    assert operations[1]["Update"]["Key"]["SK"] == "skill#p-skill-1#v0000000003"

    _assert_no_status_mutation(operations)


def test_implicit_rollback_no_prior_non_deprecated_raises_not_found():
    """Implicit rollback with no eligible prior version → NotFoundOrForbidden.

    Lineage: v1 (current) only — no prior version exists. Implicit
    rollback has nothing to flip to and surfaces a not-found error
    (rather than silently returning the current pointer unchanged).
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=True)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    index.transact_write.assert_not_awaited()


def test_implicit_rollback_only_deprecated_priors_raises_not_found():
    """Lineage v1 (deprecated), v2 (current) → no eligible prior; not-found."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="deprecated", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Explicit rollback (to_version supplied)
# ---------------------------------------------------------------------------


def test_explicit_rollback_to_specific_version():
    """Explicit rollback to v1 with v3 currently current.

    Lineage: v1, v2, v3 (current). ``rollback(to_version=1)`` should
    target v1 — even though v2 is the implicit choice — and toggle
    current accordingly.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=False)
    v3 = _make_skill(version=3, status="published", current=True)
    index = _make_index([v1, v2, v3])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.rollback(ctx=ctx, published_id="p-skill-1", to_version=1)
    )

    assert result.new_current_version == 1
    assert result.prior_current_version == 3

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    assert operations[0]["Update"]["Key"]["SK"] == "skill#p-skill-1#v0000000001"
    assert operations[1]["Update"]["Key"]["SK"] == "skill#p-skill-1#v0000000003"

    _assert_no_status_mutation(operations)


def test_explicit_rollback_to_deprecated_raises_value_error():
    """Requirement 8.4: explicit rollback to a deprecated version is rejected.

    Lineage: v1 (deprecated), v2 (current). ``to_version=1`` is a
    validation error, distinct from a missing version (which surfaces
    NotFoundOrForbidden).
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="deprecated", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError, match="deprecated"):
        _run(
            crud.rollback(ctx=ctx, published_id="p-skill-1", to_version=1)
        )

    index.transact_write.assert_not_awaited()


def test_explicit_rollback_to_missing_version_raises_not_found():
    """Requirement 8.4: explicit rollback to a missing version → not-found."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.rollback(ctx=ctx, published_id="p-skill-1", to_version=99)
        )

    index.transact_write.assert_not_awaited()


def test_explicit_rollback_to_current_version_raises_value_error():
    """Rollback to the already-current version is a no-op → ValueError."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ValueError, match="equals current"):
        _run(
            crud.rollback(ctx=ctx, published_id="p-skill-1", to_version=2)
        )

    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Lineage shape errors
# ---------------------------------------------------------------------------


def test_rollback_empty_lineage_raises_not_found():
    """Empty lineage → NotFoundOrForbidden (no existence disclosure)."""
    ctx = _make_ctx(actor_id="u-jane")
    index = _make_index([])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(crud.rollback(ctx=ctx, published_id="p-nope"))

    index.transact_write.assert_not_awaited()


def test_rollback_no_current_row_raises_not_found():
    """Lineage with no current pointer cannot be rolled back → not-found."""
    ctx = _make_ctx(actor_id="u-jane")
    # Both rows deprecated, neither marked current — typical post-state
    # of deprecating the only published version. Rollback has no
    # anchor to flip away from.
    v1 = _make_skill(version=1, status="deprecated", current=False)
    v2 = _make_skill(version=2, status="deprecated", current=False)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden, match="no current"):
        _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Authz (Requirement 8.5, Property 8)
# ---------------------------------------------------------------------------


def test_rollback_actor_not_in_publish_policy_raises_authority_denied():
    """Property 8: an actor not in publish_policy is denied; no transaction.

    The authority check runs against the *current* row's
    publish_policy (per the design's Rollback section), and rejects
    BEFORE any DDB write.
    """
    ctx = _make_ctx(actor_id="u-bob")
    # publish_policy lists u-jane only; u-bob is not authorized.
    v1 = _make_skill(actor_id="u-jane", version=1, current=False)
    v2 = _make_skill(actor_id="u-jane", version=2, current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(AuthorityDenied):
        _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    index.transact_write.assert_not_awaited()
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_rollback_contributor_is_authorized():
    """A contributor (not owner) may roll back — covers Requirement 8.5."""
    ctx = _make_ctx(actor_id="u-bob")
    v1 = _make_skill(
        actor_id="u-jane",
        contributor_ids=["u-bob"],
        version=1,
        current=False,
    )
    v2 = _make_skill(
        actor_id="u-jane",
        contributor_ids=["u-bob"],
        version=2,
        current=True,
    )
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    assert isinstance(result, RolledBack)
    assert result.new_current_version == 1
    assert result.prior_current_version == 2
    index.transact_write.assert_awaited_once()


# ---------------------------------------------------------------------------
# Category routing (specialty / workflow happy paths)
# ---------------------------------------------------------------------------


def test_rollback_specialty_happy_path():
    """Rollback works against SpecialtyAgent rows (category routing)."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_specialty(version=1, status="published", current=False)
    v2 = _make_specialty(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    result = _run(crud.rollback(ctx=ctx, published_id="p-spec-1"))

    assert result.new_current_version == 1
    assert result.prior_current_version == 2

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    assert operations[0]["Update"]["Key"]["SK"] == (
        "specialty-agent#p-spec-1#v0000000001"
    )
    assert operations[1]["Update"]["Key"]["SK"] == (
        "specialty-agent#p-spec-1#v0000000002"
    )
    _assert_no_status_mutation(operations)


def test_rollback_workflow_happy_path():
    """Rollback works against Workflow rows (category routing)."""
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_workflow(version=1, status="published", current=False)
    v2 = _make_workflow(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    s3 = _make_s3_mock()

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(crud.rollback(ctx=ctx, published_id="p-wf-1"))

    assert result.new_current_version == 1
    assert result.prior_current_version == 2

    operations = index.transact_write.await_args.args[0]
    assert len(operations) == 2
    assert operations[0]["Update"]["Key"]["SK"] == "workflow#p-wf-1#v0000000001"
    assert operations[1]["Update"]["Key"]["SK"] == "workflow#p-wf-1#v0000000002"
    _assert_no_status_mutation(operations)


# ---------------------------------------------------------------------------
# Conflict on transaction cancel
# ---------------------------------------------------------------------------


def test_rollback_transaction_canceled_raises_conflict_no_s3_calls():
    """When the DDB transaction cancels, surface ConflictError; no S3 calls.

    Rollback has no S3 mutations, so there is no compensating action.
    The operation simply re-raises ConflictError carrying the
    cancellation reasons.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1, status="published", current=False)
    v2 = _make_skill(version=2, status="published", current=True)
    index = _make_index([v1, v2])
    index.transact_write = AsyncMock(side_effect=_txn_canceled_error())
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(ConflictError) as exc_info:
        _run(crud.rollback(ctx=ctx, published_id="p-skill-1"))

    assert exc_info.value.cancellation_reasons
    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tool collections — out of scope in this sprint
# ---------------------------------------------------------------------------


def test_tool_collection_rollback_raises_not_implemented():
    """Tool collections have no per-version rollback in this sprint."""
    ctx = _make_ctx(actor_id="u-jane")
    s3 = _make_s3_mock()
    index = MagicMock()
    gateway = MagicMock(spec=GatewayBrowser)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=gateway)
    with pytest.raises(NotImplementedError):
        _run(crud.rollback(ctx=ctx, published_id="p-tc"))
