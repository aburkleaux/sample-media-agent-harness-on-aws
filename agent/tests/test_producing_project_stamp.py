"""Jobs carry the Producing_Turn's Project (ingestion Req 9.3) so async media registers into it."""
from __future__ import annotations

import contextvars
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jobs import context as jobs_context  # noqa: E402
from jobs.models import JobRecord  # noqa: E402
from jobs.registry import JobRegistry  # noqa: E402
from project_search.context import TurnContext, current_turn_context  # noqa: E402

PID = "p" + "a" * 32
OTHER = "p" + "b" * 32


def _turn(pid):
    return TurnContext.parse({"turn_id": "t", "active_project": {"id": pid, "name": "P", "collection_id": pid},
                              "web_search": False, "project_access": "ok"})


def test_main_agent_turn_context_wins_over_forwarded():
    def run():
        assert jobs_context.current_producing_project_id() is None
        jobs_context.set_producing_project_id(OTHER)
        assert jobs_context.current_producing_project_id() == OTHER  # a task agent: forwarded value
        current_turn_context.set(_turn(PID))
        assert jobs_context.current_producing_project_id() == PID  # the Main_Agent turn's own Project

    contextvars.copy_context().run(run)


def test_registry_round_trips_producing_project_id():
    rec = JobRecord(job_id="j", actor_id="a", session_id="s", trio_id="t", gateway_id="g", status_tool="st",
                    results_tool="rt", status="PENDING", start_at="x", updated_at="x", producing_project_id=PID)
    item = JobRegistry._to_item(rec)
    assert item["producingProjectId"] == PID
    assert JobRegistry._from_item(item).producing_project_id == PID
    bare = JobRegistry._to_item(JobRecord(**{**rec.__dict__, "producing_project_id": None}))
    assert "producingProjectId" not in bare and JobRegistry._from_item(bare).producing_project_id is None
