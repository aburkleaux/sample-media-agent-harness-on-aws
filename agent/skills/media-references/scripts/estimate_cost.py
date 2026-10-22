#!/usr/bin/env python3
"""Estimate the cost of a fal.ai generation job before submitting a paid job.

Pure arithmetic over a rate you supply. This script deliberately embeds NO price
numbers: fal's catalog changes weekly, and a hardcoded table would go stale and
quietly lie. Fetch the current per-unit rate from a live pricing capability (the
fal `get_pricing` tool) and pass it as ``--rate`` together with the meter type the
model bills on (from its live schema).

Meter types (billing unit):
  per_megapixel  cost = rate * (width * height * frames) / 1_000_000  per segment
  per_second     cost = rate * seconds                                per segment
  per_run        cost = rate                                          per segment (flat)

Images are a single frame: for ``per_megapixel`` pass ``--segments N`` (each image is
one frame of width*height) or ``--lengths 1,1``; for a flat per-image model use
``per_run``. Video passes real per-segment frame counts or durations via ``--lengths``.

Emits one JSON object on stdout. Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Optional

_MEGAPIXEL = 1_000_000
_METERS = ("per_megapixel", "per_second", "per_run")


def parse_lengths(raw: str) -> list[float]:
    try:
        values = [float(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("lengths must be comma-separated numbers") from exc
    if not values:
        raise argparse.ArgumentTypeError("at least one length is required")
    return values


def resolve_lengths(
    *,
    lengths: Optional[list[float]],
    segments: Optional[int],
    default_length: Optional[float] = None,
) -> list[float]:
    """Return one entry per segment. ``lengths`` wins; otherwise repeat by count.

    ``default_length`` is the fill used for ``--segments`` without ``--lengths``.
    For ``per_megapixel`` this is 1.0 (one image frame); other meters use 0.0.
    """
    if lengths:
        return lengths
    if segments is not None:
        if segments <= 0:
            raise ValueError("segments must be a positive integer")
        fill = default_length if default_length is not None else 0.0
        return [fill] * segments
    raise ValueError("provide --lengths or --segments")


def frames_for(length: float, *, length_mode: str, fps: float) -> int:
    if length_mode == "frames":
        if length != int(length):
            raise ValueError(f"frame length {length} must be a whole number")
        frames = int(length)
    else:  # duration seconds -> frames
        if fps <= 0:
            raise ValueError("fps must be positive to convert seconds to frames")
        frames = math.ceil(length * fps)
    if frames <= 0:
        raise ValueError("each segment must have a positive frame count")
    return frames


def seconds_for(length: float, *, length_mode: str, fps: float) -> float:
    if length_mode == "duration":
        seconds = length
    else:  # frames -> seconds
        if fps <= 0:
            raise ValueError("fps must be positive to convert frames to seconds")
        seconds = length / fps
    if seconds <= 0:
        raise ValueError("each segment must have a positive duration")
    return seconds


def segment_cost(
    length: float,
    *,
    meter: str,
    rate: float,
    width: Optional[int],
    height: Optional[int],
    length_mode: str,
    fps: float,
) -> dict:
    if meter == "per_megapixel":
        if not width or not height or width <= 0 or height <= 0:
            raise ValueError("per_megapixel requires positive --width and --height")
        frames = frames_for(length, length_mode=length_mode, fps=fps)
        megapixels = (width * height * frames) / _MEGAPIXEL
        cost = rate * megapixels
        return {
            "frames": frames,
            "megapixels": megapixels,
            "cost_usd": cost,
        }
    if meter == "per_second":
        seconds = seconds_for(length, length_mode=length_mode, fps=fps)
        cost = rate * seconds
        return {
            "seconds": seconds,
            "cost_usd": cost,
        }
    # per_run: flat per segment; length is ignored
    return {"cost_usd": rate}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meter", choices=_METERS, required=True, help="How the model bills (from its live schema)")
    parser.add_argument("--rate", type=float, required=True, help="Current per-unit price; fetch it live with get_pricing, do not guess")
    parser.add_argument(
        "--rate-source",
        default="unspecified",
        help="Provenance label for the rate, e.g. 'live:get_pricing:2026-09-20'",
    )
    parser.add_argument("--width", type=int, help="Output width in pixels (per_megapixel)")
    parser.add_argument("--height", type=int, help="Output height in pixels (per_megapixel)")
    parser.add_argument("--length-mode", choices=("frames", "duration"), default="frames")
    parser.add_argument("--fps", type=float, default=24.0, help="Frames per second, used to convert between frames and seconds")
    parser.add_argument("--lengths", type=parse_lengths, help="Comma-separated per-segment lengths (frames or seconds per --length-mode)")
    parser.add_argument("--segments", type=int, help="Segment count when lengths are uniform or ignored (per_run)")
    parser.add_argument("--currency", default="USD")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not math.isfinite(args.rate) or args.rate < 0:
            raise ValueError("rate must be a finite, non-negative number")
        default_length = 1.0 if args.meter == "per_megapixel" else None
        lengths = resolve_lengths(
            lengths=args.lengths, segments=args.segments, default_length=default_length
        )
        segments = []
        for index, length in enumerate(lengths):
            detail = segment_cost(
                length,
                meter=args.meter,
                rate=args.rate,
                width=args.width,
                height=args.height,
                length_mode=args.length_mode,
                fps=args.fps,
            )
            detail["index"] = index
            if args.meter != "per_run":
                detail["length"] = length
                detail["length_mode"] = args.length_mode
            segments.append(detail)
        total = sum(segment["cost_usd"] for segment in segments)
        result = {
            "meter": args.meter,
            "rate": args.rate,
            "rate_source": args.rate_source,
            "currency": args.currency,
            "fps": args.fps,
            "width": args.width,
            "height": args.height,
            "segment_count": len(segments),
            "segments": segments,
            "total_cost_usd": total,
            "total_cost_display": f"${total:.4f}",
            "note": "Estimate only. Rate is caller-supplied; verify against the live catalog before spending.",
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
