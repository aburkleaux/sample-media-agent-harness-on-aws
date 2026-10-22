"""Unit tests for ``lab.migration.bootstrap.ensure_core_task_agent`` (Task 8.4).

Exercises the four interesting paths through the bootstrap:

- Resume: the ``core-task-agent`` marker is already present →
  the function returns the stored ``publishedId`` and issues NO S3,
  AssetIndex, or marker writes.
- Fresh real run: no marker → S3 ``put_object`` lands the agent.json
  body at ``org/<admin_org>/published/specialty-agents/<pid>/v1/``,
  AssetIndex ``put`` records the org-scoped Published_Record with the
  required shape, and the marker is written *last* with the new
  ``publishedId``.
- Dry-run: no marker → no S3 writes, no AssetIndex writes, no marker
  writes; the function still returns a freshly-generated ``publishedId``
  so the rewrite step has a stable target name in the dry-run report.
- S3-OK / DDB-fail compensating delete: ``asset_index.put`` raises →
  ``s3.delete_object`` is called with the agent.json key and the
  original error is re-raised.

A separate test asserts the scopeHistory shape: a single entry with
``from_scope is None``, ``to_scope == "org"``, ``actor_id == admin_user``,
and a syntactically-valid ISO-8601 ``timestamp``.

Validates: Requirement 14.4
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration import bootstrap  # noqa: E402
from lab.models import SpecialtyAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_USER = "u-admin"
ADMIN_ORG = "o-acme"
BUCKET = "test-bucket"

# Lowercase hex matching ``uuid.uuid4().hex``: 32 chars, [0-9a-f]. The
# bootstrap is documented to use this convention so dry-run / real-run
# ids are indistinguishable from a publishedId produced by Promote.
_PID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_asset_index(*, marker_value=None) -> MagicMock:
    """Build a MagicMock AssetIndex.

    ``get_marker`` resolves to ``marker_value`` (default ``None`` =
    no marker); ``put_marker`` is a no-op AsyncMock; ``put`` is a
    no-op AsyncMock. The bootstrap calls ``put_marker`` indirectly
    via :func:`marker.mark_completed`, so we install both surfaces.
    """
    index = MagicMock()
    index.get_marker = AsyncMock(return_value=marker_value)
    index.put_marker = AsyncMock(return_value=None)
    index.put = AsyncMock(return_value=None)
    return index


def _make_s3() -> MagicMock:
    """Build a MagicMock boto3 ``s3`` client.

    Only ``put_object`` and ``delete_object`` are exercised by the
    bootstrap; both are synchronous methods on a real boto3 client and
    are wired here as plain ``MagicMock`` attributes (NOT AsyncMock).
    """
    s3 = MagicMock()
    s3.put_object = MagicMock(return_value={})
    s3.delete_object = MagicMock(return_value={})
    return s3


# ---------------------------------------------------------------------------
# Resume path
# ---------------------------------------------------------------------------


def test_resume_returns_existing_published_id_without_writes() -> None:
    """When the marker is present, the function returns the stored
    publishedId and issues NO S3, AssetIndex, or marker writes."""
    existing_pid = "existing-pid-1234"
    index = _make_asset_index(
        marker_value={"publishedId": existing_pid, "version": 1}
    )
    s3 = _make_s3()

    result = bootstrap.ensure_core_task_agent(
        asset_index=index,
        s3=s3,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=False,
    )

    assert result == existing_pid

    # No S3 / DDB writes on the resume path.
    s3.put_object.assert_not_called()
    s3.delete_object.assert_not_called()
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()

    # The resume path reads exactly the core-task-agent marker.
    index.get_marker.assert_awaited_once_with("core-task-agent")


def test_resume_with_missing_published_id_raises() -> None:
    """A marker without a ``publishedId`` payload is a bootstrap bug;
    the function must surface it loudly rather than create a duplicate
    agent under a fresh id."""
    index = _make_asset_index(marker_value={"version": 1})  # no publishedId
    s3 = _make_s3()

    with pytest.raises(RuntimeError, match="publishedId"):
        bootstrap.ensure_core_task_agent(
            asset_index=index,
            s3=s3,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    s3.put_object.assert_not_called()
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fresh-run real path
# ---------------------------------------------------------------------------


def test_fresh_run_writes_s3_then_asset_index_then_marker() -> None:
    """No marker → S3 put, AssetIndex put, then marker (in that order).

    Verifies the destination key, the AssetIndex row's identifying
    fields, and that the marker write happens last with the
    generated ``publishedId``.
    """
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3()

    result = bootstrap.ensure_core_task_agent(
        asset_index=index,
        s3=s3,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=False,
    )

    # Returned publishedId looks like a uuid4 hex.
    assert _PID_RE.match(result), f"unexpected pid format: {result!r}"

    # ---- S3 ----------------------------------------------------------
    s3.put_object.assert_called_once()
    call = s3.put_object.call_args
    assert call.kwargs["Bucket"] == BUCKET
    expected_key = (
        f"org/{ADMIN_ORG}/published/specialty-agents/{result}/v1/agent.json"
    )
    assert call.kwargs["Key"] == expected_key

    # Body is JSON with at least name/description so it round-trips.
    body = call.kwargs["Body"]
    assert isinstance(body, (bytes, bytearray))
    decoded = json.loads(body.decode("utf-8"))
    assert "name" in decoded
    assert "description" in decoded

    # ---- AssetIndex --------------------------------------------------
    index.put.assert_awaited_once()
    put_call = index.put.await_args
    asset = put_call.args[0] if put_call.args else put_call.kwargs.get("asset")
    assert isinstance(asset, SpecialtyAgent)
    assert asset.asset_id == result
    assert asset.category == "specialty-agent"
    assert asset.status == "published"
    assert asset.scope == "org"
    assert asset.version == 1
    assert asset.current is True
    assert asset.publish_policy is not None
    assert asset.publish_policy.owner_ids == [ADMIN_USER]
    assert asset.publish_policy.contributor_ids == []
    expected_prefix = (
        f"org/{ADMIN_ORG}/published/specialty-agents/{result}/v1/"
    )
    assert asset.s3_prefix == expected_prefix

    # owner_org_id passed through so AssetIndex.put can validate the
    # prefix and route the row to ORG#<o>#PUBLISHED.
    assert put_call.kwargs.get("owner_org_id") == ADMIN_ORG

    # ---- Marker (last) ----------------------------------------------
    index.put_marker.assert_awaited_once()
    marker_args = index.put_marker.await_args
    assert marker_args.args[0] == "core-task-agent"
    assert marker_args.args[1]["publishedId"] == result
    assert marker_args.args[1]["version"] == 1

    # No compensating delete on the happy path.
    s3.delete_object.assert_not_called()


def test_fresh_run_scope_history_has_one_initial_entry() -> None:
    """The bootstrapped row carries exactly one scopeHistory entry with
    ``from is None``, ``to == "org"``, the admin user as ``actorId``,
    and a parseable ISO-8601 ``timestamp``."""
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3()

    bootstrap.ensure_core_task_agent(
        asset_index=index,
        s3=s3,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=False,
    )

    asset = index.put.await_args.args[0]
    assert len(asset.scope_history) == 1
    entry = asset.scope_history[0]
    assert entry.from_scope is None
    assert entry.to_scope == "org"
    assert entry.actor_id == ADMIN_USER
    assert entry.group_ids is None

    # Parse the timestamp. The bootstrap renders ISO-8601 with a
    # trailing 'Z'; ``datetime.fromisoformat`` accepts the
    # ``+00:00`` form, so swap the suffix back before parsing.
    iso = entry.timestamp
    assert iso.endswith("Z"), f"expected trailing 'Z': {iso!r}"
    parseable = iso[:-1] + "+00:00"
    parsed = datetime.fromisoformat(parseable)
    assert parsed.tzinfo is not None


def test_fresh_run_marker_written_after_asset_index() -> None:
    """The marker MUST be written only after AssetIndex.put has resolved.

    The bootstrap commits S3 → AssetIndex → marker; this test enforces
    the ordering by failing the AssetIndex put and asserting that the
    marker was never touched.
    """
    index = _make_asset_index(marker_value=None)
    index.put = AsyncMock(side_effect=RuntimeError("ddb broke"))
    s3 = _make_s3()

    with pytest.raises(RuntimeError, match="ddb broke"):
        bootstrap.ensure_core_task_agent(
            asset_index=index,
            s3=s3,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    # Marker was NOT written when AssetIndex.put failed.
    index.put_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_returns_a_pid() -> None:
    """Dry-run with no marker MUST NOT touch S3 / DDB / marker but
    still returns a freshly-generated publishedId so the rewrite stage
    has a stable target name in the dry-run report."""
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3()

    result = bootstrap.ensure_core_task_agent(
        asset_index=index,
        s3=s3,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=True,
    )

    assert isinstance(result, str)
    assert _PID_RE.match(result), f"unexpected pid format: {result!r}"

    # No writes whatsoever.
    s3.put_object.assert_not_called()
    s3.delete_object.assert_not_called()
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compensating delete
# ---------------------------------------------------------------------------


def test_asset_index_put_failure_compensates_with_s3_delete() -> None:
    """If ``asset_index.put`` raises after a successful S3 put, the
    bootstrap MUST issue a compensating ``delete_object`` for the
    agent.json key and re-raise the original error."""
    index = _make_asset_index(marker_value=None)
    boom = RuntimeError("conditional check failed")
    index.put = AsyncMock(side_effect=boom)
    s3 = _make_s3()

    with pytest.raises(RuntimeError, match="conditional check failed"):
        bootstrap.ensure_core_task_agent(
            asset_index=index,
            s3=s3,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    # The agent.json was put, then deleted on compensation.
    s3.put_object.assert_called_once()
    put_key = s3.put_object.call_args.kwargs["Key"]
    s3.delete_object.assert_called_once()
    del_call = s3.delete_object.call_args
    assert del_call.kwargs["Bucket"] == BUCKET
    assert del_call.kwargs["Key"] == put_key

    # No marker was written on the failure path.
    index.put_marker.assert_not_awaited()


def test_compensation_failure_still_raises_original_error() -> None:
    """A compensating-delete failure does not mask the AssetIndex
    error; the original error is the one the caller sees."""
    index = _make_asset_index(marker_value=None)
    index.put = AsyncMock(side_effect=RuntimeError("ddb cancel"))
    s3 = _make_s3()
    s3.delete_object = MagicMock(side_effect=RuntimeError("s3 unreachable"))

    with pytest.raises(RuntimeError, match="ddb cancel"):
        bootstrap.ensure_core_task_agent(
            asset_index=index,
            s3=s3,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    # The compensating delete was attempted; it just failed.
    s3.delete_object.assert_called_once()
    index.put_marker.assert_not_awaited()
