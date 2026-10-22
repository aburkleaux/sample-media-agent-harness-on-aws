"""Neighbour lookups over a loaded graph snapshot — two interchangeable implementations.

``CypherQueries`` runs typed lance-graph Cypher; ``ArrowQueries`` filters the Arrow edge tables directly.
They return the same edges (parity-tested on x86 in CI), so tests and the arm64 dev loop use Arrow while the
Lambda uses Cypher.

lance-graph 0.5.4 quirks this works around (graph-lab spike):
- an undirected pattern ``-[:R]-`` returns OUTGOING edges only → outgoing and incoming are separate queries;
- ``$params`` exist only on ``CypherQuery.with_parameter``, not ``CypherEngine``;
- every node must be labelled → one query per relationship type whose endpoint label matches.
"""
from __future__ import annotations

import logging
from typing import Mapping, Protocol

from . import ids
from .schema import RELATIONSHIPS, edge_table, table_label

logger = logging.getLogger(__name__)

Edge = tuple[str, str, str]   # src, dst, type


class GraphQueries(Protocol):
    def neighbours(self, node_id: str) -> list[Edge]: ...


class ArrowQueries:
    def __init__(self, tables: Mapping[str, object]):
        self.tables = tables

    def neighbours(self, node_id: str) -> list[Edge]:
        import pyarrow.compute as pc

        label = ids.label_of(node_id)
        out: list[Edge] = []
        for rel, (src_l, dst_l) in RELATIONSHIPS.items():
            t = self.tables[edge_table(rel)]
            if src_l == label:
                hit = t.filter(pc.equal(t["src"], node_id))
                out += [(node_id, d, rel) for d in hit["dst"].to_pylist()]
            if dst_l == label:
                hit = t.filter(pc.equal(t["dst"], node_id))
                out += [(s, node_id, rel) for s in hit["src"].to_pylist() if not (src_l == dst_l and s == node_id)]
        return out


class CypherQueries:
    """Typed lance-graph Cypher, one query per (relationship, direction).

    lance-graph 0.5.4 raises ``RuntimeError: Table has no data`` when ANY dataset it is given is empty — and a real
    Project always has some empty tables (no tags, no members besides the owner, no extracted entities yet). So
    each query is given ONLY the two node tables and the one relationship table it names, under a config that
    declares only those; a query over an empty table is answered as no rows without calling lance-graph (the
    correct MATCH result). Any other lance-graph failure falls back to ``ArrowQueries`` for that lookup, so a
    click on a node never fails because of an engine quirk.
    """

    def __init__(self, tables: Mapping[str, object]):
        # lance-graph keys datasets by label / relationship name, not by our "nodes/X" table names.
        self._datasets = {table_label(t): v for t, v in tables.items()}
        self._arrow = ArrowQueries(tables)
        self._configs: dict[tuple[str, str, str], object] = {}

    def _config(self, src_l: str, rel: str, dst_l: str):
        key = (src_l, rel, dst_l)
        if key not in self._configs:
            import lance_graph as lg

            b = lg.GraphConfig.builder()
            for label in dict.fromkeys((src_l, dst_l)):
                b = b.with_node_label(label, "id")
            self._configs[key] = b.with_relationship(rel, "src", "dst").build()
        return self._configs[key]

    def _ids(self, query: str, node_id: str, src_l: str, rel: str, dst_l: str) -> list[str]:
        import lance_graph as lg

        needed = list(dict.fromkeys((src_l, dst_l, rel)))
        if any(getattr(self._datasets.get(n), "num_rows", 0) == 0 for n in needed):
            return []
        q = lg.CypherQuery(query).with_config(self._config(src_l, rel, dst_l)).with_parameter("id", node_id)
        return q.execute({n: self._datasets[n] for n in needed}).column("id").to_pylist()

    def neighbours(self, node_id: str) -> list[Edge]:
        label = ids.label_of(node_id)
        out: list[Edge] = []
        try:
            for rel, (src_l, dst_l) in RELATIONSHIPS.items():
                if src_l == label:
                    q = f"MATCH (a:{src_l})-[:{rel}]->(b:{dst_l}) WHERE a.id = $id RETURN b.id AS id"
                    out += [(node_id, d, rel) for d in self._ids(q, node_id, src_l, rel, dst_l)]
                if dst_l == label:
                    q = f"MATCH (a:{src_l})-[:{rel}]->(b:{dst_l}) WHERE b.id = $id RETURN a.id AS id"
                    out += [(s, node_id, rel) for s in self._ids(q, node_id, src_l, rel, dst_l)
                            if not (src_l == dst_l and s == node_id)]
        except Exception:  # noqa: BLE001 — an engine quirk must not fail the lookup; Arrow is parity-tested
            logger.warning("lance-graph neighbours(%s) failed; answering from Arrow", label, exc_info=True)
            return self._arrow.neighbours(node_id)
        return out


def make_queries(tables: Mapping[str, object], engine: str = "auto") -> GraphQueries:
    if engine == "arrow":
        return ArrowQueries(tables)
    if engine == "cypher":
        return CypherQueries(tables)
    try:
        import lance_graph  # noqa: F401
    except ImportError:
        return ArrowQueries(tables)
    return CypherQueries(tables)
