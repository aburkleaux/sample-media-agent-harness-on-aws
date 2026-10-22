"""The job-registry projection must reach a terminal status even when plan-state already did.

Reported live as a node stuck at "1/2 · Running" while the plan itself had finished.

The job-registry row the UI reads is a PROJECTION of the authoritative plan-state table, kept
in step by a best-effort mirror at the end of `_dispatch_sfn_output_as_result`. Two early
returns sit in front of that mirror:

    if not manifest_updated: return                       # manifest write failed
    ...
    except ConditionalCheckFailedException:                # plan row already past results_ready
        logger.info(...); return                           # <-- returns BEFORE the mirror

The second is the killer. "The authoritative row already advanced" is exactly the case where
the projection is most likely to be the stale one — and it is the case in which we skip
updating it. The plan reads `completed`, the projection is left at RUNNING/PENDING, and nothing
recovers it: plan/node rows have `pollPartition` removed, so the polling worker is deliberately
excluded, and the mirror's own failures are swallowed at `logger.debug`, so a miss is invisible.

What must hold:
  1. plan row already terminal  -> the projection is STILL driven terminal (no early return)
  2. the mirror never regresses an already-terminal projection (SUCCEEDED must not clobber
     FAILED, and a replay must be a no-op)
  3. a mirror failure is logged loudly enough to find, not at debug
  4. the terminal projection row carries `ttl`, so a miss ages out instead of sticking forever
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN", "PLAN_RESULT_QUEUE_URL"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_proj", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_proj"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


class _CCF(Exception):
    """Stands in for botocore's ConditionalCheckFailedException."""


def _meta():
    return type("M", (), {"client": type("C", (), {"exceptions": type(
        "E", (), {"ConditionalCheckFailedException": _CCF})})})


class FakePlanTable:
    """`already_advanced=True` reproduces the live state: plan-state is past results_ready."""

    def __init__(self, already_advanced: bool):
        self.already_advanced = already_advanced
        self.meta = _meta()
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)
        if self.already_advanced:
            raise _CCF("status is not results_ready")
        return {}


class FakeJobTable:
    """Records writes and enforces the conditions the real table would."""

    def __init__(self, status="RUNNING", terminal_at=None, fail=False):
        self.item = {"status": status}
        if terminal_at:
            self.item["terminalAt"] = terminal_at
        self.fail = fail
        self.meta = _meta()
        self.updates = []

    def update_item(self, **kw):
        self.updates.append(kw)
        if self.fail:
            raise RuntimeError("throughput exceeded")
        cond = kw.get("ConditionExpression") or ""
        if "attribute_not_exists(terminalAt)" in cond and "terminalAt" in self.item:
            raise _CCF("already terminal")
        vals = kw.get("ExpressionAttributeValues") or {}
        if ":s" in vals:
            self.item["status"] = vals[":s"]
        if ":now" in vals:
            self.item["terminalAt"] = vals[":now"]
        return {}


class FakeS3:
    """Enough S3 for the manifest step to SUCCEED, so `manifest_updated` is True and the
    early return in front of the projection mirror is not what we are measuring."""

    def put_object(self, **kw):
        return {}

    def get_object(self, **kw):
        raise RuntimeError("no manifest yet")  # handler falls back to a fresh manifest dict


class FakeSfn:
    def describe_execution(self, executionArn=None):
        return {"status": "SUCCEEDED", "output": '{"ok": true}'}


def _run(monkeypatch, plan_table, job_table, caplog=None):
    """Drive the real `_dispatch_sfn_output_as_result` end to end.

    Only the three AWS clients are faked, and they are faked to SUCCEED, so the two early
    returns in front of the projection mirror (`DescribeExecution` not SUCCEEDED, and
    `manifest_updated` False) are out of the way and the mirror itself is what is measured.
    """
    monkeypatch.setattr(pc, "_plan_table", plan_table, raising=False)
    monkeypatch.setattr(pc, "_job_table", job_table, raising=False)
    monkeypatch.setattr(pc, "_s3", FakeS3(), raising=False)
    monkeypatch.setattr(pc, "_sfn_client_for_arn", lambda *a, **k: FakeSfn(), raising=False)
    monkeypatch.setattr(pc, "SKILLS_S3_BUCKET", "test-bucket", raising=False)
    return pc._dispatch_sfn_output_as_result(
        "sess-1", "plan-1", "t2", "job-abc", "2026-08-18T00:00:00+00:00")


def test_projection_goes_terminal_even_when_plan_state_already_did(monkeypatch):
    plan = FakePlanTable(already_advanced=True)   # the live state
    job = FakeJobTable(status="RUNNING")
    _run(monkeypatch, plan, job)
    assert job.item["status"] == "SUCCEEDED", (
        f"projection stuck at {job.item['status']!r} — this is the '1/2 Running' bug")


def test_projection_still_goes_terminal_on_the_normal_path(monkeypatch):
    plan = FakePlanTable(already_advanced=False)
    job = FakeJobTable(status="RUNNING")
    _run(monkeypatch, plan, job)
    assert job.item["status"] == "SUCCEEDED"


def test_terminal_write_does_not_regress_an_already_terminal_projection(monkeypatch):
    # A node that FAILED must not be relabelled SUCCEEDED by a late mirror.
    plan = FakePlanTable(already_advanced=True)
    job = FakeJobTable(status="FAILED", terminal_at="2026-08-17T00:00:00+00:00")
    _run(monkeypatch, plan, job)
    assert job.item["status"] == "FAILED"


def test_terminal_projection_row_carries_a_ttl(monkeypatch):
    # Without a ttl, a row the mirror never reached sticks in the Activity panel forever.
    plan = FakePlanTable(already_advanced=False)
    job = FakeJobTable(status="RUNNING")
    _run(monkeypatch, plan, job)
    terminal = [u for u in job.updates
                if ":s" in (u.get("ExpressionAttributeValues") or {})
                and (u["ExpressionAttributeValues"][":s"] in ("SUCCEEDED", "FAILED"))]
    assert terminal, "no terminal projection write happened at all"
    expr = terminal[-1].get("UpdateExpression", "")
    assert "ttl" in expr, f"terminal projection write has no ttl: {expr}"


def test_a_mirror_failure_is_logged_above_debug(monkeypatch, caplog):
    # Swallowing at logger.debug is why this class of bug reproduces with no error anywhere.
    import logging
    plan = FakePlanTable(already_advanced=False)
    job = FakeJobTable(status="RUNNING", fail=True)
    with caplog.at_level(logging.WARNING):
        _run(monkeypatch, plan, job)
    assert any("job-abc" in r.message or "t2" in r.message for r in caplog.records), (
        f"mirror failure not surfaced at WARNING+: {[r.message for r in caplog.records]}")
