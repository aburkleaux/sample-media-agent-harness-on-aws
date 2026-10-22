"""Tests for Task 3.6 — workflow Promote Asset_Ref resolution.

Exercises ``WorkflowCRUD.promote`` ref-resolvability gate (Requirement
11.1–11.4 / Property 9):

* Happy path — a workflow whose steps' Asset_Refs all resolve to
  non-deprecated published rows promotes successfully and the
  canonical-op layer commits both S3 and the AssetIndex transaction.
* Deprecated-ref rejection — any Asset_Ref pointing at a
  ``status="deprecated"`` published row raises
  :class:`DeprecatedReferenceError` BEFORE any S3 copy or AssetIndex
  mutation.
* Missing-ref rejection — any Asset_Ref naming a ``(publishedId,
  version)`` that does not resolve raises
  :class:`NotFoundOrForbidden` BEFORE any mutation.
* Pinning to exact version — a ref to a non-current version of a
  lineage with a different ``current=true`` row is accepted; the
  resolver does not fall back to ``current``.
* Bare core-agent refs — ``agent.ref="task-agent"`` is not an
  Asset_Ref and is left alone (no spurious resolution attempt).
* Module-level grammar — the regex parser accepts the documented form
  and rejects malformed strings.

Validates: Requirements 11.1, 11.2, 11.3, 11.4
Properties: 9
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.errors import DeprecatedReferenceError, NotFoundOrForbidden  # noqa: E402
from lab.models import Skill, SpecialtyAgent, Workflow  # noqa: E402
from lab.operation_results import Promoted  # noqa: E402
from lab.workflow_crud import WorkflowCRUD, _parse_asset_ref  # noqa: E402


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(actor_id: str = "u-jane") -> LabRequestContext:
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
    )


def _make_workflow_draft(
    *, user_id: str = "u-jane", draft_id: str = "d-77"
) -> Workflow:
    now = int(time.time() * 1000)
    return Workflow(
        asset_id=draft_id,
        category="workflow",
        name="my-workflow",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/workflows/{draft_id}/",
        created_at=now,
        updated_at=now,
        steps=[],
    )


def _make_published(
    *,
    category: str,
    published_id: str,
    version: int,
    status: str = "published",
    scope: str = "org",
) -> object:
    """Build a typed published-row dataclass for query_lineage to return."""
    now = int(time.time() * 1000)
    cls = {"skill": Skill, "specialty-agent": SpecialtyAgent}[category]
    plural = (
        "skills" if category == "skill" else "specialty-agents"
    )
    return cls(
        asset_id=published_id,
        category=category,
        name=f"{published_id}-{version}",
        status=status,
        scope=scope,
        s3_prefix=f"org/o-acme/published/{plural}/{published_id}/v{version}/",
        created_at=now,
        updated_at=now,
        version=version,
        current=(status != "deprecated"),
    )


def _workflow_json(steps: list[dict]) -> bytes:
    """Render a workflow.json bytes payload from a list of raw step dicts."""
    return json.dumps({"name": "wf", "steps": steps}, indent=2).encode("utf-8")


def _make_index(draft: Workflow, lineages: dict[str, list]):
    """Build a mock AssetIndex.

    Args:
        draft: The workflow draft returned by `index.get`.
        lineages: Map from `published_id` to the list of typed
            published-row dataclasses to return from `query_lineage`.
    """
    index = MagicMock()
    index.table_name = "test-asset-index"
    index.get = AsyncMock(return_value=draft)

    async def _query_lineage(published_id: str):
        return list(lineages.get(published_id, []))

    index.query_lineage = AsyncMock(side_effect=_query_lineage)
    index.transact_write = AsyncMock()
    return index


def _make_s3_mock(workflow_bytes: bytes | None = b''):
    """Build a mock S3UserFilesTool that serves a fixed workflow.json.

    When ``workflow_bytes`` is None, ``read`` raises FileNotFoundError;
    otherwise it returns the bytes for the draft path and raises for
    any other path.
    """
    s3 = MagicMock()
    s3.copy_prefix_absolute = AsyncMock()
    s3.delete_prefix_absolute = AsyncMock()

    async def _read(path: str) -> bytes:
        if workflow_bytes is None:
            raise FileNotFoundError(f"{path} not found")
        return workflow_bytes

    s3.read = AsyncMock(side_effect=_read)
    # Artifact reads moved to canonical-prefix absolute reads; the promote
    # empty-source guard (BUG-2) lists the draft prefix — return a non-empty
    # listing so the copy proceeds.
    s3.read_absolute = s3.read
    s3.list_absolute = AsyncMock(return_value=["<draft>/workflow.json"])
    return s3


# ---------------------------------------------------------------------------
# Asset_Ref grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("skill/p-001@v1", ("skill", "p-001", 1)),
        ("specialty-agent/abc-123@v42", ("specialty-agent", "abc-123", 42)),
        ("tool-collection/tc_xyz@v7", ("tool-collection", "tc_xyz", 7)),
        ("workflow/wf-aa@v100", ("workflow", "wf-aa", 100)),
        # Non-Asset_Ref strings: bare core-agent ref, plain assetId, None.
        ("task-agent", None),
        ("d-1234-uuid", None),
        ("", None),
        (None, None),
        # Malformed: missing version, version=0, unknown category.
        ("skill/p-001", None),
        ("skill/p-001@v", None),
        ("skill/p-001@v0", None),
        ("not-a-category/p-001@v1", None),
        # Path-traversal inside published_id: rejected by the
        # `[A-Za-z0-9_-]+` character class.
        ("skill/p-001/foo@v1", None),
    ],
)
def test_parse_asset_ref_grammar(value, expected):
    """The Asset_Ref parser accepts only the canonical form."""
    assert _parse_asset_ref(value) == expected


# ---------------------------------------------------------------------------
# Happy path: all refs resolve to non-deprecated rows
# ---------------------------------------------------------------------------


def test_promote_workflow_with_valid_refs_succeeds():
    """A workflow whose Asset_Refs all resolve to non-deprecated rows
    promotes successfully and the canonical-op layer commits both
    sides of the Promote.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")

    # The draft's workflow.json names two distinct lineages: a specialty
    # agent at v1 and a skill at v2. Both rows are status="published".
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa-001@v1"},
            "skill": {"assetId": "skill/sk-001@v2"},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))

    lineages = {
        "sa-001": [_make_published(category="specialty-agent", published_id="sa-001", version=1)],
        "sk-001": [_make_published(category="skill", published_id="sk-001", version=2)],
    }
    index = _make_index(draft, lineages)

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(ctx=ctx, draft_id="d-77", scope="org", org_id="o-acme")
    )

    assert isinstance(result, Promoted)
    assert result.version == 1

    # Each unique publishedId was queried exactly once.
    assert index.query_lineage.await_count == 2
    queried_ids = {
        call.args[0] for call in index.query_lineage.await_args_list
    }
    assert queried_ids == {"sa-001", "sk-001"}

    # The canonical-op layer ran S3 copy + transact_write, proving the
    # gate passed and dispatched downstream.
    s3.copy_prefix_absolute.assert_awaited_once()
    index.transact_write.assert_awaited_once()


def test_promote_workflow_no_steps_passes_gate():
    """A workflow draft with no steps has no Asset_Refs to validate; the
    gate passes vacuously and the canonical Promote runs unaffected.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    s3 = _make_s3_mock(workflow_bytes=_workflow_json([]))
    index = _make_index(draft, lineages={})

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(ctx=ctx, draft_id="d-77", scope="private")
    )
    assert isinstance(result, Promoted)
    # No refs ⇒ no lineage queries.
    index.query_lineage.assert_not_awaited()
    s3.copy_prefix_absolute.assert_awaited_once()


def test_promote_workflow_with_only_core_agent_refs_passes_gate():
    """``agent.ref="task-agent"`` is a bare core-agent ref, not an
    Asset_Ref; it must NOT trigger a lineage query and must NOT block
    the Promote.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "core", "ref": "task-agent"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))
    index = _make_index(draft, lineages={})

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(ctx=ctx, draft_id="d-77", scope="private")
    )
    assert isinstance(result, Promoted)
    index.query_lineage.assert_not_awaited()


# ---------------------------------------------------------------------------
# Rejection: deprecated ref
# ---------------------------------------------------------------------------


def test_promote_workflow_with_deprecated_ref_raises_and_no_writes():
    """An Asset_Ref pinned to a deprecated version raises
    :class:`DeprecatedReferenceError` BEFORE any S3 copy or AssetIndex
    transaction (Property 9: rejection produces no writes).
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")

    # The lineage has v1 (deprecated, the ref's target) and v2
    # (published, current). The author pinned v1 explicitly; the
    # resolver MUST NOT fall back to v2 (Requirement 11.4) — it MUST
    # reject because the named version is deprecated.
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa-001@v1"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))
    lineages = {
        "sa-001": [
            _make_published(
                category="specialty-agent",
                published_id="sa-001",
                version=1,
                status="deprecated",
            ),
            _make_published(
                category="specialty-agent",
                published_id="sa-001",
                version=2,
                status="published",
            ),
        ]
    }
    index = _make_index(draft, lineages)

    crud = WorkflowCRUD(s3=s3, index=index)
    with pytest.raises(DeprecatedReferenceError) as exc_info:
        _run(
            crud.promote(
                ctx=ctx, draft_id="d-77", scope="org", org_id="o-acme"
            )
        )

    err = exc_info.value
    assert err.category == "specialty-agent"
    assert err.published_id == "sa-001"
    assert err.version == 1
    assert err.step_location is not None
    assert "s1" in err.step_location

    # Property 9: rejection produces no AssetIndex or S3 write.
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Rejection: unresolvable ref
# ---------------------------------------------------------------------------


def test_promote_workflow_with_missing_lineage_raises_not_found():
    """An Asset_Ref whose lineage has no rows at all raises
    :class:`NotFoundOrForbidden` BEFORE any mutation.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa-missing@v1"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))
    # Empty lineage list ⇒ resolver finds no matching version.
    index = _make_index(draft, lineages={"sa-missing": []})

    crud = WorkflowCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden) as exc_info:
        _run(
            crud.promote(
                ctx=ctx, draft_id="d-77", scope="org", org_id="o-acme"
            )
        )
    assert "sa-missing" in str(exc_info.value)
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


def test_promote_workflow_with_wrong_version_raises_not_found():
    """An Asset_Ref naming a version that does not exist in the lineage
    raises :class:`NotFoundOrForbidden` even when other versions of
    the same lineage exist.
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    # Author pinned v5 but only v1 and v2 exist.
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa-001@v5"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))
    lineages = {
        "sa-001": [
            _make_published(category="specialty-agent", published_id="sa-001", version=1),
            _make_published(category="specialty-agent", published_id="sa-001", version=2),
        ]
    }
    index = _make_index(draft, lineages)

    crud = WorkflowCRUD(s3=s3, index=index)
    with pytest.raises(NotFoundOrForbidden):
        _run(
            crud.promote(
                ctx=ctx, draft_id="d-77", scope="org", org_id="o-acme"
            )
        )
    s3.copy_prefix_absolute.assert_not_awaited()
    index.transact_write.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pinning: ref to non-current version of a lineage is accepted
# ---------------------------------------------------------------------------


def test_promote_workflow_pins_to_exact_version_ignoring_current():
    """An Asset_Ref to a non-current but non-deprecated version
    succeeds. The resolver pins to the named version regardless of
    which version of the lineage carries ``current=true``
    (Requirement 11.4).
    """
    ctx = _make_ctx(actor_id="u-jane")
    draft = _make_workflow_draft(user_id="u-jane", draft_id="d-77")
    # Author pinned v1; lineage has v1 (published, not current) and
    # v2 (published, current). The Promote MUST succeed: v1 still
    # exists and is not deprecated; resolution does not fall back to
    # v2 just because v2 is current.
    steps = [
        {
            "stepId": "s1",
            "agent": {"kind": "specialty", "assetId": "specialty-agent/sa-001@v1"},
            "skill": {"assetId": None},
            "toolCollection": {"assetId": None},
            "prompt": "do thing",
            "inputs": [],
        }
    ]
    s3 = _make_s3_mock(workflow_bytes=_workflow_json(steps))
    v1 = _make_published(
        category="specialty-agent", published_id="sa-001", version=1
    )
    v1.current = False
    v2 = _make_published(
        category="specialty-agent", published_id="sa-001", version=2
    )
    v2.current = True
    lineages = {"sa-001": [v1, v2]}
    index = _make_index(draft, lineages)

    crud = WorkflowCRUD(s3=s3, index=index)
    result = _run(
        crud.promote(ctx=ctx, draft_id="d-77", scope="org", org_id="o-acme")
    )
    assert isinstance(result, Promoted)
    s3.copy_prefix_absolute.assert_awaited_once()
    index.transact_write.assert_awaited_once()
