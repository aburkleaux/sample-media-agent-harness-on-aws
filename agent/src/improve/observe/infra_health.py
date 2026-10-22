"""Was the PLATFORM degraded while a run was in flight? Three states, never a bool.

The loop cannot currently tell "the agent failed" from "the platform failed underneath the
agent" — both look identical in the evidence, so a bad afternoon at AWS can be mined into a
confident recommendation to change harness code. This module supplies the missing evidence:
given a run's stamped ``RunWindow``, it reports whether CloudWatch alarms were firing over
that interval.

What it is NOT:
  * Not a score input. ``evaluate`` does not read it; a FAIL stays a FAIL. Infrastructure
    context changes BLAME, never the verdict.
  * Not per-run attribution. Alarm state is ACCOUNT-WIDE. Two runs in the same window get
    the identical stamp, passing runs included, and a firing alarm may belong to a subsystem
    the run never touched. This is ambient correlation only.
  * Not the mine-time gate. Routing degraded-and-failing runs to an environment bucket is a
    separate change; this module only produces the evidence it will read.

Three rules make the three-state honest, and each is a case where a ``degraded: bool``
defaulting to ``False`` would have lied:
  1. **A failed lookup is UNKNOWN.** Absent credentials, a throttle, a wrong region, any
     botocore error — ``AlarmLookup.conclusive`` stays False and the state is UNKNOWN.
  2. **Silence is not health.** Zero alarms in the account means nothing was watching, and
     an unwatched platform is not a healthy one. ``DescribeAlarmHistory`` returning no
     transitions inside the window likewise proves nothing on its own — an alarm that went
     into ALARM *before* the window and stayed there emits no item inside it, so the state
     entering the window has to be reconstructed (see ``_state_over_window``).
  3. **Retention bounds what is knowable.** CloudWatch keeps alarm history ~14 days. A
     window older than that cannot be answered, and must not come back HEALTHY because the
     query returned nothing. The one exception is a source that declares ``coverage()``
     spanning the window — a harvest taken while the history still existed still knows the
     answer, and discarding a snapshot we already hold would be a different kind of
     dishonesty. Silence is still not coverage: the default is the conservative answer.

A positive sighting outweighs incompleteness: if any alarm was seen in ALARM during the
window, the state is DEGRADED even when other alarms were indeterminate. Evidence that
something WAS broken is not erased by not knowing about the rest.

Sources are injectable and offline-first, mirroring ``HarvestedSpanSource`` /
``HarvestedPlanSource``: the default is a local harvest file, so the golden path stays
deterministic and creds-free. ``LiveCloudWatchAlarmSource`` is opt-in — either injected, or
selected by ``IMPROVE_INFRA_ALARMS=live``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

try:
    from ..contract import UNKNOWN, InfraHealth, InfraState, RunWindow, parse_utc
    from .span_trajectory import harvest_dir
except ImportError:  # pragma: no cover
    from improve.contract import (  # type: ignore
        UNKNOWN, InfraHealth, InfraState, RunWindow, parse_utc)
    from improve.observe.span_trajectory import harvest_dir  # type: ignore

# CloudWatch retains alarm history for 14 days. A window older than this is unanswerable —
# not healthy. Slightly conservative (13d) so a run right at the edge, whose earliest
# transitions may already have aged out mid-query, is reported UNKNOWN rather than clean.
ALARM_HISTORY_RETENTION_DAYS = 13

# The only two CloudWatch state values that answer the question. Anything else the API can
# return — INSUFFICIENT_DATA, or an unreadable HistoryData blob — is indeterminate, and falls
# through to UNKNOWN rather than being lumped in with OK.
ALARM = "ALARM"
OK = "OK"


@dataclass(frozen=True)
class AlarmTransition:
    """One state change of one alarm, as recorded by DescribeAlarmHistory."""
    ts: str
    old_state: str
    new_state: str
    summary: str = ""


@dataclass(frozen=True)
class AlarmRecord:
    """One alarm: its state at lookup time plus every retained transition."""
    name: str
    current_state: str = ""
    transitions: tuple[AlarmTransition, ...] = ()


@dataclass(frozen=True)
class AlarmObservation:
    """What we could establish about ONE alarm over the run's window.

    ``in_alarm`` is a tri-state on purpose: ``None`` means indeterminate for this alarm
    (e.g. it was INSUFFICIENT_DATA entering the window), which contributes UNKNOWN rather
    than quietly counting as OK.
    """
    name: str
    in_alarm: Optional[bool]
    basis: str = ""


@dataclass(frozen=True)
class AlarmLookup:
    """A source's answer. ``conclusive`` defaults False so a half-answer cannot read HEALTHY.

    ``coverage_start``/``coverage_end`` are the interval the source could actually speak
    about; a window sticking out of it is not answerable.
    """
    alarms: tuple[AlarmObservation, ...] = ()
    conclusive: bool = False
    reason: str = ""
    source: str = ""


class AlarmSource(Protocol):
    """Answers "which alarms were firing between these two instants?"."""

    def alarms_in_window(self, start: datetime, end: datetime) -> AlarmLookup: ...


# --------------------------------------------------------------------------------------
# shared window reasoning
# --------------------------------------------------------------------------------------

def _state_over_window(record: AlarmRecord, start: datetime, end: datetime,
                       coverage_start: Optional[datetime]) -> AlarmObservation:
    """Was this alarm in ALARM at any point in [start, end]? True / False / None.

    The subtle case is the state ENTERING the window. An alarm that fired an hour before the
    run and was still firing throughout it emits no history item inside the window, so
    "no transitions in the window" cannot mean OK. Resolution order:

      1. a transition to ALARM inside the window  -> True, conclusively;
      2. the last transition BEFORE the window     -> the state it left the alarm in;
      3. no transition before, but one inside      -> that item's ``oldState``;
      4. no transition before or inside, but one after -> that item's ``oldState``;
      5. no retained transition at all             -> the state has held unchanged for the
         whole retained period, so the alarm's CURRENT state applies to the window too.

    (5) is only sound if the source looked back far enough that "no transitions" really means
    "none retained" — hence ``coverage_start``, which the caller must have pushed back to the
    retention horizon. Without it the answer is indeterminate rather than optimistic.
    """
    inside, before, after = [], [], []
    for transition in record.transitions:
        ts = parse_utc(transition.ts)
        if ts is None:
            continue
        if ts < start:
            before.append((ts, transition))
        elif ts <= end:
            inside.append((ts, transition))
        else:
            after.append((ts, transition))
    inside.sort(key=lambda p: p[0])
    before.sort(key=lambda p: p[0])
    after.sort(key=lambda p: p[0])

    if any(t.new_state == ALARM for _, t in inside):
        firing = [t for _, t in inside if t.new_state == ALARM][0]
        return AlarmObservation(record.name, True,
                                f"transitioned {firing.old_state}->ALARM inside the window")

    if before:
        entering, basis = before[-1][1].new_state, "state at window start, from the last transition before it"
    elif inside:
        entering, basis = inside[0][1].old_state, "state at window start, from the first in-window transition's oldState"
    elif after:
        entering, basis = after[0][1].old_state, "state at window start, from the first post-window transition's oldState"
    elif coverage_start is not None and coverage_start <= start:
        # No transition anywhere in the retained history means the state has held unchanged for
        # the whole period. That is evidence of a PERSISTENT CONDITION, not of an incident that
        # coincided with this run. For OK it is harmless: a platform quiet for a long time was
        # quiet during the window too. For ALARM it is not.
        #
        # An alarm lit continuously for longer than the history we can read, with nothing tying
        # it to this run, carries no information about the run. Treating it as "firing during
        # the window" would divert every failure to the environment bucket and leave the loop
        # with nothing to mine, while one unattended alarm stayed lit.
        #
        # So a chronic ALARM is INDETERMINATE, not confirmed. That is honest in both directions:
        # it does not claim the platform was fine, and it does not claim this run was its victim.
        # It reaches the reader as a caveat rather than a verdict.
        if record.current_state == ALARM:
            return AlarmObservation(record.name, None, (
                "chronic: in ALARM for longer than the retained history, with no transition "
                "inside or before the window, so nothing ties it to this run. An unattended "
                "alarm is not an incident"))
        entering, basis = record.current_state, "no retained transition; current state held across the window"
    else:
        return AlarmObservation(record.name, None,
                                "no transition retained and history was not read back past the window")

    if entering == ALARM:
        return AlarmObservation(record.name, True, f"already in ALARM entering the window ({basis})")
    if entering == OK and all(t.new_state == OK for _, t in inside):
        return AlarmObservation(record.name, False, f"OK throughout ({basis})")
    return AlarmObservation(record.name, None,
                            f"state entering the window was {entering or 'unrecorded'} ({basis})")


def lookup_from_records(records: list, start: datetime, end: datetime,
                        coverage_start: Optional[datetime], coverage_end: Optional[datetime],
                        source: str) -> AlarmLookup:
    """Turn raw alarm records into an ``AlarmLookup``, shared by every source.

    Inconclusive when: the account has no alarms at all (nothing was watching — silence is
    not health), the coverage does not span the window, or any single alarm's state could not
    be established.
    """
    if coverage_start is not None and coverage_start > start:
        return AlarmLookup(conclusive=False, source=source, reason=(
            f"alarm history only covers back to {coverage_start.isoformat()}, "
            f"but the run window starts at {start.isoformat()}"))
    if coverage_end is not None and coverage_end < end:
        return AlarmLookup(conclusive=False, source=source, reason=(
            f"alarm history only covers up to {coverage_end.isoformat()}, "
            f"but the run window ends at {end.isoformat()}"))
    if not records:
        return AlarmLookup(conclusive=False, source=source, reason=(
            "no alarms found — nothing was watching this account/region over the window, "
            "which is not the same as a healthy platform"))

    observations = tuple(_state_over_window(r, start, end, coverage_start) for r in records)
    firing = [o for o in observations if o.in_alarm is True]
    indeterminate = [o for o in observations if o.in_alarm is None]
    if firing:
        return AlarmLookup(observations, conclusive=True, source=source, reason=(
            f"{len(firing)} of {len(observations)} alarms were in ALARM during the window"
            + (f"; {len(indeterminate)} indeterminate" if indeterminate else "")))
    if indeterminate:
        return AlarmLookup(observations, conclusive=False, source=source, reason=(
            f"{len(indeterminate)} of {len(observations)} alarms could not be placed: "
            + "; ".join(f"{o.name} ({o.basis})" for o in indeterminate[:3])))
    return AlarmLookup(observations, conclusive=True, source=source, reason=(
        f"all {len(observations)} alarms were OK across the window"))


# --------------------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------------------

class HarvestedAlarmSource:
    """Alarm history from a local harvest file — the DEFAULT, offline and creds-free.

    Written by ``ideas/staging-data/harvest.py`` (``--skip-alarms`` to omit). When the file
    is absent the answer is an explicit inconclusive with the path in the reason, so a loop
    running without harvested alarms says "unknown, and here is what to harvest" rather than
    reporting a clean platform.
    """

    SOURCE = "harvested-alarm-history"

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            fresh = os.path.join(harvest_dir(), "fresh", "alarm-history-fresh.json")
            original = os.path.join(harvest_dir(), "alarms", "alarm-history.json")
            path = fresh if os.path.exists(fresh) else original
        self._path = path
        self._data: Optional[dict] = None

    def coverage(self) -> tuple:
        """``(start, end)`` the FILE can speak about, or ``(None, None)``.

        Declared so a snapshot taken while CloudWatch still held the history can answer for a
        window that has since aged past retention. Without this the retention rule would make
        the golden set permanently unanswerable a fortnight after it was harvested — throwing
        away evidence we already have on disk.
        """
        data = self._load() or {}
        coverage = data.get("coverage") or {}
        return parse_utc(coverage.get("start")), parse_utc(coverage.get("end"))

    def _load(self) -> Optional[dict]:
        if self._data is None:
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.info("no usable harvested alarm history at %s (%s)", self._path, e)
                self._data = {}
        return self._data or None

    def alarms_in_window(self, start: datetime, end: datetime) -> AlarmLookup:
        data = self._load()
        if not data:
            return AlarmLookup(conclusive=False, source=self.SOURCE, reason=(
                f"no harvested alarm history at {self._path} — run "
                "ideas/staging-data/harvest.py, or inject LiveCloudWatchAlarmSource"))
        coverage = data.get("coverage") or {}
        records = [
            AlarmRecord(
                name=a.get("name", ""),
                current_state=a.get("current_state", ""),
                transitions=tuple(
                    AlarmTransition(ts=t.get("ts", ""), old_state=t.get("old_state", ""),
                                    new_state=t.get("new_state", ""),
                                    summary=t.get("summary", ""))
                    for t in (a.get("transitions") or [])),
            )
            for a in (data.get("alarms") or [])
        ]
        return lookup_from_records(records, start, end,
                                   parse_utc(coverage.get("start")),
                                   parse_utc(coverage.get("end")),
                                   self.SOURCE)


class LiveCloudWatchAlarmSource:
    """Read-only CloudWatch lookup: ``DescribeAlarms`` + ``DescribeAlarmHistory``.

    Same shape as ``LiveSpanSource``: opt-in, read-only, no writes of any kind, and it needs
    no infrastructure change — alarm HISTORY is queryable whether or not anything subscribes
    to the alarm's SNS topic.

    History is read from the retention horizon (not just the run's window) because the state
    ENTERING the window is what makes "no transitions inside it" interpretable — see
    ``_state_over_window`` rule (5).

    Every failure path returns an inconclusive lookup carrying the error text. That is the
    whole contract of this class: no credentials, a throttle, a denied ``cloudwatch:Describe*``
    or a bad region all yield UNKNOWN, never HEALTHY.
    """

    SOURCE = "cloudwatch-describe-alarm-history"

    def __init__(self, region: str = "us-west-2", profile: Optional[str] = None,
                 alarm_name_prefix: Optional[str] = None) -> None:
        self._region = region
        self._profile = profile
        self._prefix = alarm_name_prefix

    def _client(self):
        import boto3
        session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
        return session.client("cloudwatch", region_name=self._region)

    def alarms_in_window(self, start: datetime, end: datetime) -> AlarmLookup:
        coverage_start = datetime.now(timezone.utc) - timedelta(days=ALARM_HISTORY_RETENTION_DAYS)
        try:
            client = self._client()
            records = self._describe(client, coverage_start, end)
        except Exception as e:  # noqa: BLE001 — ANY failure must degrade to UNKNOWN
            logger.warning("alarm lookup failed (%s: %s) — infra health is UNKNOWN, not healthy",
                           type(e).__name__, e)
            return AlarmLookup(conclusive=False, source=self.SOURCE,
                               reason=f"alarm lookup failed: {type(e).__name__}: {e}")
        return lookup_from_records(records, start, end, coverage_start,
                                   datetime.now(timezone.utc), self.SOURCE)

    def _describe(self, client, coverage_start: datetime, end: datetime) -> list:
        current: dict = {}
        kwargs: dict = {"AlarmTypes": ["CompositeAlarm", "MetricAlarm"]}
        if self._prefix:
            kwargs["AlarmNamePrefix"] = self._prefix
        token = None
        while True:
            page = client.describe_alarms(**({**kwargs, "NextToken": token} if token else kwargs))
            for key in ("MetricAlarms", "CompositeAlarms"):
                for alarm in page.get(key, []):
                    current[alarm["AlarmName"]] = alarm.get("StateValue", "")
            token = page.get("NextToken")
            if not token:
                break

        transitions: dict = {name: [] for name in current}
        history_kwargs = {
            "HistoryItemType": "StateUpdate",
            "StartDate": coverage_start,
            # Read to NOW, not to the window end: a transition AFTER the window still
            # identifies the state that held during it (rule 4).
            "EndDate": datetime.now(timezone.utc),
            "ScanBy": "TimestampAscending",
            "MaxRecords": 100,
        }
        # No name filter on the history call: ``DescribeAlarmHistory`` takes a single
        # AlarmName, not a prefix, so the prefix is applied by the membership test below
        # (``transitions`` holds only the alarms DescribeAlarms returned).
        token = None
        while True:
            page = client.describe_alarm_history(
                **({**history_kwargs, "NextToken": token} if token else history_kwargs))
            for item in page.get("AlarmHistoryItems", []):
                name = item.get("AlarmName", "")
                if name not in transitions:
                    continue  # an alarm outside the requested prefix, or since deleted
                old_state, new_state = _states_from_history_data(item.get("HistoryData"))
                transitions[name].append(AlarmTransition(
                    ts=_iso(item.get("Timestamp")), old_state=old_state, new_state=new_state,
                    summary=(item.get("HistorySummary") or "")[:200]))
            token = page.get("NextToken")
            if not token:
                break

        return [AlarmRecord(name=name, current_state=state,
                            transitions=tuple(transitions.get(name, ())))
                for name, state in current.items()]


def _iso(value) -> str:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _states_from_history_data(raw) -> tuple:
    """``(old_state, new_state)`` from a history item's ``HistoryData`` JSON blob.

    Unparseable data yields ``("", "")``, which makes the alarm INDETERMINATE rather than OK
    — the transition existed, so pretending we read it as OK would be a fabricated pass.
    """
    if not raw:
        return "", ""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return (data.get("oldState", {}).get("stateValue", "") or "",
                data.get("newState", {}).get("stateValue", "") or "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "", ""


def default_alarm_source() -> AlarmSource:
    """The source ``build_run`` uses when none is injected.

    Offline harvest by default — deterministic, creds-free, so the golden path and the whole
    test suite never touch AWS. ``IMPROVE_INFRA_ALARMS=live`` opts into the CloudWatch
    lookup (region from ``IMPROVE_INFRA_ALARM_REGION``, credentials from ``AWS_PROFILE``),
    which is the switch that keeps ``LiveCloudWatchAlarmSource`` on a reachable path instead
    of being wired-but-never-run.
    """
    if (os.environ.get("IMPROVE_INFRA_ALARMS") or "").strip().lower() == "live":
        return LiveCloudWatchAlarmSource(
            region=os.environ.get("IMPROVE_INFRA_ALARM_REGION", "us-west-2"),
            profile=os.environ.get("AWS_PROFILE"),
            alarm_name_prefix=os.environ.get("IMPROVE_INFRA_ALARM_PREFIX") or None)
    return HarvestedAlarmSource()


def infra_health_for_window(window, source: Optional[AlarmSource] = None,
                            now: Optional[datetime] = None) -> InfraHealth:
    """Ambient platform health over ``window``. Always returns an ``InfraHealth``.

    ``window`` may be the ``UNKNOWN`` sentinel — that is the common case for runs collected
    before windows were stamped, and it yields UNKNOWN with the reason spelled out.

    ``now`` is injectable so the retention rule is testable without freezing the clock.
    """
    if not isinstance(window, RunWindow):
        return InfraHealth(
            state=InfraState.UNKNOWN, window=UNKNOWN, source="",
            reason=("no run window was stamped, so no interval could be correlated against "
                    "alarm history (a window is never derived from spans at read time — "
                    "spans are what goes missing when infrastructure breaks)"))

    now = now or datetime.now(timezone.utc)
    src = source or default_alarm_source()
    source_name = getattr(src, "SOURCE", type(src).__name__)
    age = now - window.start_dt()
    if age > timedelta(days=ALARM_HISTORY_RETENTION_DAYS) and not _source_covers(src, window):
        return InfraHealth(
            state=InfraState.UNKNOWN, window=window, source=source_name,
            reason=(f"window starts {age.days}d ago, past CloudWatch's ~"
                    f"{ALARM_HISTORY_RETENTION_DAYS}d alarm-history retention — the answer "
                    "is unavailable, which is not the same as healthy"))

    try:
        lookup = src.alarms_in_window(window.start_dt(), window.end_dt())
    except Exception as e:  # noqa: BLE001 — a raising source must not take the loop down
        logger.warning("alarm source %s raised (%s: %s) — infra health is UNKNOWN",
                       source_name, type(e).__name__, e)
        return InfraHealth(state=InfraState.UNKNOWN, window=window, source=source_name,
                           reason=f"alarm source raised: {type(e).__name__}: {e}")

    firing = tuple(o.name for o in lookup.alarms if o.in_alarm is True)
    # `in_alarm is None` is the third state `AlarmObservation` exists to keep: the alarm was looked
    # at and its state over the window could not be established. Carried alongside `firing` because
    # a HEALTHY or UNKNOWN verdict is only readable against how many alarms were unplaceable —
    # `lookup.reason` names at most the first three of them and only as prose.
    unplaceable = tuple(o.name for o in lookup.alarms if o.in_alarm is None)
    detail = _window_caveat(window)
    if firing:
        return InfraHealth(state=InfraState.DEGRADED, window=window, alarms=firing,
                           indeterminate=unplaceable,
                           source=lookup.source or source_name,
                           reason=f"{lookup.reason}{detail}")
    if lookup.conclusive:
        return InfraHealth(state=InfraState.HEALTHY, window=window,
                           indeterminate=unplaceable,
                           source=lookup.source or source_name,
                           reason=f"{lookup.reason}{detail}")
    return InfraHealth(state=InfraState.UNKNOWN, window=window,
                       indeterminate=unplaceable,
                       source=lookup.source or source_name,
                       reason=f"{lookup.reason}{detail}")


def _source_covers(src, window: RunWindow) -> bool:
    """Does this source hold a snapshot that DEMONSTRABLY spans the window?

    The retention rule exists because CloudWatch forgets, not because old runs are
    unknowable: a harvest captured while the history still existed can still answer. Only an
    explicit, parseable ``coverage()`` spanning the whole window counts — the default is
    False, so a source that says nothing gets the conservative answer.
    """
    coverage = getattr(src, "coverage", None)
    if not callable(coverage):
        return False
    try:
        start, end = coverage()
    except Exception as e:  # noqa: BLE001 — a source that cannot state coverage has none
        logger.debug("alarm source %r could not state coverage: %s", src, e)
        return False
    return bool(start and end and start <= window.start_dt() and end >= window.end_dt())


def _window_caveat(window: RunWindow) -> str:
    """Carry a derived window's weakness into the reason text.

    A window reconstructed from plan rows or spans may be narrower than the run really was,
    so an alarm firing just outside it would be missed. Saying so here is the difference
    between a reader trusting a HEALTHY and knowing how far to trust it.
    """
    if not window.derived():
        return ""
    return (f" [window is DERIVED ({window.provenance}), not stamped by a collector, so it "
            "may be narrower than the run's true extent]")
