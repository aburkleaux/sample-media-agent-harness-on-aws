"""The video-processing skill carries no hybrid-search instruction (media-harness-projects-access task 11.16).

Project search / indexing / media Q&A is Projects-only now (the `media-search-qa` skill), not a user-facing
tool of this skill (Req 5.7, design Component 8). So the skill — SKILL.md and every reference under it —
must contain no instruction to call the hybrid-search tools, and the standalone hybrid-search reference is
gone. The skill points media search and Q&A at `media-search-qa` instead.

Note: the bare verbs ``create_collection``/``list_collections`` are ALSO face-recognition tools
(`face-recognition___create_collection`, a different target that stays), so those bare names are asserted
absent only in SKILL.md — whose sole collection/search content was the removed hybrid-search row — while the
references are checked against the unambiguous ``hybrid-search___`` / ``hybrid-search-admin`` namespaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parents[1] / "skills" / "video-processing"
SKILL_MD = SKILL_ROOT / "SKILL.md"
REFERENCES = SKILL_ROOT / "references"

# Unambiguous hybrid-search tool namespaces — these belong to no other target and must appear nowhere.
HYBRID_SEARCH_NAMESPACES = ["hybrid-search___", "hybrid-search-admin"]

# The hybrid-search operation names that, within SKILL.md, only ever came from the hybrid-search row.
HYBRID_SEARCH_VERBS = ["search_content", "index_content", "create_collection", "list_collections"]

REFERENCE_MARKDOWN = sorted(REFERENCES.glob("*.md"))


def test_the_standalone_hybrid_search_reference_is_removed():
    assert not (REFERENCES / "hybrid_search_agent.md").exists()


@pytest.mark.parametrize("name", HYBRID_SEARCH_NAMESPACES + HYBRID_SEARCH_VERBS)
def test_skill_md_names_no_hybrid_search_tool(name):
    assert name not in SKILL_MD.read_text(encoding="utf-8"), f"{name!r} still appears in SKILL.md"


@pytest.mark.parametrize("path", REFERENCE_MARKDOWN, ids=lambda p: p.name)
@pytest.mark.parametrize("name", HYBRID_SEARCH_NAMESPACES)
def test_no_reference_names_a_hybrid_search_tool(path, name):
    assert name not in path.read_text(encoding="utf-8"), f"{name!r} still appears in {path.name}"


def test_skill_md_routes_media_search_to_the_media_search_qa_skill():
    assert "media-search-qa" in SKILL_MD.read_text(encoding="utf-8")
