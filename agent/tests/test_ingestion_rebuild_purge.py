"""Collection_Rebuild (Req 6.9, 10.5–10.13) and the Project-deletion purge (Req 10.20–10.22) over moto."""
from __future__ import annotations

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_ingestion_pipeline import (  # noqa: E402,F401
    INGEST, PID, FakeM2C, FakeSFN, Harness, counts_match_rows, env, register, stream_event,
)

from ingestion import rebuild  # noqa: E402
from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.handlers.purge import PurgeDeps, PurgeTick, deleted_pids_from_stream, sweep_pids  # noqa: E402
from ingestion.handlers.rebuild_tick import RebuildDeps, RebuildTick  # noqa: E402
from ingestion.m2c import ToolError  # noqa: E402
from ingestion.pipeline_store import from_ddb  # noqa: E402

SM = "arn:aws:states:us-west-2:1:stateMachine:rebuild"


class World(Harness):
    def __init__(self, store, s3, m2c):
        super().__init__(store, s3, m2c)
        self.rsfn = FakeSFN()
        self.rtick = RebuildTick(RebuildDeps(store=store, m2c=m2c, m2c_admin=m2c, s3=s3, ingest_bucket=INGEST,
                                             sfn=self.rsfn, sm_arn=SM, concurrency=2, clock=lambda: self.clock[0]))
        self.tick.d.request_rebuild = self.request

    def request(self, pid, reason="test"):
        return rebuild.request_rebuild(self.store.table, self.rsfn, SM, pid, reason=reason, now_iso="t")

    def ingest(self, n=1):
        aids = register(self.store, n)
        self.dispatcher.handle(stream_event(aids))
        self.drive(max_ticks=400)
        return aids

    def drive_rebuild(self, max_ticks=300):
        done = set()
        for _ in range(max_ticks):
            live = [s for s in self.rsfn.started if s["name"] not in done]
            if not live:
                return
            for s in live:
                out = self.rtick.run(s["input"])
                if out["done"]:
                    done.add(s["name"])
            self.clock[0] += 20
        raise AssertionError("rebuild did not finish")


@pytest.fixture
def world(env):
    store, s3 = env
    return World(store, s3, FakeM2C())


def test_remove_triggers_a_rebuild_that_resubmits_only_indexed_assets(world):
    a, b, c = world.ingest(3)
    first_index_calls = len(world.m2c.indexed)
    svc = AssetService(store=world.store, verifier=type("V", (), {"verify": lambda s, t, now: {"sub": "u"}})(),
                       s3_client=world.store.table.meta.client and __import__("boto3").client("s3", region_name="us-west-2"),
                       ingest_bucket=INGEST, access_check=lambda *_: type("R", (), {"ok": True})(),
                       request_rebuild=world.request)
    reply = svc.handle({"userToken": "t", "projectId": PID, "command": "remove", "args": {"assetId": b}})
    assert reply["ok"], reply
    meta = world.meta()
    assert meta["collectionState"] == "rebuilding" and meta["rebuild"]["phase"] == "waiting"
    assert [s["name"] for s in world.rsfn.started] == [rebuild.execution_name(PID, 1)]
    world.drive_rebuild()
    meta = world.meta()
    assert (meta["collectionState"], meta["incarnation"], meta["rebuild"]["phase"]) == ("ready", 2, "done")
    assert world.m2c.deleted_collections == [PID]
    resubmitted = world.m2c.indexed[first_index_calls:]
    assert {d["asset_id"] for ch in resubmitted for d in ch["documents"]} == {a, c}  # not the removed one
    assert not any(d["refresh"] for ch in resubmitted for d in ch["documents"])     # fresh incarnation
    for aid in (a, c):
        row = world.row(aid)
        assert row["status"] == "indexed" and row["index"]["byIncarnation"]["2"]["accepted"] is True
    counts_match_rows(world)


def test_triggers_during_a_rebuild_coalesce_into_one_follow_up(world):
    world.ingest(1)
    assert world.request(PID) == rebuild.STARTED
    assert world.request(PID) == rebuild.COALESCED
    assert world.request(PID) == rebuild.COALESCED
    world.drive_rebuild()
    names = [s["name"] for s in world.rsfn.started]
    assert names == [rebuild.execution_name(PID, 1), rebuild.execution_name(PID, 2)]
    meta = world.meta()
    assert meta["collectionState"] == "ready" and meta["incarnation"] == 3 and meta["rebuild"]["seq"] == 2


def test_absent_collection_has_nothing_to_rebuild(world):
    assert world.request(PID) == rebuild.NOT_STARTED


def test_indexing_is_held_until_the_new_incarnation_exists(world):
    world.ingest(1)
    world.request(PID)
    (aid,) = register(world.store, 1)
    world.dispatcher.handle(stream_event([aid]))
    inp = world.sfn.started[-1]["input"]
    for _ in range(60):  # analysis finishes; indexing is held while the rebuild is in phase waiting
        world.tick.run(inp)
        world.clock[0] += 15
    assert world.row(aid)["status"] == "indexing"
    assert not any(d["asset_id"] == aid for ch in world.m2c.indexed for d in ch["documents"])
    world.drive_rebuild()
    world.drive()
    assert world.row(aid)["status"] == "indexed"
    assert any(d["asset_id"] == aid for ch in world.m2c.indexed for d in ch["documents"])


class FailingDelete(FakeM2C):
    def call_tool(self, name, args):
        if name == "hybrid-search-admin___delete_collection":
            raise ToolError("backend exploded", "BOOM")
        return super().call_tool(name, args)


def test_a_step_failing_three_times_sets_rebuild_failed_then_retry_works(env):
    store, s3 = env
    world = World(store, s3, FailingDelete())
    world.ingest(1)
    world.request(PID)
    world.drive_rebuild()
    meta = world.meta()
    assert meta["collectionState"] == "rebuild_failed"
    assert meta["rebuild"]["failure"]["step"] == "rebuild delete"
    assert meta["rebuild"]["failure"]["reason"].startswith("rebuild delete: backend exploded")
    assert world.request(PID) == rebuild.NOT_STARTED  # no automatic follow-up (Req 10.10)
    world.m2c.__class__ = FakeM2C
    assert rebuild.retry_rebuild(store.table, world.rsfn, SM, PID, now_iso="t") == 2
    world.drive_rebuild()
    assert world.meta()["collectionState"] == "ready"


def test_rebuild_failed_holds_indexing(env):
    store, s3 = env
    world = World(store, s3, FailingDelete())
    world.ingest(1)
    world.request(PID)
    world.drive_rebuild()
    (aid,) = register(store, 1)
    world.dispatcher.handle(stream_event([aid]))
    inp = world.sfn.started[-1]["input"]
    for _ in range(60):
        assert world.tick.run(inp)["done"] is False or world.row(aid)["status"] != "indexed"
        world.clock[0] += 15
    assert world.row(aid)["status"] == "indexing"


# ------------------------------------------------------------------------------------------- purge
def _delete_project(store):
    store.table.update_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"},
                            UpdateExpression="SET #st = :d, deletedAt = :at",
                            ExpressionAttributeNames={"#st": "status"},
                            ExpressionAttributeValues={":d": "deleted", ":at": "2026-10-06T00:00:00.000Z"})


def test_purge_removes_collection_objects_versions_records_and_leaves_a_tombstone(env):
    store, s3 = env
    world = World(store, s3, FakeM2C())
    s3.put_bucket_versioning(Bucket=INGEST, VersioningConfiguration={"Status": "Enabled"})
    a, b = world.ingest(2)
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{a}/clip.mp4", Body=b"v1")
    s3.put_object(Bucket=INGEST, Key=f"projects/{PID}/{a}/clip.mp4", Body=b"v2")
    s3.delete_object(Bucket=INGEST, Key=f"projects/{PID}/{a}/clip.mp4")          # a delete marker
    other = "p" + "d" * 32
    s3.put_object(Bucket=INGEST, Key=f"projects/{other}/x.mp4", Body=b"keep")     # another Project
    s3.put_object(Bucket=INGEST, Key="artifacts/sess/input/x.mp4", Body=b"keep")  # never in scope
    s3.create_multipart_upload(Bucket=INGEST, Key=f"projects/{PID}/{b}/big.mp4")
    _delete_project(store)
    tick = PurgeTick(PurgeDeps(table=store.table, s3=s3, m2c_admin=world.m2c, ingest_bucket=INGEST,
                               clock=lambda: world.clock[0]))
    for _ in range(30):
        if tick.run({"pid": PID})["done"]:
            break
        world.clock[0] += 30
    meta = from_ddb(store.get_meta(PID))
    assert meta.get("tombstone") is True and meta.get("purgedAt")
    items = store.table.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"PROJECT#{PID}"})["Items"]
    assert [i["SK"] for i in items] == ["META"]
    v = s3.list_object_versions(Bucket=INGEST, Prefix=f"projects/{PID}/")
    assert not v.get("Versions") and not v.get("DeleteMarkers")
    assert not s3.list_multipart_uploads(Bucket=INGEST, Prefix=f"projects/{PID}/").get("Uploads")
    assert s3.get_object(Bucket=INGEST, Key=f"projects/{other}/x.mp4")["Body"].read() == b"keep"
    assert s3.get_object(Bucket=INGEST, Key="artifacts/sess/input/x.mp4")["Body"].read() == b"keep"
    assert world.m2c.deleted_collections == [PID]
    assert tick.run({"pid": PID})["done"] is True  # idempotent


def test_purge_never_touches_a_live_project(env):
    store, s3 = env
    world = World(store, s3, FakeM2C())
    world.ingest(1)
    tick = PurgeTick(PurgeDeps(table=store.table, s3=s3, m2c_admin=world.m2c, ingest_bucket=INGEST))
    assert tick.run({"pid": PID})["done"] is True
    assert from_ddb(store.get_meta(PID))["status"] == "active"
    assert not getattr(world.m2c, "deleted_collections", [])


def test_purge_trigger_shapes(env):
    store, _ = env
    rec = lambda name, old, new: {"eventName": name, "dynamodb": {  # noqa: E731
        "OldImage": {"PK": {"S": f"PROJECT#{PID}"}, "SK": {"S": "META"}, "status": {"S": old}},
        "NewImage": {"PK": {"S": f"PROJECT#{PID}"}, "SK": {"S": "META"}, "status": {"S": new}}}}
    assert deleted_pids_from_stream({"Records": [rec("MODIFY", "active", "deleted")]}) == [PID]
    assert deleted_pids_from_stream({"Records": [rec("MODIFY", "deleted", "deleted"),
                                                 rec("MODIFY", "active", "active"),
                                                 rec("INSERT", "active", "deleted")]}) == []
    _delete_project(store)
    import datetime
    now = datetime.datetime(2026, 10, 6, 2, tzinfo=datetime.timezone.utc).timestamp()
    assert sweep_pids(store.table, now) == [PID]
    assert sweep_pids(store.table, now - 1.5 * 3600) == []  # deleted under 1 h ago


def test_stale_scope_after_reindex_requests_a_rebuild(world):
    (aid,) = world.ingest(1)
    row = world.row(aid)
    # Pretend an earlier submission of this incarnation also held `topic` rows the new record lacks.
    inc = row["index"]["byIncarnation"]["1"]
    inc["scopesAccepted"] = inc["scopesAccepted"] + ["segment"]
    world.store.table.update_item(Key={"PK": f"PROJECT#{PID}", "SK": f"ASSET#{aid}"},
                                  UpdateExpression="SET #i.byIncarnation = :b, #st = :f, slotHeld = :t",
                                  ExpressionAttributeNames={"#i": "index", "#st": "status"},
                                  ExpressionAttributeValues={":b": {"1": inc}, ":f": "failed", ":t": False})
    meta_counts = world.meta()["statusCounts"]
    world.store.table.update_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"},
                                  UpdateExpression="SET statusCounts.#ix = :z, statusCounts.#fl = :one",
                                  ExpressionAttributeNames={"#ix": "indexed", "#fl": "failed"},
                                  ExpressionAttributeValues={":z": meta_counts["indexed"] - 1, ":one": 1})
    world.store.retry_asset(PID, aid)
    world.dispatcher.handle(stream_event([aid]))
    world.drive()
    assert world.row(aid)["status"] == "indexed"
    assert world.meta()["collectionState"] == "rebuilding"
    assert world.rsfn.started and "left segment" in world.meta()["rebuild"]["reason"]
