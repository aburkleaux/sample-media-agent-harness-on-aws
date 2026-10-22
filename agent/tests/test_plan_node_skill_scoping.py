"""A plan/dispatch node inherits only the ACTIVE skills its TOOLS need, not all of them.

Run 863816b4: an 8-shot fal image->video plan's submit nodes each inherited all 11
active skills. media-references' explicit rule -- "MiniMax has no `fal-ai/` prefix"
-- competed with ten unrelated skill bodies (pdf, aws-research, graph-assistant,
capability-editor, skill-creator, agent-harness-help, ...), so the agent emitted the
prefixed id `fal-ai/minimax/h3-max/image-to-video`, which fal rejected as "unavailable
for the connected account". Scoping the inherited set to the node's declared tools makes
the relevant skill prominent.

This is a QUALITY layer; L1a (finite task-agent ceiling) and L1b (chainer terminal
honesty on a provider-access rejection) already make a bad submit safe. So the scoping is
deliberately fail-toward-keeping: unknown skills are always kept, a tool-less node keeps
all active skills (the mux-node inheritance fix), and an empty filter falls back to all.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from orchestration.plan_and_execute_tool import _relevant_skills  # noqa: E402

_ALL = [
    "pdf", "agent-harness-help", "content-moderation", "render-media",
    "video-processing", "skill-creator", "media-references", "capability-editor",
    "aws-research", "graph-assistant", "ai-storyboarding",
]


def test_fal_submit_node_keeps_media_drops_unrelated():
    """The 863816b4 shape: a fal submit node keeps media-references and sheds the
    authoring/meta/unrelated skills that buried it."""
    kept = _relevant_skills(
        _ALL,
        ["usr-8ddf64af___submit_job", "usr-8ddf64af___check_job",
         "usr-8ddf64af___get_job_result"],
    )
    assert "media-references" in kept
    for dropped in ("pdf", "aws-research", "graph-assistant", "capability-editor",
                    "skill-creator", "agent-harness-help"):
        assert dropped not in kept
    # Far fewer than the whole active catalogue -> the relevant rule is prominent.
    assert len(kept) < len(_ALL)


def test_combine_node_keeps_video_processing():
    """A crossfade/mux node runs ffmpeg in code_interpreter -> video-processing is
    exactly the skill it needs (authoritative for all combining)."""
    kept = _relevant_skills(_ALL, ["code_interpreter"])
    assert "video-processing" in kept


def test_tool_less_node_keeps_all_active_mux_node_fix_preserved():
    """A node that declares NO tools can't be scoped, so it keeps every active skill --
    preserving the inheritance fix this whole block was added for (a skills=[] mux node
    that must not run with zero media skills)."""
    assert _relevant_skills(_ALL, []) == _ALL
    assert _relevant_skills(_ALL, None) == _ALL


def test_empty_filter_falls_back_to_all_never_zero():
    """If scoping would remove EVERYTHING, keep all active -- a node never inherits zero."""
    only_gated = [{"name": "pdf"}, {"name": "aws-research"}]
    # neither is relevant to a fal submit tool -> filter empty -> fallback to all
    assert _relevant_skills(only_gated, ["usr___submit_job"]) == only_gated


def test_unknown_skill_is_always_kept():
    """A skill not in the relevance map is broadly useful -> always inherited (no regression
    when a new skill is added without a mapping)."""
    active = ["media-references", "some-new-unmapped-skill"]
    kept = _relevant_skills(active, ["code_interpreter"])  # media-references not relevant here
    assert "some-new-unmapped-skill" in kept  # unmapped -> kept


def test_preserves_object_identity_and_dict_form():
    """Returns the original skill objects (strings or dicts) unchanged."""
    active = [{"name": "media-references", "instructions": "body"}, {"name": "pdf"}]
    kept = _relevant_skills(active, ["usr___submit_job"])
    assert kept[0] is active[0]  # same dict object, not a copy
    assert all(isinstance(s, dict) for s in kept)
