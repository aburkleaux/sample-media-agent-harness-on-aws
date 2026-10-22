"""Skill loading: bundled skills preload at container start; S3 round trips overlap.

The S3 part used to be strictly sequential (SKILL.md, then every package object, then
tools.json, prefix by prefix). Only the waiting overlaps now: every decision (local precedence,
first prefix wins for a duplicate name, md-only fallback) is still made in prefix order.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import skills_manager as smod  # noqa: E402


class _NoSuchKey(Exception):
    pass


class _FakeS3:
    """list_objects_v2 / get_object over an in-memory bucket, with per-call latency."""

    def __init__(self, objects: dict[str, bytes], delay=0.05, fail_lists=()):
        self.objects = objects
        self.delay = delay
        self.fail_lists = set(fail_lists)
        self.exceptions = type("E", (), {"NoSuchKey": _NoSuchKey})
        self.lock = threading.Lock()
        self.calls = 0

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        time.sleep(self.delay)
        if Delimiter:
            dirs = sorted({k[len(Prefix):].split("/", 1)[0] for k in self.objects if k.startswith(Prefix)})
            return {"CommonPrefixes": [{"Prefix": f"{Prefix}{d}/"} for d in dirs]}
        if Prefix in self.fail_lists:
            raise RuntimeError("list failed")
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        time.sleep(self.delay)
        with self.lock:
            self.calls += 1
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def _md(name, desc="d"):
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nInstructions for {name}.\n".encode()


def _manager(tmp_path, s3):
    m = smod.SkillsManager(skills_dir=tmp_path / "skills", s3_bucket="b", s3_prefix="skills/")
    m._bundled_skills_dir = tmp_path / "bundled"   # no bundled skills in these tests
    m._s3_client = s3
    return m


def _bucket(n):
    objs = {}
    for i in range(n):
        objs[f"skills/s{i}/SKILL.md"] = _md(f"skill-{i}")
        objs[f"skills/s{i}/references/a.md"] = b"ref"
        objs[f"skills/s{i}/tools.json"] = b'["t1"]'
    return objs


def test_s3_skills_load_concurrently_and_in_prefix_order(tmp_path):
    s3 = _FakeS3(_bucket(6), delay=0.05)
    m = _manager(tmp_path, s3)
    started = time.monotonic()
    skills = m._load_from_s3()
    elapsed = time.monotonic() - started
    assert [s.name for s in skills] == [f"skill-{i}" for i in range(6)]
    assert all(m._skill_source[f"skill-{i}"] == "s3" for i in range(6))
    assert m._skill_tools["skill-0"] == ["t1"]
    # 6 prefixes x (SKILL.md + list + reference + tools.json) at 50ms = ~1.25s one at a time.
    assert elapsed < 0.6, f"S3 skill load took {elapsed:.2f}s"


def test_first_prefix_wins_for_a_duplicate_name_and_local_takes_precedence(tmp_path):
    objs = {
        "skills/a/SKILL.md": _md("dup", "first"),
        "skills/b/SKILL.md": _md("dup", "second"),
        "skills/c/SKILL.md": _md("local-one"),
    }
    m = _manager(tmp_path, _FakeS3(objs, delay=0.0))
    m._skill_source["local-one"] = "local"
    skills = m._load_from_s3()
    assert [s.name for s in skills] == ["dup"]
    assert m._skill_s3_prefix["dup"] == "skills/a/"


def test_missing_skill_md_is_skipped_and_a_package_failure_falls_back_to_md_only(tmp_path):
    objs = {
        "skills/nomd/readme.txt": b"x",
        "skills/ok/SKILL.md": _md("ok"),
    }
    m = _manager(tmp_path, _FakeS3(objs, delay=0.0, fail_lists={"skills/ok/"}))
    skills = m._load_from_s3()
    assert [s.name for s in skills] == ["ok"]
    assert "ok" not in m._skill_package_root          # md-only fallback: no package dir
    assert "Instructions for ok." in skills[0].instructions


def test_preloaded_disk_skills_are_used_once(tmp_path, monkeypatch):
    m = smod.SkillsManager(skills_dir=tmp_path / "skills")
    calls = []
    monkeypatch.setattr(m, "_load_from_disk", lambda: calls.append(1) or [])
    assert m.preload_disk_skills() == 0
    m.create_plugin()
    assert calls == [1]                                # the plugin reused the preload
    m.create_plugin()
    assert calls == [1, 1]                             # a later plugin re-reads the disk
