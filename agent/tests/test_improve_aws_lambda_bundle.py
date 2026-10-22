"""The bundle guards in the ``improve-worker`` producer: what must be present, what a
too-large bundle is allowed to lose, and what the daily history must carry forward.

`aws_lambda` had no tests at all, and both guards here fail SILENTLY when wrong. A bundle that
lost a top-level key would be written and the Improve page would render "no data" as if healthy
(`_require_bundle_keys` exists for exactly that), and a trim that dropped a measurement it did
not have to would degrade the surface with no error anywhere.

The same two guards apply to each PER-ACTOR item the producer also writes, with its own required-key
set and its own TWO rungs on the trim ladder — one guard function and one trim loop, two contracts,
because "refuse a bundle missing a key" and "drop from the tail until it fits" are each one rule and a
hand copy of either would drift. The rungs are ordered: run rows give way first because the next
harvest recomputes them, then day rows, which are folded into the rolled-up bucket rather than dropped
because they are the only copy of a past day.

The daily history is the one part of either bundle that is CUMULATIVE, and every SNAPSHOT write is an
unconditional PutItem of a whole item — so its correctness is entirely about the read that carries the
previous item's rows forward, and about not fabricating a number for a term nothing measured. Both fail
silently too: a wiped history looks exactly like a first run, and a zero looks exactly like a
measurement. The per-actor half is the one that was actually costing readers their figures: 5 of the 7
per-actor items in staging were stamped a day older than the global one, because their owner ran
nothing in the last window and the producer skipped the write.

AWS is reached nowhere: `_require_bundle_keys`, `_fit_bundle`, `_fit_actor_bundle`, `_with_history`,
`_fit_history` and `build_item` are pure functions over the wire dict, and the one function that
takes a table (`_read_history`) is given `_StrictTable` below. Importing the module pulls boto3 at
module scope, which is fine — it is a producer module and the isolation boundary
(`test_capability_api_isolation.py`) covers the two targets that must NOT import it.
"""
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# The read route's own transforms, so "a history inside the bundle reaches a consumer" is checked
# against the code that actually serves it rather than against a restatement of it here.
from capability_api.improve_dto import (  # noqa: E402
    decode_decimals, merge_overlay, with_actor_bundle,
)
from improve import aws_lambda  # noqa: E402  — the module itself, for the `run` seams
from improve.aws_lambda import (  # noqa: E402
    _EMPTY_ROLLED_UP, _HISTORY_BUDGET_BYTES, _HISTORY_FIRST_DATE_KEY, _HISTORY_KEY, _HISTORY_ROWS,
    _ITEM_BUDGET_BYTES, _POPULATION_LIMIT, _REQUIRED_ACTOR_BUNDLE_KEYS, _REQUIRED_BUNDLE_KEYS,
    _ROLLED_UP_KEY, _actor_item, _fit_actor_bundle, _fit_bundle, _fit_history, _history_row,
    _merge_history, _read_history, _require_bundle_keys, _scoped_environ, _with_actor_history,
    _with_history, _zero_actor_bundle,
)
from improve.contract import (  # noqa: E402
    EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict,
)
from improve.ddb_store import STORE_SK, _decimalize  # noqa: E402
from improve.dto import (  # noqa: E402
    _SPEND_MODELS_NAMED, components_to_wire, scorecard_to_wire, waste_to_wire,
)
from improve.registry import SigmaRegistry  # noqa: E402
from improve.seed_snapshot import (  # noqa: E402
    SNAPSHOT_SK, SYSTEM_ACTOR, build_item, load_bundle,
)
# Real harvested windows (REQ-017): a day row is keyed on the day a run's window starts, so every date
# these tests reason about is one the harvest actually produced rather than one invented here.
from improve_fixtures import FFAB73F8, real_window  # noqa: E402

# DynamoDB's own hard per-item cap, which is the fact `_ITEM_BUDGET_BYTES` leaves headroom under.
# Named here rather than shared with that constant because they are two different bounds: one is the
# store's limit, the other is the producer's self-imposed target inside it.
_DDB_ITEM_LIMIT_BYTES = 400 * 1024

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
"""


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "sigma.yaml"
    path.write_text(REGISTRY_FIXTURE)
    return SigmaRegistry.load(str(path))


def _verdict(run_id, delivered_outcome=Outcome.PASS, score=0.9):
    return Verdict(run_id,
                   [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, score, ""),
                    EvaluatorResult("results_delivered", "llm", delivered_outcome, score, "")],
                   UtilityVector(), Overall.PASS, "", workload_class="conversational",
                   gating=("dispatch_correct", "results_delivered"))


def _population(count=3):
    runs = {f"r{i}": Run(run_id=f"r{i}", resources={"latency_ms": 10}) for i in range(count)}
    verdicts = {f"r{i}": _verdict(f"r{i}") for i in range(count)}
    return runs, verdicts


def _suggestion(index, padding=0):
    """A wire suggestion. It carries NO member run ids — a session id list may not ride the shared
    item — so which runs attribute to it travels separately, as `_members`."""
    return {
        "suggestionId": f"s{index}",
        "targetId": "editable-persona",
        "reproducing": True,
        "lastSeenAt": f"2026-08-2{index}T00:00:00Z",
        # Bulk, so a small budget forces the trim without needing a realistic 350 KB fixture.
        "evidenceSummary": "x" * padding,
    }


def _members(count):
    """``{suggestionId: [run ids]}`` as `dto.suggestion_members` builds it, one run each."""
    return {f"s{i}": [f"r{i}"] for i in range(count)}


def _bundle(registry, suggestions, runs, verdicts, members=None):
    return {
        "suggestions": suggestions,
        # The pre-trim count, exactly as the producers stamp it (`dto.published_suggestions_block`).
        # Set here rather than left out so `_fit_bundle` in these tests carries the same denominator
        # the served item does — a fixture without it would let a trim look like a shrink.
        "suggestionsTotal": len(suggestions),
        "components": components_to_wire(registry, suggestions, runs_by_id=runs,
                                         verdicts_by_id=verdicts,
                                         members_by_suggestion=members),
        "scorecard": scorecard_to_wire(list(runs.values()), list(verdicts.values())),
        # These runs carry no trajectory, so the efficiency path has nothing to observe and the
        # block is empty-but-complete. It is here because the producer must never omit the key.
        "waste": waste_to_wire(None),
        "patterns": {"admitted": [], "residue": [], "evidenceGaps": [], "environment": []},
    }


# --- what must be present ---------------------------------------------------
def test_required_bundle_keys_includes_scorecard_and_waste():
    """Both are FIXED size and always derivable, so a bundle without either is a producer bug.

    `scorecard` is the only thing the surface can say when `admitted` is 0. `waste` is required for
    a sharper reason: `{"runsWithWaste": 0}` and an absent key mean different things — nothing
    wasted versus nobody looked — and only the producer can keep them apart (invariant 1).
    """
    assert "scorecard" in _REQUIRED_BUNDLE_KEYS
    assert "waste" in _REQUIRED_BUNDLE_KEYS
    with pytest.raises(RuntimeError, match="scorecard"):
        _require_bundle_keys({"suggestions": [], "components": {}, "patterns": {},
                              "waste": waste_to_wire(None)})
    with pytest.raises(RuntimeError, match="waste"):
        _require_bundle_keys({"suggestions": [], "components": {}, "patterns": {},
                              "scorecard": {}})


def test_the_suggestion_count_is_a_required_bundle_key(registry):
    """`suggestions` is the ONE list on the shared item that grows without bound, and `_fit_bundle`
    drops from its tail. A bundle that lost the pre-trim count would publish the trim as a shrink —
    the served item presenting a truncated list as the whole set, with only a log line dissenting."""
    assert "suggestionsTotal" in _REQUIRED_BUNDLE_KEYS
    with pytest.raises(RuntimeError, match="suggestionsTotal"):
        _require_bundle_keys({"suggestions": [], "components": {}, "patterns": {},
                              "scorecard": {}, "waste": waste_to_wire(None)})


def test_a_complete_bundle_passes_the_guard(registry):
    runs, verdicts = _population()
    _require_bundle_keys(_bundle(registry, [], runs, verdicts))  # must not raise


# --- what a trim may and may not lose ---------------------------------------
def test_a_bundle_that_already_fits_is_returned_untouched(registry):
    runs, verdicts = _population()
    wire = _bundle(registry, [_suggestion(0)], runs, verdicts, _members(1))
    fitted, dropped = _fit_bundle(wire, registry, budget=10 ** 7)
    assert dropped == 0
    assert fitted is wire


def test_a_trimmed_bundle_still_says_how_many_suggestions_there_were(registry):
    """The trace of the trim, on the item rather than only in the log.

    `_trim_to_budget` prints what it dropped, which reaches an operator tailing the worker and no
    reader of the surface. `suggestionsTotal` is carried through the rebuild untouched, so
    ``len(suggestions) < suggestionsTotal`` is the whole statement — the same shape `runsTotal` gives
    the per-actor bundle. Latent today at 29 KB of a 350 KB budget, and the store only grows.
    """
    runs, verdicts = _population()
    suggestions = [_suggestion(i, padding=4000) for i in range(3)]
    members = _members(3)
    wire = _bundle(registry, suggestions, runs, verdicts, members)
    assert wire["suggestionsTotal"] == 3

    trimmed, dropped = _fit_bundle(wire, registry, budget=9000, runs_by_id=runs,
                                   verdicts_by_id=verdicts, members_by_suggestion=members)

    assert dropped > 0, "the fixture must actually force a trim"
    assert trimmed["suggestionsTotal"] == 3, (
        "re-deriving the total from the survivors would describe the trimmed list as the whole set")
    assert len(trimmed["suggestions"]) == 3 - dropped < trimmed["suggestionsTotal"]


def test_a_trimmed_bundle_keeps_its_scorecard_and_its_component_health(registry):
    """Two things a trim must not quietly take with it.

    `scorecard` is population-level and fixed size, so it stays complete however many
    suggestions are dropped — that is what makes it the thing a metrics row can rely on. And the
    re-derived `components` must keep `health` for the survivors: without the population handed
    back in, every row would come back `None` and a trim would silently downgrade "measured" to
    "unknown".
    """
    runs, verdicts = _population()
    suggestions = [_suggestion(i, padding=4000) for i in range(3)]
    members = _members(3)
    wire = _bundle(registry, suggestions, runs, verdicts, members)

    trimmed, dropped = _fit_bundle(wire, registry, budget=9000, runs_by_id=runs,
                                   verdicts_by_id=verdicts, members_by_suggestion=members)

    assert dropped > 0 and len(trimmed["suggestions"]) < len(suggestions)
    assert len(json.dumps(trimmed)) <= 9000
    assert trimmed["scorecard"] == wire["scorecard"]
    assert trimmed["scorecard"]["population"]["total"] == 3
    # Same rule for `waste`: counts and per-kind totals over a closed set of kinds, so it cannot
    # grow with the population and has no rung on the trim ladder.
    assert trimmed["waste"] == wire["waste"]
    row = next(r for section in trimmed["components"]["yours"]
               for r in section["components"] if r["componentId"] == "editable-persona")
    assert row["health"] is not None, "a trim must not turn a measurement into 'unknown'"
    assert row["health"]["runs"] == len(trimmed["suggestions"])


def test_a_trim_without_the_population_reports_health_as_unknown_never_as_zero(registry):
    """The degraded case, pinned so it degrades the RIGHT way: no population handed in — and no
    member map, which is the other half of a measurement now — means `health` is `None` (unknown),
    never `{"runs": 0}` (measured and empty)."""
    runs, verdicts = _population()
    suggestions = [_suggestion(i, padding=4000) for i in range(3)]
    wire = _bundle(registry, suggestions, runs, verdicts, _members(3))

    trimmed, dropped = _fit_bundle(wire, registry, budget=9000)

    row = next(r for section in trimmed["components"]["yours"]
               for r in section["components"] if r["componentId"] == "editable-persona")
    assert row["health"] is None


# --- the per-actor item -----------------------------------------------------
def _day(date: str, runs=1, tokens=100, failed=0, unscored=0):
    """One per-actor day row as `dto._day_rows` builds one.

    ``pass`` is whatever ``runs`` the other two outcomes leave, because the three PARTITION ``runs``
    on every row the mapper writes and a fixture that broke that would be testing a row the producer
    cannot emit. There is no ``passed`` parameter for the same reason.
    """
    return {"date": date, "runs": runs, "pass": runs - failed - unscored, "fail": failed,
            "unscored": unscored, "tokens": tokens, "llmCalls": 2, "latencyMs": 30,
            "cost": 0.5, "costTokensPriced": tokens, "costTokensUnpriced": 0,
            "byCategory": {"input": 1, "output": 9, "cacheRead": 80, "cacheWrite": 10},
            "models": ["global.anthropic.claude-opus-4-8"],
            "coverage": {"total": runs, "tokens": runs, "llmCalls": runs, "latencyMs": runs,
                         "cost": runs, "costTokensPriced": runs, "costTokensUnpriced": runs,
                         "byCategory": {"input": runs, "output": runs, "cacheRead": runs,
                                        "cacheWrite": runs}}}


def _actor_bundle(run_count=3, padding=0, days=0, rolled=None):
    """A per-actor bundle shaped as `dto.actor_bundles` builds one, newest run first.

    ``days`` seeds the OTHER list that now competes for the same item — the per-day rows, oldest
    first. ``rolled`` is the bucket a previous run's evictions already folded into, which the fit has
    to keep extending rather than replacing.
    """
    bundle = {
        "runs": [{"runId": f"r{i}", "workloadClass": "conversational",
                  "verdict": {"overall": "FAIL", "reason": "gating: results_delivered:FAIL"},
                  # Bulk on the judge's sentence, so a small budget forces the trim without
                  # needing a realistic 350 KB fixture.
                  "tiers": {"delivery": {"explanation": "x" * padding}}}
                 for i in range(run_count)],
        "runsTotal": run_count,
        "evidence": {"s0": {"shapeFacets": ["fanout_parent_ran_early"],
                            "memberRunIds": ["r0"], "rootCauseHypo": "sg2 before sg1"}},
        "waste": waste_to_wire(None),
        "patterns": {"admitted": [], "residue": [], "evidenceGaps": [], "environment": [],
                     "unattributed": []},
    }
    if days:
        bundle[_HISTORY_KEY] = [_day(f"2026-08-{day:02d}") for day in range(1, days + 1)]
        bundle[_ROLLED_UP_KEY] = rolled or dict(_EMPTY_ROLLED_UP)
        bundle[_HISTORY_FIRST_DATE_KEY] = bundle[_HISTORY_KEY][0]["date"]
    return bundle


def test_the_actor_bundle_guard_names_its_own_required_keys():
    """A different contract from the global bundle's, so it has its own key set — but the same
    guard function, because "refuse a bundle missing a top-level key" is one rule.

    Every key is derivable for any actor that has a bundle at all, so a missing one is a producer
    bug. Losing `runs` or `waste` would render "you have no runs" / "nothing was wasted" as if
    measured.
    """
    assert set(_REQUIRED_ACTOR_BUNDLE_KEYS) == {"runs", "runsTotal", "evidence", "waste",
                                                "patterns"}
    _require_bundle_keys(_actor_bundle(), _REQUIRED_ACTOR_BUNDLE_KEYS)   # must not raise
    for missing in _REQUIRED_ACTOR_BUNDLE_KEYS:
        short = {k: v for k, v in _actor_bundle().items() if k != missing}
        with pytest.raises(RuntimeError, match=missing):
            _require_bundle_keys(short, _REQUIRED_ACTOR_BUNDLE_KEYS)
    # ...and the two contracts are not interchangeable: an actor bundle is not a global one.
    with pytest.raises(RuntimeError, match="suggestions"):
        _require_bundle_keys(_actor_bundle())


def test_an_actor_bundle_that_already_fits_is_returned_untouched():
    bundle = _actor_bundle(days=3)
    fitted, runs_dropped, days_rolled = _fit_actor_bundle(bundle, budget=10 ** 7)
    assert (runs_dropped, days_rolled) == (0, 0) and fitted is bundle


def test_a_trimmed_actor_bundle_drops_the_oldest_runs_and_keeps_its_true_count():
    """Runs are the FIRST rung on this ladder. They arrive newest-first, so the tail dropped is the
    oldest — and `runsTotal` still says how many the actor really had, or a trim would read as a
    smaller population."""
    bundle = _actor_bundle(run_count=4, padding=3000)
    trimmed, dropped, days_rolled = _fit_actor_bundle(bundle, budget=8000,
                                                      actor_id="6f1c2f6e-1111")

    assert dropped > 0 and len(trimmed["runs"]) < 4
    assert days_rolled == 0, "this bundle has no day rows to roll up"
    assert len(json.dumps(trimmed)) <= 8000
    assert trimmed["runsTotal"] == 4
    # The tail went, not the head: r0 is the newest and must survive to the last rung.
    assert [row["runId"] for row in trimmed["runs"]] == [f"r{i}"
                                                        for i in range(len(trimmed["runs"]))]
    # Everything that is NOT runs survives a trim whole — none of it grows with traffic.
    for key in ("evidence", "waste", "patterns", "runsTotal"):
        assert trimmed[key] == bundle[key]


def test_the_run_rows_yield_before_a_single_day_row_does():
    """THE ORDER OF THE LADDER, and it is not arbitrary.

    A run row is evidence about a session — a judge's sentence, a window, a verdict — and the next
    harvest recomputes it. A day row is the only copy of a past day: the harvest window is 24h and
    `aws/spans` retains 30 days, so a day this producer drops can never be recovered by any later run.
    So every run row goes before the first day row does, and this asserts the ordering directly rather
    than inferring it from a size that happens to work out.
    """
    bundle = _actor_bundle(run_count=4, padding=3000, days=5)
    # A budget that the run rows alone cannot fit inside, but that the day rows comfortably can.
    trimmed, dropped, days_rolled = _fit_actor_bundle(bundle, budget=4000)

    assert dropped == 4 and trimmed["runs"] == [], "every run row gave way"
    assert days_rolled == 0, "...and not one day row had to"
    assert [r["date"] for r in trimmed[_HISTORY_KEY]] == [r["date"] for r in bundle[_HISTORY_KEY]]
    assert trimmed[_ROLLED_UP_KEY] == _EMPTY_ROLLED_UP
    assert trimmed["runsTotal"] == 4, "the true request count survives an empty runs list"


def test_an_exhausted_first_rung_is_not_reported_as_an_emergency_while_a_second_remains(capsys):
    """A MISREPORTED COUNTER, found on real data and fixed here.

    `_trim_to_budget`'s ERROR branch claims the bundle's fixed part alone exceeds the budget, which
    tells an operator the write is doomed. Measured on a real 70-request bundle at a 2,000-byte
    budget: emptying the run rows left 2,935 bytes, that branch declared the emergency, and then the
    day-row rung fitted the item at 1,737. The emergency is a claim about the WHOLE ladder, so only
    its last rung may make it.
    """
    bundle = _actor_bundle(run_count=2, padding=800, days=6)
    # Above the bundle's fixed part (1,108 bytes with both lists empty) and below the run rows, so the
    # first rung empties and still cannot fit while the second one can.
    fitted, dropped, rolled = _fit_actor_bundle(bundle, budget=1900, actor_id="6f1c2f6e-1111")

    out = capsys.readouterr().out
    assert dropped == 2 and rolled > 0, "both rungs must have been used, or this proves nothing"
    assert len(json.dumps(fitted)) <= 1900, "...and the item DID fit, so nothing was an emergency"
    assert "ERROR" not in out, out
    assert "trying the next rung" in out


def test_a_day_row_the_byte_trim_evicts_is_ROLLED_UP_and_not_lost():
    """The second rung, and the property that makes it safe to have one at all.

    Once the run rows are gone the day rows are the only thing left to give, and dropping one would
    silently subtract that day from every cumulative figure — a wrong total nothing downstream can
    detect. So an evicted day is FOLDED into the bucket: its resolution goes, its figures do not.
    """
    bundle = _actor_bundle(run_count=1, padding=0, days=6)
    before = sum(r["tokens"] for r in bundle[_HISTORY_KEY])
    # Small enough that the day rows have to give way after the runs have.
    trimmed, dropped, days_rolled = _fit_actor_bundle(bundle, budget=1600)

    assert days_rolled > 0, "the fixture must actually force the second rung"
    assert len(trimmed[_HISTORY_KEY]) == 6 - days_rolled
    rolled = trimmed[_ROLLED_UP_KEY]
    assert rolled["days"] == days_rolled
    # The whole point: rolled-up + what is left still totals what went in.
    assert rolled["tokens"] + sum(r["tokens"] for r in trimmed[_HISTORY_KEY]) == before
    # And the caption still names the real first day, not the oldest row that survived.
    assert trimmed[_HISTORY_FIRST_DATE_KEY] == "2026-08-01"
    assert rolled["fromDate"] == "2026-08-01"
    assert len(json.dumps(trimmed)) <= 1600


def test_an_actor_item_is_keyed_on_that_actor_and_a_rewrite_is_idempotent():
    """The producer overwrites on every run, so the write has to be a replace and not an append.
    `build_item` is the SAME builder the global item uses, with the partition as a parameter — so
    the two cannot drift about how a bundle is stamped."""
    bundle = _actor_bundle()
    stamp = dict(created_at="2026-08-25T04:00:00Z", judge_config_hash="h", code_version="deadbee")
    first = _actor_item("6f1c2f6e-1111", bundle, **stamp)
    second = _actor_item("6f1c2f6e-1111", bundle, **stamp)
    globally = build_item(bundle=bundle, **stamp)

    assert first["actorId"] == "6f1c2f6e-1111"
    # The SAME sort key as the global item: the partition is what scopes a read, not the sk.
    assert first["sk"] == SNAPSHOT_SK == globally["sk"]
    assert globally["actorId"] == SYSTEM_ACTOR       # the default is still the shared partition
    assert first == second, "a re-run must replace the item, not produce a different one"
    assert set(first) == set(globally), "one stamp shape for both items"


def test_an_actor_item_refuses_the_reserved_partition_and_an_empty_id():
    """The reservation is not self-enforcing, and its old justification was false: 6 of the 14
    actors in the staging conversation memory are not UUIDs (`EXT_…` principals, `local-dev-user`,
    `slack-demo`). An actor id equal to the reserved name would land on the shared bundle's own key
    and replace it for everybody, so the write is refused rather than the id space trusted."""
    stamp = dict(created_at="2026-08-25T04:00:00Z", judge_config_hash="h", code_version="deadbee")
    with pytest.raises(RuntimeError, match="reserved partition"):
        _actor_item(SYSTEM_ACTOR, _actor_bundle(), **stamp)
    with pytest.raises(RuntimeError, match="no actor id"):
        _actor_item("", _actor_bundle(), **stamp)


# --- the third route: a bundle hand-fed from a file --------------------------
def test_the_seeder_refuses_a_bundle_carrying_a_withheld_field(tmp_path):
    """`seed_snapshot` reads a bundle from a FILE the operator names, assembled by whatever code was
    current when it was written — a third way onto the shared item, and one neither producer path
    guards.

    It REFUSES rather than scrubs, and that is the decision: the two producer paths scrub because
    they cannot ask anyone, while here rewriting the file silently would publish a bundle whose
    other blocks are also from that older shape, and would hide from the operator that the file they
    chose is stale. Regenerating is one command, which the message names.
    """
    stale = {
        "suggestions": [{"suggestionId": "s1",
                         "pattern": {"patternId": "p", "memberRunIds": ["bc42dea7"],
                                     "rootCauseHypo": "failed: ['sg5_index']"}}],
        "components": {}, "patterns": {"residue": [{"runId": "e2cb300b", "refinementLog": []}]},
    }
    path = tmp_path / "improve-wire.json"
    path.write_text(json.dumps(stale))
    with pytest.raises(ValueError) as exc:
        load_bundle(str(path))
    message = str(exc.value)
    # It names every offending field, so the operator can see WHY rather than being told to retry.
    for field in ("memberRunIds", "rootCauseHypo", "runId", "refinementLog"):
        assert field in message
    assert "mine_local --emit-wire" in message

    # A bundle carrying none of them loads, and the three-key check still applies underneath.
    clean = {"suggestions": [{"suggestionId": "s1",
                              "pattern": {"patternId": "p", "runCount": 1, "passHits": 0}}],
             "components": {}, "patterns": {"residue": [{"passHits": 1, "reason": "why"}]}}
    path.write_text(json.dumps(clean))
    assert load_bundle(str(path))["suggestions"][0]["suggestionId"] == "s1"
    path.write_text(json.dumps({"suggestions": [], "components": {}}))
    with pytest.raises(ValueError, match="patterns"):
        load_bundle(str(path))


# --- the rolling daily history ----------------------------------------------
def _reject_floats(node, path: str = "") -> None:
    """What the DynamoDB resource layer does to a bare ``float``, and the reason `_decimalize` is
    load-bearing rather than decorative."""
    where = path or "<root>"
    if isinstance(node, float):
        raise TypeError(f"Float types are not supported. Use Decimal types instead (at {where}).")
    if isinstance(node, dict):
        for key, value in node.items():
            _reject_floats(value, f"{where}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _reject_floats(value, f"{where}[{index}]")


def _as_stored(value):
    """Numbers as the resource layer HANDS THEM BACK: every one a ``Decimal``, integers included.

    This is what makes a carried-forward history arrive as Decimal on the next run, so a fake that
    returned plain ints and floats would leave that entire path untested.
    """
    if isinstance(value, bool):
        return value                      # bool is an int subclass; DynamoDB stores it as BOOL
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {key: _as_stored(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_as_stored(inner) for inner in value]
    return value


class _StrictTable:
    """In-memory ``Table`` enforcing the three production behaviours these tests depend on.

    A fake looser than the real API hides total breakage, so this one refuses what the resource layer
    refuses and returns what it returns:

    1. ``put_item`` rejects a ``float`` anywhere in the item, as the resource layer does.
    2. ``put_item`` rejects an item over DynamoDB's hard 400 KB cap, so a trim that did not actually
       trim fails here rather than passing.
    3. ``get_item`` returns every number as ``Decimal``.

    What it deliberately does NOT reproduce: DynamoDB sizes an item from its attribute names plus a
    type-dependent encoding of each value, and this approximates that with the length of the JSON
    encoding — enough to catch an item that is far too big, not a byte-exact oracle. It also models
    no conditional expressions, no throttling and no eventual consistency.
    """

    def __init__(self, items=None) -> None:
        self._items: dict = {}
        self.get_calls: list = []
        self.put_calls: list = []
        for item in items or []:
            self.put_item(Item=item)

    def get_item(self, Key=None, **kw):
        assert Key is not None and set(Key) == {"actorId", "sk"}, Key
        assert not kw, f"unexpected get_item kwargs: {kw}"
        self.get_calls.append(dict(Key))
        found = self._items.get((Key["actorId"], Key["sk"]))
        return {"Item": found} if found is not None else {}

    def put_item(self, Item=None, **kw):
        assert Item is not None and "actorId" in Item and "sk" in Item, Item
        assert not kw, f"unexpected put_item kwargs: {kw}"
        _reject_floats(Item)
        size = len(json.dumps(Item, default=str))
        if size > _DDB_ITEM_LIMIT_BYTES:
            raise ValueError(f"Item size has exceeded the maximum allowed size ({size} bytes)")
        self.put_calls.append(Item)
        self._items[(Item["actorId"], Item["sk"])] = _as_stored(Item)


def _row(date: str, population: int, pass_rate=0.5) -> dict:
    """A prior population history row as an earlier run would have written one.

    Every spend term unmeasured, which is the widest a row gets: the sentinel string is longer than
    most of the numbers it stands in for, so the byte ceiling is measured against the worst case.
    """
    return {"date": date, "population": population, "passRate": pass_rate,
            "tokens": "UNMEASURED", "llmCalls": "UNMEASURED", "latencyMs": "UNMEASURED",
            "cost": "UNMEASURED", "costTokensPriced": "UNMEASURED",
            "costTokensUnpriced": "UNMEASURED", "input": "UNMEASURED",
            "cacheRead": "UNMEASURED", "cacheWrite": "UNMEASURED", "output": "UNMEASURED",
            "models": ["global.anthropic.claude-opus-4-8"], "runsWithWaste": 0,
            "coverage": {"total": population, "tokens": 0, "llmCalls": 0, "latencyMs": 0,
                         "cost": 0, "costTokensPriced": 0, "costTokensUnpriced": 0,
                         "byCategory": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}}


def _prior(rows, rolled=None) -> dict:
    """A prior history BLOCK as `_read_history` returns one: the rows, and the rolled-up bucket.

    The two travel together because reading either without the other is a defect — the rows alone
    would drop every evicted day from a cumulative figure, and the bucket alone would keep the total
    while losing the recent chart.
    """
    block = {_HISTORY_KEY: list(rows)}
    if rolled is not None:
        block[_ROLLED_UP_KEY] = rolled
    return block


def _snapshot_table(history=None, *, bundle_key: str = "bundle") -> _StrictTable:
    """A table holding one global SNAPSHOT item, written the way the producer writes it.

    Seeded THROUGH ``put_item`` after `_decimalize`, so the rows come back as Decimal exactly as the
    next run would find them — the fixture cannot be more convenient than reality.
    """
    bundle = {"suggestions": [], "components": {}, "patterns": {},
              "scorecard": {}, "waste": {}}
    if history is not None:
        bundle[_HISTORY_KEY] = history
    item = {"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK, bundle_key: _decimalize(bundle),
            "createdAt": "2026-08-29T04:00:00Z"}
    return _StrictTable([item])


def _wire_with_scorecard(runs, verdicts, waste=None) -> dict:
    """The two blocks `_history_row` reads, as the real mappers produce them."""
    return {"scorecard": scorecard_to_wire(list(runs.values()), list(verdicts.values())),
            "waste": waste_to_wire(waste)}


def _measured_population(count=3):
    """A population whose resources ARE recorded, so the row has real numbers to carry."""
    resources = {"latency_ms": 10, "tokens": 100, "llm_calls": 2, "output_tokens": 30,
                 "cache_read_tokens": 700, "cache_write_tokens": 5}
    runs = {f"r{i}": Run(run_id=f"r{i}", resources=dict(resources)) for i in range(count)}
    verdicts = {f"r{i}": _verdict(f"r{i}") for i in range(count)}
    return runs, verdicts


def test_the_history_sits_inside_the_bundle_where_the_read_route_can_reach_it():
    """Placement, not presence — and the reason it is not a free choice.

    The read route serves ``snapshot["bundle"]`` and nothing else, so the provenance attributes
    `build_item` puts BESIDE the bundle (`createdAt`, `judgeConfigHash`, `codeVersion`) never reach a
    consumer. A history stored as an item-level sibling would be written every day and be invisible
    forever. Checked against the route's OWN transforms rather than against a restatement of them.
    """
    runs, verdicts = _population()
    wire, _ = _with_history(_wire_with_scorecard(runs, verdicts),
                            _prior([_row("2026-08-28", 9)]), date="2026-08-29")
    item = build_item(bundle=_decimalize(wire), created_at="2026-08-29T04:00:00Z",
                      judge_config_hash="h", code_version="deadbee")

    # Inside, and NOT beside: a sibling would show up as a top-level item attribute.
    assert _HISTORY_KEY in item["bundle"]
    assert _HISTORY_KEY not in set(item) - {"bundle"}

    served = decode_decimals(with_actor_bundle(merge_overlay(item["bundle"], {}), None))
    assert [r["date"] for r in served[_HISTORY_KEY]] == ["2026-08-28", "2026-08-29"]
    assert served[_HISTORY_KEY][0]["population"] == 9


def test_a_first_run_with_no_prior_item_starts_a_history_of_one_row():
    """A fresh environment must produce a history, not an error and not an empty block."""
    table = _StrictTable()
    runs, verdicts = _population()
    prior = _read_history(table)
    assert prior == {}

    wire, moved = _with_history(_wire_with_scorecard(runs, verdicts), prior, date="2026-08-30")
    assert moved == {"rows": 1, "rolled_up": 0, "refused": 0}
    assert [r["date"] for r in wire[_HISTORY_KEY]] == ["2026-08-30"]
    assert wire[_HISTORY_KEY][0]["population"] == 3
    # A first run has nothing rolled up, and says so as a measured zero rather than by omission —
    # `days: 0` is "no day has been evicted", which is what makes the total readable as complete.
    assert wire[_ROLLED_UP_KEY] == _EMPTY_ROLLED_UP
    assert wire[_HISTORY_FIRST_DATE_KEY] == "2026-08-30"


def test_the_prior_items_rows_are_read_back_and_carried_forward():
    """THE defect this exists for: the SNAPSHOT write is an unconditional PutItem, so a history that
    is not read back off the item first is destroyed by the very next run.

    The seeded rows are asserted BY VALUE, both out of `_read_history` and out of the finished
    bundle. A probe that only checked "the output has a history" would pass over an input that could
    never have exhibited the failure, since this run appends a row regardless.
    """
    seeded = [_row("2026-08-27", 11), _row("2026-08-28", 22), _row("2026-08-29", 33)]
    table = _snapshot_table(seeded)

    prior = _read_history(table)
    # The fixture loaded: a seed that silently failed would look identical to a wiped history.
    assert [(r["date"], int(r["population"])) for r in prior[_HISTORY_KEY]] == [
        ("2026-08-27", 11), ("2026-08-28", 22), ("2026-08-29", 33)]

    runs, verdicts = _population()
    wire, moved = _with_history(_wire_with_scorecard(runs, verdicts), prior, date="2026-08-30")

    assert moved["rolled_up"] == 0
    assert [(r["date"], int(r["population"])) for r in wire[_HISTORY_KEY]] == [
        ("2026-08-27", 11), ("2026-08-28", 22), ("2026-08-29", 33), ("2026-08-30", 3)]


def test_every_shape_of_missing_history_starts_fresh_instead_of_raising(capsys):
    """Four ways the prior item can carry no usable history, all of them ordinary.

    A first run, an item written before this block existed, and an item hand-seeded from a file by
    `seed_snapshot` (whose bundles carry none) all land here. Only the schema surprise says anything,
    and even that starts fresh rather than raising — nothing but the producer writes this item, so
    refusing would wedge every future run over a value only the producer could repair.
    """
    assert _read_history(_StrictTable()) == {}                              # no item at all
    assert _read_history(_snapshot_table(bundle_key="payload")) == {}       # item, no bundle
    assert _read_history(_snapshot_table()) == {}                          # bundle, no history
    capsys.readouterr()

    wrong_type = _StrictTable([{"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK,
                                "bundle": {_HISTORY_KEY: {"2026-08-29": 3}}}])
    assert _read_history(wrong_type) == {}
    assert "not a list" in capsys.readouterr().out


def test_an_unmeasured_term_is_carried_as_the_sentinel_and_never_as_zero():
    """The row must not fabricate a number, because on the deployed population most spend terms are
    unmeasured and a trend drawn through zeros reads as "free" or "instant".

    Every field shares ONE spelling for "not measured", including ``passRate`` — which reaches the
    wire as ``None`` rather than as the sentinel, because it has no denominator when nothing was
    scored, and ``null`` styles like any other empty cell — and ``models``, whose empty roster is the
    sentinel and never ``[]`` for the same reason `dto._run_models` gives.

    The measured half is what stops the sentinel half from being satisfied by a row that always says
    ``"UNMEASURED"``.
    """
    empty = _history_row(scorecard_to_wire([], []), waste_to_wire(None), date="2026-08-30")
    assert set(empty) == {"date", "population", "passRate", "tokens", "llmCalls", "latencyMs",
                          "cost", "costTokensPriced", "costTokensUnpriced", "input", "cacheRead",
                          "cacheWrite", "output", "models", "runsWithWaste", "coverage"}
    unmeasurable = ("passRate", "tokens", "llmCalls", "latencyMs", "cost", "input", "cacheRead",
                    "cacheWrite", "output", "models")
    for field in unmeasurable:
        assert empty[field] == "UNMEASURED", field
    assert 0 not in [empty[field] for field in unmeasurable]
    assert None not in empty.values()
    # Counts, where 0 IS the measurement — we looked and there were none. The two cost-token counts
    # are in this group and not the one above, which is `dto._measured_total`'s rule rather than a
    # concession: they are the DENOMINATORS a reader compares `cost` against, so a sentinel there
    # would leave the sum with nothing to be read against. `cost` itself is unmeasured here.
    assert empty["population"] == 0 and empty["runsWithWaste"] == 0
    assert empty["costTokensPriced"] == 0 and empty["costTokensUnpriced"] == 0
    # `coverage` may NEVER hold a sentinel: it is what a lenient sum is read against, so an absence
    # there would leave the sum with no denominator at all. `0 of 0` is the honest empty answer.
    assert empty["coverage"] == {"total": 0, "tokens": 0, "llmCalls": 0, "latencyMs": 0, "cost": 0,
                                 "costTokensPriced": 0, "costTokensUnpriced": 0,
                                 "byCategory": {"input": 0, "output": 0, "cacheRead": 0,
                                                "cacheWrite": 0}}

    runs, verdicts = _measured_population()
    measured = _history_row(
        scorecard_to_wire(list(runs.values()), list(verdicts.values())),
        waste_to_wire({"findings": [{"run_id": "r0", "charged": ["c1"], "wall_ms": 5}],
                       "runs_examined": 3}),
        date="2026-08-30")
    assert measured == {"date": "2026-08-30", "population": 3, "passRate": 1.0,
                        "tokens": 300, "llmCalls": 6, "latencyMs": 30,
                        "cost": "UNMEASURED", "costTokensPriced": 0, "costTokensUnpriced": 0,
                        "input": "UNMEASURED",
                        "cacheRead": 2100, "cacheWrite": 15, "output": 90,
                        "models": "UNMEASURED", "runsWithWaste": 1,
                        # Copied off the scorecard's own per-term pair, not recounted — which is why
                        # `input` reads 0 measured beside a 3-of-3 `cacheRead`: these runs record no
                        # uncached input at all, and coverage differing BETWEEN terms is exactly why
                        # this is per term rather than one number for the block.
                        "coverage": {"total": 3, "tokens": 3, "llmCalls": 3, "latencyMs": 3,
                                     "cost": 0, "costTokensPriced": 0, "costTokensUnpriced": 0,
                                     "byCategory": {"input": 0, "output": 3, "cacheRead": 3,
                                                    "cacheWrite": 3}}}


def test_a_short_or_absent_scorecard_block_yields_sentinels_never_zeros():
    """The case where a fabricated zero would actually get written.

    `_require_bundle_keys` checks that `scorecard` and `waste` are PRESENT, never what is inside
    them, so a bundle assembled by older producer code can reach here with a term missing or with a
    term carrying no ``sum``. A ``.get("sum", 0)`` default there is indistinguishable from a real
    measurement of zero, and what it would report as free or instant is the whole population's spend.
    """
    bare = _history_row({}, {}, date="2026-08-30")
    assert bare == {"date": "2026-08-30", "population": "UNMEASURED", "passRate": "UNMEASURED",
                    "tokens": "UNMEASURED", "llmCalls": "UNMEASURED", "latencyMs": "UNMEASURED",
                    "cost": "UNMEASURED", "costTokensPriced": "UNMEASURED",
                    "costTokensUnpriced": "UNMEASURED", "input": "UNMEASURED",
                    "cacheRead": "UNMEASURED", "cacheWrite": "UNMEASURED", "output": "UNMEASURED",
                    "models": "UNMEASURED", "runsWithWaste": "UNMEASURED",
                    "coverage": {"total": 0, "tokens": 0, "llmCalls": 0, "latencyMs": 0, "cost": 0,
                                 "costTokensPriced": 0, "costTokensUnpriced": 0,
                                 "byCategory": {"input": 0, "output": 0, "cacheRead": 0,
                                                "cacheWrite": 0}}}

    # A term present but carrying no `sum`, and a spend block with no `byCategory` at all.
    partial = _history_row({"population": {"total": 4}, "passRate": 0.5,
                            "spend": {"tokens": {}, "llmCalls": {"measuredOf": 0}}},
                           {"runsWithWaste": 0}, date="2026-08-30")
    for field in ("tokens", "llmCalls", "latencyMs", "cost", "costTokensPriced",
                  "costTokensUnpriced", "input", "cacheRead", "cacheWrite", "output", "models"):
        assert partial[field] == "UNMEASURED", field
    # ...while what IS present is carried, so this is not satisfied by a row of pure sentinels.
    assert partial["population"] == 4 and partial["passRate"] == 0.5
    assert partial["runsWithWaste"] == 0, "a real count of zero is a measurement, not an absence"


def test_a_prior_row_with_no_usable_date_is_dropped_and_said_out_loud(capsys):
    """A row that cannot be placed on a time axis can be neither ordered nor deduplicated, so both
    the tail slice and the one-row-per-day rule would be reasoning about a row they cannot read.
    Dropped rather than guessed at, and reported, because it should not happen."""
    prior = [_row("2026-08-28", 7), {"population": 3}, "not-a-row",
             {**_row("2026-08-29", 8), "date": ""}]
    runs, verdicts = _population()
    wire, moved = _with_history(_wire_with_scorecard(runs, verdicts), _prior(prior),
                                date="2026-08-30")

    assert [r["date"] for r in wire[_HISTORY_KEY]] == ["2026-08-28", "2026-08-30"]
    assert moved["rolled_up"] == 0, "three unusable rows are not a bound discarding anything"
    assert "dropped 3 history row(s) with no usable date" in capsys.readouterr().out


def test_a_second_run_on_the_same_day_replaces_that_days_row():
    """One row per day, keyed on ``date``, and the later run wins.

    Every run recomputes the whole population from the same harvest window, so two runs on one day
    are two measurements of ONE interval; appending would make a manual retry silently change the
    shape of the trend, and would stop a retention count in rows meaning a span in days.
    """
    prior = [_row("2026-08-29", 11), _row("2026-08-30", 1)]
    runs, verdicts = _population()
    wire, moved = _with_history(_wire_with_scorecard(runs, verdicts), _prior(prior),
                                date="2026-08-30")

    rows = wire[_HISTORY_KEY]
    assert moved["rolled_up"] == 0
    assert [r["date"] for r in rows] == ["2026-08-29", "2026-08-30"]
    assert [r["date"] for r in rows].count("2026-08-30") == 1
    assert rows[-1]["population"] == 3, "the later run's measurement, not the earlier one's"
    assert rows[0]["population"] == 11, "an earlier day is untouched by a same-day rewrite"


def test_retention_keeps_the_newest_rows_drops_the_oldest_and_is_env_overridable(monkeypatch):
    """The bound is a named constant read at call time, and the tail is what survives.

    ``IMPROVE_HISTORY_ROWS=0`` is pinned because it is a footgun that does the opposite of what it
    reads: retention is a tail slice, and ``rows[-0:]`` is the whole list.
    """
    prior = [_row(f"2026-07-{day:02d}", day) for day in range(1, 32)]
    runs, verdicts = _population()
    wire = _wire_with_scorecard(runs, verdicts)

    kept, moved = _with_history(wire, _prior(prior), date="2026-08-30")
    rows = kept[_HISTORY_KEY]
    assert len(rows) == _HISTORY_ROWS
    assert moved["rolled_up"] == len(prior) + 1 - _HISTORY_ROWS
    assert rows[-1]["date"] == "2026-08-30", "today's row is never the one dropped"
    assert rows[0]["date"] == "2026-07-03", "the oldest went, not the newest"
    # Retention moves a day out of the CHART, never out of the TOTAL. The bucket now holds the two
    # oldest days, still names the real first one, and the caption follows it rather than the horizon.
    rolled = kept[_ROLLED_UP_KEY]
    assert rolled["days"] == 2 and rolled["fromDate"] == "2026-07-01"
    assert rolled["population"] == 1 + 2, "the evicted days' figures are summed, not discarded"
    assert kept[_HISTORY_FIRST_DATE_KEY] == "2026-07-01", "the real first date, not the horizon"

    monkeypatch.setenv("IMPROVE_HISTORY_ROWS", "5")
    short, _ = _with_history(wire, _prior(prior), date="2026-08-30")
    assert [r["date"] for r in short[_HISTORY_KEY]] == [
        "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-30"]

    monkeypatch.setenv("IMPROVE_HISTORY_ROWS", "0")
    floored, _ = _with_history(wire, _prior(prior), date="2026-08-30")
    assert [r["date"] for r in floored[_HISTORY_KEY]] == ["2026-08-30"], \
        "0 must mean one row, not every row"


def test_an_out_of_order_prior_history_is_sorted_before_the_tail_slice(monkeypatch):
    """What licenses the tail slice. "Keep the newest N" is only "drop the oldest" if the newest are
    at the end, so an item whose rows arrived out of order must be ordered before it is cut — or the
    slice silently keeps whichever rows happened to be last in the list."""
    monkeypatch.setenv("IMPROVE_HISTORY_ROWS", "3")
    prior = [_row("2026-08-27", 1), _row("2026-08-25", 2), _row("2026-08-29", 3),
             _row("2026-08-26", 4)]
    runs, verdicts = _population()
    wire, _ = _with_history(_wire_with_scorecard(runs, verdicts), _prior(prior), date="2026-08-30")

    assert [r["date"] for r in wire[_HISTORY_KEY]] == [
        "2026-08-27", "2026-08-29", "2026-08-30"]


# --- the per-actor day rows: the cumulative half of the surface -------------
#
# The DEFECT these exist for: an actor's bundle is rebuilt from a 24h harvest and PutItem'd over the
# previous one every night, so a reader who ran nothing yesterday had no figures at all — and while
# `population_size` was still landing ON the cap, even yesterday was partial. A cumulative
# figure therefore needs a durable home, and the item is the only one available: `IMPROVE_HARVEST_DIR`
# is `/tmp`, which does not survive between invocations, which is exactly the boundary these rows have
# to cross.
def test_an_actors_prior_day_rows_are_read_back_off_their_own_item():
    """The per-actor half of the carry-forward, and it is keyed on the ACTOR's partition.

    Reading the global item's history here would give every actor the platform's rows, and reading
    nothing would wipe theirs on every run — which is the state this closes.
    """
    seeded = {"actorId": "6f1c2f6e-1111", "sk": SNAPSHOT_SK, "createdAt": "2026-08-29T04:00:00Z",
              "bundle": _decimalize({_HISTORY_KEY: [_day("2026-08-28", runs=2, tokens=500)],
                                     _ROLLED_UP_KEY: dict(_EMPTY_ROLLED_UP)})}
    other = {"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK, "createdAt": "2026-08-29T04:00:00Z",
             "bundle": _decimalize({_HISTORY_KEY: [_row("2026-08-28", 99)]})}
    table = _StrictTable([seeded, other])

    prior = _read_history(table, "6f1c2f6e-1111")
    assert [r["date"] for r in prior[_HISTORY_KEY]] == ["2026-08-28"]
    assert prior[_HISTORY_KEY][0]["tokens"] == 500, "the ACTOR's row, not the platform's"

    today = _actor_bundle(run_count=1)
    today[_HISTORY_KEY] = [_day("2026-08-29", runs=1, tokens=70)]
    merged, moved = _with_actor_history(today, prior)

    assert [r["date"] for r in merged[_HISTORY_KEY]] == ["2026-08-28", "2026-08-29"]
    assert [r["tokens"] for r in merged[_HISTORY_KEY]] == [500, 70]
    assert moved == {"rows": 2, "rolled_up": 0, "refused": 0}
    assert merged[_HISTORY_FIRST_DATE_KEY] == "2026-08-28"


def test_a_wide_reseed_rewrites_each_date_independently_and_is_idempotent():
    """A manual 168h run covers MANY days at once, and every one of them replaces its own day.

    Three things this pins, because a cumulative store can get each of them wrong differently: the
    reseed must not MERGE the wide window into one row, must not APPEND alongside the days already
    there, and must not REFUSE because a day is already recorded. And running it twice must leave
    every figure exactly where the first run left it — which is what makes a retry safe to perform.
    """
    prior = _prior([_day("2026-08-25", runs=1, tokens=10),
                    _day("2026-08-26", runs=1, tokens=20),
                    _day("2026-08-27", runs=1, tokens=30)])
    # A wide run recomputes three of those days differently and adds two the item never had.
    wide = _actor_bundle(run_count=1)
    wide[_HISTORY_KEY] = [_day("2026-08-24", runs=4, tokens=400),
                          _day("2026-08-25", runs=5, tokens=500),
                          _day("2026-08-26", runs=6, tokens=600),
                          _day("2026-08-28", runs=8, tokens=800)]

    once, moved = _with_actor_history(wide, prior)
    assert moved["refused"] == 0, "a day inside the retained window is rewritten, never refused"
    assert [(r["date"], r["tokens"]) for r in once[_HISTORY_KEY]] == [
        ("2026-08-24", 400),   # a day EARLIER than anything the item held
        ("2026-08-25", 500),   # replaced, not merged: 500 and not 510
        ("2026-08-26", 600),   # replaced
        ("2026-08-27", 30),    # untouched — the reseed said nothing about this day
        ("2026-08-28", 800),   # new
    ]
    assert once[_HISTORY_FIRST_DATE_KEY] == "2026-08-24", "the caption follows the reseed back"

    # ...and again, from the state the first run wrote. Byte-identical is the assertion, because a
    # figure that drifts on a retry is a figure nobody can reconcile against anything.
    twice, moved_again = _with_actor_history(wide, {k: once[k] for k in
                                                   (_HISTORY_KEY, _ROLLED_UP_KEY)})
    assert twice[_HISTORY_KEY] == once[_HISTORY_KEY]
    assert twice[_ROLLED_UP_KEY] == once[_ROLLED_UP_KEY]
    assert twice[_HISTORY_FIRST_DATE_KEY] == once[_HISTORY_FIRST_DATE_KEY]
    assert moved_again == moved


def test_a_reseed_reaching_inside_the_rolled_up_bucket_is_refused_and_not_double_counted(capsys):
    """The one day a reseed CANNOT rewrite, and why refusing beats the alternative.

    A fold is lossy by construction — the bucket keeps sums, not days — so a fresh row for a day
    already inside it could only be ADDED, which inflates a lifetime total by a day it already counts.
    An inflated total is a wrong number nothing downstream can detect; a refusal is bounded, logged,
    and visible in the run summary. So it is refused.
    """
    rolled = {"days": 3, "fromDate": "2026-08-01", "toDate": "2026-08-03", "runs": 3, "tokens": 300}
    prior = _prior([_day("2026-08-10", runs=1, tokens=10)], rolled=rolled)
    wide = _actor_bundle(run_count=1)
    wide[_HISTORY_KEY] = [_day("2026-08-02", runs=9, tokens=900),   # inside the bucket
                          _day("2026-08-11", runs=1, tokens=11)]    # outside it

    merged, moved = _with_actor_history(wide, prior)

    assert moved["refused"] == 1
    assert [r["date"] for r in merged[_HISTORY_KEY]] == ["2026-08-10", "2026-08-11"]
    assert merged[_ROLLED_UP_KEY]["tokens"] == 300, "the bucket is unchanged, not increased by 900"
    assert merged[_ROLLED_UP_KEY]["days"] == 3
    assert merged[_HISTORY_FIRST_DATE_KEY] == "2026-08-01"
    assert "double-count" in capsys.readouterr().out


def test_the_first_date_is_the_real_one_and_never_the_retention_horizon(monkeypatch):
    """The caption a cumulative total is read with, and the one way to get it subtly wrong.

    A total labelled with the retention horizon overstates its coverage when the data starts later,
    and understates it when the data starts earlier — and understating is the case retention creates
    on every busy actor, because the bucket holds days the rows no longer do.
    """
    monkeypatch.setenv("IMPROVE_HISTORY_ROWS", "2")
    prior = _prior([_day("2026-08-01"), _day("2026-08-02"), _day("2026-08-03")])
    bundle = _actor_bundle(run_count=1)
    bundle[_HISTORY_KEY] = [_day("2026-08-04")]

    merged, moved = _with_actor_history(bundle, prior)

    assert [r["date"] for r in merged[_HISTORY_KEY]] == ["2026-08-03", "2026-08-04"]
    assert moved["rolled_up"] == 2
    # NOT "2026-08-03", which is the oldest row that survived retention.
    assert merged[_HISTORY_FIRST_DATE_KEY] == "2026-08-01"
    assert merged[_ROLLED_UP_KEY]["fromDate"] == "2026-08-01"
    assert merged[_ROLLED_UP_KEY]["toDate"] == "2026-08-02"


def test_a_rolled_up_bucket_totals_the_days_it_swallowed_and_never_averages_a_rate(monkeypatch):
    """What the fold does to each KIND of field, on the population row where all three kinds appear.

    Counts and totals sum. Model ids UNION, because summing them is meaningless and concatenating
    would report one model used on thirty days as thirty models. ``passRate`` is weighted by its own
    denominator, which is exact rather than approximate: each day's rate is passes/population, so the
    population-weighted mean over several days IS the rate over all of them.
    """
    monkeypatch.setenv("IMPROVE_HISTORY_ROWS", "1")
    # Two days, deliberately different sizes, so an UNWEIGHTED mean (0.75) differs from the true rate.
    wide = [{**_row("2026-08-01", 10, pass_rate=0.5), "tokens": 100,
             "models": ["model-a"]},
            {**_row("2026-08-02", 30, pass_rate=1.0), "tokens": 300,
             "models": ["model-a", "model-b"]}]
    block, moved = _merge_history(_prior(wide), [_row("2026-08-03", 1)])

    assert moved["rolled_up"] == 2
    rolled = block[_ROLLED_UP_KEY]
    assert rolled["days"] == 2 and rolled["population"] == 40
    assert rolled["tokens"] == 400, "totals sum"
    assert rolled["models"] == ["model-a", "model-b"], "ids union, first-seen order"
    # (0.5*10 + 1.0*30) / 40 = 0.875, and NOT the unweighted (0.5 + 1.0) / 2 = 0.75.
    assert rolled["passRate"] == 0.875


def test_a_fresh_row_with_no_usable_date_is_dropped_by_the_same_rule_as_a_carried_one(capsys):
    """One rule for both sides of the merge. The mapper builds fresh rows and the store hands back
    carried ones, and if the two were admitted by different rules the merge could sort a list holding
    a row it cannot order — or raise on a key it assumed was there."""
    prior = _prior([_day("2026-08-27"), {"tokens": 5}])
    fresh = [_day("2026-08-28"), {**_day("2026-08-29"), "date": ""}, {"tokens": 9}, "not-a-row"]

    block, moved = _merge_history(prior, fresh)

    assert [r["date"] for r in block[_HISTORY_KEY]] == ["2026-08-27", "2026-08-28"]
    assert moved == {"rows": 2, "rolled_up": 0, "refused": 0}, \
        "an undated row is not a bound discarding anything, and not a refusal either"
    assert "dropped 4 history row(s) with no usable date" in capsys.readouterr().out


def test_a_term_no_day_measured_stays_the_sentinel_through_the_fold():
    """A fold must not turn "nobody measured this" into a number, and must not lose a real one.

    Both halves matter and they pull opposite ways. A sum that defaulted the sentinel to ``0`` would
    report a month of unmeasured cost as free. A sum that let one unmeasured day poison the whole
    would blank a reader's all-time figure permanently — a worse answer than a figure short by a day,
    which is why the lenient rule is the one used here and in `dto._resource_sum`.
    """
    days = [{**_day("2026-08-01", tokens=100), "cost": "UNMEASURED"},
            {**_day("2026-08-02", tokens=200), "cost": 0.25},
            {**_day("2026-08-03", tokens=300), "cost": "UNMEASURED"}]
    block, _ = _merge_history({}, days, limit=1)

    rolled = block[_ROLLED_UP_KEY]
    assert rolled["tokens"] == 300, "two measured days summed"
    assert rolled["cost"] == 0.25, "the one measured day survives the two that were not"

    nothing = [{**_day("2026-08-01"), "cost": "UNMEASURED"},
               {**_day("2026-08-02"), "cost": "UNMEASURED"}]
    block, _ = _merge_history({}, nothing, limit=1)
    assert block[_ROLLED_UP_KEY]["cost"] == "UNMEASURED", "not 0, which would claim it was free"


def test_a_day_row_written_before_the_outcome_counts_keeps_them_ABSENT_through_the_carry_forward():
    """The rows already in the store, and the one way to make them lie.

    ``pass``/``fail``/``unscored`` are new, so every row written before them lacks all three, and there
    is nothing to back-fill from: `aws/spans` retains 30 days and a day row is the only copy of its day.
    Carried forward they must stay ABSENT, because a ``0`` on this row is a MEASUREMENT — the day was
    scored and nothing landed in that bucket — so three zeros would report a day of real work as a day
    with no successes, at a rate of 0%. Absent is "no outcome recorded" and renders as neither.
    """
    old = {k: v for k, v in _day("2026-08-01", runs=4).items()
           if k not in ("pass", "fail", "unscored")}
    fresh = _actor_bundle(run_count=1)
    fresh[_HISTORY_KEY] = [_day("2026-08-02", runs=3, failed=1, unscored=1)]

    merged, _ = _with_actor_history(fresh, _prior([old]))

    carried, today = merged[_HISTORY_KEY]
    assert carried["date"] == "2026-08-01" and carried["runs"] == 4, "the row itself survives whole"
    for name in ("pass", "fail", "unscored"):
        assert name not in carried, f"{name} must stay absent, never a fabricated 0"
    assert (today["pass"], today["fail"], today["unscored"]) == (1, 1, 1), \
        "...and a row this producer wrote carries all three, summing to its runs"


def test_a_rolled_up_bucket_totals_only_the_days_that_recorded_an_outcome():
    """What the fold says when the bucket spans both sides of the change, and why short beats padded.

    The counts SUM like every other count on the row — nothing in `_fold_terms` had to learn a new rule
    — and a day that never recorded them contributes to no side. So the bucket's partition comes out
    SHORT of the ``runs`` beside it, and that shortfall IS the fact: those days have no outcome
    recorded. Zero-filling them to make the three add up would fabricate a measurement.

    A bucket holding only pre-change days omits the three entirely rather than reporting zeros, which
    is the same distinction one level up.
    """
    old = {k: v for k, v in _day("2026-08-01", runs=4).items()
           if k not in ("pass", "fail", "unscored")}
    block, moved = _merge_history(
        _prior([old, _day("2026-08-02", runs=3, failed=1, unscored=1)]),
        [_day("2026-08-03", runs=2)], limit=1)

    assert moved["rolled_up"] == 2, "the fixture must actually fold both older days"
    rolled = block[_ROLLED_UP_KEY]
    assert rolled["runs"] == 7, "every folded day's requests are in the bucket"
    assert (rolled["pass"], rolled["fail"], rolled["unscored"]) == (1, 1, 1)
    assert rolled["pass"] + rolled["fail"] + rolled["unscored"] == 3 < rolled["runs"], \
        "short by the 4 requests of the day that recorded no outcome, which is the true statement"

    only_old, _ = _merge_history(_prior([old, {**old, "date": "2026-08-02"}]),
                                 [_day("2026-08-03", runs=2)], limit=1)
    for name in ("pass", "fail", "unscored"):
        assert name not in only_old[_ROLLED_UP_KEY], f"{name} absent, not 0 over 8 requests"
    assert only_old[_ROLLED_UP_KEY]["runs"] == 8


def test_a_bucket_folding_one_day_with_no_coverage_reports_NO_coverage_rather_than_a_ratio():
    """The one field a partial fold cannot report as short, so it is dropped instead.

    ``coverage`` was added to this row after the row itself, so the store holds days without it, and
    those days fold into the same bucket as the days that have it. Folding it over only the days that
    carry it shrinks the numerator and the denominator TOGETHER — ``coverage.total`` is the denominator
    — so a bucket whose spend came from 20 sessions, half of them never measured, would caption itself
    "10 of 10 sessions": a plausible, complete-looking 100% with nothing anywhere to contradict it. The
    consumer cannot catch it either, because it checks for the BLOCK and the block would be there.

    The spend beside it is untouched: those sums are lenient by design and a day that measured nothing
    is simply absent from them, which is the rule every other term on the row already follows.
    """
    old = {k: v for k, v in _row("2026-08-01", 10).items() if k != "coverage"}
    old = {**old, "tokens": 1000, "llmCalls": 50}
    measured = {**_row("2026-08-02", 10), "tokens": 1000, "llmCalls": 50,
                "coverage": {**_row("2026-08-02", 10)["coverage"], "tokens": 10, "llmCalls": 10}}

    block, moved = _merge_history(_prior([old, measured]), [_row("2026-08-03", 1)], limit=1)

    assert moved["rolled_up"] == 2, "the fixture must actually fold both older days"
    rolled = block[_ROLLED_UP_KEY]
    assert "coverage" not in rolled, \
        "a ratio over the measured half would read as complete over the whole bucket"
    assert rolled["days"] == 2 and rolled["population"] == 20
    assert (rolled["tokens"], rolled["llmCalls"]) == (2000, 100), \
        "the spend the bucket holds is unaffected — only the claim about how much of it was measured"
    assert block[_HISTORY_KEY][0]["coverage"]["tokens"] == 0, \
        "the rule is about the FOLD; a retained row keeps its own block whatever the bucket lost"


def test_a_bucket_whose_every_day_recorded_its_coverage_keeps_it_and_sums_BOTH_halves():
    """The case that must not regress: a complete fold publishes the pair, denominator included.

    Both halves sum, which is what makes a period captionable at all — the measured counts add up and
    so does the ``total`` they are out of, so the bucket carries exactly the pair one day carries.

    It also pins the participant rule. The first roll-up folds the prior bucket in as an EMPTY block, so
    a rule that counted that as a day lacking ``coverage`` would drop the block from every bucket that
    was ever built, complete or not.
    """
    days = [{**_row("2026-08-01", 10), "tokens": 1000,
             "coverage": {**_row("2026-08-01", 10)["coverage"], "tokens": 10, "cost": 4,
                          "byCategory": {"input": 10, "output": 10, "cacheRead": 2,
                                         "cacheWrite": 2}}},
            {**_row("2026-08-02", 30), "tokens": 3000,
             "coverage": {**_row("2026-08-02", 30)["coverage"], "tokens": 21, "cost": 0,
                          "byCategory": {"input": 21, "output": 21, "cacheRead": 0,
                                         "cacheWrite": 0}}}]

    block, moved = _merge_history(_prior(days), [_row("2026-08-03", 1)], limit=1)

    assert moved["rolled_up"] == 2, "the fixture must actually fold both days"
    covered = block[_ROLLED_UP_KEY]["coverage"]
    assert covered["total"] == 40, "the denominator sums across the days, like the terms do"
    assert covered["tokens"] == 31, "31 of 40 sessions, which is the honest fraction"
    assert covered["cost"] == 4, "each term keeps its own coverage; they differ per term"
    assert covered["byCategory"] == {"input": 31, "output": 31, "cacheRead": 2, "cacheWrite": 2}, \
        "a block nested INSIDE coverage folds by summation like the one holding it"
    assert block[_ROLLED_UP_KEY]["tokens"] == 4000, \
        "and the sums it captions are the sums they always were"


def test_a_partition_folds_SHORT_where_a_self_denominated_block_drops_out_ENTIRELY():
    """The two sides of the rule, in ONE fold, from one and the same missing day.

    ``byCategory`` is a partition of ``tokens`` and its denominator is that field sitting BESIDE it, so
    a day missing from the fold leaves the four categories visibly short of the tokens they split — the
    same honest shortfall ``pass``/``fail``/``unscored`` report against ``runs``. ``coverage`` states
    its own denominator, so the same missing day leaves no trace at all and it is dropped instead.

    This is the boundary the next person adding a nested block has to place themselves on: not "is it
    nested", but "does the block carry the denominator its own numbers are out of".
    """
    old = {k: v for k, v in _day("2026-08-01", runs=4, tokens=100).items()
           if k not in ("byCategory", "coverage")}

    block, moved = _merge_history(_prior([old, _day("2026-08-02", runs=3, tokens=100)]),
                                 [_day("2026-08-03", runs=2)], limit=1)

    assert moved["rolled_up"] == 2, "the fixture must actually fold both older days"
    rolled = block[_ROLLED_UP_KEY]
    assert rolled["tokens"] == 200, "both days' tokens are in the bucket"
    assert sum(rolled["byCategory"].values()) == 100 < rolled["tokens"], \
        "the partition is short by the day that split none of its tokens, and that reads as one"
    assert "coverage" not in rolled, "where the same missing day would have read as complete"


def test_a_bucket_that_swallowed_an_unmeasured_day_never_regains_its_coverage():
    """Once lost, gone — because a later fully-measured day cannot vouch for an earlier one.

    Every eviction re-folds the bucket, so the rule has to hold on the SECOND roll-up and not only on
    the first. A bucket that regained the pair here would state a complete fraction over a total that
    still includes the day nobody measured, which is the original defect arriving one day late.
    """
    old = {k: v for k, v in _row("2026-08-01", 10).items() if k != "coverage"}
    first, _ = _merge_history(_prior([old, _row("2026-08-02", 10)]),
                              [_row("2026-08-03", 10)], limit=1)
    assert "coverage" not in first[_ROLLED_UP_KEY]

    second, moved = _merge_history(first, [_row("2026-08-04", 10)], limit=1)

    assert moved["rolled_up"] == 1, "the fixture must actually re-fold the bucket"
    assert second[_ROLLED_UP_KEY]["days"] == 3
    assert "coverage" not in second[_ROLLED_UP_KEY], \
        "the fully-measured day joining it does not make the unmeasured one measured"
    assert second[_ROLLED_UP_KEY]["population"] == 30, "while every figure it holds still totals"


def test_a_failed_history_read_is_not_swallowed():
    """The read failing is the one thing that must not be degraded around: a PutItem that cannot see
    the prior history destroys it, so catching here would turn a loud failure into silent data loss.

    It is not a new failure mode either — `DdbSuggestionStore` point-gets the same table and the same
    partition unguarded earlier in the same run, so a table this run cannot read has already aborted
    it long before this call.
    """
    class _Unreadable(_StrictTable):
        def get_item(self, Key=None, **kw):
            raise RuntimeError("ProvisionedThroughputExceededException")

    with pytest.raises(RuntimeError, match="ProvisionedThroughput"):
        _read_history(_Unreadable())


def _run_spending_on(model_count: int) -> Run:
    """One measured run whose tokens are spread over ``model_count`` distinct model ids.

    Every model carries identical counts on purpose, so the only thing that differs between two
    rosters is HOW MANY of them there are — which is what the bound below is about.
    """
    counts = {"tokens": 100, "input_tokens": 20, "output_tokens": 10,
              "cache_read_tokens": 50, "cache_write_tokens": 20, "llm_calls": 1}
    return Run(run_id="r0", resources={
        "latency_ms": 10, "tokens": 100 * model_count, "llm_calls": model_count,
        "by_model": {f"m{i:03d}": dict(counts) for i in range(model_count)}})


def test_a_wider_bedrock_roster_costs_the_suggestion_trim_nothing(registry):
    """`scorecard` is FIXED size and never trimmed, so every byte it gains comes out of the
    suggestions' allowance in `_fit_bundle`. `spend.byModel` is keyed on a Bedrock model id — the
    one thing in that block that could grow without limit — so `dto._SPEND_MODELS_NAMED` bounds it,
    and this is the check that the BOUND governs the size rather than the roster being small.

    Quadrupling the roster leaves the named list the same length and the same bytes; the models it
    withheld are accounted for by one integer in `byModelOther`.
    """
    def spend_for(model_count):
        return scorecard_to_wire([_run_spending_on(model_count)], [_verdict("r0")])["spend"]

    small, wide = spend_for(_SPEND_MODELS_NAMED * 2), spend_for(_SPEND_MODELS_NAMED * 4)
    assert len(small["byModel"]) == len(wide["byModel"]) == _SPEND_MODELS_NAMED
    assert small["byModelOther"]["models"] == _SPEND_MODELS_NAMED
    assert wide["byModelOther"]["models"] == _SPEND_MODELS_NAMED * 3
    assert len(json.dumps(wide["byModel"])) == len(json.dumps(small["byModel"]))

    # ...and at the REAL item budget the whole bundle still fits with nothing given up for it.
    runs = {"r0": _run_spending_on(_SPEND_MODELS_NAMED * 4)}
    verdicts = {"r0": _verdict("r0")}
    wire = _bundle(registry, [_suggestion(i) for i in range(9)], runs, verdicts)
    assert wire["scorecard"]["spend"]["byModelOther"]["models"] == _SPEND_MODELS_NAMED * 3
    fitted, dropped = _fit_bundle(wire, registry, runs_by_id=runs, verdicts_by_id=verdicts)
    assert dropped == 0 and len(fitted["suggestions"]) == 9

    # And DynamoDB itself accepts the item, measured by the fake that enforces its bounds.
    table = _StrictTable()
    table.put_item(Item=build_item(bundle=_decimalize(fitted),
                                   created_at="2026-08-29T04:00:00Z",
                                   judge_config_hash="h", code_version="v"))
    stored = table.get_item(Key={"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK})["Item"]
    named = stored["bundle"]["scorecard"]["spend"]["byModel"]
    assert len(named) == _SPEND_MODELS_NAMED


def test_the_size_ceiling_bounds_the_block_and_stays_a_small_share_of_the_item_budget(capsys):
    """The ceiling is what turns "the history barely costs the suggestions anything" from an
    assumption into a bound, since the append happens BEFORE `_fit_bundle` measures.

    It also must not fire at the real row width, or the measurement behind the constant is wrong.
    """
    assert _HISTORY_BUDGET_BYTES * 10 < _ITEM_BUDGET_BYTES, \
        "a history costing a tenth of the item would make appending before the trim expensive"

    real = [_row(f"2026-07-{day:02d}", day, pass_rate=0.8735632183908046)
            for day in range(1, _HISTORY_ROWS + 1)]
    fitted, evicted = _fit_history(real)
    assert evicted == [] and fitted == real, "the measured row width must fit the retained count"

    fat = [{**row, "unexpectedlyWide": "x" * 600} for row in real]
    trimmed, over = _fit_history(fat)
    # The evicted ROWS come back, not a count: the caller folds them into the rolled-up bucket, so a
    # day pushed out by a size bound keeps its figures in the cumulative total.
    assert over and trimmed + [] == fat[len(over):]
    assert len(json.dumps(trimmed, default=str)) <= _HISTORY_BUDGET_BYTES
    assert trimmed[-1] == fat[-1], "the newest row is the one a trend cannot lose"
    assert "exceeded" in capsys.readouterr().out


def test_the_history_survives_the_suggestion_trim_byte_for_byte(registry):
    """Why the append can sit BEFORE `_fit_bundle`: that trim rebuilds around the suggestions list
    and carries every other key through untouched, so the history cannot be trimmed away by a
    mechanism that knows nothing about it — only measured by it, which is the point."""
    runs, verdicts = _population()
    members = _members(3)
    wire = _bundle(registry, [_suggestion(i, padding=4000) for i in range(3)], runs, verdicts,
                   members)
    wire, _ = _with_history(wire, _prior([_row(f"2026-08-2{d}", d) for d in range(1, 6)]),
                            date="2026-08-30")

    trimmed, dropped = _fit_bundle(wire, registry, budget=9000, runs_by_id=runs,
                                   verdicts_by_id=verdicts, members_by_suggestion=members)

    assert dropped > 0, "the fixture must actually force a trim"
    assert trimmed[_HISTORY_KEY] == wire[_HISTORY_KEY]
    # And the history's bytes were INSIDE the measurement, which is the whole reason for the order.
    assert len(json.dumps(trimmed, default=str)) <= 9000


def test_the_history_is_not_a_required_bundle_key(registry):
    """Adding it to `_REQUIRED_BUNDLE_KEYS` would make the producer refuse its own first run, and
    every bundle written before the block existed."""
    assert _HISTORY_KEY not in _REQUIRED_BUNDLE_KEYS
    runs, verdicts = _population()
    without = _bundle(registry, [], runs, verdicts)
    _require_bundle_keys(without)                                    # must not raise
    with_rows, _ = _with_history(without, _prior([]), date="2026-08-30")
    _require_bundle_keys(with_rows)                                  # nor with the block present


def test_a_carried_row_round_trips_through_decimal_and_the_next_days_append():
    """The store hands numbers back as ``Decimal``, and the next write must accept what came out.

    Driven through the strict fake in both directions: `_decimalize` on the way in (the fake rejects
    a bare ``float``, as the resource layer does) and Decimal on the way out (the fake returns every
    number as one), so a carried row that was silently dropped, arithmetic that mixed Decimal with
    float, or a float that escaped conversion all fail here.
    """
    runs, verdicts = _population()
    table = _snapshot_table([_row("2026-08-28", 7, pass_rate=0.25)])

    first, _ = _with_history(_wire_with_scorecard(runs, verdicts), _read_history(table),
                             date="2026-08-29")
    table.put_item(Item=build_item(bundle=_decimalize(first), created_at="2026-08-29T04:00:00Z",
                                   judge_config_hash="h", code_version="deadbee"))

    stored = table.get_item(Key={"actorId": SYSTEM_ACTOR, "sk": SNAPSHOT_SK})["Item"]
    assert all(isinstance(r["population"], Decimal) for r in stored["bundle"][_HISTORY_KEY]), \
        "the fake must hand back Decimal or this path is untested"

    carried = _read_history(table)
    assert [r["date"] for r in carried[_HISTORY_KEY]] == ["2026-08-28", "2026-08-29"]
    # ...and the read converts them, which is now load-bearing rather than tidy: rows are summed when
    # a day is rolled up, and `Decimal + float` raises rather than returning a slightly wrong number.
    assert all(not isinstance(r["population"], Decimal) for r in carried[_HISTORY_KEY])
    assert carried[_HISTORY_KEY][0]["passRate"] == 0.25

    second, moved = _with_history(_wire_with_scorecard(runs, verdicts), carried,
                                  date="2026-08-30")
    table.put_item(Item=build_item(bundle=_decimalize(second), created_at="2026-08-30T04:00:00Z",
                                   judge_config_hash="h", code_version="deadbee"))

    assert moved["rolled_up"] == 0
    assert [r["date"] for r in second[_HISTORY_KEY]] == ["2026-08-28", "2026-08-29", "2026-08-30"]
    assert second[_HISTORY_KEY][0]["passRate"] == 0.25
    assert _read_history(table)[_HISTORY_KEY][0]["population"] == 7


# --- what the log summary reports -------------------------------------------
class _RosterReader:
    """`MemoryReader`'s roster contract only — the two calls `aws_lambda._known_actors` makes.

    Named after what the producer wants from it rather than after the class it stands in for, because
    the whole surface it needs is "who exists" and "did the sweep finish". Kept this narrow on purpose:
    a fake that also answered `conversation_for_session` would invite a test to depend on a path the
    producer does not take here.
    """

    def __init__(self, actor_ids=(), complete=True) -> None:
        self._actor_ids = list(actor_ids)
        self._complete = complete

    def actor_ids(self):
        return list(self._actor_ids)

    def actor_list_complete(self):
        return self._complete


def _judged(run_id: str, actor_id: str, *, prose: int = 0, session: str = FFAB73F8):
    """``(Run, Verdict)`` for one OWNED run, with the judge's per-tier sentence ``prose`` wide.

    That sentence is the one knob that moves the two item sizes apart. `dto._run_row` carries each
    tier's ``explanation`` verbatim onto the actor's item, and the global item may not carry it at
    all — so widening it pushes the per-actor bundles over the shared budget while leaving the global
    bundle exactly where it was.

    The WINDOW is a real harvested one (REQ-017), because it is what decides which day row the run
    lands in — and a synthetic date would let a day-row test pass over a shape the harvest never
    produces. Its id stays synthetic: the trim fixtures need runs that differ only in bulk.
    """
    run = Run(run_id=run_id, actor_id=actor_id, resources={"latency_ms": 10},
              window=real_window(session))
    sentence = "x" * prose
    verdict = Verdict(run_id,
                      [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9, sentence),
                       EvaluatorResult("results_delivered", "llm", Outcome.PASS, 0.9, sentence)],
                      UtilityVector(), Overall.PASS, "", workload_class="conversational",
                      gating=("dispatch_correct", "results_delivered"))
    return run, verdict


def _drive_producer(monkeypatch, table, *, registry, population, budget, harvest_dir,
                    env_seen=None, roster=None):
    """One full producer cycle with only its OUTBOUND edges stubbed; returns the log summary.

    The memory pre-flight, the harvest and the judged pipeline are replaced because those are the
    calls that reach AWS. Everything downstream of them is the real code — `to_wire`, the store, both
    guards, both fits and the summary assembly — because the summary is what is under test here and a
    hand-assembled one would assert nothing about how the producer fills it in.

    ``harvest_dir=None`` leaves the two path variables UNSET on the way in, which is what makes it
    observable whether the cycle puts them back. ``env_seen``, when given, is filled with those
    variables as the stubbed harvest and pipeline observed them: the real sources resolve their paths
    from the environment at call time, so mid-cycle is the only moment the configured values exist.
    """
    runs = [run for run, _ in population]
    verdicts = [verdict for _, verdict in population]

    def note_environment():
        if env_seen is not None:
            env_seen.update({name: os.environ.get(name)
                             for name in ("IMPROVE_HARVEST_DIR", "IMPROVE_SIGMA_REGISTRY")})

    def pipeline(*, context, **kw):
        note_environment()
        context["runs"] = runs
        context["verdicts"] = verdicts
        context["registry"] = registry
        # The reader the real pipeline puts here, so the roster path runs rather than being skipped
        # by an absent key. `None` would be the offline-mode answer, which is a different test.
        context["memory_reader"] = roster
        return {"proposals": [], "population_size": len(runs), "registry": {"available": True}}

    monkeypatch.setattr(aws_lambda, "_preflight_memory", lambda *a, **k: None)
    monkeypatch.setattr(aws_lambda, "harvest_live", lambda **kw: note_environment())
    monkeypatch.setattr(aws_lambda, "run_pipeline", pipeline)
    monkeypatch.setattr(aws_lambda.boto3, "resource",
                        lambda *a, **k: SimpleNamespace(Table=lambda name: table))
    monkeypatch.setenv("IMPROVE_TABLE", "improve-store-under-test")
    monkeypatch.setenv("IMPROVE_CREATED_AT", "2026-08-30T04:00:00Z")
    # ONE budget for both items, which is the fact that makes this test's fixture non-trivial.
    monkeypatch.setenv("IMPROVE_ITEM_BUDGET_BYTES", str(budget))
    # Where the cycle should read its harvest from, when the caller cares. Not a containment
    # measure: `run` scopes what it assigns and restores it, which is its own test below.
    if harvest_dir is None:
        monkeypatch.delenv("IMPROVE_HARVEST_DIR", raising=False)
        monkeypatch.delenv("IMPROVE_SIGMA_REGISTRY", raising=False)
    else:
        monkeypatch.setenv("IMPROVE_HARVEST_DIR", str(harvest_dir))
    monkeypatch.delenv("IMPROVE_JUDGE_RUNS", raising=False)
    return aws_lambda.run()


def test_the_summary_reports_the_global_drop_count_and_never_an_actors(registry, tmp_path,
                                                                      monkeypatch):
    """``suggestions_dropped_to_fit`` is a claim about the SHARED item's published suggestion set.
    An actor's fit drops that actor's RUN ROWS from a different item. Two quantities, and this log
    line is the only place either is ever visible — so one reported under the other's name is a
    number an operator would read while verifying a deploy and could not check.

    They agree only while no actor is ever trimmed, which is why the fixture makes them differ: one
    shared budget that the global bundle fits inside and that every actor's run list exceeds.
    """
    population = [_judged(f"r{i}", "an-actor-with-fat-verdicts", prose=3000) for i in range(3)]
    # A suggestion already in the store, so the global fit has a real ladder to decline to climb —
    # otherwise "dropped nothing" would be true of an empty list rather than of a bundle that fit.
    table = _StrictTable([{"actorId": SYSTEM_ACTOR, "sk": STORE_SK, "version": 1,
                           "suggestions": _decimalize({"s0": _suggestion(0)})}])

    summary = _drive_producer(monkeypatch, table, registry=registry, population=population,
                              budget=10_000, harvest_dir=tmp_path,
                              roster=_RosterReader(["an-actor-with-fat-verdicts"]))

    written = {(item["actorId"], item["sk"]): item for item in table.put_calls}
    bundle = written[(SYSTEM_ACTOR, SNAPSHOT_SK)]["bundle"]
    assert len(bundle["suggestions"]) == 1, "the global bundle kept what it had"
    assert len(json.dumps(bundle, default=str)) <= 10_000, "...because it FIT, not by luck"
    assert summary["suggestions"] == 1
    assert summary["suggestions_dropped_to_fit"] == 0

    # ...while the actor's own item did lose run rows, reported under its own name. Asserted as a
    # WHOLE dict rather than key by key, so a counter added later cannot go unreported by omission.
    assert summary["actors"] == {
        "bundles": 1, "zero_bundles": 0, "roster": 1, "roster_complete": True,
        "runs": 1, "runs_dropped_to_fit": 2,
        # Three runs on one real harvested day, so one day row — and the run trim did NOT touch it,
        # which is the ladder's order visible in the log rather than only in the item.
        "history_rows": 1, "history_rolled_up": 0, "history_days_refused": 0,
        "fanned_out_requests": 0, "sub_sessions_folded": 0,
        "sub_sessions_without_a_request": 0, "runs_with_no_window": 0,
        "unattributable_runs": 0}
    # The day row survived in the ITEM too, not just in the count.
    actor_bundle = written[("an-actor-with-fat-verdicts", SNAPSHOT_SK)]["bundle"]
    assert len(actor_bundle[_HISTORY_KEY]) == 1
    assert actor_bundle[_HISTORY_KEY][0]["runs"] == 3, "three requests, one day"
    assert actor_bundle["runsTotal"] == 3, "...and the true count outlives the trimmed run list"


# --- 0 versus "we do not know" -----------------------------------------------
def test_a_zero_bundle_is_a_measured_zero_and_passes_the_same_guard():
    """The whole point of writing one: ``0`` and "nobody looked" stop rendering the same.

    A reader with this item was swept and had nothing — ``runsTotal: 0`` is that measurement. A reader
    with NO item is one the harness has never heard of, and that is what the sentinel is for. The
    difference only exists because the producer writes the first and not the second.
    """
    zero = _zero_actor_bundle()
    _require_bundle_keys(zero, _REQUIRED_ACTOR_BUNDLE_KEYS)   # the same contract, not a lesser one

    assert zero["runsTotal"] == 0, "a swept window that found nothing is a measurement"
    assert zero["runs"] == [] and zero[_HISTORY_KEY] == []
    # No day row for today. A day with no activity gets no row (`dto._day_rows`), so a quiet day
    # cannot appear as a real day that spent nothing.
    assert not zero[_HISTORY_KEY]
    # The waste block comes from the real mapper, so an actor with nothing is described by the same
    # code as an actor with something rather than by a hand-written second definition of "empty".
    assert zero["waste"] == waste_to_wire({"findings": [], "runs_examined": 0,
                                           "runs_not_scanned": []})
    assert zero["waste"]["runsWithWaste"] == 0
    json.dumps(zero)   # the item is written with json.dump; this is that contract


def test_a_zero_bundle_cannot_erase_the_day_rows_already_on_the_item():
    """THE objection that used to make writing one unsafe, and the reason it no longer holds.

    The write was skipped because an empty bundle would replace real evidence with a measured-looking
    blank — true while the item held only the last 24 hours. Now the day rows are read back off the
    item first, so a quiet day costs the reader today's run rows and none of the days there were.
    """
    prior = _prior([_day("2026-08-27", runs=2, tokens=500), _day("2026-08-28", runs=1, tokens=90)])

    merged, moved = _with_actor_history(_zero_actor_bundle(), prior)

    assert merged["runsTotal"] == 0 and merged["runs"] == []
    assert [(r["date"], r["tokens"]) for r in merged[_HISTORY_KEY]] == [
        ("2026-08-27", 500), ("2026-08-28", 90)], "yesterday's real rows survived today's zero"
    assert moved == {"rows": 2, "rolled_up": 0, "refused": 0}
    assert merged[_HISTORY_FIRST_DATE_KEY] == "2026-08-27"


def test_every_actor_the_roster_names_gets_an_item_including_one_with_no_runs(registry, tmp_path,
                                                                             monkeypatch):
    """The roster is conversation memory's, which is why an actor absent from it stays unknown.

    An actor id reaches this producer only because Memory named it, so every actor that has ever had a
    bundle is in that list and one that is NOT in it is one the harness has genuinely never seen.
    Driven through the real cycle, because the union of "actors with evidence" and "actors on the
    roster" is assembled there and a hand-built call would assert nothing about it.
    """
    population = [_judged("r0", "actor-who-ran-something")]
    table = _StrictTable()

    summary = _drive_producer(monkeypatch, table, registry=registry, population=population,
                              budget=200_000, harvest_dir=tmp_path,
                              roster=_RosterReader(["actor-who-ran-something",
                                                    "actor-who-ran-nothing"]))

    written = {(item["actorId"], item["sk"]) for item in table.put_calls}
    assert ("actor-who-ran-something", SNAPSHOT_SK) in written
    assert ("actor-who-ran-nothing", SNAPSHOT_SK) in written, \
        "a reader with no runs must be able to see a 0 rather than the unknown sentinel"
    assert ("an-actor-nobody-has-heard-of", SNAPSHOT_SK) not in written
    assert summary["actors"]["bundles"] == 2 and summary["actors"]["zero_bundles"] == 1

    quiet = next(item for item in table.put_calls
                 if item["actorId"] == "actor-who-ran-nothing")["bundle"]
    assert quiet["runsTotal"] == 0
    busy = next(item for item in table.put_calls
                if item["actorId"] == "actor-who-ran-something")["bundle"]
    assert busy["runsTotal"] == 1 and len(busy[_HISTORY_KEY]) == 1


def test_a_truncated_roster_is_reported_because_it_withholds_somebodys_zero(registry, tmp_path,
                                                                           monkeypatch):
    """A short roster and a small account look identical from outside, and only the sweep knows which
    it saw. A withheld zero renders as the unknown sentinel — the exact confusion the zero bundle
    exists to remove — so the flag has to reach the log rather than being assumed true."""
    summary = _drive_producer(monkeypatch, _StrictTable(), registry=registry,
                              population=[_judged("r0", "an-actor")], budget=200_000,
                              harvest_dir=tmp_path,
                              roster=_RosterReader(["an-actor"], complete=False))
    assert summary["actors"]["roster_complete"] is False


def test_with_no_memory_reader_the_actors_with_evidence_are_still_written(registry, tmp_path,
                                                                         monkeypatch):
    """The roster is a UNION, not a replacement, and this is the case that makes the difference visible.

    Offline mode consults no Memory, so nothing can be named and there is no roster. An actor whose
    owner this run resolved is real regardless, so their item must still be written — a roster read
    that came back empty must not silently turn the whole per-actor write off.
    """
    summary = _drive_producer(monkeypatch, _StrictTable(), registry=registry,
                              population=[_judged("r0", "an-actor")], budget=200_000,
                              harvest_dir=tmp_path, roster=None)
    assert summary["actors"] == {**summary["actors"], "bundles": 1, "roster": 0,
                                 "roster_complete": None, "zero_bundles": 0}


# --- the environment the cycle configures, and gives back -------------------
#
# `run` tells the evidence sources where to read by ENVIRONMENT rather than by argument, because they
# resolve their paths at call time from inside `run_pipeline` and nothing threads a path down to them.
# That makes the configuration ambient, and an ambient write that is never undone is a leak everywhere
# except the one place it was written for.
def test_the_cycle_configures_both_source_paths_and_leaves_neither_behind(registry, monkeypatch):
    """The two halves are one test on purpose. Asserting only that the variables are gone afterwards
    would also be true of a producer that stopped configuring its sources at all — which would send
    the harvest and the registry reader to their own defaults and is the failure the assignment
    exists to prevent. So the mid-cycle values are asserted first, then their absence after.
    """
    seen: dict = {}
    _drive_producer(monkeypatch, _StrictTable(), registry=registry,
                    population=[_judged("r0", "an-actor")], budget=_ITEM_BUDGET_BYTES,
                    harvest_dir=None, env_seen=seen)

    assert seen["IMPROVE_HARVEST_DIR"], "the harvest and its readers must agree on one directory"
    assert seen["IMPROVE_SIGMA_REGISTRY"].endswith("sigma-registry.json"), (
        "the bundled JSON copy — the runtime has no PyYAML, so a .yaml default would crash")

    assert "IMPROVE_HARVEST_DIR" not in os.environ
    assert "IMPROVE_SIGMA_REGISTRY" not in os.environ


def test_a_path_the_caller_chose_wins_during_the_cycle_and_survives_it(registry, tmp_path,
                                                                       monkeypatch):
    """Restoring means putting a PRIOR value back, not merely unsetting the variable.

    `IMPROVE_SIGMA_REGISTRY` is how a deploy points the producer at a registry other than the bundled
    one, so the caller's choice has to win while the cycle runs and still be there when it ends. A
    scope that overrode it would silently attribute against the wrong permission model; one that
    popped it on the way out would break the next reader in the process instead of the current one.
    """
    chosen = str(tmp_path / "a-registry-the-deploy-named.json")
    monkeypatch.setenv("IMPROVE_SIGMA_REGISTRY", chosen)
    seen: dict = {}

    _drive_producer(monkeypatch, _StrictTable(), registry=registry,
                    population=[_judged("r0", "an-actor")], budget=_ITEM_BUDGET_BYTES,
                    harvest_dir=tmp_path, env_seen=seen)

    assert seen["IMPROVE_SIGMA_REGISTRY"] == chosen
    assert seen["IMPROVE_HARVEST_DIR"] == str(tmp_path)
    assert os.environ["IMPROVE_SIGMA_REGISTRY"] == chosen
    assert os.environ["IMPROVE_HARVEST_DIR"] == str(tmp_path)


def test_the_environment_scope_is_restored_even_when_the_cycle_raises(monkeypatch):
    """The restore is in a ``finally``, and that is the case worth pinning.

    This producer raises for real, foreseeable reasons — no `IMPROVE_TABLE`, an actor id colliding
    with the reserved partition, a `put_item` the store refuses — and a scope that only restored on
    the success path would leave the process reconfigured exactly when someone is retrying or
    debugging. Both directions are covered: a variable that HAD a value goes back to it, and one that
    had none is removed again rather than left holding the producer's default.
    """
    monkeypatch.setenv("IMPROVE_SIGMA_REGISTRY", "/a/registry/the/caller/already/chose.json")
    monkeypatch.delenv("IMPROVE_HARVEST_DIR", raising=False)

    with pytest.raises(RuntimeError, match="the pipeline failed"):
        with _scoped_environ(IMPROVE_SIGMA_REGISTRY="/the/producers/own.json",
                             IMPROVE_HARVEST_DIR="/tmp/scratch-harvest"):
            assert os.environ["IMPROVE_SIGMA_REGISTRY"] == "/the/producers/own.json"
            assert os.environ["IMPROVE_HARVEST_DIR"] == "/tmp/scratch-harvest"
            raise RuntimeError("the pipeline failed")

    assert os.environ["IMPROVE_SIGMA_REGISTRY"] == "/a/registry/the/caller/already/chose.json"
    assert "IMPROVE_HARVEST_DIR" not in os.environ


# --- the population cap, which lives in two languages ------------------------
#
# `IMPROVE_LIMIT` is set by the deploy and read by the producer, so the number exists in a CDK
# TypeScript literal AND in this package's fallback. They cannot share a constant across that
# boundary, so a test is the only thing that can hold them together — and the drift is silent in the
# worst direction: the fallback is what a local reproduction of a cloud run uses, so a stale one makes
# every local population quietly different from the deployed one while both look right.
_STACK = Path(__file__).resolve().parents[2] / "infra" / "lib" / "agent-infra-stack.ts"


def test_the_producers_population_fallback_matches_what_the_deploy_sets():
    deployed = re.search(r'IMPROVE_LIMIT:\s*"(\d+)"', _STACK.read_text())
    assert deployed, "IMPROVE_LIMIT is no longer set in the stack — the fallback is now the only value"
    assert int(deployed.group(1)) == _POPULATION_LIMIT, (
        f"the deploy scores {deployed.group(1)} sessions and this package falls back to "
        f"{_POPULATION_LIMIT}; move both or a local run mines a different population")


def test_an_empty_or_unparseable_limit_falls_back_instead_of_raising(monkeypatch):
    """The `or`-fallback idiom this package uses for every bound, asserted on this one.

    An env var set to the empty string is the case that actually happens — a CDK value rendered from
    an unset context key, a shell export with nothing after the `=` — and `int("")` raises, which in
    the producer means the cycle dies before the harvest instead of scoring a default population.
    """
    for bad in ("", "   ", "sixty", "60.0"):
        monkeypatch.setenv("IMPROVE_LIMIT", bad)
        assert aws_lambda._env_int("IMPROVE_LIMIT", _POPULATION_LIMIT) == _POPULATION_LIMIT
    monkeypatch.setenv("IMPROVE_LIMIT", "12")
    assert aws_lambda._env_int("IMPROVE_LIMIT", _POPULATION_LIMIT) == 12, \
        "an operator's value must still win — this is an operational bound, not a fixed one"
