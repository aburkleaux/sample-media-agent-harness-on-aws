"""Skill selection must follow the payload, not the file extension.

Every media analysis tool emits ``application/json``, so selecting on MIME
returns whichever JSON-handling skill sorts first in discovery order. That is
how a shot-segmentation result selected the face skill and rendered as a grid
of stills instead of markers over the video.

The payload shapes below are the real ones, read from a live shot-segmentation
job (execution cbf3d066-76a3-54be-a1c9-0d4f74f72908) and the face/diarization
results from the same session, so a change in the declarations that stops
matching production data fails here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent  # noqa: E402
import render.projection as proj  # noqa: E402


# --- Real payload excerpts -------------------------------------------------

SHOTS_JSON = {
    "uuid": "cbf3d066-76a3-54be-a1c9-0d4f74f72908",
    "n_shots": 15,
    "actual_duration_sec": 60.026689,
    "actual_frame_count": 1799,
    "shot_boundary_threshold": 0.35,
    "shots": [
        {"start_frame": 0, "end_frame": 29, "start_time": 0.0,
         "end_time": 0.967633, "shot_type": "black", "shot_id": 0,
         "frame_count": 30, "selected_frames": [5, 19, 28, 29]},
        {"start_frame": 30, "end_frame": 119, "start_time": 1.001,
         "end_time": 4.004, "shot_type": "content", "shot_id": 1,
         "frame_count": 90, "selected_frames": [40, 80]},
        {"start_frame": 120, "end_frame": 300, "start_time": 4.004,
         "end_time": 10.01, "shot_type": "content", "shot_id": 2,
         "frame_count": 181, "selected_frames": [150]},
    ],
}

RECURRENCE_JSON = {
    "uuid": "cbf3d066-76a3-54be-a1c9-0d4f74f72908",
    "total_groups": 3,
    "total_recurring_shots": 7,
    "actual_duration_sec": 60.026689,
    "recurrence_groups": [{"group_id": 0, "shot_ids": [1, 4, 9]}],
}

DIARIZATION_JSON = {
    "num_global_speakers": 2,
    "num_sentences": 2,
    "language": "en",
    "sentences": [
        {"id": 0, "text": "Hello there.", "start": 0.5, "end": 3.2, "speaker_id": "SPEAKER_00"},
        {"id": 1, "text": "General Kenobi.", "start": 3.4, "end": 7.9, "speaker_id": "SPEAKER_01"},
    ],
}

FACE_METADATA_JSON = {
    "duration_sec": 60.059,
    "stats": {"num_people": 5, "num_tracks": 12, "faces_detected": 99,
              "frames_analyzed": 1799},
    "people": [
        {"name": "Kevin Kilner", "gender": "male", "confidence": 99.94,
         "thumbnail_key": "faces/p0.jpg", "num_tracks": 4,
         "total_screen_time_sec": 18.2, "name_source": "celebrity",
         "age_range": "35-52",
         "appearances": [{"begin_ms": 1000, "end_ms": 4000}]},
    ],
}

FACE_TRACKS_JSON = {
    "num_faces": 99,
    "num_tracks": 12,
    "tracks": [
        {"track_id": 0, "first_ts": 1.0, "last_ts": 4.0,
         "faces": [{"box": [0.1, 0.2, 0.3, 0.4], "timestamp": 1.0,
                    "gender": "male", "frame_key": "frames/f1.jpg"}]},
    ],
    "clusters": [{"face_group_id": 0, "celebrity": "Kevin Kilner",
                  "track_ids": [0]}],
}


def _name(skill):
    return skill.get("name") if skill else None


# --- Selection -------------------------------------------------------------

class TestSelectByContent:
    def test_shots_payload_selects_shot_segmentation(self):
        """The regression: this used to resolve to the face skill via MIME."""
        picked = render_agent.select_render_skill_by_content(
            {"shots.json": SHOTS_JSON, "recurrence.json": RECURRENCE_JSON})
        assert _name(picked) == "render-shot-segmentation"

    def test_shot_skill_declares_a_marker_timeline(self):
        picked = render_agent.select_render_skill_by_content({"s": SHOTS_JSON})
        assert picked["template"] == "nle-timeline"
        assert (picked.get("declaration") or {}).get("markers")

    def test_diarization_payload_selects_diarization_skill(self):
        picked = render_agent.select_render_skill_by_content(
            {"diarization.json": DIARIZATION_JSON})
        assert _name(picked) == "render-speaker-diarization"

    def test_face_payload_still_selects_face_people(self):
        """A face result matches both `people` and `tracks`; the richer
        declaration must keep winning so the overlay view is unchanged."""
        picked = render_agent.select_render_skill_by_content(
            {"facemetadata.json": FACE_METADATA_JSON,
             "face_tracks.json": FACE_TRACKS_JSON})
        assert _name(picked) == "render-face-people"

    def test_unrecognised_payload_defers_to_mime(self):
        """None means "keep the extension-based choice", not "render nothing"."""
        assert render_agent.select_render_skill_by_content(
            {"whatever.json": {"totally": "unrelated"}}) is None

    def test_empty_and_malformed_sidecars_are_safe(self):
        assert render_agent.select_render_skill_by_content({}) is None
        assert render_agent.select_render_skill_by_content(
            {"a.json": None, "b.json": "text", "c.json": 7}) is None

    def test_selection_never_returns_a_detectless_skill(self):
        """Skills without `detect` always "apply" and would match anything."""
        picked = render_agent.select_render_skill_by_content({"s": SHOTS_JSON})
        assert (picked.get("declaration") or {}).get("detect")


# --- Projection ------------------------------------------------------------

class TestShotMarkerProjection:
    def _decl(self):
        skill = render_agent.select_render_skill_by_content({"s": SHOTS_JSON})
        return skill["declaration"]

    def test_every_shot_becomes_a_marker(self):
        markers = proj.project_markers(SHOTS_JSON, self._decl()["markers"])
        assert len(markers) == len(SHOTS_JSON["shots"])

    def test_markers_carry_span_label_and_category(self):
        markers = proj.project_markers(SHOTS_JSON, self._decl()["markers"])
        first = markers[0]
        assert first["time"] == 0.0
        assert first["endTime"] == 0.968  # projector rounds to 3dp
        assert str(first["label"]) == "0"
        assert first["category"] == "black"
        assert {m["category"] for m in markers} == {"black", "content"}

    def test_duration_comes_from_the_measured_field(self):
        # Rounded to 3dp by the projector, matching the live manifests.
        assert proj.project_duration(
            SHOTS_JSON, self._decl()["duration"]) == 60.027

    def test_markers_are_ordered_by_time(self):
        markers = proj.project_markers(SHOTS_JSON, self._decl()["markers"])
        times = [m["time"] for m in markers]
        assert times == sorted(times)
