"""Example tests for the ``list_visible_assets`` resolver (Task 5.1).

Covers the resolver's tier-fan-out, mode-aware post-filter, deprecation
filter, stable sort, cursor round-trip, and group fan-out. Uses an
``AsyncMock``-backed :class:`AssetIndex` so the tests run without
DynamoDB.

Validates: Requirements 9.1, 9.2, 9.3, 9.4, 9.5
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to sys.path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext  # noqa: E402
from lab.models import (  # noqa: E402
    PublishPolicy,
    ScopeHistoryEntry,
    Skill,
)
from lab.operation_results import VisibleAssetsPage  # noqa: E402
from lab.service import (  # noqa: E402
    _decode_cursor,
    _encode_cursor,
    list_visible_assets,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine to completion in a sync test."""
    return asyncio.run(coro)


def _ctx(actor_id: str = "u-jane", group_ids=None) -> LabRequestContext:
    """Build a LabRequestContext with optional group memberships."""
    return LabRequestContext(
        actor_id=actor_id,
        session_id="sess-test",
        request_id="req-test",
        group_ids=list(group_ids or []),
    )


def _draft_skill(*, draft_id: str, user_id: str = "u-jane", updated_at: int) -> Skill:
    return Skill(
        asset_id=draft_id,
        category="skill",
        name=f"draft-{draft_id}",
        status="draft",
        scope="private",
        s3_prefix=f"users/{user_id}/drafts/skills/{draft_id}/",
        created_at=updated_at,
        updated_at=updated_at,
    )


def _published_skill(
    *,
    published_id: str,
    version: int,
    scope: str,
    updated_at: int,
    status: str = "published",
    user_id: str = "u-jane",
    group_id: str | None = None,
    org_id: str | None = None,
) -> Skill:
    if scope == "private":
        prefix = f"users/{user_id}/published/skills/{published_id}/v{version}/"
    elif scope == "group":
        assert group_id, "group scope requires group_id"
        prefix = f"groups/{group_id}/published/skills/{published_id}/v{version}/"
    elif scope == "org":
        assert org_id, "org scope requires org_id"
        prefix = f"org/{org_id}/published/skills/{published_id}/v{version}/"
    else:
        raise ValueError(f"unknown scope: {scope}")

    return Skill(
        asset_id=published_id,
        category="skill",
        name=f"pub-{published_id}-v{version}",
        status=status,
        scope=scope,
        s3_prefix=prefix,
        created_at=updated_at,
        updated_at=updated_at,
        version=version,
        publish_policy=PublishPolicy(owner_ids=[user_id]),
        scope_history=[
            ScopeHistoryEntry(
                from_scope=None,
                to_scope=scope,
                actor_id=user_id,
                timestamp="2024-01-01T00:00:00Z",
                group_ids=[group_id] if group_id else None,
            )
        ],
        current=True,
        promoted_from="d-source",
    )


def _make_index(
    *,
    drafts: list | None = None,
    user_pub: list | None = None,
    org_pub: list | None = None,
    group_pub: dict[str, list] | None = None,
    drafts_lek=None,
    user_pub_lek=None,
    org_pub_lek=None,
    group_pub_leks: dict[str, dict] | None = None,
) -> MagicMock:
    """Build a MagicMock AssetIndex whose ``query_pk`` dispatches by PK.

    The mock matches the resolver's call signature: positional ``pk``,
    keyword ``sk_prefix``, ``last_evaluated_key``, ``limit``.
    """
    drafts = list(drafts or [])
    user_pub = list(user_pub or [])
    org_pub = list(org_pub or [])
    group_pub = dict(group_pub or {})
    group_pub_leks = dict(group_pub_leks or {})

    async def _query_pk(pk, *, sk_prefix=None, last_evaluated_key=None, limit=100):
        if pk.endswith("#OWNED"):
            return list(drafts), drafts_lek
        if pk.startswith("USER#") and pk.endswith("#VERSIONS"):
            return list(user_pub), user_pub_lek
        if pk.startswith("ORG#") and pk.endswith("#SHARED"):
            return list(org_pub), org_pub_lek
        if pk.startswith("GROUP#") and pk.endswith("#SHARED"):
            # Extract group id from "GROUP#<gid>#SHARED".
            gid = pk.removeprefix("GROUP#").removesuffix("#SHARED")
            return list(group_pub.get(gid, [])), group_pub_leks.get(gid)
        return [], None

    index = MagicMock()
    index.query_pk = AsyncMock(side_effect=_query_pk)
    return index


# ---------------------------------------------------------------------------
# Single-tier behavior
# ---------------------------------------------------------------------------


def test_lab_mode_single_tier_drafts_only():
    """Lab mode with only drafts populated returns those drafts unfiltered."""
    drafts = [
        _draft_skill(draft_id="d-1", updated_at=1),
        _draft_skill(draft_id="d-2", updated_at=2),
    ]
    index = _make_index(drafts=drafts)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
        )
    )

    assert isinstance(page, VisibleAssetsPage)
    assert page.next_cursor is None
    assert {a.asset_id for a in page.items} == {"d-1", "d-2"}
    # Sorted desc by updated_at
    assert [a.asset_id for a in page.items] == ["d-2", "d-1"]


# ---------------------------------------------------------------------------
# Multi-tier union, all four populated
# ---------------------------------------------------------------------------


def test_lab_mode_full_union_returns_all_tiers():
    """Lab mode unions drafts + private + group + org rows."""
    drafts = [_draft_skill(draft_id="d-1", updated_at=10)]
    private_pub = [
        _published_skill(
            published_id="pp-1", version=1, scope="private", updated_at=20
        )
    ]
    group_pub = {
        "g-marketing": [
            _published_skill(
                published_id="gp-1",
                version=1,
                scope="group",
                updated_at=30,
                group_id="g-marketing",
            )
        ],
        "g-eng": [
            _published_skill(
                published_id="gp-2",
                version=1,
                scope="group",
                updated_at=15,
                group_id="g-eng",
            )
        ],
    }
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=40,
            org_id="o-acme",
        )
    ]
    index = _make_index(
        drafts=drafts,
        user_pub=private_pub,
        group_pub=group_pub,
        org_pub=org_pub,
    )

    page = _run(
        list_visible_assets(
            _ctx(group_ids=["g-marketing", "g-eng"]),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
        )
    )

    assert page.next_cursor is None
    # All 5 records, sorted desc by updated_at
    assert [a.asset_id for a in page.items] == [
        "op-1",  # 40
        "gp-1",  # 30
        "pp-1",  # 20
        "gp-2",  # 15
        "d-1",   # 10
    ]


# ---------------------------------------------------------------------------
# Mode filters
# ---------------------------------------------------------------------------


def test_chat_mode_strips_drafts():
    """Chat mode strips status='draft' from the union."""
    drafts = [_draft_skill(draft_id="d-1", updated_at=10)]
    private_pub = [
        _published_skill(
            published_id="pp-1", version=1, scope="private", updated_at=20
        )
    ]
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=30,
            org_id="o-acme",
        )
    ]
    index = _make_index(drafts=drafts, user_pub=private_pub, org_pub=org_pub)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="chat",
            asset_index=index,
            org_id="o-acme",
        )
    )

    asset_ids = {a.asset_id for a in page.items}
    assert "d-1" not in asset_ids
    # Private published is still visible in chat mode
    assert "pp-1" in asset_ids
    assert "op-1" in asset_ids


def test_project_mode_strips_drafts_and_private():
    """Project mode strips drafts AND scope='private' rows."""
    drafts = [_draft_skill(draft_id="d-1", updated_at=10)]
    private_pub = [
        _published_skill(
            published_id="pp-1", version=1, scope="private", updated_at=20
        )
    ]
    group_pub = {
        "g-eng": [
            _published_skill(
                published_id="gp-1",
                version=1,
                scope="group",
                updated_at=25,
                group_id="g-eng",
            )
        ],
    }
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=30,
            org_id="o-acme",
        )
    ]
    index = _make_index(
        drafts=drafts,
        user_pub=private_pub,
        group_pub=group_pub,
        org_pub=org_pub,
    )

    page = _run(
        list_visible_assets(
            _ctx(group_ids=["g-eng"]),
            category="skill",
            mode="project",
            asset_index=index,
            org_id="o-acme",
        )
    )

    asset_ids = {a.asset_id for a in page.items}
    assert "d-1" not in asset_ids       # draft stripped
    assert "pp-1" not in asset_ids      # private stripped
    assert asset_ids == {"gp-1", "op-1"}


# ---------------------------------------------------------------------------
# Deprecation filter
# ---------------------------------------------------------------------------


def test_include_deprecated_false_strips_deprecated():
    """Default behavior: deprecated rows are filtered out."""
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=10,
            status="deprecated",
            org_id="o-acme",
        ),
        _published_skill(
            published_id="op-2",
            version=1,
            scope="org",
            updated_at=20,
            org_id="o-acme",
        ),
    ]
    index = _make_index(org_pub=org_pub)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
        )
    )

    assert {a.asset_id for a in page.items} == {"op-2"}


def test_include_deprecated_true_keeps_deprecated():
    """``include_deprecated=True`` preserves deprecated rows."""
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=10,
            status="deprecated",
            org_id="o-acme",
        ),
        _published_skill(
            published_id="op-2",
            version=1,
            scope="org",
            updated_at=20,
            org_id="o-acme",
        ),
    ]
    index = _make_index(org_pub=org_pub)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
            include_deprecated=True,
        )
    )

    assert {a.asset_id for a in page.items} == {"op-1", "op-2"}


# ---------------------------------------------------------------------------
# Cursor round-trip
# ---------------------------------------------------------------------------


def test_cursor_roundtrip_encodes_and_decodes_state():
    """Encoding then decoding a cursor recovers the per-tier state."""
    state = {
        "user_drafts": {"PK": "USER#u#OWNED", "SK": "skill#d-9"},
        "user_pub": None,
        "group_pub": {
            "g-marketing": {"PK": "GROUP#g-marketing#SHARED", "SK": "skill#p#v0000000003"},
        },
        "org_pub": {"PK": "ORG#o-acme#SHARED", "SK": "skill#p#v0000000007"},
    }
    cursor = _encode_cursor(state)
    assert cursor is not None
    decoded = _decode_cursor(cursor)
    assert decoded == state


def test_cursor_none_when_all_tiers_exhausted():
    """A state with no open tier encodes to None."""
    state = {
        "user_drafts": None,
        "user_pub": None,
        "group_pub": {},
        "org_pub": None,
    }
    assert _encode_cursor(state) is None


def test_resolver_returns_cursor_when_tier_has_more_pages():
    """When any tier reports a LastEvaluatedKey, next_cursor is opaque."""
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=10,
            org_id="o-acme",
        )
    ]
    index = _make_index(
        org_pub=org_pub,
        org_pub_lek={"PK": "ORG#o-acme#SHARED", "SK": "skill#x#v0000000005"},
    )

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
        )
    )

    assert page.next_cursor is not None
    state = _decode_cursor(page.next_cursor)
    assert state["org_pub"] == {
        "PK": "ORG#o-acme#SHARED",
        "SK": "skill#x#v0000000005",
    }
    # Other tiers exhausted
    assert state["user_drafts"] is None
    assert state["user_pub"] is None
    assert state["group_pub"] == {}


def test_decode_cursor_rejects_invalid_input():
    """Malformed cursor input raises ValueError."""
    with pytest.raises(ValueError):
        _decode_cursor("not-base64!!!")


def test_decode_cursor_none_returns_empty_state():
    """``None`` and empty string decode to a fresh empty state."""
    for value in (None, ""):
        state = _decode_cursor(value)
        assert state == {
            "user_drafts": None,
            "user_pub": None,
            "group_pub": {},
            "org_pub": None,
        }


# ---------------------------------------------------------------------------
# Group fan-out
# ---------------------------------------------------------------------------


def test_group_fanout_queries_each_group_partition():
    """One ``query_pk`` call is issued per group in ``ctx.group_ids``."""
    group_pub = {
        "g-1": [
            _published_skill(
                published_id="gp-1",
                version=1,
                scope="group",
                updated_at=10,
                group_id="g-1",
            )
        ],
        "g-2": [
            _published_skill(
                published_id="gp-2",
                version=1,
                scope="group",
                updated_at=20,
                group_id="g-2",
            )
        ],
        "g-3": [
            _published_skill(
                published_id="gp-3",
                version=1,
                scope="group",
                updated_at=30,
                group_id="g-3",
            )
        ],
    }
    index = _make_index(group_pub=group_pub)

    page = _run(
        list_visible_assets(
            _ctx(group_ids=["g-1", "g-2", "g-3"]),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
        )
    )

    assert {a.asset_id for a in page.items} == {"gp-1", "gp-2", "gp-3"}

    # 3 group queries + drafts + user_pub + org_pub = 6 total
    pks_called = {call.args[0] for call in index.query_pk.await_args_list}
    assert "GROUP#g-1#SHARED" in pks_called
    assert "GROUP#g-2#SHARED" in pks_called
    assert "GROUP#g-3#SHARED" in pks_called


def test_no_org_id_skips_org_tier():
    """When ``org_id`` is None and ctx has no org_id, the org tier is skipped."""
    org_pub = [
        _published_skill(
            published_id="op-1",
            version=1,
            scope="org",
            updated_at=10,
            org_id="o-acme",
        )
    ]
    index = _make_index(org_pub=org_pub)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id=None,
        )
    )

    # Org tier was not queried (resolver short-circuits).
    pks_called = {call.args[0] for call in index.query_pk.await_args_list}
    assert all(not pk.startswith("ORG#") for pk in pks_called)
    # And of course the org row is not in the result.
    assert page.items == []


# ---------------------------------------------------------------------------
# Limit & sort
# ---------------------------------------------------------------------------


def test_limit_truncates_after_sort():
    """The post-filter sort + truncate keeps the newest ``limit`` rows."""
    org_pub = [
        _published_skill(
            published_id=f"op-{i}",
            version=1,
            scope="org",
            updated_at=i,
            org_id="o-acme",
        )
        for i in range(1, 11)  # 10 rows, updated_at 1..10
    ]
    index = _make_index(org_pub=org_pub)

    page = _run(
        list_visible_assets(
            _ctx(),
            category="skill",
            mode="lab",
            asset_index=index,
            org_id="o-acme",
            limit=3,
        )
    )

    assert [a.asset_id for a in page.items] == ["op-10", "op-9", "op-8"]


def test_invalid_mode_raises():
    """Unknown mode is rejected up-front."""
    index = _make_index()
    with pytest.raises(ValueError):
        _run(
            list_visible_assets(
                _ctx(),
                category="skill",
                mode="invalid",  # type: ignore[arg-type]
                asset_index=index,
                org_id="o-acme",
            )
        )
