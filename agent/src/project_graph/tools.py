"""Agent-facing graph tools, served as an AgentCore Gateway **Lambda target** (`project-graph`) on the
project Search_Gateway — the knowledge-graph extension of project search.

Privacy is exactly search's: the gateway accepts only a Project_Token (issued after a membership check) and its
ENFORCE-mode Cedar policy permits `project-graph___<tool>` only when the call's `project_id` equals the token's
`project_id` claim. This module therefore never decides WHO may read a Project; it only loads that one Project's
snapshot and answers within it. Everything is read-only (the graph writer is the sole write path).

`TOOL_SPECS` is the single source of the tools' MCP schemas; `tool_schema.json` (the inline schema CDK gives the
gateway target) is generated from it — `python -m project_graph.tools --emit-schema` — and a test pins the two.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any, Optional

from projects.ids import is_project_id

from . import cypher_guard, ids
from .reader import GraphReader, Snapshot
from .schema import NODE_LABELS, RELATIONSHIPS, edge_table, node_table

TARGET_NAME = "project-graph"
MAX_ROWS = cypher_guard.MAX_ROWS
TEXT_CAP = 300

_PID = {"type": "string", "description": "The active Project's id (the harness fills this in)."}

# The gateway's inline tool schema supports only type / description / properties / required / items (no enum,
# minimum, maxItems …), so allowed values and ranges are stated in descriptions and enforced by the tools below.
_LABELS_TXT = ", ".join(NODE_LABELS)
_RELS_TXT = ", ".join(RELATIONSHIPS)

TOOL_SPECS: list[dict] = [
    {"name": "graph_schema",
     "description": ("The active Project's knowledge-graph schema: node labels and relationship types (with their "
                     "source → destination labels), row counts, columns, id formats, and the query rules. Call once "
                     "before writing graph_query."),
     "inputSchema": {"type": "object", "properties": {"project_id": _PID}, "required": ["project_id"]}},
    {"name": "graph_find",
     "description": ("Find graph nodes whose name contains `text` (case-insensitive). Use it first to turn names in the "
                     "user's question (a person, place, topic, file) into node ids."),
     "inputSchema": {"type": "object", "properties": {
         "project_id": _PID,
         "text": {"type": "string", "description": "Name or part of a name."},
         "types": {"type": "array", "items": {"type": "string"},
                   "description": f"Only these node labels: any of {_LABELS_TXT}."},
         "include_sample": {"type": "boolean", "description": "Include the starter sample graph (default true)."},
         "limit": {"type": "integer", "description": "1–50 (default 20)."}},
         "required": ["project_id", "text"]}},
    {"name": "graph_neighbours",
     "description": "Nodes connected to `node_id`, optionally only some relationship types / one direction, 1–2 hops.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": _PID,
         "node_id": {"type": "string", "description": "A node id from graph_find (e.g. person:…, asset:…)."},
         "relationships": {"type": "array", "items": {"type": "string"},
                           "description": f"Only these relationship types: any of {_RELS_TXT}."},
         "direction": {"type": "string", "description": "both (default), out, or in."},
         "hops": {"type": "integer", "description": "1 (default) or 2."},
         "limit": {"type": "integer", "description": f"Max relationships returned, 1–{MAX_ROWS} (default 100)."}},
         "required": ["project_id", "node_id"]}},
    {"name": "graph_paths",
     "description": "How two nodes are connected: up to `limit` shortest paths of at most `max_hops` (≤ 3) relationships.",
     "inputSchema": {"type": "object", "properties": {
         "project_id": _PID,
         "from_id": {"type": "string", "description": "Start node id."},
         "to_id": {"type": "string", "description": "End node id."},
         "max_hops": {"type": "integer", "description": "1–3 (default 3)."},
         "limit": {"type": "integer", "description": "Max paths, 1–10 (default 3)."}},
         "required": ["project_id", "from_id", "to_id"]}},
    {"name": "graph_context",
     "description": ("Graph context for project files — e.g. the `asset_id`s of search hits: their scenes and the people, "
                     "locations, topics and organizations linked to them. Use it to enrich and explain search results."),
     "inputSchema": {"type": "object", "properties": {
         "project_id": _PID,
         "asset_ids": {"type": "array", "items": {"type": "string"},
                       "description": "1–20 asset ids (as returned by search hits) or asset node ids."},
         "start_ms": {"type": "integer", "description": "Optional: only scenes overlapping this start (ms)."},
         "end_ms": {"type": "integer", "description": "Optional: only scenes overlapping this end (ms)."}},
         "required": ["project_id", "asset_ids"]}},
    {"name": "graph_query",
     "description": ("Run a READ-ONLY Cypher query on the active Project's graph. One `MATCH … [WHERE …] RETURN …`; every "
                     "node labelled and every relationship typed (see graph_schema); patterns are directed; no OPTIONAL "
                     "MATCH / WITH / writes; variable-length up to *1..3; `$name` parameters via `params`; at most "
                     f"{MAX_ROWS} rows. Prefer the typed tools when one fits."),
     "inputSchema": {"type": "object", "properties": {
         "project_id": _PID,
         "cypher": {"type": "string", "description": "The read-only Cypher query."},
         "params": {"type": "object", "description": "Values for $name parameters (strings or numbers)."}},
         "required": ["project_id", "cypher"]}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOL_SPECS)

QUERY_RULES = [
    "Node ids: project:<pid>, user:<sub>, tag:<tag>, asset:<assetId>, session:<sid>, job:<jobId>, "
    "sample:<Label>:<n>.",
    "Lineage: DERIVED_FROM points from a derived asset TO its input, so what CAME FROM a clip is its "
    "incoming DERIVED_FROM and what WENT INTO a cut is its outgoing one; PRODUCED_BY / OUTPUT_OF name "
    "the session and the job that made an asset.",
    "Rows with sample=true are the starter SAMPLE graph — illustrative only, never cite them as facts about the project.",
    "Cypher: label every node, type every relationship, write patterns in their schema direction "
    "(an undirected -[:R]- only returns outgoing edges); add b.id <> a.id when both ends share a label.",
    "Not supported: OPTIONAL MATCH, WITH, UNION, writes, shortestPath (use graph_paths). Use count()/collect()/DISTINCT/ORDER BY.",
]


# Every successful result also carries `subgraph` — the nodes/relationships it touched, bounded — which the
# agent-side wrapper strips from the model's text and records for the turn's "↳ Graph" render (the graph
# extension of the Moments gallery). Shape: {nodes:[{id,type,name,sample}], edges:[[src,dst,rel]], focus:[ids]}.
SUBGRAPH_MAX_NODES = 300
SUBGRAPH_MAX_EDGES = 600


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _cap(v: Any) -> Any:
    if isinstance(v, str) and len(v) > TEXT_CAP:
        return v[:TEXT_CAP] + "…"
    return v


class GraphTools:
    def __init__(self, reader: GraphReader):
        self.reader = reader

    # ---- helpers ------------------------------------------------------------------------------------
    def _snapshot(self, pid: str) -> tuple[Optional[Snapshot], Optional[dict]]:
        snap, state = self.reader.snapshot(pid)
        if snap is not None:
            return snap, None
        return None, {"off": _err("graph_off", "This project has no knowledge graph."),
                      "seeding": _err("graph_seeding", "The project's knowledge graph is still being built; try again shortly."),
                      }.get(state or "", _err("project_not_found", "Project not found"))

    @staticmethod
    def _node(snap: Snapshot, nid: str) -> Optional[dict]:
        hit = snap.node_index().get(nid)
        if hit is None:
            return None
        label, row = hit
        try:
            props = json.loads(row.get("props") or "{}")
        except ValueError:
            props = {}
        out = {"id": nid, "type": label, "name": _cap(row.get("name")), "sample": bool(row.get("sample"))}
        for k in ("kind", "status"):
            if row.get(k):
                out[k] = row[k]
        if props:
            out["props"] = {k: _cap(v) for k, v in props.items()}
        return out

    def _adjacency(self, snap: Snapshot) -> dict[str, list[tuple[str, str, str]]]:
        """``id → [(other, rel, 'out'|'in')]``, built once per snapshot."""
        adj = getattr(snap, "_adj", None)
        if adj is None:
            adj = {}
            for rel in RELATIONSHIPS:
                t = snap.tables[edge_table(rel)]
                for s, d in zip(t["src"].to_pylist(), t["dst"].to_pylist()):
                    adj.setdefault(s, []).append((d, rel, "out"))
                    adj.setdefault(d, []).append((s, rel, "in"))
            snap._adj = adj  # type: ignore[attr-defined]
        return adj

    def _subgraph(self, snap: Snapshot, node_ids, edges=(), focus=()) -> dict:
        """The bounded visual footprint of one tool result (focus nodes first, then the rest)."""
        focus = [f for f in dict.fromkeys(focus) if f in snap.node_index()]
        ordered = list(dict.fromkeys([*focus, *node_ids]))
        idx = snap.node_index()
        nodes = []
        for nid in ordered:
            hit = idx.get(nid)
            if hit is None:
                continue
            label, row = hit
            nodes.append({"id": nid, "type": label, "name": _cap(row.get("name")), "sample": bool(row.get("sample"))})
            if len(nodes) >= SUBGRAPH_MAX_NODES:
                break
        keep = {n["id"] for n in nodes}
        out_edges = [list(e) for e in dict.fromkeys(tuple(e) for e in edges) if e[0] in keep and e[1] in keep]
        return {"nodes": nodes, "edges": out_edges[:SUBGRAPH_MAX_EDGES], "focus": focus,
                "truncated": len(ordered) > len(nodes) or len(out_edges) > SUBGRAPH_MAX_EDGES}

    def _edges_among(self, snap: Snapshot, ids: set) -> list[tuple[str, str, str]]:
        """Every relationship between two of `ids` (for graph_query rows and graph_find results)."""
        adj = self._adjacency(snap)
        out = []
        for nid in ids:
            for other, rel, dirn in adj.get(nid, []):
                if dirn == "out" and other in ids:
                    out.append((nid, other, rel))
        return out

    # ---- tools --------------------------------------------------------------------------------------
    def graph_schema(self, snap: Snapshot, _args: dict) -> dict:
        nodes = []
        for label in NODE_LABELS:
            t = snap.tables[node_table(label)]
            sample = sum(1 for v in t["sample"].to_pylist() if v)
            nodes.append({"label": label, "rows": t.num_rows, "sample_rows": sample})
        rels = [{"type": r, "from": s, "to": d, "rows": snap.tables[edge_table(r)].num_rows}
                for r, (s, d) in RELATIONSHIPS.items()]
        return {"ok": True, "nodes": nodes, "relationships": rels,
                "node_columns": ["id", "name", "sample", "kind", "status", "props"],
                "edge_columns": ["id", "src", "dst", "sample", "props"],
                "rules": QUERY_RULES, "generation": snap.doc.generation, "updatedAt": snap.doc.updatedAt}

    def graph_find(self, snap: Snapshot, args: dict) -> dict:
        import pyarrow.compute as pc

        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err("invalid_arguments", "text must be a non-empty string")
        types = args.get("types") or list(NODE_LABELS)
        if not isinstance(types, list) or any(t not in NODE_LABELS for t in types):
            return _err("invalid_arguments", f"types must be a subset of {list(NODE_LABELS)}")
        include_sample = args.get("include_sample", True) is not False
        limit = max(1, min(int(args.get("limit") or 20), 50))
        needle = text.strip().lower()
        found: list[dict] = []
        for label in types:
            t = snap.tables[node_table(label)]
            if t.num_rows == 0:
                continue
            mask = pc.match_substring(pc.utf8_lower(t["name"]), needle)
            if not include_sample:
                mask = pc.and_(mask, pc.invert(t["sample"]))
            for row in t.filter(mask).select(["id"]).to_pylist():
                found.append(self._node(snap, row["id"]))
        # Real nodes first, then exact-ish matches, then shorter names.
        found.sort(key=lambda n: (n["sample"], not str(n["name"]).lower().startswith(needle), len(str(n["name"]))))
        hits = [n["id"] for n in found[:limit]]
        return {"ok": True, "nodes": found[:limit], "truncated": len(found) > limit,
                "subgraph": self._subgraph(snap, hits, self._edges_among(snap, set(hits)), focus=hits)}

    def graph_neighbours(self, snap: Snapshot, args: dict) -> dict:
        nid = args.get("node_id")
        if not ids.valid_node_id(nid) or snap.node_index().get(nid) is None:
            return _err("node_not_found", "no such node; use graph_find to get a node id")
        rels = args.get("relationships")
        if rels is not None and (not isinstance(rels, list) or any(r not in RELATIONSHIPS for r in rels)):
            return _err("invalid_arguments", f"relationships must be a subset of {list(RELATIONSHIPS)}")
        direction = args.get("direction") or "both"
        if direction not in ("both", "out", "in"):
            return _err("invalid_arguments", "direction must be both, out or in")
        hops = 2 if int(args.get("hops") or 1) >= 2 else 1
        limit = max(1, min(int(args.get("limit") or 100), MAX_ROWS))
        q = self.reader.queries(snap)
        edges: list[list[str]] = []
        seen: set[tuple[str, str, str]] = set()
        frontier, visited, truncated = [nid], {nid}, False
        for _hop in range(hops):
            nxt = []
            for cur in frontier:
                for s, d, r in q.neighbours(cur):
                    if rels and r not in rels:
                        continue
                    if cur == nid and ((direction == "out" and s != cur) or (direction == "in" and d != cur)):
                        continue
                    if (s, d, r) in seen:
                        continue
                    if len(edges) >= limit:
                        truncated = True
                        break
                    seen.add((s, d, r))
                    edges.append([s, d, r])
                    other = d if s == cur else s
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                if truncated:
                    break
            frontier = nxt
            if truncated:
                break
        nodes = [n for n in (self._node(snap, v) for v in sorted(visited)) if n]
        return {"ok": True, "node": self._node(snap, nid), "nodes": nodes, "edges": edges, "truncated": truncated,
                "subgraph": self._subgraph(snap, sorted(visited), edges, focus=[nid])}

    def graph_paths(self, snap: Snapshot, args: dict) -> dict:
        a, b = args.get("from_id"), args.get("to_id")
        idx = snap.node_index()
        if not (ids.valid_node_id(a) and ids.valid_node_id(b)) or a not in idx or b not in idx:
            return _err("node_not_found", "from_id and to_id must be existing node ids (use graph_find)")
        max_hops = max(1, min(int(args.get("max_hops") or 3), 3))
        limit = max(1, min(int(args.get("limit") or 3), 10))
        adj = self._adjacency(snap)
        # Bounded BFS that keeps every shortest predecessor, then enumerates up to `limit` paths.
        dist = {a: 0}
        preds: dict[str, list[tuple[str, str, str]]] = {}
        queue = deque([a])
        while queue:
            cur = queue.popleft()
            if dist[cur] >= max_hops or (b in dist and dist[cur] >= dist[b]):
                continue
            for other, rel, dirn in adj.get(cur, []):
                nd = dist[cur] + 1
                if other not in dist:
                    dist[other] = nd
                    queue.append(other)
                if dist[other] == nd:
                    preds.setdefault(other, []).append((cur, rel, dirn))
        if b not in dist:
            return {"ok": True, "paths": [], "message": f"not connected within {max_hops} hops"}
        paths: list[list[dict]] = []

        def walk(node: str, tail: list[dict]) -> None:
            if len(paths) >= limit:
                return
            if node == a:
                paths.append(list(reversed(tail)))
                return
            for prev, rel, dirn in preds.get(node, []):
                src, dst = (prev, node) if dirn == "out" else (node, prev)
                walk(prev, tail + [{"from": src, "rel": rel, "to": dst}])

        walk(b, [])
        involved = sorted({x for p in paths for step in p for x in (step["from"], step["to"])})
        path_edges = [(st["from"], st["to"], st["rel"]) for p in paths for st in p]
        return {"ok": True, "hops": dist[b], "paths": paths,
                "nodes": [n for n in (self._node(snap, v) for v in involved) if n],
                "subgraph": self._subgraph(snap, involved, path_edges, focus=[a, b])}

    def graph_context(self, snap: Snapshot, args: dict) -> dict:
        asset_ids = args.get("asset_ids")
        if not isinstance(asset_ids, list) or not asset_ids or len(asset_ids) > 20:
            return _err("invalid_arguments", "asset_ids must be a list of 1–20 asset ids")
        lo, hi = args.get("start_ms"), args.get("end_ms")
        adj = self._adjacency(snap)
        out = []
        sg_nodes: list[str] = []
        sg_edges: list[tuple[str, str, str]] = []
        sg_focus: list[str] = []
        for aid in asset_ids:
            nid = aid if str(aid).startswith(("asset:", "sample:Asset:")) else ids.asset_node(str(aid))
            node = self._node(snap, nid)
            if node is None:
                out.append({"asset_id": aid, "found": False})
                continue
            sg_focus.append(nid)
            sg_nodes.append(nid)
            scenes = []
            for other, rel, dirn in adj.get(nid, []):
                if rel != "HAS_SCENE" or dirn != "out":
                    continue
                sc = self._node(snap, other) or {}
                p = sc.get("props", {})
                if lo is not None and hi is not None and isinstance(p.get("startMs"), int) and isinstance(p.get("endMs"), int):
                    if p["endMs"] < lo or p["startMs"] > hi:
                        continue
                linked: dict[str, list[str]] = {}
                sg_nodes.append(other)
                sg_edges.append((nid, other, "HAS_SCENE"))
                for o2, r2, d2 in adj.get(other, []):
                    if r2 in ("APPEARS_IN", "LOCATED_AT", "ABOUT"):
                        n2 = self._node(snap, o2)
                        if n2:
                            linked.setdefault(n2["type"], []).append(n2["name"])
                            sg_nodes.append(o2)
                            sg_edges.append((other, o2, r2) if d2 == "out" else (o2, other, r2))
                scenes.append({"id": other, "name": sc.get("name"), "startMs": p.get("startMs"), "endMs": p.get("endMs"),
                               **{k: sorted(set(v)) for k, v in linked.items()}})
                if len(scenes) >= 50:
                    break
            out.append({"asset_id": aid, "found": True, "node": node, "scenes": scenes})
        return {"ok": True, "assets": out, "subgraph": self._subgraph(snap, sg_nodes, sg_edges, focus=sg_focus)}

    def graph_query(self, snap: Snapshot, args: dict) -> dict:
        try:
            q = cypher_guard.check(args.get("cypher"), NODE_LABELS, RELATIONSHIPS)
        except cypher_guard.GuardError as e:
            return _err(e.code, e.message)
        params = args.get("params") or {}
        if not isinstance(params, dict) or any(not isinstance(v, (str, int, float, bool)) for v in params.values()):
            return _err("invalid_arguments", "params must be an object of strings / numbers / booleans")
        # Only the tables the pattern names (lance-graph 0.5.4 fails with "Table has no data" on ANY empty dataset
        # it is given, and a real Project always has empty ones); a pattern over an empty table matches nothing.
        labels, rels = cypher_guard.referenced(q)
        tables = {**{label: snap.tables[node_table(label)] for label in labels},
                  **{rel: snap.tables[edge_table(rel)] for rel in rels}}
        if any(t.num_rows == 0 for t in tables.values()):
            return {"ok": True, "columns": [], "rows": [], "row_count": 0, "truncated": False, "cypher": q,
                    "note": "a label or relationship in this pattern has no rows in this project yet",
                    "subgraph": self._subgraph(snap, [])}
        try:
            import lance_graph as lg
        except ImportError:
            return _err("query_unavailable", "graph_query needs the lance-graph engine, which is not available here")
        b = lg.GraphConfig.builder()
        for label in labels:
            b = b.with_node_label(label, "id")
        for rel in rels:
            b = b.with_relationship(rel, "src", "dst")
        query = lg.CypherQuery(q).with_config(b.build())
        for k, v in params.items():
            query = query.with_parameter(k, v)
        try:
            table = query.execute(tables)
        except Exception as e:  # noqa: BLE001 — engine planning/parse errors go back to the model to fix its query
            return _err("query_failed", str(e).splitlines()[0][:400])
        rows = [{k: _cap(v) for k, v in r.items()} for r in table.slice(0, MAX_ROWS).to_pylist()]
        # The rows' node ids (any column holding one, e.g. `RETURN p.id AS id`) and the relationships among them.
        idx = snap.node_index()
        row_ids = [v for r in rows for v in r.values() if isinstance(v, str) and v in idx]
        return {"ok": True, "columns": table.column_names, "rows": rows, "row_count": table.num_rows,
                "truncated": table.num_rows > MAX_ROWS, "cypher": q,
                "subgraph": self._subgraph(snap, row_ids, self._edges_among(snap, set(row_ids)))}

    # ---- dispatch -----------------------------------------------------------------------------------
    def call(self, tool: str, args: Any) -> dict:
        if tool not in TOOL_NAMES:
            return _err("unknown_tool", f"unknown graph tool {tool!r}")
        if not isinstance(args, dict):
            return _err("invalid_arguments", "arguments must be an object")
        pid = args.get("project_id")
        if not is_project_id(pid):
            return _err("invalid_arguments", "project_id is missing or malformed")
        snap, err = self._snapshot(pid)
        if err:
            return err
        return getattr(self, tool)(snap, args)


def tool_name_from_context(context: Any) -> Optional[str]:
    """The bare tool name from the gateway's Lambda client context (``project-graph___graph_find`` → ``graph_find``)."""
    cc = getattr(context, "client_context", None)
    custom = getattr(cc, "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName") if isinstance(custom, dict) else None
    if not isinstance(full, str):
        return None
    return full.split("___", 1)[1] if "___" in full else full


def emit_schema() -> str:
    return json.dumps(TOOL_SPECS, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    import argparse
    import pathlib

    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-schema", action="store_true")
    if ap.parse_args().emit_schema:
        (pathlib.Path(__file__).parent / "tool_schema.json").write_text(emit_schema())
        print("wrote tool_schema.json")
