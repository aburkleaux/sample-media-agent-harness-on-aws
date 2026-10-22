"""Integrated replay of run 863816b4 (the fal 8-shot "animate + crossfade" burn).

Root cause (proven live): the plan handed the fal submit nodes the endpoint id
`fal-ai/minimax/h3-max/image-to-video` -- WITH a `fal-ai/` prefix that MiniMax's
real id does not have (`minimax/h3-max/image-to-video`). fal rejected it
("unavailable for the connected account", read by the model as insufficient_scope),
and the node agent retried the submit 236 times in ONE turn, then the run rolled up
as a false SUCCEEDED.

This pins the two agent-side layers of the fix together against that run's shape
(the third layer, chainer terminal honesty on the provider-access rejection, is
pinned in infra/lib/lambdas/plan-chainer/test_p2_error_retry.py):

  * L1a -- a single internal turn CANNOT loop a paid submit forever: the internal
    execution ceilings are now a finite runaway backstop (was unbounded).
  * L2  -- a focused fal submit node inherits only the skills its tools need, so
    media-references' "MiniMax has no `fal-ai/` prefix" rule is prominent instead
    of buried under all 11 active skills.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import task_agent  # noqa: E402
from task_agent import _select_ceilings  # noqa: E402
from orchestration.plan_and_execute_tool import _relevant_skills  # noqa: E402

# The active skill set on the run (all 11 built-ins were active).
_ACTIVE_863816B4 = [
    "pdf", "agent-harness-help", "content-moderation", "render-media",
    "video-processing", "skill-creator", "media-references", "capability-editor",
    "aws-research", "graph-assistant", "ai-storyboarding",
]
# A shot_animate node's declared tools (fal async trio), gateway-namespaced as on the run.
_SHOT_TOOLS = [
    "usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job",
    "usr-8ddf64af24be45c894a7a8c1f254c83f___check_job",
    "usr-8ddf64af24be45c894a7a8c1f254c83f___get_job_result",
]


def test_L1a_one_internal_turn_cannot_loop_236_submits():
    """The 236-in-one-turn burn is structurally impossible: the internal cycle
    backstop is finite and below the observed runaway."""
    internal = _select_ceilings(is_external=False)
    assert internal["cycles"], "internal cycle ceiling must be a finite backstop, not 0/unbounded"
    assert internal["cycles"] < 236
    assert internal["wall_clock_sec"], "internal wall-clock ceiling must be finite"


def test_L2_shot_node_scopes_to_media_not_all_eleven():
    """Each shot's fal submit node inherits the media skill and sheds the unrelated
    ones -- so the 'no fal-ai/ prefix' rule the run ignored is now prominent."""
    kept = [
        s if isinstance(s, str) else s.get("name")
        for s in _relevant_skills(_ACTIVE_863816B4, _SHOT_TOOLS)
    ]
    assert "media-references" in kept
    # The skills that diluted the node on the real run are gone.
    for buried in ("pdf", "aws-research", "graph-assistant", "capability-editor",
                   "skill-creator", "agent-harness-help"):
        assert buried not in kept
    assert len(kept) <= 5, f"node still over-loaded with skills: {kept}"


def test_L2_crossfade_assembler_still_gets_video_processing():
    """The assemble_crossfade node (ffmpeg in code_interpreter) keeps the combining
    skill -- scoping must not starve the node that actually needs video-processing."""
    kept = [
        s if isinstance(s, str) else s.get("name")
        for s in _relevant_skills(_ACTIVE_863816B4, ["code_interpreter"])
    ]
    assert "video-processing" in kept
