"""Migration idempotency markers — synchronous helpers around AssetIndex.

Wraps :meth:`AssetIndex.get_marker` / :meth:`AssetIndex.put_marker` in
helpers keyed by the migration's two marker namespaces:

- ``legacy-skill::<legacy_key_prefix>`` — written after both the S3
  copy and the AssetIndex put for one legacy skill commit (Requirement
  14.7). Existence implies the (publishedId, version) row is in place.
- ``core-task-agent`` — written after the bootstrap path commits the
  org-scoped Core_Task_Agent (Task 8.4). Existence implies the agent's
  S3 prefix and AssetIndex row both exist.

These helpers are agnostic to the marker namespace — ``cli.py`` builds
the keys (``legacy-skill::<prefix>`` and ``core-task-agent``) and
passes them through. The functions intentionally hide the asyncio
nature of :meth:`AssetIndex.get_marker` / :meth:`put_marker` from the
synchronous driver: the driver runs as a one-shot script under
``python -m`` and would rather ``asyncio.run`` once per call than
restructure the entire migration as async.

Markers are written ONLY after both the S3 copy and the AssetIndex
commit succeed — that ordering is the responsibility of the caller
(``copy.migrate_one`` and ``bootstrap.ensure_core_task_agent``); this
module only provides the synchronous read/write seam.

Requirements: 14.7
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional


def is_completed(asset_index: Any, key: str) -> bool:
    """Return ``True`` if a marker for ``key`` exists.

    Synchronous wrapper around :meth:`AssetIndex.get_marker`. The
    AssetIndex API is async; the migration driver is one-shot and
    synchronous, so each call spins a fresh event loop via
    :func:`asyncio.run`. That is acceptable here because (a) the
    migration is not on a hot path, (b) there is no surrounding loop
    to share, and (c) per-call loops keep the helper trivially safe to
    invoke from any thread.

    Args:
        asset_index: The :class:`AssetIndex` instance.
        key: The marker key (e.g. ``"legacy-skill::skills/my-skill/"``
            or ``"core-task-agent"``).

    Returns:
        ``True`` if the marker exists, ``False`` otherwise.
    """
    result = asyncio.run(asset_index.get_marker(key))
    return result is not None


def mark_completed(
    asset_index: Any,
    key: str,
    value: Optional[dict[str, Any]] = None,
) -> None:
    """Persist a marker for ``key`` with the given ``value`` payload.

    Synchronous wrapper around :meth:`AssetIndex.put_marker`. Called by
    ``copy.migrate_one`` and ``bootstrap.ensure_core_task_agent`` AFTER
    both the S3 copy and the AssetIndex commit succeed (per the
    design's "Migration markers" rule that markers MUST NOT be written
    until the underlying work is durable).

    Args:
        asset_index: The :class:`AssetIndex` instance.
        key: The marker key.
        value: An optional payload (e.g. ``{"publishedId": ..., "version": 1}``).
            Defaults to an empty dict so the marker still records the
            commit fact via :meth:`AssetIndex.put_marker`, which
            requires a ``dict`` value.
    """
    payload: dict[str, Any] = value if value is not None else {}
    asyncio.run(asset_index.put_marker(key, payload))
