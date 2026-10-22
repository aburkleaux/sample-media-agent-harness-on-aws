"""Per-turn record of what the knowledge-graph tools touched — the graph twin of the search ledger.

`ProjectGraphTool` (Main_Agent only) records each successful call's `subgraph`; at `session_end` the locked
`render-graph` rule takes the turn's entries, merges them, and renders one "↳ Graph" tab — the graph extension of
the Moments gallery. In-memory and bounded (the oldest turns are evicted), like `ledger.py`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

_MAX_TURNS = 64
_lock = threading.Lock()


@dataclass
class GraphCall:
    tool: str
    subgraph: dict


@dataclass
class TurnGraph:
    project_id: str
    calls: list[GraphCall] = field(default_factory=list)


_turns: "OrderedDict[str, TurnGraph]" = OrderedDict()


def record(turn_id: str, project_id: str, tool: str, subgraph: Any) -> None:
    if not turn_id or not isinstance(subgraph, dict) or not subgraph.get("nodes"):
        return
    with _lock:
        tg = _turns.get(turn_id)
        if tg is None or tg.project_id != project_id:
            tg = _turns[turn_id] = TurnGraph(project_id)
        tg.calls.append(GraphCall(tool, subgraph))
        _turns.move_to_end(turn_id)
        while len(_turns) > _MAX_TURNS:
            _turns.popitem(last=False)


def take(turn_id: str, project_id: str) -> Optional[TurnGraph]:
    """The turn's graph calls for `project_id`, removed from the ledger (a turn renders once)."""
    with _lock:
        tg = _turns.pop(turn_id, None)
    if tg is None or tg.project_id != project_id or not tg.calls:
        return None
    return tg


def _reset_for_test() -> None:
    with _lock:
        _turns.clear()
