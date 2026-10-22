"""N slices of ONE source file must not be planned as a fan-out.

Session 27c33c57: "can you clip shots 3-9, and show me every single clip in a gallery view?"
became an 8-node plan. Nothing in code counts clips — the fan-out came entirely from prompt
text ("Running the SAME operation over multiple inputs is itself ONE fan-out request") plus
a frames-only exception that did not cover trims. Each node re-read the whole 216 MB source
and paid its own dispatch; the turn took 7m39s where the slowest single trim was 133s.

These assertions pin the INTENT of the carve-out. `test_prompts_centralized.py` pins the
bytes; it would pass just as happily if the carve-out were deleted and the hash rebaselined.
"""
from pathlib import Path
import re

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _flat(text: str) -> str:
    """Collapse whitespace so an assertion is immune to where a line happens to wrap."""
    return re.sub(r"\s+", " ", text)


def _orchestrator_prompt() -> str:
    from prompts.orchestrator import build_orchestrator_system_prompt  # noqa: PLC0415

    return build_orchestrator_system_prompt("<CATALOG>", "", "b", "p", "u", "m")


def test_orchestrator_exception_covers_ranges_not_only_frames():
    text = _flat(_orchestrator_prompt())
    assert "N SLICES OF ONE SOURCE FILE IS NOT A FAN-OUT" in text
    # The exception must name trimming, not just frame extraction — that gap is the bug.
    lowered = text.lower()
    assert "trimming many clips out of one video" in lowered
    assert "never plan a node per range" in lowered


def test_planning_prompt_excludes_slices_of_one_input_from_fanout():
    from prompts.planning import PLANNING_SYSTEM_PROMPT_TEMPLATE  # noqa: PLC0415

    text = _flat(PLANNING_SYSTEM_PROMPT_TEMPLATE)
    assert "N SLICES OF ONE INPUT FILE" in text
    # It must sit with the other "do NOT fan out" exclusions so it is read alongside them.
    assert text.count("Do NOT fan out when") >= 2
    assert "one code_interpreter ffmpeg pass" in text


def test_video_processing_skill_has_a_multi_range_trim_carve_out():
    text = _flat((SKILLS / "video-processing" / "SKILL.md").read_text())
    assert "TRIMMING N CLIPS OUT OF ONE SOURCE is ONE step" in text
    # The batch must keep the three safety properties the fan-out gave for free.
    assert "build_trim_plan" in text, "positional pairing (silent wrong-length bug)"
    assert "verify_trim" in text, "per-output ffprobe verification"
    assert "per-clip status" in text, "failure isolation across 7 outputs in one script"


def test_split_rule_is_encode_time_not_file_size():
    """The old guard ("well under ~50 MB") would have sent session 27c33c57's 227 MB source to a
    slow managed proxy job first. Measured: that source downloads into the sandbox in 2-3 s; the
    real cost is re-encode time (~0.5x realtime at 1080p)."""
    text = _flat((SKILLS / "video-processing" / "SKILL.md").read_text())
    assert "well under ~50 MB" not in text
    assert "Size is NOT the reason to split" in text
    assert "ENCODE time" in text or "ENCODE TIME" in text
    # Splitting, when it is warranted, is by batches — never back to one node per clip.
    assert "never one node per clip" in text


def test_agent_olive_batch_trim_rule_does_not_generalize_to_one_source():
    """agent-olive's N clips are N distinct WAVs — a real fan-out — but the sentence
    "one process_media per clip" is what a model generalizes from."""
    text = _flat((SKILLS / "agent-olive" / "SKILL.md").read_text())
    assert "N DISTINCT inputs" in text
    assert "does NOT generalize to N ranges of ONE file" in text


def test_ffmpeg_guidance_names_the_exact_key_and_no_public_link():
    """The staged ffmpeg is read by S3 KEY with the sandbox role; the CDN serves no path (403 by design).

    Session 764a71c0 burned 22.9s and 4 model rounds on a node discovering the filename, because the
    guidance said `/tools/ffmpeg/…` and left it to the model — so the exact key stays spelled out.
    """
    text = _flat((SKILLS / "video-processing" / "SKILL.md").read_text())
    assert "key = `tools/ffmpeg/ffmpeg-release-arm64-static.tar.xz`" in text
    assert "NO public or CDN link" in text
    assert "403 by design" in text


def test_even_dimension_rule_is_stated_with_probe_before_encode():
    """1355px panels cannot survive H.264 even-dimension rounding; discovering that cost
    three full encodes of the same video (225s, 65% of the node) in session 764a71c0."""
    text = _flat((SKILLS / "video-processing" / "SKILL.md").read_text())
    assert "EVERY encoded dimension MUST BE EVEN" in text
    assert "ffprobe" in text and "TEST ENCODE" in text
