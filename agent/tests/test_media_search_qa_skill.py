"""Unit tests for the built-in ``media-search-qa`` skill package and its loader.

Covers design Component 1 "Test (Req 1.14, 1.17)":

- the two runtimes (the Main_Agent and the Task_Agent) load the *identical* bundled
  skill text, so neither can drift from the other;
- every stable rule ID in :data:`skill_text.MSQ_RULE_IDS` is present in that text;
- the package stays within the bundling/prompt budgets and binds no tools
  (nothing bundled, ``SKILL.md`` under its design cap, no ``tools.json``);
- ``skill_prompt_section`` is injected into the Main_Agent's search ``_prompt_block`` and is
  *absent* from a turn that binds no search tool.

Validates: Requirements 1.14, 1.17

The skill ships at ``agent/skills/media-search-qa``, but ``skills_manager.SKILLS_DIR`` resolves to
``agent/src/skills`` — the two coincide only inside the deploy bundle (where the CDK walker copies
``agent/skills/**`` to ``<bundle>/skills/`` beside the flattened modules). The autouse fixture
points the bundled directory at the shipped location so the real package loads locally.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import skills_manager
from skills_manager import SkillsManager

from project_search import skill_text
from project_search.context import ActiveProject, TurnContext

SHIPPED_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
SKILL_DIR = SHIPPED_SKILLS_DIR / skill_text.SKILL_NAME
SKILL_MD = SKILL_DIR / "SKILL.md"

#: Design cap on the injected SKILL.md body (bounds per-turn prompt cost; Req 1.14).
SKILL_MD_MAX_BYTES = 12_288
#: The reference-bundle budget the loader must respect (Req 1.14).
BUNDLE_MAX_BYTES = 24_576

PID = "p" + "a" * 32


@pytest.fixture(autouse=True)
def _use_shipped_bundle(monkeypatch):
    """Resolve the real shipped package and clear the per-process loader cache around each test."""
    monkeypatch.setattr(skills_manager, "SKILLS_DIR", SHIPPED_SKILLS_DIR)
    skill_text._reset_cache_for_test()
    yield
    skill_text._reset_cache_for_test()


def _turn_context() -> TurnContext:
    return TurnContext(
        turn_id="t1",
        active_project=ActiveProject(id=PID, name="Demo", collection_id=PID, personal=False),
        web_search=False,
        project_access="ok",
    )


def test_package_binds_no_tools_and_fits_the_budgets() -> None:
    """Req 1.14: everything in SKILL.md, nothing bundled, no tools.json, both size caps honored."""
    assert SKILL_MD.is_file(), f"shipped skill package missing at {SKILL_DIR}"
    assert not (SKILL_DIR / "tools.json").exists(), "media-search-qa must bind no tools (no tools.json)"

    loaded = skill_text.load_media_search_qa()
    assert loaded is not None, "the shipped skill must load"
    # No reference files, so the bundle total is 0 and trivially under budget.
    assert loaded.bundled_bytes == 0
    assert loaded.bundled_bytes <= BUNDLE_MAX_BYTES

    assert len(SKILL_MD.read_bytes()) <= SKILL_MD_MAX_BYTES


def test_whole_asset_about_rule_opens_parent_asset() -> None:
    """4c: 'what is X about' must summarize the PARENT asset and open it, not scatter moments.

    Session aa9e30ba answered a whole-video 'do we have a Meridian video, what is it about?' by
    citing an 8-second shot clip (`meridian_shot_10.mp4`) and a reel derivative
    (`meridian_vertical_reel.mp4`) as if they were separate videos, and never opened the source.
    MSQ-C7b tells the model to pick the parent asset, summarize it, and open it with ``show_asset``.
    """
    loaded = skill_text.load_media_search_qa()
    assert loaded is not None
    body = loaded.instructions

    assert "MSQ-C7b" in body, "the whole-asset 'about' rule must be present"
    # The rule must name the parent-asset choice.
    assert "parent asset" in body.lower()
    # 2026-10-11 (session 484a9d5e): the model generalised "open the parent with show_asset" to every moment
    # question, so each search also opened the whole video. show_asset is now for an explicit open/play/show only.
    assert "MSQ-C7c" in body
    c7c = body.split("**MSQ-C7c**", 1)[1].split("\n\n", 1)[0]
    assert "Never call `show_asset`" in c7c and "what is X about" in c7c and "find the moment" in c7c
    assert "ONLY when the user explicitly asks to open, play or show a FILE" in c7c


def test_skill_md_within_byte_cap_after_new_rule() -> None:
    """Adding MSQ-C7b must not push SKILL.md past its per-turn prompt budget (Req 1.14)."""
    assert len(SKILL_MD.read_bytes()) <= SKILL_MD_MAX_BYTES


def test_every_rule_id_is_present_in_the_loaded_text() -> None:
    """Req 1.14: each stable MSQ-* rule ID appears in the loaded instructions."""
    loaded = skill_text.load_media_search_qa()
    assert loaded is not None

    missing = [rule_id for rule_id in skill_text.MSQ_RULE_IDS if rule_id not in loaded.instructions]
    assert not missing, f"SKILL.md is missing rule IDs: {missing}"


def test_both_runtimes_load_identical_instructions() -> None:
    """Req 1.17: the Main_Agent (arm A) and Task_Agent (arm B) load the same bundled copy.

    Both runtimes construct their own ``SkillsManager`` and read only the bundled directory, so a
    user skill of the same name cannot shadow it and the two copies cannot diverge.
    """
    main_skill = SkillsManager().load_bundled(skill_text.SKILL_NAME)
    task_skill = SkillsManager().load_bundled(skill_text.SKILL_NAME)
    assert main_skill is not None and task_skill is not None
    assert main_skill.instructions == task_skill.instructions

    # The loader (used by both arms) produces the same text and version digest either way.
    arm_a = skill_text.load_media_search_qa(SkillsManager())
    skill_text._reset_cache_for_test()
    arm_b = skill_text.load_media_search_qa(SkillsManager())
    assert arm_a is not None and arm_b is not None
    assert arm_a.instructions == arm_b.instructions == main_skill.instructions
    assert arm_a.sha256 == arm_b.sha256


def test_skill_section_injected_into_the_searching_agents_prompt() -> None:
    """Req 1.17: the Main_Agent's arm-A ``_prompt_block`` carries the skill text."""
    from project_search import turn

    header = skill_text.PROMPT_SECTION_HEADER

    for binding in ("search_tool", "search_tool_unavailable"):
        block = turn._prompt_block(_turn_context(), binding)
        assert header in block, f"binding {binding!r} must inject the skill section"
        assert "MSQ-S1" in block


def test_skill_section_absent_from_unbound_turns() -> None:
    """Req 1.17: a turn that binds no search tool carries no skill text."""
    from project_search import turn

    header = skill_text.PROMPT_SECTION_HEADER

    none_block = turn._prompt_block(_turn_context(), None)
    assert header not in none_block
    assert "MSQ-S1" not in none_block


def test_a_web_question_does_not_re_cite_earlier_project_moments() -> None:
    """Staging a4a7e245 (2026-10-21): a web-research answer added 'the moments you found earlier …
    [meridian.mp4 @ 03:58–04:01]', so a web question surfaced a Moments link. Every searching turn's prompt now
    says to cite a project moment only when it answers this turn's question."""
    from project_search import turn

    for binding in ("search_tool", "search_tool_unavailable"):
        block = turn._prompt_block(_turn_context(), binding)
        assert "only when it answers THIS turn's question" in block
        assert "cite web sources only" in block
