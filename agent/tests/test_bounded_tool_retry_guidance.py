"""A broken tool path must END, not turn into a search for another engine.

Live turn: `crop_video` returned an error twice, and the orchestrator then spent 220s and 26 steps
trying a local ffmpeg, a static ffmpeg download, and finally an M2C fallback job — the user would
rather have had the tool error. These assertions pin the INTENT of the bound in both places that
carry it (the orchestrator prompt and the video-processing skill); `test_prompts_centralized.py`
pins the bytes and would pass just as happily if the guidance were deleted and the hash rebaselined.
"""
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "video-processing" / "SKILL.md"


def _flat(text: str) -> str:
    """Collapse whitespace so an assertion is immune to where a line happens to wrap."""
    return re.sub(r"\s+", " ", text)


def _orchestrator_prompt() -> str:
    from prompts.orchestrator import build_orchestrator_system_prompt  # noqa: PLC0415

    return build_orchestrator_system_prompt("<CATALOG>", "", "b", "p", "u", "m")


def test_orchestrator_bounds_a_broken_tool_path_at_two_errors():
    text = _flat(_orchestrator_prompt())
    assert "TWO ERRORS FROM THE SAME TOOL END THAT PATH" in text
    lowered = text.lower()
    # The two permitted outcomes: the one documented fallback, or report the error.
    assert "take the one fallback the skill documents, once" in lowered
    assert "report the tool's error to the user plainly" in lowered
    # And the one thing never to do, which is what the live turn did.
    assert "never install, download or hunt for a media binary" in lowered


def test_crop_section_bounds_retries_and_names_the_single_fallback():
    text = _flat(SKILL.read_text())
    assert "Two errors end this path" in text
    assert "run that ONE documented fallback once" in text
    assert "scripts/crop_video.py" in text, "the single documented fallback must still be named"
    # No escalation ladder: not an inline ffmpeg cell, not an ffmpeg download, not a managed job.
    assert "no fetching an ffmpeg build to repair a broken tool" in text
    assert "no `process_media` job, which cannot express an arbitrary box" in text


def test_never_section_carries_the_same_bound():
    text = _flat(SKILL.read_text())
    assert "After two errors from the same tool for one request" in text
    assert "never go looking for a different engine to do that tool's job" in text
