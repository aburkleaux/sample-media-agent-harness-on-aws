"""A `HITS#` stream item is a Hit payload, never a plan node, so it must never reach
`_process_plan`.

media-harness-search-qa-rendering, Requirement 2.5: the Session_Hit_Store shares the plan-state
table but lives in its own ``HITS#<session_id>`` partition (``DynamoSessionHitStore`` in
``agent/src/project_search/session_hits.py``). Those rows are search Hits the Render_Harvester
and citation verification read — not plan definitions or node-state rows. The chainer's stream
handler guards them explicitly (``if session_id.startswith("HITS#"): continue``) so a Hit item is
never mistaken for a plan node and never kicks off node dispatch.

This feeds the handler a DynamoDB-stream-shaped event carrying a ``HITS#`` record that — but for
the guard — would be actionable (``node_id="PLAN"``, ``status="pending"``), alongside a normal
plan record, and asserts ``_process_plan`` runs for the real session only.

Loads the handler by path (its own ``sys.modules`` name) to avoid the sibling-lambda module
shadowing the conftest documents.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

REAL_SESSION = "0a1b2c3d-1c4d-4a2e-9f31-6b0d5e2a77c1"  # 36 chars, uuid-shaped like a real chat id
HIT_SESSION = f"HITS#{REAL_SESSION}"


def _load():
    for name in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE",
                 "SKILLS_S3_BUCKET", "TASK_AGENT_RUNTIME_ARN"):
        os.environ.setdefault(name, "t")
    spec = importlib.util.spec_from_file_location("_chainer_hits_guard", HERE / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_chainer_hits_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load()


def _record(session_id: str, node_id: str, status: str, event_name: str = "INSERT") -> dict:
    """A DynamoDB-stream NewImage record shaped exactly as the handler unmarshals it."""
    return {
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "session_id": {"S": session_id},
                "node_id": {"S": node_id},
                "status": {"S": status},
            }
        },
    }


def _spy_process_plan(monkeypatch) -> list:
    """Record every session id the handler forwards to _process_plan."""
    processed: list = []
    monkeypatch.setattr(pc, "_process_plan", lambda session_id: processed.append(session_id))
    return processed


def test_hits_record_never_reaches_process_plan(monkeypatch):
    processed = _spy_process_plan(monkeypatch)

    # The HITS# record is deliberately given the SAME shape that would dispatch a real plan
    # (PLAN row, INSERT, pending). Only the partition prefix should keep it out.
    event = {"Records": [
        _record(HIT_SESSION, "PLAN", "pending"),
        _record(REAL_SESSION, "PLAN", "pending"),
    ]}
    pc.handler(event, None)

    assert HIT_SESSION not in processed
    assert not any(str(s).startswith("HITS#") for s in processed)
    # The real plan in the same batch is unaffected — the guard skips only Hit rows.
    assert REAL_SESSION in processed


def test_hits_record_alone_dispatches_nothing(monkeypatch):
    processed = _spy_process_plan(monkeypatch)

    pc.handler({"Records": [_record(HIT_SESSION, "PLAN", "pending")]}, None)

    assert processed == []


if __name__ == "__main__":
    import types

    class _MP:
        """Minimal monkeypatch stand-in for direct `python test_...py` runs."""
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    for _t in (test_hits_record_never_reaches_process_plan, test_hits_record_alone_dispatches_nothing):
        _mp = _MP()
        try:
            _t(_mp)
        finally:
            _mp.undo()
    print("all session-hits-guard tests passed")
