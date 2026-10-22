"""``ingestion.registrar`` pure parse/admission functions (design §6, Req 2.2, 2.5)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from ingestion.registrar import (  # noqa: E402
    DOCUMENT_MAX_BYTES,
    IMAGE_MAX_BYTES,
    LocationError,
    check_allowlist,
    check_project_prefix,
    classify_object,
    parse_s3_location,
)

PID = "p" + "d" * 32


def test_parse_object_vs_prefix():
    obj = parse_s3_location(f"s3://bucket/projects/{PID}/a.mp4")
    assert obj.bucket == "bucket" and obj.key == f"projects/{PID}/a.mp4" and not obj.is_prefix
    pfx = parse_s3_location(f"s3://bucket/projects/{PID}/")
    assert pfx.is_prefix
    bucket_only = parse_s3_location("s3://bucket")
    assert bucket_only.key == "" and bucket_only.is_prefix


def test_parse_rejects_non_s3_and_bad_bucket():
    with pytest.raises(LocationError):
        parse_s3_location("https://bucket/x")
    with pytest.raises(LocationError):
        parse_s3_location("s3://A_B/x")  # uppercase/underscore not valid
    with pytest.raises(LocationError):
        parse_s3_location(123)


def test_allowlist():
    loc = parse_s3_location("s3://external/x/a.mp4")
    check_allowlist(loc, ingest_bucket="ingest", allowlist=["external"])  # ok
    with pytest.raises(LocationError) as e:
        check_allowlist(loc, ingest_bucket="ingest", allowlist=[])
    assert e.value.code == "bucket_not_allowed"


def test_project_prefix_only_enforced_in_ingest_bucket():
    inside = parse_s3_location(f"s3://ingest/projects/{PID}/a.mp4")
    check_project_prefix(inside, pid=PID, ingest_bucket="ingest")  # ok
    outside = parse_s3_location("s3://ingest/elsewhere/a.mp4")
    with pytest.raises(LocationError) as e:
        check_project_prefix(outside, pid=PID, ingest_bucket="ingest")
    assert e.value.code == "outside_project_prefix"
    # A non-Ingest allowlisted bucket is exempt from the prefix rule.
    external = parse_s3_location("s3://external/anywhere/a.mp4")
    check_project_prefix(external, pid=PID, ingest_bucket="ingest")


def test_classify_object_by_type_size_and_extension():
    assert classify_object("a.mp4", "video/mp4", 100) == "video"
    assert classify_object("a.wav", "audio/wav", 100) == "audio"
    assert classify_object("a.png", "image/png", 100) == "image"
    # octet-stream falls back to extension.
    assert classify_object("a.mov", "application/octet-stream", 100) == "video"
    # every other type is a document (Projects Phase B: index ALL file types).
    assert classify_object("a.txt", "text/plain", 100) == "document"
    assert classify_object("deck.pdf", "application/pdf", 100) == "document"
    assert classify_object("noext", "", 100) == "document"
    # a non-media content type falls back to the extension.
    assert classify_object("a.mp4", "text/plain", 100) == "video"
    # image over its cap, a document over its cap, and a zero-byte video are not admitted.
    assert classify_object("a.pdf", "application/pdf", DOCUMENT_MAX_BYTES + 1) is None
    assert classify_object("a.png", "image/png", IMAGE_MAX_BYTES + 1) is None
    assert classify_object("a.mp4", "video/mp4", 0) is None
