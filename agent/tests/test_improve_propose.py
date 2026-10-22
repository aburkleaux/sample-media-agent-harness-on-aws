"""Attribution and candidate generation.

The permission checks matter most: a LOCKED or unrecorded component must never yield a
publishable candidate, no matter how good the remedy. That is the permission matrix
being enforced in code rather than described in a document (REQ-019's discipline applied
to the proposer).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import FailureSignature, Plan, PlanNode, Run, Step  # noqa: E402
from improve.propose import (  # noqa: E402
    CONST_MIN_CANDIDATES, attribute, headline_for, propose, propose_all, prose_for, unified_diff,
)
from improve.registry import SigmaRegistry, default_registry_path  # noqa: E402
from improve.signature import shape_facets  # noqa: E402

# The longest a headline may be, in WORDS. Literal here for the reason every field set in
# `test_improve_dto.py` is literal: a bound read out of the module it constrains would agree with
# itself whatever it said, so widening `propose._MAX_HEADLINE_WORDS` has to fail a test.
#
# Eight is where a phrase acquires a subordinate clause. The headline leads `evidence_summary`,
# whose facet clause is thirty words on the live bundle and is the only line distinguishing one
# card from the next, so a lead line that is itself a sentence buys the reader nothing.
MAX_HEADLINE_WORDS = 8

# The headlines themselves, literal for the same reason. Paired with the long form each one
# shortens, so a reader of this file can check that the short version did not quietly promote a
# careful statement into a confident one — `recorded finished` keeps its `recorded`, the two
# async-lifecycle facets still say different things, `handed off` is not `ran`, and the three
# evidence gaps still describe OUR records rather than the run's behaviour.
FACET_HEADLINES = {
    "fanout_parent_ran_early": "A relied-on step was recorded finished early",
    "edge_order_violation": "A step was recorded finished before its dependency",
    "terminal_node_without_gateway_status": "A step was marked done without an outcome",
    "has_failed_node": "A step failed",
    "session_ended_on_unretrieved_dispatch": "The last job's result was never fetched",
    "dispatch_unpaired_result_unobserved": "A job's result fetch was not recorded",
    "has_fanout_node": "One step's output feeds several later steps",
    "wide_plan": "The plan had four or more steps",
    "deep_plan": "The steps chain three or more deep",
    "chained_plan": "The steps depend on each other",
    "all_task_nodes_terminal": "Every step reported success",
    "multi_node_plan": "The plan had more than one step",
    "flat_plan": "The steps did not depend on each other",
    "async_dispatched": "Work was handed off as a background job",
    "has_non_terminal_node": "A step never finished",
    "gateway_reported_failure": "A step's job reported failure",
    "plan_absent": "The plan was never recorded",
    "trajectory_absent": "No actions were recorded for this request",
    "plan_edges_unreadable": "The plan's step list could not be read",
}

FIXTURE = """
meta:
  head_sha: deadbee
  revalidated: 2026-08-21
prompt_layer:
  - component_id: fetch-guidance-persona
    type: system_prompt
    source_location: agent/src/lab/specialty.py:1
    versioned: YES (Lab asset lifecycle, immutable vN + working copy)
    mutability: AUTO_CANDIDATE
    blast_radius: single
    known_failure_modes: never tells the model to read_result after starting a job
control_layer:
  - component_id: chainer-readiness-rule
    type: gate
    source_location: infra/lib/lambdas/plan-chainer/handler.py:432-470
    versioned: NONE
    mutability: LOCKED
    blast_radius: single
  - component_id: undecided-ceiling
    type: budget
    source_location: agent/src/task_agent.py:131
    versioned: NONE
    blast_radius: single
    known_failure_modes: unbounded by default
"""


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "sigma.yaml"
    path.write_text(FIXTURE)
    return SigmaRegistry.load(str(path))


def _signature(*shape, run_id="e2cb300b"):
    return FailureSignature(run_id=run_id, symptom=("tier_failed:results_delivered",),
                            shape=tuple(shape), pass_hits=0,
                            evidence={s: f"evidence for {s}" for s in shape})


# --- attribution ------------------------------------------------------------
def test_ordering_failure_attributes_to_the_readiness_rule(registry):
    attributions = attribute(_signature("fanout_parent_ran_early"), registry)
    assert attributions
    assert attributions[0].component_id == "chainer-readiness-rule"
    assert attributions[0].source_location.startswith("infra/lib/lambdas/plan-chainer")


def test_attribution_carries_the_permission_facts_not_just_the_id(registry):
    best = attribute(_signature("fanout_parent_ran_early"), registry)[0]
    assert best.mutability == "LOCKED"
    assert best.versioned is False
    assert best.blast_radius == "single"
    assert best.basis in ("registry_declared", "keyword_inferred")


def test_a_facet_with_no_search_terms_attributes_to_nothing(registry):
    """Better to return no attribution than to blame whatever happens to match."""
    assert attribute(_signature("some_unmapped_facet"), registry) == []


def test_terms_from_several_facets_merge_by_max_not_sum(registry):
    """A broad signature must not outrank a specific one merely by carrying more facets."""
    one = attribute(_signature("fanout_parent_ran_early"), registry)
    many = attribute(_signature("fanout_parent_ran_early", "edge_order_violation",
                                "chained_plan", "deep_plan"), registry)
    assert one[0].component_id == many[0].component_id == "chainer-readiness-rule"


# --- disposition / permissions ---------------------------------------------
def test_locked_platform_code_yields_code_change_never_publishable(registry):
    proposal = propose(_signature("fanout_parent_ran_early"), registry)
    assert proposal is not None
    assert proposal.attribution.component_id == "chainer-readiness-rule"
    assert {c.disposition for c in proposal.candidates} == {"code_change"}
    assert all(c.disposition != "publishable" for c in proposal.candidates)


def test_locked_target_offers_two_genuinely_different_remedies(registry):
    proposal = propose(_signature("fanout_parent_ran_early"), registry)
    assert len(proposal.candidates) >= CONST_MIN_CANDIDATES
    labels = {c.label for c in proposal.candidates}
    assert len(labels) == len(proposal.candidates), "candidates must not be one fix restated"
    assert proposal.single_candidate_forced is None


def test_unrecorded_mutability_degrades_to_advisory_and_says_why(registry):
    proposal = propose(_signature("wide_plan"), registry)
    assert proposal.attribution.component_id == "undecided-ceiling"
    assert {c.disposition for c in proposal.candidates} == {"advisory"}
    # REQ-021 is not met here, and the reason is recorded rather than the list padded — and
    # it names the REAL reason (no applyable remedy) rather than blaming the wrong field.
    assert proposal.single_candidate_forced
    assert "not recorded" in proposal.single_candidate_forced
    assert "no version lineage" in proposal.single_candidate_forced


def test_publishable_target_without_current_text_degrades_to_advisory(registry):
    """A diff needs a real `before`. Inventing one would render in the UI exactly like a
    real diff, so absence must downgrade the proposal instead — and say so honestly."""
    proposal = propose(_signature("session_ended_on_unretrieved_dispatch"), registry)
    assert proposal.attribution.component_id == "fetch-guidance-persona"
    assert {c.disposition for c in proposal.candidates} == {"advisory"}
    assert all(not c.diff_text for c in proposal.candidates)
    # The reason must blame the unreadable text, not the permission column.
    assert "could not be read" in (proposal.single_candidate_forced or "")


def test_empty_current_text_is_distinguishable_from_unreadable(registry):
    """`if not current_text` conflated "" with None, and `_Sentinel.__bool__` is False so a
    NO_DATA would have been swallowed by the same branch and blamed on availability."""
    proposal = propose(_signature("session_ended_on_unretrieved_dispatch"), registry,
                       current_text_for=lambda _cid: "")
    assert {c.disposition for c in proposal.candidates} == {"publishable"}


def test_publishable_target_with_current_text_produces_a_real_diff(registry):
    current = "You are a media assistant.\nSummarise the results.\n"
    proposal = propose(_signature("session_ended_on_unretrieved_dispatch"), registry,
                       current_text_for=lambda _cid: current)
    candidate = proposal.candidates[0]
    assert candidate.disposition == "publishable"
    assert candidate.before == current
    assert candidate.after.startswith(current.rstrip())
    assert "fetch the result of every job" in candidate.after.lower()
    assert candidate.diff_text.startswith("---")
    assert "+" in candidate.diff_text
    # A real Lab category, not a Sigma layer — the guard for this was a no-op ternary whose
    # two branches were identical, so `target_type` always leaked the layer name.
    assert proposal.target_type == "skill" or proposal.target_type == "prompt"


def test_candidate_ids_are_unique_when_two_remedies_share_a_label_prefix(registry):
    """Labels are truncated to 70 chars for display, and two additions shared their first 70
    characters — so both candidates got the SAME id with different `after` text. A fan-out
    parent running early implies an ordering violation, so a shape with both is normal."""
    current = "Base prompt.\n"
    proposal = propose(_signature("fanout_parent_ran_early", "edge_order_violation"),
                       registry, current_text_for=lambda _cid: current)
    ids = [c.candidate_id for c in proposal.candidates]
    assert len(ids) == len(set(ids)), ids


# --- provenance -------------------------------------------------------------
def test_provenance_records_the_basis_the_alternatives_and_the_registry_version(registry):
    proposal = propose(_signature("fanout_parent_ran_early"), registry)
    provenance = proposal.provenance
    assert provenance["attribution_basis"] in ("registry_declared", "keyword_inferred")
    assert provenance["registry_head_sha"] == "deadbee"
    # Normalised to a string on load: YAML parses an unquoted date as
    # datetime.date, which json.dump cannot serialize.
    assert provenance["registry_revalidated"] == "2026-08-21"
    import json
    json.dumps(provenance)  # must not raise
    assert "attribution_alternatives" in provenance
    assert provenance["heuristic_facets"] == []


def test_blast_radius_counts_the_runs_the_pattern_rests_on(registry):
    proposal = propose(_signature("fanout_parent_ran_early"), registry,
                       member_run_ids=["a", "b", "c"])
    assert proposal.blast_radius == {"class": "single", "instanceCount": 3}
    assert "from 3 runs" in proposal.evidence_summary


def test_the_evidence_summary_agrees_with_a_single_run(registry):
    """`evidence_summary` reaches a reader verbatim, so it has to be English at a count of 1.

    Both counts asserted in one place: a helper that returned the singular unconditionally would
    satisfy the one-run case on its own, and `test_blast_radius...` above only pins the plural.
    """
    one = propose(_signature("fanout_parent_ran_early"), registry, member_run_ids=["a"])
    assert one.evidence_summary.startswith("from 1 run where ")
    two = propose(_signature("fanout_parent_ran_early"), registry, member_run_ids=["a", "b"])
    assert two.evidence_summary.startswith("from 2 runs where ")


def test_proposal_id_is_stable_for_the_same_component_and_shape(registry):
    first = propose(_signature("fanout_parent_ran_early"), registry)
    second = propose(_signature("fanout_parent_ran_early"), registry)
    assert first.proposal_id == second.proposal_id
    # ...and differs once the shape does, so two patterns cannot collide.
    other = propose(_signature("wide_plan"), registry)
    assert other.proposal_id != first.proposal_id


# --- presentation -----------------------------------------------------------
def test_summaries_are_plain_english_not_facet_names(registry):
    proposal = propose(_signature("fanout_parent_ran_early"), registry)
    assert "fanout_parent_ran_early" not in proposal.summary
    assert "fanout_parent_ran_early" not in proposal.evidence_summary
    assert "step" in proposal.evidence_summary


def test_prose_falls_back_readably_for_an_unmapped_facet():
    assert prose_for(("some_new_facet",)) == "some new facet"


def _facets_the_miner_can_emit() -> set:
    """Every facet name `signature.shape_facets` can produce, DERIVED from that function.

    Never listed as a literal: a hand-written list is the thing that goes stale, and a facet added
    later would then reach a user unlabelled with every test still green.

    Two runs are unioned because one of them contributes nothing today — `shape_facets` emits every
    facet for every run, present-and-False — and that stops being true the moment someone adds a
    facet inside a branch.
    """
    bare = Run(run_id="no-evidence")
    populated = Run(
        run_id="plan-and-spans",
        plan=Plan(nodes=[PlanNode(id="PLAN"),
                         PlanNode(id="a", status="completed", gateway_status="FAILED"),
                         PlanNode(id="b", depends_on=["a"], status="pending")]),
        trajectory=[Step(name="start_transcode_job"), Step(name="read_result")],
    )
    emitted = {f.name for run in (bare, populated) for f in shape_facets(run)}
    assert emitted, "shape_facets emitted nothing — every derivation from this would pass vacuously"
    return emitted


def test_every_facet_the_miner_can_emit_renders_as_a_sentence_not_a_variable_name():
    """`prose_for`'s fallback is `name.replace("_", " ")`, and it reaches the reader.

    `evidence_summary` is a finished sentence shown to a media editor, so an unmapped facet
    surfaces as "from 1 run where plan absent" — a variable name with the underscore taken
    out. Which facet gets shown depends on which one won the greedy refinement, so this is
    not a rare corner; before this was pinned, 8 of the 19 facets read that way.
    """
    from improve.propose import _FACET_PROSE

    emitted = _facets_the_miner_can_emit()

    for name in sorted(emitted):
        assert name in _FACET_PROSE, (
            f"{name} has no plain English, so a card carrying it reads "
            f'"...where {name.replace("_", " ")}" to a media editor')
        prose = _FACET_PROSE[name]
        assert prose != name.replace("_", " "), f"{name}'s entry only repeats its own name"
        assert "_" not in prose, f"{name}'s prose carries an identifier: {prose}"
        # The facet name itself, de-underscored, must not be embedded either — that is the
        # fallback smuggled inside a longer sentence.
        assert name.replace("_", " ") not in prose, f"{name}'s prose quotes its own name"

    # And the joined form a reader actually sees, over the whole set at once.
    assert "_" not in prose_for(sorted(emitted))


# --- the headline: the card's LEAD line -------------------------------------
def test_every_facet_the_miner_can_emit_has_exactly_one_headline():
    """Coverage in BOTH directions, which is the difference between this and the prose test above.

    Subset: a facet the miner can emit with no headline would leave the card leading with nothing,
    and which facet wins the greedy refinement decides which card that is — so a twentieth facet
    added to `shape_facets` fails here rather than shipping unlabelled.

    Superset: an entry for a facet the miner CANNOT emit is dead text that no reader will ever see,
    and it is how a stale line survives a facet's retirement (`dispatch_without_retrieve` left one
    behind in a sibling table). Equality catches both.

    The sentences are compared against literals in this file for the reason the bound is: a table
    read out of the module it describes would agree with itself whatever it said.
    """
    from improve.propose import _FACET_HEADLINE

    assert set(_FACET_HEADLINE) == _facets_the_miner_can_emit()
    assert _FACET_HEADLINE == FACET_HEADLINES


def test_a_headline_stays_inside_the_length_bound():
    """The bound is what stops the next careful qualifier from turning a lead line into a sentence.

    Pinned as a NUMBER as well as per entry: raising `_MAX_HEADLINE_WORDS` would otherwise let every
    headline grow at once, which is exactly the edit this guards against.
    """
    from improve.propose import _FACET_HEADLINE, _MAX_HEADLINE_WORDS

    assert _MAX_HEADLINE_WORDS == MAX_HEADLINE_WORDS
    for name, headline in _FACET_HEADLINE.items():
        words = headline.split()
        assert words, f"{name}'s headline is empty"
        assert len(words) <= MAX_HEADLINE_WORDS, (
            f"{name}'s headline is {len(words)} words: {headline!r}")


@pytest.mark.skipif(not os.path.exists(default_registry_path()),
                    reason="the sigma registry (ideas/sigma-registry.yaml) lives outside the repo (OURS-2)")
def test_no_headline_carries_the_engines_own_vocabulary():
    """The audience is a freelance video editor, so a lead line naming an identifier has failed.

    Four families of identifier, each of which a plausible-looking line could pick up: anything
    snake_case (every facet in this loop is), the facet names de-underscored (the `prose_for`
    fallback smuggled into a short line), and the Σ component ids and layer names read from the
    SHIPPED registry rather than a fixture, so the check covers the records that actually exist.

    Sentence-cased and unpunctuated at the end, unlike `_FACET_PROSE`'s fragments: a headline stands
    alone as a line rather than sitting after "from 1 run where".
    """
    from improve.propose import _FACET_HEADLINE

    shipped = SigmaRegistry.load(default_registry_path())
    records = [c.component_id for c in shipped.all()]
    assert records, "the shipped registry must load, or this check is vacuous"
    layers = {c.layer for c in shipped.all() if c.layer}

    for name, headline in _FACET_HEADLINE.items():
        assert "_" not in headline, f"{name}'s headline names an identifier: {headline!r}"
        assert not any(facet.replace("_", " ") in headline for facet in _FACET_HEADLINE), (
            f"{name}'s headline quotes a facet name: {headline!r}")
        assert not any(record in headline for record in records), (
            f"{name}'s headline names a Σ record: {headline!r}")
        assert not any(f" {layer} " in f" {headline.lower()} " for layer in layers), (
            f"{name}'s headline names a Σ layer: {headline!r}")
        assert headline[0].isupper() and not headline.endswith("."), (
            f"{name}'s headline is not a standalone line: {headline!r}")


def test_no_two_facets_share_a_headline():
    """The defect this whole field answers is two cards reading as near-duplicates.

    The live bundle's two suggestions differ in one clause and agree in every other line, so if two
    facets shared a lead line the surface would be back where it started. In particular the two
    async-lifecycle facets must not converge: one asserts a result was never fetched and the other
    asserts only that no fetch was recorded, and the split exists so those cannot be confused.
    """
    from improve.propose import _FACET_HEADLINE

    assert len(set(_FACET_HEADLINE.values())) == len(_FACET_HEADLINE)


def test_the_headline_names_the_facet_the_pattern_id_names():
    """One facet, not a joined conjunction: the FIRST, which is the one `patternId` publishes.

    `mine._refine` adds the most discriminative facet first, so the shape's leading name is both
    what the card is keyed on and the most defensible thing to lead with. The rest of the
    conjunction keeps its place in `prose_for`'s sentence underneath.
    """
    assert headline_for(("has_failed_node",)) == "A step failed"
    assert headline_for(("has_failed_node", "wide_plan")) == "A step failed"
    # And the headline is not the sentence: the long form for the same facet stays untouched.
    assert prose_for(("has_failed_node",)) == "a step failed"


def test_an_unmapped_leading_facet_omits_the_headline_rather_than_naming_itself():
    """Absence is absence — `dto._WASTE_KIND_PROSE`'s rule, and the opposite of `prose_for`'s.

    `prose_for` still falls back to the de-underscored name, which is what makes this contrast
    deliberate rather than an oversight: a long form degrades to something readable in a sentence,
    whereas a lead line has nothing around it to soften a machine name, so it is omitted.
    """
    assert headline_for(("some_new_facet",)) == ""
    assert headline_for(()) == ""
    assert headline_for(None) == ""
    # The leading facet decides. A mapped facet BEHIND an unmapped one does not get promoted into a
    # headline, because then the lead line and `patternId` would name different observations.
    assert headline_for(("some_new_facet", "has_failed_node")) == ""
    assert prose_for(("some_new_facet",)) == "some new facet"


def test_a_proposal_carries_the_headline_beside_its_unchanged_summary(registry):
    """Both, and the sentence is not shortened to make room for the line above it."""
    proposal = propose(_signature("session_ended_on_unretrieved_dispatch"), registry)
    assert proposal.evidence_headline == "The last job's result was never fetched"
    assert proposal.evidence_summary == (
        "from 1 run where the session finished without fetching the result of the last job it "
        "started, although it had fetched results for earlier jobs in the same session")


def test_a_proposal_on_an_unmapped_shape_carries_no_headline(registry):
    """The empty string, so `dto` can omit the field rather than publish a machine name."""
    patterns = [{"shape": ("wide_plan", "some_new_facet"), "symptom": (),
                 "member_run_ids": ["r1"], "evidence_by_run": {"r1": {}}, "heuristic_facets": ()}]
    proposals, _ = propose_all(patterns, registry)
    assert proposals[0].evidence_headline == "The plan had four or more steps"

    reversed_shape = [{**patterns[0], "shape": ("some_new_facet", "wide_plan")}]
    proposals, _ = propose_all(reversed_shape, registry)
    assert proposals[0].evidence_headline == ""
    # ...and the sentence still says everything, jargon fallback included.
    assert "some new facet" in proposals[0].evidence_summary


def test_unified_diff_is_empty_when_nothing_changed():
    assert unified_diff("same\n", "same\n") == ""


# --- batch ------------------------------------------------------------------
def test_propose_all_reports_patterns_nothing_owns_instead_of_dropping_them(registry):
    """An admitted pattern that produced no proposal used to vanish silently, which
    contradicts the principle residue exists for. It happens for any shape whose facets have
    no registry search terms — `flat_plan`, `multi_node_plan` and `async_dispatched` are all
    admissible and unmapped."""
    patterns = [
        {"shape": ("fanout_parent_ran_early",), "symptom": (), "member_run_ids": ["r1"],
         "evidence_by_run": {"r1": {}}, "heuristic_facets": ()},
        {"shape": ("some_unmapped_facet",), "symptom": (), "member_run_ids": ["r2"],
         "evidence_by_run": {"r2": {}}, "heuristic_facets": ()},
    ]
    proposals, unattributed = propose_all(patterns, registry)
    assert len(proposals) == 1
    assert proposals[0].attribution.component_id == "chainer-readiness-rule"
    assert len(unattributed) == 1
    assert unattributed[0]["shape"] == ["some_unmapped_facet"]
    assert "no registry search terms" in unattributed[0]["reason"]
    assert unattributed[0]["member_run_ids"] == ["r2"]


# --- the warrant, and the whole hypothesis ------------------------------------
def test_an_attribution_quotes_the_registry_sentence_that_justified_it(registry):
    """`basis: "registry_declared"` claims the registry declares this component fails this way. The
    sentence it declared has to travel with the claim, or the basis is a label nothing can check.

    Guaranteed non-empty for a declared basis, and pinned in that direction: `match` reaches
    `registry_declared` only via a word-boundary hit INSIDE `known_failure_modes`, so a declared hit
    with nothing to quote is unreachable. The pairing is what makes an empty value elsewhere legible.
    """
    best = attribute(_signature("session_ended_on_unretrieved_dispatch"), registry)[0]
    assert best.basis == "registry_declared"
    assert best.matched_failure_mode == (
        "never tells the model to read_result after starting a job")
    component = registry.get(best.component_id)
    assert best.matched_failure_mode == component.declared_failure_mode
    assert component.declared_failure_mode in component.known_failure_modes


def test_a_keyword_inferred_attribution_quotes_nothing_because_there_was_nothing_to_quote(registry):
    """`chainer-readiness-rule` declares no failure modes at all; the match came from its id. So the
    empty sentence is not a dropped measurement — it is what `keyword_inferred` MEANS, and the basis
    beside it is the field that says so. 37 of the 80 real components are in this state."""
    best = attribute(_signature("fanout_parent_ran_early"), registry)[0]
    assert best.component_id == "chainer-readiness-rule"
    assert best.basis == "keyword_inferred"
    assert best.matched_failure_mode == ""
    assert registry.get(best.component_id).known_failure_modes == ""


def test_a_proposal_carries_the_whole_gate_triple_and_not_only_the_numerator(registry):
    """`controls_excluded` alone reached the Proposal, so a suggestion built from it could say how
    many controls its shape survived and never how many it was offered — and "3 of 3" and "3 of 12"
    are the difference between a gate that decided its evidence and one that could not place most of
    it. All three now cross the boundary together, and they still close."""
    patterns = [{"shape": ("fanout_parent_ran_early",), "symptom": ("tier_failed:x",),
                 "member_run_ids": ["r1", "r2"], "evidence_by_run": {"r1": {}},
                 "heuristic_facets": (), "controls_excluded": 3,
                 "controls_inconclusive": ("blind-1",), "controls_considered": 4,
                 "equally_discriminative": ("has_fanout_node",)}]
    proposals, _ = propose_all(patterns, registry)
    signature = proposals[0].signature
    assert signature.controls_excluded == 3
    assert signature.controls_inconclusive == ("blind-1",)
    assert signature.controls_considered == 4
    assert (signature.controls_excluded + len(signature.controls_inconclusive)
            == signature.controls_considered)
    assert signature.equally_discriminative == ("has_fanout_node",)


def test_an_unattributed_pattern_names_the_symptom_it_predicts(registry):
    """A signature is a (shape -> symptom) hypothesis and this record published the antecedent alone,
    so two unattributed patterns with the same topology but different failing checks read as one
    repeated finding. Symptom facets are named after the evaluators that produced them, so they are
    harness identifiers and safe on the shared item."""
    patterns = [
        {"shape": ("some_unmapped_facet",), "symptom": ("check_failed:DagCompletionValidator",),
         "member_run_ids": ["r1"], "evidence_by_run": {"r1": {}}, "heuristic_facets": ()},
        {"shape": ("some_unmapped_facet",), "symptom": ("tier_failed:results_delivered",),
         "member_run_ids": ["r2"], "evidence_by_run": {"r2": {}}, "heuristic_facets": ()},
    ]
    _, unattributed = propose_all(patterns, registry)
    assert [u["symptom"] for u in unattributed] == [
        ["check_failed:DagCompletionValidator"], ["tier_failed:results_delivered"]]
    # The two records are distinguishable, which is the whole point of carrying it.
    assert unattributed[0] != unattributed[1]
