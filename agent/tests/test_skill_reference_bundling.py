"""Tests for whole-skill loading — bundling a skill's text references into its instructions so
they reach the model in every execution context (including DAG plan nodes), with a byte budget
that preserves progressive disclosure for large reference sets.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from skills_manager import SkillsManager, _BUNDLE_MAX_BYTES  # noqa: E402

AGENT_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _write_skill(root: Path, name: str, body: str, refs: dict[str, str]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f'---\nname: {name}\ndescription: "d"\n---\n\n{body}')
    for rel, content in refs.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


class TestDiskBundling:
    def test_real_content_moderation_bundles_the_rating_table(self):
        """The reported bug: the rating enum lives in references/rating-system.md and never reached
        the end-to-end sg5 node. It must now be inlined into the loaded instructions."""
        sm = SkillsManager(skills_dir=AGENT_SKILLS, s3_bucket="")
        cm = {s.name: s for s in sm._load_from_disk()}["content-moderation"]
        assert "Bundled references" in cm.instructions
        assert "All Ages" in cm.instructions and "Child 7+" in cm.instructions and "Adult 18+" in cm.instructions
        assert "report-template.md" in cm.instructions  # every reference bundled, not just one

    def test_over_budget_skill_keeps_progressive_disclosure(self):
        """video-processing has ~100KB of per-tool references — it must NOT be bundled (context)."""
        sm = SkillsManager(skills_dir=AGENT_SKILLS, s3_bucket="")
        vp = {s.name: s for s in sm._load_from_disk()}["video-processing"]
        assert "Bundled references" not in vp.instructions

    def test_all_or_nothing_budget_gate(self, tmp_path):
        _write_skill(tmp_path, "under-skill", "body", {"references/a.md": "x" * (_BUNDLE_MAX_BYTES - 500)})
        _write_skill(tmp_path, "over-skill", "body", {"references/a.md": "x" * (_BUNDLE_MAX_BYTES + 500)})
        loaded = {s.name: s for s in SkillsManager(skills_dir=tmp_path, s3_bucket="")._load_from_disk()}
        assert "Bundled references" in loaded["under-skill"].instructions
        assert "Bundled references" not in loaded["over-skill"].instructions

    def test_skips_binary_and_never_raises(self, tmp_path):
        d = _write_skill(tmp_path, "bin-skill", "body", {"references/note.md": "hello-ref"})
        (d / "references" / "img.png").write_bytes(b"\x89PNG\x00\x00\x01")
        s = {x.name: x for x in SkillsManager(skills_dir=tmp_path, s3_bucket="")._load_from_disk()}["bin-skill"]
        assert "hello-ref" in s.instructions  # text bundled
        assert "img.png" not in s.instructions  # binary/non-text skipped

    def test_skill_with_no_references_is_unchanged(self, tmp_path):
        _write_skill(tmp_path, "bare-skill", "just a body", {})
        s = {x.name: x for x in SkillsManager(skills_dir=tmp_path, s3_bucket="")._load_from_disk()}["bare-skill"]
        assert "Bundled references" not in s.instructions
        assert "just a body" in s.instructions


class TestCrudRoundTrip:
    def test_get_skill_returns_original_instructions_without_bundle(self):
        """save_skill rebuilds SKILL.md from get_skill['instructions'] — it MUST be the original
        body, never the bundled augmentation, or a save would persist references into SKILL.md."""
        sm = SkillsManager(skills_dir=AGENT_SKILLS, s3_bucket="")
        sm.create_plugin()
        gs = sm.get_skill("content-moderation")
        assert gs is not None
        assert "Bundled references" not in gs["instructions"]
        assert "Bundled skill reference" not in gs["instructions"]
        assert "Bundled references" not in gs["skillMd"]
        # the author's own inline content survives (sanity)
        assert "Adult 18+" in gs["instructions"]
        rebuilt = f"---\nname: {gs['name']}\ndescription: {gs['description']}\n---\n\n{gs['instructions']}"
        assert "Bundled skill reference" not in rebuilt


class _FakeBody:
    def __init__(self, b: bytes):
        self._b = b

    def read(self) -> bytes:
        return self._b


class _FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        if Delimiter == "/":
            return {"CommonPrefixes": [{"Prefix": "skills/s3-skill/"}]}
        return {"Contents": [{"Key": k} for k in self._objects if k.startswith(Prefix)], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        if Key not in self._objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": _FakeBody(self._objects[Key])}


class TestS3FullPackage:
    def test_s3_downloads_full_package_materializes_and_bundles(self, tmp_path):
        objects = {
            "skills/s3-skill/SKILL.md": b'---\nname: s3-skill\ndescription: "d"\n---\n\nbody',
            "skills/s3-skill/references/r.md": b"RATING_TABLE_SENTINEL",
        }
        sm = SkillsManager(skills_dir=tmp_path, s3_bucket="b", s3_prefix="skills/")
        sm._s3_client = _FakeS3(objects)
        loaded = {s.name: s for s in sm._load_from_s3()}
        assert "s3-skill" in loaded
        s = loaded["s3-skill"]
        assert s.path is not None, "S3 skill must be materialized to a package dir (parity with disk)"
        assert "RATING_TABLE_SENTINEL" in s.instructions, "S3 reference must be downloaded + bundled"
        assert "Bundled references" in s.instructions

    def test_s3_falls_back_to_md_only_when_package_download_fails(self, tmp_path, monkeypatch):
        objects = {"skills/s3-skill/SKILL.md": b'---\nname: s3-skill\ndescription: "d"\n---\n\nbody'}
        sm = SkillsManager(skills_dir=tmp_path, s3_bucket="b", s3_prefix="skills/")
        sm._s3_client = _FakeS3(objects)
        # Force the package path to blow up; loader must still return a usable md-only skill.
        monkeypatch.setattr(sm, "materialize_runtime_skill_package",
                            lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
        loaded = {s.name: s for s in sm._load_from_s3()}
        assert "s3-skill" in loaded
        assert "body" in loaded["s3-skill"].instructions
