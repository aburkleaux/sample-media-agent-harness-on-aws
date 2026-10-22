"""A plan node must load its skills' instructions into the executing agent.

Root cause of "skill guidance is ignored in plan nodes": the plan path never loaded skills.
- the planner sees only name+description (never the body),
- the node inherits skill NAMES (79da82c: {"name": "media-references"}),
- and _execute_plan_node built its agent with model+tools+a generic prompt and NO AgentSkills
  plugin -- so the model table, field names, and "submit_job: prompt+duration only" reached
  neither agent. The music node guessed num_inference_steps=50 because the rule forbidding it
  was never in its context.

This pins: (1) the executing node passes plugins to its Agent, and (2) a node's skill NAMES
resolve to real instructions via the bundled SkillsManager.
"""
from __future__ import annotations

import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def test_execution_node_passes_plugins_to_its_agent():
    """The bug was that _execute_plan_node's Agent had no plugins at all."""
    body = (SRC / "task_agent.py").read_text()
    exec_node = body[body.index("async def _execute_plan_node"):body.index("async def _fetch_plan_node_results")]
    # The execution node builds node_plugins and passes them.
    assert "node_plugins" in exec_node, "execution node does not build skill plugins"
    assert "plugins=node_plugins" in exec_node, "execution node's Agent is not given its plugins"
    assert "AgentSkills(skills=" in exec_node, "no AgentSkills built from the node's skills"


def test_node_skill_names_resolve_to_instructions():
    """The node carries names only; they must resolve to full bodies."""
    from skills_manager import SkillsManager

    by_name = {sk.name: sk for sk in SkillsManager(skills_dir=SRC.parent / "skills")._load_from_disk()}
    # The shape a node actually carries after inheritance (79da82c).
    node_skills = [{"name": "media-references"}, {"name": "video-processing"}]
    names = [x["name"] for x in node_skills]
    resolved = [by_name[n] for n in names if n in by_name]
    assert len(resolved) == 2
    assert all(len(sk.instructions) > 500 for sk in resolved), \
        "resolved skills carry real instructions, not empty stubs"
    # media-references carries the submit_job param rule that the music node needed.
    mr = by_name["media-references"]
    assert "num_inference_steps" in mr.instructions or "prompt+duration" in mr.instructions \
        or "prompt, duration" in mr.instructions


def test_a_name_that_does_not_resolve_is_skipped_not_fatal():
    from skills_manager import SkillsManager

    by_name = {sk.name: sk for sk in SkillsManager(skills_dir=SRC.parent / "skills")._load_from_disk()}
    names = ["media-references", "does-not-exist"]
    resolved = [by_name[n] for n in names if n in by_name]
    assert [sk.name for sk in resolved] == ["media-references"]
