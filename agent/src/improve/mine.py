"""MINE — turn scored runs into discriminative failure signatures, or into residue.

The method is per-failure attribution with a discriminative gate, not clustering. Clustering
a coarse boolean needs volume we do not have; refining one failure's shape until no passing
run matches it does not. Measured on the 6-session golden set: ``has_failed_node`` alone
matches a PASSING run (6309f154 passes carrying a failed node), so REQ-018 rejects it — and
that rejection is the whole reason this stage exists.

Algorithm, per failing run:
  1. seed the shape with nothing; every passing run matches
  2. greedily add the SHAPE facet that conclusively excludes the most passing runs
  3. admit at zero matches IF enough controls were conclusively excluded; else residue

Three properties the gate needs in order to mean anything, each of which was absent in the
first version and is enforced here:

**Controls must be observable.** A passing run whose plan was never captured has every
plan-derived facet False, so *any* facet "excludes" it for free. Counting those would let
missing data masquerade as discrimination — the can-show-RED rule applied to mining. Such a
run is INCONCLUSIVE, not excluded, and is named in the signature.

**Enough controls must survive.** ``pass_hits == 0`` against a single control is nearly
free; against four it is a claim. ``MIN_CONTROLS`` is the floor, and the count is recorded
on the signature so a reader can weigh it.

**A shape facet must not be a subterm of a gating evaluator.** ``DagCompletionValidator``
FAILs iff ``has_failed_node ∨ has_non_terminal_node ∨ edge_order_violation`` — computed by
the same code the facets read. Gate on that evaluator and those three facets entail their
own symptom, so ``pass_hits == 0`` becomes free and 1-admitted/1-residue silently becomes
4-admitted/0-residue. ``_SUBTERM_OF`` records the entailment and such facets are refused
while their evaluator gates.

**A failure under a DEGRADED platform is not the agent's failure.** From the facets alone an
AWS outage underneath the agent looks identical to an agent defect, so mining one would turn
an incident into a confident recommendation to change harness code. Before ANY pattern
matching, a FAILING run whose platform was ``InfraState.DEGRADED`` over its window is diverted
to a fourth bucket, ``environment`` — never as a facet the miner reasons with — and routed to
ops / incident review rather than the proposer. Two asymmetries are deliberate and
load-bearing: (1) ONLY ``DEGRADED`` diverts — ``UNKNOWN`` keeps looking, the opposite polarity
from the sentinels elsewhere, because gating on ``UNKNOWN`` would silently empty the mining
population the moment alarm history expires (~2 weeks). (2) Only FAILING runs are gated;
degraded PASSING runs stay controls (a shape that did not force failure even under adversity
is a STRONGER control, and removing controls would perturb the central quality gate). The
platform signal is account-wide ambient correlation used only to STAND DOWN from attribution,
never to blame a specific run. Its inverse risk — a HEALTHY verdict resting on a DERIVED
(hence possibly too-narrow) window that missed an alarm just outside it — is not gated but is
recorded as a caveat on the resulting signature.

This is distinct from ``evidence_gaps``: an evidence gap means "we could not SEE the run"
(observability defect → defect register); an environment divert means "we saw it fine, the
platform was sick" (ops / incident review). They route to different owners, so they are
different buckets.
"""
from __future__ import annotations

from typing import Optional

try:
    from .contract import (
        UNKNOWN, FacetKind, FailureSignature, InfraState, Outcome, Overall, ResidueEntry, Run,
        RunWindow, Verdict, infra_state_of, pluralise,
    )
    from .signature import HEURISTIC_FACETS, facets_for
except ImportError:  # pragma: no cover
    from improve.contract import (  # type: ignore
        UNKNOWN, FacetKind, FailureSignature, InfraState, Outcome, Overall, ResidueEntry, Run,
        RunWindow, Verdict, infra_state_of, pluralise,
    )
    from improve.signature import HEURISTIC_FACETS, facets_for  # type: ignore


# Facets that describe missing evidence rather than a behaviour worth changing. A signature
# built on these says "we could not see the run", which routes to the defect register (an
# observability gap), not to a proposal about the agent's behaviour.
_EVIDENCE_GAP_FACETS = frozenset({"plan_absent", "trajectory_absent", "plan_edges_unreadable"})

# Of those, the ones that are a gap only when a plan was actually EXPECTED.
#
# `trajectory_absent` and `plan_edges_unreadable` are unconditional: no spans at all means the
# run could not be seen by anyone, and an offloaded-and-unread node list means the plan existed
# and part of it was lost. But `plan_absent` is unconditionally TRUE for every class that has
# no DAG dimension, so treating it as a gap on its own filed a real S3 failure in a Lab
# control-plane session as an observability defect and lost the error. It needs corroboration
# that something expected a plan.
_CONDITIONAL_GAP_FACETS = frozenset({"plan_absent"})

# shape facet -> code evaluators whose FAIL condition it is a definitional subterm of.
# See the module docstring; `evaluators_improve.dag_completion` is the source.
#
# The two async-lifecycle entries are a STRICT entailment, and that is why they must stay.
# `async_job_flow` and both facets read the same `evaluators_improve.pair_dispatches` rule, so
# `check_failed:AsyncJobFlowValidator` is a pure function of them: over 541 sessions of a 336 h
# harvest it is identical to `session_ended_on_unretrieved_dispatch`, and if the evaluator's
# undecided ambiguous case is ever assigned FAIL or NO_DATA it becomes exactly
# `session_ended_on_unretrieved_dispatch OR dispatch_unpaired_result_unobserved`. Either way each
# facet entails its own symptom, so `pass_hits == 0` would be free.
#
# The refusal is INERT today, because `classify.GATING_BY_CLASS` gates no class on this evaluator.
# Make it gate and the refusal activates, mining loses both facets, and behaviour changes — decide
# that deliberately rather than discover it.
_SUBTERM_OF: dict = {
    "has_failed_node": ("DagCompletionValidator",),
    "has_non_terminal_node": ("DagCompletionValidator",),
    "edge_order_violation": ("DagCompletionValidator",),
    "fanout_parent_ran_early": ("DagCompletionValidator",),
    "dispatch_unpaired_result_unobserved": ("AsyncJobFlowValidator",),
    "session_ended_on_unretrieved_dispatch": ("AsyncJobFlowValidator",),
}

# Fewest conclusively-excluded passing runs for an admitted signature. Two is the smallest
# number that is not "it differed from the one other run we had".
MIN_CONTROLS = 2

# Tie-break order when several facets satisfy the gate equally well: prefer the one that
# describes something going WRONG over the topology that merely made it possible.
#
# This does not weaken REQ-018 — the gate is applied first, and every facet that ties is
# recorded in `equally_discriminative` so the reader can see the choice was ours and not the
# data's. It decides only which of several equally-discriminative explanations to report,
# and it picks the more specific (hence more refutable) one. Without it, greedy fell back to
# alphabetical order and reported `has_fanout_node` — "one step's output feeds several later
# steps" — as the cause of a delivery failure, which describes a normal DAG, not a defect.
# Anything unlisted sorts last, so a new facet is treated as structural until classified.
#
# The two async-lifecycle facets sit at different heights on purpose.
# `session_ended_on_unretrieved_dispatch` ASSERTS a defect, so it ranks with the other
# symptoms, where the retired `dispatch_without_retrieve` used to sit.
# `dispatch_unpaired_result_unobserved` asserts nothing — it records that an unpaired dispatch may
# equally have returned its result inline — so it must not out-rank any facet that names a
# real fault, and ranks below the definite symptoms but above plan topology.
_FACET_PRECEDENCE = (
    "fanout_parent_ran_early",
    "edge_order_violation",
    "session_ended_on_unretrieved_dispatch",
    "terminal_node_without_gateway_status",
    "gateway_reported_failure",
    "has_failed_node",
    "has_non_terminal_node",
    "dispatch_unpaired_result_unobserved",
    "has_fanout_node",
    "deep_plan",
    "chained_plan",
    "wide_plan",
    "flat_plan",
    "multi_node_plan",
    "all_task_nodes_terminal",
    "async_dispatched",
)


def _precedence(name: str) -> int:
    try:
        return _FACET_PRECEDENCE.index(name)
    except ValueError:
        return len(_FACET_PRECEDENCE)


class MineResult:
    """Admitted signatures plus everything that failed the gate, and why."""

    def __init__(self, signatures: list[FailureSignature], residue: list[ResidueEntry],
                 evidence_gaps: list[ResidueEntry],
                 environment: Optional[list[ResidueEntry]] = None) -> None:
        self.signatures = signatures
        self.residue = residue
        self.evidence_gaps = evidence_gaps
        # Failures diverted because the PLATFORM was degraded over their window. A distinct
        # bucket, not a reuse of evidence_gaps: it routes to ops / incident review, not to
        # the defect register.
        self.environment = environment if environment is not None else []

    def summary(self) -> dict:
        return {
            "admitted": len(self.signatures),
            "residue": len(self.residue),
            "evidence_gaps": len(self.evidence_gaps),
            "environment": len(self.environment),
        }


def mine(runs: list[Run], verdicts: list[Verdict], max_conjuncts: int = 4,
         gating_evaluators: tuple = (), min_controls: int = MIN_CONTROLS) -> MineResult:
    """Mine signatures from a scored population.

    ``runs`` and ``verdicts`` are matched on ``run_id``; a verdict without a run (or the
    reverse) is skipped rather than guessed at. ``gating_evaluators`` is the set the verdict
    was gated on — needed to refuse self-satisfying shape facets (see ``_SUBTERM_OF``).
    """
    by_run = {r.run_id: r for r in runs}
    paired = [(by_run[v.run_id], v) for v in verdicts if v.run_id in by_run]
    facets = {run.run_id: facets_for(run, verdict) for run, verdict in paired}
    verdict_by_run = {v.run_id: v for _, v in paired}

    failing = [v.run_id for _, v in paired if v.overall is Overall.FAIL]
    passing = [v.run_id for _, v in paired if v.overall is Overall.PASS]

    def _controls_for(run_id: str) -> list:
        """Passing runs that are legitimate negative controls for THIS failure.

        Two restrictions, both of which were missing and both of which inflated the central
        REQ-018 gate:

        **Same class only.** Each class is gated on a different objective, so a passing
        `tool_discovery` run was never asked whether the agent achieved a goal. Counting it as
        a run that "did not exhibit this shape" is counting a run that was not examined.
        Measured: admitting the 29 non-conversational passes raised `controls_excluded` for
        the `e2cb300b` signature from 4 to 12, which is the number a reader weighs the claim
        by.

        **One control per parent request.** Sibling `plan-session-*` sub-sessions of one plan
        share a user request, so they are one piece of evidence. `population.py` records this
        hazard and `classify` supplies the `parent_hint` for it.

        This restriction spent its first months doing NOTHING, and the way it failed is worth
        keeping: the hint was the sub-session id's own suffix, which is unique per row, so every
        sibling arrived with a different key and there was never anything to collapse. It reads as
        a working guard at every level — the field is populated, the set is consulted, the loop
        skips duplicates — and only a count of distinct values over real ids shows it is a no-op.
        The hint is now the parent's session id, resolved from the OTEL trace
        (`observe.span_trajectory.parents_by_session`). Measured over 14 days of staging traffic,
        141 sub-sessions collapse to 70 parents, 26 of which fanned out and one into 6 — so a
        `plan_node` control count can now legitimately halve, and where it does, the old number
        was counting one request several times.

        An UNRECORDED class on either side never excludes. A caller that builds verdicts by
        hand leaves the field empty, and filtering on it would drop every control and turn
        every signature into residue, silently. Excluding on an absent field is the same
        missing-data-as-decision error the inconclusive-controls rule exists to prevent, so
        the restriction applies only when both classes are known.
        """
        target = verdict_by_run.get(run_id)
        target_class = getattr(target, "workload_class", "") or ""
        seen_parents: set = set()
        controls = []
        for candidate in passing:
            verdict = verdict_by_run.get(candidate)
            candidate_class = getattr(verdict, "workload_class", "") or ""
            if target_class and candidate_class and candidate_class != target_class:
                continue
            # A run with no recorded parent stands alone, so it keys on its own id.
            parent = getattr(verdict, "workload_parent", "") or candidate
            if parent in seen_parents:
                continue
            seen_parents.add(parent)
            controls.append(candidate)
        return controls
    # UNSCORED runs are in neither bucket on purpose: they are not evidence of failure, and
    # using them as negative controls would let an unscorable run vouch for a pattern. They
    # surface as evidence gaps instead.
    unscored = [v.run_id for _, v in paired if v.overall is Overall.UNSCORED]

    # Gating is per-class, so the population-wide tuple no longer describes every verdict.
    # Each verdict carries the set that actually gated it; the argument stays as the fallback
    # for a caller that builds verdicts by hand.
    def _banned_for(run_id: str) -> set:
        verdict = verdict_by_run.get(run_id)
        active = set(getattr(verdict, "gating", ()) or gating_evaluators)
        return {name for name, evaluators in _SUBTERM_OF.items()
                if any(e in active for e in evaluators)}

    signatures: list[FailureSignature] = []
    residue: list[ResidueEntry] = []
    evidence_gaps: list[ResidueEntry] = []
    environment: list[ResidueEntry] = []

    for run_id in failing:
        banned = _banned_for(run_id)
        symptom = tuple(sorted(
            name for name, f in facets[run_id].items()
            if f.kind is FacetKind.SYMPTOM and f.value))

        # Environment gate — decided BEFORE any pattern matching. If the platform was
        # DEGRADED over this run's window, stand down from attribution entirely: an
        # account-wide outage is indistinguishable from an agent defect at the facet level,
        # and mining it would recommend a harness change for an incident. ONLY DEGRADED
        # diverts; UNKNOWN (and HEALTHY) proceed to mining as before — gating on UNKNOWN
        # would empty the population the moment alarm history expires. The signal is ambient
        # correlation used to stand down, never per-run blame, so this never touches a score.
        if infra_state_of(by_run[run_id]) is InfraState.DEGRADED:
            environment.append(_environment_entry(by_run[run_id], symptom))
            continue

        # Evidence-gap facets are SHAPE and observed (their whole job is to report that
        # something else was NOT observed), so they must be excluded explicitly or they
        # become candidate predictors and a fully-blind run never reaches the gap branch.
        candidates = [name for name, f in facets[run_id].items()
                      if f.kind is FacetKind.SHAPE and f.value and f.observed
                      and name not in banned and name not in _EVIDENCE_GAP_FACETS]

        gap = sorted(name for name, f in facets[run_id].items()
                     if name in _EVIDENCE_GAP_FACETS and f.value
                     and (name not in _CONDITIONAL_GAP_FACETS
                          or _expected_missing_evidence(verdict_by_run.get(run_id))))
        if gap and not candidates:
            evidence_gaps.append(ResidueEntry(
                run_id=run_id, symptom=symptom, best_shape=tuple(gap),
                pass_hits=-1, pass_hit_runs=(),
                reason=("evidence gap, not a behaviour pattern: "
                        + "; ".join(f"{g} ({facets[run_id][g].evidence})" for g in gap)
                        + " — route to the defect register, not the proposer"),
            ))
            continue

        controls = _controls_for(run_id)
        outcome = _refine(run_id, symptom, candidates, facets, controls, max_conjuncts,
                          min_controls, gap, banned)
        # Say which pool the gate was measured against. `controls_excluded` is the number a
        # reader weighs the claim by, so where it came from cannot be left implicit.
        target_class = getattr(verdict_by_run.get(run_id), "workload_class", "") or ""
        scoped = ("scoped to the same workload class" if target_class
                  else "NOT class-scoped: this run has no recorded workload class")
        outcome.refinement_log.append(
            f"controls: {len(controls)} of {len(passing)} passing run(s) in the population, "
            f"{scoped} ({target_class or 'unrecorded'}) and de-duplicated by parent request. "
            "A class gated on a different objective was never asked this question, so it "
            "cannot vouch for this shape")
        # This run was NOT gated, but if its HEALTHY verdict rests on a derived (possibly
        # too-narrow) window, an alarm firing just outside it could have been missed — a
        # MISS, not a false alarm. Record that caveat on the outcome via the same
        # refinement_log mechanism established caveats use; do not gate.
        caveat = _derived_window_health_caveat(by_run[run_id])
        if caveat:
            outcome.refinement_log.append(caveat)
        if isinstance(outcome, FailureSignature):
            signatures.append(outcome)
        else:
            residue.append(outcome)

    for run_id in unscored:
        verdict = verdict_by_run.get(run_id)
        workload = getattr(verdict, "workload_class", "") or "unknown"
        detail = getattr(verdict, "reason", "") or "no gating evaluator produced an outcome"
        evidence_gaps.append(ResidueEntry(
            run_id=run_id, symptom=(), best_shape=(), pass_hits=-1, pass_hit_runs=(),
            # Naming the class matters: an UNSCORED `unclassified` run is a hole in the
            # classifier, while an UNSCORED `conversational` one is a hole in the evidence.
            # They route to different owners, and a single generic reason hid that.
            reason=(f"UNSCORED ({workload} invocation): {detail}. Not counted as a failure "
                    "and not used as a negative control"),
        ))

    return MineResult(signatures, residue, evidence_gaps, environment)


def _expected_missing_evidence(verdict) -> bool:
    """Did some evaluator report NO_DATA, i.e. record that evidence it needed was absent?

    The discriminator between "we could not see this run" (an observability defect, which
    routes to the defect register) and "this kind of evidence does not apply here". Only the
    first is an evidence gap. Without the check, every failure in a class that has no plan
    became a `plan_absent` gap, because the facet is unconditionally true there.
    """
    results = getattr(verdict, "evaluator_results", None) or ()
    return any(r.outcome is Outcome.NO_DATA for r in results)


def _environment_entry(run: Run, symptom: tuple) -> ResidueEntry:
    """The record for a failure diverted because the platform was DEGRADED.

    Reuses ``ResidueEntry`` for shape, but its ``reason`` names ops / incident review as the
    owner and says explicitly that the platform state is ambient correlation, never per-run
    blame — so it can never be mistaken for the evidence-gap bucket (which routes to the
    defect register) or for an attribution.
    """
    health = getattr(run, "infra_health", UNKNOWN)
    alarms = getattr(health, "alarms", ()) or ()
    reason_text = getattr(health, "reason", "") or "alarms firing account-wide"
    source = getattr(health, "source", "") or ""
    return ResidueEntry(
        run_id=run.run_id, symptom=symptom, best_shape=(),
        pass_hits=-1, pass_hit_runs=(),
        reason=(f"platform was DEGRADED during this run's window: {reason_text}"
                + (f"; alarms in ALARM: {', '.join(alarms)}" if alarms
                   else "; no alarm names recorded")
                + (f" (source: {source})" if source else "")
                + " — this is NOT the agent. Attribution stood down; route to ops / incident "
                  "review, not the proposer and not the defect register. Alarm state is "
                  "account-wide ambient correlation and can never blame a specific run."),
    )


def _derived_window_health_caveat(run: Run) -> Optional[str]:
    """A caveat for a NON-gated run whose HEALTHY verdict rests on a DERIVED window.

    The inverse risk of the gate. A derived window may be NARROWER than the true interval
    (spans are the first thing to vanish when the platform breaks), so an alarm just outside
    it is missed and the run reads HEALTHY and proceeds to mining when perhaps it should have
    been gated. That is a MISS, not a false alarm — the run is NOT gated, only annotated, and
    the caveat rides the established ``refinement_log`` mechanism. UNKNOWN health carries no
    such risk (nobody claimed the platform was healthy), so it gets no caveat.
    """
    window = getattr(run, "window", UNKNOWN)
    if (infra_state_of(run) is InfraState.HEALTHY
            and isinstance(window, RunWindow) and window.derived()):
        return ("caveat: the platform-HEALTHY check rests on a DERIVED run window "
                f"({window.provenance}), which may be narrower than the true interval — an "
                "alarm firing just outside it would be missed, so HEALTHY here is a possible "
                "narrow-window miss, not a stamped all-clear")
    return None


def _classify(run_id: str, shape: tuple, facets: dict) -> str:
    """How this passing run bears on the shape: 'matches' | 'excluded' | 'inconclusive'.

    'matches' is bad (the shape fires on a passing run). 'excluded' is the only outcome that
    counts as a control the shape survived. 'inconclusive' means at least one facet in the
    shape could not be observed for this run, so its non-match proves nothing.
    """
    run_facets = facets.get(run_id) or {}
    unobserved = False
    for name in shape:
        facet = run_facets.get(name)
        if facet is None or not facet.observed:
            unobserved = True
            continue
        if not facet.value:
            # Conclusively different on an observed facet — a genuine control.
            return "excluded"
    return "inconclusive" if unobserved else "matches"


def _refine(run_id: str, symptom: tuple, candidates: list[str], facets: dict,
            passing: list[str], max_conjuncts: int, min_controls: int,
            gap: list[str], banned: set):
    """Greedily add shape facets until no passing run matches, or give up."""
    shape: tuple = ()
    remaining = sorted(candidates)
    log: list[str] = []
    matching = list(passing)
    inconclusive: list[str] = []
    log.append(f"seed: {len(matching)} passing run(s) to exclude "
               f"({[h[:8] for h in matching]})")
    if banned:
        log.append(f"refused as subterms of a gating evaluator: {sorted(banned)}")

    while matching and len(shape) < max_conjuncts and remaining:
        scored = []
        for name in remaining:
            trial = shape + (name,)
            buckets = {h: _classify(h, trial, facets) for h in matching}
            still = [h for h, verdict in buckets.items() if verdict == "matches"]
            unknown = [h for h, verdict in buckets.items() if verdict == "inconclusive"]
            # Fewest still-matching wins; then fewest inconclusive (a facet that leaves
            # controls unjudgeable is weaker evidence than one that decides them); then the
            # more specific facet; then the name, so the result is deterministic.
            scored.append((len(still), len(unknown), _precedence(name), name, still, unknown))
        scored.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
        best_count, _, _, best_name, best_still, best_unknown = scored[0]
        if best_count == len(matching):
            log.append("no remaining facet excludes any passing run; best was "
                       f"{best_name!r} (still {best_count})")
            break
        # Record every facet that did equally well, so a tie is visible rather than hidden
        # behind the precedence order.
        ties = [row[3] for row in scored if row[0] == best_count and row[3] != best_name]
        shape = shape + (best_name,)
        remaining.remove(best_name)
        evidence = facets[run_id][best_name].evidence
        log.append(f"+{best_name} -> still-matching {len(matching)} => {best_count}"
                   + (f", inconclusive {len(best_unknown)}" if best_unknown else "")
                   + (f"  [{evidence}]" if evidence else ""))
        if ties:
            log.append(f"  tie: {ties} were equally discriminative; reported facet chosen "
                       f"by precedence, not by evidence")
        matching = best_still
        # ACCUMULATED, never replaced. `buckets` is computed over the runs still matching, so a
        # control that went inconclusive in round 1 is absent from round 2's `matching` and can
        # never reappear in round 2's `unknown` — overwriting therefore forgot it, and `excluded`
        # below (which is "everything not still matching and not inconclusive") then counted it as
        # a control the shape conclusively excluded. That is precisely the missing-data-as-
        # discrimination the module docstring says this rule exists to prevent, and it inflated the
        # one number a reader weighs the claim by.
        inconclusive = inconclusive + [h for h in best_unknown if h not in inconclusive]

    excluded = [h for h in passing
                if h not in matching and h not in inconclusive]

    if matching:
        return ResidueEntry(
            run_id=run_id, symptom=symptom, best_shape=shape,
            # WHICH runs still match belongs in `pass_hit_runs` and only there. The reason used to
            # spell the same ids out again, which made the sentence a second copy of the field
            # beside it — and a copy that travels differently: a consumer can withhold a structured
            # id list where its audience must not see it, and cannot withhold ids baked into prose.
            # `mine_local._print_report` reads the field.
            pass_hits=len(matching), pass_hit_runs=tuple(matching),
            # `reason` is rendered verbatim on both Improve surfaces — the Inbox falls back to it
            # because `residue_to_wire` refuses to invent a `plainReason` — so the noun has to
            # agree here. "the runs" below is unaffected: it means this failure AND the passing
            # ones, which is two or more whatever `matching` holds.
            reason=("REQ-018: no shape conjunction (<= "
                    f"{max_conjuncts} facets) separates this failure from "
                    f"{len(matching)} passing {pluralise(len(matching), 'run')}"
                    + " — the runs are structurally alike, so shape alone cannot explain "
                      "the difference. Needs a semantic facet or more examples."),
            refinement_log=log,
        )

    if not shape:
        # Nothing to exclude, so the gate never ran. Admitting an unconstrained signature
        # here is the vacuity REQ-018 exists to prevent.
        return ResidueEntry(
            run_id=run_id, symptom=symptom, best_shape=(), pass_hits=0, pass_hit_runs=(),
            reason=("no passing runs in the population, so REQ-018's discriminative gate "
                    "could not be applied — a signature admitted here would be untested"),
            refinement_log=log,
        )

    if len(excluded) < min_controls:
        return ResidueEntry(
            run_id=run_id, symptom=symptom, best_shape=shape, pass_hits=0,
            pass_hit_runs=(),
            # The COUNT of unjudgeable controls, not their ids. An inconclusive control is another
            # run in the population, so naming it here is the same exposure as naming a pass hit —
            # and the count is what a reader weighs the claim by. The ids are not carried elsewhere
            # either: unlike `pass_hit_runs` there is no field for them, and adding one would put
            # a second tenant's session ids on a record whose only safe home is a count.
            reason=(f"shape excluded only {len(excluded)} passing "
                    f"{pluralise(len(excluded), 'run')} conclusively "
                    f"(minimum {min_controls})"
                    + (f"; {len(inconclusive)} could not be judged because their own "
                       f"evidence was missing" if inconclusive else "")
                    + " — pass_hits is 0 but the gate had almost nothing to test against"),
            refinement_log=log,
        )

    equally = _ties_from_log(log)
    return FailureSignature(
        run_id=run_id, symptom=symptom, shape=shape,
        pass_hits=0, pass_hit_runs=(), refinement_log=log,
        evidence={name: facets[run_id][name].evidence for name in shape},
        heuristic_facets=tuple(sorted(set(shape) & HEURISTIC_FACETS)),
        controls_excluded=len(excluded),
        controls_inconclusive=tuple(inconclusive),
        # The pool the two counts above are OUT OF, recorded here because this is the only place
        # that holds it: `_controls_for` narrows the population's passing runs by class and by
        # parent request before handing them over, so neither the caller nor any consumer can
        # re-derive it from a population count.
        controls_considered=len(passing),
        equally_discriminative=equally,
    )


def _ties_from_log(log: list[str]) -> tuple:
    """Facet names recorded as equally discriminative during refinement."""
    out: list[str] = []
    for line in log:
        marker = "  tie: ["
        if line.startswith(marker):
            body = line[len(marker):].split("]")[0]
            out.extend(name.strip().strip("'\"") for name in body.split(",") if name.strip())
    return tuple(dict.fromkeys(out))


def group_signatures(signatures: list[FailureSignature]) -> list[dict]:
    """Collapse signatures sharing the same (shape, symptom) into one pattern.

    Two runs failing the same way for the same structural reason are one pattern with two
    members, not two patterns — and member count is the honest measure of how much evidence
    a proposal rests on.

    Evidence is kept PER MEMBER, in ``evidence_by_run``, so the pattern itself no longer describes
    one run's detail under a caption reading "from 5 runs". Both of its consumers then re-select
    ``members[0]`` anyway — ``propose_all`` to build the ``FailureSignature`` a Proposal carries,
    ``dto._pattern_evidence`` to fill ``rootCauseHypo`` — so what this fixed is the AVAILABILITY of
    the rest, not the choice either of them makes. The per-actor bundle is where that matters: it
    intersects the member list with the reader's own runs first, so the sentence a reader sees
    describes a session they own rather than whichever member happened to be first.

    THE GATE TRIPLE MOVES TOGETHER, from the WEAKEST member. ``controls_excluded``,
    ``controls_inconclusive`` and ``controls_considered`` only reconcile within one run — the sum
    ``excluded + inconclusive == considered`` is a fact about a single refinement, not about a set
    of them — so taking a min of one and a max of another would publish three numbers that describe
    no run in the population and do not add up. The weakest member is the one with the fewest
    conclusively-excluded controls, which keeps the previous rule ("a pattern is never stronger than
    its least-tested member") and makes the other two agree with it. Ties go to the first member
    seen, so the result is deterministic.

    ``equally_discriminative`` stays a UNION, deliberately, because it is a different kind of fact:
    it says the data failed to distinguish this explanation from another, and that is a defect of
    the pattern if it happened for ANY member.
    """
    grouped: dict = {}
    for sig in signatures:
        key = (sig.shape, sig.symptom)
        entry = grouped.setdefault(key, {
            "shape": sig.shape, "symptom": sig.symptom, "member_run_ids": [],
            "evidence_by_run": {}, "heuristic_facets": (), "pass_hits": 0,
            "controls_excluded": 0, "controls_inconclusive": (), "controls_considered": 0,
            "equally_discriminative": (),
        })
        first = not entry["member_run_ids"]
        entry["member_run_ids"].append(sig.run_id)
        entry["evidence_by_run"][sig.run_id] = sig.evidence
        # Union, so a pattern is marked heuristic if ANY member rests on a heuristic facet.
        entry["heuristic_facets"] = tuple(sorted(
            set(entry["heuristic_facets"]) | set(sig.heuristic_facets)))
        entry["equally_discriminative"] = tuple(sorted(
            set(entry["equally_discriminative"]) | set(sig.equally_discriminative)))
        if first or sig.controls_excluded < entry["controls_excluded"]:
            entry["controls_excluded"] = sig.controls_excluded
            entry["controls_inconclusive"] = sig.controls_inconclusive
            entry["controls_considered"] = sig.controls_considered
    return sorted(grouped.values(), key=lambda e: (-len(e["member_run_ids"]), e["shape"]))
