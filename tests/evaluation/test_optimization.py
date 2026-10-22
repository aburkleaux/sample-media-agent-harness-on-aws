"""Unit tests for optimization pure helpers (tests/evaluation/optimization.py)."""
from tests.evaluation.optimization import (
    ConfigBundle, OptimizationRunner, target_low_scorers, RECOMMENDATION_TYPES,
)


def test_recommendation_types():
    assert set(RECOMMENDATION_TYPES) == {"SYSTEM_PROMPT_RECOMMENDATION", "TOOL_DESCRIPTION_RECOMMENDATION"}


def test_target_low_scorers_custom():
    reports = [
        {"scenario_id": "a", "custom_results": [{"evaluator_name": "ManifestValidator", "score": 0.0}]},
        {"scenario_id": "b", "custom_results": [{"evaluator_name": "ManifestValidator", "score": 1.0}]},
    ]
    low = target_low_scorers(reports, "ManifestValidator", threshold=0.8)
    assert [r["scenario_id"] for r in low] == ["a"]


def test_target_low_scorers_builtin():
    reports = [
        {"scenario_id": "a", "custom_results": [], "builtin_scores": {"Builtin.ToolSelectionAccuracy": 0.5}},
        {"scenario_id": "b", "custom_results": [], "builtin_scores": {"Builtin.ToolSelectionAccuracy": 0.95}},
    ]
    low = target_low_scorers(reports, "Builtin.ToolSelectionAccuracy", threshold=0.8)
    assert [r["scenario_id"] for r in low] == ["a"]


def test_create_config_bundle_from_recommendation():
    runner = OptimizationRunner(region="us-east-1")
    rec = {
        "recommendationId": "rec-abc-XYZ1234567",
        "status": "COMPLETED",
        "recommendationConfig": {"systemPrompt": "You are a sharper agent."},
    }
    bundle = runner.create_config_bundle("v2-improved", rec, version=2)
    assert isinstance(bundle, ConfigBundle)
    assert bundle.system_prompt == "You are a sharper agent."
    assert bundle.version == 2
    assert bundle.source_recommendation_id == "rec-abc-XYZ1234567"
    assert bundle.bundle_id.startswith("bundle-")
    # serializable
    assert "v2-improved" in bundle.to_json()


def test_start_recommendation_rejects_bad_type():
    runner = OptimizationRunner(region="us-east-1")
    try:
        runner.start_recommendation("n", "BOGUS", {})
        assert False, "expected ValueError"
    except ValueError:
        pass
