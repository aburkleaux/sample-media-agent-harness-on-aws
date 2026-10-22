"""Declaration-driven projection of analysis JSON onto template props.

Fixtures are the REAL shapes read out of S3 from live tool runs, not invented:
shots.json from shot-segmentation, global_speakers.json from speech-diarization,
face_tracks.json / identity_clusters.json from face-recognition.

The point of these tests is that adding a tool means adding a SKILL.md
declaration, not editing projector code — so they exercise the generic engine
through declarations rather than testing per-tool branches.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from render.projection import (  # noqa: E402
    declaration_applies, parse_spec, resolve_path,
    project_markers, project_segments, project_boxes, project_rows, project_lanes,
    project_boxes_by_entity,
    project_duration, project_summary, to_percent_boxes,
)

SHOTS = {
    "n_shots": 9, "actual_duration_sec": 30.03, "actual_frame_count": 900,
    "shots": [
        {"shot_id": 0, "start_time": 0.0, "end_time": 0.967633, "shot_type": "black"},
        {"shot_id": 1, "start_time": 1.001, "end_time": 5.172, "shot_type": "content"},
        {"shot_id": 2, "start_time": 5.205, "end_time": 9.176, "shot_type": "content"},
    ],
}

SPEAKERS = {
    "language": "en", "language_confidence": 0.983,
    "num_global_speakers": 2, "num_sentences": 3,
    "sentences": [
        {"id": 0, "text": "So these guys just disappeared?", "start": 26.978,
         "end": 29.318, "speaker_id": "SPEAKER_00"},
        {"id": 1, "text": "Yeah, on that stretch of road.", "start": 30.018,
         "end": 34.015, "speaker_id": "SPEAKER_01"},
        {"id": 2, "text": "With the big rock.", "start": 34.5,
         "end": 36.0, "speaker_id": "SPEAKER_00"},
    ],
}

FACE_TRACKS = {
    "tracks": [
        {"track_id": 0, "first_ts": 30.03, "last_ts": 30.03, "gender": "Male",
         "appearance_count": 1,
         "faces": [{"frame_num": 900, "timestamp": 30.03, "confidence": 99.95,
                    "box": {"l": 0.304877, "t": 0.173639, "w": 0.14083, "h": 0.355808}}]},
        {"track_id": 1, "first_ts": 31.031, "last_ts": 32.5, "gender": "Female",
         "appearance_count": 2,
         "faces": [{"timestamp": 31.031, "box": {"l": 0.1, "t": 0.2, "w": 0.3, "h": 0.4}},
                   {"timestamp": 32.5, "box": {"l": 0.11, "t": 0.21, "w": 0.3, "h": 0.4}}]},
    ]
}

CLUSTERS = {
    "clusters": [
        {"face_group_id": 0, "track_ids": [0, 1, 2],
         "celebrity": {"name": "Kevin Kilner", "confidence": 99.94241973876953}},
        {"face_group_id": 1, "track_ids": [7], "celebrity": None},
    ]
}

# v6 top-level deliverables (read from a real face-recognition job). facegroup.json
# nests per-face boxes under people[].tracks[].faces[]; face_clusters.json is the
# 2D similarity layout per identity. The old face_tracks.json/identity_clusters.json
# are now internal ephemeral/ files and are no longer delivered to the renderer.
FACEGROUP = {
    "version": 1,
    "people": [
        {"person_id": 0, "name": "Kevin Kilner", "named": True, "name_source": "celebrity",
         "confidence": 99.94, "gender": "Male", "age_range": [45, 53],
         "total_screen_time_sec": 8.8, "num_tracks": 2,
         "thumbnail_key": "debug/people/person_0000.jpg",
         "appearances": [{"begin_ms": 30030, "end_ms": 36070, "representative_frame": 900}],
         "tracks": [
             {"track_id": 0, "centroid_idx": 0, "faces": [
                 {"frame_num": 900, "timestamp": 30.03, "frame_key": "frame_0000900.jpg",
                  "box": {"l": 0.304877, "t": 0.173639, "w": 0.14083, "h": 0.355808},
                  "confidence": 99.95, "gender": "Male"}]},
             {"track_id": 1, "centroid_idx": 1, "faces": [
                 {"frame_num": 1380, "timestamp": 46.0, "frame_key": "frame_0001380.jpg",
                  "box": {"l": 0.31, "t": 0.18, "w": 0.14, "h": 0.35},
                  "confidence": 99.9, "gender": "Male"}]}]},
        {"person_id": 1, "name": "Reid Scott", "named": True, "name_source": "celebrity",
         "confidence": 99.99, "gender": "Male", "age_range": [38, 46],
         "total_screen_time_sec": 8.8, "num_tracks": 1,
         "thumbnail_key": "debug/people/person_0001.jpg",
         "appearances": [{"begin_ms": 37000, "end_ms": 45000, "representative_frame": 1110}],
         "tracks": [
             {"track_id": 2, "centroid_idx": 2, "faces": [
                 {"frame_num": 1110, "timestamp": 37.0, "frame_key": "frame_0001110.jpg",
                  "box": {"l": 0.5, "t": 0.2, "w": 0.12, "h": 0.3},
                  "confidence": 99.8, "gender": "Male"}]}]},
    ],
}

FACE_CLUSTERS = {
    "people": [
        {"person_id": 0, "name": "Kevin Kilner", "coord": [-1.2, 0.3],
         "num_tracks": 2, "thumbnail_key": "debug/people/person_0000.jpg"},
        {"person_id": 1, "name": "Reid Scott", "coord": [1.1, -0.2],
         "num_tracks": 1, "thumbnail_key": "debug/people/person_0001.jpg"},
    ]
}


class TestSpecGrammar:
    def test_parses_path_and_fields(self):
        path, f = parse_spec("shots[] time=start_time end=end_time label=shot_id")
        assert path == "shots[]"
        assert f == {"time": "start_time", "end": "end_time", "label": "shot_id"}

    def test_flatmaps_nested_lists(self):
        faces = resolve_path(FACE_TRACKS, "tracks[].faces[]")
        # 1 face on track 0 + 2 on track 1
        assert isinstance(faces, list) and len(faces) == 3

    def test_missing_path_is_none_not_an_error(self):
        assert resolve_path(SHOTS, "nope.not.here") is None


class TestDetect:
    def test_detect_disambiguates_same_mime_sidecars(self):
        # every one of these is application/json; `detect` is what tells them apart
        assert declaration_applies(SHOTS, {"detect": "shots"}) is True
        assert declaration_applies(SPEAKERS, {"detect": "shots"}) is False
        assert declaration_applies(SPEAKERS, {"detect": "sentences"}) is True
        assert declaration_applies(FACE_TRACKS, {"detect": "tracks, clusters"}) is True
        assert declaration_applies(SHOTS, {"detect": "tracks, clusters"}) is False

    def test_no_detect_always_applies(self):
        assert declaration_applies(SHOTS, {}) is True


class TestShotSegmentation:
    SPEC = "shots[] time=start_time end=end_time label=shot_id category=shot_type"

    def test_every_shot_becomes_a_marker(self):
        m = project_markers(SHOTS, self.SPEC)
        assert len(m) == 3, "a 9-shot clip must not render as one marker"
        assert m[0]["time"] == 0.0 and m[0]["endTime"] == 0.968
        assert m[0]["category"] == "black" and m[1]["category"] == "content"

    def test_authoritative_duration_beats_the_generic_hint(self):
        assert project_duration(SHOTS, "actual_duration_sec") == 30.03

    def test_summary_surfaces_the_headline_numbers(self):
        s = project_summary(SHOTS, "n_shots, actual_duration_sec, actual_frame_count")
        assert s == {"n_shots": 9, "actual_duration_sec": 30.03, "actual_frame_count": 900}


class TestSpeakerDiarization:
    def test_turns_become_segments_grouped_by_speaker(self):
        segs = project_segments(SPEAKERS, "sentences[] start=start end=end group=speaker_id")
        assert len(segs) == 3
        assert {s["group"] for s in segs} == {"SPEAKER_00", "SPEAKER_01"}
        # two turns by the same speaker share a lane
        assert [s["group"] for s in segs].count("SPEAKER_00") == 2
        assert segs[0]["start"] == 26.978 and segs[0]["end"] == 29.318

    def test_zero_length_and_inverted_turns_are_dropped(self):
        bad = {"sentences": [{"start": 5, "end": 5, "speaker_id": "A"},
                             {"start": 9, "end": 2, "speaker_id": "B"}]}
        assert project_segments(bad, "sentences[] start=start end=end group=speaker_id") == []


class TestFaceRecognition:
    def test_normalized_ltwh_boxes_project_to_xywh(self):
        boxes = project_boxes(FACE_TRACKS, "tracks[].faces[] box=box time=timestamp label=gender")
        assert len(boxes) == 3, "must walk faces of every track"
        b = boxes[0]
        assert b["x"] == 0.304877 and b["y"] == 0.173639
        assert b["w"] == 0.14083 and b["h"] == 0.355808
        assert b["time"] == 30.03

    def test_alternate_box_conventions_need_no_code_change(self):
        for box in ({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                    {"Left": 0.1, "Top": 0.2, "Width": 0.3, "Height": 0.4}):
            d = {"items": [{"box": box}]}
            out = project_boxes(d, "items[] box=box")
            assert len(out) == 1 and out[0]["x"] == 0.1, box

    def test_degenerate_boxes_are_dropped(self):
        d = {"items": [{"box": {"l": 0, "t": 0, "w": 0, "h": 0.5}},
                       {"box": "not-a-dict"}, {"nobox": 1}]}
        assert project_boxes(d, "items[] box=box") == []

    def test_tracks_become_an_appearance_timeline(self):
        segs = project_segments(FACE_TRACKS, "tracks[] start=first_ts end=last_ts name=track_id")
        # track 0 is a single instant (first_ts == last_ts) so it is not a span
        assert len(segs) == 1
        assert segs[0]["start"] == 31.031 and segs[0]["end"] == 32.5

    def test_identity_clusters_become_a_people_table(self):
        tbl = project_rows(CLUSTERS, "clusters[] cols=face_group_id,celebrity,track_ids")
        assert [c["key"] for c in tbl["columns"]] == ["face_group_id", "celebrity", "track_ids"]
        assert len(tbl["rows"]) == 2
        # a nested celebrity object collapses to its name for display
        assert tbl["rows"][0]["celebrity"] == "Kevin Kilner"
        # a short list of ids is shown inline; only long/nested lists collapse
        # to a count, since [0, 1, 2] is more use than "3"
        assert tbl["rows"][0]["track_ids"] == "0, 1, 2"

    def test_table_falls_back_to_scalar_columns(self):
        tbl = project_rows(FACE_TRACKS, "tracks[]")
        keys = [c["key"] for c in tbl["columns"]]
        assert "track_id" in keys and "faces" not in keys


class TestRobustness:
    def test_empty_and_malformed_inputs_return_empty_not_raise(self):
        for data in ({}, {"shots": "nope"}, {"shots": [1, 2, 3]}, None):
            assert project_markers(data, "shots[] time=start_time") == []
        assert project_rows({}, "clusters[]") == {"columns": [], "rows": []}
        assert project_duration({}, "actual_duration_sec") is None

    def test_markers_without_a_time_field_are_skipped(self):
        d = {"shots": [{"shot_id": 1}, {"shot_id": 2, "start_time": 3.0}]}
        m = project_markers(d, "shots[] time=start_time label=shot_id")
        assert len(m) == 1 and m[0]["time"] == 3.0


class TestCorrelationUsesSkillDeclarations:
    """End-to-end: sidecar URLs -> skill match -> projected props.

    Exercises the real skills on disk, so a malformed SKILL.md fails here rather
    than silently rendering nothing on staging.
    """

    def _patch_fetch(self, monkeypatch, mapping):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: mapping.get(url))
        return ra

    def test_shots_sidecar_yields_markers_and_real_duration(self, monkeypatch):
        ra = self._patch_fetch(monkeypatch, {"s3://b/embeddings/shots.json": SHOTS})
        got = ra._correlate_sidecars(["s3://b/embeddings/shots.json"])
        assert len(got["markers"]) == 3
        assert got["duration"] == 30.03
        assert got["summary"]["n_shots"] == 9
        assert "render-shot-segmentation" in got["skills"]

    def test_speakers_sidecar_yields_grouped_segments(self, monkeypatch):
        ra = self._patch_fetch(monkeypatch, {"s3://b/global_speakers.json": SPEAKERS})
        got = ra._correlate_sidecars(["s3://b/global_speakers.json"])
        assert len(got["segments"]) == 3
        assert {s["group"] for s in got["segments"]} == {"SPEAKER_00", "SPEAKER_01"}
        assert got["summary"]["num_global_speakers"] == 2
        assert "render-speaker-diarization" in got["skills"]

    def test_face_sidecars_yield_boxes_and_a_people_table(self, monkeypatch):
        ra = self._patch_fetch(monkeypatch, {
            "s3://b/facegroup.json": FACEGROUP,
            "s3://b/face_clusters.json": FACE_CLUSTERS,
        })
        got = ra._correlate_sidecars(["s3://b/facegroup.json", "s3://b/face_clusters.json"])
        assert len(got["annotations"]) == 3, "per-face boxes across all tracks"
        assert got["tables"] and got["tables"][0]["rows"][0]["name"] == "Kevin Kilner"
        assert "render-face-recognition" in got["skills"]

    def test_unrelated_json_matches_nothing(self, monkeypatch):
        ra = self._patch_fetch(monkeypatch, {"s3://b/probe.json": {"format": "mp4", "codec": "h264"}})
        got = ra._correlate_sidecars(["s3://b/probe.json"])
        assert got["markers"] == [] and got["segments"] == [] and got["skills"] == []

    def test_no_sidecars_is_a_cheap_noop(self, monkeypatch):
        import render_agent as ra
        got = ra._correlate_sidecars([])
        assert got["markers"] == [] and got["duration"] is None

    def test_a_failed_fetch_does_not_break_the_render(self, monkeypatch):
        ra = self._patch_fetch(monkeypatch, {})   # every fetch returns None
        got = ra._correlate_sidecars(["s3://b/shots.json"])
        assert got["skills"] == []


class TestManifestSelectsFromTheVettedInventory:
    """The builder must pick a template that RendererRegistry actually registers.

    Emitting an unregistered name renders nothing, so selection is closed over the
    inventory. These assert the three previously-broken cases now select the
    specialised template rather than degrading to a generic one.
    """

    REGISTERED = {
        "video-markers", "audio-waveform", "image-annotated", "chart-interactive",
        "code-artifact", "data-table", "markdown-rich", "nle-timeline",
        "composite-timeline", "document-viewer", "manifest-router",
        "video-annotations", "generic-preview",
    }

    def _build(self, monkeypatch, urls, sidecars):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: sidecars.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')
        return ra._build_media_manifest(urls, "mid-test", {})

    def test_shot_segmentation_selects_video_markers_with_all_shots(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/proxy/segment_0000.mp4", "https://x/embeddings/shots.json"],
                        {"https://x/embeddings/shots.json": SHOTS})
        assert m["template"] == "nle-timeline"
        assert m["template"] in self.REGISTERED
        assert len(m["markers"]) == 3, "shots must not collapse to one"
        # authoritative duration replaces the 600s hint
        assert m["duration"] == 30.03
        assert m["metadata"]["n_shots"] == 9
        assert "render-shot-segmentation" in m["renderSkill"]

    def test_diarization_selects_composite_timeline_with_speaker_lanes(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/audio_16k_mono.wav", "https://x/global_speakers.json"],
                        {"https://x/global_speakers.json": SPEAKERS})
        assert m["template"] == "composite-timeline"
        assert m["template"] in self.REGISTERED
        assert {s["group"] for s in m["segments"]} == {"SPEAKER_00", "SPEAKER_01"}
        assert m["metadata"]["num_global_speakers"] == 2

    def test_faces_with_source_video_selects_the_timeline_container(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/proxy/transcoded.mp4",
                         "https://x/facegroup.json", "https://x/face_clusters.json"],
                        {"https://x/facegroup.json": FACEGROUP,
                         "https://x/face_clusters.json": FACE_CLUSTERS})
        # Folded: boxes render in the ONE timeline container, which draws them on the frame AND
        # puts each appearance on the shared time gutter. The point of this test is unchanged —
        # a face payload must not degrade to generic-preview, and its boxes/table must survive.
        assert m["template"] == "nle-timeline", "must not degrade to generic-preview"
        assert len(m["annotations"]) == 3
        assert m["tables"][0]["rows"][0]["name"] == "Kevin Kilner"

    def test_plain_media_still_renders_as_a_timeline(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4"], {})
        assert m["template"] == "nle-timeline"
        assert m.get("markers") is None and m.get("segments") is None

    def test_sidecars_are_not_reported_as_unsupported(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/a.mp4", "https://x/embeddings/shots.json"],
                        {"https://x/embeddings/shots.json": SHOTS})
        assert "shots.json" not in json.dumps(m.get("errors") or [])
        assert len(m["tracks"]) == 1


class TestDurationPrecedence:
    """measured > tool-reported > generic hint.

    The first attempt at this took max(sidecar, track_end), but a track end that
    came from the 600s hint is not a measurement — so the fake value won and a 30s
    clip still drew on a 600s timeline.
    """

    def _build(self, monkeypatch, urls, sidecars, peak_dur=None):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: sidecars.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        pk = json.dumps({"peaks": [], **({"duration": peak_dur} if peak_dur else {})})
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: pk)
        return ra._build_media_manifest(urls, "mid", {})

    def test_tool_reported_duration_beats_the_hint(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4", "https://x/shots.json"],
                        {"https://x/shots.json": SHOTS})
        assert m["duration"] == 30.03
        # and the hinted track is clamped, not left at 600
        assert all(t["end"] == 30.03 for t in m["tracks"])

    def test_a_real_measurement_beats_the_tool_report(self, monkeypatch):
        # a 45s measured wav alongside a sidecar claiming 30.03 keeps the longer
        # real value so no audio is cut off
        m = self._build(monkeypatch, ["https://x/a.wav", "https://x/shots.json"],
                        {"https://x/shots.json": SHOTS}, peak_dur=45.0)
        assert m["duration"] == 45.0

    def test_hint_only_when_nothing_is_known(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4"], {})
        assert m["duration"] == 600.0

    def test_private_hint_flag_never_leaks_into_the_manifest(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4", "https://x/shots.json"],
                        {"https://x/shots.json": SHOTS})
        assert "_endIsHint" not in json.dumps(m)


class TestAnalysisOnlyResults:
    """Tools that emit no media must still render their findings.

    Face recognition returns only JSON, so the builder's `if not tracks` guard
    returned UNSUPPORTED_FORMAT — the richest result in the pipeline (93 tracks,
    per-face boxes, 5 named people) surfaced as an unsupported-format notice.
    """

    def _build(self, monkeypatch, urls, sidecars):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: sidecars.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: "{}")
        return ra._build_media_manifest(urls, "mid", {})

    def test_json_only_faces_render_the_people_table(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/facegroup.json", "https://x/face_clusters.json"],
                        {"https://x/facegroup.json": FACEGROUP,
                         "https://x/face_clusters.json": FACE_CLUSTERS})
        assert m["template"] == "data-table"
        assert m.get("errors") is None, "must not degrade to UNSUPPORTED_FORMAT"
        assert m["rows"][0]["name"] == "Kevin Kilner"
        # the appearance timeline and the boxes both survive
        assert m["segments"] and m["annotations"]
        assert any("source video" in n for n in m["notes"])

    def test_json_only_segments_render_a_timeline(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/global_speakers.json"],
                        {"https://x/global_speakers.json": SPEAKERS})
        assert m["template"] == "composite-timeline"
        assert len(m["segments"]) == 3
        # With no media and no stated media duration, the spans are all we know.
        # total_speech_sec is NOT used: it is the sum of turns, not the recording
        # length, so it would compress the timeline.
        assert m["duration"] == 36.0, "duration derives from the projected spans"

    def test_unrecognised_json_still_errors(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/probe.json"],
                        {"https://x/probe.json": {"codec": "h264"}})
        assert m["errors"][0]["code"] == "UNSUPPORTED_FORMAT", \
            "an empty view is worse than saying nothing is renderable"

    def test_artifacts_are_downloadable_from_an_analysis_render(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/facegroup.json"],
                        {"https://x/facegroup.json": FACEGROUP})
        assert m["artifacts"], "the raw findings must stay reachable"


class TestInvokePreservesProjections:
    """The invoke path must not undo the builder's work.

    Two defects lived here: `_build_media_manifest(media_urls, ...)` was called
    with media ONLY, so sidecars never reached correlation, and the next line
    unconditionally assigned `manifest["template"]`, clobbering the specialised
    template the builder had just chosen from the data.
    """

    def _invoke(self, monkeypatch, urls, sidecars):
        import asyncio
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: sidecars.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')

        async def _boom(*a, **k):
            raise AssertionError("LLM enrichment must not run when a declaration matched")
        monkeypatch.setattr(ra, "_populate_from_sidecars_llm", _boom)

        async def run():
            out = []
            payload = {"prompt": "Render: " + " ".join(urls), "manifestId": "mid"}
            async for chunk in ra._render_deterministic(payload, None):
                out.append(chunk)
            return out

        chunks = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run())
        for c in chunks:
            try:
                obj = json.loads(c)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("tracks") is not None and obj.get("template"):
                return obj
            if isinstance(obj, dict) and obj.get("type") == "manifest":
                return obj.get("manifest") or obj
        return None

    def test_sidecars_reach_the_builder_through_invoke(self, monkeypatch):
        import render_agent as ra
        captured = {}
        real = ra._build_media_manifest

        def spy(urls, mid, opts, **kw):
            captured["urls"] = list(urls)
            captured["prefetched"] = sorted((kw.get("prefetched") or {}).keys())
            return real(urls, mid, opts, **kw)
        monkeypatch.setattr(ra, "_build_media_manifest", spy)
        self._invoke(monkeypatch,
                     ["https://x/a.mp4", "https://x/shots.json"],
                     {"https://x/shots.json": SHOTS})
        assert "urls" in captured, "the media builder was never reached"
        assert any(u.endswith("shots.json") for u in captured["urls"]), \
            "sidecars must be passed in, not filtered out before correlation"

    def test_projected_template_survives_invoke(self, monkeypatch):
        # `_populate_from_sidecars_llm` is patched to raise in _invoke, so this
        # also proves the LLM round-trip is skipped once a declaration matched.
        m = self._invoke(monkeypatch,
                         ["https://x/proxy/segment_0000.mp4", "https://x/embeddings/shots.json"],
                         {"https://x/embeddings/shots.json": SHOTS})
        assert m is not None, "invoke produced no manifest"
        assert m["template"] == "nle-timeline", \
            f"specialised template was clobbered back to {m['template']!r}"
        assert len(m["markers"]) == 3
        assert m["duration"] == 30.03

    def test_diarization_lanes_survive_invoke(self, monkeypatch):
        m = self._invoke(monkeypatch,
                         ["https://x/audio_16k_mono.wav", "https://x/global_speakers.json"],
                         {"https://x/global_speakers.json": SPEAKERS})
        assert m is not None
        assert m["template"] == "composite-timeline"
        assert {s["group"] for s in m["segments"]} == {"SPEAKER_00", "SPEAKER_01"}

    def test_undeclared_sidecars_still_use_the_llm_fallback(self, monkeypatch):
        import asyncio
        import render_agent as ra
        called = {"n": 0}

        async def fake(sidecars, skill, manifest):
            called["n"] += 1
            return {}
        monkeypatch.setattr(ra, "_populate_from_sidecars_llm", fake)
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda url, timeout=5.0: {"codec": "h264"} if url.endswith(".json") else None)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')

        async def run():
            async for _ in ra._render_deterministic(
                {"prompt": "Render: https://x/a.mp4 https://x/probe.json", "manifestId": "mid"}, None,
            ):
                pass
        asyncio.run(run())
        assert called["n"] == 1, "an undeclared shape must still get the LLM fallback"


# The canonical top-level face-recognition output, read from a real job.
FACEMETA = {
    "version": "1", "collection_id": "", "duration_sec": 300.0998,
    "stats": {"frames_analyzed": 30, "frames_skipped": 0, "faces_detected": 4,
              "num_tracks": 4, "num_people": 2},
    "people": [
        {"person_id": 0, "name": "Kawhi Leonard", "named": True, "name_source": "celebrity",
         "confidence": 98.68563842773438, "face_id": None, "gender": "Male",
         "age_range": [28, 36], "total_screen_time_sec": 1.6, "num_tracks": 2,
         "thumbnail_key": "…/debug/people/person_0000.jpg",
         "appearances": [{"begin_ms": 62996, "end_ms": 63796, "representative_frame": 1888},
                         {"begin_ms": 63997, "end_ms": 64797, "representative_frame": 1918}]},
        {"person_id": 1, "name": "Person_1", "named": False, "name_source": "",
         "confidence": 0, "gender": "Female", "age_range": [20, 28],
         "total_screen_time_sec": 0.8, "num_tracks": 2,
         "appearances": [{"begin_ms": 10000, "end_ms": 11000}]},
    ],
}

LANES_SPEC = "people[] name=name spans=appearances start=begin_ms end=end_ms units=ms"


class TestUnitScaling:
    """Tools report the unit that suits them; timelines are in seconds."""

    def test_milliseconds_convert_to_seconds(self):
        lanes = project_lanes(FACEMETA, LANES_SPEC)
        # 62996ms is one minute in, NOT 62996 seconds
        assert lanes[0]["start"] == 62.996 and lanes[0]["end"] == 63.796

    def test_unscaled_by_default(self):
        segs = project_segments(SPEAKERS, "sentences[] start=start end=end group=speaker_id")
        assert segs[0]["start"] == 26.978

    def test_markers_scale_too(self):
        d = {"items": [{"t": 1500, "e": 2500}]}
        m = project_markers(d, "items[] time=t end=e units=ms")
        assert m[0]["time"] == 1.5 and m[0]["endTime"] == 2.5

    def test_explicit_scale_factor(self):
        d = {"items": [{"start": 10, "end": 20}]}
        s = project_segments(d, "items[] start=start end=end scale=0.5")
        assert s[0]["start"] == 5.0 and s[0]["end"] == 10.0

    def test_frame_units_are_refused_rather_than_guessed(self):
        # frames need an fps this layer does not have; silently treating them as
        # seconds would place everything wrongly
        d = {"items": [{"start": 900, "end": 1800}]}
        assert project_segments(d, "items[] start=start end=end units=frames") == []


class TestLanesCarryParentContext:
    """A flat path reaches nested spans but loses which entity owns them."""

    def test_one_lane_per_person(self):
        lanes = project_lanes(FACEMETA, LANES_SPEC)
        assert len(lanes) == 3, "two appearances for one person plus one for the other"
        assert {l["group"] for l in lanes} == {"Kawhi Leonard", "Person_1"}
        assert [l["group"] for l in lanes].count("Kawhi Leonard") == 2

    def test_flat_mapping_would_have_lost_the_person(self):
        # demonstrates why `lanes` exists: the spans are reachable, the owner is not
        spans = resolve_path(FACEMETA, "people[].appearances[]")
        assert len(spans) == 3
        assert all("name" not in s for s in spans)

    def test_unnamed_entities_still_get_a_lane(self):
        d = {"people": [{"appearances": [{"begin_ms": 0, "end_ms": 1000}]}]}
        lanes = project_lanes(d, LANES_SPEC)
        assert len(lanes) == 1 and lanes[0]["group"] == "1"

    def test_missing_or_malformed_spans_are_skipped(self):
        d = {"people": [{"name": "A", "appearances": "nope"},
                        {"name": "B"},
                        {"name": "C", "appearances": [{"begin_ms": 5, "end_ms": 5}]},
                        {"name": "D", "appearances": [{"begin_ms": 0, "end_ms": 2000}]}]}
        lanes = project_lanes(d, LANES_SPEC)
        assert [l["group"] for l in lanes] == ["D"]


class TestCanonicalFaceOutput:
    """facemetadata.json was ignored entirely, which is why the tab was empty."""

    def test_skill_matches_and_projects_everything(self, monkeypatch):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda url, timeout=5.0: FACEMETA if url.endswith("facemetadata.json") else None)
        got = ra._correlate_sidecars(["https://x/facemetadata.json"])
        assert "render-face-people" in got["skills"]
        assert len(got["segments"]) == 3
        assert got["duration"] == 300.1
        assert got["summary"]["num_people"] == 2
        assert got["tables"][0]["rows"][0]["name"] == "Kawhi Leonard"

    def test_people_render_as_lanes_and_a_table_without_any_media(self, monkeypatch):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda url, timeout=5.0: FACEMETA if url.endswith("facemetadata.json") else None)
        monkeypatch.setattr(ra, "media_probe", lambda url: "{}")
        m = ra._build_media_manifest(["https://x/facemetadata.json"], "mid", {})
        assert m.get("errors") is None, "must not degrade to UNSUPPORTED_FORMAT"
        assert m["template"] == "data-table"
        assert m["rows"][0]["name"] == "Kawhi Leonard"
        assert {s["group"] for s in m["segments"]} == {"Kawhi Leonard", "Person_1"}

    def test_age_range_reads_as_a_range_not_a_count(self):
        tbl = project_rows(FACEMETA, "people[] cols=name,age_range,total_screen_time_sec")
        assert tbl["rows"][0]["age_range"] == "28\u201336"
        assert tbl["rows"][0]["total_screen_time_sec"] == 1.6


class TestAnnotatedStills:
    """Boxes drawn on extracted frames — the fix that needs no source video.

    Face recognition emits no video, but it does emit the frames it analysed, and
    every detection names its frame. Those stills were being discarded as
    "unsupported" while the boxes had nowhere to go.
    """

    FT = {"people": [{"person_id": 0, "name": "Kevin Kilner", "num_tracks": 1,
        "appearances": [{"begin_ms": 30030, "end_ms": 60000}],
        "tracks": [{"track_id": 0, "faces": [
        {"timestamp": 30.03, "frame_key": "frame_0000900.jpg", "gender": "Male",
         "box": {"l": 0.304877, "t": 0.173639, "w": 0.14083, "h": 0.355808}},
        {"timestamp": 30.03, "frame_key": "frame_0000900.jpg", "gender": "Female",
         "box": {"l": 0.6, "t": 0.2, "w": 0.1, "h": 0.2}},
        {"timestamp": 60.0, "frame_key": "frame_0001798.jpg", "gender": "Male",
         "box": {"l": 0.1, "t": 0.1, "w": 0.2, "h": 0.2}},
    ]}]}]}

    def _build(self, monkeypatch, urls, sidecars):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: sidecars.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: "{}")
        return ra._build_media_manifest(urls, "mid", {})

    def test_boxes_render_on_the_busiest_frame(self, monkeypatch):
        m = self._build(monkeypatch, [
            "https://x/facegroup.json",
            "https://x/face_frames/frame_0000900.jpg",
            "https://x/face_frames/frame_0001798.jpg",
        ], {"https://x/facegroup.json": self.FT})
        assert m["template"] == "image-annotated"
        # frame_0000900 has two detections, so it is chosen over the single
        assert m["src"].endswith("frame_0000900.jpg")
        assert len(m["annotations"]) == 2
        # percent, not normalized — the convention this template renders
        assert m["annotations"][0]["x"] == 30.488
        # the other matched still stays available
        assert {i["label"] for i in m["images"]} == {"frame_0000900.jpg", "frame_0001798.jpg"}

    def test_stills_are_no_longer_discarded_as_unsupported(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/facegroup.json", "https://x/frame_0000900.jpg"],
                        {"https://x/facegroup.json": self.FT})
        assert m.get("errors") is None

    def test_falls_back_to_a_table_when_no_frame_matches(self, monkeypatch):
        m = self._build(monkeypatch,
                        ["https://x/facemetadata.json", "https://x/unrelated.jpg"],
                        {"https://x/facemetadata.json": FACEMETA})
        assert m["template"] == "data-table", "an empty picture is worse than a table"
        assert m["rows"][0]["name"] == "Kawhi Leonard"

    def test_people_table_and_timeline_survive_alongside_the_still(self, monkeypatch):
        combined = dict(self.FT)
        m = self._build(monkeypatch, [
            "https://x/facegroup.json", "https://x/facemetadata.json",
            "https://x/frame_0000900.jpg",
        ], {"https://x/facegroup.json": combined,
            "https://x/facemetadata.json": FACEMETA})
        assert m["template"] == "image-annotated"
        assert m["tables"], "the people table must still be reachable"
        assert m["segments"], "the appearance lanes must still be reachable"
        assert m["metadata"]["num_people"] == 2

    def test_boxes_are_clamped_inside_the_image(self):
        boxes = project_boxes({"i": [{"box": {"l": 0.9, "t": 0.9, "w": 0.5, "h": 0.5}}]}, "i[] box=box")
        pct = to_percent_boxes(boxes)
        assert pct[0]["x"] + pct[0]["w"] <= 100.0
        assert pct[0]["y"] + pct[0]["h"] <= 100.0


class TestProductionArtifactSet:
    """Only the artifacts a deployed run actually delivers.

    The job registry drops any path containing `ephemeral` (and `probe`) from the
    renderable set, verified against artifact_capability. So `face_tracks.json` — the
    file holding the bounding boxes — never reaches the renderer in a real run, while
    `facemetadata.json` and the person thumbnails do. Testing with the boxes present
    would prove a path production never takes.
    """

    URLS = [
        "https://x/facemetadata.json",
        "https://x/debug/people/person_0000.jpg",
        "https://x/debug/people/person_0001.jpg",
    ]

    def _build(self, monkeypatch, urls=None):
        import render_agent as ra
        meta = json.loads(json.dumps(FACEMETA))
        meta["people"][0]["thumbnail_key"] = "…/debug/people/person_0000.jpg"
        meta["people"][1]["thumbnail_key"] = "…/debug/people/person_0001.jpg"
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda url, timeout=5.0: meta if url.endswith("facemetadata.json") else None)
        monkeypatch.setattr(ra, "media_probe", lambda url: "{}")
        return ra._build_media_manifest(urls or self.URLS, "mid", {})

    def test_faces_show_who_was_found_with_names_and_timeline(self, monkeypatch):
        m = self._build(monkeypatch)
        assert m.get("errors") is None
        assert m["template"] == "image-annotated"
        # a thumbnail per person, labelled by name
        assert [i["label"] for i in m["images"]] == ["Kawhi Leonard", "Person_1"]
        # plus the table and the per-person appearance lanes
        assert m["tables"][0]["rows"][0]["name"] == "Kawhi Leonard"
        assert {s["group"] for s in m["segments"]} == {"Kawhi Leonard", "Person_1"}
        assert m["metadata"]["num_people"] == 2

    def test_falls_back_to_the_table_when_thumbnails_are_absent(self, monkeypatch):
        m = self._build(monkeypatch, urls=["https://x/facemetadata.json"])
        assert m["template"] == "data-table"
        assert m["rows"][0]["name"] == "Kawhi Leonard"

    def test_shots_and_diarization_work_from_their_production_artifacts(self, monkeypatch):
        import render_agent as ra
        data = {"https://x/embeddings/shots.json": SHOTS,
                "https://x/global_speakers.json": SPEAKERS}
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: data.get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')

        shots = ra._build_media_manifest(
            ["https://x/proxy/segment_0000.mp4", "https://x/embeddings/shots.json"], "m1", {})
        assert shots["template"] == "nle-timeline" and len(shots["markers"]) == 3

        diar = ra._build_media_manifest(
            ["https://x/audio_16k_mono.wav", "https://x/global_speakers.json"], "m2", {})
        assert diar["template"] == "composite-timeline" and len(diar["segments"]) == 3


class TestFetchEfficiency:
    """Sidecars were downloaded twice per render, serially.

    `invoke` fetched every JSON artifact into `sidecars`, then the builder's
    correlation step fetched each one AGAIN — and both loops were sequential, so a
    render cost the sum of every request's latency, twice over.
    """

    def test_each_sidecar_is_fetched_once_per_render(self, monkeypatch):
        import asyncio
        import render_agent as ra
        calls: list[str] = []
        payloads = {"https://x/shots.json": SHOTS, "https://x/global_speakers.json": SPEAKERS}

        def counting_fetch(url, timeout=5.0):
            calls.append(url)
            return payloads.get(url)
        monkeypatch.setattr(ra, "_fetch_json", counting_fetch)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')

        async def run():
            async for _ in ra._render_deterministic({
                "prompt": "Render: https://x/a.mp4 https://x/shots.json https://x/global_speakers.json",
                "manifestId": "mid",
            }, None):
                pass
        asyncio.run(run())
        assert sorted(calls) == sorted(payloads), f"expected one fetch each, got {calls}"

    def test_the_builder_reuses_payloads_it_is_given(self, monkeypatch):
        import render_agent as ra

        def boom(url, timeout=5.0):
            raise AssertionError(f"refetched {url}")
        monkeypatch.setattr(ra, "_fetch_json", boom)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        m = ra._build_media_manifest(
            ["https://x/a.mp4", "https://x/shots.json"], "mid", {},
            prefetched={"https://x/shots.json": SHOTS},
        )
        assert len(m["markers"]) == 3

    def test_independent_fetches_run_concurrently(self, monkeypatch):
        import time
        import render_agent as ra
        urls = [f"https://x/f{i}.json" for i in range(6)]

        def slow(url, timeout=5.0):
            time.sleep(0.15)
            return {"n": 1}
        monkeypatch.setattr(ra, "_fetch_json", slow)
        started = time.monotonic()
        got = ra._fetch_json_many(urls)
        elapsed = time.monotonic() - started
        assert len(got) == 6
        # serial would be ~0.9s; concurrent must be far below that
        assert elapsed < 0.5, f"fetches appear serial ({elapsed:.2f}s for 6)"

    def test_a_single_sidecar_avoids_pool_overhead(self, monkeypatch):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: {"ok": 1})
        assert ra._fetch_json_many(["https://x/one.json"]) == {"https://x/one.json": {"ok": 1}}

    def test_a_failed_fetch_is_recorded_as_attempted_not_retried(self, monkeypatch):
        """A failure costs a full timeout; retrying it doubles that for nothing."""
        import render_agent as ra
        attempts: list[str] = []

        def flaky(url, timeout=5.0):
            attempts.append(url)
            return None if "bad" in url else SHOTS
        monkeypatch.setattr(ra, "_fetch_json", flaky)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        fetched = ra._fetch_json_many(["https://x/bad.json", "https://x/shots.json"])
        # the failure is present as None, marking it attempted
        assert fetched["https://x/bad.json"] is None
        attempts.clear()
        m = ra._build_media_manifest(
            ["https://x/a.mp4", "https://x/bad.json", "https://x/shots.json"],
            "mid", {}, prefetched=fetched)
        assert attempts == [], f"retried an already-failed fetch: {attempts}"
        assert len(m["markers"]) == 9 or len(m["markers"]) == 3


class TestContainerDuration:
    """A plain video render must not draw on the 600s fallback.

    media_probe returns duration None by design and peak extraction is WAV-only, so
    nothing supplied a real length for video. Reading the container header does.
    """

    def _build(self, monkeypatch, urls, container=None, sidecars=None):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: (sidecars or {}).get(url))
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')
        monkeypatch.setattr(ra, "_container_duration", lambda url, timeout=4.0: container)
        return ra._build_media_manifest(urls, "mid", {})

    def test_video_uses_its_container_length(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4"], container=30.03)
        assert m["duration"] == 30.03, "should not fall back to the 600s hint"
        assert m["tracks"][0]["end"] == 30.03

    def test_hint_remains_when_the_container_says_nothing(self, monkeypatch):
        m = self._build(monkeypatch, ["https://x/a.mp4"], container=None)
        assert m["duration"] == 600.0

    def test_a_stated_sidecar_duration_still_wins_over_a_shorter_container(self, monkeypatch):
        # the container covers one segment; the sidecar describes the whole asset
        m = self._build(monkeypatch, ["https://x/a.mp4", "https://x/shots.json"],
                        container=9.2, sidecars={"https://x/shots.json": SHOTS})
        assert m["duration"] == 30.03

    def test_container_is_not_consulted_for_audio(self, monkeypatch):
        import render_agent as ra
        called = []
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: None)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "audio/wav"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": [], "duration": 12.0}')
        monkeypatch.setattr(ra, "_container_duration",
                            lambda url, timeout=4.0: called.append(url) or 99.0)
        m = ra._build_media_manifest(["https://x/a.wav"], "mid", {})
        assert called == [], "audio duration comes from peak extraction"
        assert m["duration"] == 12.0

    def test_a_network_failure_is_not_fatal(self, monkeypatch):
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda url, timeout=5.0: None)
        monkeypatch.setattr(ra, "media_probe", lambda url: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda url, n: '{"peaks": []}')

        def boom(url, timeout=4.0):
            raise OSError("no network")
        monkeypatch.setattr(ra, "_find_mvhd", lambda fetch: (_ for _ in ()).throw(OSError("x")))
        m = ra._build_media_manifest(["https://x/a.mp4"], "mid", {})
        assert m["duration"] == 600.0, "must degrade to the hint, not fail the render"


class TestRestatedFindingsCollapse:
    """Several artifacts restate one finding; the view must show it once.

    Face recognition writes `people[]` into facemetadata.json, facegroup.json AND
    face_clusters.json. Projecting each sidecar independently produced one table per
    file — the same person listed four times. Deduping on CONTENT rather than on which
    file it came from keeps per-tool knowledge out of the code, and also absorbs
    duplicates that arise inside a single file.
    """

    PPL = [
        {"person_id": 0, "name": "Kawhi Leonard", "gender": "Male", "age_range": [28, 36],
         "confidence": 98.7, "total_screen_time_sec": 1.6, "num_tracks": 2,
         "thumbnail_key": "debug/people/person_0000.jpg",
         "appearances": [{"begin_ms": 62996, "end_ms": 63796}]},
        {"person_id": 1, "name": "Person_1", "gender": "Female", "age_range": [20, 28],
         "confidence": 0, "total_screen_time_sec": 0.8, "num_tracks": 2,
         "thumbnail_key": "debug/people/person_0001.jpg",
         "appearances": [{"begin_ms": 10000, "end_ms": 11000}]},
    ]

    def _files(self):
        return {
            "facemetadata.json": {"duration_sec": 300.1,
                                  "stats": {"num_people": 2, "num_tracks": 4},
                                  "people": self.PPL},
            "facegroup.json": {"version": "1", "people": self.PPL, "triaged_frames": []},
            "face_clusters.json": {"people": [
                {"person_id": 0, "name": "Kawhi Leonard", "coord": [-1.0, 0.0], "num_tracks": 2},
                {"person_id": 1, "name": "Person_1", "coord": [1.0, 0.0], "num_tracks": 2}]},
        }

    def _correlate(self, monkeypatch, files):
        import render_agent as ra
        base = "https://cdn/x/"
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda u, timeout=5.0: files.get(u.replace(base, "")))
        return ra._correlate_sidecars([base + k for k in files])

    def test_three_restatements_become_one_table(self, monkeypatch):
        got = self._correlate(monkeypatch, self._files())
        assert len(got["tables"]) == 1, f"expected one people table, got {len(got['tables'])}"
        assert len(got["tables"][0]["rows"]) == 2

    def test_the_richest_restatement_wins(self, monkeypatch):
        got = self._correlate(monkeypatch, self._files())
        cols = [c["key"] for c in got["tables"][0]["columns"]]
        # facemetadata carries screen time and confidence; face_clusters only coords
        assert "total_screen_time_sec" in cols and "confidence" in cols
        assert "coord" not in cols

    def test_genuinely_different_findings_are_kept(self):
        # v6 faces emit only people[] tables (facemetadata/facegroup/face_clusters
        # all restate the same identities and collapse). The engine must still keep
        # genuinely DIFFERENT entity tables apart rather than merging them.
        from render.projection import dedupe_tables
        people = {"columns": [{"key": "name"}], "rows": [{"name": "Kawhi Leonard"}]}
        shots = {"columns": [{"key": "shot_id"}], "rows": [{"shot_id": 0}, {"shot_id": 1}]}
        out = dedupe_tables([people, shots])
        assert len(out) == 2, "different entity tables are not restatements"

    def test_an_identity_indexed_twice_is_listed_once(self, monkeypatch):
        """A face collection accumulating the same identity across ingests.

        Not the current cause of duplication — the observed collection is empty and the
        run used none — but nothing prevents it, and content dedupe covers it for free.
        """
        dup = {"duration_sec": 300.1, "stats": {"num_people": 2},
               "people": [self.PPL[0], dict(self.PPL[0]), self.PPL[1]]}
        got = self._correlate(monkeypatch, {"facemetadata.json": dup})
        names = [r["name"] for r in got["tables"][0]["rows"]]
        assert names == ["Kawhi Leonard", "Person_1"], names

    def test_repeated_spans_and_thumbnails_collapse(self, monkeypatch):
        got = self._correlate(monkeypatch, self._files())
        # facemetadata and facegroup declare the same appearances: one per person
        spans = [(s["group"], s["start"], s["end"]) for s in got["segments"]]
        assert len(spans) == len(set(spans)) == 2, spans
        assert len(got["thumbnails"]) == 2

    def test_dedupe_preserves_first_seen_order(self):
        from render.projection import dedupe_rows, dedupe_segments
        rows = [{"n": "b"}, {"n": "a"}, {"n": "b"}]
        assert [r["n"] for r in dedupe_rows(rows)] == ["b", "a"]
        segs = [{"group": "B", "name": "B", "start": 1, "end": 2},
                {"group": "A", "name": "A", "start": 0, "end": 1},
                {"group": "B", "name": "B", "start": 1, "end": 2}]
        assert [s["group"] for s in dedupe_segments(segs)] == ["B", "A"]

    def test_row_order_of_keys_does_not_defeat_dedupe(self):
        from render.projection import dedupe_rows
        assert len(dedupe_rows([{"a": 1, "b": 2}, {"b": 2, "a": 1}])) == 1


class TestDeclaredImageResolution:
    """A result can name its images by key while never listing them as artifacts.

    The real face job's artifactUrls held 8 JSON files and ZERO images, even though
    every person carries a `thumbnail_key` and the harness filter would have kept the
    jpgs. So the images existed, were named, and still could not be shown. Resolving
    the declared keys through the gateway download-url path fixes that on our side,
    without waiting on the tool's payload to change.
    """

    FM = {
        "duration_sec": 300.1, "stats": {"num_people": 2},
        "people": [
            {"person_id": 0, "name": "Kawhi Leonard", "gender": "Male",
             "thumbnail_key": "micro-workflow/face-recognition/X/debug/people/person_0000.jpg",
             "appearances": [{"begin_ms": 62996, "end_ms": 63796}]},
            {"person_id": 1, "name": "Person_1", "gender": "Female",
             "thumbnail_key": "micro-workflow/face-recognition/X/debug/people/person_0001.jpg",
             "appearances": [{"begin_ms": 10000, "end_ms": 11000}]},
        ],
    }

    def test_declared_keys_are_discovered_from_the_sidecar(self):
        import render_agent as ra
        keys = ra._declared_image_keys({"https://x/facemetadata.json": self.FM})
        assert keys == [p["thumbnail_key"] for p in self.FM["people"]]

    def test_unrelated_sidecars_declare_nothing(self):
        import render_agent as ra
        assert ra._declared_image_keys({"https://x/probe.json": {"codec": "h264"}}) == []

    def test_bucket_comes_from_a_canonical_reference(self):
        import render_agent as ra
        assert ra._bucket_from_refs(
            ["https://cdn/x.json", "s3://mediaworks-proxy-123/micro-workflow/a/b.json"]
        ) == "mediaworks-proxy-123"
        assert ra._bucket_from_refs(["https://cdn/x.json"]) is None

    def test_keys_resolve_through_the_gateway_path(self, monkeypatch):
        import asyncio
        import render_agent as ra
        seen = {}

        async def fake_resolve(refs, budget_s=60.0):
            seen["refs"] = list(refs)
            return [r.replace("s3://b/", "https://signed/") for r in refs]
        monkeypatch.setattr(ra, "_resolve_s3_urls_via_gateway", fake_resolve)
        out = asyncio.run(ra._resolve_declared_images(
            {"https://x/facemetadata.json": self.FM}, ["s3://b/micro-workflow/x/facemetadata.json"]))
        assert all(r.startswith("s3://b/") for r in seen["refs"])
        assert len(out) == 2 and all(u.startswith("https://signed/") for u in out)

    def test_no_bucket_means_no_resolution_rather_than_a_guess(self, monkeypatch):
        import asyncio
        import render_agent as ra

        async def boom(refs, budget_s=60.0):
            raise AssertionError("must not resolve without a bucket")
        monkeypatch.setattr(ra, "_resolve_s3_urls_via_gateway", boom)
        assert asyncio.run(ra._resolve_declared_images(
            {"https://x/facemetadata.json": self.FM}, ["https://cdn/facemetadata.json"])) == []

    def test_a_gateway_failure_is_not_fatal(self, monkeypatch):
        import asyncio
        import render_agent as ra

        async def boom(refs, budget_s=60.0):
            raise RuntimeError("gateway down")
        monkeypatch.setattr(ra, "_resolve_s3_urls_via_gateway", boom)
        assert asyncio.run(ra._resolve_declared_images(
            {"https://x/facemetadata.json": self.FM}, ["s3://b/x.json"])) == []

    def test_resolved_images_produce_a_face_gallery(self, monkeypatch):
        """The payoff: names plus actual faces, from JSON-only artifacts."""
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json",
                            lambda u, timeout=5.0: self.FM if u.endswith("facemetadata.json") else None)
        monkeypatch.setattr(ra, "media_probe", lambda u: "{}")
        m = ra._build_media_manifest([
            "https://cdn/facemetadata.json",
            "https://signed/micro-workflow/face-recognition/X/debug/people/person_0000.jpg",
            "https://signed/micro-workflow/face-recognition/X/debug/people/person_0001.jpg",
        ], "mid", {})
        assert m["template"] == "image-annotated"
        assert [i["label"] for i in m["images"]] == ["Kawhi Leonard", "Person_1"]
        assert m["tables"][0]["rows"][0]["name"] == "Kawhi Leonard"


class TestInvokeRoutesFindingsOnlyResults:
    """A JSON-only result must reach the builder, not the artifact listing.

    Caught on staging, not locally: the deployed faces render still came back as
    `generic-preview` with `renderSkill: render-generic` even with the skills loaded,
    because `_build_media_manifest` sits behind `elif media_urls:` and face
    recognition emits NO media. Every earlier test called the builder directly and so
    never exercised this routing — the lesson being that a unit test of the builder
    cannot prove the branch that reaches it.
    """

    FM = {
        "duration_sec": 300.1, "stats": {"num_people": 2},
        "people": [
            {"person_id": 0, "name": "Kawhi Leonard", "gender": "Male",
             "age_range": [28, 36], "total_screen_time_sec": 1.6,
             "appearances": [{"begin_ms": 62996, "end_ms": 63796}]},
            {"person_id": 1, "name": "Person_1", "gender": "Female",
             "age_range": [20, 28], "total_screen_time_sec": 0.8,
             "appearances": [{"begin_ms": 10000, "end_ms": 11000}]},
        ],
    }

    def _invoke(self, monkeypatch, urls, payloads):
        import asyncio
        import render_agent as ra
        monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: payloads.get(u))
        monkeypatch.setattr(ra, "media_probe", lambda u: "{}")

        async def no_images(sidecars, request_urls):
            return []
        monkeypatch.setattr(ra, "_resolve_declared_images", no_images)

        async def run():
            chunks = []
            async for ch in ra._render_deterministic({"prompt": "Render: " + " ".join(urls)}, None):
                chunks.append(ch)
            return chunks
        chunks = asyncio.run(run())
        for ch in chunks:
            try:
                obj = json.loads(ch)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("template"):
                return obj
        return None

    def test_json_only_faces_reach_the_projected_view(self, monkeypatch):
        m = self._invoke(monkeypatch, ["https://x/facemetadata.json"],
                         {"https://x/facemetadata.json": self.FM})
        assert m is not None
        assert m["template"] != "generic-preview", "regressed to an artifact listing"
        assert m["template"] == "data-table"
        assert m["renderSkill"] != "render-generic"
        assert m["rows"][0]["name"] == "Kawhi Leonard"
        assert {s["group"] for s in m["segments"]} == {"Kawhi Leonard", "Person_1"}

    def test_unprojectable_json_only_emits_no_tab(self, monkeypatch):
        """Unprojectable JSON-only input shows NO tab (was a generic artifact listing).

        JSON is plumbing/overlay data, never a standalone deliverable — a lone json that projects
        to nothing is the "generic file viewer" tab users hit, so it is suppressed. Projectable
        JSON (faces -> data-table, above) is unaffected because it never reaches the fallback.
        """
        m = self._invoke(monkeypatch, ["https://x/probe.json"],
                         {"https://x/probe.json": {"codec": "h264", "format": "mp4"}})
        assert m is None, "unprojectable JSON-only must emit no manifest/tab"


class TestBucketFromResolvedRefs:
    """Declared images must resolve even after s3:// refs are rewritten.

    On the first deployed run the faces manifest had `images: []` although every
    person declared a `thumbnail_key` and the objects existed (48KB, 57KB, 11KB).
    Cause: `invoke` rewrites canonical s3:// refs to https early, so by the time the
    declared images were resolved there was no s3:// left to take a bucket from.
    """

    def test_canonical_ref(self):
        import render_agent as ra
        assert ra._bucket_from_refs(["s3://my-bucket/a/b.json"]) == "my-bucket"

    def test_virtual_hosted_resolved_url(self):
        import render_agent as ra
        u = "https://mediaworks-proxy-100000000002-us-west-2.s3.amazonaws.com/micro-workflow/x/facemetadata.json"
        assert ra._bucket_from_refs([u]) == "mediaworks-proxy-100000000002-us-west-2"

    def test_regional_virtual_hosted_url(self):
        import render_agent as ra
        u = "https://my-bucket.s3.us-west-2.amazonaws.com/k.json?X-Amz-Signature=abc"
        assert ra._bucket_from_refs([u]) == "my-bucket"

    def test_path_style_url(self):
        import render_agent as ra
        assert ra._bucket_from_refs(["https://s3.us-west-2.amazonaws.com/my-bucket/k.json"]) == "my-bucket"

    def test_a_cloudfront_url_yields_no_bucket(self):
        """Better to skip resolution than to guess a bucket from a CDN host."""
        import render_agent as ra
        assert ra._bucket_from_refs(["https://d123.cloudfront.net/artifacts/x.json"]) is None

    def test_resolution_works_from_an_already_resolved_request(self, monkeypatch):
        import asyncio
        import render_agent as ra
        fm = {"stats": {"num_people": 1}, "people": [{"name": "A", "thumbnail_key": "p/a.jpg",
                          "appearances": [{"begin_ms": 0, "end_ms": 1000}]}]}
        seen = {}

        async def fake(refs, budget_s=60.0):
            seen["refs"] = list(refs)
            return ["https://signed/" + r.split("/", 3)[-1] for r in refs]
        monkeypatch.setattr(ra, "_resolve_s3_urls_via_gateway", fake)
        out = asyncio.run(ra._resolve_declared_images(
            {"u": fm},
            ["https://mediaworks-proxy-1.s3.amazonaws.com/micro-workflow/x/facemetadata.json"]))
        assert seen["refs"] == ["s3://mediaworks-proxy-1/p/a.jpg"]
        assert len(out) == 1


class TestPromotedTableIsNotRepeated:
    def test_top_level_table_is_excluded_from_tables(self, monkeypatch):
        import render_agent as ra
        fm = {"duration_sec": 100.0, "stats": {"num_people": 1},
              "people": [{"name": "A", "gender": "M", "total_screen_time_sec": 1.0,
                          "appearances": [{"begin_ms": 0, "end_ms": 1000}]}]}
        ic = {"clusters": [{"face_group_id": 0, "track_ids": [0],
                            "celebrity": {"name": "A", "confidence": 90.0}}]}
        data = {"https://x/facemetadata.json": fm, "https://x/identity_clusters.json": ic}
        monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: data.get(u))
        monkeypatch.setattr(ra, "media_probe", lambda u: "{}")
        m = ra._build_media_manifest(list(data), "mid", {})
        top = [c["key"] for c in m["columns"]]
        others = [[c["key"] for c in t["columns"]] for t in (m.get("tables") or [])]
        assert top not in others, "the promoted table must not repeat in tables[]"
        # v6: identity_clusters.json is no longer delivered/projected, and
        # face_clusters dedupes into the promoted people table — so there is no
        # second table.
        assert others == []


class TestGalleryKeepsDetections:
    """A thumbnail gallery must not discard the detected boxes.

    Seen on the deployed runtime: once thumbnails resolved, the faces manifest went
    from `annotations: 100` to `annotations: 0`, because the gallery form carries no
    boxes of its own and its empty list overwrote the real ones. Those boxes are what
    a later render with the source video draws, and what the note counts.
    """

    FM = {"duration_sec": 300.0, "stats": {"num_people": 1},
          "people": [{"name": "A", "gender": "M", "thumbnail_key": "d/a.jpg",
                      "total_screen_time_sec": 1.0,
                      "appearances": [{"begin_ms": 0, "end_ms": 1000}]}]}
    FT = {"people": [{"person_id": 0, "name": "A", "num_tracks": 1,
        "appearances": [{"begin_ms": 1000, "end_ms": 2000}],
        "tracks": [{"track_id": 0, "faces": [
        {"timestamp": 1.0, "frame_key": "f1.jpg", "box": {"l": .1, "t": .1, "w": .2, "h": .2}},
        {"timestamp": 2.0, "frame_key": "f2.jpg", "box": {"l": .3, "t": .3, "w": .2, "h": .2}}]}]}]}

    def test_boxes_survive_alongside_thumbnails(self, monkeypatch):
        import render_agent as ra
        data = {"https://x/facemetadata.json": self.FM, "https://x/facegroup.json": self.FT}
        monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: data.get(u))
        monkeypatch.setattr(ra, "media_probe", lambda u: "{}")
        m = ra._build_media_manifest(list(data) + ["https://x/d/a.jpg"], "mid", {})
        assert m["template"] == "image-annotated"
        assert [i["label"] for i in m["images"]] == ["A"]
        assert len(m["annotations"]) == 2, "detections were discarded by the gallery"
        assert any("supply the source video" in n for n in m["notes"])

    def test_a_boxed_frame_still_wins_over_thumbnails(self, monkeypatch):
        """Boxes drawn on the real frame are more informative than a portrait."""
        import render_agent as ra
        data = {"https://x/facemetadata.json": self.FM, "https://x/facegroup.json": self.FT}
        monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: data.get(u))
        monkeypatch.setattr(ra, "media_probe", lambda u: "{}")
        m = ra._build_media_manifest(list(data) + ["https://x/f1.jpg", "https://x/d/a.jpg"], "mid", {})
        assert m["src"].endswith("f1.jpg")
        assert len(m["annotations"]) >= 1
        assert not any("supply the source video" in n for n in (m.get("notes") or []))


class TestBoxesByEntity:
    """Per-box identity: a face box must read as WHO it is, not just its gender.

    project_boxes flat-maps people[].tracks[].faces[] and loses the parent, so a
    box could only be labelled by a per-face field. project_boxes_by_entity keeps
    the person and stamps their name onto every box.
    """

    SPEC = "people[] label=name faces=tracks[].faces[] box=box time=timestamp frame=frame_key"

    def test_box_label_comes_from_the_person(self):
        boxes = project_boxes_by_entity(FACEGROUP, self.SPEC)
        assert len(boxes) == 3, "all faces across all tracks of all people"
        assert [b["label"] for b in boxes] == ["Kevin Kilner", "Kevin Kilner", "Reid Scott"]
        assert boxes[0]["x"] == 0.304877 and boxes[0]["time"] == 30.03
        assert boxes[0]["frame"] == "frame_0000900.jpg"

    def test_flat_projection_cannot_label_by_person(self):
        # contrast: the flat box path only sees per-face fields (gender), not name
        flat = project_boxes(FACEGROUP, "people[].tracks[].faces[] box=box label=gender")
        assert {b["label"] for b in flat} == {"Male"}

    def test_unnamed_entity_falls_back_to_index(self):
        d = {"people": [{"tracks": [{"faces": [{"box": {"l": .1, "t": .1, "w": .2, "h": .2}}]}]}]}
        boxes = project_boxes_by_entity(d, self.SPEC)
        assert len(boxes) == 1 and boxes[0]["label"] == "1"
