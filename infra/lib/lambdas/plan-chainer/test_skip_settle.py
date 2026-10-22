"""Phase 1.5 C: a terminally-failed node must skip its dependents so the plan settles
(instead of hanging RUNNING), while a retry-eligible failure is retried, not skipped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_skip", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_skip"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


def _plan_record(nodes):
    return {"node_id": "PLAN", "plan_id": "P", "nodes_json": json.dumps(nodes),
            "actor_id": "", "turn_id": "", "delegation_grant": "", "model_id": ""}


def test_failed_node_skips_dependents_and_settles(monkeypatch):
    skipped, completed = [], []
    monkeypatch.setattr(pc, "_mark_node_skipped", lambda s, p, n, r: skipped.append(n))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda s, p, nj, ns, **_: completed.append(dict(ns)))

    def _no_dispatch(*a, **k):
        raise AssertionError("should not dispatch a node whose only path is through a failed dep")
    monkeypatch.setattr(pc, "_dispatch_node", _no_dispatch)

    # t1 failed with NO error -> not retry-eligible -> terminal. t2 -> t3 chain behind it.
    nodes = [{"id": "t1", "depends_on": []},
             {"id": "t2", "depends_on": ["t1"]},
             {"id": "t3", "depends_on": ["t2"]}]
    pr = _plan_record(nodes)
    items = [pr,
             {"node_id": "t1", "status": "failed"},
             {"node_id": "t2", "status": "pending"},
             {"node_id": "t3", "status": "pending"}]
    pc._advance_plan("sess", pr, items)

    assert sorted(skipped) == ["t2", "t3"]           # transitive skip
    assert len(completed) == 1                        # plan settled in this pass
    final = completed[0]
    assert final["t1"] == "failed" and final["t2"] == "skipped" and final["t3"] == "skipped"


def test_retryable_failure_is_retried_not_skipped(monkeypatch):
    monkeypatch.setattr(pc, "MAX_NODE_RETRIES", 1)
    skipped, completed, retried = [], [], []
    monkeypatch.setattr(pc, "_mark_node_skipped", lambda s, p, n, r: skipped.append(n))
    monkeypatch.setattr(pc, "_mark_run_complete", lambda *a, **k: completed.append(True))
    monkeypatch.setattr(pc, "_retry_failed_node",
                        lambda s, p, a, node_def, *rest, **k: retried.append(node_def["id"]))
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None)

    nodes = [{"id": "t1", "depends_on": []}, {"id": "t2", "depends_on": ["t1"]}]
    pr = _plan_record(nodes)
    # truncation error recorded (Phase 1.5 B) -> t1 is retry-eligible
    items = [pr,
             {"node_id": "t1", "status": "failed", "error": "output exceeded the node token limit", "retries": "0"},
             {"node_id": "t2", "status": "pending"}]
    pc._advance_plan("sess", pr, items)

    assert retried == ["t1"]     # truncation-aware retry fires
    assert skipped == []         # a node being retried is NOT terminal, so nothing is skipped
    assert completed == []       # plan not settled — waiting on the retry


def test_mark_node_skipped_writes_both_stores(monkeypatch):
    writes = {"plan": None, "job": None}

    class _PT:
        def update_item(self, **kw):
            writes["plan"] = kw

    class _JT:
        def update_item(self, **kw):
            writes["job"] = kw

    monkeypatch.setattr(pc, "_plan_table", _PT())
    monkeypatch.setattr(pc, "_job_table", _JT())
    pc._mark_node_skipped("sess", "P", "t2", "upstream dependency failed")

    assert writes["plan"]["ExpressionAttributeValues"][":s"] == "skipped"
    assert writes["plan"]["Key"]["node_id"] == "t2"
    assert writes["job"]["ExpressionAttributeValues"][":s"] == "SKIPPED"
    assert writes["job"]["Key"]["jobId"] == "P#t2"
