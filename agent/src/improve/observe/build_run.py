"""Assemble a Run from real evidence: trajectory + tokens (spans), response
(Memory), plan (plan-state).

Offline sources back the golden-set path; live sources back fresh runs. The Run
shape is identical across both — only where the fields are sourced differs.
Absent-but-expected fields carry NO_DATA; harness_snapshot is UNKNOWN in M1.

``window`` is READ from whatever collected the run, never computed here. Deriving it from
``trajectory`` would be one line and is deliberately not written: spans are the first thing
to vanish when the platform breaks, so a span-derived window would be narrowest or absent in
exactly the runs whose infrastructure context matters most, and a missing window reads as "no
infrastructure problem". If no collector recorded one, the field is UNKNOWN and
``infra_health`` says why.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

try:
    from ..contract import (COST_UNMEASURED_REASON, NO_DATA, UNKNOWN, UNMEASURED, Plan, PlanNode,
                            Run, RunWindow, Scenario)
    from .infra_health import AlarmSource, infra_health_for_window
    from .span_trajectory import (
        HarvestedSpanSource, TrajectorySource, default_sessions_path, default_sessions_paths,
        default_usage_audit_path, harvest_dir, existing_harvests, trajectory_for_session)
    from .memory_reader import MemoryReader
except ImportError:
    from improve.contract import (COST_UNMEASURED_REASON, NO_DATA, UNKNOWN, UNMEASURED, Plan,  # type: ignore
                                  PlanNode, Run, RunWindow, Scenario)
    from improve.observe.infra_health import AlarmSource, infra_health_for_window  # type: ignore
    from improve.observe.span_trajectory import (  # type: ignore
        HarvestedSpanSource, TrajectorySource, default_sessions_path, default_sessions_paths,
        default_usage_audit_path, harvest_dir, existing_harvests, trajectory_for_session)
    from improve.observe.memory_reader import MemoryReader  # type: ignore


def _sort_key_parts(row: dict) -> tuple:
    """(plan_id, node) of a harvested plan-state row, for either key format.

    New plans key rows ``PLAN#<plan_id>`` / ``<plan_id>#<node>``; older ones ``PLAN`` / ``<node>``
    (plan_id ``""``). The plan's own row reads as node ``"PLAN"`` in both.
    """
    sk = row.get("node_id", {}).get("S", "")
    if sk == "PLAN":
        return "", "PLAN"
    if sk.startswith("PLAN#"):
        return sk[len("PLAN#"):], "PLAN"
    if "#" in sk:
        plan_id, node = sk.split("#", 1)
        return plan_id, node
    return "", sk


def _latest_plan_rows(rows: list) -> list:
    """The rows of a session's newest plan.

    A session can hold several plans now that rows are keyed by plan id; the newest ``PLAN#*``
    row by ``updated_at`` is the session's plan, with its ``<plan_id>#*`` rows. A session with
    only legacy rows (one ``PLAN`` row, bare node rows) is returned unchanged.
    """
    plans = [r for r in rows if _sort_key_parts(r)[1] == "PLAN" and _sort_key_parts(r)[0]]
    if not plans:
        return rows
    newest = max(plans, key=lambda r: r.get("updated_at", {}).get("S", ""))
    plan_id = _sort_key_parts(newest)[0]
    return [r for r in rows if _sort_key_parts(r)[0] == plan_id]


def _harvest_dir() -> str:
    """Kept as a local name; the rule itself lives in ``span_trajectory.harvest_dir``."""
    return harvest_dir()


class HarvestedPlanSource:
    """Plan-state rows from the offline harvest. Returns Plan or NO_DATA.

    Every row is a node, including the PLAN parent — but PLAN-row status is
    unreliable (stuck 'pending' even on success), so downstream evaluators read
    task-node terminality, not the PLAN row.
    """

    def __init__(self, plan_state_path: Optional[str] = None) -> None:
        if plan_state_path is None:
            # Merged, oldest first, same rule as the sessions harvest: a plan resolves from
            # whichever harvest holds it. See `existing_harvests`.
            self._paths = existing_harvests(
                os.path.join(_harvest_dir(), "fresh", "plan-state-fresh.json"),
                os.path.join(_harvest_dir(), "ddb", "plan-state.json"),
            )
        else:
            self._paths = [plan_state_path]
        self._path = self._paths[-1] if self._paths else ""
        self._by_session: Optional[dict] = None

    def _load(self) -> dict:
        if self._by_session is None:
            self._by_session = {}
            # A session's rows come from whichever harvest holds them. Oldest first, and a later
            # harvest REPLACES a session's rows rather than appending, so a re-harvest does not
            # leave two generations of node rows interleaved for the same plan.
            for path in self._paths:
                try:
                    with open(path) as f:
                        raw = json.load(f)
                    rows = raw["Items"] if isinstance(raw, dict) and "Items" in raw else raw
                except FileNotFoundError:
                    continue
                seen_here: dict = {}
                for item in rows:
                    session_id = item.get("session_id", {}).get("S")
                    if session_id:
                        seen_here.setdefault(session_id, []).append(item)
                self._by_session.update(seen_here)
        return self._by_session

    def plan_for(self, session_id: str):
        rows = self._load().get(session_id)
        if not rows:
            return NO_DATA
        rows = _latest_plan_rows(rows)
        goal = ""
        nodes: list[PlanNode] = []
        for row in rows:
            node_id = _sort_key_parts(row)[1]
            status = row.get("status", {}).get("S", "")
            if "goal" in row and not goal:
                goal = row["goal"]["S"]
            deps_raw = row.get("depends_on", {}).get("S", "")
            deps = []
            if deps_raw:
                try:
                    deps = json.loads(deps_raw)
                except (json.JSONDecodeError, TypeError):
                    deps = []
            nodes.append(PlanNode(
                id=node_id,
                depends_on=deps,
                status=status,
                updated_at=row.get("updated_at", {}).get("S", ""),
                gateway_status=row.get("gateway_status", {}).get("S", ""),
            ))

        # The per-node `depends_on` ATTRIBUTE is empty in practice — the edges live
        # only inside the PLAN row's `nodes_json`. Without this backfill every plan
        # reads as edgeless and no ordering check can fire.
        #
        # `edges_available` carries the difference between "this plan has no edges" and "we
        # could not read this plan's edges". Downstream, mining reads edges to decide
        # `flat_plan` / `has_fanout_node` / `edge_order_violation`, so conflating the two
        # would let an offloaded wide fan-out plan be mined as clean and flat — and, if it
        # passed, be counted as a control that a signature successfully excluded.
        edges, available = self._edges_from_plan_row(rows)
        if edges:
            for n in nodes:
                if not n.depends_on and n.id in edges:
                    n.depends_on = edges[n.id]
        return Plan(goal=goal, nodes=nodes, edges_available=available)

    @staticmethod
    def _edges_from_plan_row(rows) -> tuple:
        """``({node_id: [dep_ids]}, edges_available)`` from the PLAN row.

        ``edges_available`` is False when the edge list existed but could not be read, so the
        caller can mark the Plan rather than present an edgeless one as a fact. Returning a
        bare ``{}`` for both cases is what let an unreadable plan read as flat.

        Mirrors the chainer's ``_load_nodes_json``: read inline ``nodes_json`` first,
        else follow the ``nodes_s3_key`` pointer. A wide fan-out plan breaches
        DynamoDB's 400KB item cap, so the writer offloads the node list to
        ``plans/<session>/<plan>/nodes.json`` and blanks the inline field
        (``plan_and_execute_tool.py``, >300_000 bytes). Reading only the inline field
        yields an EDGELESS plan for exactly those plans — which silently disables the
        dependency-order check in ``dag_completion``. Absence must be loud, so the
        offload case is reported rather than degraded to "no edges".
        """
        for row in rows:
            if _sort_key_parts(row)[1] != "PLAN":
                continue
            raw = row.get("nodes_json", {}).get("S") or ""
            if not raw:
                key = row.get("nodes_s3_key", {}).get("S") or ""
                if key:
                    raw = HarvestedPlanSource._read_offloaded_nodes(key)
                if not raw:
                    if key:
                        logger.warning(
                            "plan %s: nodes_json offloaded to %s and not readable offline — "
                            "edges unavailable, so the dependency-ORDER check cannot run",
                            row.get("plan_id", {}).get("S", "?"), key)
                        return {}, False
                    # No inline field AND no offload pointer: this plan genuinely records
                    # no edges, which is a fact rather than a gap.
                    return {}, True
            try:
                spec = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("plan %s: nodes_json present but unparseable — edges unknown",
                               row.get("plan_id", {}).get("S", "?"))
                return {}, False
            return ({n["id"]: list(n.get("depends_on") or [])
                     for n in spec if isinstance(n, dict) and n.get("id")}, True)
        # No PLAN row at all. The per-node `depends_on` attributes are the only edge source
        # then, and they are empty in practice — so edges are unknown, not absent.
        return {}, False

    @staticmethod
    def _read_offloaded_nodes(key: str) -> str:
        """Offloaded nodes.json, from the local harvest if present, else S3.

        Offline-first so the golden path stays deterministic and creds-free; the S3
        read is the live fallback. Returns "" when neither is available.
        """
        local = os.path.join(_harvest_dir(), "fresh", "nodes-json", os.path.basename(key))
        if os.path.exists(local):
            with open(local) as f:
                return f.read()
        bucket = os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("IMPROVE_SKILLS_BUCKET")
        if not bucket:
            return ""
        try:
            import boto3
            body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
            return body.decode("utf-8")
        except Exception as e:  # noqa: BLE001 — absence is reported by the caller
            logger.warning("could not read offloaded nodes_json s3://%s/%s: %s", bucket, key, e)
            return ""


class WindowSource(Protocol):
    """Yields the wall-clock ``RunWindow`` a collector recorded for a session, or UNKNOWN."""

    def window_for(self, session_id: str): ...


class HarvestedWindowSource:
    """Reads the interval a collector STAMPED for a session. Never derives one.

    Two files, in precedence order:

      1. the sessions harvest itself (``fresh/sessions-fresh.json`` -> ``sessions/sessions.json``),
         whose per-session ``window`` key is written by ``harvest.py`` at collection time;
      2. a sidecar ``fresh/run-windows-fresh.json``, the labelled BACKFILL for sessions
         harvested before windows existed.

    The harvest wins on purpose: a re-harvest that stamps a better window should supersede an
    older backfill automatically, rather than being shadowed by it. Both carry
    ``provenance``, so a derived interval can never be mistaken for a stamped one — and a
    record missing ``provenance`` is refused rather than assumed stamped.

    Anything unusable (file absent, bad JSON, unparseable or inverted bounds) yields UNKNOWN
    with a log line. A half-built window would be worse than none: downstream, absent means
    "we cannot say", while a fabricated interval would silently answer the alarm question
    about the wrong stretch of time.
    """

    def __init__(self, sessions_path: Optional[str] = None,
                 sidecar_path: Optional[str] = None) -> None:
        self._sessions_path = sessions_path or default_sessions_path()
        if sidecar_path is None:
            sidecar_path = os.path.join(_harvest_dir(), "fresh", "run-windows-fresh.json")
        self._sidecar_path = sidecar_path
        self._cache: Optional[dict] = None

    def _load(self) -> dict:
        """``{session_id: window_dict}`` merged from both files, harvest taking precedence."""
        if self._cache is None:
            merged: dict = {}
            for path, extract in ((self._sidecar_path, _windows_from_sidecar),
                                  (self._sessions_path, _windows_from_sessions)):
                try:
                    with open(path) as f:
                        raw = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError) as e:
                    logger.debug("no run windows from %s (%s)", path, e)
                    continue
                merged.update(extract(raw))
            self._cache = merged
        return self._cache

    def window_for(self, session_id: str):
        record = self._load().get(session_id)
        if not record:
            return UNKNOWN
        return _window_from_record(record, session_id)


def _windows_from_sessions(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {sid: value["window"] for sid, value in raw.items()
            if isinstance(value, dict) and isinstance(value.get("window"), dict)}


def _windows_from_sidecar(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    windows = raw.get("windows") if isinstance(raw.get("windows"), dict) else raw
    return {sid: value for sid, value in windows.items()
            if isinstance(value, dict) and value.get("start")}


def _window_from_record(record: dict, session_id: str):
    """A recorded window dict -> RunWindow, or UNKNOWN when it cannot be trusted."""
    provenance = record.get("provenance")
    if not provenance:
        logger.warning("session %s: recorded window has no provenance — refusing it rather "
                       "than assuming it was stamped", session_id[:8])
        return UNKNOWN
    try:
        return RunWindow(start=record.get("start", ""), end=record.get("end", ""),
                         provenance=provenance, recorded_at=record.get("recorded_at", "") or "",
                         note=record.get("note", "") or "")
    except ValueError as e:
        logger.warning("session %s: unusable recorded window (%s) — window is UNKNOWN",
                       session_id[:8], e)
        return UNKNOWN


# The usage provenances this reader will believe. Same discipline as `_window_from_record`: a
# recorded number whose provenance is absent or unrecognised is REFUSED, not trusted.
#
# There is exactly one un-provenanced usage file in existence — `fresh/token-usage-fresh.json`,
# written by the offline reaudit — and it is EXACTLY 2x wrong, because the query that built it
# summed `gen_ai.usage.total_tokens` over every span that carried the attribute. `chat` spans
# carry one LLM call each while `invoke_agent Strands Agents` spans carry the ACCUMULATED total
# for the whole invocation, so each call was counted twice (measured against staging for all six
# golden sessions: 3,599,064 recorded vs 1,799,532 real). Believing it is how a laptop `--emit-wire`
# came to report 13,344,216 tokens over `measuredOf: 6/6` while the deployed worker reported
# UNMEASURED — a number that was not merely unavailable in production but double the truth.
# Refusing it makes both paths say the same thing, which is the point.
_TRUSTED_USAGE_PROVENANCE = frozenset({"chat_spans_grouped_by_session_and_model"})

# `models` and `by_model` are EMPTY here, not zeroed. A run that recorded no usage did not spend
# zero tokens on every model — it recorded nothing, and it does not say which models it did not
# use. An empty mapping is what lets the wire keep those runs out of every per-model denominator
# instead of asserting an absence nobody observed.
#
# The two `cost_tokens_*` counts follow the same rule and are UNMEASURED rather than 0 for the same
# reason: no pricing was ATTEMPTED on a session with no per-model counts, and `0 priced, 0 unpriced`
# would read as an attempt that found nothing to skip. `dto._measured_total` ignores the sentinel,
# so such a run contributes nothing to either half of the population's token denominator.
_UNMEASURED_USAGE = {
    "tokens": UNMEASURED, "cost": UNMEASURED, "cost_tokens_priced": UNMEASURED,
    "cost_tokens_unpriced": UNMEASURED, "input_tokens": UNMEASURED,
    "output_tokens": UNMEASURED, "cache_read_tokens": UNMEASURED,
    "cache_write_tokens": UNMEASURED, "llm_calls": UNMEASURED, "models": (),
    "by_model": {},
}

# ---- pricing: hand-entered rates, read off the published price page --------------------------
#
# USD per MILLION tokens, and BEDROCK's rates — `_RATE_PROVENANCE` below carries the page and the
# date they were read off it. Anthropic's own published prices DO NOT APPLY here: these are Bedrock
# inference-profile ids and Bedrock is partner-operated with separate pricing, which its own price
# list says. The page quotes these models identically in every region, so the harness's `us-west-2`
# is priced at the same numbers as the page's US East (Ohio) column.
#
#   * `cache_write` and `cache_read` are separate rates, not multipliers. Cache read is roughly a
#     tenth of input and cache reads are ~80% of prompt tokens on this population, so omitting
#     them and pricing everything at `input` overstates by several times.
#   * `input` is UNCACHED input only, matching the derived `input_tokens` term.
#   * `cache_write` is the FIVE-MINUTE-TTL price. The page prices a 1-hour TTL separately and
#     higher — for opus 4.8, $10.00 against $6.25, i.e. 1.6x — and the telemetry records
#     `cache_write_tokens` with no TTL at all, so this code cannot tell the two apart. Five minutes
#     is the default, so it is the right assumption today; but a run that sets `"ttl": "1h"` (a
#     documented recommendation for long agentic runs) costs up to 1.6x what this reports, and
#     nothing in the data would reveal it. Separating them needs a TTL recorded on the span, not a
#     second rate here — so the limitation travels to the reader in `costBasis` instead.
#
# Keys are NORMALISED model ids (see `_normalise_model`): the same model appears as both
# `global.anthropic.claude-opus-4-8` and `us.anthropic.claude-opus-4-8` — cross-region routing
# prefixes, not different models — and keying on the raw id would silently miss half the rows.
# The four rows below are every distinct model this population has used. A model with NO row is
# SKIPPED and its tokens counted as unpriced (`_cost_usd`), which costs the figure that model's
# share rather than the whole session: add a row only from the price page, because a guessed rate is
# worse than an absent one, and the unpriced token count says exactly how much is missing.
_BEDROCK_RATES_USD_PER_MTOK: dict = {
    "anthropic.claude-opus-4-8": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "anthropic.claude-sonnet-5": {
        "input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "anthropic.claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
}

# Where the rates above came from and WHEN. Travels to the wire as part of `costBasis`, which the UI
# renders verbatim, so a rate entered by hand a quarter ago is visible to a reader rather than
# silently stale. It carries the cache-write TTL caveat for the same reason: that limitation is a
# property of the numbers, so it belongs beside them rather than in the head of whoever typed them.
_RATE_PROVENANCE = (
    "https://aws.amazon.com/bedrock/pricing/, read by hand on 2026-08-31 — that page quotes these "
    "models identically in every region, checked, so its US East (Ohio) column is what this "
    "us-west-2 harness is priced at; and the cache-write figure is the 5-MINUTE-TTL one, because "
    "the telemetry records cache_write_tokens with no TTL at all, so a run that opted into the "
    "1-hour TTL costs up to 1.6x what is reported here"
)


def _normalise_model(model_id: str) -> str:
    """A Bedrock model id with its cross-region routing prefix stripped.

    ``global.anthropic.claude-opus-4-8`` and ``us.anthropic.claude-opus-4-8`` are the same model
    reached two ways, and both occur in this population. A rate table keyed on the raw string
    would price one and miss the other — and a missed model is silently skipped by `_cost_usd`, so
    the failure would surface as an unexplained unpriced-token count rather than as a typo.
    """
    parts = (model_id or "").split(".")
    if len(parts) > 2 and parts[0] in ("global", "us", "eu", "apac", "us-gov"):
        return ".".join(parts[1:])
    return model_id or ""


# The four separately-priced terms, as ``(rate key, per-model counts key)``. ONE list, walked by the
# pricing arithmetic AND by the priced/unpriced token counts beside it, so the two can never come to
# disagree about which tokens a price is supposed to cover.
_PRICED_TERMS = (("input", "input_tokens"), ("output", "output_tokens"),
                 ("cache_read", "cache_read_tokens"), ("cache_write", "cache_write_tokens"))


def _cost_usd(by_model: dict) -> tuple:
    """``(USD | UNMEASURED, tokens priced, tokens NOT priced)`` for one session's usage.

    Four priced terms per model — uncached input, output, cache read, cache write — because they
    are priced very differently and a single rate over `total_tokens` is wrong by most of an order
    of magnitude on this population, not slightly off.

    PARTIAL PER SESSION, and that is a reversal. A model with no rate row used to make its whole
    session UNMEASURED, discarding every priced model beside it. The objection behind that was real
    — a smaller-but-plausible number captioned "cost" understates the total while looking
    authoritative — but the remedy also threw away the sessions that WERE priceable, so one new
    model id could cost the population its entire bill. The unpriced model is now skipped and its
    tokens counted, and the two counts returned here are what keep the shortfall visible: the wire
    publishes them as ``spend.cost.tokensPriced`` / ``tokensUnpriced`` (`dto._spend`), so a partial
    figure reads as "N of M tokens priced" instead of as a complete one.

    The denominator has to be TOKENS. ``measuredOf``/``total`` count RUNS on every spend term, and a
    partly-priced session is still one measured run — so runs alone would report "8 of 8 sessions
    priced" over a total short by whatever the skipped model spent, which is worse than the sentinel
    it replaced.

    A rate row missing one TERM still refuses the WHOLE model. Three of four categories is a
    different and worse failure than none: cache reads are ~80% of prompt tokens on this population,
    so an omitted ``cache_read`` would remove most of the bill from a figure that still looked
    priced.

    UNMEASURED — never ``0.0`` — when NOTHING priced: an empty rate table, no per-model counts, or
    no model in the session with a usable row. A cost of zero must not stand in for "could not
    price". A model that priced and spent no tokens is a different case and does cost 0.
    """
    total = 0.0
    priced_models = 0
    priced_tokens = 0
    unpriced_tokens = 0
    for model_id, counts in (by_model or {}).items():
        tokens = sum(counts.get(key) or 0 for _, key in _PRICED_TERMS)
        rates = _BEDROCK_RATES_USD_PER_MTOK.get(_normalise_model(model_id))
        if not rates:
            logger.warning("no Bedrock rate for model %r (normalised %r) — its %d tokens are "
                           "SKIPPED and reported as unpriced; the models that ARE known are still "
                           "priced", model_id, _normalise_model(model_id), tokens)
            unpriced_tokens += tokens
            continue
        missing = next((term for term, _ in _PRICED_TERMS if term not in rates), "")
        if missing:
            logger.warning("Bedrock rate for %r has no %r term — the whole model is SKIPPED rather "
                           "than priced on the categories it does carry", model_id, missing)
            unpriced_tokens += tokens
            continue
        # Accumulated one TERM at a time, each divided by a million before it is added, exactly as
        # the all-or-nothing version accumulated it. Summing the four products and dividing once
        # would be the same arithmetic on paper and a different float in the last bits, which is
        # enough to move a reported total that nothing about this change should move.
        for term, key in _PRICED_TERMS:
            total += (counts.get(key) or 0) * rates[term] / 1_000_000
        priced_models += 1
        priced_tokens += tokens
    if not priced_models:
        return UNMEASURED, 0, unpriced_tokens
    return total, priced_tokens, unpriced_tokens


def _per_model_usage(by_model: dict) -> dict:
    """One session's usage SPLIT BY MODEL, in ``Run.resources``' own spelling.

    ``models`` beside this carries the model IDENTITIES, which is enough to say how many models a
    population used. This carries how much each one actually cost, which is the figure a reader can
    act on: measured over a 336 h harvest, one of five ids is a third of the sessions and three
    quarters of the tokens, and a bare count of five cannot show that.

    Renamed to the vocabulary the sibling top-level terms already use (``total_tokens`` ->
    ``tokens``) so the wire mapper needs ONE set of term names for the population and for its
    per-model split, rather than a second table mapping one spelling onto the other.

    A term the record does not carry is UNMEASURED, never 0 — the same rule as the top-level terms.
    A model whose output tokens went unrecorded did not emit none.
    """
    if not isinstance(by_model, dict):
        return {}
    return {
        model: {
            "tokens": counts.get("total_tokens", UNMEASURED),
            "input_tokens": counts.get("input_tokens", UNMEASURED),
            "output_tokens": counts.get("output_tokens", UNMEASURED),
            "cache_read_tokens": counts.get("cache_read_tokens", UNMEASURED),
            "cache_write_tokens": counts.get("cache_write_tokens", UNMEASURED),
            "llm_calls": counts.get("llm_calls", UNMEASURED),
        }
        for model, counts in by_model.items()
        if isinstance(model, str) and model and isinstance(counts, dict)
    }


def cost_basis() -> str:
    """The ``costBasis`` string for the wire: why cost is absent, or where its rates came from.

    It reaches the UI verbatim, so it has to say that pricing can be PARTIAL and how a reader tells.
    Since `_cost_usd` stopped refusing a session it can only partly price, the same sentence that
    used to promise "UNMEASURED rather than a partial price" would now describe an all-or-nothing
    total that no longer exists — and a reader with no way to spot a short figure is exactly the
    failure the token counts were added to prevent.
    """
    if _BEDROCK_RATES_USD_PER_MTOK and _RATE_PROVENANCE:
        return (f"ESTIMATE, not billed truth: computed per model from uncached input / output / "
                f"cache read / cache write at rates from {_RATE_PROVENANCE}. Pricing is PARTIAL "
                f"where a model has no rate row: its tokens are skipped rather than costing the "
                f"whole session its price, so read the priced and unpriced TOKEN counts beside the "
                f"figure — any unpriced tokens mean the total is short by exactly those. A session "
                f"where nothing priced reports UNMEASURED, never $0.")
    return COST_UNMEASURED_REASON


#: The keys `harvest_usage_shortfall` will report, in the spelling the audit file uses. Named so a
#: file written by an older harvester yields UNMEASURED for the terms it lacks instead of dropping
#: them from the shape a consumer branches on.
_USAGE_SHORTFALL_TERMS = ("tokens_discarded", "tokens_measured",
                          "sessions_discarded", "sessions_measured")


def harvest_usage_shortfall(path: Optional[str] = None) -> dict:
    """How much measured token spend the harvest could not attach to a trajectory. Never zeroes.

    Every term is UNMEASURED unless the harvest recorded it, and that is the whole point: a harvest
    taken before this audit existed, or one whose usage query was skipped, writes no file, and
    reporting ``0`` for it would assert that the span cap cost nothing on evidence nobody checked.
    A file that IS present and says ``0`` is a real measurement — the cap did not bind — and reads
    differently.

    Read from a file rather than threaded out of ``harvest_live.harvest``, because the local mining
    path (``mine_local``) reads a harvest it did not take and would otherwise have no route to this
    fact at all.
    """
    path = path or default_usage_audit_path()
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("no usage shortfall audit at %s (%s)", path, e)
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {term: (raw[term] if isinstance(raw.get(term), (int, float))
                   and not isinstance(raw.get(term), bool) else UNMEASURED)
            for term in _USAGE_SHORTFALL_TERMS}


class HarvestedUsageSource:
    """Per-session token usage from the sessions harvest. Never derives, never zeroes.

    Reads the SAME file the trajectory and window sources read (``sessions/sessions.json`` and
    its ``fresh/`` sibling, merged oldest-first), because that is where ``harvest_live``
    writes the per-session ``usage`` block — next to ``window``. Nothing new to point at, and
    the offline and deployed paths therefore agree by construction rather than by coincidence.

    A session with no ``usage`` key, or one whose provenance is not in
    ``_TRUSTED_USAGE_PROVENANCE``, yields the UNMEASURED sentinels — including ``cost``, since a
    session with no per-model counts is unpriceable rather than free. A session whose models are
    only PARTLY priceable reports the part it could price plus the tokens it could not, and is
    UNMEASURED only when nothing at all priced; see ``_cost_usd``.
    """

    def __init__(self, sessions_paths: Optional[list] = None) -> None:
        self._paths = list(sessions_paths) if sessions_paths else default_sessions_paths()
        self._cache: Optional[dict] = None

    def _load(self) -> dict:
        if self._cache is None:
            merged: dict = {}
            for path in self._paths:
                try:
                    with open(path) as f:
                        raw = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError) as e:
                    logger.debug("no token usage from %s (%s)", path, e)
                    continue
                if not isinstance(raw, dict):
                    continue
                for sid, value in raw.items():
                    if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                        merged[sid] = value["usage"]
            self._cache = merged
        return self._cache

    def usage_for(self, session_id: str) -> dict:
        record = self._load().get(session_id)
        if not record:
            return {**_UNMEASURED_USAGE, "cost_basis": cost_basis()}
        provenance = record.get("provenance")
        if provenance not in _TRUSTED_USAGE_PROVENANCE:
            logger.warning(
                "session %s: token usage has provenance %r, which this reader does not trust — "
                "reporting UNMEASURED rather than a number that may double-count "
                "(trusted: %s)", session_id[:8], provenance,
                ", ".join(sorted(_TRUSTED_USAGE_PROVENANCE)))
            return {**_UNMEASURED_USAGE, "cost_basis": cost_basis()}
        by_model = record.get("byModel") or {}
        cost, priced_tokens, unpriced_tokens = _cost_usd(by_model)
        return {
            "tokens": record.get("total_tokens", UNMEASURED),
            # An ESTIMATE from hand-entered rates, never billed truth — and PARTIAL whenever some
            # model here has no rate row. `cost_basis` beside it says so in words; the two token
            # counts below say how much of this session's spend the figure actually covers, which is
            # the only thing that stops a short total reading as a complete one.
            "cost": cost,
            "cost_tokens_priced": priced_tokens,
            "cost_tokens_unpriced": unpriced_tokens,
            # How that cost was arrived at, or why it is absent — carried on the RUN so the wire
            # states what it actually received rather than re-reading the producer's config, and
            # so a hand-entered rate's provenance and date reach the UI instead of going stale
            # invisibly. Collapsed to one string by `dto._spend`; never a per-run list.
            "cost_basis": cost_basis(),
            "input_tokens": record.get("input_tokens", UNMEASURED),
            "output_tokens": record.get("output_tokens", UNMEASURED),
            "cache_read_tokens": record.get("cache_read_tokens", UNMEASURED),
            "cache_write_tokens": record.get("cache_write_tokens", UNMEASURED),
            "llm_calls": record.get("llm_calls", UNMEASURED),
            # Which models this run's tokens were spent on. Carried so the wire can say HOW MANY
            # distinct models the population used — the fact that makes a single-rate cost wrong.
            "models": tuple(sorted(by_model)) if isinstance(by_model, dict) else (),
            # ...and HOW MUCH went to each of them, which is the half a reader can act on: it is
            # what says which step to move onto a cheaper model. Only `_cost_usd` saw these counts
            # before, so the wire could report the model COUNT and never the split.
            "by_model": _per_model_usage(by_model),
        }


class HarvestedAgentCoreEvalSource:
    """Per-session AgentCore online-eval scores from the sessions harvest, or ``None``.

    Reads the ``agentcore_eval`` block ``harvest_live.reconstruct_sessions`` writes beside
    ``usage`` — ``{evaluator: {"mean": float, "n": int}}`` — from the same files, merged
    oldest-first like usage. ``None`` means nobody scored the session (AgentCore samples, its
    configs score main-agent sessions only, and results land minutes after a session ends); it
    is never turned into an empty or zeroed block. Entries whose mean is not a number in [0, 1]
    are dropped — AgentCore's built-in scores are normalised to that range.
    """

    def __init__(self, sessions_paths: Optional[list] = None) -> None:
        self._paths = list(sessions_paths) if sessions_paths else default_sessions_paths()
        self._cache: Optional[dict] = None

    def _load(self) -> dict:
        if self._cache is None:
            merged: dict = {}
            for path in self._paths:
                try:
                    with open(path) as f:
                        raw = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError) as e:
                    logger.debug("no AgentCore eval scores from %s (%s)", path, e)
                    continue
                if not isinstance(raw, dict):
                    continue
                for sid, value in raw.items():
                    if isinstance(value, dict) and isinstance(value.get("agentcore_eval"), dict):
                        merged[sid] = value["agentcore_eval"]
            self._cache = merged
        return self._cache

    def eval_for(self, session_id: str) -> Optional[dict]:
        record = self._load().get(session_id)
        if not isinstance(record, dict):
            return None
        clean = {}
        for evaluator, entry in record.items():
            if not isinstance(evaluator, str) or not isinstance(entry, dict):
                continue
            mean, n = entry.get("mean"), entry.get("n")
            if isinstance(mean, bool) or not isinstance(mean, (int, float)) or not 0 <= mean <= 1:
                continue
            clean[evaluator] = {"mean": float(mean), "n": int(n) if isinstance(n, int) else 1}
        return clean or None


def _ask_span_source(span_source, method: str, session_id: str, absent, what: str):
    """One optional question to the span source, or ``absent`` when it cannot answer.

    Probed rather than required, and shared by every such question rather than copied per field.
    ``TrajectorySource`` declares only ``spans_for``, so ``LiveSpanSource`` and every injected test
    double lack these methods — and a source that cannot answer must yield the ABSENT sentinel,
    never a value. Each caller supplies what absence means for its own field, because "we did not
    ask" and "we asked and it was fine" are the pair this whole layer exists to keep apart.

    A raising source is absent too: an unreadable harvest is not evidence of anything.
    """
    accessor = getattr(span_source, method, None)
    if not callable(accessor):
        return absent
    try:
        return accessor(session_id)
    except Exception as e:  # noqa: BLE001 — an unreadable harvest is absent, never a value
        logger.warning("session %s: could not read %s (%s) — treating as absent",
                       session_id[:8], what, e)
        return absent


def _completeness_from(span_source, session_id: str) -> tuple:
    """``(evidence_complete, evidence_note)`` from the span source, or UNKNOWN when it cannot say.

    A source that cannot tell whether its read was clipped must yield UNKNOWN, not True.
    Defaulting to True here would silently re-arm the exact failure the field exists to close: a
    partially-read passing run acting as a control that a signature "survived".
    """
    return _ask_span_source(span_source, "completeness_for", session_id, (UNKNOWN, ""),
                            "harvest completeness")


def _parent_from(span_source, session_id: str):
    """The parent session id from the span source, or UNKNOWN when it cannot say.

    UNKNOWN and not NO_DATA for a source that has no answer: NO_DATA is reserved for a source
    that DID look and found no parent (see ``HarvestedSpanSource.parent_for``), and a run whose
    parent was never looked for must not join the population of runs that genuinely have none.
    """
    return _ask_span_source(span_source, "parent_for", session_id, UNKNOWN, "the parent session")


def _busy_ms(steps: list):
    """Total tool busy time over these steps, or UNMEASURED if ANY of them lacks a duration.

    ALL-OR-NOTHING, the same rule ``waste._sum_durations`` applies to the same evidence. A span
    whose ``durationNano`` the harvest never carried is a step nobody timed, and adding up only the
    steps that happen to carry one produces a number smaller than the truth wearing a caption that
    says total. There is no denominator on ``Run.resources`` to declare the shortfall in, and
    ``dto._sum_term``/``dto._measured_count`` count this run as MEASURED the moment the value is a
    number — so a partial sum here becomes a complete-looking ``spend.latencyMs`` one layer up.

    An empty trajectory is UNMEASURED too, never ``0``: no steps means nothing was observed, not a
    run that finished instantly.
    """
    if not steps:
        return UNMEASURED
    total = 0
    for step in steps:
        if step.duration_ms is None:
            return UNMEASURED
        total += step.duration_ms
    return total


def _token_usage_from_harvest(session_id: str) -> dict:
    """``Run.resources``' token half for one session, or UNMEASURED sentinels.

    Kept as a module function because that is what ``build_run`` has always called, and because
    there is only ever ONE source for it: usage lives in the harvest, and ``harvest_live`` puts
    it there in the cloud exactly as an offline harvest would locally. Nothing to inject.
    """
    return HarvestedUsageSource().usage_for(session_id)


def build_run(
    scenario: Scenario,
    span_source: Optional[TrajectorySource] = None,
    plan_source: Optional[HarvestedPlanSource] = None,
    memory_reader: Optional[MemoryReader] = None,
    window_source: Optional[WindowSource] = None,
    alarm_source: Optional[AlarmSource] = None,
) -> Run:
    """Build a Run for a labeled Scenario from real offline+Memory evidence.

    ``window_source``/``alarm_source`` follow the same injection contract as the span, plan
    and Memory sources: the defaults are offline and creds-free, so tests and the golden path
    never reach AWS, and a live lookup is opted into explicitly (see
    ``infra_health.default_alarm_source``).
    """
    span_source = span_source or HarvestedSpanSource()
    plan_source = plan_source or HarvestedPlanSource()
    window_source = window_source or HarvestedWindowSource()

    session_id = scenario.source_session or scenario.scenario_id
    steps = trajectory_for_session(session_id, source=span_source)
    trajectory = steps if steps else NO_DATA
    # UNMEASURED when the trajectory is absent OR when any step in it carries no duration — never
    # a 0 that reads as "instant", and never a partial sum that reads as a total. See `_busy_ms`.
    latency_ms = _busy_ms(steps)

    response = NO_DATA
    turns: list[dict] = []
    goal = scenario.goal or NO_DATA
    # Who this session belongs to. UNKNOWN unless Memory names an owner: the same read that
    # supplies the goal and the response already resolves the actor (it has to, to page the
    # events), and this is the only field on the Run that says whose evidence it is. Without it
    # nothing derived from a session can be shown to the person who ran it.
    actor_id = UNKNOWN
    if memory_reader is not None:
        conversation = memory_reader.conversation_for_session(session_id)
        if conversation.final_assistant_text:
            response = conversation.final_assistant_text
        turns = [{"role": t["role"], "text": t["text"]} for t in conversation.turns]
        if goal is NO_DATA and conversation.user_texts:
            goal = conversation.user_texts[0]
        if conversation.actor_id:
            actor_id = conversation.actor_id

    resources = {"latency_ms": latency_ms, **_token_usage_from_harvest(session_id)}
    # AgentCore's own online-eval scores for this session, when it scored it. ABSENT (no key) when
    # not — `dto._by_model_scores` counts a run toward an evaluator's mean only if it has one.
    agentcore_eval = HarvestedAgentCoreEvalSource().eval_for(session_id)
    if agentcore_eval:
        resources["agentcore_eval"] = agentcore_eval

    # `latency_ms` above stays what it is — the SUM of tool durations, i.e. busy time. The
    # window is additive wall clock: a run can be 40 minutes long with 90s of tool work, and
    # only the wall-clock extent can be lined up against an account-wide alarm.
    window = window_source.window_for(session_id)
    infra_health = infra_health_for_window(window, source=alarm_source)
    # Whether the trajectory above is the WHOLE trajectory. Read from the harvest that produced
    # the spans, never inferred here: a short trajectory and a clipped one look identical from
    # this side, and only the harvester knows which windows it lost.
    evidence_complete, evidence_note = _completeness_from(span_source, session_id)
    # Which request spawned this session, when it is a plan sub-session. Comes from the same
    # source as the spans because the link IS a span field (the OTEL trace), so a source that
    # could not read the trajectory could not have read the parent either.
    parent_run_id = _parent_from(span_source, session_id)
    # A plan sub-session has NO conversation of its own — the read above asked Memory for this
    # session's own id and got nothing, because the turns live under the request that spawned it. So
    # the owner comes from the parent, by id, and only the id is taken: pulling the parent's turns in
    # here would hand this run somebody else's prose for the judge to score.
    #
    # Guarded on `actor_id` still being UNKNOWN rather than on the class, so a session that DID name
    # its own owner always keeps it. That ordering is the whole safety of it — an inherited owner can
    # only ever fill a gap, never overwrite an answer Memory gave directly.
    if (actor_id is UNKNOWN and memory_reader is not None
            and isinstance(parent_run_id, str) and parent_run_id):
        inherited = memory_reader.owner_of_session(parent_run_id)
        if inherited:
            actor_id = inherited

    return Run(
        run_id=session_id,
        actor_id=actor_id,
        goal=goal,
        response=response,
        trajectory=trajectory,
        plan=plan_source.plan_for(session_id),
        turns=turns,
        resources=resources,
        harness_snapshot=UNKNOWN,
        scenario=scenario,
        window=window,
        infra_health=infra_health,
        evidence_complete=evidence_complete,
        evidence_note=evidence_note,
        parent_run_id=parent_run_id,
    )
