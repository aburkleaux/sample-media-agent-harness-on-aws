"""Ingestion row-key builders in ``projects.schema`` (media-harness-project-ingestion design §2).

The keys are the contract every ingestion write and the proxy reads share, so a typo in a prefix or a
padding width cannot split one logical item into two. These tests pin the exact key shapes the design
Data Models table names.
"""
from __future__ import annotations

import hashlib
import os
import sys
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from projects import schema  # noqa: E402

PID = "p" + "0" * 32
AID = "video:6f1c0a2b-1111-4222-8333-444455556666"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def test_asset_key_shape():
    assert schema.asset_key(PID, AID) == {"PK": f"PROJECT#{PID}", "SK": f"ASSET#{AID}"}


def test_asset_key_rejects_malformed_id():
    with pytest.raises(ValueError):
        schema.asset_key(PID, "video:not-a-uuid")
    with pytest.raises(ValueError):
        schema.asset_key(PID, "doc:6f1c0a2b-1111-4222-8333-444455556666")


def test_src_guard_hashes_bucket_slash_key():
    k = schema.src_guard_key(PID, "my-bucket", "projects/x/a.mp4")
    assert k["PK"] == f"PROJECT#{PID}"
    assert k["SK"] == "SRC#" + _sha("my-bucket/projects/x/a.mp4")


def test_dname_guard_folds_nfc_casefold():
    name = "Clip.MP4"
    expected_fold = unicodedata.normalize("NFC", name).casefold()
    k = schema.dname_guard_key(PID, name)
    assert k["SK"] == "DNAME#" + _sha(expected_fold)
    # Case-insensitive: a differently-cased name folds to the SAME guard.
    assert schema.dname_guard_key(PID, "clip.mp4")["SK"] == k["SK"]


def test_queue_key_zero_pads_13_digits_then_asset_id():
    k = schema.queue_key(PID, 1791201600000, AID)
    assert k["SK"] == f"QUEUE#1791201600000#{AID}"
    # Padding keeps lexical order chronological: a smaller time sorts first.
    early = schema.queue_key(PID, 5, AID)["SK"]
    late = schema.queue_key(PID, 1791201600000, AID)["SK"]
    assert early < late
    assert early == f"QUEUE#0000000000005#{AID}"


def test_failidx_key_shape():
    k = schema.failidx_key(PID, 42, AID)
    assert k["SK"] == f"FAILIDX#0000000000042#{AID}"


def test_queue_key_rejects_bad_inputs():
    with pytest.raises(ValueError):
        schema.queue_key(PID, -1, AID)
    with pytest.raises(ValueError):
        schema.queue_key(PID, True, AID)  # bool is not an accepted ms value
    with pytest.raises(ValueError):
        schema.queue_key(PID, 10, "image:bad")


def test_is_asset_id_and_kind():
    assert schema.is_asset_id(AID)
    assert schema.asset_kind(AID) == "video"
    assert schema.asset_kind("audio:6f1c0a2b-1111-4222-8333-444455556666") == "audio"
    assert schema.asset_kind("document:6f1c0a2b-1111-4222-8333-444455556666") == "document"
    assert schema.asset_kind("data:6f1c0a2b-1111-4222-8333-444455556666") == "data"
    assert schema.ASSET_KINDS == ("video", "image", "audio", "document", "data")
    assert not schema.is_asset_id("doc:6f1c0a2b-1111-4222-8333-444455556666")
    assert not schema.is_asset_id("video:6f1c0a2b-1111-1222-8333-444455556666")  # version nibble != 4
    assert schema.asset_kind("nope") is None


def test_existing_keys_unchanged():
    # The projects-access keys must be byte-for-byte what they were (no regressions).
    assert schema.meta_key(PID) == {"PK": f"PROJECT#{PID}", "SK": "META"}
    assert schema.member_key(PID, "sub")["SK"] == "MEMBER#sub"
    assert schema.project_prefix(PID) == f"projects/{PID}/"


def test_hash_guard_and_job_keys():
    k = schema.hash_guard_key(PID, "CRC64NVME", "AbCd+/==", 42)
    assert k == {"PK": f"PROJECT#{PID}", "SK": "HASH#CRC64NVME:AbCd+/==:42"}
    assert schema.job_key(PID, "arn:aws:states:x")["SK"] == "JOB#arn:aws:states:x"
    for bad in (("", "v", 1), ("A:B", "v", 1), ("A", "", 1), ("A", "v", -1), ("A", "v", True)):
        with pytest.raises(ValueError):
            schema.hash_guard_key(PID, *bad)
    with pytest.raises(ValueError):
        schema.job_key(PID, "")
