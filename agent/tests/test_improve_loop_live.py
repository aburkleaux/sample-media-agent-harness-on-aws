"""Live end-to-end test: the loop scores the golden set with the real judge.

Verifies CRIT-M1-001 (a run CAN FAIL) and CRIT-M1-005 (output is Verdict with a
UtilityVector). Marked `live`; skipped without creds.

  AWS_PROFILE=megs-staging AWS_REGION=us-west-2 \
    uv run --extra dev pytest -m live agent/tests/test_improve_loop_live.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import Overall, UtilityVector, Verdict  # noqa: E402
from improve.loop import calibrate, load_config, load_golden, report, run_improvement_loop  # noqa: E402

pytestmark = pytest.mark.live

requires_creds = pytest.mark.skipif(
    not (os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID")),
    reason="live test needs AWS creds",
)


@requires_creds
def test_loop_output_is_universal_verdicts_with_utility():
    verdicts = run_improvement_loop(load_config())
    assert verdicts and all(isinstance(v, Verdict) for v in verdicts)
    assert all(isinstance(v.utility, UtilityVector) for v in verdicts)
    # per-tier scores are present and unambiguous
    for v in verdicts:
        names = {r.name for r in v.evaluator_results}
        assert "dispatch_correct" in names and "results_delivered" in names


# Sessions whose delivery score sits near the pass boundary and is not stable
# across judge runs (documented in experiments/FINDINGS.md). Their label is
# provisional until human confirmation, so we don't assert exact reproduction.
# Measured judge spread for a single 3-run mean: 0.133 over 5 trials on a3a8fac0
# (0.833 / 0.783 / 0.700 / 0.750 / 0.750), and build_golden recorded 0.617 for that
# same session — so the real spread crosses ~0.15. A label whose tier sits closer than
# this to the pass threshold is inside the noise and cannot be asserted on.
JUDGE_TIER_SPREAD = 0.15


def _label_is_outside_noise(expected: dict, threshold: float = 0.7) -> bool:
    """True only if BOTH tiers sit further than the measured spread from the threshold.

    A hand-maintained exemption list was the previous mechanism and it was wrong twice
    in one day: it named the session that USED to be borderline while the reader fix
    (OURS-5) moved every score. Deriving the exemption from the label's own margin is
    self-maintaining — and it makes the real problem visible instead of hiding it:
    measured 2026-08-18, only 2 of 6 golden labels (e2cb300b, ffab73f8) are outside the
    band. That is a property of the DATASET, not of this test, and it is the strongest
    argument for capturing more runs (TASK-M2-009) before any label anchors anything.
    """
    return all(abs(expected[tier] - threshold) > JUDGE_TIER_SPREAD
               for tier in ("dispatch_correct", "results_delivered"))


@requires_creds
def test_a_run_can_fail_and_verdicts_match_golden_labels():
    verdicts = run_improvement_loop(load_config())
    golden = {s.scenario_id: s for s in load_golden()}
    rep = report(verdicts)
    # CRIT-M1-001: at least one run FAILs (both-tier gating; delivery failures exist)
    assert rep["pass_rate"] < 1.0, "expected at least one FAIL in the golden set"
    # Only labels outside the measured judge spread can be asserted on. Skipped
    # sessions are COUNTED and reported, so shrinking coverage can never pass silently.
    asserted, skipped = [], []
    for v in verdicts:
        expected = golden[v.run_id].expected
        if not _label_is_outside_noise(expected):
            skipped.append(v.run_id[:8])
            continue
        asserted.append(v.run_id[:8])
        assert v.overall.value == expected.get("overall"), (
            f"{v.run_id[:12]}: {v.overall.value} != {expected.get('overall')} "
            f"(dispatch={expected['dispatch_correct']:.3f} "
            f"delivery={expected['results_delivered']:.3f})")

    print(f"\nlabel-reproduction asserted on {len(asserted)}/{len(verdicts)}: {asserted}; "
          f"inside noise band, not asserted: {skipped}")
    # If the dataset ever degrades to zero stable labels, this test would silently
    # assert nothing at all. Fail loudly instead.
    assert asserted, ("no golden label is outside the judge noise band — the dataset "
                      "can no longer validate anything; capture more runs")


@requires_creds
def test_calibration_reports_dispatch_tier_vs_agentcore():
    verdicts = run_improvement_loop(load_config())
    cal = calibrate(verdicts, "dispatch_correct")
    assert "agreement" in cal and cal["rows"]
