"""The audio-stem input must not become its own render tab.

Reported from a live session: two tabs both titled "Audio Stem" -- one with the three
separated WAVs, one showing a 2:00 player. 2:00 is 120.02s, the duration of
`data.audio_key` (audio_48k_mono.wav), which is what the tool was GIVEN.

extract_source_uris classified source media by token (`input`, `source`, `asset`,
`original`, `src`). `audio_key` carries none of them, so the input was not recognised
as source, fell through to the artifact scrape, and rendered as a deliverable.

The resolver already had this rule via _INPUT_KEYS. Only the scrape path -- used when
a tool result is read directly rather than through the plan aggregate -- was missing it,
which is why the aggregate replay looked correct while the live session did not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The real shape, from the live stem job.
STEM_RESULT = {
    "data": {
        "audio_key": "micro-workflow/audio-stem/9270/media_processing/audio_48k_mono.wav",
        "audio_bucket": "mediaworks-proxy-100000000002-us-west-2",
        "audiostem_sep_output": {
            "speech_key": "micro-workflow/audio-stem/9270/audio_speech.wav",
            "music_key": "micro-workflow/audio-stem/9270/audio_music.wav",
            "sfx_key": "micro-workflow/audio-stem/9270/audio_sfx.wav",
        },
        "usage_tracking": {
            "audio_key": "micro-workflow/audio-stem/9270/media_processing/audio_48k_mono.wav",
        },
    },
    "output": {"s3uri": "s3://mediaworks-proxy-100000000002-us-west-2/micro-workflow/audio-stem/9270"},
}


def _result(payload: dict) -> dict:
    return {"content": [{"text": json.dumps(payload)}]}


def test_bare_audio_key_is_treated_as_source_not_output():
    from hooks.provider import extract_source_uris

    payload = {
        "audio_key": "s3://bkt/wf/exec/media_processing/audio_48k_mono.wav",
        "audiostem_sep_output": {
            "speech_key": "s3://bkt/wf/exec/audio_speech.wav",
        },
    }
    sources = extract_source_uris(_result(payload))
    assert any(u.endswith("audio_48k_mono.wav") for u in sources), sources
    # The product must NOT be claimed as source -- that would hide the real result.
    assert not any(u.endswith("audio_speech.wav") for u in sources), sources


def test_the_token_rule_still_matches_explicit_source_keys():
    """The original rule must keep working; this widens it, not replaces it."""
    from hooks.provider import extract_source_uris

    for key in ("input_s3_uri", "source_video", "asset_s3uri", "original_key", "src_uri"):
        got = extract_source_uris(_result({key: "s3://bkt/a/original.mp4"}))
        assert got == ["s3://bkt/a/original.mp4"], (key, got)


def test_a_bare_key_naming_a_non_media_file_is_ignored():
    """Only media extensions count, so a JSON sidecar cannot become a source player."""
    from hooks.provider import extract_source_uris

    assert extract_source_uris(_result({"audio_key": "s3://bkt/a/metrics.json"})) == []
