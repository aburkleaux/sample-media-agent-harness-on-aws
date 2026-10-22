"""Pure parts of the per-Project knowledge graph: ids, starter rows, demo rows, _meta.json, payload shapes.

No pyarrow / lance / lance_graph needed — these run in the default agent suite on any machine.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_graph import demo, ids, layout, payload, starter  # noqa: E402
from project_graph.schema import NODE_LABELS, RELATIONSHIPS, TABLES, edge_table, node_table  # noqa: E402

PID = "p" + "a" * 32
OWNER, MEMBER = "owner-sub", "member-sub"
ASSET_V = "video:0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
ASSET_I = "image:1b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
ASSET_R = "audio:2b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
ASSET_D = "data:3b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"


def _meta(**kw):
    m = {"projectId": PID, "name": "Launch reel", "ownerSub": OWNER, "personal": False,
         "description": "trailer cut", "tags": ["Sport", "sport ", "Finals"], "createdAt": "2026-10-07T00:00:00Z"}
    m.update(kw)
    return m


MEMBERS = [{"SK": f"MEMBER#{OWNER}", "role": "owner", "display": "Olive Owner"},
           {"SK": f"MEMBER#{MEMBER}", "role": "member", "display": "Max Member"}]
SID = "11112222-3333-4444-5555-666677778888"
JOB = "job-7f3a"
# asset-lineage Phase 1 attributes: the video is a session output derived from the uploaded poster, the
# audio is a removed asset, and the data file is an M2C job's analysis output.
ASSETS = [{"assetId": ASSET_V, "kind": "video", "displayName": "final.mp4", "status": "indexed",
           "source": "session", "role": "deliverable", "sessionId": SID, "derivedFrom": [ASSET_I],
           "folder": f"Sessions/Launch reel cut · 2026-10-08"},
          {"assetId": ASSET_I, "kind": "image", "displayName": "poster.jpg", "status": "stored"},
          {"assetId": ASSET_R, "kind": "audio", "displayName": "old.wav", "status": "removed"},
          {"assetId": ASSET_D, "kind": "data", "displayName": "shots.json", "status": "stored",
           "source": "m2c", "role": "analysis", "jobId": JOB, "derivedFrom": [ASSET_V],
           "producer": {"sessionId": SID, "jobId": JOB},
           "folder": f"Sessions/Launch reel cut · 2026-10-08/Shot segmentation"}]
JOBS = [{"SK": f"JOB#{JOB}", "jobId": JOB, "tool": "shot-segmentation", "label": "Shot segmentation",
         "sessionId": SID, "outputPrefix": "s3://m2c-proxy/out/", "sourceAssetId": ASSET_V}]


# ---- ids -------------------------------------------------------------------------------------------

def test_ids_round_trip_to_labels():
    assert ids.label_of(ids.project_node(PID)) == "Project"
    assert ids.label_of(ids.user_node(OWNER)) == "User"
    assert ids.label_of(ids.tag_node("Ünïcode  ")) == "Tag"
    assert ids.label_of(ids.asset_node(ASSET_V)) == "Asset"
    for label in NODE_LABELS:
        assert ids.label_of(ids.sample_node(label, 3)) == label


@pytest.mark.parametrize("bad", ["", "nope", "project:", "sample:Nope:1", "x" * 300, "user:a\nb", 7, None,
                                 "asset:" + "a" * 250])
def test_malformed_node_ids_have_no_label(bad):
    assert ids.label_of(bad) is None
    assert not ids.valid_node_id(bad)


def test_tag_ids_fold_case_and_whitespace():
    assert ids.tag_node("Sport") == ids.tag_node(" sport ") == "tag:sport"


def test_edge_id_rejects_unknown_relationship():
    with pytest.raises(ValueError):
        ids.edge_id("LIKES", "a", "b")


# ---- starter ---------------------------------------------------------------------------------------

def test_project_rows_cover_project_members_tags_and_live_assets():
    rows = starter.project_rows(_meta(), MEMBERS, ASSETS)
    assert set(rows) == set(TABLES)
    assert [r["id"] for r in rows[node_table("Project")]] == [f"project:{PID}"]
    assert {r["id"] for r in rows[node_table("User")]} == {f"user:{OWNER}", f"user:{MEMBER}"}
    assert [r["src"] for r in rows[edge_table("OWNS")]] == [f"user:{OWNER}"]
    assert len(rows[edge_table("MEMBER_OF")]) == 2
    # "Sport" and "sport " fold to one tag
    assert sorted(r["id"] for r in rows[node_table("Tag")]) == ["tag:finals", "tag:sport"]
    # the removed asset is not a node
    assert sorted(r["id"] for r in rows[node_table("Asset")]) == sorted(
        [f"asset:{ASSET_V}", f"asset:{ASSET_I}", f"asset:{ASSET_D}"])
    assert all(r["sample"] is False for t in rows.values() for r in t)


def test_project_rows_are_deterministic():
    a = starter.project_rows(_meta(), MEMBERS, ASSETS, JOBS)
    b = starter.project_rows(_meta(), list(reversed(MEMBERS)), list(reversed(ASSETS)), list(reversed(JOBS)))
    assert a == b


# ---- starter: lineage (asset-lineage Phase 3, R6) ---------------------------------------------------

def test_lineage_edges_answer_what_came_from_this_clip():
    rows = starter.project_rows(_meta(), MEMBERS, ASSETS, JOBS)
    v, i, d = (ids.asset_node(a) for a in (ASSET_V, ASSET_I, ASSET_D))
    # DERIVED_FROM points from the derived asset TO its input, so the poster's INCOMING edges are the
    # things made from it and the cut's OUTGOING edges are what went into it.
    assert {(e["src"], e["dst"]) for e in rows[edge_table("DERIVED_FROM")]} == {(v, i), (d, v)}
    assert [e["src"] for e in rows[edge_table("DERIVED_FROM")] if e["dst"] == v] == [d]


def test_session_and_job_nodes_come_from_the_asset_rows():
    rows = starter.project_rows(_meta(), MEMBERS, ASSETS, JOBS)
    s, j = ids.session_node(SID), ids.job_node(JOB)
    assert [r["id"] for r in rows[node_table("Session")]] == [s]
    # The title is read off the folder the session's assets live in, not invented.
    assert rows[node_table("Session")][0]["name"] == "Launch reel cut"
    assert [r["id"] for r in rows[node_table("Job")]] == [j]
    assert rows[node_table("Job")][0]["name"] == "Shot segmentation"
    assert rows[node_table("Job")][0]["kind"] == "shot-segmentation"
    # Both session outputs point at the session; only the job's output points at the job.
    assert {e["src"] for e in rows[edge_table("PRODUCED_BY")]} == {ids.asset_node(ASSET_V), ids.asset_node(ASSET_D)}
    assert all(e["dst"] == s for e in rows[edge_table("PRODUCED_BY")])
    assert [(e["src"], e["dst"]) for e in rows[edge_table("OUTPUT_OF")]] == [(ids.asset_node(ASSET_D), j)]
    assert [(e["src"], e["dst"]) for e in rows[edge_table("RAN_IN")]] == [(j, s)]


def test_a_project_with_no_lineage_has_no_lineage_rows():
    plain = [{"assetId": ASSET_I, "kind": "image", "displayName": "poster.jpg", "status": "stored"}]
    rows = starter.project_rows(_meta(), MEMBERS, plain)
    for t in ("Session", "Job"):
        assert rows[node_table(t)] == []
    for rel in ("DERIVED_FROM", "PRODUCED_BY", "OUTPUT_OF", "RAN_IN"):
        assert rows[edge_table(rel)] == []


def test_lineage_never_points_at_a_node_that_is_not_in_the_graph():
    """A removed or foreign input is dropped, not left dangling — every reader resolves endpoints."""
    assets = [{"assetId": ASSET_V, "kind": "video", "displayName": "final.mp4", "status": "indexed",
               "derivedFrom": [ASSET_R, "video:" + "9" * 36, ASSET_V]},
              {"assetId": ASSET_R, "kind": "audio", "displayName": "old.wav", "status": "removed"}]
    rows = starter.project_rows(_meta(), MEMBERS, assets)
    assert rows[edge_table("DERIVED_FROM")] == [], "removed, foreign and self edges are all dropped"


def test_a_job_with_no_record_still_gets_a_node_from_its_asset():
    """`producer.jobId` without a JOB# row (a fal job, a record not written yet) is still lineage."""
    assets = [{"assetId": ASSET_V, "kind": "video", "status": "stored",
               "producer": {"sessionId": SID, "jobId": "fal-123"}}]
    rows = starter.project_rows(_meta(), MEMBERS, assets)
    assert [r["id"] for r in rows[node_table("Job")]] == ["job:fal-123"]
    assert rows[node_table("Job")][0]["name"] == "fal-123"
    assert [(e["src"], e["dst"]) for e in rows[edge_table("RAN_IN")]] == [("job:fal-123", ids.session_node(SID))]


@pytest.mark.parametrize("folder,expected", [
    ("Chats/Launch reel · 2026-10-08", "Launch reel"),
    ("Chats/Launch reel · 2026-10-08/Shot segmentation", "Launch reel"),
    ("Sessions/Launch reel · 2026-10-08", "Launch reel"),
    ("Sessions/Launch reel · 2026-10-08/Shot segmentation", "Launch reel"),
    ("Sessions/2026-10-08", "2026-10-08"),
    ("Sessions/", "Session 11112222"),
    ("Uploads/clips", "Session 11112222"),
    (None, "Session 11112222"),
    (7, "Session 11112222"),
])
def test_session_title_reads_the_folder_with_fallbacks(folder, expected):
    assert starter.session_title(folder, SID) == expected


def test_every_edge_endpoint_exists_in_starter_plus_demo():
    rows = starter.project_rows(_meta(), MEMBERS, ASSETS, JOBS)
    d = demo.demo_rows(PID)
    node_ids = {r["id"] for t in TABLES if t.startswith("nodes/") for r in rows[t] + d[t]}
    for rel, (src_l, dst_l) in RELATIONSHIPS.items():
        for e in rows[edge_table(rel)] + d[edge_table(rel)]:
            assert e["src"] in node_ids and e["dst"] in node_ids, e
            assert ids.label_of(e["src"]) == src_l and ids.label_of(e["dst"]) == dst_l, e


# ---- demo ------------------------------------------------------------------------------------------

def test_demo_is_small_deterministic_and_all_sample():
    a, b = demo.demo_rows(PID), demo.demo_rows(PID)
    assert a == b
    n_nodes = sum(len(a[node_table(l)]) for l in NODE_LABELS)
    assert 200 <= n_nodes <= 400
    assert all(r["sample"] is True for t in a.values() for r in t)
    # the demo hangs off the real Project node
    assert {e["src"] for e in a[edge_table("HAS_ASSET")]} == {f"project:{PID}"}


# ---- _meta.json ------------------------------------------------------------------------------------

def test_meta_doc_round_trip():
    doc = layout.MetaDoc(generation=3, updatedAt="t", tables={"nodes/Asset": layout.TableState(4, 2, 1, "h")},
                         counts={"nodes": 9, "edges": 8}, source={"changeSeq": 5}, demo={"seeded": True})
    back = layout.MetaDoc.from_json(doc.to_json())
    assert back == doc
    assert back.storageVersion == "2.1"


def test_layout_keys_live_under_the_project_prefix():
    assert layout.meta_key(PID) == f"projects/{PID}/graph/_meta.json"
    assert layout.table_uri("s3://b", PID, "nodes/Asset") == f"s3://b/projects/{PID}/graph/v1/nodes/Asset.lance"


# ---- payload ---------------------------------------------------------------------------------------

def test_overview_is_compact_and_indexed():
    nodes = [("project:x", "Project", "X", False), ("asset:a", "Asset", "a.mp4", False),
             ("sample:Person:0", "Person", "Ava", True)]
    edges = [("project:x", "asset:a", "HAS_ASSET")]
    out = payload.overview(nodes, edges, generation=2)
    assert out["totals"] == {"nodes": 3, "edges": 1} and out["truncated"] is False
    assert out["nodes"][2] == ["sample:Person:0", NODE_LABELS.index("Person"), "Ava", 1, 0]
    assert out["edges"] == [[0, 1, list(RELATIONSHIPS).index("HAS_ASSET")]]


def test_overview_truncates_to_highest_degree_nodes():
    nodes = [(f"sample:Person:{i}", "Person", str(i), True) for i in range(10)]
    edges = [("sample:Person:0", f"sample:Person:{i}", "KNOWS") for i in range(1, 10)]
    out = payload.overview(nodes, edges, generation=1, max_nodes=3)
    assert out["truncated"] is True and len(out["nodes"]) == 3
    assert out["nodes"][0][0] == "sample:Person:0"
    assert all(max(s, d) < 3 for s, d, _ in out["edges"])


# ---- neighbours of the graph in the existing ingestion code ------------------------------------------

def test_registrar_treats_graph_objects_as_managed():
    from ingestion.registrar import _is_managed_key

    prefix = f"projects/{PID}/"
    assert _is_managed_key(f"{prefix}graph/_meta.json", prefix)
    assert _is_managed_key(f"{prefix}graph/v1/nodes/Asset.lance/data/x.lance", prefix)
    assert _is_managed_key(f"{prefix}{ASSET_V}/final.mp4", prefix)
    assert not _is_managed_key(f"{prefix}graph", prefix)              # a user file literally named "graph"
    assert not _is_managed_key(f"{prefix}graphs/clip.mp4", prefix)
    assert not _is_managed_key(f"projects/{'p' + 'c' * 32}/graph/_meta.json", prefix)


@pytest.mark.parametrize("meta,on", [({"graph": True}, True), ({"graph": False}, False), ({}, False),
                                     ({"graph": "true"}, False), (None, False)])
def test_graph_on_only_for_true(meta, on):
    from ingestion.store import graph_on

    assert graph_on(meta) is on
