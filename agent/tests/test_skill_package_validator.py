from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.skill_package_validator import (
    MAX_FILE_BYTES,
    SkillPackageValidationError,
    validate_skill_document,
    validate_skill_package,
)


VALID_SKILL_MD = """---
name: folder-skill
description: Use when a folder package needs validation.
allowed-tools:
  - read_file
metadata:
  owner: media-team
---

# Folder Skill
"""


def test_valid_skill_package_accepts_standard_directories() -> None:
    parsed = validate_skill_document(VALID_SKILL_MD, expected_name="folder-skill")
    assert parsed["name"] == "folder-skill"

    files = validate_skill_package(
        name="folder-skill",
        skill_md=VALID_SKILL_MD,
        supporting_files=[
            ("references/guide.md", b"# Guide\n"),
            ("scripts/helpers/run.py", b"print('ok')\n"),
            ("assets/icons/logo.png", b"png"),
        ],
    )
    assert [path for path, _ in files] == [
        "references/guide.md",
        "scripts/helpers/run.py",
        "assets/icons/logo.png",
    ]


@pytest.mark.parametrize(
    ("skill_md", "message"),
    [
        ("# Missing", "YAML frontmatter"),
        (VALID_SKILL_MD.replace("name: folder-skill", "name: Folder Skill"), "lowercase"),
        (VALID_SKILL_MD.replace("description:", "unknown: true\ndescription:"), "Unsupported"),
    ],
)
def test_invalid_skill_document_is_rejected(skill_md: str, message: str) -> None:
    with pytest.raises(SkillPackageValidationError, match=message):
        validate_skill_document(skill_md, expected_name="folder-skill")


def test_import_name_must_match_frontmatter() -> None:
    with pytest.raises(SkillPackageValidationError, match="must match"):
        validate_skill_document(VALID_SKILL_MD, expected_name="renamed-skill")


def test_references_are_one_level_deep() -> None:
    with pytest.raises(SkillPackageValidationError, match="directly under references"):
        validate_skill_package(
            name="folder-skill",
            skill_md=VALID_SKILL_MD,
            supporting_files=[("references/nested/guide.md", b"guide")],
        )


def test_case_insensitive_duplicate_paths_are_rejected() -> None:
    with pytest.raises(SkillPackageValidationError, match="Duplicate package path"):
        validate_skill_package(
            name="folder-skill",
            skill_md=VALID_SKILL_MD,
            supporting_files=[
                ("assets/schema.json", b"{}"),
                ("Assets/schema.json", b"{}"),
            ],
        )


def test_root_skill_md_obeys_individual_file_limit() -> None:
    oversized = VALID_SKILL_MD + ("x" * MAX_FILE_BYTES)

    with pytest.raises(SkillPackageValidationError, match="SKILL.md exceeds the 2 MB limit"):
        validate_skill_package(
            name="folder-skill",
            skill_md=oversized,
            supporting_files=[],
        )
