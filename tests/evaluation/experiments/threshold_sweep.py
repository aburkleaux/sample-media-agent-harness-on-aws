"""Threshold + stability experiment for the two-tier judge.

For each golden session: run the judge N times (raw per-run scores to expose
variance), average each tier, then show PASS/FAIL at candidate thresholds. Lets
us pick the pass boundary by where classification is stable, and produces the
averaged tier scores used as golden labels.

Real calls, needs creds:
  AWS_PROFILE=megs-staging AWS_REGION=us-west-2 \
    uv run --extra dev python tests/evaluation/experiments/threshold_sweep.py
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
SESSION_PREFIXES = ["e2cb300b", "1b42d744", "abc839ca", "6309f154", "ffab73f8"]
RUNS = 3
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9]


def main():
    span, plan, memory = HarvestedSpanSource(), HarvestedPlanSource(), MemoryReader(profile=PROFILE)
    full_ids = {p: next((k for k in span._load() if k.startswith(p)), None) for p in SESSION_PREFIXES}
    # runs=1 judge; we call it RUNS times ourselves to keep each raw score.
    judge = GoalSuccessJudge(JudgeConfig(runs=1), profile=PROFILE)

    per_session = {}
    for prefix, full in full_ids.items():
        if not full:
            continue
        sc = Scenario(scenario_id=full, source_session=full)
        run = build_run(sc, span_source=span, plan_source=plan, memory_reader=memory)
        dispatch_runs, delivered_runs = [], []
        for _ in range(RUNS):
            d, dl, _ = judge._judge_once(run)
            if d is not None:
                dispatch_runs.append(d)
            if dl is not None:
                delivered_runs.append(dl)
        per_session[prefix] = {
            "dispatch_runs": dispatch_runs,
            "delivered_runs": delivered_runs,
            "dispatch_mean": _mean(dispatch_runs),
            "delivered_mean": _mean(delivered_runs),
            "dispatch_spread": _spread(dispatch_runs),
            "delivered_spread": _spread(delivered_runs),
        }
        print(f"{prefix}: dispatch {dispatch_runs} mean={per_session[prefix]['dispatch_mean']:.2f} "
              f"spread={per_session[prefix]['dispatch_spread']:.2f} | "
              f"delivered {delivered_runs} mean={per_session[prefix]['delivered_mean']:.2f}")

    print("\n=== dispatch_correct PASS/FAIL by threshold (on the 3-run mean) ===")
    print(f"{'threshold':>9} | " + " ".join(f"{p:>10}" for p in SESSION_PREFIXES))
    for thr in THRESHOLDS:
        row = []
        for p in SESSION_PREFIXES:
            m = per_session.get(p, {}).get("dispatch_mean")
            row.append("PASS" if (m is not None and m >= thr) else "FAIL")
        print(f"{thr:>9} | " + " ".join(f"{v:>10}" for v in row))

    out = os.path.join(os.path.dirname(__file__), "threshold_sweep_result.json")
    with open(out, "w") as f:
        json.dump(per_session, f, indent=2)
    print(f"\nwrote {out}")


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _spread(xs):
    return (max(xs) - min(xs)) if xs else float("nan")


if __name__ == "__main__":
    main()
