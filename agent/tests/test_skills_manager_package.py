from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strands import Skill

import skills_manager as skills_manager_module
from skills_manager import SkillsManager


class FakePlugin:
    def __init__(self, skills):
        self._skills = skills

    def get_available_skills(self):
        return self._skills

    def set_available_skills(self, skills):
        self._skills = skills


def test_get_local_skill_returns_actual_package_contents(tmp_path) -> None:
    package = tmp_path / "video-processing"
    references = package / "references"
    references.mkdir(parents=True)
    skill_md = """---
name: video-processing
description: Use when processing media.
---

# Video Processing
"""
    (package / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (references / "guide.md").write_text("# Guide\n", encoding="utf-8")

    manager = SkillsManager(skills_dir=tmp_path)
    manager._plugin = FakePlugin([
        SimpleNamespace(
            name="video-processing",
            description="Use when processing media.",
            instructions="# Video Processing",
        )
    ])
    manager._skill_source["video-processing"] = "local"
    manager._skill_package_root["video-processing"] = package

    result = manager.get_skill("video-processing")

    assert result is not None
    assert result["skillMd"] == skill_md
    assert result["supportingFiles"] == ["references/guide.md"]
    assert result["source"] == "local"


def test_load_runtime_package_materializes_supporting_files(tmp_path) -> None:
    manager = SkillsManager(skills_dir=tmp_path)
    manager._plugin = FakePlugin([])
    skill_md = """---
name: uploaded-skill
description: Use when testing uploaded references.
---

# Uploaded Skill

Read references/guide.md.
"""

    result = manager.load_skill_package_programmatic(
        name="uploaded-skill",
        skill_md=skill_md,
        supporting_files={"references/guide.md": b"# Guide\n"},
        package_id="actor-1::owned::asset-1",
    )

    assert result["name"] == "uploaded-skill"
    detail = manager.get_skill("uploaded-skill")
    assert detail is not None
    assert detail["skillMd"] == skill_md
    assert detail["supportingFiles"] == ["references/guide.md"]
    package_root = manager._skill_package_root["uploaded-skill"]
    assert (package_root / "references" / "guide.md").read_text() == "# Guide\n"

    assert manager.unload_skill("uploaded-skill") is True
    assert not package_root.exists()


def test_request_plugins_isolate_actor_skills_and_preserve_shared_plugin(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(skills_manager_module, "AgentSkills", FakePlugin)
    manager = SkillsManager(skills_dir=tmp_path)
    shared = Skill(
        name="shared-skill",
        description="Shared baseline",
        instructions="shared instructions",
    )
    manager._shared_skills = [shared]
    manager._plugin = FakePlugin([shared])

    actor_one_working = Skill(
        name="private-skill",
        description="Working copy",
        instructions="working instructions",
    )
    actor_one_published = Skill(
        name="private-skill",
        description="Published copy",
        instructions="published instructions",
    )
    actor_two = Skill(
        name="other-private-skill",
        description="Actor two",
        instructions="actor two instructions",
    )

    actor_one_plugin = manager.create_request_plugin(
        [actor_one_working, actor_one_published]
    )
    actor_two_plugin = manager.create_request_plugin([actor_two])

    actor_one_skills = actor_one_plugin.get_available_skills()
    actor_two_skills = actor_two_plugin.get_available_skills()
    assert [skill.name for skill in actor_one_skills] == [
        "shared-skill",
        "private-skill",
    ]
    assert actor_one_skills[-1].instructions == "working instructions"
    assert [skill.name for skill in actor_two_skills] == [
        "shared-skill",
        "other-private-skill",
    ]
    assert [skill.name for skill in manager.plugin.get_available_skills()] == [
        "shared-skill"
    ]


def test_request_package_materialization_can_be_cleaned(tmp_path) -> None:
    manager = SkillsManager(skills_dir=tmp_path)
    skill_md = """---
name: isolated-package
description: Use when testing request package cleanup.
---

# Isolated package
"""

    skill, package_root = manager.materialize_runtime_skill_package(
        name="isolated-package",
        skill_md=skill_md,
        supporting_files={"references/guide.md": b"# Guide\n"},
        package_id="actor-isolated::owned::asset-1",
    )

    assert skill.name == "isolated-package"
    assert (package_root / "references" / "guide.md").exists()
    manager.cleanup_runtime_package(package_root)
    assert not package_root.exists()
