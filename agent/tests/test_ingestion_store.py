"""``ingestion.store.AssetStore`` over a moto projects table (design §2 "Registration transaction").

Covers the read side (``batch_get_assets`` omission semantics) and the write side (the registration
transaction, idempotency on ``asset_id``, the Display_Name ` (n)` allocation, and the META counter
update). No network: moto's in-memory DynamoDB.
"""
from __future__ import annotations

import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.store import AssetStore, asset_public_view  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
PID = "p" + "a" * 32
OTHER_PID = "p" + "b" * 32
SUB = "owner-sub"


def _vid() -> str:
    return f"video:{uuid.uuid4()}"


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        res = boto3.resource("dynamodb", region_name=REGION)
        t = res.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for pid in (PID, OTHER_PID):
            t.put_item(Item={"PK": f"PROJECT#{pid}", "SK": "META", "projectId": pid, "status": "active"})
        yield AssetStore(t)


def _register(store, pid=PID, file_name="clip.mp4", asset_id=None, bucket="ingest", key=None):
    asset_id = asset_id or _vid()
    key = key or f"projects/{pid}/{asset_id}/{file_name}"
    return store.register_asset(
        pid=pid,
        asset_id=asset_id,
        source="upload",
        source_uri=f"s3://{bucket}/{key}",
        source_bucket=bucket,
        source_key=key,
        source_size=1234,
        content_type="video/mp4",
        file_name=file_name,
        added_by=SUB,
        added_by_display=SUB,
    )


def test_register_writes_row_guards_queue_and_counts(store):
    asset = _register(store)
    aid = asset["assetId"]

    row = store.get_asset(PID, aid)
    assert row is not None
    assert row["status"] == "queued"
    assert row["kind"] == "video"
    assert row["displayName"] == "clip.mp4"
    assert row["source"] == "upload"

    # SRC + DNAME guards exist and point at the asset.
    src = store.table.get_item(Key=schema.src_guard_key(PID, "ingest", f"projects/{PID}/{aid}/clip.mp4"))["Item"]
    assert src["assetId"] == aid
    dname = store.table.get_item(Key=schema.dname_guard_key(PID, "clip.mp4"))["Item"]
    assert dname["assetId"] == aid

    # META counters moved.
    meta = store.get_meta(PID)
    assert meta["statusCounts"]["queued"] == 1
    assert meta["activeAssetCount"] == 1
    assert meta["changeSeq"] == 1


def test_register_is_idempotent_on_asset_id(store):
    asset = _register(store)
    again = _register(store, asset_id=asset["assetId"], key=asset["sourceUri"].split("ingest/", 1)[1])
    assert again["assetId"] == asset["assetId"]
    # The counter did not double-count.
    assert store.get_meta(PID)["statusCounts"]["queued"] == 1


def test_display_name_allocation_appends_suffix(store):
    first = _register(store, file_name="dup.mp4")
    second = _register(store, file_name="dup.mp4")
    assert first["displayName"] == "dup.mp4"
    assert second["displayName"] == "dup.mp4 (2)"
    assert store.get_meta(PID)["statusCounts"]["queued"] == 2


def test_batch_get_omits_absent_malformed_and_other_project(store):
    a = _register(store)["assetId"]
    other = _register(store, pid=OTHER_PID)["assetId"]
    absent = _vid()
    got = store.batch_get_assets(PID, [a, other, absent, "garbage", a])
    assert set(got) == {a}  # other-Project id absent (keyed by PID), malformed dropped, absent omitted


def test_asset_public_view_shapes_and_rejects():
    assert asset_public_view({"kind": "video", "displayName": "x", "status": "indexed"}) == {
        "kind": "video",
        "displayName": "x",
        "status": "indexed",
    }
    assert asset_public_view({"kind": "doc", "displayName": "x", "status": "indexed"}) is None
    assert asset_public_view({"kind": "video", "status": "indexed"}) is None
    assert asset_public_view("nope") is None
