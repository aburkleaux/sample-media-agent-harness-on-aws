"""Asset lineage — the session output registrar and remove semantics (asset-lineage design §3, D4).

``ingestion.handlers.generated_media`` and the Asset_Service ``remove`` over a moto projects table, a
versioned Ingest bucket, an M2C proxy bucket, and a sessions table:

- turn ``lineage`` → ``derivedFrom`` (a project key carries its id; a chat copy and any tracked location map
  through the SRC guard) and ``producer.tool``;
- the code tool's invented ``artifacts/<uuid>/output/`` folder: registered when this turn names it, wrote it
  in the window, and the uuid is no existing session; never by listing the bucket;
- identical bytes dedupe (R5);
- M2C job outputs by reference (``source = m2c``) with role by type, the job-label folder, ``derivedFrom`` the
  source asset, and a ``JOB#`` record; the durable copy of an M2C output is not registered again;
- ``remove``: ``m2c`` → record only; ``session`` → record and the ``output/`` bytes.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import generated as gen  # noqa: E402
from ingestion.handlers.asset_service import AssetService  # noqa: E402
from ingestion.handlers.generated_media import Deps, GeneratedMedia, handler, work_from_turn  # noqa: E402
from ingestion.limits import DEFAULT_UPLOAD_LIMITS  # noqa: E402
from ingestion.store import AssetStore  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
BUCKET = "ingest"
M2C = "m2c-proxy"
PID = "p" + "d" * 32
ACTOR = "owner-sub"
SID = "11111111-2222-4333-8444-555555555555"
OTHER_SID = "99999999-2222-4333-8444-555555555555"
INVENTED = "0f0f0f0f-1234-4abc-8def-0123456789ab"
DAY = "2026-10-07"


class FakeVerifier:
    def verify(self, token, now):
        return {"sub": ACTOR}


@pytest.fixture
def world(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("INGESTION_CONFIG", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName="projects", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={**schema.meta_key(PID), "projectId": PID, "status": "active"})
        t.put_item(Item={**schema.member_key(PID, ACTOR), "role": "owner"})
        jobs = ddb.create_table(TableName="jobs", BillingMode="PAY_PER_REQUEST",
                                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        sessions = ddb.create_table(TableName="sessions", BillingMode="PAY_PER_REQUEST",
                                    KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
                                    AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}])
        sessions.put_item(Item={"sessionId": SID, "title": "Promo", "createdAt": f"{DAY}T10:00:00Z"})
        sessions.put_item(Item={"sessionId": OTHER_SID, "title": "Someone else"})
        s3 = boto3.client("s3", region_name=REGION)
        for b in (BUCKET, M2C):
            s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        svc = GeneratedMedia(Deps(table=t, s3=s3, bucket=BUCKET, limits=dict(DEFAULT_UPLOAD_LIMITS),
                                  job_table=jobs, sessions_table=sessions, m2c_bucket=M2C))
        yield svc, t, s3, jobs


def _put(s3, key, body=b"bytes", ct="video/mp4", bucket=BUCKET):
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ct, ChecksumAlgorithm="SHA256")


def _turn(**kw):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {"kind": "turn", "sessionId": SID, "turnId": "t1", "projectId": PID, "actorSub": ACTOR,
            "startedAt": now - 60_000, "endedAt": now + 1_000, "externalRefs": [], **kw}


def _rows(t):
    items = t.query(KeyConditionExpression=Key("PK").eq(schema.project_pk(PID))
                    & Key("SK").begins_with("ASSET#"))["Items"]
    return {i["displayName"]: i for i in items}


def _upload(t, s3, name="clip.mp4"):
    aid = f"video:{uuid.uuid4()}"
    key = f"projects/{PID}/{aid}/{name}"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"source-" + name.encode(), ContentType="video/mp4")
    AssetStore(t).register_asset(pid=PID, asset_id=aid, source="upload", source_uri=f"s3://{BUCKET}/{key}",
                                 source_bucket=BUCKET, source_key=key, source_size=10, content_type="video/mp4",
                                 file_name=name, added_by=ACTOR, added_by_display=ACTOR)
    return aid, key


# ------------------------------------------------------------------ turn lineage
def test_turn_lineage_becomes_derived_from(world):
    svc, t, s3, _ = world
    up_a, key_a = _upload(t, s3, "a.mp4")
    up_b, _ = _upload(t, s3, "b.mp4")
    copy_key = f"artifacts/{SID}/input/1791-b.mp4"
    AssetStore(t).add_chat_copy(PID, up_b, copy_key, bucket=BUCKET)
    out_key = f"artifacts/{SID}/output/trim.mp4"
    _put(s3, out_key)
    msg = _turn(lineage=[{"output": f"s3://{BUCKET}/{out_key}", "tool": "crop_video",
                          "inputs": [f"s3://{BUCKET}/{key_a}", f"https://cdn.example/{copy_key}",
                                     f"s3://{BUCKET}/artifacts/{SID}/input/untracked.mp4"]}])
    assert handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m", "body": json.dumps(msg)}]},
                   service=svc) == {"batchItemFailures": []}
    row = _rows(t)["trim.mp4"]
    assert row["derivedFrom"] == [up_a, up_b]
    assert row["producer"] == {"sessionId": SID, "turnId": "t1", "tool": "crop_video"}
    assert row["source"] == "session" and row["folder"] == f"Chats/Promo · {DAY}"
    assert row["checksum"]["algo"] == "SHA256" and row["checksum"]["size"] == 5


def test_lineage_output_that_is_an_earlier_output_of_the_turn(world):
    svc, t, s3, _ = world
    first, second = f"artifacts/{SID}/output/a_trim.mp4", f"artifacts/{SID}/output/b_crop.mp4"
    _put(s3, first, b"one")
    _put(s3, second, b"two")
    svc.run(work_from_turn(_turn(lineage=[{"output": f"s3://{BUCKET}/{second}", "inputs": [f"s3://{BUCKET}/{first}"]}])))
    rows = _rows(t)
    assert rows["b_crop.mp4"]["derivedFrom"] == [rows["a_trim.mp4"]["assetId"]]


# ------------------------------------------------------------------ invented folders (R3.4)
def test_invented_uuid_folder_named_by_the_turn_is_this_sessions_output(world):
    svc, t, s3, _ = world
    key = f"artifacts/{INVENTED}/output/wolf_clip.mp4"
    _put(s3, key)
    out = svc.run(work_from_turn(_turn(lineage=[{"output": f"s3://{BUCKET}/{key}", "inputs": []}])))
    assert len(out["registered"]) == 1
    row = _rows(t)["wolf_clip.mp4"]
    assert row["sessionId"] == SID and row["sourceUri"] == f"s3://{BUCKET}/{key}"


def test_invented_folder_named_by_a_render_manifest(world):
    svc, t, s3, _ = world
    key = f"artifacts/{INVENTED}/output/frame.png"
    _put(s3, key, ct="image/png")
    _put(s3, f"artifacts/{SID}/render/r1.json", json.dumps({"src": f"https://cdn/{key}"}).encode(), "application/json")
    assert len(svc.run(work_from_turn(_turn()))["registered"]) == 1
    assert _rows(t)["frame.png"]["role"] == "deliverable"


def test_another_sessions_folder_is_never_adopted(world):
    svc, t, s3, _ = world
    key = f"artifacts/{OTHER_SID}/output/theirs.mp4"
    _put(s3, key)
    out = svc.run(work_from_turn(_turn(lineage=[{"output": f"s3://{BUCKET}/{key}"}])))
    assert out["registered"] == [] and {"key": key, "reason": "other_session"} in out["skipped"]


def test_invented_folder_outside_the_turn_window_is_refused(world):
    svc, t, s3, _ = world
    key = f"artifacts/{INVENTED}/output/old.mp4"
    _put(s3, key)
    out = svc.run(work_from_turn(_turn(startedAt=1_000_000, endedAt=2_000_000,
                                       lineage=[{"output": f"s3://{BUCKET}/{key}"}])))
    assert out["registered"] == [] and {"key": key, "reason": "outside_turn_window"} in out["skipped"]


def test_an_unnamed_invented_folder_is_not_found_by_listing(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{INVENTED}/output/stray.mp4")
    assert svc.run(work_from_turn(_turn()))["registered"] == []


def test_input_and_render_stay_excluded_even_when_named(world):
    svc, t, s3, _ = world
    keys = [f"artifacts/{SID}/input/x.mp4", f"artifacts/{SID}/render/r.json", f"artifacts/{INVENTED}/input/y.mp4"]
    for k in keys:
        _put(s3, k)
    out = svc.run(work_from_turn(_turn(lineage=[{"output": f"s3://{BUCKET}/{k}"} for k in keys])))
    assert out["registered"] == [] and _rows(t) == {}


# ------------------------------------------------------------------ dedupe (R5)
def test_identical_session_outputs_dedupe(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/a.mp4", b"same")
    _put(s3, f"artifacts/{SID}/output/b.mp4", b"same")
    out = svc.run(work_from_turn(_turn()))
    assert len(out["registered"]) == 1 and out["deduped"] == out["registered"]
    assert len(_rows(t)) == 1


# ------------------------------------------------------------------ M2C job outputs by reference (R4, R6.2)
def _ddb(item):
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in item.items()}


def _job_record(new, old_status="RUNNING"):
    old = {k: v for k, v in new.items() if k != "resultKey"}
    old["status"] = old_status
    return {"eventSource": "aws:dynamodb", "eventName": "MODIFY",
            "dynamodb": {"SequenceNumber": "1", "NewImage": _ddb(new), "OldImage": _ddb(old)}}


def test_m2c_plan_outputs_are_registered_in_place(world):
    svc, t, s3, jobs = world
    up, up_key = _upload(t, s3)
    exec_dir = "micro-workflow/shot-segmentation/0a1b2c3d-1111-4222-8333-444455556666"
    _put(s3, f"{exec_dir}/shots.json", b'{"shots": []}', "application/json", bucket=M2C)
    _put(s3, f"{exec_dir}/vocals.wav", b"vocals", "audio/wav", bucket=M2C)
    _put(s3, f"{exec_dir}/frames/f001.jpg", b"frame", "image/jpeg", bucket=M2C)
    durable = f"artifacts/{SID}/output/plan-1/sg/shots.json"  # the resolver's durable copy of shots.json
    _put(s3, durable, b'{"shots": []}', "application/json")
    _put(s3, f"artifacts/{SID}/output/reel.mp4", b"reel")
    s3.put_object(Bucket=BUCKET, Key="plans/x/result.json", Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": "sg", "status": "completed", "tool": "m2c-gateway___start_shot_segmentation",
         "handle": {"output_prefix": f"s3://{M2C}/{exec_dir}/"},
         "artifacts": [
             {"url": f"https://cdn/{durable}", "source_ref": f"s3://{M2C}/{exec_dir}/shots.json", "role": "analysis"},
             {"url": f"s3://{M2C}/{exec_dir}/vocals.wav", "role": "output"},
             {"url": f"s3://{M2C}/{exec_dir}/frames/f001.jpg", "role": "output"},
             {"url": f"s3://{BUCKET}/{up_key}", "role": "source"}]},
        {"node_id": "edit", "status": "completed",
         "artifacts": [{"url": f"https://cdn/artifacts/{SID}/output/reel.mp4", "role": "output"}]},
    ]}).encode())
    jobs.put_item(Item={"jobId": "plan-1", "producingProjectId": PID})
    rec = _job_record({"jobId": "plan-1", "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
                       "resultKey": "plans/x/result.json", "producingProjectId": PID, "turnId": "t9"})
    assert handler({"Records": [rec]}, service=svc) == {"batchItemFailures": []}

    rows = _rows(t)
    # Every M2C output is named by the workflow, so its Display_Name names the source and the job instead
    # (the raw file name stays on `sourceUri`). The durable copy of shots.json is skipped.
    assert set(rows) == {"clip.mp4", "clip — Shot Segmentation (shots).json",
                         "clip — Shot Segmentation (vocals).wav", "clip — Shot Segmentation (f001).jpg",
                         "reel.mp4"}
    shots = rows["clip — Shot Segmentation (shots).json"]
    vocals, frame = rows["clip — Shot Segmentation (vocals).wav"], rows["clip — Shot Segmentation (f001).jpg"]
    assert shots["source"] == "m2c" and shots["sourceUri"] == f"s3://{M2C}/{exec_dir}/shots.json"
    assert (shots["kind"], shots["role"], shots["status"]) == ("data", "analysis", "stored")
    assert (vocals["role"], vocals["status"]) == ("deliverable", "queued")
    assert (frame["role"], frame["status"]) == ("intermediate", "stored")
    folder = f"Chats/Promo · {DAY}/Shot Segmentation"
    for r in (shots, vocals, frame):
        assert r["folder"] == folder and r["derivedFrom"] == [up] and r["jobId"] == "plan-1#sg"
        assert r["producer"] == {"sessionId": SID, "turnId": "t9", "tool": "m2c-gateway___start_shot_segmentation",
                                 "jobId": "plan-1#sg"}
    assert rows["reel.mp4"]["source"] == "session" and rows["reel.mp4"]["folder"] == f"Chats/Promo · {DAY}"
    job = AssetStore(t).get_job(PID, "plan-1#sg")
    assert job["label"] == "Shot Segmentation" and job["sourceAssetId"] == up and job["role"] == "analysis"
    assert job["outputPrefix"] == f"s3://{M2C}/{exec_dir}/" and job["sessionId"] == SID
    assert sorted(job["outputs"]) == sorted(r["assetId"] for r in (shots, vocals, frame))
    # Nothing was copied anywhere.
    assert [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix="projects/")["Contents"]] == [up_key]
    # A replayed record registers nothing new and keeps one JOB# record.
    assert handler({"Records": [_job_record({**rec_new(rec), "status": "SUCCEEDED"}, "RUNNING")]},
                   service=svc) == {"batchItemFailures": []}
    assert len(_rows(t)) == 5 and AssetStore(t).get_job(PID, "plan-1#sg")["outputs"] == job["outputs"]


def rec_new(rec):
    from boto3.dynamodb.types import TypeDeserializer

    d = TypeDeserializer()
    return {k: d.deserialize(v) for k, v in rec["dynamodb"]["NewImage"].items()}


def test_lone_m2c_job_uses_the_rows_source_media(world):
    svc, t, s3, _ = world
    up, up_key = _upload(t, s3)
    job = "arn:aws:states:us-west-2:1:execution:diarize:0a1b2c3d-1111-4222-8333-444455556666"
    _put(s3, "mw/diar/0a1b/diarization.json", b"{}", "application/json", bucket=M2C)
    s3.put_object(Bucket=BUCKET, Key="jobs/j/result.json", Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": job, "status": "SUCCEEDED",
         "artifacts": [{"url": f"s3://{M2C}/mw/diar/0a1b/diarization.json", "role": "analysis"}]}]}).encode())
    rec = _job_record({"jobId": job, "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
                       "resultKey": "jobs/j/result.json", "producingProjectId": PID,
                       "trioId": "m2c___speaker_diarization", "sourceMedia": [f"s3://{BUCKET}/{up_key}"]})
    handler({"Records": [rec]}, service=svc)
    row = _rows(t)["clip — Speaker Diarization (diarization).json"]
    assert row["jobId"] == job and row["derivedFrom"] == [up]
    assert row["folder"] == f"Chats/Promo · {DAY}/Speaker Diarization"
    assert AssetStore(t).get_job(PID, job)["outputPrefix"] == f"s3://{M2C}/mw/diar/0a1b/"


def test_without_an_m2c_bucket_m2c_refs_are_ignored(world):
    svc, t, s3, _ = world
    svc.deps.m2c_bucket = ""
    _put(s3, "x/shots.json", b"{}", "application/json", bucket=M2C)
    s3.put_object(Bucket=BUCKET, Key="jobs/j/result.json", Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": "j", "status": "SUCCEEDED", "artifacts": [{"url": f"s3://{M2C}/x/shots.json", "role": "analysis"}]}
    ]}).encode())
    handler({"Records": [_job_record({"jobId": "j", "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
                                      "resultKey": "jobs/j/result.json", "producingProjectId": PID})]}, service=svc)
    assert _rows(t) == {}


def _m2c_job(s3, jobs, node_id, tool, keys, source_key, *, result="jobs/r.json", job_id=None, body=b"out"):
    """A terminal job record whose node wrote ``keys`` in the M2C bucket from ``source_key``."""
    for k in keys:
        _put(s3, k, body, "video/mp4", bucket=M2C)
    s3.put_object(Bucket=BUCKET, Key=result, Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": node_id, "status": "SUCCEEDED", "tool": tool,
         "artifacts": [*({"url": f"s3://{M2C}/{k}", "role": "output"} for k in keys),
                       {"url": f"s3://{BUCKET}/{source_key}", "role": "source"}]}]}).encode())
    return _job_record({"jobId": job_id or node_id, "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
                        "resultKey": result, "producingProjectId": PID, "turnId": "t9"})


def test_a_generic_m2c_output_name_becomes_the_source_plus_the_job(world):
    """Verified live: an M2C trim of meridian_left_trim.mp4 was registered as `transcoded.mp4`."""
    svc, t, s3, jobs = world
    _, src = _upload(t, s3, "meridian_left_trim.mp4")
    rec = _m2c_job(s3, jobs, "trim", "media-processing___process_media", ["mw/pm/1/transcoded.mp4"], src)
    assert handler({"Records": [rec]}, service=svc) == {"batchItemFailures": []}
    row = _rows(t)["meridian_left_trim — Process Media (transcoded).mp4"]
    assert row["sourceUri"] == f"s3://{M2C}/mw/pm/1/transcoded.mp4"  # the raw name is still on the row


def test_same_generic_output_of_two_sources_does_not_need_the_n_suffix(world):
    svc, t, s3, jobs = world
    _, a = _upload(t, s3, "left.mp4")
    _, b = _upload(t, s3, "right.mp4")
    tool = "media-processing___process_media"
    handler({"Records": [_m2c_job(s3, jobs, "n1", tool, ["mw/pm/1/transcoded.mp4"], a, job_id="j1")]}, service=svc)
    handler({"Records": [_m2c_job(s3, jobs, "n2", tool, ["mw/pm/2/transcoded.mp4"], b, job_id="j2",
                                  result="jobs/r2.json", body=b"other")]}, service=svc)
    assert {"left — Process Media (transcoded).mp4", "right — Process Media (transcoded).mp4"} <= set(_rows(t))


def test_two_identical_outputs_of_one_source_still_get_the_allocators_suffix(world):
    """Same source, same job, same raw name, two objects: the per-Project ` (n)` allocation still applies."""
    svc, t, s3, jobs = world
    _, src = _upload(t, s3, "left.mp4")
    tool = "media-processing___process_media"
    handler({"Records": [_m2c_job(s3, jobs, "n1", tool, ["mw/pm/1/transcoded.mp4"], src, job_id="j1")]}, service=svc)
    handler({"Records": [_m2c_job(s3, jobs, "n2", tool, ["mw/pm/2/transcoded.mp4"], src, job_id="j2",
                                  result="jobs/r2.json", body=b"other")]}, service=svc)
    assert {"left — Process Media (transcoded).mp4",
            "left — Process Media (transcoded).mp4 (2)"} <= set(_rows(t))


def test_an_m2c_output_with_no_source_asset_keeps_its_raw_name(world):
    svc, t, s3, jobs = world
    rec = _m2c_job(s3, jobs, "trim", None, ["mw/pm/1/transcoded.mp4"], "artifacts/nowhere/untracked.mp4")
    handler({"Records": [rec]}, service=svc)
    assert "transcoded.mp4" in _rows(t)


def test_a_generic_session_output_is_named_from_its_lineage(world):
    svc, t, s3, _ = world
    _, src = _upload(t, s3, "meridian_left_trim.mp4")
    generic, specific = f"artifacts/{SID}/output/output.mp4", f"artifacts/{SID}/output/wolf_9x16.mp4"
    _put(s3, generic, b"one")
    _put(s3, specific, b"two")
    svc.run(work_from_turn(_turn(lineage=[
        {"output": f"s3://{BUCKET}/{generic}", "tool": "crop_video", "inputs": [f"s3://{BUCKET}/{src}"]},
        {"output": f"s3://{BUCKET}/{specific}", "tool": "crop_video", "inputs": [f"s3://{BUCKET}/{src}"]}])))
    rows = _rows(t)
    assert "meridian_left_trim — Crop Video (output).mp4" in rows
    assert "wolf_9x16.mp4" in rows  # a name the agent chose itself is left alone


def test_a_generic_session_output_with_no_lineage_keeps_its_raw_name(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/output.mp4")
    svc.run(work_from_turn(_turn()))
    assert "output.mp4" in _rows(t)


def test_job_outputs_split_and_labels():
    body = json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": "n", "status": "failed", "artifacts": [{"url": f"s3://{M2C}/x/a.wav", "role": "output"}]},
        {"node_id": "m", "status": "completed",
         "artifacts": [{"url": f"s3://{M2C}/artifacts/{SID}/output/own.mp4", "role": "output"}]}]})
    keys, nodes = gen.job_outputs(body, SID, M2C)
    assert nodes == []  # a failed node registers nothing; a harness key is never an M2C output
    assert gen.job_label("node|mw___start_face_detection_job") == "Face Detection"
    assert gen.m2c_role("video", "x/segments/s1.mp4") == "intermediate"
    assert gen.m2c_role("video", "x/clip.mp4") == "deliverable"


# ------------------------------------------------------------------ remove (D4)
def _remove(t, s3, aid):
    svc = AssetService(store=AssetStore(t), verifier=FakeVerifier(), s3_client=s3, ingest_bucket=BUCKET)
    reply = svc.handle({"userToken": "x", "projectId": PID, "command": "remove", "args": {"assetId": aid}})
    assert reply["ok"] is True, reply
    return reply["value"]


def _exists(s3, bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def test_remove_session_asset_deletes_record_and_bytes(world):
    svc, t, s3, _ = world
    key = f"artifacts/{SID}/output/cut.mp4"
    _put(s3, key)
    svc.run(work_from_turn(_turn()))
    v = _remove(t, s3, _rows(t)["cut.mp4"]["assetId"])
    assert v["deleted"] == [key] and not _exists(s3, BUCKET, key)


def test_remove_external_session_asset_keeps_the_shared_bytes(world):
    svc, t, s3, _ = world
    ext = "artifacts/external/" + "ab" * 12 + ".mp4"
    _put(s3, ext)
    svc.run(work_from_turn(_turn(externalRefs=[ext])))
    (row,) = _rows(t).values()
    assert _remove(t, s3, row["assetId"])["deleted"] == [] and _exists(s3, BUCKET, ext)


def test_remove_m2c_asset_keeps_m2c_bytes(world):
    svc, t, s3, _ = world
    _put(s3, "x/vocals.wav", b"v", "audio/wav", bucket=M2C)
    s3.put_object(Bucket=BUCKET, Key="jobs/j/result.json", Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"node_id": "j", "status": "SUCCEEDED", "artifacts": [{"url": f"s3://{M2C}/x/vocals.wav", "role": "output"}]}
    ]}).encode())
    handler({"Records": [_job_record({"jobId": "j", "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
                                      "resultKey": "jobs/j/result.json", "producingProjectId": PID})]}, service=svc)
    row = _rows(t)["vocals.wav"]
    v = _remove(t, s3, row["assetId"])
    assert v["changed"] is True and v["asset"]["status"] == "removed" and v["deleted"] == []
    assert _exists(s3, M2C, "x/vocals.wav")
