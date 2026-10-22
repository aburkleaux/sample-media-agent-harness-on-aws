"""``ingestion.handlers.asset_service`` — the Asset_Service Lambda (design §2).

Exercises the prologue (token + membership), the ``resolve_assets`` contract shape that
``project_search.assets`` depends on, ``register_upload`` (HeadObject size verification), and
``register_s3_location`` over a moto Ingest bucket. The Cognito verifier is injected (a fake), so no
JWT/JWKS machinery runs; everything else is real against moto.
"""
from __future__ import annotations

import os
import sys
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.store import AssetStore  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects-test"
BUCKET = "ingest-bucket"
PID = "p" + "c" * 32
OWNER = "owner-sub"
STRANGER = "stranger-sub"


class FakeVerifier:
    def __init__(self, sub=OWNER):
        self._sub = sub

    def verify(self, token, now):
        if not token or token == "bad":
            raise ValueError("invalid token")
        return {"sub": self._sub}


def _vid():
    return f"video:{uuid.uuid4()}"


@pytest.fixture
def ctx(monkeypatch):
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
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "projectId": PID, "status": "active"})
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"MEMBER#{OWNER}", "role": "owner"})
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        store = AssetStore(t)
        service = AssetService(
            store=store,
            verifier=FakeVerifier(),
            s3_client=s3,
            ingest_bucket=BUCKET,
        )
        yield service, store, s3


def _event(command, args, token="good", sub=None):
    ev = {"userToken": token, "projectId": PID, "command": command, "args": args, "caller": "proxy"}
    return ev


# ------------------------------------------------------------------ prologue
def test_bad_token_is_401(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("resolve_assets", {"assetIds": []}, token="bad"))
    assert reply == {"ok": False, "status": 401, "error": {"code": "unauthenticated", "message": "Sign in again"}}


def test_non_member_is_uniform_404(ctx):
    service, store, s3 = ctx
    stranger_service = AssetService(
        store=store, verifier=FakeVerifier(sub=STRANGER), s3_client=s3, ingest_bucket=BUCKET
    )
    reply = stranger_service.handle(_event("resolve_assets", {"assetIds": []}))
    assert reply["ok"] is False
    assert reply["status"] == 404
    assert reply["error"]["code"] == "project_not_found"


def test_unknown_project_is_404(ctx):
    service, _, _ = ctx
    ev = _event("resolve_assets", {"assetIds": []})
    ev["projectId"] = "not-a-project-id"
    reply = service.handle(ev)
    assert reply["status"] == 404 and reply["error"]["code"] == "project_not_found"


# ------------------------------------------------------------------ resolve_assets
def test_resolve_assets_contract_shape(ctx):
    service, store, _ = ctx
    a = _seed_asset(store, status="indexed", display="one.mp4")
    b = _seed_asset(store, status="removed", display="two.mp4")
    reply = service.handle(_event("resolve_assets", {"assetIds": [a, b, _vid(), "garbage"]}))
    assert reply["ok"] is True
    assets = reply["value"]["assets"]
    assert assets[a] == {"kind": "video", "displayName": "one.mp4", "status": "indexed"}
    # removed status is returned verbatim (eligibility is a downstream decision).
    assert assets[b]["status"] == "removed"
    # absent + malformed ids omitted.
    assert set(assets) == {a, b}


def test_resolve_assets_rejects_oversized_page(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("resolve_assets", {"assetIds": [_vid() for _ in range(101)]}))
    assert reply["status"] == 400 and reply["error"]["code"] == "too_many_ids"


# ------------------------------------------------------------------ register_upload
def test_register_upload_happy_path(ctx):
    service, store, s3 = ctx
    aid = _vid()
    key = f"projects/{PID}/{aid}/clip.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"x" * 2048, ContentType="video/mp4")
    reply = service.handle(
        _event("register_upload", {"assetId": aid, "fileName": "clip.mp4", "size": 2048, "contentType": "video/mp4"})
    )
    assert reply["ok"] is True
    assert reply["value"]["asset"]["assetId"] == aid
    assert store.get_asset(PID, aid)["status"] == "queued"


def test_register_upload_size_mismatch_is_409(ctx):
    service, _, s3 = ctx
    aid = _vid()
    key = f"projects/{PID}/{aid}/clip.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"x" * 10, ContentType="video/mp4")
    reply = service.handle(
        _event("register_upload", {"assetId": aid, "fileName": "clip.mp4", "size": 2048, "contentType": "video/mp4"})
    )
    assert reply["status"] == 409 and reply["error"]["code"] == "size_mismatch"


def test_register_upload_missing_object_is_409(ctx):
    service, _, _ = ctx
    aid = _vid()
    reply = service.handle(
        _event("register_upload", {"assetId": aid, "fileName": "clip.mp4", "size": 2048, "contentType": "video/mp4"})
    )
    assert reply["status"] == 409 and reply["error"]["code"] == "object_missing"


def test_register_upload_type_mismatch_is_400(ctx):
    service, _, s3 = ctx
    aid = _vid()  # a video: id but an image content type
    key = f"projects/{PID}/{aid}/clip.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"x" * 10, ContentType="image/png")
    reply = service.handle(
        _event("register_upload", {"assetId": aid, "fileName": "clip.mp4", "size": 10, "contentType": "image/png"})
    )
    assert reply["status"] == 400 and reply["error"]["code"] == "type_mismatch"


def test_register_upload_malformed_asset_id_is_404(ctx):
    service, _, _ = ctx
    reply = service.handle(
        _event("register_upload", {"assetId": "video:bad", "fileName": "c.mp4", "size": 1, "contentType": "video/mp4"})
    )
    assert reply["status"] == 404 and reply["error"]["code"] == "asset_not_found"


# ------------------------------------------------------------------ register_s3_location
def test_register_s3_location_registers_prefix_objects(ctx):
    service, store, s3 = ctx
    prefix = f"projects/{PID}/incoming/"
    s3.put_object(Bucket=BUCKET, Key=prefix + "a.mp4", Body=b"x" * 100, ContentType="video/mp4")
    s3.put_object(Bucket=BUCKET, Key=prefix + "b.png", Body=b"x" * 100, ContentType="image/png")
    s3.put_object(Bucket=BUCKET, Key=prefix + "notes.txt", Body=b"x" * 100, ContentType="text/plain")
    s3.put_object(Bucket=BUCKET, Key=prefix + "empty.bin", Body=b"", ContentType="application/octet-stream")
    reply = service.handle(_event("register_s3_location", {"location": f"s3://{BUCKET}/{prefix}"}))
    assert reply["ok"] is True
    value = reply["value"]
    # Every type registers: the .txt is a document. Only the zero-byte object is skipped.
    kinds = sorted(r["kind"] for r in value["registered"])
    assert kinds == ["document", "image", "video"]
    assert all("PK" not in r and r["status"] == "queued" for r in value["registered"])
    assert value["skipped"] == [{"key": prefix + "empty.bin", "reason": "empty"}]
    assert value["errors"] == []
    # A repeat registers nothing new: the SRC guard reports each key as already registered.
    again = service.handle(_event("register_s3_location", {"location": f"s3://{BUCKET}/{prefix}"}))["value"]
    assert again["registered"] == []
    assert sorted(s["reason"] for s in again["skipped"]) == ["already_registered"] * 3 + ["empty"]


def test_register_s3_location_outside_prefix_is_rejected(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("register_s3_location", {"location": f"s3://{BUCKET}/elsewhere/a.mp4"}))
    assert reply["status"] == 400 and reply["error"]["code"] == "outside_project_prefix"


def test_register_s3_location_bucket_not_allowed(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("register_s3_location", {"location": "s3://some-other-bucket/x/a.mp4"}))
    assert reply["status"] == 400 and reply["error"]["code"] == "bucket_not_allowed"


# ------------------------------------------------------------------ unknown command
def test_rebuild_retry_without_a_rebuild_machine_is_unavailable(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("rebuild_retry", {}))
    assert reply["status"] == 503 and reply["error"]["code"] == "rebuild_unavailable"


def test_unknown_command(ctx):
    service, _, _ = ctx
    reply = service.handle(_event("nope", {}))
    assert reply["status"] == 400 and reply["error"]["code"] == "unknown_command"


# ------------------------------------------------------------------ retry / readd / project_status
def _fail(store, aid, reason="shot-segmentation: boom"):
    from ingestion.pipeline_store import PipelineStore

    ps = PipelineStore(store.table)
    row = store.get_asset(PID, aid)
    ps.claim(PID, row)
    ps.transition(PID, store.get_asset(PID, aid), "failed", failure={"step": "shot-segmentation", "reason": reason})


def test_retry_command(ctx):
    service, store, _ = ctx
    aid = _seed_asset(store, status="queued", display="a.mp4")
    reply = service.handle(_event("retry", {"assetId": aid}))
    assert reply["status"] == 409 and reply["error"]["code"] == "not_failed"
    assert "queued" in reply["error"]["message"]
    _fail(store, aid)
    reply = service.handle(_event("retry", {"assetId": aid}))
    assert reply["ok"] and reply["value"]["asset"]["status"] == "queued"
    assert service.handle(_event("retry", {"assetId": aid}))["status"] == 409  # exactly one retry commits
    assert service.handle(_event("retry", {"assetId": "video:nope"}))["status"] == 404


def test_project_status_command(ctx):
    service, store, _ = ctx
    a = _seed_asset(store, status="queued", display="one.mp4")
    _seed_asset(store, status="queued", display="two.mp4")
    _fail(store, a, "shot-segmentation: video duration is missing or invalid")
    v = service.handle(_event("project_status", {"name": "ONE"}))["value"]
    assert v["counts"]["failed"] == 1 and v["counts"]["queued"] == 1
    assert v["collectionState"] == "absent"
    assert v["failed"] == [{"assetId": a, "displayName": "one.mp4",
                            "reason": "shot-segmentation: video duration is missing or invalid"}]
    assert v["failedNotListed"] == 0
    assert [m["displayName"] for m in v["matches"]] == ["one.mp4"]


def test_readd_command(ctx):
    service, store, s3 = ctx
    aid = _seed_asset(store, status="queued", display="clip.mp4")
    assert service.handle(_event("readd", {"assetId": aid}))["error"]["code"] == "not_removed"
    store.remove_asset(PID, aid, removed_by=OWNER)
    # The upload's bytes were deleted by remove → it cannot come back without a new upload.
    assert service.handle(_event("readd", {"assetId": aid}))["error"]["code"] == "source_gone"
    s3.put_object(Bucket=BUCKET, Key=f"projects/{PID}/{aid}/clip.mp4", Body=b"x")
    reply = service.handle(_event("readd", {"assetId": aid}))
    assert reply["ok"], reply
    assert reply["value"]["asset"]["status"] == "queued" and reply["value"]["asset"]["displayName"] == "clip.mp4"


def test_catalog_refusal_before_any_write(ctx):
    service, store, s3 = ctx
    from ingestion.handlers.asset_service import AssetService

    aid = _seed_asset(store, status="queued", display="a.mp4")
    _fail(store, aid)
    missing = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET,
                           catalog_tools=lambda: {"hybrid-search___index_content"})
    reply = missing.handle(_event("retry", {"assetId": aid}))
    assert reply["status"] == 503 and reply["error"]["code"] == "catalog_missing"
    assert "shot-segmentation___segment_shots" in reply["error"]["message"]
    assert store.get_asset(PID, aid)["status"] == "failed"

    def boom():
        raise TimeoutError("slow")
    unreadable = AssetService(store=store, verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET,
                              catalog_tools=boom)
    reply = unreadable.handle(_event("retry", {"assetId": aid}))
    assert reply["error"] == {"code": "catalog_unreadable", "message": "the M2C tool catalog is unreadable"}


# ------------------------------------------------------------------ helpers
def _seed_asset(store, *, status, display):
    aid = _vid()
    asset = store.register_asset(
        pid=PID,
        asset_id=aid,
        source="upload",
        source_uri=f"s3://{BUCKET}/projects/{PID}/{aid}/{display}",
        source_bucket=BUCKET,
        source_key=f"projects/{PID}/{aid}/{display}",
        source_size=1,
        content_type="video/mp4",
        file_name=display,
        added_by=OWNER,
        added_by_display=OWNER,
    )
    if status != "queued":
        store.table.update_item(
            Key=schema.asset_key(PID, aid),
            UpdateExpression="SET #s = :v",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":v": status},
        )
    return aid


# ------------------------------------------------------------------ search_images
def _put_image(store, *, kind="image", status="indexed", rendition=True, visibility=None, session=None, name="a.jpg"):
    aid = f"{kind}:{uuid.uuid4()}"
    item = {"PK": f"PROJECT#{PID}", "SK": f"ASSET#{aid}", "assetId": aid, "kind": kind, "status": status,
            "displayName": name}
    if rendition:
        item["rendition"] = {"key": f"projects/{PID}/{aid}/rendition.jpg", "width": 100, "height": 80, "bytes": 10}
    if visibility:
        item["visibility"] = visibility
        item["sessionId"] = session
    store.table.put_item(Item=item)
    return aid


def test_search_images_returns_each_images_rendition(ctx):
    service, store, _ = ctx
    a = _put_image(store, name="shot.jpg")
    reply = service.handle(_event("search_images", {"assetIds": [a], "sessionId": "s1"}))
    assert reply["ok"] is True
    assert reply["value"]["images"][a] == {"uri": f"s3://{BUCKET}/projects/{PID}/{a}/rendition.jpg",
                                           "displayName": "shot.jpg"}


def test_search_images_says_why_an_asset_cannot_be_a_query(ctx):
    service, store, _ = ctx
    video = _put_image(store, kind="video", name="clip.mp4")          # a video HAS a rendition (its poster)
    audio = _put_image(store, kind="audio", rendition=False, name="a.wav")
    pending = _put_image(store, rendition=False, status="analyzing")
    removed = _put_image(store, status="removed")
    other_chat = _put_image(store, visibility="session", session="other")
    mine = _put_image(store, visibility="session", session="s1")
    absent = f"image:{uuid.uuid4()}"
    ids = [video, audio, pending, removed, other_chat, mine, absent]
    images = service.handle(_event("search_images", {"assetIds": ids[:5], "sessionId": "s1"}))["value"]["images"]
    images.update(service.handle(_event("search_images", {"assetIds": ids[5:], "sessionId": "s1"}))["value"]["images"])
    assert images[video] == {"reason": "not_image", "kind": "video", "displayName": "clip.mp4"}
    assert images[audio]["reason"] == "not_image" and images[audio]["kind"] == "audio"
    assert images[pending] == {"reason": "not_ready", "displayName": "a.jpg"}
    assert images[removed] == {"reason": "not_found"}
    assert images[other_chat] == {"reason": "not_found"}           # another chat's "this chat only" upload
    assert "uri" in images[mine]                                    # this chat's own upload is fine
    assert images[absent] == {"reason": "not_found"}


def test_search_images_validates_its_arguments(ctx):
    service, store, _ = ctx
    a = _put_image(store)
    for args in ({"assetIds": [], "sessionId": "s1"}, {"assetIds": [a] * 6, "sessionId": "s1"},
                 {"assetIds": [a]}, {"assetIds": a, "sessionId": "s1"}):
        reply = service.handle(_event("search_images", args))
        assert reply["status"] == 400 and reply["error"]["code"] == "invalid_args", args


def _png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


def _put_unrendered(store, s3, *, status="stored", session="s1", body=None):
    aid = f"image:{uuid.uuid4()}"
    key = f"projects/{PID}/{aid}/upload.png"
    s3.put_object(Bucket=BUCKET, Key=key, Body=body if body is not None else _png_bytes())
    store.table.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"ASSET#{aid}", "assetId": aid, "kind": "image",
                               "status": status, "displayName": "upload.png", "visibility": "session",
                               "sessionId": session, "sourceUri": f"s3://{BUCKET}/{key}", "rowVersion": 3})
    return aid


def test_search_images_renders_a_chat_only_upload_on_demand(ctx):
    # A "this chat only" upload is never ingested, so it has no rendition until it is used as a query.
    service, store, s3 = ctx
    aid = _put_unrendered(store, s3)
    entry = service.handle(_event("search_images", {"assetIds": [aid], "sessionId": "s1"}))["value"]["images"][aid]
    want = f"projects/{PID}/{aid}/rendition.jpg"
    assert entry == {"uri": f"s3://{BUCKET}/{want}", "displayName": "upload.png"}
    body = s3.get_object(Bucket=BUCKET, Key=want)["Body"].read()
    assert body[:2] == b"\xff\xd8"                                   # a JPEG, made by the shared writer
    row = store.get_asset(PID, aid)
    assert row["rendition"]["key"] == want and int(row["rendition"]["width"]) == 40
    assert int(row["rowVersion"]) == 3 and row["status"] == "stored"   # an annotation: no status/version change
    # the second ask reuses it
    again = service.handle(_event("search_images", {"assetIds": [aid], "sessionId": "s1"}))["value"]["images"][aid]
    assert again == entry


def test_search_images_never_renders_for_another_chat_or_an_in_flight_run(ctx):
    service, store, s3 = ctx
    other = _put_unrendered(store, s3, session="other")
    running = _put_unrendered(store, s3, status="analyzing", session="s1")
    images = service.handle(_event("search_images", {"assetIds": [other, running], "sessionId": "s1"}))["value"]["images"]
    assert images[other] == {"reason": "not_found"}
    assert images[running]["reason"] == "not_ready"
    for aid in (other, running):
        assert "rendition" not in store.get_asset(PID, aid)


def test_search_images_reports_an_undecodable_image(ctx):
    service, store, s3 = ctx
    aid = _put_unrendered(store, s3, body=b"not an image")
    entry = service.handle(_event("search_images", {"assetIds": [aid], "sessionId": "s1"}))["value"]["images"][aid]
    assert entry == {"reason": "unreadable", "displayName": "upload.png"}
    assert "rendition" not in store.get_asset(PID, aid)

