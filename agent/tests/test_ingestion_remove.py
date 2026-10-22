"""Projects Phase B: the ``document`` kind, ``remove``, ``record_chat_copy``, and ``capabilities``.

Asset_Service over a moto projects table + a moto **versioned** Ingest bucket (the deployed skills bucket
is versioned), with a fake User_Token verifier. Covers:

- ``remove`` — status ``removed`` (idempotent), META counters moved from the old status, QUEUE item and
  DNAME guard dropped (the name is free again), the row out of the ``assetsByRegistered`` list, the
  uploaded bytes and every chat copy deleted, and an s3-location source NEVER deleted;
- ``record_chat_copy`` — key shape enforced, refused for a removed asset;
- ``document`` — a non-media upload registers as ``document:<uuid>`` and waits in ``queued``;
- ``capabilities`` and the per-kind size limit at registration (config-driven via INGESTION_CONFIG);
- the reply is JSON-serializable (no ``Decimal`` / ``set``), which the Lambda runtime requires;
- the shared ``upload_cases.json`` fixture pins ``sanitize`` and ``kind_for`` for the TS twin.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.limits import DEFAULT_UPLOAD_LIMITS, kind_for, limits_from_env  # noqa: E402
from ingestion.names import sanitize  # noqa: E402
from ingestion.store import AssetStore, jsonable  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
BUCKET = "ingest-bucket"
PID = "p" + "e" * 32
OWNER = "owner-sub"
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "src", "ingestion", "fixtures", "upload_cases.json")


class FakeVerifier:
    def verify(self, token, now):
        if token != "good":
            raise ValueError("invalid token")
        return {"sub": OWNER}


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("INGESTION_CONFIG", raising=False)
    with mock_aws():
        res = boto3.resource("dynamodb", region_name=REGION)
        t = res.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "projectId": PID, "status": "active"})
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{OWNER}", "role": "owner"})
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        store = AssetStore(t)
        service = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET)
        yield service, store, s3, t


def _call(service, command, args):
    reply = service.handle({"userToken": "good", "projectId": PID, "command": command, "args": args,
                            "caller": "proxy"})
    json.dumps(reply)  # the Lambda runtime serializes with plain json: must never raise
    return reply


def _upload(service, s3, name, body, content_type):
    kind = kind_for(content_type, name)
    aid = f"{kind}:{uuid.uuid4()}"
    key = f"projects/{PID}/{aid}/{sanitize(name)}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)
    reply = _call(service, "register_upload",
                  {"assetId": aid, "fileName": name, "size": len(body), "contentType": content_type})
    assert reply["ok"] is True, reply
    return aid, key


def _exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def _meta(table):
    return table.get_item(Key=schema.meta_key(PID), ConsistentRead=True)["Item"]


# ------------------------------------------------------------------ document kind
def test_document_upload_registers_queued(ctx):
    service, store, s3, _ = ctx
    aid, _ = _upload(service, s3, "Quarterly report.pdf", b"%PDF-1.7 hello", "application/pdf")
    assert aid.startswith("document:")
    row = store.get_asset(PID, aid)
    assert row["kind"] == "document" and row["status"] == "queued"
    assert row["displayName"] == "Quarterly report.pdf"
    # resolve_assets surfaces a document like any other kind.
    reply = _call(service, "resolve_assets", {"assetIds": [aid]})
    assert reply["value"]["assets"][aid] == {"kind": "document", "displayName": "Quarterly report.pdf",
                                             "status": "queued"}


def test_register_upload_type_mismatch_for_document(ctx):
    service, _, s3, _ = ctx
    aid = f"video:{uuid.uuid4()}"  # a document declared under a video id
    key = f"projects/{PID}/{aid}/notes.txt"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"hi", ContentType="text/plain")
    reply = _call(service, "register_upload", {"assetId": aid, "fileName": "notes.txt", "size": 2,
                                                "contentType": "text/plain"})
    assert reply["status"] == 400 and reply["error"]["code"] == "type_mismatch"


def test_register_upload_enforces_the_kind_limit(ctx, monkeypatch):
    service, store, s3, _ = ctx
    limited = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET,
                           limits={**DEFAULT_UPLOAD_LIMITS, "document": 4})
    aid = f"document:{uuid.uuid4()}"
    key = f"projects/{PID}/{aid}/a.txt"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"12345", ContentType="text/plain")
    reply = _call(limited, "register_upload", {"assetId": aid, "fileName": "a.txt", "size": 5,
                                                "contentType": "text/plain"})
    assert reply["status"] == 400 and reply["error"]["code"] == "too_large"


def test_capabilities_and_config_override(ctx, monkeypatch):
    service, _, _, _ = ctx
    reply = _call(service, "capabilities", {})
    assert reply["value"] == {
        "maxFilesPerBatch": 20,
        "limits": {"video": 5368709120, "audio": 5368709120, "image": 52428800, "document": 524288000},
        "multipartThresholdBytes": 104857600,
        "partSizeBytes": 67108864,
    }
    env = {"INGESTION_CONFIG": json.dumps({"uploadLimits": {"image": 1000, "video": "bad", "audio": -1}})}
    assert limits_from_env(env) == {**DEFAULT_UPLOAD_LIMITS, "image": 1000}


# ------------------------------------------------------------------ remove
def test_remove_upload_deletes_bytes_and_chat_copies_and_frees_the_name(ctx):
    service, store, s3, table = ctx
    aid, key = _upload(service, s3, "clip.mp4", b"x" * 64, "video/mp4")
    copy_key = "artifacts/sess-1/input/1700000000000-clip.mp4"
    s3.copy_object(Bucket=BUCKET, Key=copy_key, CopySource={"Bucket": BUCKET, "Key": key})
    assert _call(service, "record_chat_copy", {"assetId": aid, "key": copy_key})["ok"] is True
    row = store.get_asset(PID, aid)
    assert row["chatCopyKeys"] == {copy_key}
    before = _meta(table)
    assert before["statusCounts"]["queued"] == 1 and before["activeAssetCount"] == 1

    reply = _call(service, "remove", {"assetId": aid})
    assert reply["ok"] is True, reply
    value = reply["value"]
    assert value["changed"] is True and value["asset"]["status"] == "removed"
    assert sorted(value["deleted"]) == sorted([key, copy_key]) and value["failed"] == []
    assert not _exists(s3, key) and not _exists(s3, copy_key)

    row = store.get_asset(PID, aid)
    assert row["status"] == "removed" and row["previousStatus"] == "queued"
    assert "assetListPk" not in row and "assetListSk" not in row  # out of the asset list GSI
    meta = _meta(table)
    assert meta["statusCounts"]["queued"] == 0 and meta["statusCounts"]["removed"] == 1
    assert meta["activeAssetCount"] == 0
    assert meta["changeSeq"] == before["changeSeq"] + 1
    # QUEUE item and DNAME guard are gone; the SRC guard stays.
    items = table.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"PROJECT#{PID}"})
    sks = [i["SK"] for i in items["Items"]]
    assert not any(sk.startswith("QUEUE#") or sk.startswith("DNAME#") for sk in sks)
    assert any(sk.startswith("SRC#") for sk in sks)
    # The freed name is reused by the next upload of the same file name (no " (2)").
    aid2, _ = _upload(service, s3, "clip.mp4", b"y" * 8, "video/mp4")
    assert store.get_asset(PID, aid2)["displayName"] == "clip.mp4"


def test_remove_is_idempotent_and_retries_cleanup(ctx):
    service, store, s3, table = ctx
    aid, key = _upload(service, s3, "a.png", b"png", "image/png")
    assert _call(service, "remove", {"assetId": aid})["value"]["changed"] is True
    counts_after_first = _meta(table)["statusCounts"]
    again = _call(service, "remove", {"assetId": aid})
    assert again["ok"] is True
    assert again["value"]["changed"] is False and again["value"]["asset"]["status"] == "removed"
    # A repeat never double-counts, and deletes nothing once cleanup finished (no extra delete markers).
    assert _meta(table)["statusCounts"] == counts_after_first
    assert again["value"]["deleted"] == [] and again["value"]["failed"] == []
    assert store.get_asset(PID, aid)["cleanupDone"] is True


def test_remove_retries_a_failed_delete_on_the_next_call(ctx):
    service, store, s3, _ = ctx
    aid, key = _upload(service, s3, "b.png", b"png", "image/png")

    class FlakyS3:
        def __init__(self, real):
            self._real = real
            self.fail = True

        def delete_object(self, **kw):
            if self.fail:
                raise RuntimeError("throttled")
            return self._real.delete_object(**kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    flaky = FlakyS3(s3)
    svc = AssetService(store=store, verifier=FakeVerifier(), s3_client=flaky, ingest_bucket=BUCKET)
    first = _call(svc, "remove", {"assetId": aid})
    assert first["value"]["failed"] == [key] and _exists(s3, key)
    assert "cleanupDone" not in store.get_asset(PID, aid)
    flaky.fail = False
    second = _call(svc, "remove", {"assetId": aid})
    assert second["value"]["changed"] is False and second["value"]["deleted"] == [key]
    assert not _exists(s3, key)


def test_remove_never_deletes_an_s3_location_source(ctx):
    service, store, s3, _ = ctx
    key = f"projects/{PID}/incoming/source.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"v" * 10, ContentType="video/mp4")
    reg = _call(service, "register_s3_location", {"location": f"s3://{BUCKET}/{key}"})
    [row] = reg["value"]["registered"]
    assert row["source"] == "s3"
    reply = _call(service, "remove", {"assetId": row["assetId"]})
    assert reply["ok"] is True
    assert reply["value"]["deleted"] == []
    assert _exists(s3, key), "an s3-location source object must never be deleted"


def test_remove_unknown_and_malformed_asset_is_uniform_404(ctx):
    service, _, _, _ = ctx
    for aid in (f"video:{uuid.uuid4()}", "video:nope", None):
        reply = _call(service, "remove", {"assetId": aid})
        assert reply["status"] == 404 and reply["error"]["code"] == "asset_not_found"


def test_record_chat_copy_rejects_bad_keys_and_removed_assets(ctx):
    service, _, s3, _ = ctx
    aid, _ = _upload(service, s3, "a.wav", b"RIFF", "audio/wav")
    for bad in ("projects/x/y", "artifacts/s/output/a.wav", "artifacts/../input/a", "artifacts/s/input/a/b"):
        reply = _call(service, "record_chat_copy", {"assetId": aid, "key": bad})
        assert reply["status"] == 400, bad
    _call(service, "remove", {"assetId": aid})
    reply = _call(service, "record_chat_copy", {"assetId": aid, "key": "artifacts/s/input/1-a.wav"})
    assert reply["status"] == 404 and reply["error"]["code"] == "asset_not_found"


def test_jsonable_converts_decimal_and_sets():
    from decimal import Decimal

    assert jsonable({"a": Decimal("3"), "b": Decimal("1.5"), "c": {"y", "x"}, "d": [Decimal(1)]}) == {
        "a": 3, "b": 1.5, "c": ["x", "y"], "d": [1]}


# ------------------------------------------------------------------ shared TS/Python fixture
def test_upload_cases_fixture_matches_python():
    with open(FIXTURE, encoding="utf-8") as f:
        cases = json.load(f)
    for c in cases["sanitize"]:
        assert sanitize(c["name"]) == c["sanitized"], c["name"]
    for c in cases["kind"]:
        assert kind_for(c["contentType"], c["name"]) == c["kind"], c
