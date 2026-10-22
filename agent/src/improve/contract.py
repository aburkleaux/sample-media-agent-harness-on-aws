"""Universal contract types shared by every loop stage.

OBSERVE emits ``Run``; EVALUATE emits ``Verdict`` (carrying a ``UtilityVector``).
Later phases add ``Proposal`` etc. Keeping one set of types across stages is what
lets the loop be re-targeted without a rewrite.

Sentinels encode "absent" so it can never read as a passing value: a missing
evidence field is ``NO_DATA`` (not ``None``/``[]``); an undeterminable harness
version is ``UNKNOWN``; an unmeasured utility term is ``UNMEASURED`` (not ``0``).
Use identity checks (``x is NO_DATA``).

Timestamps: ONE format, everywhere a new field is added here — ISO-8601 with an
explicit ``+00:00`` UTC offset. ``parse_utc``/``to_utc_iso`` are the only sanctioned
converters; see ``parse_utc`` for why a bare ``Z`` and an offset-less value are both
hazards this codebase has already been bitten by.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Union


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return False


NO_DATA = _Sentinel("NO_DATA")
UNKNOWN = _Sentinel("UNKNOWN")
UNMEASURED = _Sentinel("UNMEASURED")

# Why ``Run.resources['cost']`` can still read UNMEASURED, now that it is normally priced. Lives
# here, next to the sentinel it explains, so the reader that sets it (``observe.build_run``) and the
# wire that publishes it (``dto``) name the SAME fact and cannot drift into two explanations.
#
# Cost IS measured on the live path: priced per model and per token category from
# ``observe.build_run._BEDROCK_RATES_USD_PER_MTOK``, whose source and read-date live once, in
# ``_RATE_PROVENANCE`` beside it, and travel to the UI verbatim as ``costBasis``. Do not copy them
# here. Hand-entered rates DO go stale — that objection was answered by shipping the date with
# them, so an old table is visible to a reader rather than silently wrong.
#
# So the sentinel now means NOTHING in the session priced: no per-model counts to price, or no model
# in it with a complete rate row. A session the table can price only in PART reports the part, with
# ``spend.cost``'s token counts carrying the shortfall — a partial figure is not an absent one, and
# a zero cost may never stand in for either.
#
# This string is the FALLBACK, and it is reachable: ``build_run.cost_basis`` returns it when the
# rate table or its provenance is empty, and ``dto._cost_basis`` returns it for a population that
# reports no basis at all or more than one — two rate configurations merged, where no single price
# applied throughout. Its arithmetic warning holds regardless: cache reads are ~0.1x an input token
# and most of the prompt tokens here, so total tokens times one rate is wrong by most of an order
# of magnitude.
COST_UNMEASURED_REASON = (
    "tokens measured, cost not priced: no per-model Bedrock rate table is reachable from the "
    "deploy path (the AWS Price List API publishes no rates for this population's models and no "
    "Claude cache dimensions; Anthropic's list prices exclude Bedrock as partner-priced). The "
    "per-category and per-model token breakdown is measured, so a sourced rate table is the only "
    "missing input — do not multiply total tokens by one rate: cache reads are ~0.1x input and "
    "77% of prompt tokens here"
)


def parse_utc(raw) -> Optional[datetime]:
    """Any recorded timestamp -> a UTC-AWARE datetime, or None if unusable.

    Three formats reach this code and all three must land on the same instant type:
      * ``...+00:00`` — the canonical form, what everything new writes;
      * ``...Z`` — emitted by the plan writer's failure path and by Logs Insights;
      * offset-LESS (``2026-08-13 21:39:22.278``) — what the span harvest carries.

    Comparing them as STRINGS is wrong (``'…19.5Z' > '…19.5+00:00'`` because ``'Z' > '+'``,
    which manufactures an ordering violation out of two identical instants), and comparing a
    naive datetime with an aware one raises ``TypeError`` — which, on the plan side, would
    propagate out of ``dag_completion`` -> ``evaluate`` and kill the scoring run. So an
    offset-less value is assumed UTC (true for every producer here: Logs Insights
    ``@timestamp`` and DynamoDB writes are both UTC) and every parse failure degrades to
    ``None`` for the caller to turn into a sentinel.

    ``evaluators_improve._parse_ts`` delegates here so there is exactly one rule.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def to_utc_iso(raw) -> str:
    """Canonical ``...+00:00`` string for a recorded timestamp. Raises on unusable input.

    Raising is deliberate: the alternative — silently blanking — would produce a window
    object whose bounds are empty strings, and a downstream reader cannot tell that from a
    real interval. A source that cannot produce a usable timestamp must emit the ``UNKNOWN``
    sentinel instead of a half-built window.
    """
    parsed = parse_utc(raw)
    if parsed is None:
        raise ValueError(f"not a usable timestamp: {raw!r}")
    return parsed.astimezone(timezone.utc).isoformat()


def pluralise(count, singular: str, plural: Optional[str] = None) -> str:
    """The NOUN for a count: ``pluralise(1, "run")`` is ``run``, ``pluralise(2, "run")`` is ``runs``.

    The noun alone rather than ``1 run``, so a caller interpolates the figure itself —
    ``f"{n} {pluralise(n, 'run')}"``. Several of these sentences put words between the figure
    and the noun (``passing run``, ``dispatch call``) and a helper returning the whole phrase
    could not express them.

    Here, in the module every producing stage already imports, because these are FINISHED
    SENTENCES rather than UI strings: ``propose``'s ``evidence_summary``, ``signature``'s facet
    evidence (which reaches a reader as the wire's ``rootCauseHypo``) and ``mine``'s residue
    reasons are all composed in this package and rendered verbatim, so no consumer can repair
    them. The Improve surface holds the same rule for the strings IT composes
    (``improveChrome.pluralise``); a reader sees both halves in one suggestion, so the two must
    decide the same way about the same sentence. Deliberately not one spelling per module: the
    UI reached three spellings of this rule across twelve sites and two of them were wrong.

    Only exactly 1 is singular. ``0`` is plural (``0 runs``), and so is anything a producer bug
    can put here — a negative, or a count that is not a whole number — because English has no
    singular for those, and in that case the figure beside the noun is the visible defect and
    not the noun.

    ``plural`` is for a word whose plural is not ``+s``: ``pluralise(n, "dispatch",
    "dispatches")``. An argument rather than a conditional at the call site, so the singular
    rule stays in one place. It also covers the VERB that has to agree with the same count —
    ``pluralise(n, "has", "have")`` — because ``1 of 3 dispatches ... have no later retrieval``
    is the same defect as ``1 run(s)`` in the same sentence, and splitting the two across two
    mechanisms is how they would come to disagree about which count governs.
    """
    if count == 1:
        return singular
    return plural if plural is not None else f"{singular}s"


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # scenario has no such dimension
    NO_DATA = "NO_DATA"                # scenario claims the dimension; evidence absent -> RED


class Overall(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNSCORED = "UNSCORED"  # nothing scorable; never treated as PASS


@dataclass
class Step:
    """One node in a Run's cross-runtime trajectory (from a span)."""
    ts: str = ""
    runtime: str = ""
    name: str = ""
    status: str = "UNSET"          # OK | ERROR | UNSET
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None


@dataclass
class PlanNode:
    id: str
    depends_on: list[str] = field(default_factory=list)
    status: str = ""               # completed | pending | failed | ...
    # Terminality alone cannot show whether a node ran AFTER the dependency whose
    # output it consumes. `ffab73f8` marked every node completed in 96s while the
    # diarization it depended on was still running, and scored PASS everywhere.
    # These two fields make that detectable (DF-11).
    updated_at: str = ""           # ISO ts of the last status write, "" if unknown
    gateway_status: str = ""        # async job outcome, e.g. SUCCEEDED — "" if not async


@dataclass
class Plan:
    goal: str = ""
    nodes: list[PlanNode] = field(default_factory=list)
    # False when the dependency edges could not be read (the writer offloads a wide plan's
    # node list to S3 and blanks the inline field). An edgeless plan and an unreadable one
    # are indistinguishable without this, so a plan whose edges were lost would be mined as
    # a genuinely FLAT, well-ordered plan — absence reading as a clean bill of health, which
    # is the one thing the sentinels exist to prevent.
    edges_available: bool = True


# Where a RunWindow's bounds came from. Only the first is authoritative; the rest are
# reconstructions and are labelled as such so nothing can pass one off as a stamp.
WINDOW_STAMPED = "collector_stamped"                  # the collector recorded the interval
WINDOW_FROM_PLAN_STATE = "derived_plan_updated_at"    # bounds of plan-state `updated_at`
WINDOW_FROM_SPANS = "derived_span_bounds"             # bounds of the session's span ts
WINDOW_FROM_PLAN_AND_SPANS = "derived_plan_and_span_bounds"   # union of the two above

DERIVED_WINDOW_PROVENANCE = frozenset({
    WINDOW_FROM_PLAN_STATE, WINDOW_FROM_SPANS, WINDOW_FROM_PLAN_AND_SPANS,
})


@dataclass(frozen=True)
class RunWindow:
    """The wall-clock interval a run occupied, and WHERE that interval came from.

    Wall-clock, not busy-time: ``resources['latency_ms']`` is the sum of tool durations and
    stays what it is. This is the span of real time the run sat inside, which is the only
    thing an account-wide infrastructure signal can be correlated against.

    ``provenance`` is not decoration. The interval is supposed to be STAMPED by whatever
    collected the run, because the alternative — recomputing it from spans at read time — is
    absent in exactly the case it is needed: spans are the first thing to disappear when the
    platform breaks, so a span-derived window narrows or vanishes precisely during an
    outage, and a missing window would then read as "no infrastructure problem". A derived
    window is still useful evidence, so it is allowed, but it is labelled
    (``DERIVED_WINDOW_PROVENANCE``) and a reader can discount it. Never relabel one as
    ``WINDOW_STAMPED``.

    Bounds are normalized on construction to the one documented format (ISO-8601 with an
    explicit ``+00:00`` offset). Unusable input raises rather than yielding a window with
    empty bounds — a source that cannot produce one must emit ``UNKNOWN``.
    """
    start: str
    end: str
    provenance: str = WINDOW_STAMPED
    recorded_at: str = ""    # when the stamp/derivation was written, canonical form or ""
    note: str = ""           # free text: which rows/spans the bounds came from

    def __post_init__(self) -> None:
        start, end = to_utc_iso(self.start), to_utc_iso(self.end)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.recorded_at:
            object.__setattr__(self, "recorded_at", to_utc_iso(self.recorded_at))
        if self.end_dt() < self.start_dt():
            raise ValueError(f"window ends before it starts: {start} .. {end}")

    def start_dt(self) -> datetime:
        parsed = parse_utc(self.start)
        assert parsed is not None  # guaranteed by __post_init__
        return parsed

    def end_dt(self) -> datetime:
        parsed = parse_utc(self.end)
        assert parsed is not None
        return parsed

    def duration_s(self) -> float:
        return (self.end_dt() - self.start_dt()).total_seconds()

    def derived(self) -> bool:
        """True when the bounds were reconstructed rather than stamped by a collector."""
        return self.provenance in DERIVED_WINDOW_PROVENANCE


class InfraState(str, Enum):
    """Three states, deliberately. A bool here is the bug this type exists to prevent.

    ``UNKNOWN`` is not a soft ``HEALTHY``: a throttled or failed CloudWatch call, absent
    credentials, no configured alarms, or a window older than alarm-history retention all
    land here. A ``degraded: bool`` defaulting to ``False`` would report each of those as a
    clean platform, which is the same absence-reads-as-a-pass failure the sentinels exist to
    stop.
    """
    DEGRADED = "degraded"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InfraHealth:
    """Whether the PLATFORM was alarming during a run's window. Ambient, not attributed.

    Read it as: "while this run was in flight, the account had N alarms firing". Alarm state
    is ACCOUNT-WIDE, not per-session — two runs sharing a window get the identical stamp,
    passing runs included, and an alarm may belong to a subsystem the run never touched. So
    this field can support correlation ("every failure that afternoon coincided with a
    degraded platform") and can never support per-run attribution ("THIS run failed because
    of THAT alarm"). Do not build blame on it.

    It also never changes a score. A FAIL stays a FAIL; ``evaluate`` does not read this
    field, by design. What it changes is where a mined failure is ROUTED — a later
    mine-time gate is expected to send degraded-and-failing runs to an environment bucket
    instead of into a recommendation to change harness code.

    There is deliberately NO boolean form and no ``.degraded`` property: compare ``state``
    by identity (``h.state is InfraState.DEGRADED``). Anything that collapses three states
    into two loses the only distinction the type carries.
    """
    state: InfraState = InfraState.UNKNOWN
    reason: str = ""                                  # why this state, in words
    alarms: tuple[str, ...] = ()                      # names seen in ALARM during the window
    # Alarms whose state over the window could NOT be established — INSUFFICIENT_DATA entering it,
    # an unreadable HistoryData blob, or a chronic ALARM with nothing tying it to this run. Named
    # separately from `alarms` because they are the reason a state is UNKNOWN rather than HEALTHY,
    # and because `indeterminate: ()` beside `state=UNKNOWN` says the doubt came from somewhere
    # else (no coverage, no alarms configured at all) rather than from a specific alarm. Only
    # `reason` used to carry them, and only the first three of them, as prose.
    indeterminate: tuple[str, ...] = ()
    window: Union[RunWindow, _Sentinel] = UNKNOWN      # the interval this refers to
    source: str = ""                                  # which alarm source answered


@dataclass
class Run:
    """What happened in one session, assembled from spans + Memory.

    Evidence fields carry NO_DATA when absent-but-expected. ``harness_snapshot``
    is UNKNOWN unless a config version was stamped at capture time.
    """
    run_id: str
    goal: Union[str, _Sentinel] = NO_DATA
    response: Union[str, _Sentinel] = NO_DATA          # final assistant text (Memory)
    # Which actor's conversation this session belongs to, read from AgentCore Memory alongside
    # the goal and response. UNKNOWN when no Memory read was made (the offline path) or when no
    # actor claimed the session — never ``""``, because the per-actor Improve bundle partitions
    # on this value and an empty string would collect every unowned session into one shared
    # partition that some real actor could then be served.
    actor_id: Union[str, _Sentinel] = UNKNOWN
    trajectory: Union[list[Step], _Sentinel] = NO_DATA
    plan: Union[Plan, _Sentinel] = NO_DATA
    turns: list[dict] = field(default_factory=list)    # [{role, text}] full conversation
    resources: dict = field(default_factory=dict)      # {latency_ms, tokens, cost}
    harness_snapshot: Union[dict, _Sentinel] = UNKNOWN
    scenario: Optional["Scenario"] = None
    # Wall-clock interval the run occupied, from whatever COLLECTED the run. UNKNOWN when no
    # collector recorded one — never recomputed here from spans (see RunWindow).
    window: Union[RunWindow, _Sentinel] = UNKNOWN
    # Ambient platform health over that window. Two ways to be unknown, and they mean
    # different things: the UNKNOWN sentinel means nobody looked (no source was wired — the
    # failure mode this codebase keeps hitting), while InfraHealth(state=UNKNOWN, reason=...)
    # means a lookup ran and could not tell. Neither may be read as healthy.
    infra_health: Union[InfraHealth, _Sentinel] = UNKNOWN
    # False when this run's spans overlap a harvest window that hit the Insights row cap, so part
    # of its trajectory is KNOWN missing. UNKNOWN when no harvester recorded either way — never
    # defaulted to True, which would read as "we checked and it was fine". Compare with
    # `is not False`: UNKNOWN is falsy (see `_Sentinel.__bool__`), so truthiness would read
    # "nobody looked" as "known incomplete".
    #
    # Distinct from `trajectory=NO_DATA`, which means NO spans at all. "We saw none" and "we saw
    # some and know we lost the rest" are different facts and each has its own consumer:
    # `trajectory_absent` reports the first, this field makes the trajectory-derived facets
    # UNOBSERVABLE for the second, so a partially-read passing run cannot act as a control.
    evidence_complete: Union[bool, _Sentinel] = UNKNOWN
    evidence_note: str = ""
    # The session id of the request that spawned this one, when this run is a plan sub-session.
    # Read from the OTEL trace its spans share with the parent's
    # (`observe.span_trajectory.parents_by_session`) — never parsed out of the id, which carries
    # no parent and never did. `classify` turns it into `Workload.parent_hint` so mining can
    # count sibling sub-sessions of one request as ONE piece of evidence.
    #
    # Two ways to be absent and they are not interchangeable: UNKNOWN means no source could say
    # (a harvest with no trace projection, or a live source that reads one session at a time),
    # NO_DATA means a source looked and the trace named no parent. Collapsing them would make an
    # unrefreshed harvest look like a population of parentless runs, which is exactly how the
    # de-duplication was inert without anything failing.
    parent_run_id: Union[str, _Sentinel] = UNKNOWN


@dataclass
class EvaluatorResult:
    name: str
    kind: str                      # code | llm
    outcome: Outcome
    score: Optional[float] = None
    explanation: str = ""


@dataclass
class UtilityVector:
    """Multi-term objective. A term is a float or the UNMEASURED sentinel."""
    terms: dict = field(default_factory=dict)

    def measured(self) -> dict:
        return {k: v for k, v in self.terms.items() if v is not UNMEASURED}


@dataclass
class Verdict:
    run_id: str
    evaluator_results: list[EvaluatorResult]
    utility: UtilityVector
    overall: Overall
    reason: str = ""
    # Which kind of invocation this was, and on what grounds (see `classify.py`). Recorded on
    # the verdict rather than recomputed downstream because it decided which evaluators ran:
    # a reader who cannot see the class cannot tell why `dispatch_correct` is absent from a
    # run that had no request to dispatch.
    workload_class: str = ""
    workload_basis: str = ""
    workload_evidence: str = ""
    # The parent request this run belongs to, when it is a sub-session. Carried because sibling
    # sub-sessions of one plan are ONE piece of evidence, not several, and mining has to be able
    # to say so: `classify` computes it and discarding it here would leave the de-duplication
    # key unavailable exactly where it is needed.
    workload_parent: str = ""
    # The evaluator names that gated THIS verdict. Gating is per-class now, so a single
    # population-wide tuple no longer describes it, and mining needs the real set to refuse
    # shape facets that are definitional subterms of a gating evaluator.
    gating: tuple = ()


@dataclass
class Scenario:
    """A labeled example: which run, which evaluators apply, expected outcome.

    Deliberately swappable — the dataset is the proof, not the machine.
    """
    scenario_id: str
    goal: str = ""
    category: str = ""
    evaluators: list[str] = field(default_factory=list)  # only named ones run
    claims: dict = field(default_factory=dict)           # {dag, async, manifest: bool}
    expected: dict = field(default_factory=dict)         # {overall, ...}
    source_session: str = ""
    # What produced this record's labels — judge model, threshold, goal source, run count.
    # Carried so a pipeline standing on recorded labels can say WHICH judgement it is
    # reproducing, instead of asserting a generic "recorded labels" and letting a relabel at
    # a different threshold pass unnoticed.
    label_provenance: str = ""


class FacetKind(str, Enum):
    """Why the split matters: only SHAPE facets may be used to test REQ-018.

    A GATING symptom facet ("the delivery tier failed") is the thing being explained, and
    it is false on every passing run by construction — so a signature containing one would
    satisfy ``pass_hits == 0`` for free, turning the discriminative gate into a no-op. The
    predictor must be the run's observable structure.

    Note the qualifier: this holds for *gating* symptoms only. A non-gating check can RED
    on a run that passes overall — measured, `DagCompletionValidator` FAILs on `6309f154`
    and `ffab73f8`, both of which PASS — so ``check_failed:*`` facets are not
    automatically false on passing runs. They are still classified SYMPTOM because they are
    outcomes rather than structure, but the "free pass_hits" argument does not apply to
    them, and a related hazard does: a shape facet that is a definitional subterm of a
    gating code evaluator makes that evaluator's symptom self-satisfying. ``mine`` guards
    that explicitly.
    """
    SHAPE = "shape"
    SYMPTOM = "symptom"


@dataclass(frozen=True)
class Facet:
    name: str
    kind: FacetKind
    value: bool
    evidence: str = ""
    # False when the evidence this facet reads was absent, so `value=False` means "we could
    # not see" rather than "we looked and it was not there". The distinction is load-bearing
    # for mining: an unobserved passing run must not be counted as a control the signature
    # successfully excluded, or missing data becomes evidence of discrimination.
    observed: bool = True


@dataclass
class FailureSignature:
    """A (shape -> symptom) hypothesis about one failing run.

    ``shape`` is the conjunction of SHAPE facet names that predicts ``symptom``.
    ``pass_hits`` counts passing runs whose shape also satisfies that conjunction;
    REQ-018 admits the signature only at 0.
    """
    run_id: str
    symptom: tuple[str, ...]
    shape: tuple[str, ...]
    pass_hits: int = -1                                  # -1 = not yet gated
    pass_hit_runs: tuple[str, ...] = ()
    refinement_log: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    # True when any facet in `shape` is a documented heuristic rather than a direct
    # measurement, so a reviewer can discount the signature accordingly.
    heuristic_facets: tuple[str, ...] = ()
    # How many passing runs this shape CONCLUSIVELY excluded, and how many could not be
    # judged because their own evidence was missing. `pass_hits == 0` against one control is
    # nearly free; against four it is a real claim. Without these two numbers an admitted
    # signature cannot be told apart from a lucky one.
    controls_excluded: int = 0
    controls_inconclusive: tuple[str, ...] = ()
    # The DENOMINATOR of the two above: how many passing runs were offered to the gate as controls
    # for this failure, after the same-class and one-per-parent-request restrictions. Recorded
    # rather than left to be re-derived, because `controls_excluded: 3` says nothing without it —
    # 3 of 3 is a gate that decided everything it was given, 3 of 12 is a gate that could not
    # place three quarters of its own evidence. For an admitted signature
    # `controls_excluded + len(controls_inconclusive) == controls_considered`, and nothing else
    # closes that sum, so a reader can check the triple against itself.
    controls_considered: int = 0
    # Other facets that reached pass_hits == 0 on the same population. A non-empty value
    # means the data does not distinguish this explanation from those, and the reported
    # shape was chosen by precedence rather than by evidence.
    equally_discriminative: tuple[str, ...] = ()


@dataclass
class ResidueEntry:
    """A signature that could NOT be made discriminative. Reporting these is the
    point: an unexplained failure must stay visible, not vanish."""
    run_id: str
    symptom: tuple[str, ...]
    best_shape: tuple[str, ...]
    pass_hits: int
    pass_hit_runs: tuple[str, ...]
    reason: str
    refinement_log: list[str] = field(default_factory=list)


@dataclass
class Attribution:
    """Which Σ component the signature is blamed on, and what we may do about it."""
    component_id: str
    layer: str
    mutability: str
    versioned: bool
    blast_radius: str
    source_location: str
    matched_failure_mode: str = ""
    # registry_declared = the registry's own known_failure_modes matched;
    # keyword_inferred = matched on component id/type/notes only. Weaker.
    basis: str = "keyword_inferred"


@dataclass
class Candidate:
    candidate_id: str
    label: str
    before: str = ""
    after: str = ""
    diff_text: str = ""
    claim: dict = field(default_factory=dict)
    risk: Optional[str] = None
    disposition: str = "advisory"      # publishable | advisory | code_change


@dataclass
class Proposal:
    proposal_id: str
    target_type: str
    target_id: str
    target_name: str
    operation: str                     # add | edit | remove
    summary: str
    evidence_summary: str
    # The short lead line for `evidence_summary`, from `propose.headline_for`. Empty when the
    # shape's leading facet has no headline, so a consumer omits the field rather than showing a
    # facet name; defaulted so a proposal built without one is still a valid Proposal.
    evidence_headline: str = ""
    signature: Optional[FailureSignature] = None
    attribution: Optional[Attribution] = None
    candidates: list[Candidate] = field(default_factory=list)
    base_version: Optional[int] = None
    blast_radius: dict = field(default_factory=dict)
    single_candidate_forced: Optional[str] = None
    provenance: dict = field(default_factory=dict)


def infra_state_of(run: "Run") -> InfraState:
    """A Run's platform state, collapsing "nobody looked" and "looked, could not tell".

    Both map to ``UNKNOWN`` — the distinction is diagnostic (see ``Run.infra_health``) and
    must not become a third branch in policy code. Provided so a caller never has to write
    ``if run.infra_health:``, whose truthiness would be meaningless here.
    """
    health = getattr(run, "infra_health", UNKNOWN)
    if isinstance(health, InfraHealth):
        return health.state
    return InfraState.UNKNOWN


def verdict_counts(verdicts) -> dict:
    """How a set of verdicts landed: ``{total, PASS, FAIL, UNSCORED}``.

    Keys are ``Overall``'s own values so neither consumer has to re-spell them. UNSCORED is a
    THIRD count and never folded into either of the others — see ``evaluate.gate``: it means a
    gating evaluator could not measure, which says something about the evidence and nothing
    about the agent.
    """
    counts = {"total": 0, Overall.PASS.value: 0, Overall.FAIL.value: 0,
              Overall.UNSCORED.value: 0}
    for verdict in verdicts:
        counts["total"] += 1
        counts[verdict.overall.value] += 1
    return counts


def verdict_counts_by_class(verdicts) -> dict:
    """Per workload class: how the verdicts landed, and what gated them.

    ``{cls: {total, PASS, FAIL, UNSCORED, gating: [tuple, ...], basis: [str, ...]}}``.

    ONE implementation, deliberately, because there are TWO consumers with two different output
    shapes — ``mine_local._workload_breakdown`` writes the offline artifact and
    ``dto._class_entry`` writes the wire — and this codebase has already paid for a hand copy of
    exactly this kind of fold. ``evaluate.gate`` carries the note: ``mine_local`` had its own
    copy of the verdict fold, it drifted, and the artifact's reason then concealed that both of
    the passing runs an ordering claim rested on carried a RED ``DagCompletionValidator``. The
    two PROJECTIONS are legitimately different (one is a stored artifact, one is a typed wire
    contract) and stay separate; the COUNTING is one fact about ``Verdict``s and lives here.

    ``gating`` and ``basis`` collect every DISTINCT value, sorted, never the first one seen.
    Keeping only the first would present it as though it described the whole class, so a mixed
    population — a config override applied to some runs, or golden's recorded labels alongside a
    harvest's inferred ones — would be reported as uniform. This field exists to say why a
    class's numbers mean what they do, so it must not be able to say something false.
    """
    grouped: dict = {}
    for verdict in verdicts:
        grouped.setdefault(verdict.workload_class or "unknown", []).append(verdict)
    return {
        cls: {
            **verdict_counts(group),
            "gating": sorted({tuple(sorted(v.gating or ())) for v in group}),
            "basis": sorted({v.workload_basis or "" for v in group}),
        }
        for cls, group in grouped.items()
    }


def gating_outcome_counts(verdicts) -> dict:
    """Which GATING evaluators failed, and which could not measure, across a population.

    ``{"failedGating": {name: n}, "unmeasured": {name: n}}`` — both keyed by evaluator name,
    both carrying every gating name observed in the population so a `0` is distinguishable from
    a check that was never gated at all (absent ≠ empty).

    Derived from ``EvaluatorResult.outcome``, never by parsing ``Verdict.reason``. The reason
    string carries an ``(on partial evidence: …)`` suffix, so counting distinct reasons would
    fragment across near-identical keys and read as many causes where there is one.

    A run that fails two gating checks counts once under EACH, so these figures do not sum to
    ``population.fail``. That is the point — the question they answer is "which check is failing"
    and not "how many runs failed".

    Gating membership is per verdict (gating is per workload class), and the names come from the
    verdict's RESULTS intersected with its gating set — the same set ``evaluate.gate`` folds, so
    the two cannot disagree about what gated a run.
    """
    failed: dict = {}
    unmeasured: dict = {}
    for verdict in verdicts:
        gating = set(verdict.gating or ())
        for result in verdict.evaluator_results or ():
            if result.name not in gating:
                continue
            failed.setdefault(result.name, 0)
            unmeasured.setdefault(result.name, 0)
            if result.outcome is Outcome.FAIL:
                failed[result.name] += 1
            elif result.outcome is Outcome.NO_DATA:
                unmeasured[result.name] += 1
    return {"failedGating": {k: failed[k] for k in sorted(failed)},
            "unmeasured": {k: unmeasured[k] for k in sorted(unmeasured)}}


def tool_name_sequence(trajectory: Any) -> list[str]:
    """Ordered tool names from a trajectory, prefixes stripped so evaluator
    name-predicates match. NO_DATA/empty -> []."""
    if trajectory is NO_DATA or not trajectory:
        return []
    names: list[str] = []
    for step in trajectory:
        name = step.name if isinstance(step, Step) else str(step)
        if name.startswith("execute_tool "):
            name = name[len("execute_tool "):]
        elif name.startswith("mcp tools/call "):
            name = name[len("mcp tools/call "):]
        if "___" in name:
            name = name.rsplit("___", 1)[-1]
        names.append(name)
    return names
