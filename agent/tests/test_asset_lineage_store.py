"""Asset lineage — the ASSET-row model (asset-lineage design §2; R1.2, R3.2, R3.5, R4.1, R5).

``ingestion.store.AssetStore`` over a moto projects table:

- the lineage attributes (``role``, ``visibility``, ``sessionId``, ``producer``, ``derivedFrom``, ``jobId``,
  ``checksum``) are written only when given, and ``derivedFrom`` keeps well-formed ids other than its own;
- ``analysis``/``intermediate`` (and ``queue_for_indexing=False``) register ``stored`` with indexing ON, never
  queued, and ``enable_indexing``/``readd`` keep them stored;
- the ``HASH#`` guard: identical bytes in one Project return the existing asset with ``deduped``; a removed
  asset frees the guard; a stale guard is cleaned up; no checksum → no guard;
- ``checksum_from_head``, ``asset_id_for_location`` (incl. a chat copy's guard), and ``put_job``/``get_job``.
"""
from __future__ import annotations

import os
import sys
import uuid

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.store import AssetStore, ProjectStore, checksum_from_head, queueable  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "projects"
BUCKET = "ingest"
PID = "p" + "c" * 32
SUB = "owner-sub"
SID = "11111111-2222-4333-8444-555555555555"
CK = {"algo": "CRC64NVME", "value": "q0Tf3JkWZ0U=", "size": 5}


@pytest.fixture
def store(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={**schema.meta_key(PID), "projectId": PID, "status": "active"})
        yield AssetStore(t)


def _reg(store, *, kind="video", name="clip.mp4", key=None, aid=None, **kw):
    aid = aid or f"{kind}:{uuid.uuid4()}"
    key = key or f"artifacts/{SID}/output/{uuid.uuid4().hex[:6]}-{name}"
    return store.register_asset(
        pid=PID, asset_id=aid, source=kw.pop("source", "session"), source_uri=f"s3://{BUCKET}/{key}",
        source_bucket=BUCKET, source_key=key, source_size=5, content_type="video/mp4", file_name=name,
        added_by=SUB, added_by_display=SUB, **kw)


def _items(store, prefix):
    resp = store.table.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID))
                             & Key("SK").begins_with(prefix), ConsistentRead=True)
    return resp["Items"]


def _meta(store):
    return store.get_meta(PID)


# ------------------------------------------------------------------ attributes
def test_lineage_attributes_are_written_when_given(store):
    src = _reg(store, source="upload")
    aid = f"video:{uuid.uuid4()}"
    row = _reg(store, aid=aid, role="deliverable", visibility="project", session_id=SID,
               producer={"sessionId": SID, "turnId": "t1", "tool": None},
               derived_from=[src["assetId"], src["assetId"], aid, "nonsense"], job_id="job-1", checksum=CK)
    stored = store.get_asset(PID, aid)
    assert stored["role"] == "deliverable" and stored["visibility"] == "project" and stored["sessionId"] == SID
    assert stored["producer"] == {"sessionId": SID, "turnId": "t1"}  # empty fields dropped
    assert stored["derivedFrom"] == [src["assetId"]]  # unique, well-formed, never itself
    assert stored["jobId"] == "job-1" and row["checksum"] == CK
    assert stored["checksum"]["size"] == 5


def test_legacy_registration_writes_no_lineage_attributes(store):
    row = store.get_asset(PID, _reg(store, source="upload")["assetId"])
    for attr in ("role", "visibility", "sessionId", "producer", "derivedFrom", "jobId", "checksum"):
        assert attr not in row
    assert queueable(row)  # absent role ⇒ source (legacy): indexable


def test_derived_from_is_capped(store):
    ids = [f"video:{uuid.uuid4()}" for _ in range(25)]
    assert len(_reg(store, derived_from=ids)["derivedFrom"]) == 20


def test_bad_role_or_visibility_is_refused(store):
    with pytest.raises(ValueError):
        _reg(store, role="owner")
    with pytest.raises(ValueError):
        _reg(store, visibility="public")


# ------------------------------------------------------------------ never queued (R3.5)
@pytest.mark.parametrize("kw", [{"role": "analysis"}, {"role": "intermediate"}, {"queue_for_indexing": False}])
def test_analysis_is_stored_even_with_indexing_on(store, kw):
    row = _reg(store, kind="data", name="shots.json", **kw)
    assert row["status"] == "stored" and "queuedAt" not in row
    assert _items(store, schema.QUEUE_SK_PREFIX) == []
    meta = _meta(store)
    assert meta["statusCounts"]["stored"] == 1 and meta["statusCounts"]["queued"] == 0
    assert meta.get("assetListPk") != "SCHED"


def test_deliverable_follows_the_project_flag(store):
    assert _reg(store, role="deliverable")["status"] == "queued"
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :f",
                            ExpressionAttributeValues={":f": False})
    assert _reg(store, role="deliverable")["status"] == "stored"
    assert _reg(store, role="analysis", kind="data", name="a.json")["status"] == "stored"  # OFF: stored too


def test_enable_indexing_and_readd_never_queue_analysis(store):
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :f",
                            ExpressionAttributeValues={":f": False})
    media = _reg(store, role="deliverable")
    data = _reg(store, role="analysis", kind="data", name="shots.json")
    assert store.enable_indexing(PID, by=SUB) is True
    assert store.queue_stored(PID) == 1
    assert store.get_asset(PID, media["assetId"])["status"] == "queued"
    assert store.get_asset(PID, data["assetId"])["status"] == "stored"
    # Removed and re-added with indexing ON: an analysis file comes back stored, not queued.
    store.remove_asset(PID, data["assetId"], removed_by=SUB)
    assert store.readd_asset(PID, data["assetId"])["status"] == "stored"
    meta = _meta(store)
    assert meta["statusCounts"]["stored"] == 1 and meta["statusCounts"]["queued"] == 1



def test_queue_stored_can_be_limited_to_one_kind(store):
    """scripts/requeue_stored_documents.py: only the PDFs registered `stored` before session documents were
    indexed move; a stored video (or anything else) in the same Project is left for the indexing switch."""
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :f",
                            ExpressionAttributeValues={":f": False})
    video = _reg(store, role="deliverable")
    pdf = _reg(store, role="deliverable", kind="document", name="report.pdf")
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :t",
                            ExpressionAttributeValues={":t": True})
    assert store.queue_stored(PID, kind="document") == 1
    assert store.get_asset(PID, pdf["assetId"])["status"] == "queued"
    assert store.get_asset(PID, video["assetId"])["status"] == "stored"
    assert store.queue_stored(PID, kind="document") == 0  # idempotent


def test_requeue_script_dry_run_writes_nothing_and_execute_queues(store):
    import importlib.util
    from pathlib import Path as _P

    spec = importlib.util.spec_from_file_location(
        "requeue_stored_documents", _P(__file__).resolve().parents[1] / "scripts" / "requeue_stored_documents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :f",
                            ExpressionAttributeValues={":f": False})
    pdf = _reg(store, role="deliverable", kind="document", name="report.pdf")
    store.table.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :t",
                            ExpressionAttributeValues={":t": True})
    lines: list[str] = []
    assert mod.run(store.table, execute=False, out=lines.append)["eligible"] == 1
    assert store.get_asset(PID, pdf["assetId"])["status"] == "stored"
    assert mod.run(store.table, execute=True, out=lines.append)["queued"] == 1
    assert store.get_asset(PID, pdf["assetId"])["status"] == "queued"


# ------------------------------------------------------------------ HASH guard (R5)
def test_identical_bytes_dedupe_to_the_existing_asset(store):
    first = _reg(store, name="a.mp4", checksum=CK)
    guard = store.table.get_item(Key=schema.hash_guard_key(PID, CK["algo"], CK["value"], CK["size"]))["Item"]
    assert guard["assetId"] == first["assetId"]
    key = f"artifacts/{SID}/output/b.mp4"
    second_id = f"video:{uuid.uuid4()}"
    dup = _reg(store, name="b.mp4", key=key, aid=second_id, checksum=CK)
    assert dup["deduped"] is True and dup["assetId"] == first["assetId"]
    assert store.get_asset(PID, second_id) is None  # nothing was written for the duplicate
    assert store.asset_id_for_location(PID, BUCKET, key) is None
    assert _meta(store)["activeAssetCount"] == 1
    # Same value, different size: different content.
    other = _reg(store, name="c.mp4", checksum={**CK, "size": 6})
    assert "deduped" not in other


def test_removing_an_asset_frees_its_hash_guard(store):
    first = _reg(store, name="a.mp4", checksum=CK)
    store.remove_asset(PID, first["assetId"], removed_by=SUB)
    assert store.table.get_item(Key=schema.hash_guard_key(PID, CK["algo"], CK["value"], 5)).get("Item") is None
    again = _reg(store, name="a2.mp4", checksum=CK)
    assert "deduped" not in again and again["assetId"] != first["assetId"]


def test_a_stale_hash_guard_is_cleaned_up(store):
    hk = schema.hash_guard_key(PID, CK["algo"], CK["value"], 5)
    store.table.put_item(Item={**hk, "assetId": f"video:{uuid.uuid4()}"})  # names an asset that does not exist
    row = _reg(store, checksum=CK)
    assert "deduped" not in row and store.table.get_item(Key=hk)["Item"]["assetId"] == row["assetId"]


def test_no_checksum_means_no_guard(store):
    _reg(store, checksum=None)
    _reg(store, checksum={"algo": "CRC64NVME", "value": "", "size": 5})  # malformed: ignored
    assert _items(store, schema.HASH_SK_PREFIX) == []


def test_checksum_from_head():
    assert checksum_from_head({"ContentLength": 9, "ChecksumCRC64NVME": "n", "ChecksumSHA256": "s"}) == \
        {"algo": "CRC64NVME", "value": "n", "size": 9}
    assert checksum_from_head({"ContentLength": 9, "ChecksumSHA256": "s", "ChecksumCRC32": "c"}) == \
        {"algo": "SHA256", "value": "s", "size": 9}
    assert checksum_from_head({"ContentLength": 9, "ChecksumCRC32": "c"}) is None
    assert checksum_from_head({"ChecksumSHA256": "s"}) is None and checksum_from_head(None) is None


# ------------------------------------------------------------------ lookups and JOB#
def test_asset_id_for_location(store):
    key = f"artifacts/{SID}/output/cut.mp4"
    row = _reg(store, key=key)
    assert store.asset_id_for_location(PID, BUCKET, key) == row["assetId"]
    assert store.asset_id_for_location(PID, BUCKET, "artifacts/x/output/none.mp4") is None
    assert store.asset_id_for_location("nope", BUCKET, key) is None
    assert ProjectStore is AssetStore


def test_a_chat_copy_maps_back_to_its_asset(store):
    up = _reg(store, source="upload", key=f"projects/{PID}/x/clip.mp4")
    copy_key = f"artifacts/{SID}/input/1791-clip.mp4"
    store.add_chat_copy(PID, up["assetId"], copy_key, bucket=BUCKET)
    assert store.asset_id_for_location(PID, BUCKET, copy_key) == up["assetId"]
    # Without a bucket (older callers) only the row is updated.
    store.add_chat_copy(PID, up["assetId"], f"artifacts/{SID}/input/2-clip.mp4")
    assert store.asset_id_for_location(PID, BUCKET, f"artifacts/{SID}/input/2-clip.mp4") is None


def test_put_job_is_idempotent_and_unions_outputs(store):
    a, b = f"data:{uuid.uuid4()}", f"audio:{uuid.uuid4()}"
    first = store.put_job(PID, {"jobId": "arn:job", "tool": "m2c___shot_segmentation", "label": "Shot Segmentation",
                                "sessionId": SID, "turnId": None, "outputPrefix": "s3://m2c/x/", "outputs": [a]})
    assert first["role"] == "analysis" and "turnId" not in first
    again = store.put_job(PID, {"jobId": "arn:job", "outputs": [b, a], "tool": "m2c___shot_segmentation"})
    job = store.get_job(PID, "arn:job")
    assert job["outputs"] == [a, b] and job["createdAt"] == first["createdAt"] == again["createdAt"]
    assert job["label"] == "Shot Segmentation" and job["sessionId"] == SID  # kept from the first write
    assert job["SK"] == "JOB#arn:job" and store.get_job(PID, "missing") is None
    with pytest.raises(ValueError):
        store.put_job(PID, {"outputs": []})


def test_removing_a_readded_asset_that_lost_its_guard_still_removes(store):
    first = _reg(store, name="a.mp4", checksum=CK)
    store.remove_asset(PID, first["assetId"], removed_by=SUB)
    store.readd_asset(PID, first["assetId"])  # re-added without re-claiming the guard
    other = _reg(store, name="b.mp4", checksum=CK)  # so identical bytes take it
    assert "deduped" not in other
    row, changed = store.remove_asset(PID, first["assetId"], removed_by=SUB)
    assert changed and row["status"] == "removed"
    hk = schema.hash_guard_key(PID, CK["algo"], CK["value"], 5)
    assert store.table.get_item(Key=hk)["Item"]["assetId"] == other["assetId"]  # not freed: not its guard
