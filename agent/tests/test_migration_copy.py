"""Unit tests for ``lab.migration.copy.migrate_one`` (Task 8.5).

Mirrors :mod:`tests.test_migration_bootstrap` in shape and exercises
the six interesting paths through ``migrate_one``:

- Resume: the ``legacy-skill::<prefix>`` marker is already present →
  the function returns the cached ``(publishedId, version)`` and
  issues NO S3 / AssetIndex / marker writes.
- Resume malformed: the marker exists but is missing ``publishedId`` →
  the function raises :class:`RuntimeError` rather than silently
  generating a duplicate row.
- Fresh real run: no marker → ``copy_object`` once per source object
  (pagination across 2 pages of 3 total objects), ``asset_index.put``
  once with the right :class:`Skill` shape, and ``mark_completed``
  written *last* with the generated ``publishedId``.
- Dry-run: no marker → no S3 / AssetIndex / marker writes; the
  function still returns a freshly-generated ``publishedId`` (uuid4
  hex).
- S3-OK / DDB-fail compensating delete: ``asset_index.put`` raises →
  the target prefix is enumerated via ``list_objects_v2`` and deleted
  via ``delete_objects``, the original error is re-raised (wrapped
  with the source-key context per Req 14.5), and the marker is
  never written.
- ScopeHistory shape: a single initial entry with ``from is None``,
  ``to == "org"``, the admin user as ``actorId``, and a parseable
  ISO-8601 ``timestamp`` ending in ``Z``.

All cases assert that the legacy source prefix is **never** deleted —
the migration treats it as immutable for this sprint (per Req 14.5).

Validates: Requirements 14.1, 14.2, 14.5, 14.7
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to path so imports resolve.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration import copy as copy_mod  # noqa: E402
from lab.migration.discovery import LegacySkill  # noqa: E402
from lab.models import Skill  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ADMIN_USER = "u-admin"
ADMIN_ORG = "o-acme"
BUCKET = "test-bucket"

LEGACY_PREFIX = "skills/my-skill/"
LEGACY_NAME = "my-skill"
LEGACY = LegacySkill(legacy_key_prefix=LEGACY_PREFIX, name=LEGACY_NAME)

# Lowercase hex matching ``uuid.uuid4().hex``: 32 chars, [0-9a-f]. The
# migration is documented to use this convention so dry-run / real-run
# ids are indistinguishable from a publishedId produced by Promote.
_PID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_asset_index(*, marker_value=None) -> MagicMock:
    """Build a MagicMock AssetIndex.

    ``get_marker`` resolves to ``marker_value`` (default ``None`` =
    no marker present); ``put_marker`` and ``put`` are no-op
    AsyncMocks. The bootstrap-style migrate_one calls ``put_marker``
    indirectly via :func:`marker.mark_completed`.
    """
    index = MagicMock()
    index.get_marker = AsyncMock(return_value=marker_value)
    index.put_marker = AsyncMock(return_value=None)
    index.put = AsyncMock(return_value=None)
    return index


def _make_paginator(pages: list[dict]) -> MagicMock:
    """Build a ``get_paginator`` return value whose ``paginate`` yields ``pages``.

    Returns the paginator (not a function) so the caller can wire it
    onto ``s3.get_paginator.return_value`` or, when multiple paginators
    are needed (copy + compensating delete), via ``side_effect``.
    """
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    return paginator


def _make_s3(
    *,
    list_pages: list[dict] | None = None,
    list_pages_factory=None,
) -> MagicMock:
    """Build a MagicMock boto3 ``s3`` client.

    ``list_pages`` wires a single fixed paginator. ``list_pages_factory``
    is a callable returning a fresh paginator per ``get_paginator``
    call — useful when the same test exercises both the copy-time
    and compensating-delete-time list calls (each consumes its own
    iterator).
    """
    s3 = MagicMock()
    s3.copy_object = MagicMock(return_value={})
    s3.delete_objects = MagicMock(return_value={})
    if list_pages_factory is not None:
        s3.get_paginator = MagicMock(side_effect=lambda name: list_pages_factory())
    else:
        pages = list_pages if list_pages is not None else [{}]
        s3.get_paginator = MagicMock(return_value=_make_paginator(pages))
    return s3


# ---------------------------------------------------------------------------
# Resume path
# ---------------------------------------------------------------------------


def test_resume_returns_cached_pid_and_version_without_writes() -> None:
    """When the marker is present, return the stored ``(publishedId,
    version)`` and issue NO S3 / AssetIndex / marker writes."""
    existing_pid = "existing-pid-1234"
    index = _make_asset_index(
        marker_value={"publishedId": existing_pid, "version": 1}
    )
    s3 = _make_s3()

    result = copy_mod.migrate_one(
        s3=s3,
        asset_index=index,
        legacy_skill=LEGACY,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=False,
    )

    assert result == (existing_pid, 1)

    # No S3 work on the resume path.
    s3.copy_object.assert_not_called()
    s3.delete_objects.assert_not_called()

    # No AssetIndex put or marker write.
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()

    # The resume path reads exactly the legacy-skill marker.
    index.get_marker.assert_awaited_once_with(
        f"legacy-skill::{LEGACY_PREFIX}"
    )


def test_resume_with_missing_published_id_raises() -> None:
    """A marker without a ``publishedId`` payload is a migration bug;
    the function MUST surface it loudly rather than create a duplicate
    row under a fresh id."""
    index = _make_asset_index(marker_value={"version": 1})  # no publishedId
    s3 = _make_s3()

    with pytest.raises(RuntimeError, match="publishedId"):
        copy_mod.migrate_one(
            s3=s3,
            asset_index=index,
            legacy_skill=LEGACY,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    s3.copy_object.assert_not_called()
    s3.delete_objects.assert_not_called()
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fresh-run real path
# ---------------------------------------------------------------------------


def test_fresh_run_copies_every_object_then_puts_then_marker() -> None:
    """No marker → ``copy_object`` once per source object (across two
    paginated pages totalling 3 objects), then a single ``asset_index.put``
    of a published Skill row, then ``mark_completed`` last.

    Verifies the destination keys preserve nested suffixes, that the
    legacy source prefix is NEVER deleted, and that the marker is
    written with the generated ``publishedId``.
    """
    pages = [
        {
            "Contents": [
                {"Key": f"{LEGACY_PREFIX}skill.json"},
                {"Key": f"{LEGACY_PREFIX}icon.png"},
            ]
        },
        {
            "Contents": [
                {"Key": f"{LEGACY_PREFIX}assets/extra.txt"},
            ]
        },
    ]
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3(list_pages=pages)

    result = copy_mod.migrate_one(
        s3=s3,
        asset_index=index,
        legacy_skill=LEGACY,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=False,
    )

    pid, version = result
    assert version == 1
    assert _PID_RE.match(pid), f"unexpected pid format: {pid!r}"

    expected_target_prefix = (
        f"org/{ADMIN_ORG}/published/skills/{pid}/v1/"
    )

    # ---- S3 copy_object: one call per source object, dest_key keeps
    # the legacy suffix.
    assert s3.copy_object.call_count == 3
    copy_calls = s3.copy_object.call_args_list

    expected_pairs = [
        (f"{LEGACY_PREFIX}skill.json",
         f"{expected_target_prefix}skill.json"),
        (f"{LEGACY_PREFIX}icon.png",
         f"{expected_target_prefix}icon.png"),
        (f"{LEGACY_PREFIX}assets/extra.txt",
         f"{expected_target_prefix}assets/extra.txt"),
    ]
    actual_pairs = [
        (call.kwargs["CopySource"]["Key"], call.kwargs["Key"])
        for call in copy_calls
    ]
    assert actual_pairs == expected_pairs

    # All copies stay inside the same bucket.
    for call in copy_calls:
        assert call.kwargs["Bucket"] == BUCKET
        assert call.kwargs["CopySource"]["Bucket"] == BUCKET

    # The list paginator was called with the LEGACY prefix (read-only).
    s3.get_paginator.assert_called_with("list_objects_v2")
    paginator = s3.get_paginator.return_value
    paginator.paginate.assert_called_once_with(
        Bucket=BUCKET, Prefix=LEGACY_PREFIX
    )

    # ---- AssetIndex put: a single Skill row with the right shape.
    index.put.assert_awaited_once()
    put_call = index.put.await_args
    asset = (
        put_call.args[0] if put_call.args else put_call.kwargs.get("asset")
    )
    assert isinstance(asset, Skill)
    assert asset.asset_id == pid
    assert asset.category == "skill"
    assert asset.name == LEGACY_NAME
    assert asset.status == "published"
    assert asset.scope == "org"
    assert asset.version == 1
    assert asset.current is True
    assert asset.s3_prefix == expected_target_prefix
    assert asset.publish_policy is not None
    assert asset.publish_policy.owner_ids == [ADMIN_USER]
    assert asset.publish_policy.contributor_ids == []
    assert asset.promoted_from is None
    assert asset.last_published_as is None
    assert asset.cloned_from is None

    # owner_org_id passed through so AssetIndex.put validates the
    # prefix against the org-scope partition.
    assert put_call.kwargs.get("owner_org_id") == ADMIN_ORG

    # ---- Marker last: written with the new pid + version.
    index.put_marker.assert_awaited_once()
    marker_args = index.put_marker.await_args
    assert marker_args.args[0] == f"legacy-skill::{LEGACY_PREFIX}"
    assert marker_args.args[1] == {"publishedId": pid, "version": 1}

    # ---- Legacy source NEVER deleted.
    s3.delete_objects.assert_not_called()


def test_fresh_run_scope_history_has_one_initial_entry() -> None:
    """The migrated row carries exactly one scopeHistory entry with
    ``from is None``, ``to == "org"``, the admin user as ``actorId``,
    and a parseable ISO-8601 ``timestamp`` ending in ``Z``."""
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3(
        list_pages=[{"Contents": [{"Key": f"{LEGACY_PREFIX}skill.json"}]}]
    )

    copy_mod.migrate_one(
        s3=s3,
        asset_index=index,
        legacy_skill=LEGACY,
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

    # The migration renders ISO-8601 with a trailing 'Z';
    # ``datetime.fromisoformat`` accepts the ``+00:00`` form so we
    # swap the suffix back before parsing.
    iso = entry.timestamp
    assert iso.endswith("Z"), f"expected trailing 'Z': {iso!r}"
    parseable = iso[:-1] + "+00:00"
    parsed = datetime.fromisoformat(parseable)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_returns_a_pid() -> None:
    """Dry-run with no marker MUST NOT touch S3 / DDB / marker but
    still returns ``(uuid-hex, 1)`` so the rewrite stage has a stable
    target name in the dry-run report."""
    index = _make_asset_index(marker_value=None)
    s3 = _make_s3()

    result = copy_mod.migrate_one(
        s3=s3,
        asset_index=index,
        legacy_skill=LEGACY,
        admin_user=ADMIN_USER,
        admin_org=ADMIN_ORG,
        bucket=BUCKET,
        dry_run=True,
    )

    pid, version = result
    assert version == 1
    assert _PID_RE.match(pid), f"unexpected pid format: {pid!r}"

    # No S3 work whatsoever — neither the copy nor a list call.
    s3.copy_object.assert_not_called()
    s3.delete_objects.assert_not_called()
    s3.get_paginator.assert_not_called()

    # No AssetIndex put or marker write.
    index.put.assert_not_awaited()
    index.put_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Compensating delete
# ---------------------------------------------------------------------------


def test_asset_index_put_failure_compensates_with_target_delete() -> None:
    """If ``asset_index.put`` raises after a successful S3 copy, the
    migration MUST sweep the target prefix via list+delete_objects and
    re-raise the original error (wrapped with the source-key context
    per Req 14.5). The legacy source prefix is NEVER touched."""
    # Two paginate() calls happen on the same s3 object: one for the
    # copy (over LEGACY_PREFIX) and one for the compensating delete
    # (over the target prefix). Each needs its own iterator.
    legacy_objects = [
        {"Key": f"{LEGACY_PREFIX}skill.json"},
        {"Key": f"{LEGACY_PREFIX}icon.png"},
    ]
    # Compensating delete enumerates the target. Use a placeholder key
    # — the migrate_one code computes the dest key from the source by
    # ``replace(source_prefix, target_prefix, 1)``, but the target
    # paginator below is independent (a fresh fake list).

    # We can't know the exact target prefix at fixture-build time
    # (uuid is generated inside migrate_one), so the compensating-delete
    # paginator returns a single fake key under whatever prefix the
    # caller asked for. We capture it via a closure.
    captured_target_prefix: dict[str, str] = {}

    paginator_calls = {"count": 0}

    def factory():
        # First call → copy-time list (over LEGACY_PREFIX): real keys.
        # Second call → compensating-delete-time list (over the target
        # prefix). We synthesize a single object so delete_objects is
        # invoked with at least one chunk.
        paginator_calls["count"] += 1
        if paginator_calls["count"] == 1:
            return _make_paginator([{"Contents": legacy_objects}])

        def synthetic_pages(Bucket: str, Prefix: str):  # noqa: N803
            captured_target_prefix["prefix"] = Prefix
            return iter([
                {"Contents": [{"Key": f"{Prefix}skill.json"},
                              {"Key": f"{Prefix}icon.png"}]}
            ])

        paginator = MagicMock()
        paginator.paginate.side_effect = synthetic_pages
        return paginator

    s3 = _make_s3(list_pages_factory=factory)

    boom = RuntimeError("ddb conditional check failed")
    index = _make_asset_index(marker_value=None)
    index.put = AsyncMock(side_effect=boom)

    with pytest.raises(RuntimeError) as excinfo:
        copy_mod.migrate_one(
            s3=s3,
            asset_index=index,
            legacy_skill=LEGACY,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    # The wrapper error names the legacy source key (Req 14.5); the
    # original error is preserved as ``__cause__``.
    assert LEGACY_PREFIX in str(excinfo.value)
    assert excinfo.value.__cause__ is boom

    # ---- S3 copy did happen (S3 first, then DDB).
    assert s3.copy_object.call_count == 2

    # ---- Compensating delete swept the target prefix.
    assert "prefix" in captured_target_prefix
    target_prefix = captured_target_prefix["prefix"]
    assert target_prefix.startswith(
        f"org/{ADMIN_ORG}/published/skills/"
    )
    assert target_prefix.endswith("/v1/")
    # The target prefix is NOT the legacy prefix — we never touch it.
    assert target_prefix != LEGACY_PREFIX

    s3.delete_objects.assert_called_once()
    delete_call = s3.delete_objects.call_args
    assert delete_call.kwargs["Bucket"] == BUCKET
    delete_keys = [
        obj["Key"] for obj in delete_call.kwargs["Delete"]["Objects"]
    ]
    # Every deleted key sits under the target prefix (NEVER the
    # legacy source).
    assert all(k.startswith(target_prefix) for k in delete_keys)
    assert all(not k.startswith(LEGACY_PREFIX) for k in delete_keys)

    # ---- Marker was NOT written on the failure path.
    index.put_marker.assert_not_awaited()


def test_compensation_failure_still_raises_with_source_context() -> None:
    """A compensating-delete failure does not mask the AssetIndex
    error: the wrapper RuntimeError still carries the source key and
    its ``__cause__`` is the original AssetIndex error."""
    legacy_objects = [{"Key": f"{LEGACY_PREFIX}skill.json"}]

    paginator_calls = {"count": 0}

    def factory():
        paginator_calls["count"] += 1
        if paginator_calls["count"] == 1:
            return _make_paginator([{"Contents": legacy_objects}])

        # Compensating-delete-time list: returns one synthetic key so
        # ``delete_objects`` is invoked (and raises).
        def synthetic_pages(Bucket: str, Prefix: str):  # noqa: N803
            return iter([
                {"Contents": [{"Key": f"{Prefix}skill.json"}]}
            ])

        paginator = MagicMock()
        paginator.paginate.side_effect = synthetic_pages
        return paginator

    s3 = _make_s3(list_pages_factory=factory)
    s3.delete_objects = MagicMock(side_effect=RuntimeError("s3 unreachable"))

    original = RuntimeError("ddb cancel")
    index = _make_asset_index(marker_value=None)
    index.put = AsyncMock(side_effect=original)

    with pytest.raises(RuntimeError) as excinfo:
        copy_mod.migrate_one(
            s3=s3,
            asset_index=index,
            legacy_skill=LEGACY,
            admin_user=ADMIN_USER,
            admin_org=ADMIN_ORG,
            bucket=BUCKET,
            dry_run=False,
        )

    # Wrapper carries the source key and the AssetIndex error is the
    # cause (the compensation error is logged, not propagated).
    assert LEGACY_PREFIX in str(excinfo.value)
    assert excinfo.value.__cause__ is original

    # The compensating delete was attempted; it just failed.
    s3.delete_objects.assert_called_once()
    index.put_marker.assert_not_awaited()
