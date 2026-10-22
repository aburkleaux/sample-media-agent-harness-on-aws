"""Load the built-in `media-search-qa` skill text and render its system-prompt section
(search-qa-rendering design Component 1, Req 1.14, 1.17).

The `AgentSkills` plugin only makes a skill *available* for on-demand activation; "available" is
not "applied". To make Req 1.17 deterministic, a searching turn receives the skill text in its
system-prompt block directly, in addition to the catalog entry. This module is the single place
that loads the bundled copy (via `SkillsManager.load_bundled`, which reads the bundled dir only so
a user skill cannot shadow it) and turns it into the prompt section used by arm A (`turn.py`) and
arm B (`task_agent.py`). It never injects the text itself and never raises: when the package is
absent, loading returns `None` and the searching turn continues without the section.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from skills_manager import _BUNDLE_MAX_BYTES, _BUNDLE_TEXT_EXTS, SkillsManager

logger = logging.getLogger(__name__)

SKILL_NAME = "media-search-qa"

#: Every stable rule ID the SKILL.md must carry. The loader test asserts each appears in the loaded
#: `instructions`; `turn.py`/`task_agent.py` do not read it. Keep in step with SKILL.md.
MSQ_RULE_IDS: tuple[str, ...] = (
    "MSQ-S1", "MSQ-S2", "MSQ-S3",
    "MSQ-C1", "MSQ-C2", "MSQ-C3", "MSQ-C4", "MSQ-C5", "MSQ-C6", "MSQ-C7", "MSQ-C7a", "MSQ-C7b", "MSQ-C8", "MSQ-C9",
    "MSQ-I1", "MSQ-I1b", "MSQ-I2", "MSQ-I3",
    "MSQ-G1", "MSQ-G2", "MSQ-G3", "MSQ-G4", "MSQ-G5",
    "MSQ-T1", "MSQ-T2",
    "MSQ-D1", "MSQ-D2",
    "MSQ-E1", "MSQ-E2", "MSQ-E3",
    "MSQ-W1",
)

PROMPT_SECTION_HEADER = "\n## Media search and Q&A rules (media-search-qa)\n"


@dataclass(frozen=True)
class LoadedSkill:
    """One loaded copy of the built-in skill, as the model would receive it."""

    name: str
    instructions: str      # SKILL.md body + bundled references, exactly what the model receives
    bundled_bytes: int     # sum of sizes of files _bundle_references would inline (0 for this skill)
    sha256: str            # hex digest of `instructions`; the Req 4.1 "skill version"


# Per-process cache. _CACHE_SENTINEL distinguishes "not yet loaded" from "loaded, absent (None)" so
# an absent package is logged exactly once.
_CACHE_SENTINEL = object()
_cached: object = _CACHE_SENTINEL


def _bundled_reference_bytes(skill_dir: Path) -> int:
    """Sum of sizes of the text reference files `SkillsManager._bundle_references` would inline from
    `skill_dir`, or 0 when they exceed the bundle budget (matching the all-or-nothing rule there)."""
    try:
        root = Path(skill_dir)
        if not root.exists():
            return 0
        total = 0
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.name in ("SKILL.md", "tools.json") or "__pycache__" in p.parts:
                continue
            if p.suffix.lower() not in _BUNDLE_TEXT_EXTS:
                continue
            total += p.stat().st_size
        return total if total <= _BUNDLE_MAX_BYTES else 0
    except Exception:  # noqa: BLE001 - a size estimate must never break loading
        return 0


def load_media_search_qa(manager: Optional[SkillsManager] = None) -> Optional[LoadedSkill]:
    """Load the bundled `media-search-qa` skill as a :class:`LoadedSkill`, cached per process.

    `manager` lets each runtime pass its own `SkillsManager` (so both arms use their bundled copy);
    when omitted a plain `SkillsManager` is constructed, which reads the same bundled directory.
    Returns `None` when the package is absent or cannot be parsed — the searching turn continues
    without the section, and the absence is logged once. Never raises.
    """
    global _cached
    if _cached is not _CACHE_SENTINEL:
        return _cached  # type: ignore[return-value]

    loaded: Optional[LoadedSkill] = None
    try:
        mgr = manager or SkillsManager()
        skill = mgr.load_bundled(SKILL_NAME)
        if skill is None:
            logger.info("load_media_search_qa: '%s' not bundled; searching continues without it", SKILL_NAME)
        else:
            instructions = skill.instructions or ""
            loaded = LoadedSkill(
                name=skill.name,
                instructions=instructions,
                bundled_bytes=_bundled_reference_bytes(mgr._bundled_skills_dir / SKILL_NAME),
                sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            )
    except Exception:  # noqa: BLE001 - loading the built-in text must never break a turn
        logger.warning("load_media_search_qa failed; searching continues without the skill", exc_info=True)
        loaded = None

    _cached = loaded
    return loaded


def skill_prompt_section(skill: Optional[LoadedSkill]) -> str:
    """The system-prompt section for `skill`, or `""` when it is `None`.

    The section header precedes the skill instructions so the static text can sit ahead of the
    per-turn lines and keep the prompt prefix cacheable.
    """
    if skill is None:
        return ""
    return PROMPT_SECTION_HEADER + skill.instructions


def _reset_cache_for_test() -> None:
    """Clear the per-process cache. For tests only."""
    global _cached
    _cached = _CACHE_SENTINEL


# ---- project-knowledge-graph (the graph extension of search) -----------------------------------------

GRAPH_SKILL_NAME = "project-knowledge-graph"
GRAPH_SECTION_HEADER = "\n## Knowledge graph rules (project-knowledge-graph)\n"
_graph_cached: object = _CACHE_SENTINEL


def load_project_knowledge_graph(manager: Optional[SkillsManager] = None) -> Optional[LoadedSkill]:
    """The bundled `project-knowledge-graph` skill, cached per process; `None` when absent. Never raises.

    Bundled-only, like `media-search-qa`, so a user skill of the same name cannot replace the rules that go
    with the graph tools.
    """
    global _graph_cached
    if _graph_cached is not _CACHE_SENTINEL:
        return _graph_cached  # type: ignore[return-value]
    loaded: Optional[LoadedSkill] = None
    try:
        mgr = manager or SkillsManager()
        skill = mgr.load_bundled(GRAPH_SKILL_NAME)
        if skill is not None:
            instructions = skill.instructions or ""
            loaded = LoadedSkill(name=skill.name, instructions=instructions,
                                 bundled_bytes=_bundled_reference_bytes(mgr._bundled_skills_dir / GRAPH_SKILL_NAME),
                                 sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest())
    except Exception:  # noqa: BLE001
        logger.warning("load_project_knowledge_graph failed; graph tools bind without their rules", exc_info=True)
    _graph_cached = loaded
    return loaded


def graph_prompt_section(skill: Optional[LoadedSkill]) -> str:
    return "" if skill is None else GRAPH_SECTION_HEADER + skill.instructions


def _reset_graph_cache_for_test() -> None:
    global _graph_cached
    _graph_cached = _CACHE_SENTINEL
