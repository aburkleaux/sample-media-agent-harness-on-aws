"""Unit tests for the custom evaluators (tests/evaluation/evaluators.py)."""
from tests.evaluation.evaluators import (
    manifest_validator,
    async_job_flow_validator,
    dag_completion_validator,
    session_continuity_validator,
    run_custom_evaluators,
    CUSTOM_EVALUATORS,
)


class TestManifestValidator:
    def test_not_applicable_when_no_manifest(self):
        r = manifest_validator({"manifests": []})
        assert r.passed and "not applicable" in r.details.lower()

    def test_valid_manifest_passes(self):
        m = {"template": "nle-timeline", "tracks": [
            {"type": "video", "src": "https://x/v.mp4"},
            {"type": "audio", "src": "https://x/a.wav"},
            {"type": "subtitle"},
        ]}
        assert manifest_validator({"manifests": [m]}).passed

    def test_missing_template_fails(self):
        m = {"tracks": [{"type": "video", "src": "x"}]}
        assert not manifest_validator({"manifests": [m]}).passed

    def test_media_track_missing_src_fails(self):
        m = {"template": "nle-timeline", "tracks": [{"type": "video"}]}
        r = manifest_validator({"manifests": [m]})
        assert not r.passed and any("src" in c.get("error", "") for c in r.checks)

    def test_empty_tracks_fails(self):
        assert not manifest_validator({"manifests": [{"template": "nle-timeline", "tracks": []}]}).passed


class TestAsyncJobFlowValidator:
    def test_not_applicable_without_dispatch(self):
        assert async_job_flow_validator({"trajectory": ["artifact"]}).passed

    def test_full_flow_passes(self):
        r = async_job_flow_validator({"trajectory": ["dispatch_tool", "list_my_jobs", "get_job"]})
        assert r.passed

    def test_dispatch_without_retrieve_fails(self):
        r = async_job_flow_validator({"trajectory": ["dispatch_tool", "list_my_jobs"]})
        assert not r.passed

    def test_start_prefix_dispatch_and_results_retrieve(self):
        r = async_job_flow_validator({"trajectory": ["start_diarization", "get_diarization_status", "get_diarization_results"]})
        assert r.passed


class TestDagCompletionValidator:
    def test_not_applicable_without_plan(self):
        assert dag_completion_validator({"plan": None}).passed

    def test_all_completed_passes(self):
        plan = {"nodes": [{"id": "a", "status": "completed"}, {"id": "b", "status": "completed"}]}
        assert dag_completion_validator({"plan": plan}).passed

    def test_incomplete_fails(self):
        plan = {"nodes": [{"id": "a", "status": "completed"}, {"id": "b", "status": "running"}]}
        r = dag_completion_validator({"plan": plan})
        assert not r.passed and "b" in r.details


class TestSessionContinuityValidator:
    def test_not_applicable_single_turn(self):
        assert session_continuity_validator({"turns": [{"input": "hi", "response_text": "hello"}]}).passed

    def test_retained_context_passes(self):
        turns = [
            {"input": "analyze podcast.wav", "response_text": "It is 60 minutes long."},
            {"input": "how long was it?", "response_text": "It was 60 minutes."},
        ]
        assert session_continuity_validator({"turns": turns}).passed

    def test_lost_context_fails(self):
        turns = [
            {"input": "analyze podcast.wav", "response_text": "Done."},
            {"input": "how long was it?", "response_text": "Which file are you referring to? Please provide it again."},
        ]
        assert not session_continuity_validator({"turns": turns}).passed


class TestRegistryAndIndependence:
    def test_all_four_registered(self):
        assert set(CUSTOM_EVALUATORS) == {
            "ManifestValidator", "AsyncJobFlowValidator",
            "DagCompletionValidator", "SessionContinuityValidator",
        }

    def test_unknown_evaluator_recorded_not_raised(self):
        results = run_custom_evaluators(["NopeValidator", "ManifestValidator"], {"manifests": []})
        assert len(results) == 2
        assert results[0].evaluator_name == "NopeValidator" and not results[0].passed
        assert results[1].passed  # ManifestValidator still ran

    def test_independence_all_run(self):
        results = run_custom_evaluators(
            ["ManifestValidator", "AsyncJobFlowValidator", "DagCompletionValidator", "SessionContinuityValidator"],
            {"manifests": [], "trajectory": [], "plan": None, "turns": []},
        )
        assert len(results) == 4 and all(r.passed for r in results)
