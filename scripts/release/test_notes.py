"""Tests for scripts/release/notes.py (the GitLab Release body). Pure: no git calls except the e2e case,
which builds a throwaway repo.

Run:  cd agent && uv run --extra dev pytest -q ../scripts/release/test_notes.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import notes  # noqa: E402


def test_versions_parse_as_numbers_and_pre_releases_are_not_releases():
    assert notes.parse_version("1.0.0") == (1, 0, 0)
    assert notes.parse_version("v0.14.2") == (0, 14, 2)
    for v in ("v1.0.0-rc.3", "1.0.0-rc.12+abc1234", "v1.0", "latest", ""):
        assert notes.parse_version(v) is None, v


def test_the_first_major_release_follows_the_highest_0x_release():
    tags = ["v0.9.0", "v0.10.0", "v0.13.1", "v0.14.0", "v0.14.2", "v0.14.1"]
    assert notes.pick_previous(tags, "1.0.0") == "v0.14.2"           # numeric, not lexical (v0.9 > v0.14)
    assert notes.pick_previous(tags, "0.14.1") == "v0.14.0"


def test_the_release_never_follows_itself_or_a_later_or_pre_release_tag():
    # release:prod re-run after the tag exists: still the commits since the PREVIOUS release.
    assert notes.pick_previous(["v0.14.2", "v1.0.0"], "1.0.0") == "v0.14.2"
    assert notes.pick_previous(["v0.14.2", "v1.0.0-rc.1", "v1.1.0"], "1.0.0") == "v0.14.2"
    assert notes.pick_previous([], "1.0.0") is None
    assert notes.pick_previous(["v1.0.0"], "1.0.0") is None


def test_notes_for_1_0_0_end_to_end(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    for i, msg in enumerate(["feat: old thing", "fix(ui): released bug", "feat(agent): new skill",
                             "fix(security): owner-scoped reads", "docs: changelog"]):
        (r / f"f{i}").write_text(str(i))
        git("add", ".")
        git("commit", "-q", "-m", msg)
        if i == 1:
            git("tag", "v0.14.2")
    git("tag", "v1.0.0")                                              # tagged before a re-run
    monkeypatch.setattr(notes, "ROOT", r)
    prev = notes.previous_tag("1.0.0")
    assert prev == "v0.14.2"
    body = notes.notes("1.0.0", prev)
    assert body.startswith("## v1.0.0\n\n3 commits since v0.14.2.")
    assert "- **agent**: new skill" in body and "- **security**: owner-scoped reads" in body
    assert "### Other" in body and "- docs: changelog" in body
    assert "old thing" not in body and "released bug" not in body
