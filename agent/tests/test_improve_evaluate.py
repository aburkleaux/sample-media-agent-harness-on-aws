"""Offline tests for the evaluate() gating / can-show-RED core — no LLM, no creds.

This is the fail-path coverage CI can see: it proves a run can FAIL, that
UNSCORED is never PASS, and that absent-but-expected evidence escalates to RED
intrinsically (not via a scenario label). The LLM judge is replaced by a
deterministic stub that returns fixed tier scores — this is a test double for
the gating logic, NOT a mock of the model under evaluation (the real judge has
its own live tests).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve_fixtures import ONEB42D744, A3A8FAC0, real_plan, real_trajectory  # noqa: E402

from improve.contract import (  # noqa: E402
    NO_DATA, UNMEASURED, EvaluatorResult, Outcome, Overall, Plan, PlanNode, Run, Step,
)
from improve.evaluate import evaluate, gate  # noqa: E402
from improve.judge import TwoTierScore  # noqa: E402


class StubJudge:
    """Deterministic two-tier judge double — returns preset tier scores."""

    def __init__(self, dispatch: float, delivered: float, threshold: float = 0.7):
        self._dispatch, self._delivered, self._threshold = dispatch, delivered, threshold

    def judge(self, run):
        def result(name, score):
            outcome = Outcome.PASS if score >= self._threshold else Outcome.FAIL
            return EvaluatorResult(name, "llm", outcome, score, "stub")
        return TwoTierScore(result("dispatch_correct", self._dispatch),
                            result("results_delivered", self._delivered), "stub")


def _real_run(session=ONEB42D744):
    """A Run built from REAL harvested plan + trajectory (REQ-017).

    Default is 1b42d744 — the verified-clean reference: 4 genuinely independent nodes,
    all reaching gateway_status=SUCCEEDED, every timestamp correctly ordered. Using a
    clean real plan means these gating tests isolate the JUDGE tiers, which is what
    they are about; a plan with its own defect would confound them.
    """
    return Run(run_id=session, goal="g", response="done",
               trajectory=real_trajectory(session), plan=real_plan(session),
               resources={"latency_ms": 100})


CFG = {"evaluators": ["goal_judge", "AsyncJobFlowValidator", "DagCompletionValidator"],
       "gating_evaluators": ["dispatch_correct", "results_delivered"]}


def test_both_tiers_pass_is_overall_pass():
    v = evaluate(_real_run(), config=CFG, judge=StubJudge(1.0, 1.0))
    assert v.overall is Overall.PASS


def test_delivery_fail_is_overall_fail():
    v = evaluate(_real_run(), config=CFG, judge=StubJudge(1.0, 0.0))
    assert v.overall is Overall.FAIL
    assert "results_delivered" in v.reason


def test_dispatch_fail_is_overall_fail():
    """a3a8fac0 is the real dispatch-tier failure (judge dispatch 0.62 < 0.7)."""
    v = evaluate(_real_run(A3A8FAC0), config=CFG, judge=StubJudge(0.0, 1.0))
    assert v.overall is Overall.FAIL
    assert "dispatch_correct" in v.reason


def test_absent_plan_evidence_is_red_and_makes_the_verdict_unscored_not_pass():
    """Missing evidence on the ONLY gating evaluator is UNSCORED, and above all not PASS.

    The evaluator's own result stays NO_DATA (RED, evidence-intrinsic), which is the claim
    this test was written to pin and which is unchanged. What moved is the verdict: gating on
    a check that could not measure anything now yields UNSCORED rather than FAIL, because
    "we could not look" and "we looked and it was broken" route to different owners. The
    can-show-RED invariant is asserted directly here so it cannot be lost in the rename.
    """
    # A model span and a turn, so this classifies `conversational` and the config's evaluator
    # and gating lists apply (they describe that class). Without them the run would be
    # `unclassified`, which by design runs nothing.
    run = Run(run_id="r", goal="g", response=NO_DATA,
              trajectory=[Step(name="chat"), Step(name="execute_tool dispatch_tool")],
              plan=NO_DATA, turns=[{"role": "USER", "text": "g"}], resources={})
    # dispatched async but NO plan captured -> DagCompletion NO_DATA (RED, intrinsic)
    v = evaluate(run, config={"evaluators": ["DagCompletionValidator"],
                              "gating_evaluators": ["DagCompletionValidator"]},
                 judge=StubJudge(1.0, 1.0))
    dag = next(r for r in v.evaluator_results if r.name == "DagCompletionValidator")
    assert dag.outcome is Outcome.NO_DATA
    assert v.overall is Overall.UNSCORED
    assert v.overall is not Overall.PASS
    assert "could not measure" in v.reason


def test_genuinely_not_applicable_does_not_fail():
    # No async dispatch and no plan -> DagCompletion NOT_APPLICABLE (not RED),
    # AsyncFlow NOT_APPLICABLE. With only those gating, nothing scorable -> UNSCORED.
    run = Run(run_id="r", goal="g", response="hi", trajectory=[Step(name="chat")], plan=NO_DATA,
              resources={})
    v = evaluate(run, config={"evaluators": ["AsyncJobFlowValidator", "DagCompletionValidator"],
                              "gating_evaluators": ["AsyncJobFlowValidator", "DagCompletionValidator"]},
                 judge=StubJudge(1.0, 1.0))
    assert v.overall is Overall.UNSCORED  # never PASS on no-scorable-evidence


def test_unscored_is_never_pass_when_no_gating_evaluator_scores():
    run = Run(run_id="r", goal="g", response="hi", trajectory=[Step(name="chat")], plan=NO_DATA)
    v = evaluate(run, config={"evaluators": ["DagCompletionValidator"],
                              "gating_evaluators": ["DagCompletionValidator"]},
                 judge=StubJudge(1.0, 1.0))
    assert v.overall is not Overall.PASS


def test_utility_flags_unmeasured_tokens_not_zero():
    run = _real_run()   # resources has latency but no tokens
    v = evaluate(run, config=CFG, judge=StubJudge(1.0, 1.0))
    assert v.utility.terms["tokens"] is UNMEASURED
    assert v.utility.terms["latency_ms"] == 100


def test_a_measured_failure_beside_an_unmeasurable_check_is_fail_but_says_so():
    """FAIL wins over NO_DATA, because a measured failure is real evidence. But a failure
    established on partial evidence is weaker than one established on all of it, so the
    unmeasured sibling is named rather than dropped."""
    results = [
        EvaluatorResult("dispatch_correct", "llm", Outcome.FAIL, 0.2, "misrouted"),
        EvaluatorResult("results_delivered", "llm", Outcome.NO_DATA, None, "no response"),
    ]
    overall, reason = gate(results, ("dispatch_correct", "results_delivered"))
    assert overall is Overall.FAIL
    assert "dispatch_correct:FAIL" in reason
    assert "on partial evidence" in reason and "results_delivered:NO_DATA" in reason


def test_all_not_applicable_gating_is_unscored_never_a_free_pass():
    """NOT_APPLICABLE means the dimension does not exist here, so it cannot fail anything. It
    also cannot vouch for anything, so a run with nothing else to stand on is UNSCORED."""
    results = [EvaluatorResult("DagCompletionValidator", "code", Outcome.NOT_APPLICABLE, None,
                               "no DAG and no async dispatch")]
    overall, reason = gate(results, ("DagCompletionValidator",))
    assert overall is Overall.UNSCORED
    assert overall is not Overall.PASS
    assert "NOT_APPLICABLE" in reason


def test_not_applicable_does_not_block_a_pass_earned_elsewhere():
    """The asymmetry with NO_DATA: an absent DIMENSION is not missing evidence, so it must not
    turn a real pass into UNSCORED."""
    results = [
        EvaluatorResult("ControlPlaneOpValidator", "code", Outcome.PASS, 1.0, "ok"),
        EvaluatorResult("ToolDiscoveryValidator", "code", Outcome.NOT_APPLICABLE, None, "none"),
    ]
    overall, _ = gate(results, ("ControlPlaneOpValidator", "ToolDiscoveryValidator"))
    assert overall is Overall.PASS
