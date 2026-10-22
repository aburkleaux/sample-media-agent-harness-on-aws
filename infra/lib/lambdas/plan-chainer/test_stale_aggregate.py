"""Regression harness for the "SUCCEEDED but most jobs not done" bug (stale aggregate).

Reproduces the real failure from session 7a51d5fc: a 6-node parallel plan where every
gateway job SUCCEEDED and every node row is completed, yet the published result.json showed
4 of 6 nodes as `pending`. Root cause: each completed gateway node read-modify-writes the ONE
shared manifest.json, so parallel nodes CLOBBER each other and the manifest keeps only the last
writers — while each node's durable per-node output file (plans/<s>/<p>/<node>/output) reliably
exists. The aggregate read the racy manifest, so clobbered-but-completed nodes resolved pending.

_assemble_aggregate_nodes must recover those lost nodes from the per-node output files so a
finished plan never publishes SUCCEEDED-with-pending.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402
import result_resolution as rr  # noqa: E402


class _ManifestRaceS3:
    """S3 double where the shared manifest LOST 4 of 6 node outputs to the write race, but every
    node's durable per-node output file is present (the reliable recovery source)."""

    def __init__(self, session, plan, present_in_manifest, per_node_outputs):
        self.session = session
        self.plan = plan
        self._manifest = {
            "plan_id": plan,
            "outputs": {nid: {"status": "completed", "output_type": "json", "value": val}
                        for nid, val in present_in_manifest.items()},
        }
        self._per_node = per_node_outputs  # nid -> raw_result dict

    def get_object(self, Bucket, Key):
        if Key.endswith("/manifest.json"):
            body = json.dumps(self._manifest).encode("utf-8")
            return {"Body": _Body(body)}
        # per-node output file: plans/<s>/<p>/<nid>/output
        if Key.endswith("/output"):
            nid = Key.rsplit("/", 2)[-2]
            if nid in self._per_node:
                return {"Body": _Body(json.dumps(self._per_node[nid]).encode("utf-8"))}
        raise Exception("NoSuchKey")


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")
    monkeypatch.setattr(handler, "ARTIFACTS_DOMAIN", "d.cloudfront.net")


def _diarize_value():
    # A completed diarization output that resolve_node turns into artifacts.
    return {"output": {"s3uri": "s3://proxy/micro-workflow/speech-diarization/exec-x"},
            "artifact_urls": ["s3://proxy/micro-workflow/speech-diarization/exec-x/final_transcript.json"]}


def test_manifest_race_recovers_lost_nodes_from_per_node_files(monkeypatch):
    session, plan = "sess-7a51", "plan-uuid"
    nodes_json = [{"id": n} for n in
                  ("extract_audio_1", "extract_audio_2", "extract_audio_3",
                   "transcript_1", "transcript_2", "transcript_3")]
    # Manifest kept ONLY the last two writers (the observed 7a51d5fc state): transcript_1/2.
    present = {"transcript_1": _diarize_value(), "transcript_2": _diarize_value()}
    # But every node's durable per-node output file exists.
    per_node = {n: _diarize_value() for n in
                ("extract_audio_1", "extract_audio_2", "extract_audio_3",
                 "transcript_1", "transcript_2", "transcript_3")}
    monkeypatch.setattr(handler, "_s3", _ManifestRaceS3(session, plan, present, per_node))

    nodes = handler._assemble_aggregate_nodes(session, plan, nodes_json, {"outputs":
             {"transcript_1": present["transcript_1"], "transcript_2": present["transcript_2"]}})
    # All six nodes now carry a completed result (the four clobbered ones recovered).
    assert set(nodes.keys()) == {n["id"] for n in nodes_json}
    for nid, entry in nodes.items():
        assert str(entry.get("status")).lower() == "completed", f"{nid} not recovered: {entry}"


def test_recovered_aggregate_has_no_pending_nodes(monkeypatch):
    session, plan = "sess-7a51", "plan-uuid"
    nodes_json = [{"id": n} for n in ("extract_audio_1", "transcript_1", "transcript_2")]
    present = {"transcript_1": _diarize_value(), "transcript_2": _diarize_value()}
    per_node = {n: _diarize_value() for n in ("extract_audio_1", "transcript_1", "transcript_2")}
    monkeypatch.setattr(handler, "_s3", _ManifestRaceS3(session, plan, present, per_node))

    manifest = {"outputs": present}
    nodes = handler._assemble_aggregate_nodes(session, plan, nodes_json, manifest)
    agg = rr.build_aggregate(plan_id=plan, status="SUCCEEDED", nodes=nodes)
    statuses = {n["node_id"]: n["status"] for n in agg["nodes"]}
    # The bug: extract_audio_1 was pending in a SUCCEEDED aggregate. After recovery it is not.
    assert "pending" not in [str(s).lower() for s in statuses.values()], statuses
    assert (agg.get("counts") or {}).get("pending", 0) == 0


def test_genuinely_missing_node_is_left_pending(monkeypatch):
    # A node with NO per-node output file (truly unfinished) must stay pending — recovery must
    # not fabricate a result.
    session, plan = "sess-x", "plan-y"
    nodes_json = [{"id": "n1"}, {"id": "n2"}]
    monkeypatch.setattr(handler, "_s3", _ManifestRaceS3(session, plan,
                        present_in_manifest={"n1": _diarize_value()}, per_node_outputs={"n1": _diarize_value()}))
    nodes = handler._assemble_aggregate_nodes(session, plan, nodes_json, {"outputs": {"n1": _diarize_value()}})
    assert str(nodes["n1"].get("status")).lower() == "completed"
    # n2 has neither manifest entry nor a per-node file → stays an empty/pending entry.
    assert not handler._entry_has_result(nodes["n2"])
