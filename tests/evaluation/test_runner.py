"""Unit tests for loader + runner (tests/evaluation/loader.py, runner.py)."""
from pathlib import Path

from tests.evaluation.loader import validate_scenario, load_dataset, filter_scenarios
from tests.evaluation.runner import (
    run_dataset, run_scenario, summarize, make_session_id, split_evaluators,
)
from tests.evaluation.invoker import TurnOutput


def _scenario(**over):
    base = {
        "scenario_id": "harvested-render-video-x-abc",
        "category": "render_video",
        "description": "d",
        "turns": [{"input": "Analyze video.mp4"}],
        "expected_trajectory": ["dispatch_tool"],
        "assertions": ["a"],
        "evaluators": ["Builtin.Correctness", "ManifestValidator"],
    }
    base.update(over)
    return base


class TestValidate:
    def test_valid(self):
        assert validate_scenario(_scenario()) == []

    def test_missing_required(self):
        sc = _scenario()
        del sc["turns"]
        assert any("turns" in e for e in validate_scenario(sc))

    def test_bad_id(self):
        assert any("scenario_id" in e for e in validate_scenario(_scenario(scenario_id="Bad_ID")))

    def test_bad_category(self):
        assert any("category" in e for e in validate_scenario(_scenario(category="nope")))

    def test_empty_turns(self):
        assert any("at least one" in e for e in validate_scenario(_scenario(turns=[])))

    def test_bad_mode(self):
        assert any("mode" in e for e in validate_scenario(_scenario(mode="weird")))


class TestSessionAndSplit:
    def test_session_id_min_length(self):
        assert len(make_session_id("x")) >= 33

    def test_split_evaluators(self):
        b, c = split_evaluators(["Builtin.Correctness", "ManifestValidator", "Unknown"])
        assert b == ["Builtin.Correctness"] and c == ["ManifestValidator"]


class TestRunner:
    def test_run_scenario_custom_eval_pass(self):
        def fake_invoker(prompt, sid):
            return TurnOutput(response_text="done", manifest_keys=[])
        report = run_scenario(_scenario(), fake_invoker)
        # ManifestValidator is N/A (no manifest) → passes
        assert report["overall_pass"] is True
        assert report["session_id"].startswith("eval-")
        assert any(c["evaluator_name"] == "ManifestValidator" for c in report["custom_results"])

    def test_run_scenario_manifest_fail(self):
        def fake_invoker(prompt, sid):
            return TurnOutput(response_text="rendered", manifest_keys=["k1"])
        def fake_fetch(key):
            return {"template": "nle-timeline", "tracks": [{"type": "video"}]}  # missing src → invalid
        report = run_scenario(_scenario(), fake_invoker, manifest_fetch=fake_fetch)
        assert report["overall_pass"] is False

    def test_run_scenario_invoker_error_recorded(self):
        def boom(prompt, sid):
            raise RuntimeError("unreachable")
        report = run_scenario(_scenario(evaluators=["ManifestValidator"]), boom)
        # Requirement 19: the invoker error is swallowed (not propagated) and the
        # scenario still produces a report with its evaluators run.
        assert report["custom_results"]
        assert report["session_id"].startswith("eval-")

    def test_run_dataset_and_summary(self):
        def fake_invoker(prompt, sid):
            return TurnOutput(response_text="ok")
        seen = []
        reports = run_dataset([_scenario(), _scenario(scenario_id="s2")], fake_invoker, on_report=seen.append)
        assert len(reports) == 2 and len(seen) == 2
        s = summarize(reports)
        assert s["total"] == 2 and s["passed"] == 2 and s["pass_rate"] == 1.0


class TestHarvestedDatasetLoads:
    def test_harvested_dataset_is_valid(self):
        path = Path("tests/evaluation/datasets/harvested/harvested.json")
        if not path.exists():
            return  # harvest not run in this environment
        valid, errors = load_dataset(str(path))
        assert len(valid) > 0
        assert errors == [], f"harvested dataset has invalid scenarios: {errors[:3]}"
        # filtering works
        vids = filter_scenarios(valid, category="render_video")
        assert all(s["category"] == "render_video" for s in vids)
