"""Tests for the canonical Purge operation (hard delete of a published lineage).

Purge is the only operation in this layer that destroys immutable published
state, so these tests are weighted toward the ways it must REFUSE, and toward
the ordering property that keeps a failure recoverable.

Covered:

* Happy path across mixed scopes: every version row is deleted at the right
  partition, highest version first, and an outbox event is emitted for the
  org-scoped version ONLY.
* Ordering: every ``transact_write`` precedes every ``delete_prefix_absolute``.
  A row surviving over deleted objects would be a broken published version, so
  this is the load-bearing invariant, not a stylistic one.
* A DynamoDB failure means S3 is never touched at all.
* An S3 failure is reported in ``prefixes_orphaned`` and does not raise.
* Authorization is per-version: a contributor on one version cannot purge a
  lineage containing a version they have no authority over.
* ``publish_policy is None`` denies everyone.
* A stored prefix that disagrees with the canonical grammar aborts before any
  write.
* Working copy handling, both present and absent.
* Non-purgeable categories are rejected.
* ``TransactionCanceledException`` ⇒ ``ConflictError`` naming what was already
  removed, so a retry can finish.

Properties: 8 (authority independent of visibility), and the purge-specific
ordering guarantee described in ``purge_asset_lineage``'s docstring.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab._canonical_ops import (  # noqa: E402
    _PURGEABLE_CATEGORIES,
    build_purge_preflight,
    preflight_asset_purge,
    purge_asset_lineage,
)
from lab.context import LabRequestContext  # noqa: E402
from lab.errors import (  # noqa: E402
    AuthorityDenied,
    ConflictError,
    NotFoundOrForbidden,
)
from lab.models import (  # noqa: E402
    IntegrationConfig,
    PublishPolicy,
    Scope,
    ScopeHistoryEntry,
    SpecialtyAgent,
)
from lab.path_validator import PrefixMismatchError  # noqa: E402

_CATEGORY = "specialty-agent"
_LINEAGE = "p-agent-1"


def _run(coro):
    return asyncio.run(coro)


def _make_ctx(actor_id: str = "u-jane") -> LabRequestContext:
    return LabRequestContext(
        actor_id=actor_id, session_id="sess-test", request_id="req-test"
    )


def _make_agent(
    *,
    version: Optional[int],
    scope: Scope = "private",
    user_id: str = "u-jane",
    owner_ids: Optional[list[str]] = None,
    contributor_ids: Optional[list[str]] = None,
    current: bool = False,
    s3_prefix: Optional[str] = None,
    integration_enabled: bool = False,
    external_access_enabled: bool = True,
) -> SpecialtyAgent:
    """Build a specialty-agent row whose prefix matches the canonical grammar.

    The prefix matters: the purge re-derives it and refuses to delete anything
    it cannot prove is canonical.
    """
    now = int(time.time() * 1000)
    if version is None:
        prefix = f"users/{user_id}/drafts/specialty-agents/{_LINEAGE}/"
    elif scope == "private":
        prefix = f"users/{user_id}/published/specialty-agents/{_LINEAGE}/v{version}/"
    elif scope == "group":
        prefix = f"groups/g-marketing/published/specialty-agents/{_LINEAGE}/v{version}/"
    else:
        prefix = f"org/o-acme/published/specialty-agents/{_LINEAGE}/v{version}/"

    return SpecialtyAgent(
        asset_id=_LINEAGE,
        category=_CATEGORY,
        name="my-agent",
        status="draft" if version is None else "published",
        scope=scope,
        s3_prefix=s3_prefix if s3_prefix is not None else prefix,
        created_at=now,
        updated_at=now,
        version=version,
        current=current,
        publish_policy=None
        if version is None
        else PublishPolicy(
            owner_ids=list(owner_ids) if owner_ids is not None else [user_id],
            contributor_ids=list(contributor_ids or []),
        ),
        scope_history=[
            ScopeHistoryEntry(
                from_scope=None,
                to_scope=scope,
                actor_id=user_id,
                timestamp="2024-01-01T00:00:00.000000Z",
            )
        ],
        integration_config=IntegrationConfig(
            enabled=integration_enabled,
            issuer="https://issuer.example" if integration_enabled else None,
            audience="aud" if integration_enabled else None,
        ),
        external_access_enabled=external_access_enabled,
    )


def _make_index(lineage: list[SpecialtyAgent], working: Optional[SpecialtyAgent] = None):
    """AssetIndex double recording call order across DDB and S3."""
    index = MagicMock()
    index.name = "test-asset-index"
    index.table_name = "test-asset-index"
    index.get = AsyncMock(return_value=working)
    index.query_lineage = AsyncMock(return_value=list(lineage))
    index.transact_write = AsyncMock(return_value=None)
    return index


def _make_s3():
    s3 = MagicMock()
    s3.delete_prefix_absolute = AsyncMock(return_value=None)
    return s3


def _with_call_order(index, s3):
    """Record DDB/S3 calls into one list so ordering can be asserted."""
    order: list[str] = []

    async def _txn(ops):
        order.append("ddb")
        return None

    async def _del(prefix):
        order.append("s3")
        return None

    index.transact_write = AsyncMock(side_effect=_txn)
    s3.delete_prefix_absolute = AsyncMock(side_effect=_del)
    return order


def _deletes(index) -> list[dict]:
    """Every Delete op issued, flattened, in call order."""
    out = []
    for call in index.transact_write.await_args_list:
        for op in call.args[0]:
            if "Delete" in op:
                out.append(op["Delete"])
    return out


def _outbox_items(index) -> list[dict]:
    out = []
    for call in index.transact_write.await_args_list:
        for op in call.args[0]:
            item = op.get("Put", {}).get("Item", {})
            if item.get("recordType") == "outbox":
                out.append(item)
    return out


def _txn_canceled() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
        },
        "TransactWriteItems",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_purges_every_version_across_mixed_scopes():
    lineage = [
        _make_agent(version=1, scope="private"),
        _make_agent(version=2, scope="group"),
        _make_agent(version=3, scope="org", current=True),
    ]
    index, s3 = _make_index(lineage), _make_s3()

    result = _run(
        purge_asset_lineage(
            ctx=_make_ctx(), asset_index=index, s3=s3,
            category=_CATEGORY, published_id=_LINEAGE,
        )
    )

    # Highest version first, so a partial failure degrades downward and the
    # resolvers' "highest non-deprecated" fallback stays predictable.
    assert result.versions_deleted == (3, 2, 1)

    pks = [d["Key"]["PK"] for d in _deletes(index)]
    assert pks == [
        "ORG#o-acme#SHARED",
        "GROUP#g-marketing#SHARED",
        "USER#u-jane#VERSIONS",
    ]
    sks = [d["Key"]["SK"] for d in _deletes(index)]
    assert sks == [
        f"{_CATEGORY}#{_LINEAGE}#v0000000003",
        f"{_CATEGORY}#{_LINEAGE}#v0000000002",
        f"{_CATEGORY}#{_LINEAGE}#v0000000001",
    ]
    assert len(result.prefixes_deleted) == 3
    assert result.prefixes_orphaned == ()


def test_no_condition_expression_on_the_deletes():
    """A resumed purge must converge, not cancel.

    `attribute_exists(PK)` would make a retry fail on rows a previous attempt
    already removed. A DynamoDB delete of a missing key is a no-op, which is the
    semantics a retry wants.
    """
    lineage = [_make_agent(version=1, current=True)]
    index, s3 = _make_index(lineage), _make_s3()
    _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                             category=_CATEGORY, published_id=_LINEAGE))
    for delete in _deletes(index):
        assert "ConditionExpression" not in delete


def test_outbox_event_emitted_only_for_org_scoped_versions():
    """Org scope is the only registry-eligible scope.

    The event is what drives the Registry record to DEPRECATED — the projector
    reloads canonical state, finds the row gone, and converges. Private and
    group versions were never discoverable, so they need no event.
    """
    lineage = [
        _make_agent(version=1, scope="private"),
        _make_agent(version=2, scope="group"),
        _make_agent(version=3, scope="org", current=True),
    ]
    index, s3 = _make_index(lineage), _make_s3()

    result = _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                      category=_CATEGORY, published_id=_LINEAGE))

    items = _outbox_items(index)
    assert len(items) == 1 and result.outbox_events == 1
    assert items[0]["eventType"] == "Deprecated"
    assert items[0]["version"] == 3
    assert items[0]["scope"] == "org"
    # canonical_revision is that version's immutable SK, so a replay recomputes
    # the same idempotency key.
    assert items[0]["canonicalRevision"] == f"{_CATEGORY}#{_LINEAGE}#v0000000003"


def test_deletes_the_working_copy_when_one_exists():
    lineage = [_make_agent(version=1, current=True)]
    working = _make_agent(version=None)
    index, s3 = _make_index(lineage, working=working), _make_s3()

    result = _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                      category=_CATEGORY, published_id=_LINEAGE))

    assert result.working_copy_deleted is True
    owned = [d for d in _deletes(index) if d["Key"]["PK"] == "USER#u-jane#OWNED"]
    assert len(owned) == 1
    assert owned[0]["Key"]["SK"] == f"{_CATEGORY}#{_LINEAGE}"


def test_no_working_copy_is_fine():
    lineage = [_make_agent(version=1, current=True)]
    index, s3 = _make_index(lineage, working=None), _make_s3()
    result = _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                      category=_CATEGORY, published_id=_LINEAGE))
    assert result.working_copy_deleted is False
    assert not [d for d in _deletes(index) if "OWNED" in d["Key"]["PK"]]


# ---------------------------------------------------------------------------
# Ordering — the load-bearing invariant
# ---------------------------------------------------------------------------


def test_every_row_delete_precedes_every_prefix_delete():
    lineage = [_make_agent(version=v, current=(v == 2)) for v in (1, 2)]
    index, s3 = _make_index(lineage), _make_s3()
    order = _with_call_order(index, s3)

    _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                             category=_CATEGORY, published_id=_LINEAGE))

    assert order == ["ddb", "ddb", "s3", "s3"], order
    # Restated as the property: no S3 call happens before the last DDB call.
    assert order.index("s3") > max(i for i, v in enumerate(order) if v == "ddb")


def test_dynamodb_failure_never_touches_s3():
    """The whole reason rows go first.

    If S3 went first and DDB failed, the surviving rows would point at deleted
    objects — a published version that resolvers happily serve but whose
    artifacts are gone.
    """
    lineage = [_make_agent(version=1, current=True)]
    index, s3 = _make_index(lineage), _make_s3()
    index.transact_write = AsyncMock(side_effect=_txn_canceled())

    with pytest.raises(ConflictError):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))

    s3.delete_prefix_absolute.assert_not_awaited()


def test_conflict_names_what_was_already_removed():
    """So the UI can say "partially deleted — retry to finish"."""
    lineage = [_make_agent(version=v, current=(v == 3)) for v in (1, 2, 3)]
    index, s3 = _make_index(lineage), _make_s3()
    calls = {"n": 0}

    async def _txn(ops):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _txn_canceled()
        return None

    index.transact_write = AsyncMock(side_effect=_txn)

    with pytest.raises(ConflictError) as excinfo:
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))
    # v3 went first, so it is the one already gone.
    assert "[3]" in str(excinfo.value)
    s3.delete_prefix_absolute.assert_not_awaited()


def test_s3_failure_is_reported_not_raised():
    lineage = [_make_agent(version=v, current=(v == 2)) for v in (1, 2)]
    index, s3 = _make_index(lineage), _make_s3()
    seen: list[str] = []

    async def _del(prefix):
        seen.append(prefix)
        if "v2" in prefix:
            raise RuntimeError("s3 said no")
        return None

    s3.delete_prefix_absolute = AsyncMock(side_effect=_del)

    result = _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                      category=_CATEGORY, published_id=_LINEAGE))

    assert len(result.prefixes_orphaned) == 1
    assert "v2" in result.prefixes_orphaned[0]
    assert len(result.prefixes_deleted) == 1
    # The remaining prefix is still attempted after the failure.
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# Authorization — per version, fail closed
# ---------------------------------------------------------------------------


def test_authority_is_required_on_every_version():
    """Being a contributor on v2 must not license destroying v1."""
    lineage = [
        _make_agent(version=1, owner_ids=["u-someone-else"]),
        _make_agent(version=2, owner_ids=["u-jane"], current=True),
    ]
    index, s3 = _make_index(lineage), _make_s3()

    with pytest.raises(AuthorityDenied):
        _run(purge_asset_lineage(ctx=_make_ctx("u-jane"), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))

    index.transact_write.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


def test_contributor_may_purge():
    lineage = [
        _make_agent(version=1, owner_ids=["u-owner"], contributor_ids=["u-jane"]),
    ]
    index, s3 = _make_index(lineage), _make_s3()
    result = _run(purge_asset_lineage(ctx=_make_ctx("u-jane"), asset_index=index, s3=s3,
                                      category=_CATEGORY, published_id=_LINEAGE))
    assert result.versions_deleted == (1,)


def test_missing_publish_policy_denies_everyone():
    lineage = [_make_agent(version=1, current=True)]
    object.__setattr__(lineage[0], "publish_policy", None)
    index, s3 = _make_index(lineage), _make_s3()

    with pytest.raises(AuthorityDenied):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))
    index.transact_write.assert_not_awaited()


def test_nothing_to_purge_is_not_found_or_forbidden():
    index, s3 = _make_index([], working=None), _make_s3()
    with pytest.raises(NotFoundOrForbidden):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))


# ---------------------------------------------------------------------------
# Prefix safety and category gating
# ---------------------------------------------------------------------------


def test_prefix_disagreeing_with_the_grammar_aborts_before_any_write():
    """A hard delete against an unrecognized prefix is unrecoverable."""
    lineage = [
        _make_agent(version=1, current=True,
                    s3_prefix="users/u-jane/published/specialty-agents/SOMEONE-ELSE/v1/"),
    ]
    index, s3 = _make_index(lineage), _make_s3()

    with pytest.raises(PrefixMismatchError):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))
    index.transact_write.assert_not_awaited()
    s3.delete_prefix_absolute.assert_not_awaited()


@pytest.mark.parametrize("bad", ["", "   ", "/", "users/u-jane/published/x"])
def test_unusable_prefix_aborts_before_any_write(bad):
    lineage = [_make_agent(version=1, current=True, s3_prefix=bad)]
    index, s3 = _make_index(lineage), _make_s3()
    with pytest.raises((ValueError, PrefixMismatchError)):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=_CATEGORY, published_id=_LINEAGE))
    index.transact_write.assert_not_awaited()


@pytest.mark.parametrize("category", ["workflow", "tool-collection", "hook"])
def test_categories_without_a_reference_scan_are_not_purgeable(category):
    """Purge is widened only alongside a reference scan.

    A category is purgeable once ``lab.purge_scan.scan_references`` can say what
    still points at it. Nothing scans for references to workflows or hooks, so
    purging one would show an empty blast radius for an asset other things depend
    on — worse than refusing.

    ``tool-collection`` stays refused for a different reason: a scan for it does
    exist (``scan_tool_collection_references``), but it gates the flat category's
    ordinary *delete* — there is no lineage to purge and no purge route. Having a
    scan is necessary for purgeability, not sufficient.
    """
    index, s3 = _make_index([]), _make_s3()
    with pytest.raises(ValueError, match="purge only supports"):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category=category, published_id=_LINEAGE))


def test_skills_are_purgeable_now_that_their_references_are_scanned():
    """The allowlist admits skills, and it is the allowlist being tested.

    Asserted by getting PAST the category gate: an empty index then fails on
    "nothing to purge", which a rejected category never reaches. Pinning the
    membership directly as well, so widening or narrowing the set is a visible
    diff rather than a behaviour change discovered later.
    """
    assert _PURGEABLE_CATEGORIES == frozenset({"skill", "specialty-agent"})

    index, s3 = _make_index([]), _make_s3()
    with pytest.raises(NotFoundOrForbidden):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3,
                                 category="skill", published_id=_LINEAGE))


# ---------------------------------------------------------------------------
# Preflight report
# ---------------------------------------------------------------------------


def test_preflight_reports_versions_scopes_and_prefixes():
    lineage = [
        _make_agent(version=1, scope="private"),
        _make_agent(version=2, scope="org", current=True),
    ]
    report = build_purge_preflight(
        category=_CATEGORY, published_id=_LINEAGE, versions=lineage, working_copy=None,
    )
    assert report.versions == (1, 2)
    assert report.scopes == ("org", "private")
    assert report.working_copy_exists is False
    assert len(report.prefixes) == 2


def test_preflight_external_access_needs_both_gates():
    """Effective external access is integration_config.enabled AND the gate.

    The gate is separately revocable, so a revoked agent with a configured
    issuer is NOT live.
    """
    def report_for(enabled, gate):
        row = _make_agent(version=1, current=True,
                          integration_enabled=enabled, external_access_enabled=gate)
        return build_purge_preflight(category=_CATEGORY, published_id=_LINEAGE,
                                     versions=[row], working_copy=None)

    assert report_for(True, True).external_access_live is True
    assert report_for(True, False).external_access_live is False
    assert report_for(False, True).external_access_live is False


def test_preflight_asset_purge_writes_nothing():
    lineage = [_make_agent(version=v, current=(v == 2)) for v in (1, 2)]
    index, s3 = _make_index(lineage), _make_s3()

    report = _run(preflight_asset_purge(
        ctx=_make_ctx(), asset_index=index,
        category=_CATEGORY, published_id=_LINEAGE,
    ))

    assert report.versions == (1, 2)
    index.transact_write.assert_not_awaited()
    assert not index.put.called and not index.update.called
    assert not s3.delete_prefix_absolute.called


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda rows: [_make_agent(version=1, owner_ids=["u-else"])], AuthorityDenied),
        (
            lambda rows: [_make_agent(version=1, s3_prefix="users/u-jane/published/x")],
            ValueError,
        ),
        (lambda rows: [], NotFoundOrForbidden),
    ],
)
def test_preflight_raises_exactly_what_the_purge_would(mutate, expected):
    """A shown preflight has already cleared every gate.

    Otherwise the user types the confirmation phrase and *then* gets a 403.
    """
    lineage = mutate([])
    index, s3 = _make_index(lineage, working=None), _make_s3()
    kwargs = dict(category=_CATEGORY, published_id=_LINEAGE)

    with pytest.raises(expected):
        _run(preflight_asset_purge(ctx=_make_ctx(), asset_index=index, **kwargs))
    with pytest.raises(expected):
        _run(purge_asset_lineage(ctx=_make_ctx(), asset_index=index, s3=s3, **kwargs))


def test_preflight_is_recorded_on_the_purge_result():
    """So a purge that broke three workflows leaves evidence of which three."""
    lineage = [_make_agent(version=1, current=True, integration_enabled=True)]
    index, s3 = _make_index(lineage), _make_s3()
    hit = __import__("lab.operation_results", fromlist=["AgentReferenceHit"]).AgentReferenceHit(
        kind="workflow-step", asset_id="wf-1", name="Ingest",
        status="published", detail="step 'transcribe'",
    )

    result = _run(purge_asset_lineage(
        ctx=_make_ctx(), asset_index=index, s3=s3,
        category=_CATEGORY, published_id=_LINEAGE,
        hits=(hit,), partial=("org-tier",),
    ))

    assert result.preflight.hits == (hit,)
    assert result.preflight.partial == ("org-tier",)
    assert result.preflight.external_access_live is True
