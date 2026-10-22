"""Custom domain evaluators for the agent harness.

Pure functions over a normalized ScenarioResult dict so they are unit-testable
without AWS and can be registered with the AgentCore evaluator dispatcher.

Normalized result shape::

    {
      "trajectory": ["dispatch_tool", "list_my_jobs", "get_job", ...],
      "turns": [{"input": str, "response_text": str}, ...],
      "manifests": [ {template, tracks:[{type, src}...]}, ... ],  # render manifests produced
      "plan": {"nodes": [{"id": str, "status": "completed"|...}]} | None,
    }

Each evaluator returns a CustomEvalResult. Evaluators are NON-APPLICABLE-safe:
when a scenario has nothing for an evaluator to judge (e.g. no manifest for the
ManifestValidator), it passes with score 1.0 and a "not applicable" note rather
than failing — mirroring the Requirement 9 semantics (validate IF produced).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

_VALID_TRACK_TYPES = {"video", "audio", "subtitle", "marker", "data", "image"}
_MEDIA_TRACK_TYPES = {"video", "audio"}


@dataclass
class CustomEvalResult:
    evaluator_name: str
    passed: bool
    score: float
    details: str
    checks: list = field(default_factory=list)


def _subsequence_index(seq: list[str], pred: Callable[[str], bool], start: int = 0) -> int:
    for i in range(start, len(seq)):
        if pred(seq[i]):
            return i
    return -1


# --- ManifestValidator (Req 9.4) -------------------------------------------

def manifest_validator(result: dict) -> CustomEvalResult:
    manifests = result.get("manifests") or []
    if not manifests:
        return CustomEvalResult("ManifestValidator", True, 1.0, "No manifest produced — not applicable.")

    checks: list[dict] = []
    ok = True
    for idx, m in enumerate(manifests):
        if not isinstance(m, dict):
            ok = False
            checks.append({"manifest": idx, "error": "not an object"})
            continue
        if not m.get("template"):
            ok = False
            checks.append({"manifest": idx, "error": "missing template"})
        tracks = m.get("tracks")
        if not isinstance(tracks, list) or len(tracks) == 0:
            ok = False
            checks.append({"manifest": idx, "error": "tracks must be a non-empty array"})
            continue
        for ti, track in enumerate(tracks):
            if not isinstance(track, dict):
                ok = False
                checks.append({"manifest": idx, "track": ti, "error": "track not an object"})
                continue
            ttype = track.get("type")
            if ttype not in _VALID_TRACK_TYPES:
                ok = False
                checks.append({"manifest": idx, "track": ti, "error": f"invalid track type {ttype!r}"})
            if ttype in _MEDIA_TRACK_TYPES and not track.get("src"):
                ok = False
                checks.append({"manifest": idx, "track": ti, "error": f"{ttype} track missing src"})
    details = "All manifests conform to nle-timeline schema." if ok else "Manifest schema violations found."
    return CustomEvalResult("ManifestValidator", ok, 1.0 if ok else 0.0, details, checks)


# --- AsyncJobFlowValidator (Req 9.5) ---------------------------------------

def _is_dispatch(name: str) -> bool:
    n = name.lower()
    return n == "dispatch_tool" or n.startswith("start_") or n.startswith("start")


def _is_poll(name: str) -> bool:
    n = name.lower()
    return n == "list_my_jobs" or "status" in n or n == "get_job"


def _is_retrieve(name: str) -> bool:
    n = name.lower()
    return "result" in n or n == "get_job"


def async_job_flow_validator(result: dict) -> CustomEvalResult:
    traj = result.get("trajectory") or []
    d = _subsequence_index(traj, _is_dispatch)
    if d < 0:
        return CustomEvalResult("AsyncJobFlowValidator", True, 1.0, "No async job dispatched — not applicable.")
    r = _subsequence_index(traj, _is_retrieve, d + 1)
    checks = [{"dispatch_at": d, "retrieve_at": r, "trajectory": traj}]
    if r < 0:
        return CustomEvalResult("AsyncJobFlowValidator", False, 0.0,
                                "Job dispatched but results never retrieved (dispatch→poll→retrieve incomplete).", checks)
    # Poll is optional, but if present should sit between dispatch and retrieve.
    p = _subsequence_index(traj, _is_poll, d + 1)
    ordered = (p < 0) or (d < p <= r)
    if not ordered:
        return CustomEvalResult("AsyncJobFlowValidator", False, 0.0,
                                "Async lifecycle out of order (expected dispatch → poll → retrieve).", checks)
    return CustomEvalResult("AsyncJobFlowValidator", True, 1.0, "Async job lifecycle correct (dispatch → poll → retrieve).", checks)


# --- DagCompletionValidator (Req 9.6) --------------------------------------

def dag_completion_validator(result: dict) -> CustomEvalResult:
    plan = result.get("plan")
    if not plan or not isinstance(plan, dict) or not plan.get("nodes"):
        return CustomEvalResult("DagCompletionValidator", True, 1.0, "No DAG plan produced — not applicable.")
    nodes = plan["nodes"]
    incomplete = [n.get("id", f"node{i}") for i, n in enumerate(nodes)
                  if not (isinstance(n, dict) and n.get("status") == "completed")]
    if incomplete:
        return CustomEvalResult("DagCompletionValidator", False, 0.0,
                                f"DAG incomplete: {len(incomplete)}/{len(nodes)} node(s) not completed: {incomplete}",
                                [{"incomplete": incomplete}])
    return CustomEvalResult("DagCompletionValidator", True, 1.0, f"All {len(nodes)} DAG nodes completed.")


# --- SessionContinuityValidator (Req 9.7) ----------------------------------

_LOST_CONTEXT_MARKERS = (
    "i don't have", "i do not have", "no context", "could you provide",
    "can you provide", "please share", "please provide", "i don't see",
    "which file", "what file are you referring", "don't recall", "do not recall",
)


def session_continuity_validator(result: dict) -> CustomEvalResult:
    turns = result.get("turns") or []
    if len(turns) < 2:
        return CustomEvalResult("SessionContinuityValidator", True, 1.0, "Single-turn — not applicable.")
    offenders = []
    for i, turn in enumerate(turns[1:], start=1):
        resp = (turn.get("response_text") or "").lower()
        for marker in _LOST_CONTEXT_MARKERS:
            if marker in resp:
                offenders.append({"turn": i, "marker": marker})
                break
    if offenders:
        return CustomEvalResult("SessionContinuityValidator", False, 0.0,
                                "Agent appears to have lost earlier context on a follow-up turn.", offenders)
    return CustomEvalResult("SessionContinuityValidator", True, 1.0, "Context retained across turns.")


# --- Registry ---------------------------------------------------------------

CUSTOM_EVALUATORS: dict[str, Callable[[dict], CustomEvalResult]] = {
    "ManifestValidator": manifest_validator,
    "AsyncJobFlowValidator": async_job_flow_validator,
    "DagCompletionValidator": dag_completion_validator,
    "SessionContinuityValidator": session_continuity_validator,
}


def run_custom_evaluators(names: list[str], result: dict) -> list[CustomEvalResult]:
    """Run the named custom evaluators independently; a failure/exception in one
    does not prevent the others (Requirement 9.2 / Property 7)."""
    out: list[CustomEvalResult] = []
    for name in names:
        fn = CUSTOM_EVALUATORS.get(name)
        if fn is None:
            out.append(CustomEvalResult(name, False, 0.0, "Evaluator not found."))
            continue
        try:
            out.append(fn(result))
        except Exception as e:  # independence: record + continue
            out.append(CustomEvalResult(name, False, 0.0, f"Evaluator raised: {e}"))
    return out
