"""Unit tests for ``lab.migration.marker``.

The module wraps :meth:`AssetIndex.get_marker` / :meth:`put_marker`
(both async) behind synchronous helpers used by the one-shot migration
driver. The tests use a ``MagicMock`` asset_index whose marker methods
are :class:`AsyncMock` instances, matching the real async API.

Validates: Requirements 14.7
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.migration import marker  # noqa: E402


def _make_asset_index(get_return=None) -> MagicMock:
    """Build a MagicMock AssetIndex with async marker methods.

    ``get_marker`` resolves to ``get_return`` (default ``None`` =
    no marker present). ``put_marker`` is a no-op AsyncMock so tests
    can assert on call args.
    """
    index = MagicMock()
    index.get_marker = AsyncMock(return_value=get_return)
    index.put_marker = AsyncMock(return_value=None)
    return index


# ---------------------------------------------------------------------------
# is_completed
# ---------------------------------------------------------------------------


def test_is_completed_returns_false_when_marker_absent() -> None:
    """``get_marker`` resolves to ``None`` → ``is_completed`` is ``False``."""
    index = _make_asset_index(get_return=None)

    result = marker.is_completed(index, "legacy-skill::skills/my-skill/")

    assert result is False
    index.get_marker.assert_awaited_once_with(
        "legacy-skill::skills/my-skill/"
    )


def test_is_completed_returns_true_when_marker_present() -> None:
    """``get_marker`` resolves to a dict payload → ``is_completed`` is ``True``."""
    index = _make_asset_index(
        get_return={"publishedId": "skill-123", "version": 1}
    )

    result = marker.is_completed(index, "legacy-skill::skills/my-skill/")

    assert result is True
    index.get_marker.assert_awaited_once_with(
        "legacy-skill::skills/my-skill/"
    )


def test_is_completed_returns_true_for_empty_dict_marker() -> None:
    """An empty-dict marker still indicates completion (presence, not payload)."""
    index = _make_asset_index(get_return={})

    result = marker.is_completed(index, "core-task-agent")

    assert result is True
    index.get_marker.assert_awaited_once_with("core-task-agent")


def test_is_completed_works_with_core_task_agent_key() -> None:
    """The helper is namespace-agnostic — caller builds the key."""
    index = _make_asset_index(get_return=None)

    assert marker.is_completed(index, "core-task-agent") is False
    index.get_marker.assert_awaited_once_with("core-task-agent")


# ---------------------------------------------------------------------------
# mark_completed
# ---------------------------------------------------------------------------


def test_mark_completed_passes_value_payload_through() -> None:
    """When a value dict is supplied, it is forwarded verbatim to put_marker."""
    index = _make_asset_index()
    payload = {"publishedId": "skill-abc", "version": 2}

    marker.mark_completed(
        index, "legacy-skill::skills/my-skill/", payload
    )

    index.put_marker.assert_awaited_once_with(
        "legacy-skill::skills/my-skill/", payload
    )


def test_mark_completed_with_none_value_passes_empty_dict() -> None:
    """``value=None`` defaults to an empty dict (the agreed sentinel)."""
    index = _make_asset_index()

    marker.mark_completed(index, "core-task-agent")

    index.put_marker.assert_awaited_once_with("core-task-agent", {})


def test_mark_completed_with_explicit_none_passes_empty_dict() -> None:
    """Explicitly passing ``value=None`` behaves the same as omitting it."""
    index = _make_asset_index()

    marker.mark_completed(index, "core-task-agent", None)

    index.put_marker.assert_awaited_once_with("core-task-agent", {})


def test_mark_completed_calls_put_marker_exactly_once() -> None:
    """A single mark_completed invocation MUST issue exactly one put_marker."""
    index = _make_asset_index()

    marker.mark_completed(
        index,
        "legacy-skill::skills/another/",
        {"publishedId": "p-1", "version": 1},
    )

    assert index.put_marker.await_count == 1


def test_mark_completed_does_not_read_first() -> None:
    """``mark_completed`` is a pure write — it MUST NOT call get_marker."""
    index = _make_asset_index()

    marker.mark_completed(index, "core-task-agent", {"agentId": "ag-1"})

    index.get_marker.assert_not_awaited()


# ---------------------------------------------------------------------------
# Integration-ish: round trip across the two helpers
# ---------------------------------------------------------------------------


def test_round_trip_uses_same_key_namespace() -> None:
    """A mark+is_completed pair using the same key targets the same record."""
    index = _make_asset_index(get_return={"publishedId": "p-1"})
    key = "legacy-skill::skills/round-trip/"

    marker.mark_completed(index, key, {"publishedId": "p-1"})
    assert marker.is_completed(index, key) is True

    index.put_marker.assert_awaited_once_with(key, {"publishedId": "p-1"})
    index.get_marker.assert_awaited_once_with(key)
