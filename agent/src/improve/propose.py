"""PROPOSE — turn an admitted signature into a Proposal with several candidates.

Two steps, in this order, because the second depends on the first:

  ATTRIBUTE  which Σ component owns this failure? Answered by matching the signature's
             facets against the registry's ``known_failure_modes``, so the blame is
             something the registry already asserts about itself rather than something
             this module infers. A match on id/type/notes alone is kept but marked
             ``keyword_inferred`` and ranked below.

  GENERATE   what could be done about it? The component's ``disposition`` decides which
             handler runs, and therefore what the proposal is allowed to be: a
             publishable edit, an advisory note, or an engineering escalation. A LOCKED
             component cannot yield a publishable proposal no matter how good the
             remedy — that is the permission matrix doing its job.

REQ-021 wants several candidates so acceptance is a selection rather than a yes/no.
When a target genuinely admits only one, ``single_candidate_forced`` records why instead
of padding the list with a variant nobody would pick.
"""
from __future__ import annotations

import difflib
import hashlib
from typing import Callable, Optional

try:
    from .contract import Attribution, Candidate, FailureSignature, Proposal, pluralise
    from .registry import PUBLISHABLE_TARGET_TYPES, Component, SigmaRegistry
except ImportError:  # pragma: no cover
    from improve.contract import (  # type: ignore
        Attribution, Candidate, FailureSignature, Proposal, pluralise)
    from improve.registry import PUBLISHABLE_TARGET_TYPES, Component, SigmaRegistry  # type: ignore


CONST_MIN_CANDIDATES = 2

# Facet -> weighted registry search terms. Explicit rather than derived from the facet
# name, because the registry's vocabulary ("readiness", "settle", "partial success") is
# not the facet vocabulary.
#
# Weights encode how diagnostic a term is. "readiness" and "chainer" name one subsystem;
# "depend", "order" and "plan" appear across half the registry, so at weight 1.0 they act
# as tie-breakers rather than as evidence. Giving them equal weight attributed a DAG
# ordering failure to `capability-image-artifact`, whose declared failure mode merely
# contains the word "independently".
_FACET_KEYWORDS: dict = {
    "fanout_parent_ran_early": {
        "readiness": 6.0, "chainer": 5.0, "settle": 3.0, "depends_on": 3.0, "order": 1.0},
    "edge_order_violation": {
        "readiness": 6.0, "chainer": 5.0, "depends_on": 3.0, "order": 1.0, "chain": 1.0},
    "terminal_node_without_gateway_status": {
        "partial success": 6.0, "settle": 5.0, "gateway_status": 4.0, "binary": 2.0},
    "gateway_reported_failure": {"settle": 5.0, "gateway_status": 4.0, "gateway": 2.0},
    "has_failed_node": {"retry": 5.0, "fail-fast": 4.0, "settle": 3.0, "error": 1.0},
    # `session_ended_on_unretrieved_dispatch` inherits the retired `dispatch_without_retrieve`
    # terms because it is the facet that now carries the claim those terms answer: a result
    # that was not fetched. Its sibling `dispatch_unpaired_result_unobserved` is deliberately NOT
    # mapped — it asserts only that spans cannot tell an unfetched result from one returned
    # inline, so attributing it to a Σ component and drafting a remedy would be the same
    # confident-but-unsupported move this facet pair exists to stop. A pattern whose shape
    # holds only that facet therefore lands in `unattributed` (see `propose_all`), which is
    # the honest outcome, not a gap.
    "session_ended_on_unretrieved_dispatch": {
        "read_result": 6.0, "retrieve": 5.0, "poll": 4.0, "result files": 4.0},
    "has_non_terminal_node": {"ttl": 5.0, "stuck": 5.0, "pending": 3.0, "readiness": 3.0},
    "deep_plan": {"chainer": 3.0, "depends_on": 2.0, "chain": 1.0},
    "has_fanout_node": {"fanout": 5.0, "fan-out": 5.0, "chainer": 2.0},
    "wide_plan": {"ceiling": 5.0, "budget": 4.0, "unbounded": 3.0},
    "chained_plan": {"chainer": 3.0, "depends_on": 2.0},
}

# Plain-English names for the shapes, used in user-facing summaries. The Inbox must not
# say "fanout_parent_ran_early" to a media editor.
#
# EVERY facet `signature.shape_facets` emits needs an entry, not just the ones a proposal can
# act on. `prose_for` falls back to `name.replace("_", " ")`, and that fallback reaches the
# reader verbatim inside `evidence_summary` — "from 1 run(s) where plan absent" is a variable
# name with its underscore removed, shown to a media editor. Which facet won the greedy
# refinement decides which sentence appears, so an unmapped one is not a rare corner: it is a
# coin flip on whether a card reads as English. A test pins the whole set for that reason.
#
# Three registers here, and mixing them is the way this table goes wrong:
#   * a behaviour that cost the user something ("a step failed"),
#   * a neutral structural observation, which must not imply fault,
#   * an evidence gap — WE could not see it — which must not read as the agent misbehaving.
_FACET_PROSE: dict = {
    "fanout_parent_ran_early":
        "a step that several later steps rely on was recorded finished before the step "
        "it depends on, so those later steps used unfinished work",
    "edge_order_violation":
        "a step was recorded finished before the step it depends on",
    "terminal_node_without_gateway_status":
        "a step was marked done without recording whether its job actually succeeded",
    "has_failed_node": "a step failed",
    # The retired `dispatch_without_retrieve` said "work was started but its result was never
    # fetched" for ANY run that did not call the retrieve API — including runs whose results
    # came back inline and needed no fetch. These two say only what each one establishes.
    "session_ended_on_unretrieved_dispatch":
        "the session finished without fetching the result of the last job it started, "
        "although it had fetched results for earlier jobs in the same session",
    "dispatch_unpaired_result_unobserved":
        "a job was started with no matching result fetch recorded; the result may have come "
        "back directly instead, which the recorded evidence cannot tell apart",
    "has_fanout_node": "one step's output feeds several later steps",
    "wide_plan": "the request was split into four or more steps",
    "deep_plan": "the steps form a chain three or more deep",
    "chained_plan": "the steps depend on each other",
    "all_task_nodes_terminal": "every step reported success",

    # --- neutral structure. True of healthy runs too, so no sentence here may imply fault. ---
    # `all_task_nodes_terminal` above sets the precedent: a shape facet that describes a run
    # rather than accusing it still needs prose, because the reader sees it either way.
    "multi_node_plan": "the request was split into more than one step",
    # The negation of `chained_plan`: no node declared a dependency on another.
    "flat_plan": "the steps did not depend on each other",
    # Records only that a dispatch call was made. It is NOT a defect — and it is unmapped in
    # `_FACET_KEYWORDS` for that reason, so it never draws a remedy — but it still reaches a
    # reader when it lands in a shape beside a facet that is one.
    "async_dispatched": "work was handed off to run as a background job",

    # --- something went wrong ---
    # A node that reached neither success nor failure: no end state was ever written for it.
    "has_non_terminal_node": "a step never finished, neither succeeding nor failing",
    # The async job's own reported outcome, which is a different field from the step's status
    # row — a step can read done while its job reported FAILED. The trailing clause is what
    # keeps this from reading as a duplicate of `has_failed_node` when a shape carries both.
    "gateway_reported_failure":
        "a step's job reported that it failed, whatever the step's own status said",

    # --- evidence gaps. WE could not see this; the run is not accused of anything. ---
    # These three are what `mine` routes to the defect register instead of the proposer, so
    # the sentence has to describe a blind spot in our own recording. Phrasing any of them as
    # a behaviour would make an observability hole look like a failure the reader caused, and
    # would put the blame in a card whose whole purpose is to say where to look next.
    "plan_absent":
        "the steps this request was broken into were never recorded, so its plan could not "
        "be examined",
    # No spans at all — a different fact from a trajectory the harvest is known to have
    # clipped, and a different remedy, which is why they are separate facets.
    "trajectory_absent":
        "none of the actions taken while working on this request were recorded, so what "
        "actually happened could not be examined",
    # The plan existed and its node list was offloaded and not read back, so the dependency
    # order is unknown. Distinct from `flat_plan`, which is a plan we DID read and that had
    # no edges — the point of the facet is that those two must not be confused.
    "plan_edges_unreadable":
        "the plan's list of steps could not be read back, so which steps depended on which "
        "is unknown",
}


def prose_for(shape) -> str:
    parts = [_FACET_PROSE.get(name, name.replace("_", " ")) for name in shape]
    return "; ".join(parts)


# The longest a headline may be, in words.
#
# Eight is where a phrase acquires a subordinate clause and stops being scannable. The line this
# leads is `evidence_summary`, whose facet clause runs to thirty words on the live bundle and was
# the ONLY text distinguishing one card from the next — the card's other line names the component
# and its permission, which is the same sentence on every locked card. So the reader's first line
# has to be readable at a glance or the two cards read as duplicates again.
#
# A bound with a test behind it rather than a guideline, because a table of careful sentences grows
# one careful sentence at a time: every entry below was written short, and nothing but a failing
# test stops the next qualifier from being appended to one of them.
_MAX_HEADLINE_WORDS = 8


# Facet -> the SHORT lead line for a card, at most `_MAX_HEADLINE_WORDS` words.
#
# The counterpart to `_FACET_PROSE` above, not a replacement for it: that table is the
# EXPLANATION and is long on purpose, this one is the headline a reader sees first. Both are
# emitted, so a card can lead with the short form and keep the careful one underneath.
#
# Three rules. The second and third are where short prose on this wire has gone wrong before —
# `_FACET_PROSE`'s retired `dispatch_without_retrieve` note and `dto._WASTE_KIND_PROSE`'s are both
# records of it — and the first is what keeps this line from restating the ones beside it:
#
#   * IT SAYS WHAT HAPPENED. Not what it means, not what to do, not how bad it is. Blame,
#     remedy and severity all belong to other fields on the card (`summary`, `candidates`,
#     `blastRadius`), and a headline that anticipated them would be a second opinion beside them.
#   * IT STAYS INSIDE THE FACET. Brevity must not promote a careful statement into a false one —
#     so `recorded finished` keeps its `recorded`, `handed off` does not become `ran`, and the
#     evidence-gap three still say `recorded` rather than accusing the run of anything. Where a
#     qualifier is what makes the long form true, a word is spent on it here too.
#   * IT IS A CONSTANT. Never interpolated, for `_WASTE_KIND_PROSE`'s reason (`dto.py`): these
#     ride to ONE DynamoDB item that every actor reads, and a facet name is a harness literal
#     while anything pulled out of a session is not. Generated in the producer so a browser
#     cannot assemble one and drift from what the facet measured.
#
# Sentence-cased and unpunctuated at the end, unlike `_FACET_PROSE`'s lower-case fragments: those
# are embedded mid-sentence after "from 1 run where", and these stand alone as a line.
#
# EVERY facet `signature.shape_facets` emits has an entry, pinned by a test that derives the set
# from that function. An unmapped one yields NO headline (`headline_for`) rather than a machine
# name — `dto._WASTE_KIND_PROSE`'s rule, and the opposite of `prose_for`'s fallback.
_FACET_HEADLINE: dict = {
    # Both ordering facets keep `recorded`: the observation is a pair of recorded completion
    # timestamps, not the work itself, and "finished before" without it claims something about
    # the work that the timestamps cannot establish. `relied-on` is the whole difference between
    # this facet and the next — the early step is itself feeding two or more later ones — so it
    # is what the short form spends its distinguishing word on.
    "fanout_parent_ran_early": "A relied-on step was recorded finished early",
    "edge_order_violation": "A step was recorded finished before its dependency",
    "terminal_node_without_gateway_status": "A step was marked done without an outcome",
    "has_failed_node": "A step failed",
    # The claim, and the one beside it that deliberately makes none. This pair is the reason the
    # retired `dispatch_without_retrieve` was split (see `_FACET_PROSE`), and a short form that
    # rendered them alike would undo that split on the surface where it matters most. So the
    # first names the LAST job and says its result was never fetched, which is what the facet's
    # in-session-retrieve precondition licenses; the second speaks only about the RECORD.
    "session_ended_on_unretrieved_dispatch": "The last job's result was never fetched",
    "dispatch_unpaired_result_unobserved": "A job's result fetch was not recorded",
    "has_fanout_node": "One step's output feeds several later steps",
    "wide_plan": "The plan had four or more steps",
    # `The plan was three steps deep` would read as a plan of three steps. The measurement is the
    # longest dependency CHAIN, so the short form keeps the chaining verb the long one uses.
    "deep_plan": "The steps chain three or more deep",
    "chained_plan": "The steps depend on each other",
    "all_task_nodes_terminal": "Every step reported success",

    # --- neutral structure. True of healthy runs too, so nothing here may imply fault. ---
    "multi_node_plan": "The plan had more than one step",
    "flat_plan": "The steps did not depend on each other",
    # `handed off`, never `ran`: the facet records that a dispatch CALL was made and nothing about
    # what the background job then did.
    "async_dispatched": "Work was handed off as a background job",

    # --- something went wrong ---
    "has_non_terminal_node": "A step never finished",
    # `job reported` is what keeps this off `has_failed_node`'s line: the async job's own outcome
    # is a different field from the step's status row, and a shape can carry both facets at once.
    "gateway_reported_failure": "A step's job reported failure",

    # --- evidence gaps. WE could not see this; the run is not accused of anything. ---
    # Each of the three says `recorded` or `read`, so it describes our own records and not the
    # run's behaviour. Dropping that verb is all it would take to turn an observability hole into
    # an accusation on the card whose purpose is to say where to look next.
    "plan_absent": "The plan was never recorded",
    "trajectory_absent": "No actions were recorded for this request",
    # Distinct from `plan_absent`: this plan WAS recorded, and its step list could not be read
    # back. Two different gaps with two different remedies, so two different lines.
    "plan_edges_unreadable": "The plan's step list could not be read",
}


def headline_for(shape) -> str:
    """The short lead line for a shape, or ``""`` when it has none.

    Reads the FIRST facet and only the first. That is the facet the greedy refinement picked
    before any other (`mine._refine`) and the one `dto._pattern_to_wire` already publishes as
    ``patternId``, so the headline and the id the card is keyed on always name one observation.
    The rest of the conjunction keeps its place in `prose_for`'s sentence underneath — a headline
    joining several would be back over the bound this table exists to hold.

    No fallback, deliberately, and this is where it parts company with `prose_for`: an unmapped
    facet yields the empty string so a caller can OMIT the field, rather than putting
    ``plan edges unreadable`` in front of a reader. Returning "" rather than raising because a
    missing headline must degrade to the sentence below it and never take a bundle down.
    """
    leading = next(iter(shape or ()), None)
    return _FACET_HEADLINE.get(leading, "") if leading else ""


# Σ layer + component -> the Lab category a proposal would actually target. Only the three
# versioned Lab categories are publishable; everything else names its layer so a consumer
# cannot mistake a Σ layer for a Lab category and try to publish against it.
#
# This replaces a line that read `comp.layer if comp.layer not in PUBLISHABLE_TARGET_TYPES
# else comp.layer` — both branches identical, and the condition unsatisfiable anyway since
# Σ layers ('prompt', 'control', …) and Lab categories ('skill', 'specialty-agent', …) are
# disjoint sets. So `PUBLISHABLE_TARGET_TYPES` had no runtime effect at all.
_COMPONENT_TARGET_TYPE: dict = {
    "custom-agent-persona": "specialty-agent",
    "specialty-agent-persona": "specialty-agent",
    "lab-user-skill-bodies": "skill",
    "lab-workflow-definition": "workflow",
}


def _target_type(comp: Component) -> str:
    """The Lab category this proposal targets, or the Σ layer when it is not a Lab asset."""
    mapped = _COMPONENT_TARGET_TYPE.get(comp.component_id)
    if mapped and comp.disposition == "publishable":
        return mapped
    if comp.disposition == "code_change":
        return "code_change"
    return comp.layer


def _candidate_id(proposal_seed: str, *parts: str) -> str:
    """Stable id over everything that makes the candidate distinct.

    It used to hash only the label, and labels are truncated to 70 characters for display —
    so the `fanout_parent_ran_early` and `edge_order_violation` additions, which share their
    first 70 characters, produced two candidates with the SAME id and different `after`
    text. A fan-out parent running early implies an ordering violation, so a shape carrying
    both is the normal case, not a corner. Hash the full remedy text too.
    """
    return hashlib.sha256("|".join((proposal_seed, *parts)).encode()).hexdigest()[:12]


def unified_diff(before: str, after: str, label: str = "current") -> str:
    """Server-side unified diff. Computed here, not in the browser: no JS diff library
    is installed, and a diff produced next to the proposal is the same diff the reviewer
    later publishes."""
    if before == after:
        return ""
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=label, tofile=f"{label} (proposed)", n=3,
    ))


# ---------------------------------------------------------------------------
# attribution
# ---------------------------------------------------------------------------
def attribute(signature: FailureSignature, registry: SigmaRegistry,
              limit: int = 3) -> list[Attribution]:
    """Rank Σ components that plausibly own this signature.

    Terms from several facets are merged by taking the MAX weight per term, not the sum:
    a term appearing in two facets of the same signature is not twice as diagnostic, and
    summing would let a broad signature drown out a specific one.
    """
    weights: dict = {}
    for facet in signature.shape:
        for term, weight in (_FACET_KEYWORDS.get(facet) or {}).items():
            weights[term] = max(weights.get(term, 0.0), weight)
    if not weights:
        return []
    seen: set = set()
    out: list[Attribution] = []
    for comp, basis, matched in registry.match(weights):
        if comp.component_id in seen:
            continue
        seen.add(comp.component_id)
        out.append(Attribution(
            component_id=comp.component_id, layer=comp.layer, mutability=comp.mutability,
            versioned=comp.versioned, blast_radius=comp.blast_radius,
            source_location=comp.source_location, matched_failure_mode=matched, basis=basis,
        ))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# candidate generation, dispatched on disposition
# ---------------------------------------------------------------------------
def _code_change_candidates(seed: str, signature: FailureSignature,
                            comp: Component) -> list[Candidate]:
    """Remedies for a component that lives in platform code. Two genuinely different
    engineering fixes, not one fix phrased twice — the choice between "refuse to
    proceed" and "record the truth" is a real design decision for a human to make."""
    shape = set(signature.shape)
    if "fanout_parent_ran_early" in shape or "edge_order_violation" in shape:
        return [
            Candidate(
                candidate_id=_candidate_id(seed, "gate-on-dependency-completion"),
                label="Hold a step until every step it depends on is recorded finished",
                claim={"utilityTerm": "results_delivered", "direction": "increase",
                       "magnitude": None, "onPattern": ",".join(signature.shape)},
                risk="Adds latency, and a dependency whose completion is never recorded "
                     "would stall instead of proceeding on unfinished input.",
                disposition="code_change",
            ),
            Candidate(
                candidate_id=_candidate_id(seed, "record-partial-completion"),
                label="Record a step's real outcome instead of collapsing it to done/failed",
                claim={"utilityTerm": "results_delivered", "direction": "increase",
                       "magnitude": None, "onPattern": ",".join(signature.shape)},
                risk="Does not prevent the early read; it makes it visible, so the "
                     "delivery failure becomes detectable rather than silent.",
                disposition="code_change",
            ),
        ]
    return [
        Candidate(
            candidate_id=_candidate_id(seed, "escalate"),
            label=f"Investigate {comp.component_id} for: {prose_for(signature.shape)}",
            claim={"utilityTerm": "results_delivered", "direction": "increase",
                   "magnitude": None, "onPattern": ",".join(signature.shape)},
            disposition="code_change",
        ),
    ]


def _advisory_candidates(seed: str, signature: FailureSignature,
                         comp: Component) -> list[Candidate]:
    return [
        Candidate(
            candidate_id=_candidate_id(seed, "advisory"),
            label=(f"{comp.component_id} is implicated, but no change can be proposed "
                   + (f"(permission: {comp.mutability or 'not recorded'})"
                      if not comp.editable else
                      "— it is editable, but no remedy template matches this failure shape")),
            claim={"utilityTerm": "results_delivered", "direction": "increase",
                   "magnitude": None, "onPattern": ",".join(signature.shape)},
            disposition="advisory",
        ),
    ]


def _publishable_candidates(seed: str, signature: FailureSignature, comp: Component,
                            current_text: Optional[str]) -> list[Candidate]:
    """Edits to a versioned, user-editable asset.

    Without the asset's current text there is nothing to diff, so this degrades to
    advisory rather than inventing a `before` — a diff against invented text would look
    exactly like a real one in the UI.
    """
    # `is None`, not falsiness: an asset whose real text is "" must be distinguishable from
    # one we could not read, and `_Sentinel.__bool__` is False so a NO_DATA would otherwise
    # be swallowed by this branch and blamed on availability.
    if current_text is None or not isinstance(current_text, str):
        return [
            Candidate(
                candidate_id=_candidate_id(seed, "needs-current-text"),
                label=f"Edit {comp.component_id}, but its current text was not "
                      "available to diff against",
                claim={"utilityTerm": "results_delivered", "direction": "increase",
                       "magnitude": None, "onPattern": ",".join(signature.shape)},
                disposition="advisory",
            ),
        ]
    additions = {
        "fanout_parent_ran_early":
            "Before using an earlier step's output, confirm that step finished "
            "successfully; if it has not, wait or report that it is still running.",
        "edge_order_violation":
            "Before using an earlier step's output, confirm that step finished "
            "successfully.",
        # Only the facet that makes a claim gets a remedy. `dispatch_unpaired_result_unobserved` is
        # absent on purpose: drafting an edit against an ambiguity the evidence cannot resolve
        # would put a confident instruction in front of a user on no grounds.
        "session_ended_on_unretrieved_dispatch":
            "Before finishing, fetch the result of every job you started in this session, "
            "including the last one.",
    }
    out: list[Candidate] = []
    for facet in signature.shape:
        addition = additions.get(facet)
        if not addition:
            continue
        after = current_text.rstrip() + "\n\n" + addition + "\n"
        label = f"Add: {addition.split(';')[0][:70]}"
        out.append(Candidate(
            candidate_id=_candidate_id(seed, facet, addition), label=label,
            before=current_text, after=after,
            diff_text=unified_diff(current_text, after, comp.component_id),
            claim={"utilityTerm": "results_delivered", "direction": "increase",
                   "magnitude": None, "onPattern": facet},
            disposition="publishable",
        ))
    return out


_HANDLERS: dict = {
    "code_change": _code_change_candidates,
    "advisory": _advisory_candidates,
}


def propose(signature: FailureSignature, registry: SigmaRegistry,
            current_text_for: Optional[Callable[[str], Optional[str]]] = None,
            member_run_ids: Optional[list] = None) -> Optional[Proposal]:
    """Build a Proposal for one admitted signature, or None if nothing owns it."""
    attributions = attribute(signature, registry)
    if not attributions:
        return None
    best = attributions[0]
    comp = registry.get(best.component_id)
    if comp is None:  # pragma: no cover - attribute() only returns known ids
        return None

    members = member_run_ids or [signature.run_id]
    seed = f"{comp.component_id}|{','.join(signature.shape)}"
    proposal_id = hashlib.sha256(seed.encode()).hexdigest()[:12]

    disposition = comp.disposition
    if disposition == "publishable":
        text = current_text_for(comp.component_id) if current_text_for else None
        candidates = _publishable_candidates(seed, signature, comp, text)
        if not candidates:
            candidates = _advisory_candidates(seed, signature, comp)
    else:
        candidates = _HANDLERS[disposition](seed, signature, comp)

    # REQ-021 wants several candidates. When only one exists, record the REAL reason —
    # blaming mutability for what is actually a missing remedy template, or an unreadable
    # asset, produced a message that contradicted the proposal beside it.
    forced = None
    if len(candidates) < CONST_MIN_CANDIDATES:
        only = candidates[0].disposition if candidates else disposition
        if disposition == "publishable" and only == "advisory":
            forced = (f"{comp.component_id} is publishable, but its current text could not "
                      "be read, so there is nothing to diff a proposed version against")
        elif only == "advisory":
            forced = (f"no applyable remedy exists for {comp.component_id}: permission is "
                      f"{comp.mutability or 'not recorded'} and it "
                      f"{'has' if comp.versioned else 'has no'} version lineage")
        else:
            forced = ("no remedy template matches this failure shape "
                      f"({', '.join(signature.shape)}), so only a generic escalation "
                      "could be generated")

    return Proposal(
        proposal_id=proposal_id,
        target_type=_target_type(comp),
        target_id=comp.component_id,
        target_name=comp.component_id.replace("-", " "),
        operation="edit",
        summary=_summary(signature, candidates),
        # A finished sentence, not a template: `evidenceSummary` reaches the reader verbatim, so
        # the noun has to agree here. A single-run pattern is the COMMON case in a single-digit
        # population, which is where "from 1 run(s)" was visible on the surface.
        evidence_summary=(f"from {len(members)} {pluralise(len(members), 'run')} where "
                          f"{prose_for(signature.shape)}"),
        # The lead line for the sentence above, which keeps its text and its meaning. Carries no
        # count: the figure belongs to the sentence that has a denominator to put it in.
        evidence_headline=headline_for(signature.shape),
        signature=signature,
        attribution=best,
        candidates=candidates,
        base_version=None,
        blast_radius={"class": comp.blast_radius or "unknown", "instanceCount": len(members)},
        single_candidate_forced=forced,
        provenance={
            "attribution_basis": best.basis,
            "attribution_alternatives": [a.component_id for a in attributions[1:]],
            "registry_revalidated": registry.meta.get("revalidated"),
            "registry_head_sha": registry.meta.get("head_sha"),
            "heuristic_facets": list(signature.heuristic_facets),
        },
    )


def _summary(signature: FailureSignature, candidates: list[Candidate]) -> str:
    if candidates and candidates[0].label:
        return candidates[0].label
    return f"Address: {prose_for(signature.shape)}"


def propose_all(patterns: list[dict], registry: SigmaRegistry,
                current_text_for: Optional[Callable[[str], Optional[str]]] = None,
                ) -> tuple[list[Proposal], list[dict]]:
    """One Proposal per grouped pattern (see ``mine.group_signatures``).

    Returns ``(proposals, unattributed)``. A pattern that no Σ component owns used to be
    dropped silently, which contradicts the principle residue exists for: an admitted
    pattern that produced nothing must stay visible, with a reason. It happens whenever no
    facet in the shape has a `_FACET_KEYWORDS` entry — `flat_plan`, `multi_node_plan`,
    `all_task_nodes_terminal` and `async_dispatched` are all admissible and unmapped, and so
    is `dispatch_unpaired_result_unobserved`, which is unmapped DELIBERATELY (see `_FACET_KEYWORDS`)
    rather than for want of terms.
    """
    out: list[Proposal] = []
    unattributed: list[dict] = []
    for pattern in patterns:
        members = pattern["member_run_ids"]
        # Evidence is now per-run; a proposal shows the first member's, and the rest stay
        # reachable on the pattern.
        evidence_by_run = pattern.get("evidence_by_run") or {}
        signature = FailureSignature(
            run_id=members[0],
            symptom=pattern["symptom"], shape=pattern["shape"], pass_hits=0,
            evidence=evidence_by_run.get(members[0]) or pattern.get("evidence") or {},
            heuristic_facets=tuple(pattern.get("heuristic_facets") or ()),
            # The whole gate triple, not just the numerator. `controls_excluded` alone was carried
            # here, so a Proposal — and therefore the wire suggestion built from it — could state
            # how many controls the shape survived and never how many it was offered.
            controls_excluded=int(pattern.get("controls_excluded") or 0),
            controls_inconclusive=tuple(pattern.get("controls_inconclusive") or ()),
            controls_considered=int(pattern.get("controls_considered") or 0),
            equally_discriminative=tuple(pattern.get("equally_discriminative") or ()),
        )
        proposal = propose(signature, registry, current_text_for, member_run_ids=members)
        if proposal is not None:
            out.append(proposal)
        else:
            unmapped = [f for f in pattern["shape"] if not _FACET_KEYWORDS.get(f)]
            unavailable = bool(registry.meta.get("unavailable"))
            unattributed.append({
                "shape": list(pattern["shape"]),
                # The CONSEQUENT of the hypothesis, carried for the same reason the shape is. A
                # signature is a (shape -> symptom) claim, and this record used to publish the
                # antecedent alone: two unattributed patterns with the same topology but different
                # failing checks read as one repeated finding. Symptom facets are named after the
                # EVALUATORS that produced them (`signature`'s `tier_failed:*` / `check_failed:*`),
                # so they are harness identifiers and safe on the shared item.
                "symptom": list(pattern.get("symptom") or ()),
                "member_run_ids": list(members),
                # WHICH of the three causes, as a label. The `reason` below is the admin
                # wording and stays exactly as it was; the label exists so a plain-English
                # rendering for a user-facing surface can be a translation of an enum rather
                # than a second opinion assembled by parsing this sentence. Classified HERE
                # because this is the only place that holds all three inputs (`_FACET_KEYWORDS`,
                # the attribution result, and the registry's availability) — a consumer
                # re-deriving it would be a second definition, free to drift from the reason
                # printed beside it. `dto._UNATTRIBUTED_PLAIN_REASON` maps the label to prose.
                # `registry_unavailable` DOMINATES: with no catalogue to search, "terms matched
                # no component" is an artifact of an empty registry rather than a finding.
                "cause": ("registry_unavailable" if unavailable
                          else "no_search_terms" if unmapped
                          else "no_matching_component"),
                "reason": (
                    "no Σ component owns this pattern: "
                    + (f"no registry search terms are defined for {unmapped}"
                       if unmapped else
                       "search terms are defined but matched no component")
                    + (" — the registry was unavailable" if unavailable else "")),
            })
    return out, unattributed
