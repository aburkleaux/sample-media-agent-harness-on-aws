"""Render keys of plan nodes are plan-scoped, on every path (staging session d6a560a8).

Three Phase 1 plans — one per video — shared node ids, so all three wrote
``render/sg1_segment_shots.json`` / ``render/sg2_diarize_speech.json`` and each video's tab showed
whichever manifest survived. The key is ``<plan_id>--<node_id>`` on the in-turn render-group path, the
job-id path and the artifact-path path, so they still converge on one manifest per node.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hooks.provider as provider  # noqa: E402
from hooks import actions  # noqa: E402
from render.keys import plan_node_render_key  # noqa: E402
from render_agent import render_key_from_artifact_paths  # noqa: E402

SID = "d6a560a8-549e-4878-8c78-56f32651dcee"
CDN = "https://dexamplecdn002.cloudfront.net"


def _aggregate(plan_id: str, video: str) -> dict:
    return {"schema": "plan-result/v1", "plan_id": plan_id, "nodes": [
        {"node_id": "sg1_segment_shots", "tool": "shot-segmentation___get_job_status",
         "artifacts": [{"url": f"{CDN}/artifacts/{SID}/output/{plan_id}/sg1_segment_shots/shots.json",
                        "role": "analysis"},
                       {"url": f"{CDN}/projects/p1/{video}.mp4", "role": "source"}]}]}


def test_two_plans_with_the_same_node_id_get_distinct_keys(monkeypatch):
    monkeypatch.setattr(provider, "_recorded_source_media_for_node", lambda *a: [], raising=False)
    keys = []
    for plan_id, video in (("d1b060fd-7514", "meridian"), ("92f2811a-723f", "nba")):
        captured = []
        monkeypatch.setattr(actions, "_buffer_render",
                            lambda urls, src, label, render_key="", tool_name="": captured.append(render_key))
        monkeypatch.setattr(actions, "_batch_is_open", lambda: True)
        # Isolate the keying from the session/dedup gates other tests leave bound.
        monkeypatch.setattr(actions, "_drop_foreign_session_urls", lambda urls: list(urls))
        monkeypatch.setattr(actions, "_is_plan_node_clip", lambda urls: False)
        monkeypatch.setattr(actions, "_foreign_session_in", lambda ctx: None)
        monkeypatch.setattr(actions, "_mirror_external_media", lambda urls: list(urls))
        groups = provider._aggregate_artifacts(json.dumps(_aggregate(plan_id, video)))["groups"]
        actions.render_action({"urls": "result.urls"},
                              {"result": {"urls": [u for g in groups for u in g["urls"]], "render_groups": groups}})
        keys.append(captured)
    assert keys == [["d1b060fd-7514--sg1_segment_shots"], ["92f2811a-723f--sg1_segment_shots"]]


def test_the_job_path_and_the_artifact_path_agree_with_the_render_groups(monkeypatch):
    plan, node = "92f2811a-723f-4a4d-a29e-850c8292ad42", "sg1_segment_shots"
    arn = "arn:aws:states:us-west-2:1:execution:m2c-shot-segmentation:2d2596d9"
    monkeypatch.setattr(provider, "_job_row_lookup",
                        lambda jid: {"jobId": arn, "planNodeId": node, "runId": plan}, raising=False)
    expected = plan_node_render_key(plan, node)
    assert expected == f"{plan}--{node}"
    assert provider.canonical_render_key(arn) == expected
    assert render_key_from_artifact_paths(
        [f"{CDN}/artifacts/{SID}/output/{plan}/{node}/shots.json"]) == expected
    # The same node in another plan is another key.
    assert render_key_from_artifact_paths(
        [f"{CDN}/artifacts/{SID}/output/other-plan/{node}/shots.json"]) != expected


def test_no_plan_keeps_the_bare_node_and_no_node_keeps_nothing():
    assert plan_node_render_key(None, "sg1") == "sg1"
    arn = "arn:aws:states:us-west-2:1:execution:m2c:894d044a"
    assert plan_node_render_key("p", arn) == arn  # a standalone job's node id is its own key
    assert plan_node_render_key("p", "") == ""
    assert plan_node_render_key("p/1", "a b") == "p_1--a_b"
