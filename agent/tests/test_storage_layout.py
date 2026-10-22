"""Session output layout (asset-lineage Phase 3 (a), design §9.1): projects/storage_layout.py.

The reader cases and writer decisions come from ``fixtures/session_layout_cases.json``, which the TS twin
(``ui/src/shared/sessionLayout.test.ts``) and the Lambda copies load too.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from projects import storage_layout as sl

CASES = json.loads((Path(__file__).parent / "fixtures" / "session_layout_cases.json").read_text())
PID, SID = CASES["pid"], CASES["sid"]


@pytest.mark.parametrize("case", CASES["keys"], ids=lambda c: c["key"] or "<empty>")
def test_parse_session_key_matches_fixture(case):
    got = sl.parse_session_key(case["key"])
    if case["parsed"] is None:
        assert got is None
    else:
        assert got == sl.SessionKey(**case["parsed"])
    assert sl.is_project_session_output_key(case["key"]) is case["isProjectSessionOutput"]


@pytest.mark.parametrize("case", CASES["writers"], ids=lambda c: f"{c['flag'] or 'unset'}-{c['pid']}-{c['sid']}")
def test_writer_decision_matches_fixture(case, monkeypatch):
    if case["flag"]:
        monkeypatch.setenv(sl.LAYOUT_ENV, case["flag"])
    else:
        monkeypatch.delenv(sl.LAYOUT_ENV, raising=False)
    assert sl.session_output_prefix(case["sid"], case["pid"]) == case["expectedPrefix"]


def test_flag_is_read_lazily(monkeypatch):
    monkeypatch.delenv(sl.LAYOUT_ENV, raising=False)
    assert sl.session_output_prefix(SID, PID).startswith("artifacts/")
    monkeypatch.setenv(sl.LAYOUT_ENV, "project")  # e.g. hydrated from RuntimeConfigSecret after import
    assert sl.session_output_prefix(SID, PID) == f"projects/{PID}/sessions/{SID}/output/"


def test_custom_artifacts_prefix_for_legacy_writers(monkeypatch):
    monkeypatch.delenv(sl.LAYOUT_ENV, raising=False)
    assert sl.session_output_prefix(SID, PID, artifacts_prefix="x/") == f"x/{SID}/output/"
    assert sl.session_output_prefix("", PID, artifacts_prefix="x/") == "x/"


def test_readers_are_flag_independent(monkeypatch):
    for flag in ("", "project"):
        monkeypatch.setenv(sl.LAYOUT_ENV, flag)
        assert sl.session_roots(SID, PID) == [f"artifacts/{SID}/", f"projects/{PID}/sessions/{SID}/"]
        assert sl.session_output_roots(SID, PID) == [
            f"artifacts/{SID}/output/", f"projects/{PID}/sessions/{SID}/output/"]
        assert sl.session_roots(SID, None) == [f"artifacts/{SID}/"]
        assert sl.session_roots(SID, "not-a-pid") == [f"artifacts/{SID}/"]
        assert sl.parse_session_key(f"artifacts/{SID}/output/a.mp4").layout == "artifacts"
        assert sl.parse_session_key(f"projects/{PID}/sessions/{SID}/output/a.mp4").layout == "project"


def test_session_root_pattern_reads_either_root():
    rx = re.compile(r"(?:^|/)" + sl.SESSION_ROOT_PATTERN)
    m = rx.search(f"https://cdn.example/projects/{PID}/sessions/{SID}/output/a.mp4")
    assert m and sl.session_id_of(m) == SID and m.group("pid") == PID
    m = rx.search(f"https://cdn.example/artifacts/{SID}/output/a.mp4")
    assert m and sl.session_id_of(m) == SID and m.group("pid") is None
    m = rx.search("https://cdn.example/artifacts/external/0123456789abcdef01234567.mp4")
    assert m is None or sl.session_id_of(m) is None
    # A project ASSET key is not a session key.
    assert rx.search(f"projects/{PID}/video:0b6c1b5e-3a3f-4b44-9c55-111122223333/x.mp4") is None


def test_find_session_key_in_urls():
    assert sl.find_session_key(
        f"https://d.cloudfront.net/projects/{PID}/sessions/{SID}/output/a.mp4?X-Amz-Signature=x"
    ) == sl.SessionKey("project", SID, PID, "output/a.mp4")
    assert sl.find_session_key(f"s3://bkt/artifacts/{SID}/output/x/y.json") == sl.SessionKey(
        "artifacts", SID, None, "output/x/y.json")
    assert sl.find_session_key("https://d.cloudfront.net/artifacts/external/0123456789abcdef01234567.mp4") is None
    assert sl.find_session_key(None) is None
