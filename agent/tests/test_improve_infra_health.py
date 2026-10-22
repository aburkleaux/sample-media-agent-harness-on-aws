"""Three-state infrastructure health, offline. No AWS, no credentials, no network.

What these tests are really defending is one property: **absence never reads as HEALTHY**.
A failed CloudWatch call, missing credentials, a throttle, an account with no alarms, a
window past alarm-history retention, and a run with no stamped window at all must every one
of them come back ``unknown``. The bug being designed out is a ``degraded: bool`` defaulting
to ``False``, which would have called all six of those a clean platform.

The window cases run on REAL plan-state timestamps from the golden sessions
(``improve_fixtures.real_window``). The alarm records are necessarily synthetic — the harvest
carries no alarm history yet, and inventing one inside ``ideas/staging-data`` would put a
fabricated file where a real harvest belongs. They are written to ``tmp_path`` instead, in
the exact shape ``harvest.py`` writes, and ``test_harvested_source_reads_the_shape_harvest_writes``
pins that agreement.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve_fixtures import A3A8FAC0, E2CB300B, FFAB73F8, real_window  # noqa: E402

from improve.contract import (  # noqa: E402
    UNKNOWN, WINDOW_STAMPED, InfraHealth, InfraState, Run, RunWindow, Scenario, infra_state_of,
)
from improve.observe.build_run import (  # noqa: E402
    HarvestedPlanSource, HarvestedWindowSource, build_run,
)
from improve.observe.memory_reader import Conversation  # noqa: E402
from improve.observe.infra_health import (  # noqa: E402
    ALARM_HISTORY_RETENTION_DAYS, AlarmLookup, AlarmObservation, AlarmRecord, AlarmTransition,
    HarvestedAlarmSource, LiveCloudWatchAlarmSource, default_alarm_source,
    infra_health_for_window, lookup_from_records,
)

# Anchor to real now (not a fixed date): every window below is built as a relative
# offset from NOW, and `build_run` gates the alarm lookup on real `datetime.now()`
# with no clock injection. A fixed NOW drifted past the ~13d retention boundary over
# time and desynced from build_run (test_build_run_wires… saw 0 alarm calls once the
# fixed date aged out). Real now keeps every offset-based scenario stable and in sync.
NOW = datetime.now(timezone.utc)


def _recent_window(minutes_ago: int = 60, length_minutes: int = 20,
                   provenance: str = WINDOW_STAMPED) -> RunWindow:
    """A window inside retention, so the retention rule is not what the test is measuring."""
    start = NOW - timedelta(minutes=minutes_ago)
    return RunWindow(start=start.isoformat(),
                     end=(start + timedelta(minutes=length_minutes)).isoformat(),
                     provenance=provenance)


class _StubSource:
    """An injected alarm source. Mirrors how HarvestedSpanSource is injected into build_run."""

    SOURCE = "stub"

    def __init__(self, lookup=None, raises: Exception = None) -> None:
        self._lookup = lookup
        self._raises = raises
        self.calls: list = []

    def alarms_in_window(self, start, end):
        self.calls.append((start, end))
        if self._raises is not None:
            raise self._raises
        return self._lookup


# ---------------------------------------------------------------------------------------
# the central property: nothing absent may read as healthy
# ---------------------------------------------------------------------------------------

def test_absent_window_is_unknown_never_healthy():
    """The pre-change golden set: no collector stamped a window."""
    health = infra_health_for_window(UNKNOWN, source=_StubSource(AlarmLookup()), now=NOW)
    assert health.state is InfraState.UNKNOWN
    assert "no run window was stamped" in health.reason
    assert health.window is UNKNOWN


def test_a_raising_source_is_unknown_never_healthy():
    """Missing credentials / a throttle / a denied Describe* all arrive here."""
    class _NoCreds(Exception):
        pass

    source = _StubSource(raises=_NoCreds("Unable to locate credentials"))
    health = infra_health_for_window(_recent_window(), source=source, now=NOW)
    assert health.state is InfraState.UNKNOWN
    assert "Unable to locate credentials" in health.reason


def test_an_inconclusive_lookup_is_unknown_never_healthy():
    lookup = AlarmLookup(conclusive=False, reason="ThrottlingException: Rate exceeded",
                         source="cloudwatch")
    health = infra_health_for_window(_recent_window(), source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.UNKNOWN
    assert "Throttling" in health.reason


def test_an_account_with_no_alarms_is_unknown_not_healthy():
    """Silence is not health: nothing was watching, so nothing can be vouched for."""
    lookup = lookup_from_records([], _recent_window().start_dt(), _recent_window().end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    assert lookup.conclusive is False
    assert "nothing was watching" in lookup.reason
    health = infra_health_for_window(_recent_window(), source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.UNKNOWN


def test_window_past_alarm_history_retention_is_unknown_not_healthy():
    """~14d of retention means an older run is unanswerable — and must say so."""
    old_start = NOW - timedelta(days=ALARM_HISTORY_RETENTION_DAYS + 3)
    window = RunWindow(start=old_start.isoformat(),
                       end=(old_start + timedelta(minutes=10)).isoformat(),
                       provenance=WINDOW_STAMPED)
    source = _StubSource(AlarmLookup(conclusive=True, reason="all alarms OK"))
    health = infra_health_for_window(window, source=source, now=NOW)
    assert health.state is InfraState.UNKNOWN
    assert "retention" in health.reason
    # And the source was never even consulted — the answer cannot exist.
    assert source.calls == []


def test_retention_is_waived_only_for_a_snapshot_that_covers_the_window(tmp_path):
    """A harvest taken while the history existed still knows the answer.

    Retention is a fact about CloudWatch forgetting, not about old runs being unknowable — and
    the golden set ages past 14 days within days of being harvested, so a blanket rule would
    permanently discard evidence already on disk. The waiver is narrow: only an explicit
    ``coverage()`` spanning the whole window, so a source that says nothing still gets UNKNOWN.
    """
    old_start = NOW - timedelta(days=ALARM_HISTORY_RETENTION_DAYS + 5)
    window = RunWindow(start=old_start.isoformat(),
                       end=(old_start + timedelta(minutes=30)).isoformat(),
                       provenance=WINDOW_STAMPED)
    covering = _write_harvest(
        tmp_path, [{"name": "a", "current_state": "OK", "transitions": []}],
        old_start - timedelta(days=1), old_start + timedelta(days=1))
    health = infra_health_for_window(window, source=HarvestedAlarmSource(covering), now=NOW)
    assert health.state is InfraState.HEALTHY

    # Same age, a source that declares no coverage at all -> the conservative answer.
    silent = _StubSource(AlarmLookup(conclusive=True, reason="all OK"))
    assert not hasattr(silent, "coverage")
    assert infra_health_for_window(window, source=silent, now=NOW).state is InfraState.UNKNOWN


def test_coverage_shorter_than_the_window_is_unknown():
    """A harvest that does not span the run cannot answer for it."""
    window = _recent_window(minutes_ago=60, length_minutes=20)
    records = [AlarmRecord(name="a", current_state="OK")]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 coverage_start=window.start_dt() + timedelta(minutes=5),
                                 coverage_end=NOW, source="stub")
    assert lookup.conclusive is False
    assert "only covers back to" in lookup.reason


def test_there_is_no_boolean_form_of_infra_health():
    """A two-valued reading is the bug this type exists to prevent."""
    assert not hasattr(InfraHealth, "degraded")
    assert not any(isinstance(getattr(InfraHealth(), name, None), bool)
                   for name in ("state", "reason", "alarms", "window", "source"))
    assert len(set(InfraState)) == 3


# ---------------------------------------------------------------------------------------
# the states that ARE determinable
# ---------------------------------------------------------------------------------------

def test_alarm_firing_inside_the_window_is_degraded():
    window = _recent_window()
    inside = (window.start_dt() + timedelta(minutes=2)).isoformat()
    records = [
        AlarmRecord(name="quiet", current_state="OK"),
        AlarmRecord(name="noisy", current_state="OK", transitions=(
            AlarmTransition(ts=inside, old_state="OK", new_state="ALARM"),)),
    ]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.DEGRADED
    assert health.alarms == ("noisy",)


def test_alarm_that_started_firing_before_the_window_is_still_degraded():
    """The case a naive "any history in this window?" query gets wrong.

    An alarm that went into ALARM an hour earlier and stayed there emits NO history item
    inside the window, so the state entering it has to be reconstructed. Getting this wrong
    would report the worst outages — the sustained ones — as a healthy platform.
    """
    window = _recent_window()
    earlier = (window.start_dt() - timedelta(hours=3)).isoformat()
    records = [AlarmRecord(name="sustained", current_state="ALARM", transitions=(
        AlarmTransition(ts=earlier, old_state="OK", new_state="ALARM"),))]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.DEGRADED
    assert health.alarms == ("sustained",)


def test_a_steady_ok_holds_but_a_chronic_alarm_is_indeterminate():
    """No retained transition means the state held for the whole window.

    A steady OK is a quiet platform, and the window inherits HEALTHY. A steady ALARM is a
    CHRONIC condition rather than an incident: an alarm lit for longer than the retained
    history, with nothing tying it to this run, is INDETERMINATE (UNKNOWN), never DEGRADED.
    Reading it as degraded is what diverted every staging failure into the environment bucket
    and left mining with nothing to work on, so the two current states diverge here.
    """
    window = _recent_window()
    for current, expected in (("OK", InfraState.HEALTHY), ("ALARM", InfraState.UNKNOWN)):
        records = [AlarmRecord(name="steady", current_state=current)]
        lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                     NOW - timedelta(days=2), NOW, "stub")
        health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
        assert health.state is expected, f"current_state={current}"
    # The chronic-ALARM case must SAY why it stood down, so it reaches the reader as a caveat
    # rather than a silent gap. `health` here is the last iteration, the ALARM one.
    assert "chronic" in health.reason


def test_insufficient_data_entering_the_window_is_unknown_not_healthy():
    """INSUFFICIENT_DATA means the alarm could not judge — neither can we."""
    window = _recent_window()
    records = [AlarmRecord(name="blind", current_state="INSUFFICIENT_DATA")]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    assert lookup.conclusive is False
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.UNKNOWN


def test_all_ok_across_the_window_is_healthy():
    window = _recent_window()
    records = [AlarmRecord(name="a", current_state="OK"), AlarmRecord(name="b", current_state="OK")]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.HEALTHY
    assert health.alarms == ()


def test_a_firing_alarm_outweighs_indeterminate_ones():
    """Evidence that something WAS broken is not erased by not knowing about the rest.

    The firing alarm has to be a REAL incident, so it transitions into ALARM inside the
    window. A steady ALARM with no retained transition is chronic, reads as indeterminate,
    and would outweigh nothing, see the steady/chronic test above. The point here is that one
    genuine incident plus one blind (INSUFFICIENT_DATA) alarm still lands on DEGRADED, naming
    only the alarm we could actually place.
    """
    window = _recent_window()
    inside = (window.start_dt() + timedelta(minutes=2)).isoformat()
    records = [
        AlarmRecord(name="blind", current_state="INSUFFICIENT_DATA"),
        AlarmRecord(name="firing", current_state="ALARM", transitions=(
            AlarmTransition(ts=inside, old_state="OK", new_state="ALARM"),)),
    ]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.DEGRADED
    assert health.alarms == ("firing",)
    assert "indeterminate" in health.reason


def test_a_derived_window_carries_its_caveat_into_the_reason():
    """A reconstructed window may be narrower than the run — the verdict must say so."""
    window = real_window(FFAB73F8)
    recent = RunWindow(start=(NOW - timedelta(hours=2)).isoformat(),
                       end=(NOW - timedelta(hours=1)).isoformat(),
                       provenance=window.provenance)
    lookup = lookup_from_records([AlarmRecord(name="a", current_state="OK")],
                                 recent.start_dt(), recent.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(recent, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.HEALTHY
    assert "window is DERIVED" in health.reason
    assert window.derived() is True
    assert RunWindow(start=window.start, end=window.end).derived() is False  # stamped default


# ---------------------------------------------------------------------------------------
# the harvested (default, offline) source
# ---------------------------------------------------------------------------------------

def _write_harvest(tmp_path, alarms, coverage_start, coverage_end) -> str:
    path = tmp_path / "alarm-history.json"
    path.write_text(json.dumps({
        "harvested_at": NOW.isoformat(),
        "coverage": {"start": coverage_start.isoformat(), "end": coverage_end.isoformat()},
        "alarms": alarms,
    }))
    return str(path)


def test_harvested_source_reads_the_shape_harvest_writes(tmp_path):
    """Pins the reader against the collector's file shape.

    A reader looser than its producer is how the Registry projector shipped three
    never-worked bugs with a green suite: the keys here (``coverage.start``/``end``,
    ``alarms[].name``/``current_state``/``transitions[].ts``/``old_state``/``new_state``) are
    exactly what ``ideas/staging-data/harvest.py::harvest_alarms`` writes.
    """
    window = _recent_window()
    path = _write_harvest(tmp_path, [{
        "name": "mediaworks-harness-task-agent-errors",
        "current_state": "OK",
        "kind": "metric",
        "transitions": [{
            "ts": (window.start_dt() + timedelta(minutes=1)).isoformat(),
            "old_state": "OK", "new_state": "ALARM", "summary": "threshold crossed",
        }],
    }], NOW - timedelta(days=2), NOW)
    health = infra_health_for_window(window, source=HarvestedAlarmSource(path), now=NOW)
    assert health.state is InfraState.DEGRADED
    assert health.alarms == ("mediaworks-harness-task-agent-errors",)
    assert health.source == "harvested-alarm-history"


def test_missing_harvest_file_is_unknown_with_an_actionable_reason(tmp_path):
    source = HarvestedAlarmSource(str(tmp_path / "does-not-exist.json"))
    health = infra_health_for_window(_recent_window(), source=source, now=NOW)
    assert health.state is InfraState.UNKNOWN
    assert "no harvested alarm history" in health.reason
    assert "harvest.py" in health.reason


def test_unparseable_harvest_file_is_unknown(tmp_path):
    path = tmp_path / "alarm-history.json"
    path.write_text("{not json")
    health = infra_health_for_window(_recent_window(), source=HarvestedAlarmSource(str(path)),
                                     now=NOW)
    assert health.state is InfraState.UNKNOWN


def test_default_source_is_offline_and_never_touches_aws(monkeypatch):
    """The default test/golden path must be deterministic and creds-free."""
    monkeypatch.delenv("IMPROVE_INFRA_ALARMS", raising=False)
    assert isinstance(default_alarm_source(), HarvestedAlarmSource)
    monkeypatch.setenv("IMPROVE_INFRA_ALARMS", "live")
    assert isinstance(default_alarm_source(), LiveCloudWatchAlarmSource)


def test_live_source_degrades_to_unknown_when_boto3_fails(monkeypatch):
    """The live path's failure contract, without calling AWS.

    ``_client`` is what needs credentials, so failing it stands in for every real failure
    mode: no credentials, wrong region, denied ``cloudwatch:DescribeAlarms``, a throttle.
    """
    source = LiveCloudWatchAlarmSource(region="us-west-2")
    monkeypatch.setattr(source, "_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("NoCredentialsError")))
    lookup = source.alarms_in_window(_recent_window().start_dt(), _recent_window().end_dt())
    assert lookup.conclusive is False
    assert "NoCredentialsError" in lookup.reason
    health = infra_health_for_window(_recent_window(), source=source, now=NOW)
    assert health.state is InfraState.UNKNOWN


# ---------------------------------------------------------------------------------------
# the contract types
# ---------------------------------------------------------------------------------------

def test_run_window_normalizes_every_format_this_system_emits():
    """One documented format out, three formats in — the `Z` vs `+00:00` hazard."""
    same_instant = [
        "2026-08-13T18:02:55.022000+00:00",
        "2026-08-13T18:02:55.022000Z",
        "2026-08-13 18:02:55.022000",     # offset-less, as the span harvest carries
    ]
    normalized = {RunWindow(start=raw, end=raw).start for raw in same_instant}
    assert normalized == {"2026-08-13T18:02:55.022000+00:00"}
    window = RunWindow(start=same_instant[1], end=same_instant[0])
    assert window.start_dt().tzinfo is not None and window.end_dt().tzinfo is not None


def test_run_window_refuses_unusable_bounds():
    """A half-built window would answer the alarm question about the wrong stretch of time."""
    for bad in ("", "not-a-timestamp", None, 1755000000):
        with pytest.raises(ValueError):
            RunWindow(start=bad, end="2026-08-13T18:02:55+00:00")
    with pytest.raises(ValueError):  # inverted
        RunWindow(start="2026-08-13T19:00:00+00:00", end="2026-08-13T18:00:00+00:00")


def test_real_golden_window_is_wall_clock_and_derived():
    """Real plan-state bounds from the golden sessions, wall-clock and derived."""
    for session in (FFAB73F8, E2CB300B, A3A8FAC0):
        window = real_window(session)
        assert window.duration_s() > 0
        assert window.derived() is True
        assert window.start.endswith("+00:00") and window.end.endswith("+00:00")


def test_infra_state_of_collapses_both_kinds_of_unknown():
    """Nobody-looked and looked-but-could-not-tell are both UNKNOWN to policy code."""
    assert infra_state_of(Run(run_id="never-looked")) is InfraState.UNKNOWN
    looked = Run(run_id="looked", infra_health=InfraHealth(state=InfraState.UNKNOWN,
                                                          reason="throttled"))
    assert infra_state_of(looked) is InfraState.UNKNOWN
    assert infra_state_of(Run(run_id="d", infra_health=InfraHealth(state=InfraState.DEGRADED))) \
        is InfraState.DEGRADED


def test_alarm_observation_in_alarm_is_tri_state():
    """None means indeterminate for that alarm, which must not count as OK."""
    assert AlarmObservation("a", None).in_alarm is None
    assert AlarmObservation("a", False).in_alarm is False


# ---------------------------------------------------------------------------------------
# build_run wiring: the window is READ, never derived
# ---------------------------------------------------------------------------------------

class _StubWindowSource:
    def __init__(self, windows: dict) -> None:
        self._windows = windows

    def window_for(self, session_id: str):
        return self._windows.get(session_id, UNKNOWN)


class _StubSpanSource:
    """Real span shapes, so a derivation, if one existed, would have something to bite on."""

    def __init__(self, spans: list) -> None:
        self._spans = spans

    def spans_for(self, session_id: str) -> list:
        return list(self._spans)


class _StubParentedSpanSource(_StubSpanSource):
    """A span source that resolves a PARENT, which is what `build_run._parent_from` reads.

    Separate from `_StubSpanSource` rather than a flag on it, so every existing test keeps a source
    that names no parent — the state a harvest taken before the trace projection existed is in.
    """

    def __init__(self, parent: str, spans: list = ()) -> None:
        super().__init__(list(spans))
        self._parent = parent

    def parent_for(self, session_id: str):
        return self._parent


def _scenario(session: str) -> Scenario:
    return Scenario(scenario_id=session, source_session=session, goal="g")


def test_build_run_never_derives_a_window_from_spans():
    """The load-bearing negative test.

    Spans are the first evidence to vanish when the platform breaks, so a span-derived window
    would be narrowest or absent in exactly the runs whose infrastructure context matters —
    and an absent window reads downstream as "no infrastructure problem". So: rich spans,
    no recorded window, and the answer must still be UNKNOWN.
    """
    spans = [{"ts": "2026-08-13 18:02:55.022", "runtime": "main_agent", "name": "execute_tool x",
              "status": "OK", "duration_ms": 1200},
             {"ts": "2026-08-13 18:41:12.900", "runtime": "task_agent", "name": "execute_tool y",
              "status": "ERROR", "duration_ms": 900}]
    run = build_run(_scenario("s-no-window"),
                    span_source=_StubSpanSource(spans),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({}),
                    alarm_source=_StubSource(AlarmLookup(conclusive=True, reason="all OK")))
    assert run.trajectory is not UNKNOWN and len(run.trajectory) == 2  # spans WERE read
    assert run.window is UNKNOWN
    assert run.infra_health.state is InfraState.UNKNOWN
    assert "never derived from spans" in run.infra_health.reason


def test_build_run_wires_the_recorded_window_into_the_alarm_lookup():
    window = _recent_window()
    alarm_source = _StubSource(AlarmLookup(
        (AlarmObservation("some-alarm", True, "fired"),), conclusive=True, reason="1 firing"))
    run = build_run(_scenario("s1"),
                    span_source=_StubSpanSource([]),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({"s1": window}),
                    alarm_source=alarm_source)
    assert run.window is window
    # The lookup was driven by the run's OWN interval, not by "recently".
    assert alarm_source.calls == [(window.start_dt(), window.end_dt())]
    assert run.infra_health.state is InfraState.DEGRADED
    assert run.infra_health.window is window
    # latency_ms is untouched: busy time, not wall clock.
    assert "latency_ms" in run.resources


def test_build_run_defaults_are_offline(monkeypatch):
    """No injection at all must still not reach AWS (the golden path runs this way)."""
    monkeypatch.delenv("IMPROVE_INFRA_ALARMS", raising=False)
    monkeypatch.setenv("IMPROVE_HARVEST_DIR", "/nonexistent-harvest")
    monkeypatch.setenv("IMPROVE_HARVEST_SESSIONS", "/nonexistent-harvest/sessions.json")
    run = build_run(_scenario("nobody-home"), span_source=_StubSpanSource([]))
    assert run.window is UNKNOWN
    assert run.infra_health.state is InfraState.UNKNOWN
    # No Memory read was made, so nothing says who this session belongs to. Same discipline as the
    # window above: read from the source that knows, or say UNKNOWN — never invent an owner.
    assert run.actor_id is UNKNOWN


class _StubMemoryReader:
    """`MemoryReader`'s two lookups: the conversation for a session, and the OWNER of one.

    Both are stubbed even where a test only needs the first, because a fake looser than the real API
    hides breakage — a reader missing `owner_of_session` would make the inherited-owner branch below
    fail with an AttributeError that reads as a bug in the branch rather than in the fake.

    ``owners`` maps a session id to the actor that owns it, which is deliberately a DIFFERENT question
    from ``actor_id``: the latter is who owns the session being built, and a plan sub-session's answer
    to that is nobody.
    """

    def __init__(self, actor_id, turns=(), owners=None) -> None:
        self._actor_id = actor_id
        self._turns = list(turns)
        self._owners = dict(owners or {})
        self.owner_lookups: list[str] = []

    def conversation_for_session(self, session_id: str):
        return Conversation(session_id=session_id, actor_id=self._actor_id, turns=self._turns)

    def owner_of_session(self, session_id: str):
        self.owner_lookups.append(session_id)
        return self._owners.get(session_id)


def test_build_run_carries_the_actor_memory_resolved():
    """The owner is already discovered and was already being thrown away.

    `conversation_for_session` has to find the actor before it can page the session's events, so it
    returns one — and `build_run` read the goal and the response off that same Conversation while
    dropping the field that says whose they are. Nothing else on a Run identifies an owner, so
    without this the per-actor Improve bundle has no partition key.
    """
    reader = _StubMemoryReader("6f1c2f6e-1111-4c2a-9a55-aaaaaaaaaaaa")
    run = build_run(_scenario("s1"), span_source=_StubSpanSource([]),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({}), memory_reader=reader)
    assert run.actor_id == "6f1c2f6e-1111-4c2a-9a55-aaaaaaaaaaaa"


def test_a_session_no_actor_claims_is_unknown_never_an_empty_owner():
    """`conversation_for_session` returns a Conversation with `actor_id=None` when no actor's event
    list held the session. That must stay the sentinel: a per-actor bundle keyed on `""` would be
    one shared partition holding every unowned session's evidence."""
    run = build_run(_scenario("s1"), span_source=_StubSpanSource([]),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({}),
                    memory_reader=_StubMemoryReader(None))
    assert run.actor_id is UNKNOWN



def test_a_sub_session_inherits_its_owner_from_the_request_that_spawned_it():
    """A plan sub-session has NO conversation of its own — its turns live under the parent — so its
    own Memory read names no owner and `Run.actor_id` used to stay UNKNOWN. That left every plan
    step's spend belonging to nobody, and the spend is real.

    The lookup is keyed on the parent's session id, which `Run.parent_run_id` already carries from the
    OTEL trace the two sessions share. Only the ID is taken: reading the parent's conversation here
    would hand this run a different session's goal and response for the judge to score.
    """
    reader = _StubMemoryReader(None, owners={"the-parent-request": "6f1c2f6e-1111"})
    run = build_run(_scenario("plan-session-1787244223"),
                    span_source=_StubParentedSpanSource("the-parent-request"),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({}), memory_reader=reader)

    assert run.parent_run_id == "the-parent-request"
    assert run.actor_id == "6f1c2f6e-1111"
    assert reader.owner_lookups == ["the-parent-request"], "asked about the PARENT, not itself"


def test_an_inherited_owner_can_only_fill_a_gap_and_never_overwrite_one():
    """The ordering that makes the inheritance safe. A session whose own Memory read named an owner
    keeps it, so a parent's actor can never be written over an answer Memory gave directly — which is
    what would silently move one tenant's run into another's bundle."""
    reader = _StubMemoryReader("its-own-owner", owners={"the-parent-request": "somebody-else"})
    run = build_run(_scenario("plan-session-1787244223"),
                    span_source=_StubParentedSpanSource("the-parent-request"),
                    plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                    window_source=_StubWindowSource({}), memory_reader=reader)

    assert run.actor_id == "its-own-owner"
    assert reader.owner_lookups == [], "no lookup at all when the session answered for itself"


def test_an_unresolvable_parent_leaves_the_owner_unknown_rather_than_guessing():
    """Three states, kept apart. No parent in the evidence, and a parent no actor claims, both leave
    `actor_id` UNKNOWN — never `""`, which would collect every unowned session into one partition."""
    no_parent = build_run(_scenario("plan-session-1787244223"),
                          span_source=_StubSpanSource([]),
                          plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                          window_source=_StubWindowSource({}),
                          memory_reader=_StubMemoryReader(None))
    assert no_parent.actor_id is UNKNOWN

    unclaimed = build_run(_scenario("plan-session-1787244223"),
                          span_source=_StubParentedSpanSource("a-parent-nobody-claims"),
                          plan_source=HarvestedPlanSource("/nonexistent/plan-state.json"),
                          window_source=_StubWindowSource({}),
                          memory_reader=_StubMemoryReader(None))
    assert unclaimed.actor_id is UNKNOWN


def _sessions_file(tmp_path, session_id, window) -> str:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({session_id: {"n_spans": 3, "spans": [], "window": window}}))
    return str(path)


def _sidecar_file(tmp_path, session_id, window) -> str:
    path = tmp_path / "run-windows.json"
    path.write_text(json.dumps({"windows": {session_id: window}}))
    return str(path)


def test_harvested_window_source_reads_the_shape_harvest_writes(tmp_path):
    """The keys here are what ``harvest.py::_window_for_session`` writes."""
    window = {"start": "2026-08-13T18:02:55.022000+00:00",
              "end": "2026-08-13T18:43:34.769000+00:00",
              "provenance": "derived_plan_and_span_bounds",
              "recorded_at": "2026-08-24T10:00:00+00:00",
              "note": "harvest.py; 424 spans"}
    source = HarvestedWindowSource(sessions_path=_sessions_file(tmp_path, "s1", window),
                                   sidecar_path=str(tmp_path / "absent.json"))
    got = source.window_for("s1")
    assert got.provenance == "derived_plan_and_span_bounds"
    assert got.derived() is True
    assert round(got.duration_s()) == 2440
    assert source.window_for("unknown-session") is UNKNOWN


def test_a_harvest_stamp_beats_the_backfill_sidecar(tmp_path):
    """A re-harvest that stamps a better window must supersede an older backfill."""
    stamped = {"start": "2026-08-13T18:00:00+00:00", "end": "2026-08-13T18:30:00+00:00",
               "provenance": "collector_stamped"}
    backfilled = {"start": "2026-08-13T18:05:00+00:00", "end": "2026-08-13T18:25:00+00:00",
                  "provenance": "derived_plan_updated_at"}
    source = HarvestedWindowSource(sessions_path=_sessions_file(tmp_path, "s1", stamped),
                                   sidecar_path=_sidecar_file(tmp_path, "s1", backfilled))
    assert source.window_for("s1").provenance == "collector_stamped"


def test_sidecar_is_used_when_the_harvest_stamped_nothing(tmp_path):
    backfilled = {"start": "2026-08-13T18:05:00+00:00", "end": "2026-08-13T18:25:00+00:00",
                  "provenance": "derived_plan_updated_at"}
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"s1": {"n_spans": 3, "spans": []}}))  # no window key
    source = HarvestedWindowSource(sessions_path=str(path),
                                   sidecar_path=_sidecar_file(tmp_path, "s1", backfilled))
    assert source.window_for("s1").provenance == "derived_plan_updated_at"


def test_a_recorded_window_without_provenance_is_refused(tmp_path):
    """Unlabelled cannot be assumed stamped — that is how a derived value gets laundered."""
    source = HarvestedWindowSource(
        sessions_path=_sessions_file(tmp_path, "s1", {"start": "2026-08-13T18:00:00+00:00",
                                                     "end": "2026-08-13T18:30:00+00:00"}),
        sidecar_path=str(tmp_path / "absent.json"))
    assert source.window_for("s1") is UNKNOWN


def test_an_unusable_recorded_window_is_unknown_not_partial(tmp_path):
    for bad in ({"start": "", "end": "2026-08-13T18:30:00+00:00", "provenance": "collector_stamped"},
                {"start": "2026-08-13T19:00:00+00:00", "end": "2026-08-13T18:00:00+00:00",
                 "provenance": "collector_stamped"}):
        source = HarvestedWindowSource(sessions_path=_sessions_file(tmp_path, "s1", bad),
                                       sidecar_path=str(tmp_path / "absent.json"))
        assert source.window_for("s1") is UNKNOWN, bad


# ---------------------------------------------------------------------------------------
# EVALUATE must not learn about either field
# ---------------------------------------------------------------------------------------

def test_evaluate_reads_no_new_run_attributes():
    """Infrastructure context changes BLAME, never the score. A FAIL stays a FAIL.

    Pinned structurally rather than by convention: this walks ``evaluate.py``'s AST for every
    ``run.<attr>`` access. The temptation the next reader will feel is to soften a verdict
    when the platform was degraded; that belongs in mining's routing, not in scoring, and this
    test is what stops it landing here by accident.
    """
    import ast

    path = os.path.join(os.path.dirname(__file__), "..", "src", "improve", "evaluate.py")
    tree = ast.parse(open(path).read())
    read = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "run"}
    assert read == {"plan", "resources", "run_id", "scenario", "trajectory"}, (
        f"evaluate.py now reads {sorted(read)}. `window`/`infra_health` are evidence for "
        "mining's routing, not inputs to a verdict.")


# ---------------------------------------------------------------------------------------
# the third state, carried structurally rather than only in prose
# ---------------------------------------------------------------------------------------

def test_an_alarm_that_could_not_be_placed_is_named_structurally_not_only_in_prose():
    """`AlarmObservation.in_alarm` is a tri-state and only two of the three survived onto
    `InfraHealth`: this function kept `in_alarm is True` and dropped the `None`s, so an alarm nobody
    could place reached a reader inside `reason` — at most three of them, as prose, in a sentence a
    consumer cannot branch on or count.

    Both lists are needed to read `state`. Here one genuine incident and two unplaceable alarms land
    on DEGRADED, and the reader can see the verdict rests on one alarm out of three.
    """
    window = _recent_window()
    inside = (window.start_dt() + timedelta(minutes=2)).isoformat()
    records = [
        AlarmRecord(name="blind-1", current_state="INSUFFICIENT_DATA"),
        AlarmRecord(name="blind-2", current_state="INSUFFICIENT_DATA"),
        AlarmRecord(name="firing", current_state="ALARM", transitions=(
            AlarmTransition(ts=inside, old_state="OK", new_state="ALARM"),)),
    ]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.alarms == ("firing",)
    assert health.indeterminate == ("blind-1", "blind-2")


def test_an_unknown_state_says_whether_it_had_anything_to_be_unsure_about():
    """The two ways to be UNKNOWN after a lookup that RAN, and the field that separates them.

    A chronic ALARM is deliberately indeterminate (`_state_over_window` rule 5), so this population
    is every alarm unplaceable: state UNKNOWN with two named alarms behind it. A lookup with nothing
    to be unsure about — no coverage over the window — is UNKNOWN with an empty list. Absence is
    never zero, so `indeterminate: ()` may not stand in for "we could not place them".
    """
    window = _recent_window()
    chronic = [AlarmRecord(name=f"chronic-{i}", current_state="ALARM") for i in range(2)]
    lookup = lookup_from_records(chronic, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    could_not_place = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert could_not_place.state is InfraState.UNKNOWN
    assert could_not_place.indeterminate == ("chronic-0", "chronic-1")

    nothing_to_place = infra_health_for_window(
        window, source=_StubSource(AlarmLookup(conclusive=False, reason="no coverage")), now=NOW)
    assert nothing_to_place.state is InfraState.UNKNOWN
    assert nothing_to_place.indeterminate == ()


def test_a_conclusively_healthy_window_names_no_indeterminate_alarm():
    """The measured-and-empty case, so an empty list is reachable for a real reason as well as for a
    lookup that never got that far."""
    window = _recent_window()
    before = (window.start_dt() - timedelta(hours=2)).isoformat()
    records = [AlarmRecord(name="quiet", current_state="OK", transitions=(
        AlarmTransition(ts=before, old_state="ALARM", new_state="OK"),))]
    lookup = lookup_from_records(records, window.start_dt(), window.end_dt(),
                                 NOW - timedelta(days=2), NOW, "stub")
    health = infra_health_for_window(window, source=_StubSource(lookup), now=NOW)
    assert health.state is InfraState.HEALTHY
    assert health.alarms == () and health.indeterminate == ()
