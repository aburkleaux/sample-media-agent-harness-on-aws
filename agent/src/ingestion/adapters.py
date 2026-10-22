"""M2C workflow outputs → the document builder's typed inputs (design Component 1, ``adapters.py``).

Pure. Every timestamp is converted here, once, to whole milliseconds rounded half up (Req 5.11). Conversion goes
through ``Decimal(str(x))`` so the decimal the workflow wrote is rounded, not its binary float: ``1.0005`` s is
1001 ms, where ``round(1.0005 * 1000)`` would give 1000. A value that cannot be read becomes ``None``, which the
builder counts as an omitted document (Req 5.14) instead of the build raising half-way.

Output shapes, measured on the staging M2C (fixtures under ``agent/tests/fixtures/ingestion/``):

- shot-segmentation ``shot-list``: ``{actual_duration_sec, shots: [{shot_id, start_frame, end_frame, start_time,
  end_time, selected_frames}]}`` (seconds).
- media-processing frame group ``frames``: ``{prefix: <key prefix>, keys: ["frame_0000006.jpg", ...]}``, where N
  is the SOURCE frame number, the numbering of ``start_frame``/``end_frame``/``selected_frames``.
- shot-metadata-enrichment ``shots`` (JSONL): ``{shot_id, description, objects: [{name}], setting,
  texts_on_screen, brands, sentiment, named_entities, people}``.
- speech-diarization ``transcript``: ``{audio_duration_sec, sentences: [{start, end, text, speaker_id}]}``.
- conversation-analysis ``analysis``: ``{audio_segments: [{start_time: "HH:MM:SS.mmm", end_time, segment_topic,
  key_events, named_entities}]}``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

_FRAME_RE = re.compile(r"frame_(\d+)\.jpe?g$", re.IGNORECASE)
_HMS_RE = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)$")


@dataclass(frozen=True)
class ShotIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    frame_uri: str  # "" when no captured frame lies inside the shot (Req 5.24)


@dataclass(frozen=True)
class SceneIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    description: str = ""
    objects: tuple[str, ...] = ()
    setting: str = ""
    on_screen_text: str = ""
    brands: tuple[str, ...] = ()
    sentiment: str = ""
    entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    text: str
    speaker: Optional[str]


@dataclass(frozen=True)
class TopicIn:
    start_ms: Optional[int]
    end_ms: Optional[int]
    summary: str
    entities: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------- time
def sec_to_ms(v: Any) -> Optional[int]:
    """Seconds (number or numeric string) → whole ms, half up. ``None`` for anything unreadable."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return int((d * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def hms_to_ms(v: Any) -> Optional[int]:
    """``HH:MM:SS(.fff)`` (conversation-analysis) or plain seconds → whole ms. ``None`` when malformed."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return sec_to_ms(v)
    m = _HMS_RE.match(str(v or "").strip())
    if not m:
        return None
    total = Decimal(int(m.group(1)) * 3600 + int(m.group(2)) * 60) + Decimal(m.group(3))
    return int((total * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def duration_ms(value_sec: Any) -> Optional[int]:
    """D for Req 5.12 / 12.9: ``None`` when missing, unreadable, or ≤ 0."""
    ms = sec_to_ms(value_sec)
    return ms if ms is not None and ms > 0 else None


# -------------------------------------------------------------------------------------------- lists
def strs(items: Any, *keys: str) -> tuple[str, ...]:
    """A list of strings or of objects carrying the first present of ``keys`` → non-blank strings, in order."""
    out: list[str] = []
    if isinstance(items, str):
        items = [items]
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                if isinstance(it.get(k), str) and it[k].strip():
                    out.append(it[k])
                    break
    return tuple(s for s in out if s.strip())


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


# ------------------------------------------------------------------------------------------- frames
def frame_number(key: str) -> Optional[int]:
    m = _FRAME_RE.search(key or "")
    return int(m.group(1)) if m else None


def pick_frames(shots: list[dict], manifest: dict, manifest_uri: str) -> dict[Any, str]:
    """``shot_id`` → S3 URI of the shot's representative frame (Req 5.24).

    Only frames with ``start_frame <= N <= end_frame``; the one nearest the shot's first ``selected_frames``
    entry (or ``floor((start_frame + end_frame) / 2)``), ties to the smaller N. A shot with no frame inside its
    span is absent from the result.
    """
    if not manifest_uri.startswith("s3://"):
        return {}
    bucket = manifest_uri[5:].split("/", 1)[0]
    prefix = str(manifest.get("prefix") or "").strip("/")
    if prefix.startswith("s3://"):
        prefix = prefix[5:].split("/", 1)[1] if "/" in prefix[5:] else ""
    frames: list[tuple[int, str]] = []
    for k in manifest.get("keys") or []:
        n = frame_number(str(k))
        if n is not None:
            key = f"{prefix}/{k}" if prefix else str(k)
            frames.append((n, f"s3://{bucket}/{key}"))
    frames.sort()
    out: dict[Any, str] = {}
    for sh in shots:
        try:
            s, e = int(sh["start_frame"]), int(sh["end_frame"])
        except (KeyError, TypeError, ValueError):
            continue
        sel = sh.get("selected_frames") or []
        try:
            target = int(sel[0]) if sel else (s + e) // 2
        except (TypeError, ValueError):
            target = (s + e) // 2
        inside = [f for f in frames if s <= f[0] <= e]
        if inside:
            out[sh.get("shot_id")] = min(inside, key=lambda f: (abs(f[0] - target), f[0]))[1]
    return out


# ------------------------------------------------------------------------------------------- inputs
def shots_input(shot_list: dict, frame_for: dict[Any, str]) -> list[ShotIn]:
    return [ShotIn(sec_to_ms(sh.get("start_time")), sec_to_ms(sh.get("end_time")),
                   frame_for.get(sh.get("shot_id"), ""))
            for sh in shot_list.get("shots") or [] if isinstance(sh, dict)]


def enrich_input(shot_list: dict, shot_md: list[dict]) -> list[SceneIn]:
    """One scene input per enrichment line, in the enrichment output's order, timed by its shot."""
    times = {sh.get("shot_id"): sh for sh in shot_list.get("shots") or [] if isinstance(sh, dict)}
    out: list[SceneIn] = []
    for md in shot_md:
        if not isinstance(md, dict):
            continue
        sh = times.get(md.get("shot_id")) or {}
        out.append(SceneIn(
            start_ms=sec_to_ms(sh.get("start_time")), end_ms=sec_to_ms(sh.get("end_time")),
            description=_s(md.get("description")),
            objects=strs(md.get("objects"), "name"),
            setting=_s(md.get("setting")),
            on_screen_text=", ".join(s.strip() for s in strs(md.get("texts_on_screen"), "text", "value", "content")),
            brands=strs(md.get("brands"), "name"),
            sentiment=_s(md.get("sentiment")),
            entities=strs(md.get("named_entities"), "name", "text") + strs(md.get("people"), "name"),
        ))
    return out


def has_speech(transcript: Optional[dict]) -> bool:
    """Req 4.7: some transcript sentence with non-blank text."""
    return any(isinstance(s, dict) and _s(s.get("text")).strip()
               for s in (transcript or {}).get("sentences") or [])


def diar_input(transcript: dict) -> list[TurnIn]:
    out: list[TurnIn] = []
    for s in transcript.get("sentences") or []:
        if not isinstance(s, dict):
            continue
        spk = s.get("speaker_id")
        out.append(TurnIn(sec_to_ms(s.get("start")), sec_to_ms(s.get("end")), _s(s.get("text")),
                          str(spk) if spk not in (None, "") else None))
    return out


def topic_summary(seg: dict) -> str:
    """conversation-analysis has no ``summary``: the topic title plus its key events."""
    title = _s(seg.get("segment_topic")).strip()
    events = "; ".join(e.strip() for e in strs(seg.get("key_events")))
    return f"{title}. {events}" if title and events else (title or events)


def topics_input(analysis: dict) -> list[TopicIn]:
    return [TopicIn(hms_to_ms(s.get("start_time")), hms_to_ms(s.get("end_time")), topic_summary(s),
                    strs(s.get("named_entities"), "name"))
            for s in analysis.get("audio_segments") or [] if isinstance(s, dict)]


def _num(v: Any) -> Optional[float]:
    """A positive finite number from an ffprobe value (they are often strings: ``"16981548"``), else ``None``."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and f == f and f != float("inf") else None


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(f) if f is not None else None


def _fps(rate: Any) -> Optional[float]:
    """``"24/1"`` / ``"30000/1001"`` → 24.0 / 29.97 (2 dp); ``"0/0"`` and junk → ``None``."""
    if not isinstance(rate, str) or "/" not in rate:
        return None
    n, _, d = rate.partition("/")
    num, den = _num(n), _num(d)
    return round(num / den, 2) if num and den else None


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", {})}


def technical_from_probe(probe: Any) -> Optional[dict]:
    """The media-processing ``probe.json`` → a compact, model-readable technical summary, or ``None``.

    Shape measured on the staging M2C (2026-10-09, ``micro-workflow/media-processing/<uuid>/probe.json``):
    ``{format_name, duration_sec, file_size, probe_info: {streams: [...ffprobe streams], format: {bit_rate,
    tags: {encoder}}}}``. Only an allowlist is copied: ``probe_info.format.filename`` is a presigned URL that
    carries a session token, and must never reach the asset row.
    """
    if not isinstance(probe, dict):
        return None
    info = probe.get("probe_info") if isinstance(probe.get("probe_info"), dict) else {}
    streams = [s for s in (info.get("streams") or []) if isinstance(s, dict)]
    fmt = info.get("format") if isinstance(info.get("format"), dict) else {}
    tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = _compact({
        "container": _s(probe.get("format_name") or fmt.get("format_name")) or None,
        "durationSec": _num(probe.get("duration_sec") or fmt.get("duration")),
        "sizeBytes": _int(probe.get("file_size") or fmt.get("size")),
        "bitRate": _int(fmt.get("bit_rate")),
        "encoder": _s(tags.get("encoder")) or None,
        "video": _compact({
            "codec": _s(v.get("codec_name")) or None, "profile": _s(v.get("profile")) or None,
            "width": _int(v.get("width")), "height": _int(v.get("height")),
            "fps": _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate")),
            "bitRate": _int(v.get("bit_rate")), "pixFmt": _s(v.get("pix_fmt")) or None,
        }) if v else None,
        "audio": _compact({
            "codec": _s(a.get("codec_name")) or None, "channels": _int(a.get("channels")),
            "sampleRate": _int(a.get("sample_rate")), "bitRate": _int(a.get("bit_rate")),
        }) if a else None,
    })
    return out if (out.get("video") or out.get("audio")) else None
