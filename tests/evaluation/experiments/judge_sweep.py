"""Judge-config sweep: measure how goal-source and input-set affect the score.

Runs the real LLM judge over a set of sessions under each (goal_source,
input_set) combination and charts the scores next to the AgentCore online-eval
GoalSuccessRate (the reference). Purpose: choose the judge config by measured
behavior, not assertion (REQ-016).

Usage (real calls, needs creds):
  AWS_PROFILE=megs-staging AWS_REGION=us-west-2 \
    uv run --extra dev python tests/evaluation/experiments/judge_sweep.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "agent", "src"))

from improve.contract import Scenario  # noqa: E402
from improve.judge import GoalSuccessJudge, JudgeConfig  # noqa: E402
from improve.observe.build_run import build_run, HarvestedPlanSource  # noqa: E402
from improve.observe.memory_reader import MemoryReader  # noqa: E402
from improve.observe.span_trajectory import HarvestedSpanSource  # noqa: E402

PROFILE = os.environ.get("AWS_PROFILE", "megs-staging")

# Sessions to sweep (short-id prefixes; resolved to full ids from the harvest).
SESSION_PREFIXES = ["e2cb300b", "1b42d744", "abc839ca", "6309f154", "ffab73f8"]

# The reference: AgentCore online-eval GoalSuccessRate (harvested).
REFERENCE_GOAL_SUCCESS = {
    "e2cb300b": 1.0, "1b42d744": 1.0, "abc839ca": 1.0, "6309f154": 1.0, "ffab73f8": 1.0,
}

GOAL_SOURCES = ["first_user", "all_user", "plan_state", "plan_or_first_user", "full_conversation"]
INPUT_SETS = [
    ("goal", "response"),
    ("goal", "response", "trajectory"),
    ("goal", "response", "trajectory", "plan"),
]


def main():
    span = HarvestedSpanSource()
    plan = HarvestedPlanSource()
    memory = MemoryReader(profile=PROFILE)
    full_ids = {p: next((k for k in span._load() if k.startswith(p)), None) for p in SESSION_PREFIXES}

    runs = {}
    for prefix, full in full_ids.items():
        if not full:
            print(f"[warn] {prefix} not in harvest; skipping")
            continue
        sc = Scenario(scenario_id=full, source_session=full, claims={"goal": True})
        runs[prefix] = build_run(sc, span_source=span, plan_source=plan, memory_reader=memory)

    results = []  # (goal_source, input_set, prefix, score, outcome)
    for goal_source in GOAL_SOURCES:
        for input_set in INPUT_SETS:
            judge = GoalSuccessJudge(
                JudgeConfig(goal_source=goal_source, input_set=input_set, pass_threshold=1.0),
                profile=PROFILE,
            )
            for prefix, run in runs.items():
                r = judge.judge(run)
                results.append({
                    "goal_source": goal_source,
                    "input_set": "+".join(input_set),
                    "session": prefix,
                    "score": r.score,
                    "outcome": r.outcome.value,
                    "reason": r.explanation[:80],
                })
                print(f"{goal_source:20} {'+'.join(input_set):32} {prefix}: "
                      f"score={r.score} {r.outcome.value}")

    out = os.path.join(os.path.dirname(__file__), "judge_sweep_result.json")
    with open(out, "w") as f:
        json.dump({"reference": REFERENCE_GOAL_SUCCESS, "results": results}, f, indent=2)
    print(f"\nwrote {out}")
    _chart(results)


def _chart(results):
    """Text chart: per (goal_source, input_set), mean score across sessions +
    agreement with the reference (both call PASS at threshold 1.0)."""
    from collections import defaultdict
    by_config = defaultdict(list)
    for r in results:
        by_config[(r["goal_source"], r["input_set"])].append(r)
    print("\n=== mean score & reference-agreement per config ===")
    print(f"{'goal_source':20} {'input_set':32} {'mean':>5} {'agree':>6}")
    for (gs, iset), rs in by_config.items():
        scores = [r["score"] for r in rs if r["score"] is not None]
        mean = sum(scores) / len(scores) if scores else float("nan")
        # agreement: our PASS (score>=1.0) matches reference PASS (1.0)
        agree = sum(1 for r in rs
                    if (r["score"] is not None)
                    and ((r["score"] >= 1.0) == (REFERENCE_GOAL_SUCCESS.get(r["session"], 0) >= 1.0)))
        print(f"{gs:20} {iset:32} {mean:5.2f} {agree}/{len(rs):>3}")


if __name__ == "__main__":
    main()
