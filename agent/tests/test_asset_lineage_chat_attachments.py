"""Asset-lineage Phase 2: chat attachments by reference, and "this chat only" session assets (R12, D2).

Asset_Service over a moto projects table + a moto versioned Ingest bucket, with a fake User_Token verifier
(the same harness as ``test_ingestion_remove.py``). Covers:

- ``record_chat_session`` — the session association on the ASSET row (``chatSessions``), nothing copied;
- ``register_upload`` with ``visibility="session"`` — a hidden asset of the session: ``role=source``,
  ``sessionId``, the ``Chats/<title> · <date>`` folder, ``stored`` and never queued for indexing;
- ``remove`` of a session-visibility asset — its bytes go with it (the session owns them);
- ``remove_session_assets`` — a deleted session takes its hidden files with it, idempotently, and leaves
  project-visible assets of the same session alone;
- backward compatibility — a legacy row's ``chatCopyKeys`` copies are still deleted on ``remove``.
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
from ingestion.limits import kind_for  # noqa: E402
from ingestion.names import sanitize  # noqa: E402
from ingestion.store import AssetStore  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
BUCKET = "ingest-bucket"
PID = "p" + "f" * 32
OWNER = "owner-sub"
SID = "sess-11111111"


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


def _upload(service, s3, name, body, content_type, **extra):
    kind = kind_for(content_type, name)
    aid = f"{kind}:{uuid.uuid4()}"
    key = f"projects/{PID}/{aid}/{sanitize(name)}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type)
    reply = _call(service, "register_upload",
                  {"assetId": aid, "fileName": name, "size": len(body), "contentType": content_type, **extra})
    assert reply["ok"] is True, reply
    return aid, key


def _exists(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def _sks(table):
    items = table.query(KeyConditionExpression="PK = :p", ExpressionAttributeValues={":p": f"PROJECT#{PID}"})
    return [i["SK"] for i in items["Items"]]


# ------------------------------------------------------------------ record_chat_session
def test_record_chat_session_records_the_association_and_copies_nothing(ctx):
    service, store, s3, table = ctx
    aid, key = _upload(service, s3, "clip.mp4", b"x" * 32, "video/mp4")
    assert _call(service, "record_chat_session", {"assetId": aid, "sessionId": SID})["ok"] is True
    # A second chat referencing the same asset joins the set; a repeat of the same chat changes nothing.
    _call(service, "record_chat_session", {"assetId": aid, "sessionId": "sess-2"})
    _call(service, "record_chat_session", {"assetId": aid, "sessionId": SID})
    row = store.get_asset(PID, aid)
    assert row["chatSessions"] == {SID, "sess-2"}
    assert "chatCopyKeys" not in row
    # Nothing was copied anywhere: the asset's own object is still the only one.
    assert [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]] == [key]


def test_record_chat_session_rejects_bad_ids_and_a_removed_asset(ctx):
    service, _, s3, _ = ctx
    aid, _ = _upload(service, s3, "a.wav", b"RIFF", "audio/wav")
    for bad in ("", "a/b", "x" * 129, 5, None):
        reply = _call(service, "record_chat_session", {"assetId": aid, "sessionId": bad})
        assert reply["status"] == 400, bad
    assert _call(service, "record_chat_session", {"assetId": "audio:nope", "sessionId": SID})["status"] == 404
    _call(service, "remove", {"assetId": aid})
    reply = _call(service, "record_chat_session", {"assetId": aid, "sessionId": SID})
    assert reply["status"] == 404 and reply["error"]["code"] == "asset_not_found"


# ------------------------------------------------------------------ "this chat only" uploads
def test_session_visibility_upload_is_a_hidden_never_indexed_asset_in_the_sessions_folder(ctx):
    service, store, s3, table = ctx

    def catalog():  # a session upload is never queued, so the M2C catalog is not consulted at all
        raise AssertionError("the catalog must not be read for a session-visibility upload")

    svc = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET,
                       catalog_tools=catalog)
    aid, key = _upload(svc, s3, "note.mp4", b"v" * 16, "video/mp4",
                       visibility="session", sessionId=SID, sessionTitle="Trailer cut",
                       sessionCreatedAt="2026-10-09T08:00:00.000Z")
    row = store.get_asset(PID, aid)
    assert row["visibility"] == "session"
    assert row["role"] == "source"
    assert row["sessionId"] == SID
    assert row["producer"] == {"sessionId": SID}
    assert row["folder"] == "Chats/Trailer cut · 2026-10-09"
    assert row["status"] == "stored"
    assert row["sourceUri"] == f"s3://{BUCKET}/{key}"
    # Never queued for indexing, whatever META.indexing says: no QUEUE item was written.
    assert not any(sk.startswith("QUEUE#") for sk in _sks(table))


def test_session_visibility_falls_back_to_the_session_id_folder(ctx):
    service, store, s3, _ = ctx
    aid, _ = _upload(service, s3, "note.png", b"png", "image/png",
                     visibility="session", sessionId=SID, sessionCreatedAt="2026-10-09T08:00:00.000Z")
    assert store.get_asset(PID, aid)["folder"] == "Chats/Session sess-111 · 2026-10-09"


def test_session_visibility_needs_a_session_and_refuses_any_other_visibility(ctx):
    service, _, s3, _ = ctx
    for extra in ({"visibility": "session"}, {"visibility": "session", "sessionId": "a/b"},
                  {"visibility": "project", "sessionId": SID}, {"visibility": "hidden", "sessionId": SID}):
        aid = f"image:{uuid.uuid4()}"
        key = f"projects/{PID}/{aid}/a.png"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"png", ContentType="image/png")
        reply = _call(service, "register_upload",
                      {"assetId": aid, "fileName": "a.png", "size": 3, "contentType": "image/png", **extra})
        assert reply["status"] == 400 and reply["error"]["code"] == "invalid_args", extra


def test_remove_deletes_a_session_visibility_assets_bytes(ctx):
    service, store, s3, _ = ctx
    aid, key = _upload(service, s3, "note.png", b"png", "image/png", visibility="session", sessionId=SID)
    reply = _call(service, "remove", {"assetId": aid})
    assert reply["value"]["deleted"] == [key] and reply["value"]["failed"] == []
    assert not _exists(s3, key)
    assert store.get_asset(PID, aid)["status"] == "removed"


# ------------------------------------------------------------------ session deletion
def test_remove_session_assets_removes_the_hidden_files_of_that_session_only(ctx):
    service, store, s3, _ = ctx
    hidden_a, key_a = _upload(service, s3, "a.png", b"a", "image/png", visibility="session", sessionId=SID)
    hidden_b, key_b = _upload(service, s3, "b.png", b"b", "image/png", visibility="session", sessionId=SID)
    other, key_other = _upload(service, s3, "c.png", b"c", "image/png", visibility="session", sessionId="sess-2")
    shared, key_shared = _upload(service, s3, "d.png", b"d", "image/png")  # a project-visible file of the project

    reply = _call(service, "remove_session_assets", {"sessionId": SID})
    assert reply["ok"] is True
    assert sorted(reply["value"]["removed"]) == sorted([hidden_a, hidden_b])
    assert reply["value"]["failed"] == []
    assert not _exists(s3, key_a) and not _exists(s3, key_b)
    assert _exists(s3, key_other) and _exists(s3, key_shared)
    assert store.get_asset(PID, other)["status"] != "removed"
    assert store.get_asset(PID, shared)["status"] != "removed"

    # Idempotent: a repeat finds nothing left to remove.
    again = _call(service, "remove_session_assets", {"sessionId": SID})
    assert again["value"] == {"removed": [], "failed": []}


def test_remove_session_assets_validates_the_session_id(ctx):
    service, _, _, _ = ctx
    for bad in ("", "a/b", "x" * 129, None):
        assert _call(service, "remove_session_assets", {"sessionId": bad})["status"] == 400, bad


def test_session_assets_lists_only_live_hidden_rows_of_one_session(ctx):
    service, store, s3, _ = ctx
    hidden, _ = _upload(service, s3, "a.png", b"a", "image/png", visibility="session", sessionId=SID)
    _upload(service, s3, "b.png", b"b", "image/png", visibility="session", sessionId="sess-2")
    _upload(service, s3, "c.png", b"c", "image/png")
    assert [r["assetId"] for r in store.session_assets(PID, SID)] == [hidden]
    _call(service, "remove", {"assetId": hidden})
    assert store.session_assets(PID, SID) == []


# ------------------------------------------------------------------ backward compatibility
def test_a_legacy_chat_copy_is_still_deleted_when_its_asset_is_removed(ctx):
    """Phase 2 records no new copies, but rows written before it still own bytes under `artifacts/<sid>/input/`."""
    service, store, s3, _ = ctx
    aid, key = _upload(service, s3, "clip.mp4", b"x" * 64, "video/mp4")
    legacy_copy = f"artifacts/{SID}/input/1700000000000-clip.mp4"
    s3.copy_object(Bucket=BUCKET, Key=legacy_copy, CopySource={"Bucket": BUCKET, "Key": key})
    store.add_chat_copy(PID, aid, legacy_copy, bucket=BUCKET)  # as the pre-Phase-2 proxy did
    _call(service, "record_chat_session", {"assetId": aid, "sessionId": SID})  # and the new association

    reply = _call(service, "remove", {"assetId": aid})
    assert sorted(reply["value"]["deleted"]) == sorted([key, legacy_copy])
    assert not _exists(s3, key) and not _exists(s3, legacy_copy)


def test_record_chat_copy_still_answers_for_an_older_proxy_image(ctx):
    service, store, s3, _ = ctx
    aid, _ = _upload(service, s3, "clip.mp4", b"x" * 8, "video/mp4")
    key = f"artifacts/{SID}/input/1700000000000-clip.mp4"
    assert _call(service, "record_chat_copy", {"assetId": aid, "key": key})["ok"] is True
    assert store.get_asset(PID, aid)["chatCopyKeys"] == {key}


def test_a_this_chat_only_asset_is_never_referenced_from_another_chat(ctx):
    """The D2 rule lives in the service, so the proxy's attach route and the agent's `use_asset` share it."""
    service, store, s3, _ = ctx
    svc = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET,
                       catalog_tools=lambda: [])
    aid, _ = _upload(svc, s3, "note.mp4", b"v" * 16, "video/mp4", visibility="session", sessionId=SID,
                     sessionTitle="Trailer cut", sessionCreatedAt="2026-10-09T08:00:00.000Z")
    assert _call(svc, "record_chat_session", {"assetId": aid, "sessionId": "sess-other"})["status"] == 404
    assert _call(svc, "record_chat_session", {"assetId": aid, "sessionId": SID})["ok"] is True
    assert store.get_asset(PID, aid)["chatSessions"] == {SID}
