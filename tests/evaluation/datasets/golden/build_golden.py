#!/usr/bin/env python3
"""Build the M1 golden dataset with labels COMPUTED FRESH by the real two-tier judge.

Labels are not copied from any cached score (those drift — Finding 3/DF-7). Each
session's expected outcome is the judge's 3-run-averaged two-tier verdict under
the locked config (full_conversation, threshold 0.7). Human confirmation of
near-boundary labels is deferred.

Real calls, needs creds:
  AWS_PROFILE=megs-staging AWS_REGION=us-west-2 \
    uv run --extra dev python tests/evaluation/datasets/golden/build_golden.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agent", "src"))

from improve.contract import Outcome, Scenario  # noqa: E402
from improve.evaluate import evaluate  # noqa: E402
from improve.judge import GoalSuccessJudge, JudgeConfig  # noqa: E402
from improve.observe.build_run import build_run, HarvestedPlanSource  # noqa: E402
from improve.observe.memory_reader import MemoryReader  # noqa: E402
from improve.observe.span_trajectory import HarvestedSpanSource  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.environ.get("AWS_PROFILE", "megs-staging")
# Includes a3a8fac0 (a crashed face-recognition node) as a clear delivery-FAIL anchor.
SESSION_PREFIXES = ["e2cb300b", "1b42d744", "abc839ca", "6309f154", "ffab73f8", "a3a8fac0"]

EVAL_CONFIG = {
    "evaluators": ["goal_judge", "AsyncJobFlowValidator", "DagCompletionValidator"],
    "gating_evaluators": ["dispatch_correct", "results_delivered"],
}


def build():
    span, plan, memory = HarvestedSpanSource(), HarvestedPlanSource(), MemoryReader(profile=PROFILE)
    judge = GoalSuccessJudge(
        JudgeConfig(goal_source="full_conversation",
                    input_set=("goal", "response", "trajectory", "plan"),
                    pass_threshold=0.7, runs=3),
        profile=PROFILE,
    )
    full_ids = {p: next((k for k in span._load() if k.startswith(p)), None) for p in SESSION_PREFIXES}

    records = []
    for prefix, full in full_ids.items():
        if not full:
            print(f"[warn] {prefix} not in harvest; skipping")
            continue
        scenario = Scenario(scenario_id=full, source_session=full,
                            claims={"goal": True, "async": True, "dag": True},
                            evaluators=EVAL_CONFIG["evaluators"])
        run = build_run(scenario, span_source=span, plan_source=plan, memory_reader=memory)
        verdict = evaluate(run, config=EVAL_CONFIG, judge=judge)
        tiers = {r.name: r for r in verdict.evaluator_results}
        dispatch = tiers.get("dispatch_correct")
        delivered = tiers.get("results_delivered")
        records.append({
            "scenario_id": full,
            "goal": run.goal if isinstance(run.goal, str) else "",
            "category": "media_dag",
            "evaluators": EVAL_CONFIG["evaluators"],
            "claims": {"goal": True, "async": True, "dag": True},
            "expected": {
                "overall": verdict.overall.value,
                "dispatch_correct": dispatch.score if dispatch else None,
                "results_delivered": delivered.score if delivered else None,
            },
            "label_provenance": "judge:3-run-mean threshold=0.7 goal_source=full_conversation",
            "source_session": full,
        })
        print(f"  {verdict.overall.value:8} {prefix} dispatch={dispatch.score if dispatch else None} "
              f"delivered={delivered.score if delivered else None}")

    out = os.path.join(HERE, "golden.jsonl")
    with open(out, "w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    n_fail = sum(1 for r in records if r["expected"]["overall"] == "FAIL")
    n_pass = sum(1 for r in records if r["expected"]["overall"] == "PASS")
    print(f"\nwrote {len(records)} golden records ({n_pass} PASS, {n_fail} FAIL) -> {out}")


if __name__ == "__main__":
    build()
