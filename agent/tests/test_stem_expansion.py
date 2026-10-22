"""Regression: audio-stem separation must surface ALL stems, not just one.

The stem job writes speech/music/sfx as direct-child audio_<name>.wav files under the
run dir, but only one uri reaches the render (session 1b42d744 rendered audio_music
alone → a single-track timeline). _expand_m2c_siblings pulls the rest so each stem gets
its own track.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import render.artifacts as R  # noqa: E402

PROXY = "m2cv6-0e0000000002-100000000002-us-west-2-proxy"
JOB = "micro-workflow/audio-stem/0345192f-b4ae-5f7a-a65b-b6b9c4da6cc7"


class _S3:
    def __init__(self, contents):
        self.contents = contents
        self.calls = []

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, **kw):
        self.calls.append({"Prefix": Prefix, "Delimiter": Delimiter})
        return {"Contents": [{"Key": k} for k in self.contents]}


def test_stem_expansion_pulls_all_three_stems(monkeypatch):
    monkeypatch.setattr(R, "_SKILLS_BUCKET", "skills-bkt")
    contents = [f"{JOB}/audio_music.wav", f"{JOB}/audio_sfx.wav", f"{JOB}/audio_speech.wav"]
    s3 = _S3(contents)
    monkeypatch.setattr(R, "_s3_client", lambda: s3)
    music = f"s3://{PROXY}/{JOB}/audio_music.wav"
    got = R._expand_m2c_siblings([music])
    assert set(got) == {music,
                        f"s3://{PROXY}/{JOB}/audio_sfx.wav",
                        f"s3://{PROXY}/{JOB}/audio_speech.wav"}
    # listed the run dir with a delimiter so intermediate subdirs are excluded
    assert s3.calls and s3.calls[0]["Prefix"] == f"{JOB}/" and s3.calls[0]["Delimiter"] == "/"


def test_stem_expansion_excludes_intermediate_subdir_files(monkeypatch):
    monkeypatch.setattr(R, "_SKILLS_BUCKET", "skills-bkt")
    # Delimiter='/' means the listing only returns direct children; the intermediate
    # media_processing/audio_48k_mono.wav lives in a subdir and is not in Contents.
    contents = [f"{JOB}/audio_music.wav", f"{JOB}/audio_speech.wav"]
    monkeypatch.setattr(R, "_s3_client", lambda: _S3(contents))
    got = R._expand_m2c_siblings([f"s3://{PROXY}/{JOB}/audio_music.wav"])
    assert not any("48k_mono" in u for u in got)
    assert len(got) == 2


def test_non_stem_audio_is_not_expanded(monkeypatch):
    monkeypatch.setattr(R, "_SKILLS_BUCKET", "skills-bkt")
    called = []
    monkeypatch.setattr(R, "_s3_client", lambda: called.append(1))
    plain = "s3://some-bucket/artifacts/song.wav"  # not under /audio-stem/
    got = R._expand_m2c_siblings([plain])
    assert got == [plain]
    assert called == [], "no listing for a non-stem, non-diarization url"
