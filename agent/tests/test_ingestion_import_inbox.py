"""The import inbox: files copied to ``projects/<pid>/import/`` become assets by themselves (``ingestion.import_inbox``).

Real store and S3 over moto. What matters: media is registered in place in the folder its path names, RAW and oversize
files are flagged (never failed silently), sidecars and system files are ignored, nothing is imported twice — not
even a file the user later removed — and a rescan re-checks only the files that needed attention.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from test_ingestion_asset_service import BUCKET, OWNER, PID, _event, ctx  # noqa: E402,F401
from ingestion import import_inbox as inbox  # noqa: E402
from ingestion.import_rules import classify  # noqa: E402
from ingestion.limits import DEFAULT_UPLOAD_LIMITS, GIB  # noqa: E402

P = f"projects/{PID}/import/"


def _put(s3, rel, size=1024):
    s3.put_object(Bucket=BUCKET, Key=P + rel, Body=b"x" * size)


def _owner_meta(store):
    store.table.update_item(Key={"PK": f"PROJECT#{PID}", "SK": "META"}, UpdateExpression="SET ownerSub = :o",
                            ExpressionAttributeValues={":o": OWNER})


def _assets(store):
    rows = store.table.query(KeyConditionExpression="PK = :p AND begins_with(SK, :s)",
                             ExpressionAttributeValues={":p": f"PROJECT#{PID}", ":s": "ASSET#"})["Items"]
    return {r["displayName"]: r for r in rows}


@pytest.mark.parametrize("name, action, reason", [
    ("Day1/A003_C017_0815XK.MXF", "ingest", None),
    ("Drone/DJI_0042.MOV", "ingest", None),
    ("Boards/FullSwing_Boards_v3.pdf", "ingest", None),
    ("Audio/VO_take3.wav", "ingest", None),
    ("B-cam/B001_C004.R3D", "attention", "not_supported"),
    ("Grade/FullSwing.cube", "keep", None),
    ("Edit/cut_v2.edl", "keep", None),
    ("Day1/A003_C017_0815XKM01.XML", "ignore", "sidecar"),
    ("Day1/CLIP.BIM", "ignore", "sidecar"),
    (".DS_Store", "ignore", "system_file"),
    ("Day1/._A003.MXF", "ignore", "system_file"),
])
def test_classify(name, action, reason):
    v = classify(P + name, 1024, None, DEFAULT_UPLOAD_LIMITS)
    assert v["action"] == action and v.get("reason") == reason


def test_classify_flags_a_file_over_its_kind_limit():
    assert classify(P + "Day2/A007.MXF", 6 * GIB, None, DEFAULT_UPLOAD_LIMITS) == {
        "action": "attention", "kind": "video", "reason": "too_large"}


def test_sweep_registers_in_place_with_folders_and_flags_the_rest(ctx):
    _, store, s3 = ctx
    _owner_meta(store)
    for rel in ("Day1/A003_C017.MXF", "Drone/DJI_0042.MOV", "top.mp4", "B-cam/B001.R3D", "Grade/look.cube",
                "Day1/A003_C017M01.XML", ".DS_Store"):
        _put(s3, rel)
    r = inbox.sweep_project(store, s3, BUCKET, PID)
    assert r["ingested"] == 3 and r["kept"] == 1 and r["attention"] == 1 and r["ignored"] == 2
    a = _assets(store)
    assert a["A003_C017.MXF"]["folder"] == "Day1" and a["DJI_0042.MOV"]["folder"] == "Drone"
    assert "folder" not in a["top.mp4"]
    assert a["A003_C017.MXF"]["source"] == "s3"
    assert a["A003_C017.MXF"]["sourceUri"] == f"s3://{BUCKET}/{P}Day1/A003_C017.MXF"   # in place: no copy
    assert a["A003_C017.MXF"]["addedBy"] == OWNER
    assert a["look.cube"]["status"] == "stored"          # kept, never queued
    st = inbox.import_status(store, PID)
    assert st["counts"] == {"registered": 3, "kept": 1, "attention": 1, "ignored": 2}
    assert st["attention"] == [{"key": P + "B-cam/B001.R3D", "reason": "not_supported", "size": 1024}]


def test_nothing_is_imported_twice_even_after_the_user_removes_it(ctx):
    _, store, s3 = ctx
    _owner_meta(store)
    _put(s3, "Day1/a.mp4")
    inbox.sweep_project(store, s3, BUCKET, PID)
    aid = _assets(store)["a.mp4"]["assetId"]
    store.remove_asset(PID, aid, removed_by=OWNER)
    again = inbox.sweep_project(store, s3, BUCKET, PID, full=True)
    assert again["ingested"] == 0 and again["already"] == 1
    assert [r for r in _assets(store).values() if r["status"] != "removed"] == []


def test_a_rescan_rechecks_flagged_files_after_a_limit_is_raised(ctx):
    _, store, s3 = ctx
    _owner_meta(store)
    _put(s3, "Day2/big.mp4", size=4096)
    small = {**DEFAULT_UPLOAD_LIMITS, "video": 1024}
    assert inbox.sweep_project(store, s3, BUCKET, PID, limits=small)["attention"] == 1
    assert inbox.sweep_project(store, s3, BUCKET, PID, limits=small)["attention"] == 0   # a sweep does not re-check
    r = inbox.sweep_project(store, s3, BUCKET, PID, full=True)                           # a rescan does
    assert r["ingested"] == 1 and _assets(store)["big.mp4"]["folder"] == "Day2"


def test_a_periodic_sweep_only_reads_recent_objects(ctx, monkeypatch):
    _, store, s3 = ctx
    _owner_meta(store)
    _put(s3, "old.mp4")
    inbox.sweep_project(store, s3, BUCKET, PID)
    monkeypatch.setattr(inbox, "WATERMARK_MARGIN", inbox.timedelta(0))
    r = inbox.sweep_project(store, s3, BUCKET, PID)
    assert r["scanned"] == 0                              # nothing modified since the watermark


def test_the_asset_service_reports_status_and_rescans(ctx):
    service, store, s3 = ctx
    _owner_meta(store)
    _put(s3, "Day1/a.mp4")
    _put(s3, "B-cam/b.braw")
    r = service.handle(_event("rescan_import", {}))
    assert r["ok"] is True and r["value"]["ingested"] == 1 and r["value"]["attention"] == 1
    st = service.handle(_event("import_status", {}))["value"]
    assert st["bucket"] == BUCKET and st["prefix"] == P
    assert st["counts"]["registered"] == 1 and st["attention"][0]["reason"] == "not_supported"


def test_the_lambda_finds_projects_by_listing_and_sweeps_each(ctx, monkeypatch):
    _, store, s3 = ctx
    from ingestion.handlers import import_sweep

    _owner_meta(store)
    _put(s3, "Day1/a.mp4")
    s3.put_object(Bucket=BUCKET, Key="projects/not-a-project/import/x.mp4", Body=b"x")
    assert import_sweep.project_ids(s3, BUCKET) == [PID]
