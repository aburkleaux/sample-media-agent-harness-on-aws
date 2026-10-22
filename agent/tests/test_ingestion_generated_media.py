"""Generated_Media registration (ingestion Req 9, design §16): selection, idempotency, cap, access, async."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import generated as gen  # noqa: E402
from ingestion import names  # noqa: E402
from ingestion.handlers.generated_media import (  # noqa: E402
    Deps, GeneratedMedia, handler, work_from_job_image, work_from_turn,
)
from ingestion.limits import DEFAULT_UPLOAD_LIMITS  # noqa: E402
from projects import schema  # noqa: E402

REGION = "us-west-2"
TABLE = "projects"
JOBS = "jobs"
BUCKET = "ingest"
PID = "p" + "a" * 32
ACTOR = "user-1"
SID = "11111111-2222-4333-8444-555555555555"
UUID_DIR = "09c9c2e8-3781-51bf-b187-41190afa2c7d"
EXT_KEY = "artifacts/external/" + "ab" * 12 + ".mp4"


# ------------------------------------------------------------------ pure selection
def test_session_source_rules():
    p = f"artifacts/{SID}/"
    assert gen.is_session_source(p + "output/meridian_seg01_crop.mp4", SID)
    assert gen.is_session_source(p + "output/meridian_reel/a.mp4", SID)
    assert gen.is_session_source(p + "voice.wav", SID)
    assert not gen.is_session_source(p + "input/1791-clip.mp4", SID)  # a chat copy
    assert not gen.is_session_source(p + "render/abc.json", SID)
    # The session folder's own state file is not a turn output (registered as an analysis asset on staging,
    # 2026-10-08); a data file counts only under output/.
    assert not gen.is_session_source(p + "transcript.json", SID)
    assert not gen.is_session_source(p + "notes.csv", SID)
    assert gen.is_session_source(p + "output/report.json", SID)
    assert not gen.is_session_source(p + f"output/{UUID_DIR}/audio_16k_mono.wav", SID)  # M2C working dir
    assert gen.is_session_source(p + f"output/{UUID_DIR}/node_a/reel.mp4", SID)  # a plan node's deliverable
    assert not gen.is_session_source("artifacts/other-session-id/output/x.mp4", SID)
    assert not gen.is_session_source(f"projects/{PID}/x.mp4", SID)
    assert not gen.is_session_id("external") and not gen.is_session_id("durable") and gen.is_session_id(SID)


def test_external_refs_from_urls_and_presigned():
    h = "0123456789abcdef01234567"
    text = (f"see https://cdn.example/artifacts/external/{h}.mp4 and "
            f"https://b.s3.amazonaws.com/artifacts/external/{h}.png?X-Amz-Signature=zz and "
            f"artifacts/external/{h}.mp4 again, not artifacts/external/{h}ff.mp4")
    assert gen.external_refs_in(text) == [f"artifacts/external/{h}.mp4", f"artifacts/external/{h}.png"]


def test_plan_result_ignores_source_role():
    body = json.dumps({"schema": "plan-result/v1", "nodes": [{"artifacts": [
        {"url": f"https://cdn/artifacts/{SID}/output/cut.mp4", "role": "output"},
        {"url": f"s3://b/artifacts/{SID}/earlier.mp4", "role": "source"},
        {"url": f"https://cdn/artifacts/{SID}/output/{UUID_DIR}/SPEAKER_00.wav", "role": "output"},
        {"url": f"https://cdn/{EXT_KEY}", "role": "output"},
    ]}]})
    assert gen.refs_in_result(body, SID) == [f"artifacts/{SID}/output/cut.mp4", EXT_KEY]


def test_rendered_refs_reach_folders_the_agent_named():
    other = "99999999-2222-4333-8444-555555555555"
    manifest = json.dumps({"images": [{"src": "https://cdn/artifacts/wolf-crop-888104cf/output/wolf_9x16.jpg"}],
                           "candidate_keys": [
                               "artifacts/wolf-crop-888104cf/output/wolf_9x16.jpg",
                               f"artifacts/{SID}/output/cut.mp4",
                               f"artifacts/{SID}/input/1791-upload.mp4",  # a chat attachment
                               f"artifacts/{other}/output/theirs.mp4",  # another chat's folder
                               "artifacts/durable/0a47.wav",
                               "artifacts/arn:aws:states:us-west-2:1:execution:wf:abc/out.mp4",
                               f"artifacts/{SID}/output/{UUID_DIR}/SPEAKER_00.wav",
                               EXT_KEY]})
    assert gen.rendered_refs_in(manifest, SID) == sorted([
        EXT_KEY, f"artifacts/{SID}/output/cut.mp4", "artifacts/wolf-crop-888104cf/output/wolf_9x16.jpg"])


def test_asset_id_is_deterministic_and_well_formed():
    a = gen.asset_id_for("video", PID, "k", "v1")
    assert schema.is_asset_id(a) and a == gen.asset_id_for("video", PID, "k", "v1")
    assert a != gen.asset_id_for("video", PID, "k", "v2")
    assert a != gen.asset_id_for("video", "p" + "b" * 32, "k", "v1")


def test_external_display_name_is_stable():
    c = gen.Candidate(key=EXT_KEY, size=1, content_type="video/mp4",
                      last_modified=datetime(2026, 10, 1, 13, 4, 5, tzinfo=timezone.utc),
                      version="v", versioned=True, kind="video")
    assert gen.display_name_for(c) == "generated-20261001-130405-ababab.mp4"
    s = gen.Candidate(key=f"artifacts/{SID}/output/cut.mp4", size=1, content_type="video/mp4",
                      last_modified=c.last_modified, version="v", versioned=True, kind="video")
    assert gen.display_name_for(s) == "cut.mp4"


# ------------------------------------------------------------------ output names (meaningful, not `transcoded.mp4`)
def _cand(key, ct="video/mp4", kind="video"):
    return gen.Candidate(key=key, size=1, content_type=ct,
                         last_modified=datetime(2026, 10, 1, 13, 4, 5, tzinfo=timezone.utc),
                         version="v", versioned=True, kind=kind, bucket="b")


@pytest.mark.parametrize("name, generic", [
    ("transcoded.mp4", True), ("output.mp4", True), ("result.json", True), ("proxy.mp4", True),
    ("audio_16k_mono.wav", True), ("segment_0000.mp4", True), ("part-2.mp4", True), ("0001.png", True),
    ("TRANSCODED.MP4", True), ("SPEAKER_00.wav", True),
    ("meridian_left_trim.mp4", False), ("shots.json", False), ("wolf_9x16_center.jpg", False),
    ("generated-20261001-130405-ababab.mp4", False), ("", False),
])
def test_generic_output_names_are_the_ones_a_workflow_chose(name, generic):
    assert gen.is_generic_name(name) is generic


@pytest.mark.parametrize("raw, source, tool, expected", [
    # The live bug: an M2C trim of meridian_left_trim.mp4 was registered as `transcoded.mp4`.
    ("transcoded.mp4", "meridian_left_trim.mp4", "media-processing___process_media",
     "meridian_left_trim — Process Media (transcoded).mp4"),
    # A bare index keeps its index, so the N outputs of one job stay distinguishable.
    ("segment_0000.mp4", "promo.mp4", "m2c___start_shot_segmentation", "promo — Shot Segmentation (segment_0000).mp4"),
    ("segment_0001.mp4", "promo.mp4", "m2c___start_shot_segmentation", "promo — Shot Segmentation (segment_0001).mp4"),
    ("audio_16k_mono.wav", "promo.mp4", "m2c___speaker_diarization",
     "promo — Speaker Diarization (audio_16k_mono).wav"),
    # Fallbacks: one half missing keeps the other; with neither, the raw name is kept as it is.
    ("output.mp4", "promo.mp4", None, "promo (output).mp4"),
    ("output.mp4", None, "m2c___start_shot_segmentation", "Shot Segmentation (output).mp4"),
    ("output.mp4", None, None, "output.mp4"),
    ("output.mp4", "", "   ", "output.mp4"),
    # The source's own ` (n)` allocation suffix and extension are not carried into the qualifier.
    ("transcoded.mp4", "clip.mp4 (2)", None, "clip (transcoded).mp4"),
])
def test_output_display_name_qualifies_a_generic_name(raw, source, tool, expected):
    c = _cand(f"mw/exec/{raw}")
    assert gen.output_display_name(c, source, gen.output_label(tool)) == expected
    # The same inputs always derive the same name (a replayed message must not make a second asset name).
    assert gen.output_display_name(c, source, gen.output_label(tool)) == expected


def test_a_specific_session_name_is_kept_but_an_m2c_output_is_always_qualified():
    c = _cand(f"artifacts/{SID}/output/meridian_reel.mp4")
    label = gen.output_label("media-processing___process_media")
    assert gen.output_display_name(c, "promo.mp4", label) == "meridian_reel.mp4"
    # Every M2C output is named by the workflow, so the qualifier applies even to a specific-looking name.
    assert gen.output_display_name(c, "promo.mp4", label, always=True) == "promo — Process Media (meridian_reel).mp4"
    # ...and with nothing to qualify it with, it still keeps its own name.
    assert gen.output_display_name(c, None, None, always=True) == "meridian_reel.mp4"


def test_qualified_name_keeps_the_extension_within_the_display_name_cap():
    long_source = "m" * 400 + ".mp4"
    name = gen.qualify_name("transcoded.mp4", long_source, "Process Media")
    assert len(name) == names.DISPLAY_NAME_MAX  # the source gives way first, not the job or the stem
    assert name.endswith(" — Process Media (transcoded).mp4")
    # Even an absurd label cannot push the name over the cap, and the real extension survives.
    long_label = gen.qualify_name("transcoded.mp4", None, "L" * 400)
    assert len(long_label) <= names.DISPLAY_NAME_MAX and long_label.endswith(".mp4")
    # A name with no extension stays extension-free.
    assert gen.qualify_name("output", "promo.mp4", "Process Media") == "promo — Process Media (output)"


def test_candidate_filters():
    base = {"ContentLength": 10, "ContentType": "video/mp4", "VersionId": "v1",
            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    lim = DEFAULT_UPLOAD_LIMITS
    assert gen.candidate_from_head("a.mp4", base, lim)[0].version == "v1"
    # A PDF report is a deliverable document (staging session d6a560a8's compliance reports were skipped).
    assert gen.candidate_from_head("a.pdf", {**base, "ContentType": "application/pdf"}, lim)[0].kind == "document"
    assert gen.candidate_from_head("r.pdf", {**base, "ContentType": "binary/octet-stream"}, lim)[0].kind == "document"
    assert gen.candidate_from_head("a.zip", {**base, "ContentType": "application/zip"}, lim)[1] == "unsupported_type"
    # An HTML storyboard (and Markdown / Office) is a deliverable document too, kept but not indexed; scripts are not.
    assert gen.candidate_from_head("board.html", {**base, "ContentType": "text/html"}, lim)[0].kind == "document"
    assert gen.candidate_from_head("deck.pptx", {**base, "ContentType": ""}, lim)[0].kind == "document"
    assert gen.candidate_from_head("run.py", {**base, "ContentType": "text/x-python"}, lim)[1] == "unsupported_type"
    # An analysis file is a `data` asset now (asset-lineage design §2), by type or by extension.
    assert gen.candidate_from_head("a.json", {**base, "ContentType": "application/json"}, lim)[0].kind == "data"
    assert gen.candidate_from_head("subs.srt", {**base, "ContentType": ""}, lim)[0].kind == "data"
    assert gen.candidate_from_head("a.mp4", {**base, "ContentLength": 0}, lim)[0] is None
    assert gen.candidate_from_head("a.mp4", {**base, "Metadata": {"harness-origin": "project-asset"}},
                                   lim)[1] == "project_asset_copy"
    unversioned = gen.candidate_from_head("a.mp4", {**base, "VersionId": "null", "ETag": '"e1"'}, lim)[0]
    assert (unversioned.version, unversioned.versioned) == ("e1", False)


def test_turn_message_validation():
    ok = {"kind": "turn", "sessionId": SID, "turnId": "t1", "projectId": PID, "actorSub": ACTOR,
          "startedAt": 1000, "endedAt": 2000, "externalRefs": [EXT_KEY, "nonsense"]}
    w = work_from_turn(ok)
    assert w.window_ms == (1000 - 5000, 2000 + 5000) and w.keys == [EXT_KEY] and w.producer == "turn:t1"
    assert work_from_turn({**ok, "projectId": "nope"}) is None
    assert work_from_turn({**ok, "sessionId": "external"}) is None
    assert work_from_turn({**ok, "startedAt": 3000}) is None


# ------------------------------------------------------------------ moto
@pytest.fixture
def world(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        t = ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={**schema.meta_key(PID), "projectId": PID, "status": "active", "name": "P"})
        t.put_item(Item={**schema.member_key(PID, ACTOR), "role": "owner"})
        jobs = ddb.create_table(TableName=JOBS, BillingMode="PAY_PER_REQUEST",
                                KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
                                AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}])
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        svc = GeneratedMedia(Deps(table=t, s3=s3, bucket=BUCKET, limits=dict(DEFAULT_UPLOAD_LIMITS), job_table=jobs))
        yield svc, t, s3, jobs


def _put(s3, key, body=b"bytes", ct="video/mp4"):
    s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=ct)


def _turn_msg(**kw):
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {"kind": "turn", "sessionId": SID, "turnId": "t1", "projectId": PID, "actorSub": ACTOR,
            "startedAt": now - 60_000, "endedAt": now + 1_000, "externalRefs": [], **kw}


def _assets(t):
    items = t.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(schema.project_pk(PID))
                    & boto3.dynamodb.conditions.Key("SK").begins_with("ASSET#"))["Items"]
    return {i["displayName"]: i for i in items}


def test_turn_registers_deliverables_and_skips_intermediates(world):
    svc, t, s3, _ = world
    p = f"artifacts/{SID}/"
    _put(s3, p + "output/cut.mp4")
    _put(s3, p + "voice.wav", ct="audio/wav")
    _put(s3, p + f"output/{UUID_DIR}/SPEAKER_00.wav", ct="audio/wav")
    _put(s3, p + "input/1791-upload.mp4")
    _put(s3, p + "output/notes.json", ct="application/json")
    _put(s3, EXT_KEY, body=b"fal-video")
    # A render manifest in the window names a second external object the reply did not.
    ext2 = "artifacts/external/" + "cd" * 12 + ".png"
    _put(s3, ext2, ct="image/png")
    _put(s3, p + "render/r1.json", body=json.dumps({"items": [{"url": f"https://cdn/{ext2}"}]}).encode(),
         ct="application/json")

    out = handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m1",
                                "body": json.dumps(_turn_msg(externalRefs=[EXT_KEY]))}]}, service=svc)
    assert out == {"batchItemFailures": []}
    rows = _assets(t)
    names = sorted(rows)
    assert len(names) == 5 and {"cut.mp4", "voice.wav", "notes.json"} <= set(names)
    gen_names = [n for n in names if n.startswith("generated-")]
    assert [n[-11:] for n in gen_names] == ["-ababab.mp4", "-cdcdcd.png"]
    cut = rows["cut.mp4"]
    # Registered IN PLACE (asset-lineage R3.1): the source is the session object itself, nothing is copied.
    assert cut["source"] == "session" and cut["status"] == "queued" and cut["addedBy"] == ACTOR
    assert cut["sourceUri"] == f"s3://{BUCKET}/{p}output/cut.mp4"
    assert cut["role"] == "deliverable" and cut["visibility"] == "project" and cut["sessionId"] == SID
    assert cut["producer"] == {"sessionId": SID, "turnId": "t1"}
    assert not s3.list_objects_v2(Bucket=BUCKET, Prefix="projects/").get("Contents")
    notes = rows["notes.json"]
    assert notes["kind"] == "data" and notes["role"] == "analysis" and notes["status"] == "stored"
    meta = t.get_item(Key=schema.meta_key(PID))["Item"]
    assert meta["statusCounts"]["queued"] == 4 and meta["statusCounts"]["stored"] == 1
    assert meta["assetListPk"] == "SCHED"


def test_a_session_pdf_report_is_a_queued_deliverable(world):
    """Staging session d6a560a8: three compliance_report PDFs were skipped `unsupported_type`, so the
    moderation deliverable never reached the Project. A PDF is QUEUED like an upload: the document pipeline
    (page text) indexes it, so a report is searchable. It was registered `stored` for a day after the
    pipeline landed, which left every staging report unsearchable."""
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/nba_compliance_report.pdf", body=b"%PDF-1.7", ct="application/pdf")
    out = handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m1",
                                "body": json.dumps(_turn_msg())}]}, service=svc)
    assert out == {"batchItemFailures": []}
    row = _assets(t)["nba_compliance_report.pdf"]
    assert (row["kind"], row["role"], row["status"], row["source"]) == ("document", "deliverable", "queued", "session")
    meta = t.get_item(Key=schema.meta_key(PID))["Item"]
    assert meta["statusCounts"]["queued"] == 1


def test_a_session_html_storyboard_is_a_stored_deliverable(world):
    """Staging 2026-10-10..15: three `*_storyboard.html` outputs were skipped `unsupported_type` and never reached
    their Projects. An HTML deliverable is registered like a PDF, but STORED: nothing indexes HTML yet, so queuing
    it would only fail."""
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/ai_summit_storyboard.html", body=b"<html></html>", ct="text/html")
    out = handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m1",
                                "body": json.dumps(_turn_msg())}]}, service=svc)
    assert out == {"batchItemFailures": []}
    row = _assets(t)["ai_summit_storyboard.html"]
    assert (row["kind"], row["role"], row["status"], row["source"]) == ("document", "deliverable", "stored", "session")


def test_rendered_crop_outside_the_session_folder_is_registered(world):
    """Session 6a097cd6: the agent wrote the 9:16 crop to a folder it named, and the turn rendered it."""
    svc, t, s3, _ = world
    crop = "artifacts/wolf-crop-888104cf/output/wolf_9x16_center.jpg"
    _put(s3, crop, ct="image/jpeg")
    _put(s3, f"artifacts/{SID}/render/85387fb9.json", ct="application/json",
         body=json.dumps({"template": "image-annotated", "src": f"https://cdn/{crop}",
                          "candidate_keys": [crop], "sourceKey": crop}).encode())
    out = svc.run(work_from_turn(_turn_msg()))
    assert len(out["registered"]) == 1
    row = _assets(t)["wolf_9x16_center.jpg"]
    assert row["sourceUri"] == f"s3://{BUCKET}/{crop}" and row["kind"] == "image"


def test_project_with_indexing_off_stores_generated_media(world):
    svc, t, s3, _ = world
    t.update_item(Key=schema.meta_key(PID), UpdateExpression="SET indexing = :f", ExpressionAttributeValues={":f": False})
    _put(s3, f"artifacts/{SID}/output/cut.mp4")
    assert len(svc.run(work_from_turn(_turn_msg()))["registered"]) == 1
    assert _assets(t)["cut.mp4"]["status"] == "stored"  # kept and findable, not ingested


def test_replay_is_idempotent_and_a_rewrite_is_the_same_asset(world):
    svc, t, s3, _ = world
    key = f"artifacts/{SID}/output/cut.mp4"
    _put(s3, key, body=b"v1")
    r1 = svc.run(work_from_turn(_turn_msg()))
    r2 = svc.run(work_from_turn(_turn_msg()))
    assert len(r1["registered"]) == 1 and r2["registered"] == [] and r2["existing"] == r1["registered"]
    # In place, the asset IS the location: a later turn rewriting the key does not make a second asset.
    _put(s3, key, body=b"v2")
    r3 = svc.run(work_from_turn(_turn_msg(turnId="t2")))
    assert r3["registered"] == [] and r3["existing"] == r1["registered"]
    _put(s3, f"artifacts/{SID}/output/other/cut.mp4", body=b"v3")  # same name, another file
    svc.run(work_from_turn(_turn_msg(turnId="t3")))
    assert set(_assets(t)) == {"cut.mp4", "cut.mp4 (2)"}  # the shared ` (n)` allocation (Req 3.5)


def test_cap_of_fifty_per_message(world):
    svc, t, s3, _ = world
    for i in range(53):
        _put(s3, f"artifacts/{SID}/output/frame_{i:03d}.png", ct="image/png")
    out = svc.run(work_from_turn(_turn_msg()))
    assert len(out["registered"]) == 50 and {"reason": "limit", "count": 3} in out["skipped"]


def test_no_access_registers_nothing(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/cut.mp4")
    t.delete_item(Key=schema.member_key(PID, ACTOR))
    out = svc.run(work_from_turn(_turn_msg()))
    assert out["reason"] == "no_access" and _assets(t) == {}
    assert not s3.list_objects_v2(Bucket=BUCKET, Prefix="projects/").get("Contents")


def test_outside_window_is_ignored(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/cut.mp4")
    old = _turn_msg(startedAt=1_000_000, endedAt=2_000_000)
    assert svc.run(work_from_turn(old))["registered"] == []


def _ddb_image(item):
    from boto3.dynamodb.types import TypeSerializer
    s = TypeSerializer()
    return {k: s.serialize(v) for k, v in item.items()}


def test_async_plan_completion_uses_the_plan_rows_project(world):
    svc, t, s3, jobs = world
    _put(s3, f"artifacts/{SID}/output/reel.mp4")
    _put(s3, f"artifacts/{SID}/output/{UUID_DIR}/audio_music.wav", ct="audio/wav")
    s3.put_object(Bucket=BUCKET, Key="plans/x/result.json", Body=json.dumps({"schema": "plan-result/v1", "nodes": [
        {"artifacts": [{"url": f"https://cdn/artifacts/{SID}/output/reel.mp4", "role": "output"},
                       {"url": f"https://cdn/artifacts/{SID}/output/{UUID_DIR}/audio_music.wav",
                        "role": "output"}]}]}).encode())
    jobs.put_item(Item={"jobId": "plan-1", "producingProjectId": PID})
    new = {"jobId": "node-1", "runId": "plan-1", "actorId": ACTOR, "sessionId": SID, "status": "SUCCEEDED",
           "resultKey": "plans/x/result.json"}
    old = {**new, "status": "RUNNING"}
    old.pop("resultKey")
    rec = {"eventSource": "aws:dynamodb", "eventName": "MODIFY",
           "dynamodb": {"SequenceNumber": "1", "NewImage": _ddb_image(new), "OldImage": _ddb_image(old)}}
    assert handler({"Records": [rec]}, service=svc) == {"batchItemFailures": []}
    rows = _assets(t)
    assert set(rows) == {"reel.mp4"} and rows["reel.mp4"]["source"] == "session"
    assert rows["reel.mp4"]["producer"] == {"sessionId": SID}
    # A row without any project (not a Projects turn) does nothing.
    jobs.delete_item(Key={"jobId": "plan-1"})
    assert work_from_job_image({**new, "jobId": "node-2"}, old, svc.deps) is None
    # Already-settled row touched again: ignored.
    assert work_from_job_image({**new, "producingProjectId": PID}, new, svc.deps) is None


def test_failure_is_reported_for_redelivery(world):
    svc, t, s3, _ = world
    _put(s3, f"artifacts/{SID}/output/cut.mp4")

    def boom(**_kw):
        raise RuntimeError("ddb down")

    svc.store.register_asset = boom
    out = handler({"Records": [{"eventSource": "aws:sqs", "messageId": "m9", "body": json.dumps(_turn_msg())}]},
                  service=svc)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m9"}]} and _assets(t) == {}


# ------------------------------------------------------------------ asset-lineage Phase 3 (a): project layout
OTHER_PID = "p" + "f" * 32
NEW_ROOT = f"projects/{PID}/sessions/{SID}/"


def test_project_layout_session_source_rules():
    assert gen.session_roots(SID, PID) == [f"artifacts/{SID}/", NEW_ROOT]
    assert gen.is_session_source(NEW_ROOT + "output/cut.mp4", SID, PID)
    # Only THIS Project's root: another Project's (or no Project) is not this session's folder.
    assert not gen.is_session_source(f"projects/{OTHER_PID}/sessions/{SID}/output/cut.mp4", SID, PID)
    assert not gen.is_session_source(NEW_ROOT + "output/cut.mp4", SID)
    # The same exclusions as the legacy root, relative to the root.
    assert not gen.is_session_source(NEW_ROOT + "input/up.mp4", SID, PID)
    assert not gen.is_session_source(NEW_ROOT + f"output/{UUID_DIR}/SPEAKER_00.wav", SID, PID)
    assert not gen.is_session_source(NEW_ROOT + "transcript.json", SID, PID)
    # Legacy keeps working with or without a pid.
    assert gen.is_session_source(f"artifacts/{SID}/output/cut.mp4", SID, PID)
    assert gen.is_session_source(f"artifacts/{SID}/output/cut.mp4", SID)


def test_project_layout_rendered_and_result_refs():
    other_sid = "99999999-2222-4333-8444-555555555555"
    text = json.dumps({"tracks": [
        {"src": f"https://cdn/{NEW_ROOT}output/cut.mp4"},
        {"src": f"https://cdn/projects/{PID}/sessions/{other_sid}/output/theirs.mp4"},
        {"src": f"https://cdn/projects/{OTHER_PID}/sessions/{SID}/output/foreign.mp4"},
    ]})
    assert gen.rendered_refs_in(text, SID, PID) == [NEW_ROOT + "output/cut.mp4"]
    body = json.dumps({"schema": "plan-result/v1", "nodes": [{"artifacts": [
        {"url": f"https://cdn/{NEW_ROOT}output/p1/n1/reel.mp4", "role": "output"},
        {"url": f"https://cdn/{NEW_ROOT}output/src.mp4", "role": "source"}]}]})
    assert gen.refs_in_result(body, SID, PID) == [NEW_ROOT + "output/p1/n1/reel.mp4"]
    assert gen.refs_in_result(body, SID) == []  # no Project → the project root is not this session's
    # An invented-folder namespace is a legacy-root notion only.
    assert gen.invented_output_namespace(f"projects/{PID}/sessions/99999999-2222-4333-8444-555555555555/output/x.mp4", SID) is None


def test_turn_registers_new_layout_outputs_in_place_and_lists_both_roots(world):
    svc, t, s3, _ = world
    _put(s3, NEW_ROOT + "output/new_cut.mp4")
    _put(s3, f"artifacts/{SID}/output/old_cut.mp4")  # a session that began before the flag flipped
    _put(s3, f"projects/{OTHER_PID}/sessions/{SID}/output/foreign.mp4")
    out = svc.run(work_from_turn(_turn_msg()))
    assert len(out["registered"]) == 2
    rows = _assets(t)
    assert set(rows) == {"new_cut.mp4", "old_cut.mp4"}
    new = rows["new_cut.mp4"]
    assert new["source"] == "session" and new["sessionId"] == SID
    assert new["sourceUri"] == f"s3://{BUCKET}/{NEW_ROOT}output/new_cut.mp4"
    assert rows["old_cut.mp4"]["sourceUri"] == f"s3://{BUCKET}/artifacts/{SID}/output/old_cut.mp4"
    # The SRC guard maps the new location to its asset (identity is the record).
    assert svc.store.asset_id_for_location(PID, BUCKET, f"{NEW_ROOT}output/new_cut.mp4") == new["assetId"]


def test_lineage_output_under_the_new_root_is_this_sessions(world):
    svc, t, s3, _ = world
    key = NEW_ROOT + "output/graded.mp4"
    _put(s3, key)
    msg = _turn_msg(lineage=[{"output": f"s3://{BUCKET}/{key}", "inputs": [], "tool": "code_interpreter"}])
    work = work_from_turn(msg)
    own, invented = svc._lineage_keys(work)
    assert own == [key] and invented == []
