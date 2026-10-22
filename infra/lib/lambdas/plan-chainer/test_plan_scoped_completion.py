"""A plan must be judged complete on ITS OWN last node, not a sibling plan's.

Reported live: with parallel M2C tasks, the run was announced complete while some
parallel nodes were still going.

plan-state is keyed (session_id HASH, node_id RANGE) and node rows carried NO plan_id
-- 0 of 7 rows in staging had one. So node ids repeat across plans in a session, and
the all-terminal check counted a previous plan's terminal row as this plan's progress.
Node ids like t1/t2/t3 are positional, so collision is the normal case, not an edge one.

Fix: stamp plan_id on node rows and ignore rows belonging to another plan. Rows with no
plan_id predate the stamp and are treated as belonging, so runs in flight keep working.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET"):
        __import__("os").environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_scoped", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_scoped"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()

NODES = [{"id": "t1", "tools": []}, {"id": "t2", "tools": []}, {"id": "t3", "tools": []}]


class FakeTable:
    """Returns rows for one session, mimicking the (session_id, node_id) key space."""

    def __init__(self, rows):
        self.rows = rows
        self.meta = type("M", (), {"client": type("C", (), {"exceptions": type(
            "E", (), {"ConditionalCheckFailedException": Exception})})})

    def query(self, **kwargs):
        return {"Items": list(self.rows)}


def _run(rows, monkeypatch, plan_id="planB"):
    """Drive _process_plan and capture whether the run was marked complete."""
    completed = []
    monkeypatch.setattr(pc, "_plan_table", FakeTable(rows), raising=False)
    monkeypatch.setattr(pc, "_mark_run_complete",
                        lambda s, p, n, st, **_: completed.append((p, dict(st))), raising=False)
    monkeypatch.setattr(pc, "_dispatch_node", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(pc, "_dispatch_results_fetch", lambda *a, **k: None, raising=False)
    pc._process_plan("sess1")
    return completed


def _plan_row(plan_id="planB"):
    return {"session_id": "sess1", "node_id": "PLAN", "plan_id": plan_id,
            "nodes_json": json.dumps(NODES), "status": "running", "actor_id": "a"}


def test_a_previous_plans_completed_rows_do_not_complete_this_plan(monkeypatch):
    """The reported bug: stale terminal rows announced the new run as done."""
    rows = [_plan_row("planB")] + [
        {"session_id": "sess1", "node_id": nid, "plan_id": "planA", "status": "completed"}
        for nid in ("t1", "t2", "t3")
    ]
    assert _run(rows, monkeypatch) == [], "completed on another plan's nodes"


def test_this_plans_own_terminal_nodes_do_complete_it(monkeypatch):
    rows = [_plan_row("planB")] + [
        {"session_id": "sess1", "node_id": nid, "plan_id": "planB", "status": "completed"}
        for nid in ("t1", "t2", "t3")
    ]
    done = _run(rows, monkeypatch)
    assert len(done) == 1 and done[0][0] == "planB", done


def test_one_node_still_running_blocks_completion(monkeypatch):
    """The parallel case: two done, one in flight."""
    rows = [_plan_row("planB")] + [
        {"session_id": "sess1", "node_id": "t1", "plan_id": "planB", "status": "completed"},
        {"session_id": "sess1", "node_id": "t2", "plan_id": "planB", "status": "completed"},
        {"session_id": "sess1", "node_id": "t3", "plan_id": "planB", "status": "running"},
    ]
    assert _run(rows, monkeypatch) == [], "announced complete with a node still running"


def test_rows_without_a_plan_id_still_count(monkeypatch):
    """Backward compatibility: runs already in flight have unstamped rows."""
    rows = [_plan_row("planB")] + [
        {"session_id": "sess1", "node_id": nid, "status": "completed"}
        for nid in ("t1", "t2", "t3")
    ]
    assert len(_run(rows, monkeypatch)) == 1


def test_the_state_query_is_paginated():
    """A truncated page drops nodes, and a dropped node reads as pending forever."""
    src = (HERE / "handler.py").read_text()
    assert "ExclusiveStartKey" in src and "LastEvaluatedKey" in src



# --- Watchdog: a node stuck RUNNING with no progress is force-failed so the plan settles ---
# (session 20f7315e: seg1_validate ran a hung code_interpreter probe, never terminalized, and
#  the plan hung RUNNING until the 24h TTL sweep — its Phase 6 review gate never fired.)
import time as _time


def _running_row(nid, stalled_seconds):
    """A RUNNING node whose sliding TTL implies it last progressed `stalled_seconds` ago."""
    now = int(_time.time())
    return {"session_id": "sess1", "node_id": nid, "plan_id": "planB", "status": "running",
            "ttl": now + pc._PLAN_TTL_SECONDS - stalled_seconds}


def test_stalled_running_node_is_force_failed_and_plan_settles(monkeypatch):
    failed = []
    monkeypatch.setattr(pc, "_mark_node_failed",
                        lambda s, p, nid, reason, **k: failed.append(nid) or True, raising=False)
    # A stalled node is terminal, not retryable, for this test.
    monkeypatch.setattr(pc, "_should_retry_node", lambda st: False, raising=False)
    # Nothing registered in flight for it (the job-registry GSI lookup).
    monkeypatch.setattr(pc, "_node_has_inflight_registered_job", lambda *a: False, raising=False)
    rows = [_plan_row("planB"),
            {"session_id": "sess1", "node_id": "t1", "plan_id": "planB", "status": "completed"},
            {"session_id": "sess1", "node_id": "t2", "plan_id": "planB", "status": "completed"},
            _running_row("t3", pc._PLAN_NODE_STALL_SECONDS + 500)]
    done = _run(rows, monkeypatch)
    assert failed == ["t3"], f"the stalled node should be force-failed, got {failed}"
    assert len(done) == 1, "the plan should settle once the stalled node is force-failed"


def test_fresh_running_node_is_not_reaped(monkeypatch):
    failed = []
    monkeypatch.setattr(pc, "_mark_node_failed",
                        lambda s, p, nid, reason, **k: failed.append(nid) or True, raising=False)
    rows = [_plan_row("planB"),
            {"session_id": "sess1", "node_id": "t1", "plan_id": "planB", "status": "completed"},
            {"session_id": "sess1", "node_id": "t2", "plan_id": "planB", "status": "completed"},
            _running_row("t3", 5)]  # just dispatched — nowhere near the stall threshold
    done = _run(rows, monkeypatch)
    assert failed == [], "a fresh running node must never be reaped"
    assert done == [], "still legitimately running → plan not complete"


def test_stalled_node_with_inflight_sfn_job_is_exempt(monkeypatch):
    """A long-running gateway/SFN job is legitimately slow — the watchdog must not kill it."""
    failed = []
    monkeypatch.setattr(pc, "_mark_node_failed",
                        lambda s, p, nid, reason, **k: failed.append(nid) or True, raising=False)
    monkeypatch.setattr(pc, "_has_pending_gateway_job", lambda states: True, raising=False)
    row = _running_row("t3", pc._PLAN_NODE_STALL_SECONDS + 500)
    row["job_id"] = "arn:aws:states:us-west-2:1:execution:m2c:abc"
    rows = [_plan_row("planB"),
            {"session_id": "sess1", "node_id": "t1", "plan_id": "planB", "status": "completed"},
            {"session_id": "sess1", "node_id": "t2", "plan_id": "planB", "status": "completed"},
            row]
    _run(rows, monkeypatch)
    assert failed == [], "a node with an in-flight SFN job must be exempt from the watchdog"
