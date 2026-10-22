#!/usr/bin/env python3
"""Calculate Agent Olive segment timing from text.

Uses only the Python standard library and emits one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from typing import Sequence

_WORD_RE = re.compile(r"\b[\w]+(?:[’'-][\w]+)*\b", re.UNICODE)
_ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…)")
_PAUSE_RE = re.compile(r"[,;:!?]|(?<!\.)\.(?!\.)")


def estimate_seconds(text: str) -> tuple[int, int, int, float]:
    words = len(_WORD_RE.findall(text))
    ellipses = len(_ELLIPSIS_RE.findall(text))
    without_ellipses = _ELLIPSIS_RE.sub("", text)
    punctuation_pauses = len(_PAUSE_RE.findall(without_ellipses))
    if words == 0:
        raise ValueError("text must contain at least one word")
    seconds = 0.8 + words / 2.55 + 0.5 * ellipses + 0.15 * punctuation_pauses + 1.0
    return words, ellipses, punctuation_pauses, seconds


def round_up_grid(value: int, *, step: int, offset: int) -> int:
    if step <= 0:
        raise ValueError("grid step must be positive")
    if value <= offset:
        return offset
    return offset + step * math.ceil((value - offset) / step)


def frame_plan(
    seconds: float,
    *,
    fps: float,
    grid_step: int,
    grid_offset: int,
    minimum: int,
    maximum: int,
) -> dict:
    if fps <= 0:
        raise ValueError("fps must be positive")
    if minimum < 0 or maximum < minimum:
        raise ValueError("frame bounds must satisfy 0 <= minimum <= maximum")
    raw_frames = math.ceil(seconds * fps)
    minimum_on_grid = round_up_grid(minimum, step=grid_step, offset=grid_offset)
    frames = max(
        minimum_on_grid,
        round_up_grid(raw_frames, step=grid_step, offset=grid_offset),
    )
    if frames > maximum:
        raise ValueError(
            f"line requires {frames} frames after grid rounding, above maximum {maximum}; split the line"
        )
    if (frames - grid_offset) % grid_step != 0:
        raise ValueError("calculated frame count does not satisfy the configured grid")
    return {
        "mode": "frames",
        "fps": fps,
        "raw_frames": raw_frames,
        "frames": frames,
        "actual_seconds": frames / fps,
        "grid": {"step": grid_step, "offset": grid_offset},
        "bounds": {
            "minimum": minimum,
            "minimum_on_grid": minimum_on_grid,
            "maximum": maximum,
        },
    }


def duration_plan(seconds: float, allowed: Sequence[float], *, allow_auto: bool) -> dict:
    numeric = sorted({float(value) for value in allowed if math.isfinite(float(value)) and float(value) > 0})
    if not numeric:
        raise ValueError("at least one positive finite allowed duration is required")
    selected = next((value for value in numeric if value >= seconds), None)
    if selected is None:
        if allow_auto:
            return {
                "mode": "duration",
                "duration": "auto",
                "allowed_durations": numeric,
                "warning": "target exceeds every numeric duration; verify auto behavior or split the line",
            }
        raise ValueError(
            f"target {seconds:.3f}s exceeds maximum allowed duration {numeric[-1]:g}s; split the line"
        )
    return {
        "mode": "duration",
        "duration": int(selected) if selected.is_integer() else selected,
        "allowed_durations": [int(v) if v.is_integer() else v for v in numeric],
    }


def parse_durations(raw: str) -> list[float]:
    try:
        return [float(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("allowed durations must be comma-separated numbers") from exc


def _measure_frames(text: str, *, fps: float, grid_step: int, grid_offset: int, min_frames: int) -> tuple[float, int]:
    """Estimate seconds for text and round up to the frame grid (no maximum raise)."""
    _, _, _, seconds = estimate_seconds(text)
    raw_frames = math.ceil(seconds * fps)
    minimum_on_grid = round_up_grid(min_frames, step=grid_step, offset=grid_offset)
    frames = max(minimum_on_grid, round_up_grid(raw_frames, step=grid_step, offset=grid_offset))
    return seconds, frames


def pack_segments(
    lines: Sequence[str],
    *,
    fps: float,
    grid_step: int,
    grid_offset: int,
    min_frames: int,
    hard_max: int,
    target_min: int,
    target_max: int,
) -> tuple[list[dict], list[str]]:
    """Greedily combine consecutive lines into good-duration segments.

    Splits only at line boundaries; a segment grows until adding the next line
    would exceed ``target_max`` frames. A single line longer than ``target_max``
    becomes its own segment. Warns for segments below ``target_min`` (may freeze)
    or above ``hard_max`` (cannot fit the model).
    """
    if not (0 < target_min <= target_max <= hard_max):
        raise ValueError("frame bounds must satisfy 0 < target_min <= target_max <= hard_max")
    clean = [line.strip() for line in lines if line and line.strip()]
    if not clean:
        raise ValueError("no non-empty lines to pack")

    groups: list[list[str]] = []
    current: list[str] = []
    for line in clean:
        _, trial_frames = _measure_frames(
            " ".join(current + [line]), fps=fps, grid_step=grid_step,
            grid_offset=grid_offset, min_frames=min_frames)
        if current and trial_frames > target_max:
            groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)

    segments: list[dict] = []
    warnings: list[str] = []
    for index, group in enumerate(groups):
        text = " ".join(group)
        seconds, frames = _measure_frames(
            text, fps=fps, grid_step=grid_step, grid_offset=grid_offset, min_frames=min_frames)
        if frames > hard_max:
            warnings.append(f"segment {index} needs {frames} frames > hard max {hard_max}; split a sentence")
        elif frames < target_min:
            warnings.append(f"segment {index} is {frames} frames (< target min {target_min}); short segment may freeze")
        segments.append({
            "index": index,
            "text": text,
            "lines": group,
            "frames": frames,
            "target_seconds": seconds,
            "actual_seconds": frames / fps,
        })
    return segments, warnings


def parse_lines_arg(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("--lines must be a JSON array of strings") from exc
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise argparse.ArgumentTypeError("--lines must be a JSON array of strings")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="One spoken segment (single-line sizing mode)")
    parser.add_argument("--pack", action="store_true", help="Pack multiple lines into good-duration segments")
    parser.add_argument("--lines", type=parse_lines_arg, help="JSON array of script lines (with --pack)")
    parser.add_argument("--lines-file", help="Path to a JSON array of script lines (with --pack); '-' reads stdin")
    parser.add_argument("--mode", choices=("frames", "duration"), default="frames")
    parser.add_argument("--fps", type=float, default=24.0)
    parser.add_argument("--grid-step", type=int, default=8)
    parser.add_argument("--grid-offset", type=int, default=1)
    parser.add_argument("--min-frames", type=int, default=9)
    parser.add_argument("--max-frames", type=int, default=481)
    parser.add_argument("--target-min-frames", type=int, default=150, help="Good-duration floor for packing")
    parser.add_argument("--target-max-frames", type=int, default=250, help="Good-duration ceiling for packing")
    parser.add_argument(
        "--allowed-durations",
        type=parse_durations,
        default=parse_durations("3,4,5,6,7,8,9,10,11,12,13,14,15"),
    )
    parser.add_argument("--allow-auto", action="store_true")
    return parser


def _load_lines(args) -> list[str]:
    if args.lines is not None:
        return args.lines
    if args.lines_file:
        raw = sys.stdin.read() if args.lines_file == "-" else open(args.lines_file, encoding="utf-8").read()
        return parse_lines_arg(raw)
    raise ValueError("--pack requires --lines or --lines-file")


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.pack:
            segments, warnings = pack_segments(
                _load_lines(args),
                fps=args.fps,
                grid_step=args.grid_step,
                grid_offset=args.grid_offset,
                min_frames=args.min_frames,
                hard_max=args.max_frames,
                target_min=args.target_min_frames,
                target_max=args.target_max_frames,
            )
            result = {
                "mode": "pack",
                "fps": args.fps,
                "target_min_frames": args.target_min_frames,
                "target_max_frames": args.target_max_frames,
                "hard_max_frames": args.max_frames,
                "segment_count": len(segments),
                "total_frames": sum(s["frames"] for s in segments),
                "segments": segments,
                "warnings": warnings,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if not args.text:
            raise ValueError("provide --text for single-line sizing, or --pack with --lines/--lines-file")
        words, ellipses, punctuation_pauses, seconds = estimate_seconds(args.text)
        if args.mode == "frames":
            plan = frame_plan(
                seconds,
                fps=args.fps,
                grid_step=args.grid_step,
                grid_offset=args.grid_offset,
                minimum=args.min_frames,
                maximum=args.max_frames,
            )
        else:
            plan = duration_plan(seconds, args.allowed_durations, allow_auto=args.allow_auto)
        result = {
            "text": args.text,
            "words": words,
            "ellipses": ellipses,
            "punctuation_pauses": punctuation_pauses,
            "target_seconds": seconds,
            **plan,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
