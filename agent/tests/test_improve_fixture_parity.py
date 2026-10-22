"""Pin that the committed fixture agrees with production on every facet.

REQ-017 says the offline tests run on real harvested data, and the harvest lives outside the
repo (OURS-2), so a minimal slice is committed instead. That arrangement only holds if the
slice is faithful — a fixture looser than the real source hides breakage rather than
surfacing it, which is how the Registry projector shipped three never-worked bugs with a
green suite.

The gap this test exists to catch was real: the fixture dropped span `status`, so it defaulted
to `UNSET`, so any facet reading status computed a different value in tests than in
production — silently, and in the direction that made tests pass.

Skipped when the harvest is absent, because that is the whole reason the fixture exists.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.classify import classify  # noqa: E402
from improve.contract import Run  # noqa: E402
from improve.observe.build_run import HarvestedPlanSource, build_run  # noqa: E402
from improve.observe.span_trajectory import (  # noqa: E402
    HarvestedSpanSource, trajectory_for_session,
)
from improve.loop import load_golden  # noqa: E402
from improve.signature import shape_facets  # noqa: E402

from improve_fixtures import real_plan, real_tool_trajectory, real_trajectory  # noqa: E402


def _harvest_present() -> bool:
    try:
        return bool(HarvestedSpanSource().spans_for(next(iter(_sessions()))))
    except Exception:  # noqa: BLE001
        return False


def _sessions() -> list:
    return [s.source_session or s.scenario_id for s in load_golden()]


requires_harvest = pytest.mark.skipif(
    not _harvest_present(),
    reason="offline harvest lives outside the repo (OURS-2)")


@requires_harvest
@pytest.mark.parametrize("session", _sessions())
def test_fixture_and_production_agree_on_every_facet(session):
    scenario = next(s for s in load_golden()
                    if (s.source_session or s.scenario_id) == session)
    production = build_run(scenario, span_source=HarvestedSpanSource(),
                          plan_source=HarvestedPlanSource())
    from_fixture = Run(run_id=session, plan=real_plan(session),
                       trajectory=real_trajectory(session))

    prod = {f.name: (f.value, f.observed) for f in shape_facets(production)}
    fixt = {f.name: (f.value, f.observed) for f in shape_facets(from_fixture)}
    assert set(prod) == set(fixt)

    disagreements = {name: {"production": prod[name], "fixture": fixt[name]}
                     for name in prod if prod[name] != fixt[name]}
    assert not disagreements, (
        f"fixture disagrees with the harvest for {session[:8]}: {disagreements}. "
        "Regenerate with scripts/refresh_improve_fixtures.py, and if a field is missing from "
        "the fixture, add it there rather than loosening this test.")


@requires_harvest
@pytest.mark.parametrize("session", _sessions())
def test_fixture_trajectory_is_span_for_span_identical_to_production(session):
    """Names, statuses, durations, messages and ORDER, not just the derived facets.

    `test_fixture_and_production_agree_on_every_facet` above compares `shape_facets`, which
    reads none of those fields. I measured it: with the fixture holding only tool spans, the
    facet dicts were byte-identical to production for all six sessions, so that test would pass
    unchanged even though `classify` saw a completely different trajectory and put every golden
    session in the wrong workload class. A parity test that only covers the derived values
    cannot see a gap in the raw ones.
    """
    steps = real_trajectory(session)
    production = trajectory_for_session(session, source=HarvestedSpanSource())

    def shape(step):
        return (step.name, step.status, step.duration_ms, step.error_message)

    assert len(steps) == len(production), (
        f"{session[:8]}: fixture has {len(steps)} spans, production {len(production)}. "
        "Regenerate with scripts/refresh_improve_fixtures.py.")
    # Compared as ORDERED lists: step order is what the async-lifecycle facets read, and the
    # generator and production sort on the parsed instant for exactly that reason.
    assert [shape(s) for s in steps] == [shape(p) for p in production]


@requires_harvest
@pytest.mark.parametrize("session", _sessions())
def test_fixture_and_production_classify_the_same_way(session):
    """The defect the regeneration existed to fix, pinned.

    `classify` reads non-tool spans (`chat`, `execute_event_loop_cycle`, `mcp tools/list`,
    `POST /invocations`). On the tool-only fixture every golden session came out
    `unclassified`, which runs NO evaluators, so every `evaluate` test silently asserted
    UNSCORED instead of what it was written to assert. Nothing caught that.
    """
    production = trajectory_for_session(session, source=HarvestedSpanSource())
    assert (classify(Run(run_id=session, trajectory=real_trajectory(session))).cls
            is classify(Run(run_id=session, trajectory=production)).cls)


@requires_harvest
@pytest.mark.parametrize("session", _sessions())
def test_fixture_carries_span_status(session):
    """The specific omission that caused a silent divergence.

    Asserted on the TOOL slice on purpose. Ranged over the full trajectory this proves nothing:
    ~90% of those spans are AWS SDK plumbing that always carries OK or ERROR, so plumbing alone
    satisfies it while every tool span could still be defaulted to UNSET, which is the exact
    omission being guarded.
    """
    steps = real_tool_trajectory(session)
    assert steps, f"no tool spans in the fixture for {session[:8]}"
    assert all(s.status for s in steps)
    # Not every span is UNSET — that default is what made the gap invisible.
    assert {s.status for s in steps} != {"UNSET"}


@requires_harvest
@pytest.mark.parametrize("session", _sessions())
def test_fixture_plan_topology_matches_the_harvest(session):
    fixture_plan = real_plan(session)
    harvest_plan = HarvestedPlanSource().plan_for(session)
    assert {n.id for n in fixture_plan.nodes} == {n.id for n in harvest_plan.nodes}
    for node in fixture_plan.nodes:
        twin = next(n for n in harvest_plan.nodes if n.id == node.id)
        assert node.status == twin.status
        assert node.updated_at == twin.updated_at
        assert node.gateway_status == twin.gateway_status
        assert sorted(node.depends_on) == sorted(twin.depends_on)
