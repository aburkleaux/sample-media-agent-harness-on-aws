"""``improve-worker`` Lambda handler — the in-cloud producer.

This is what makes the Improve SNAPSHOT self-refreshing with no laptop. On a schedule
(EventBridge, daily) it runs the whole producer end to end and writes the finished bundle to
the global ``(__system__, "SNAPSHOT")`` item the Capability API serves — the exact write
``improve.seed_snapshot`` did by hand, now automated. It REPLACES the manual
``mine_local --emit-wire`` + ``seed_snapshot`` two-step; nothing else on the deploy path
changes.

It also writes ``(<actor>, "SNAPSHOT")`` for every actor conversation memory can name, carrying that
actor's own evidence (``dto.actor_bundles``) plus their per-day figures. Two items, two audiences: the
global one is the shared harness's claim and may hold nothing derived from a user's session; the
per-actor ones hold exactly that, and their KEY is what scopes them.

Both items carry a CUMULATIVE half, and that is the one part of this producer a re-run does not simply
recompute. Everything else is rebuilt from a bounded harvest window and replaced wholesale, which is
why a reader who ran nothing yesterday used to have no figures at all. The day rows, the rolled-up
bucket for the days retention pushed out, and the first date they cover are read off the item BEFORE it
is overwritten and carried forward — one mechanism (`_merge_history`) for both items, because a second
one shaped like it is how two definitions of "keep the newest N" come to disagree. There is no file
anywhere in this: ``/tmp`` does not survive between invocations, which is exactly the boundary a
cumulative figure has to cross, so the item is the only durable home.

Pipeline, in order:

  1. **Harvest** fresh telemetry into ``/tmp/improve-harvest`` (``harvest_live`` — the boto3
     port of the offline harvester). Writes ``sessions/sessions.json`` and ``ddb/plan-state.json``
     in the exact paths the ``Harvested*`` sources read under ``IMPROVE_HARVEST_DIR``. Each
     session in that file carries its token ``usage`` beside its ``window`` — one extra Insights
     query per day-window, aggregated server-side by ``(session, model)``, so the round-trip count
     tracks the window rather than ``IMPROVE_LIMIT``.
  2. **Mine + score** via ``mine_local.run_pipeline`` in ``population="harvest", mode="live"``:
     discover every session with enough spans, re-judge each with the real Bedrock judge, mine
     signatures, attribute them against the Σ-registry. ``config=<dict>`` is passed so the yaml
     ``load_config`` is never reached (the runtime has no PyYAML); the registry is shipped as
     JSON and pointed at by ``IMPROVE_SIGMA_REGISTRY`` (``registry.py`` reads ``.json`` natively).
  3. **Persist + emit** — replicates ``mine_local``'s ``--emit-wire`` block exactly, with two
     transport swaps: the cross-run store is ``DdbSuggestionStore`` (a DynamoDB item, not a
     ``/tmp`` file that would evaporate between invocations), and the finished bundle is written
     to the ``SNAPSHOT`` item instead of a local file. One thing there is NOT a straight
     replacement: the bundle carries a short rolling per-day history of population-level scalars,
     read off the item before it is overwritten and carried forward, because an unconditional
     PutItem of a recomputed bundle is otherwise incapable of accumulating anything and the surface
     can only ever show a snapshot.

Bounded for the 15-minute Lambda ceiling: a short harvest window (``IMPROVE_HARVEST_HOURS``,
default 24h → one Insights day-window) and a capped population (``IMPROVE_LIMIT``, newest
sessions first). Live scoring is full-population per run, so the cap is the cost control; daily
cadence keeps it fresh.

Producer-only. boto3 at module scope is fine here (this module is never imported by the
Capability API image or the projection Lambda — the isolation boundary
``tests/test_capability_api_isolation.py`` guards those, and neither imports ``improve.*``).
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os

import boto3

try:
    from .dto import (
        _is_number, _measured, actor_bundles, components_to_wire, publishable_suggestion,
        published_suggestions_block, suggestion_members, summed, to_wire, waste_to_wire,
    )
    from .ddb_store import DdbSuggestionStore, _decimalize, _floatize
    from .harvest_live import harvest as harvest_live
    from .mine_local import run_pipeline
    from .seed_snapshot import SNAPSHOT_SK, SYSTEM_ACTOR, build_item
except ImportError:  # pragma: no cover — direct-script fallback, mirrors the package
    from improve.dto import (  # type: ignore
        _is_number, _measured, actor_bundles, components_to_wire, publishable_suggestion,
        published_suggestions_block, suggestion_members, summed, to_wire, waste_to_wire,
    )
    from improve.ddb_store import DdbSuggestionStore, _decimalize, _floatize  # type: ignore
    from improve.harvest_live import harvest as harvest_live  # type: ignore
    from improve.mine_local import run_pipeline  # type: ignore
    from improve.seed_snapshot import SNAPSHOT_SK, SYSTEM_ACTOR, build_item  # type: ignore


def _data_path(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _preflight_memory(memory_id, region: str) -> None:
    """Fail fast and LOUD if conversation memory is unreadable — BEFORE the harvest spends its
    Insights/DDB budget.

    The live judge reads every session's goal/response from Memory
    (``observe/memory_reader.py``), which calls ``bedrock-agentcore.list_actors`` then
    ``list_events``. Neither the client construction nor ``list_actors`` is wrapped there, so a
    stale runtime boto3 (no ``bedrock-agentcore`` service) or a missing grant would crash the
    whole run on its FIRST session — after the harvest already ran. One cheap ``list_actors``
    here turns both into a fast abort that wastes nothing, and names which of the two it is.
    """
    if not memory_id:
        raise RuntimeError(
            "no memory_id resolved (IMPROVE_MEMORY_ID unset and none in loop.config.json) — "
            "the live judge cannot read session goals/responses.")
    try:
        client = boto3.client("bedrock-agentcore", region_name=region)
    except Exception as e:  # noqa: BLE001 — UnknownServiceError and friends
        raise RuntimeError(
            f"this runtime's boto3 has no 'bedrock-agentcore' client ({type(e).__name__}: {e}); "
            f"the memory reader cannot run. Attach a newer boto3 layer to improve-worker.") from e
    try:
        client.list_actors(memoryId=memory_id, maxResults=1)
    except Exception as e:  # noqa: BLE001 — AccessDenied / ResourceNotFound / throttling
        raise RuntimeError(
            f"cannot read conversation memory {memory_id!r} ({type(e).__name__}: {e}). Check the "
            f"improve-worker's bedrock-agentcore:ListActors + ListEvents grant. Failing before "
            f"the harvest so its budget is not wasted.") from e


#: `scorecard` is required: it is FIXED size, always derivable (an empty population yields an
#: empty-but-complete scorecard), and it is the only thing the surface can say when `admitted`
#: is 0 — a bundle that lost it would render "no data" as if healthy.
#: `waste` is required for the same reason, plus a sharper one: `{"runsWithWaste": 0, "runs": 174}`
#: and an ABSENT key mean different things (nothing wasted vs nobody looked), and only the producer
#: can keep them distinguishable. It is derivable over an empty population too.
#: ``suggestionsTotal`` is in here beside ``suggestions`` for the same reason ``runsTotal`` is on the
#: per-actor contract below: this list is the ONE thing on the shared item that grows without bound,
#: `_fit_bundle` drops from its tail to fit 400 KB, and without the pre-trim count the item presents
#: a truncated list as the whole set. A bundle that lost the count would publish a trim as a shrink.
_REQUIRED_BUNDLE_KEYS = ("suggestions", "suggestionsTotal", "components", "patterns", "scorecard",
                         "waste")

#: The same guard on the PER-ACTOR item (``dto.actor_bundles``). Every key is derivable for any
#: actor that has a bundle at all — an actor with no run in this population gets no item rather
#: than an item with keys missing — so a bundle short of one is a producer bug, and one that lost
#: `runs` or `waste` would render "you have no runs" / "nothing was wasted" as if measured.
_REQUIRED_ACTOR_BUNDLE_KEYS = ("runs", "runsTotal", "evidence", "waste", "patterns")

# DynamoDB item hard cap is 400 KB; leave headroom for provenance keys + Decimal encoding.
# ONE constant for BOTH items: the global SNAPSHOT and each per-actor SNAPSHOT are separate items
# under the same 400 KB DynamoDB limit, so this is one fact about the store and not two bounds that
# happen to agree. `IMPROVE_ITEM_BUDGET_BYTES` therefore moves both together, which is correct.
_ITEM_BUDGET_BYTES = 350_000

#: The bundle key the rolling daily history lives under, and it is INSIDE the bundle rather than
#: beside it for a hard reason: the read route serves ``snapshot["bundle"]`` and nothing else, so the
#: provenance attributes ``build_item`` puts NEXT to the bundle — ``createdAt``, ``judgeConfigHash``,
#: ``codeVersion`` — never reach a consumer. A history written as an item-level sibling would be
#: stored correctly, cost storage every day, and be invisible forever.
#:
#: Deliberately NOT in `_REQUIRED_BUNDLE_KEYS`: an item written before this block existed carries
#: none, and a producer that demanded it would refuse its own first run.
_HISTORY_KEY = "history"

#: How many daily rows the bundle carries. The producer runs once a day, so this is about a month —
#: long enough for a change landed a week ago to read as a slope rather than as two adjacent points,
#: and short enough that the whole block stays a rounding error against the item budget. Operational,
#: so ``IMPROVE_HISTORY_ROWS`` moves it; raising it a long way is backstopped by
#: `_HISTORY_BUDGET_BYTES`, which says so in the log when it bites.
_HISTORY_ROWS = 30

#: Hard ceiling on the SERIALIZED history, independent of the row count. Measured rather than
#: guessed: the widest population row — a full-precision ``passRate``, a real Bedrock model id, the
#: ``"UNMEASURED"`` sentinel for every spend term (longer than most of the numbers it stands in for)
#: and the per-term coverage block — is 614 bytes, so `_HISTORY_ROWS` rows are 18,480. A per-actor day
#: row measured the same way is 620 and 30 of them are 18,600, which makes it now marginally the wider
#: of the two shapes. Both fit, holding the block at 6.3% of `_ITEM_BUDGET_BYTES`, and that percentage
#: is the whole justification for appending the history BEFORE the suggestion trim: these bytes come
#: out of the suggestions' allowance, and this is the most they can ever take. REAL staging rows come
#: in at 534-602 bytes, under the widest form because most of their terms are numbers rather than the
#: sentinel — but a ceiling has to be set from the widest form and not from a measured day.
#:
#: Raised twice from its original 12,000, both times by a row that got wider for a measured reason:
#: ``cost`` and its two token counts, ``input`` and ``models`` (cost reached the live population as
#: $20.17 at 25 of 25 measured), then ``coverage``, without which a lenient sum ships with no
#: denominator. Re-measured each time rather than padded, because a ceiling that no longer matches the
#: row it bounds is a size bound doing retention's job by accident: it would start evicting days before
#: `_HISTORY_ROWS` ever applied, making the row count a number that never bites.
#:
#: NOT raised for the per-actor row's three outcome counts, and the arithmetic is why: they add 37
#: bytes to a row — every staging row measured (497 -> 534, 563 -> 600, 565 -> 602) and the widest form
#: (583 -> 620) — so 30 rows grew by 1,110 bytes and the block still sits 15% under this ceiling.
#: Recorded rather than left implicit, because the next field added here has that much room and no more.
#:
#: Not env-overridable, unlike the row count above. It is a measurement of this block rather than an
#: operator preference, and an operator who wants more history raises the row count — which this
#: then bounds. It is also not a fraction of any other bound; two bounds sharing a literal would read
#: as a relationship that is not there.
_HISTORY_BUDGET_BYTES = 22_000

#: The bucket holding the days the bounds above pushed out of `_HISTORY_KEY`. Without it retention
#: would be a silent subtraction from every cumulative figure: the rows are the only copy of a past
#: day (the harvest window is 24h and `aws/spans` keeps 30 days, so a dropped day can never be
#: recomputed), and a total that quietly stopped including day 31 is a wrong number nothing can
#: detect. Folding instead loses the day's RESOLUTION and keeps its figures, which is the difference
#: between a shorter chart and a false total.
_ROLLED_UP_KEY = "rolledUp"

#: The earliest day any figure in this bundle covers, the rolled-up bucket included. It is what a
#: cumulative total has to be captioned with, and it must be the REAL first date rather than the
#: retention horizon — a total labelled "since 30 days ago" when the data starts later overstates its
#: own coverage, and one labelled with the horizon when the data starts earlier understates it.
_HISTORY_FIRST_DATE_KEY = "historyFirstDate"

#: ``"UNMEASURED"`` as it appears ON THE WIRE. `dto._measured` produces it from the sentinel object;
#: this module reads rows back out of DynamoDB, where only the string survives, so the two spellings
#: have to be comparable. One name for it rather than the literal at each site.
_UNMEASURED_WIRE = _measured(None)

#: How a day row's fields combine when several days are folded into ONE bucket. Declared rather than
#: hardcoded per field because the fold runs over BOTH row shapes — the population's and each actor's
#: — and a rule written twice is the hand copy `evaluate.gate` records the cost of.
#:
#: Everything not named here is a SUM, which is right for a count or a total and wrong for nothing
#: else currently on either row.
_ROW_DATE_FIELD = "date"

#: Model ids: a UNION. Summing them is meaningless and concatenating them would report one model used
#: on thirty days as thirty models.
_ROW_UNIONED_FIELDS = ("models",)

#: ``field: the field that weights it``. A rate cannot be summed and its unweighted mean is wrong
#: whenever the days differ in size, so it is folded against its own denominator — which is exact, not
#: an approximation: each day's ``passRate`` is passes/population, so the population-weighted mean of
#: several days is the pass rate over all of them.
_ROW_WEIGHTED_FIELDS = {"passRate": "population"}

#: Nested blocks that state their OWN denominator, and so fold ALL-OR-NOTHING: if any block in the
#: fold does not carry one, it is left OUT of the result entirely rather than folded over the subset
#: of blocks that do.
#:
#: THAT PROPERTY IS WHAT EARNS MEMBERSHIP, and nothing else does. Every other field on a row is a
#: numerator whose denominator sits BESIDE it — ``pass``/``fail``/``unscored`` against ``runs``,
#: ``byCategory`` against ``tokens`` — so folding one of those over a subset of the days yields a sum
#: visibly SHORT of the denominator next to it, which is the true statement that some of those days
#: recorded no such figure. ``coverage`` is the other way round: ``coverage.total`` IS the denominator
#: of the counts inside it, so a day that drops out of the fold shrinks numerator and denominator
#: together and the shortfall becomes INVISIBLE — a bucket whose 20 sessions of spend were measured on
#: 10 captions itself "measured on 10 of 10 sessions", a complete-looking 100%. A partial fold here
#: OVER-states a ratio where a partial fold anywhere else UNDER-states a sum, and only the first is
#: undetectable by whoever reads it.
#:
#: Dropping it is the honest answer rather than a loss, because absence already MEANS something to the
#: consumer: a period folding in one row with no ``coverage`` block is captioned "session coverage not
#: recorded" instead of with a fraction. That is the same rule as this one, applied one level up — and
#: it is why the rule is needed HERE as well, since a rolled-up bucket presents to that consumer as a
#: single row and it cannot see which days went into it.
_ROW_ALL_OR_NOTHING_FIELDS = ("coverage",)


def _require_bundle_keys(wire: dict, required=_REQUIRED_BUNDLE_KEYS) -> None:
    """Refuse a bundle missing a top-level key, mirroring ``seed_snapshot.load_bundle``.

    aws_lambda calls ``build_item`` directly — there is no wire file to ``load_bundle`` — so the
    manual path's fail-loud presence check is not otherwise in the loop. Without it a wire that
    lost a key would be written and the Improve page would render "no data" as if healthy.

    ``required`` selects which contract is being checked: the global bundle's or a per-actor one's.
    """
    missing = [k for k in required if k not in wire]
    if missing:
        raise RuntimeError(
            f"refusing to write the SNAPSHOT: wire bundle is missing {missing}. This is a "
            f"producer bug (the to_wire/store contract changed), not a data condition.")


def _budget(budget=None) -> int:
    return budget if budget is not None else _env_int("IMPROVE_ITEM_BUDGET_BYTES",
                                                      _ITEM_BUDGET_BYTES)


def _trim_to_budget(wire: dict, budget: int, *, ordered: list, rebuild, label: str,
                    last_rung: bool = True):
    """Drop from the TAIL of ``ordered`` until ``rebuild(kept)`` fits one item; return (wire, n).

    Shared by the global and per-actor fits because the loop is the same fact — a single DynamoDB
    item has a hard 400 KB cap, an unbounded list inside it would eventually make ``put_item``
    reject the write, and the old item would then be served stale forever with no future run able
    to unstick it. What differs is only which list gives way and how the survivors are rebuilt, so
    those are the parameters and the arithmetic is written once.

    Never a silent cap: the drop count is logged and returned, and the case where the FIXED part
    alone exceeds the budget is reported as a producer emergency rather than presented as a fit —
    ``put_item`` will correctly reject that item and it must fail loud.

    ``last_rung=False`` says another list is still to be tried, and it exists because the emergency
    is a claim about the WHOLE ladder rather than about one rung. Measured on a real 70-request
    bundle at a 2,000-byte budget: emptying the run rows left 2,935 bytes, this branch declared the
    fixed part over budget, and then the day-row rung fitted the item at 1,737. Reporting the first
    number as an ERROR is exactly the kind of misreported counter an operator would act on.
    """
    def size(w: dict) -> int:
        return len(json.dumps(w, default=str))

    if size(wire) <= budget:
        return wire, 0

    kept = list(ordered)
    while kept:
        if size(rebuild(kept)) <= budget:
            break
        kept.pop()  # drop the lowest-priority survivor and re-measure
    dropped = len(ordered) - len(kept)
    trimmed = rebuild(kept)
    final = size(trimmed)
    if final <= budget:
        print(f"improve-worker: WARNING {label} exceeded {budget} bytes for one DynamoDB item — "
              f"dropped {dropped} lowest-priority entries to fit ({len(kept)} kept, "
              f"{final} bytes). The item still refreshed; only the tail was capped.")
    elif last_rung:
        print(f"improve-worker: ERROR {label} is {final} bytes with ALL {len(ordered)} "
              f"trimmable entries dropped — its FIXED part alone exceeds the {budget}-byte item "
              f"budget. put_item will reject this; investigate the producer.")
    else:
        print(f"improve-worker: WARNING {label} is {final} bytes with ALL {len(ordered)} "
              f"trimmable entries dropped and is still over the {budget}-byte budget — trying the "
              f"next rung. Not an error yet; the rung after this one reports if nothing fits.")
    return trimmed, dropped


def _fit_bundle(wire: dict, registry, budget=None, runs_by_id=None, verdicts_by_id=None,
                members_by_suggestion=None):
    """Trim published suggestions until the global bundle fits ONE item; return (wire, dropped).

    The store keeps every finding it has ever seen, so the published set only grows. Reproducing
    findings are kept first, then most-recently seen, so a still-firing finding outranks one whose
    evidence aged out — and components are re-derived from whatever survives so the two stay
    consistent.

    ``runs_by_id``/``verdicts_by_id``/``members_by_suggestion`` are forwarded to the components
    re-derivation for one reason: without them a trimmed bundle's `ComponentRow.health` would come
    back `None` for every row, i.e. a trim would silently downgrade "measured" to "unknown". The
    third is now required for a measurement at all, because a suggestion's member run ids do not
    travel on the wire. `scorecard` and `waste` are FIXED size and never trimmed, so neither needs a
    rung on this ladder — `waste` is counts and per-kind totals over a closed set of kinds, so it
    cannot grow with the population.

    ``suggestionsTotal`` is deliberately NOT recomputed here. It is stamped upstream from the store's
    own view (`dto.published_suggestions_block`) and carried through the rebuild untouched, which is
    what makes ``len(suggestions) < suggestionsTotal`` the trace of this trim on the served item.
    Re-deriving it from the survivors would make the item describe the trimmed list as the whole set
    — the exact silence this exists to break — and the log line below would be its only witness.
    """
    ordered = sorted(
        wire["suggestions"],
        key=lambda r: (bool(r.get("reproducing")), r.get("lastSeenAt") or ""),
        reverse=True)

    def with_suggestions(kept: list) -> dict:
        return {**wire, "suggestions": kept,
                "components": components_to_wire(
                    registry, kept, runs_by_id=runs_by_id, verdicts_by_id=verdicts_by_id,
                    members_by_suggestion=members_by_suggestion)}

    return _trim_to_budget(wire, _budget(budget), ordered=ordered, rebuild=with_suggestions,
                           label="the global bundle")


def _fit_actor_bundle(bundle: dict, budget=None, actor_id: str = ""):
    """Trim an actor's bundle to ONE item; return ``(bundle, runs dropped, days rolled up)``.

    TWO RUNGS, AND THE ORDER IS THE WHOLE DESIGN. Two lists now grow inside one 400 KB item: the run
    rows, and the day rows. A run row is 1,146 bytes and 304 of them fit one item; ~30 day rows sit
    beside them.

      1. RUN ROWS give way first, oldest first, down to none. They are the EVIDENCE half — a judge's
         sentence, a window, a verdict — and every one of them is recomputed from the next harvest.
         Losing the oldest costs a reader detail about a session they can still see counted.
      2. DAY ROWS give way second, and are FOLDED INTO THE ROLLED-UP BUCKET rather than dropped. They
         are the only copy of a past day: the harvest window is 24h and `aws/spans` retains 30 days,
         so a dropped day can never be recovered by any later run. Folding costs the day's RESOLUTION
         and keeps its figures, so even a bundle trimmed to zero day rows still totals correctly.
      3. Nothing else yields. `evidence` and `patterns` are bounded by the published suggestion set,
         which `_fit_bundle` already caps; `waste.detail` is bounded by `waste._MAX_DETAIL_NAMES`; and
         `rolledUp` plus `historyFirstDate` are fixed-size and are what the cumulative figure IS. A
         bundle still over budget with both lists empty reaches `_trim_to_budget`'s fail-loud branch.

    So the cumulative figure survives a trim that the evidence does not, which is the right way round:
    `runsTotal` keeps the true request count visible beside a shortened `runs` list, and the tiles keep
    reading a complete total.
    """
    label = f"actor {actor_id[:8] or '?'}'s bundle"
    history = list(bundle.get(_HISTORY_KEY) or ())
    fitted, runs_dropped = _trim_to_budget(
        bundle, _budget(budget), ordered=list(bundle.get("runs") or ()),
        rebuild=lambda kept: {**bundle, "runs": kept}, label=label,
        # Not the last rung whenever there are day rows left to give, so an empty run list that is
        # still over budget reads as "trying the next one" rather than as a producer emergency.
        last_rung=not history)

    if not history:
        return fitted, runs_dropped, 0

    def with_days(kept: list) -> dict:
        # `kept` is a prefix of the newest-first ordering, so the survivors are the newest rows and
        # what left is the oldest. Rebuilt oldest-first, which is the order every reader expects.
        surviving = {row[_ROW_DATE_FIELD] for row in kept}
        evicted = [row for row in history if row[_ROW_DATE_FIELD] not in surviving]
        rolled = _roll_up(fitted.get(_ROLLED_UP_KEY) or {}, evicted)
        keep = sorted(kept, key=lambda row: row[_ROW_DATE_FIELD])
        spanned = [d for d in (rolled.get("fromDate"),
                               keep[0][_ROW_DATE_FIELD] if keep else None) if _usable_date(d)]
        return {**fitted, _HISTORY_KEY: keep, _ROLLED_UP_KEY: rolled,
                _HISTORY_FIRST_DATE_KEY: min(spanned) if spanned else _UNMEASURED_WIRE}

    fitted, days_rolled = _trim_to_budget(
        fitted, _budget(budget), ordered=list(reversed(history)), rebuild=with_days,
        label=f"{label}'s day rows")
    return fitted, runs_dropped, days_rolled


def _history_rows(limit=None) -> int:
    """How many daily rows to retain. ``IMPROVE_HISTORY_ROWS`` overrides; ``limit`` is the seam.

    Floored at 1 rather than trusted, because the floor prevents a footgun that does the exact
    OPPOSITE of what it reads: retention is applied as a tail slice, and ``rows[-0:]`` is the whole
    list, so ``IMPROVE_HISTORY_ROWS=0`` would retain everything forever instead of nothing.
    """
    if limit is not None:
        return max(1, limit)
    return max(1, _env_int("IMPROVE_HISTORY_ROWS", _HISTORY_ROWS))


def _history_row(scorecard: dict, waste: dict, *, date: str) -> dict:
    """One day's scalars, COPIED off the bundle this run is about to write. Nothing is recomputed.

    Ten fields, every one of them already published elsewhere in the same bundle, so a row can never
    disagree with the scorecard sitting beside it.

    Sentinels are the whole difficulty, and `dto._measured` is imported rather than reimplemented
    because it already IS the answer this pipeline settled on. Each spend term reaches the wire as
    ``{"sum": <number> | "UNMEASURED", ...}`` and on the deployed population most of them are
    unmeasured, so a row that defaulted a missing ``sum`` to ``0`` would draw a trend through
    fabricated zeros — reading as "free" or "instant" where the truth is that nobody measured, which
    is worse than no trend at all. ``passRate`` is the one field that arrives as ``None`` rather than
    as the sentinel (it has no denominator when nothing was scored), and `_measured` folds ``None``
    into ``"UNMEASURED"`` for the reason its own docstring gives: ``null`` styles like any other empty
    cell. So all ten fields share ONE spelling for "not measured" and a reader needs one rule.

    ``population`` and ``runsWithWaste`` are counts, so ``0`` is a measurement there — we looked and
    there were none — and `_measured` leaves a real ``0`` alone. Only absence becomes the sentinel.

    ``cost``, its two token counts, ``input`` and ``models`` were added once the deploy started
    measuring them: cost reached the live population as a real $20.17 at ``measuredOf 25 of 25`` with a
    per-model split, and a row without them makes the platform-wide cost, the context-vs-generated split
    and the model count unsummable over any period even though the numbers exist. They also make this
    row's spend terms the SAME SET a per-actor day row carries, which is what lets one fold rule serve
    both. ``costTokensPriced``/``costTokensUnpriced`` are what keep a summed cost readable: pricing skips
    a model with no rate row rather than failing, so a cumulative cost without them accumulates an
    unstated shortfall for as long as the history is kept.

    ``models`` is a LIST of ids and not ``modelsSeen``' count, because counts cannot be unioned: two
    days of "1 model" may be the same model or two. The ids come from ``spend.byModel``, so the list
    inherits `dto._SPEND_MODELS_NAMED` as its bound and a roster wider than that is short by whatever
    ``byModelOther`` withheld — measured at 2 ids against a bound of 20, so nowhere near binding.

    The four category terms stay FLAT here (``cacheRead`` beside ``tokens``) rather than nesting under
    ``byCategory`` the way a per-actor row does. Not a preference: rows written by earlier producer
    code are read back and folded, so changing this row's layout would make the fold meet two shapes
    and silently mis-add the older one. The field NAMES match; only the nesting differs.

    ``coverage`` carries each term's ``measuredOf`` and the population it is out of, COPIED off the
    scorecard's own per-term pair rather than recounted. It is what makes a period total readable: the
    sums beside it are lenient (`dto.summed`), so without it a figure covering two thirds of the
    population captions itself as complete — measured, tokens are recorded on 354 of 553 sessions.
    Per TERM because coverage differs between terms: ``latencyMs`` comes from spans and every token
    term from the usage record, so one number would caption both wrongly. Identical in shape to the
    per-actor day row's block, so one consumer reads both scopes.

    Its ``total`` is ``spend.tokens.total`` and not ``population.total``, even though they are the same
    number on every bundle written so far. They answer different questions — how many runs a spend term
    was measured OUT OF, versus how many runs were scored — and taking the spend denominator from the
    spend block is what keeps the pair internally consistent if they ever part.
    """
    spend = scorecard.get("spend") or {}
    by_category = spend.get("byCategory") or {}

    def term(block: dict, name: str):
        entry = block.get(name)
        return _measured(entry.get("sum") if isinstance(entry, dict) else None)

    def measured_of(block: dict, name: str) -> int:
        entry = block.get(name)
        count = entry.get("measuredOf") if isinstance(entry, dict) else None
        # A count, so a real 0 stays 0 and an ABSENT one is 0 too — this block may never contain a
        # sentinel. A missing count means no run measured the term, which is what 0 says; the
        # ``total`` beside it is what keeps that from reading as "complete over nothing".
        return count if _is_number(count) else 0

    named_models = [row.get("model") for row in (spend.get("byModel") or ())
                    if isinstance(row, dict) and isinstance(row.get("model"), str)
                    and row.get("model")]
    return {
        # The day this bundle was minted, taken from the SAME `createdAt` stamped onto the item, so
        # the row and the item's provenance cannot disagree about which day a point describes. A
        # fresh clock reading here could straddle midnight relative to that stamp.
        "date": date,
        "population": _measured((scorecard.get("population") or {}).get("total")),
        "passRate": _measured(scorecard.get("passRate")),
        "tokens": term(spend, "tokens"),
        "llmCalls": term(spend, "llmCalls"),
        "latencyMs": term(spend, "latencyMs"),
        "cost": term(spend, "cost"),
        # The two counts that say whether that cost is SHORT. Nested inside `spend.cost` on the
        # scorecard because there it hangs off a sum with denominators; flat here, like every other
        # term on this row, and named exactly as the per-actor day row names them.
        "costTokensPriced": _measured((spend.get("cost") or {}).get("tokensPriced")
                                      if isinstance(spend.get("cost"), dict) else None),
        "costTokensUnpriced": _measured((spend.get("cost") or {}).get("tokensUnpriced")
                                        if isinstance(spend.get("cost"), dict) else None),
        "input": term(by_category, "input"),
        "cacheRead": term(by_category, "cacheRead"),
        "cacheWrite": term(by_category, "cacheWrite"),
        "output": term(by_category, "output"),
        "models": named_models if named_models else _UNMEASURED_WIRE,
        "runsWithWaste": _measured(waste.get("runsWithWaste")),
        "coverage": {
            # The RUNS a spend term was measured out of. From the spend block rather than from
            # `population.total`, which answers the different question of how many were scored.
            "total": ((spend.get("tokens") or {}).get("total")
                      if _is_number((spend.get("tokens") or {}).get("total")) else 0),
            "tokens": measured_of(spend, "tokens"),
            "llmCalls": measured_of(spend, "llmCalls"),
            "latencyMs": measured_of(spend, "latencyMs"),
            "cost": measured_of(spend, "cost"),
            # Both priced counts share `cost`'s denominator: they are `dto._measured_total` sums over
            # the same runs `cost` was measured on, and pricing has no separate coverage of its own.
            "costTokensPriced": measured_of(spend, "cost"),
            "costTokensUnpriced": measured_of(spend, "cost"),
            "byCategory": {name: measured_of(by_category, name)
                           for name in ("input", "output", "cacheRead", "cacheWrite")},
        },
    }


def _union_ids(values: list):
    """Every id named across these rows, first-seen order, or the sentinel when none named any."""
    named: list = []
    for value in values:
        for entry in value if isinstance(value, (list, tuple)) else ():
            if isinstance(entry, str) and entry and entry not in named:
                named.append(entry)
    return named or _UNMEASURED_WIRE


def _weighted(rows: list, name: str, weight: str):
    """A rate folded against its own denominator, or the sentinel when the denominator is 0.

    Exact rather than approximate — see `_ROW_WEIGHTED_FIELDS`. A row missing either half contributes
    to neither, so a day whose rate was never measured cannot drag the fold toward zero.
    """
    pairs = [(row[name], row[weight]) for row in rows
             if _is_number(row.get(name)) and _is_number(row.get(weight))]
    total = sum(w for _, w in pairs)
    if not total:
        return _UNMEASURED_WIRE
    return sum(value * w for value, w in pairs) / total


def _fold_terms(blocks: list) -> dict:
    """Several rows' worth of figures combined into ONE, by the rules `_ROW_UNIONED_FIELDS`,
    `_ROW_WEIGHTED_FIELDS` and `_ROW_ALL_OR_NOTHING_FIELDS` name and by summing everything else.

    Field names are collected across the blocks rather than taken from any one of them, so a row
    written by earlier producer code folds with a row written by this one instead of dropping whichever
    fields the other lacks. A nested block (a per-actor row's ``byCategory``) recurses into the same
    rules, which is how one function serves two row layouts.

    A field ABSENT from every block stays absent from the fold, which is what keeps a mixed bucket
    honest rather than tidy. The per-actor row's ``pass``/``fail``/``unscored`` partition its ``runs``,
    and a bucket that folded rows from before those counts existed reports a partition SHORT of the
    ``runs`` beside it — which is the true statement that some of those days recorded no outcome.
    Zero-filling the older rows to make the three add up would be a fabricated measurement, and there
    is nothing to back-fill from: `aws/spans` retains 30 days and each row is the only copy of its day.

    `_ROW_ALL_OR_NOTHING_FIELDS` is the ONE exception to that paragraph, and states which property makes
    it one: a block carrying its own denominator cannot report a partial fold as short, so a partial
    fold of it is dropped rather than published.

    A block with NO fields is not a participant in the fold at all. `_roll_up` folds the prior bucket in
    as an empty one whenever nothing has been rolled up yet, and counting that as a block that LACKS
    every field would drop the all-or-nothing ones from every first roll-up — a bucket of one fully
    measured day claiming not to know its own coverage.

    The sum is `dto.summed` — the SAME function the mapper builds a row's terms with, not a copy of its
    rule. It has to be: a row is built from `Run.resources`, where absence is the sentinel object, and
    folded from a row read back out of DynamoDB, where only the sentinel's NAME survives. Two
    implementations would eventually disagree about which of those is a measurement.
    """
    blocks = [block for block in blocks if block]
    names: list = []
    for block in blocks:
        for name in block:
            if name not in names:
                names.append(name)
    folded: dict = {}
    for name in names:
        values = [block[name] for block in blocks if name in block]
        nested = [value for value in values if isinstance(value, dict)]
        if name in _ROW_UNIONED_FIELDS:
            folded[name] = _union_ids(values)
        elif name in _ROW_WEIGHTED_FIELDS:
            folded[name] = _weighted(blocks, name, _ROW_WEIGHTED_FIELDS[name])
        elif name in _ROW_ALL_OR_NOTHING_FIELDS and len(nested) < len(blocks):
            continue
        elif nested:
            folded[name] = _fold_terms(nested)
        else:
            folded[name] = summed(values)
    return folded


#: The bucket's own bookkeeping, as opposed to the figures IN it. Stripped before a fold and rebuilt
#: after, so `_fold_terms` never has to know that a bucket is anything other than a row.
_ROLLED_UP_BOOKKEEPING = ("days", "fromDate", "toDate")

#: The bucket before anything has ever been evicted. ``days: 0`` is a measurement — no day has been
#: rolled up — and the spend terms are ABSENT rather than ``0``: there is no day to report a figure
#: for, and ``tokens: 0`` here would be indistinguishable from a bucket of days that spent nothing.
_EMPTY_ROLLED_UP = {"days": 0, "fromDate": _UNMEASURED_WIRE, "toDate": _UNMEASURED_WIRE}


def _usable_date(value) -> bool:
    """Whether this is a date a row can be ordered and deduplicated by.

    One predicate for both callers. A row that cannot be placed on a time axis can be neither
    retained-by-recency nor replaced-by-day, and the sentinel is not a date however it is spelled.
    """
    return isinstance(value, str) and bool(value) and value != _UNMEASURED_WIRE


def _roll_up(prior: dict, evicted: list) -> dict:
    """The rolled-up bucket after ``evicted`` day rows join whatever ``prior`` already held.

    Composable by construction: the bucket has the same field shape as a row, so folding one more day
    into it is the same operation as folding two days together and re-rolling is not a special case.
    That is what makes the two eviction reasons — the row count and the byte ceiling — one code path.

    ``fromDate`` is never dragged forward by a later roll: a wide reseed can contribute a day EARLIER
    than anything the bucket held, and the caption has to be the earliest day actually covered.
    """
    rows = [row for row in evicted if isinstance(row, dict)]
    days = prior.get("days") if isinstance(prior, dict) else None
    days = days if _is_number(days) else 0
    if not rows:
        return dict(prior) if days else dict(_EMPTY_ROLLED_UP)
    carried = ({k: v for k, v in prior.items() if k not in _ROLLED_UP_BOOKKEEPING}
               if days else {})
    folded = _fold_terms([carried] + [{k: v for k, v in row.items() if k != _ROW_DATE_FIELD}
                                      for row in rows])
    spanned = sorted(
        [row[_ROW_DATE_FIELD] for row in rows if _usable_date(row.get(_ROW_DATE_FIELD))]
        + ([d for d in (prior.get("fromDate"), prior.get("toDate")) if _usable_date(d)]
           if days else []))
    return {**folded, "days": days + len(rows),
            "fromDate": spanned[0] if spanned else _UNMEASURED_WIRE,
            "toDate": spanned[-1] if spanned else _UNMEASURED_WIRE}


def _read_history(table, actor_id: str = SYSTEM_ACTOR) -> dict:
    """The daily history carried by the item this run is about to REPLACE.

    This read exists because the SNAPSHOT write is an unconditional PutItem of a whole item: anything
    on that item which this run does not carry forward is destroyed by it. Every other block in the
    bundle is recomputed from the harvest each run and is MEANT to be replaced. The history is the one
    cumulative part, and the only copy of it is the item being overwritten — so without this read, the
    history appended by one run is wiped by the next and the item can only ever hold a single point.

    Absent is not an error. A first run, a fresh environment, an item written before this block
    existed, and an item hand-seeded from a file by ``seed_snapshot`` (whose bundles carry no history)
    all arrive here and all start a new history rather than failing the run.

    A read that FAILS is deliberately NOT caught. It cannot be degraded around — a PutItem that
    cannot see the prior history destroys it — so swallowing the error would convert a loud failure
    into silent data loss. And it is not a new failure mode: `DdbSuggestionStore` already point-gets
    the same table and the same partition unguarded earlier in this run, so a table this run cannot
    read has already aborted it long before here.

    ``actor_id`` selects WHICH item, because the per-actor items have exactly the same problem and it
    is the same read. Their bundle is rebuilt from a 24h harvest and PutItem'd over the previous one
    every night, so a reader who ran nothing yesterday had no figures at all — which is the defect this
    parameter closes. One function rather than a per-actor copy: what differs is the partition key.

    Returns the whole history BLOCK — the rows and the rolled-up bucket — because they are one fact.
    Reading either without the other would double-count the evicted days or else lose them.
    ``Decimal`` is converted back to plain numbers here, once, so nothing downstream has to: the rows
    are arithmetic inputs now that they fold, and ``Decimal + float`` raises.
    """
    resp = table.get_item(Key={"actorId": actor_id, "sk": SNAPSHOT_SK})
    item = resp.get("Item")
    bundle = item.get("bundle") if isinstance(item, dict) else None
    if not isinstance(bundle, dict):
        return {}
    prior = bundle.get(_HISTORY_KEY)
    rolled = bundle.get(_ROLLED_UP_KEY)
    if prior is not None and not isinstance(prior, list):
        # A schema surprise rather than a data condition, so it is reported — but then started fresh
        # rather than raised. Nothing except the producer writes this item, so refusing would wedge
        # every future run over a value only the producer itself could repair.
        print(f"improve-worker: WARNING the prior {actor_id[:8]!r} SNAPSHOT's {_HISTORY_KEY!r} is a "
              f"{type(prior).__name__}, not a list — starting a new history.")
        prior = None
    if rolled is not None and not isinstance(rolled, dict):
        # Same treatment, and it must be separate: losing the bucket while keeping the rows would keep
        # the recent days and silently drop every older one from the cumulative figure.
        print(f"improve-worker: WARNING the prior {actor_id[:8]!r} SNAPSHOT's {_ROLLED_UP_KEY!r} is a "
              f"{type(rolled).__name__}, not an object — starting a new rolled-up bucket.")
        rolled = None
    block = {}
    if prior is not None:
        block[_HISTORY_KEY] = prior
    if rolled is not None:
        block[_ROLLED_UP_KEY] = rolled
    return _floatize(block)


def _fit_history(rows: list):
    """Split rows at the point the serialized block fits `_HISTORY_BUDGET_BYTES`; ``(kept, evicted)``.

    A backstop under the row count, not a second retention policy. It is here so that what the history
    costs the suggestion budget is a BOUND rather than an assumption, since the append happens before
    `_fit_bundle` measures. Measured on the widest row — a model id, every spend term spelled as the
    sentinel, and the coverage block — a population row serializes to 614 bytes and a per-actor day row
    to 620, so `_HISTORY_ROWS` rows are 18,480 and 18,600 bytes. This ceiling leaves room for a field
    added later while holding the block at 6.3% of `_ITEM_BUDGET_BYTES`, and that percentage is the
    whole justification for appending the history BEFORE the suggestion trim: these bytes come out of
    the suggestions' allowance, and this is the most they can ever take.

    Rows come out rather than being discarded, because the caller folds them into the rolled-up bucket:
    a day pushed out by a size bound is a day whose figures must survive in the cumulative total even
    though its own point is gone.

    The newest row always survives: a trend that dropped today's point to satisfy a size bound would
    have thrown away the only row this run produced. One row cannot approach the ceiling by itself, so
    the bound stays effective.
    """
    kept = list(rows)
    evicted: list = []
    while len(kept) > 1 and len(json.dumps(kept, default=str)) > _HISTORY_BUDGET_BYTES:
        evicted.append(kept.pop(0))
    if evicted:
        print(f"improve-worker: WARNING the daily history exceeded {_HISTORY_BUDGET_BYTES} bytes — "
              f"rolled up {len(evicted)} oldest row(s) ({len(kept)} kept). Either rows are wider than "
              f"the measurement this ceiling was set from, or IMPROVE_HISTORY_ROWS is set high.")
    return kept, evicted


def _merge_history(prior: dict, fresh: list, *, limit=None) -> tuple:
    """``(the history block, {what moved})`` — the whole cumulative mechanism, for either item.

    ONE row per day, keyed on ``date``, and a row this run produced REPLACES that day's prior row
    instead of adding one. Three reasons, most important first: a run recomputes each day from a fixed
    harvest window, so two runs covering one day are two measurements of ONE interval and a line drawn
    between them shows judge variance rather than change; the rest of this producer is already
    idempotent by replacement (every SNAPSHOT write is an unconditional PutItem), and a history that
    appended would be the single part of the item where a manual retry altered the shape of the result;
    and a retention bound counted in ROWS only means "about a month" if a day contributes exactly one.
    The later run wins: it is the more recent measurement of that day, and its bundle is the one served.

    THIS IS WHAT MAKES A WIDE RESEED IDEMPOTENT. ``fresh`` may cover many days (a manual 168h run does),
    and every one of them replaces its own day independently. Nothing is merged, nothing is appended,
    and re-running the same wide window twice leaves every figure where it was.

    The one day it CANNOT replace is one already folded into the rolled-up bucket. A fold is lossy by
    construction — the bucket holds sums, not days — so a fresh row for such a day could only be
    ADDED, double-counting a day already inside the total, or refused. It is refused, counted, and
    said out loud, because an inflated cumulative total is a wrong number nothing downstream can
    detect while a refusal is a bounded, visible loss. In practice this is nearly unreachable: `aws/spans`
    keeps 30 days and `_HISTORY_ROWS` retains 30, so a reseed cannot normally reach past the bucket.

    Rows are held oldest-first and sorted by ``date``, because that is what licenses the tail slice:
    "keep the newest N" is only "drop the oldest" when the newest are at the end.
    """
    prior_rows = prior.get(_HISTORY_KEY) or []
    rolled = prior.get(_ROLLED_UP_KEY) or {}
    # A row must be identifiable by day, or it can be neither deduplicated nor ordered. One that is
    # not is dropped rather than guessed at — and said out loud, since it should not happen. Applied
    # to BOTH sides here rather than by each caller, so a fresh row and a carried one cannot come to
    # be admitted by different rules.
    usable = [r for r in prior_rows if isinstance(r, dict) and _usable_date(r.get(_ROW_DATE_FIELD))]
    dated = [r for r in fresh if isinstance(r, dict) and _usable_date(r.get(_ROW_DATE_FIELD))]
    undated = (len(prior_rows) - len(usable)) + (len(fresh) - len(dated))
    if undated:
        print(f"improve-worker: WARNING dropped {undated} history row(s) with no usable date — a row "
              f"that cannot be placed on a time axis cannot be ordered or deduplicated.")

    # ``YYYY-MM-DD`` compares lexicographically exactly as it does chronologically, which is why this
    # is a string comparison and not a parse. It is not the ISO-timestamp comparison invariant 4
    # forbids: those carry an offset, and `'Z' > '+'` is what bites there. A date has neither.
    horizon = rolled.get("toDate") if isinstance(rolled, dict) else None
    horizon = horizon if _usable_date(horizon) else ""
    accepted = [r for r in dated if not (horizon and r[_ROW_DATE_FIELD] <= horizon)]
    refused = len(dated) - len(accepted)
    if refused:
        print(f"improve-worker: WARNING refused {refused} freshly-computed day row(s) dated on or "
              f"before {horizon}, which is already inside the rolled-up bucket. A rolled-up day cannot "
              f"be replaced because the fold kept its figures and not its days, so adding one would "
              f"double-count it. Widen IMPROVE_HISTORY_ROWS before reseeding that far back.")

    replaced = {r[_ROW_DATE_FIELD] for r in accepted}
    rows = sorted([r for r in usable if r[_ROW_DATE_FIELD] not in replaced] + accepted,
                  key=lambda r: r[_ROW_DATE_FIELD])
    retained = _history_rows(limit)
    evicted = rows[:-retained] if len(rows) > retained else []
    kept, over_budget = _fit_history(rows[len(evicted):])
    rolled = _roll_up(rolled, evicted + over_budget)
    # The earliest day ANY figure here covers. From the bucket first, because it holds the older half —
    # taking it from the retained rows alone would caption a total with the retention horizon and
    # understate its own reach, which is the specific mislabel this key exists to prevent.
    spanned = [d for d in (rolled.get("fromDate"),
                           kept[0][_ROW_DATE_FIELD] if kept else None) if _usable_date(d)]
    block = {_HISTORY_KEY: kept, _ROLLED_UP_KEY: rolled,
             _HISTORY_FIRST_DATE_KEY: min(spanned) if spanned else _UNMEASURED_WIRE}
    return block, {"rows": len(kept), "rolled_up": len(evicted) + len(over_budget),
                   "refused": refused}


def _with_history(wire: dict, prior: dict, *, date: str, limit=None):
    """``(wire carrying the population's daily history, {what moved})``.

    One day row per producer run, because the population block IS one aggregate over the whole harvest
    window and there is nothing per-day inside it to split. `_merge_history` does the rest, so the
    population and the per-actor items cannot drift about retention, replacement or roll-up.
    """
    row = _history_row(wire.get("scorecard") or {}, wire.get("waste") or {}, date=date)
    block, moved = _merge_history(prior, [row], limit=limit)
    return {**wire, **block}, moved


def _with_actor_history(bundle: dict, prior: dict, *, limit=None):
    """``(bundle carrying this actor's daily history, {what moved})``.

    The fresh rows arrive ON the bundle: `dto.actor_bundles` builds one per day the actor had activity,
    because only the mapper knows which runs fall on which day. Everything cumulative — replacement by
    day, retention, the roll-up, the first date — is `_merge_history`, the same code the population
    history uses. That is the whole point: there is one mechanism, not a second one shaped like it.
    """
    block, moved = _merge_history(prior, list(bundle.get(_HISTORY_KEY) or ()), limit=limit)
    return {**bundle, **block}, moved


def _actor_item(actor_id: str, bundle: dict, *, created_at: str, judge_config_hash: str,
                code_version: str) -> dict:
    """One actor's SNAPSHOT item, refusing the reserved partition.

    ``build_item`` is the same builder the shared bundle uses, so both items are stamped one way.
    Floats become Decimal for the resource layer, which rejects ``float``.

    An actor id equal to ``SYSTEM_ACTOR`` would land on the SAME key as the shared bundle and
    replace it for every actor, and the id space does not rule that out: measured against the
    staging conversation memory, 6 of 14 actor ids are not UUIDs (four 85-character ``EXT_…``
    external-caller principals, ``local-dev-user``, ``slack-demo``). So the reservation is enforced
    here rather than assumed from the shape of an id, and it fails the run rather than destroying
    the shared item. An actor genuinely named that needs the reserved name changed, not this guard
    relaxed.
    """
    if actor_id == SYSTEM_ACTOR:
        raise RuntimeError(
            f"refusing to write a per-actor bundle for {actor_id!r}: that is the reserved "
            f"partition holding the shared bundle, so this write would replace it for every actor.")
    if not actor_id:
        raise RuntimeError("refusing to write a per-actor bundle with no actor id: an empty "
                          "partition key would collect every unowned run into one item.")
    return build_item(bundle=_decimalize(bundle), created_at=created_at,
                      judge_config_hash=judge_config_hash, code_version=code_version,
                      actor_id=actor_id)


#: Where the harvest lands when nothing names a directory. Lambda gives the function ``/tmp``, which
#: is the only writable path in the execution environment.
_DEFAULT_HARVEST_DIR = "/tmp/improve-harvest"

#: How many discovered sessions ONE cycle scores, newest first. OPERATIONAL, so ``IMPROVE_LIMIT``
#: moves it — and the deploy sets it explicitly, so this value is the fallback for a cycle run with
#: no environment (a script, a test, a notebook). It is kept in step with the deployed value on
#: purpose: a fallback that scored a different population than production would make every local
#: reproduction of a cloud run silently incomparable.
#:
#: It is a NUMBER OF SESSIONS and not a cost estimate, because those two do not scale together.
#: `observe.population.discover_sessions` returns every session with enough spans; only the ones
#: `classify` calls CONVERSATIONAL reach the Bedrock judge, and a conversational run with neither a
#: request nor a response short-circuits before the call (`judge.GoalSuccessJudge.judge`). So the
#: judge bill tracks the conversational share, measured 14 of 25 on the deployed window, and raising
#: this bound admits plan sub-sessions and harness work for free.
#:
#: 60 clears a whole day rather than sampling one — see the `IMPROVE_LIMIT` comment in
#: `infra/lib/agent-infra-stack.ts` for the measurement and the 900s time budget behind it. The cap
#: still exists, and must: nothing bounds how busy a day can get, and an unbounded population
#: against a fixed ceiling is a producer that stops finishing.
_POPULATION_LIMIT = 60


@contextlib.contextmanager
def _scoped_environ(**values: str):
    """Set environment variables for the duration of the block, then put the environment back.

    Restored on the way out — including being REMOVED again when they were absent on the way in, so
    the block cannot leave a variable behind that its caller never set. The restore is in a
    ``finally`` because a cycle that raises is exactly when a leaked variable does damage: the next
    attempt in the same process would start from the failed run's configuration.

    Under Lambda the difference is invisible: one invocation per process and the container is
    eventually discarded. Anywhere else the process outlives the cycle — a test module, a notebook, a
    script that runs two cycles over different harvests — and a variable left set silently
    reconfigures whatever runs next, so it is the ORDER of unrelated work that decides what a reader
    points at.
    """
    prior = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, before in prior.items():
            if before is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = before


def _zero_actor_bundle() -> dict:
    """A bundle for an actor the window found nothing for — a MEASURED zero, not a blank.

    This exists so that ``0`` and "we do not know" stop rendering the same. Two different facts used to
    produce the same empty surface: a reader who genuinely ran nothing, and a reader for whom the
    producer had never written anything at all. Only the producer can tell them apart, and only by
    writing for the first and not the second — so an actor named by conversation memory gets this, and
    an actor nothing has ever heard of still gets no item and therefore the sentinel.

    IT REVERSES A DELIBERATE CHOICE, and the reason it is now safe is the history. The write was skipped
    because an empty bundle would have replaced real evidence with a measured-looking blank, which was
    true while the item held only the last 24 hours. Now the day rows are read off the item and
    carried forward, so a bundle with no runs today cannot erase the rows from the days there were:
    today contributes no day row at all (`dto._day_rows` emits one only for a day with activity), and
    every earlier row survives.

    Every figure here is a real measurement over an examined window. ``runsTotal: 0`` is "we swept and
    you had none", which is why it is ``0`` and not the sentinel — the sentinel is what the ABSENCE of
    this whole item means.
    """
    return {
        "runs": [],
        "runsTotal": 0,
        _HISTORY_KEY: [],
        "evidence": {},
        # Through the real mapper, so an actor with nothing is described by the same code as an actor
        # with something. Hand-writing `{"runsWithWaste": 0, ...}` here would be a second definition of
        # the empty waste block, free to drift from the one every other bundle gets.
        "waste": waste_to_wire({"findings": [], "runs_examined": 0, "runs_not_scanned": []}),
        "patterns": {"admitted": [], "residue": [], "evidenceGaps": [], "environment": [],
                     "unattributed": []},
    }


def _known_actors(ctx: dict) -> tuple:
    """``([every actor this harness can name], whether the roster is complete)``.

    Conversation memory is the roster, and it is the RIGHT one rather than a convenient one: an actor's
    id reaches this producer only because Memory named it (`build_run` reads the owner from there), so
    every actor that has ever had a bundle is in this list, and an actor NOT in it is one the harness
    has genuinely never seen. Scanning the store's own partitions would answer a narrower question —
    who already has an item — and could never produce a first zero for a reader who is new.

    Free: `run_pipeline` has already swept it to resolve owners and the reader caches it, so this is a
    read of a list already in memory rather than a second ``list_actors``.

    The completeness flag travels because a truncated roster silently withholds someone's zero, and a
    withheld zero renders as the unknown sentinel — the exact confusion this whole path removes.
    """
    reader = ctx.get("memory_reader")
    if reader is None:
        return (), None
    return tuple(reader.actor_ids()), reader.actor_list_complete()


def run(event=None, context=None) -> dict:
    """Do one full producer cycle. Returns a JSON-serializable summary (also the Lambda result).

    The evidence sources are told where to read by ENVIRONMENT rather than by argument, and that is
    not incidental: each resolves its own path at call time from deep inside ``run_pipeline``
    (``registry.default_registry_path``, ``harvest_live.default_out_dir``,
    ``observe.span_trajectory.harvest_root``), and nothing threads a path down to them. So the two
    variables are settled HERE, once, before anything reads them.

    They are settled for the duration of THIS CALL and no longer. The cycle is a function, not a
    process, so a permanent edit to the ambient environment would be a side effect on every later
    reader in the same interpreter rather than configuration of this one — see ``_scoped_environ``.
    """
    with _scoped_environ(
            # Defaults, not overrides: a deploy or an operator that names either path wins. The
            # registry is the bundled JSON copy, because the managed runtime has no PyYAML.
            IMPROVE_HARVEST_DIR=os.environ.get("IMPROVE_HARVEST_DIR") or _DEFAULT_HARVEST_DIR,
            IMPROVE_SIGMA_REGISTRY=(os.environ.get("IMPROVE_SIGMA_REGISTRY")
                                    or _data_path("sigma-registry.json"))):
        return _run_cycle(event, context)


def _run_cycle(event=None, context=None) -> dict:
    """The cycle itself, with the source paths already in the environment (see ``run``)."""
    region = os.environ.get("AWS_REGION") or os.environ.get("IMPROVE_REGION") or "us-west-2"
    table_name = os.environ.get("IMPROVE_TABLE")
    if not table_name:
        # No default table on purpose — mirrors `seed_snapshot`'s safety rule: naming the target
        # is the operator's decision, so a misconfigured deploy fails loud instead of guessing.
        raise RuntimeError("IMPROVE_TABLE is required (the Improve store table name).")

    # Read back rather than re-derived: `run` has put the resolved path here, and the harvest writer
    # below and the `Harvested*` readers inside `run_pipeline` must not be able to disagree about it.
    harvest_dir = os.environ["IMPROVE_HARVEST_DIR"]
    hours = _env_int("IMPROVE_HARVEST_HOURS", 24)
    limit = _env_int("IMPROVE_LIMIT", _POPULATION_LIMIT)
    runs_override = os.environ.get("IMPROVE_JUDGE_RUNS")
    code_version = os.environ.get("IMPROVE_CODE_VERSION", "UNKNOWN")

    # 1. Load the config as a DICT (dodges loop.config.yaml — the runtime has no PyYAML) and,
    #    if asked, cap judge runs for the time budget (config ships runs=3; 1 is cheapest).
    with open(_data_path("loop.config.json")) as f:
        cfg = json.load(f)
    if runs_override:
        cfg.setdefault("judge", {})["runs"] = _env_int("IMPROVE_JUDGE_RUNS", cfg["judge"].get("runs", 1))
    # The deployed memory wins over the id baked into loop.config.json — CDK binds
    # `conversationMemory.attrMemoryId` into IMPROVE_MEMORY_ID, so the reader can never point at
    # a stale memory the JSON happened to name. run_pipeline reads observe.memory_id from config.
    memory_id = os.environ.get("IMPROVE_MEMORY_ID") or (cfg.get("observe") or {}).get("memory_id")
    if memory_id:
        cfg.setdefault("observe", {})["memory_id"] = memory_id

    # 2. Pre-flight the memory read BEFORE the (expensive) harvest, so a missing grant or a stale
    #    runtime boto3 aborts fast and loud instead of crashing the first session post-harvest.
    _preflight_memory(memory_id, region)

    # 3. Harvest fresh telemetry into the paths the Harvested* sources read.
    harvest_live(out_dir=harvest_dir, hours=hours, region=region)

    # 4. Mine + score the harvested population with the live judge.
    ctx: dict = {}
    result = run_pipeline(mode="live", population="harvest", limit=limit,
                          config=cfg, context=ctx)

    # 5. Persist to the cross-run store and emit the wire bundle — this is `mine_local`'s
    #    `--emit-wire` block, verbatim except DdbSuggestionStore replaces the file store.
    created_at = os.environ.get("IMPROVE_CREATED_AT") or _now_iso()
    judge_cfg = cfg.get("judge", {}) or {}
    judge_config_hash = (
        f"{judge_cfg.get('model', '?')}:{judge_cfg.get('goal_source', '?')}"
        f":{judge_cfg.get('pass_threshold', '?')}:runs{judge_cfg.get('runs', '?')}")

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    store = DdbSuggestionStore(table)
    observed = to_wire(
        result=result, runs=ctx["runs"], registry=ctx["registry"], created_at=created_at,
        judge_config_hash=judge_config_hash, judge_runs=int(judge_cfg.get("runs") or 0),
        evidence_sources=["aws/spans", "ddb:plan-state", "harvest:live"],
        # The scorecard and ComponentRow.health are the only run-level facts on this wire, and
        # both are aggregates: the verdicts are read for scores, outcomes and workload class,
        # never for the judge's prose (this item is global — see `dto`'s content rule).
        verdicts=ctx["verdicts"])
    # Scrubbed on the way IN as well as out. Defence in depth, not the fix: an already-poisoned row
    # that never reproduces again is never rewritten, so the read-time funnel below is the
    # mandatory half. This one stops the store acquiring anything new to replay.
    change = store.upsert_all(
        [publishable_suggestion(row) for row in observed["suggestions"]], seen_at=created_at)
    store.save()
    runs_by_id = {run.run_id: run for run in ctx["runs"]}
    verdicts_by_id = {verdict.run_id: verdict for verdict in ctx["verdicts"]}

    # Which runs attribute to which suggestion. Taken from the RESULT, because no suggestion carries
    # its member ids onto the wire — a store row from an earlier run is simply absent from the map,
    # which `component_health` already reads as "no surviving member".
    members_by_suggestion = suggestion_members(result)
    # Emit the STORE's view rather than this run's, so a finding whose evidence expired stays
    # visible with reproducing=False — through `published_suggestions_block`, which is the whole
    # publish rule (status normalisation, the field projection, AND the pre-trim count that makes
    # `_fit_bundle`'s drop legible on the item) in one place. The store is a REPLAY CHANNEL that
    # bypasses `to_wire`: it keeps each suggestion as it stood when written and never rewrites one
    # that stops reproducing, so a row minted by older producer code still carries that code's
    # fields. Do not re-implement any part of this here; `mine_local` calls the same function.
    wire = {**observed,
            **published_suggestions_block(store.all_suggestions(include_dismissed=True))}
    wire["components"] = components_to_wire(ctx["registry"], wire["suggestions"],
                                            runs_by_id=runs_by_id,
                                            verdicts_by_id=verdicts_by_id,
                                            members_by_suggestion=members_by_suggestion)

    # 6. Guard the bundle before it becomes the served SNAPSHOT: refuse one missing a top-level
    #    key (the fail-loud check `seed_snapshot.load_bundle` did, which build_item does NOT), and
    #    trim to fit ONE DynamoDB item so an ever-growing store can't wedge the write forever.
    _require_bundle_keys(wire)

    #    Then append today's scalars to the rolling daily history, carried forward off the item this
    #    write is about to replace. BEFORE `_fit_bundle`, not after, so the size the fit measures is
    #    the size actually written — appending afterwards would put bytes outside the measurement
    #    whose entire purpose is to keep the item under the store's hard cap, and a put_item the fit
    #    said would succeed but which is rejected leaves the old item served stale forever with no
    #    later run able to unstick it. The price is that the history's bytes come out of the
    #    suggestions' allowance, so an over-budget bundle drops a suggestion or two sooner than it
    #    otherwise would; `_HISTORY_BUDGET_BYTES` is what caps that price. The history itself is not
    #    at risk from the trim: `_fit_bundle` rebuilds around the suggestions list and carries every
    #    other key through untouched.
    wire, history_moved = _with_history(wire, _read_history(table), date=created_at[:10])

    wire, dropped = _fit_bundle(wire, ctx["registry"], runs_by_id=runs_by_id,
                                verdicts_by_id=verdicts_by_id,
                                members_by_suggestion=members_by_suggestion)

    # 7. Write the global SNAPSHOT — the same item `seed_snapshot` wrote by hand. Floats
    #    become Decimal for the resource layer (which rejects `float`).
    item = build_item(bundle=_decimalize(wire), created_at=created_at,
                      judge_config_hash=judge_config_hash, code_version=code_version)
    table.put_item(Item=item)

    # 8. Write one item per ACTOR, holding the evidence that may not travel on the shared item:
    #    their run rows with the judge's reason, the facet evidence behind the suggestions their
    #    runs support, their waste findings with the tool sequence named, their session ids.
    #    Same key layout, same `SNAPSHOT` sort key, their own partition — so a read is scoped by
    #    the caller's verified token and nothing else.
    #
    #    Each write is an unconditional PutItem on (actorId, SNAPSHOT), so a re-run replaces the
    #    actor's bundle rather than appending to it: the producer is a daily cron and every run
    #    recomputes from the harvest, so idempotence is the required behaviour, not a nicety.
    #
    #    EVERY ACTOR CONVERSATION MEMORY CAN NAME gets one, including an actor with no run in this
    #    window — see `_zero_actor_bundle` for why that is now safe and what it buys. Their day rows
    #    are read back off their own item first, for the same reason the global history is: this write
    #    is an unconditional PutItem, so a row it does not carry forward is destroyed by it. That is
    #    one GetItem per actor, which is what the roster costs and is bounded by the roster.
    actor_stats: dict = {}
    actor_wire = actor_bundles(result=result, runs=ctx["runs"], verdicts=ctx["verdicts"],
                              stats=actor_stats)
    known, roster_complete = _known_actors(ctx)
    # Union, not a replacement. The roster is the authority on who EXISTS, but an actor whose owner
    # this run resolved is real whether or not the roster read succeeded — and if the roster came back
    # empty (no reader in context, a truncated sweep, a refused call), the actors with evidence must
    # still get their items rather than the run silently writing nothing.
    zero_bundles = 0
    for actor_id in known:
        if actor_id not in actor_wire:
            actor_wire[actor_id] = _zero_actor_bundle()
            zero_bundles += 1
    actor_runs_written = 0
    actor_runs_dropped = 0
    actor_days_written = 0
    actor_days_rolled_up = 0
    actor_days_refused = 0
    for actor_id, bundle in sorted(actor_wire.items()):
        _require_bundle_keys(bundle, _REQUIRED_ACTOR_BUNDLE_KEYS)
        bundle, moved = _with_actor_history(bundle, _read_history(table, actor_id))
        # Qualified so it cannot shadow the global fit's count, which the summary reports separately.
        bundle, actor_dropped, days_rolled = _fit_actor_bundle(bundle, actor_id=actor_id)
        actor_runs_written += len(bundle["runs"])
        actor_runs_dropped += actor_dropped
        actor_days_written += len(bundle[_HISTORY_KEY])
        actor_days_rolled_up += moved["rolled_up"] + days_rolled
        actor_days_refused += moved["refused"]
        table.put_item(Item=_actor_item(
            actor_id, bundle, created_at=created_at,
            judge_config_hash=judge_config_hash, code_version=code_version))

    summary = {
        "ok": True,
        "createdAt": created_at,
        "population_size": result.get("population_size"),
        "suggestions": len(wire["suggestions"]),
        "suggestions_dropped_to_fit": dropped,
        # Counts and rates only — this line goes to CloudWatch Logs. It is here so a deploy can
        # be verified from the log summary rather than by reading the SNAPSHOT item.
        "scorecard": {"population": wire["scorecard"]["population"],
                      "passRate": wire["scorecard"]["passRate"]},
        # The rolling daily history, so a run that quietly stopped accumulating is visible in the log
        # rather than only by reading the item. `rows` is what the bundle now carries, `rolled_up` what
        # the retention count and the size ceiling folded into the bucket, `refused` a reseed reaching
        # further back than the bucket (see `_merge_history`) — which is a lost day and not a trim.
        "history": history_moved,
        # Counts, never the actor ids — this line goes to CloudWatch Logs, which is not the
        # per-actor boundary. `unattributable` is here because it is the one number that is
        # invisible otherwise: those runs are scored and counted and reach no actor's surface.
        #
        # `zero_bundles` is the roster's whole point made checkable: it is how many readers were told
        # "you ran nothing" rather than left to render the unknown sentinel. Counted where the bundle
        # is MINTED and not by asking a finished bundle whether `runsTotal` is 0, because an actor
        # whose only spend was a sub-session with no request in the population also has `runsTotal: 0`
        # and did NOT run nothing. `roster_complete` is `False` when the actor sweep truncated, in
        # which case some reader's zero was withheld and reads as the unknown sentinel instead.
        #
        # The three fan-out numbers are the counting rule made auditable, and they are here rather than
        # on the wire because the wire's row shape is what the surface is coded against.
        # `sub_sessions_without_a_request` is the one to watch: those are plan steps whose own request
        # fell outside `IMPROVE_LIMIT`, so their spend reaches the reader's day rows but no run row —
        # a non-zero number here means the cap is cutting requests away from their own steps.
        "actors": {"bundles": len(actor_wire),
                   "zero_bundles": zero_bundles,
                   "roster": len(known),
                   "roster_complete": roster_complete,
                   "runs": actor_runs_written,
                   "runs_dropped_to_fit": actor_runs_dropped,
                   "history_rows": actor_days_written,
                   "history_rolled_up": actor_days_rolled_up,
                   "history_days_refused": actor_days_refused,
                   "fanned_out_requests": actor_stats.get("fanned_out_requests"),
                   "sub_sessions_folded": actor_stats.get("sub_sessions_folded"),
                   "sub_sessions_without_a_request":
                       actor_stats.get("sub_sessions_without_a_request"),
                   "runs_with_no_window": actor_stats.get("runs_with_no_window"),
                   "unattributable_runs":
                       wire["scorecard"]["attribution"]["unattributable"]},
        "components": wire["components"]["registryMeta"]["componentCount"],
        "store": {"added": len(change["added"]), "updated": len(change["updated"]),
                  "not_reproducing": len(change["quiet"]), "total": change["total"]},
        "registry_available": result.get("registry", {}).get("available"),
        "table": table_name,
        "harvest_dir": harvest_dir,
    }
    print(f"improve-worker: {json.dumps(summary)}")
    return summary


def handler(event=None, context=None) -> dict:
    """Lambda entrypoint. Configure the function's handler as ``improve.aws_lambda.handler``."""
    return run(event, context)


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))
