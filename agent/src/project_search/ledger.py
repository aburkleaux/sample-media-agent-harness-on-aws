"""Per-turn search ledger (Req 3.10, 7.10): every Search_Tool result of a turn, in memory only.

media-harness-search-qa-rendering reads it to verify citations and render Moments, scoped to the
turn's Active_Project. Never persisted; entries of one turn never leak into another.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

_MAX_TURNS = 64


@dataclass(frozen=True)
class SearchLedgerEntry:
    turn_id: str
    tool_use_id: str
    project_id: str
    ok: bool
    hits: list = field(default_factory=list)
    source: str = "search_tool"
    # Hits ranked after the model's `k` (compact.ShapedResult.overflow): the Moments tab only. NOT delivered
    # to the model, so verification, the Session_Hit_Store and `frame_keys_for` read `hits` alone.
    extra_hits: list = field(default_factory=list)
    # The call's `search_term`: the Moments tab pre-fills its search box with the turn's first one.
    search_term: str = ""
    # The call's query image, as the model passed it: {"assetId": "image:…"} or {"s3uri": "s3://…"}. The
    # Moments tab shows it as the search bar's image chip (turn_end query_ref). None for a text search.
    query_image: Any = None


_lock = threading.Lock()
_by_turn: dict[str, list[SearchLedgerEntry]] = {}


def record(entry: SearchLedgerEntry) -> None:
    with _lock:
        if entry.turn_id not in _by_turn and len(_by_turn) >= _MAX_TURNS:
            _by_turn.pop(next(iter(_by_turn)))  # bound memory on a long-lived container
        _by_turn.setdefault(entry.turn_id, []).append(entry)


def results_for(turn_id: str) -> list[SearchLedgerEntry]:
    with _lock:
        return list(_by_turn.get(turn_id, ()))


def clear_turn(turn_id: str) -> None:
    with _lock:
        _by_turn.pop(turn_id, None)


def hits_from_result(result: Any) -> list:
    """Hits of a successful search_content result (M2C returns `{"results": [...]}`)."""
    import json
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return []
    if isinstance(result, dict):
        hits = result.get("results")
        return list(hits) if isinstance(hits, list) else []
    return []


def frame_keys_for(turn_id: str, project_id: str) -> frozenset:
    """`frame_s3_key` of every delivered Hit recorded this turn under `project_id`.

    The per-turn subset of Component 2 source (c) (a hit's frame as a query image). The Session_Hit_Store
    (task 6.x) supplies the cross-turn keys; this turn's ledger supplies the ones delivered so far.
    """
    keys: set[str] = set()
    for entry in results_for(turn_id):
        if not entry.ok or entry.project_id != project_id:
            continue
        for hit in entry.hits:
            key = hit.get("frame_s3_key") if isinstance(hit, dict) else None
            if isinstance(key, str) and key:
                keys.add(key)
    return frozenset(keys)
