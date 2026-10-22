"""The wire mapping: field names, and the fields nothing can source.

`ui/src/improve-api.ts` describes the wire and this module produces it, so a rename on either
side has to break something. These tests are that something: they assert the emitted keys
literally, which is the only way a Python module and a TypeScript interface can be kept in step
without a shared schema.

The other half is the honest-absence contract. Three groups of field have no source, and each
has a required shape rather than a plausible-looking value: `status` is always `open` because
nothing stores a decision, the Lab-dependent affordances are `False` so a card withholds an
action instead of offering one that would fail, and sentinels serialize as their NAME so the UI
renders them hatched instead of blank.

The third half — and the one with teeth — is the CONTENT rule, which is now a rule about WHICH OF
TWO ITEMS a value lands on. The global bundle is one DynamoDB item served to every actor, so a
string derived from user input or model prose, or a session id, would put one tenant's work in
front of all the others; a per-actor bundle carries exactly that and is served only to the holder
of that actor's token, so the guard there is that nothing of anybody ELSE's is in it. Four tests
carry that weight and matter more than any other assertion in this file:

    global      `test_the_global_scorecard_contains_no_user_derived_text`
                `test_the_global_unattributed_bucket_contains_no_user_derived_text`
                `test_the_global_bundle_carries_no_session_id_and_no_facet_evidence`
    per-actor   `test_an_actor_bundle_carries_no_other_actors_evidence`
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import (  # noqa: E402
    UNKNOWN, UNMEASURED, EvaluatorResult, FailureSignature, InfraHealth, InfraState, Outcome,
    Overall, Run, RunWindow, UtilityVector, Verdict,
)
from improve.dto import (  # noqa: E402
    _SPEND_MODELS_NAMED,
    _members_and_evidence, _patterns_by_shape, _residue_evidence, actor_bundles, component_health,
    components_to_wire, evidence_quality, platform_to_wire, residue_to_wire, scorecard_to_wire,
    publishable_suggestion, publishable_suggestions, published_suggestions_block,
    suggestion_members, suggestion_to_wire, to_wire, waste_to_wire, window_to_wire,
    withheld_fields,
)
from improve.evaluate import DEFAULT_GATING  # noqa: E402
from improve.judge import DEFAULT_PASS_THRESHOLD  # noqa: E402
from improve.loop import load_golden  # noqa: E402
from improve.mine_local import _jsonable  # noqa: E402
from improve.mine_local import verdict_from_recorded  # noqa: E402
from improve.propose import propose_all  # noqa: E402
from improve.registry import SigmaRegistry, default_registry_path  # noqa: E402
from improve.waste import WASTE_KINDS, find_waste  # noqa: E402

from improve_fixtures import fanout_harvest, real_plan, real_trajectory  # noqa: E402

# Field sets copied from `ui/src/improve-api.ts`. Deliberately literal: a set built from the
# emitter would agree with itself no matter what it emitted.
#
# `_NOT_YET_TYPED` marks keys the PRODUCER emits that `improve-api.ts` does not declare yet. They are
# listed in their own set rather than merged into the sets above, so the boundary between "the two
# sides agree" and "the producer is ahead" stays visible in this file — and so removing one when the
# TypeScript catches up is a deletion from one place. `scripts/improve-wire-census.py` reports the
# same gap from the other direction and exits non-zero on it.
SUGGESTION_KEYS = {
    "suggestionId", "createdAt", "status", "targetType", "targetId", "targetName",
    "targetLocation", "baseVersion", "mutability", "operation", "summary", "evidenceSummary",
    "pattern", "candidates", "singleCandidateForced", "blastRadius", "affordances",
    "evidenceQuality", "provenance",
}
# `evidenceHeadline` is OPTIONAL for the reason `plainReason` and `plainSummary` are: the builder
# omits it when the shape's leading facet has no headline, so a consumer branching on whether the
# key is there is correct and nothing falls back to a facet name. It is in its own set rather than
# in `SUGGESTION_KEYS` above because that set is the TypeScript `Suggestion` interface, which does
# not declare this field yet — see `_NOT_YET_TYPED`.
SUGGESTION_OPTIONAL_KEYS = {"evidenceHeadline"}
AFFORDANCE_KEYS = {
    "canTryBoth", "tryBothBlockedReason", "canPublish", "requiresTrialBeforePublish",
    "canRollback", "rollbackToVersion", "canSendToTeam", "hostAgentId", "forkNeeded",
}
EVIDENCE_KEYS = {"runCount", "window", "derivedWindows", "platform", "caveats"}
# One `PlatformHealth`. `alarmsIndeterminate` is what makes `state` readable — `unknown` with four
# unplaceable alarms is a lookup that could not decide, `unknown` with none is one that had nothing
# to decide about — and `window` is the interval the alarm correlation actually covered, without which
# nothing else in the block can be checked.
PLATFORM_KEYS = {"state", "reason", "alarms", "alarmsIndeterminate", "window", "source"}
# The GLOBAL half of a pattern. `memberRunIds` and `rootCauseHypo` are deliberately NOT here —
# they are session ids and facet evidence prose, and they travel on the owning actor's own item.
#
# `symptom` is the CONSEQUENT of the (shape -> symptom) hypothesis, and the gate triple is what makes
# `passHits: 0` mean anything: the controls the shape excluded, the controls it could not place, and
# the pool both are out of. Counts only — an inconclusive control's id is another tenant's session,
# which is why `controlsInconclusive` is a number and there is no field for the ids anywhere.
PATTERN_KEYS = {"patternId", "shape", "shapeFacets", "symptom", "runCount", "passHits",
                "controlsExcluded", "controlsInconclusive", "controlsConsidered",
                "equallyDiscriminative"}
# ...and the per-actor half. `PATTERN_KEYS` and this set must stay DISJOINT apart from
# `shapeFacets`, or a field is on both items or on neither.
PATTERN_EVIDENCE_KEYS = {"shapeFacets", "memberRunIds", "rootCauseHypo"}
RESIDUE_KEYS = {"symptom", "bestShape", "passHits", "reason"}
RESIDUE_EVIDENCE_KEYS = RESIDUE_KEYS | {"runId"}
# `environment` is the one diverted bucket whose divert was DECIDED by a platform reading, so it is
# the one that carries the reading. `residue`/`evidenceGaps` must NOT grow this key: an empty platform
# block on them would invite a reader to compare it against this one.
ENVIRONMENT_KEYS = RESIDUE_KEYS | {"platform"}
ENVIRONMENT_EVIDENCE_KEYS = ENVIRONMENT_KEYS | {"runId"}
UNATTRIBUTED_KEYS = {"shape", "shapeFacets", "symptom", "runCount", "reason"}
# `plainReason` is OPTIONAL on the TypeScript interface, so it is not in the required set: the
# mapper omits it rather than guessing when the record carries no known cause label.
UNATTRIBUTED_OPTIONAL_KEYS = {"plainReason"}
# The four registry facts a reader needs to weigh an accusation against the component named in it.
# `knownFailureModes` is THE WARRANT — `SigmaRegistry.match` attributes by finding a hit inside it —
# and `notes` is the other half of what a `keyword_inferred` hit can have matched.
COMPONENT_ROW_KEYS = {"componentId", "name", "layer", "type", "assetId", "currentVersion",
                      "versioned", "mutability", "sourceLocation", "blastRadius",
                      "knownFailureModes", "notes", "suggestionCount", "health"}
COMPONENTS_KEYS = {"yours", "fixFirst", "admin", "locked", "registryMeta"}
SCORECARD_KEYS = {"population", "passRate", "tiers", "spend", "outcomes", "byWorkloadClass",
                  "window", "derivedWindows", "evidenceComplete", "attribution", "platform",
                  "byModelScores", "byModelScoresOther"}
ATTRIBUTION_KEYS = {"actors", "attributed", "unattributable", "ownable", "runs"}
# The population's platform state. Counts and a state, never a growing list, because the scorecard is
# fixed size. The four run counts PARTITION `runs`, and `unconsulted` is separate from `inconclusive`
# because a lookup that never ran and one that ran and could not tell are different facts — only the
# first is fixable by configuration.
SCORECARD_PLATFORM_KEYS = {"state", "degraded", "healthy", "inconclusive", "unconsulted", "runs",
                           "alarmsInAlarm", "alarmsIndeterminate", "sources"}
WASTE_KEYS = {"runsWithWaste", "runs", "runsNotScanned", "totalCalls", "totalWallMs", "byKind"}
# The per-actor waste block is the same aggregate PLUS the findings the global one may not carry.
ACTOR_WASTE_KEYS = WASTE_KEYS | {"findings"}
# `history` is this run's per-day rows. `aws_lambda` merges them with the rows already on the item
# and adds `rolledUp`/`historyFirstDate`, so those two are absent HERE and present on the wire —
# the mapper has no view of the store and so cannot know what earlier days the bucket holds.
ACTOR_BUNDLE_KEYS = {"runs", "runsTotal", "history", "evidence", "waste", "patterns"}
# One day of an actor's activity. `runs` counts REQUESTS, so it does not move when a request fans out
# into plan sub-sessions; the spend terms DO include those sub-sessions. Pinned by equality in both
# directions for the same reason the run row is: this is what the metric tiles sum.
# `pass`/`fail`/`unscored` partition `runs`, in the scorecard's own spelling — counts and never a rate,
# because counts fold across days by addition and a rate cannot be re-derived from the row.
ACTOR_DAY_KEYS = {"date", "runs", "pass", "fail", "unscored", "tokens", "llmCalls", "latencyMs",
                  "cost", "costTokensPriced", "costTokensUnpriced", "byCategory", "models",
                  "coverage"}
# Each spend term's `measuredOf` plus the SESSIONS it is out of. Pinned by equality because a lenient
# sum with a missing denominator is the specific way this row can lie (`dto._sum_term`).
ACTOR_DAY_COVERAGE_KEYS = {"total", "tokens", "llmCalls", "latencyMs", "cost", "costTokensPriced",
                           "costTokensUnpriced", "byCategory"}
ACTOR_RUN_KEYS = {"runId", "workloadClass", "window", "verdict", "tiers", "spend"}
# One run's spend, PROJECTED from `Run.resources`. Eight of the twelve terms `usage_for` measures,
# and pinned by EQUALITY in both directions: a term dropped here leaves a personal metric with no
# counterpart to a population one, and a term ADDED silently grows the only list in the per-actor
# item that `aws_lambda._fit_actor_bundle` trims, so both directions are worth a failing test.
ACTOR_RUN_SPEND_KEYS = {"latencyMs", "tokens", "cost", "llmCalls", "byCategory", "models",
                        "costTokensPriced", "costTokensUnpriced"}
WASTE_KIND_KEYS = {"findings", "runs", "calls", "wallMs"}
# `plainSummary` is OPTIONAL for the same reason `plainReason` is: the mapper omits it for a kind it
# has no sentence for, so a kind added to the detectors before its prose is visibly unmapped rather
# than printed to a reader as a machine name. All three kinds that ship today DO carry it.
WASTE_KIND_OPTIONAL_KEYS = {"plainSummary"}
# The sentences themselves, literal here for the reason the field sets above are: a table built from
# the producer would agree with itself no matter what it said. Each one is held to what its kind's
# `waste._CONFIDENCE` note records as OBSERVED — "steps" and "more than once" because a repeated
# block is the same ordered tool names and can run more than twice, "before the request ended"
# because one session's spans can establish nothing later than that, and no word claiming the second
# attempt was avoidable.
WASTE_KIND_PLAIN = {
    "abandoned_dispatch": "A job's result was never collected before the request ended",
    "error_then_retry": "A step failed and ran again",
    "repeated_sequence": "The same series of steps ran more than once",
}
POPULATION_KEYS = {"total", "pass", "fail", "unscored"}
# `sessions` is present ONLY when the population was DISCOVERED, i.e. when there was a cap and a span
# floor to report on. The labelled benchmark has fixed membership, so zeros there would claim a
# measurement of a step that never ran — hence optional rather than in the set above.
POPULATION_OPTIONAL_KEYS = {"sessions"}
POPULATION_SESSIONS_KEYS = {"discoverable", "admitted", "belowMinSpans"}
SPEND_KEYS = {"tokens", "cost", "costBasis", "latencyMs", "byCategory", "llmCalls", "modelsSeen",
              "byModel", "byModelOther", "usageShortfall"}
# The one block here describing evidence that never became a run: spend the HARVEST measured and
# could not attach to a trajectory, so it is absent from `tokens` rather than short within it. Pinned
# by equality because a dropped term leaves `tokens` looking complete again, which is the defect.
SPEND_USAGE_SHORTFALL_KEYS = {"tokensDiscarded", "tokensMeasured",
                              "sessionsDiscarded", "sessionsMeasured"}
# `cost` is the ONE spend term with a second denominator, and the extra keys are why it can be
# trusted: pricing is per model, so a single fully-measured run can be only partly priced
# (`build_run._cost_usd`). `measuredOf`/`total` count RUNS here as they do on every sibling, and
# the token counts are what say how much of the spend the figure covers. Pinned by EQUALITY so
# neither the run denominator nor the token denominator can be dropped or renamed unnoticed.
SPEND_COST_KEYS = {"sum", "measuredOf", "total", "tokensPriced", "tokensUnpriced"}
# One row of `spend.byModel`, and the remainder `_SPEND_MODELS_NAMED` withheld. `byModelOther`
# carries a model COUNT and one token sum, never a second list — its purpose is to let the named
# rows be reconciled against `spend.tokens`, not to smuggle the bound's contents back in.
SPEND_BY_MODEL_KEYS = {"model", "tokens", "byCategory", "llmCalls"}
SPEND_BY_MODEL_OTHER_KEYS = {"models", "tokens"}
WORKLOAD_CLASS_KEYS = POPULATION_KEYS | {"passRate", "gating", "gatingIsMixed", "basis"}

#: Keys the PRODUCER emits that `ui/src/improve-api.ts` does not declare and no component reads yet.
#: Listed so the sets above stay honest about which of them are a two-sided contract and which are one
#: side of it: the TypeScript and its consumers are a separate change, and until they land these fields
#: are stored, served, and rendered by nothing. `scripts/improve-wire-census.py` finds the same gap
#: from the UI end and exits non-zero while it is open, so this is a record and not an excuse.
#:
#: Every one of them is a fact the loop already computed and dropped — the registry sentence that
#: justified an accusation, the controls a shape was tested against, what the platform was doing, and
#: the symptom half of a (shape -> symptom) hypothesis.
#:
#: SEVEN MORE new fields are absent from this set only because the TypeScript happens to declare the
#: same NAME on a different interface, which the census's name-based matching cannot tell apart and
#: which buys a consumer nothing: `symptom` (declared on `ResidueEntry`, now also on
#: `FailurePattern`), `window`/`state`/`alarms` (on `PlatformHealth`, now also the platform block's
#: own window and the scorecard's), `platform` (on `EvidenceQuality`, now also on the scorecard and on
#: an environment row), `blastRadius` (on `Suggestion`, now also on a `ComponentRow`), and `runs` (on
#: several blocks, now also `scorecard.platform.runs`). Count them as unread too.
_NOT_YET_TYPED = {
    # Waste `byKind`. This one is invisible to the census as well as to the TypeScript: the census
    # reads dict LITERALS out of the producer, and this key is set by subscript so a kind with no
    # sentence can omit it. So this line is the only record that it is emitted and unread.
    "plainSummary",
    # The card's lead line. Emitted by `suggestion_to_wire` and derived for a frozen row by
    # `publishable_suggestion`; nothing renders it until the TypeScript declares it.
    "evidenceHeadline",
    "matchedFailureMode",                                        # suggestion provenance
    "symptom", "controlsExcluded", "controlsInconclusive",        # pattern (admitted + suggestion)
    "controlsConsidered", "equallyDiscriminative",
    "alarmsIndeterminate",                                       # PlatformHealth
    "type", "knownFailureModes", "notes",                        # ComponentRow
    "degraded", "healthy", "inconclusive", "unconsulted",         # scorecard.platform
    "alarmsInAlarm", "sources",
}

REGISTRY_FIXTURE = """
meta:
  head_sha: deadbee
  revalidated: '2026-08-25'
prompt_layer:
  - component_id: editable-persona
    type: system_prompt
    source_location: agent/src/lab/specialty.py:1
    versioned: YES (Lab asset lifecycle, immutable vN + working copy)
    mutability: AUTO_CANDIDATE
    blast_radius: single
control_layer:
  - component_id: locked-rule
    type: gate
    source_location: infra/lib/lambdas/plan-chainer/handler.py:432
    versioned: NONE
    mutability: LOCKED
    blast_radius: single
  - component_id: undecided-thing
    type: budget
    source_location: agent/src/task_agent.py:131
    versioned: NONE
    blast_radius: single
tool_layer:
  # The one component here that DECLARES a failure mode, so an attribution against it comes back
  # `registry_declared` and has a sentence to quote. Its wording deliberately avoids every
  # `propose._FACET_KEYWORDS` term except `read_result`, so it cannot change which component the
  # other tests in this file attribute their facets to.
  - component_id: declaring-tool
    type: tool_wrapper
    source_location: agent/src/tools/async_jobs.py:88
    versioned: NONE
    mutability: REVIEW
    blast_radius: all sessions
    known_failure_modes: >
      never prompts a read_result once a job has been started. A second sentence, which the
      bound must drop.
    notes: >
      Wraps the async job API. A second sentence, which the bound must drop.
"""


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "sigma.yaml"
    path.write_text(REGISTRY_FIXTURE)
    return SigmaRegistry.load(str(path))


def _proposal(target="locked-rule", **over):
    base = {
        "proposal_id": "abc123",
        "target_type": "code_change",
        "target_id": target,
        "target_name": "locked rule",
        "operation": "edit",
        "summary": "Hold a step until its dependencies finish",
        "evidence_summary": "from 1 run where a step ran early",
        # What `propose.propose` puts on a Proposal for this shape. Carried here so `_wire` is a
        # CURRENT suggestion: a proposal dict without it is the older-producer case, which
        # `publishable_suggestion` repairs and which has its own test.
        "evidence_headline": "A relied-on step was recorded finished early",
        "blast_radius": {"class": "single", "instanceCount": 1},
        "base_version": None,
        "single_candidate_forced": None,
        "attribution": {"basis": "keyword_inferred", "mutability": "LOCKED",
                        "source_location": "infra/lib/lambdas/plan-chainer/handler.py:432"},
        "signature": {"run_id": "run-1", "shape": ["fanout_parent_ran_early"], "pass_hits": 0,
                      "evidence": {"fanout_parent_ran_early": "sg2 finished before sg1"}},
        "candidates": [{"candidate_id": "c1", "label": "Hold the step", "disposition":
                        "code_change", "claim": {}, "risk": "adds latency"}],
        "provenance": {"attribution_alternatives": ["other-thing"],
                       "registry_head_sha": "deadbee", "heuristic_facets": []},
    }
    base.update(over)
    return base


def _wire(registry, proposal=None, runs_by_id=None):
    return suggestion_to_wire(
        proposal or _proposal(), created_at="2026-08-25T04:00:00Z", registry=registry,
        runs_by_id=runs_by_id or {}, judge_config_hash="h", judge_runs=3,
        evidence_sources=["aws/spans"])


# --- field names match the TypeScript ---------------------------------------
def test_suggestion_emits_exactly_the_wire_keys(registry):
    assert set(_wire(registry)) == SUGGESTION_KEYS | SUGGESTION_OPTIONAL_KEYS
    # ...and the optional one is genuinely optional: a proposal with no headline emits the
    # required set and nothing more, rather than a key holding a facet name or an empty string.
    unlabelled = _proposal(evidence_headline="")
    assert set(_wire(registry, unlabelled)) == SUGGESTION_KEYS


def test_nested_shapes_emit_exactly_their_wire_keys(registry):
    wire = _wire(registry)
    assert set(wire["affordances"]) == AFFORDANCE_KEYS
    assert set(wire["evidenceQuality"]) == EVIDENCE_KEYS
    assert set(wire["evidenceQuality"]["platform"]) == PLATFORM_KEYS
    assert set(wire["pattern"]) == PATTERN_KEYS
    assert set(wire["candidates"][0]) == {
        "candidateId", "label", "diffText", "before", "after", "claim", "risk", "disposition"}


def test_residue_emits_exactly_its_wire_keys():
    entry = residue_to_wire({"run_id": "r", "symptom": [], "best_shape": [], "pass_hits": 1,
                             "pass_hit_runs": ["p"], "reason": "why", "refinement_log": []})
    assert set(entry) == RESIDUE_KEYS
    # The failing session's id reaches its OWNER and nobody else; the passing runs that
    # disqualified the shape reach nobody, because they belong to whichever tenants ran them —
    # only `passHits`, the count, survives either way.
    assert set(_residue_evidence({"run_id": "r", "symptom": [], "best_shape": [], "pass_hits": 1,
                                 "pass_hit_runs": ["p"], "reason": "why",
                                 "refinement_log": []})) == RESIDUE_EVIDENCE_KEYS


def test_components_emit_exactly_their_wire_keys(registry):
    wire = components_to_wire(registry, [])
    assert set(wire) == COMPONENTS_KEYS
    assert set(wire["yours"][0]) == {"layer", "label", "editable", "components"}
    assert set(wire["registryMeta"]) == {"componentCount", "revalidated", "headSha"}
    rows = [r for section in wire["yours"] for r in section["components"]]
    assert rows and set(rows[0]) == COMPONENT_ROW_KEYS


# --- the honest-absence contract --------------------------------------------
def test_status_is_always_open_because_nothing_stores_a_decision(registry):
    assert _wire(registry)["status"] == "open"


def test_lab_dependent_affordances_are_false_not_optimistic(registry):
    """Whether a working copy exists needs a live Lab query this process does not make. False
    makes the card withhold the action; True would offer a Try-both that fails on click."""
    affordances = _wire(registry)["affordances"]
    assert affordances["canTryBoth"] is False
    assert affordances["forkNeeded"] is False
    # RollbackAsset is implemented and tested but wired nowhere, so a revert cannot be offered.
    assert affordances["canRollback"] is False
    assert affordances["rollbackToVersion"] is None


def test_a_blocked_try_both_always_carries_a_reason(registry):
    assert _wire(registry)["affordances"]["tryBothBlockedReason"]


def test_locked_platform_code_is_never_publishable(registry):
    affordances = _wire(registry)["affordances"]
    assert affordances["canPublish"] is False
    assert affordances["canSendToTeam"] is True


def test_an_editable_versioned_target_is_publishable(registry):
    affordances = _wire(registry, _proposal(target="editable-persona"))["affordances"]
    assert affordances["canPublish"] is True
    # Wider-than-single changes have no revert, so a trial comes first.
    assert affordances["requiresTrialBeforePublish"] is True
    assert affordances["canSendToTeam"] is False


def test_unrecorded_mutability_is_not_publishable(registry):
    """Default-deny: a component the registry never assessed must not be editable."""
    affordances = _wire(registry, _proposal(target="undecided-thing"))["affordances"]
    assert affordances["canPublish"] is False


def test_plain_reason_is_omitted_rather_than_invented():
    """The engine writes only `reason`, which carries facet names and internal jargon. A rewrite
    belongs on the server; templating one here would drift from what the engine means."""
    entry = residue_to_wire({"run_id": "r", "symptom": [], "best_shape": [], "pass_hits": 0,
                             "pass_hit_runs": [], "reason": "no discriminative shape found: ...",
                             "refinement_log": []})
    assert "plainReason" not in entry


# --- sentinels --------------------------------------------------------------
def test_sentinels_serialize_as_names_never_null():
    assert window_to_wire(UNKNOWN) == "UNKNOWN"
    platform = platform_to_wire(UNKNOWN)
    # Two ways to be unknown: nobody looked, or a lookup could not tell. Neither is healthy.
    assert platform["state"] == "unknown"
    assert platform["reason"]


def test_a_real_window_reports_whether_it_was_derived():
    stamped = RunWindow(start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:40:00+00:00")
    assert window_to_wire(stamped)["derived"] is False
    derived = RunWindow(start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:40:00+00:00",
                        provenance="derived_plan_and_span_bounds")
    assert window_to_wire(derived)["derived"] is True


# --- evidence quality -------------------------------------------------------
def _run(run_id, *, derived=True, state=InfraState.UNKNOWN):
    window = RunWindow(
        start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:40:00+00:00",
        provenance="derived_plan_and_span_bounds" if derived else "collector_stamped")
    return Run(run_id=run_id, window=window,
               infra_health=InfraHealth(state=state, reason="because", source="test"))


def test_evidence_quality_counts_runs_and_derived_windows():
    quality = evidence_quality(["a", "b"], {"a": _run("a"), "b": _run("b", derived=False)})
    assert quality["runCount"] == 2
    assert quality["derivedWindows"] == 1


def test_worst_platform_state_wins_not_the_most_common():
    """The question is whether ANY of the evidence is compromised, not whether most is fine."""
    runs = {
        "a": _run("a", state=InfraState.HEALTHY),
        "b": _run("b", state=InfraState.HEALTHY),
        "c": _run("c", state=InfraState.DEGRADED),
    }
    assert evidence_quality(["a", "b", "c"], runs)["platform"]["state"] == "degraded"


def test_unknown_outranks_healthy_because_it_cannot_be_ruled_out():
    runs = {"a": _run("a", state=InfraState.HEALTHY), "b": _run("b", state=InfraState.UNKNOWN)}
    assert evidence_quality(["a", "b"], runs)["platform"]["state"] == "unknown"


def test_a_single_run_pattern_says_so():
    quality = evidence_quality(["a"], {"a": _run("a")})
    assert any("one run" in caveat for caveat in quality["caveats"])


def test_no_member_runs_yields_a_sentinel_window_and_a_caveat():
    quality = evidence_quality([], {})
    assert quality["runCount"] == 0
    assert quality["window"] == "UNKNOWN"
    assert quality["caveats"]


def test_a_pattern_resting_on_a_clipped_run_says_so_on_its_own_card():
    """A suggestion whose evidence the harvest partly dropped has to admit it where it is read.

    The count travels; the run ids do not — this rides the one global item every actor reads. And
    the caveat names the CAUSE, because "less evidence than its run count suggests" is actionable
    only if the reader knows it was the Insights row cap and not a thin population.
    """
    runs = {"a": _run("a"), "b": _run("b")}
    runs["a"].evidence_complete = False
    quality = evidence_quality(["a", "b"], runs)
    caveat = next(c for c in quality["caveats"] if "row cap" in c)
    assert "1 of 2 runs behind this pattern" in caveat
    assert "unobserved rather than False" in caveat
    assert "a" not in caveat.split() and "b" not in caveat.split(), "no run ids on the wire"


def test_the_clipped_run_caveat_agrees_with_the_denominator():
    """`1 of 1 run`, not `1 of 1 runs`: the noun counts off the members, and this caveat is one of
    the sentences the card renders verbatim. A one-run pattern is the common case in a single-digit
    population, so this is the shape a reader is most likely to meet."""
    runs = {"a": _run("a")}
    runs["a"].evidence_complete = False
    quality = evidence_quality(["a"], runs)
    caveat = next(c for c in quality["caveats"] if "row cap" in c)
    assert caveat.startswith("1 of 1 run behind this pattern had part of their ")


def test_completeness_nobody_recorded_is_not_caveated_as_incomplete():
    """UNKNOWN is the state of every harvest taken before adaptive windowing, including the whole
    golden set. Caveating it would put the same sentence on every card in the bundle and teach a
    reader to skip the field; the unknown population is reported as a NUMBER on the scorecard
    instead (`evidenceComplete.measuredOf`), where 0-of-N is legible without being noise."""
    quality = evidence_quality(["a", "b"], {"a": _run("a"), "b": _run("b")})
    assert not any("row cap" in c for c in quality["caveats"])


# --- components bucketing ---------------------------------------------------
def test_every_component_lands_in_exactly_one_bucket(registry):
    wire = components_to_wire(registry, [])
    counted = (sum(len(s["components"]) for s in wire["yours"])
               + wire["fixFirst"]["count"] + wire["admin"]["count"] + wire["locked"]["count"])
    assert counted == len(registry)


def test_suggestion_counts_are_joined_onto_the_rows(registry):
    wire = components_to_wire(registry, [{"targetId": "locked-rule"}, {"targetId": "locked-rule"}])
    row = next(r for r in wire["fixFirst"]["components"] if r["componentId"] == "locked-rule")
    assert row["suggestionCount"] == 2


def test_lab_only_row_fields_are_null_not_guessed(registry):
    """`assetId`, `currentVersion` and `health` need a live Lab query. Null renders as an em-less
    placeholder; a zero would read as a real measurement."""
    row = components_to_wire(registry, [])["yours"][0]["components"][0]
    assert row["assetId"] is None
    assert row["currentVersion"] is None
    assert row["health"] is None


# --- the scorecard ----------------------------------------------------------
def _tier(name, score, outcome):
    return EvaluatorResult(name, "llm", outcome, score, "")


def _verdict(run_id, overall, *, dispatch=None, delivered=None, workload="conversational",
             gating=("dispatch_correct", "results_delivered"), results=None, reason="",
             basis="recorded_label", parent=""):
    """A Verdict with the two goal tiers, as `verdict_from_recorded` builds them.

    `None` for a tier score means the tier was never measured (NO_DATA), which is what makes a
    run UNSCORED rather than failing — the distinction the scorecard has to keep.
    """
    if results is None:
        results = [
            _tier("dispatch_correct", dispatch,
                  Outcome.NO_DATA if dispatch is None
                  else Outcome.PASS if dispatch >= 0.7 else Outcome.FAIL),
            _tier("results_delivered", delivered,
                  Outcome.NO_DATA if delivered is None
                  else Outcome.PASS if delivered >= 0.7 else Outcome.FAIL),
        ]
    return Verdict(run_id, results, UtilityVector(), overall, reason,
                   workload_class=workload, workload_basis=basis, gating=tuple(gating),
                   workload_parent=parent)


def _resource_run(run_id, **resources) -> Run:
    return Run(run_id=run_id, resources=resources)


def test_scorecard_scores_each_model_on_every_metric_crediting_a_run_to_the_model_that_did_most_of_it():
    """Improve's model comparison: the same scores per model. A run that touched two models counts
    once, for the one its tokens mostly went to; a check a model never needed is absent, not 0%."""
    opus, sonnet = "global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5"
    dag = lambda o: EvaluatorResult("DagCompletionValidator", "code", o)  # noqa: E731
    runs = [
        _resource_run("a", tokens=100, llm_calls=4, latency_ms=2000,
                      by_model={opus: {"tokens": 90}, sonnet: {"tokens": 10}}),
        _resource_run("b", tokens=50, llm_calls=2, latency_ms=1000, by_model={opus: {"tokens": 50}}),
        _resource_run("c", tokens=20, by_model={sonnet: {"tokens": 20}}),
        _resource_run("d", models=[sonnet]),                     # no per-model usage: its first model
        _resource_run("e"),                                      # names no model at all
    ]
    verdicts = [
        _verdict("a", Overall.PASS, results=[_tier("dispatch_correct", 1.0, Outcome.PASS),
                                             _tier("results_delivered", 0.8, Outcome.PASS), dag(Outcome.PASS)]),
        _verdict("b", Overall.FAIL, results=[_tier("dispatch_correct", 0.5, Outcome.FAIL),
                                             _tier("results_delivered", 0.4, Outcome.FAIL), dag(Outcome.FAIL)]),
        _verdict("c", Overall.PASS, dispatch=0.9, delivered=0.9),
        _verdict("d", Overall.UNSCORED),
        _verdict("e", Overall.PASS, dispatch=0.9, delivered=0.9),
    ]
    card = scorecard_to_wire(runs, verdicts)
    rows = {r["model"]: r for r in card["byModelScores"]}
    assert [r["model"] for r in card["byModelScores"]] == [opus, sonnet]   # ranked by runs, ties by id
    o, s_ = rows[opus], rows[sonnet]
    assert o["population"] == {"total": 2, "pass": 1, "fail": 1, "unscored": 0} and o["passRate"] == 0.5
    assert o["tiers"]["dispatch"] == {"mean": 0.75, "measuredOf": 2, "total": 2}
    assert o["tiers"]["delivery"]["mean"] == pytest.approx(0.6)
    assert o["checks"] == {"DagCompletionValidator": {"pass": 1, "fail": 1, "notMeasured": 0, "rate": 0.5}}
    assert o["perRun"]["tokens"] == {"mean": 75.0, "measuredOf": 2, "total": 2}
    assert o["perRun"]["latencyMs"]["mean"] == 1500.0
    assert s_["population"] == {"total": 2, "pass": 1, "fail": 0, "unscored": 1} and s_["passRate"] == 1.0
    assert s_["checks"] == {}                                               # never needed: absent, not 0%
    assert card["byModelScoresOther"] == {"models": 0, "runs": 0, "unattributed": 1}


def test_scorecard_emits_exactly_the_wire_keys():
    card = scorecard_to_wire([_resource_run("a", tokens=10)], [_verdict("a", Overall.PASS,
                                                                       dispatch=0.9,
                                                                       delivered=0.9)])
    assert set(card) == SCORECARD_KEYS
    assert set(card["population"]) == POPULATION_KEYS
    assert set(card["tiers"]) == {"dispatch", "delivery"}
    assert set(card["tiers"]["dispatch"]) == {"mean", "measuredOf", "total"}
    assert set(card["spend"]) == SPEND_KEYS
    assert set(card["spend"]["tokens"]) == {"sum", "measuredOf", "total"}
    assert set(card["spend"]["usageShortfall"]) == SPEND_USAGE_SHORTFALL_KEYS
    assert set(card["spend"]["cost"]) == SPEND_COST_KEYS
    # And the token counts are cost's ALONE. A sibling term measured per run has no partial state to
    # report, so carrying them everywhere would invite a reader to look for one.
    assert set(card["spend"]["llmCalls"]) == {"sum", "measuredOf", "total"}
    assert set(card["spend"]["latencyMs"]) == {"sum", "measuredOf", "total"}
    assert set(card["spend"]["byCategory"]) == {"input", "output", "cacheRead", "cacheWrite"}
    assert set(card["spend"]["byCategory"]["cacheRead"]) == {"sum", "measuredOf", "total"}
    # `byModel` is empty here (this run's usage names no model) but its companion is not optional:
    # a consumer must never have to branch on whether the remainder key exists.
    assert card["spend"]["byModel"] == []
    assert set(card["spend"]["byModelOther"]) == SPEND_BY_MODEL_OTHER_KEYS
    assert set(card["outcomes"]) == {"failedGating", "unmeasured"}
    assert set(card["byWorkloadClass"]["conversational"]) == WORKLOAD_CLASS_KEYS
    assert set(card["platform"]) == SCORECARD_PLATFORM_KEYS


def test_scorecard_pass_rate_excludes_unscored():
    """UNSCORED is neither pass nor fail. Folding it into either side would report the
    evidence's failure as the agent's, or hide it inside a rate that looks fine."""
    verdicts = ([_verdict(f"p{i}", Overall.PASS, dispatch=0.9, delivered=0.9) for i in range(3)]
                + [_verdict("f0", Overall.FAIL, dispatch=0.9, delivered=0.1)]
                + [_verdict(f"u{i}", Overall.UNSCORED, dispatch=0.9) for i in range(5)])
    card = scorecard_to_wire([], verdicts)
    assert card["population"] == {"total": 9, "pass": 3, "fail": 1, "unscored": 5}
    assert card["passRate"] == 0.75
    # 9 runs, and the rate is over 4 of them. Both numbers are on the wire.
    assert card["population"]["total"] == 9


def test_the_scorecard_counts_evidence_incomplete_runs_and_its_own_denominator():
    """`incomplete: 0` alone cannot be read. Three numbers, or the field lies.

    Zero-of-zero is what every bundle produced before adaptive windowing looks like, and it means
    NOT CHECKED. Reporting only `incomplete` would make that indistinguishable from a population
    verified clean — invariant 1, one level up from the sentinels.
    """
    unchecked = scorecard_to_wire(
        [_resource_run("a"), _resource_run("b")],
        [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
         _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert unchecked["evidenceComplete"] == {"incomplete": 0, "measuredOf": 0, "total": 2}

    clipped, clean = _resource_run("a"), _resource_run("b")
    clipped.evidence_complete = False
    clean.evidence_complete = True
    card = scorecard_to_wire(
        [clipped, clean],
        [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
         _verdict("b", Overall.FAIL, dispatch=0.9, delivered=0.1)])
    assert card["evidenceComplete"] == {"incomplete": 1, "measuredOf": 2, "total": 2}
    # The clipped run is still IN the population — its trajectory facets are unobservable, which
    # takes it out of control pools, not out of the count.
    assert card["population"]["total"] == 2


def test_pass_rate_is_none_when_nothing_was_scored():
    """`0.0` would read as "everything failed"; the truth is that nothing was measured."""
    card = scorecard_to_wire([], [_verdict("u", Overall.UNSCORED)])
    assert card["passRate"] is None


# --- what the population is a SAMPLE of -------------------------------------
# The counts below are one real 24h staging harvest, replayed at two caps and already recorded in
# `_plan_children`'s docstring: 36 sessions discoverable, and a cap of 25 admitting 25 of them.
_REPLAYED_DISCOVERY = {"harvested": 39, "below_min_spans": 3, "discoverable": 36,
                       "min_spans": 5, "admitted": 25, "limit": 25}


def test_the_population_says_what_fraction_of_the_minable_harvest_it_scored():
    """`total: 25` captioned "the population" is the defect; `admitted` of `discoverable` is the fix.

    Everything folded from these runs — the pass rate, the spend, every day row — is a figure over
    the admitted sample, so without these two numbers a reader cannot tell a whole day from a
    quarter of one.
    """
    card = scorecard_to_wire([_resource_run("a", tokens=10)],
                            [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)],
                            discovery=_REPLAYED_DISCOVERY)
    assert set(card["population"]) == POPULATION_KEYS | POPULATION_OPTIONAL_KEYS
    assert set(card["population"]["sessions"]) == POPULATION_SESSIONS_KEYS
    assert card["population"]["sessions"] == {"discoverable": 36, "admitted": 25,
                                             "belowMinSpans": 3}
    # The two exclusions are separately named, so `discoverable + belowMinSpans` recovers the whole
    # harvest and neither bound can hide inside the other.
    assert (card["population"]["sessions"]["discoverable"]
            + card["population"]["sessions"]["belowMinSpans"]) == 39


def test_a_population_with_no_discovery_step_omits_the_block_rather_than_zeroing_it():
    """The labelled benchmark has fixed membership and no cap, so there is nothing to report.

    Zeros here would claim a discovery step ran and discarded nothing — a measurement of something
    that never happened, which is the same lie one level up from the sentinels.
    """
    card = scorecard_to_wire([_resource_run("a", tokens=10)],
                            [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert set(card["population"]) == POPULATION_KEYS
    assert "sessions" not in card["population"]


def test_a_discovery_block_missing_a_count_is_dropped_rather_than_half_reported():
    """Two of three counts is not a denominator. An older caller that supplies only some of them
    must reach the consumer as "not recorded", never as a fraction with an invented term."""
    card = scorecard_to_wire([], [], discovery={"discoverable": 36, "admitted": 25})
    assert "sessions" not in card["population"]


# --- the spend the harvest never handed over --------------------------------
# One real staging harvest, 2026-08-29, 24h: 44 sessions measured, 20 kept, 13.3M of 36.8M tokens
# discarded. Recorded in `harvest_live._usage_shortfall` and in the seam test that replays it.
_REPLAYED_SHORTFALL = {"tokens_discarded": 13_300_000, "tokens_measured": 36_800_000,
                       "sessions_discarded": 24, "sessions_measured": 44}


def test_the_spend_says_what_the_harvest_discarded_before_the_population_existed():
    """`tokens` cannot be short of a session it never saw — it is absent from both halves.

    The live item read `tokens {sum: 10320108, measuredOf: 25, total: 25}`, i.e. complete coverage,
    on a harvest that had thrown a third of the measured spend away. Complete coverage of an
    incomplete population is the exact reading this block exists to break.
    """
    card = scorecard_to_wire([_resource_run("a", tokens=10)],
                            [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)],
                            usage_shortfall=_REPLAYED_SHORTFALL)
    assert card["spend"]["usageShortfall"] == {
        "tokensDiscarded": 13_300_000, "tokensMeasured": 36_800_000,
        "sessionsDiscarded": 24, "sessionsMeasured": 44}
    # And `tokens` still says it measured every run it had, which is TRUE and is why the block
    # beside it is the only thing that can say the money number is short.
    assert card["spend"]["tokens"]["measuredOf"] == card["spend"]["tokens"]["total"] == 1


def test_a_harvest_that_recorded_no_shortfall_is_unmeasured_never_zero():
    """A harvest that predates the audit, or skipped its usage query, must not read as clean."""
    card = scorecard_to_wire([_resource_run("a", tokens=10)],
                            [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert set(card["spend"]["usageShortfall"]) == SPEND_USAGE_SHORTFALL_KEYS
    assert all(v == "UNMEASURED" for v in card["spend"]["usageShortfall"].values())


def test_the_shortfall_survives_the_artifacts_sentinel_flattening():
    """The local artifact writes a sentinel as its NAME, so the reader's UNMEASURED arrives here as
    the string ``"UNMEASURED"``. Both spellings mean unmeasured and neither may become a number."""
    for absent in (UNMEASURED, "UNMEASURED", None):
        card = scorecard_to_wire([], [], usage_shortfall={
            **_REPLAYED_SHORTFALL, "tokens_discarded": absent})
        shortfall = card["spend"]["usageShortfall"]
        assert shortfall["tokensDiscarded"] == "UNMEASURED", absent
        assert shortfall["tokensMeasured"] == 36_800_000, "one absent term unmeasures only itself"


def test_scorecard_reports_the_denominator_for_every_average():
    """A mean over the subset that happened to be measured is not a population mean, and a
    reader who cannot see the denominator will read it as one."""
    runs = [_resource_run("a", tokens=100, latency_ms=5), _resource_run("b", tokens=UNMEASURED)]
    verdicts = [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                _verdict("b", Overall.UNSCORED, dispatch=0.9)]  # delivery never measured
    card = scorecard_to_wire(runs, verdicts)

    assert card["tiers"]["dispatch"] == {"mean": 0.9, "measuredOf": 2, "total": 2}
    delivery = card["tiers"]["delivery"]
    assert delivery["mean"] == 0.9 and delivery["measuredOf"] == 1 and delivery["total"] == 2
    tokens = card["spend"]["tokens"]
    assert tokens["sum"] == 100 and tokens["measuredOf"] == 1 and tokens["total"] == 2
    latency = card["spend"]["latencyMs"]
    assert latency["measuredOf"] == 1 and latency["total"] == 2


def test_an_entirely_unmeasured_term_is_the_sentinel_name_not_zero():
    """A `sum: 0` captioned "spend" would read as "free"; `"UNMEASURED"` cannot.

    Tokens are measured on the deploy path now, so this covers the case that remains real: a
    population whose sessions recorded no LLM usage (roughly a third of harvested sessions carry
    none), plus `cost`, which is UNMEASURED by decision rather than by gap."""
    card = scorecard_to_wire([_resource_run("a", tokens=UNMEASURED, cost=UNMEASURED)],
                             [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    for term in ("tokens", "cost"):
        assert card["spend"][term]["sum"] == "UNMEASURED"
        assert card["spend"][term]["sum"] is not None
        assert card["spend"][term]["measuredOf"] == 0
        assert card["spend"][term]["total"] == 1


def test_a_run_the_producer_could_not_price_reaches_the_wire_as_the_sentinel_and_says_why():
    """Tokens fully measured and cost still absent, which is a reachable state and not a broken one.

    A session with per-category counts and a named model can still be unpriceable — no model in it
    has a complete rate row — and this mapper must publish the producer's sentinel rather than
    inferring a price from the tokens sitting right beside it. Zero tokens priced is what says the
    figure covers none of this population, and `costBasis` says why in words.
    """
    runs = [_resource_run("a", tokens=90570, output_tokens=831, cache_read_tokens=60671,
                          cache_write_tokens=29064, input_tokens=4, llm_calls=1,
                          cost_tokens_priced=0, cost_tokens_unpriced=90570,
                          models=("global.anthropic.claude-opus-4-8",))]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    spend = card["spend"]

    assert spend["tokens"]["sum"] == 90570 and spend["tokens"]["measuredOf"] == 1
    assert spend["cost"]["sum"] == "UNMEASURED" and spend["cost"]["measuredOf"] == 0
    assert spend["cost"]["tokensPriced"] == 0
    assert spend["cost"]["tokensUnpriced"] == 90570
    assert "rate table" in spend["costBasis"]
    # The two facts that make a single-rate cost wrong, both on the wire beside the sentinel.
    assert "cache reads" in spend["costBasis"]
    assert spend["modelsSeen"] == 1


def test_a_partly_priced_population_states_the_TOKENS_it_priced_and_not_only_the_runs():
    """The trap this key set closes: a short total captioned with a complete-looking denominator.

    Pricing is per model, so a run can be fully MEASURED and only partly PRICED. Both runs here are
    priced — `cost.measuredOf` is 2 of 2, and on runs alone the figure would read as the whole
    population's bill — while a third of the second run's tokens went to a model with no rate row.
    The only thing that says so is the token pair, so it is asserted as the pair a reader divides.
    """
    runs = [_resource_run("a", tokens=90570, cost=0.2327805,
                          cost_tokens_priced=90570, cost_tokens_unpriced=0),
            _resource_run("b", tokens=30000, cost=0.05,
                          cost_tokens_priced=20000, cost_tokens_unpriced=10000)]
    spend = scorecard_to_wire(runs, [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9)
                                     for r in ("a", "b")])["spend"]

    # Every run priced, so the RUN denominator says nothing is missing...
    assert spend["cost"]["measuredOf"] == 2 and spend["cost"]["total"] == 2
    assert spend["cost"]["sum"] == pytest.approx(0.2827805)
    # ...and the TOKEN denominator is the one that shows the figure is short by 10,000 tokens.
    assert spend["cost"]["tokensPriced"] == 110570
    assert spend["cost"]["tokensUnpriced"] == 10000
    # The two counts partition the population's tokens, so "N of M" reconciles against `tokens`.
    assert (spend["cost"]["tokensPriced"] + spend["cost"]["tokensUnpriced"]
            == spend["tokens"]["sum"] == 120570)


def test_the_token_counts_ignore_runs_that_recorded_no_usage_rather_than_zeroing_them():
    """A run with no usage block is missing from both halves, not counted as fully priced.

    Roughly a third of harvested sessions record no LLM usage at all, and `build_run` gives those
    runs the UNMEASURED sentinel for both counts. Folding a sentinel in as 0 unpriced would be
    harmless; folding it in as priced would inflate the covered share with sessions that never
    spent, which is how "N of M tokens priced" would come to overstate its own coverage.
    """
    runs = [_resource_run("a", tokens=1000, cost=0.01,
                          cost_tokens_priced=600, cost_tokens_unpriced=400),
            _resource_run("b", tokens=UNMEASURED, cost=UNMEASURED,
                          cost_tokens_priced=UNMEASURED, cost_tokens_unpriced=UNMEASURED),
            _resource_run("c")]  # no usage keys at all — the same fact, spelled by omission
    spend = scorecard_to_wire(runs, [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9)
                                     for r in ("a", "b", "c")])["spend"]

    assert spend["cost"]["measuredOf"] == 1 and spend["cost"]["total"] == 3
    assert spend["cost"]["tokensPriced"] == 600
    assert spend["cost"]["tokensUnpriced"] == 400


def test_the_population_cost_is_exactly_the_sum_of_the_per_actor_run_costs_when_partly_priced():
    """The two surfaces must not disagree about money, and partial pricing is where they could.

    `_run_row` publishes each run's own `cost` straight off `Run.resources`, and the scorecard sums
    the same values — so a partly-priced run contributes the same partial figure to an actor's row
    and to the population. Pinned because the alternative that suggests itself, suppressing a
    partial per-run cost to the sentinel, would silently make the population's total larger than
    the rows that are supposed to add up to it.
    """
    runs = [_measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3)}),
            _measured_run("b", {_OPUS: _model_counts(30000, 300, 20000, 9000, 2)})]
    for run, usd, unpriced in ((runs[0], 0.2327805, 0), (runs[1], 0.05, 10000)):
        run.actor_id = _ACTOR_A
        run.resources["cost"] = usd
        run.resources["cost_tokens_priced"] = run.resources["tokens"] - unpriced
        run.resources["cost_tokens_unpriced"] = unpriced
    verdicts = [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9) for r in ("a", "b")]

    spend = scorecard_to_wire(runs, verdicts)["spend"]
    rows = actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts)[_ACTOR_A]["runs"]

    assert sorted(row["spend"]["cost"] for row in rows) == [0.05, 0.2327805]
    assert spend["cost"]["sum"] == pytest.approx(sum(row["spend"]["cost"] for row in rows))
    assert spend["cost"]["tokensUnpriced"] == 10000


def test_the_token_categories_are_reported_separately_because_they_are_priced_differently():
    """One number times one rate is not an imprecise cost, it is the wrong number.

    A cache READ costs about a tenth of an uncached input token, and on this population cache
    reads are the overwhelming majority of prompt tokens. So the four categories ship separately
    and each carries its own denominator — the split is what a future rate table multiplies, and
    what stops anyone multiplying `tokens` by one rate in the meantime.
    """
    runs = [_resource_run("a", tokens=90570, output_tokens=831, cache_read_tokens=60671,
                          cache_write_tokens=29064, input_tokens=4),
            _resource_run("b", tokens=45720, output_tokens=422, cache_read_tokens=44439,
                          cache_write_tokens=857, input_tokens=2)]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                                    _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9)])
    by_category = card["spend"]["byCategory"]

    assert by_category["cacheRead"]["sum"] == 105110
    assert by_category["cacheWrite"]["sum"] == 29921
    assert by_category["output"]["sum"] == 1253
    assert by_category["input"]["sum"] == 6
    # The categories reconstruct the total exactly — Bedrock's own arithmetic, so a chart can
    # stack them against `tokens` without a residual to explain away.
    assert sum(by_category[k]["sum"] for k in by_category) == card["spend"]["tokens"]["sum"]
    # And the reason the split matters: uncached input is 6 tokens of 136,290.
    assert by_category["input"]["sum"] * 1000 < by_category["cacheRead"]["sum"]


def test_models_seen_is_a_count_taken_over_the_identities_not_the_spend():
    """`modelsSeen` is a number, deliberately, and it is what justifies the UNMEASURED cost.

    It is derived from the model IDENTITIES a run recorded (`Run.resources['models']`), which is a
    different field from the per-model spend beside it — a run can name models it has no counts for.
    So the count still answers its own question, "can one rate be applied to this population", from
    the widest evidence available rather than from whatever the split happened to aggregate.

    The ids themselves DO reach this item, in `byModel`, and are permitted to: they come from the
    `gen_ai.request.model` span attribute, which the harness sets from its own inference-profile
    configuration. They are not user input, model prose, a tool argument, a session id or an actor
    id, which is the whole of the global item's content rule.
    """
    runs = [_resource_run("a", tokens=10, models=("global.anthropic.claude-opus-4-8",
                                                  "us.anthropic.claude-haiku-4-5-20251001-v1:0")),
            _resource_run("b", tokens=20, models=("global.anthropic.claude-opus-4-8",))]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                                    _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9)])

    assert card["spend"]["modelsSeen"] == 2
    # Neither run carries per-model counts, so nothing is claimed about the split — the count is
    # NOT back-derived from `byModel`, which would report 0 models for this population.
    assert card["spend"]["byModel"] == []
    assert card["spend"]["byModelOther"]["models"] == 0


# --- the per-model split: which model the tokens actually went to ------------
#
# Two real Bedrock inference-profile ids from the harvested population, used verbatim so the
# fixtures below have the same shape (and the same cross-region prefixes) as the real thing.
_OPUS = "global.anthropic.claude-opus-4-8"
_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# The OTHER id today's live population spends on, alongside `_OPUS`. Used where a fixture has to
# be the roster a reader would actually see on their own runs rather than any two real ids.
_SONNET = "global.anthropic.claude-sonnet-5"


def _model_counts(tokens, output, cache_read, cache_write, calls) -> dict:
    """One model's slice of a session's usage, in `build_run.usage_for`'s per-model spelling.

    Uncached input is DERIVED as `total - output - cacheRead - cacheWrite`, exactly as
    `harvest_live._usage_by_session` derives it, so a fixture cannot describe a split that fails to
    reconstruct its own total.
    """
    return {"tokens": tokens, "output_tokens": output, "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write, "llm_calls": calls,
            "input_tokens": tokens - output - cache_read - cache_write}


def _measured_run(run_id, by_model) -> Run:
    """A Run whose `resources` look like `usage_for`'s output for a session that WAS measured.

    The top-level terms are the sum over `by_model`, which is the invariant
    `harvest_live._usage_by_session` maintains — it accumulates both from the same query rows — so a
    fixture here cannot describe a session the producer could not have produced.
    """
    terms = ("tokens", "input_tokens", "output_tokens", "cache_read_tokens",
             "cache_write_tokens", "llm_calls")
    return _resource_run(run_id, by_model=by_model, models=tuple(sorted(by_model)),
                         **{term: sum(counts[term] for counts in by_model.values())
                            for term in terms})


def test_spend_names_which_model_the_tokens_went_to_and_a_shared_run_counts_once_in_each():
    """The fact `modelsSeen` destroys: the roster is not evenly loaded.

    Measured over a 336 h harvest, one of five ids is a third of the sessions and three quarters of
    the tokens — so which model the spend went to is the only figure on this surface a reader can
    act on without a rate table, and a bare count of five hides it completely.

    The multi-model run is the interesting case and the one an aggregation gets wrong. Run `a`
    spends on BOTH models, so each row must count it once and neither may count it twice: `_HAIKU`
    appears in two runs (`measuredOf: 2`) with the two slices SUMMED, while `_OPUS` appears in one.
    """
    runs = [_measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3),
                                _HAIKU: _model_counts(5399, 228, 0, 5169, 1)}),
            _measured_run("b", {_HAIKU: _model_counts(1200, 100, 900, 150, 2)})]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                                    _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9)])
    spend = card["spend"]

    # Ranked by measured tokens, descending. These two real ids happen to sort the same way by id,
    # so it is `test_the_named_model_bound_folds_the_remainder_with_its_own_model_count` — where the
    # ids ascend as the spend descends — that pins the rank to the spend.
    assert [row["model"] for row in spend["byModel"]] == [_OPUS, _HAIKU]
    assert set(spend["byModel"][0]) == SPEND_BY_MODEL_KEYS
    opus, haiku = spend["byModel"]

    assert opus["tokens"] == {"sum": 90570, "measuredOf": 1, "total": 2}
    assert opus["byCategory"]["cacheRead"] == {"sum": 60671, "measuredOf": 1, "total": 2}
    assert opus["byCategory"]["cacheWrite"] == {"sum": 29064, "measuredOf": 1, "total": 2}
    assert opus["byCategory"]["output"] == {"sum": 831, "measuredOf": 1, "total": 2}
    assert opus["byCategory"]["input"] == {"sum": 4, "measuredOf": 1, "total": 2}
    assert opus["llmCalls"] == {"sum": 3, "measuredOf": 1, "total": 2}

    # 5,399 from run `a` plus 1,200 from run `b`. Double-counting either would show up here as
    # 11,998 / 2,400, and charging the whole run to each of its models as 95,969.
    assert haiku["tokens"] == {"sum": 6599, "measuredOf": 2, "total": 2}
    assert haiku["byCategory"]["cacheRead"] == {"sum": 900, "measuredOf": 2, "total": 2}
    assert haiku["byCategory"]["cacheWrite"] == {"sum": 5319, "measuredOf": 2, "total": 2}
    assert haiku["byCategory"]["output"] == {"sum": 328, "measuredOf": 2, "total": 2}
    assert haiku["byCategory"]["input"] == {"sum": 52, "measuredOf": 2, "total": 2}
    assert haiku["llmCalls"] == {"sum": 3, "measuredOf": 2, "total": 2}

    # Nothing is withheld at this roster size, and the parts reconcile against the population totals
    # term by term — a chart can stack the rows against `tokens` with no residual to explain away.
    assert spend["byModelOther"] == {"models": 0,
                                     "tokens": {"sum": "UNMEASURED", "measuredOf": 0, "total": 2}}
    assert sum(row["tokens"]["sum"] for row in spend["byModel"]) == spend["tokens"]["sum"] == 97169
    for name in ("input", "output", "cacheRead", "cacheWrite"):
        assert (sum(row["byCategory"][name]["sum"] for row in spend["byModel"])
                == spend["byCategory"][name]["sum"]), name
    assert sum(row["llmCalls"]["sum"] for row in spend["byModel"]) == spend["llmCalls"]["sum"] == 6


def test_a_per_model_denominator_is_the_measured_runs_and_never_the_population():
    """The subtle half, and the one that would let a one-session model read as a platform trend.

    A row's `total` is `spend.tokens.measuredOf` — the runs whose usage was measured at ALL — not
    the population. A run with no usage block does not record which model it did not use, so putting
    it in a per-model denominator would assert an absence nobody observed. The two denominators
    compose instead: model-share within the measured runs, measured-share within the population.

    Here two of four runs recorded usage, so every per-model figure reads `of 2` while the
    population is 4. On the real harvest that is `1 of 354` beside a population of 553.
    """
    runs = [_measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3)}),
            _measured_run("b", {_HAIKU: _model_counts(1200, 100, 900, 150, 2)}),
            _resource_run("c", latency_ms=10),
            _resource_run("d", latency_ms=20)]
    card = scorecard_to_wire(runs, [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9)
                                    for r in ("a", "b", "c", "d")])
    spend = card["spend"]

    assert spend["tokens"] == {"sum": 91770, "measuredOf": 2, "total": 4}
    assert [row["tokens"]["measuredOf"] for row in spend["byModel"]] == [1, 1]
    for row in spend["byModel"]:
        assert row["tokens"]["total"] == 2, row["model"]
        assert row["llmCalls"]["total"] == 2, row["model"]
        for name in ("input", "output", "cacheRead", "cacheWrite"):
            assert row["byCategory"][name]["total"] == 2, (row["model"], name)
    assert spend["byModelOther"]["tokens"]["total"] == 2


def test_the_named_model_bound_folds_the_remainder_with_its_own_model_count():
    """The bound exists so this field's size is knowable rather than lucky, and it must not LOSE
    tokens on the way — the remainder is folded up with its own model count so the named rows can
    still be reconciled against `spend.tokens`.

    Model ids ascend while their spend DESCENDS with the id, so a bound applied to an unranked or
    id-ranked roster keeps the wrong models: the twenty that survive are the twenty heaviest.
    """
    roster = {f"m{i:02d}": _model_counts((i + 1) * 100, 10, (i + 1) * 50, 20, 1)
              for i in range(_SPEND_MODELS_NAMED + 3)}
    runs = [_measured_run("a", roster),
            _measured_run("b", {"m00": _model_counts(50, 5, 20, 10, 1)})]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                                    _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9)])
    spend = card["spend"]

    assert len(spend["byModel"]) == _SPEND_MODELS_NAMED
    assert [row["model"] for row in spend["byModel"]] == [
        f"m{i:02d}" for i in reversed(range(3, _SPEND_MODELS_NAMED + 3))]

    # m02 (300) + m01 (200) + m00 (100) from run `a`, and m00's further 50 from run `b`. Its own
    # model count is what tells a reader those 650 tokens are three models and not one.
    assert spend["byModelOther"] == {
        "models": 3, "tokens": {"sum": 650, "measuredOf": 2, "total": 2}}
    assert sum(row["tokens"]["sum"] for row in spend["byModel"]) == 27000
    assert (sum(row["tokens"]["sum"] for row in spend["byModel"])
            + spend["byModelOther"]["tokens"]["sum"]) == spend["tokens"]["sum"] == 27650

    # And `modelsSeen` still reports the WHOLE roster, so the count and the bounded list together
    # say how many models were withheld even before `byModelOther` is read.
    assert spend["modelsSeen"] == _SPEND_MODELS_NAMED + 3


def test_usage_carrying_no_model_attribute_is_named_rather_than_dropped_or_folded():
    """`harvest_live._usage_by_session` substitutes the literal `"UNKNOWN"` for a usage row with no
    `gen_ai.request.model`, so that string can arrive as a model id.

    Those tokens are real and measured; only their attribution is missing. Dropping them would stop
    the rows adding up to `spend.tokens` — silently understating whichever model a reader is looking
    at — and folding them into `byModelOther` would report "nobody recorded a model" as "the bound
    withheld this". Named, the gap stays visible and stays in the arithmetic.

    CONSTRUCTED, not measured: the 336 h harvest contains zero such rows (all 365 session-model
    pairs carry a model id), so this fixture is the only evidence available for that path.
    """
    runs = [_measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3),
                                "UNKNOWN": _model_counts(1000, 100, 800, 50, 1)})]
    spend = scorecard_to_wire(
        runs, [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])["spend"]

    assert [row["model"] for row in spend["byModel"]] == [_OPUS, "UNKNOWN"]
    assert spend["byModel"][1]["tokens"] == {"sum": 1000, "measuredOf": 1, "total": 1}
    assert spend["byModelOther"]["models"] == 0
    assert sum(row["tokens"]["sum"] for row in spend["byModel"]) == spend["tokens"]["sum"] == 91570


def test_a_run_that_measured_no_usage_names_no_model_rather_than_zero_on_every_model():
    """UNMEASURED is a value, not a zero, and this is the shape that failure would take here.

    `usage_for` returns an EMPTY per-model mapping for a session with no usage block — never one
    zeroed row per model — because such a run did not spend nothing on each model, it recorded
    nothing and does not say which models it did not use. A run whose `resources` lacks the key
    entirely arrives as the UNMEASURED sentinel and must be treated the same way.
    """
    runs = [_measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3)}),
            _resource_run("b", latency_ms=5, tokens=UNMEASURED, by_model={}),
            _resource_run("c", latency_ms=5)]
    spend = scorecard_to_wire(runs, [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9)
                                     for r in ("a", "b", "c")])["spend"]

    assert [row["model"] for row in spend["byModel"]] == [_OPUS]
    assert spend["byModel"][0]["tokens"] == {"sum": 90570, "measuredOf": 1, "total": 1}
    assert spend["byModel"][0]["byCategory"]["cacheRead"]["measuredOf"] == 1
    assert spend["byModelOther"] == {
        "models": 0, "tokens": {"sum": "UNMEASURED", "measuredOf": 0, "total": 1}}


def _one_actors_row(run) -> dict:
    """`_run_row` for a single owned run, taken through `actor_bundles` rather than called directly.

    Through the real entry point because the row is only reachable that way in production, and
    `_owner_split` gates it on both the workload class and a named owner — a test that called the
    private mapper would keep passing if the row stopped being published at all.
    """
    run.actor_id = "actor-1"
    bundles = actor_bundles(result={"proposals": []}, runs=[run],
                            verdicts=[_verdict(run.run_id, Overall.PASS, dispatch=0.9,
                                               delivered=0.9)])
    return bundles["actor-1"]["runs"][0]["spend"]


def test_the_per_model_split_is_absent_from_the_per_actor_run_row():
    """`_run_row`'s `spend` is a shorter list than the population's, and stays shorter in ONE way.

    A per-actor row describes ONE run, where the population-level question the per-model split
    answers — which model this workload's spend concentrates on — is not being asked, and where four
    terms per model would multiply the largest list in the trimmed bundle. `_newest_first` and
    `_fit_actor_bundle` exist because that list is what gives way when an actor's item is too big.

    The four terms the row DOES now carry beyond the original four are scalars and a short id list,
    not a structure per model: measured, they cost 229 bytes on a 917-byte row and take the runs
    that fit one item from 380 to 304, where a per-model split would scale with the roster.
    """
    row_spend = _one_actors_row(
        _measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3)}))
    assert set(row_spend) == ACTOR_RUN_SPEND_KEYS
    assert row_spend["tokens"] == 90570
    # No mapping keyed on a model id anywhere in the row — `models` is a flat list of ids and
    # `byCategory` is keyed on the four categories, neither of which grows a term per model.
    assert row_spend["models"] == [_OPUS]
    assert set(row_spend["byCategory"]) == {"input", "output", "cacheRead", "cacheWrite"}
    assert not [value for value in row_spend.values()
                if isinstance(value, dict) and _OPUS in value]


def test_a_run_row_splits_its_own_tokens_by_category_because_they_are_priced_differently():
    """The projection that makes a PERSONAL cache-reuse figure possible at all.

    The four counts were measured per session all along and dropped on the way to the row, so the
    only cache figure a reader could see was the platform's. They reconstruct this run's own total
    exactly — Bedrock's own arithmetic, the same property `spend.byCategory` has one level up — which
    is what lets a share be drawn over them with no residual term.
    """
    run = _measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3)})
    by_category = _one_actors_row(run)["byCategory"]
    assert by_category == {"input": 4, "output": 831, "cacheRead": 60671, "cacheWrite": 29064}
    assert sum(by_category.values()) == run.resources["tokens"] == 90570


def test_a_run_row_names_the_models_its_own_tokens_went_to():
    """WHICH model, per run, and the two live staging ids used verbatim.

    A list rather than the population's count: `modelsSeen: 2` says a single rate is indefensible,
    and on the reader's own run the actionable fact is which of the two their session spent on.
    """
    run = _measured_run("a", {_OPUS: _model_counts(90570, 831, 60671, 29064, 3),
                              _SONNET: _model_counts(5399, 228, 0, 5169, 1)})
    assert _one_actors_row(run)["models"] == [_OPUS, _SONNET]


def test_a_run_that_named_no_model_reports_the_sentinel_and_never_an_empty_roster():
    """`[]` would be the one value on this row that read as a measurement.

    `usage_for` hands back `models: ()` for a session with no usage record AND for a measured record
    whose per-model mapping was missing, so an empty list here would say "this run used no model"
    where the truth is that nothing named one. Every sibling term on the row spells that as a
    sentinel, and a reader needs one rule.
    """
    for resources in ({"latency_ms": 5}, {"latency_ms": 5, "models": ()},
                      {"latency_ms": 5, "models": UNMEASURED}):
        row_spend = _one_actors_row(_resource_run("a", **resources))
        assert row_spend["models"] == "UNMEASURED", resources
        assert row_spend["models"] != []


def test_a_run_row_carries_the_token_counts_that_say_its_own_price_is_short():
    """Cost's second denominator, per run, because a run cost cannot report a partial bill alone.

    `_cost_usd` skips a model with no rate row rather than failing the run, so a fully measured run
    can contribute a SHORT price — and until these two counts travelled per run, a personal cost sum
    was a silent lower bound with nothing on the row able to say by how much. The pair is a
    measurement even when the priced half is 0: `_cost_usd` returns `(UNMEASURED, 0, n)` for a run it
    tried and could price nothing of, which is a different fact from never having tried.
    """
    row_spend = _one_actors_row(_resource_run(
        "a", latency_ms=5, tokens=90570, cost=0.2327805,
        cost_tokens_priced=60000, cost_tokens_unpriced=30570))
    assert row_spend["cost"] == pytest.approx(0.2327805)
    assert row_spend["costTokensPriced"] == 60000
    assert row_spend["costTokensUnpriced"] == 30570

    # Nothing priced, and it was tried: a real 0 beside a real remainder.
    tried = _one_actors_row(_resource_run("a", latency_ms=5, tokens=90570, cost=UNMEASURED,
                                          cost_tokens_priced=0, cost_tokens_unpriced=90570))
    assert tried["cost"] == "UNMEASURED"
    assert tried["costTokensPriced"] == 0 and tried["costTokensUnpriced"] == 90570


def test_a_run_pricing_never_attempted_reports_both_counts_unmeasured_not_zero():
    """`0 priced, 0 unpriced` would read as an attempt that found nothing to skip.

    That is exactly the shape a `?? 0` on the consumer side would invent, and it is why
    `build_run._UNMEASURED_USAGE` carries the sentinel on both halves rather than zeros. A run with
    no usage record at all is the ordinary case here, not an edge one.
    """
    row_spend = _one_actors_row(_resource_run("a", latency_ms=5))
    for term in ("cost", "costTokensPriced", "costTokensUnpriced", "tokens", "llmCalls"):
        assert row_spend[term] == "UNMEASURED", term
    assert set(row_spend["byCategory"].values()) == {"UNMEASURED"}


def test_the_per_run_cost_token_counts_add_up_to_the_populations_own_pair():
    """The two items must agree about how much of the money is missing.

    The population publishes `spend.cost.tokensPriced` / `tokensUnpriced` as a plain sum over the
    same runs (`_measured_total`), so a personal figure summed from the rows and the shared figure
    are the same arithmetic over the same numbers — and if they ever disagree, one of the two is
    quoting a denominator it did not compute. Two runs, one of them only partly priced.
    """
    runs = [_resource_run("a", latency_ms=5, tokens=90570, cost=0.2327805,
                          cost_tokens_priced=90570, cost_tokens_unpriced=0),
            _resource_run("b", latency_ms=5, tokens=30000, cost=0.05,
                          cost_tokens_priced=20000, cost_tokens_unpriced=10000)]
    for run in runs:
        run.actor_id = "actor-1"
    verdicts = [_verdict(r, Overall.PASS, dispatch=0.9, delivered=0.9) for r in ("a", "b")]
    rows = actor_bundles(result={"proposals": []}, runs=runs,
                         verdicts=verdicts)["actor-1"]["runs"]
    cost = scorecard_to_wire(runs, verdicts)["spend"]["cost"]

    assert sum(row["spend"]["costTokensPriced"] for row in rows) == cost["tokensPriced"] == 110570
    assert sum(row["spend"]["costTokensUnpriced"] for row in rows) == cost["tokensUnpriced"] == 10000


def test_a_missing_resource_key_is_unmeasured_not_absent():
    """`Run.resources` may not carry a key at all. That is the same fact as UNMEASURED, and it
    must not reach the wire as `null`."""
    card = scorecard_to_wire([_resource_run("a")],
                             [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert card["spend"]["latencyMs"]["sum"] == "UNMEASURED"


def test_each_workload_class_carries_its_own_rate_and_what_gated_it():
    """The pass/fail split is only meaningful WITHIN a class, because each class is gated on a
    different objective. Without `gating`, `tool_discovery: 1 PASS` is indistinguishable from a
    class that was never checked."""
    verdicts = [
        _verdict("c", Overall.FAIL, dispatch=0.9, delivered=0.1),
        _verdict("t", Overall.PASS, workload="tool_discovery", gating=("ToolDiscoveryValidator",),
                 results=[_tier("ToolDiscoveryValidator", 1.0, Outcome.PASS)]),
    ]
    card = scorecard_to_wire([], verdicts)
    assert card["byWorkloadClass"]["conversational"]["passRate"] == 0.0
    assert card["byWorkloadClass"]["tool_discovery"]["passRate"] == 1.0
    assert card["byWorkloadClass"]["tool_discovery"]["gating"] == [["ToolDiscoveryValidator"]]
    assert card["byWorkloadClass"]["conversational"]["gatingIsMixed"] is False


def test_a_mixed_gating_set_is_reported_rather_than_collapsed():
    """Mirrors `mine_local._workload_breakdown`: keeping only the first set would describe a
    mixed population as uniform, which is the one thing this field must not be able to do."""
    verdicts = [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                _verdict("b", Overall.PASS, dispatch=0.9, delivered=0.9,
                         gating=("dispatch_correct",))]
    entry = scorecard_to_wire([], verdicts)["byWorkloadClass"]["conversational"]
    assert entry["gatingIsMixed"] is True
    assert len(entry["gating"]) == 2


def test_an_empty_population_is_zeros_and_sentinels_never_a_fabricated_mean():
    card = scorecard_to_wire([], [])
    assert card["population"] == {"total": 0, "pass": 0, "fail": 0, "unscored": 0}
    assert card["passRate"] is None
    assert card["tiers"]["dispatch"] == {"mean": "UNMEASURED", "measuredOf": 0, "total": 0}
    # The token counts are a real 0 rather than the sentinel: no run priced and none went unpriced,
    # which is a measurement over an empty population. They are what a reader divides by, so a
    # sentinel there would leave the figure beside them uncomparable rather than uncomputed.
    assert card["spend"]["cost"] == {"sum": "UNMEASURED", "measuredOf": 0, "total": 0,
                                     "tokensPriced": 0, "tokensUnpriced": 0}
    assert card["window"] == "UNKNOWN"
    assert card["byWorkloadClass"] == {}
    assert card["outcomes"] == {"failedGating": {}, "unmeasured": {}}


# --- outcomes: WHY runs are failing, by evaluator name -----------------------
def test_outcomes_name_the_failing_gating_evaluator_rather_than_parsing_prose():
    """The structural half of "why", derived from `EvaluatorResult.outcome`.

    `Verdict.reason` says the same thing in words and carries an `(on partial evidence: …)`
    suffix, so counting reason STRINGS would fragment one cause across near-identical keys — and
    the prose half may not travel on a globally-readable item anyway.
    """
    verdicts = [
        # delivery MEASURED a failure; dispatch could not be measured at all.
        _verdict("r1", Overall.FAIL, dispatch=None, delivered=0.1),
        _verdict("r2", Overall.PASS, dispatch=0.9, delivered=0.9),
    ]
    card = scorecard_to_wire([], verdicts)
    assert card["outcomes"]["failedGating"] == {"dispatch_correct": 0, "results_delivered": 1}
    assert card["outcomes"]["unmeasured"] == {"dispatch_correct": 1, "results_delivered": 0}


def test_a_gating_check_that_never_failed_is_a_zero_not_an_absent_key():
    """`0` means "gated, and it never failed"; an absent key would mean "never gated here". Two
    different facts, and a reader cannot recover the difference from a dict that omits both."""
    card = scorecard_to_wire([], [_verdict("r", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert card["outcomes"]["failedGating"] == {"dispatch_correct": 0, "results_delivered": 0}


def test_a_run_failing_two_gating_checks_counts_under_each():
    """So these figures do NOT sum to `population.fail` — they answer "which check is failing",
    not "how many runs failed", and the population count is right there to compare against."""
    card = scorecard_to_wire([], [_verdict("r", Overall.FAIL, dispatch=0.2, delivered=0.1)])
    assert card["outcomes"]["failedGating"] == {"dispatch_correct": 1, "results_delivered": 1}
    assert card["population"]["fail"] == 1


def test_a_non_gating_red_is_not_counted_as_a_failing_gate():
    """`DagCompletionValidator` REDs on runs that pass overall (`contract.FacetKind`). Counting an
    informational check here would report a failure the gate never treated as one."""
    verdicts = [_verdict(
        "r", Overall.PASS, gating=("dispatch_correct", "results_delivered"),
        results=[_tier("dispatch_correct", 0.9, Outcome.PASS),
                 _tier("results_delivered", 0.9, Outcome.PASS),
                 _tier("DagCompletionValidator", 0.0, Outcome.FAIL)])]
    card = scorecard_to_wire([], verdicts)
    assert "DagCompletionValidator" not in card["outcomes"]["failedGating"]
    assert card["outcomes"]["failedGating"] == {"dispatch_correct": 0, "results_delivered": 0}


def test_outcomes_follow_each_verdicts_own_gating_set():
    """Gating is per workload class, so "which check failed" has to be asked per verdict. A
    population-wide gating tuple would credit a class with a check it was never held to."""
    verdicts = [
        _verdict("c", Overall.FAIL, dispatch=0.9, delivered=0.1),
        _verdict("t", Overall.FAIL, workload="tool_discovery",
                 gating=("ToolDiscoveryValidator",),
                 results=[_tier("ToolDiscoveryValidator", 0.0, Outcome.FAIL),
                          # present but NOT gating for this class: must not be counted.
                          _tier("results_delivered", 0.1, Outcome.FAIL)]),
    ]
    card = scorecard_to_wire([], verdicts)
    assert card["outcomes"]["failedGating"] == {"ToolDiscoveryValidator": 1,
                                               "dispatch_correct": 0, "results_delivered": 1}


def test_the_population_window_is_the_union_of_the_member_windows():
    runs = [_run("a"), _run("b", derived=False)]
    card = scorecard_to_wire(runs, [_verdict("a", Overall.PASS), _verdict("b", Overall.PASS)])
    assert card["window"]["provenance"] == "union_of_member_windows"
    assert card["window"]["derived"] is True   # one of the two was reconstructed
    assert card["derivedWindows"] == 1


# --- the content rule on the ONE GLOBAL item ---------------------------------
def test_the_global_scorecard_contains_no_user_derived_text():
    """The SNAPSHOT is a single `actorId="__system__"` item served to EVERY actor, so any string
    derived from user input or model prose leaks one tenant's work to all of them.

    The judge is fed the goal and the response and asked for a one-sentence reason
    (`judge.py:46-57`), so its `EvaluatorResult.explanation` restates the user's request BY
    CONSTRUCTION. This test builds the population from `golden.jsonl`'s REAL goals, hangs a
    judge-style explanation off each verdict, and asserts none of it survives into the
    scorecard. Aggregates are counts, rates, enum values, evaluator names and timestamps —
    nothing else may travel here.
    """
    scenarios = load_golden()
    assert scenarios and all(s.goal for s in scenarios), (
        "golden.jsonl carries the real user goals; without them this test is vacuous")

    runs, verdicts = [], []
    for scenario in scenarios:
        # `judge.py` prefixes a multi-run explanation "[mean of 3 runs]" and then restates the
        # request. This mimics that shape over the REAL goal text.
        prose = (f"[mean of 3 runs] The user asked: {scenario.goal[:120]} — the agent dispatched "
                 f"correctly but no usable result arrived.")
        runs.append(Run(run_id=scenario.scenario_id, goal=scenario.goal,
                        response=f"Here is the proxy for {scenario.goal[:40]}",
                        resources={"latency_ms": 1000},
                        window=RunWindow(start="2026-08-13T18:00:00+00:00",
                                         end="2026-08-13T18:40:00+00:00")))
        verdicts.append(_verdict(
            scenario.scenario_id, Overall.FAIL,
            reason="gating: results_delivered:FAIL (on partial evidence: dispatch_correct:NO_DATA)",
            results=[_tier("dispatch_correct", 0.9, Outcome.PASS),
                     EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.4, prose)]))

    dumped = json.dumps(scorecard_to_wire(runs, verdicts))

    # Every distinctive token of every real goal, plus the judge's own sentence and the
    # response text. A leak of any one of them is the defect.
    #
    # The last three probe `Verdict.reason`: `outcomes` reports WHY runs failed, and it must do
    # so from `EvaluatorResult.outcome` by evaluator NAME. No reason string may ride along —
    # partly because it is prose, and partly because counting it would fragment one cause across
    # its `(on partial evidence: …)` variants.
    probes = {"The user asked", "the agent dispatched", "Here is the proxy", "mean of 3 runs",
              "on partial evidence", "gating: results_delivered", ":FAIL"}
    for scenario in scenarios:
        probes.update(word for word in scenario.goal.split() if len(word) >= 12)
        probes.add(scenario.scenario_id)          # not even a session id list belongs here
    leaked = sorted(probe for probe in probes if probe in dumped)
    assert not leaked, f"user-derived text reached the global SNAPSHOT scorecard: {leaked}"

    # ...and the test is not vacuous: the aggregates it SHOULD carry are there, including the
    # structural "why" — the failing evaluator is named while its sentence is not.
    card = json.loads(dumped)
    assert card["population"]["fail"] == len(scenarios)
    assert card["tiers"]["delivery"]["mean"] == pytest.approx(0.4)
    assert card["outcomes"]["failedGating"]["results_delivered"] == len(scenarios)


# --- the efficiency path's aggregate -----------------------------------------
def _real_waste_artifact() -> dict:
    """The artifact's `waste` key, over the REAL golden spans, built by the production path.

    `waste.find_waste` + `mine_local._jsonable` are exactly what `run_pipeline` writes, so the
    guard below tests the shipped serialization rather than a plausible imitation of it.
    """
    runs = []
    for scenario in load_golden():
        session = scenario.source_session or scenario.scenario_id
        runs.append(Run(run_id=session, plan=real_plan(session),
                        trajectory=real_trajectory(session)))
    return _jsonable(find_waste(runs))


def test_waste_aggregate_carries_no_tool_sequence_detail():
    """The tenant-isolation guard, and the reason this aggregate is counts-only.

    Every finding's `detail` names tool names, and `tool_name_sequence` strips
    `mcp tools/call <target>___<tool>` down to the bare tool name — so a tool name from a
    USER-REGISTERED MCP server is user-derived. The SNAPSHOT is one `actorId="__system__"` item
    served to every actor, so neither a named sequence nor a run id may travel on it.

    Built from the REAL golden trajectories, so the probes are the tool names the fixtures actually
    contain (`process_media`, `get_download_url`, …) rather than invented ones.
    """
    waste = _real_waste_artifact()
    assert waste["findings"], "the golden spans must still produce findings, or this is vacuous"

    dumped = json.dumps(waste_to_wire(waste))
    probes = set()
    for finding in waste["findings"]:
        probes.add(finding["detail"])
        probes.update(part.split(" x")[0] for part in finding["detail"].split(" -> "))
        probes.add(finding["run_id"])
        probes.add(finding["confidence"])
        probes.add(finding["kind"] + "_detail_should_not_exist")
    leaked = sorted(probe for probe in probes if probe and probe in dumped)
    assert not leaked, f"user-derivable text reached the global SNAPSHOT waste block: {leaked}"

    # ...and not vacuous: the counts it SHOULD carry are there, with their denominator.
    block = json.loads(dumped)
    assert set(block) == WASTE_KEYS
    assert block["runs"] == len(load_golden())
    assert block["runsWithWaste"] == len({f["run_id"] for f in waste["findings"]})
    assert block["totalCalls"] == sum(f["calls"] for f in waste["findings"])


def test_the_waste_aggregate_names_every_kind_even_when_a_signal_found_nothing():
    """Absent != empty. A kind missing from `byKind` would be indistinguishable from a producer
    that has no such signal, so every kind is present with zeros."""
    block = waste_to_wire(_real_waste_artifact())
    assert set(block["byKind"]) == set(WASTE_KINDS)
    assert all(set(entry) == WASTE_KIND_KEYS | WASTE_KIND_OPTIONAL_KEYS
               for entry in block["byKind"].values())
    # The golden set repeats work and never leaves a dispatch unretrieved (its unpaired-dispatch
    # sessions are not golden-set members), so this is a real zero next to a real count.
    assert block["byKind"]["repeated_sequence"]["findings"] > 0
    assert block["byKind"]["abandoned_dispatch"] == {
        "findings": 0, "runs": 0, "calls": 0,
        "wallMs": {"sum": "UNMEASURED", "measuredOf": 0, "total": 0},
        # Present on a kind that found NOTHING, because it describes the kind and not the findings.
        "plainSummary": "A job's result was never collected before the request ended"}


def test_the_waste_block_is_emitted_for_an_artifact_that_has_no_waste_key():
    """A bundle whose producer predates the efficiency path still gets the key, empty-but-complete
    — a consumer must never have to branch on whether it exists (the `scorecard` rule)."""
    block = waste_to_wire(None)
    assert set(block) == WASTE_KEYS
    assert block == {"runsWithWaste": 0, "runs": 0, "runsNotScanned": 0, "totalCalls": 0,
                     "totalWallMs": {"sum": "UNMEASURED", "measuredOf": 0, "total": 0},
                     "byKind": {kind: {"findings": 0, "runs": 0, "calls": 0,
                                       "wallMs": {"sum": "UNMEASURED", "measuredOf": 0,
                                                  "total": 0},
                                       "plainSummary": WASTE_KIND_PLAIN[kind]}
                                for kind in WASTE_KINDS}}


def test_every_waste_kind_the_detectors_can_emit_ships_one_plain_sentence():
    """A media reader must never be shown `repeated_sequence`, so every kind carries prose.

    The kind set is taken from `waste.WASTE_KINDS` — the detectors' own declaration — rather than
    from the prose table, so a FOURTH kind added to the detectors without a sentence fails here
    instead of reaching a reader as a machine name. Sentences are compared against literals in this
    file for the reason the field sets at the top are: a table read out of the producer would agree
    with itself whatever it said.
    """
    from improve.dto import _WASTE_KIND_PROSE

    assert _WASTE_KIND_PROSE == WASTE_KIND_PLAIN
    assert set(_WASTE_KIND_PROSE) == set(WASTE_KINDS)
    block = waste_to_wire(_real_waste_artifact())
    for kind in WASTE_KINDS:
        assert block["byKind"][kind]["plainSummary"] == WASTE_KIND_PLAIN[kind]


@pytest.mark.skipif(not os.path.exists(default_registry_path()),
                    reason="the sigma registry (ideas/sigma-registry.yaml) lives outside the repo (OURS-2)")
def test_no_plain_waste_sentence_carries_the_engines_own_vocabulary():
    """The audience is a media editor, so a sentence that leaks an identifier has failed at its job.

    Three families of identifier, all of which a plausible-looking sentence could pick up: the kind
    names themselves, anything snake_case (every facet and kind in this loop is), and the Σ component
    ids read from the SHIPPED registry rather than a fixture, so the check covers the ids that
    actually exist. Tool names are covered separately and for a stronger reason by
    `test_waste_aggregate_carries_no_tool_sequence_detail`, which dumps the whole block.
    """
    from improve.dto import _WASTE_KIND_PROSE

    component_ids = [c.component_id for c in SigmaRegistry.load(default_registry_path()).all()]
    assert component_ids, "the shipped registry must load, or this check is vacuous"
    for kind, sentence in _WASTE_KIND_PROSE.items():
        assert "_" not in sentence, f"{kind} names an identifier"
        assert not any(k in sentence for k in WASTE_KINDS), f"{kind} names a kind"
        assert not any(cid in sentence for cid in component_ids), f"{kind} names a component"
        assert sentence[0].isupper() and not sentence.endswith("."), f"{kind} is a table row"


def test_an_unmapped_waste_kind_omits_its_sentence_rather_than_naming_itself(monkeypatch):
    """Absence is absence. A kind with no prose has no `plainSummary` at all.

    Not an empty string and not the raw kind name: a consumer branching on whether the key is there
    is then correct, and the alternative is a reader being shown an engine identifier. Driven by
    adding a kind the prose table does not know, which is exactly what a later detector looks like.
    """
    from improve import dto as dto_module

    monkeypatch.setattr(dto_module, "WASTE_KINDS", tuple(WASTE_KINDS) + ("mystery_kind",))
    block = waste_to_wire(None)

    assert set(block["byKind"]) == set(WASTE_KINDS) | {"mystery_kind"}
    entry = block["byKind"]["mystery_kind"]
    assert set(entry) == WASTE_KIND_KEYS
    assert "plainSummary" not in entry
    assert "mystery_kind" not in json.dumps(list(entry.values())), "the raw name is not a fallback"
    # The mapped kinds are untouched by the unmapped one's presence.
    assert block["byKind"]["repeated_sequence"]["plainSummary"] == WASTE_KIND_PLAIN[
        "repeated_sequence"]


def test_totalCalls_counts_a_call_two_kinds_charged_only_once():
    """`byKind` is exact per kind and therefore does NOT sum to `totalCalls`.

    The kinds are independent observations of one trajectory: an errored call can sit inside a
    repeated block, and on the complete 336 h population 11 of 107 affected runs have that overlap.
    So the total is the UNION of the calls charged per run — otherwise the headline over-charges.
    """
    waste = {
        "runs_examined": 1,
        "runs_not_scanned": [],
        "findings": [
            {"run_id": "r1", "kind": "repeated_sequence", "calls": 3, "wall_ms": 30,
             "occurrences": 2, "detail": "a -> b", "confidence": "", "charged": [4, 5, 6]},
            {"run_id": "r1", "kind": "error_then_retry", "calls": 1, "wall_ms": 10,
             "occurrences": 1, "detail": "a", "confidence": "", "charged": [5]},
        ],
    }
    block = waste_to_wire(waste)
    assert block["byKind"]["repeated_sequence"]["calls"] == 3
    assert block["byKind"]["error_then_retry"]["calls"] == 1
    assert block["totalCalls"] == 3, "call 5 is charged by both kinds and counted once"
    # Two findings, ONE run. `measuredOf`/`total` count runs like every other `_sum_term` on this
    # wire, so they read 1 — and match `runsWithWaste` beside them, which is the check a reader can
    # do without leaving the object. The SUM is still every measured millisecond.
    assert block["totalWallMs"] == {"sum": 40, "measuredOf": 1, "total": 1}
    assert block["totalWallMs"]["total"] == block["runsWithWaste"]


def test_an_unmeasured_wall_ms_is_not_folded_into_a_total():
    """Invariant 1, at the aggregate: a finding whose spans carry no duration contributes
    UNMEASURED, and the sum carries the denominator rather than pretending to be complete."""
    waste = {
        "runs_examined": 2,
        "runs_not_scanned": [],
        "findings": [
            {"run_id": "r1", "kind": "repeated_sequence", "calls": 2, "wall_ms": "UNMEASURED",
             "occurrences": 2, "detail": "a -> b", "confidence": "", "charged": [0, 1]},
            {"run_id": "r2", "kind": "repeated_sequence", "calls": 2, "wall_ms": 500,
             "occurrences": 2, "detail": "a -> b", "confidence": "", "charged": [0, 1]},
        ],
    }
    block = waste_to_wire(waste)
    assert block["totalWallMs"] == {"sum": 500, "measuredOf": 1, "total": 2}
    assert block["totalCalls"] == 4


def test_a_kinds_wall_ms_denominator_counts_runs_like_every_sibling_on_this_wire():
    """`measuredOf`/`total` mean RUNS everywhere on this wire, and this term used to mean findings.

    Live, `error_then_retry` shipped `wallMs {measuredOf: 18, total: 18}` next to `runs: 4` — a
    reader applying the documented convention got a denominator 4.5x too large. The fix folds the
    sum per run first, so the unit of the sum and the unit of its denominator are one fact, and each
    denominator is checkable against a sibling on the same object.
    """
    waste = {
        "runs_examined": 5,
        "runs_not_scanned": [],
        "findings": [
            {"run_id": "r1", "kind": "error_then_retry", "calls": 1, "wall_ms": 10,
             "occurrences": 1, "detail": "a", "confidence": "", "charged": [0]},
            {"run_id": "r1", "kind": "error_then_retry", "calls": 1, "wall_ms": 20,
             "occurrences": 1, "detail": "b", "confidence": "", "charged": [1]},
            {"run_id": "r2", "kind": "error_then_retry", "calls": 1, "wall_ms": 30,
             "occurrences": 1, "detail": "c", "confidence": "", "charged": [2]},
        ],
    }
    entry = waste_to_wire(waste)["byKind"]["error_then_retry"]
    assert entry["findings"] == 3 and entry["runs"] == 2
    assert entry["wallMs"] == {"sum": 60, "measuredOf": 2, "total": 2}
    assert entry["wallMs"]["total"] == entry["runs"] != entry["findings"]


def test_one_untimed_finding_unmeasures_its_own_run_and_no_other():
    """All-or-nothing per RUN, the rule `waste._sum_durations` applies per finding.

    A run with one timed finding and one untimed one has a waste wall time of "at least X", and
    reporting X while counting the run as measured would publish the lower bound as the figure. The
    OTHER run's measurement is untouched — the sentinel is scoped to what it describes.
    """
    waste = {
        "runs_examined": 2,
        "runs_not_scanned": [],
        "findings": [
            {"run_id": "r1", "kind": "repeated_sequence", "calls": 2, "wall_ms": 400,
             "occurrences": 2, "detail": "a -> b", "confidence": "", "charged": [0, 1]},
            {"run_id": "r1", "kind": "repeated_sequence", "calls": 2, "wall_ms": "UNMEASURED",
             "occurrences": 2, "detail": "c -> d", "confidence": "", "charged": [2, 3]},
            {"run_id": "r2", "kind": "repeated_sequence", "calls": 2, "wall_ms": 500,
             "occurrences": 2, "detail": "a -> b", "confidence": "", "charged": [0, 1]},
        ],
    }
    entry = waste_to_wire(waste)["byKind"]["repeated_sequence"]
    assert entry["wallMs"] == {"sum": 500, "measuredOf": 1, "total": 2}, "r2's 500 still counts"
    # And the sentinel cannot be undone by a later measured finding on the same run, so the answer
    # does not depend on the order the harvest happened to list them in.
    reversed_first = {**waste, "findings": list(reversed(waste["findings"]))}
    assert waste_to_wire(reversed_first)["byKind"]["repeated_sequence"]["wallMs"] == entry["wallMs"]


# --- admitted patterns that no Σ component owns -------------------------------
# A REAL grouped pattern, copied verbatim out of a harvest run's `mine_result_live.json`
# (REQ-017: real harvested data, never fabricated). Its one facet is
# `dispatch_unpaired_result_unobserved`, which `propose._FACET_KEYWORDS` leaves unmapped
# DELIBERATELY — spans cannot tell an unfetched result from one returned inline, so drafting a
# remedy for it would be the confident-but-unsupported move the facet pair exists to stop. So
# `propose_all` returns no proposal and records why, which is exactly the case this bucket
# reports. Note `evidence_by_run`: real span indices and a hedge sentence. That is the text the
# wire record must NOT carry.
_REAL_UNOWNED_PATTERN = {
    "shape": ["dispatch_unpaired_result_unobserved"],
    "symptom": ["check_failed:AsyncJobFlowValidator", "check_failed:DagCompletionValidator",
                "tier_failed:dispatch_correct", "tier_failed:results_delivered"],
    "member_run_ids": ["58dd92d2-67ef-4410-8c21-4440b4e0a65c"],
    "pass_hits": 0,
    "controls_excluded": 3,
    "equally_discriminative": [],
    "heuristic_facets": ["dispatch_unpaired_result_unobserved"],
    "evidence_by_run": {
        "58dd92d2-67ef-4410-8c21-4440b4e0a65c": {
            "dispatch_unpaired_result_unobserved":
                "5 of 5 dispatch(es), at span(s) [46, 79, 88, 109, 134], have no later "
                "retrieval, and this session retrieved 0 result(s) in total; the results may "
                "have been returned inline, which spans cannot distinguish from a leak",
        },
    },
}


def _unowned(registry, shape=None):
    """``propose_all`` over a pattern, asserted to actually be unattributable.

    ``shape`` overrides the facet so each of the three CAUSES can be provoked with real facet
    names rather than by hand-writing the record the producer is supposed to write.
    """
    pattern = dict(_REAL_UNOWNED_PATTERN)
    if shape is not None:
        pattern["shape"] = list(shape)
    proposals, unattributed = propose_all([pattern], registry)
    assert not proposals and len(unattributed) == 1, (
        "the fixture must be a pattern the proposer CANNOT attribute, or this test proves "
        "nothing about the unattributed path")
    return unattributed


def _plain(registry, shape=None) -> dict:
    """The single wire record for an unattributable pattern."""
    wire = to_wire(result={"proposals": [], "unattributed": _unowned(registry, shape)}, runs=[],
                   registry=registry, created_at="2026-08-25T04:00:00Z")
    return wire["patterns"]["unattributed"][0]


def test_unattributed_patterns_reach_the_wire_with_their_reason(registry):
    """The defect this closes: `propose_all` computed an honest record, `mine_local` wrote it to
    the artifact, and `to_wire` dropped it — so the reasoning existed everywhere except on the
    one surface a user reads."""
    wire = to_wire(result={"proposals": [], "unattributed": _unowned(registry)}, runs=[],
                   registry=registry, created_at="2026-08-25T04:00:00Z")

    assert len(wire["patterns"]["unattributed"]) == 1
    entry = wire["patterns"]["unattributed"][0]
    assert set(entry) >= UNATTRIBUTED_KEYS
    assert set(entry) <= UNATTRIBUTED_KEYS | UNATTRIBUTED_OPTIONAL_KEYS
    assert entry["shapeFacets"] == ["dispatch_unpaired_result_unobserved"]
    # Spelled with the same joiner as `admitted`, so one shape cannot read two ways.
    assert entry["shape"] == "dispatch_unpaired_result_unobserved"
    # The COUNT, not the ids: which sessions is per-row exposure on an item every actor reads.
    assert entry["runCount"] == len(_REAL_UNOWNED_PATTERN["member_run_ids"])
    # The engine's OWN words, carried through, naming the facet nothing owns — which is the
    # whole point: a bucket saying "1 pattern" without the reason is not an explanation.
    assert "no Σ component owns this pattern" in entry["reason"]
    assert "dispatch_unpaired_result_unobserved" in entry["reason"]
    json.dumps(wire)  # the payload is written with json.dump; this is that contract


# --- the three causes, each with its own plain sentence -----------------------
def test_a_facet_with_no_search_terms_says_nothing_is_catalogued_as_the_owner(registry):
    """Cause 1. `dispatch_unpaired_result_unobserved` has no `_FACET_KEYWORDS` entry, so no
    search was even possible — the plain sentence must not imply one ran and came back empty."""
    entry = _plain(registry)
    assert "no registry search terms are defined" in entry["reason"]   # admin wording, kept
    assert entry["plainReason"] == (
        "No part of the system has been catalogued as the owner of this kind of failure yet, "
        "so there is nothing here to recommend a change to.")


def test_terms_that_match_no_component_say_so_without_claiming_none_exist(registry):
    """Cause 2. `has_non_terminal_node` IS mapped (ttl / stuck / pending / readiness), and this
    registry holds none of those words, so terms were searched and matched nothing. A different
    fact from cause 1, and it gets a different sentence."""
    entry = _plain(registry, shape=["has_non_terminal_node"])
    assert "search terms are defined but matched no component" in entry["reason"]
    assert entry["plainReason"] == (
        "The parts of the system this points at are catalogued, but none of them matches this "
        "pattern closely enough to recommend a change with any confidence.")


def test_an_unreadable_registry_dominates_and_never_claims_nothing_matched(tmp_path):
    """Cause 3, and the reason it OUTRANKS the other two rather than qualifying them.

    With no catalogue to search, "terms matched no component" is an artifact of an empty
    registry, not a finding. The engine's `reason` composes both clauses, which is fine for an
    admin reading facet names; a plain sentence cannot, because "none of them matches" would be
    a statement about a search that never happened.
    """
    absent = SigmaRegistry.load(str(tmp_path / "not-there.yaml"), missing_ok=True)
    assert absent.meta.get("unavailable") and len(absent) == 0
    entry = _plain(absent, shape=["has_non_terminal_node"])
    # The engine still composes both clauses — unchanged, and still shown on Findings.
    assert "matched no component" in entry["reason"]
    assert "the registry was unavailable" in entry["reason"]
    # The plain sentence says only the true thing, and calls it a gap in the loop.
    assert entry["plainReason"] == (
        "The catalogue of system components could not be read on this run, so nothing could be "
        "matched to this pattern at all. That is a temporary gap in the loop, not a finding "
        "about your agent.")
    assert "matches" not in entry["plainReason"]


def test_plain_reason_is_omitted_rather_than_guessed_when_the_cause_is_unlabelled(registry):
    """A record written before `propose_all` labelled the cause, or by a newer producer using a
    label this mapper does not know. Omitted, so the Inbox falls back to `reason` — never a
    sentence picked because it was the most likely of three."""
    record = {"shape": ["x"], "member_run_ids": ["r"], "reason": "no Σ component owns this"}
    unlabelled = to_wire(result={"proposals": [], "unattributed": [record]}, runs=[],
                         registry=registry,
                         created_at="2026-08-25T04:00:00Z")["patterns"]["unattributed"][0]
    assert "plainReason" not in unlabelled

    future = to_wire(result={"proposals": [], "unattributed": [{**record, "cause": "brand_new"}]},
                     runs=[], registry=registry,
                     created_at="2026-08-25T04:00:00Z")["patterns"]["unattributed"][0]
    assert "plainReason" not in future


def test_no_plain_sentence_carries_the_engines_vocabulary(registry):
    """The point of `plainReason` is that the Inbox does not show a media editor a facet name,
    a REQ id, a Σ, or a bracketed Python list. If one leaks in, the surface may as well render
    `reason` and the rewrite is buying nothing."""
    from improve.dto import _UNATTRIBUTED_PLAIN_REASON

    facet_names = {"dispatch_unpaired_result_unobserved", "has_non_terminal_node", "facet",
                   "Σ", "REQ-", "[", "]", "_"}
    for cause, sentence in _UNATTRIBUTED_PLAIN_REASON.items():
        leaked = sorted(token for token in facet_names if token in sentence)
        assert not leaked, f"{cause}'s plain sentence carries engine vocabulary: {leaked}"
        assert sentence.endswith(".") and sentence[0].isupper(), (
            f"{cause}'s plain sentence is not a sentence")


def test_an_empty_unattributed_bucket_is_emitted_never_omitted(registry):
    """Absent ≠ empty (`AGENTS.md`). `[]` says "every admitted pattern found an owner", which is
    the golden set's real state; a MISSING key says "this bundle predates the producer change".
    Only a consumer that is never handed the second in place of the first can tell them apart,
    so the producer always emits the key."""
    wire = to_wire(result={"proposals": []}, runs=[], registry=registry,
                   created_at="2026-08-25T04:00:00Z")
    assert wire["patterns"]["unattributed"] == []


def test_the_global_unattributed_bucket_contains_no_user_derived_text(registry):
    """The content rule, on the newest block. Same argument as
    `test_the_global_scorecard_contains_no_user_derived_text`: one `actorId="__system__"` item
    is served to every actor, so a string derived from user input or model prose leaks one
    tenant's work to all of them.

    Not vacuous in two directions. `to_wire` receives the `runs`, so a mapper that reached for
    `runs_by_id[...]` to enrich a row — as `evidence_quality` legitimately does for a
    suggestion — would leak the goal and the response, and the member run ids here are the REAL
    golden scenario ids, so that path is live. And the source pattern carries real facet
    EVIDENCE prose (span indices, node ids), which `propose_all` deliberately drops and the
    mapper must not restore.
    """
    scenarios = load_golden()
    assert scenarios and all(s.goal for s in scenarios), (
        "golden.jsonl carries the real user goals; without them this test is vacuous")

    pattern = dict(_REAL_UNOWNED_PATTERN,
                   member_run_ids=[s.scenario_id for s in scenarios],
                   evidence_by_run={s.scenario_id: _REAL_UNOWNED_PATTERN["evidence_by_run"][
                       "58dd92d2-67ef-4410-8c21-4440b4e0a65c"] for s in scenarios})
    proposals, unattributed = propose_all([pattern], registry)
    assert not proposals and unattributed

    runs = [Run(run_id=s.scenario_id, goal=s.goal,
                response=f"Here is the proxy for {s.goal[:40]}") for s in scenarios]
    dumped = json.dumps(to_wire(result={"proposals": [], "unattributed": unattributed},
                                runs=runs, registry=registry,
                                created_at="2026-08-25T04:00:00Z")["patterns"]["unattributed"])

    probes = {"Here is the proxy",
              # the facet's own evidence prose: real span indices and the inline-return hedge
              "have no later retrieval", "returned inline", "span(s)"}
    for scenario in scenarios:
        probes.update(word for word in scenario.goal.split() if len(word) >= 12)
    leaked = sorted(probe for probe in probes if probe in dumped)
    assert not leaked, f"user-derived text reached the global unattributed bucket: {leaked}"

    # ...and what SHOULD be there is: the harness facet name, and the engine's reason.
    entry = json.loads(dumped)[0]
    assert entry["shapeFacets"] == ["dispatch_unpaired_result_unobserved"]
    assert "no registry search terms are defined" in entry["reason"]


# --- component health -------------------------------------------------------
def _health_suggestion(target, member_run_ids, suggestion_id=None):
    """A wire suggestion plus the member map that goes WITH it, never inside it.

    Two values, because the wire row genuinely does not carry its member ids any more — they are
    session ids on an item every actor reads. A helper that kept them on the row would be looser
    than the real wire and would hide exactly the regression
    `test_component_health_survives_the_real_wire_shape` exists to catch (`AGENTS.md`: a fake
    looser than the real API hides total breakage).
    """
    sid = suggestion_id or f"s-{target}"
    return {"targetId": target, "suggestionId": sid}, {sid: list(member_run_ids)}


def test_component_health_is_none_when_no_run_attributes_and_populated_when_one_does(registry):
    """`None` means "nothing attributes here, we do not know how it is doing". `{"runs": 0}`
    would claim we looked and measured nothing — absent ≠ empty (`AGENTS.md`)."""
    runs_by_id = {"r1": _resource_run("r1")}
    verdicts_by_id = {"r1": _verdict("r1", Overall.FAIL, dispatch=0.9, delivered=0.1)}
    wire = components_to_wire(
        registry, [_wire(registry, _proposal(target="locked-rule"), runs_by_id=runs_by_id)],
        runs_by_id=runs_by_id, verdicts_by_id=verdicts_by_id)

    rows = {r["componentId"]: r for section in wire["yours"] for r in section["components"]}
    rows.update({r["componentId"]: r for r in wire["fixFirst"]["components"]})
    # `_proposal`'s signature names run-1, which is NOT in the population, so even the targeted
    # component stays unknown rather than claiming a measurement.
    assert rows["locked-rule"]["health"] is None
    assert rows["editable-persona"]["health"] is None

    attributed, members = _health_suggestion("locked-rule", ["r1"])
    health = component_health([attributed], runs_by_id, verdicts_by_id, members)
    assert health == {"locked-rule": {"runs": 1, "delivered": 0, "deliveredOf": 1}}


def test_component_health_delivered_denominator_is_zero_when_delivery_was_never_measured():
    """`runs` is not the denominator of the delivered share, and a row saying `runs: 2` with
    `deliveredOf: 0` is stating exactly that.

    A zero denominator, not a `None` share: it is the count of runs whose delivery tier returned a
    PASS or a FAIL, we did look, and the answer is none of them. The absence this map still carries
    is a different one — a component NO run attributes to, which is missing from the map entirely.
    """
    runs_by_id = {"r1": _resource_run("r1"), "r2": _resource_run("r2")}
    verdicts_by_id = {"r1": _verdict("r1", Overall.UNSCORED, dispatch=0.9),
                      "r2": _verdict("r2", Overall.UNSCORED, dispatch=0.9)}
    suggestion, members = _health_suggestion("c", ["r1", "r2"])
    health = component_health([suggestion], runs_by_id, verdicts_by_id, members)
    assert health == {"c": {"runs": 2, "delivered": 0, "deliveredOf": 0}}


def test_component_health_counts_only_runs_in_this_population():
    """A store row's member run may predate this population. It cannot contribute a
    measurement, and a component with no surviving member is absent from the map entirely."""
    runs_by_id = {"r1": _resource_run("r1")}
    verdicts_by_id = {"r1": _verdict("r1", Overall.PASS, dispatch=0.9, delivered=0.9)}
    kept, kept_members = _health_suggestion("c", ["r1", "aged-out"])
    stale, stale_members = _health_suggestion("gone", ["aged-out"])
    health = component_health([kept, stale], runs_by_id, verdicts_by_id,
                              {**kept_members, **stale_members})
    assert health == {"c": {"runs": 1, "delivered": 1, "deliveredOf": 1}}
    assert "gone" not in health


# --- the whole payload ------------------------------------------------------
def test_to_wire_emits_the_datasets_the_ui_reads(registry):
    result = {"proposals": [_proposal()], "patterns": [], "residue": [], "evidence_gaps": [],
              "environment": []}
    wire = to_wire(result=result, runs=[], registry=registry,
                   created_at="2026-08-25T04:00:00Z")
    assert set(wire) == {"suggestions", "suggestionsTotal", "components", "patterns", "scorecard",
                         "waste"}
    assert set(wire["patterns"]) == {"admitted", "residue", "evidenceGaps", "environment",
                                    "unattributed"}
    json.dumps(wire)  # the payload is written with json.dump; this is that contract


def test_the_suggestion_list_always_ships_with_the_count_it_is_a_projection_of(registry):
    """Untrimmed, the total EQUALS the list — and it is still the reading a consumer needs.

    The key must never be absent, or "nothing was dropped" and "this producer does not say" collapse
    into one state and the trim downstream becomes invisible again.
    """
    result = {"proposals": [_proposal()], "patterns": [], "residue": [], "evidence_gaps": [],
              "environment": []}
    wire = to_wire(result=result, runs=[], registry=registry,
                   created_at="2026-08-25T04:00:00Z")
    assert wire["suggestionsTotal"] == len(wire["suggestions"]) == 1


def test_the_publish_block_counts_what_it_published_not_what_the_store_held(registry):
    """The denominator is the PUBLISHABLE set, because that is the list the trim will cut.

    Taken after the projection rather than off `all_suggestions`, so a row the publish rule dropped
    is not counted as one a byte budget removed — two different losses that must not read alike.
    """
    rows = [{"suggestionId": f"s{i}", "status": "dismissed", "componentId": "c",
             "summary": "x", "lastSeenAt": "2026-08-25T00:00:00Z"} for i in range(4)]
    block = published_suggestions_block(rows)
    assert set(block) == {"suggestions", "suggestionsTotal"}
    assert block["suggestionsTotal"] == len(block["suggestions"]) == 4
    assert block["suggestions"] == publishable_suggestions(rows), "one publish rule, not two"


def test_to_wire_carries_the_discovery_and_the_shortfall_off_the_result(registry):
    """Both facts live in `run_pipeline`'s result because neither is derivable from `runs`.

    A fold over the runs in hand cannot see the sessions the cap excluded or the spend the harvest
    could not host — so if `to_wire` stopped reading them, the scorecard would go back to reporting
    a sample as a population with nothing on the wire to say so.
    """
    result = {"proposals": [], "patterns": [], "residue": [], "evidence_gaps": [],
              "environment": [],
              "population_discovery": _REPLAYED_DISCOVERY,
              "usage_shortfall": _REPLAYED_SHORTFALL}
    runs = [_resource_run("a", tokens=10)]
    wire = to_wire(result=result, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z",
                   verdicts=[_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9)])
    assert wire["scorecard"]["population"]["sessions"]["admitted"] == 25
    assert wire["scorecard"]["spend"]["usageShortfall"]["tokensDiscarded"] == 13_300_000
    json.dumps(wire)


def test_to_wire_without_verdicts_still_emits_a_scorecard(registry):
    """Back-compat, and more: a consumer must never have to branch on whether the key exists.
    An empty scorecard says "no scored population", which is true; a missing one says nothing."""
    wire = to_wire(result={"proposals": []}, runs=[_resource_run("a", tokens=5)],
                   registry=registry, created_at="2026-08-25T04:00:00Z")
    assert set(wire["scorecard"]) == SCORECARD_KEYS
    assert wire["scorecard"]["population"]["total"] == 0
    assert wire["scorecard"]["passRate"] is None
    # The run is unscored-by-absence, so its tokens are NOT summed into a population of 0.
    assert wire["scorecard"]["spend"]["tokens"] == {"sum": "UNMEASURED", "measuredOf": 0,
                                                   "total": 0}


def test_to_wire_scorecard_covers_the_population_it_was_given(registry):
    runs = [_resource_run("a", tokens=7), _resource_run("b", tokens=3)]
    verdicts = [_verdict("a", Overall.PASS, dispatch=0.9, delivered=0.9),
                _verdict("b", Overall.FAIL, dispatch=0.9, delivered=0.1)]
    wire = to_wire(result={"proposals": []}, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)
    assert wire["scorecard"]["population"] == {"total": 2, "pass": 1, "fail": 1, "unscored": 0}
    assert wire["scorecard"]["spend"]["tokens"]["sum"] == 10


def test_to_wire_is_byte_reproducible_for_the_same_input(registry):
    """`created_at` is injected rather than read from the clock, so two runs over one input
    produce identical bytes and a diff means something changed."""
    result = {"proposals": [_proposal()], "patterns": [], "residue": [], "evidence_gaps": [],
              "environment": []}
    args = {"result": result, "runs": [], "registry": registry,
            "created_at": "2026-08-25T04:00:00Z"}
    assert json.dumps(to_wire(**args), sort_keys=True) == \
        json.dumps(to_wire(**args), sort_keys=True)


@pytest.mark.skipif(not os.path.exists(default_registry_path()),
                    reason="the sigma registry lives outside the repo")
def test_the_real_registry_buckets_cleanly():
    real = SigmaRegistry.load()
    wire = components_to_wire(real, [])
    counted = (sum(len(s["components"]) for s in wire["yours"])
               + wire["fixFirst"]["count"] + wire["admin"]["count"] + wire["locked"]["count"])
    assert counted == len(real)
    # Every editable component must reach a named section; a catch-all holding most of them
    # meant the sections were keyed on Lab categories rather than Sigma layers.
    other = [s for s in wire["yours"] if s["layer"] == "other"]
    assert not other, f"editable components fell outside the named sections: {other}"


# --- the per-actor bundle -----------------------------------------------------
#
# REQ-017 note, said out loud because it is a real exception: everything below is real harvested
# data EXCEPT which actor owns which session. No committed fixture records an owner —
# `scripts/refresh_improve_fixtures.py` harvests spans and plan-state, while the actor lives in
# AgentCore Memory, which has no fixture at all — so the session→actor mapping is assigned by the
# test. The session ids, the goals, the plans, the trajectories, the mined pattern with its real
# facet evidence prose, and the waste findings are all real.

_ACTOR_A = "6f1c2f6e-1111-4c2a-9a55-aaaaaaaaaaaa"
_ACTOR_B = "6f1c2f6e-2222-4c2a-9a55-bbbbbbbbbbbb"


def _golden_sessions() -> list:
    """The golden session ids, in dataset order. Read from the dataset, never written out — the
    fixture module records what a hand-copied list cost (three of six wrong, silently)."""
    return [s.source_session or s.scenario_id for s in load_golden()]


def _as_json_text(value: str) -> str:
    """A probe string as it would appear INSIDE a JSON document.

    The real goals contain newlines, which `json.dumps` escapes — so probing a serialized bundle
    with the raw Python string silently never matches, and a leak test built that way would pass
    no matter what leaked.
    """
    return json.dumps(value)[1:-1]


def _judge_prose(goal: str) -> str:
    """A judge explanation shaped like the real one: `judge.py` prefixes a multi-run mean and then
    restates the request, which is why this string may never reach the shared item.

    Built from the goal's TAIL rather than its head, and that is not cosmetic: the golden goals
    share a file-attachment preamble, so only 3 of the 6 have a distinct first 120 characters while
    all 6 have a distinct last 120. A probe built from the head cannot ATTRIBUTE a leak to an actor,
    which is the whole job of the test below.
    """
    return (f"[mean of 3 runs] The user asked: …{goal[-120:]} — the agent dispatched correctly but "
            f"no usable result arrived.")


def _owned_population(owners: dict, workload: str = "conversational") -> tuple:
    """``(runs, verdicts)`` over the REAL golden sessions, with an owner assigned to each.

    ``owners`` maps a golden session id to an actor id, or to ``None`` for a session Memory named
    no owner for. Every verdict carries the judge's sentence about that session's REAL goal, so a
    leak of one into the wrong bundle is detectable.
    """
    runs, verdicts = [], []
    for scenario in load_golden():
        session = scenario.source_session or scenario.scenario_id
        if session not in owners:
            continue
        owner = owners[session]
        runs.append(Run(
            run_id=session,
            actor_id=owner if owner is not None else UNKNOWN,
            goal=scenario.goal,
            response=f"Here is the proxy for {scenario.goal[:40]}",
            trajectory=real_trajectory(session), plan=real_plan(session),
            resources={"latency_ms": 1000, "tokens": 500},
            window=RunWindow(start="2026-08-13T18:00:00+00:00",
                             end="2026-08-13T18:40:00+00:00")))
        verdicts.append(_verdict(
            session, Overall.FAIL, workload=workload,
            reason="gating: results_delivered:FAIL",
            results=[_tier("dispatch_correct", 0.9, Outcome.PASS),
                     EvaluatorResult("results_delivered", "llm", Outcome.FAIL, 0.4,
                                     _judge_prose(scenario.goal))]))
    assert runs, "no golden session matched the requested owners; the test would be vacuous"
    return runs, verdicts


def _owned_result(registry, member_run_ids: list) -> dict:
    """A pipeline result whose ONE admitted pattern is owned by `member_run_ids`.

    Built from `_REAL_UNOWNED_PATTERN` with its members re-pointed at golden sessions and its real
    facet-evidence prose copied onto each — the same substitution
    `test_the_global_unattributed_bucket_contains_no_user_derived_text` makes, for the same reason:
    the runs then carry real goals, so a mapper that reached into `runs_by_id` would be caught.
    """
    prose = _REAL_UNOWNED_PATTERN["evidence_by_run"][
        "58dd92d2-67ef-4410-8c21-4440b4e0a65c"]
    pattern = dict(_REAL_UNOWNED_PATTERN, shape=["fanout_parent_ran_early"],
                   member_run_ids=list(member_run_ids),
                   evidence_by_run={rid: dict(prose) for rid in member_run_ids})
    proposals, unattributed = propose_all([pattern], registry)
    assert proposals, "the fixture must be attributable, or there is no suggestion to split"
    return {
        "proposals": [_jsonable(p) for p in proposals],
        "patterns": [_jsonable(pattern)],
        "unattributed": [_jsonable(u) for u in unattributed],
        # `reason` is `mine`'s CURRENT sentence — counts, no ids, which is what makes it publishable
        # (`test_improve_mine.test_no_reason_this_engine_writes_names_a_session` is the guard that
        # keeps it that way at the source). The tenant-foreign id sits in `pass_hit_runs` and in
        # `refinement_log`, which are the fields this mapper must drop, alongside the gateway tool
        # name a refinement log also carries.
        "residue": [{"run_id": rid, "symptom": ["tier_failed:results_delivered"],
                     "best_shape": ["flat_plan"], "pass_hits": 2,
                     "pass_hit_runs": ["a-passing-run-of-someone-elses"],
                     "reason": "REQ-018: no shape conjunction (<= 4 facets) separates this "
                               "failure from 2 passing runs — the runs are structurally alike",
                     "refinement_log": [
                         "seed: 2 passing run(s) to exclude "
                         "(['a-passing-run-of-someone-elses'])",
                         "+gateway_reported_failure -> still-matching 2 => 1  "
                         "[gateway reported FAILED for ['recognize_faces_in_proxy']]"]}
                    for rid in member_run_ids],
        "evidence_gaps": [],
        "environment": [],
        "waste": _real_waste_artifact(),
    }


def test_an_actor_bundle_emits_exactly_the_wire_keys(registry):
    runs, verdicts = _owned_population({s: _ACTOR_A for s in _golden_sessions()})
    result = _owned_result(registry, [runs[0].run_id])
    bundles = actor_bundles(result=result, runs=runs, verdicts=verdicts)

    assert set(bundles) == {_ACTOR_A}
    bundle = bundles[_ACTOR_A]
    assert set(bundle) == ACTOR_BUNDLE_KEYS
    assert set(bundle["runs"][0]) == ACTOR_RUN_KEYS
    assert set(bundle["runs"][0]["spend"]) == ACTOR_RUN_SPEND_KEYS
    assert set(bundle["history"][0]) == ACTOR_DAY_KEYS
    assert set(bundle["history"][0]["byCategory"]) == {"input", "output", "cacheRead", "cacheWrite"}
    assert set(bundle["history"][0]["coverage"]) == ACTOR_DAY_COVERAGE_KEYS
    assert set(bundle["history"][0]["coverage"]["byCategory"]) == {"input", "output", "cacheRead",
                                                                  "cacheWrite"}
    assert set(bundle["waste"]) == ACTOR_WASTE_KEYS
    assert set(bundle["patterns"]) == {"admitted", "residue", "evidenceGaps", "environment",
                                       "unattributed"}
    assert set(next(iter(bundle["evidence"].values()))) == PATTERN_EVIDENCE_KEYS
    assert set(bundle["patterns"]["admitted"][0]) == PATTERN_EVIDENCE_KEYS
    assert set(bundle["patterns"]["residue"][0]) == RESIDUE_EVIDENCE_KEYS
    json.dumps(bundle)  # the item is written with json.dump; this is that contract


def _fanout_population(group: str = "fanout_1", *, parent_in_population: bool = True) -> tuple:
    """``(runs, verdicts, parent_id, child_ids)`` for ONE REAL request that fanned out (REQ-017).

    Real ids, real windows and a real parent link: `improve_fixtures.fanout_harvest` runs the harvester
    over committed Insights rows and hands back the reader, so ``parent_run_id`` is DERIVED from the
    OTEL trace the two sessions share rather than asserted here. That matters because the whole
    counting rule rests on it — a hand-set parent id would let this pass over a link that never worked.

    ``parent_in_population=False`` drops the parent's own run, which is what ``IMPROVE_LIMIT`` does
    WHEN IT BINDS: `observe.population.discover_sessions` orders newest-first and a child's first span
    is later than its parent's, so a binding cap keeps children and drops parents. Replayed on one
    real 24h harvest of 36 discoverable sessions: a cap of 25 admitted 11 sub-sessions, 1 with its
    parent outside; the deployed 60 admits all 36 and leaves none. Constructed here rather than
    harvested because the state is currently unreachable on this account's traffic and the branch
    still has to hold for the day it is not.
    """
    parent, children, span_source = fanout_harvest(group)
    harvest = span_source._load()

    def window_of(session: str) -> RunWindow:
        stamped = harvest[session]["window"]
        return RunWindow(start=stamped["start"], end=stamped["end"],
                         provenance=stamped["provenance"])

    runs, verdicts = [], []
    if parent_in_population:
        runs.append(Run(run_id=parent, actor_id=_ACTOR_A, window=window_of(parent),
                        resources={"latency_ms": 4000, "tokens": 1000, "llm_calls": 4,
                                   "cost": 0.4, "output_tokens": 100, "input_tokens": 4,
                                   "cache_read_tokens": 800, "cache_write_tokens": 96,
                                   "models": ["global.anthropic.claude-opus-4-8"]}))
        verdicts.append(_verdict(parent, Overall.PASS, dispatch=0.9, delivered=0.9))
    for child in children:
        # The owner is INHERITED from the parent's conversation — a sub-session has none of its own,
        # which is what `build_run` resolves through `MemoryReader.owner_of_session`.
        runs.append(Run(run_id=child, actor_id=_ACTOR_A, window=window_of(child),
                        parent_run_id=span_source.parent_for(child),
                        resources={"latency_ms": 100, "tokens": 10, "llm_calls": 1,
                                   "cost": 0.01, "output_tokens": 2, "input_tokens": 1,
                                   "cache_read_tokens": 5, "cache_write_tokens": 2,
                                   "models": ["global.anthropic.claude-opus-4-8"]}))
        verdicts.append(_verdict(child, Overall.PASS, workload="plan_node",
                                 gating=("DagCompletionValidator",),
                                 results=[_tier("DagCompletionValidator", 1.0, Outcome.PASS)]))
    return runs, verdicts, parent, children


def test_a_sub_sessions_spend_joins_its_parents_row_and_is_never_a_run_of_its_own():
    """THE counting rule, on a real request that fanned out into six plan steps.

    Both alternatives are wrong and differently so. Counting each sub-session as a run inflates volume
    — measured 2.1x over 7 days, 91 sub-sessions against 44 parents, up to 6x for one request — and a
    sub-session has no owner to count it under anyway (0 of 357 non-conversational sessions resolved to
    an actor), so it could only borrow its parent's, at which point the parent is already the unit.
    Dropping them instead under-reports real spend: sub-sessions are 3.0% of the tokens in a measured
    24h harvest. So one row per request, its terms the sum over itself and its children.
    """
    runs, verdicts, parent, children = _fanout_population()
    assert len(children) == 6, "the fixture must be a real fan-out or this proves nothing"

    bundle = actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts)[_ACTOR_A]

    assert [row["runId"] for row in bundle["runs"]] == [parent], "one row, and it is the request"
    assert bundle["runsTotal"] == 1, "six plan steps are part of one request, not seven runs"
    spend = bundle["runs"][0]["spend"]
    assert spend["tokens"] == 1000 + 6 * 10, "the children's tokens are IN the request's total"
    assert spend["llmCalls"] == 4 + 6
    assert spend["cost"] == pytest.approx(0.4 + 6 * 0.01)
    assert spend["byCategory"]["cacheRead"] == 800 + 6 * 5
    # UNIONED, not concatenated: six steps on one model are one model and not six.
    assert spend["models"] == ["global.anthropic.claude-opus-4-8"]

    # ...and the day row agrees, on the request's own real date, counting requests and not sessions.
    assert [(r["date"], r["runs"], r["tokens"]) for r in bundle["history"]] == [
        ("2026-08-20", 1, 1060)]


def test_a_sub_session_whose_request_fell_outside_the_population_still_reaches_the_day_row():
    """The boundary `IMPROVE_LIMIT` creates when it binds, and the honest side to err on.

    The harvest WINDOW is not the boundary — the parent's linking span sits in the same trace as the
    child, so a parent cannot fall outside a window its child is inside (measured: every sub-session
    in a real 24h harvest had its parent present and discoverable in it). The CAP is: the population is
    ordered newest-first, so a binding cap keeps children and drops the parents they belong to.

    The deployed cap clears a whole day, so nothing is dropped today and this state does not arise. It
    is asserted anyway: nothing bounds how busy a day gets, so the bound can start binding again
    without any code changing, and a day-row total that quietly stopped including a plan's spend is
    exactly the silent subtraction this branch exists to prevent.

    Such a child has no request to put a row on, but it still knows its owner, and its tokens were
    really spent. So its spend joins the actor's day row — the cumulative figure stays complete — and
    contributes nothing to ``runs``, because no request of its own happened.
    """
    runs, verdicts, parent, children = _fanout_population(parent_in_population=False)

    bundle = actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts)[_ACTOR_A]

    assert bundle["runs"] == [] and bundle["runsTotal"] == 0, "no request, so no row and no count"
    assert [(r["date"], r["runs"], r["tokens"]) for r in bundle["history"]] == [
        ("2026-08-20", 0, 6 * 10)], "the spend is still the reader's, and still on its real day"


def test_the_producer_reports_the_fanout_it_folded_because_the_wire_does_not():
    """The counting rule has to be auditable somewhere, and the row shape is fixed.

    ``runsTotal`` deliberately does not move when a request fans out, so from the wire alone a reader
    cannot tell a six-step request from a one-step one. The counts therefore go to the run summary,
    where an operator verifying a deploy can see them — including the one that says the cap is cutting
    requests away from their own steps.
    """
    runs, verdicts, _, children = _fanout_population()
    stats: dict = {}
    actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts, stats=stats)
    assert stats == {"fanned_out_requests": 1, "sub_sessions_folded": len(children),
                     "sub_sessions_without_a_request": 0, "runs_with_no_window": 0}

    orphaned, orphan_verdicts, _, _ = _fanout_population(parent_in_population=False)
    stats = {}
    actor_bundles(result={"proposals": []}, runs=orphaned, verdicts=orphan_verdicts, stats=stats)
    assert stats["sub_sessions_without_a_request"] == len(children)
    assert stats["fanned_out_requests"] == 0, "nothing was folded, because no request was here"


def test_a_day_rows_sums_carry_their_own_denominator_per_term():
    """What makes a lenient sum readable, and it has to be PER TERM.

    The sums on a day row are `dto.summed` — what was measured, with no denominator of their own — which
    is the rule the population's `spend` uses and which `_sum_term`'s docstring is explicit about: it can
    understate the truth by orders of magnitude while looking authoritative. On the harvest behind these
    figures tokens are recorded on 354 of 553 sessions.

    Per term rather than one count for the block, because coverage differs BETWEEN terms: ``latencyMs``
    is summed from spans and every token term comes from the usage record, so a session can be measured
    for one and not the other — which is exactly what this fixture shows.
    """
    parent, children, span_source = fanout_harvest("fanout_1")
    harvest = span_source._load()
    window = RunWindow(start=harvest[parent]["window"]["start"],
                       end=harvest[parent]["window"]["end"],
                       provenance=harvest[parent]["window"]["provenance"])
    # Two requests on one real day: one with a usage record, one with only span latency.
    measured = Run(run_id=parent, actor_id=_ACTOR_A, window=window,
                   resources={"latency_ms": 4000, "tokens": 1000, "llm_calls": 4, "cost": 0.4,
                              "input_tokens": 4, "output_tokens": 100, "cache_read_tokens": 800,
                              "cache_write_tokens": 96, "cost_tokens_priced": 1000,
                              "cost_tokens_unpriced": 0,
                              "models": ["global.anthropic.claude-opus-4-8"]})
    unmeasured = Run(run_id=children[0], actor_id=_ACTOR_A, window=window,
                     resources={"latency_ms": 700})
    verdicts = [_verdict(measured.run_id, Overall.PASS, dispatch=0.9, delivered=0.9),
                _verdict(unmeasured.run_id, Overall.PASS, dispatch=0.9, delivered=0.9)]

    row = actor_bundles(result={"proposals": []}, runs=[measured, unmeasured],
                        verdicts=verdicts)[_ACTOR_A]["history"][0]

    assert row["runs"] == 2 and row["coverage"]["total"] == 2
    assert row["latencyMs"] == 4700 and row["coverage"]["latencyMs"] == 2, "spans measured both"
    assert row["tokens"] == 1000 and row["coverage"]["tokens"] == 1, "...usage measured one"
    assert row["coverage"]["cost"] == 1 and row["coverage"]["costTokensPriced"] == 1
    assert row["coverage"]["byCategory"] == {"input": 1, "output": 1, "cacheRead": 1,
                                            "cacheWrite": 1}


def test_a_day_that_measured_nothing_reports_zero_of_its_real_session_count():
    """Both-or-neither, and never ``0 of 0``.

    A day whose sessions recorded no usage contributes ``0`` to each term's count and its REAL session
    count to ``total``. That is a measured zero and reads as "none of these two sessions recorded it".
    ``0 of 0`` would read as complete over nothing, which is the one thing a coverage pair must not say.
    """
    runs, verdicts = _owned_population({s: _ACTOR_A for s in _golden_sessions()[:2]})
    for run in runs:
        run.resources = {"latency_ms": 10}     # spans only: no usage record at all

    row = actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts)[_ACTOR_A]["history"][0]

    assert row["tokens"] == "UNMEASURED", "nothing measured it, so not a 0 that reads as free"
    assert row["coverage"]["total"] == 2, "...but the sessions were real and are counted"
    assert row["coverage"]["tokens"] == 0
    assert row["coverage"]["byCategory"] == {"input": 0, "output": 0, "cacheRead": 0,
                                            "cacheWrite": 0}
    assert "UNMEASURED" not in row["coverage"].values(), "a denominator may never be a sentinel"


def test_a_day_rows_outcome_counts_partition_its_requests():
    """The row has to answer "did it work", not only "what did it cost".

    Without these three a reader could see their lifetime tokens and cost and nothing about whether any
    of it succeeded, while the population row beside it carried a rate. The counts are the scorecard's
    own three in its own spelling, from the same `contract.verdict_counts`, so one vocabulary serves
    both scopes.

    ``unscored`` is a THIRD count and folds into neither of the others, which is the line `evaluate.gate`
    holds: a gating evaluator that could not measure says something about the evidence and nothing about
    the agent, so could-not-measure is not could-not-pass.

    COUNTS AND NOT A RATE. Counts fold across days by addition, exactly as the spend terms do; a rate
    would need its weight stored beside it and could not be re-derived from the row if that weight were
    ever wrong. The division is `pass / (pass + fail)` at read time, over whatever span was asked for.
    """
    runs, _ = _owned_population({s: _ACTOR_A for s in _golden_sessions()[:3]})
    # One verdict each, one of each outcome. `_owned_population` stamps one window on every golden run,
    # so these three requests land on one day and this is that day's row.
    verdicts = [_verdict(runs[0].run_id, Overall.PASS, dispatch=0.9, delivered=0.9),
                _verdict(runs[1].run_id, Overall.FAIL, dispatch=0.9, delivered=0.4),
                # What a NO_DATA gating tier produces: nothing scorable, and not a failure.
                _verdict(runs[2].run_id, Overall.UNSCORED, dispatch=0.9, delivered=None)]

    rows = actor_bundles(result={"proposals": []}, runs=runs,
                         verdicts=verdicts)[_ACTOR_A]["history"]

    assert len(rows) == 1, "one window, so one row — or these are not one day's counts"
    row = rows[0]
    assert (row["pass"], row["fail"], row["unscored"]) == (1, 1, 1)
    assert row["pass"] + row["fail"] + row["unscored"] == row["runs"] == 3
    assert "passRate" not in row, "the rate is a read-time division, never a stored figure"


def test_a_fanned_out_requests_outcome_is_its_parents_and_is_counted_once():
    """Who owns the OUTCOME when one request becomes seven sessions, on a REAL fan-out (REQ-017).

    The rule the spend side already follows, read the other way round: the unit is the request, so a
    plan sub-session contributes its tokens to the day and no outcome of its own. This fixture is the
    sharpest available demonstration, because all six real children carry a PASS of their own from
    `DagCompletionValidator` — counting them would report ``pass: 7`` against ``runs: 1``.

    Measured on the live population it fails the other way too: children are overwhelmingly UNSCORED
    (74 of 88), so counting them would report a day whose every request was scored as mostly
    unmeasurable.
    """
    runs, verdicts, parent, children = _fanout_population()
    assert len(children) == 6 and all(v.overall is Overall.PASS for v in verdicts), \
        "every child must carry a verdict of its own, or nothing is being suppressed here"

    row = actor_bundles(result={"proposals": []}, runs=runs,
                        verdicts=verdicts)[_ACTOR_A]["history"][0]

    assert (row["runs"], row["pass"], row["fail"], row["unscored"]) == (1, 1, 0, 0)
    assert row["coverage"]["total"] == 7, "...while all seven sessions still counted for spend"

    # A child whose request fell outside the population has no outcome to contribute either, so the day
    # reports real spend against no requests rather than borrowing its children's verdicts.
    orphaned, orphan_verdicts, _, _ = _fanout_population(parent_in_population=False)
    orphan_row = actor_bundles(result={"proposals": []}, runs=orphaned,
                               verdicts=orphan_verdicts)[_ACTOR_A]["history"][0]

    assert (orphan_row["runs"], orphan_row["pass"], orphan_row["fail"],
            orphan_row["unscored"]) == (0, 0, 0, 0), "a measured zero: no request of their own here"
    assert orphan_row["tokens"] == 6 * 10, "the spend is still real and still theirs"


def test_a_run_with_no_window_is_counted_and_never_given_a_date():
    """A run that cannot be placed on a time axis cannot be placed in a day row either.

    Guessing one — today's date, the harvest's — would silently move real spend onto a day it did not
    happen on, which for a cumulative figure is worse than leaving it out. So it is left out and the
    count is reported, and the run's evidence row is unaffected: only the day rows need a date.
    """
    runs, verdicts = _owned_population({_golden_sessions()[0]: _ACTOR_A})
    runs[0].window = UNKNOWN
    stats: dict = {}
    bundle = actor_bundles(result={"proposals": []}, runs=runs, verdicts=verdicts,
                           stats=stats)[_ACTOR_A]

    assert bundle["history"] == [], "no date, so no day row"
    assert stats["runs_with_no_window"] == 1
    assert bundle["runsTotal"] == 1 and len(bundle["runs"]) == 1, "the evidence row is untouched"


def test_an_actor_bundle_carries_no_other_actors_evidence(registry):
    """The isolation guard on the SECOND item, and the one that matters most in this file.

    A per-actor bundle is the first place on this surface that model prose and session ids travel,
    so the only thing standing between one tenant's prompts and another's screen is which
    partition a value was written to. Two actors, both with real golden runs, both with the judge's
    sentence about their own real goal: each bundle must contain its own and none of the other's.
    """
    sessions = _golden_sessions()
    assert len(sessions) >= 4, "this test needs at least two sessions per actor"
    mine, theirs = sessions[:2], sessions[2:4]
    owners = {**{s: _ACTOR_A for s in mine}, **{s: _ACTOR_B for s in theirs}}
    runs, verdicts = _owned_population(owners)
    goal_of = {r.run_id: r.goal for r in runs}

    result = _owned_result(registry, mine + theirs)
    bundles = actor_bundles(result=result, runs=runs, verdicts=verdicts)
    assert set(bundles) == {_ACTOR_A, _ACTOR_B}

    def long_words(session_ids: list) -> set:
        return {w for s in session_ids for w in goal_of[s].split() if len(w) >= 12}

    for actor, own, other in ((_ACTOR_A, mine, theirs), (_ACTOR_B, theirs, mine)):
        dumped = json.dumps(bundles[actor])
        # Words shared by both actors' goals are excluded, because they cannot ATTRIBUTE a leak:
        # the golden goals name the same public test media, so `Netflix_Open_Content_Meridian.mp4`
        # appears in several and finding it would prove nothing about whose it was.
        probes = set(other) | {_judge_prose(goal_of[s]) for s in other}
        probes |= long_words(other) - long_words(own)
        probes.discard("")
        leaked = sorted(p for p in probes if _as_json_text(p) in dumped)
        assert not leaked, f"another actor's evidence reached {actor[:8]}'s bundle: {leaked}"
        # ...and not vacuous: their OWN ids and their OWN judge sentence are there.
        for session in own:
            assert session in dumped
            assert _as_json_text(_judge_prose(goal_of[session])) in dumped
        # No actor id at all, not even their own: the partition key already says whose it is, and
        # repeating it inside invites a reader to trust the body over the key.
        assert _ACTOR_A not in dumped and _ACTOR_B not in dumped


def test_the_passing_runs_that_disqualified_a_residue_entry_reach_nobody(registry):
    """A control belongs to whichever tenant ran it, so `passHitRuns` is safe neither globally nor
    on the failing run's owner's item — and neither is `refinementLog`, which names the same control
    set plus the tools a gateway reported failing (a tool name can come from a user-registered MCP
    server, the same class of value as `waste.detail`).

    ``reason`` DOES travel, to both items, because it states the counts and not the ids.
    `test_improve_mine.test_no_reason_this_engine_writes_names_a_session` is what keeps that true at
    the source; here the claim under test is only that this mapper carries it and drops the rest.
    """
    sessions = _golden_sessions()[:1]
    runs, verdicts = _owned_population({sessions[0]: _ACTOR_A})
    result = _owned_result(registry, sessions)
    bundle = actor_bundles(result=result, runs=runs, verdicts=verdicts)[_ACTOR_A]
    row = bundle["patterns"]["residue"][0]
    assert row["runId"] == sessions[0] and row["passHits"] == 2

    wire = to_wire(result=result, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)
    for served, where in ((json.dumps(bundle), "the actor's bundle"),
                          (json.dumps(wire), "the global bundle")):
        for probe in ("a-passing-run-of-someone-elses", "recognize_faces_in_proxy",
                      "passing run(s) to exclude"):
            assert probe not in served, f"{probe!r} reached {where}"
        # ...and the explanation is not collateral damage: it survives on both.
        assert "no shape conjunction" in served
    # The operator artifact keeps the ids too; it has no tenant boundary.
    assert "a-passing-run-of-someone-elses" in json.dumps(result["residue"])


def test_only_conversational_runs_reach_an_actor_bundle(registry):
    """The other three classes have no user conversation by construction, so no per-actor evidence
    can exist for them. Filtered on the CLASS explicitly, not left to fall out of "Memory named no
    owner" — a plan sub-session that did resolve to an actor would otherwise start publishing a
    shared plan's evidence into one sibling's bundle."""
    sessions = _golden_sessions()[:2]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions}, workload="plan_node")
    result = _owned_result(registry, sessions)
    assert actor_bundles(result=result, runs=runs, verdicts=verdicts) == {}
    # ...while the same runs DO count in the global population.
    card = scorecard_to_wire(runs, verdicts)
    assert card["population"]["total"] == len(sessions)
    assert card["attribution"] == {"actors": 0, "attributed": 0, "unattributable": 0,
                                  "ownable": 0, "runs": len(sessions)}


def test_a_conversational_run_with_no_owner_is_counted_and_reaches_no_bundle(registry):
    """The residual case, and it must not vanish silently: a conversational session Memory names no
    actor for is scored, counted and judged, and no actor's surface will ever show it. Stated as a
    number, because otherwise it is indistinguishable from a bundle that failed to write."""
    sessions = _golden_sessions()[:3]
    owners = {sessions[0]: _ACTOR_A, sessions[1]: None, sessions[2]: None}
    runs, verdicts = _owned_population(owners)
    result = _owned_result(registry, sessions)

    bundles = actor_bundles(result=result, runs=runs, verdicts=verdicts)
    assert set(bundles) == {_ACTOR_A}
    assert [row["runId"] for row in bundles[_ACTOR_A]["runs"]] == [sessions[0]]
    for orphan in sessions[1:]:
        assert orphan not in json.dumps(bundles)

    card = scorecard_to_wire(runs, verdicts)
    assert set(card["attribution"]) == ATTRIBUTION_KEYS
    assert card["attribution"] == {"actors": 1, "attributed": 1, "unattributable": 2,
                                  "ownable": 3, "runs": 3}
    # The orphans are population, so they are still in the pass/fail split above.
    assert card["population"]["total"] == 3


def test_the_attribution_block_counts_actors_and_never_names_them(registry):
    """A count is what makes the platform-wide reading of this scorecard checkable — it aggregates
    every tenant, so it cannot be captioned as one agent's score. A LIST would tell every tenant
    who else is on the harness."""
    sessions = _golden_sessions()[:4]
    owners = {sessions[0]: _ACTOR_A, sessions[1]: _ACTOR_A, sessions[2]: _ACTOR_B,
              sessions[3]: _ACTOR_B}
    runs, verdicts = _owned_population(owners)
    card = scorecard_to_wire(runs, verdicts)
    assert card["attribution"]["actors"] == 2
    dumped = json.dumps(card)
    assert _ACTOR_A not in dumped and _ACTOR_B not in dumped


def test_a_suggestion_whose_evidence_is_another_actors_has_no_entry_here(registry):
    """The honest degradation, and it reuses the state that already exists rather than inventing an
    empty one: the actor simply has no `evidence` entry for that suggestion, and the shared
    suggestion's own `evidenceQuality` is what says how much evidence there is.

    `evidence_quality` already words the no-member case — pinned here beside the case it degrades
    into, so the two cannot drift apart.
    """
    sessions = _golden_sessions()[:2]
    owners = {sessions[0]: _ACTOR_A, sessions[1]: _ACTOR_B}
    runs, verdicts = _owned_population(owners)
    # The pattern rests on B's run alone.
    result = _owned_result(registry, [sessions[1]])
    bundles = actor_bundles(result=result, runs=runs, verdicts=verdicts)

    suggestion_id = result["proposals"][0]["proposal_id"]
    assert suggestion_id in bundles[_ACTOR_B]["evidence"]
    assert bundles[_ACTOR_A]["evidence"] == {}
    assert "no run in the current dataset attributes here" in json.dumps(
        evidence_quality([], {})["caveats"])


def test_an_actor_run_row_carries_the_judges_sentence_and_its_own_denominators(registry):
    """What the per-actor item exists for. `Verdict.reason` names WHICH gating check failed and the
    shared `scorecard.outcomes` counts that by evaluator name; neither can say why the judge
    thought so, because that sentence restates the request. Here the reader owns the session."""
    session = _golden_sessions()[0]
    runs, verdicts = _owned_population({session: _ACTOR_A})
    bundle = actor_bundles(result=_owned_result(registry, [session]), runs=runs,
                           verdicts=verdicts)[_ACTOR_A]
    row = bundle["runs"][0]
    assert row["verdict"] == {"overall": "FAIL", "reason": "gating: results_delivered:FAIL"}
    assert row["tiers"]["delivery"]["explanation"] == _judge_prose(runs[0].goal)
    assert row["tiers"]["delivery"]["score"] == pytest.approx(0.4)
    assert row["tiers"]["dispatch"]["outcome"] == "PASS"
    assert row["workloadClass"] == "conversational"
    assert row["spend"]["tokens"] == 500
    # Unmeasured stays the SENTINEL NAME, never 0 — a `cost: 0` on a run row reads as "free".
    assert row["spend"]["cost"] == "UNMEASURED"
    assert bundle["runsTotal"] == 1


def test_a_tier_the_verdict_never_scored_is_not_applicable_never_a_pass(registry):
    session = _golden_sessions()[0]
    runs, verdicts = _owned_population({session: _ACTOR_A})
    verdicts[0].evaluator_results = [_tier("dispatch_correct", 0.9, Outcome.PASS)]
    bundle = actor_bundles(result=_owned_result(registry, [session]), runs=runs,
                           verdicts=verdicts)[_ACTOR_A]
    assert bundle["runs"][0]["tiers"]["delivery"] == {
        "score": "UNMEASURED", "outcome": "NOT_APPLICABLE", "explanation": ""}


def test_an_actors_waste_findings_carry_the_sequence_the_shared_block_may_not(registry):
    """The same findings, the same aggregation, one extra key. `detail` names tool names — which can
    include a user-registered MCP server's — so it may not ride the shared item; the argument is
    about who reads it, and here the reader owns the session."""
    waste = _real_waste_artifact()
    owned = [f["run_id"] for f in waste["findings"]][:1]
    assert owned, "the golden spans must still produce findings, or this is vacuous"
    runs, verdicts = _owned_population({owned[0]: _ACTOR_A})
    bundle = actor_bundles(result=_owned_result(registry, owned), runs=runs,
                           verdicts=verdicts)[_ACTOR_A]

    findings = bundle["waste"]["findings"]
    assert findings and all(f["runId"] == owned[0] for f in findings)
    assert any(f["detail"] for f in findings)
    assert all(f["confidence"] for f in findings)
    # The denominator is THIS actor's population, not the platform's.
    assert bundle["waste"]["runs"] == 1
    assert bundle["waste"]["runsWithWaste"] == 1
    # And the shared block over the same artifact still carries no detail at all.
    assert "findings" not in waste_to_wire(waste)


def test_no_actor_bundle_is_produced_when_nothing_is_attributable(registry):
    """The producer side of the absent case. An empty bundle would replace real evidence with a
    measured-looking blank on the next cron run, so the producer writes NOTHING for an actor with
    no owned run — which is the normal state over a bounded harvest window."""
    sessions = _golden_sessions()[:2]
    runs, verdicts = _owned_population({s: None for s in sessions})
    assert actor_bundles(result=_owned_result(registry, sessions), runs=runs,
                         verdicts=verdicts) == {}
    assert actor_bundles(result={"proposals": []}, runs=[], verdicts=[]) == {}


def test_the_global_bundle_carries_no_session_id_and_no_facet_evidence(registry):
    """The content rule on the WHOLE global bundle, not one block of it.

    Session ids used to ride on four keys — a suggestion's and an admitted pattern's
    `memberRunIds`, an unattributed pattern's, and every residue row's `runId` + `passHitRuns` —
    and the facet evidence prose rode on `rootCauseHypo`, which interpolates plan node ids and
    timestamps out of one tenant's session. All of it is per-actor now. Probed over the real
    golden sessions and the real facet evidence, so the probes are strings the producer genuinely
    handles rather than invented ones.
    """
    sessions = _golden_sessions()[:3]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    result = _owned_result(registry, sessions)
    wire = to_wire(result=result, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)
    dumped = json.dumps(wire)

    probes = set(sessions) | {"a-passing-run-of-someone-elses",
                              # the real facet evidence prose, and the judge's sentence
                              "have no later retrieval", "returned inline", "Here is the proxy"}
    for run in runs:
        probes.add(_judge_prose(run.goal))
        probes.update(w for w in run.goal.split() if len(w) >= 12)
    leaked = sorted(p for p in probes if _as_json_text(p) in dumped)
    assert not leaked, f"per-actor evidence reached the global bundle: {leaked}"

    # ...and not vacuous: the CLAIM is still whole. Shape, plain prose, counts, candidates.
    suggestion = wire["suggestions"][0]
    assert suggestion["pattern"]["runCount"] == len(sessions)
    assert suggestion["pattern"]["shapeFacets"] == ["fanout_parent_ran_early"]
    # Plural because this fixture carries three sessions; the noun is spelled out rather than
    # derived so this stays an exact prefix of the sentence a reader gets. `run(s)` until the
    # producer started agreeing it with the count.
    assert suggestion["evidenceSummary"].startswith(f"from {len(sessions)} runs where ")
    assert suggestion["candidates"] and suggestion["affordances"]
    # `evidenceQuality` is over EVERY member, so a multi-run pattern no longer reports one run.
    assert suggestion["evidenceQuality"]["runCount"] == len(sessions)
    assert len(wire["patterns"]["residue"]) == len(sessions)


def test_a_suggestions_run_count_is_every_member_not_just_the_first(registry):
    """`propose` builds a `FailureSignature` whose `run_id` is the FIRST member only, so a
    suggestion resting on several runs used to report one — beside an `evidenceSummary` reading
    "from 3 run(s)" and an `evidenceQuality` caveat saying "one run, a pattern this thin cannot
    show a rate". The join on (shape, symptom) is what recovers the rest."""
    sessions = _golden_sessions()[:3]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    result = _owned_result(registry, sessions)
    wire = to_wire(result=result, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)

    assert wire["suggestions"][0]["pattern"]["runCount"] == 3
    assert wire["suggestions"][0]["blastRadius"]["instanceCount"] == 3
    caveats = wire["suggestions"][0]["evidenceQuality"]["caveats"]
    assert not any("one run" in c for c in caveats)
    # Without the join it degrades to the signature's own single run rather than to none.
    lone = suggestion_to_wire(result["proposals"][0], created_at="2026-08-25T04:00:00Z",
                              registry=registry, runs_by_id={}, judge_config_hash="h",
                              judge_runs=1, evidence_sources=[])
    assert lone["pattern"]["runCount"] == 1


def test_a_suggestion_leads_with_a_short_headline_beside_its_unchanged_summary(registry):
    """The lead line a card can open with, driven through the whole producer rather than hand-built.

    The line it leads is the defect this field answers: on the live bundle `evidenceSummary` is a
    single thirty-word clause and is the ONLY text distinguishing one card from the next, because
    the line under it names the component and its permission and reads the same on every locked
    card. So the headline is asserted TOGETHER with the sentence — the sentence keeps its text, its
    run count and its meaning, and the short line is additive.
    """
    sessions = _golden_sessions()[:3]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    wire = to_wire(result=_owned_result(registry, sessions), runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)

    suggestion = wire["suggestions"][0]
    assert suggestion["evidenceHeadline"] == "A relied-on step was recorded finished early"
    assert suggestion["evidenceSummary"] == (
        "from 3 runs where a step that several later steps rely on was recorded finished before "
        "the step it depends on, so those later steps used unfinished work")
    # The headline is the shorter of the two by a long way, and carries no count: the figure belongs
    # to the sentence, which has a denominator to put it in.
    assert len(suggestion["evidenceHeadline"].split()) < len(
        suggestion["evidenceSummary"].split())
    assert "3" not in suggestion["evidenceHeadline"]


def test_a_suggestion_whose_leading_facet_has_no_headline_omits_the_field(registry):
    """Omitted, never an empty string and never the facet name — `plainSummary`'s rule.

    A facet the headline table does not know is exactly what a later `shape_facets` addition looks
    like from here, and the sentence below still says everything (`prose_for` keeps its fallback).
    """
    proposal = _proposal(
        signature={"run_id": "run-1", "shape": ["some_new_facet"], "pass_hits": 0, "evidence": {}},
        evidence_headline="",
        evidence_summary="from 1 run where some new facet")
    wire = _wire(registry, proposal)

    assert "evidenceHeadline" not in wire
    assert wire["evidenceSummary"] == "from 1 run where some new facet"
    # The publish funnel does not invent one either: it derives from the same table.
    assert "evidenceHeadline" not in publishable_suggestion(wire)


def test_component_health_survives_the_real_wire_shape(registry):
    """The regression that hid behind a loose helper: `component_health` used to read the member ids
    off `suggestion["pattern"]["memberRunIds"]`, and those ids left the wire. Every row's `health`
    came back `None` — a silent downgrade of a measurement to "unknown", which is the exact failure
    `_fit_bundle`'s docstring warns a trim must not cause.

    Driven through `to_wire` rather than through a hand-built suggestion, so the shape under test is
    the shape the producer actually emits.
    """
    sessions = _golden_sessions()[:2]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    wire = to_wire(result=_owned_result(registry, sessions), runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)

    rows = {r["componentId"]: r for section in wire["components"]["yours"]
            for r in section["components"]}
    rows.update({r["componentId"]: r for r in wire["components"]["fixFirst"]["components"]})
    target = wire["suggestions"][0]["targetId"]
    assert rows[target]["health"] == {"runs": 2, "delivered": 0, "deliveredOf": 2}
    # ...and the ids that produced that measurement are still nowhere on the wire.
    assert not [s for s in sessions if s in json.dumps(wire)]


def test_a_trim_reports_health_only_for_the_suggestions_that_survive(registry):
    """`suggestion_members` is keyed on the SUGGESTION id, not the component, precisely so a trim can
    drop rows and have `health` follow the survivors rather than describe what was trimmed away."""
    sessions = _golden_sessions()[:2]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    result = _owned_result(registry, sessions)
    members = suggestion_members(result)
    wire = to_wire(result=result, runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)

    kept = components_to_wire(registry, wire["suggestions"],
                              runs_by_id={r.run_id: r for r in runs},
                              verdicts_by_id={v.run_id: v for v in verdicts},
                              members_by_suggestion=members)
    dropped = components_to_wire(registry, [],
                                 runs_by_id={r.run_id: r for r in runs},
                                 verdicts_by_id={v.run_id: v for v in verdicts},
                                 members_by_suggestion=members)
    target = wire["suggestions"][0]["targetId"]

    def health_of(components, component_id):
        rows = {r["componentId"]: r for section in components["yours"]
                for r in section["components"]}
        rows.update({r["componentId"]: r for r in components["fixFirst"]["components"]})
        return rows[component_id]["health"]

    assert health_of(kept, target) == {"runs": 2, "delivered": 0, "deliveredOf": 2}
    # Its suggestion is gone, so nothing attributes there any more: UNKNOWN, not `{"runs": 0}`.
    assert health_of(dropped, target) is None


def test_the_pattern_lookup_is_group_signatures_own_key_and_not_a_second_grouping():
    """`_patterns_by_shape` indexes the OUTPUT of `mine.group_signatures`, so it cannot be a rival
    grouping rule — there is exactly one, at `group_signatures`, and this inverts it to recover a
    proposal's members. Asserted by building both sides from a real mined population: every key the
    index holds is a key the grouper produced, and every proposal finds its pattern.

    If `group_signatures` ever changes its key, this fails here rather than degrading quietly into
    "every suggestion rests on one run".
    """
    from improve.mine import group_signatures, mine

    sessions = _golden_sessions()
    runs = [Run(run_id=s, plan=real_plan(s), trajectory=real_trajectory(s)) for s in sessions]
    for run in runs:
        run.scenario = next(sc for sc in load_golden()
                            if (sc.source_session or sc.scenario_id) == run.run_id)
    # The PRODUCTION verdict path, so the mined population here is the one the pipeline mines.
    verdicts = [verdict_from_recorded(run, DEFAULT_PASS_THRESHOLD, DEFAULT_GATING)
                for run in runs]
    mined = mine(runs, verdicts)
    patterns = group_signatures(mined.signatures)
    assert patterns, "the golden set must still mine a pattern, or this proves nothing"

    result = {"patterns": [_jsonable(p) for p in patterns]}
    index = _patterns_by_shape(result)
    grouper_keys = {(p["shape"], p["symptom"]) for p in patterns}
    assert set(index) == grouper_keys

    registry = SigmaRegistry.load(missing_ok=True)
    proposals, _ = propose_all(patterns, registry)
    for proposal in proposals:
        signature = _jsonable(proposal)["signature"]
        members, evidence_by_run = _members_and_evidence(signature, index)
        pattern = next(p for p in patterns
                       if p["shape"] == tuple(signature["shape"])
                       and p["symptom"] == tuple(signature["symptom"]))
        assert members == list(pattern["member_run_ids"]), (
            "a proposal did not resolve to its own pattern's members")
        assert evidence_by_run == pattern["evidence_by_run"]


# --- the publish funnel -------------------------------------------------------
#
# The cross-run store is a REPLAY CHANNEL that bypasses this mapper, and that is what these tests
# exist for. `store.upsert_all` persists the whole wire suggestion as it stood when written, and a
# row absent from a later batch is marked `reproducing=False` with its CONTENT UNTOUCHED — so a row
# minted by older producer code keeps that code's fields for as long as the finding lives and is
# republished on every run, without `to_wire` ever seeing it.
#
# EVERY OTHER TEST IN THIS FILE EXERCISES FRESHLY BUILT ROWS, which is precisely why none of them
# catches this: a row built by the current `suggestion_to_wire` is already clean, so a probe of
# fresh output proves nothing about the replay path. The fixture below is deliberately in the OLD
# shape, copied from what the deployed producer actually left in the staging store.

#: A store row exactly as pre-change producer code wrote it. `memberRunIds` and `rootCauseHypo` on
#: the pattern, no `runCount` anywhere, plus a `dismissReason` from a local operator decision.
_LEGACY_STORE_ROW = {
    "suggestionId": "ec7e1ed065a6",
    "createdAt": "2026-08-29T08:00:12Z",
    "status": "dismissed",
    "targetType": "code_change",
    "targetId": "chainer-settle-rule",
    "targetName": "chainer settle rule",
    "targetLocation": "infra/lib/lambdas/plan-chainer/handler.py:432",
    "baseVersion": None,
    "mutability": "LOCKED",
    "operation": "edit",
    "summary": "Hold a step until its dependencies finish",
    "evidenceSummary": "from 1 run(s) where a step was marked done",
    "pattern": {
        "patternId": "gateway_reported_failure",
        "shape": "gateway_reported_failure",
        "shapeFacets": ["gateway_reported_failure"],
        "memberRunIds": ["bc42dea7-da9e-4e18-9448-338b7a4d1697"],
        "passHits": 0,
        "rootCauseHypo": "failed: ['sg5_index']",
    },
    "candidates": [],
    "singleCandidateForced": None,
    "blastRadius": {"class": "single", "instanceCount": 1},
    "affordances": {"canPublish": False},
    "evidenceQuality": {"runCount": 1, "caveats": []},
    "provenance": {"evaluatedAt": "2026-08-29T08:00:12Z"},
    "firstSeenAt": "2026-08-20T08:00:00Z",
    "lastSeenAt": "2026-08-29T08:00:12Z",
    "reproducing": True,
    "dismissReason": "not worth it",
}


def test_the_publish_funnel_scrubs_a_row_written_by_older_producer_code():
    """THE regression fixture. A legacy store row replayed through the publish rule must carry none
    of what the current projection withholds.

    Asserted through `publishable_suggestions`, which IS the production expression at both call
    sites — so this covers the wiring and not merely the existence of a scrubber.
    """
    published = publishable_suggestions([_LEGACY_STORE_ROW])
    assert len(published) == 1
    dumped = json.dumps(published[0])

    # The two pattern fields, the session id inside them, and the operator's private note.
    for probe in ("memberRunIds", "rootCauseHypo", "bc42dea7-da9e-4e18-9448-338b7a4d1697",
                  "dismissReason", "not worth it", "sg5_index"):
        assert probe not in dumped, f"{probe!r} survived the publish funnel"
    assert withheld_fields(published[0]) == []

    # ...and the CLAIM is intact: the row is still a usable suggestion, not a husk.
    row = published[0]
    assert row["suggestionId"] == "ec7e1ed065a6"
    assert row["evidenceSummary"].startswith("from 1 run(s)")
    assert row["pattern"]["shapeFacets"] == ["gateway_reported_failure"]
    assert row["reproducing"] is True and row["firstSeenAt"] == "2026-08-20T08:00:00Z"
    # A local dismissal is never published, so every row goes out open.
    assert row["status"] == "open"


def test_a_replayed_row_keeps_a_run_count_derived_from_the_ids_being_withheld():
    """The count is the safe half of `memberRunIds`, so it is DERIVED rather than dropped with it.

    The staging row carried no `runCount` at all — it predates the field — so without this a
    long-lived finding, which is exactly the kind most likely to be on screen, would render
    "run(s)" with no number beside it.
    """
    legacy = publishable_suggestion(_LEGACY_STORE_ROW)
    assert legacy["pattern"]["runCount"] == 1

    two = {**_LEGACY_STORE_ROW,
           "pattern": {**_LEGACY_STORE_ROW["pattern"], "memberRunIds": ["a", "b"]}}
    assert publishable_suggestion(two)["pattern"]["runCount"] == 2

    # A producer-supplied count wins over the derivation.
    fresh = {**_LEGACY_STORE_ROW,
             "pattern": {**_LEGACY_STORE_ROW["pattern"], "runCount": 8}}
    assert publishable_suggestion(fresh)["pattern"]["runCount"] == 8

    # Neither present: the key is ABSENT, never a fabricated 0 that would read as a measurement.
    blank = {**_LEGACY_STORE_ROW, "pattern": {"patternId": "x", "shape": "x", "shapeFacets": []}}
    assert "runCount" not in publishable_suggestion(blank)["pattern"]


def test_a_replayed_row_gets_the_headline_its_frozen_content_cannot_carry():
    """A row that stopped reproducing keeps the bytes it was written with forever, so the builder
    can never reach it — the publish funnel is the only place a change can.

    Not hypothetical. On the live item the two suggestions were last seen five days apart, and the
    older one is `reproducing: False`: it would have been the one card on the surface with no lead
    line, resting on `has_failed_node`, one of only two facets the bundle has ever carried. Derived
    from the `shapeFacets` the row already holds, by the same `headline_for` the builder calls, so
    this is one table reached twice rather than a second opinion about the same facet.
    """
    legacy = publishable_suggestion(_LEGACY_STORE_ROW)
    assert legacy["evidenceHeadline"] == "A step's job reported failure"
    # ...and the frozen sentence beside it is untouched, `run(s)` and all.
    assert legacy["evidenceSummary"] == "from 1 run(s) where a step was marked done"

    # A row that carries its own headline keeps it: the derivation fills an absence, never overrides.
    supplied = {**_LEGACY_STORE_ROW, "evidenceHeadline": "A step failed"}
    assert publishable_suggestion(supplied)["evidenceHeadline"] == "A step failed"

    # Nothing to derive from, so nothing is published — not an empty string, and not the facet name.
    for pattern in ({"patternId": "x", "shape": "x", "shapeFacets": []},
                    {"patternId": "x", "shape": "x", "shapeFacets": ["some_new_facet"]}):
        row = publishable_suggestion({**_LEGACY_STORE_ROW, "pattern": pattern})
        assert "evidenceHeadline" not in row, row.get("evidenceHeadline")

    # An empty string on a hand-fed row loses the key rather than publishing a blank lead line.
    hollow = {**_LEGACY_STORE_ROW, "evidenceHeadline": "",
              "pattern": {"patternId": "x", "shape": "x", "shapeFacets": []}}
    assert "evidenceHeadline" not in publishable_suggestion(hollow)


def test_the_publish_funnel_is_a_no_op_for_a_freshly_built_suggestion(registry):
    """And that is the point of routing both paths through it: no behaviour changes for fresh rows,
    so afterwards there is one funnel and a field added to the per-actor half later is withheld from
    replayed rows automatically."""
    fresh = _wire(registry)
    fresh_stored = {**fresh, "firstSeenAt": "2026-08-25T04:00:00Z",
                    "lastSeenAt": "2026-08-25T04:00:00Z", "reproducing": True}
    assert publishable_suggestion(fresh_stored) == fresh_stored
    assert publishable_suggestions([fresh_stored]) == [{**fresh_stored, "status": "open"}]


def test_the_publish_allowlist_matches_what_the_builders_emit(registry):
    """The allowlists and the builders are two statements of one contract, pinned to each other so
    they cannot drift — which is the risk an allowlist carries and a denylist does not.

    The suggestion side is `suggestion_to_wire`'s keys plus the three the store adds afterwards; the
    pattern side is exactly `_pattern_to_wire`'s.
    """
    from improve.dto import _PUBLISHED_PATTERN_KEYS, _PUBLISHED_SUGGESTION_KEYS

    fresh = _wire(registry)
    store_added = {"firstSeenAt", "lastSeenAt", "reproducing"}
    assert set(fresh) | store_added == set(_PUBLISHED_SUGGESTION_KEYS)
    assert set(fresh["pattern"]) == set(_PUBLISHED_PATTERN_KEYS)
    # The suggestion allowlist is the TypeScript `Suggestion` interface, which the file's own key set
    # mirrors, plus the optional lead line the TypeScript has yet to declare. The PATTERN allowlist is
    # the producer alone for now: `FailurePattern` declares five of its ten keys, and the five in
    # `_NOT_YET_TYPED` are emitted and typed by nothing.
    assert set(_PUBLISHED_SUGGESTION_KEYS) == (
        SUGGESTION_KEYS | SUGGESTION_OPTIONAL_KEYS | store_added)
    assert SUGGESTION_OPTIONAL_KEYS <= _NOT_YET_TYPED
    assert set(_PUBLISHED_PATTERN_KEYS) == PATTERN_KEYS
    assert PATTERN_KEYS & _NOT_YET_TYPED == {
        "symptom", "controlsExcluded", "controlsInconclusive", "controlsConsidered",
        "equallyDiscriminative"}
    # `dismissReason` is excluded deliberately: it is the note beside a locally-set status, and
    # `publishable_suggestions` already refuses to publish the status itself.
    assert "dismissReason" not in _PUBLISHED_SUGGESTION_KEYS


def test_withheld_fields_finds_a_leak_at_any_depth_and_a_real_bundle_has_none(registry):
    """The detector, and the structural half of the content rule.

    Name-based and recursive on purpose: it assumes nothing about WHERE a leak would appear, so it
    covers a block this module does not know about yet — including the diverted rows, which the
    suggestion funnel does not touch and which a hand-fed bundle can still carry.
    """
    assert withheld_fields({"a": [{"b": {"memberRunIds": ["x"]}}]}) == ["memberRunIds@a[].b.memberRunIds"]
    assert withheld_fields({"patterns": {"residue": [{"runId": "r", "refinementLog": []}]}}) == [
        "refinementLog@patterns.residue[].refinementLog", "runId@patterns.residue[].runId"]
    assert withheld_fields({"suggestions": []}) == []

    # A real emitted bundle over the real golden population carries none of them.
    sessions = _golden_sessions()[:3]
    runs, verdicts = _owned_population({s: _ACTOR_A for s in sessions})
    wire = to_wire(result=_owned_result(registry, sessions), runs=runs, registry=registry,
                   created_at="2026-08-25T04:00:00Z", verdicts=verdicts)
    assert withheld_fields(wire) == []

    # ...and the per-actor bundle is where those very names belong, so it MUST trip the detector —
    # otherwise this test would pass on a producer that had stopped carrying the evidence at all.
    bundle = actor_bundles(result=_owned_result(registry, sessions), runs=runs,
                           verdicts=verdicts)[_ACTOR_A]
    tripped = {entry.split("@")[0] for entry in withheld_fields(bundle)}
    assert {"memberRunIds", "rootCauseHypo", "runId"} <= tripped


# --- the delivered share is a count pair, not a percentage -------------------
def test_component_health_carries_counts_a_percentage_could_not_state_honestly():
    """The defect that changed this field's shape, measured on the population that showed it.

    `100.0 * 2 / 3` is `66.66666666666667`, and the UI set the number it was given verbatim — so a
    component with three attributing runs asserted sixteen significant digits. Halves and quarters
    hid it (`1` of `2` read `50%`), thirds, sixths and sevenths did not.

    Two integers cannot do that. They also cannot hide the denominator, which here is the one number
    a reader most needs: it is NOT `runs`, and under the percentage that could only be said in a
    tooltip.
    """
    runs_by_id = {f"r{i}": _resource_run(f"r{i}") for i in range(1, 4)}
    # Two delivered, one did not — the 2-of-3 case.
    verdicts_by_id = {
        "r1": _verdict("r1", Overall.PASS, dispatch=0.9, delivered=0.9),
        "r2": _verdict("r2", Overall.PASS, dispatch=0.9, delivered=0.8),
        "r3": _verdict("r3", Overall.FAIL, dispatch=0.9, delivered=0.1),
    }
    suggestion, members = _health_suggestion("c", ["r1", "r2", "r3"])
    health = component_health([suggestion], runs_by_id, verdicts_by_id, members)

    assert health == {"c": {"runs": 3, "delivered": 2, "deliveredOf": 3}}
    # Integers, and specifically not `bool`: `sum()` over the PASS flags is what produces the
    # numerator, and a stray `True` would serialize as `true` and render as a word.
    for value in health["c"].values():
        assert type(value) is int
    # No float reaches the wire from here at all, so there is nothing left to round.
    assert "66" not in json.dumps(health)
    assert "." not in json.dumps(health)


def test_component_health_denominator_excludes_the_runs_delivery_could_not_be_judged_on():
    """The denominator is the JUDGED runs, so `runs` and `deliveredOf` genuinely differ.

    The whole reason this is a pair rather than one number over `runs`. Three runs attribute, one
    was never scored on delivery, and the share is over the two that were — a fraction reading
    `1 of 2` beside a run count of `3`, which is the state the old tooltip had to explain.
    """
    runs_by_id = {f"r{i}": _resource_run(f"r{i}") for i in range(1, 4)}
    verdicts_by_id = {
        "r1": _verdict("r1", Overall.PASS, dispatch=0.9, delivered=0.9),
        "r2": _verdict("r2", Overall.FAIL, dispatch=0.9, delivered=0.1),
        # `delivered=None` is NO_DATA: the tier exists and holds no measurement.
        "r3": _verdict("r3", Overall.UNSCORED, dispatch=0.9, delivered=None),
    }
    suggestion, members = _health_suggestion("c", ["r1", "r2", "r3"])
    health = component_health([suggestion], runs_by_id, verdicts_by_id, members)

    assert health == {"c": {"runs": 3, "delivered": 1, "deliveredOf": 2}}
    assert health["c"]["deliveredOf"] != health["c"]["runs"]


def test_component_health_states_a_measured_zero_and_an_unjudged_row_differently():
    """Three states, and the pair keeps all three apart — the point of not collapsing them.

    A measured zero (`0 of 2`) is a finding: two runs were judged and neither delivered. A row
    nothing could judge (`deliveredOf: 0`) is not, and a component nothing attributes to is absent
    from the map so `row()` leaves its `health` `None`. Any two of those reading alike would let a
    missing measurement pass for a bad one, or for a good one.
    """
    runs_by_id = {f"r{i}": _resource_run(f"r{i}") for i in range(1, 5)}
    verdicts_by_id = {
        "r1": _verdict("r1", Overall.FAIL, dispatch=0.9, delivered=0.1),
        "r2": _verdict("r2", Overall.FAIL, dispatch=0.9, delivered=0.0),
        "r3": _verdict("r3", Overall.UNSCORED, dispatch=0.9, delivered=None),
        "r4": _verdict("r4", Overall.UNSCORED, dispatch=0.9, delivered=None),
    }
    measured, measured_members = _health_suggestion("zero", ["r1", "r2"], suggestion_id="s-zero")
    unjudged, unjudged_members = _health_suggestion("unjudged", ["r3", "r4"],
                                                    suggestion_id="s-unjudged")
    health = component_health([measured, unjudged], runs_by_id, verdicts_by_id,
                              {**measured_members, **unjudged_members})

    assert health["zero"] == {"runs": 2, "delivered": 0, "deliveredOf": 2}
    assert health["unjudged"] == {"runs": 2, "delivered": 0, "deliveredOf": 0}
    assert health["zero"] != health["unjudged"]
    # And the third state is not in the map at all, which is what `row()` reads as UNKNOWN.
    assert "never-attributed" not in health


# --- the warrant a suggestion carries for its own accusation -------------------
def test_a_suggestion_quotes_the_registry_SENTENCE_that_justified_blaming_the_component(registry):
    """`attributionBasis: "registry_declared"` says the registry declares this component fails this
    way. Which declaration matched was unrecoverable from the bundle — measured on the live global
    item, both suggestions said `registry_declared` and neither said what had been declared — so the
    basis was an unfalsifiable label.

    Built through the PRODUCTION attribution path (`propose.attribute` over the fixture registry)
    rather than from a literal, because the point is that the wire carries the registry's own text
    and not that a dict key survives a copy.
    """
    from improve.propose import attribute

    best = attribute(FailureSignature(run_id="r", symptom=(),
                                      shape=("session_ended_on_unretrieved_dispatch",),
                                      pass_hits=0), registry)[0]
    assert best.component_id == "declaring-tool"
    assert best.basis == "registry_declared"

    wire = _wire(registry, _proposal(target="declaring-tool", attribution={
        "basis": best.basis, "mutability": best.mutability,
        "source_location": best.source_location,
        "matched_failure_mode": best.matched_failure_mode}))
    assert wire["provenance"]["attributionBasis"] == "registry_declared"
    assert wire["provenance"]["matchedFailureMode"] == (
        "never prompts a read_result once a job has been started.")
    # The bound is a FIRST SENTENCE, so the declaration's second sentence does not travel.
    assert "must drop" not in wire["provenance"]["matchedFailureMode"]


def test_a_declared_basis_can_never_reach_the_wire_with_nothing_to_quote(registry):
    """The one state that would put us back where we started. `registry.match` reaches
    `registry_declared` only through a word-boundary hit INSIDE `known_failure_modes`, so the field
    it quotes is non-empty by construction — and this asserts it over every component that declares
    anything, so the guarantee is a property of the registry path rather than of one fixture row."""
    from improve.propose import _FACET_KEYWORDS, attribute

    declared = [c for c in registry.all() if c.known_failure_modes]
    assert declared, "the fixture must declare a failure mode or this test proves nothing"
    for facet in _FACET_KEYWORDS:
        for best in attribute(FailureSignature(run_id="r", symptom=(), shape=(facet,),
                                               pass_hits=0), registry):
            if best.basis == "registry_declared":
                assert best.matched_failure_mode, best.component_id


def test_a_keyword_inferred_suggestion_quotes_nothing_and_its_basis_says_why(registry):
    """Empty here is not a dropped measurement: a `keyword_inferred` hit matched the component's id,
    type, notes or path, so there was no declaration to quote. The two fields are emitted together
    and always, so no consumer has to branch on the key existing to read the pair."""
    wire = _wire(registry)
    assert wire["provenance"]["attributionBasis"] == "keyword_inferred"
    assert wire["provenance"]["matchedFailureMode"] == ""


def test_a_component_row_carries_the_declaration_an_attribution_QUOTES(registry):
    """One definition, so the Σ map and the suggestion cannot show two truncations of one sentence.

    A reader checking an accusation looks at the component it names; if the row's declaration and the
    suggestion's quote differ by a character, they have to work out which is authoritative. They come
    from `Component.declared_failure_mode` and nowhere else.
    """
    row = next(r for r in components_to_wire(registry, [])["admin"]["components"]
               if r["componentId"] == "declaring-tool")
    assert row["knownFailureModes"] == registry.get("declaring-tool").declared_failure_mode
    assert row["type"] == "tool_wrapper"
    assert row["blastRadius"] == "all sessions"
    assert row["notes"] == "Wraps the async job API."
    # Both prose fields are bounded, because the Σ map's bytes come out of the suggestion list's
    # allowance and `notes` runs to 375 characters in the real registry.
    assert "must drop" not in row["knownFailureModes"] + row["notes"]


def test_a_component_that_declares_nothing_carries_an_empty_declaration_not_a_guess(registry):
    """37 of the 80 real components declare no failure modes. The row says so with an empty string
    rather than borrowing `notes` or the source path, either of which would read as a declaration the
    registry never made."""
    row = next(r for r in components_to_wire(registry, [])["fixFirst"]["components"]
               if r["componentId"] == "locked-rule")
    assert row["knownFailureModes"] == ""
    assert row["notes"] == ""
    assert row["sourceLocation"]      # ...and the fields that ARE recorded still travel


# --- the gate, published --------------------------------------------------------
def _gated_signature(**over) -> dict:
    base = {"run_id": "run-1", "shape": ["fanout_parent_ran_early"],
            "symptom": ["check_failed:DagCompletionValidator", "tier_failed:results_delivered"],
            "pass_hits": 0, "controls_excluded": 3, "controls_inconclusive": ["blind-1", "blind-2"],
            "controls_considered": 5, "equally_discriminative": ["has_fanout_node"],
            "evidence": {"fanout_parent_ran_early": "sg2 finished before sg1"}}
    base.update(over)
    return base


def test_a_pattern_publishes_the_controls_its_pass_hit_count_was_measured_against(registry):
    """`passHits: 0` against one control is nearly free and against twelve it is a claim — `mine`'s
    own docstring says so — and the live global item shipped `passHits: 0` with no control count at
    all, so the two rendered identically.

    Three numbers because two of them are denominators, and they CLOSE: excluded plus inconclusive is
    the whole pool, so a reader can check the triple against itself.
    """
    pattern = _wire(registry, _proposal(signature=_gated_signature()))["pattern"]
    assert pattern["passHits"] == 0
    assert pattern["controlsExcluded"] == 3
    assert pattern["controlsInconclusive"] == 2
    assert pattern["controlsConsidered"] == 5
    assert pattern["controlsExcluded"] + pattern["controlsInconclusive"] == (
        pattern["controlsConsidered"])
    # The tie is a caveat on the claim: this shape was chosen by precedence, not by the evidence.
    assert pattern["equallyDiscriminative"] == ["has_fanout_node"]


def test_a_zero_control_gate_is_distinguishable_from_a_twelve_control_one(registry):
    """The defect stated as the two states it could not tell apart. Same `passHits`, same shape,
    same run count; the only thing that differs is how much the gate had to test against, and that
    is the whole strength of the claim."""
    nearly_free = _wire(registry, _proposal(signature=_gated_signature(
        controls_excluded=0, controls_inconclusive=[], controls_considered=0)))["pattern"]
    a_claim = _wire(registry, _proposal(signature=_gated_signature(
        controls_excluded=12, controls_inconclusive=[], controls_considered=12)))["pattern"]
    assert nearly_free["passHits"] == a_claim["passHits"] == 0
    assert nearly_free != a_claim
    assert (nearly_free["controlsConsidered"], a_claim["controlsConsidered"]) == (0, 12)


def test_an_inconclusive_control_reaches_the_wire_as_a_count_and_never_as_an_id(registry):
    """An inconclusive control is ANOTHER RUN in the population and belongs to whichever tenant ran
    it, so its id is safe neither on the shared item nor on the failing run's owner's item — the
    identical argument `residue_to_wire` gives for keeping only `passHits` and withholding
    `pass_hit_runs`. There is no field for these ids anywhere on the wire."""
    signature = _gated_signature(controls_inconclusive=["4a7c9e10-secret", "b2d1f003-secret"])
    wire = _wire(registry, _proposal(signature=signature))
    dumped = json.dumps(wire)
    assert wire["pattern"]["controlsInconclusive"] == 2
    for run_id in signature["controls_inconclusive"]:
        assert run_id not in dumped, "an inconclusive control's session id reached the wire"


def test_an_admitted_pattern_names_the_symptom_it_predicts(registry):
    """A signature is a (shape -> symptom) hypothesis. `residue`, `evidenceGaps` and `environment`
    all publish the symptom already; `patterns.admitted` and `suggestions[].pattern` — the rows that
    produced the recommendations — published the antecedent alone, so two patterns differing only in
    which gating check failed rendered identically.

    Evaluator names, which is the same class of value `scorecard.outcomes` publishes globally.
    """
    grouped = {"shape": ("has_failed_node",),
               "symptom": ("check_failed:DagCompletionValidator",),
               "member_run_ids": ["r1", "r2"], "evidence_by_run": {},
               "controls_excluded": 4, "controls_inconclusive": (), "controls_considered": 4}
    wire = to_wire(result={"proposals": [], "patterns": [grouped]}, runs=[], registry=registry,
                   created_at="2026-08-25T04:00:00Z")
    admitted = wire["patterns"]["admitted"][0]
    assert admitted["symptom"] == ["check_failed:DagCompletionValidator"]
    assert admitted["controlsExcluded"] == 4 and admitted["controlsConsidered"] == 4
    # ...and on the suggestion built from the same signature.
    assert _wire(registry, _proposal(signature=_gated_signature()))["pattern"]["symptom"] == [
        "check_failed:DagCompletionValidator", "tier_failed:results_delivered"]


def test_an_unattributed_pattern_publishes_its_symptom_too(registry):
    """The same half-hypothesis, one bucket over. Two unattributed records with one topology and two
    failing checks are two findings, and this is what makes them distinguishable."""
    wire = to_wire(result={"proposals": [], "unattributed": [
        {"shape": ["some_unmapped_facet"], "symptom": ["tier_failed:dispatch_correct"],
         "member_run_ids": ["r1"], "cause": "no_search_terms", "reason": "nothing owns it"}]},
        runs=[], registry=registry, created_at="2026-08-25T04:00:00Z")
    entry = wire["patterns"]["unattributed"][0]
    assert entry["symptom"] == ["tier_failed:dispatch_correct"]
    assert set(entry) >= UNATTRIBUTED_KEYS


# --- what the platform was doing under the population --------------------------
def _health_run(run_id: str, state: InfraState, *, alarms=(), indeterminate=(),
                source="harvested-alarm-history", reason="because") -> Run:
    window = RunWindow(start="2026-08-13T18:00:00+00:00", end="2026-08-13T18:40:00+00:00",
                       provenance="derived_span_bounds")
    return Run(run_id=run_id, window=window,
               infra_health=InfraHealth(state=state, reason=reason, alarms=tuple(alarms),
                                        indeterminate=tuple(indeterminate), window=window,
                                        source=source))


def test_the_scorecard_says_what_the_platform_was_doing_under_the_population():
    """`InfraHealth` is one CloudWatch correlation PER RUN — the most expensive observation in the
    harvest — and the population's state reached the served item nowhere.

    Not hypothetical: on the live global item 5 of 5 failures were diverted for platform reasons and
    ZERO patterns were admitted, so the bundle read as "nothing found" and the only way to learn
    otherwise was to read five near-identical 577-character paragraphs.

    The four run counts PARTITION `runs`, which is what makes any one of them readable.
    """
    runs = [
        _health_run("d", InfraState.DEGRADED, alarms=("StuckJobsSweptAlarm",),
                    indeterminate=("FanoutDlqDepthAlarm",)),
        _health_run("h", InfraState.HEALTHY),
        _health_run("u", InfraState.UNKNOWN, indeterminate=("FanoutDlqDepthAlarm",
                                                            "zero-scale-alarm")),
        Run(run_id="never-looked"),           # the UNKNOWN sentinel: no source was consulted
    ]
    verdicts = [_verdict(r.run_id, Overall.PASS, dispatch=0.9, delivered=0.9) for r in runs]
    platform = scorecard_to_wire(runs, verdicts)["platform"]

    assert platform["state"] == "degraded", "one degraded run compromises the population"
    assert platform["degraded"] == 1
    assert platform["healthy"] == 1
    assert platform["inconclusive"] == 1
    assert platform["unconsulted"] == 1
    assert platform["runs"] == 4
    assert (platform["degraded"] + platform["healthy"] + platform["inconclusive"]
            + platform["unconsulted"] == platform["runs"])
    # DISTINCT names, unioned: two runs each naming `FanoutDlqDepthAlarm` is one alarm.
    assert platform["alarmsInAlarm"] == 1
    assert platform["alarmsIndeterminate"] == 2
    assert platform["sources"] == ["harvested-alarm-history"]


def test_a_population_nobody_asked_about_the_platform_for_is_unconsulted_not_healthy():
    """The offline golden path's actual state, and the distinction the three-state type exists for. A
    lookup that never ran and one that ran and could not tell are different facts and only the first
    is fixable by configuration — so they are separate counts, and neither reports healthy."""
    runs = [Run(run_id="a"), Run(run_id="b")]
    platform = scorecard_to_wire(
        runs, [_verdict(r.run_id, Overall.PASS, dispatch=0.9, delivered=0.9) for r in runs]
    )["platform"]
    assert platform["state"] == "unknown"
    assert platform["unconsulted"] == 2
    assert platform["inconclusive"] == 0
    assert platform["healthy"] == 0
    assert platform["sources"] == []


def test_an_empty_population_reports_unknown_because_nothing_was_measured():
    """`healthy` would be the one reading no evidence supports. Zero runs is zero measurements."""
    platform = scorecard_to_wire([], [])["platform"]
    assert platform["state"] == "unknown"
    assert platform["runs"] == 0
    assert platform["alarmsInAlarm"] == 0


def test_an_all_healthy_population_reports_healthy_so_the_state_is_not_decorative():
    """The test above is only meaningful if `healthy` is reachable at all."""
    runs = [_health_run("a", InfraState.HEALTHY), _health_run("b", InfraState.HEALTHY)]
    platform = scorecard_to_wire(
        runs, [_verdict(r.run_id, Overall.PASS, dispatch=0.9, delivered=0.9) for r in runs]
    )["platform"]
    assert platform["state"] == "healthy"
    assert platform["healthy"] == 2


def test_the_scorecard_platform_block_names_no_alarm_and_stays_fixed_size():
    """Counts and a state, never a growing list: `scorecard_to_wire` is documented fixed size so its
    bytes never come out of the suggestion list's allowance in `aws_lambda._fit_bundle`, and the
    account's alarm roster grows with the account. The NAMES travel per row on
    `patterns.environment`, where the row is about one run and the list is what a reader takes to
    CloudWatch."""
    runs = [_health_run(f"r{i}", InfraState.DEGRADED,
                        alarms=(f"mediaworks-harness-infra-staging-Alarm{i}",))
            for i in range(30)]
    platform = scorecard_to_wire(
        runs, [_verdict(r.run_id, Overall.PASS, dispatch=0.9, delivered=0.9) for r in runs]
    )["platform"]
    dumped = json.dumps(platform)
    assert platform["alarmsInAlarm"] == 30
    for i in range(30):
        assert f"Alarm{i}" not in dumped
    # Nor a session id, which is the other thing a per-run fold could leak onto the shared item.
    assert "r0" not in dumped
    assert len(dumped) < 300, f"the block grew with the population: {len(dumped)} bytes"


def test_an_environment_divert_carries_the_platform_state_structurally_not_only_in_prose():
    """The bucket whose divert was DECIDED by a platform reading is the one that carries the reading.

    Before this, `mine._environment_entry` flattened state, alarms and source into one sentence, so a
    consumer had no branchable field and a reader had 577 characters of prose per diverted failure.
    """
    entry = {"run_id": "d", "symptom": ["tier_failed:results_delivered"], "best_shape": [],
             "pass_hits": -1, "pass_hit_runs": [], "reason": "platform was DEGRADED ..."}
    run = _health_run("d", InfraState.DEGRADED, alarms=("StuckJobsSweptAlarm",),
                      indeterminate=("FanoutDlqDepthAlarm",))
    wire = to_wire(result={"proposals": [], "environment": [entry]}, runs=[run],
                   registry=SigmaRegistry([], {}), created_at="2026-08-25T04:00:00Z")
    row = wire["patterns"]["environment"][0]
    assert set(row) == ENVIRONMENT_KEYS
    assert row["platform"]["state"] == "degraded"
    assert row["platform"]["alarms"] == ["StuckJobsSweptAlarm"]
    assert row["platform"]["alarmsIndeterminate"] == ["FanoutDlqDepthAlarm"]
    # The interval the correlation actually covered, and whether it was reconstructed — a HEALTHY on
    # a derived window may simply have missed an alarm just outside it.
    assert row["platform"]["window"]["derived"] is True
    assert row["platform"]["window"]["start"] == "2026-08-13T18:00:00+00:00"
    # ...and the engine's own sentence is still there: it is the only explanation an Inbox reader gets.
    assert row["reason"] == "platform was DEGRADED ..."
    # The other two diverted buckets must NOT grow the key: an empty platform block on them would
    # invite a reader to compare it against this one.
    assert "platform" not in residue_to_wire(entry)


def test_a_diverted_run_missing_from_the_population_is_unknown_never_healthy(registry):
    """The sentinel path. A result whose runs came from another invocation still produces a row, and
    it says nobody here could speak for the platform rather than reporting a clean one."""
    entry = {"run_id": "gone", "symptom": [], "best_shape": [], "pass_hits": -1,
             "pass_hit_runs": [], "reason": "platform was DEGRADED ..."}
    wire = to_wire(result={"proposals": [], "environment": [entry]}, runs=[], registry=registry,
                   created_at="2026-08-25T04:00:00Z")
    platform = wire["patterns"]["environment"][0]["platform"]
    assert platform["state"] == "unknown"
    assert platform["reason"] == "no platform-health source was consulted"
    assert platform["window"] == "UNKNOWN"


def test_a_platform_block_names_the_alarms_it_could_not_place():
    """`state: "unknown"` with four unplaceable alarms is a lookup that ran and could not decide;
    `state: "unknown"` with none is one that had nothing to decide about (no coverage, or no alarms
    configured at all). `infra_health_for_window` used to keep only `in_alarm is True`, so the
    indeterminate set reached a reader as prose and only its first three names.

    Absence is never zero, applied to alarms: the count of firing alarms is unreadable alone.
    """
    could_not_tell = platform_to_wire(
        InfraHealth(state=InfraState.UNKNOWN, reason="4 of 10 alarms could not be placed",
                    indeterminate=("a", "b", "c", "d")))
    nothing_to_tell = platform_to_wire(
        InfraHealth(state=InfraState.UNKNOWN, reason="no alarms found"))
    assert could_not_tell["state"] == nothing_to_tell["state"] == "unknown"
    assert could_not_tell["alarms"] == nothing_to_tell["alarms"] == []
    assert could_not_tell["alarmsIndeterminate"] == ["a", "b", "c", "d"]
    assert nothing_to_tell["alarmsIndeterminate"] == []
    assert could_not_tell != nothing_to_tell


def test_the_platform_reason_is_not_also_appended_as_a_caveat():
    """Measured, not theorised: the live global item shipped this string TWICE per suggestion — 741
    characters at `platform.reason` and the same 741 again in `caveats`, 1,488 JSON bytes across two
    suggestions, 5.4% of a 27,605-byte bundle whose suggestion list is the one thing
    `aws_lambda._fit_bundle` trims.

    `platform.state` is what a consumer branches on and `platform.reason` is what it renders; a
    duplicate in a free-text list can only be rendered twice.
    """
    long_reason = "4 of 10 alarms could not be placed: " + "x" * 700
    runs = {"a": _health_run("a", InfraState.UNKNOWN, reason=long_reason),
            "b": _health_run("b", InfraState.UNKNOWN, reason=long_reason)}
    quality = evidence_quality(["a", "b"], runs)
    assert quality["platform"]["reason"] == long_reason
    assert long_reason not in quality["caveats"]
    assert not any(long_reason in caveat for caveat in quality["caveats"])
    assert json.dumps(quality).count("could not be placed") == 1


def test_dropping_the_duplicate_caveat_keeps_the_caveats_that_are_not_duplicates():
    """The two caveats that are NOT a copy of a structured field beside them stay: a one-run pattern
    cannot show a rate, and a pattern nothing attributes to has no evidence at all."""
    thin = evidence_quality(["a"], {"a": _health_run("a", InfraState.UNKNOWN)})
    assert any("one run" in caveat for caveat in thin["caveats"])
    assert evidence_quality([], {})["caveats"] == [
        "no run in the current dataset attributes here"]


def test_an_actors_own_environment_row_carries_the_same_platform_block(registry):
    """The owner and the admin surface get one story about one divert.

    "Your run was diverted" is not readable without what the platform was doing, so the per-actor
    copy carries the same structured block rather than only the sentence — and both are spread over
    one projection (`environment_to_wire`), so the two cannot come to disagree.
    """
    session = _golden_sessions()[0]
    runs, verdicts = _owned_population({session: _ACTOR_A})
    runs[0].infra_health = InfraHealth(
        state=InfraState.DEGRADED, reason="platform was DEGRADED ...",
        alarms=("StuckJobsSweptAlarm",), indeterminate=("FanoutDlqDepthAlarm",),
        window=runs[0].window, source="harvested-alarm-history")
    entry = {"run_id": session, "symptom": ["tier_failed:results_delivered"], "best_shape": [],
             "pass_hits": -1, "pass_hit_runs": [], "reason": "platform was DEGRADED ..."}

    bundle = actor_bundles(result={"proposals": [], "environment": [entry]}, runs=runs,
                           verdicts=verdicts)[_ACTOR_A]
    row = bundle["patterns"]["environment"][0]
    assert set(row) == ENVIRONMENT_EVIDENCE_KEYS
    assert row["runId"] == session
    assert row["platform"]["state"] == "degraded"
    assert row["platform"]["alarms"] == ["StuckJobsSweptAlarm"]
    # The global row for the same divert says the same thing, minus the session id.
    global_row = to_wire(result={"proposals": [], "environment": [entry]}, runs=runs,
                         registry=registry, created_at="2026-08-25T04:00:00Z",
                         verdicts=verdicts)["patterns"]["environment"][0]
    assert {k: v for k, v in row.items() if k != "runId"} == global_row


def test_another_actors_divert_never_reaches_this_actors_environment_rows(registry):
    """The isolation guard on the new field. A platform block is account infrastructure and safe on
    either item, but the ROW it hangs off names a session, so the filter has to hold."""
    sessions = _golden_sessions()[:2]
    runs, verdicts = _owned_population({sessions[0]: _ACTOR_A, sessions[1]: _ACTOR_B})
    for run in runs:
        run.infra_health = InfraHealth(state=InfraState.DEGRADED, reason="degraded",
                                       alarms=("StuckJobsSweptAlarm",), window=run.window)
    entries = [{"run_id": s, "symptom": [], "best_shape": [], "pass_hits": -1,
                "pass_hit_runs": [], "reason": "platform was DEGRADED ..."} for s in sessions]

    bundles = actor_bundles(result={"proposals": [], "environment": entries}, runs=runs,
                            verdicts=verdicts)
    assert [r["runId"] for r in bundles[_ACTOR_A]["patterns"]["environment"]] == [sessions[0]]
    assert sessions[1] not in json.dumps(bundles[_ACTOR_A])


def test_the_publish_funnel_drops_a_caveat_that_only_repeats_the_platform_reason():
    """The builder alone cannot fix this, and that is the whole reason the projection is here.

    `store.upsert_all` refreshes a row's content only when the finding was SEEN AGAIN; a row that
    stopped reproducing keeps the bytes it was written with and is republished on every run. Both
    suggestions on the live global item are `reproducing: False`, so both were shipping the same 741
    characters twice — 1,488 JSON bytes off a 27,605-byte bundle, 5.4%, out of the allowance of the
    one list `aws_lambda._fit_bundle` trims. No change to `evidence_quality` could ever reach them.
    """
    reason = "4 of 10 alarms could not be placed: " + "x" * 700
    frozen = {**_LEGACY_STORE_ROW, "reproducing": False, "evidenceQuality": {
        "runCount": 1,
        "platform": {"state": "unknown", "reason": reason, "alarms": [],
                     "alarmsIndeterminate": [], "window": "UNKNOWN", "source": "harvested"},
        "caveats": ["one run, a pattern this thin cannot show a rate", reason],
    }}
    before = len(json.dumps(frozen["evidenceQuality"], separators=(",", ":")))
    published = publishable_suggestion(frozen)
    after = len(json.dumps(published["evidenceQuality"], separators=(",", ":")))

    assert published["evidenceQuality"]["caveats"] == [
        "one run, a pattern this thin cannot show a rate"]
    assert published["evidenceQuality"]["platform"]["reason"] == reason, "the reason itself stays"
    assert before - after == len(json.dumps(reason)) + 1


def test_the_funnel_leaves_a_caveat_that_merely_RESEMBLES_the_platform_reason():
    """Exact equality only. These sentences reach a reader verbatim and no consumer can repair one,
    so shortening a caveat on a substring match would take away whatever part of it was not a
    quote."""
    reason = "4 of 10 alarms could not be placed: FanoutDlqDepthAlarm (chronic)"
    row = {**_LEGACY_STORE_ROW, "evidenceQuality": {
        "runCount": 1,
        "platform": {"state": "unknown", "reason": reason},
        "caveats": [reason + " — and the window was derived, so this may be a narrow-window miss"],
    }}
    assert publishable_suggestion(row)["evidenceQuality"]["caveats"] == (
        row["evidenceQuality"]["caveats"])


def test_the_funnel_leaves_a_row_that_carries_no_platform_block_alone():
    """A row written before `platform` existed has nothing to compare a caveat against, and its
    `evidenceQuality` must survive the funnel unchanged rather than be rebuilt around a key it has
    never had."""
    published = publishable_suggestion(_LEGACY_STORE_ROW)
    assert published["evidenceQuality"] == _LEGACY_STORE_ROW["evidenceQuality"]
