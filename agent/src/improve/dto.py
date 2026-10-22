"""Map loop domain objects onto the wire shape the Improve UI reads.

The mapping lives here, once, on the PYTHON side. `ui/src/improve-api.ts` describes the wire
and is not a second definition of the domain, so a field is renamed in exactly one place. The
same module serves a static file today, an Express route next, and the Capability API after
that; only the transport changes.

Three things the loop does not produce, and how each is handled rather than faked:

``status``
    `open | accepted | dismissed | superseded` is STATE, and the pipeline is stateless: it
    recomputes from a harvest every run. Everything is emitted `open`, and a dismissal cannot
    survive the next run until something stores it. `proposal_id` is already a deterministic
    hash of (component, shape), so the identity a store would key on exists; only the store
    does not.

``affordances``
    Derived from the registry alone. Whether a Lab working copy exists, which is what
    `canTryBoth` and `forkNeeded` really turn on, needs a live Lab query this process does not
    make. Those flags are therefore FALSE with a stated reason, so the card withholds an action
    rather than offering a Try-both that would fail.

``evidenceQuality``
    Genuinely computable, and computed here: the member runs carry a window and an
    `InfraHealth`, so run count, worst platform state and the derived-window count are real.

``scorecard``
    Population-level aggregates, added because with ``admitted: 0`` this bundle carried zero
    facts about zero runs: no pass rate, no tier scores, no spend, no sense of why anything
    failed, so the surface could say nothing at all while the producer computed all of it and
    dropped it here. FIXED size, so it never participates in bundle trimming. It answers "why are
    runs failing" by EVALUATOR NAME (``outcomes``) — the structural half of the verdict, which is
    a harness identifier — and never with the judge's prose, per the content rule below. It also
    answers "was anything under this population BROKEN" (``platform``), which is the question
    ``admitted: 0`` most often turns out to mean: measured on the live item, 5 of 5 failures were
    diverted for platform reasons and no pattern was admitted at all.

TWO ITEMS, and which one a field belongs to is a security boundary
-----------------------------------------------------------------

``to_wire`` builds the **GLOBAL** bundle, one DynamoDB item at ``actorId="__system__"``, read by
every actor. ``actor_bundles`` builds one bundle **per actor**, each written to that actor's own
partition and served only to the holder of that actor's token. The content rule follows from
which item a value lands in, and it is not a style preference:

    GLOBAL       numbers, counts, rates, enum values, timestamps and facet / evaluator /
                 component / CLOUDWATCH ALARM names. Nothing derived from user input or model prose
                 — no goal, no response, no turn text, no tool argument, **no judge
                 ``EvaluatorResult.explanation``** (a sentence about what the user asked for, by
                 construction), **no session id and no actor id**. A list of session ids is
                 per-row exposure even with no text attached, and a list of actor ids tells every
                 tenant who else is on the harness. An alarm name is on the far side of that line
                 rather than a borderline case: CloudWatch alarms are THIS DEPLOY'S OWN
                 infrastructure, named by CDK over its queues, Lambdas and sweepers, and no tenant
                 can create or name one. Ambient account state, like the deploy sha.

    PER-ACTOR    the same actor's own evidence, and only theirs: their run rows with the judge's
                 reason, the facet evidence behind the suggestions their runs support, their
                 waste findings with the tool sequence named, their session ids. This is the
                 first place model prose travels on this surface, and the key IS the guard — the
                 reader's actor comes from their verified token and never from a request body.

So the global ``scorecard`` is aggregates, a global ``patterns`` row carries a shape and a count
where it used to carry run ids, and ``rootCauseHypo`` (facet evidence prose: plan node ids,
span indices, timestamps) is emitted per actor rather than globally. The paired projections sit
next to each other — ``_pattern_to_wire`` / ``_pattern_evidence`` and ``residue_to_wire`` /
``_residue_evidence`` — so the split of one record's fields across the two items is decided in
one place and cannot drift.

Only ``conversational`` sessions can have an owner, so only they produce per-actor evidence. The
other three classes the classifier emits have no user conversation at all by definition
(``tool_discovery`` is session warm-up, ``plan_node`` is a plan's own sub-session,
``control_plane`` is a Lab operation), which is the same reason ``population.py`` gives for not
running the goal judge on them. They still count in the global ``scorecard``, because they are
real population.

``test_improve_dto.test_the_global_scorecard_contains_no_user_derived_text`` and its siblings are
the guards on the first column; ``test_an_actor_bundle_carries_no_other_actors_evidence`` is the
guard on the second.

``scorecard.spend`` carries REAL tokens and a priced cost, and the difference between them is
which denominators each needs. ``harvest_live.harvest_token_usage`` measures each session's tokens
from its ``chat`` spans — per category (uncached input / output / cache read / cache write) and per
model — and ``build_run`` reads them back, so a laptop and the Lambda report the same number.
``cost`` is then an ESTIMATE from a hand-entered per-model Bedrock rate table
(``build_run._BEDROCK_RATES_USD_PER_MTOK``), never billed truth, and it can be PARTIAL: a model
with no rate row is skipped, so ``cost`` alone carries token counts for what it did and did not
price on top of the run counts every term carries. See ``_spend`` and ``contract
.COST_UNMEASURED_REASON`` for the full argument; a chart drawn from this bundle must say
"unmeasured" for money, never "$0", and must not multiply ``tokens`` by a single rate.
"""
from __future__ import annotations

from typing import Iterable, Optional

try:
    from .classify import WorkloadClass
    from .contract import (
        COST_UNMEASURED_REASON, UNKNOWN, UNMEASURED, InfraHealth, InfraState, Outcome,
        Overall, Proposal, ResidueEntry, Run, RunWindow, Verdict, _Sentinel,
        gating_outcome_counts, pluralise, verdict_counts, verdict_counts_by_class,
    )
    from .propose import headline_for
    from .registry import SigmaRegistry
    from .waste import WASTE_KINDS
except ImportError:  # pragma: no cover
    from improve.classify import WorkloadClass  # type: ignore
    from improve.contract import (  # type: ignore
        COST_UNMEASURED_REASON, UNKNOWN, UNMEASURED, InfraHealth, InfraState, Outcome,
        Overall, Proposal, ResidueEntry, Run, RunWindow, Verdict, _Sentinel,
        gating_outcome_counts, pluralise, verdict_counts, verdict_counts_by_class,
    )
    from improve.propose import headline_for  # type: ignore
    from improve.registry import SigmaRegistry  # type: ignore
    from improve.waste import WASTE_KINDS  # type: ignore


# Worst-first, so a mixed set of runs reports its most compromised state. `unknown` outranks
# `healthy` because it cannot be ruled out, only `degraded` is worse.
#
# ONE definition of the ordering, read by both callers. `_platform_population` folds `InfraState`
# enums and `_worst_platform` compares already-mapped wire dicts, so the two need the same order in
# two spellings — the wire-keyed form is DERIVED below rather than written out again, because it had
# already been written out again and this constant was left unread by anything.
_STATE_SEVERITY = {InfraState.DEGRADED: 2, InfraState.UNKNOWN: 1, InfraState.HEALTHY: 0}
_WIRE_STATE_SEVERITY = {state.value: rank for state, rank in _STATE_SEVERITY.items()}

# Sections are Σ LAYERS, which is what the registry actually records: prompt / memory / tool /
# control / ingress / infra. They are NOT Lab categories (skill, specialty-agent, workflow,
# tool_collection, hook) — two different taxonomies that an earlier version conflated, keying
# sections on Lab categories so Skills, Hooks and Workflows all rendered EMPTY while a catch-all
# "Other" held most of the content. A registry component's layer says where in the harness it
# sits; a Lab category says what kind of asset a user owns. Only the first is in this data.
_USER_SECTIONS = (
    ("prompt", "Prompts and instructions"),
    ("control", "Control logic"),
    ("memory", "Memory and state"),
    ("tool", "Tools"),
)


def _sentinel_or(value, mapper):
    """Wire value for a field that may hold a sentinel. Sentinels serialize as their NAME
    (`"UNKNOWN"`), never null, so the UI can render them hatched instead of blank."""
    if isinstance(value, _Sentinel):
        return repr(value)
    return mapper(value)


def _measured(value):
    """One `Measured<number>` on the wire: the number, or the sentinel's NAME.

    `None` is folded into UNMEASURED deliberately. Every number in this file that could be
    absent comes from a dict the harvest may not have filled (`Run.resources`) or from an
    evaluator that may not have scored (`EvaluatorResult.score is None`), and both must reach
    the UI as `"UNMEASURED"` rather than `null` — `null` styles like any other empty cell and
    `0` reads as "free" or "instant" (invariant 1, `improve-api.ts` rule 1).
    """
    return _sentinel_or(UNMEASURED if value is None else value, lambda v: v)


def _is_number(value) -> bool:
    """A real measured number. `bool` is excluded because `True + True == 2`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _window_span(windows: Iterable[object]) -> tuple:
    """``(wire window over these windows, how many were derived)``.

    Shared by `evidence_quality` and `scorecard_to_wire` so a pattern's interval and the
    population's interval cannot be computed two different ways. UNKNOWN — never a half-built
    window — when no member carried a real one.
    """
    real = [w for w in windows if isinstance(w, RunWindow)]
    derived = sum(1 for w in real if w.derived())
    if not real:
        return "UNKNOWN", 0
    return {
        "start": min(real, key=lambda w: w.start_dt()).start,
        "end": max(real, key=lambda w: w.end_dt()).end,
        "derived": derived > 0,
        "provenance": real[0].provenance if len(real) == 1 else "union_of_member_windows",
    }, derived


def window_to_wire(window) -> object:
    return _sentinel_or(window, lambda w: {
        "start": w.start,
        "end": w.end,
        "derived": w.derived(),
        "provenance": w.provenance,
    })


def platform_to_wire(health) -> dict:
    """Always a PlatformHealth on the wire, even when nobody looked.

    The UNKNOWN sentinel ("no source was wired") and `InfraHealth(state=UNKNOWN)` ("a lookup ran
    and could not tell") are different facts, and the reason string keeps them apart. Neither
    may render as healthy, so both land on `state: "unknown"`.

    ``alarmsIndeterminate`` is the other half of `alarms`, and without it `state` is unreadable:
    `state: "unknown"` with four unplaceable alarms is a lookup that ran and could not decide, while
    `state: "unknown"` with none is a lookup that found nothing to decide about (no coverage, or no
    alarms configured at all). `infra_health_for_window` used to keep only `in_alarm is True`, so the
    indeterminate set reached a reader as prose inside `reason` and only its first three names.
    A COUNT is not enough here because the names are what a reader takes to CloudWatch.

    ``window`` is the interval the alarm correlation actually covered, and it is the fact that makes
    every other field checkable — a HEALTHY resting on a DERIVED window may simply have missed an
    alarm just outside it, which is why `derived` and `provenance` travel with it. It was computed
    per run and dropped here. On a multi-run pattern this is the WORST member's window, not the
    union: `_worst_platform` reports one member's state, so it must report that member's interval
    and not a span the state was never measured over. The pattern's own extent is
    `evidenceQuality.window`, one level up.

    CONTENT: alarm names are ACCOUNT infrastructure — CloudWatch alarms on this deploy's own queues,
    Lambdas and sweepers, named by CDK — so they are harness identifiers and not tenant data. A
    window is two timestamps. Nothing here is derived from user input or model prose, so the whole
    block is safe on the shared item.
    """
    if isinstance(health, _Sentinel):
        return {"state": "unknown", "reason": "no platform-health source was consulted",
                "alarms": [], "alarmsIndeterminate": [], "window": "UNKNOWN", "source": ""}
    return {
        "state": health.state.value,
        "reason": health.reason,
        "alarms": list(health.alarms),
        "alarmsIndeterminate": list(getattr(health, "indeterminate", ()) or ()),
        "window": window_to_wire(getattr(health, "window", UNKNOWN)),
        "source": health.source,
    }


def _worst_platform(healths: Iterable[object]) -> dict:
    """Worst state across member runs, not the most common: the question is whether ANY of the
    evidence is compromised.

    ONE member's whole block is returned, never a merge of several: `alarms`, `reason` and `window`
    only mean anything together, and combining the worst state with another member's alarm list
    would describe a run that does not exist.
    """
    wires = [platform_to_wire(h) for h in healths]
    if not wires:
        return {"state": "unknown", "reason": "no member runs", "alarms": [],
                "alarmsIndeterminate": [], "window": "UNKNOWN", "source": ""}
    # An unrecognised state ranks with `unknown`, never with `healthy`.
    return max(wires, key=lambda w: _WIRE_STATE_SEVERITY.get(
        w["state"], _STATE_SEVERITY[InfraState.UNKNOWN]))


def _evidence_completeness(runs, total: Optional[int] = None) -> dict:
    """How many runs are KNOWN to be missing part of their own trajectory. Counts only.

    THREE numbers, not one, for the reason invariant 1 exists: ``incomplete: 0`` alone is
    indistinguishable from "no harvester recorded completeness for any of these runs", which is the
    state every harvest taken before adaptive windowing is in. ``measuredOf`` is how many runs
    carried a verdict either way, so ``incomplete: 0, measuredOf: 0`` reads as "not checked" rather
    than "all clean".

    ``is False`` / ``isinstance(..., bool)`` rather than truthiness, because UNKNOWN is falsy
    (`_Sentinel.__bool__`) and would otherwise be counted as incomplete.

    No session ids and no note text: this rides the ONE global SNAPSHOT item that every actor
    reads, so the number of affected runs may travel and which runs they were may not. The
    per-actor carrier that could name them DOES exist (`actor_bundles`), and this stays a count
    anyway: the ids would have to be split per owner to travel there, and what a reader needs from
    a POPULATION figure is how much of it to discount, which is the number.
    """
    verdicts = [getattr(run, "evidence_complete", UNKNOWN) for run in runs or ()]
    return {
        "incomplete": sum(1 for v in verdicts if v is False),
        "measuredOf": sum(1 for v in verdicts if isinstance(v, bool)),
        "total": len(verdicts) if total is None else total,
    }


def evidence_quality(member_run_ids: list, runs_by_id: dict) -> dict:
    """How good the evidence behind a pattern is, from the member runs themselves.

    ``caveats`` carries only what is NOT already a structured field beside it. The platform reason
    used to be appended here as well, and `platform.reason` is the same string: measured on the live
    global item that shipped 741 characters twice per suggestion — 1,488 JSON bytes across the two
    suggestions, 5.4% of a 27,605-byte bundle whose suggestion list is the one thing
    `aws_lambda._fit_bundle` trims. `platform.state` is what a consumer branches on and
    `platform.reason` is what it renders; a duplicate in a free-text list can only be rendered twice.
    """
    members = [runs_by_id[rid] for rid in member_run_ids if rid in runs_by_id]
    # The span of all member windows, so a multi-run pattern reports one interval.
    span, derived = _window_span([m.window for m in members])
    platform = _worst_platform([m.infra_health for m in members])

    caveats: list[str] = []
    if len(members) == 1:
        caveats.append("one run, a pattern this thin cannot show a rate, only that it "
                       "happened once")
    if not members:
        caveats.append("no run in the current dataset attributes here")
    # A suggestion resting on a run whose trajectory was partly dropped by the harvest says so on
    # its own card, because the pattern is thinner than its run count implies.
    #
    # Only KNOWN-incomplete fires. `evidence_complete is UNKNOWN` means no harvester recorded
    # either way, which is the pre-adaptive-windowing state of every offline harvest — caveating
    # that here would attach the same sentence to every card in the bundle and teach a reader to
    # skip the field. The unknown population is reported as a NUMBER instead, on
    # `scorecard.evidenceComplete.measuredOf`, where 0-of-N is legible without being noise.
    incomplete = sum(1 for m in members if getattr(m, "evidence_complete", UNKNOWN) is False)
    if incomplete:
        caveats.append(
            # The noun agrees with the DENOMINATOR — `1 of 2 runs`, `1 of 1 run` — because that is
            # what is being counted off. This caveat is rendered verbatim on a suggestion card, so
            # it cannot be repaired downstream.
            f"{incomplete} of {len(members)} {pluralise(len(members), 'run')} behind this "
            f"pattern had part of their "
            f"trajectory dropped by the harvest's Insights row cap, so their trajectory-derived "
            f"observations are marked unobserved rather than False — this pattern rests on less "
            f"evidence than its run count suggests")

    return {
        "runCount": len(members),
        "window": span,
        "derivedWindows": derived,
        "platform": platform,
        "caveats": caveats,
    }


def _affordances(disposition: str, versioned: bool) -> dict:
    """Registry-derived only.

    `canTryBoth`/`forkNeeded` depend on whether a Lab working copy exists, which needs a live
    query this process does not make, so they are False. `canRollback` is False because
    `RollbackAsset` is unwired everywhere (implemented and tested in `_canonical_ops`, absent
    from the dispatch table, the tool list and every route), and a card must not offer a revert
    the platform cannot perform.
    """
    publishable = disposition == "publishable"
    return {
        "canTryBoth": False,
        "tryBothBlockedReason": (
            "locked_today" if disposition == "code_change"
            else "flat_category" if not versioned
            else "no_host_agent"),
        "canPublish": publishable,
        "requiresTrialBeforePublish": publishable,
        "canRollback": False,
        "rollbackToVersion": None,
        "canSendToTeam": not publishable,
        "hostAgentId": None,
        "forkNeeded": False,
    }


def _candidate_to_wire(candidate: dict) -> dict:
    return {
        "candidateId": candidate["candidate_id"],
        "label": candidate["label"],
        "diffText": candidate.get("diff_text") or "",
        "before": candidate.get("before") or "",
        "after": candidate.get("after") or "",
        "claim": candidate.get("claim") or {},
        "risk": candidate.get("risk"),
        "disposition": candidate.get("disposition") or "advisory",
    }


def _shape_prose(facets: Iterable[str]) -> str:
    """How a MINED-BUCKET shape conjunction is SPELLED on the wire, in one place.

    Facet names, joined — not a plain-English rewrite. `propose._FACET_PROSE` exists and is
    what a user-facing summary uses, but it is keyed on the facets the proposer can act on,
    so it has no entry for exactly the ones that reach `patterns.unattributed`; rendering
    through it would silently fall back to the raw name for the bucket that needs it most.
    Shared by `admitted` and `unattributed` so those two buckets cannot spell one shape two ways.

    ``suggestions[].pattern.shape`` is spelled DIFFERENTLY and deliberately does not come through
    here: `suggestion_to_wire` passes the proposal's own `evidence_summary`, which is
    `propose.prose_for` over the same facets plus a run count ("from 3 runs where a step failed").
    A suggestion is read by a media editor and a mined bucket by an admin, so the two audiences get
    two spellings of one shape — `shapeFacets` is the field that is identical across all three.
    """
    return "; ".join(facets)


def _pattern_to_wire(signature: dict, prose: str, member_run_ids: Iterable[str]) -> dict:
    """The GLOBAL half of a mined pattern: what shape it is, what it predicts, and how well it was
    tested.

    Paired with `_pattern_evidence`, which carries the other half to the owning actor. The split
    is here rather than at each call site so a field cannot end up on both items or on neither.

    Takes either a grouped pattern from `mine.group_signatures` or a Proposal's own jsonified
    `FailureSignature`; the seven keys read here are spelled the same on both, which is what lets
    `patterns.admitted[]` and `suggestions[].pattern` be the same shape.

    ``runCount`` replaces the member-id list. The count is what a reader needs — "one run" versus
    "eight" is the difference between an anecdote and a rate — and WHICH runs is a per-row session
    id list on an item every actor reads.

    ``symptom`` is the CONSEQUENT, and without it this row publishes half a hypothesis. A signature
    is a (shape -> symptom) claim (`contract.FailureSignature`), and every OTHER bucket on this wire
    already carries it — `residue`, `evidenceGaps` and `environment` all do, via `residue_to_wire`.
    So the admitted patterns, the ones that produced the suggestions, were the only rows that named
    the topology and not the failure it predicts: two patterns differing only in which gating check
    failed rendered identically. Symptom facets are named after the EVALUATORS that produced them
    (`signature`'s `tier_failed:<tier>` / `check_failed:<check>`), so they are harness identifiers,
    which is the same argument that lets `scorecard.outcomes` name a failing evaluator globally.

    ``controlsExcluded`` / ``controlsInconclusive`` / ``controlsConsidered`` ARE THE GATE, and
    ``passHits`` is meaningless without them. `mine`'s own docstring says `pass_hits == 0` against a
    single control is "nearly free" and against four "a claim", and the live item shipped
    ``passHits: 0`` with no control count at all — so a shape tested against one run and a shape
    tested against twelve rendered identically. Three numbers rather than one because two of them are
    denominators (a count with no denominator is the defect this whole wire is arranged against): the
    pool the gate was offered, how much of that pool it decided, and how much it could not place
    because those runs' own evidence was missing. For an admitted pattern the three close —
    ``excluded + inconclusive == considered`` — so a reader can check them against each other rather
    than taking any one of them on trust.

    COUNTS, never the ids. An inconclusive control is another run in the population and belongs to
    whichever tenant ran it, so its id is safe neither here nor on the failing run's owner's item —
    the identical argument `residue_to_wire` gives for withholding `pass_hit_runs` and keeping only
    `passHits`. There is no field for these ids anywhere on the wire, by design.

    ``equallyDiscriminative`` names the facets that reached ``pass_hits == 0`` equally well, so a
    reader can see the reported shape was chosen by `mine._FACET_PRECEDENCE` and not by the evidence.
    A non-empty value is a caveat on the claim, and facet names are harness literals.

    ``patternId`` and ``shapeFacets`` stay: facet names are literals in `signature.shape_facets`,
    i.e. harness identifiers. The facet EVIDENCE strings are not — they interpolate plan node ids,
    span indices and timestamps out of one tenant's session — so they are absent here and travel
    as `_pattern_evidence`'s `rootCauseHypo` instead.
    """
    shape = list(signature.get("shape") or ())
    return {
        "patternId": shape[0] if shape else "unknown",
        "shape": prose,
        "shapeFacets": shape,
        "symptom": list(signature.get("symptom") or ()),
        "runCount": len(list(member_run_ids)),
        "passHits": int(signature.get("pass_hits") or 0),
        "controlsExcluded": int(signature.get("controls_excluded") or 0),
        "controlsInconclusive": len(signature.get("controls_inconclusive") or ()),
        "controlsConsidered": int(signature.get("controls_considered") or 0),
        "equallyDiscriminative": list(signature.get("equally_discriminative") or ()),
    }


def _pattern_evidence(shape_facets: Iterable[str], member_run_ids: Iterable[str],
                      evidence_by_run: dict) -> dict:
    """The PER-ACTOR half of the same pattern: which of this actor's runs, and what was seen.

    ``rootCauseHypo`` is the facet evidence of the FIRST member run listed, not of all of them —
    the same one-run detail the field has always carried, except that the run is now guaranteed to
    be one the reader owns. ``memberRunIds`` says how many there are, so a reader can see that the
    sentence describes one of several. Falls back to the empty string rather than to the shape
    prose: the global row already carries that prose, and repeating it here would make "no facet
    evidence was recorded" look like an explanation.
    """
    members = list(member_run_ids)
    evidence = (evidence_by_run or {}).get(members[0]) if members else None
    return {
        "shapeFacets": list(shape_facets),
        "memberRunIds": members,
        "rootCauseHypo": "; ".join(str(v) for v in (evidence or {}).values()),
    }


def _patterns_by_shape(result: dict) -> dict:
    """``{(shape, symptom): grouped pattern}`` from the mining result.

    ``mine.group_signatures`` collapses signatures on exactly that pair, so the map is injective
    by construction and one lookup recovers a proposal's full member list and its per-run
    evidence. Both are needed and neither is on the ``Proposal``: ``propose`` builds a
    ``FailureSignature`` whose ``run_id`` is the FIRST member only, so a suggestion resting on
    eight runs reported one. The association is created here rather than recorded upstream because
    nothing upstream needs it — `propose_all` walks the two lists in step and then discards the
    correspondence.
    """
    return {
        (tuple(p.get("shape") or ()), tuple(p.get("symptom") or ())): p
        for p in result.get("patterns") or ()
        if isinstance(p, dict)
    }


def _members_and_evidence(signature: dict, patterns_by_shape: dict) -> tuple:
    """``(member run ids, {run_id: facet evidence})`` for one proposal's signature.

    Degrades to the signature's own single run when the result carries no grouped patterns — the
    shape several tests and every pre-`patterns` artifact are in — so a missing join reports one
    run rather than none.
    """
    key = (tuple(signature.get("shape") or ()), tuple(signature.get("symptom") or ()))
    pattern = patterns_by_shape.get(key) or {}
    members = list(pattern.get("member_run_ids") or ())
    if not members:
        members = [signature["run_id"]] if signature.get("run_id") else []
    evidence_by_run = dict(pattern.get("evidence_by_run") or {})
    if not evidence_by_run and signature.get("run_id"):
        evidence_by_run = {signature["run_id"]: signature.get("evidence") or {}}
    return members, evidence_by_run


def suggestion_to_wire(proposal: dict, *, created_at: str, registry: SigmaRegistry,
                       runs_by_id: dict, judge_config_hash: str, judge_runs: int,
                       evidence_sources: list,
                       patterns_by_shape: Optional[dict] = None) -> dict:
    """One Proposal as a wire Suggestion — the CLAIM, without the evidence detail.

    ``patterns_by_shape`` (from `_patterns_by_shape`) recovers the pattern's full member list.
    Optional so existing callers keep working; without it a suggestion reports the one run its
    signature names, which is what this function did for every suggestion before the join existed.

    ``evidenceHeadline`` is ADDITIVE. `evidenceSummary` keeps its text and its meaning — the full
    facet conjunction with the run count in front of it — and the headline is the short line that
    leads it, `propose.headline_for` over the same shape. OMITTED rather than filled in when the
    shape's leading facet has no headline, which is `waste_to_wire`'s rule for `plainSummary` and
    `_unattributed_to_wire`'s for `plainReason`: a consumer branching on whether the key is there
    is correct, and the alternative is a reader being shown a facet name. A proposal dict written
    before the field existed carries no `evidence_headline` and lands on the same absence.
    """
    attribution = proposal.get("attribution") or {}
    signature = proposal.get("signature") or {}
    component = registry.get(proposal["target_id"])
    disposition = component.disposition if component else "advisory"
    versioned = bool(component.versioned) if component else False
    prose = proposal.get("evidence_summary") or proposal.get("summary") or ""
    headline = proposal.get("evidence_headline") or ""

    members, _ = _members_and_evidence(signature, patterns_by_shape or {})
    pattern = _pattern_to_wire(signature, prose, members)

    wire = {
        "suggestionId": proposal["proposal_id"],
        "createdAt": created_at,
        # A newly OBSERVED suggestion is open. `SuggestionStore.upsert_all` preserves the
        # stored value for one it has seen before, so a dismissal is never clobbered by a
        # pipeline run.
        "status": "open",
        "targetType": proposal.get("target_type") or "control",
        "targetId": proposal["target_id"],
        "targetName": proposal.get("target_name") or proposal["target_id"],
        "targetLocation": attribution.get("source_location") or None,
        "baseVersion": proposal.get("base_version"),
        "mutability": attribution.get("mutability") or "UNRECORDED",
        "operation": proposal.get("operation") or "edit",
        "summary": proposal.get("summary") or "",
        "evidenceSummary": prose,
        "pattern": pattern,
        "candidates": [_candidate_to_wire(c) for c in proposal.get("candidates") or ()],
        "singleCandidateForced": proposal.get("single_candidate_forced"),
        "blastRadius": proposal.get("blast_radius") or {"class": "unknown", "instanceCount": 0},
        "affordances": _affordances(disposition, versioned),
        # Computed over EVERY member run, not just the reader's: run counts, a window and the
        # worst platform state are aggregates, and "how good is this evidence" is a question about
        # all of it. No id and no prose comes back from here.
        "evidenceQuality": evidence_quality(members, runs_by_id),
        "provenance": {
            "evaluatedAt": created_at,
            "judgeConfigHash": judge_config_hash,
            "judgeRuns": judge_runs,
            "evidenceSources": list(evidence_sources),
            "attributionBasis": attribution.get("basis"),
            # THE WARRANT FOR THE ACCUSATION. `attributionBasis: "registry_declared"` says the
            # registry itself declares this component fails this way; this is the sentence it
            # declared, so the claim can be checked instead of taken. Without it the basis is an
            # unfalsifiable label — the live item said `registry_declared` on both suggestions and
            # WHICH of the registry's declarations matched was unrecoverable from the bundle.
            #
            # `""` exactly when the basis is `keyword_inferred`, which is not an absence but a
            # different fact: there was no declaration to quote and the match came from the
            # component's id, type, notes or path. The two fields are emitted together and the basis
            # is what says which state this is, so no consumer has to branch on the key existing.
            # `registry_declared` with an empty sentence is impossible by construction — a declared
            # hit requires a word-boundary match inside `known_failure_modes` — and pinned by test.
            #
            # CONTENT: this is text from `ideas/sigma-registry.yaml`, a repo document written by
            # engineers about the harness's own components. It is not user input, not model prose,
            # and names no session — the same class of value as `sourceLocation` and `mutability`,
            # which already travel here.
            "matchedFailureMode": attribution.get("matched_failure_mode") or "",
            "attributionAlternatives": list(
                (proposal.get("provenance") or {}).get("attribution_alternatives") or ()),
            "registryHeadSha": (proposal.get("provenance") or {}).get("registry_head_sha"),
            "heuristicFacets": list(
                (proposal.get("provenance") or {}).get("heuristic_facets") or ()),
        },
    }
    if headline:
        wire["evidenceHeadline"] = headline
    return wire


#: Every key a suggestion may carry onto the GLOBAL bundle — an ALLOWLIST, and the direction is the
#: whole point. A denylist would withhold today's per-actor fields and silently publish tomorrow's.
#:
#: It is exactly the ``Suggestion`` interface in ``ui/src/improve-api.ts``: the keys
#: `suggestion_to_wire` builds, plus the three the cross-run store adds afterwards
#: (`firstSeenAt` / `lastSeenAt` / `reproducing`, all on that interface). `_published_keys_match_the
#: _builders` pins the first group against the builder, so the two cannot drift.
#:
#: ``evidenceHeadline`` is the one key here the TypeScript does not declare yet, and is OPTIONAL:
#: `suggestion_to_wire` omits it when the shape's leading facet has no headline, so it is listed
#: because a row that HAS one must survive this projection, not because every row carries it.
_PUBLISHED_SUGGESTION_KEYS = frozenset({
    "suggestionId", "createdAt", "status", "targetType", "targetId", "targetName",
    "targetLocation", "baseVersion", "mutability", "operation", "summary", "evidenceSummary",
    "evidenceHeadline",
    "pattern", "candidates", "singleCandidateForced", "blastRadius", "affordances",
    "evidenceQuality", "provenance",
    "firstSeenAt", "lastSeenAt", "reproducing",
})

#: The same allowlist one level down, for the nested pattern. Exactly `_pattern_to_wire`'s keys.
_PUBLISHED_PATTERN_KEYS = frozenset({
    "patternId", "shape", "shapeFacets", "symptom", "runCount", "passHits",
    "controlsExcluded", "controlsInconclusive", "controlsConsidered", "equallyDiscriminative",
})


def publishable_suggestion(row: dict) -> dict:
    """One suggestion reduced to what may appear on the GLOBAL bundle. THE PUBLISH FUNNEL.

    Every suggestion reaching the shared item goes through here, and that is not belt-and-braces:
    the cross-run store is a REPLAY CHANNEL THAT BYPASSES THIS MAPPER. `store.upsert_all` persists
    the whole wire suggestion as it stood when written, and a row absent from a later batch is
    marked `reproducing=False` with its CONTENT UNTOUCHED — so a row written by older producer code
    keeps that code's fields for as long as the finding lives, and is republished on every run. A
    projection applied only where a suggestion is BUILT therefore cannot hold: it has to be applied
    where suggestions are PUBLISHED, which is where the fresh and replayed paths converge.

    Measured, not theorised: the staging store held a row carrying `pattern.memberRunIds` and
    `pattern.rootCauseHypo` and no `runCount` at all, written by the deployed producer. Both rows on
    the live item are `reproducing: False`, so their content is frozen and this is the only place a
    change can reach them — see `_without_duplicated_caveats`, which exists for exactly that reason.

    ``runCount`` is DERIVED from a legacy row's `memberRunIds` rather than merely dropped alongside
    it. The count is the safe half of the field being withheld, it is true, and without it a
    long-lived finding — exactly the kind most likely to be on screen — would render a bare
    "runs" with no number (`EvidenceQualityBlock` sets the figure and the noun in separate spans,
    and an absent count is not 1, so the noun stays plural). A row carrying neither is left
    without the key rather than given a fabricated ``0``.

    ``evidenceHeadline`` is DERIVED here for a row that has none, from the `shapeFacets` the row
    already carries, and for the same reason ``runCount`` is: a stored row's content is frozen the
    moment it stops reproducing, so the builder can never reach it. Measured on the live item, one
    of the two suggestions was last seen five days before the other and is `reproducing: False` —
    it would have been the one card on the surface with no headline, and the facet it rests on
    (`has_failed_node`) is one of only two the bundle has ever carried. Derived by calling
    `propose.headline_for`, the same function the builder calls on the same facet list, so this is
    one definition reached twice and not a second opinion. Filled only when ABSENT: a row that
    already carries one keeps its own text.

    For a suggestion built by the current producer end to end this is a no-op — the builder has
    already set everything this function would derive — and that is the point: afterwards there is
    one funnel, so a field added to the per-actor half later is withheld from replayed rows
    automatically, with no second list to remember.
    """
    out = {key: value for key, value in row.items() if key in _PUBLISHED_SUGGESTION_KEYS}
    pattern = row.get("pattern")
    if isinstance(pattern, dict):
        kept = {k: v for k, v in pattern.items() if k in _PUBLISHED_PATTERN_KEYS}
        if "runCount" not in kept and isinstance(pattern.get("memberRunIds"), (list, tuple)):
            kept["runCount"] = len(pattern["memberRunIds"])
        out["pattern"] = kept
    if not out.get("evidenceHeadline"):
        # Absence is absence. A row carrying an empty string loses the key rather than publishing
        # one, so a consumer's "is the key there" test stays the right question to ask.
        out.pop("evidenceHeadline", None)
        headline = headline_for((out.get("pattern") or {}).get("shapeFacets") or ())
        if headline:
            out["evidenceHeadline"] = headline
    quality = row.get("evidenceQuality")
    if isinstance(quality, dict):
        out["evidenceQuality"] = _without_duplicated_caveats(quality)
    return out


def _without_duplicated_caveats(quality: dict) -> dict:
    """Drop any caveat that is a VERBATIM copy of `platform.reason` beside it.

    A no-op for anything `evidence_quality` builds today, and that is why it lives on the publish
    funnel rather than in the builder: `store.upsert_all` refreshes a row's content only when the
    finding was seen again, so a suggestion that stopped reproducing keeps the bytes it was written
    with FOREVER and is republished on every run. Measured on the live global item, both suggestions
    are `reproducing: False`, so both were carrying the same 741 characters twice — 1,488 JSON bytes
    off a 27,605-byte bundle, 5.4% — and no change to the builder alone could ever remove them.

    EXACT equality only. A near-copy is left alone: shortening prose a reader sees verbatim on the
    strength of a substring match is how a caveat that happened to quote the reason would lose the
    half that was not a quote.
    """
    reason = (quality.get("platform") or {}).get("reason")
    caveats = quality.get("caveats")
    if not isinstance(reason, str) or not reason or not isinstance(caveats, (list, tuple)):
        return quality
    kept = [c for c in caveats if c != reason]
    return quality if len(kept) == len(caveats) else {**quality, "caveats": kept}


def publishable_suggestions(stored_rows: Iterable[dict]) -> list:
    """The cross-run store's whole view, as the GLOBAL bundle may carry it. THE PUBLISH RULE.

    Two things happen here and both are the rule rather than a step in it, which is why the loop
    lives in this function instead of at each call site — it had been hand-copied in three places,
    including a test, and every copy carried the same blind spot.

    ``status`` is normalised to ``open`` for every row, dismissed rows included. A dismissal is
    PER-ACTOR and lives in the store's ``SUGG#<id>`` overlay, applied at read time by
    ``capability_api.improve_dto.merge_overlay``; publishing a locally-set status would apply one
    operator's decision to every user, and since ``merge_overlay`` only ever ADDS a dismissal there
    would be no undo. Dismissed rows are INCLUDED for a sharper reason: ``all_suggestions()`` hides
    them by default, so publishing the default view would make a suggestion vanish for everybody —
    the exact silent disappearance the store exists to prevent.

    Then every row goes through `publishable_suggestion`, which is where the replayed path and the
    freshly-built one converge.
    """
    return [publishable_suggestion({**row, "status": "open"}) for row in stored_rows]


def published_suggestions_block(stored_rows: Iterable[dict]) -> dict:
    """``{"suggestions": [...], "suggestionsTotal": n}`` — the publish rule AND its denominator.

    ``suggestionsTotal`` is how many publishable suggestions existed before any byte trim, so
    ``len(suggestions) < suggestionsTotal`` is what makes a trim legible. Without it the global
    bundle's ``aws_lambda._fit_bundle`` drops from the tail and the only trace reaches a log line:
    the item served to every reader presents a truncated list as the whole set. The per-actor bundle
    has had this since it was written — ``runsTotal`` beside ``runs`` — and this is the same fact for
    the one list on the shared item that grows without bound.

    Returned as a BLOCK rather than left to each call site, because both producers replace
    ``to_wire``'s suggestions with the store's view (`mine_local`, `aws_lambda`) and a count set
    beside one of those replacements and not the other is a denominator that silently describes the
    wrong list. The store is append-only by its own contract, so this only ever grows.
    """
    rows = publishable_suggestions(stored_rows)
    return {"suggestions": rows, "suggestionsTotal": len(rows)}


#: Field NAMES that may never appear anywhere on the global bundle, whatever nests them.
#:
#: A detector, not a projection — the projections above are the allowlists that decide what IS
#: published, and this is how a bundle assembled somewhere else (a file hand-fed to
#: `seed_snapshot`) is checked before it becomes the served item. Every name here is either a
#: session id list, a session id, or prose interpolating one.
GLOBAL_WITHHELD_FIELDS: tuple = (
    "memberRunIds",     # session ids behind a pattern
    "rootCauseHypo",    # facet evidence prose: plan node ids, span indices, timestamps
    "runId",            # the session a per-run row is about
    "passHitRuns",      # the passing runs that disqualified a shape — other tenants' controls
    "refinementLog",    # the search trace: the control set, and gateway-reported tool names
    # A note beside a dismissal. No code here can write one any more — the producer-private store's
    # setter is gone and a real dismissal is a per-actor `SUGG#` row — so this entry is not about
    # this repo's output: it guards a bundle handed to `seed_snapshot` from a FILE, which is the one
    # input the allowlists above never see.
    "dismissReason",
)


def withheld_fields(bundle: dict) -> list:
    """Sorted ``field@path`` for every `GLOBAL_WITHHELD_FIELDS` name found in ``bundle``.

    Empty is the only acceptable answer for anything about to be written to the shared item.
    Recursive and name-based on purpose: it makes no assumption about WHERE a leak would appear, so
    it also covers a block this module does not know about yet.
    """
    found: list = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else key
                if key in GLOBAL_WITHHELD_FIELDS:
                    found.append(f"{key}@{here}")
                walk(value, here)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, f"{path}[]")

    walk(bundle, "")
    return sorted(set(found))


def residue_to_wire(entry: dict) -> dict:
    """The GLOBAL half of a residue / evidence-gap / environment entry: the shape, the counts, and
    the engine's own reason.

    ``plainReason`` is omitted, not invented. The engine writes only `reason`, which carries facet
    names and REQ ids, and a rewrite belongs on the server rather than assembled from a template
    here, or the two would drift about what a failure means. The UI falls back to `reason` and the
    jargon is visible, which is the honest degradation.

    ``reason`` is safe to publish because every one of `mine`'s six constructions of it
    interpolates only harness-defined values — facet names, evaluator names and outcomes, a
    workload class, alarm names, and counts. Two of them used to interpolate session ids and no
    longer do: the pass-hit list now lives only in `pass_hit_runs`, which it duplicated, and the
    unjudgeable-control list is reported as a count. **A reason that names a run again makes this
    field unpublishable, so add the ids to a structured field instead.**

    Two session-id fields do NOT travel:

    ``runId``            the failing session. Reaches its owner as `_residue_evidence`.
    ``passHitRuns``      the PASSING runs that disqualified the shape. Reaches NOBODY — a control
                         belongs to whichever tenant ran it, so it is safe neither globally nor on
                         the failing run's owner's item. Only ``passHits``, the count, survives.

    Nor does ``refinementLog``: the search trace names both the control set and the tools a gateway
    reported failing, and a tool name can come from a user-registered MCP server
    (`contract.tool_name_sequence` strips it to the bare name), so it is the same class of value as
    `waste.detail`. No single actor is a safe home for it either.

    The operator artifact (`mine_local`'s `mine_result_*.json`) carries all three: it has no tenant
    boundary, which is the same reason its `population.evidence_incomplete` names ids.

    One row per failure, so the LENGTH of the list is still the failure count even with no ids in
    it.
    """
    return {
        "symptom": list(entry.get("symptom") or ()),
        "bestShape": list(entry.get("best_shape") or ()),
        "passHits": int(entry.get("pass_hits") if entry.get("pass_hits") is not None else -1),
        "reason": entry.get("reason") or "",
    }


def _residue_evidence(entry: dict) -> dict:
    """The same row for the actor who ran it, plus the session it is about.

    Spreads `residue_to_wire` rather than restating its fields, so the two projections cannot
    disagree about what a residue entry says.
    """
    return {"runId": entry["run_id"], **residue_to_wire(entry)}


def environment_to_wire(entry: dict, health) -> dict:
    """An ENVIRONMENT divert, plus the platform state that caused it. STRUCTURED, not only prose.

    The one bucket of the four that gets a `platform` block, because it is the only one whose divert
    was DECIDED by a platform reading: `mine` stands down from attribution when a failing run's
    window was `InfraState.DEGRADED`. `residue` and `evidenceGaps` were diverted for reasons about
    the evidence, and giving them an empty platform block would invite a reader to compare it against
    this one.

    Measured on the live global item, this is not a hypothetical: 5 of 5 failures were diverted here
    and ZERO patterns were admitted, so the bundle said "no findings" — and the only way to learn
    that the platform was the cause was to read five near-identical 577-character paragraphs and
    notice they all named the same alarm. A reader had no structured way to tell an ops day from a
    healthy one. `state` is now branchable and `alarms` is the name to take to CloudWatch.

    ``health`` comes from the RUN this entry is about rather than from the entry, because that is
    where it already lives (`Run.infra_health`, one CloudWatch correlation per run) — recording a
    second copy on `ResidueEntry` would be a second definition free to drift from the reason printed
    beside it. A run absent from the population reaches `platform_to_wire` as the UNKNOWN sentinel
    and lands on `state: "unknown"` with its own reason, never on healthy.

    ``reason`` still names the alarms in prose. It is rendered verbatim on both Improve surfaces and
    `residue_to_wire` refuses to invent a `plainReason`, so shortening it would take the only
    explanation an Inbox reader gets. This block is what a CONSUMER reads; the sentence is what a
    person reads.
    """
    return {**residue_to_wire(entry), "platform": platform_to_wire(health)}


def _infra_health_of(entry: dict, runs_by_id: dict):
    """The platform reading for the run an entry is about, or the UNKNOWN sentinel.

    The sentinel — never a fabricated healthy — for a run absent from the population, which the two
    callers can reach for different reasons: `to_wire` may be handed a result whose runs came from
    another invocation, and `actor_bundles` filters to one actor's slice before looking. Both mean
    "nobody here can say", which is what `platform_to_wire` renders the sentinel as.
    """
    run = (runs_by_id or {}).get(entry.get("run_id"))
    return getattr(run, "infra_health", UNKNOWN) if run is not None else UNKNOWN


def _environment_evidence(entry: dict, health) -> dict:
    """The same environment row for its owner, plus the session it is about.

    Spread over `environment_to_wire` for the reason `_residue_evidence` is spread over
    `residue_to_wire`: the admin's copy and the owner's copy of one divert must not be able to
    disagree about what happened to it.
    """
    return {"runId": entry["run_id"], **environment_to_wire(entry, health)}


#: ``propose_all``'s cause label -> one plain sentence, for the user-facing Inbox.
#:
#: This is the ONE `plainReason` the producer supplies, and it is a translation rather than a
#: rewrite. `residue_to_wire` refuses to invent one because `mine.py`'s reasons are free-form
#: prose about refinement, so any short version would be a new claim. `unattributed`'s causes are
#: a CLOSED SET OF THREE that `propose_all` already branches on and now labels, so mapping the
#: label to a sentence adds no information and cannot disagree with the engine — which is exactly
#: the condition `residue_to_wire`'s docstring sets when it says a rewrite "belongs on the server
#: rather than assembled from a template here". Keyed on the label and not on the reason STRING:
#: parsing prose to decide what prose to show is how the two come to contradict each other.
#:
#: No facet names, no session ids, no counts pulled from the row — the audience is a media
#: editor, and `improve-api.ts` says this surface is not supposed to show them the engine's
#: vocabulary. The admin wording stays available beside it as `reason`.
_UNATTRIBUTED_PLAIN_REASON = {
    "no_search_terms":
        "No part of the system has been catalogued as the owner of this kind of failure yet, "
        "so there is nothing here to recommend a change to.",
    "no_matching_component":
        "The parts of the system this points at are catalogued, but none of them matches this "
        "pattern closely enough to recommend a change with any confidence.",
    # Deliberately says nothing about matching: with no catalogue to read, "nothing matched" was
    # never established. And it names itself a temporary gap, because it is one — a later run
    # with a readable registry may well attribute this pattern.
    "registry_unavailable":
        "The catalogue of system components could not be read on this run, so nothing could be "
        "matched to this pattern at all. That is a temporary gap in the loop, not a finding "
        "about your agent.",
}


#: Waste kind -> one plain sentence, for a reader who does not know the engine's vocabulary.
#:
#: The second closed-set translation this producer supplies, and it earns the same licence
#: `_UNATTRIBUTED_PLAIN_REASON` does: `waste.WASTE_KINDS` is a fixed set of three harness literals
#: that the detectors already branch on, so mapping a kind to a sentence adds no information and
#: cannot disagree with the signal. Generated here rather than assembled in a browser, so a consumer
#: cannot invent a sentence and drift from what the detector measured.
#:
#: Each sentence says only what its kind's `waste._CONFIDENCE` note records as OBSERVED, which in
#: all three cases is narrower than the kind's own name suggests — and a summary that outran its own
#: confidence note would be the defect this table exists to avoid:
#:
#: * A repeated block is the same ordered TOOL NAMES running again. Spans record no arguments for
#:   harness builtins, so a second pass over different inputs is indistinguishable from a redo —
#:   hence "steps" and not "work". And "more than once" and not "twice": a block can run three or
#:   four times, and all but the last occurrence is charged.
#: * An uncollected result was never fetched BEFORE THAT REQUEST ENDED, which is the whole of what
#:   one session's spans can establish. The charge is the dispatch call's own duration, a floor.
#: * A step that failed and later ran without erroring is a second attempt, and nothing here
#:   establishes it was avoidable — so no "had to", and no claim about why it ran again.
#:
#: CONSTANT PER KIND, and never interpolated. The aggregate these ride on is ONE DynamoDB item read
#: by every actor: a waste kind is a harness literal and safe there, a tool name is not.
#: `contract.tool_name_sequence` strips `mcp tools/call <target>___<tool>` to the bare tool name, so
#: a tool from a user-registered MCP server is indistinguishable from a builtin and is user-derived.
#: Sequences stay on the per-actor `waste.findings[].detail`, where the reader owns the session they
#: came out of.
_WASTE_KIND_PROSE = {
    "abandoned_dispatch":  "A job's result was never collected before the request ended",
    "error_then_retry":    "A step failed and ran again",
    "repeated_sequence":   "The same series of steps ran more than once",
}


def _unattributed_to_wire(entry: dict) -> dict:
    """An ADMITTED pattern that no Σ component owns, carrying WHY nothing owns it.

    `propose_all` (`propose.py:411-422`) already computes this — a pattern survived
    discrimination, so it does describe a real difference between failing and passing runs,
    and then no registry component could be attributed to it. Without this key that record
    reached the artifact and the console and stopped there, so the surface showed neither the
    pattern nor the reason: the same class of drop as a missing scorecard, one field shallower.

    ``reason`` is the engine's own wording, carried through verbatim and never rewritten: it
    names the unmapped FACETS ("no registry search terms are defined for [...]") and whether
    the registry was reachable. The admin Findings surface shows exactly that.

    ``plainReason`` is the user-facing counterpart, and unlike `residue_to_wire`'s it CAN be
    supplied honestly — see `_UNATTRIBUTED_PLAIN_REASON` for why a closed cause set makes this
    a translation rather than a second opinion. It is OMITTED, not guessed, when the record
    carries no `cause` (an artifact written before the label existed) or a label this table
    does not know (a newer producer read by older mapper code): the Inbox falls back to
    `reason`, jargon and all, which is the honest degradation.

    Content rule (this bundle is ONE globally-readable item): `shape`/`shapeFacets` are facet
    names, which are literals in `signature.shape_facets`, and `reason` is a template over
    those same names plus a registry-availability flag. Nothing here is derived from user
    input or model prose — in particular the facet EVIDENCE strings, which do carry plan node
    ids and span names, are not part of this record.

    ``runCount`` replaces the member session ids this record used to carry. It is the same rule
    `_pattern_to_wire` follows: the count is the useful half, and the id list is per-row exposure
    even with no text attached. The ids reach the actors who own them, on their own bundles.
    """
    facets = list(entry.get("shape") or ())
    wire = {
        "shape": _shape_prose(facets),
        "shapeFacets": facets,
        # The CONSEQUENT, for the reason `_pattern_to_wire` carries it: this record is a
        # (shape -> symptom) hypothesis that found no owner, and publishing the antecedent alone made
        # two unattributed patterns differing only in which check failed read as one finding. Named
        # after the evaluators that produced them, so harness identifiers.
        "symptom": list(entry.get("symptom") or ()),
        "runCount": len(entry.get("member_run_ids") or ()),
        "reason": entry.get("reason") or "",
    }
    plain = _UNATTRIBUTED_PLAIN_REASON.get(entry.get("cause"))
    if plain:
        wire["plainReason"] = plain
    return wire


# The two goal-judge tiers. Named here rather than imported from `evaluate` so this mapper stays
# out of the judge's import chain — `evaluate` pulls `judge`, and this module is imported by the
# Lambda handler for its own sake. They are `evaluate.DEFAULT_GATING`.
_DISPATCH_TIER = "dispatch_correct"
_DELIVERY_TIER = "results_delivered"


def _mean_term(values: Iterable, total: int) -> dict:
    """Mean of the MEASURED values, carrying the denominator it was taken over.

    ``measuredOf`` is not decoration. A mean over 3 of 174 runs is not a population mean, and
    a reader who cannot see the denominator will read it as one. When nothing was measured the
    mean is ``UNMEASURED`` — not `0`, and not a mean over the empty set.
    """
    measured = [v for v in values if _is_number(v)]
    return {"mean": _measured(sum(measured) / len(measured) if measured else UNMEASURED),
            "measuredOf": len(measured), "total": total}


def _sum_term(values: Iterable, total: int) -> dict:
    """Sum of the MEASURED values, carrying the denominator. Same rule as `_mean_term`.

    This is the one a spend chart reads, and it is the one most likely to lie: summing the 3
    runs whose tokens happened to be recorded and captioning it "spend" understates the true
    figure by two orders of magnitude while looking authoritative. So the sum is ``UNMEASURED``
    when no run measured the term, and always ships with ``measuredOf``/``total``.
    """
    measured = [v for v in values if _is_number(v)]
    return {"sum": _measured(sum(measured) if measured else UNMEASURED),
            "measuredOf": len(measured), "total": total}


def _measured_total(values: Iterable) -> int:
    """Plain SUM of the measured values, for a count that is itself a denominator.

    `_sum_term`'s UNMEASURED sentinel is right for a figure a reader could mistake for a complete
    total. These counts are what a reader COMPARES such a figure against, so a sentinel here would
    leave nothing to compare it to. A run that recorded no usage contributes 0 to both halves, which
    is what it spent and what it left unpriced — the runs it is missing from are already visible in
    the ``measuredOf``/``total`` beside it.
    """
    return sum(value for value in values if _is_number(value))


def _pass_rate(passed: int, failed: int):
    """PASS / (PASS + FAIL). `None` when the denominator is 0.

    UNSCORED is in NEITHER term. A run whose gating evaluator could not measure is not a
    failure (`evaluate.gate`) and obviously not a pass, so folding it into either side would
    move a rate that describes the agent using runs that describe the evidence. It is reported
    as its own count instead.
    """
    scored = passed + failed
    return (passed / scored) if scored else None


def _population(counts: dict) -> dict:
    """``contract.verdict_counts`` in the wire's spelling: lowercase keys, same numbers."""
    return {"total": counts["total"], "pass": counts[Overall.PASS.value],
            "fail": counts[Overall.FAIL.value], "unscored": counts[Overall.UNSCORED.value]}


def _discovered_sessions(discovery: Optional[dict]) -> dict:
    """``{"sessions": {...}}`` — what discovery found versus what it handed on. Or ``{}``.

    ``population.total`` is the size of the SCORED set, and on the discovered population that set is
    a SAMPLE: `observe.population.discover_sessions` drops every session under the span floor and
    `harvest_population` then slices the rest to the cap. Neither difference used to be measured, so
    ``total: 25`` was captioned "the population" while being an unknown fraction of one — and the
    pass rate, the spend, and every history row folded from those runs inherited the caption.

    Three counts, each naming its own unit so no two can be confused:

      ``discoverable``    sessions in the harvest with enough spans to be worth judging — the
                          denominator, i.e. what ``admitted`` falls short of when the cap binds.
      ``admitted``        how many of those the cap let through. This is what ``total`` counts.
      ``belowMinSpans``   sessions the span floor excluded, so ``discoverable + belowMinSpans`` is
                          the whole harvest and neither exclusion is silent.

    ``{}`` — the key ABSENT — for a population that had no discovery step at all (the labelled
    benchmark has fixed membership and no cap), because zeros there would claim a measurement of a
    thing that never ran. That is the same rule as everything else here, one level up.
    """
    if not discovery:
        return {}
    counts = {"discoverable": discovery.get("discoverable"),
              "admitted": discovery.get("admitted"),
              "belowMinSpans": discovery.get("below_min_spans")}
    if any(not _is_number(v) for v in counts.values()):
        return {}
    return {"sessions": counts}


def _class_entry(counts: dict) -> dict:
    """The WIRE's projection of one workload class's counts.

    The counting is `contract.verdict_counts_by_class`, shared with
    `mine_local._workload_breakdown` so the artifact and the wire can never disagree about the
    same population. This function only decides the spelling, and it differs from the artifact's
    in two deliberate ways: lowercase count keys, and ``gating`` normalised to a list-of-sets
    ALWAYS. The artifact flattens a single set to a flat list; a field whose type depends on the
    data is not something a TypeScript reader can consume, so the wire keeps one shape and lets
    `gatingIsMixed` carry the fact instead.
    """
    population = _population(counts)
    return {
        **population,
        "passRate": _pass_rate(population["pass"], population["fail"]),
        "gating": [list(g) for g in counts["gating"]],
        "gatingIsMixed": len(counts["gating"]) > 1,
        "basis": list(counts["basis"]),
    }


#: The four separately-priced token categories, as ``(wire name, Run.resources key)``. ONE mapping,
#: read by the population total AND by the per-model split, so the two cannot come to disagree about
#: what a category is called or which measured term backs it.
#:
#: ``input`` is UNCACHED input, DERIVED upstream as ``total - output - cacheRead - cacheWrite``,
#: which is Bedrock's own arithmetic; the recorded ``input_tokens`` span attribute is inconsistent
#: about whether it includes cached tokens (see ``harvest_live``).
_TOKEN_CATEGORY_TERMS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cacheRead", "cache_read_tokens"),
    ("cacheWrite", "cache_write_tokens"),
)

#: How many model ids ``spend.byModel`` names before the remainder is folded into ``byModelOther``.
#:
#: SEMANTIC, so deliberately NOT env-overridable: it says how wide a Bedrock roster this item will
#: describe, which is a property of the wire shape rather than an operator's tuning knob.
#:
#: 20 despite a 336 h harvest carrying only FIVE ids, because the id space grows with ROUTING
#: CONFIGURATION and not with the number of models. That population already reaches the same four
#: models under two different cross-region prefixes (`build_run._normalise_model` recognises five
#: such prefixes), so enabling another region MULTIPLIES the roster instead of adding to it — four
#: models over five prefixes is 20 ids for exactly today's model set.
#:
#: The other half of the argument is the item budget. ``scorecard`` is FIXED size and never trimmed,
#: so these bytes come out of the suggestions' allowance in ``aws_lambda._fit_bundle``, and a bound
#: is the only thing that caps them. A row is six ``_sum_term``s plus an id, and the widest one this
#: population produces serializes to 434 bytes, so 20 rows are under 9 KB — 2.5% of
#: ``aws_lambda._ITEM_BUDGET_BYTES``, and the most this field can ever take.
#:
#: Its own literal, shared with nothing. ``waste._MAX_DETAIL_NAMES`` and
#: ``signature._MAX_EVIDENCE_SPAN_IDS`` bound how much evidence is PRINTED, which is a different
#: fact about a different field.
_SPEND_MODELS_NAMED = 20


def _by_model_spend(per_run: Iterable, measured_of: int) -> tuple:
    """``(byModel rows, byModelOther)`` — the population's tokens split by the model they went to.

    This is the one figure on this surface a reader can act on: it is what says a step should move
    onto a cheaper model. ``modelsSeen`` beside it says only how many models exist, and on the
    measured population one id is a third of the sessions and three quarters of the tokens, so the
    count alone destroys the fact.

    THE DENOMINATOR IS THE SUBTLE PART, and it is not the population. A row's ``measuredOf`` is the
    number of runs in which THAT model appears; its ``total`` is ``spend.tokens.measuredOf`` — the
    runs whose usage was measured at all. A run with no usage block does not record which model it
    did not use, so it cannot sit in a per-model denominator without asserting an absence nobody
    observed. The two denominators compose instead: model-share within the measured runs, then
    measured-share within the population from ``spend.tokens`` itself. So a model seen in one
    session reads ``1 of 354`` and cannot pass as a platform-wide trend.

    Rows are ranked by measured tokens and bounded by `_SPEND_MODELS_NAMED`. What the bound
    withholds is folded into ``byModelOther`` WITH its own model count, so the parts still reconcile
    against ``spend.tokens`` rather than leaving a silent shortfall. ``byModelOther.tokens`` is a
    ``_sum_term`` like every other sum here even when nothing was withheld — ``models: 0`` is what
    says the remainder is empty, and a bare ``0`` in that slot would be the one number in this block
    that meant something different from all the rest.

    An id of ``"UNKNOWN"`` is named like any other, deliberately. `harvest_live._usage_by_session`
    substitutes that literal when a usage row carries no model attribute, and those tokens are real
    and measured — only their attribution is missing. Dropping them would stop the rows adding up to
    ``spend.tokens``; folding them into ``byModelOther`` would report "nobody recorded a model" as
    "the bound withheld this". Named, the gap stays visible and stays in the arithmetic. It is a
    model id in a ``model`` field, never a value in a measured slot, so it cannot be confused with
    the UNKNOWN sentinel's wire spelling.
    """
    runs = []
    for by_model in per_run:
        # A run whose usage was never measured arrives as the UNMEASURED sentinel and names no
        # model, so it contributes to no per-model denominator.
        if not isinstance(by_model, dict):
            continue
        entry = {model: counts for model, counts in by_model.items()
                 if isinstance(model, str) and model and isinstance(counts, dict)}
        if entry:
            runs.append(entry)

    ranked_by: dict = {}
    for entry in runs:
        for model, counts in entry.items():
            value = counts.get("tokens", UNMEASURED)
            ranked_by[model] = ranked_by.get(model, 0) + (value if _is_number(value) else 0)
    # Descending spend, ties broken by id, so the same population always yields the same rows.
    ranked = sorted(ranked_by, key=lambda model: (-ranked_by[model], model))
    named = ranked[:_SPEND_MODELS_NAMED]
    withheld = set(ranked[_SPEND_MODELS_NAMED:])

    def values(model: str, key: str) -> list:
        return [entry[model].get(key, UNMEASURED) for entry in runs if model in entry]

    rows = [{
        "model": model,
        "tokens": _sum_term(values(model, "tokens"), measured_of),
        "byCategory": {wire: _sum_term(values(model, key), measured_of)
                       for wire, key in _TOKEN_CATEGORY_TERMS},
        "llmCalls": _sum_term(values(model, "llm_calls"), measured_of),
    } for model in named]

    remainder = []
    for entry in runs:
        theirs = [entry[model].get("tokens", UNMEASURED) for model in entry if model in withheld]
        if not theirs:
            continue
        measured = [value for value in theirs if _is_number(value)]
        remainder.append(sum(measured) if measured else UNMEASURED)
    return rows, {"models": len(withheld), "tokens": _sum_term(remainder, measured_of)}


# The most models one scorecard compares by name; the rest are summed into ``byModelScoresOther``.
_SCORE_MODELS_NAMED = 12


def _primary_model(run) -> Optional[str]:
    """The model that did most of a run's work: the one its measured tokens mostly went to.

    A request routinely touches two models (the orchestrator and a cheaper sub-step), and crediting
    its verdict to both would count one outcome twice; crediting it to the one that spent the most
    is what "which model ran this" means to a reader comparing models. Without per-model usage the
    run's first named model is used, and with neither the run is unattributed (`None`).
    """
    resources = getattr(run, "resources", None) or {}
    by_model = resources.get("by_model")
    if isinstance(by_model, dict) and by_model:
        best, best_tokens = None, -1.0
        for model, counts in sorted(by_model.items()):
            if not isinstance(model, str) or not model or not isinstance(counts, dict):
                continue
            tokens = counts.get("tokens")
            value = float(tokens) if _is_number(tokens) else 0.0
            if value > best_tokens:
                best, best_tokens = model, value
        if best:
            return best
    models = resources.get("models")
    if isinstance(models, (list, tuple)):
        for model in models:
            if isinstance(model, str) and model:
                return model
    return None


def _agentcore_scores(members: list, total: int) -> dict:
    """``{evaluator: {mean, measuredOf, total}}`` — AgentCore's online-eval scores for these runs.

    Read off ``Run.resources["agentcore_eval"]`` (``build_run.HarvestedAgentCoreEvalSource``), one
    value per run: that session's mean for the evaluator (a TRACE or TOOL_CALL evaluator scores a
    session many times; it counts once here, so a long session does not outweigh a short one).
    ``measuredOf`` is how many of the model's runs AgentCore scored — it samples, scores main-agent
    sessions only, and lands minutes after a session ends — so a run it never scored is in neither
    term. An evaluator no run carries is ABSENT, never a 0. Nothing is re-judged here.
    """
    names = sorted({name for run, _ in members
                    for name in ((run.resources or {}).get("agentcore_eval") or {})})
    out = {}
    for name in names:
        values = []
        for run, _ in members:
            entry = ((run.resources or {}).get("agentcore_eval") or {}).get(name)
            values.append(entry.get("mean") if isinstance(entry, dict) else None)
        out[name] = _mean_term(values, total)
    return out


def _by_model_scores(runs_by_id: dict, verdicts: list) -> tuple:
    """``(rows, other)`` — every score this scorecard reports, again PER MODEL, for comparing models.

    Each judged run is credited to its `_primary_model`. A row carries the same terms as the
    population: the verdict counts and pass rate (UNSCORED in neither term), the two judged tiers as
    means over the runs that scored them, every gating CHECK as a pass rate over the runs it
    actually measured (``NOT_APPLICABLE``/``NO_DATA`` in neither term, so a check a model never
    needed reads as absent rather than 0%), and what a run cost (tokens, LLM calls, wall time) as
    means with their denominators. Nothing new is judged — this regroups what the evaluators already
    produced. Rows rank by runs, named up to `_SCORE_MODELS_NAMED`; ``other`` counts the rest and
    the runs no model could be credited for.
    """
    groups: dict = {}
    unattributed = 0
    for verdict in verdicts:
        run = runs_by_id.get(verdict.run_id)
        model = _primary_model(run) if run is not None else None
        if model is None:
            unattributed += 1
            continue
        groups.setdefault(model, []).append((run, verdict))

    def row(model: str, members: list) -> dict:
        vs = [v for _, v in members]
        population = _population(verdict_counts(vs))
        total = len(members)

        def tier(name: str) -> dict:
            return _mean_term([next((r.score for r in (v.evaluator_results or ())
                                     if r.name == name and r.score is not None), None) for v in vs], total)

        checks: dict = {}
        for v in vs:
            for result in v.evaluator_results or ():
                if result.kind != "code":
                    continue
                entry = checks.setdefault(result.name, {"pass": 0, "fail": 0, "notMeasured": 0})
                if result.outcome == Outcome.PASS:
                    entry["pass"] += 1
                elif result.outcome == Outcome.FAIL:
                    entry["fail"] += 1
                else:
                    entry["notMeasured"] += 1
        for entry in checks.values():
            entry["rate"] = _pass_rate(entry["pass"], entry["fail"])

        def per_run(key: str) -> dict:
            return _mean_term([(run.resources or {}).get(key, UNMEASURED) for run, _ in members], total)

        return {
            "model": model,
            "population": population,
            "passRate": _pass_rate(population["pass"], population["fail"]),
            "tiers": {"dispatch": tier(_DISPATCH_TIER), "delivery": tier(_DELIVERY_TIER)},
            "checks": {name: checks[name] for name in sorted(checks)},
            "perRun": {"tokens": per_run("tokens"), "llmCalls": per_run("llm_calls"),
                       "latencyMs": per_run("latency_ms")},
            "agentcore": _agentcore_scores(members, total),
        }

    ranked = sorted(groups, key=lambda m: (-len(groups[m]), m))
    named = ranked[:_SCORE_MODELS_NAMED]
    withheld = ranked[_SCORE_MODELS_NAMED:]
    return ([row(m, groups[m]) for m in named],
            {"models": len(withheld), "runs": sum(len(groups[m]) for m in withheld), "unattributed": unattributed})


def _cost_basis(bases: Iterable) -> str:
    """The ONE ``costBasis`` for the population, from what the runs reported.

    Read off ``Run.resources`` rather than re-read from the producer's own config, so the wire
    describes the data it actually received. Every run in one pipeline invocation shares a rate
    table, so there is normally exactly one distinct value; anything else (none, or a mix, which
    would mean the population was assembled from two configurations) falls back to the
    unmeasured reason rather than picking a winner and implying a price applied throughout.
    """
    distinct = {b for b in bases if isinstance(b, str) and b}
    return distinct.pop() if len(distinct) == 1 else COST_UNMEASURED_REASON


def _usage_shortfall(shortfall: Optional[dict]) -> dict:
    """What the HARVEST measured and could not hand to the population, beside the sum it is missing
    from. Four `Measured<number>`s, never zeros.

    ``spend.tokens`` sums the runs the population admitted, and its ``measuredOf``/``total`` are
    over those runs alone — so a session whose spend was measured but whose spans the harvest's row
    cap dropped is not short in that fraction, it is ABSENT from both halves of it. The live item
    read ``{sum: 10320108, measuredOf: 25, total: 25}``, i.e. complete coverage, on a harvest that
    had thrown tokens away. This block is the only term that can say so.

    ``tokensDiscarded`` is the amount missing from ``tokens.sum``; ``tokensMeasured`` is what the
    usage query saw across the whole window, and it is the honest denominator for it. The two do
    NOT close the gap to ``tokens.sum`` by subtraction, and must not be read as if they did: the
    span floor and the population cap narrow the set again afterwards, and that shortfall is
    reported separately (`population.sessions`). What this block licenses is one statement — the
    money number is short by at least ``tokensDiscarded``.

    UNMEASURED throughout when the harvest recorded no audit (`build_run.harvest_usage_shortfall`),
    because a harvest that never looked and a harvest that looked and found nothing dropped are
    different facts and only the second is good news.
    """
    shortfall = shortfall or {}

    def term(key: str):
        # Anything that is not a real number becomes the sentinel: the reader hands these over as
        # `UNMEASURED` objects, the local artifact has already flattened them to the string
        # `"UNMEASURED"`, and an older audit file may carry neither. All three mean the same thing
        # and must not depend on which path the value arrived by.
        value = shortfall.get(key)
        return _measured(value if _is_number(value) else None)

    return {
        "tokensDiscarded": term("tokens_discarded"),
        "tokensMeasured": term("tokens_measured"),
        "sessionsDiscarded": term("sessions_discarded"),
        "sessionsMeasured": term("sessions_measured"),
    }


def _spend(resource, total: int, usage_shortfall: Optional[dict] = None) -> dict:
    """What the population cost, in the units the harness can actually measure. FIXED size.

    ``tokens`` is a real measurement now: ``harvest_live.harvest_token_usage`` sums the session's
    ``chat`` spans server-side and ``build_run`` reads it back, so this is the same number on a
    laptop and in the Lambda. It still carries ``measuredOf``/``total``, because roughly a third
    of harvested sessions record no LLM usage at all (measured: 356 of 549 sessions with >=5
    spans carry it), so a bare sum would understate a partly-harvested population.

    ``byCategory`` is the half that makes a spend chart honest rather than merely large. The four
    categories are priced very differently — a cache READ is about a tenth of an input token —
    and on this population cache reads are 77% of all prompt tokens. So one number times one rate
    is not a slightly-imprecise cost, it is wrong by most of an order of magnitude, which is why
    the split ships even though nothing prices it yet.

    ``cost`` is an ESTIMATE priced upstream per model and per token category
    (`build_run._cost_usd`), and it carries TWO denominators because it is the one term here that
    can be partly measured within a single run. ``measuredOf``/``total`` count RUNS, like every
    sibling. ``tokensPriced``/``tokensUnpriced`` count TOKENS, and they are what make a partial
    figure legible: a model with no rate row is skipped rather than costing its whole session, so a
    session can be one fully-measured run and still contribute a short amount. Reported on runs
    alone this wire would say "8 of 8 sessions priced" over a total missing whatever the skipped
    model spent — a silent undercount that reads as complete, which is worse than the sentinel this
    replaced. ``costBasis`` says the same thing in the words the UI renders, and ``modelsSeen`` is
    the count that makes a single-rate cost indefensible in the first place.

    ``byModel`` / ``byModelOther`` are the same asymmetry one level down, and they are the part of
    this block a reader can act on without any rate table at all. Tokens are wildly unevenly
    distributed across the roster — measured on a 336 h harvest, one of five ids carries 76% of the
    tokens over a third of the sessions — so knowing WHICH model the spend went to is what leads to
    moving a step onto a cheaper one, while ``modelsSeen: 5`` flattens it away. See
    `_by_model_spend` for the per-model denominator, which is not the population.

    ``usageShortfall`` is the term that keeps ``tokens`` honest about what it never saw — see
    `_usage_shortfall`. Everything else here is a fold over the runs in hand, which is exactly why
    none of them can report a session the harvest dropped before the population was assembled.
    """
    tokens = _sum_term(resource("tokens"), total)
    # The runs whose usage was measured at all. Read off the population term rather than recounted,
    # so a per-model row and the total above can never disagree about their own denominator.
    by_model, by_model_other = _by_model_spend(resource("by_model"), tokens["measuredOf"])
    return {
        "tokens": tokens,
        # The two token counts are EXPLICIT keys rather than a re-reading of `measuredOf`/`total`,
        # which mean runs on every other term on this wire. Overloading them for this one term would
        # make the raw wire mislead anyone comparing cost's coverage against tokens' coverage.
        "cost": {**_sum_term(resource("cost"), total),
                 "tokensPriced": _measured_total(resource("cost_tokens_priced")),
                 "tokensUnpriced": _measured_total(resource("cost_tokens_unpriced"))},
        "costBasis": _cost_basis(resource("cost_basis")),
        "latencyMs": _sum_term(resource("latency_ms"), total),
        "byCategory": {wire: _sum_term(resource(key), total)
                       for wire, key in _TOKEN_CATEGORY_TERMS},
        "llmCalls": _sum_term(resource("llm_calls"), total),
        # How many DISTINCT model ids the population's tokens were spent on. A count, not a list:
        # the count is what makes a single-rate cost indefensible.
        "modelsSeen": len({m for models in resource("models")
                           if isinstance(models, (list, tuple)) for m in models}),
        "byModel": by_model,
        "byModelOther": by_model_other,
        "usageShortfall": _usage_shortfall(usage_shortfall),
    }


#: The one workload class whose sessions can have an owner, so the one whose evidence can reach a
#: per-actor bundle. MEASURED, not assumed: over a 336h harvest of 541 sessions with enough spans,
#: 182 of 184 `conversational` sessions resolved to an actor in AgentCore Memory and 0 of the 357
#: in the other three classes did. That is not an attribution defect — those classes have no user
#: conversation by construction, which is the same reason `population.py` gives for not running the
#: goal judge on them ("running a check whose dimension does not exist manufactures failures").
#:
#: Filtered on explicitly rather than left to fall out of "no actor was found", so that a future
#: Memory read which DOES name an owner for a plan sub-session cannot silently start publishing a
#: shared plan's evidence into one sibling's bundle.
_OWNED_WORKLOAD_CLASS = WorkloadClass.CONVERSATIONAL.value


def _owner_split(runs: list, verdicts: list) -> tuple:
    """``({actor_id: [Run]}, how many runs COULD have had an owner)``. One definition, two readers.

    `scorecard_to_wire` reports these as counts on the global item and `actor_bundles` writes the
    partitions themselves, and the rule deciding whose evidence is whose must be one fact — a hand
    copy of a fold over verdicts has already drifted once here (see `evaluate.gate`).

    A run reaches an actor only if BOTH hold: its class is `_OWNED_WORKLOAD_CLASS`, and Memory
    named an owner (`Run.actor_id` is a real string, not the UNKNOWN sentinel). A conversational
    run with no owner therefore lands in the second number and in nobody's bundle — it still counts
    in the population above, because it is real population; it simply has no addressee.
    """
    class_by_id = {v.run_id: v.workload_class for v in verdicts or ()}
    by_actor: dict = {}
    ownable = 0
    for run in runs or ():
        if class_by_id.get(run.run_id) != _OWNED_WORKLOAD_CLASS:
            continue
        ownable += 1
        actor = getattr(run, "actor_id", UNKNOWN)
        if isinstance(actor, str) and actor:
            by_actor.setdefault(actor, []).append(run)
    return by_actor, ownable


def _attribution(runs: list, verdicts: list) -> dict:
    """How much of the population has a per-actor bundle behind it. COUNTS ONLY.

    Five numbers because three of them are denominators (invariant 1). ``unattributable`` is the
    one that must not go missing: those runs are scored, counted and judged, and no actor's surface
    will ever show them — a fact that is invisible unless it is stated, and that would otherwise
    look like a bundle that failed to write.

    ``actors`` is a count and never a list. Actor ids are Cognito subjects; naming them here would
    tell every tenant who else is on the harness. The count is what makes the platform-wide reading
    of the numbers above it checkable — this scorecard aggregates every tenant, so it cannot be
    captioned as one agent's score, and this is how a reader can tell.
    """
    by_actor, ownable = _owner_split(runs, verdicts)
    attributed = sum(len(members) for members in by_actor.values())
    return {
        "actors": len(by_actor),
        "attributed": attributed,
        "unattributable": ownable - attributed,
        "ownable": ownable,
        "runs": len(runs or ()),
    }


#: How many alarm SOURCES `platform.sources` may name before it stops being fixed-size.
#:
#: Bounded by the number of `observe.infra_health.AlarmSource` implementations that exist — two today
#: (`HarvestedAlarmSource`, `LiveCloudWatchAlarmSource`) — and not by the population, the account's
#: alarm roster or anything else that grows with traffic. One producer invocation normally reports
#: exactly one, because `default_alarm_source` picks per process; a mixed population would mean runs
#: stamped by two different harvests, which is worth seeing rather than collapsing. 4 leaves room for
#: two more sources without the bound ever becoming the thing that hides one.
_PLATFORM_SOURCES_NAMED = 4


def _platform_population(runs: list) -> dict:
    """What the PLATFORM was doing under this population. COUNTS AND A STATE, so it stays fixed size.

    `InfraHealth` is computed per run — one CloudWatch correlation each, the most expensive
    observation in the harvest — and the population's state reached the served item NOWHERE. The
    consequence is measurable on the live global item: 5 of 5 failures were diverted to
    `patterns.environment` for platform reasons and ZERO patterns were admitted, so the bundle read
    as "nothing found" with no structured way to tell that from a healthy day.

    ``state`` is the WORST state in the population, not the most common, and it is the same
    worst-first rule `_worst_platform` applies to one pattern's members — literally the same, via
    `_STATE_SEVERITY`. The question a reader is asking is whether ANY of this evidence sits under a
    compromised platform.

    FOUR counts partitioning ``runs``, because collapsing them loses the distinction the three-state
    type exists for (`contract.InfraState`, `Run.infra_health`):

      ``degraded``      a lookup ran and found an alarm firing over the run's window.
      ``healthy``       a lookup ran and conclusively found none.
      ``inconclusive``  a lookup RAN and could not tell — throttled, out of retention, or every
                        alarm unplaceable. This is not a soft healthy.
      ``unconsulted``   no alarm source was wired for this run at all. The UNKNOWN sentinel, and a
                        different fact from the one above: one is a failed measurement, the other is
                        an absent one, and only the second is fixable by configuration.

    The four always sum to ``runs``, which is the denominator (invariant: every count carries one).
    ``degraded: 0`` alone is indistinguishable from a population nobody looked at, and on the offline
    golden path that is the actual state.

    ``alarmsInAlarm`` and ``alarmsIndeterminate`` are counts of DISTINCT alarm names, unioned across
    the population — not lists. Names travel per row on `patterns.environment`, where the row is
    about one run and the list is what a reader takes to CloudWatch; here a list would grow with the
    account's alarm roster, and `scorecard_to_wire` is documented fixed-size precisely so its bytes
    never come out of the suggestion list's allowance. The second count is why the first is readable:
    ``alarmsInAlarm: 0`` with twelve indeterminate is not a quiet platform.

    ``sources`` names WHICH alarm source answered, bounded by `_PLATFORM_SOURCES_NAMED`. It is the
    difference between a live CloudWatch read and a harvested snapshot that may predate the run, and
    it is a class name from this repo rather than anything the account or a tenant supplied.

    CONTENT: four counts, two counts, a state and a bounded list of source class names. No session
    id, no actor id, no alarm name, no prose. Safe on the shared item by the same rule as every other
    aggregate here.
    """
    healths = [getattr(run, "infra_health", UNKNOWN) for run in runs or ()]
    counts = {"degraded": 0, "healthy": 0, "inconclusive": 0, "unconsulted": 0}
    states: list = []
    firing: set = set()
    unplaceable: set = set()
    sources: set = set()
    for health in healths:
        if not isinstance(health, InfraHealth):
            counts["unconsulted"] += 1
            # An unconsulted run is UNKNOWN for the purpose of the population's state — it is
            # counted apart because the two are different facts, not because one is milder.
            states.append(InfraState.UNKNOWN)
            continue
        states.append(health.state)
        if health.state is InfraState.DEGRADED:
            counts["degraded"] += 1
        elif health.state is InfraState.HEALTHY:
            counts["healthy"] += 1
        else:
            counts["inconclusive"] += 1
        firing.update(health.alarms or ())
        unplaceable.update(getattr(health, "indeterminate", ()) or ())
        if health.source:
            sources.add(health.source)
    # `_STATE_SEVERITY`, the same worst-first order `_worst_platform` uses on one pattern's members.
    # An empty population is UNKNOWN: nothing was measured, which is not a clean platform.
    worst = max(states, key=lambda s: _STATE_SEVERITY.get(s, 1)) if states else InfraState.UNKNOWN
    return {
        "state": worst.value,
        **counts,
        "runs": len(healths),
        "alarmsInAlarm": len(firing),
        "alarmsIndeterminate": len(unplaceable),
        "sources": sorted(sources)[:_PLATFORM_SOURCES_NAMED],
    }


def scorecard_to_wire(runs: list, verdicts: list, discovery: Optional[dict] = None,
                      usage_shortfall: Optional[dict] = None) -> dict:
    """Population-level aggregates. FIXED size, so it never participates in bundle trimming.

    ``discovery`` and ``usage_shortfall`` are the two facts NOT derivable from ``runs``/``verdicts``,
    and they are the two that say how much this scorecard never saw: what the population cap and
    span floor discarded (`_discovered_sessions`), and what the harvest's row cap discarded before
    either could look (`_usage_shortfall`). Both default to absent/UNMEASURED rather than to zero,
    so a caller that cannot supply them says "not measured" and not "nothing was dropped".

    This is what a metrics row and a spend chart read. Five rules, all consequences of
    invariant 1:

    * UNSCORED is neither pass nor fail — its own count, never folded into either, and
      `passRate` is taken over `pass + fail` alone.
    * every average and every sum carries its denominator (`measuredOf` vs `total`), because
      most terms here are UNMEASURED for most runs.
    * per workload class as well as overall, because each class is gated on a different
      objective — a `tool_discovery` PASS and a `conversational` FAIL do not measure the same
      thing, and one number over both means nothing.
    * `total` is the size of the SCORED population (one verdict per run), so an unpaired
      verdict shows up as `measuredOf < total` rather than vanishing from the denominator.
    * WHY runs are failing is named by EVALUATOR, from outcomes — `outcomes`, via
      `contract.gating_outcome_counts`. Never by counting `Verdict.reason` strings: that field
      carries an `(on partial evidence: …)` suffix, so near-identical causes would fragment into
      separate keys, and the prose half of "why" (the judge's sentence) may not travel on this
      global item at all.

    The counting of verdicts is `contract.verdict_counts` / `verdict_counts_by_class`, shared
    with `mine_local._workload_breakdown`; only the spelling is decided here.
    """
    verdicts = list(verdicts or ())
    runs_by_id = {run.run_id: run for run in runs or ()}
    total = len(verdicts)
    paired = [runs_by_id[v.run_id] for v in verdicts if v.run_id in runs_by_id]

    def tier_scores(name: str) -> list:
        return [next((r.score for r in (v.evaluator_results or ())
                      if r.name == name and r.score is not None), None) for v in verdicts]

    def resource(key: str) -> list:
        return [run.resources.get(key, UNMEASURED) for run in paired]

    population = _population(verdict_counts(verdicts))
    span, derived = _window_span([run.window for run in paired])
    by_model_scores, by_model_scores_other = _by_model_scores(runs_by_id, verdicts)

    return {
        "population": {**population, **_discovered_sessions(discovery)},
        "passRate": _pass_rate(population["pass"], population["fail"]),
        "tiers": {
            "dispatch": _mean_term(tier_scores(_DISPATCH_TIER), total),
            "delivery": _mean_term(tier_scores(_DELIVERY_TIER), total),
        },
        "spend": _spend(resource, total, usage_shortfall),
        # The same scores per model (who ran each judged run), so a model change is a comparison.
        "byModelScores": by_model_scores,
        "byModelScoresOther": by_model_scores_other,
        # Which gating check is failing, and which could not measure. Evaluator names are harness
        # identifiers, so this is the one form of "why" that may travel on a globally-readable
        # item — the judge's sentence about what the user asked for may not.
        "outcomes": gating_outcome_counts(verdicts),
        "byWorkloadClass": {cls: _class_entry(counts)
                            for cls, counts in sorted(
                                verdict_counts_by_class(verdicts).items())},
        # Wall-clock extent of the scored population, from the member windows themselves.
        "window": span,
        "derivedWindows": derived,
        # How much of this population's own evidence is KNOWN missing. A count, never a list of
        # runs — every actor reads this item. It belongs next to `derivedWindows` because it answers
        # the same question — how far to trust the numbers above — and because a reader who cannot
        # see it will read a pass rate computed over partially-observed runs as a pass rate.
        "evidenceComplete": _evidence_completeness(paired, total),
        # How many actors the numbers above are aggregated over, and how much of the population has
        # a per-actor bundle behind it. Taken over `runs` rather than `paired` so a run with no
        # verdict is counted in the denominator instead of vanishing from it.
        "attribution": _attribution(runs or [], verdicts),
        # What the PLATFORM was doing underneath all of it — the fact that decides whether a day
        # with no findings was a quiet day or an ops day. Over `runs` and not `paired` for the same
        # reason: a run whose verdict went missing still had its window correlated against alarm
        # history, and dropping it would understate a degraded population.
        "platform": _platform_population(runs or []),
    }


def _wall_ms_by_run(findings: list) -> dict:
    """``{run_id: that run's waste wall time}`` — UNMEASURED for a run any of whose findings is.

    Exists so ``wallMs`` can carry the denominator the rest of this wire uses. `_sum_term`'s
    ``measuredOf``/``total`` count RUNS everywhere else (`_spend` says so, and says why it added
    explicitly-named keys rather than overload them), but the waste sums used to pass the FINDING
    count — so `error_then_retry` reported ``wallMs {measuredOf: 18, total: 18}`` next to
    ``runs: 4``, and a reader applying the documented rule got a denominator 4.5x too large.

    Folding per run first makes the unit of the sum and the unit of its denominator the same fact.
    The sum's VALUE is unchanged whenever every finding was measured, which is the normal case.

    ALL-OR-NOTHING per run, matching `waste._sum_durations` ("Never a partial sum") which produced
    these values in the first place: a run with one timed finding and one untimed one has a waste
    wall time of "at least X", and reporting X while counting the run as measured would state the
    lower bound as the figure. It is deliberately the STRICTER rule than `summed`, which is lenient
    for a different reason — that one accumulates across days and must survive one bad day.
    """
    by_run: dict = {}
    for finding in findings:
        run_id = finding.get("run_id")
        wall_ms = finding.get("wall_ms")
        if run_id in by_run and not _is_number(by_run[run_id]):
            continue                       # already unmeasured; nothing can restore it
        if not _is_number(wall_ms):
            by_run[run_id] = UNMEASURED
        else:
            by_run[run_id] = (by_run.get(run_id) or 0) + wall_ms
    return by_run


def waste_to_wire(waste: Optional[dict]) -> dict:
    """The efficiency path's findings, as counts and totals. FIXED size, never trimmed.

    **`detail` MUST NOT be added to this.** Every finding names the tool names it observed, and
    `contract.tool_name_sequence` strips `mcp tools/call <target>___<tool>` down to the bare tool
    name — so a tool name from a USER-REGISTERED MCP server is user-derived data, and nothing cheap
    distinguishes it from a harness builtin like `dispatch_tool`. This bundle is ONE DynamoDB item,
    `actorId="__system__"`, served verbatim to every actor. A named sequence, a session id, or a
    list of run ids here is one tenant's activity shown to all of them. The per-actor bundle IS where
    that detail travels — `actor_bundles` adds a `findings` list to this same block and
    `_waste_finding_to_wire` carries the sequence, because there the reader owns the session. So this
    block is counts only for a reason about its AUDIENCE, not for want of a carrier.

    Three shapes of denominator, all for invariant 1:

    * `runs` — the population these counts were taken over. `runsWithWaste: 12` is meaningless
      without it, and this population changes size run to run.
    * `runsNotScanned` — runs whose logical-call count exceeded `waste._MAX_CALLS_SCANNED`, so the
      repeated-block search did not look at them. Reported, because "looked and found nothing" and
      "did not look" are different facts and only one is good news.
    * `wallMs` as a `_sum_term` rather than a bare number — a finding whose spans carry no duration
      contributes UNMEASURED, and summing the ones that happened to be measured while captioning
      it a total is precisely the lie `_sum_term` exists to prevent. Its ``measuredOf``/``total``
      count RUNS, the convention every other `_sum_term` on this wire follows (`_spend`), which is
      why the sum is folded per run first — see `_wall_ms_by_run`. So each kind's ``wallMs.total``
      equals its own ``runs``, and ``totalWallMs.total`` equals ``runsWithWaste``: the denominator
      is checkable against a sibling on the same object rather than being taken on trust.

    `totalCalls` is the UNION of the calls charged per run, not the sum over findings. The kinds
    are independent observations of the same trajectory and can overlap — an errored call can sit
    inside a repeated block — and on the complete 336 h population 11 of 107 affected runs have
    exactly that overlap, so summing would over-charge by 23 calls out of 834. `byKind` totals stay
    exact per kind, which means they do NOT add up to `totalCalls`; that is the honest arrangement
    of the two, not a rounding error.

    EVERY kind in `waste.WASTE_KINDS` is present with zeros when it found nothing, so a consumer
    can tell "this signal found nothing" from "this producer had no such signal".

    `plainSummary` is `_WASTE_KIND_PROSE`'s sentence for the kind, and it is OMITTED rather than
    filled in when a kind has none — a kind added to the detectors before its sentence exists is
    then visibly unmapped, instead of reaching a media reader as `repeated_sequence`. A consumer
    branching on whether the key is there is correct; one falling back to the kind's own name is
    printing an engine identifier.
    """
    waste = waste or {}
    findings = [f for f in (waste.get("findings") or ()) if isinstance(f, dict)]
    charged_by_run: dict = {}
    for finding in findings:
        charged_by_run.setdefault(finding.get("run_id"), set()).update(
            finding.get("charged") or ())

    def kind_entry(kind: str) -> dict:
        of_kind = [f for f in findings if f.get("kind") == kind]
        per_run = _wall_ms_by_run(of_kind)
        entry: dict = {
            "findings": len(of_kind),
            "runs": len(per_run),
            "calls": sum(f.get("calls") or 0 for f in of_kind),
            "wallMs": _sum_term(per_run.values(), len(per_run)),
        }
        plain = _WASTE_KIND_PROSE.get(kind)
        if plain:
            entry["plainSummary"] = plain
        return entry

    total_per_run = _wall_ms_by_run(findings)
    return {
        "runsWithWaste": len(charged_by_run),
        "runs": waste.get("runs_examined") or 0,
        "runsNotScanned": len(waste.get("runs_not_scanned") or ()),
        "totalCalls": sum(len(charged) for charged in charged_by_run.values()),
        "totalWallMs": _sum_term(total_per_run.values(), len(total_per_run)),
        "byKind": {kind: kind_entry(kind) for kind in WASTE_KINDS},
    }


def suggestion_members(result: dict) -> dict:
    """``{suggestionId: [member run ids]}`` for this run's proposals.

    The wire suggestion no longer carries its member ids, so anything needing them takes this map.
    Keyed on the SUGGESTION id rather than the component id because it has to survive
    `aws_lambda._fit_bundle`: a trim drops whole suggestions and re-derives the Σ map from the
    survivors, and `ComponentRow.health` has to follow the survivors rather than describe the
    population that was trimmed away.

    Not a second definition of the join — `_members_and_evidence` is the single one, and this walks
    the proposals through it.
    """
    patterns_by_shape = _patterns_by_shape(result)
    out: dict = {}
    for proposal in result.get("proposals") or ():
        members, _ = _members_and_evidence(proposal.get("signature") or {}, patterns_by_shape)
        out[proposal["proposal_id"]] = members
    return out


def component_health(suggestions: list, runs_by_id: dict, verdicts_by_id: dict,
                     members_by_suggestion: Optional[dict] = None) -> dict:
    """``{component_id: {"runs": n, "delivered": k, "deliveredOf": m}}`` from the runs that
    attribute to each component.

    A component NO run attributes to is ABSENT from this map, so `row()` leaves its `health`
    `None`. That is the point: `None` means "nothing attributes here, we do not know how it is
    doing", while `{"runs": 0}` would claim we looked and found zero — measured-and-empty
    (`AGENTS.md`, "Absent ≠ empty").

    ``deliveredOf`` is the DENOMINATOR, and it is deliberately not ``runs``: only the attributing
    runs whose `results_delivered` tier actually MEASURED an outcome count towards it. So a row
    reading `runs: 4, deliveredOf: 0` says four runs attribute here and none of them could be
    judged on delivery — the unmeasured state, distinct from `delivered: 0, deliveredOf: 3`,
    which is a measured zero.

    A COUNT PAIR rather than the percentage this used to emit. Two reasons, both measured. The
    populations are single-digit, and `100.0 * 2 / 3` is `66.66666666666667` — a figure the UI
    rendered verbatim, asserting sixteen digits of precision over three runs. And a percentage
    hides its denominator, which here is the one number a reader most needs: it is not the run
    count beside it, and the old wire made that difference explicable only in a tooltip.

    ``members_by_suggestion`` (from `suggestion_members`) supplies which runs attribute where. It
    is a separate argument rather than read off each suggestion because the member ids do not
    travel on the wire — a session id list is per-row exposure on an item every actor reads. A
    caller that omits it gets an empty map, i.e. every row `None`: honest, and the same "we do not
    know" this function has always emitted for a suggestion whose runs left the population.
    """
    members: dict = {}
    for suggestion in suggestions or ():
        component_id = suggestion.get("targetId")
        if not component_id:
            continue
        for run_id in (members_by_suggestion or {}).get(suggestion.get("suggestionId")) or ():
            if run_id in runs_by_id:
                members.setdefault(component_id, set()).add(run_id)

    health: dict = {}
    for component_id, run_ids in members.items():
        outcomes = []
        for run_id in run_ids:
            verdict = verdicts_by_id.get(run_id)
            result = next((r for r in (verdict.evaluator_results or ())
                           if r.name == _DELIVERY_TIER), None) if verdict else None
            if result is not None and result.outcome in (Outcome.PASS, Outcome.FAIL):
                outcomes.append(result.outcome is Outcome.PASS)
        health[component_id] = {
            "runs": len(run_ids),
            "delivered": sum(outcomes),
            "deliveredOf": len(outcomes),
        }
    return health


def components_to_wire(registry: SigmaRegistry, suggestions: list,
                       row_limit: int = 12, runs_by_id: Optional[dict] = None,
                       verdicts_by_id: Optional[dict] = None,
                       members_by_suggestion: Optional[dict] = None) -> dict:
    """The Σ map, bucketed by what the user may do about each component.

    Bucketing is by `disposition` and `editable`, both registry-derived, so the permission story
    on screen is the registry's and not a second opinion. Default-deny falls out of it: a
    component whose mutability was never recorded is not editable, so it lands in `locked`.

    ``runs_by_id``/``verdicts_by_id``/``members_by_suggestion`` are optional because two callers
    re-derive the Σ map from a TRIMMED suggestion list (`aws_lambda._fit_bundle`) and one has no
    population at all. Without them every row's `health` stays `None`, which is the same "we do not
    know" this function has always emitted — never a fabricated zero. All three must be supplied
    together to get a measurement: the member ids no longer ride on the wire suggestion, so
    `members_by_suggestion` is what says which runs attribute where (`suggestion_members`).

    A row now carries the four REGISTRY facts a reader needs to weigh an accusation against the
    component named in it — `type`, `blastRadius`, `knownFailureModes`, `notes` — and those bytes
    come out of the suggestion list's allowance in `aws_lambda._fit_bundle`, so they were measured
    rather than reasoned about. Over the real 80-component registry this function emits 41 rows (the
    editable sections in full, each of the other three buckets capped at ``row_limit``), taking the
    block from 12,342 bytes to 19,067: +6,725, or 1.9% of `aws_lambda._ITEM_BUDGET_BYTES`, and the
    most they can take, because the row count is bounded and the two prose fields are bounded to a
    first sentence each. The block does not grow with the population.
    """
    counts: dict = {}
    for suggestion in suggestions:
        counts[suggestion["targetId"]] = counts.get(suggestion["targetId"], 0) + 1
    health = component_health(suggestions, runs_by_id or {}, verdicts_by_id or {},
                              members_by_suggestion)

    def row(component) -> dict:
        return {
            "componentId": component.component_id,
            "name": component.component_id.replace("-", " "),
            "layer": component.layer,
            # What KIND of component this is, in the registry's own vocabulary (`system_prompt`,
            # `gate`, `budget`). `layer` says where in the harness it sits and this says what it is;
            # a reader deciding whether an accusation is plausible needs both.
            "type": component.type,
            "assetId": None,
            "currentVersion": None,
            "versioned": component.versioned,
            "mutability": component.mutability or "UNRECORDED",
            "sourceLocation": component.source_location,
            # How far a change here reaches, in the registry's words (`single`, `all sessions`).
            # Already published per SUGGESTION as `blastRadius.class`; here it is a property of the
            # component itself, so a component with no suggestion still says how dangerous it is.
            "blastRadius": component.blast_radius,
            # THE WARRANT, on the accused rather than on the accusation. `SigmaRegistry.match`
            # attributes a failure by finding a word-boundary hit inside this text, so it is what
            # makes `attributionBasis: "registry_declared"` checkable from the Σ map as well as from
            # the suggestion. Byte-identical to a suggestion's `provenance.matchedFailureMode` when
            # that suggestion names this component with a declared basis — one definition, in
            # `Component.declared_failure_mode`, so the two cannot be two different truncations.
            # `""` for the 37 of 80 real components that declare none, which is an absence the
            # attribution basis already accounts for.
            "knownFailureModes": component.declared_failure_mode,
            # The other half of what `match` searches: a `keyword_inferred` attribution can hit the
            # id, the type, the source location or these notes, and the first three are already on
            # this row. Bounded to a first sentence (`Component.notes_summary`) — see there for why.
            "notes": component.notes_summary,
            "suggestionCount": counts.get(component.component_id, 0),
            # Absent from the map => no run attributes here => unknown, which is `None`. NOT
            # `{"runs": 0}`, which would claim we measured and found nothing.
            "health": health.get(component.component_id),
        }

    editable = [c for c in registry.all() if c.editable]
    fix_first = [c for c in registry.all()
                 if not c.editable and c.disposition == "code_change"]
    admin = [c for c in registry.all()
             if not c.editable and c.disposition != "code_change" and c.mutability]
    locked = [c for c in registry.all()
              if not c.editable and c.disposition != "code_change" and not c.mutability]

    # A component carrying a suggestion is listed first, so truncation never hides the rows a
    # reader came for.
    def head(components: list) -> list:
        ranked = sorted(components, key=lambda c: -counts.get(c.component_id, 0))
        return [row(c) for c in ranked[:row_limit]]

    yours = []
    for layer, label in _USER_SECTIONS:
        members = [c for c in editable if c.layer == layer]
        yours.append({
            "layer": layer,
            "label": label,
            "editable": True,
            "components": [row(c) for c in sorted(
                members, key=lambda c: -counts.get(c.component_id, 0))],
        })
    # Editable components in a layer the user sections do not name would otherwise vanish.
    named = {layer for layer, _ in _USER_SECTIONS}
    other = [c for c in editable if c.layer not in named]
    if other:
        yours.append({"layer": "other", "label": "Other", "editable": True,
                      "components": [row(c) for c in other]})

    return {
        "yours": yours,
        "fixFirst": {
            "count": len(fix_first), "components": head(fix_first),
            # NOT the registry's `fix_first` permission class, which is narrower and lists
            # defect ids rather than component ids. This bucket is "the fix lives in platform
            # code", which is the grouping a user needs and is true of all of them. Claiming
            # the narrower name would overstate the count.
            "note": "The fix lives in platform code. These need an engineer, not an edit.",
        },
        "admin": {
            "count": len(admin), "components": head(admin),
            "note": "Managed by admins: system prompts, routing and validators. View only.",
        },
        "locked": {
            "count": len(locked), "components": head(locked),
            "note": "Mutability not recorded in the registry. Undecided is not editable, "
                    "default-deny.",
        },
        "registryMeta": {
            "componentCount": len(registry),
            "revalidated": str(registry.meta.get("revalidated") or ""),
            "headSha": str(registry.meta.get("head_sha") or ""),
        },
    }


def to_wire(*, result: dict, runs: list, registry: SigmaRegistry, created_at: str,
            judge_config_hash: str = "", judge_runs: int = 0,
            evidence_sources: Optional[list] = None,
            verdicts: Optional[list] = None) -> dict:
    """A `mine_local.run_pipeline` result as the UI's `ImproveData`.

    ``created_at`` is injected rather than read from the clock so the artifact stays
    byte-reproducible for a given input, which is what lets a test diff two runs.

    ``verdicts`` defaults to `None` so every existing caller keeps working. Absent, the bundle
    still carries a `scorecard` — of an empty population, with each aggregate `UNMEASURED` and
    a `total` of 0. That is deliberate: a consumer must never have to branch on whether the key
    exists, and an empty scorecard says "no scored population here", which is true, whereas a
    missing one says nothing at all.

    ``suggestionsTotal`` is emitted here so the key is never absent, and OVERWRITTEN by both
    producers when they swap in the store's view (`published_suggestions_block`). At this point it
    can only equal ``len(suggestions)``: nothing has been trimmed yet, and a total that is equal to
    its list is still the reading a consumer needs — "none dropped" rather than "not recorded".

    ``population.sessions`` and ``spend.usageShortfall`` come from the RESULT rather than from
    ``runs``, because both describe evidence that never became a run: the sessions the cap and the
    span floor excluded, and the token spend the harvest's row cap could not host. A fold over
    ``runs`` cannot see either by construction.
    """
    runs_by_id = {run.run_id: run for run in runs}
    verdicts = list(verdicts or ())
    verdicts_by_id = {verdict.run_id: verdict for verdict in verdicts}
    sources = evidence_sources if evidence_sources is not None else []
    patterns_by_shape = _patterns_by_shape(result)

    suggestions = [
        suggestion_to_wire(p, created_at=created_at, registry=registry, runs_by_id=runs_by_id,
                           judge_config_hash=judge_config_hash, judge_runs=judge_runs,
                           evidence_sources=sources, patterns_by_shape=patterns_by_shape)
        for p in result.get("proposals") or ()
    ]

    return {
        "suggestions": suggestions,
        "suggestionsTotal": len(suggestions),
        "components": components_to_wire(registry, suggestions, runs_by_id=runs_by_id,
                                         verdicts_by_id=verdicts_by_id,
                                         members_by_suggestion=suggestion_members(result)),
        "scorecard": scorecard_to_wire(runs, verdicts,
                                       discovery=result.get("population_discovery"),
                                       usage_shortfall=result.get("usage_shortfall")),
        # The efficiency path. Counts and totals ONLY — see `waste_to_wire`, which explains why
        # the repeated tool sequence may not travel on this item.
        "waste": waste_to_wire(result.get("waste")),
        "patterns": {
            "admitted": [
                _pattern_to_wire(pattern, _shape_prose(pattern.get("shape") or ()),
                                 pattern.get("member_run_ids") or ())
                for pattern in result.get("patterns") or ()
            ],
            "residue": [residue_to_wire(r) for r in result.get("residue") or ()],
            "evidenceGaps": [residue_to_wire(g) for g in result.get("evidence_gaps") or ()],
            # The platform reading is looked up on the RUN, so a divert says WHAT the platform was
            # doing and not only that it was doing something. A run no longer in the population
            # reaches `platform_to_wire` as the UNKNOWN sentinel, never as healthy.
            "environment": [
                environment_to_wire(e, _infra_health_of(e, runs_by_id))
                for e in result.get("environment") or ()
            ],
            # Admitted patterns that no Σ component owns. ALWAYS emitted, empty list included:
            # "the proposer found an owner for every pattern" and "this bundle predates the
            # key" are different facts, and only the consumer can tell them apart if the
            # producer never omits it.
            "unattributed": [
                _unattributed_to_wire(u) for u in result.get("unattributed") or ()
            ],
        },
    }


# ---------------------------------------------------------------------------
# The per-actor bundle: one item per actor, served only to that actor
# ---------------------------------------------------------------------------


def _tier_row(verdict: Verdict, name: str) -> dict:
    """One goal tier for the run's owner: the score, the outcome, and the judge's sentence.

    The sentence is the whole point. `Verdict.reason` names WHICH gating check failed and the
    global `scorecard.outcomes` counts that by evaluator name, but neither can say why the judge
    thought so — it is fed the goal and the response and asked for one sentence grading the
    session, so its `explanation` restates the request by construction and cannot travel on the
    shared item. Here the reader owns the session, so it can.

    A tier the verdict has no result for is NOT_APPLICABLE with an UNMEASURED score, never a 0 and
    never PASS by omission.
    """
    result = next((r for r in (verdict.evaluator_results or ()) if r.name == name), None)
    if result is None:
        return {"score": _measured(None), "outcome": Outcome.NOT_APPLICABLE.value,
                "explanation": ""}
    return {"score": _measured(result.score), "outcome": result.outcome.value,
            "explanation": result.explanation or ""}


def summed(values: Iterable) -> object:
    """Sum of the MEASURED values, or the sentinel when none of them was measured. ONE rule.

    The same rule `_sum_term` applies to the population, minus the denominator it has nowhere to go in
    the places this is used: sum what was measured, and spell "nobody measured this" as the sentinel
    rather than as a ``0`` that reads as free or instant. It is deliberately not the STRICTER rule
    ("sentinel unless every contributor measured it") — that would blank a whole request's tokens over
    one sub-session with no usage record, and a figure a reader accumulates over months must not be
    destroyed by one such day. Measured on the live population the distinction is currently empty:
    usage is recorded for 25 of 25 runs.

    Takes VALUES rather than runs so the same rule serves both sides of the wire: `_resource_sum`
    feeds it `Run.resources` entries, whose absence is the sentinel OBJECT, and `aws_lambda` feeds it
    day-row fields read back out of DynamoDB, where only the sentinel's NAME survives. `_is_number`
    admits neither, so one function is correct for both and there is no second copy to drift.
    """
    measured = [value for value in values if _is_number(value)]
    return _measured(sum(measured) if measured else UNMEASURED)


def _resource_sum(runs: Iterable, key: str) -> object:
    """One spend term summed over the runs that make up a request. `summed`'s rule, over resources.

    Called with a single run for a request that spawned nothing, where it reduces to exactly what the
    field was before: one value, or its sentinel.
    """
    return summed((getattr(run, "resources", None) or {}).get(key, UNMEASURED) for run in runs)


def _run_models(runs: Iterable) -> object:
    """The model ids one REQUEST's tokens went to, or the sentinel when nothing named any.

    Takes the runs that make the request up — the session the user started plus any plan sub-session
    it spawned — and UNIONS their rosters rather than concatenating them. A request and its plan steps
    normally run on the same model, and naming that model once per step would read as several models.
    Order is first-seen, so the row is stable across two producer runs over the same population.

    An empty roster reaches the wire as UNMEASURED and never as ``[]``. Both spellings of absence
    arrive here as the same empty tuple — `build_run._UNMEASURED_USAGE` carries ``models: ()`` for a
    session with no usage record at all, and a measured record whose ``byModel`` key was missing
    yields ``()`` too — so an empty list would be the one value on this row that reads as a
    measurement ("this run used no model") where every sibling spells absence as a sentinel. A run
    that really did spend tokens names at least one id: `harvest_live._usage_by_session` substitutes
    ``"UNKNOWN"`` when a usage row carries no model attribute rather than dropping the row.

    A LIST of ids and not a count, unlike the population's ``modelsSeen``. The count is what makes a
    single-rate cost indefensible over a population; on one run the reader is asking WHICH model
    their own session spent on, and two ids is a list a tile can print.

    UNBOUNDED, unlike ``_SPEND_MODELS_NAMED`` one level up, and that is a judgement rather than an
    oversight. This is one request's roster, not the harness's: measured on the live population every
    session names one or two ids, and a bound would have to be a number nothing observed. It is also
    the only term on this row that can grow, so the worst case was measured rather than reasoned
    about: twenty ids on every row make a 1,883-byte row and take the runs that fit one item from 304
    to 185, which `_fit_actor_bundle` absorbs by dropping older rows rather than failing the write.
    The union is what keeps the sub-sessions from widening it — six steps on one model stay one id.
    """
    named: list = []
    sentinel = UNMEASURED
    for run in runs:
        models = (getattr(run, "resources", None) or {}).get("models", UNMEASURED)
        if isinstance(models, _Sentinel):
            sentinel = models
            continue
        for model in models if isinstance(models, (list, tuple)) else ():
            if isinstance(model, str) and model and model not in named:
                named.append(model)
    return named if named else _measured(sentinel)


def _run_row(run: Run, verdict: Verdict, children: Iterable = ()) -> dict:
    """One of this actor's runs. The row `ImproveRun` describes and nothing has ever emitted.

    Carries what the loop actually measured and nothing it did not: no span count, no
    AgentCore reference score, no harness snapshot — all three are unsourced, and a row with
    plausible-looking blanks in them is worse than a shorter row.

    ``workloadClass`` rides along because a verdict cannot be read without it: which checks gated
    this run is per class, so a reader who cannot see the class cannot tell why a tier is
    NOT_APPLICABLE (`Verdict.workload_class` carries the same argument).

    ``spend`` PROJECTS eight of the twelve terms `build_run.usage_for` measures per session, and the
    four it adds are the four that let a PERSONAL figure answer a question the population scorecard
    already answers. They were measured and dropped here: the four categories are what make cache
    reuse and context-vs-generated readable at all (on the live population cache reads are 68% of
    every token, so one undifferentiated total says nothing about either), ``models`` is what says
    which model a run's own spend went to, and the two ``costTokens`` counts are the only thing that
    can tell a reader their own bill is SHORT — pricing skips a model with no rate row rather than
    failing the run, so a per-run cost with no counts beside it is a silent lower bound.

    The two cost counts sit BESIDE ``cost`` rather than nested inside it, unlike the population's
    ``spend.cost.tokensPriced``. There ``cost`` is a sum with denominators and has an object to hang
    them on; here it is one run's bare amount, and turning it into an object to carry two counts
    would change the type of the field every existing reader branches on.

    Still NO per-model split (`test_the_per_model_split_is_absent_from_the_per_actor_run_row`): four
    terms per model would multiply the largest list in the trimmed bundle, and the question it
    answers — which model a WORKLOAD concentrates on — is a population question. Measured on the two
    live staging rows, the four additions cost 229 bytes on a row of 917 (25%), which drops the runs
    that fit one `aws_lambda._ITEM_BUDGET_BYTES` item from 380 to 304. `_fit_actor_bundle` drops
    oldest-first and `runsTotal` keeps the true count, and the actor these figures come from has 2.

    ``spend`` COVERS THE WHOLE REQUEST, this session plus every plan sub-session it spawned. That is
    the counting rule for this surface and it is a choice between two wrong alternatives. Counting a
    sub-session as a run of its own inflates volume — measured 2.1x over 7 days (91 sub-sessions
    against 44 parents), up to 6x for one request — and a sub-session has no owner to count it under
    anyway: 0 of 357 non-conversational sessions resolved to an actor, so it could only ever borrow
    its parent's, at which point the parent is already the unit. Dropping them instead under-reports
    real spend: sub-sessions are 3.0% of the tokens in a measured 24h harvest (498,745 of 16,504,245).
    So one row per request, its terms the SUM over its own session and its children.

    ``latencyMs`` is summed like the rest, with a measured overlap acknowledged rather than hidden:
    the parent holds an ``InvokeAgentRuntime`` span for each dispatch, so a few seconds of each child
    is inside the parent's busy time too. Measured on a real 4-way fan-out, the dispatch spans are
    6-9 s against a parent busy time of 4,526,706 ms (0.0002%), while NOT summing would lose 100% of
    each child's 136,000-361,000 ms. The double count is the far smaller error of the two.

    ``runsTotal`` beside this list counts REQUESTS, so it does not move when a request fans out. How
    wide the fan-out was is a real signal and it is reported in the producer's log summary rather than
    here, because this row's shape is what the surface is coded against.
    """
    # This session first, so `_run_models`' first-seen order names the request's own model before any
    # model a sub-session was routed to.
    contributors = [run, *children]
    return {
        "runId": run.run_id,
        "workloadClass": verdict.workload_class or "",
        "window": window_to_wire(getattr(run, "window", UNKNOWN)),
        "verdict": {"overall": verdict.overall.value, "reason": verdict.reason or ""},
        "tiers": {"dispatch": _tier_row(verdict, _DISPATCH_TIER),
                  "delivery": _tier_row(verdict, _DELIVERY_TIER)},
        "spend": {
            "latencyMs": _resource_sum(contributors, "latency_ms"),
            "tokens": _resource_sum(contributors, "tokens"),
            "cost": _resource_sum(contributors, "cost"),
            "llmCalls": _resource_sum(contributors, "llm_calls"),
            # `_TOKEN_CATEGORY_TERMS`, the same mapping the population total and the per-model split
            # read, so a category cannot come to be called one thing per run and another in the
            # aggregate over those runs.
            "byCategory": {wire: _resource_sum(contributors, key)
                           for wire, key in _TOKEN_CATEGORY_TERMS},
            "models": _run_models(contributors),
            # UNMEASURED, never 0, when pricing was never ATTEMPTED on this run — `0 priced, 0
            # unpriced` would read as an attempt that found nothing to skip. `build_run._cost_usd`
            # returns a real 0 priced with a positive unpriced count for a run it tried and could
            # not price, and that pair is a measurement worth publishing.
            "costTokensPriced": _resource_sum(contributors, "cost_tokens_priced"),
            "costTokensUnpriced": _resource_sum(contributors, "cost_tokens_unpriced"),
        },
    }


def _newest_first(run: Run) -> tuple:
    """Sort key putting the most recent run first, windowless runs last, ties broken by id.

    Ordered rather than left in discovery order because the byte trim drops from the TAIL, so the
    order decides what an over-large bundle loses. Compared as datetimes, never as ISO strings —
    `RunWindow` normalises its bounds but `parse_utc` exists because this codebase has already been
    bitten by `'…Z' > '…+00:00'`.
    """
    window = getattr(run, "window", UNKNOWN)
    if isinstance(window, RunWindow):
        return (0, -window.start_dt().timestamp(), run.run_id)
    return (1, 0.0, run.run_id)


#: The workload class whose sessions are PART of another run rather than runs of their own. Their
#: spend is real and is folded into the request that spawned them (`_run_row`); they never get a row,
#: a verdict tier or a place in any count of runs.
_SUB_SESSION_WORKLOAD_CLASS = WorkloadClass.PLAN_NODE.value


def _plan_children(runs: list, verdicts: list, with_rows: set) -> tuple:
    """``({parent run id: [child Run]}, [children whose parent got no row])``.

    The parent is read off `Run.parent_run_id`, which the harvest resolves from the OTEL trace the
    two sessions share. It is NEVER re-derived from the id: a sub-session id's suffix is unique per
    child, so parsing it gave every sibling a different "parent" and any grouping on it was a no-op.

    The second half of the return is a boundary this cannot close and must not hide. The population
    is capped at ``IMPROVE_LIMIT`` and `observe.population.discover_sessions` orders it NEWEST FIRST,
    while a child's first span is by definition later than its parent's — so a cap that BINDS
    preferentially keeps children and drops the parents they belong to. Measured on one real 24h
    harvest, replayed at two caps: 36 sessions discoverable, and at a cap of 25 that admitted 11
    sub-sessions of which 1 had its parent outside, while at the deployed 60 all 36 are admitted, 16
    are sub-sessions, and NONE has a parent outside. So the state is currently unreachable on this
    account's traffic and this branch stays anyway — the cap is a bound on a population nothing
    limits, so a busier day puts it back.

    The harvest WINDOW is not the boundary and no code here should imply it is — the parent's linking
    span sits in the same trace as the child, so every sub-session in both replays had its parent
    present and discoverable in the same harvest.

    A child in that state still knows its owner (`build_run` inherits it from the parent's
    conversation), so its spend reaches the right actor's day rows. What it cannot reach is a run row,
    because the request it belongs to has none.
    """
    class_by_id = {verdict.run_id: verdict.workload_class for verdict in verdicts or ()}
    by_parent: dict = {}
    orphaned: list = []
    for run in runs or ():
        if class_by_id.get(run.run_id) != _SUB_SESSION_WORKLOAD_CLASS:
            continue
        parent = getattr(run, "parent_run_id", UNKNOWN)
        if not isinstance(parent, str) or not parent:
            continue
        if parent in with_rows:
            by_parent.setdefault(parent, []).append(run)
        else:
            orphaned.append(run)
    return by_parent, orphaned


def _run_date(run: Run) -> Optional[str]:
    """The ``YYYY-MM-DD`` a run's window starts on, or ``None`` when nothing stamped one.

    From the PARSED instant and never by slicing the ISO string, which is invariant 4 in this package:
    the harvest mixes ``…Z`` and ``…+00:00`` spellings, and a slice happens to work only until a
    source starts writing an offset other than UTC.

    ``None`` rather than a guess. A run that cannot be placed on a time axis cannot be placed in a
    day row either, and the count of such runs is reported instead of a date being invented for them.
    """
    window = getattr(run, "window", UNKNOWN)
    if not isinstance(window, RunWindow):
        return None
    return window.start_dt().strftime("%Y-%m-%d")


#: ``wire name: Run.resources key`` for every scalar spend term on a day row. Named once and read by
#: both the sums and their coverage counts, so a term can never be summed without its denominator or
#: counted without its sum. `_TOKEN_CATEGORY_TERMS` does the same job for the nested categories.
_DAY_SPEND_TERMS = (
    ("tokens", "tokens"),
    ("llmCalls", "llm_calls"),
    ("latencyMs", "latency_ms"),
    ("cost", "cost"),
    ("costTokensPriced", "cost_tokens_priced"),
    ("costTokensUnpriced", "cost_tokens_unpriced"),
)


def _measured_count(runs: Iterable, key: str) -> int:
    """How many of these runs RECORDED this term. Always a number, and ``0`` is a measurement.

    The numerator `summed` needs to be readable: it sums what was measured and says nothing about how
    much it skipped, so this is what says so. `_is_number` is the same admission test `summed` uses, so
    the count and the sum cannot disagree about which values were real.
    """
    return sum(1 for run in runs
               if _is_number((getattr(run, "resources", None) or {}).get(key)))


def _day_row(date: str, requests: list, contributors: list, verdicts_by_id: dict) -> dict:
    """One DAY of an actor's activity: what they ran that day, whether it worked, and what it cost.

    The durable row. Everything else in a per-actor bundle is recomputed from a 24h harvest and
    replaced wholesale every night, so a reader who ran nothing yesterday has no figures at all; these
    rows are read off the item before it is overwritten and carried forward, which is what lets a
    cumulative total exist. `aws_lambda` owns that carry-forward — this only builds the row.

    ``runs`` counts REQUESTS, so it does not move when one request fans out into six plan steps.
    ``contributors`` is the wider list: every session whose spend belongs to that day, sub-sessions
    included. So ``tokens / runs`` is tokens per request, which is the figure a reader means.

    ``pass``/``fail``/``unscored`` PARTITION ``runs`` — the three always add to it — and without them
    this row is a spend ledger that cannot say whether any of the spend worked. Same spelling as the
    population scorecard's ``population: {total, pass, fail, unscored}`` block, via the same counting
    (`contract.verdict_counts`) and the same projection (`_population`), so a reader meets ONE
    vocabulary across both scopes and neither can drift about what a count means. ``total`` is dropped
    because ``runs`` beside them already IS it.

    COUNTS AND NOT A RATE, deliberately. Counts fold across days by addition, so a trailing window and
    an all-time total are the same sum over the same rows as every spend term. A rate cannot be summed:
    it needs its weight carried beside it, and it cannot be recovered from the row if that weight is
    ever wrong. So the division happens at read time, over whatever span was asked for. The population
    row stores the rate instead, which is why folding a month of those needs `aws_lambda._weighted` to
    undo the division one day at a time.

    A FANNED-OUT REQUEST TAKES ITS PARENT'S VERDICT. Not a new rule — it is the counting rule this row
    already applies to spend, read the other way round: the unit is the request, so a plan sub-session
    contributes its tokens to the day and no outcome of its own. Measured on the live population the
    children are overwhelmingly UNSCORED (74 of 88), so counting them as outcomes would report a day
    whose requests were scored as mostly unmeasurable.

    ``unscored`` is a THIRD count and never folds into either of the others, which is the line
    `evaluate.gate` and the scorecard both already hold: a gating evaluator that could not measure says
    something about the evidence and nothing about the agent. Could-not-measure is not could-not-pass.

    The verdict lookup is unguarded, like the one `actor_bundles` makes one line away for the run rows:
    every owned run has a verdict by construction, so a missing one is a producer bug and a
    ``KeyError`` is the right way for it to surface. Defaulting it to UNSCORED would silently break the
    partition instead.

    ``coverage`` IS THE DENOMINATOR, and the row is unreadable without it. The sums here are lenient
    — see `summed` — which is the same rule the population's ``spend`` uses and for the same reason,
    and `_sum_term`'s docstring is explicit about what that rule costs when it ships alone: summing the
    runs whose tokens happened to be recorded and captioning it "spend" can understate the truth by
    orders of magnitude while looking authoritative. Measured on the harvest behind these figures,
    tokens are recorded on 354 of 553 sessions, so a total captioned only by the days it spans would
    read as complete over a figure covering two thirds.

    PER TERM, not one count for the block, because coverage differs BETWEEN terms: ``latencyMs`` is
    summed from spans while every token term comes from the usage record, so a session can be measured
    for one and not the other. One number would caption both wrongly.

    It is the population's ``measuredOf``/``total`` pair, factored rather than reshaped. There each term
    is an object carrying its own pair; here the pair is split out into this block and the terms stay
    scalars, because turning ``tokens`` into an object would change the type of every field a consumer
    already branches on. ``coverage.tokens of coverage.total`` reads exactly as
    ``spend.tokens.measuredOf of spend.tokens.total``, and ``total`` is named once because it is one
    fact about the day rather than one per term.

    Both-or-neither, per day. A day whose sessions recorded no usage contributes ``0`` to each term and
    its REAL session count to ``total`` — a measured zero, not an absence. The pair can never be
    ``0 of 0``, which would read as complete over nothing.

    Every count sums across days, which is what makes a trailing period or an all-time total caption
    itself the same way the scorecard does: ``measuredOf`` is the sum of the per-day counts and
    ``total`` the sum of the per-day session counts.

    ``latencyMs`` and the token terms sum by the same rule as a run row, and a term nothing measured
    is the sentinel rather than a ``0`` — see `_resource_sum` for why the lenient sum is the right one
    for a figure that accumulates.

    Same field NAMES as a run row's ``spend`` block, deliberately: the cumulative tiles and the
    evidence rows are read side by side, and a term called one thing per run and another per day would
    invite exactly the reconciliation a reader cannot perform.

    The two ``costTokens`` counts travel for the reason they travel on a run row, and it is sharper
    here: pricing SKIPS a model with no rate row rather than failing the run, so a cost with no counts
    beside it is a silent lower bound — and a cumulative total accumulates that shortfall for months
    without ever saying so. They are the only thing on this row that can tell a reader their own bill
    is SHORT. Measured cost, unpriced count and priced count are three different facts and only the
    three together can be read.
    """
    outcome = _population(verdict_counts(verdicts_by_id[run.run_id] for run in requests))
    return {
        "date": date,
        "runs": len(requests),
        "pass": outcome["pass"],
        "fail": outcome["fail"],
        "unscored": outcome["unscored"],
        "tokens": _resource_sum(contributors, "tokens"),
        "llmCalls": _resource_sum(contributors, "llm_calls"),
        "latencyMs": _resource_sum(contributors, "latency_ms"),
        "cost": _resource_sum(contributors, "cost"),
        "costTokensPriced": _resource_sum(contributors, "cost_tokens_priced"),
        "costTokensUnpriced": _resource_sum(contributors, "cost_tokens_unpriced"),
        "byCategory": {wire: _resource_sum(contributors, key)
                       for wire, key in _TOKEN_CATEGORY_TERMS},
        "models": _run_models(contributors),
        "coverage": {
            # SESSIONS and not requests: it is the denominator of the sums beside it, and those are
            # summed over every session whose spend lands on this day, sub-sessions included.
            "total": len(contributors),
            **{wire: _measured_count(contributors, key) for wire, key in _DAY_SPEND_TERMS},
            "byCategory": {wire: _measured_count(contributors, key)
                           for wire, key in _TOKEN_CATEGORY_TERMS},
        },
    }


def _day_rows(requests: list, children_by_parent: dict, orphans: list,
              verdicts_by_id: dict) -> tuple:
    """``([day rows oldest-first], runs that carried no date)`` for ONE actor.

    Oldest-first because that is what licenses every bound downstream: "keep the newest N" is only
    "drop the oldest" when the newest are at the end.

    A request's sub-sessions are dated by the REQUEST, not by their own windows, so a plan step that
    ran past midnight lands on the day of the request it belongs to. Two reasons: the row is about
    requests, and dating a child separately would produce a day row with ``runs: 0`` beside real
    tokens, which reads as a fabricated row. An ORPHAN child — one whose parent is outside the
    population, see `_plan_children` — has no request to be dated by and so falls back to its own day.

    ``verdicts_by_id`` is threaded through for the outcome counts and covers the requests only. An
    orphan child never appears in one: it is not a request, so it contributes spend to the day and no
    outcome — the same rule a child WITH a request follows, so neither case needs handling of its own.
    """
    by_date: dict = {}
    undated = 0
    for run in requests:
        date = _run_date(run)
        if date is None:
            undated += 1
            continue
        requests_on_day, contributors = by_date.setdefault(date, ([], []))
        requests_on_day.append(run)
        contributors.append(run)
        contributors.extend(children_by_parent.get(run.run_id) or ())
    for child in orphans:
        date = _run_date(child)
        if date is None:
            undated += 1
            continue
        by_date.setdefault(date, ([], []))[1].append(child)
    rows = [_day_row(date, *by_date[date], verdicts_by_id=verdicts_by_id)
            for date in sorted(by_date)]
    return rows, undated


def _waste_finding_to_wire(finding: dict) -> dict:
    """One waste finding WITH its detail — the named tool sequence.

    `waste_to_wire` explains why `detail` may not reach the global item: it names tool names, and
    `contract.tool_name_sequence` strips `mcp tools/call <target>___<tool>` to the bare name, so a
    tool from a user-registered MCP server is user-derived and nothing cheap tells it apart from a
    harness builtin. That argument is about WHO READS IT, and here the reader owns the session, so
    the sequence travels. Its length is bounded by `waste._MAX_DETAIL_NAMES`.

    ``confidence`` rides along because a finding without it invites the wrong reading: every kind
    says out loud what it did NOT establish (spans carry no tool arguments, so a repeat over
    different inputs is indistinguishable from a redo).
    """
    return {
        "runId": finding.get("run_id") or "",
        "kind": finding.get("kind") or "",
        "calls": int(finding.get("calls") or 0),
        "wallMs": finding.get("wall_ms"),
        "occurrences": int(finding.get("occurrences") or 1),
        "detail": finding.get("detail") or "",
        "confidence": finding.get("confidence") or "",
    }


def actor_bundles(*, result: dict, runs: list, verdicts: Optional[list] = None,
                  stats: Optional[dict] = None) -> dict:
    """``{actor_id: bundle}`` — the evidence half of the surface, partitioned by owner.

    Everything here is computed by the same pipeline run that produces the global bundle and was
    previously dropped on the way out: the judge's per-tier sentence, the per-run rows
    `ImproveRun` has always described, the facet evidence behind each suggestion, the tool
    sequences behind each waste finding, and the session ids. None of it can travel on the shared
    `__system__` item, so the store grows a second key rather than the mapper growing a redaction
    pass.

    An actor with no `_OWNED_WORKLOAD_CLASS` run in this population gets NO bundle — not an empty
    one. That is the normal state, not an edge case: the producer harvests a bounded window, so on
    any given day most actors have nothing in it, and the consumer must serve the global bundle
    alone rather than blank the surface (`capability_api.improve_dto.with_actor_bundle`).

    Every id in a bundle is one of that actor's own. A pattern's member list is intersected with
    their runs before it is written, so a suggestion whose evidence belongs to somebody else has no
    entry at all here — the global suggestion still carries the claim and its `evidenceQuality`,
    whose `caveats` already say "no run in the current dataset attributes here" for the same
    situation one level up. `residue_to_wire` records the one field that reaches nobody:
    `pass_hit_runs` names PASSING runs that may belong to other tenants, so only its count travels.

    Each bundle also carries ``history``: THIS RUN's per-day rows for that actor. They are the fresh
    half only — `aws_lambda` reads the rows the previous item carried and merges them, because the
    write is an unconditional PutItem and this mapper has no view of the store.

    One actor CAN get a bundle with no run in it: the one whose only presence in this population is a
    plan sub-session whose request fell outside `IMPROVE_LIMIT` (see `_plan_children`). Their tokens
    were really spent, so the day row is real, and it carries ``runs: 0`` because no request of their
    own is here to count. Dropping them instead would be a silent subtraction from a cumulative figure.

    ``stats``, when given, is filled with counts the log summary reports and the wire deliberately does
    not: how many requests fanned out, into how many sub-sessions, how many of those had no request in
    the population, and how many runs carried no window. All four are invisible otherwise, and the
    third is what says whether ``IMPROVE_LIMIT`` is cutting requests away from their own plan steps.
    """
    verdicts = list(verdicts or ())
    verdicts_by_id = {verdict.run_id: verdict for verdict in verdicts}
    by_actor, _ = _owner_split(runs, verdicts)
    # Every run that will get a row, across all actors — which is what decides whether a sub-session
    # can be folded into its request or is left holding spend with no row to belong to.
    with_rows = {run.run_id for actor_runs in by_actor.values() for run in actor_runs}
    children_by_parent, orphaned_children = _plan_children(runs, verdicts, with_rows)
    orphans_by_actor: dict = {}
    for child in orphaned_children:
        actor = getattr(child, "actor_id", UNKNOWN)
        if isinstance(actor, str) and actor:
            orphans_by_actor.setdefault(actor, []).append(child)
    undated_runs = 0
    patterns_by_shape = _patterns_by_shape(result)
    waste = result.get("waste") or {}
    waste_findings = [f for f in (waste.get("findings") or ()) if isinstance(f, dict)]
    not_scanned = list(waste.get("runs_not_scanned") or ())

    runs_by_id = {run.run_id: run for run in runs or ()}

    def diverted(key: str, mine: set) -> list:
        return [_residue_evidence(e) for e in result.get(key) or ()
                if e.get("run_id") in mine]

    def diverted_by_platform(mine: set) -> list:
        """`environment` only. The owner gets the same structured platform block the admin surface
        gets, because "your run was diverted" is not readable without what the platform was doing."""
        return [_environment_evidence(e, _infra_health_of(e, runs_by_id))
                for e in result.get("environment") or ()
                if e.get("run_id") in mine]

    bundles: dict = {}
    for actor_id in list(by_actor) + [a for a in orphans_by_actor if a not in by_actor]:
        actor_runs = by_actor.get(actor_id) or []
        mine = {run.run_id for run in actor_runs}

        evidence: dict = {}
        for proposal in result.get("proposals") or ():
            signature = proposal.get("signature") or {}
            members, evidence_by_run = _members_and_evidence(signature, patterns_by_shape)
            theirs = [m for m in members if m in mine]
            if theirs:
                evidence[proposal["proposal_id"]] = _pattern_evidence(
                    signature.get("shape") or (), theirs, evidence_by_run)

        admitted = []
        for pattern in result.get("patterns") or ():
            theirs = [m for m in (pattern.get("member_run_ids") or ()) if m in mine]
            if theirs:
                admitted.append(_pattern_evidence(pattern.get("shape") or (), theirs,
                                                  pattern.get("evidence_by_run") or {}))
        unattributed = []
        for entry in result.get("unattributed") or ():
            theirs = [m for m in (entry.get("member_run_ids") or ()) if m in mine]
            if theirs:
                # No `evidence_by_run`: `propose_all` does not carry facet evidence onto an
                # unowned record, so `rootCauseHypo` is honestly empty rather than back-filled
                # from the shape prose the global row already shows.
                unattributed.append(_pattern_evidence(entry.get("shape") or (), theirs, {}))

        theirs_findings = [f for f in waste_findings if f.get("run_id") in mine]
        # `waste_to_wire` again, over this actor's slice: the aggregation rule is one fact and the
        # per-actor totals must agree with the global ones about how a kind is counted. Only the
        # `findings` list is added, which is the half the global item may not carry.
        actor_waste = waste_to_wire({
            "findings": theirs_findings,
            "runs_examined": len(actor_runs),
            "runs_not_scanned": [r for r in not_scanned if r in mine],
        })
        actor_waste["findings"] = [_waste_finding_to_wire(f) for f in theirs_findings]

        rows, undated = _day_rows(actor_runs, children_by_parent,
                                  orphans_by_actor.get(actor_id) or [], verdicts_by_id)
        undated_runs += undated

        bundles[actor_id] = {
            # Every owned run has a verdict by construction: `_owner_split` reads the class off
            # one, so a run without a verdict never reaches here.
            "runs": [_run_row(run, verdicts_by_id[run.run_id],
                              children_by_parent.get(run.run_id) or ())
                     for run in sorted(actor_runs, key=_newest_first)],
            # The actor's true REQUEST count, so a trimmed `runs` list is visible as a trim rather
            # than as a smaller population (invariant 1: every count carries its denominator). It
            # does not move when a request fans out — a plan's steps are part of it, not extra runs.
            "runsTotal": len(actor_runs),
            # This run's day rows. `aws_lambda` merges them with the ones already on the item.
            "history": rows,
            "evidence": evidence,
            "waste": actor_waste,
            "patterns": {
                "admitted": admitted,
                "residue": diverted("residue", mine),
                "evidenceGaps": diverted("evidence_gaps", mine),
                "environment": diverted_by_platform(mine),
                "unattributed": unattributed,
            },
        }
    if stats is not None:
        stats.update({
            "fanned_out_requests": len(children_by_parent),
            "sub_sessions_folded": sum(len(kids) for kids in children_by_parent.values()),
            "sub_sessions_without_a_request": len(orphaned_children),
            "runs_with_no_window": undated_runs,
        })
    return bundles
