"""``scripts/backfill_no_speech_audio.py``: failed no-speech audio rows move to ``stored``; nothing else moves."""
from __future__ import annotations

import importlib.util
import os

import boto3
import pytest
from moto import mock_aws

REGION = "us-west-2"
TABLE = "mediaworks-harness-projects"
PID = "p" + "a" * 32
SFX_ID, MUSIC_ID = "audio:11111111-1111-4111-8111-111111111111", "audio:22222222-2222-4222-8222-222222222222"


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "backfill_no_speech_audio.py")
    spec = importlib.util.spec_from_file_location("backfill_no_speech_audio", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load()

SFX = '{"errorMessage":"No VAD turns found after flattening \u2014 check VAD output","errorType":"RuntimeError"}'
MUSIC = '{"errorMessage":"No high-quality embeddings found","errorType":"ValueError"}'


def _asset(aid, kind, reason, step="speech-diarization", failed_at=1_791_000_000_000):
    return {"PK": f"PROJECT#{PID}", "SK": f"ASSET#{aid}", "assetId": aid, "kind": kind, "status": "failed",
            "displayName": aid, "rowVersion": 3, "failedAt": failed_at,
            "failure": {"step": step, "reason": f"{step}: {reason}", "at": "2026-10-09T11:56:00Z"}}


@pytest.fixture
def table(monkeypatch):
    for k, v in (("AWS_ACCESS_KEY_ID", "t"), ("AWS_SECRET_ACCESS_KEY", "t"), ("AWS_DEFAULT_REGION", REGION)):
        monkeypatch.setenv(k, v)
    with mock_aws():
        t = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": "META", "status": "active", "changeSeq": 10,
                         "statusCounts": {"failed": 4, "indexed": 7}})
        rows = [_asset("audio:11111111-1111-4111-8111-111111111111", "audio", SFX, failed_at=1), _asset("audio:22222222-2222-4222-8222-222222222222", "audio", MUSIC, failed_at=2),
                _asset("audio:33333333-3333-4333-8333-333333333333", "audio", "Lambda.Unknown: out of memory", failed_at=3),
                _asset("image:44444444-4444-4444-8444-444444444444", "image", "the image cannot be read: AccessDenied", step="image rendition",
                       failed_at=4)]
        for r in rows:
            t.put_item(Item=r)
            t.put_item(Item={"PK": f"PROJECT#{PID}", "SK": f"FAILIDX#{r['failedAt']:013d}#{r['assetId']}",
                             "assetId": r["assetId"]})
        yield t


def _row(t, aid):
    return t.get_item(Key={"PK": f"PROJECT#{PID}", "SK": f"ASSET#{aid}"})["Item"]


def test_dry_run_writes_nothing(table):
    out = []
    s = bf.run(table, boto3.client("dynamodb", region_name=REGION), execute=False, out=out.append)
    assert (s["failedAudioRows"], s["noSpeech"], s["written"]) == (3, 2, 0)
    assert _row(table, "audio:11111111-1111-4111-8111-111111111111")["status"] == "failed"


def test_execute_moves_only_the_no_speech_rows(table):
    s = bf.run(table, boto3.client("dynamodb", region_name=REGION), execute=True, out=lambda *_: None,
               now_iso="2026-10-09T20:00:00.000Z")
    assert s["written"] == 2 and sorted(c["assetId"] for c in s["changed"]) == sorted([SFX_ID, MUSIC_ID])
    for aid in ("audio:11111111-1111-4111-8111-111111111111", "audio:22222222-2222-4222-8222-222222222222"):
        row = _row(table, aid)
        assert row["status"] == "stored" and row["indexSkippedReason"] == "no_speech"
        assert "failure" not in row and "failedAt" not in row and row["rowVersion"] == 4
    assert _row(table, "audio:33333333-3333-4333-8333-333333333333")["status"] == "failed"
    assert _row(table, "image:44444444-4444-4444-8444-444444444444")["status"] == "failed"
    meta = table.get_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"})["Item"]
    assert meta["statusCounts"] == {"failed": 2, "indexed": 7, "stored": 2} and meta["changeSeq"] == 12
    idx = table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :f)",
                      ExpressionAttributeValues={":p": f"PROJECT#{PID}", ":f": "FAILIDX#"})["Items"]
    assert sorted(i["assetId"] for i in idx) == ["audio:33333333-3333-4333-8333-333333333333", "image:44444444-4444-4444-8444-444444444444"]
    # Idempotent: a second run finds nothing to move.
    again = bf.run(table, boto3.client("dynamodb", region_name=REGION), execute=True, out=lambda *_: None)
    assert again["written"] == 0 and again["noSpeech"] == 0


def test_only_staging_is_accepted():
    assert sorted(bf.ENVIRONMENTS) == ["staging"]
    with pytest.raises(SystemExit):
        bf.main(["--env", "prod"])
