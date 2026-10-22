"""Graph writer + reader against REAL pylance (local directory root) and moto (DynamoDB + _meta.json in S3).

Needs the ``graph`` extra (pylance, pyarrow): ``uv run --extra dev --extra graph pytest tests/test_project_graph_sync.py``.
Skipped without it. moto does not intercept Lance's Rust object store, so Lance tables live under
``tmp_path`` while ``_meta.json`` (boto3) goes through moto — the same split the Lambda has.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("lance")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from project_graph import layout, writer  # noqa: E402
from project_graph.handler import pids_from_stream  # noqa: E402
from project_graph.reader import GraphReader  # noqa: E402
from project_graph.schema import NODE_LABELS, edge_table, node_table  # noqa: E402
from projects import schema as pschema  # noqa: E402

PID = "p" + "b" * 32
TABLE, BUCKET = "graph-test-projects", "graph-test-ingest"
OWNER, MEMBER = "owner-sub", "member-sub"
VID = "video:0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
IMG = "image:1b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
SID = "11112222-3333-4444-5555-666677778888"
JOB = "job-7f3a"


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-west-2",
                 "AWS_REGION": "us-west-2"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                         KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                         AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                               {"AttributeName": "SK", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name="us-west-2")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        yield ddb.Table(TABLE), s3


def _seed(table, *, graph=True, assets=(VID, IMG), tags=("Sport",), change_seq=1):
    pk = pschema.project_pk(PID)
    meta = {"PK": pk, "SK": "META", "projectId": PID, "name": "Finals", "ownerSub": OWNER, "personal": False,
            "status": "active", "memberCount": 1, "tags": list(tags), "description": "", "changeSeq": change_seq,
            "activeAssetCount": len(assets)}
    if graph is not None:
        meta["graph"] = graph
    table.put_item(Item=meta)
    table.put_item(Item={"PK": pk, "SK": f"MEMBER#{OWNER}", "role": "owner", "display": "Olive"})
    table.put_item(Item={"PK": pk, "SK": f"MEMBER#{MEMBER}", "role": "member", "display": "Max"})
    for a in assets:
        item = {"PK": pk, "SK": f"ASSET#{a}", "assetId": a, "kind": a.split(":")[0],
                "displayName": f"{a.split(':')[0]}.file", "status": "indexed"}
        # asset-lineage Phase 1 attributes on the video: a session output of SID, produced by JOB and
        # derived from the image. The graph's lineage edges are built from exactly these.
        if a == VID:
            item.update({"source": "session", "role": "deliverable", "sessionId": SID, "jobId": JOB,
                         "derivedFrom": [IMG], "folder": "Sessions/Finals cut · 2026-10-08"})
        table.put_item(Item=item)
    table.put_item(Item={"PK": pk, "SK": f"JOB#{JOB}", "jobId": JOB, "tool": "shot-segmentation",
                         "label": "Shot segmentation", "sessionId": SID,
                         "outputPrefix": "s3://m2c-proxy/out/", "sourceAssetId": IMG})


def _bump(table, **sets):
    expr = "SET changeSeq = changeSeq + :one" + "".join(f", {k} = :{k}" for k in sets)
    vals = {":one": 1, **{f":{k}": v for k, v in sets.items()}}
    table.update_item(Key=pschema.meta_key(PID), UpdateExpression=expr, ExpressionAttributeValues=vals)


def _deps(aws, tmp_path, **kw):
    table, s3 = aws
    return writer.WriterDeps(table=table, s3=s3, bucket=BUCKET, root=str(tmp_path), now=lambda: "2026-10-08T00:00:00Z",
                             versions={"pylance": "test"}, **kw)


def _reader(aws, tmp_path, **kw):
    table, s3 = aws
    return GraphReader(table=table, s3=s3, bucket=BUCKET, root=str(tmp_path), engine="arrow", **kw)


def _ids(tmp_path, table_name):
    import lance

    return sorted(lance.dataset(layout.table_uri(str(tmp_path), PID, table_name)).to_table()["id"].to_pylist())


# ---- writer --------------------------------------------------------------------------------------

def test_first_sync_writes_starter_plus_demo_in_format_2_1(aws, tmp_path):
    import lance

    _seed(aws[0])
    out = writer.sync_project(PID, _deps(aws, tmp_path))
    assert out["result"] == "synced" and out["generation"] == 1
    doc, etag, _ = layout.read_meta(aws[1], BUCKET, PID)
    assert doc and etag and doc.demo["seeded"] is True and doc.storageVersion == "2.1"
    assert _ids(tmp_path, node_table("Asset"))[:2] == [f"asset:{IMG}", f"asset:{VID}"]
    assert doc.counts["nodes"] > 250 and doc.counts["edges"] > 250
    ds = lance.dataset(layout.table_uri(str(tmp_path), PID, node_table("Person")))
    assert ds.data_storage_version == "2.1"


def test_unchanged_meta_is_a_no_op(aws, tmp_path):
    _seed(aws[0])
    deps = _deps(aws, tmp_path)
    writer.sync_project(PID, deps)
    assert writer.sync_project(PID, deps)["result"] == "unchanged"


def test_asset_removal_deletes_its_node_but_keeps_sample_rows(aws, tmp_path):
    table, _ = aws
    _seed(table)
    deps = _deps(aws, tmp_path)
    writer.sync_project(PID, deps)
    samples_before = [i for i in _ids(tmp_path, node_table("Asset")) if i.startswith("sample:")]
    table.update_item(Key={"PK": pschema.project_pk(PID), "SK": f"ASSET#{IMG}"},
                      UpdateExpression="SET #s = :r", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":r": "removed"})
    _bump(table)
    out = writer.sync_project(PID, deps)
    assert out["result"] == "synced"
    asset_ids = _ids(tmp_path, node_table("Asset"))
    assert f"asset:{IMG}" not in asset_ids and f"asset:{VID}" in asset_ids
    assert [i for i in asset_ids if i.startswith("sample:")] == samples_before
    assert f"HAS_ASSET|project:{PID}|asset:{IMG}" not in _ids(tmp_path, edge_table("HAS_ASSET"))


def test_tag_change_rewrites_only_affected_tables(aws, tmp_path):
    table, s3 = aws
    _seed(table)
    deps = _deps(aws, tmp_path)
    writer.sync_project(PID, deps)
    before, _, _ = layout.read_meta(s3, BUCKET, PID)
    _bump(table, tags=["Sport", "Final"])
    out = writer.sync_project(PID, deps)
    after, _, _ = layout.read_meta(s3, BUCKET, PID)
    assert out["tablesWritten"] == 2   # nodes/Tag + edges/TAGGED
    assert _ids(tmp_path, node_table("Tag")) == ["tag:final", "tag:sport"]
    assert after.tables[node_table("Person")] == before.tables[node_table("Person")]


@pytest.mark.parametrize("graph,expected", [(False, "off"), (None, "off")])
def test_graph_off_or_absent_writes_nothing(aws, tmp_path, graph, expected):
    _seed(aws[0], graph=graph)
    assert writer.sync_project(PID, _deps(aws, tmp_path))["result"] == expected
    assert layout.read_meta(aws[1], BUCKET, PID)[0] is None


def test_deleted_project_is_not_synced(aws, tmp_path):
    _seed(aws[0])
    aws[0].update_item(Key=pschema.meta_key(PID), UpdateExpression="SET #s = :d",
                       ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "deleted"})
    assert writer.sync_project(PID, _deps(aws, tmp_path))["result"] == "inactive"


def test_compaction_keeps_fragment_count_bounded(aws, tmp_path):
    import lance

    table, _ = aws
    _seed(table)
    deps = _deps(aws, tmp_path, compact_fragments=2, cleanup_older_than_s=0)
    writer.sync_project(PID, deps)
    for i in range(4):
        _bump(table, tags=[f"t{i}"])
        writer.sync_project(PID, deps)
    ds = lance.dataset(layout.table_uri(str(tmp_path), PID, node_table("Tag")))
    assert len(ds.get_fragments()) <= 3
    assert len(ds.versions()) <= 3   # old versions cleaned up


def test_meta_conflict_is_retried(aws, tmp_path, monkeypatch):
    _seed(aws[0])
    real = layout.write_meta
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise layout.MetaConflict("412")
        return real(*a, **kw)

    monkeypatch.setattr(layout, "write_meta", flaky)
    assert writer.sync_project(PID, _deps(aws, tmp_path))["result"] == "synced"
    assert calls["n"] == 2


def test_meta_cas_rejects_a_stale_etag(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    doc, etag, _ = layout.read_meta(aws[1], BUCKET, PID)
    doc.generation += 1
    layout.write_meta(aws[1], BUCKET, PID, doc, etag)          # someone else moved it on
    doc.generation += 1
    with pytest.raises(layout.MetaConflict):
        layout.write_meta(aws[1], BUCKET, PID, doc, etag)       # our ETag is now stale


def test_project_deleted_during_sync_removes_graph_objects(aws, tmp_path, monkeypatch):
    table, s3 = aws
    _seed(table)
    real_get = writer._get_meta
    calls = {"n": 0}

    def get_meta(t, pid):
        calls["n"] += 1
        item = real_get(t, pid)
        if calls["n"] >= 2:
            item = {**item, "status": "deleted"}
        return item

    monkeypatch.setattr(writer, "_get_meta", get_meta)
    out = writer.sync_project(PID, _deps(aws, tmp_path))
    assert out["result"] == "deleted_during_sync"
    assert layout.read_meta(s3, BUCKET, PID)[0] is None


# ---- stream filter -------------------------------------------------------------------------------

def _rec(event, sk="META", graph=True, seq="1"):
    img = {"PK": {"S": f"PROJECT#{PID}"}, "SK": {"S": sk}}
    if graph is not None:
        img["graph"] = {"BOOL": graph}
    return {"eventName": event, "dynamodb": {"NewImage": img, "SequenceNumber": seq}}


def test_stream_filter_picks_graph_enabled_meta_once():
    ev = {"Records": [_rec("INSERT"), _rec("MODIFY", seq="2"), _rec("MODIFY", graph=False),
                      _rec("MODIFY", sk="ASSET#x"), _rec("REMOVE"), _rec("INSERT", graph=None)]}
    assert pids_from_stream(ev) == [(PID, "1")]


# ---- reader --------------------------------------------------------------------------------------

def test_reader_states_off_seeding_and_ready(aws, tmp_path):
    table, _ = aws
    asked = []
    _seed(table, graph=False)
    r = _reader(aws, tmp_path, request_sync=asked.append)
    assert r.handle("overview", PID) == {"state": "off"}
    _bump(table, graph=True)
    assert r.handle("overview", PID) == {"state": "seeding"} and asked == [PID]
    writer.sync_project(PID, _deps(aws, tmp_path))
    out = r.handle("overview", PID)
    assert out["state"] == "ready" and out["totals"]["nodes"] == len(out["nodes"])
    types = out["types"]["nodes"]
    real_assets = [n for n in out["nodes"] if types[n[1]] == "Asset" and n[3] == 0]
    assert {n[0] for n in real_assets} == {f"asset:{VID}", f"asset:{IMG}"}


def test_reader_caches_by_meta_etag(aws, tmp_path, monkeypatch):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    r = _reader(aws, tmp_path)
    loads = {"n": 0}
    real = r._load

    def counting(*a):
        loads["n"] += 1
        return real(*a)

    monkeypatch.setattr(r, "_load", counting)
    r.handle("stats", PID)
    r.handle("overview", PID)
    assert loads["n"] == 1
    _bump(aws[0], tags=["New"])
    writer.sync_project(PID, _deps(aws, tmp_path))
    assert r.handle("stats", PID)["generation"] == 2 and loads["n"] == 2


def test_neighbourhood_includes_incoming_and_outgoing(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    r = _reader(aws, tmp_path)
    out = r.handle("neighbourhood", PID, {"nodeId": f"project:{PID}"})
    rels = {e[2] for e in out["edges"]}
    assert {"OWNS", "MEMBER_OF", "TAGGED", "HAS_ASSET"} <= rels   # incoming (OWNS/MEMBER_OF) + outgoing
    assert out["node"]["type"] == "Project"
    two = r.handle("neighbourhood", PID, {"nodeId": f"user:{OWNER}", "hops": 2})
    assert f"asset:{VID}" in {n["id"] for n in two["nodes"]}


@pytest.mark.parametrize("node_id,code", [("asset:nope", "node_not_found"), ("bad", "invalid_node")])
def test_neighbourhood_rejects_unknown_nodes(aws, tmp_path, node_id, code):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    assert _reader(aws, tmp_path).handle("neighbourhood", PID, {"nodeId": node_id})["error"]["code"] == code


def test_stale_snapshot_requests_a_sync_but_still_serves(aws, tmp_path):
    asked = []
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    _bump(aws[0])   # META moved on; snapshot updatedAt is "2026-10-08T00:00:00Z", far in the past of clock
    r = _reader(aws, tmp_path, request_sync=asked.append, clock=lambda: 4_000_000_000)
    assert r.handle("stats", PID)["state"] == "ready" and asked == [PID]


def test_every_label_has_a_loaded_table(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    snap, _ = _reader(aws, tmp_path).snapshot(PID)
    assert all(node_table(l) in snap.tables for l in NODE_LABELS)


# ---- lineage (asset-lineage Phase 3, R6) ---------------------------------------------------------

def test_lineage_tables_are_written_from_the_asset_and_job_rows(aws, tmp_path):
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    assert _ids(tmp_path, node_table("Session")) == [f"session:{SID}"]
    assert _ids(tmp_path, node_table("Job")) == [f"job:{JOB}"]
    assert _ids(tmp_path, edge_table("DERIVED_FROM")) == [f"DERIVED_FROM|asset:{VID}|asset:{IMG}"]
    assert _ids(tmp_path, edge_table("PRODUCED_BY")) == [f"PRODUCED_BY|asset:{VID}|session:{SID}"]
    assert _ids(tmp_path, edge_table("OUTPUT_OF")) == [f"OUTPUT_OF|asset:{VID}|job:{JOB}"]
    assert _ids(tmp_path, edge_table("RAN_IN")) == [f"RAN_IN|job:{JOB}|session:{SID}"]


def test_what_came_from_this_clip_resolves_in_arrow_and_through_the_reader(aws, tmp_path):
    """The question Phase 3 adds the edges for, asked both ways round."""
    from project_graph.engine import ArrowQueries

    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    r = _reader(aws, tmp_path)
    snap, _ = r.snapshot(PID)
    q = ArrowQueries(snap.tables)

    # What came FROM the image: the video (an INCOMING DERIVED_FROM on the input).
    assert [e for e in q.neighbours(f"asset:{IMG}") if e[2] == "DERIVED_FROM"] == \
        [(f"asset:{VID}", f"asset:{IMG}", "DERIVED_FROM")]
    # What went INTO the video, plus the session and job that made it.
    by_rel = {e[2]: e for e in q.neighbours(f"asset:{VID}")}
    assert by_rel["DERIVED_FROM"] == (f"asset:{VID}", f"asset:{IMG}", "DERIVED_FROM")
    assert by_rel["PRODUCED_BY"] == (f"asset:{VID}", f"session:{SID}", "PRODUCED_BY")
    assert by_rel["OUTPUT_OF"] == (f"asset:{VID}", f"job:{JOB}", "OUTPUT_OF")
    # And from the session: its outputs and its jobs.
    assert {e[2] for e in q.neighbours(f"session:{SID}")} == {"PRODUCED_BY", "RAN_IN"}

    # The reader answers the same neighbourhood (what the SPA's Graph tab shows).
    out = r.handle("neighbourhood", PID, {"nodeId": f"session:{SID}"})
    assert out["state"] == "ready" and out["node"]["type"] == "Session"
    assert {(e[0], e[2]) for e in out["edges"]} == {(f"asset:{VID}", "PRODUCED_BY"), (f"job:{JOB}", "RAN_IN")}


def test_a_snapshot_written_before_lineage_existed_still_loads(aws, tmp_path):
    """Backward-compatible reads: an existing graph has no Session/Job/lineage tables in `_meta.json`."""
    _seed(aws[0])
    writer.sync_project(PID, _deps(aws, tmp_path))
    doc, etag, _ = layout.read_meta(aws[1], BUCKET, PID)
    for t in (node_table("Session"), node_table("Job"), edge_table("DERIVED_FROM"),
              edge_table("PRODUCED_BY"), edge_table("OUTPUT_OF"), edge_table("RAN_IN")):
        doc.tables.pop(t)
    layout.write_meta(aws[1], BUCKET, PID, doc, etag)

    r = _reader(aws, tmp_path)
    snap, state = r.snapshot(PID)
    assert state is None and snap is not None
    assert snap.tables[node_table("Session")].num_rows == 0, "a table the snapshot predates reads as empty"
    out = r.handle("overview", PID)
    assert out["state"] == "ready"
    assert "DERIVED_FROM" in out["types"]["edges"], "the type list still describes the current schema"
    # And the next sync fills them in.
    writer.sync_project(PID, _deps(aws, tmp_path), force=True)
    assert _ids(tmp_path, node_table("Session")) == [f"session:{SID}"]
