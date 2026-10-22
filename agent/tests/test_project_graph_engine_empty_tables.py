"""Regression (staging 2026-10-08): clicking a node failed with lance-graph's ``RuntimeError: Table has no data``.

lance-graph 0.5.4 raises that when ANY dataset it is given is empty, and a real Project always has empty tables
(no tags, no extracted entities yet). `CypherQueries` must give each query only the tables it names, answer a
query over an empty table as no rows, and fall back to Arrow on any other engine failure. A fake `lance_graph`
reproduces the engine's behaviour, so this runs everywhere (the real engine is covered in
test_project_graph_lance_graph.py on x86).
"""
from __future__ import annotations

import os
import sys
import types

import pytest

pa = pytest.importorskip("pyarrow")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_graph import engine  # noqa: E402
from project_graph.schema import TABLES, arrow_schema, edge_table, node_table  # noqa: E402


class _FakeQuery:
    calls: list = []

    def __init__(self, q):
        self.q, self.params = q, {}

    def with_config(self, cfg):
        self.cfg = cfg
        return self

    def with_parameter(self, k, v):
        self.params[k] = v
        return self

    def execute(self, datasets):
        _FakeQuery.calls.append((self.q, sorted(datasets), self.cfg))
        if any(t.num_rows == 0 for t in datasets.values()):
            raise RuntimeError("Table has no data")          # lance-graph 0.5.4's behaviour
        rel = next(k for k in datasets if k.isupper())
        t = datasets[rel]
        nid = self.params["id"]
        if "WHERE a.id = $id" in self.q:
            ids = [d for s, d in zip(t["src"].to_pylist(), t["dst"].to_pylist()) if s == nid]
        else:
            ids = [s for s, d in zip(t["src"].to_pylist(), t["dst"].to_pylist()) if d == nid]
        return pa.table({"id": pa.array(ids, pa.string())})


class _FakeBuilder:
    def __init__(self):
        self.labels, self.rels = [], []

    def with_node_label(self, label, key):
        self.labels.append(label)
        return self

    def with_relationship(self, rel, s, d):
        self.rels.append(rel)
        return self

    def build(self):
        return (tuple(self.labels), tuple(self.rels))


@pytest.fixture
def fake_lance_graph(monkeypatch):
    mod = types.SimpleNamespace(CypherQuery=_FakeQuery,
                                GraphConfig=types.SimpleNamespace(builder=_FakeBuilder))
    monkeypatch.setitem(sys.modules, "lance_graph", mod)
    _FakeQuery.calls = []
    return mod


def _tables(**rows):
    out = {t: pa.Table.from_pylist([], schema=arrow_schema(t)) for t in TABLES}
    for t, r in rows.items():
        out[t] = pa.Table.from_pylist(r, schema=arrow_schema(t))
    return out


def _n(i, sample=False):
    return {"id": i, "name": i, "sample": sample, "kind": None, "status": None, "props": "{}"}


def _e(rel, s, d):
    return {"id": f"{rel}|{s}|{d}", "src": s, "dst": d, "sample": False, "props": "{}"}


PID = "project:p1"
TABLES_WITH_EMPTY = _tables(**{
    node_table("Project"): [_n(PID)], node_table("User"): [_n("user:u")], node_table("Asset"): [_n("asset:a")],
    edge_table("OWNS"): [_e("OWNS", "user:u", PID)], edge_table("MEMBER_OF"): [_e("MEMBER_OF", "user:u", PID)],
    edge_table("HAS_ASSET"): [_e("HAS_ASSET", PID, "asset:a")],
    # Tag / TAGGED (and every media table) are EMPTY, like a project with no tags on staging.
})


def test_empty_tables_no_longer_fail_and_match_arrow(fake_lance_graph):
    cy = engine.CypherQueries(TABLES_WITH_EMPTY)
    got = sorted(cy.neighbours(PID))
    assert got == sorted(engine.ArrowQueries(TABLES_WITH_EMPTY).neighbours(PID))
    assert {e[2] for e in got} == {"OWNS", "MEMBER_OF", "HAS_ASSET"}


def test_each_query_gets_only_its_own_tables_and_never_an_empty_one(fake_lance_graph):
    engine.CypherQueries(TABLES_WITH_EMPTY).neighbours(PID)
    assert _FakeQuery.calls, "the non-empty relationships still go through lance-graph"
    for _q, names, cfg in _FakeQuery.calls:
        assert len(names) <= 3 and all(TABLES_WITH_EMPTY[t].num_rows for t in TABLES_WITH_EMPTY
                                       if t.split("/", 1)[1] in names)
        labels, rels = cfg
        assert set(names) == set(labels) | set(rels)
    assert not any("TAGGED" in names for _q, names, _c in _FakeQuery.calls)


def test_any_other_engine_failure_falls_back_to_arrow(fake_lance_graph, monkeypatch):
    def boom(self, datasets):
        raise RuntimeError("some other lance-graph error")
    monkeypatch.setattr(_FakeQuery, "execute", boom)
    got = engine.CypherQueries(TABLES_WITH_EMPTY).neighbours(PID)
    assert sorted(got) == sorted(engine.ArrowQueries(TABLES_WITH_EMPTY).neighbours(PID))
