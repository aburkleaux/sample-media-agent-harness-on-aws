"""Deterministic positional trim planner + verifier for multi-clip assembly.

Why this exists: when a reel trims N clips to N per-shot target durations, a model-written loop
that hand-indexes (``targets[j]`` for ``clips[i]``) can misassign one clip's target to another.
Observed (Meridian 8-shot): shot 8 was trimmed to shot 1's length (5.17s instead of 6.61s), so a
28.46s reel silently became 27.17s. Pairing by POSITION and verifying each output against its
target turns that silent error into an impossible / loud one.

Pure helpers (no ffmpeg) so the pairing + validation logic is unit-tested without media:
- ``build_trim_plan(clips, targets)`` → ordered [{index, clip, target}], pairing clip[i]→target[i].
  Raises if the counts differ or a target is non-positive — the misassignment class, caught up front.
- ``verify_trim(index, target, actual, tol)`` → raises if a trimmed clip's measured duration drifts
  from its target beyond ``tol`` (run after ffmpeg -t + ffprobe, so an over/under-trim fails loudly).
"""
from __future__ import annotations

from typing import Sequence


def build_trim_plan(clips: Sequence[str], targets: Sequence[float]) -> list[dict]:
    """Pair clips to targets BY POSITION. clip[i] is trimmed to target[i].

    Raises ValueError when len(clips) != len(targets) (the off-by-one / misassignment class) or a
    target is not a positive finite number. Returns a list of {index, clip, target} in order."""
    clips = list(clips)
    targets = list(targets)
    if len(clips) != len(targets):
        raise ValueError(
            f"clip/target count mismatch: {len(clips)} clips vs {len(targets)} targets — "
            f"pair by position, one target per clip (this is the shot-N-gets-shot-1's-length bug)."
        )
    plan: list[dict] = []
    for i, (clip, target) in enumerate(zip(clips, targets)):
        t = float(target)
        if not (t > 0) or t != t or t in (float("inf"), float("-inf")):  # >0 and finite
            raise ValueError(f"target for clip {i} ({clip!r}) must be positive and finite, got {target!r}")
        plan.append({"index": i, "clip": clip, "target": t})
    return plan


def verify_trim(index: int, target: float, actual: float, tol: float = 0.1) -> None:
    """Raise if a trimmed clip's measured duration drifts from its target beyond ``tol`` seconds.

    Call after ``ffmpeg -t <target>`` + an ``ffprobe`` of the output, for every clip. A silent
    over-trim (shot 8 → 5.17s when its target was 6.61s) then fails here instead of shipping a
    short reel."""
    if abs(float(actual) - float(target)) > tol:
        raise ValueError(
            f"clip {index} trim mismatch: target {float(target):.3f}s but measured "
            f"{float(actual):.3f}s (tol {tol}s) — the clip was trimmed to the wrong length."
        )
