"""Tests for the canonical Clone operation across all four CRUDs.

Covers Task 3.4's behavior:

* Happy paths:
    - Skill private published → clone produces draft under the actor's
      drafts partition; verifies S3 copy_prefix runs from source to a
      ``users/<actor>/drafts/skills/<draft_id>/`` target, AssetIndex
      put with status="draft", cloned_from=(pid, version),
      promoted_from=None, version=None, scope="private",
      publish_policy=None.
    - SpecialtyAgent: compatible_skills survive the clone.
    - Workflow: steps survive the clone.
    - Different actor cloning a private published row: visibility is
      delegated to the handler, so the CRUD reads the source row
      directly and produces a draft under the cloning actor's prefix.
* Source not found:
    - Lineage empty ⇒ NotFoundOrForbidden.
    - Version missing from lineage ⇒ NotFoundOrForbidden.
* Compensating actions:
    - AssetIndex put failure ⇒ delete_prefix_absolute(target) runs and
      the original error is re-raised.
    - AssetIndex put failure + compensation failure ⇒
      CompensationFailedError carrying the orphan prefix.
* ToolCollection clone: raises NotImplementedError up-front (no S3 or
  AssetIndex calls).

Validates: Requirements 7.1, 7.2, 7.3, 7.4
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.errors import (  # noqa: E402
    CompensationFailedError,
    NotFoundOrForbidden,
)
from lab.gateway_browser import GatewayBrowser  # noqa: E402
from lab.models import (  # noqa: E402
    AgentRef,
    PublishPolicy,
    Scope,
    ScopeHistoryEntry,
    Skill,
    SkillRef,
    SpecialtyAgent,
    ToolCollectionRef,
    Workflow,
    WorkflowStep,
)
from lab.operation_results import Cloned  # noqa: E402
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
    published_id: str = "p-skill-1",
    version: int = 1,
    status: str = "published",
    current: bool = True,
) -> Skill:
    """Build a Skill row at a given scope with a canonical s3_prefix."""
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
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
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
    compatible_skills: Optional[list[str]] = None,
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/specialty-agents/{published_id}/v{version}/"
    return SpecialtyAgent(
        asset_id=published_id,
        category="specialty-agent",
        name="my-specialty",
        status="published",
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=True,
        promoted_from="d-spec",
        compatible_skills=list(compatible_skills) if compatible_skills else ["sk-001", "sk-002"],
    )


def _make_workflow(
    *,
    actor_id: str = "u-jane",
    user_id: str = "u-jane",
    published_id: str = "p-wf-1",
    version: int = 1,
    steps: Optional[list[WorkflowStep]] = None,
) -> Workflow:
    now = int(time.time() * 1000)
    prefix = f"users/{user_id}/published/workflows/{published_id}/v{version}/"
    if steps is None:
        steps = [
            WorkflowStep(
                step_id="s1",
                agent=AgentRef(kind="core", ref="task-agent"),
                skill=SkillRef(asset_id=None),
                tool_collection=ToolCollectionRef(asset_id=None),
                prompt="Do thing one",
                inputs=[],
            ),
            WorkflowStep(
                step_id="s2",
                agent=AgentRef(kind="core", ref="task-agent"),
                skill=SkillRef(asset_id=None),
                tool_collection=ToolCollectionRef(asset_id=None),
                prompt="Then thing two",
                inputs=["s1"],
            ),
        ]
    return Workflow(
        asset_id=published_id,
        category="workflow",
        name="my-workflow",
        status="published",
        scope="private",
        s3_prefix=prefix,
        created_at=now,
        updated_at=now,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[actor_id]),
        scope_history=_initial_history(actor_id=actor_id),
        current=True,
        promoted_from="d-wf",
        steps=list(steps),
    )


def _make_index(lineage: list) -> MagicMock:
    """Build a MagicMock AssetIndex whose query_lineage returns ``lineage``.

    ``put`` is an AsyncMock that succeeds by default. ``transact_write``
    is also stubbed but Clone never calls it.
    """
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.query_lineage = AsyncMock(return_value=lineage)
    index.put = AsyncMock()
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock() -> MagicMock:
    """Build a MagicMock S3UserFilesTool with the absolute helpers stubbed."""
    s3 = MagicMock()
    s3.copy_prefix_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()
    return s3


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_clone_skill_private_published_produces_actor_drafts_row():
    """Clone a private published Skill_Record into the actor's drafts partition.

    Verifies:
      * S3 copy_prefix_absolute runs from the source's prefix to a
        canonical ``users/<actor>/drafts/skills/<draft_id>/`` target.
      * AssetIndex.put is called with a Skill row carrying status="draft",
        scope="private", version=None, publish_policy=None,
        promoted_from=None, and cloned_from=(pid, version).
      * The returned :class:`Cloned` echoes the new draft_id and target
        prefix, and ``cloned_from`` matches the source.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_skill(
        user_id="u-jane",
        actor_id="u-jane",
        published_id="p-skill-1",
        version=3,
    )
    index = _make_index([source])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.clone(ctx=ctx, published_id="p-skill-1", version=3)
    )

    assert isinstance(result, Cloned)
    assert result.cloned_from == ("p-skill-1", 3)
    # Target prefix is canonical and bound to the cloning actor.
    assert result.s3_prefix == (
        f"users/u-jane/drafts/skills/{result.draft_id}/"
    )

    # S3 copy_prefix_absolute called with source.s3_prefix → target_prefix.
    s3.copy_prefix_absolute.assert_awaited_once()
    args, _ = s3.copy_prefix_absolute.await_args
    assert args[0] == source.s3_prefix
    assert args[1] == result.s3_prefix

    # Compensating delete must NOT have run on the happy path.
    s3.delete_prefix_absolute.assert_not_awaited()

    # AssetIndex.put called once with the new draft row.
    index.put.assert_awaited_once()
    put_args, put_kwargs = index.put.await_args
    new_draft = put_args[0]
    assert put_kwargs.get("owner_user_id") == "u-jane"
    assert isinstance(new_draft, Skill)
    assert new_draft.asset_id == result.draft_id
    assert new_draft.category == "skill"
    assert new_draft.name == source.name
    assert new_draft.status == "draft"
    assert new_draft.scope == "private"
    assert new_draft.version is None
    assert new_draft.publish_policy is None
    assert new_draft.scope_history == []
    assert new_draft.current is False
    assert new_draft.promoted_from is None
    assert new_draft.last_published_as is None
    assert new_draft.cloned_from == ("p-skill-1", 3)
    assert new_draft.s3_prefix == result.s3_prefix

    # Clone is a single-row insert; transact_write must not be used.
    index.transact_write.assert_not_awaited()


def test_clone_specialty_preserves_compatible_skills():
    """SpecialtyAgent clone carries the source's compatible_skills payload."""
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_specialty(
        actor_id="u-jane",
        user_id="u-jane",
        compatible_skills=["sk-alpha", "sk-beta", "sk-gamma"],
    )
    index = _make_index([source])
    s3 = _make_s3_mock()

    crud = SpecialtyCRUD(s3=s3, index=index)
    result = _run(
        crud.clone(ctx=ctx, published_id="p-spec-1", version=1)
    )

    assert result.cloned_from == ("p-spec-1", 1)
    assert result.s3_prefix == (
        f"users/u-jane/drafts/specialty-agents/{result.draft_id}/"
    )

    index.put.assert_awaited_once()
    new_draft = index.put.await_args.args[0]
    assert isinstance(new_draft, SpecialtyAgent)
    assert new_draft.compatible_skills == ["sk-alpha", "sk-beta", "sk-gamma"]
    # Verify list independence — mutating the source must not bleed through.
    source.compatible_skills.append("sk-mutated")
    assert "sk-mutated" not in new_draft.compatible_skills


def test_clone_workflow_preserves_steps():
    """Workflow clone carries the source's ordered steps payload."""
    ctx = _make_ctx(actor_id="u-jane")
    custom_steps = [
        WorkflowStep(
            step_id="alpha",
            agent=AgentRef(kind="specialty", asset_id="sa-001"),
            skill=SkillRef(asset_id="sk-001"),
            tool_collection=ToolCollectionRef(asset_id=None),
            prompt="Step alpha",
            inputs=[],
        ),
        WorkflowStep(
            step_id="beta",
            agent=AgentRef(kind="core", ref="task-agent"),
            skill=SkillRef(asset_id=None),
            tool_collection=ToolCollectionRef(asset_id=None),
            prompt="Step beta",
            inputs=["alpha"],
        ),
    ]
    source = _make_workflow(
        actor_id="u-jane",
        user_id="u-jane",
        steps=custom_steps,
    )
    index = _make_index([source])
    s3 = _make_s3_mock()

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.clone(ctx=ctx, published_id="p-wf-1", version=1)
    )

    assert result.cloned_from == ("p-wf-1", 1)
    assert result.s3_prefix == (
        f"users/u-jane/drafts/workflows/{result.draft_id}/"
    )

    index.put.assert_awaited_once()
    new_draft = index.put.await_args.args[0]
    assert isinstance(new_draft, Workflow)
    assert len(new_draft.steps) == 2
    assert new_draft.steps[0].step_id == "alpha"
    assert new_draft.steps[0].agent.kind == "specialty"
    assert new_draft.steps[0].agent.asset_id == "sa-001"
    assert new_draft.steps[0].skill.asset_id == "sk-001"
    assert new_draft.steps[1].step_id == "beta"
    assert new_draft.steps[1].inputs == ["alpha"]


def test_clone_by_different_actor_writes_under_cloning_actor_prefix():
    """A cloning actor different from the source owner gets a draft under their own prefix.

    Visibility is delegated to the Lab handler (per design), so the
    CRUD reads the source row directly. The new draft is owned by
    ``ctx.actor_id`` and lives under ``users/<ctx.actor_id>/drafts/...``,
    not under the source owner's prefix. AssetIndex.put is invoked with
    ``owner_user_id=ctx.actor_id``.
    """
    # Source is owned by u-jane (private published); u-bob is cloning.
    ctx = _make_ctx(actor_id="u-bob")
    source = _make_skill(
        user_id="u-jane",
        actor_id="u-jane",
        published_id="p-skill-1",
        version=2,
    )
    index = _make_index([source])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    result = _run(
        crud.clone(ctx=ctx, published_id="p-skill-1", version=2)
    )

    # Target prefix is under u-bob (cloner), not u-jane (source owner).
    assert result.s3_prefix.startswith("users/u-bob/drafts/skills/")
    assert "u-jane" not in result.s3_prefix

    # S3 copy ran from the source owner's prefix to the cloner's prefix.
    src_arg, dst_arg = s3.copy_prefix_absolute.await_args.args
    assert src_arg == source.s3_prefix
    assert src_arg.startswith("users/u-jane/")
    assert dst_arg.startswith("users/u-bob/")

    # AssetIndex.put owner is the cloning actor.
    put_kwargs = index.put.await_args.kwargs
    assert put_kwargs["owner_user_id"] == "u-bob"


# ---------------------------------------------------------------------------
# Source not found
# ---------------------------------------------------------------------------


def test_clone_lineage_empty_raises_not_found_or_forbidden():
    """When ``query_lineage`` returns nothing, surface NotFoundOrForbidden.

    No S3 copy and no AssetIndex put should occur.
    """
    ctx = _make_ctx(actor_id="u-jane")
    index = _make_index([])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.clone(ctx=ctx, published_id="p-nope", version=1)
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()
    index.put.assert_not_awaited()


def test_clone_version_missing_raises_not_found_or_forbidden():
    """Lineage exists but the requested version is missing ⇒ not-found.

    No S3 copy and no AssetIndex put should occur.
    """
    ctx = _make_ctx(actor_id="u-jane")
    v1 = _make_skill(version=1)
    index = _make_index([v1])
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.clone(ctx=ctx, published_id="p-skill-1", version=2)
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()
    index.put.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compensating actions on AssetIndex put failure
# ---------------------------------------------------------------------------


def test_clone_put_failure_runs_compensating_delete_and_reraises():
    """When AssetIndex.put fails, compensating delete_prefix_absolute(target) runs.

    The original error is re-raised after a successful compensation.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_skill(version=1)
    index = _make_index([source])
    original_err = RuntimeError("ddb put boom")
    index.put = AsyncMock(side_effect=original_err)
    s3 = _make_s3_mock()

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(RuntimeError) as exc_info:
        _run(
            crud.clone(ctx=ctx, published_id="p-skill-1", version=1)
        )
    assert exc_info.value is original_err

    # Source → target copy ran first.
    s3.copy_prefix_absolute.assert_awaited_once()
    # Compensating delete ran against the target prefix.
    s3.delete_prefix_absolute.assert_awaited_once()
    delete_arg = s3.delete_prefix_absolute.await_args.args[0]
    assert delete_arg.startswith("users/u-jane/drafts/skills/")
    assert delete_arg.endswith("/")


def test_clone_put_failure_and_compensation_failure_raises_compensation_failed():
    """When both AssetIndex.put AND the compensating delete fail, raise CompensationFailedError.

    The error carries the orphan target prefix and the two underlying
    exceptions. The original put error is exposed via ``original_error``
    and the delete error via ``compensation_error``.
    """
    ctx = _make_ctx(actor_id="u-jane")
    source = _make_skill(version=1)
    index = _make_index([source])
    put_err = RuntimeError("ddb put boom")
    delete_err = RuntimeError("s3 delete boom")
    index.put = AsyncMock(side_effect=put_err)
    s3 = _make_s3_mock()
    s3.delete_prefix_absolute = AsyncMock(side_effect=delete_err)

    crud = SkillCRUD(s3=s3, index=index)
    with pytest.raises(CompensationFailedError) as exc_info:
        _run(
            crud.clone(ctx=ctx, published_id="p-skill-1", version=1)
        )

    err = exc_info.value
    assert err.original_error is put_err
    assert err.compensation_error is delete_err
    assert err.orphan_prefix.startswith("users/u-jane/drafts/skills/")
    assert err.orphan_prefix.endswith("/")


# ---------------------------------------------------------------------------
# Tool collections — out of scope in this sprint
# ---------------------------------------------------------------------------


def test_tool_collection_clone_raises_not_implemented():
    """Tool collections have no per-version Clone in this sprint.

    The wrapper raises NotImplementedError up-front without touching S3
    or AssetIndex.
    """
    ctx = _make_ctx(actor_id="u-jane")
    s3 = _make_s3_mock()
    index = MagicMock()
    index.query_lineage = AsyncMock(return_value=[])
    index.put = AsyncMock()
    gateway = MagicMock(spec=GatewayBrowser)

    crud = ToolCollectionCRUD(s3=s3, index=index, gateway_browser=gateway)
    with pytest.raises(NotImplementedError):
        _run(
            crud.clone(ctx=ctx, published_id="p-tc", version=1)
        )

    s3.copy_prefix_absolute.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()
    index.query_lineage.assert_not_awaited()
    index.put.assert_not_awaited()
