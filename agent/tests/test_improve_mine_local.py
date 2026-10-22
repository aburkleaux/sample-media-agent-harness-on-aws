"""The offline pipeline entry point — previously untested, though it produces the artifact.

Covered here because these are the claims the artifact's honesty rests on: a missing tier
score becomes NO_DATA rather than a pass, sentinels serialize as their literal names rather
than null, the recomputed verdict still matches the labels it was derived from, and the whole
result is JSON-serializable.

`run_pipeline` in recorded mode needs the offline harvest, which lives outside the repo
(OURS-2), so those tests are skip-guarded. The pure functions are not.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve.contract import (  # noqa: E402
    NO_DATA, UNKNOWN, UNMEASURED, EvaluatorResult, Outcome, Overall, Plan, PlanNode, Run,
    Scenario, UtilityVector, Verdict,
)
from improve.evaluate import DEFAULT_GATING  # noqa: E402
from improve.judge import DEFAULT_PASS_THRESHOLD  # noqa: E402
from improve.dto import publishable_suggestions  # noqa: E402
from improve.mine_local import (  # noqa: E402
    _jsonable, _print_report, _tier_result, run_pipeline, verdict_from_recorded,
)
from improve.observe.build_run import HarvestedPlanSource  # noqa: E402

from improve_fixtures import E2CB300B, real_plan  # noqa: E402


def _scenario(**expected) -> Scenario:
    return Scenario(scenario_id="s1", evaluators=["goal_judge"], expected=expected,
                    source_session="s1")


def _run_with(expected: dict, plan=None) -> Run:
    run = Run(run_id="s1", plan=plan if plan is not None else NO_DATA)
    run.scenario = _scenario(**expected)
    return run


# --- _tier_result -----------------------------------------------------------
def test_missing_tier_score_is_no_data_never_a_pass():
    """The single most important sentinel claim in the offline path: an incomplete label must
    not become a passing verdict by omission."""
    result = _tier_result("dispatch_correct", None, 0.7)
    assert result.outcome is Outcome.NO_DATA
    assert result.score is None


def test_tier_at_exactly_the_threshold_passes():
    assert _tier_result("t", 0.7, 0.7).outcome is Outcome.PASS
    assert _tier_result("t", 0.6999, 0.7).outcome is Outcome.FAIL


def test_tier_explanation_records_the_threshold_it_was_judged_against():
    assert "0.7" in _tier_result("t", 0.9, 0.7).explanation


# --- verdict_from_recorded --------------------------------------------------
def test_a_missing_tier_makes_the_verdict_unscored_never_pass():
    """A half-labelled run is UNSCORED, and the claim that matters is that it is not PASS.

    This asserted FAIL until the gate learned a third state. The intent has not changed (an
    incomplete label must never become a passing verdict by omission) but FAIL was the wrong
    way to express it: one tier here MEASURED a pass and the other was never measured, so
    there is no evidence of a defect. Calling that FAIL is what put 30 unmeasurable runs of
    the 50-session harvest into the failing bucket, where mining then treated them as
    evidence of failure.
    """
    verdict = verdict_from_recorded(_run_with({"dispatch_correct": 0.9}),
                                    DEFAULT_PASS_THRESHOLD, DEFAULT_GATING)
    assert verdict.overall is Overall.UNSCORED
    assert verdict.overall is not Overall.PASS
    assert "results_delivered:NO_DATA" in verdict.reason


def test_no_recorded_tiers_at_all_is_fail_from_the_no_data_reds():
    verdict = verdict_from_recorded(_run_with({}), DEFAULT_PASS_THRESHOLD, DEFAULT_GATING)
    assert verdict.overall is not Overall.PASS


def test_gating_reason_keeps_the_informational_reds():
    """The hand copy of the gating logic omitted this suffix, so the artifact's readable
    `reason` concealed that both passing runs the ordering claim rests on carry a RED
    DagCompletionValidator. Delegating to `evaluate.gate` is what fixes it."""
    plan = Plan(nodes=[PlanNode(id="a", status="failed")])
    run = Run(run_id="s1", plan=plan)
    run.scenario = Scenario(
        scenario_id="s1", evaluators=["goal_judge", "DagCompletionValidator"],
        expected={"dispatch_correct": 0.9, "results_delivered": 0.9}, source_session="s1")
    verdict = verdict_from_recorded(run, DEFAULT_PASS_THRESHOLD, DEFAULT_GATING)
    assert verdict.overall is Overall.PASS
    assert "informational" in verdict.reason
    assert "DagCompletionValidator" in verdict.reason


def test_config_level_evaluator_list_is_honoured_like_the_live_path():
    """Reading only `scenario.evaluators` meant a config-level list applied in live mode and
    was silently ignored in recorded mode, so the two could run different evaluator sets."""
    run = _run_with({"dispatch_correct": 0.9, "results_delivered": 0.9},
                    plan=Plan(nodes=[PlanNode(id="a", status="failed")]))
    verdict = verdict_from_recorded(
        run, DEFAULT_PASS_THRESHOLD, DEFAULT_GATING,
        evaluators=["goal_judge", "DagCompletionValidator"])
    assert any(r.name == "DagCompletionValidator" for r in verdict.evaluator_results)


# --- _jsonable --------------------------------------------------------------
def test_sentinels_serialize_as_their_names_never_null():
    """A null would read as "field absent" and be styled like any other empty value in the
    UI — exactly the confusion the sentinels exist to prevent."""
    assert _jsonable(NO_DATA) == "NO_DATA"
    assert _jsonable(UNKNOWN) == "UNKNOWN"
    assert _jsonable(UNMEASURED) == "UNMEASURED"
    assert _jsonable({"tokens": UNMEASURED}) == {"tokens": "UNMEASURED"}


def test_enums_serialize_as_their_values():
    verdict = Verdict("r", [EvaluatorResult("t", "llm", Outcome.FAIL, 0.1)],
                      UtilityVector(terms={"tokens": UNMEASURED}), Overall.FAIL, "why")
    out = _jsonable(verdict)
    assert out["overall"] == "FAIL"
    assert out["evaluator_results"][0]["outcome"] == "FAIL"
    assert out["utility"]["terms"]["tokens"] == "UNMEASURED"
    json.dumps(out)  # must not raise


def test_dates_are_serializable():
    """An unquoted YAML date reaches provenance as a datetime.date, which json.dump rejects."""
    from datetime import date
    assert _jsonable({"revalidated": date(2026, 8, 21)}) == {"revalidated": "2026-08-21"}


# --- run_pipeline (needs the offline harvest) -------------------------------
def _harvest_present() -> bool:
    try:
        return bool(HarvestedPlanSource().plan_for(E2CB300B))
    except Exception:  # noqa: BLE001 - absence is the only thing we care about here
        return False


requires_harvest = pytest.mark.skipif(
    not _harvest_present(),
    reason="offline harvest lives outside the repo (OURS-2)")


@requires_harvest
def test_recorded_pipeline_is_json_serializable_end_to_end():
    result = run_pipeline(mode="recorded")
    json.dumps(result)  # the artifact is written with json.dump; this is that contract


@requires_harvest
def test_recorded_pipeline_reproduces_the_datasets_own_labels():
    """If the recomputed split ever diverges from `expected.overall`, the offline pipeline has
    stopped reproducing the judgement its labels came from — and every downstream pass_hits
    is measured against the wrong controls."""
    from improve.loop import load_golden
    result = run_pipeline(mode="recorded")
    recorded_fail = {s.source_session or s.scenario_id for s in load_golden()
                     if (s.expected or {}).get("overall") == "FAIL"}
    assert set(result["population"]["failing"]) == recorded_fail


@requires_harvest
def test_the_artifact_says_how_much_of_its_evidence_was_verified_complete():
    """Truncated evidence is reported on the OFFLINE path too, not only on the wire.

    Two numbers, and the second is the load-bearing one. On the golden harvest — taken before
    adaptive windowing existed — nothing recorded completeness either way, so this reads
    `0 incomplete of 0 measured`, which is "not checked". Reporting only the count of incomplete
    runs would make that indistinguishable from a population verified whole, which is exactly the
    silence this task exists to remove.

    Unlike the wire's version this one NAMES the runs: the artifact is a local file with no tenant
    boundary, and an operator diffing two mining runs needs to know which runs stopped being
    controls (the no-run-ids rule applies to the global SNAPSHOT item, not to this file).
    """
    population = run_pipeline(mode="recorded")["population"]
    assert population["evidence_incomplete"] == []
    assert population["completeness_measured"] == 0, (
        "the golden harvest carries no completeness verdict, so this must read as NOT CHECKED "
        "rather than as a clean bill of health")


# --- what the discovered population is a sample OF --------------------------
def _sessions_file(tmp_path, spans_per_session: dict) -> str:
    """A sessions harvest with the given span COUNT per session, in the shape the readers open.

    Only the span count and the timestamp matter to discovery, so the records carry nothing else —
    a fuller fixture would imply this test depends on evidence it does not read.
    """
    path = os.path.join(str(tmp_path), "sessions.json")
    with open(path, "w") as f:
        json.dump({sid: {"spans": [{"ts": f"2026-08-2{i % 9} 10:00:00.000"}
                                   for i in range(count)]}
                   for sid, count in spans_per_session.items()}, f)
    return path


def test_discovery_measures_both_bounds_it_narrowed_the_population_with(tmp_path):
    """Two filters shrink the population and NEITHER used to be counted.

    Without these numbers `population_size` is a sample of the harvest whose fraction nobody knows —
    and every figure computed from it (pass rate, spend, each day row) inherits that. The span floor
    and the cap are reported separately because they are different facts with different fixes.
    """
    from improve.observe.population import discover_sessions, harvest_population

    path = _sessions_file(tmp_path, {"big-1": 9, "big-2": 8, "big-3": 7,
                                     "tiny-1": 2, "tiny-2": 1})
    stats: dict = {}
    scenarios = harvest_population(path, limit=2, stats=stats)

    assert len(scenarios) == 2
    assert stats == {"harvested": 5, "below_min_spans": 2, "discoverable": 3,
                     "min_spans": 5, "admitted": 2, "limit": 2}

    # And an uncapped read admits everything it discovered, so `admitted == discoverable` is the
    # honest way to say "the cap did not bind" rather than the block being omitted.
    uncapped: dict = {}
    harvest_population(path, limit=None, stats=uncapped)
    assert uncapped["admitted"] == uncapped["discoverable"] == 3
    assert uncapped["limit"] is None

    # `discover_sessions` fills its own half, so the count of floor-excluded sessions exists even
    # for a caller that never applies a cap.
    only_discovery: dict = {}
    discover_sessions(path, stats=only_discovery)
    assert only_discovery["below_min_spans"] == 2


def test_the_discovered_population_carries_its_own_counts_into_the_artifact(monkeypatch):
    """The link between the counter and the reporter, which nothing else exercises.

    `harvest_population` is the only thing that can measure what it discarded and `run_pipeline` is
    the only caller, so if the stats dict is not passed and read here the numbers exist and reach
    nobody. Everything expensive is stubbed: this test is about that one hand-off, and the live path
    it sits on needs a judge and AgentCore Memory.
    """
    import improve.mine_local as ml

    def fake_population(limit=None, stats=None, **kwargs):
        if stats is not None:
            stats.update({"harvested": 39, "below_min_spans": 3, "discoverable": 36,
                          "min_spans": 5, "admitted": 1, "limit": limit})
        return [Scenario(scenario_id="s1", source_session="s1", category="discovered")]

    monkeypatch.setattr(ml, "harvest_population", fake_population)
    monkeypatch.setattr(ml, "build_run", lambda scenario, **kwargs: Run(run_id="s1"))
    monkeypatch.setattr(ml, "evaluate", lambda run, **kwargs: Verdict(
        "s1", [], UtilityVector(), Overall.PASS, "", workload_class="conversational"))
    monkeypatch.setattr(ml, "_make_judge", lambda cfg, profile: object())
    monkeypatch.setattr("improve.observe.memory_reader.MemoryReader",
                        lambda **kwargs: None)

    result = run_pipeline(mode="live", population="harvest", limit=1)
    assert result["population_size"] == 1
    assert result["population_discovery"] == {"harvested": 39, "below_min_spans": 3,
                                             "discoverable": 36, "min_spans": 5,
                                             "admitted": 1, "limit": 1}


def test_the_report_says_what_fraction_of_the_minable_day_it_scored(capsys):
    """The console had the same defect as the wire: "population: 25 runs" and nothing else."""
    _print_report({
        "mode": "live", "label_provenance": "fresh judge run",
        "population": {"total": 25, "failing": [], "passing": [], "unscored": [],
                       "evidence_incomplete": [], "completeness_measured": 0},
        "population_discovery": {"harvested": 39, "below_min_spans": 3, "discoverable": 36,
                                 "min_spans": 5, "admitted": 25, "limit": 25},
        "signatures": [], "residue": [], "evidence_gaps": [], "environment": [],
        "proposals": [], "unattributed": [],
    })
    out = capsys.readouterr().out
    assert "discovered 36 minable session(s) of 39 harvested" in out
    assert "3 under min_spans=5" in out
    assert "admitted 25" in out
    assert "25/36 of the minable day" in out, "a binding cap must say so, not merely be inferable"


def test_the_report_says_how_much_spend_the_harvest_discarded(capsys):
    """The staging figure this exists for: 13.3M of 36.8M tokens, invisible in every cost line."""
    _print_report({
        "mode": "live", "label_provenance": "fresh judge run",
        "population": {"total": 20, "failing": [], "passing": [], "unscored": [],
                       "evidence_incomplete": [], "completeness_measured": 0},
        "usage_shortfall": {"tokens_discarded": 13_300_000, "tokens_measured": 36_800_000,
                            "sessions_discarded": 24, "sessions_measured": 44},
        "signatures": [], "residue": [], "evidence_gaps": [], "environment": [],
        "proposals": [], "unattributed": [],
    })
    out = capsys.readouterr().out
    assert "DISCARDED 13300000 of 36800000 measured token(s)" in out
    assert "24 of 44 session(s)" in out


def test_the_report_prints_no_shortfall_line_rather_than_an_unmeasured_one(capsys):
    """The artifact flattens a sentinel to its NAME, so an unmeasured shortfall arrives as a string.

    Interpolating it would print "DISCARDED UNMEASURED of UNMEASURED", which reads as a measurement
    of nothing. Saying nothing is the honest rendering of "this harvest never looked".
    """
    _print_report({
        "mode": "recorded", "label_provenance": "recorded",
        "population": {"total": 6, "failing": [], "passing": [], "unscored": [],
                       "evidence_incomplete": [], "completeness_measured": 0},
        "usage_shortfall": {"tokens_discarded": "UNMEASURED", "tokens_measured": "UNMEASURED",
                            "sessions_discarded": "UNMEASURED",
                            "sessions_measured": "UNMEASURED"},
        "signatures": [], "residue": [], "evidence_gaps": [], "environment": [],
        "proposals": [], "unattributed": [],
    })
    out = capsys.readouterr().out
    assert "DISCARDED" not in out and "UNMEASURED" not in out


def test_the_report_prints_the_completeness_line_even_when_nothing_was_measured(capsys):
    """`0 of 0` is printed, not skipped. A missing line reads as "no problem"."""
    _print_report({
        "mode": "recorded", "label_provenance": "recorded",
        "population": {"total": 2, "failing": ["a"], "passing": ["b"], "unscored": [],
                       "evidence_incomplete": [], "completeness_measured": 0},
        "signatures": [], "residue": [], "evidence_gaps": [], "environment": [],
        "proposals": [], "unattributed": [],
    })
    assert "evidence INCOMPLETE for 0 of 0 run(s)" in capsys.readouterr().out


def test_the_report_names_the_runs_that_stopped_being_controls(capsys):
    _print_report({
        "mode": "recorded", "label_provenance": "recorded",
        "population": {"total": 2, "failing": ["a"], "passing": ["74b0a063aaaa"], "unscored": [],
                       "evidence_incomplete": ["74b0a063aaaa"], "completeness_measured": 2},
        "signatures": [], "residue": [], "evidence_gaps": [], "environment": [],
        "proposals": [], "unattributed": [],
    })
    out = capsys.readouterr().out
    assert "evidence INCOMPLETE for 1 of 2 run(s)" in out
    assert "74b0a063" in out and "cannot act as controls" in out


@requires_harvest
def test_the_labelled_benchmark_records_no_discovery_rather_than_zeros():
    """`golden` has fixed membership and no cap, so there is no discovery step to report on.

    `None` and not `{...: 0}`: the wire drops the block entirely on this reading
    (`dto._discovered_sessions`), and zeros would claim a cap ran and admitted everything. The
    shortfall block IS present either way, because it describes the harvest and not the population —
    on the committed harvest, which predates the audit, every term of it is unmeasured.
    """
    result = run_pipeline(mode="recorded")
    assert result["population_discovery"] is None
    assert result["population_size"] > 0
    assert set(result["usage_shortfall"]) == {"tokens_discarded", "tokens_measured",
                                             "sessions_discarded", "sessions_measured"}


@requires_harvest
def test_recorded_pipeline_reports_which_labels_it_stood_on():
    result = run_pipeline(mode="recorded")
    assert result["recorded_label_provenance"]
    assert result["recorded_label_provenance"] != ["(not recorded)"]
    assert "threshold" in result["recorded_label_provenance"][0]


@requires_harvest
def test_summary_counts_agree_with_the_lists_they_summarise():
    result = run_pipeline(mode="recorded")
    assert result["summary"]["admitted"] == len(result["signatures"])
    assert result["summary"]["residue"] == len(result["residue"])
    assert result["summary"]["proposals"] == len(result["proposals"])
    assert result["summary"]["unattributed"] == len(result["unattributed"])
    # The environment bucket is a first-class artifact key, not an internal-only detail: a
    # zero-signature run because the platform was on fire must not look like a clean one.
    assert "environment" in result
    assert result["summary"]["environment"] == len(result["environment"])


def test_report_surfaces_the_environment_bucket_distinct_from_evidence_gaps():
    """The human report must route platform-degraded failures to ops, in their own block —
    not fold them into EVIDENCE GAPS (which points at the defect register)."""
    result = {
        "mode": "recorded", "label_provenance": "recorded",
        "population": {"total": 1, "failing": ["e2cb300b"], "passing": [], "unscored": []},
        "signatures": [], "residue": [], "evidence_gaps": [],
        "environment": [{
            "run_id": "e2cb300baaaa", "symptom": [], "best_shape": [], "pass_hits": -1,
            "pass_hit_runs": [],
            "reason": "platform was DEGRADED during this run's window — route to ops",
        }],
        "proposals": [], "unattributed": [],
    }
    out = io.StringIO()
    with redirect_stdout(out):
        _print_report(result)
    text = out.getvalue()
    assert "ENVIRONMENT (1) — not the agent; route to ops" in text
    assert "e2cb300b" in text
    assert "EVIDENCE GAPS" not in text  # not folded into the observability bucket


@requires_harvest
def test_run_pipeline_publishes_verdicts_on_the_context_side_channel():
    """`to_wire` needs the Verdict OBJECTS to build the scorecard.

    The artifact's own `verdicts` key is `_jsonable`, i.e. already flattened to strings, so a
    consumer reading that would have to re-parse enums to count a pass. The side channel carries
    the objects, alongside the runs they belong to, keyed the same way.
    """
    context: dict = {}
    result = run_pipeline(mode="recorded", context=context)

    assert set(context) >= {"runs", "registry", "verdicts"}
    assert all(isinstance(v, Verdict) for v in context["verdicts"])
    assert len(context["verdicts"]) == len(context["runs"]) == result["population"]["total"]
    assert {v.run_id for v in context["verdicts"]} == {r.run_id for r in context["runs"]}


@requires_harvest
def test_registry_availability_is_reported_not_assumed():
    result = run_pipeline(mode="recorded")
    assert "available" in result["registry"]
    assert result["registry"]["component_count"] >= 0


def test_the_seeded_bundle_never_publishes_a_local_dismissal(tmp_path, monkeypatch):
    """`status` is per-actor and lives in the cloud overlay, so the global snapshot must not
    carry one.

    The bundle `--emit-wire` writes is seeded as the ONE GLOBAL snapshot under
    `actorId="__system__"`, while a dismissal is a per-actor `SUGG#<id>` row applied at read
    time by `capability_api.improve_dto.merge_overlay`. Two failure modes if a local status
    leaks in: it dismisses for every user, and `merge_overlay` only ADDS dismissals so there is
    no undo. And because `all_suggestions()` hides dismissed rows by default, the suggestion
    would disappear from the bundle entirely - for everyone.
    """
    from improve.store import SuggestionStore

    store = SuggestionStore(path=str(tmp_path / "store.json"))
    # The dismissal is seeded ON THE ROW rather than through a setter, which is the only way one can
    # reach this store now: `set_status` is gone (nothing called it outside this test, and a real
    # dismissal is a per-actor `SUGG#` row), and `upsert_all` keeps a status a row already carries
    # because it only `setdefault`s the field. So this is also the shape a row minted by older
    # producer code and replayed keeps forever — the path `publishable_suggestions` exists for.
    store.upsert_all([{"suggestionId": "keep"},
                      {"suggestionId": "gone", "status": "dismissed",
                       "dismissReason": "not worth it"}],
                     seen_at="2026-08-26T00:00:00Z")
    store.save()

    # The local view: the dismissal is honoured and the row is hidden.
    assert [r["suggestionId"] for r in store.all_suggestions()] == ["keep"]

    # The published view, through the SHARED rule rather than a hand copy of it: both rows present,
    # both open. This expression used to be spelled out here as well as at both producer sites, and
    # a third copy in a test is a third place the rule can be wrong while the test stays green.
    published = publishable_suggestions(store.all_suggestions(include_dismissed=True))
    assert sorted(r["suggestionId"] for r in published) == ["gone", "keep"]
    assert {r["status"] for r in published} == {"open"}
    # ...and the operator's note behind that local dismissal is not published either.
    assert "not worth it" not in json.dumps(published)


def test_a_store_row_from_older_producer_code_is_scrubbed_on_the_way_out(tmp_path):
    """The store REPLAYS suggestions, and the replay bypasses `to_wire`.

    `upsert_all` persists each suggestion as it stood when written, and a row absent from a later
    batch is marked `reproducing=False` with its content untouched — so a row minted by older
    producer code keeps that code's fields for as long as the finding lives. A test over freshly
    built rows cannot see this, because a fresh row is already clean; this one seeds the store with
    the shape the deployed producer actually left in staging.
    """
    from improve.store import SuggestionStore

    legacy = {"suggestionId": "ec7e1ed065a6", "targetId": "chainer-settle-rule",
              "pattern": {"patternId": "gateway_reported_failure",
                          "shapeFacets": ["gateway_reported_failure"], "passHits": 0,
                          "memberRunIds": ["bc42dea7-da9e-4e18-9448-338b7a4d1697"],
                          "rootCauseHypo": "failed: ['sg5_index']"}}
    store = SuggestionStore(path=str(tmp_path / "store.json"))
    store.upsert_all([legacy], seen_at="2026-08-20T00:00:00Z")
    store.save()

    # It is still in the store, verbatim — nothing rewrites a row that stops reproducing, which is
    # why read-time scrubbing is the mandatory half rather than a belt on top of a write-time one.
    reloaded = SuggestionStore(path=str(tmp_path / "store.json"))
    stored = reloaded.all_suggestions(include_dismissed=True)[0]
    assert stored["pattern"]["memberRunIds"] == ["bc42dea7-da9e-4e18-9448-338b7a4d1697"]

    published = json.dumps(publishable_suggestions(
        reloaded.all_suggestions(include_dismissed=True)))
    for probe in ("memberRunIds", "rootCauseHypo",
                  "bc42dea7-da9e-4e18-9448-338b7a4d1697", "sg5_index"):
        assert probe not in published, f"{probe!r} was republished from the store"


def _breakdown_verdict(cls, overall, gating, basis="inferred_from_spans", run_id="r"):
    return Verdict(run_id, [], UtilityVector(), overall, "", workload_class=cls,
                   workload_basis=basis, gating=gating)


def test_the_artifact_and_the_wire_report_identical_counts_for_one_population():
    """Two projections of ONE count (`contract.verdict_counts_by_class`).

    The artifact (`_workload_breakdown`) and the wire (`dto.scorecard_to_wire`) spell these
    numbers differently on purpose — uppercase vs lowercase keys, and a `gating` field the
    artifact flattens when there is only one set while the wire keeps one stable shape. What they
    may never do is DISAGREE, and a hand copy of this fold has already drifted once in this
    codebase: `evaluate.gate` records that `mine_local`'s duplicate omitted the
    `(informational: …)` suffix, which concealed a RED `DagCompletionValidator` on both of the
    passing runs an ordering claim rested on. This test is what makes the shared counter worth
    extracting.
    """
    from improve.dto import scorecard_to_wire
    from improve.mine_local import _workload_breakdown

    verdicts = [
        _breakdown_verdict("conversational", Overall.PASS, ("dispatch_correct", "results_delivered")),
        _breakdown_verdict("conversational", Overall.FAIL, ("dispatch_correct", "results_delivered")),
        # same class, DIFFERENT gating set -> mixed, and a different basis
        _breakdown_verdict("conversational", Overall.UNSCORED, ("dispatch_correct",),
                           basis="recorded_label"),
        _breakdown_verdict("tool_discovery", Overall.PASS, ("ToolDiscoveryValidator",)),
        _breakdown_verdict("", Overall.UNSCORED, ()),          # unclassified -> "unknown"
    ]

    artifact = _workload_breakdown(verdicts)
    wire = scorecard_to_wire([], verdicts)

    assert set(artifact) == set(wire["byWorkloadClass"]) == {
        "conversational", "tool_discovery", "unknown"}
    for cls, recorded in artifact.items():
        emitted = wire["byWorkloadClass"][cls]
        assert recorded["total"] == emitted["total"]
        assert recorded["PASS"] == emitted["pass"]
        assert recorded["FAIL"] == emitted["fail"]
        assert recorded["UNSCORED"] == emitted["unscored"]
        assert recorded["gating_is_mixed"] == emitted["gatingIsMixed"]
        assert recorded["basis"] == emitted["basis"]
    # The per-class totals add up to the population the wire reports overall.
    assert sum(e["total"] for e in artifact.values()) == wire["population"]["total"]
    assert sum(e["FAIL"] for e in artifact.values()) == wire["population"]["fail"]

    # ...and the two spellings of `gating` still carry the same sets. The artifact flattens a
    # single set; the wire never does.
    assert artifact["tool_discovery"]["gating"] == ["ToolDiscoveryValidator"]
    assert wire["byWorkloadClass"]["tool_discovery"]["gating"] == [["ToolDiscoveryValidator"]]
    assert artifact["conversational"]["gating"] == \
        wire["byWorkloadClass"]["conversational"]["gating"]


def test_the_workload_breakdown_reports_a_mixed_gating_set_rather_than_the_first_one():
    """The field exists to say WHY a class's numbers mean what they do, so it must not be able
    to say something false. Keeping only the first verdict's gating printed it as though it
    described the whole class."""
    from improve.mine_local import _workload_breakdown

    def verdict(gating, basis, overall=Overall.PASS):
        return Verdict("r", [], UtilityVector(), overall, "", workload_class="conversational",
                       workload_basis=basis, gating=gating)

    uniform = _workload_breakdown([
        verdict(("dispatch_correct", "results_delivered"), "inferred_from_spans"),
        verdict(("dispatch_correct", "results_delivered"), "inferred_from_spans")])
    assert uniform["conversational"]["gating"] == ["dispatch_correct", "results_delivered"]
    assert uniform["conversational"]["gating_is_mixed"] is False
    assert uniform["conversational"]["basis"] == ["inferred_from_spans"]

    mixed = _workload_breakdown([
        verdict(("dispatch_correct", "results_delivered"), "recorded_label"),
        verdict(("dispatch_correct",), "inferred_from_spans", Overall.FAIL)])
    assert mixed["conversational"]["gating_is_mixed"] is True
    assert mixed["conversational"]["basis"] == ["inferred_from_spans", "recorded_label"]
    assert mixed["conversational"]["total"] == 2
    assert mixed["conversational"]["FAIL"] == 1
