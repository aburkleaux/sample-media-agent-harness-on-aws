"""Pin the render VIEW to the TOOL that produced the output, not its content.

First-principles fix (see docs review): the view a media job renders is known at
dispatch — we chose ``shot-segmentation``, so the view is a shot timeline. Today
the view is *inferred* from the output's JSON shape by ``select_render_skill_by_content``,
which is nondeterministic on structurally-similar outputs: three identical
shot-segmentation jobs in session ``a5a1b4fa`` picked render-image, render-video and
render-shot-segmentation; the shot/diarize nodes in ``a2ebd1e9`` picked render-image /
render-audio. The tool name (federated ``<target>___<tool>``) is unambiguous and is
already parsed for the tab TITLE — this keys the VIEW off the same fact.

These assertions are RED until:
  * each reference skill declares ``tool:`` in its frontmatter, and
  * ``select_render_skill_by_tool`` exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _pick(tool: str):
    from render_agent import select_render_skill_by_tool
    skill = select_render_skill_by_tool(tool)
    return skill["name"] if skill else None


def test_shot_segmentation_tool_selects_the_shot_view():
    # session a2ebd1e9 / a5a1b4fa rendered this as render-image — the exact bug.
    assert _pick("shot-segmentation") == "render-shot-segmentation"


def test_speech_diarization_tool_selects_the_diarization_view():
    # NOTE the name mismatch the tool key resolves: the TOOL is "speech-diarization",
    # the SKILL is "render-speaker-diarization". Content inference rendered this as
    # render-audio (a bare 3-track timeline) instead of the speaker view.
    assert _pick("speech-diarization") == "render-speaker-diarization"


def test_face_recognition_tool_selects_the_boxes_view_not_face_people():
    # The one genuinely ambiguous tool: one face tool, two candidate skills. The
    # boxes-on-video view is the primary; face-people (composite-timeline, no boxes)
    # must not win. Keying by tool makes this a fixed choice, not a score.
    assert _pick("face-recognition") == "render-face-recognition"


def test_federated_prefixed_name_resolves_to_the_target():
    # Real tool names are "<target>___<verb>", e.g. shot-segmentation___get_job_status.
    assert _pick("shot-segmentation___get_job_status") == "render-shot-segmentation"


def test_unknown_tool_returns_none_so_caller_keeps_mime_choice():
    # An unmapped tool must fall back to MIME/generic — labelled generic — never a
    # confidently-wrong specialised view.
    assert _pick("some-unregistered-tool") is None
    assert _pick("") is None


# ---------------------------------------------------------------------------
# GROUND TRUTH: the exact `tool` strings observed in real stored plan
# aggregates (plans/<session>/<plan>/result.json on staging, 2026-08-18).
# Pinning these is what catches a mis-guessed target name — the first version of
# this map declared `audio-stem-separation` (found by grepping the repo) while the
# deployed gateway actually ships `audio-stem`, so stems silently matched nothing.
# Do not edit these strings to make a test pass; they are production values.
# ---------------------------------------------------------------------------
REAL_TOOLS = {
    "shot-segmentation___segment_shots": "render-shot-segmentation",
    "speech-diarization___diarize_speech": "render-speaker-diarization",
    "audio-stem___separate_audio_stems": "render-audio",
}


def test_every_real_observed_tool_resolves_to_a_view():
    for tool, expected in REAL_TOOLS.items():
        assert _pick(tool) == expected, f"{tool!r} must resolve to {expected}"


def test_alias_spellings_both_resolve():
    # One view may serve several target spellings across gateways/versions.
    assert _pick("audio-stem") == "render-audio"
    assert _pick("audio-stem-separation") == "render-audio"
