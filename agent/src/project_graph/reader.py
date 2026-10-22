"""Read side: serve a Project's graph to the proxy from a warm, snapshot-consistent in-memory cache.

Per request: one consistent META GetItem (is the graph on?) and one conditional ``_meta.json`` GET
(``IfNoneMatch`` the cached ETag). A hit costs no table I/O; a miss loads every table **at the version
``_meta.json`` names**, in parallel, into Arrow. Queries then run in memory (1–3.5 ms each in the spike).

Self-healing: when META has moved past what the snapshot was built from (a missed stream record, or a
project created before the writer existed) the reader asks the writer for a sync and keeps serving the
current snapshot; when there is no snapshot at all it answers ``seeding``.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from projects import schema as pschema

from . import ids, layout, payload
from .engine import GraphQueries, make_queries
from .schema import NODE_LABELS, RELATIONSHIPS, TABLES, edge_table, node_table
from .writer import graph_on, library_versions, source_signature

STALE_AFTER_S = 60
NEIGHBOURHOOD_LIMIT = 300
LOAD_THREADS = 16


@dataclass
class Snapshot:
    pid: str
    etag: str
    doc: layout.MetaDoc
    tables: dict[str, Any]
    load_ms: float
    _queries: Optional[GraphQueries] = None
    _nodes: Optional[dict[str, tuple[str, dict]]] = field(default=None, repr=False)

    def node_index(self) -> dict[str, tuple[str, dict]]:
        if self._nodes is None:
            idx: dict[str, tuple[str, dict]] = {}
            for label in NODE_LABELS:
                for row in self.tables[node_table(label)].to_pylist():
                    idx[row["id"]] = (label, row)
            self._nodes = idx
        return self._nodes


def _parse_iso(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class GraphReader:
    def __init__(self, *, table: Any, s3: Any, bucket: str, root: str, storage_options: Optional[dict] = None,
                 request_sync: Callable[[str], None] = lambda pid: None, engine: str = "auto",
                 cache_size: int = 8, clock: Callable[[], float] = time.time):
        self.table, self.s3, self.bucket, self.root = table, s3, bucket, root
        self.storage_options, self.request_sync, self.engine = storage_options, request_sync, engine
        self.cache_size, self.clock = cache_size, clock
        self._cache: "OrderedDict[str, Snapshot]" = OrderedDict()

    # ---- snapshot management ----------------------------------------------------------------------
    def _load(self, pid: str, doc: layout.MetaDoc, etag: str) -> Snapshot:
        import lance

        t0 = time.perf_counter()

        def one(t: str):
            st = doc.tables[t]
            return t, lance.dataset(layout.table_uri(self.root, pid, t), version=st.version,
                                    storage_options=self.storage_options).to_table()

        with ThreadPoolExecutor(max_workers=LOAD_THREADS) as ex:
            tables = dict(ex.map(one, [t for t in TABLES if t in doc.tables]))
        missing = [t for t in TABLES if t not in tables]
        if missing:
            import pyarrow as pa

            from .schema import arrow_schema
            for t in missing:
                tables[t] = pa.Table.from_pylist([], schema=arrow_schema(t))
        return Snapshot(pid, etag, doc, tables, round((time.perf_counter() - t0) * 1000, 1))

    def snapshot(self, pid: str) -> tuple[Optional[Snapshot], Optional[str]]:
        """``(snapshot, state)`` — state is ``off`` / ``not_found`` / ``seeding`` when there is no snapshot."""
        meta = self.table.get_item(Key=pschema.meta_key(pid), ConsistentRead=True).get("Item")
        if not meta or pschema.is_tombstone(meta) or meta.get("status") != "active":
            return None, "not_found"
        if not graph_on(meta):
            return None, "off"
        cached = self._cache.get(pid)
        doc, etag, not_modified = layout.read_meta(self.s3, self.bucket, pid,
                                                   if_none_match=cached.etag if cached else None)
        if not_modified and cached:
            self._cache.move_to_end(pid)
            snap = cached
        elif doc is None:
            self.request_sync(pid)
            return None, "seeding"
        else:
            snap = self._load(pid, doc, etag or "")
            self._cache[pid] = snap
            self._cache.move_to_end(pid)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        if (snap.doc.source != source_signature(meta)
                and self.clock() - _parse_iso(snap.doc.updatedAt) > STALE_AFTER_S):
            self.request_sync(pid)
        return snap, None

    def queries(self, snap: Snapshot) -> GraphQueries:
        if snap._queries is None:
            snap._queries = make_queries(snap.tables, self.engine)
        return snap._queries

    # ---- operations -------------------------------------------------------------------------------
    def handle(self, op: str, pid: str, args: Optional[dict] = None) -> dict:
        args = args or {}
        if op == "health":
            return {"state": "ok", "versions": library_versions(), "cached": list(self._cache)}
        snap, state = self.snapshot(pid)
        if snap is None:
            return {"state": state}
        if op == "overview":
            return self.overview(snap)
        if op == "stats":
            return self.stats(snap)
        if op == "neighbourhood":
            return self.neighbourhood(snap, args.get("nodeId"), int(args.get("hops") or 1),
                                      int(args.get("limit") or NEIGHBOURHOOD_LIMIT))
        return {"state": "error", "error": {"code": "unknown_op", "message": f"unknown op {op!r}"}}

    def overview(self, snap: Snapshot) -> dict:
        nodes = []
        for label in NODE_LABELS:
            t = snap.tables[node_table(label)]
            for nid, name, sample in zip(t["id"].to_pylist(), t["name"].to_pylist(), t["sample"].to_pylist()):
                nodes.append((nid, label, name, bool(sample)))
        edges = []
        for rel in RELATIONSHIPS:
            t = snap.tables[edge_table(rel)]
            edges += [(s, d, rel) for s, d in zip(t["src"].to_pylist(), t["dst"].to_pylist())]
        out = payload.overview(nodes, edges, generation=snap.doc.generation)
        out["updatedAt"] = snap.doc.updatedAt
        return out

    def stats(self, snap: Snapshot) -> dict:
        return {"state": "ready", "generation": snap.doc.generation, "updatedAt": snap.doc.updatedAt,
                "counts": snap.doc.counts, "tables": {t: s.rows for t, s in snap.doc.tables.items()},
                "loadMs": snap.load_ms}

    def _describe(self, snap: Snapshot, nid: str) -> Optional[dict]:
        hit = snap.node_index().get(nid)
        if hit is None:
            return None
        label, row = hit
        try:
            props = json.loads(row.get("props") or "{}")
        except ValueError:
            props = {}
        return {"id": nid, "type": label, "name": row.get("name"), "sample": bool(row.get("sample")),
                "kind": row.get("kind"), "status": row.get("status"), "props": props}

    def neighbourhood(self, snap: Snapshot, node_id: Any, hops: int, limit: int) -> dict:
        if not ids.valid_node_id(node_id):
            return {"state": "error", "error": {"code": "invalid_node", "message": "malformed node id"}}
        center = self._describe(snap, node_id)
        if center is None:
            return {"state": "error", "error": {"code": "node_not_found", "message": "no such node"}}
        hops = 2 if hops >= 2 else 1
        limit = max(1, min(limit, 2000))
        q = self.queries(snap)
        edges: list[tuple[str, str, str]] = []
        seen_edges: set[tuple[str, str, str]] = set()
        truncated = False
        frontier = [node_id]
        visited = {node_id}
        for _ in range(hops):
            nxt: list[str] = []
            for nid in frontier:
                for e in q.neighbours(nid):
                    if e in seen_edges:
                        continue
                    if len(edges) >= limit:
                        truncated = True
                        break
                    seen_edges.add(e)
                    edges.append(e)
                    other = e[1] if e[0] == nid else e[0]
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                if truncated:
                    break
            frontier = nxt
            if truncated:
                break
        nodes = [d for d in (self._describe(snap, n) for n in sorted(visited)) if d]
        return payload.neighbourhood(center, nodes, edges, hops=hops, truncated=truncated)
