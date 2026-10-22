"""Unit tests for reconcile-on-adopt (stuck-processing fix).

A plan node that adopts an SFN execution which is ALREADY terminal must not
be parked in "processing" (the poller->bridge pipeline only fires on a live
PENDING->terminal transition, which never happens for an already-terminal
job — so the node would hang forever). `_node_status_for_adopted_job` maps
the adopted job's current SFN status to the correct plan-state status.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from task_agent import _node_status_for_adopted_job, _describe_sfn_status  # noqa: E402


class TestNodeStatusForAdoptedJob:
    def test_already_succeeded_goes_straight_to_results_ready(self):
        # This is the dedup/already-terminal case that used to hang.
        assert _node_status_for_adopted_job("SUCCEEDED") == "results_ready"

    def test_terminal_failures_map_to_failed(self):
        assert _node_status_for_adopted_job("FAILED") == "failed"
        assert _node_status_for_adopted_job("TIMED_OUT") == "failed"
        assert _node_status_for_adopted_job("ABORTED") == "failed"

    def test_running_uses_normal_processing_path(self):
        assert _node_status_for_adopted_job("RUNNING") == "processing"

    def test_unknown_or_none_falls_back_to_processing(self):
        # None = DescribeExecution failed/denied; must fall back, never hang the
        # dispatch — the poller/bridge remain the backstop.
        assert _node_status_for_adopted_job(None) == "processing"
        assert _node_status_for_adopted_job("PENDING") == "processing"
        assert _node_status_for_adopted_job("SOMETHING_ELSE") == "processing"


class TestDescribeSfnStatusIsBestEffort:
    def test_malformed_arn_returns_none_not_raises(self):
        # Best-effort: a bad ARN (can't build a client/region) yields None so
        # the caller falls back to "processing" instead of breaking dispatch.
        assert _describe_sfn_status("not-an-arn") is None
