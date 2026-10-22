"""Compact JSON shapes the proxy relays to the SPA.

Overview (positional arrays, type names interned once — ~0.07 KB/node, so 12k nodes ≈ 0.8 MB, far under the
6 MB Lambda invoke limit):

    {state, generation, truncated, totals:{nodes,edges}, types:{nodes:[...], edges:[...]},
     nodes:[[id, typeIdx, name, sample01, degree], ...], edges:[[srcIdx, dstIdx, typeIdx], ...]}

Above the caps the overview keeps the highest-degree nodes and only the edges among them, and says so with
``truncated: true`` rather than silently dropping data.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from .schema import NODE_LABELS, RELATIONSHIPS

MAX_NODES = 20_000
MAX_EDGES = 60_000

NodeRow = tuple[str, str, str, bool]          # id, label, name, sample
EdgeRow = tuple[str, str, str]                # src, dst, type


def overview(nodes: Iterable[NodeRow], edges: Iterable[EdgeRow], *, generation: int,
             max_nodes: int = MAX_NODES, max_edges: int = MAX_EDGES) -> dict[str, Any]:
    nodes = list(nodes)
    edges = [e for e in edges]
    degree: Counter = Counter()
    for s, d, _t in edges:
        degree[s] += 1
        degree[d] += 1
    total_nodes, total_edges = len(nodes), len(edges)
    truncated = False
    if total_nodes > max_nodes or total_edges > max_edges:
        truncated = True
        nodes = sorted(nodes, key=lambda n: (-degree[n[0]], n[0]))[:max_nodes]
    index = {n[0]: i for i, n in enumerate(nodes)}
    label_ix = {l: i for i, l in enumerate(NODE_LABELS)}
    rel_ix = {r: i for i, r in enumerate(RELATIONSHIPS)}
    out_edges = []
    for s, d, t in edges:
        si, di = index.get(s), index.get(d)
        if si is None or di is None:
            continue
        out_edges.append([si, di, rel_ix[t]])
        if len(out_edges) >= max_edges:
            truncated = truncated or len(out_edges) < total_edges
            break
    return {
        "state": "ready", "generation": generation, "truncated": truncated,
        "totals": {"nodes": total_nodes, "edges": total_edges},
        "types": {"nodes": list(NODE_LABELS), "edges": list(RELATIONSHIPS)},
        "nodes": [[n[0], label_ix[n[1]], n[2], 1 if n[3] else 0, degree[n[0]]] for n in nodes],
        "edges": out_edges,
    }


def neighbourhood(center: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]],
                  edges: Iterable[EdgeRow], *, hops: int, truncated: bool) -> dict[str, Any]:
    return {"state": "ready", "hops": hops, "truncated": truncated, "node": dict(center),
            "nodes": [dict(n) for n in nodes], "edges": [list(e) for e in edges]}
