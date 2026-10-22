"""Project-level indexing (default OFF, one way OFF→ON): the ``stored`` status and ``enable_indexing``.

With indexing OFF a registered file is ``stored``: kept, findable by name, never ingested (no QUEUE item, no
SCHED pair, so the Dispatcher cannot claim it). The owner turns indexing ON once; every stored file is then
queued. A META row with no ``indexing`` attribute predates the setting and keeps ingesting (ON).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_ingestion_pipeline import INGEST, PID, Harness, FakeM2C, env, stream_event  # noqa: E402,F401

from boto3.dynamodb.conditions import Key  # noqa: E402

from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.handlers.dispatcher import Dispatcher  # noqa: E402
from ingestion.store import AssetStore, indexing_on  # noqa: E402
from projects import schema  # noqa: E402

OWNER, MEMBER = "owner-sub", "member-sub"


def _set_indexing(store, value):
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :v",
                            ExpressionAttributeValues={":v": value})


def _register(store, n=1, kind="video", ext="mp4", name="clip"):
    ids = []
    for i in range(n):
        aid = f"{kind}:{uuid.uuid4()}"
        key = f"projects/{PID}/{aid}/{name}{i}.{ext}"
        store.register_asset(pid=PID, asset_id=aid, source="upload", source_uri=f"s3://{INGEST}/{key}",
                             source_bucket=INGEST, source_key=key, source_size=10, content_type=f"{kind}/{ext}",
                             file_name=f"{name}{i}.{ext}", added_by="u", added_by_display="u")
        ids.append(aid)
    return ids


def _partition(store):
    return store.table.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID)),
                             ConsistentRead=True)["Items"]


def _queue_items(store):
    return [i for i in _partition(store) if i["SK"].startswith(schema.QUEUE_SK_PREFIX)]


def _counts_match_rows(store):
    rows = [i for i in _partition(store) if i["SK"].startswith(schema.ASSET_SK_PREFIX)]
    meta = store.get_meta(PID)
    for st in ("stored", "queued", "analyzing", "indexing", "indexed", "failed", "removed"):
        assert int(meta["statusCounts"].get(st, 0)) == sum(1 for r in rows if r["status"] == st), st
    assert int(meta["activeAssetCount"]) == sum(1 for r in rows if r["status"] != "removed")


def test_indexing_on_semantics():
    assert indexing_on(None) is True and indexing_on({}) is True        # legacy row: already ingesting
    assert indexing_on({"indexing": True}) is True
    assert indexing_on({"indexing": False}) is False
    assert indexing_on({"indexing": "yes"}) is False                     # malformed → OFF


def test_off_registers_stored_and_nothing_can_ingest_it(env):
    store, _s3 = env
    _set_indexing(store, False)
    (aid,) = _register(store)
    row = store.get_asset(PID, aid)
    assert row["status"] == "stored" and "queuedAt" not in row
    assert _queue_items(store) == []
    meta = store.get_meta(PID)
    assert int(meta["statusCounts"]["stored"]) == 1 and int(meta["statusCounts"]["queued"]) == 0
    assert "assetListPk" not in meta  # no SCHED pair → the Dispatcher's sweep never visits the Project
    assert store.find_by_name(PID, "clip0")[0]["assetId"] == aid  # still findable by name
    _counts_match_rows(store)


def test_legacy_project_without_the_flag_still_ingests(env):
    store, _s3 = env
    (aid,) = _register(store)
    assert store.get_asset(PID, aid)["status"] == "queued" and len(_queue_items(store)) == 1


def test_enable_is_one_way_and_queues_every_stored_file(env):
    store, _s3 = env
    _set_indexing(store, False)
    ids = _register(store, 3)
    assert store.enable_indexing(PID, by=OWNER) is True
    assert store.enable_indexing(PID, by=OWNER) is False          # already ON: a no-op
    assert store.queue_stored(PID) == 3
    assert store.queue_stored(PID) == 0                           # idempotent
    assert {store.get_asset(PID, a)["status"] for a in ids} == {"queued"}
    assert len(_queue_items(store)) == 3
    meta = store.get_meta(PID)
    assert meta["indexing"] is True and meta["indexingEnabledBy"] == OWNER and meta["assetListPk"] == "SCHED"
    _counts_match_rows(store)


def test_enabled_project_runs_the_pipeline_end_to_end(env):
    store, s3 = env
    _set_indexing(store, False)
    (aid,) = _register(store)
    h = Harness(store, s3, FakeM2C())
    h.dispatcher.handle(stream_event([aid]))  # even a wake-up for it claims nothing while OFF
    assert store.get_asset(PID, aid)["status"] == "stored"
    store.enable_indexing(PID, by=OWNER)
    store.queue_stored(PID)
    h.dispatcher.handle(stream_event([aid]))
    h.drive(max_ticks=400)
    assert store.get_asset(PID, aid)["status"] == "indexed"
    _counts_match_rows(store)


def test_queue_stored_does_nothing_while_off(env):
    store, _s3 = env
    _set_indexing(store, False)
    _register(store, 2)
    assert store.queue_stored(PID) == 0 and _queue_items(store) == []


def test_registration_that_races_the_flip_lands_queued(env, monkeypatch):
    """Read OFF, but the owner turned indexing ON before the transaction: it must not stay stored."""
    store, _s3 = env
    _set_indexing(store, False)
    stale = store.get_meta(PID)
    store.enable_indexing(PID, by=OWNER)
    monkeypatch.setattr(store, "_ensure_meta_counts", lambda pid: (AssetStore._ensure_meta_counts(store, pid), stale)[1])
    (aid,) = _register(store)
    assert store.get_asset(PID, aid)["status"] == "queued"
    _counts_match_rows(store)


def test_remove_and_readd_while_off_stays_stored(env):
    store, _s3 = env
    _set_indexing(store, False)
    (aid,) = _register(store)
    store.remove_asset(PID, aid, removed_by="u")
    assert store.get_asset(PID, aid)["status"] == "removed"
    _counts_match_rows(store)
    row = store.readd_asset(PID, aid)
    assert row["status"] == "stored" and _queue_items(store) == []
    _counts_match_rows(store)


# ------------------------------------------------------------------ Asset_Service enable_indexing
class _Verifier:
    def __init__(self, sub):
        self.sub = sub

    def verify(self, token, now):
        return {"sub": self.sub}


def _service(store, sub, catalog=None):
    return AssetService(store=store, verifier=_Verifier(sub), s3_client=None, ingest_bucket=INGEST,
                        catalog_tools=catalog)


def _members(store):
    store.table.put_item(Item={**schema.member_key(PID, OWNER), "role": "owner"})
    store.table.put_item(Item={**schema.member_key(PID, MEMBER), "role": "member"})


def test_service_enable_is_owner_only_and_reports_the_queue(env):
    store, _s3 = env
    _members(store)
    _set_indexing(store, False)
    _register(store, 2)
    ev = {"userToken": "t", "projectId": PID, "command": "enable_indexing", "args": {}}
    denied = _service(store, MEMBER).handle(ev)
    assert denied["ok"] is False and denied["status"] == 403 and denied["error"]["code"] == "owner_only"
    assert store.get_meta(PID)["indexing"] is False
    ok = _service(store, OWNER).handle(ev)
    assert ok == {"ok": True, "value": {"indexing": True, "changed": True, "queued": 2}}
    again = _service(store, OWNER).handle(ev)
    assert again["value"] == {"indexing": True, "changed": False, "queued": 0}


def test_service_enable_refuses_when_m2c_lacks_a_workflow(env):
    store, _s3 = env
    _members(store)
    _set_indexing(store, False)
    _register(store, 1)
    reply = _service(store, OWNER, catalog=lambda: []).handle(
        {"userToken": "t", "projectId": PID, "command": "enable_indexing", "args": {}})
    assert reply["ok"] is False and reply["error"]["code"] == "catalog_missing"
    assert store.get_meta(PID)["indexing"] is False  # nothing flipped


def test_project_status_reports_indexing_and_stored(env):
    store, _s3 = env
    _members(store)
    _set_indexing(store, False)
    _register(store, 2, name="trailer")
    reply = _service(store, MEMBER).handle(
        {"userToken": "t", "projectId": PID, "command": "project_status", "args": {"name": "trailer1"}})
    v = reply["value"]
    assert v["indexing"] is False and v["counts"]["stored"] == 2
    assert [m["displayName"] for m in v["matches"]] == ["trailer1.mp4"]
