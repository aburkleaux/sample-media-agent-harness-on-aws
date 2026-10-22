"""Downstream dispatch must wait for an upstream node's async gateway job to reach a
terminal state — not just for the node to be marked 'completed'. A node is 'completed' the
moment its Task-Agent turn SUBMITS a fire-and-forget gateway job, long before that job
writes its output; dispatching a dependent off that premature 'completed' is the 369bbe3e
bug (t3_translate_fr ran ~40s before the diarization SFN finished and found no transcript).
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
    spec = importlib.util.spec_from_file_location("_chainer_gate", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

SFN = "arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-speech-diarization:ef073a1c"


class _FakeJobTable:
    def __init__(self, status):
        self.status = status

    def get_item(self, Key):
        return {"Item": {"jobId": Key["jobId"], "status": self.status}} if self.status else {}


# ---- per-node helper ----

def test_node_gateway_pending_running(monkeypatch):
    monkeypatch.setattr(pc, "_job_table", _FakeJobTable("RUNNING"))
    assert pc._node_gateway_pending({"status": "completed", "job_id": SFN}) is True


def test_node_gateway_pending_terminal(monkeypatch):
    for terminal in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"):
        monkeypatch.setattr(pc, "_job_table", _FakeJobTable(terminal))
        assert pc._node_gateway_pending({"status": "completed", "job_id": SFN}) is False


def test_node_gateway_pending_is_registry_based(monkeypatch):
    # Gating keys on REGISTRY PRESENCE, not an SFN-ARN prefix: an unregistered id (coordinator /
    # non-job ref) is absent → not pending; a REGISTERED fal queue job (request_id) that is still
    # running → pending (the fix that lets hours-long fal renders be waited on).
    class _Reg:
        def __init__(self, reg):
            self.reg = reg

        def get_item(self, Key):
            jid = Key["jobId"]
            return {"Item": {"jobId": jid, "status": self.reg[jid]}} if jid in self.reg else {}

    monkeypatch.setattr(pc, "_job_table", _Reg({"req_abc": "IN_PROGRESS"}))
    assert pc._node_gateway_pending({"status": "completed"}) is False               # no job_id
    assert pc._node_gateway_pending({"job_id": "plan-abc#t2"}) is False             # not registered
    assert pc._node_gateway_pending({"job_id": "req_abc"}) is True                  # registered fal, running


def test_node_gateway_pending_fail_open(monkeypatch):
    class _Boom:
        def get_item(self, Key):
            raise RuntimeError("ddb down")
    monkeypatch.setattr(pc, "_job_table", _Boom())
    assert pc._node_gateway_pending({"status": "completed", "job_id": SFN}) is False


# ---- integration: dependent gated on the upstream gateway job ----

def _plan_and_items(t2_status="completed", t2_job=SFN):
    nodes = [
        {"id": "t2_diarize", "depends_on": []},
        {"id": "t3_translate_fr", "depends_on": ["t2_diarize"]},
    ]
    plan_record = {
        "node_id": "PLAN", "plan_id": "p1", "actor_id": "a", "turn_id": "",
        "delegation_grant": "", "nodes_json": json.dumps(nodes),
    }
    items = [
        plan_record,
        {"node_id": "p1#t2_diarize", "plan_id": "p1", "status": t2_status, "job_id": t2_job},
        {"node_id": "p1#t3_translate_fr", "plan_id": "p1", "status": "pending"},
    ]
    return plan_record, items


def test_dependent_not_dispatched_while_gateway_running(monkeypatch):
    monkeypatch.setattr(pc, "_job_table", _FakeJobTable("RUNNING"))
    dispatched = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: dispatched.append(a[3]["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: dispatched.extend(n["id"] for n in a[3]))
    plan_record, items = _plan_and_items()
    pc._advance_plan("sess", plan_record, items)
    assert dispatched == [], "t3 must not dispatch while t2's diarization SFN is RUNNING"


def test_dependent_dispatched_once_gateway_terminal(monkeypatch):
    monkeypatch.setattr(pc, "_job_table", _FakeJobTable("SUCCEEDED"))
    dispatched = []
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: dispatched.append(a[3]["id"]))
    monkeypatch.setattr(pc, "_dispatch_ready_nodes", lambda *a, **k: dispatched.extend(n["id"] for n in a[3]))
    plan_record, items = _plan_and_items()
    pc._advance_plan("sess", plan_record, items)
    assert dispatched == ["t3_translate_fr"], "t3 must dispatch once the diarization SFN is terminal"
