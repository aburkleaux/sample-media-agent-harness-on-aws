"""CORRECTION: the M2C face outputs DO carry bounding boxes.

An earlier version of this file asserted "no boxes," based on reading only facemetadata.json
(the identity summary, which legitimately has none) and identity_clusters.json. That was wrong.
Reading ALL the face outputs shows the per-detection boxes live in face_tracks.json:

    face_tracks.json -> tracks[].faces[] = {timestamp, frame_key, box:{l,t,w,h}, confidence, ...}

box is normalized {l,t,w,h}; project_boxes already accepts {l,t,w,h}/{x,y,w,h}/{Left,Top,...}.
So boxes ARE renderable. What kept them off screen is selection + which player draws them,
not the data -- that is the F5 work, not a data limit.
"""
from __future__ import annotations

from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / "skills"

# Verbatim from a real face_tracks.json (job 2f5e2a74).
REAL_FACE = {
    "frame_num": 900, "timestamp": 30.03, "frame_key": "frame_0000900.jpg",
    "box": {"l": 0.304877, "t": 0.173639, "w": 0.14083, "h": 0.355808},
    "confidence": 99.95, "gender": "Male",
}


def test_face_detections_carry_a_normalized_box():
    b = REAL_FACE["box"]
    assert set(b) == {"l", "t", "w", "h"}
    assert all(0.0 <= v <= 1.0 for v in b.values()), "boxes are normalized 0..1"


def test_project_boxes_accepts_the_face_tools_ltwh_convention():
    import sys
    sys.path.insert(0, str(SKILLS.parent / "src"))
    from render.projection import project_boxes

    data = {"tracks": [{"faces": [REAL_FACE]}]}
    out = project_boxes(data, "tracks[].faces[] box=box time=timestamp frame=frame_key")
    assert out, "project_boxes produced nothing from a real face box"
    a = out[0]
    assert a["x"] == round(0.304877, 6) and a["y"] == round(0.173639, 6)
    assert a["w"] > 0 and a["h"] > 0


def test_the_skill_no_longer_claims_boxes_are_impossible():
    body = (SKILLS / "render-media" / "references" / "face-recognition.md").read_text()
    assert "carry NO bounding boxes" not in body
    assert "face_tracks.json" in body
    # Must AFFIRM boxes are real/renderable, not a data limit. The reframed declaration states
    # the box view is PRIMARY (preferred whenever geometry is present) and that the old
    # "box path is not chosen" note is outdated. Accept the older affirmations too, so the
    # guard still holds if the body is ever restored to earlier wording.
    assert (
        "no longer true" in body
        or "the boxes are there" in body
        or "Not a data limit" in body
    ), "face-recognition.md must affirm boxes are renderable, not claim they're impossible"
