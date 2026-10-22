"""Regression: face render must use grouped/named people (facegroup.json), not the
ephemeral raw clusters/tracks (36 unnamed 'Person N').

Session abc839ca rendered 36 entities from ephemeral/identity_clusters.json /
ephemeral/face_tracks.json while facegroup.json held the correct 5 people (2 named:
Reid Scott, Kevin Kilner). Two guards: (1) the face-recognition skill subsumes those
ephemeral files, (2) the provider treats an operational DIRECTORY segment (ephemeral/)
as non-renderable so intermediates never reach the render agent.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
FACE_SKILL = os.path.join(REPO, "agent", "skills", "render-media", "references", "face-recognition.md")


def test_real_face_recognition_skill_subsumes_ephemeral_files():
    with open(FACE_SKILL, encoding="utf-8") as f:
        text = f.read()
    line = next((ln for ln in text.splitlines() if ln.strip().lower().startswith("subsumes:")), "")
    assert "identity_clusters.json" in line, "facegroup must subsume the ephemeral identity clusters"
    assert "face_tracks.json" in line, "facegroup must subsume the ephemeral face tracks"


def test_provider_excludes_ephemeral_directory():
    from hooks.provider import _is_renderable
    # ephemeral intermediates are not deliverables even with a clean basename
    assert _is_renderable("micro-workflow/face-recognition/JOB/ephemeral/identity_clusters.json") is False
    assert _is_renderable("micro-workflow/face-recognition/JOB/ephemeral/face_tracks.json") is False
    # the real deliverables still render
    assert _is_renderable("micro-workflow/face-recognition/JOB/facegroup.json") is True
    assert _is_renderable("micro-workflow/media-processing/JOB/proxy/transcoded.mp4") is True
