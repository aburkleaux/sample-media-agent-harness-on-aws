"""Every job carries the user request that caused it.

A session accumulates several unrelated intents. In one real session a 4-node
analysis plan and a standalone trim job shared only `sessionId`, so clicking one
job's notification pulled results for all of them — a 66-step, 320-second turn
that also went off investigating a failed node from the *other* intent.

`runId` cannot serve as the group: it identifies a PLAN execution and is bound
only while a DAG runs, so 81 of 115 real job rows had none — every standalone
dispatch. `turnId` is bound for every invocation, and is propagated (not
regenerated) across the sub-agent and plan-chainer boundaries so one request
stays one group.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from jobs import context as jobs_context  # noqa: E402
from jobs.models import JobRecord  # noqa: E402
from jobs.registry import JobRegistry  # noqa: E402


def _record(**kw) -> JobRecord:
    base = dict(job_id="arn:job:1", actor_id="a1", session_id="s1",
                trio_id="media-processing|process_media", gateway_id="gw",
                status_tool="get_job_status", results_tool="get_results",
                status="PENDING", start_at="2026-08-03T00:00:00Z")
    base.update(kw)
    return JobRecord(**base)


class TestContextVar:
    def test_unset_is_none_not_an_error(self):
        """Optional like run_id: a missing binding must not block registration."""
        assert jobs_context.current_turn_id() is None

    def test_set_and_reset_round_trip(self):
        token = jobs_context.set_turn_id("turn-abc")
        try:
            assert jobs_context.current_turn_id() == "turn-abc"
        finally:
            jobs_context.reset_turn_id(token)
        assert jobs_context.current_turn_id() is None

    def test_empty_string_counts_as_unset(self):
        token = jobs_context.set_turn_id("")
        try:
            assert jobs_context.current_turn_id() is None
        finally:
            jobs_context.reset_turn_id(token)

    def test_exported(self):
        for name in ("current_turn_id", "set_turn_id", "reset_turn_id"):
            assert name in jobs_context.__all__


class TestPersistence:
    def test_turn_id_is_written(self):
        item = JobRegistry._to_item(_record(turn_id="turn-xyz"))
        assert item["turnId"] == "turn-xyz"

    def test_absent_turn_id_is_omitted_not_null(self):
        """An empty attribute would break a future GSI on the field."""
        assert "turnId" not in JobRegistry._to_item(_record())

    def test_round_trips_through_the_item(self):
        item = JobRegistry._to_item(_record(turn_id="turn-xyz", run_id="plan-1"))
        back = JobRegistry._from_item(item)
        assert back.turn_id == "turn-xyz"
        assert back.run_id == "plan-1"

    def test_turn_id_is_independent_of_run_id(self):
        """A standalone dispatch has a turn but no plan."""
        item = JobRegistry._to_item(_record(turn_id="turn-solo"))
        assert item["turnId"] == "turn-solo"
        assert "runId" not in item
        assert JobRegistry._from_item(item).run_id is None


class TestPropagation:
    """The id must cross every boundary a single request can span."""

    def test_dispatch_forwards_it_to_sub_agents(self):
        src = (SRC / "orchestration" / "dispatch_tool.py").read_text()
        assert 'payload["turn_id"] = _turn' in src
        assert "current_turn_id" in src

    def test_sub_agent_inherits_rather_than_mints(self):
        src = (SRC / "task_agent.py").read_text()
        assert 'payload.get("turn_id")' in src
        assert "set_turn_id" in src

    def test_main_agent_binds_one_per_invocation(self):
        src = (SRC / "main.py").read_text()
        assert "jobs_context.set_turn_id" in src
        assert "jobs_context.reset_turn_id" in src

    def test_main_agent_accepts_a_forwarded_id(self):
        """A dispatched runtime must not start a new group."""
        src = (SRC / "main.py").read_text()
        assert 'payload.get("turn_id")' in src

    def test_wrap_stamps_it_on_the_record(self):
        src = (SRC / "jobs" / "strands_tool_wrap.py").read_text()
        assert "current_turn_id()" in src
        assert "turn_id=turn_id," in src

    def test_plan_persists_it_for_async_dispatch(self):
        """Nodes run after the invocation ends, so contextvars are gone."""
        src = (SRC / "orchestration" / "plan_and_execute_tool.py").read_text()
        assert '"turn_id": turn_id,' in src
        assert "current_turn_id()" in src

    def test_chainer_forwards_it_from_the_plan_row(self):
        chainer = (Path(__file__).resolve().parents[2]
                   / "infra" / "lib" / "lambdas" / "plan-chainer" / "handler.py")
        src = chainer.read_text()
        assert 'plan_record.get("turn_id"' in src
        assert '"turn_id": turn_id' in src

    def test_every_binding_site_has_a_matching_release(self):
        """A leaked contextvar mis-attributes later jobs on a reused task.

        Counts real call sites: a plain substring count is wrong because
        "reset_turn_id" ends with "set_turn_id".
        """
        import re
        for name in ("main.py", "task_agent.py"):
            src = (SRC / name).read_text()
            binds = len(re.findall(r"(?<!re)set_turn_id\(", src))
            frees = len(re.findall(r"reset_turn_id\(", src))
            assert binds > 0, f"{name} never binds a turn_id"
            assert binds == frees, f"{name}: {binds} bind(s) vs {frees} release(s)"
