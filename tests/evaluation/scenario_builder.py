"""Build evaluation scenarios from harvested session transcripts.

Pure, unit-testable transforms (no AWS). Given a transcript
(``{session_id, turns, trajectory, prompts}`` from harvester.py), produce a
Scenario dict in the design's dataset schema
(.kiro/specs/agent-evaluation-suite/design.md → JSON Dataset Schema), with:
  - user prompts → turn inputs,
  - observed tool sequence → draft ``expected_trajectory``,
  - auto-classified ``category``,
  - ``assertions`` left as REVIEW stubs for curation.
"""
from __future__ import annotations

import re

VALID_CATEGORIES = [
    "render_video", "render_audio", "render_captions",
    "gateway_stems", "gateway_diarization", "gateway_stt",
    "multi_agent_planning", "error_handling",
    "skill_selection", "session_continuity",
]


def classify(prompts: list[str], trajectory: list[str]) -> str:
    """Heuristically assign a scenario category from prompts + observed tools."""
    text = " ".join(prompts).lower()

    def has(*words: str) -> bool:
        return any(w in text for w in words)

    # Most specific gateway workflows first.
    if has("stem", "vocal", "instrumental", "karaoke"):
        return "gateway_stems"
    if has("diariz", "speaker separation", "who spoke", "speaker label"):
        return "gateway_diarization"
    if has("transcrib", "speech to text", "stt"):
        return "gateway_stt"
    if has("caption", "subtitle", ".vtt", ".srt"):
        return "render_captions"
    # Planning / multi-step synthesis.
    if has("compare", "research", "synthesize", "plan ", "step by step") or "plan_and_execute_tool" in trajectory:
        return "multi_agent_planning"
    # Media rendering by asset type.
    if has(".mp4", ".mov", ".webm", "video"):
        return "render_video"
    if has(".wav", ".mp3", ".flac", ".m4a", "audio", "waveform", "podcast"):
        return "render_audio"
    # Multi-turn conversations that don't match a workflow → continuity.
    if len(prompts) >= 3:
        return "session_continuity"
    return "skill_selection"


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "session"


def dedup_consecutive(seq: list[str]) -> list[str]:
    """Collapse consecutive duplicate tool names (retries emit the same tool)."""
    out: list[str] = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out


def build_scenario(transcript: dict, index: int = 0) -> dict | None:
    """Return a Scenario dict, or None if the transcript has no user prompts."""
    prompts = transcript.get("prompts") or []
    if not prompts:
        return None
    trajectory = transcript.get("trajectory") or []
    sid = transcript.get("session_id") or f"s{index}"
    category = classify(prompts, trajectory)

    scenario_id = f"harvested-{category.replace('_', '-')}-{_slug(prompts[0], 24)}-{_slug(str(sid), 8)}"
    scenario_id = re.sub(r"-+", "-", scenario_id).strip("-")

    return {
        "scenario_id": scenario_id,
        "category": category,
        "description": f"Harvested from UI session {sid} ({len(prompts)} user turn(s)).",
        "mode": "fixed",
        "turns": [{"input": p} for p in prompts],
        "expected_trajectory": dedup_consecutive(trajectory),
        "assertions": [
            "REVIEW: agent completed the user's request without an error/empty response",
            "REVIEW: expected_trajectory reflects the intended tool usage",
        ],
        "tags": ["harvested", "needs-review"],
        "evaluators": [
            "Builtin.Correctness",
            "Builtin.GoalSuccessRate",
        ],
        "source": {"session_id": sid, "origin": "ui-harvest"},
    }


def build_dataset(transcripts: list[dict]) -> dict:
    """Build a full dataset ({"scenarios": [...]}) from transcripts."""
    scenarios = []
    for i, t in enumerate(transcripts):
        sc = build_scenario(t, i)
        if sc is not None:
            scenarios.append(sc)
    return {"scenarios": scenarios}


def category_counts(dataset: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sc in dataset.get("scenarios", []):
        counts[sc["category"]] = counts.get(sc["category"], 0) + 1
    return counts
