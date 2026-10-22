"""Run OBSERVE -> EVALUATE -> MINE -> PROPOSE on the laptop and write the artifacts.

Two modes, and the difference matters:

``recorded`` (default) — verdict tiers come from ``golden.jsonl``'s recorded labels, so
the whole pipeline is offline, free, and byte-reproducible.

Be precise about what that does and does not cost. The SHAPE facets are judge-free: node
statuses, edges, timestamps and span names are all recorded fields. But the symptom facets
read the tier outcomes, and — more importantly — the PASS/FAIL partition of the population
is entirely judge-derived, and ``pass_hits`` is measured against the passing bucket. So a
relabel changes which runs act as negative controls and therefore which signatures survive
the discriminative gate. Recorded mode reproduces a previous judgement exactly; it does not
substitute for one. Each scenario's own ``label_provenance`` is carried into the output
rather than silently adopted.

``live`` — re-scores every run with the real Bedrock judge first. This is the discipline of
computing labels fresh rather than copying them, and is what should be run before any claim
about pass rates. It costs money and needs credentials.

Usage:
    cd agent && uv run python -m improve.mine_local
    cd agent && AWS_PROFILE=megs-staging uv run python -m improve.mine_local --mode live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from typing import Optional

try:
    from .dto import (
        components_to_wire, publishable_suggestion, published_suggestions_block,
        suggestion_members, to_wire,
    )
    from .contract import (
        EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict, _Sentinel,
        verdict_counts_by_class,
    )
    from .evaluate import DEFAULT_GATING, evaluate
    from .judge import DEFAULT_PASS_THRESHOLD
    from .loop import _make_judge, load_config, load_golden
    from .mine import group_signatures, mine
    from .observe.build_run import HarvestedPlanSource, build_run, harvest_usage_shortfall
    from .observe.population import harvest_population
    from .store import SuggestionStore
    from .observe.span_trajectory import HarvestedSpanSource
    from .propose import propose_all
    from .registry import SigmaRegistry
    from .waste import find_waste
except ImportError:  # pragma: no cover - direct-script fallback, mirrors the package
    from improve.dto import (  # type: ignore
        components_to_wire, publishable_suggestion, published_suggestions_block,
        suggestion_members, to_wire,
    )
    from improve.contract import (  # type: ignore
        EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict, _Sentinel,
        verdict_counts_by_class,
    )
    from improve.evaluate import DEFAULT_GATING, evaluate  # type: ignore
    from improve.judge import DEFAULT_PASS_THRESHOLD  # type: ignore
    from improve.loop import _make_judge, load_config, load_golden  # type: ignore
    from improve.mine import group_signatures, mine  # type: ignore
    from improve.observe.build_run import (  # type: ignore
        HarvestedPlanSource, build_run, harvest_usage_shortfall,
    )
    from improve.observe.population import harvest_population  # type: ignore
    from improve.store import SuggestionStore  # type: ignore
    from improve.observe.span_trajectory import HarvestedSpanSource  # type: ignore
    from improve.propose import propose_all  # type: ignore
    from improve.registry import SigmaRegistry  # type: ignore
    from improve.waste import find_waste  # type: ignore


def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", ".."))


def default_out_dir() -> str:
    return os.path.join(_repo_root(), "tests", "evaluation", "experiments")


def _tier_result(name: str, score, threshold: float) -> EvaluatorResult:
    """A tier result rebuilt from a recorded score.

    A missing score is NO_DATA, never a pass — the same rule the live judge follows, so
    an incomplete label cannot become a passing verdict by omission.
    """
    if score is None:
        return EvaluatorResult(name, "llm", Outcome.NO_DATA, None,
                               "no recorded score for this tier")
    outcome = Outcome.PASS if float(score) >= threshold else Outcome.FAIL
    return EvaluatorResult(name, "llm", outcome, float(score),
                           f"recorded label {score:.3f} vs threshold {threshold}")


def verdict_from_recorded(run: Run, threshold: float, gating: tuple,
                          evaluators: Optional[list] = None) -> Verdict:
    """Assemble a Verdict from the scenario's recorded tiers plus freshly-run code
    evaluators.

    Both the evaluator/gating choice and the fold are delegated to ``evaluate`` (
    ``resolve_plan`` and ``gate``, the same functions the live path uses) so the two modes
    cannot disagree about what a verdict means or which checks apply. A hand copy of either
    had already drifted once.

    ``gating`` and ``evaluators`` are the CONVERSATIONAL-class overrides, matching the config
    keys they come from; a non-conversational run is routed by its class and ignores them,
    because recorded tier labels only exist for runs that had a goal in the first place.
    """
    from .evaluate import _code_evaluator, _utility, gate, resolve_plan

    expected = (run.scenario.expected if run.scenario else {}) or {}
    names, class_gating, workload = resolve_plan(
        run, {"evaluators": evaluators, "gating_evaluators": gating})
    results: list[EvaluatorResult] = []
    for name in names:
        if name == "goal_judge":
            # Stands in for the judge call, from the labels it previously produced.
            results.append(_tier_result("dispatch_correct",
                                        expected.get("dispatch_correct"), threshold))
            results.append(_tier_result("results_delivered",
                                        expected.get("results_delivered"), threshold))
        else:
            results.append(_code_evaluator(name, run))
    overall, reason = gate(results, class_gating)
    return Verdict(run.run_id, results, _utility(run, results, workload), overall, reason,
                   workload_class=workload.cls.value, workload_basis=workload.basis,
                   workload_evidence=workload.evidence, gating=tuple(class_gating),
                   workload_parent=workload.parent_hint or "")


def _jsonable(value):
    """Serialize dataclasses and sentinels for the artifact files.

    Sentinels become their literal names (``"NO_DATA"``), never null — a null would read
    as "field absent" in the UI and could be styled like any other empty value, which is
    exactly the confusion the sentinels exist to prevent.
    """
    if isinstance(value, _Sentinel):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (Outcome, Overall)):
        return value.value
    return value


def _is_int(value) -> bool:
    """A real recorded count, so the report can tell one from the ``"UNMEASURED"`` string.

    `_jsonable` flattens a sentinel to its NAME on the way into the artifact, which is right for the
    file and wrong for arithmetic: `f"{x} of {y}"` would happily print "UNMEASURED of UNMEASURED" as
    though it were a measurement of nothing.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _workload_breakdown(verdicts: list) -> dict:
    """The ARTIFACT's projection of the per-class verdict counts.

    The counting itself is ``contract.verdict_counts_by_class`` — one implementation, because
    ``dto._class_entry`` needs the same numbers in a different shape and a hand copy of this fold
    has already drifted once in this codebase (see ``evaluate.gate``). This function is only the
    projection: it decides how the artifact SPELLS those counts, and the artifact's spelling is
    load-bearing because the file is diffed run over run.

    ``gating`` is carried per class so the artifact records WHY a class's numbers mean what
    they do. Without it, `tool_discovery: 19 PASS` is indistinguishable from a class that was
    never checked. One gating set is the normal case and stays a FLAT list here; more than one is
    a fact about the population, so it is reported as a list of sets rather than collapsed. (The
    wire projection normalises that to a list-of-sets always, because a shape-shifting field is
    not something a TypeScript reader can consume — same numbers, different spelling, on purpose.)
    """
    out: dict = {}
    for cls, counts in verdict_counts_by_class(verdicts).items():
        gating_sets = counts["gating"]
        out[cls] = {
            **{k: v for k, v in counts.items() if k not in ("gating", "basis")},
            "gating": (list(gating_sets[0]) if len(gating_sets) == 1
                       else [list(g) for g in gating_sets]),
            "gating_is_mixed": len(gating_sets) > 1,
            "basis": counts["basis"],
        }
    return out


def run_pipeline(mode: str = "recorded", dataset: str = "golden",
                 config: Optional[dict] = None, profile: Optional[str] = None,
                 context: Optional[dict] = None, population: str = "golden",
                 limit: Optional[int] = None) -> dict:
    cfg = config or load_config()
    eval_cfg = cfg.get("evaluate", {}) or {}
    judge_cfg = cfg.get("judge", {}) or {}
    # Defaults imported, not re-typed: two literals for one policy is how the offline path
    # silently re-thresholds a dataset the config disagrees with.
    threshold = float(judge_cfg.get("pass_threshold", DEFAULT_PASS_THRESHOLD))
    gating = tuple(eval_cfg.get("gating_evaluators") or DEFAULT_GATING)
    evaluators = eval_cfg.get("evaluators")

    # `golden` is the labelled benchmark: fixed membership, recorded labels, the calibration
    # reference. `harvest` is every session with usable evidence, which is what mining needs and
    # what makes the discriminative gate mean something (a pattern surviving a few controls is a
    # plausibility check; surviving many is a test). A discovered session has no labels, so the
    # harvest population only makes sense with a live judge.
    # What discovery threw away, so `population_size` below cannot read as the whole harvest. `None`
    # for the golden benchmark, which has fixed membership and no cap — there is no discovery step to
    # report on, and zeros would claim one ran.
    discovery: Optional[dict] = None
    if population == "harvest":
        discovery = {}
        scenarios = harvest_population(limit=limit, stats=discovery)
        if mode != "live":
            raise ValueError(
                "population=harvest has no recorded labels, so it needs --mode live. "
                "Scoring is what produces the pass/fail split that pass_hits measures against.")
    else:
        scenarios = load_golden(dataset)
    span_source = HarvestedSpanSource()
    plan_source = HarvestedPlanSource()

    judge = None
    memory_reader = None
    if mode == "live":
        from .observe.memory_reader import DEFAULT_MEMORY_ID, MemoryReader
        profile = profile or os.environ.get("AWS_PROFILE")
        observe_cfg = cfg.get("observe", {}) or {}
        memory_reader = MemoryReader(
            memory_id=observe_cfg.get("memory_id", DEFAULT_MEMORY_ID), profile=profile)
        judge = _make_judge(cfg, profile)

    runs: list[Run] = []
    verdicts: list[Verdict] = []
    for scenario in scenarios:
        run = build_run(scenario, span_source=span_source, plan_source=plan_source,
                        memory_reader=memory_reader)
        runs.append(run)
        verdicts.append(evaluate(run, config=eval_cfg, judge=judge) if mode == "live"
                        else verdict_from_recorded(run, threshold, gating, evaluators))

    # `gating` is handed to mine so it can refuse shape facets that are definitional
    # subterms of a gating code evaluator — otherwise those facets entail their own symptom
    # and the discriminative gate becomes free.
    mined = mine(runs, verdicts, gating_evaluators=gating)
    patterns = group_signatures(mined.signatures)
    # The efficiency path, deliberately alongside `mine` rather than inside it: it takes `runs`
    # and NOT `verdicts`, because waste is orthogonal to pass/fail and makes no causal claim, so
    # it has no discriminative gate to earn and no judge to pay for (see `waste`'s docstring).
    waste = find_waste(runs)
    # `missing_ok`: the registry lives outside the repo, and losing already-computed mining
    # results to a FileNotFoundError would be the worst possible trade.
    registry = SigmaRegistry.load(missing_ok=True)
    proposals, unattributed = propose_all(patterns, registry)

    if context is not None:
        # Opt-in side channel for callers that need the objects behind the report: `--emit-wire`
        # computes evidenceQuality from the member runs' windows. Deliberately NOT in the return
        # value, which is the artifact and has to stay JSON-serializable.
        context["runs"] = runs
        context["registry"] = registry
        # `to_wire` needs the scores, the outcomes and the workload class to build the
        # `scorecard`. The artifact's own `verdicts` key is already `_jsonable`, i.e. lossy for
        # this purpose (enums flattened to strings), so the objects travel here instead.
        context["verdicts"] = verdicts
        # The reader, so a caller can ask it for the ACTOR ROSTER rather than sweeping Memory a second
        # time. `aws_lambda` needs it to write a measured zero for an actor with nothing in this
        # window, and the sweep it caches has already been paid for by the owner resolution above.
        # `None` in offline mode, which is correct: no Memory was consulted, so nothing can be named.
        context["memory_reader"] = memory_reader

    return {
        "mode": mode,
        "dataset": dataset,
        # `population` below is the pass/fail split; this is WHICH population.
        "population_source": population,
        "population_size": len(scenarios),
        # And this is what `population_size` is a sample OF. `None` when nothing sampled — see the
        # `discovery` comment above. `dto._discovered_sessions` projects it onto the wire.
        "population_discovery": discovery,
        # What the HARVEST measured and could not hand to the population at all: token spend whose
        # sessions the Insights row cap dropped, so they have no trajectory to score. Read from the
        # harvest rather than computed here, because the harvester is the only thing that can see
        # both sides of its own truncation (`harvest_live._usage_shortfall`). Every term is
        # UNMEASURED for a harvest that recorded no audit.
        "usage_shortfall": _jsonable(harvest_usage_shortfall()),
        "label_provenance": ("fresh judge run" if mode == "live"
                             else "recorded labels from golden.jsonl"),
        # Each scenario's own recorded provenance, so the artifact says which judge config
        # and threshold produced the labels it is standing on.
        "recorded_label_provenance": sorted({
            s.label_provenance or "(not recorded)" for s in scenarios
        }) if mode == "recorded" else [],
        "registry": {
            "available": not registry.meta.get("unavailable", False),
            "component_count": len(registry),
            "revalidated": registry.meta.get("revalidated"),
            "head_sha": registry.meta.get("head_sha"),
        },
        "population": {
            "total": len(verdicts),
            "failing": [v.run_id for v in verdicts if v.overall is Overall.FAIL],
            "passing": [v.run_id for v in verdicts if v.overall is Overall.PASS],
            "unscored": [v.run_id for v in verdicts if v.overall is Overall.UNSCORED],
            # How many of these runs the HARVEST knows it read incompletely, and how many it
            # answered either way for. Both numbers, because `incomplete: 0` with
            # `completeness_measured: 0` means the harvest predates the check — not that the
            # population is verified whole. Unlike the wire's version (`dto._evidence_completeness`)
            # this is a local artifact with no tenant boundary, so the ids are named: an operator
            # diffing two runs needs to know WHICH runs stopped being controls.
            "evidence_incomplete": sorted(r.run_id for r in runs
                                          if r.evidence_complete is False),
            "completeness_measured": sum(1 for r in runs
                                         if isinstance(r.evidence_complete, bool)),
        },
        # The pass/fail split above is only meaningful WITHIN a class, because each class is
        # gated on a different objective. Reported per class so a reader cannot compare a
        # conversational FAIL against a warm-up PASS as though they measured the same thing.
        "workloads": _workload_breakdown(verdicts),
        "verdicts": [_jsonable(v) for v in verdicts],
        "signatures": [_jsonable(s) for s in mined.signatures],
        "patterns": [_jsonable(p) for p in patterns],
        "residue": [_jsonable(r) for r in mined.residue],
        "evidence_gaps": [_jsonable(g) for g in mined.evidence_gaps],
        # Failures diverted because the platform was degraded over their window. Kept
        # distinct from evidence_gaps so a run producing zero signatures because the platform
        # was on fire does NOT look identical to one producing zero because the agent was
        # fine — that distinction is the whole point, so it must reach the artifact.
        "environment": [_jsonable(e) for e in mined.environment],
        "proposals": [_jsonable(p) for p in proposals],
        "unattributed": [_jsonable(u) for u in unattributed],
        # Efficiency findings over the WHOLE population, passing runs included. Kept out of
        # `signatures`/`residue` on purpose: those buckets are the discriminative miner's and
        # carry an attribution, while a waste finding is an observation with a cost and no owner.
        # `runs_examined` travels with the findings because every count here needs its denominator,
        # and `runs_not_scanned` because a run the search skipped must not read as a clean one.
        "waste": _jsonable(waste),
        "summary": {**mined.summary(), "proposals": len(proposals),
                    "unattributed": len(unattributed),
                    "waste_findings": len(waste.findings),
                    "runs_with_waste": len({f.run_id for f in waste.findings})},
    }


def _print_report(result: dict) -> None:
    pop = result["population"]
    print(f"mode={result['mode']}  labels={result['label_provenance']}")
    print(f"population: {pop['total']} runs  "
          f"{len(pop['failing'])} FAIL  {len(pop['passing'])} PASS  "
          f"{len(pop['unscored'])} UNSCORED")
    # What the line above is a SAMPLE of. Printed whenever discovery ran, zero drops included,
    # because "the cap did not bind today" is the fact that makes the count above the whole day.
    discovery = result.get("population_discovery")
    if discovery:
        print(f"            discovered {discovery['discoverable']} minable session(s) of "
              f"{discovery['harvested']} harvested ({discovery['below_min_spans']} under "
              f"min_spans={discovery['min_spans']}); the cap "
              f"(limit={discovery['limit']}) admitted {discovery['admitted']}"
              + ("" if discovery['admitted'] >= discovery['discoverable'] else
                 " — so every figure below is over "
                 f"{discovery['admitted']}/{discovery['discoverable']} of the minable day"))
    shortfall = result.get("usage_shortfall") or {}
    if _is_int(shortfall.get("tokens_discarded")):
        print(f"            harvest DISCARDED {shortfall['tokens_discarded']} of "
              f"{shortfall['tokens_measured']} measured token(s) across "
              f"{shortfall['sessions_discarded']} of {shortfall['sessions_measured']} session(s) "
              f"whose spans the row cap dropped — that spend is missing from every cost figure "
              f"below and cannot be scored")
    # Truncated evidence, said out loud. The whole reason this line exists is that the failure it
    # reports used to be silent: a harvest that dropped the newest third of a busy day still
    # produced a confident-looking report. `0 of 0 measured` is the honest reading of a harvest
    # taken before the check existed, and is printed rather than skipped for that reason.
    incomplete = pop.get("evidence_incomplete")
    if incomplete is not None:
        measured = pop.get("completeness_measured", 0)
        print(f"            evidence INCOMPLETE for {len(incomplete)} of {measured} run(s) whose "
              f"harvest completeness was measured"
              + (f": {[r[:8] for r in incomplete]} — their trajectory-derived facets are "
                 f"unobservable, so they cannot act as controls" if incomplete else ""))
    workloads = result.get("workloads") or {}
    if workloads:
        print()
        print("BY WORKLOAD CLASS (each gated on its own objective; do not compare across)")
        for cls, counts in sorted(workloads.items()):
            if counts.get("gating_is_mixed"):
                gated = "MIXED: " + " | ".join(", ".join(g) for g in counts["gating"])
            else:
                gated = ", ".join(counts["gating"]) or "(nothing)"
            print(f"  {cls:16} {counts['total']:3} runs   "
                  f"{counts['FAIL']:2} FAIL  {counts['PASS']:2} PASS  "
                  f"{counts['UNSCORED']:2} UNSCORED   gated on {gated}")
    print()
    print(f"ADMITTED SIGNATURES ({len(result['signatures'])})")
    for sig in result["signatures"]:
        print(f"  {sig['run_id'][:8]}  symptom={list(sig['symptom'])}")
        print(f"            shape={list(sig['shape'])}  pass_hits={sig['pass_hits']}"
              f"  controls_excluded={sig['controls_excluded']}")
        if sig.get('controls_inconclusive'):
            print("            controls NOT judgeable (their own evidence was missing): "
                  f"{[c[:8] for c in sig['controls_inconclusive']]}")
        if sig.get('equally_discriminative'):
            print(f"            EQUALLY discriminative: {list(sig['equally_discriminative'])}"
                  " — the data does not choose between these")
        if sig.get('heuristic_facets'):
            print(f"            rests on heuristic facet(s): {list(sig['heuristic_facets'])}")
        for line in sig["refinement_log"]:
            print(f"              . {line}")
    print()
    print(f"RESIDUE ({len(result['residue'])}) — failures shape could not explain")
    for entry in result["residue"]:
        print(f"  {entry['run_id'][:8]}  best_shape={list(entry['best_shape'])} "
              f"pass_hits={entry['pass_hits']}")
        # WHICH passing runs still matched, from `pass_hit_runs` — the one place that holds them.
        # `reason` states the count and not the ids, because it also has to be safe to publish;
        # this console has no tenant boundary, so it reads the field and names them.
        if entry.get("pass_hit_runs"):
            print(f"            also matched: {[h[:8] for h in entry['pass_hit_runs']]}")
        print(f"            {entry['reason']}")
    if result["evidence_gaps"]:
        print()
        print(f"EVIDENCE GAPS ({len(result['evidence_gaps'])}) — route to the defect register")
        for gap in result["evidence_gaps"]:
            print(f"  {gap['run_id'][:8]}  {gap['reason']}")
    if result.get("environment"):
        print()
        print(f"ENVIRONMENT ({len(result['environment'])}) — not the agent; route to ops")
        for entry in result["environment"]:
            print(f"  {entry['run_id'][:8]}  {entry['reason']}")
    waste = result.get("waste") or {}
    findings = waste.get("findings") or []
    if waste:
        print()
        # Printed even at zero, with its denominator, because "no waste found over 174 runs" and
        # "this artifact predates the efficiency path" are different facts.
        print(f"WASTE ({len(findings)} findings over "
              f"{len({f['run_id'] for f in findings})}/{waste.get('runs_examined', 0)} runs) — "
              "observations, NOT attributions; passing runs included")
        if waste.get("runs_not_scanned"):
            print(f"  NOT SCANNED for repeats (too many calls): "
                  f"{[r[:8] for r in waste['runs_not_scanned']]}")
        for finding in sorted(findings, key=lambda f: (f["kind"], -(f["calls"] or 0))):
            print(f"  {finding['run_id'][:8]}  {finding['kind']:20} "
                  f"{finding['calls']:3} call(s)  {finding['wall_ms']} ms  "
                  f"x{finding['occurrences']}  {finding['detail']}")
    print()
    if result.get("unattributed"):
        print(f"UNATTRIBUTED ({len(result['unattributed'])}) — admitted but nothing owns them")
        for entry in result["unattributed"]:
            print(f"  {list(entry['shape'])}: {entry['reason']}")
        print()
    print(f"PROPOSALS ({len(result['proposals'])})")
    for prop in result["proposals"]:
        attribution = prop["attribution"] or {}
        print(f"  [{prop['proposal_id']}] {prop['summary']}")
        print(f"      target      {prop['target_id']} ({prop['target_type']}, "
              f"mutability={attribution.get('mutability')}, basis={attribution.get('basis')})")
        print(f"      evidence    {prop['evidence_summary']}")
        print(f"      blast       {prop['blast_radius']}")
        for cand in prop["candidates"]:
            print(f"      - [{cand['disposition']}] {cand['label']}")
        if prop["single_candidate_forced"]:
            print(f"      ! single candidate: {prop['single_candidate_forced']}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    parser.add_argument("--dataset", default="golden")
    parser.add_argument("--out", default=None,
                        help="directory for mine_result.json (default: tests/evaluation/experiments)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--population", choices=("golden", "harvest"), default="golden",
        help="golden = the labelled benchmark (fixed membership, recorded labels). "
             "harvest = every session with usable evidence, scored live. Mining wants harvest; "
             "calibration wants golden.")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the discovered population, newest sessions first.")
    parser.add_argument(
        "--emit-wire", metavar="PATH",
        help="also write the UI wire payload (ImproveData) to PATH. This is a scratch bundle "
             "that `improve.seed_snapshot` uploads into the DynamoDB Improve store as the "
             "global SNAPSHOT (e.g. tests/evaluation/experiments/improve-wire.json). Do NOT "
             "write it under ui/public/: that directory is copied verbatim into the SPA bucket, "
             "so a bundle there ships as a public, unauthenticated copy of mined data — the "
             "surface now reads the bundle through the authenticated /improve/data proxy route.")
    parser.add_argument(
        "--created-at", metavar="ISO8601",
        help="stamp for createdAt/evaluatedAt. Defaults to now; pass a fixed value to keep the "
             "wire payload byte-reproducible.")
    args = parser.parse_args(argv)

    context: dict = {}
    result = run_pipeline(mode=args.mode, dataset=args.dataset, context=context,
                          population=args.population, limit=args.limit)
    out_dir = args.out or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"mine_result_{args.mode}.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    if args.emit_wire:
        # `created_at` is passed in, not read from the clock inside the mapper, so the same
        # input yields the same bytes and a test can diff two runs.
        created_at = args.created_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        cfg = load_config()
        judge_cfg = cfg.get("judge", {}) or {}
        # Upsert into the store FIRST, then emit from it. A finding whose evidence has expired
        # stays on screen; a run that no longer reproduces is marked, not deleted.
        store = SuggestionStore()
        observed = to_wire(
            result=result, runs=context["runs"], registry=context["registry"],
            created_at=created_at,
            judge_config_hash=(
                f"{judge_cfg.get('model', '?')}:{judge_cfg.get('goal_source', '?')}"
                f":{judge_cfg.get('pass_threshold', '?')}:runs{judge_cfg.get('runs', '?')}"),
            judge_runs=int(judge_cfg.get("runs") or 0),
            evidence_sources=["aws/spans", "ddb:plan-state", f"{args.population}:{args.mode}"],
            verdicts=context["verdicts"],
        )
        # Scrubbed on the way IN as well as out — see the funnel at the emit step below. Defence in
        # depth only: a row already carrying a withheld field and no longer reproducing is never
        # rewritten, so the read-time pass is the one that closes it.
        change = store.upsert_all(
            [publishable_suggestion(row) for row in observed["suggestions"]], seen_at=created_at)
        store.save()

        # Emit the STORE's view, not this run's, so a suggestion whose evidence expired stays
        # visible with reproducing=False. That is what the store is for — and it is also why the
        # store is a REPLAY CHANNEL that bypasses `to_wire`: it keeps each suggestion as it stood
        # when written and never rewrites one that stops reproducing, so a row minted by older
        # producer code still carries that code's fields.
        #
        # `published_suggestions_block` is the whole publish rule, once: it normalises `status` (a
        # dismissal is per-actor and lives in the store's `SUGG#<id>` overlay), projects each row
        # onto what the shared item may carry, and carries the pre-trim COUNT beside the list so a
        # trim downstream is legible. `improve.aws_lambda` calls the same function. Do not
        # re-implement any part of it at either site — this expression had three hand copies.
        wire = {**observed,
                **published_suggestions_block(store.all_suggestions(include_dismissed=True))}
        wire["components"] = components_to_wire(
            context["registry"], wire["suggestions"],
            runs_by_id={r.run_id: r for r in context["runs"]},
            verdicts_by_id={v.run_id: v for v in context["verdicts"]},
            # Which runs attribute where. Taken from the RESULT, because a suggestion's member run
            # ids do not travel on the wire — they are session ids on an item every actor reads.
            members_by_suggestion=suggestion_members(result))

        os.makedirs(os.path.dirname(args.emit_wire), exist_ok=True)
        with open(args.emit_wire, "w") as f:
            json.dump(wire, f, indent=2, sort_keys=True)
        if not args.quiet:
            print(f"store: +{len(change['added'])} new, {len(change['updated'])} seen again, "
                  f"{len(change['quiet'])} not reproducing, {change['total']} total")
            print(f"wrote wire payload {args.emit_wire}: "
                  f"{len(wire['suggestions'])} suggestion(s), "
                  f"{wire['components']['registryMeta']['componentCount']} components")
    if not args.quiet:
        _print_report(result)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
