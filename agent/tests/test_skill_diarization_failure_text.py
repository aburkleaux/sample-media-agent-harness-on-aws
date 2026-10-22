"""The video-processing skill must not let the model turn "no speech" into "too short".

Staging plan-shotdiar-render (2026-10-09): speech-diarization failed a 5.2 s clip with
``No VAD turns found after flattening`` (no speech). The model, seeing only "node failed", reached for the
skill's one duration rule -- "the [mark_in_out] window must exceed 10 seconds" -- and told the user the clip was
"too short to diarize". The rule is about a ``mark_in_out`` window only; the skill now says so and names what the
real error means.
"""
from __future__ import annotations

import pathlib

SKILL = pathlib.Path(__file__).resolve().parents[1] / "skills" / "video-processing" / "SKILL.md"


def test_ten_second_rule_is_scoped_to_mark_in_out_and_vad_error_is_explained():
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "applies ONLY to a `mark_in_out` window" in text
    assert "`No VAD turns found`" in text and "no detectable speech" in text
    assert "not that it is too short" in text
