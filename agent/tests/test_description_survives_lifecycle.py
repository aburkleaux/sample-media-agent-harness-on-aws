"""``description`` survives publish, share, clone, and edit.

FOUR field-by-field builders construct an AssetCommon from another one: three
``common_kwargs`` dicts in ``_canonical_ops`` (publish / share / clone) and a
``base_kwargs`` dict in ``_lifecycle_ops`` (``_clone_to_working_copy``, the
``edit_asset`` fork path). None of them carry a new AssetCommon field
automatically, so each needs its own guard here.

A published version with an empty description would silently break discovery,
since ``list_custom_agents`` reads published rows. The edit path is the worst
of the four: it loses the description on a round trip the user initiates
deliberately (View published -> Edit -> Save), with no error.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.models import SpecialtyAgent  # noqa: E402

DESCRIPTION = "Cleans up ASR transcripts and fixes speaker labels"


def _draft(description: str = DESCRIPTION) -> SpecialtyAgent:
    now = 1_700_000_000_000
    return SpecialtyAgent(
        asset_id="draft-1",
        category="specialty-agent",
        name="Transcript Cleaner",
        status="draft",
        scope="private",
        s3_prefix="users/u1/drafts/specialty-agents/draft-1/",
        created_at=now,
        updated_at=now,
        description=description,
        compatible_skills=["sk-001"],
    )


def test_publish_snapshot_carries_the_description():
    from lab._canonical_ops import _build_published_asset

    published = _build_published_asset(
        draft=_draft(),
        published_id="pub-1",
        version=1,
        scope="private",
        target_prefix="users/u1/published/specialty-agents/pub-1/v1/",
        actor_id="u1",
        publish_policy_owner_ids=["u1"],
        group_ids=None,
        timestamp_iso="2026-08-15T00:00:00Z",
        timestamp_ms=1_700_000_000_001,
    )
    assert published.description == DESCRIPTION


def test_clone_carries_the_description():
    from lab._canonical_ops import _build_cloned_draft

    source = _draft()
    source.version = 3
    source.status = "published"
    cloned = _build_cloned_draft(
        source=source,
        draft_id="new-1",
        target_prefix="users/u1/drafts/specialty-agents/new-1/",
        timestamp_ms=1_700_000_000_002,
    )
    assert cloned.description == DESCRIPTION


def test_share_carries_the_description():
    from lab._canonical_ops import _build_shared_asset

    source = _draft()
    source.version = 3
    source.status = "published"
    shared = _build_shared_asset(
        source=source,
        new_scope="org",
        target_prefix="orgs/o1/published/specialty-agents/pub-1/v3/",
        actor_id="u1",
        group_ids=None,
        timestamp_iso="2026-08-15T00:00:00Z",
        timestamp_ms=1_700_000_000_003,
    )
    assert shared.description == DESCRIPTION


def test_edit_fork_to_working_copy_carries_the_description():
    """``edit_asset`` on a published Custom Agent must not blank the description.

    This is the fourth builder — ``base_kwargs`` in ``_lifecycle_ops``, not
    ``common_kwargs`` — and it is the one the UI's Edit button reaches
    (WorkspaceView -> editAsset('specialty-agent', id) -> edit_asset ->
    _clone_to_working_copy). The editor's Save unconditionally sends
    ``description``, so a blank here is written back as a blank on the next
    published version.
    """
    from lab._lifecycle_ops import _clone_to_working_copy

    source = _draft()
    source.version = 3
    source.status = "published"
    source.model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    source.tool_collections = ["tc-1"]

    working = _clone_to_working_copy(
        source=source,
        actor_id="u1",
        category="specialty-agent",
        asset_id="wc-1",
        forked_from_version=3,
        timestamp_ms=1_700_000_000_004,
    )

    assert working.description == DESCRIPTION
    # Sanity: the fixture is a realistic published agent, so a pass above is
    # not an artefact of an under-populated source. These three are the
    # carries the same function already documents.
    assert working.model == source.model
    assert working.tool_collections == ["tc-1"]
    assert working.version is None
    # The one extended field that must NOT carry: the version's pinned tool refs.
    # A working copy has to track the collections as they are now, and its next
    # publish re-resolves them — inheriting the pin would freeze the draft
    # against contents the owner can no longer change.
    assert working.tool_collection_snapshots is None
