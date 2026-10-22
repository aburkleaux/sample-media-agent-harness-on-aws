"""Unit tests for scenario_builder (tests/evaluation/scenario_builder.py)."""
import re

from tests.evaluation.scenario_builder import (
    classify,
    dedup_consecutive,
    build_scenario,
    build_dataset,
    category_counts,
    VALID_CATEGORIES,
)

ID_PATTERN = re.compile(r"^[a-z0-9-]+$")


class TestClassify:
    def test_stems(self):
        assert classify(["separate the vocal stems from this track"], []) == "gateway_stems"

    def test_diarization(self):
        assert classify(["run speaker diarization on the meeting"], []) == "gateway_diarization"

    def test_stt(self):
        assert classify(["transcribe this audio"], []) == "gateway_stt"

    def test_captions(self):
        assert classify(["render the video with its .vtt subtitles"], []) == "render_captions"

    def test_video(self):
        assert classify(["analyze this video https://x/clip.mp4"], []) == "render_video"

    def test_audio(self):
        assert classify(["show the waveform for this podcast.wav"], []) == "render_audio"

    def test_planning_by_tool(self):
        assert classify(["do the thing"], ["plan_and_execute_tool"]) == "multi_agent_planning"

    def test_continuity_multi_turn(self):
        assert classify(["hi", "and then?", "what about earlier?"], []) == "session_continuity"

    def test_fallback_skill_selection(self):
        assert classify(["hello"], []) == "skill_selection"

    def test_all_categories_valid(self):
        for c in ("gateway_stems", "render_video", "skill_selection"):
            assert c in VALID_CATEGORIES


class TestDedupConsecutive:
    def test_collapses_runs(self):
        assert dedup_consecutive(["a", "a", "b", "b", "a"]) == ["a", "b", "a"]

    def test_empty(self):
        assert dedup_consecutive([]) == []


class TestBuildScenario:
    def test_none_without_prompts(self):
        assert build_scenario({"session_id": "s1", "prompts": [], "trajectory": []}) is None

    def test_builds_valid_scenario(self):
        t = {
            "session_id": "abc123def456",
            "prompts": ["Analyze this video https://x/clip.mp4", "how many clips?"],
            "trajectory": ["dispatch_tool", "dispatch_tool"],
        }
        sc = build_scenario(t)
        assert sc["category"] == "render_video"
        assert ID_PATTERN.match(sc["scenario_id"]), sc["scenario_id"]
        assert [turn["input"] for turn in sc["turns"]] == t["prompts"]
        assert sc["expected_trajectory"] == ["dispatch_tool"]  # consecutive dupes collapsed
        assert sc["mode"] == "fixed"
        assert sc["assertions"] and sc["description"]

    def test_scenario_id_matches_loader_pattern(self):
        t = {"session_id": "S3!!weird", "prompts": ["Transcribe THIS"], "trajectory": []}
        sc = build_scenario(t)
        assert ID_PATTERN.match(sc["scenario_id"]), sc["scenario_id"]
        assert sc["category"] == "gateway_stt"


class TestBuildDataset:
    def test_skips_empty_and_counts(self):
        transcripts = [
            {"session_id": "a", "prompts": ["separate stems"], "trajectory": ["dispatch_tool"]},
            {"session_id": "b", "prompts": [], "trajectory": []},  # skipped
            {"session_id": "c", "prompts": ["analyze video.mp4"], "trajectory": []},
        ]
        ds = build_dataset(transcripts)
        assert len(ds["scenarios"]) == 2
        counts = category_counts(ds)
        assert counts.get("gateway_stems") == 1
        assert counts.get("render_video") == 1
