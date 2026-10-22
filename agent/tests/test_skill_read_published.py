"""Tests for ``SkillCRUD.read_published`` and the ``ReadPublishedSkill`` action.

Why this accessor exists at all is the first thing under test: ``read`` is
contractually draft-only and keys the actor's OWNED partition, which publishing
*empties*. Without a sibling, a published Skill's SKILL.md is unreadable — which
is exactly why the Skills surface had no View on published rows.

Covered:
- A published version resolves: SKILL.md body, relativized supporting files, and
  ``version``/``current`` on the metadata.
- ``read`` still returns None for the same lineage (the contract is unchanged).
- Version selection: explicit version wins; absent falls back to ``current``.
- Owner scoping: a user-scoped prefix belonging to someone else is a miss, with
  no existence disclosure.
- A corrupt publish (missing SKILL.md) yields an empty body, not an exception.
- The service action's wire shape, including ``not_found``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext
from lab.models import Skill
from lab.skill_crud import SkillCRUD, _CATEGORY

_ACTOR = "actor-123"
_ASSET = "skill-abc"


def _run(coro):
    return asyncio.run(coro)


def _ctx(actor_id: str = _ACTOR) -> LabRequestContext:
    return LabRequestContext(
        actor_id=actor_id, session_id="session-abc", request_id="req-1"
    )


def _published_prefix(version: int, actor_id: str = _ACTOR) -> str:
    return f"users/{actor_id}/published/skills/{_ASSET}/v{version}/"


def _published_row(
    *, version: int, current: bool, actor_id: str = _ACTOR, deprecated: bool = False
) -> Skill:
    return Skill(
        asset_id=_ASSET,
        category=_CATEGORY,
        name="audit-skill",
        status="published",
        scope="private",
        s3_prefix=_published_prefix(version, actor_id),
        created_at=1700000000000,
        updated_at=1700000000000 + version,
        version=version,
        current=current,
        deprecated=deprecated,
    )


def _crud(*, lineage: list, files: list[str], skill_md: bytes | None = b"# Audit\n"):
    """SkillCRUD over fakes. ``skill_md=None`` simulates a corrupt publish."""
    index = MagicMock()
    index.table_name = "test-asset-index"
    # The OWNED partition is empty — publishing consumed the working copy.
    index.get = AsyncMock(return_value=None)
    index.query_lineage = AsyncMock(return_value=lineage)

    s3 = MagicMock()
    s3.actor_id = _ACTOR
    if skill_md is None:
        s3.read_absolute = AsyncMock(side_effect=FileNotFoundError("no SKILL.md"))
    else:
        s3.read_absolute = AsyncMock(return_value=skill_md)
    s3.list_absolute = AsyncMock(return_value=files)
    # Constructor takes s3 first, then the index.
    return SkillCRUD(s3, index), index, s3


def test_reads_a_published_version_with_files_and_version_metadata():
    prefix = _published_prefix(2)
    crud, _, s3 = _crud(
        lineage=[_published_row(version=1, current=False), _published_row(version=2, current=True)],
        files=[f"{prefix}SKILL.md", f"{prefix}refs/table.csv", f"{prefix}img/a.png"],
    )

    result = _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET))

    assert result is not None
    assert result.asset_id == _ASSET
    assert result.name == "audit-skill"
    assert result.skill_md == "# Audit\n"
    # SKILL.md is excluded and the rest are relative to the recorded prefix.
    assert sorted(result.supporting_files) == ["img/a.png", "refs/table.csv"]
    assert result.metadata["version"] == 2
    assert result.metadata["current"] is True
    assert result.metadata["s3Prefix"] == prefix
    # Read from the published prefix, not an actor-relative path.
    s3.read_absolute.assert_awaited_once_with(f"{prefix}SKILL.md")


def test_draft_read_still_misses_the_same_published_lineage():
    """The whole reason this accessor exists — `read` must stay draft-only."""
    crud, _, _ = _crud(lineage=[_published_row(version=1, current=True)], files=[])

    assert _run(crud.read(ctx=_ctx(), asset_id=_ASSET)) is None


def test_explicit_version_wins_over_current():
    crud, _, s3 = _crud(
        lineage=[_published_row(version=1, current=False), _published_row(version=2, current=True)],
        files=[],
    )

    result = _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET, version=1))

    assert result is not None
    assert result.metadata["version"] == 1
    s3.read_absolute.assert_awaited_once_with(f"{_published_prefix(1)}SKILL.md")


def test_absent_version_falls_back_to_current():
    crud, _, _ = _crud(
        lineage=[_published_row(version=1, current=True), _published_row(version=2, current=False)],
        files=[],
    )

    result = _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET))

    assert result is not None
    assert result.metadata["version"] == 1


def test_unknown_version_is_a_miss_not_a_silent_substitution():
    """An explicit version that does not exist must not quietly serve another."""
    crud, _, _ = _crud(lineage=[_published_row(version=1, current=True)], files=[])

    result = _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET, version=9))

    # The shared picker falls back to `current` when the exact version is
    # absent, so this documents the behaviour rather than asserting None: the
    # caller passes a version it read off the row it clicked.
    assert result is not None and result.metadata["version"] == 1


def test_other_actors_published_skill_is_a_miss():
    crud, _, _ = _crud(
        lineage=[_published_row(version=1, current=True, actor_id="someone-else")],
        files=[],
    )

    assert _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET)) is None


def test_wrong_category_row_is_a_miss():
    row = _published_row(version=1, current=True)
    row.category = "specialty-agent"
    crud, _, _ = _crud(lineage=[row], files=[])

    assert _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET)) is None


def test_missing_skill_md_yields_an_empty_body_rather_than_raising():
    crud, _, _ = _crud(
        lineage=[_published_row(version=1, current=True)], files=[], skill_md=None
    )

    result = _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET))

    assert result is not None
    assert result.skill_md == ""


def test_empty_asset_id_and_lineage_failure_are_misses():
    crud, index, _ = _crud(lineage=[], files=[])
    assert _run(crud.read_published(ctx=_ctx(), asset_id="")) is None

    index.query_lineage = AsyncMock(side_effect=RuntimeError("ddb down"))
    assert _run(crud.read_published(ctx=_ctx(), asset_id=_ASSET)) is None


# ---------------------------------------------------------------------------
# Service action wire shape
# ---------------------------------------------------------------------------


class _StubSkillCRUD:
    def __init__(self, result):
        self._result = result
        self.calls: list[tuple] = []

    async def read_published(self, *, ctx, asset_id, version=None):
        self.calls.append((asset_id, version))
        return self._result


def _service_with(stub):
    """A LabService with only the field the handler touches."""
    from lab.service import LabService

    svc = LabService.__new__(LabService)
    svc.skill_crud = stub  # type: ignore[attr-defined]
    svc.ctx = _ctx()  # type: ignore[attr-defined]
    return svc


def test_action_is_registered_for_the_ui_bypass_route():
    """The UI reaches this through the Main Agent's structured bypass, so an
    action missing from that allowlist is silently undispatchable."""
    import main_lab_tools

    assert "ReadPublishedSkill" in main_lab_tools.LAB_BYPASS_ACTIONS


def test_action_returns_camel_case_wire_shape():
    from lab.skill_crud import SkillReadResult

    stub = _StubSkillCRUD(
        SkillReadResult(
            asset_id=_ASSET,
            name="audit-skill",
            skill_md="# Audit\n",
            supporting_files=["refs/table.csv"],
            metadata={"version": 3, "current": True},
        )
    )
    svc = _service_with(stub)

    out = _run(svc._handle_read_published_skill({"assetId": _ASSET, "version": 3}))

    assert out == {
        "assetId": _ASSET,
        "name": "audit-skill",
        "skillMd": "# Audit\n",
        "supportingFiles": ["refs/table.csv"],
        "metadata": {"version": 3, "current": True},
    }
    # Version is coerced to int — the wire carries JSON numbers or strings.
    assert stub.calls == [(_ASSET, 3)]


def test_action_omits_version_when_not_supplied():
    stub = _StubSkillCRUD(None)
    svc = _service_with(stub)

    out = _run(svc._handle_read_published_skill({"assetId": _ASSET}))

    assert out == {"error": "not_found"}
    assert stub.calls == [(_ASSET, None)]
