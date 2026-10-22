"""put_node_status must not wipe the Plan Chainer's retry accounting.

Root of the session 6c69afb4 loop (sg1_video, 45x "retry 1/1"): the chainer's `_record_retry`
SETs `retries`/`last_retry_error` on the (session_id, node_id) row, but `put_node_status` does a
FULL-ITEM put, so a routine status rewrite from the Task Agent (running -> processing/results_ready
when the node re-runs on retry) REPLACED the row and dropped those fields — resetting the count so
MAX_NODE_RETRIES never accumulated and the error-signature stop had nothing to compare. These pin
that the retry-accounting fields survive a status rewrite that doesn't set them, while an explicit
caller value still wins.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from state_store import StateStore  # noqa: E402


class _FakeTable:
    def __init__(self):
        self.items = {}

    @staticmethod
    def _k(key):
        return (key["session_id"], key["node_id"])

    def put_item(self, Item):
        self.items[(Item["session_id"], Item["node_id"])] = dict(Item)

    def get_item(self, Key):
        it = self.items.get(self._k(Key))
        return {"Item": dict(it)} if it else {}


def _store():
    s = StateStore.__new__(StateStore)  # bypass boto3 __init__
    s._table = _FakeTable()
    return s


def test_status_rewrite_preserves_retries_and_last_error():
    s = _store()
    # Chainer recorded one retry on this node row.
    s._table.put_item(Item={"session_id": "sess", "node_id": "sg1_video",
                            "status": "running", "retries": 1, "last_retry_error": "boom"})
    # Task Agent rewrites the status (no retry fields) as the node re-runs — must NOT wipe them.
    s.put_node_status("sess", "sg1_video", "results_ready")
    row = s._table.items[("sess", "sg1_video")]
    assert row["status"] == "results_ready"
    assert row["retries"] == 1              # preserved -> cap can accumulate
    assert row["last_retry_error"] == "boom"  # preserved -> signature stop can compare


def test_explicit_caller_value_wins_over_preserved():
    s = _store()
    s._table.put_item(Item={"session_id": "sess", "node_id": "sg1_video",
                            "status": "failed", "retries": 1})
    s.put_node_status("sess", "sg1_video", "running", retries=2, last_retry_error="new")
    row = s._table.items[("sess", "sg1_video")]
    assert row["retries"] == 2
    assert row["last_retry_error"] == "new"


def test_fresh_node_has_no_spurious_retry_fields():
    s = _store()
    s.put_node_status("sess", "fresh", "pending")
    row = s._table.items[("sess", "fresh")]
    assert row["status"] == "pending"
    assert "retries" not in row  # nothing to preserve on a brand-new node
