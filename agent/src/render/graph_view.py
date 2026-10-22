"""The turn's "↳ Graph" render: merge what the knowledge-graph tools touched into one `graph-view` manifest.

Pure, no I/O — the graph extension of `render/moments.py`. The `render-graph` rule (`hooks.actions.graph_render`)
is the only caller. The manifest carries node ids, types, names and the sample flag; NO URL (the SPA reads nothing
else) and nothing beyond the active Project (every subgraph came from one Project's Cedar-scoped graph calls).

    {template: "graph-view", id, createdAt, title, project_id, turn_id, truncated,
     nodes: [{id, type, name, sample, focus}], edges: [[src, dst, rel]], calls: [{tool, nodes, edges}]}
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

MAX_NODES = 300
MAX_EDGES = 600


def graph_rid(session_id: str, turn_id: str) -> str:
    """Deterministic id per (session, turn): a repeated flush rewrites one object; the SPA keeps one tab."""
    return "graph-" + hashlib.sha256(f"graph|{session_id}|{turn_id}".encode()).hexdigest()[:24]


def merge(subgraphs: Iterable[dict]) -> dict:
    """Union of subgraphs: focus nodes first (in call order), then the rest; edges deduplicated; capped."""
    subgraphs = [s for s in subgraphs if isinstance(s, dict)]
    focus: list[str] = []
    nodes: dict[str, dict] = {}
    order: list[str] = []
    for sg in subgraphs:
        for f in sg.get("focus") or []:
            if f not in focus:
                focus.append(f)
    for sg in subgraphs:
        for n in sg.get("nodes") or []:
            nid = n.get("id") if isinstance(n, dict) else None
            if not isinstance(nid, str) or nid in nodes:
                continue
            nodes[nid] = {"id": nid, "type": str(n.get("type") or ""), "name": str(n.get("name") or nid)[:200],
                          "sample": bool(n.get("sample"))}
            order.append(nid)
    ranked = [f for f in focus if f in nodes] + [n for n in order if n not in focus]
    kept = ranked[:MAX_NODES]
    keep = set(kept)
    edges: list[list[str]] = []
    seen: set[tuple] = set()
    for sg in subgraphs:
        for e in sg.get("edges") or []:
            if not (isinstance(e, (list, tuple)) and len(e) == 3):
                continue
            t = (str(e[0]), str(e[1]), str(e[2]))
            if t in seen or t[0] not in keep or t[1] not in keep:
                continue
            seen.add(t)
            edges.append(list(t))
    truncated = len(ranked) > MAX_NODES or len(edges) > MAX_EDGES or any(s.get("truncated") for s in subgraphs)
    return {"nodes": [{**nodes[n], "focus": n in focus} for n in kept], "edges": edges[:MAX_EDGES],
            "truncated": bool(truncated)}


def build_graph_manifest(calls: Iterable[Any], *, session_id: str, turn_id: str, project_id: str,
                         project_name: str, created_at: str) -> dict | None:
    """The `graph-view` manifest for a turn's graph calls, or None when there is nothing to draw."""
    calls = list(calls)
    merged = merge(c.subgraph for c in calls)
    if not merged["nodes"]:
        return None
    return {
        "id": graph_rid(session_id, turn_id),
        "createdAt": created_at,
        "template": "graph-view",
        "title": f"Graph · {project_name}" if project_name else "Graph",
        "project_id": project_id,
        "turn_id": turn_id,
        "origin": "agent",
        **merged,
        "calls": [{"tool": c.tool, "nodes": len((c.subgraph or {}).get("nodes") or []),
                   "edges": len((c.subgraph or {}).get("edges") or [])} for c in calls],
    }
