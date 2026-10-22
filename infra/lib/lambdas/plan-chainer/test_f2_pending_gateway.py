"""F2: the plan must not be announced complete while a spawned gateway job is still
running (the premature 'workflow finished' notification). _has_pending_gateway_job
looks each node's gateway job up by primary key and defers when any is non-terminal.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_f2", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_f2"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

ARN = "arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-audio-stem:0345192f-abc"


class _FakeJobTable:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def get_item(self, Key):
        self.calls.append(Key["jobId"])
        return {"Item": {"jobId": Key["jobId"], "status": self.status}} if self.status else {}


def test_defers_while_gateway_job_running(monkeypatch):
    monkeypatch.setattr(pc, "_job_table", _FakeJobTable("RUNNING"))
    node_states = {"t1": {"status": "completed", "job_id": ARN}}
    assert pc._has_pending_gateway_job(node_states) is True


def test_completes_when_gateway_job_terminal(monkeypatch):
    for terminal in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"):
        monkeypatch.setattr(pc, "_job_table", _FakeJobTable(terminal))
        assert pc._has_pending_gateway_job({"t1": {"status": "completed", "job_id": ARN}}) is False


class _RegistryJobTable:
    """Returns an Item only for jobIds it knows (registered jobs); {} otherwise — like the real
    JobRegistry. Used to verify gating now keys on REGISTRY PRESENCE, not an SFN-ARN prefix."""

    def __init__(self, registry):  # {jobId: status}
        self.registry = registry
        self.calls = []

    def get_item(self, Key):
        self.calls.append(Key["jobId"])
        jid = Key["jobId"]
        return {"Item": {"jobId": jid, "status": self.registry[jid]}} if jid in self.registry else {}


def test_defers_on_registered_fal_queue_job(monkeypatch):
    # fal queue jobs register under their request_id (NOT an SFN ARN). A running one MUST now defer
    # completion and gate downstream dispatch — the fix for hours-long fal renders. Previously the
    # SFN-only prefix filter ignored it, so fal animate nodes were never waited on (the 8-shot race).
    rt = _RegistryJobTable({"req_123": "IN_PROGRESS"})
    monkeypatch.setattr(pc, "_job_table", rt)
    assert pc._has_pending_gateway_job({"t3": {"status": "completed", "job_id": "req_123"}}) is True
    assert pc._node_gateway_pending({"status": "completed", "job_id": "req_123"}) is True


def test_terminal_fal_queue_job_does_not_defer(monkeypatch):
    rt = _RegistryJobTable({"req_123": "SUCCEEDED"})
    monkeypatch.setattr(pc, "_job_table", rt)
    assert pc._has_pending_gateway_job({"t3": {"status": "completed", "job_id": "req_123"}}) is False


def test_unregistered_and_missing_ids_do_not_defer(monkeypatch):
    # A coordinator id (plan#node) or any non-job ref is absent from the registry → not pending.
    rt = _RegistryJobTable({})
    monkeypatch.setattr(pc, "_job_table", rt)
    node_states = {
        "t1": {"status": "completed"},                            # no job_id
        "t2": {"status": "completed", "job_id": "plan-abc#t2"},   # coordinator id, not registered
    }
    assert pc._has_pending_gateway_job(node_states) is False


def test_fail_open_on_lookup_error(monkeypatch):
    class _Boom:
        def get_item(self, Key):
            raise RuntimeError("ddb down")

    monkeypatch.setattr(pc, "_job_table", _Boom())
    # fail-open: a transient read error must not wedge a plan non-terminal forever
    assert pc._has_pending_gateway_job({"t1": {"status": "completed", "job_id": ARN}}) is False
