"""Render_Manifest schema — data models, serialization, and parsing.

Defines the Render_Manifest and related models as dataclasses with
JSON round-trip support. Provides:
  - format_manifest(manifest) → 2-space-indented JSON string
  - parse_manifest(json_str) → ParseResult (success with manifest, or structured errors)

All time fields (duration, start, end) are rounded to 3 decimal places
on serialization. Unknown fields are preserved through the round-trip.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ManifestError:
    """A validation error attached to a manifest."""
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ManifestError:
        return cls(
            code=data["code"],
            message=data["message"],
            field=data.get("field"),
        )


@dataclass
class ManifestWarning:
    """A non-fatal warning attached to a manifest."""
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ManifestWarning:
        return cls(
            code=data["code"],
            message=data["message"],
            field=data.get("field"),
        )


@dataclass
class ManifestTrack:
    """A single track in the timeline."""
    id: str
    type: str  # 'video' | 'audio' | 'overlay' | 'subtitle'
    src: str
    start: float
    end: float
    _extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "src": self.src,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
        }
        d.update(self._extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ManifestTrack:
        known_keys = {"id", "type", "src", "start", "end"}
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            id=data["id"],
            type=data["type"],
            src=data["src"],
            start=float(data["start"]),
            end=float(data["end"]),
            _extra=extra,
        )


@dataclass
class ManifestClip:
    """A named clip segment."""
    id: str
    name: str
    start: float
    end: float
    sourceTrack: str
    _extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "sourceTrack": self.sourceTrack,
        }
        d.update(self._extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ManifestClip:
        known_keys = {"id", "name", "start", "end", "sourceTrack"}
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            id=data["id"],
            name=data["name"],
            start=float(data["start"]),
            end=float(data["end"]),
            sourceTrack=data["sourceTrack"],
            _extra=extra,
        )


@dataclass
class CaptionCue:
    """A single timed caption entry."""
    start: float
    end: float
    speaker: str
    text: str
    _extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "speaker": self.speaker,
            "text": self.text,
        }
        d.update(self._extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> CaptionCue:
        known_keys = {"start", "end", "speaker", "text"}
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            speaker=data["speaker"],
            text=data["text"],
            _extra=extra,
        )


@dataclass
class RenderManifest:
    """The top-level render manifest produced by the Render Agent."""
    id: str
    duration: float
    tracks: list[ManifestTrack]
    clips: list[ManifestClip]
    createdAt: str
    captions: list[CaptionCue] | None = None
    peaks: dict[str, list[float]] | None = None
    errors: list[ManifestError] | None = None
    warnings: list[ManifestWarning] | None = None
    _extra: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "duration": round(self.duration, 3),
            "tracks": [t.to_dict() for t in self.tracks],
            "clips": [c.to_dict() for c in self.clips],
            "createdAt": self.createdAt,
        }
        if self.captions is not None:
            d["captions"] = [c.to_dict() for c in self.captions]
        if self.peaks is not None:
            d["peaks"] = self.peaks
        if self.errors is not None:
            d["errors"] = [e.to_dict() for e in self.errors]
        if self.warnings is not None:
            d["warnings"] = [w.to_dict() for w in self.warnings]
        d.update(self._extra)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> RenderManifest:
        known_keys = {
            "id", "duration", "tracks", "clips", "createdAt",
            "captions", "peaks", "errors", "warnings",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}

        captions = None
        if "captions" in data and data["captions"] is not None:
            captions = [CaptionCue.from_dict(c) for c in data["captions"]]

        errors = None
        if "errors" in data and data["errors"] is not None:
            errors = [ManifestError.from_dict(e) for e in data["errors"]]

        warnings = None
        if "warnings" in data and data["warnings"] is not None:
            warnings = [ManifestWarning.from_dict(w) for w in data["warnings"]]

        return cls(
            id=data["id"],
            duration=float(data["duration"]),
            tracks=[ManifestTrack.from_dict(t) for t in data["tracks"]],
            clips=[ManifestClip.from_dict(c) for c in data["clips"]],
            createdAt=data["createdAt"],
            captions=captions,
            peaks=data.get("peaks"),
            errors=errors,
            warnings=warnings,
            _extra=extra,
        )


# ---------------------------------------------------------------------------
# Parse Error / Result types
# ---------------------------------------------------------------------------

@dataclass
class ParseError:
    """A structured error from parse_manifest."""
    path: str
    code: str  # 'MISSING_FIELD' | 'INVALID_TYPE' | 'INVALID_VALUE' | 'INVALID_JSON'
    message: str
    expected: str | None = None
    actual: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "path": self.path,
            "code": self.code,
            "message": self.message,
        }
        if self.expected is not None:
            d["expected"] = self.expected
        if self.actual is not None:
            d["actual"] = self.actual
        return d


@dataclass
class ParseResult:
    """Result of parse_manifest — either a successful manifest or errors."""
    ok: bool
    manifest: RenderManifest | None = None
    errors: list[ParseError] | None = None


# ---------------------------------------------------------------------------
# format_manifest
# ---------------------------------------------------------------------------

def format_manifest(manifest: RenderManifest) -> str:
    """Serialize a RenderManifest to a human-readable JSON string.

    - 2-space indentation
    - All time fields (duration, start, end) rounded to 3 decimal places
    """
    return json.dumps(manifest.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------

# Valid track types
_VALID_TRACK_TYPES = {"video", "audio", "overlay", "subtitle"}


def _type_name(value: Any) -> str:
    """Return a human-friendly type name for a value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _check_required_field(data: dict, field_name: str, path: str, expected_type: str,
                          errors: list[ParseError]) -> bool:
    """Check a field exists and has the expected type. Returns True if valid."""
    full_path = f"{path}.{field_name}" if path else field_name
    if field_name not in data:
        errors.append(ParseError(
            path=full_path,
            code="MISSING_FIELD",
            message=f"Required field '{field_name}' is missing",
        ))
        return False

    value = data[field_name]
    if not _type_matches(value, expected_type):
        errors.append(ParseError(
            path=full_path,
            code="INVALID_TYPE",
            message=f"Field '{field_name}' expected {expected_type}, got {_type_name(value)}",
            expected=expected_type,
            actual=_type_name(value),
        ))
        return False
    return True


def _type_matches(value: Any, expected: str) -> bool:
    """Check if a value matches the expected type string."""
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _validate_number_range(value: float, field_name: str, path: str,
                           min_val: float | None, max_val: float | None,
                           errors: list[ParseError]) -> bool:
    """Validate a number is within range. Returns True if valid."""
    full_path = f"{path}.{field_name}" if path else field_name
    if math.isnan(value) or math.isinf(value):
        errors.append(ParseError(
            path=full_path,
            code="INVALID_VALUE",
            message=f"Field '{field_name}' must be a finite number",
        ))
        return False
    if min_val is not None and value < min_val:
        errors.append(ParseError(
            path=full_path,
            code="INVALID_VALUE",
            message=f"Field '{field_name}' must be >= {min_val}, got {value}",
        ))
        return False
    if max_val is not None and value > max_val:
        errors.append(ParseError(
            path=full_path,
            code="INVALID_VALUE",
            message=f"Field '{field_name}' must be <= {max_val}, got {value}",
        ))
        return False
    return True


def _validate_track(data: Any, index: int, errors: list[ParseError]) -> bool:
    """Validate a single track entry. Returns True if valid."""
    path = f"tracks[{index}]"
    if not isinstance(data, dict):
        errors.append(ParseError(
            path=path,
            code="INVALID_TYPE",
            message=f"Track at index {index} must be an object, got {_type_name(data)}",
            expected="object",
            actual=_type_name(data),
        ))
        return False

    valid = True
    valid = _check_required_field(data, "id", path, "string", errors) and valid
    valid = _check_required_field(data, "type", path, "string", errors) and valid
    valid = _check_required_field(data, "src", path, "string", errors) and valid
    valid = _check_required_field(data, "start", path, "number", errors) and valid
    valid = _check_required_field(data, "end", path, "number", errors) and valid

    # Validate track type value
    if valid and "type" in data and isinstance(data["type"], str):
        if data["type"] not in _VALID_TRACK_TYPES:
            errors.append(ParseError(
                path=f"{path}.type",
                code="INVALID_VALUE",
                message=f"Track type must be one of {sorted(_VALID_TRACK_TYPES)}, got '{data['type']}'",
            ))
            valid = False

    return valid


def _validate_clip(data: Any, index: int, errors: list[ParseError]) -> bool:
    """Validate a single clip entry. Returns True if valid."""
    path = f"clips[{index}]"
    if not isinstance(data, dict):
        errors.append(ParseError(
            path=path,
            code="INVALID_TYPE",
            message=f"Clip at index {index} must be an object, got {_type_name(data)}",
            expected="object",
            actual=_type_name(data),
        ))
        return False

    valid = True
    valid = _check_required_field(data, "id", path, "string", errors) and valid
    valid = _check_required_field(data, "name", path, "string", errors) and valid
    valid = _check_required_field(data, "start", path, "number", errors) and valid
    valid = _check_required_field(data, "end", path, "number", errors) and valid
    valid = _check_required_field(data, "sourceTrack", path, "string", errors) and valid
    return valid


def _validate_caption(data: Any, index: int, errors: list[ParseError]) -> bool:
    """Validate a single caption cue entry. Returns True if valid."""
    path = f"captions[{index}]"
    if not isinstance(data, dict):
        errors.append(ParseError(
            path=path,
            code="INVALID_TYPE",
            message=f"Caption at index {index} must be an object, got {_type_name(data)}",
            expected="object",
            actual=_type_name(data),
        ))
        return False

    valid = True
    valid = _check_required_field(data, "start", path, "number", errors) and valid
    valid = _check_required_field(data, "end", path, "number", errors) and valid
    valid = _check_required_field(data, "speaker", path, "string", errors) and valid
    valid = _check_required_field(data, "text", path, "string", errors) and valid
    return valid


def parse_manifest(json_str: str) -> ParseResult:
    """Parse a JSON string into a validated RenderManifest or return errors.

    Returns ParseResult with either:
      - ok=True, manifest=<RenderManifest>
      - ok=False, errors=<list of ParseError>

    Unknown fields are preserved without errors (round-trip friendly).
    """
    # Step 1: Parse JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return ParseResult(
            ok=False,
            errors=[ParseError(
                path="",
                code="INVALID_JSON",
                message=f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}",
            )],
        )

    # Step 2: Top-level must be an object
    if not isinstance(data, dict):
        return ParseResult(
            ok=False,
            errors=[ParseError(
                path="",
                code="INVALID_TYPE",
                message=f"Manifest must be an object, got {_type_name(data)}",
                expected="object",
                actual=_type_name(data),
            )],
        )

    errors: list[ParseError] = []

    # Step 3: Validate required top-level fields
    _check_required_field(data, "id", "", "string", errors)
    _check_required_field(data, "duration", "", "number", errors)
    _check_required_field(data, "tracks", "", "array", errors)
    _check_required_field(data, "clips", "", "array", errors)
    _check_required_field(data, "createdAt", "", "string", errors)

    # Step 4: Validate duration range if present and valid type
    if "duration" in data and isinstance(data["duration"], (int, float)) and not isinstance(data["duration"], bool):
        _validate_number_range(data["duration"], "duration", "", 0.001, 86400, errors)

    # Step 5: Validate tracks array items
    if "tracks" in data and isinstance(data["tracks"], list):
        for i, track_data in enumerate(data["tracks"]):
            _validate_track(track_data, i, errors)

    # Step 6: Validate clips array items
    if "clips" in data and isinstance(data["clips"], list):
        for i, clip_data in enumerate(data["clips"]):
            _validate_clip(clip_data, i, errors)

    # Step 7: Validate captions if present
    if "captions" in data and data["captions"] is not None:
        if not isinstance(data["captions"], list):
            errors.append(ParseError(
                path="captions",
                code="INVALID_TYPE",
                message=f"Field 'captions' expected array, got {_type_name(data['captions'])}",
                expected="array",
                actual=_type_name(data["captions"]),
            ))
        else:
            for i, cue_data in enumerate(data["captions"]):
                _validate_caption(cue_data, i, errors)

    # Step 8: Validate peaks if present
    if "peaks" in data and data["peaks"] is not None:
        if not isinstance(data["peaks"], dict):
            errors.append(ParseError(
                path="peaks",
                code="INVALID_TYPE",
                message=f"Field 'peaks' expected object, got {_type_name(data['peaks'])}",
                expected="object",
                actual=_type_name(data["peaks"]),
            ))

    # Step 9: Validate errors array if present
    if "errors" in data and data["errors"] is not None:
        if not isinstance(data["errors"], list):
            errors.append(ParseError(
                path="errors",
                code="INVALID_TYPE",
                message=f"Field 'errors' expected array, got {_type_name(data['errors'])}",
                expected="array",
                actual=_type_name(data["errors"]),
            ))

    # Step 10: Validate warnings array if present
    if "warnings" in data and data["warnings"] is not None:
        if not isinstance(data["warnings"], list):
            errors.append(ParseError(
                path="warnings",
                code="INVALID_TYPE",
                message=f"Field 'warnings' expected array, got {_type_name(data['warnings'])}",
                expected="array",
                actual=_type_name(data["warnings"]),
            ))

    # If there are errors, return them
    if errors:
        return ParseResult(ok=False, errors=errors)

    # Step 11: Build the manifest object
    manifest = RenderManifest.from_dict(data)
    return ParseResult(ok=True, manifest=manifest)
