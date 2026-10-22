"""Unit tests for manifest_schema.py — validates core functionality."""
import json
import pytest

from render.manifest_schema import (
    RenderManifest,
    ManifestTrack,
    ManifestClip,
    CaptionCue,
    ManifestError,
    ManifestWarning,
    ParseError,
    ParseResult,
    format_manifest,
    parse_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_track(**overrides) -> dict:
    base = {"id": "t1", "type": "video", "src": "https://cdn.example.com/v.mp4", "start": 0.0, "end": 10.5}
    base.update(overrides)
    return base


def _make_clip(**overrides) -> dict:
    base = {"id": "c1", "name": "Intro", "start": 0.0, "end": 5.0, "sourceTrack": "t1"}
    base.update(overrides)
    return base


def _make_caption(**overrides) -> dict:
    base = {"start": 1.0, "end": 3.5, "speaker": "Alice", "text": "Hello world"}
    base.update(overrides)
    return base


def _make_manifest_dict(**overrides) -> dict:
    base = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "duration": 120.456,
        "tracks": [_make_track()],
        "clips": [_make_clip()],
        "createdAt": "2024-01-15T10:30:00Z",
    }
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> RenderManifest:
    data = _make_manifest_dict(**overrides)
    return RenderManifest.from_dict(data)


# ---------------------------------------------------------------------------
# format_manifest tests
# ---------------------------------------------------------------------------

class TestFormatManifest:
    def test_produces_valid_json(self):
        manifest = _make_manifest()
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert parsed["id"] == manifest.id

    def test_2_space_indentation(self):
        manifest = _make_manifest()
        result = format_manifest(manifest)
        # 2-space indent means keys at top level are indented with 2 spaces
        lines = result.split("\n")
        # First nested key should start with 2 spaces
        assert lines[1].startswith("  ")
        # Should NOT start with 4 spaces (that would be 4-space indent)
        assert not lines[1].startswith("    ")

    def test_time_fields_rounded_to_3_decimals(self):
        manifest = _make_manifest(duration=10.123456789)
        manifest.tracks[0].start = 1.9999999
        manifest.tracks[0].end = 5.1234567
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert parsed["duration"] == 10.123
        assert parsed["tracks"][0]["start"] == 2.0
        assert parsed["tracks"][0]["end"] == 5.123

    def test_clip_time_fields_rounded(self):
        manifest = _make_manifest()
        manifest.clips[0].start = 0.1111111
        manifest.clips[0].end = 3.9999999
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert parsed["clips"][0]["start"] == 0.111
        assert parsed["clips"][0]["end"] == 4.0

    def test_caption_time_fields_rounded(self):
        manifest = _make_manifest(captions=[_make_caption(start=1.5556, end=3.9994)])
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert parsed["captions"][0]["start"] == 1.556
        assert parsed["captions"][0]["end"] == 3.999

    def test_optional_fields_omitted_when_none(self):
        manifest = _make_manifest()
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert "captions" not in parsed
        assert "peaks" not in parsed
        assert "errors" not in parsed
        assert "warnings" not in parsed

    def test_optional_fields_included_when_present(self):
        manifest = _make_manifest(
            captions=[_make_caption()],
            peaks={"t1": [0.1, 0.5, 0.9]},
            errors=[{"code": "TEST", "message": "test error"}],
            warnings=[{"code": "WARN", "message": "test warning"}],
        )
        result = format_manifest(manifest)
        parsed = json.loads(result)
        assert "captions" in parsed
        assert "peaks" in parsed
        assert "errors" in parsed
        assert "warnings" in parsed


# ---------------------------------------------------------------------------
# parse_manifest tests
# ---------------------------------------------------------------------------

class TestParseManifest:
    def test_valid_minimal_manifest(self):
        data = _make_manifest_dict()
        result = parse_manifest(json.dumps(data))
        assert result.ok is True
        assert result.manifest is not None
        assert result.manifest.id == data["id"]
        assert result.manifest.duration == data["duration"]
        assert len(result.manifest.tracks) == 1
        assert len(result.manifest.clips) == 1

    def test_invalid_json(self):
        result = parse_manifest("{not valid json")
        assert result.ok is False
        assert len(result.errors) == 1
        assert result.errors[0].code == "INVALID_JSON"
        # Should mention line and column
        assert "line" in result.errors[0].message.lower()
        assert "column" in result.errors[0].message.lower()

    def test_missing_required_fields(self):
        result = parse_manifest("{}")
        assert result.ok is False
        paths = {e.path for e in result.errors}
        assert "id" in paths
        assert "duration" in paths
        assert "tracks" in paths
        assert "clips" in paths
        assert "createdAt" in paths
        for e in result.errors:
            assert e.code == "MISSING_FIELD"

    def test_invalid_type_for_id(self):
        data = _make_manifest_dict(id=123)
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        type_errors = [e for e in result.errors if e.path == "id"]
        assert len(type_errors) == 1
        assert type_errors[0].code == "INVALID_TYPE"
        assert type_errors[0].expected == "string"
        assert type_errors[0].actual == "number"

    def test_invalid_duration_type(self):
        data = _make_manifest_dict(duration="not a number")
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if e.path == "duration"]
        assert errors[0].code == "INVALID_TYPE"

    def test_duration_out_of_range_low(self):
        data = _make_manifest_dict(duration=0.0001)
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if e.path == "duration"]
        assert errors[0].code == "INVALID_VALUE"

    def test_duration_out_of_range_high(self):
        data = _make_manifest_dict(duration=100000)
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if e.path == "duration"]
        assert errors[0].code == "INVALID_VALUE"

    def test_invalid_track_type_value(self):
        data = _make_manifest_dict(tracks=[_make_track(type="unknown")])
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if "tracks[0].type" in e.path]
        assert errors[0].code == "INVALID_VALUE"

    def test_track_missing_fields(self):
        data = _make_manifest_dict(tracks=[{"id": "t1"}])
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        paths = {e.path for e in result.errors}
        assert "tracks[0].type" in paths
        assert "tracks[0].src" in paths
        assert "tracks[0].start" in paths
        assert "tracks[0].end" in paths

    def test_clip_missing_fields(self):
        data = _make_manifest_dict(clips=[{"id": "c1"}])
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        paths = {e.path for e in result.errors}
        assert "clips[0].name" in paths
        assert "clips[0].start" in paths
        assert "clips[0].end" in paths
        assert "clips[0].sourceTrack" in paths

    def test_caption_missing_fields(self):
        data = _make_manifest_dict(captions=[{"start": 0.0}])
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        paths = {e.path for e in result.errors}
        assert "captions[0].end" in paths
        assert "captions[0].speaker" in paths
        assert "captions[0].text" in paths

    def test_preserves_unknown_fields_top_level(self):
        data = _make_manifest_dict()
        data["customField"] = "preserved"
        data["anotherField"] = 42
        result = parse_manifest(json.dumps(data))
        assert result.ok is True
        # Round-trip should preserve unknown fields
        output = json.loads(format_manifest(result.manifest))
        assert output["customField"] == "preserved"
        assert output["anotherField"] == 42

    def test_preserves_unknown_fields_in_tracks(self):
        track = _make_track()
        track["customMetadata"] = {"foo": "bar"}
        data = _make_manifest_dict(tracks=[track])
        result = parse_manifest(json.dumps(data))
        assert result.ok is True
        output = json.loads(format_manifest(result.manifest))
        assert output["tracks"][0]["customMetadata"] == {"foo": "bar"}

    def test_preserves_unknown_fields_in_clips(self):
        clip = _make_clip()
        clip["color"] = "#ff0000"
        data = _make_manifest_dict(clips=[clip])
        result = parse_manifest(json.dumps(data))
        assert result.ok is True
        output = json.loads(format_manifest(result.manifest))
        assert output["clips"][0]["color"] == "#ff0000"

    def test_preserves_unknown_fields_in_captions(self):
        caption = _make_caption()
        caption["confidence"] = 0.95
        data = _make_manifest_dict(captions=[caption])
        result = parse_manifest(json.dumps(data))
        assert result.ok is True
        output = json.loads(format_manifest(result.manifest))
        assert output["captions"][0]["confidence"] == 0.95

    def test_non_object_top_level(self):
        result = parse_manifest("[1, 2, 3]")
        assert result.ok is False
        assert result.errors[0].code == "INVALID_TYPE"
        assert result.errors[0].expected == "object"

    def test_captions_invalid_type(self):
        data = _make_manifest_dict(captions="not an array")
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if e.path == "captions"]
        assert errors[0].code == "INVALID_TYPE"

    def test_peaks_invalid_type(self):
        data = _make_manifest_dict(peaks=[1, 2, 3])
        result = parse_manifest(json.dumps(data))
        assert result.ok is False
        errors = [e for e in result.errors if e.path == "peaks"]
        assert errors[0].code == "INVALID_TYPE"


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_minimal_manifest_round_trip(self):
        data = _make_manifest_dict()
        json_str = json.dumps(data)
        result = parse_manifest(json_str)
        assert result.ok is True
        output = format_manifest(result.manifest)
        result2 = parse_manifest(output)
        assert result2.ok is True
        # Compare the dict representations
        assert result2.manifest.to_dict() == result.manifest.to_dict()

    def test_full_manifest_round_trip(self):
        data = _make_manifest_dict(
            captions=[_make_caption()],
            peaks={"t1": [0.1, 0.5, 0.9]},
            errors=[{"code": "TEST", "message": "err"}],
            warnings=[{"code": "W1", "message": "warn", "field": "tracks[0].end"}],
        )
        json_str = json.dumps(data)
        result = parse_manifest(json_str)
        assert result.ok is True
        output = format_manifest(result.manifest)
        result2 = parse_manifest(output)
        assert result2.ok is True
        assert result2.manifest.to_dict() == result.manifest.to_dict()

    def test_round_trip_with_extra_fields(self):
        data = _make_manifest_dict()
        data["version"] = "2.0"
        data["customArray"] = [1, 2, 3]
        track = _make_track()
        track["label"] = "Main Video"
        data["tracks"] = [track]
        json_str = json.dumps(data)
        result = parse_manifest(json_str)
        assert result.ok is True
        output = format_manifest(result.manifest)
        result2 = parse_manifest(output)
        assert result2.ok is True
        out_dict = result2.manifest.to_dict()
        assert out_dict["version"] == "2.0"
        assert out_dict["customArray"] == [1, 2, 3]
        assert out_dict["tracks"][0]["label"] == "Main Video"

    def test_time_precision_preserved_on_round_trip(self):
        """Time fields rounded to 3 decimals survive the round-trip."""
        data = _make_manifest_dict(duration=45.678)
        data["tracks"] = [_make_track(start=1.234, end=9.876)]
        data["clips"] = [_make_clip(start=2.345, end=7.654)]
        json_str = json.dumps(data)
        result = parse_manifest(json_str)
        assert result.ok is True
        output = format_manifest(result.manifest)
        result2 = parse_manifest(output)
        assert result2.ok is True
        d = result2.manifest.to_dict()
        assert d["duration"] == 45.678
        assert d["tracks"][0]["start"] == 1.234
        assert d["tracks"][0]["end"] == 9.876
        assert d["clips"][0]["start"] == 2.345
        assert d["clips"][0]["end"] == 7.654


# ---------------------------------------------------------------------------
# Model unit tests
# ---------------------------------------------------------------------------

class TestModels:
    def test_manifest_error_with_field(self):
        err = ManifestError(code="TEST", message="msg", field="tracks[0].end")
        d = err.to_dict()
        assert d == {"code": "TEST", "message": "msg", "field": "tracks[0].end"}
        roundtrip = ManifestError.from_dict(d)
        assert roundtrip == err

    def test_manifest_error_without_field(self):
        err = ManifestError(code="TEST", message="msg")
        d = err.to_dict()
        assert "field" not in d
        roundtrip = ManifestError.from_dict(d)
        assert roundtrip == err

    def test_manifest_warning_to_dict(self):
        warn = ManifestWarning(code="W1", message="warn", field="captions[0]")
        d = warn.to_dict()
        assert d == {"code": "W1", "message": "warn", "field": "captions[0]"}

    def test_caption_cue_to_dict(self):
        cue = CaptionCue(start=1.5, end=3.5, speaker="Bob", text="Hi")
        d = cue.to_dict()
        assert d == {"start": 1.5, "end": 3.5, "speaker": "Bob", "text": "Hi"}

    def test_manifest_track_to_dict(self):
        track = ManifestTrack(id="t1", type="audio", src="url", start=0.0, end=60.0)
        d = track.to_dict()
        assert d == {"id": "t1", "type": "audio", "src": "url", "start": 0.0, "end": 60.0}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
