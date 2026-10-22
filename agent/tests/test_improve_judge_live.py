"""Live tests for the real LLM judge — makes real Bedrock calls.

Marked `live`; skipped automatically without AWS creds so normal test runs and CI
stay fast and offline. Run explicitly with real staging creds:

  AWS_PROFILE=megs-staging AWS_REGION=us-west-2 \
    uv run --extra dev pytest -m live agent/tests/test_improve_judge_live.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import Outcome, Plan, PlanNode, Run, Step  # noqa: E402
from improve.judge import GoalSuccessJudge, JudgeConfig  # noqa: E402

pytestmark = pytest.mark.live


def _creds_present() -> bool:
    return bool(os.environ.get("AWS_PROFILE") or os.environ.get("AWS_ACCESS_KEY_ID"))


requires_creds = pytest.mark.skipif(not _creds_present(),
                                    reason="live test needs AWS creds (AWS_PROFILE / keys)")


@requires_creds
def test_judge_scores_a_clearly_delivered_goal_high():
    run = Run(
        run_id="synthetic-pass",
        turns=[{"role": "USER", "text": "What is 2+2?"}],
        response="4.",
        trajectory=[Step(name="chat")],
    )
    judge = GoalSuccessJudge(JudgeConfig(goal_source="first_user", input_set=("goal", "response")),
                             profile=os.environ.get("AWS_PROFILE"))
    score = judge.judge(run)
    # A trivially-answered request: both tiers should score high (nothing to dispatch,
    # answer delivered).
    assert score.results_delivered.score is not None and score.results_delivered.score >= 0.9
    assert score.results_delivered.outcome is Outcome.PASS


@requires_creds
def test_judge_scores_a_refused_goal_low_on_delivery():
    run = Run(
        run_id="synthetic-fail",
        turns=[{"role": "USER", "text": "Trim the video to the first 30 seconds and give me the file."}],
        response="I can't help with that.",
        trajectory=[Step(name="chat")],
    )
    judge = GoalSuccessJudge(JudgeConfig(goal_source="first_user", input_set=("goal", "response")),
                             profile=os.environ.get("AWS_PROFILE"))
    score = judge.judge(run)
    # The request was refused / nothing delivered: results_delivered must be low.
    assert score.results_delivered.score is not None and score.results_delivered.score < 0.7
    assert score.results_delivered.outcome is Outcome.FAIL
