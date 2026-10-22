/**
 * What the registry must never do: turn an absent measurement into a number.
 *
 * No jsdom pragma and no jest-dom import, on purpose. Nothing here renders — the registry says
 * what to read and the tile draws it, and every rule worth defending lives on the reading side.
 * A test that needed a DOM to check a denominator would mean the two had grown together.
 *
 * The bundles below are the cases the surface actually meets, and every figure in them is real:
 *
 *  - the COMMITTED bundle, which has no `scorecard` key at all. Not production — the deployed item
 *    carries a full one — but the shape a UI build meets whenever it ships ahead of the producer,
 *    which is the ordinary order.
 *  - a real emitted bundle's scorecard, in which one term is measured and every token term is
 *    `UNMEASURED` with `measuredOf: 0`.
 *  - the 336-hour harvest's own sums, where coverage is PARTIAL: 354 of 553 sessions.
 *  - the same harvest with cost priced, where coverage is partial AND the priced sessions are
 *    fewer than the sessions that recorded tokens.
 *  - today's live population, priced in full: two models, both with rate rows, so no tokens went
 *    unpriced and the count of them is a measured `0`.
 *  - that same population with one model the rate table has no row for, where every RUN that
 *    recorded usage is still priced and the money is short anyway.
 *  - the reader's OWN two runs, in both wire generations: the four-term row the producer emitted
 *    before it projected the wider spend, and the eight-term row it emits now.
 *
 * All of them matter and none is redundant. A fixture where everything is measured passes whether
 * or not the denominator rules exist; a fixture where nothing is measured never exercises the
 * arithmetic. Only partial coverage catches a sum captioned as a population, and only two
 * different partial coverages catch a footnote quoting the wrong one of them. The `livePriced` /
 * `partlyPriced` pair is the only shape that catches coverage read off RUNS alone: their run
 * denominators are identical and one of the two figures is short. And the two GENERATIONS of run row
 * are what keep `?? 0` out of the personal reads — a bundle from before a term is served for as long
 * as its actor stays away, so "the wire never sent this" and "nothing measured it" are both live
 * and must not render alike.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

import {
  METRICS, PERIODS, PERIOD_LABEL, SCOPES, SCOPE_LABEL,
  describeBasis, formatUsd, isStructural, metricLabel, periodRefusal,
  type MetricEntry, type MetricView, type RatioReading, type ScalarReading, type Unavailable,
  type UnavailableCode,
} from './metricRegistry';
import { compactNumber, formatExtent, formatIsoDay, formatMs } from './improveChrome';
import fixture from './__fixtures__/improveData.fixture.json';
import {
  SENTINELS,
  type ActorBundle, type ActorDay, type ActorRun, type DayCoverage, type DaySpend,
  type DiscoveredSessions, type MeasuredCost, type MeasuredSum, type ModelSpend,
  type ModelSpendRemainder, type Scorecard, type UsageShortfall,
} from '../../improve-api';
import type { ImproveData } from '../../improve-data';

/**
 * The REAL payload the loop emits, committed as a fixture, and the only one of the three that can
 * be imported rather than transcribed.
 *
 * It has `components`, `patterns` and `suggestions` and NO `scorecard` — which is exactly why it is
 * here. The `noScorecard` branch can only be tested against a bundle that genuinely lacks the key, and
 * a hand-written one would be free to forget the omission.
 *
 * It is not the deployed shape. The staging item carries a full scorecard — `population`, `tiers`,
 * `spend`, `outcomes`, `attribution`, `evidenceComplete` — so this branch is what a UI build meets when
 * it ships ahead of the producer rather than what every reader hits. That order is ordinary, and it is
 * the same reason `scorecard` is optional on the type at all.
 */
const NO_SCORECARD_BUNDLE = fixture as unknown as ImproveData;

// --- the figures ------------------------------------------------------------
/*
 * Every magnitude below is measured, not chosen to be convenient. Transcribed rather than
 * imported because both sources are read-only artifacts outside the repo tree.
 *
 * `HARVEST_*` are sums over a 336-hour staging harvest of 553 sessions, 354 of which carry a
 * `usage` block. `WIRE_*` come from a real emitted bundle's `scorecard`.
 */
const HARVEST_SESSIONS = 553;
const HARVEST_SESSIONS_WITH_USAGE = 354;
const HARVEST_TOKENS = 211_662_174;
const HARVEST_CALLS = 5_406;
/** The four separately-priced categories over the same 354 sessions. They sum to the token total
 *  exactly — Bedrock's own arithmetic — which is what lets a split stack without a residual. */
const HARVEST_INPUT = 22_163;
const HARVEST_OUTPUT = 3_616_525;
const HARVEST_CACHE_READ = 157_490_856;
const HARVEST_CACHE_WRITE = 50_532_630;
/** Distinct Bedrock model ids the harvest's tokens were spent on. */
const HARVEST_MODELS = 5;
/**
 * Busy time: every harvested span's `duration_ms` summed per session, then over the population,
 * mirroring how `build_run` derives `latency_ms`. All 553 sessions carry at least one span, so
 * this one term is measured on the whole population while tokens are measured on two thirds.
 */
const HARVEST_LATENCY_MS = 412_524_975;

/** Everything sent TO a model: uncached input plus both cache directions. */
const HARVEST_CONTEXT = HARVEST_INPUT + HARVEST_CACHE_READ + HARVEST_CACHE_WRITE;

const WIRE_RUNS = 6;
const WIRE_LATENCY_MS = 12_182_621;

/**
 * The two `costBasis` sentences a bundle can carry, transcribed character for character.
 *
 * Both are live. The producer falls back to the first whenever it has no rate table — and also
 * when one population was assembled from two configurations, since it will not pick a winner
 * between two bases and imply a price applied throughout. The second is what a priced population
 * carries.
 *
 * They are here in full because the registry surfaces them VERBATIM and abridges rather than
 * paraphrases: each names something a rewrite would drop — which input is missing and what goes
 * wrong if someone prices a token total anyway, or which page the rates came from, on what date,
 * that the cache-write figure is the five-minute one, and that pricing is PARTIAL where a model has
 * no rate row.
 *
 * That last clause is the reason the token counts have to be stated as their own footnote clause
 * rather than left to this sentence to explain: it begins 530 characters in and names the counts at
 * 684, where nothing that shows a HEAD of this string will ever reach it.
 */
const NO_RATE_TABLE_BASIS = 'tokens measured, cost not priced: no per-model Bedrock rate table is '
  + "reachable from the deploy path (the AWS Price List API publishes no rates for this "
  + "population's models and no Claude cache dimensions; Anthropic's list prices exclude Bedrock "
  + 'as partner-priced). The per-category and per-model token breakdown is measured, so a sourced '
  + 'rate table is the only missing input — do not multiply total tokens by one rate: cache '
  + 'reads are ~0.1x input and 77% of prompt tokens here';

const PRICED_BASIS = 'ESTIMATE, not billed truth: computed per model from uncached input / output '
  + '/ cache read / cache write at rates from https://aws.amazon.com/bedrock/pricing/, read by '
  + 'hand on 2026-08-31 — that page quotes these models identically in every region, checked, so '
  + 'its US East (Ohio) column is what this us-west-2 harness is priced at; and the cache-write '
  + 'figure is the 5-MINUTE-TTL one, because the telemetry records cache_write_tokens with no TTL '
  + 'at all, so a run that opted into the 1-hour TTL costs up to 1.6x what is reported here. '
  + 'Pricing is PARTIAL where a model has no rate row: its tokens are skipped rather than costing '
  + 'the whole session its price, so read the priced and unpriced TOKEN counts beside the figure — '
  + 'any unpriced tokens mean the total is short by exactly those. A session where nothing priced '
  + 'reports UNMEASURED, never $0.';

/**
 * Two real priced sessions, both from the harvest above, at the rates the producer ships.
 *
 * The first is one Opus-4.8 session: 4 uncached input tokens, 831 output, 60,671 cache read and
 * 29,064 cache write. Its cache writes are 78% of the price off 32% of the tokens, which is why
 * one rate over a token total is wrong by most of an order of magnitude rather than slightly. The
 * second is a session that spent on two models.
 */
const PRICED_ONE_MODEL_USD = 0.2327805;
const PRICED_TWO_MODEL_USD = 2.7514315;
const PRICED_TOTAL_USD = PRICED_ONE_MODEL_USD + PRICED_TWO_MODEL_USD;
/**
 * How many of the harvest's 553 sessions the figure above covers.
 *
 * Two, against 354 that recorded tokens, and the gap is the point: a session NO model of which the
 * rate table can price is not priced at all, so a population can record tokens on far more sessions
 * than it can price. A footnote that quoted the token denominator would caption two sessions' bill
 * as three hundred and fifty-four sessions'.
 */
const PRICED_SESSIONS = 2;

/** About a line of meta text under a figure in a tile a third of a row wide. */
const CAVEAT_LINE = 70;

// --- building the two scorecards ---------------------------------------------
function sum(value: MeasuredSum['sum'], measuredOf: number, total: number): MeasuredSum {
  return { sum: value, measuredOf, total };
}

const UNMEASURED_OVER_WIRE = sum('UNMEASURED', 0, WIRE_RUNS);

/**
 * A scorecard carrying a given `spend`, with every other field transcribed from a real emitted
 * bundle.
 *
 * The cast is the finding, not a shortcut: that bundle has NO `attribution` key, though
 * `Scorecard` declares one as required. So a field this type guarantees can be absent on the
 * wire, and the registry's shape checks are there for that reason rather than out of caution.
 * Reproducing the omission here is what keeps the tests honest about it.
 */
function scorecardWith(spend: Scorecard['spend']): Scorecard {
  return {
    population: { total: WIRE_RUNS, pass: 4, fail: 2, unscored: 0 },
    passRate: 0.6666666666666666,
    tiers: {
      dispatch: { mean: 0.8305555555555556, measuredOf: WIRE_RUNS, total: WIRE_RUNS },
      delivery: { mean: 0.7666666666666667, measuredOf: WIRE_RUNS, total: WIRE_RUNS },
    },
    spend,
    outcomes: {
      failedGating: { dispatch_correct: 1, results_delivered: 1 },
      unmeasured: { dispatch_correct: 0, results_delivered: 0 },
    },
    byWorkloadClass: {
      conversational: {
        total: WIRE_RUNS, pass: 4, fail: 2, unscored: 0, passRate: 0.6666666666666666,
        gating: [['dispatch_correct', 'results_delivered']], gatingIsMixed: false,
        basis: ['recorded_label'],
      },
    },
    window: {
      start: '2026-08-13T17:20:56.699000+00:00',
      end: '2026-08-13T23:12:45.403000+00:00',
      derived: true,
      provenance: 'union_of_member_windows',
    },
    derivedWindows: WIRE_RUNS,
    evidenceComplete: { incomplete: 0, measuredOf: 0, total: WIRE_RUNS },
    // `as unknown as` and not a plain assertion, because the omission is real enough that the
    // compiler refuses to bridge it. That is the point being recorded.
  } as unknown as Scorecard;
}

/** A real emitted bundle: latency measured on all 6 runs, every token term UNMEASURED. */
const WIRE_SPEND: Scorecard['spend'] = {
  tokens: UNMEASURED_OVER_WIRE,
  cost: UNMEASURED_OVER_WIRE,
  costBasis: NO_RATE_TABLE_BASIS,
  latencyMs: sum(WIRE_LATENCY_MS, WIRE_RUNS, WIRE_RUNS),
  byCategory: {
    input: UNMEASURED_OVER_WIRE,
    output: UNMEASURED_OVER_WIRE,
    cacheRead: UNMEASURED_OVER_WIRE,
    cacheWrite: UNMEASURED_OVER_WIRE,
  },
  llmCalls: UNMEASURED_OVER_WIRE,
  modelsSeen: 0,
};

/** The 336-hour harvest's own sums. Tokens on 354 of 553 sessions, busy time on all 553. */
const HARVEST_SPEND: Scorecard['spend'] = {
  tokens: sum(HARVEST_TOKENS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  cost: sum('UNMEASURED', 0, HARVEST_SESSIONS),
  costBasis: NO_RATE_TABLE_BASIS,
  latencyMs: sum(HARVEST_LATENCY_MS, HARVEST_SESSIONS, HARVEST_SESSIONS),
  byCategory: {
    input: sum(HARVEST_INPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    output: sum(HARVEST_OUTPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheRead: sum(HARVEST_CACHE_READ, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheWrite: sum(HARVEST_CACHE_WRITE, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  },
  llmCalls: sum(HARVEST_CALLS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  modelsSeen: HARVEST_MODELS,
};

const wireBundle: ImproveData = { ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith(WIRE_SPEND) };
const harvestBundle: ImproveData = {
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith(HARVEST_SPEND),
};

const withSpend = (over: Partial<Scorecard['spend']>): ImproveData => ({
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith({ ...HARVEST_SPEND, ...over }),
});

/**
 * The harvest again, with the two sessions above priced. Two denominators for cost and tokens in
 * one bundle, which is the only shape that catches a footnote quoting the other one's.
 *
 * Its `cost` carries no token counts, and that is the second thing it pins: those two keys are
 * newer than the bundles, so a scorecard with `sum`/`measuredOf`/`total` alone under `cost` is a
 * live shape — the emitted bundle harvested from staging is exactly that — and a footnote must not
 * read a missing count as "nothing unpriced".
 */
const pricedBundle: ImproveData = withSpend({
  cost: sum(PRICED_TOTAL_USD, PRICED_SESSIONS, HARVEST_SESSIONS),
  costBasis: PRICED_BASIS,
});

// --- the live population, priced in full and priced in part -------------------
/**
 * Today's staging population, measured: 9 runs, 8 of which recorded usage, spending on two models
 * that the rate table prices in full.
 *
 * The four category sums add to the token total exactly, as ever, and the token total is what got
 * priced — so `tokensUnpriced` is `0` here, which makes this the fixture for the QUIET case: a
 * measured zero, where the footnote must say nothing about a shortfall rather than print one.
 *
 * `latencyMs` is deliberately ABSENT. This transcription captured the money and token terms of the
 * live population and not that one, and a figure invented to fill the shape would be the one thing
 * these fixtures never do; `totalTime` therefore reads `notInBundle` on this bundle, which is a
 * shape the registry has to answer for anyway.
 */
const LIVE_RUNS = 9;
const LIVE_RUNS_WITH_USAGE = 8;
const LIVE_TOKENS = 6_813_939;
const LIVE_CALLS = 139;
const LIVE_INPUT = 278;
const LIVE_OUTPUT = 119_381;
const LIVE_CACHE_READ = 4_616_921;
const LIVE_CACHE_WRITE = 2_077_359;
const LIVE_MODELS = 2;
/** The population's own total, over per-session prices running from $0.0869 to $12.1149. */
const LIVE_COST_USD = 16.74;

/** A cost term with the two TOKEN counts beside the RUN denominator every sibling term carries. */
function costSum(value: MeasuredSum['sum'], measuredOf: number, total: number,
                 tokensPriced: number, tokensUnpriced: number): MeasuredCost {
  return { sum: value, measuredOf, total, tokensPriced, tokensUnpriced };
}

const LIVE_SPEND = {
  tokens: sum(LIVE_TOKENS, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
  cost: costSum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS, LIVE_TOKENS, 0),
  costBasis: PRICED_BASIS,
  byCategory: {
    input: sum(LIVE_INPUT, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    output: sum(LIVE_OUTPUT, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    cacheRead: sum(LIVE_CACHE_READ, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    cacheWrite: sum(LIVE_CACHE_WRITE, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
  },
  llmCalls: sum(LIVE_CALLS, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
  modelsSeen: LIVE_MODELS,
} as Scorecard['spend'];

const livePriced: ImproveData = {
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith(LIVE_SPEND),
};

/** The live population with some of its spend replaced — `withSpend`'s counterpart for the
 *  9-session population, so a cost term under test is never surrounded by the 553-session one's. */
const liveWith = (over: Partial<Scorecard['spend']>): ImproveData => ({
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith({ ...LIVE_SPEND, ...over }),
});

/**
 * The same population with one more model in it: an id the rate table has no row for.
 *
 * SYNTHETIC, and it has to be — every model the live population uses is priced, so no real bundle
 * exercises a shortfall yet. Only the model is invented. The tokens charged to it are one real
 * session's own four category counts (4 uncached input, 831 output, 60,671 cache read, 29,064 cache
 * write, the Opus-4.8 session priced above), so the magnitude a reader sees is a size this harness
 * really produces and the categories still reconstruct the token total exactly.
 *
 * All 8 sessions still price, because a skipped model does not disqualify the two beside it — and
 * that is the whole trap: the RUN denominator reads exactly as complete as it does on `livePriced`
 * while the money is short by 90,570 tokens' worth.
 */
const UNPRICED_INPUT = 4;
const UNPRICED_OUTPUT = 831;
const UNPRICED_CACHE_READ = 60_671;
const UNPRICED_CACHE_WRITE = 29_064;
const UNPRICED_TOKENS = UNPRICED_INPUT + UNPRICED_OUTPUT + UNPRICED_CACHE_READ
  + UNPRICED_CACHE_WRITE;

const partlyPriced: ImproveData = liveWith({
  tokens: sum(LIVE_TOKENS + UNPRICED_TOKENS, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
  cost: costSum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS, LIVE_TOKENS, UNPRICED_TOKENS),
  byCategory: {
    input: sum(LIVE_INPUT + UNPRICED_INPUT, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    output: sum(LIVE_OUTPUT + UNPRICED_OUTPUT, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    cacheRead: sum(LIVE_CACHE_READ + UNPRICED_CACHE_READ, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
    cacheWrite: sum(LIVE_CACHE_WRITE + UNPRICED_CACHE_WRITE, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
  },
  modelsSeen: LIVE_MODELS + 1,
});

// --- which model the money went to --------------------------------------------
/**
 * The staging SNAPSHOT's own `scorecard.spend`, transcribed from the live item on 2026-09-03
 * (`mediaworks-harness-improve`, `actorId="__system__"`, under the top-level `bundle` attribute).
 *
 * A LATER harvest than `LIVE_*` above and a bigger one — 25 scored sessions where that was 9 — so it
 * is named apart rather than folded into those constants: two transcriptions of two real passes, and
 * a fixture that quietly became the average of both would describe no population at all.
 *
 * What makes it the fixture for the model split is that its distribution is real and lopsided: one of
 * two ids carries 97% of the tokens, and it appears in 14 of the 25 runs that recorded usage while the
 * other appears in 11. A synthetic pair at 50/50 would pass a renderer that divided by the wrong
 * denominator.
 *
 * Two properties hold on it and both are load-bearing below. The two rows' token sums add to
 * `tokens.sum` EXACTLY, which is what lets a share over them need no residual. And each row's `total`
 * is `tokens.measuredOf` — the runs that recorded usage — not `tokens.total`; those two coincide at 25
 * here, which is precisely why `ROSTER_SHORT` exists to tell them apart.
 */
const ROSTER_RUNS = 25;
const ROSTER_TOKENS = 10_320_108;
const ROSTER_OPUS = 'global.anthropic.claude-opus-4-8';
const ROSTER_SONNET = 'global.anthropic.claude-sonnet-5';
const ROSTER_OPUS_TOKENS = 9_984_406;
const ROSTER_OPUS_RUNS = 14;
const ROSTER_OPUS_CALLS = 250;
const ROSTER_SONNET_TOKENS = 335_702;
const ROSTER_SONNET_RUNS = 11;
const ROSTER_SONNET_CALLS = 22;

/** A per-model row in the wire's own shape. The category split is transcribed because the row carries
 *  it; nothing reads it, and a row without it would not be the shape the producer sends. */
function modelSpend(model: string, tokens: MeasuredSum['sum'], appearsIn: number,
                    calls: MeasuredSum['sum'],
                    categories: [number, number, number, number]): ModelSpend {
  const over = (value: number) => sum(value, appearsIn, ROSTER_RUNS);
  return {
    model,
    tokens: sum(tokens, appearsIn, ROSTER_RUNS),
    byCategory: {
      input: over(categories[0]),
      output: over(categories[1]),
      cacheRead: over(categories[2]),
      cacheWrite: over(categories[3]),
    },
    llmCalls: sum(calls, appearsIn, ROSTER_RUNS),
  };
}

const ROSTER_ROWS: ModelSpend[] = [
  modelSpend(ROSTER_OPUS, ROSTER_OPUS_TOKENS, ROSTER_OPUS_RUNS, ROSTER_OPUS_CALLS,
    [500, 154_077, 7_487_665, 2_342_164]),
  modelSpend(ROSTER_SONNET, ROSTER_SONNET_TOKENS, ROSTER_SONNET_RUNS, ROSTER_SONNET_CALLS,
    [44, 38_042, 19_338, 278_278]),
];

/**
 * The live remainder: a MEASURED zero count beside an UNMEASURED sum.
 *
 * Transcribed exactly, because the pairing is the trap. The producer emits this block on every pass
 * and writes `models: 0` when the bound withheld nothing; there were then no values to add, so the sum
 * beside it is the sentinel rather than a `0`. A reader that decided from the SUM whether a fold
 * happened would report every live bundle as one whose remainder could not be measured.
 */
const ROSTER_NONE_WITHHELD: ModelSpendRemainder = {
  models: 0,
  tokens: sum('UNMEASURED', 0, ROSTER_RUNS),
};

const ROSTER_SPEND = {
  tokens: sum(ROSTER_TOKENS, ROSTER_RUNS, ROSTER_RUNS),
  cost: costSum(23.3168531, ROSTER_RUNS, ROSTER_RUNS, ROSTER_TOKENS, 0),
  costBasis: PRICED_BASIS,
  latencyMs: sum(21_163_784, ROSTER_RUNS, ROSTER_RUNS),
  byCategory: {
    input: sum(544, ROSTER_RUNS, ROSTER_RUNS),
    output: sum(192_119, ROSTER_RUNS, ROSTER_RUNS),
    cacheRead: sum(7_507_003, ROSTER_RUNS, ROSTER_RUNS),
    cacheWrite: sum(2_620_442, ROSTER_RUNS, ROSTER_RUNS),
  },
  llmCalls: sum(272, ROSTER_RUNS, ROSTER_RUNS),
  modelsSeen: 2,
  byModel: ROSTER_ROWS,
  byModelOther: ROSTER_NONE_WITHHELD,
} as Scorecard['spend'];

const roster: ImproveData = {
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith(ROSTER_SPEND),
};

/** The same population with part of its spend replaced, so a roster under test is never surrounded by
 *  a different population's terms. */
const rosterWith = (over: Partial<Scorecard['spend']>): ImproveData => ({
  ...NO_SCORECARD_BUNDLE, scorecard: scorecardWith({ ...ROSTER_SPEND, ...over }),
});

/**
 * The live roster with the bound biting: three models folded away.
 *
 * DERIVED, and the derivation is one field on each of two objects — `byModelOther` gains a positive
 * count and a measured sum, and `modelsSeen` gains the same three so the count on the tile and the
 * roster under it describe one population. The named rows are untouched.
 *
 * The magnitude is one of the two real rows' own token sum, so it is a size this harness produces
 * rather than a round number chosen to make a percentage come out.
 */
const ROSTER_WITHHELD_MODELS = 3;
const ROSTER_WITHHELD_TOKENS = ROSTER_SONNET_TOKENS;
const folded: ImproveData = rosterWith({
  modelsSeen: 2 + ROSTER_WITHHELD_MODELS,
  byModelOther: { models: ROSTER_WITHHELD_MODELS, tokens: sum(ROSTER_WITHHELD_TOKENS, 5, 25) },
});

/**
 * The live roster with the smaller model's tokens UNMEASURED.
 *
 * DERIVED by changing one field — that row's `tokens` term becomes the sentinel over `0` of the same
 * 25, which is what the producer sends for a model whose usage rows carried no total. Its call count
 * stays measured, because the two terms come off separate keys and the producer really can record one
 * without the other.
 *
 * The point of it: the roster now has no complete denominator, so the 97% that was true a moment ago
 * is unknowable and NO share may be drawn. This is the fixture for the all-or-nothing rule.
 */
const ROSTER_SHORT: ModelSpend[] = [
  ROSTER_ROWS[0],
  { ...ROSTER_ROWS[1], tokens: sum('UNMEASURED', 0, ROSTER_RUNS) },
];
const rosterShort: ImproveData = rosterWith({ byModel: ROSTER_SHORT });

// --- actor bundles ----------------------------------------------------------
/**
 * The signed-in actor's own two runs, transcribed from a staging bundle.
 *
 * Both recorded tokens, calls and busy time, and NEITHER carries a price — so this one shape holds
 * both cases the personal tiles have to get right at once: sums that were measured, beside a term
 * nothing priced. Per-run magnitudes rather than round numbers because the busy-time sum is the
 * whole reason that tile needs `formatExtent`, and two four-figure-second runs would not show it.
 */
const MINE_TOKENS = [1_961_718, 4_611_553];
const MINE_CALLS = [44, 83];
const MINE_LATENCY_MS = [1_705_441, 7_398_989];
const MINE_RUNS = 2;

const total = (values: readonly number[]) => values.reduce((a, b) => a + b, 0);

/**
 * The two model ids today's live population spends on, verbatim.
 *
 * Used unshortened because their LENGTH is part of what the personal model metric had to decide: at
 * 31 and 33 characters a pair of them does not fit a tile's footnote, which is why that metric sets a
 * count and leaves the ids on the row.
 */
const MODEL_IDS = ['global.anthropic.claude-opus-4-8', 'global.anthropic.claude-sonnet-5'];

/**
 * The same two runs, split into the four priced categories.
 *
 * Each run's four terms sum to ITS OWN real token total above, and `input` is the residual exactly as
 * `harvest_live` derives it — so a fixture here cannot describe a split that fails to reconstruct the
 * total beside it. The SHARE between the categories is apportioned by this file rather than measured:
 * the transcription carries each of these runs' totals and the POPULATION's category sums, not each
 * run's own, so the proportions are set to the population's (68% cache read, 30% cache write, 1.8%
 * output, the rest uncached) and the magnitudes are the reader's real ones. The apportionment is said
 * out loud because the alternative — quoting the population's four sums as these two runs' — would
 * assert that the other six runs recorded nothing.
 */
const MINE_INPUT = [61, 173];
const MINE_OUTPUT = [33_000, 84_000];
const MINE_CACHE_READ = [1_330_000, 3_125_000];
const MINE_CACHE_WRITE = [598_657, 1_402_380];
/** Everything sent TO a model, on the reader's own runs. */
const MINE_CONTEXT = total(MINE_INPUT) + total(MINE_CACHE_READ) + total(MINE_CACHE_WRITE);

/** One run's category split, in the wire's own spelling. */
function categories(i: number): NonNullable<ActorRun['spend']['byCategory']> {
  return {
    input: MINE_INPUT[i],
    output: MINE_OUTPUT[i],
    cacheRead: MINE_CACHE_READ[i],
    cacheWrite: MINE_CACHE_WRITE[i],
  };
}

/**
 * One of the actor's runs, carrying only the spend these metrics read.
 *
 * `cost` and `latencyMs` default to the sentinel because that is the ordinary shape rather than an
 * edge: a run is priced only when the rate table has a row for every model it used, and a bundle
 * carrying tokens with no price beside them is what the deployed producer emits. Passed explicitly
 * by the tests that are about those two terms.
 *
 * `extra` carries the four terms that are NEWER than the bundles, and it defaults to carrying none of
 * them on purpose: a row with four spend keys and no more is the shape a personal bundle written
 * before the producer projected them still has, and it is served for as long as its actor stays away.
 * So the default fixture is the OLD row, and every test about a new term opts in.
 */
function actorRun(tokens: ActorRun['spend']['tokens'],
                  llmCalls: ActorRun['spend']['llmCalls'],
                  cost: ActorRun['spend']['cost'] = 'UNMEASURED',
                  latencyMs: ActorRun['spend']['latencyMs'] = 'UNMEASURED',
                  extra: Partial<ActorRun['spend']> = {}): ActorRun {
  return {
    runId: 'run',
    workloadClass: 'conversational',
    window: 'UNKNOWN',
    verdict: { overall: 'PASS', reason: '' },
    tiers: {
      dispatch: { score: 1, outcome: 'PASS', explanation: '' },
      delivery: { score: 1, outcome: 'PASS', explanation: '' },
    },
    spend: { latencyMs, tokens, cost, llmCalls, ...extra },
  };
}

/**
 * An actor bundle carrying `runs` out of `runsTotal`, and NO daily history.
 *
 * The two counts are given separately because they really do differ: `runs` is capped to fit one
 * DynamoDB item, so a busy actor's bundle carries a prefix of their runs and nothing else says so.
 *
 * No `history` on purpose. This is the bundle shape that predates the day rows, and it is served for
 * as long as its actor's item is not rewritten — so it is what the personal figures must report as
 * unavailable rather than as zero. `withDays` is the shape they can actually be summed from.
 */
function withActor(runs: ActorRun[], runsTotal: number, over: Partial<ActorBundle> = {}):
ImproveData {
  return {
    ...harvestBundle,
    actor: {
      runs,
      runsTotal,
      evidence: {},
      waste: {
        runsWithWaste: 0, runs: runsTotal, runsNotScanned: 0, totalCalls: 0,
        totalWallMs: sum('UNMEASURED', 0, runsTotal), byKind: {},
        findings: [],
      },
      patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
      ...over,
    },
  };
}

// --- the reader's own days ----------------------------------------------------
/**
 * The two dates the staging pair above ran on, transcribed with them.
 *
 * Four days apart rather than adjacent, which is what makes the trailing windows testable at all:
 * two consecutive days fall inside every window and a test over them would pass whatever the span
 * arithmetic did.
 */
const MINE_DATES = ['2026-08-30', '2026-09-03'];

/**
 * How many SESSIONS a fixture day holds, given the requests it attributed.
 *
 * One session per request, because none of the transcribed days spawned a plan sub-session — so the two
 * counts coincide on these fixtures and a test can vary one without minding the other. Where they
 * genuinely differ, a request that fanned out means MORE sessions than runs (a real day reads
 * `runs: 17, coverage.total: 19`), and the test about that states its own block rather than deriving one.
 *
 * A day that attributed NO request still held the session whose spend landed on it: the requests behind
 * it fell outside the producer's scoring cap while their sub-sessions' spend did not, which is the shape
 * `runs: 0` beside real tokens is. So it floors at one rather than reporting a day of nothing, which
 * would caption a real total `nothing to measure`.
 */
function daySessions(runs: ActorDay['runs']): number {
  return typeof runs === 'number' && runs > 0 ? runs : 1;
}

/**
 * A day's coverage block, in the shape `improve/dto._day_row` emits: one session count for the whole
 * day, and a per-term count of how many of those sessions recorded the term.
 *
 * DERIVED from the row rather than written beside it, because deriving it is what the producer does —
 * it counts the day's contributing sessions whose record carried the term (`_measured_count`). So a
 * fixture cannot state a sum and forget to count it, or count a term it does not carry, and adding a
 * term to a day above automatically counts it here.
 *
 * Every term the producer counts is present, and a term nothing recorded is `0` rather than absent:
 * `0` against a real session count is the measured "none of this day's sessions recorded it", which is
 * a different fact from the whole block being missing. A sentinel or a `null` is not a measurement, so
 * it counts as nothing — the same admission test the sums use.
 *
 * The four categories NEST here, on both row kinds — the population row's own category sums are flat
 * where a per-actor row's nest, but that difference belongs to the row rather than to this block, which
 * is spelled identically on each.
 */
function dayCoverage(row: DaySpend, sessions: number): DayCoverage {
  const counted = (value: unknown) => (typeof value === 'number' && Number.isFinite(value)
    ? sessions : 0);
  return {
    total: sessions,
    tokens: counted(row.tokens),
    llmCalls: counted(row.llmCalls),
    latencyMs: counted(row.latencyMs),
    cost: counted(row.cost),
    costTokensPriced: counted(row.costTokensPriced),
    costTokensUnpriced: counted(row.costTokensUnpriced),
    byCategory: {
      input: counted(row.byCategory?.input),
      output: counted(row.byCategory?.output),
      cacheRead: counted(row.byCategory?.cacheRead),
      cacheWrite: counted(row.byCategory?.cacheWrite),
    },
  };
}

/**
 * One day of the reader's own spend, ONE RUN in it, so the day's figures are that run's own.
 *
 * A day with one run is what the transcribed pair really is — each of those two staging sessions ran
 * on its own day — so nothing here is apportioned or invented. `runs` defaults to 1 for that reason
 * and is passed explicitly where a test is about the run extent rather than the figures.
 *
 * The coverage block comes free and can be overridden like any other term, including with
 * `coverage: undefined` — which is how a test writes the row an earlier producer wrote, the one shape
 * a footnote must refuse to state a fraction for.
 */
function dayRow(date: string, over: Partial<ActorDay> = {}): ActorDay {
  const row: ActorDay = { date, runs: 1, ...over };
  return 'coverage' in row ? row : { ...row, coverage: dayCoverage(row, daySessions(row.runs)) };
}

/** The pricing counts a day row carries, in the producer's own convention: both halves together, and
 *  `0` unpriced meaning nothing was skipped rather than nothing was measured. */
function pricingCounts(i: number, unpriced = 0): Partial<ActorDay> {
  return { costTokensPriced: MINE_TOKENS[i] - unpriced, costTokensUnpriced: unpriced };
}

/** A day carrying the four terms the staging pair's own run rows carry, in the day-row shape. */
function mineDay(i: number, over: Partial<ActorDay> = {}): ActorDay {
  return dayRow(MINE_DATES[i], {
    tokens: MINE_TOKENS[i],
    llmCalls: MINE_CALLS[i],
    latencyMs: MINE_LATENCY_MS[i],
    ...over,
  });
}

/**
 * The same day with every WIDER term on it: the price, the category split, the model roster and the
 * two pricing counts.
 *
 * Priced in full, because that is what the live rate table does with these two models — so the
 * shortfall clause must stay SILENT here, over a measured zero rather than an absent one.
 */
function wideDay(i: number, over: Partial<ActorDay> = {}): ActorDay {
  return mineDay(i, {
    cost: [PRICED_ONE_MODEL_USD, PRICED_TWO_MODEL_USD][i],
    byCategory: categories(i),
    models: MODEL_IDS,
    ...pricingCounts(i),
    ...over,
  });
}

/**
 * A bundle whose personal half is a daily history.
 *
 * `runs` is kept alongside it, and kept NON-empty, because the wire carries both: the day rows are
 * what the totals are summed from and the run rows are the evidence half the suggestion cards read.
 * A fixture that dropped the run rows would let a metric quietly go back to reading them and still
 * pass.
 */
function withDays(history: ActorDay[], over: Partial<ActorBundle> = {}): ImproveData {
  // One run row per day, so the two halves of the bundle describe the same actor. A fixture with day
  // rows and no runs, or runs and no days, is a shape the producer does not write.
  const runs = MINE_TOKENS.slice(0, history.length)
    .map((tokens, i) => actorRun(tokens, MINE_CALLS[i]));
  return withActor(runs, runs.length, { history, ...over });
}

/** The reader's two days, carrying what the deployed producer records: tokens, calls and busy time,
 *  and no price. */
const myDaysUnpriced: ImproveData = withDays([mineDay(0), mineDay(1)]);

/** The same two days priced, and with the category split and model roster on each — the shape every
 *  one of the eight personal figures can be read from. */
const myDaysWide: ImproveData = withDays([wideDay(0), wideDay(1)]);

/** A reader the producer swept and found idle: the history key is THERE and empty. The zero state,
 *  and the only shape a `0` may be read from. */
const myDaysNone: ImproveData = withDays([]);

/** The staging pair above, unpriced: what a reader's own bundle looks like today. */
const mineUnpriced: ImproveData = withActor(
  MINE_TOKENS.map((tokens, i) => actorRun(tokens, MINE_CALLS[i], 'UNMEASURED', MINE_LATENCY_MS[i])),
  MINE_RUNS,
);

/**
 * The same pair with each run priced — at the two real per-session prices the population test above
 * uses, because those two sessions came out of this same harvest.
 */
const minePriced: ImproveData = withActor(
  MINE_TOKENS.map((tokens, i) => actorRun(
    tokens, MINE_CALLS[i], [PRICED_ONE_MODEL_USD, PRICED_TWO_MODEL_USD][i], MINE_LATENCY_MS[i])),
  MINE_RUNS,
);

/**
 * The pair again with the FOUR wider terms on each row: the category split, both model ids, and the
 * pricing token counts.
 *
 * This is the shape the widened producer emits, and the only one on which a personal cache figure, a
 * personal context split, a personal model count or a personal shortfall clause can be read at all.
 * Priced in full, because that is what the live rate table does with these two models — so the
 * shortfall clause must stay SILENT here, over a measured zero rather than an absent one.
 */
const mineWide: ImproveData = withActor(
  MINE_TOKENS.map((tokens, i) => actorRun(
    tokens, MINE_CALLS[i], [PRICED_ONE_MODEL_USD, PRICED_TWO_MODEL_USD][i], MINE_LATENCY_MS[i], {
      byCategory: categories(i),
      models: MODEL_IDS,
      costTokensPriced: tokens,
      costTokensUnpriced: 0,
    })),
  MINE_RUNS,
);

/** The same two runs with `over` on the wire in place of one term, so a partial widening is a shape
 *  the tests meet rather than one they assume away. */
const wideRun = (i: number, over: Partial<ActorRun['spend']> = {}): ActorRun => actorRun(
  MINE_TOKENS[i], MINE_CALLS[i], [PRICED_ONE_MODEL_USD, PRICED_TWO_MODEL_USD][i],
  MINE_LATENCY_MS[i],
  { byCategory: categories(i), models: MODEL_IDS, costTokensPriced: MINE_TOKENS[i],
    costTokensUnpriced: 0, ...over },
);

// --- helpers ----------------------------------------------------------------
function entry(id: string): MetricEntry {
  const found = METRICS.find((m) => m.id === id);
  if (!found) throw new Error(`no metric ${id}`);
  return found;
}

/**
 * The two views the eight entries are read at.
 *
 * `allTime` on both, because it is the default and because a period is only meaningful on the
 * personal half — the shared figures come off one harvest window and `periodRefusal` says so. The
 * trailing windows get their own describe rather than being threaded through every test here.
 */
const EVERYONE: MetricView = { scope: 'everyone', period: 'allTime' };
const YOURS: MetricView = { scope: 'yours', period: 'allTime' };

/**
 * A read, defaulting to the SHARED scope.
 *
 * Defaulted rather than passed everywhere because the population half of this file predates the
 * switches and its assertions are unchanged by them: the same eight entries answer the same way for
 * `everyone` as the sixteen-entry registry's platform-wide family did. The personal tests pass
 * `YOURS` explicitly, which is also what keeps a personal assertion from silently reading the shared
 * figure if a default ever moved.
 */
const readOf = (id: string, data: ImproveData, view: MetricView = EVERYONE) =>
  entry(id).read(data, view);

/**
 * Whatever a read can answer, under the DECLARED union.
 *
 * `readOf` goes through `entry`, so it already has this. A sweep over `METRICS` directly does not:
 * the registry is `as const`, so each read is typed by the literal shape that entry happens to
 * return today, and a field only one metric populates does not exist on the other fifteen.
 * Annotating with the declared type is what lets a sweep ask every metric about a field that the
 * type makes optional on all of them.
 */
type Reading = ScalarReading | RatioReading | Unavailable;

/**
 * Every id, once.
 *
 * There is one list now where there were two. Scope used to be declared on the entry, so a sweep had
 * to pick a family; it is a read-time parameter, so every entry answers for both and a sweep names
 * the VIEW instead. Which is the property worth pinning on its own: there is no longer a metric a
 * reader can ask about everyone and not about themselves, because there is no longer a per-family
 * list for one to be missing from.
 */
const IDS = METRICS.map((m) => m.id);

// --- the registry as data ----------------------------------------------------
describe('the registry', () => {
  it('offers eight rows for eight figures, each with a distinct id', () => {
    const ids = METRICS.map((m) => m.id);
    // EIGHT, not sixteen. Scope used to be baked into identity — `yourTokens` beside `tokens` — so
    // one question took two rows, and adding a period the same way would have made it six.
    expect(ids).toEqual([
      'tokens', 'llmCalls', 'contextVsGenerated', 'cost', 'cacheReuse', 'totalTime',
      'modelsUsed', 'tokensPerSession',
    ]);
    expect(ids).toHaveLength(8);
    // A duplicate id would make a picker's stored selection ambiguous, and `find` would silently
    // resolve to whichever came first.
    expect(new Set(ids).size).toBe(ids.length);
    // The ids that went away are the personal family's. Kept out deliberately: they were the second
    // spelling of a question these eight already ask, and a stored selection holding one of them is
    // topped up rather than migrated.
    expect(ids.filter((id) => id.startsWith('your'))).toEqual([]);
  });

  it('answers every one of the eight for either scope, from one entry', () => {
    /*
     * The property the whole surface rests on, and it is now structural rather than checked: every
     * figure can be asked of the reader's own days and of every session on the harness, because there
     * is ONE entry and the scope is what it is asked with. There is no per-family list left for a
     * question to be missing from.
     *
     * What still needs asserting is that both readings are real — an entry that ignored the scope
     * would answer identically for both, and eight rows would then be eight platform figures wearing
     * a personal label.
     */
    for (const metric of METRICS) {
      expect(typeof metric.read, metric.id).toBe('function');
      const mine = metric.read(myDaysWide, YOURS);
      const theirs = metric.read(myDaysWide, EVERYONE);
      // The same bundle, two scopes, two different readings: `myDaysWide` carries the reader's own
      // days AND the 553-session harvest scorecard, so a figure that came out the same for both
      // would be reading one source under two labels.
      expect(mine, metric.id).not.toEqual(theirs);
    }
  });

  it('says whose runs a figure is about in its label, composed from the scope', () => {
    /*
     * The ambiguity this fixes, measured: the signed-in reader's two runs are 96.5% of the live
     * population's tokens, so `Tokens · 8 of 9 sessions` and `Your tokens · across 2 runs` differ by
     * 3.5% in the figure and by nothing at all in the caption. A bare label left a reader comparing
     * magnitudes to work out which of the two they were looking at.
     *
     * Composed and not stored, so a figure cannot be called one thing in the picker and another on
     * the tile — which two per-scope label fields on the entry would eventually allow.
     */
    for (const metric of METRICS) {
      expect(metricLabel(metric, 'yours').startsWith('Your '), metric.id).toBe(true);
      expect(metricLabel(metric, 'everyone').startsWith("Everyone's "), metric.id).toBe(true);
    }
    expect(metricLabel(entry('tokens'), 'yours')).toBe('Your tokens');
    expect(metricLabel(entry('tokens'), 'everyone')).toBe("Everyone's tokens");
    expect(metricLabel(entry('cacheReuse'), 'yours')).toBe('Your cache reuse');
    // And no label is a bare noun: the platform-wide eight used to be `Tokens`, `Cost`,
    // `Cache reuse` — captioned in sessions that never said whose.
    for (const scope of SCOPES) {
      const labels = METRICS.map((m) => metricLabel(m, scope));
      for (const bare of ['Tokens', 'Cost', 'Cache reuse']) expect(labels).not.toContain(bare);
    }
  });

  it('labels every metric in sentence case with no trailing colon, at either scope', () => {
    for (const metric of METRICS) {
      // The noun the entry carries is lower case and scope-free, so it can follow either owner word
      // without a second spelling: `Your tokens`, `Everyone's tokens`.
      expect(metric.noun, metric.id).toBe(metric.noun.toLowerCase());
      for (const scope of SCOPES) {
        const label = metricLabel(metric, scope);
        expect(label).not.toMatch(/:$/);
        expect(label[0]).toBe(label[0].toUpperCase());
        // Only the first word is capitalised — title case would fight every other label on the
        // surface. `Everyone's` contributes its own apostrophe-s and no second capital.
        expect(label.slice(1)).toBe(label.slice(1).replace(/\b[A-Z][a-z]/g, (w) =>
          w.toLowerCase()));
      }
    }
  });

  it('names both sides of every split, and only of a split', () => {
    for (const metric of METRICS) {
      if (metric.kind === 'ratio') {
        expect(metric.sides.part.length).toBeGreaterThan(0);
        expect(metric.sides.rest.length).toBeGreaterThan(0);
        // `rest` is the complement, not the whole: two identical names would draw a bar that
        // reads as a share of itself.
        expect(metric.sides.part).not.toBe(metric.sides.rest);
      } else {
        expect(metric).not.toHaveProperty('sides');
      }
    }
  });

  it('renders each figure in its own unit, money included', () => {
    expect(entry('tokens').format).toBe(compactNumber);
    // Money has its own formatter because the count one is wrong for it in both directions: it
    // prints the producer's float unrounded, and drops into exponent notation below a millionth
    // of a dollar. See the cost formatter's own tests.
    expect(entry('cost').format).toBe(formatUsd);
    expect(compactNumber(PRICED_ONE_MODEL_USD)).toBe('0.2327805');
    expect(compactNumber(9e-7)).toBe('9e-7');
    // Busy time, not a count, and in days and hours rather than minutes: `formatMs`'s largest
    // unit is the minute, so it renders the harvest's total `6875m 25s` and the bundle's
    // `203m 3s` — real figures, and ones nobody sizes at a glance.
    expect(entry('totalTime').format).toBe(formatExtent);
    expect(formatExtent(HARVEST_LATENCY_MS)).toBe('4d 19h');
    expect(formatExtent(WIRE_LATENCY_MS)).toBe('3h 23m');
    /*
     * ONE formatter per figure, whichever scope it is read at — which is now structural rather than
     * kept in step by hand. A price is a price and a duration is a duration whoever's runs it came
     * off, and the sixteen-entry registry had to state that twice.
     *
     * `formatExtent` earns its place on the personal side for the same reason as on the shared one,
     * one population down: a cumulative sum over days outgrows `formatMs`, whose largest unit is the
     * minute, on two days.
     */
    expect(formatExtent(total(MINE_LATENCY_MS))).toBe('2h 32m');
    expect(formatMs(total(MINE_LATENCY_MS))).toBe('151m 44s');
  });

  it('describes what to read without importing React', () => {
    const source = readFileSync(new URL('./metricRegistry.ts', import.meta.url), 'utf8');
    expect(source).not.toMatch(/^import .*['"]react['"]/m);
    // JSX in a `.ts` file would not compile, so the guard that matters is the import: a registry
    // that reached for React would be deciding how a figure looks, which is the tile's job.
    expect(source).not.toMatch(/<[A-Z][A-Za-z]*[\s/>]/);
  });
});

// --- which unavailability collapses ------------------------------------------
/**
 * Every member of `UnavailableCode`, kept complete by the compiler rather than by hand.
 *
 * A union cannot be enumerated at run time, so sweeping the classification needs a list — and a
 * stale list would sample it while looking exhaustive. The `Exclude` below is `never` only while
 * this names every code, so adding one to the registry makes that assignment a type error.
 */
const ALL_CODES = [
  'noScorecard', 'noActorBundle', 'noActorHistory', 'noPeriod', 'notInBundle', 'noDenominator',
] as const satisfies readonly UnavailableCode[];
const everyCodeListed: Exclude<UnavailableCode, (typeof ALL_CODES)[number]> extends never
  ? true : never = true;

/** A scorecard carrying no `spend` at all, so several metrics are each missing a term of their own
 *  — one code over three different sentences. */
const bareScorecard: ImproveData = { ...NO_SCORECARD_BUNDLE, scorecard: {} as Scorecard };

/** The unavailable reading of `id`, or a failure saying it was a figure after all. */
function unavailable(id: string, data: ImproveData, view: MetricView = EVERYONE): Unavailable {
  const reading = readOf(id, data, view);
  if (reading.available) throw new Error(`${id} is available on this bundle`);
  return reading;
}

describe('which unavailability is structural', () => {
  it('answers for every code the type admits, and puts codes in both classes', () => {
    // `tsc` is what rejects a code nobody classified; this only witnesses that the list swept
    // below is still the whole union rather than a prefix of it.
    expect(everyCodeListed).toBe(true);
    for (const code of ALL_CODES) expect(typeof isStructural(code), code).toBe('boolean');
    // A classification that answered the same way for everything would pass the sweep while making
    // the distinction it exists to draw do nothing.
    expect(ALL_CODES.filter((code) => isStructural(code)).length).toBeGreaterThan(0);
    expect(ALL_CODES.filter((code) => !isStructural(code)).length).toBeGreaterThan(0);
  });

  it('collapses the codes that are one fact about the bundle, each saying one thing', () => {
    /*
     * The case the collapse exists for, on the payload production serves: no `scorecard` key and no
     * `actor` key. Read at either scope, all eight metrics report ONE code, and it carries one
     * sentence across all of them — which is what makes saying it once complete rather than a summary
     * of several facts, and is why they are structural.
     *
     * Both scopes, because a reader can be sitting on either: the shared half reports the missing
     * scorecard and the personal half the missing bundle, and eight repetitions of either sentence
     * would bury its single cause under its own repetition.
     */
    for (const [view, expected] of [[EVERYONE, 'noScorecard'], [YOURS, 'noActorBundle']] as const) {
      const readings = METRICS.map(
        (metric) => unavailable(metric.id, NO_SCORECARD_BUNDLE, view));
      expect(readings.map((r) => r.code)).toEqual(Array(8).fill(expected));
      expect(new Set(readings.map((r) => r.reason)).size).toBe(1);
      expect(isStructural(expected)).toBe(true);
    }
  });

  it('collapses a bundle whose personal half carries no day rows', () => {
    /*
     * A third structural code, and it is not the same fact as an absent bundle. The item IS there —
     * its run rows and waste findings read fine — and what is missing is the daily history the totals
     * are summed from, which resolves on that actor's next producer pass rather than on their next
     * session. One cause behind all eight, so it collapses.
     */
    const readings = METRICS.map((metric) => unavailable(metric.id, minePriced, YOURS));
    expect(readings.map((r) => r.code)).toEqual(Array(8).fill('noActorHistory'));
    expect(new Set(readings.map((r) => r.reason)).size).toBe(1);
    expect(isStructural('noActorHistory')).toBe(true);
    // And it must not read as a zero. A present-but-empty history is the zero; this is nobody having
    // looked, and the sentence says which.
    expect(readings[0].reason).toMatch(/not a record of your having run nothing/);
  });

  it('leaves alone the two codes that answer for one metric only', () => {
    /*
     * Each of these is the whole answer for its own metric and says nothing about the other eleven.
     * `notInBundle` is the sharpest case: one code over four different missing terms, so printing
     * the first metric's sentence as the cause of all four would be wrong about three of them.
     */
    const missing = ['tokens', 'llmCalls', 'modelsUsed']
      .map((id) => unavailable(id, bareScorecard));
    expect(new Set(missing.map((r) => r.code))).toEqual(new Set(['notInBundle']));
    expect(new Set(missing.map((r) => r.reason)).size).toBe(3);

    const perMetric = [
      ...missing.map((r) => r.code),
      // A scorecard with a token total and no cost term: cost is MISSING, which is a producer
      // omission now that pricing exists, and there is no per-session figure over zero sessions.
      unavailable('cost', withSpend({ cost: undefined as unknown as MeasuredSum })).code,
      unavailable('tokensPerSession', wireBundle).code,
    ];
    expect(new Set(perMetric)).toEqual(new Set(['notInBundle', 'noDenominator']));
    for (const code of perMetric) expect(isStructural(code), code).toBe(false);
  });
});

// --- which unavailability says itself in a phrase -----------------------------
/**
 * `brief`: the one cause whose sentence a reader is NOT asked to read.
 *
 * A default view gets a handful of sentences for the whole page, and one cause was spending four of
 * them before the first figure — the reader with no history of their own, which is most readers. So
 * that cause carries a phrase as well as its argument, and what these pin is that the two are
 * different jobs: the phrase is what a page prints, the argument is what the picker and the tooltip
 * print, and neither replaces the other.
 *
 * Every code reached through a real read rather than a hand-built object, so a phrase added to one of
 * the others fails here instead of appearing on a page nobody measured.
 */
const CAUSES = {
  noScorecard: unavailable('tokens', NO_SCORECARD_BUNDLE, EVERYONE),
  noActorBundle: unavailable('tokens', NO_SCORECARD_BUNDLE, YOURS),
  noActorHistory: unavailable('tokens', minePriced, YOURS),
  noPeriod: periodRefusal({ scope: 'everyone', period: 'allTime' })!,
  notInBundle: unavailable('tokens', bareScorecard),
  noDenominator: unavailable('tokensPerSession', wireBundle),
} as const satisfies Record<UnavailableCode, Unavailable>;

describe('the cause that says itself in a phrase', () => {
  it('is the absent bundle, and is not any of the others', () => {
    for (const [code, cause] of Object.entries(CAUSES)) {
      // The map is only worth reading if each entry really is the code it is filed under.
      expect(cause.code, code).toBe(code);
      expect(cause.brief === undefined, code).toBe(code !== 'noActorBundle');
    }
  });

  it('states an absence, and never a zero, a failure or the harness’s own words', () => {
    const brief = CAUSES.noActorBundle.brief ?? '';
    expect(brief).toBe("Your own figures aren't in this report yet, but Everyone's are.");
    /*
     * The three rules the phrase exists under. It must not claim a measurement: a zero would say
     * nothing was spent where nothing was looked at, so no digit and no word standing for one. It must
     * not read as a fault, because nothing failed. And it must not be the argument in miniature — the
     * words the long form needs are the words the reader does not have.
     */
    expect(brief).not.toMatch(/\d/);
    expect(brief).not.toMatch(/\b(zero|none|nothing|never)\b/i);
    expect(brief).not.toMatch(/\b(error|fail|failed|failure|missing|unavailable)\b/i);
    for (const word of ['bundle', 'producer', 'actor', 'pass', 'harvest', 'scope', 'measure']) {
      expect(brief.toLowerCase(), word).not.toContain(word);
    }
    // One phrase. Four sentences here is the defect it was written to remove, and three is that
    // defect smaller.
    expect(brief.match(/[.!?]/g)).toHaveLength(1);
    // And somewhere to go, named as the switch itself names it rather than described.
    expect(brief).toContain(SCOPE_LABEL.everyone);
  });

  it('keeps the whole argument on the reason, which is what a reader asking why reaches', () => {
    const { brief, reason } = CAUSES.noActorBundle;
    // Not an abridgement of the reason and not a prefix of it: a head of a paragraph reads as a
    // paragraph, which is what `abridge` is for and what this is not.
    expect(reason).not.toContain(brief);
    expect(reason.match(/[.!?]/g)?.length ?? 0).toBeGreaterThan(1);
    // The two facts the argument carries that the phrase deliberately does not: why not zero, and
    // when it resolves.
    expect(reason).toMatch(/not even as zero/);
    expect(reason).toMatch(/arrives after its next pass/);
  });
});

// --- branch one: the bundle predates the scorecard ---------------------------
describe('a bundle with no scorecard', () => {
  it('is the committed payload, so this branch is production and not a hypothesis', () => {
    expect(NO_SCORECARD_BUNDLE.scorecard).toBeUndefined();
    expect(NO_SCORECARD_BUNDLE.suggestions.length).toBeGreaterThan(0);
  });

  it('reports every scorecard-backed metric unavailable, with one shared cause', () => {
    for (const id of IDS) {
      const reading = readOf(id, NO_SCORECARD_BUNDLE);
      expect(reading.available, id).toBe(false);
      // One code across all of them is what lets a picker say it once instead of eight times.
      expect(reading, id).toMatchObject({ code: 'noScorecard' });
    }
  });

  it('never turns the absent scorecard into a measured figure', () => {
    for (const id of IDS) {
      const reading = readOf(id, NO_SCORECARD_BUNDLE);
      // `?? 0` on a key the bundle does not have is how "unknown" becomes "none".
      expect(reading).not.toHaveProperty('value');
      expect(reading).not.toHaveProperty('ratio');
    }
  });

  it('says why in prose a reader can act on, never a code name', () => {
    const reading = readOf('tokens', NO_SCORECARD_BUNDLE);
    expect(reading.available).toBe(false);
    if (reading.available) return;
    expect(reading.reason).toMatch(/no population scorecard/);
    expect(reading.reason).not.toMatch(/noScorecard|undefined|null/);
  });
});

// --- branch two: the producer ran and could not measure ----------------------
describe('a bundle whose token terms are UNMEASURED', () => {
  it('shows the sentinel as the value rather than hiding the metric', () => {
    // `MetricTile` hatches a sentinel and tooltips it. Reporting the metric unavailable instead
    // would drop the one statement the bundle can make: the producer looked and found nothing.
    expect(readOf('tokens', wireBundle))
      .toEqual({ available: true, value: 'UNMEASURED', sub: `0 of ${WIRE_RUNS} sessions` });
    expect(readOf('llmCalls', wireBundle))
      .toEqual({ available: true, value: 'UNMEASURED', sub: `0 of ${WIRE_RUNS} sessions` });
  });

  it('withholds the per-call figure rather than dividing nothing by nothing', () => {
    const reading = readOf('llmCalls', wireBundle) as ScalarReading;
    expect(reading.sub).not.toMatch(/per call/);
    expect(reading.sub).not.toMatch(/NaN|Infinity/);
  });

  it('reports an unmeasured share as unmeasured, not as a zero share', () => {
    for (const id of ['contextVsGenerated', 'cacheReuse']) {
      // A bare `ratio: 0` would draw an empty track, which reads as "no cache reuse at all".
      expect(readOf(id, wireBundle), id).toEqual({
        available: true, value: 'UNMEASURED', ratio: 'UNMEASURED',
        sub: `0 of ${WIRE_RUNS} sessions`,
      });
    }
  });

  it('still reads the one term that bundle did measure', () => {
    expect(readOf('totalTime', wireBundle)).toEqual({
      available: true, value: WIRE_LATENCY_MS, sub: `${WIRE_RUNS} of ${WIRE_RUNS} sessions`,
    });
  });

  it('reads a zero model count as a count, because that is what it is', () => {
    // `modelsSeen` is a plain number, not a `MeasuredSum`. Treating it as one would read
    // `undefined` off it; treating its `0` as unmeasured would hide a correct answer.
    expect(readOf('modelsUsed', wireBundle)).toEqual({
      available: true, value: 0, sub: `over 0 of ${WIRE_RUNS} sessions`,
    });
  });

  it('refuses a per-session figure when no session was measured', () => {
    const reading = readOf('tokensPerSession', wireBundle);
    expect(reading.available).toBe(false);
    if (reading.available) return;
    expect(reading.code).toBe('noDenominator');
    // The reason has to carry the fraction, or "no per-session figure" looks like a bug.
    expect(reading.reason).toContain(`0 of ${WIRE_RUNS}`);
  });
});

// --- branch three: measured ---------------------------------------------------
describe('the real harvest, measured on two thirds of its sessions', () => {
  const expected = `${HARVEST_SESSIONS_WITH_USAGE} of ${HARVEST_SESSIONS} sessions`;

  it('reports the token total against the sessions that carried one', () => {
    expect(readOf('tokens', harvestBundle))
      .toEqual({ available: true, value: HARVEST_TOKENS, sub: expected });
  });

  it('puts the per-call figure in the footnote, with the coverage beside it', () => {
    expect(readOf('llmCalls', harvestBundle)).toEqual({
      available: true, value: HARVEST_CALLS, sub: `39.2k tokens per call · ${expected}`,
    });
    // 211.7M over 5,406 calls. Rounded to whole tokens and set by the shared count formatter, so
    // the summary cannot spell a magnitude differently from the row it summarises.
    expect(compactNumber(Math.round(HARVEST_TOKENS / HARVEST_CALLS))).toBe('39.2k');
  });

  it('splits context from generated over the whole token total, with no residual', () => {
    // Bedrock's own arithmetic. If this ever failed, the split's denominator would not be the
    // token total and both shares would be wrong.
    expect(HARVEST_CONTEXT + HARVEST_OUTPUT).toBe(HARVEST_TOKENS);
    expect(readOf('contextVsGenerated', harvestBundle)).toEqual({
      available: true, value: HARVEST_CONTEXT, ratio: HARVEST_CONTEXT / HARVEST_TOKENS,
      sub: expected,
    });
  });

  it('leaves the generated side real but small, rather than rounding it away', () => {
    const reading = readOf('contextVsGenerated', harvestBundle) as RatioReading;
    // 98.3%: nearly all of it is context, and the 1.7% that is not is 3.6M generated tokens.
    expect(reading.ratio).toBeGreaterThan(0.98);
    expect(reading.ratio).toBeLessThan(1);
  });

  it('reads cache reuse against cache writes alone, not against every token', () => {
    expect(readOf('cacheReuse', harvestBundle)).toEqual({
      available: true,
      value: HARVEST_CACHE_READ,
      ratio: HARVEST_CACHE_READ / (HARVEST_CACHE_READ + HARVEST_CACHE_WRITE),
      sub: expected,
    });
    // Folding the other two categories in would drag this from 76% to 74% and answer a question
    // nobody asked.
    expect(HARVEST_CACHE_READ / (HARVEST_CACHE_READ + HARVEST_CACHE_WRITE))
      .not.toBe(HARVEST_CACHE_READ / HARVEST_TOKENS);
  });

  it('captions each term against ITS OWN coverage, not one shared number', () => {
    // Busy time is measured on all 553 sessions while tokens are measured on 354. A summary that
    // captioned both `354 of 553` would understate the one and be right about the other by luck.
    expect(readOf('totalTime', harvestBundle)).toEqual({
      available: true, value: HARVEST_LATENCY_MS,
      sub: `${HARVEST_SESSIONS} of ${HARVEST_SESSIONS} sessions`,
    });
  });

  it('counts models over the sessions that recorded one', () => {
    expect(readOf('modelsUsed', harvestBundle))
      .toEqual({ available: true, value: HARVEST_MODELS, sub: `over ${expected}` });
  });

  it('divides tokens by exactly the sessions that contributed them', () => {
    // A run IS a session, so this is arithmetic rather than an estimate — and the divisor is
    // `measuredOf`, which is why the footnote names it.
    expect(readOf('tokensPerSession', harvestBundle)).toEqual({
      available: true,
      value: Math.round(HARVEST_TOKENS / HARVEST_SESSIONS_WITH_USAGE),
      sub: `over ${expected}`,
    });
    expect(compactNumber(Math.round(HARVEST_TOKENS / HARVEST_SESSIONS_WITH_USAGE))).toBe('597.9k');
    // Not over the whole population: that would report 383k, understating it by a third.
    expect(Math.round(HARVEST_TOKENS / HARVEST_SESSIONS_WITH_USAGE))
      .not.toBe(Math.round(HARVEST_TOKENS / HARVEST_SESSIONS));
  });
});

// --- cost -------------------------------------------------------------------
describe('cost', () => {
  /** The caveat half of a cost footnote, i.e. everything before the denominator. */
  const caveatOf = (data: ImproveData): string => {
    const reading = readOf('cost', data);
    if (!reading.available) throw new Error('cost is unavailable on this bundle');
    return (reading.sub ?? '').split(' · ')[0];
  };

  it('sets the figure the producer priced, against the sessions it priced', () => {
    expect(readOf('cost', pricedBundle)).toEqual({
      available: true,
      value: PRICED_TOTAL_USD,
      sub: `ESTIMATE, not billed truth · ${PRICED_SESSIONS} of ${HARVEST_SESSIONS} sessions priced`,
      // Both forms on one reading: the footnote's head, and the whole sentence for the channels
      // with room for it. `toEqual` is exhaustive here, so a field going missing fails this too.
      caveat: PRICED_BASIS,
    });
    // NOT the token term's denominator, which is 354 on this same bundle. A session no model of
    // which the rate table can price is not priced at all, so the two really do diverge, and a
    // footnote quoting the larger one would caption two sessions' bill as 354 sessions'.
    expect(HARVEST_SPEND.tokens.measuredOf).not.toBe(PRICED_SESSIONS);
    expect(readOf('tokens', pricedBundle))
      .toMatchObject({ sub: `${HARVEST_SESSIONS_WITH_USAGE} of ${HARVEST_SESSIONS} sessions` });
  });

  it('passes the dollars through untouched, pricing nothing of its own', () => {
    const reading = readOf('cost', pricedBundle) as ScalarReading;
    // To the last digit the producer sent. Rounding, scaling or re-deriving here would each be the
    // UI pricing tokens, which it has no rate table to do — the rounding belongs to the formatter.
    expect(reading.value).toBe(PRICED_ONE_MODEL_USD + PRICED_TWO_MODEL_USD);
    expect(reading.value).not.toBe(Number(PRICED_TOTAL_USD.toFixed(2)));
  });

  it('reports a population nothing priced as unmeasured, not as unavailable', () => {
    /*
     * `UNMEASURED` is a VALUE. The producer ran and priced nothing — because it had no rate table,
     * which its own sentence says — and the tile hatches that, exactly as it does the token total
     * on this same bundle. Reporting the metric unavailable instead would drop the statement that
     * pricing was attempted at all.
     */
    expect(readOf('cost', wireBundle)).toEqual({
      available: true,
      value: 'UNMEASURED',
      sub: `tokens measured, cost not priced · 0 of ${WIRE_RUNS} sessions priced`,
      // An unpriced population's basis is long too — it names the missing input and warns against
      // multiplying a token total by one rate — so it reaches the reader by the same channel.
      caveat: NO_RATE_TABLE_BASIS,
    });
    expect(readOf('tokens', wireBundle)).toMatchObject({ available: true, value: 'UNMEASURED' });
  });

  it('is unavailable only when the bundle carries no cost term at all', () => {
    // The one absence left, and it IS an omission now that the producer prices: `notInBundle`, so a
    // reader is told the term is missing rather than that nothing can price it.
    const reading = readOf('cost', withSpend({ cost: undefined as unknown as MeasuredSum }));
    expect(reading).toMatchObject({ available: false, code: 'notInBundle' });
    if (reading.available) return;
    expect(reading.reason).toContain('a cost total');
    // And a term with a sum but no denominator is the same answer: a bill captioned `not a count`
    // reads as authoritative to anyone who does not stop to parse the footnote.
    expect(readOf('cost', withSpend({ cost: { sum: PRICED_TOTAL_USD } as MeasuredSum })))
      .toMatchObject({ available: false, code: 'notInBundle' });
  });

  it("abridges the producer's own sentence rather than paraphrasing it", () => {
    const caveat = caveatOf(pricedBundle);
    // A PREFIX of what the producer wrote, character for character. Wording of our own would go on
    // asserting these rates' provenance the day after they are re-sourced — and where they came
    // from and when is the whole of what makes the figure trustworthy.
    expect(PRICED_BASIS.startsWith(caveat)).toBe(true);
    expect(caveat).toBe('ESTIMATE, not billed truth');
    // The rest is not lost because it was long: it does not fit. The whole sentence is 839
    // characters — a dozen lines of meta text in a tile a third of a row wide — and it grew by the
    // 213 that describe partial pricing, which is why the shortfall gets a clause of its own rather
    // than more of this sentence.
    expect(PRICED_BASIS.length).toBe(839);
    expect(caveat.length).toBeLessThan(CAVEAT_LINE);
  });

  it('gives the whole sentence a home instead of dropping the 813 characters that do not fit', () => {
    /*
     * The footnote's 26 characters are the head of the sentence, and for a while they were ALL of it
     * a reader could ever get to. While the producer could price nothing this metric was UNAVAILABLE
     * and the basis was its `reason`, which the picker prints unabridged; pricing made the reading
     * available and there was nowhere on an available reading to put a sentence longer than a
     * footnote. So the reachable text went from 839 characters to 26 as a side effect of the figure
     * starting to work.
     *
     * `caveat` is that channel reopened: the SAME string, verbatim, for the tile's tooltip and the
     * picker's row. Character-exact against the producer's own sentence, because a paraphrase would
     * go on asserting these rates' provenance the day after they are re-sourced.
     */
    const reading = readOf('cost', pricedBundle) as ScalarReading;
    expect(reading.caveat).toBe(PRICED_BASIS);
    expect(reading.caveat).toHaveLength(839);
    // And the abridgement is honest: what the footnote shows is a true PREFIX of what the caveat
    // carries, so following the one leads into the other rather than to a second wording of it.
    expect(reading.caveat?.startsWith(caveatOf(pricedBundle))).toBe(true);
  });

  it('puts every fact the footnote cannot reach where a reader can reach it', () => {
    /*
     * The four that make a hand-entered rate AUDITABLE rather than merely plausible, and their
     * character offsets in a sentence the footnote cuts at 26: the price page (117), the date the
     * rates were read off it by hand (158), the region check that justifies pricing a us-west-2
     * harness off one region's column (217), and that the cache-write rate is the five-minute one,
     * so a run opting into the 1-hour TTL costs up to 1.6x what is shown (502).
     *
     * The last is the one this population can least afford to lose: cache writes are 32% of its
     * tokens and 78% of its bill, so the 1.6x is an ambiguity about most of the money.
     *
     * Asserted on the caveat AND against the footnote, because half of this is that they did not
     * merely move — the footnote must not have grown to fit them.
     */
    const reading = readOf('cost', pricedBundle) as ScalarReading;
    for (const fact of [
      'https://aws.amazon.com/bedrock/pricing/',
      'read by hand on 2026-08-31',
      'identically in every region, checked',
      '1.6x what is reported here',
    ]) {
      expect(reading.caveat, fact).toContain(fact);
      expect(reading.sub, fact).not.toContain(fact);
    }
    // The pointer at the token counts sits 664 characters in — past anything a footnote could show —
    // which is why the shortfall is composed as its own clause rather than left to this sentence.
    expect(reading.caveat).toContain('priced and unpriced TOKEN counts');
    expect(PRICED_BASIS.indexOf('priced and unpriced TOKEN counts')).toBe(664);
  });

  it('says nothing twice: no caveat when the footnote already carries every word', () => {
    /*
     * A basis short enough to fit under the figure is already wholly in front of the reader. Sending
     * it on to the tooltip and the picker row as well would print one sentence three times, and a
     * tooltip that only ever repeats the line beneath it teaches a reader to stop opening tooltips.
     *
     * The rule is stated against the FOOTNOTE rather than against a second length constant, so the
     * two cannot come to disagree about whether anything was dropped.
     */
    for (const basis of ['rates re-sourced 2026-12-01', 'estimate from hand-entered rates']) {
      const reading = readOf('cost', withSpend({ costBasis: basis })) as ScalarReading;
      expect(reading.sub, basis).toContain(basis);
      expect(reading.caveat, basis).toBeUndefined();
    }
  });

  it('offers no empty tooltip on a bundle that stated no basis at all', () => {
    // `pricing basis not stated` is the whole of what such a bundle supports, and the footnote says
    // it. A `title=""` beside that would give the tile a tooltip-shaped affordance with nothing
    // behind it, which is a worse answer than no tooltip.
    for (const basis of ['', undefined as unknown as string, '   ']) {
      const reading = readOf('cost', withSpend({ costBasis: basis })) as ScalarReading;
      expect(reading.caveat, JSON.stringify(basis)).toBeUndefined();
    }
  });

  it('carries the wire’s long sentence, not a copy of the one it usually sees', () => {
    /*
     * A basis this file has never seen, long enough that the footnote has to cut it. Both halves must
     * come off THIS string: a caveat holding a remembered sentence would describe last quarter's
     * rate table beside this quarter's figure, which is worse than saying nothing.
     */
    const other = 'RECOMPUTED, not billed truth: rates re-read by hand on 2026-12-01 from the '
      + 'internal price sheet, whose cache-write column is the one-hour figure this time, so a run '
      + 'on the five-minute default costs LESS than reported rather than more';
    const reading = readOf('cost', withSpend({ costBasis: other })) as ScalarReading;
    expect(reading.caveat).toBe(other);
    expect(reading.caveat).not.toBe(PRICED_BASIS);
    // Same clause rule as the producer's own sentence: broken at the colon it wrote, no ellipsis.
    expect(reading.sub?.startsWith('RECOMPUTED, not billed truth · ')).toBe(true);
    expect(other.startsWith('RECOMPUTED, not billed truth')).toBe(true);
  });

  it('reads the basis off the bundle rather than holding a copy of it', () => {
    const other = 'rates sourced 2026-09-01 from the internal price sheet';
    const reading = readOf('cost', withSpend({
      cost: sum(PRICED_TOTAL_USD, PRICED_SESSIONS, HARVEST_SESSIONS), costBasis: other,
    })) as ScalarReading;
    // A hedge held here would keep printing yesterday's provenance after the producer re-sourced
    // its rates, and provenance is the whole of what makes a hand-entered price trustworthy.
    expect(reading.sub)
      .toBe(`${other} · ${PRICED_SESSIONS} of ${HARVEST_SESSIONS} sessions priced`);
  });

  it('breaks a long basis on a word when it opens with no clause of its own', () => {
    // The producer leads with `ESTIMATE, not billed truth:` today and need not tomorrow. A basis
    // with no summary clause is cut on a word and marked: a preview that stops mid-token reads as
    // corruption rather than as abridgement.
    const runOn = 'rates were read by hand from the vendor pricing page on a date this sentence '
      + 'declines to name, and no clause of it summarises the rest';
    const caveat = caveatOf(withSpend({ costBasis: runOn }));
    expect(caveat.endsWith('…')).toBe(true);
    expect(runOn.startsWith(caveat.slice(0, -1))).toBe(true);
    expect(caveat.slice(0, -1).endsWith(' ')).toBe(false);
    expect(caveat.length).toBeLessThan(CAVEAT_LINE);
  });

  it('carries a short basis whole rather than abridging what already fits', () => {
    const short = 'rates re-sourced 2026-12-01';
    expect(caveatOf(withSpend({ costBasis: short }))).toBe(short);
  });

  it('says the basis is missing rather than presenting a bare amount', () => {
    /*
     * A dollar figure with nothing about where its rates came from reads as billed truth, and this
     * producer's figure is an estimate off hand-entered rates. The figure is still SHOWN: it was
     * computed, and withholding a real measurement over a missing caption is the mistake this
     * metric used to make from the other end.
     */
    for (const basis of ['', undefined as unknown as string, '   ']) {
      const reading = readOf('cost', withSpend({
        cost: sum(PRICED_TOTAL_USD, PRICED_SESSIONS, HARVEST_SESSIONS), costBasis: basis,
      })) as ScalarReading;
      expect(reading.value).toBe(PRICED_TOTAL_USD);
      expect(reading.sub).toBe(
        `pricing basis not stated · ${PRICED_SESSIONS} of ${HARVEST_SESSIONS} sessions priced`);
    }
  });
});

// --- a bill that is short, and says so ----------------------------------------
describe('a cost the producer could price only in part', () => {
  const subOf = (data: ImproveData): string => {
    const reading = readOf('cost', data);
    if (!reading.available) throw new Error('cost is unavailable on this bundle');
    return reading.sub ?? '';
  };
  /** What the two fixtures below share: the same basis head over the same RUN denominator. */
  const PRICED_RUNS_CLAUSE = 'ESTIMATE, not billed truth · '
    + `${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions priced`;

  it('says nothing about a shortfall when every token was priced', () => {
    /*
     * The common case, and the live one: two models, both with rate rows, so the count of unpriced
     * tokens is a measured `0`. A `0 of 6.8M tokens unpriced` clause under every priced figure
     * would be noise on every population priced so far and would teach a reader to skip it — so the
     * footnote is the two-clause one it has always been, and the third clause means something when
     * it does appear.
     */
    expect(subOf(livePriced)).toBe(PRICED_RUNS_CLAUSE);
    expect(subOf(livePriced)).not.toMatch(/unpriced/);
    // Silence over a MEASURED zero, not over an absent key: the wire really does carry the pair.
    expect(livePriced.scorecard!.spend.cost.tokensUnpriced).toBe(0);
    expect(livePriced.scorecard!.spend.cost.tokensPriced).toBe(LIVE_TOKENS);
  });

  it('names the tokens a short figure leaves out, after the runs that contributed', () => {
    /*
     * The failure the token counts exist to prevent, in one comparison: this bundle and the one
     * above have the SAME run denominator — every session that recorded usage was priced, because
     * one unpriceable model does not disqualify the two beside it — and this figure is short by
     * 90,570 tokens' worth of spend. Coverage read off runs alone would caption both identically.
     */
    expect(readOf('cost', partlyPriced)).toEqual({
      available: true,
      value: LIVE_COST_USD,
      sub: `${PRICED_RUNS_CLAUSE} · 90.6k of 6.9M tokens unpriced`,
      caveat: PRICED_BASIS,
    });
    expect(partlyPriced.scorecard!.spend.cost.measuredOf)
      .toBe(livePriced.scorecard!.spend.cost.measuredOf);
    expect(subOf(partlyPriced).startsWith(subOf(livePriced))).toBe(true);
    // And the figure itself is untouched — the clause reports the gap, it does not adjust the sum.
    expect(formatUsd(LIVE_COST_USD)).toBe('$16.74');
  });

  it('counts the tokens PRICING saw, not every token the population measured', () => {
    /*
     * The denominator of that clause is the pair's own sum, and the two can diverge: a session that
     * recorded token counts but no per-model breakdown is unpriceable, so its tokens are in the
     * token term and in NEITHER half of the cost pair. Here a ninth session is exactly that — nine
     * of nine sessions recorded tokens, eight of nine priced — and the clause must still be taken
     * over the 6.9M pricing actually saw. Read against `spend.tokens` it would say `7.0M`, quietly
     * reporting a smaller share of the money as missing than is.
     */
    const withUnpriceableSession = liveWith({
      tokens: sum(LIVE_TOKENS + UNPRICED_TOKENS * 2, LIVE_RUNS, LIVE_RUNS),
      cost: costSum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS, LIVE_TOKENS, UNPRICED_TOKENS),
    });
    expect(subOf(withUnpriceableSession)).toBe(`${PRICED_RUNS_CLAUSE} · 90.6k of 6.9M tokens `
      + 'unpriced');
    expect(compactNumber(LIVE_TOKENS + UNPRICED_TOKENS * 2)).toBe('7.0M');
  });

  it('states the whole population unpriced beside the sentinel it hatches', () => {
    /*
     * Nothing priced, which the producer reports as the sentinel over `measuredOf: 0` and never as
     * `$0`. The clause still belongs: `0 of 9 sessions priced` says no session was priced and this
     * says how much spend that was, which is the difference between a population that cost nothing
     * and one nobody could price. The value stays the sentinel, so no formatter is reached.
     */
    const nothingPriced = liveWith({ cost: costSum('UNMEASURED', 0, LIVE_RUNS, 0, LIVE_TOKENS) });
    expect(readOf('cost', nothingPriced)).toEqual({
      available: true,
      value: 'UNMEASURED',
      sub: `ESTIMATE, not billed truth · 0 of ${LIVE_RUNS} sessions priced · `
        + '6.8M of 6.8M tokens unpriced',
      caveat: PRICED_BASIS,
    });
  });

  it('stays quiet on a bundle written before the two counts existed', () => {
    // Absent is not zero. A scorecard whose `cost` carries the run denominator alone is a live
    // shape — the emitted bundle is one — and `?? 0` on those keys would assert a complete figure
    // nobody measured. The two-clause footnote is the whole of what such a bundle supports.
    for (const bundle of [pricedBundle, wireBundle]) {
      const cost = bundle.scorecard!.spend.cost;
      expect('tokensPriced' in cost).toBe(false);
      expect('tokensUnpriced' in cost).toBe(false);
      expect(subOf(bundle)).not.toMatch(/unpriced/);
      expect(subOf(bundle).split(' · ')).toHaveLength(2);
    }
  });

  it('will not print a shortfall it cannot caption', () => {
    // One half of the pair is not a fraction. A bare `90.6k tokens unpriced` is a magnitude with no
    // denominator, which is what carrying a denominator everywhere else exists to prevent — so the
    // clause is withheld and cost's run coverage is left to say what it can.
    for (const half of [{ tokensUnpriced: UNPRICED_TOKENS }, { tokensPriced: LIVE_TOKENS }]) {
      const data = liveWith({
        cost: { ...sum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS), ...half },
      });
      expect(subOf(data), JSON.stringify(half)).toBe(PRICED_RUNS_CLAUSE);
    }
  });

  it('treats a count that is not a positive number as stating no shortfall', () => {
    /*
     * `0` is the measured "nothing was skipped". A negative, a `NaN` or a string off a malformed
     * wire is corruption, and corruption cannot be turned into a quantity of missing money —
     * `compactNumber` would set it as `not a count`, which under a price reads as a producer bug in
     * the figure rather than in the caption.
     */
    for (const broken of [0, -UNPRICED_TOKENS, Number.NaN, Number.POSITIVE_INFINITY,
      null as unknown as number, '90570' as unknown as number]) {
      const data = liveWith({
        cost: costSum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS, LIVE_TOKENS, broken),
      });
      expect(subOf(data), String(broken)).toBe(PRICED_RUNS_CLAUSE);
    }
  });
});

// --- what never reached the population at all ---------------------------------
/**
 * One real 24h staging harvest, replayed at the deployed cap: 39 sessions harvested, 3 under the span
 * floor, so 36 discoverable, of which a cap of 25 admitted 25. Transcribed from the same replay
 * `improve/dto` records, and `discoverable + belowMinSpans` is the whole harvest by construction.
 */
const DISCOVERED: DiscoveredSessions = { discoverable: 36, admitted: 25, belowMinSpans: 3 };

/**
 * The same harness on 2026-08-29, before adaptive windowing: the usage query measured 44 sessions,
 * the span harvest kept 20, and 13.3M of the 36.8M measured tokens were discarded with the 24 it
 * dropped. Measured in `harvest_live._usage_shortfall`.
 *
 * The magnitude is the point: a third of the measured spend, missing from a figure whose own coverage
 * fraction reads complete.
 */
const DISCARDED_SESSIONS = 24;
const MEASURED_SESSIONS = 44;
const SHORTFALL: UsageShortfall = {
  tokensDiscarded: 13_300_000,
  tokensMeasured: 36_800_000,
  sessionsDiscarded: DISCARDED_SESSIONS,
  sessionsMeasured: MEASURED_SESSIONS,
};

/** What a harvest that never audited itself sends: the sentinel on all four, never zeros. A skipped
 *  usage query and a cap that did not bind are different facts and only the second is good news. */
const NO_AUDIT: UsageShortfall = {
  tokensDiscarded: 'UNMEASURED', tokensMeasured: 'UNMEASURED',
  sessionsDiscarded: 'UNMEASURED', sessionsMeasured: 'UNMEASURED',
};

/**
 * The live population with either or both of the two blocks the producer just started emitting.
 *
 * Both default to ABSENT, which is what the deployed item carries — read from staging: its
 * `population` has no `sessions` and its `spend` no `usageShortfall` — so every test here opts in and
 * the absent case is the one every other test in this file already covers.
 */
function withAudit(over: {
  sessions?: DiscoveredSessions; usageShortfall?: UsageShortfall;
}): ImproveData {
  const card = scorecardWith(over.usageShortfall === undefined ? LIVE_SPEND
    : { ...LIVE_SPEND, usageShortfall: over.usageShortfall });
  return {
    ...NO_SCORECARD_BUNDLE,
    scorecard: over.sessions === undefined ? card
      : { ...card, population: { ...card.population, sessions: over.sessions } },
  };
}

describe('the spend the harvest measured and never handed over', () => {
  const subOf = (id: string, data: ImproveData): string => {
    const reading = readOf(id, data);
    if (!reading.available) throw new Error(`${id} is unavailable on this bundle`);
    return reading.sub ?? '';
  };

  it('names the discarded tokens under the figure they are missing from', () => {
    /*
     * The reading this block exists to break. `spend.tokens` sums the sessions the population ADMITTED
     * and counts its coverage over those alone, so a session whose spans the row cap dropped is absent
     * from both halves of the fraction rather than short within it — the live item reads
     * `25 of 25 sessions`, complete, over a harvest that had thrown a third of the measured tokens
     * away. No other term on the wire can say so, which is why this one may not be silent.
     */
    const audited = withAudit({ usageShortfall: SHORTFALL });
    expect(subOf('tokens', audited))
      .toBe(`${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions · 13.3M of 36.8M tokens discarded`);
    // The figure itself is untouched: the clause reports what is missing, it does not adjust the sum.
    expect(readOf('tokens', audited)).toMatchObject({ value: LIVE_TOKENS });
    // The denominator is what the usage query SAW, never the sum above it — a total over the admitted
    // sample cannot account for the sessions it never saw.
    expect(subOf('tokens', audited)).not.toMatch(/of 6.8M/);
  });

  it('carries the same clause under the money, which is short by the same tokens', () => {
    // Cost is priced FROM those tokens, so a figure missing a third of them is short by whatever they
    // would have cost. Both shortfalls appear when both exist and neither reads as the other: one is
    // money this population could not price, the other is money that never entered it.
    const both: ImproveData = {
      ...NO_SCORECARD_BUNDLE,
      scorecard: scorecardWith({
        ...LIVE_SPEND,
        cost: costSum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS, LIVE_TOKENS, UNPRICED_TOKENS),
        usageShortfall: SHORTFALL,
      }),
    };
    expect(subOf('cost', both)).toBe('ESTIMATE, not billed truth · '
      + `${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions priced · `
      + '90.6k of 6.9M tokens unpriced · 13.3M of 36.8M tokens discarded');
  });

  it('carries it under every figure read off the same usage records, not only the two sums', () => {
    /*
     * `tokens` and `cost` were captioned and their three siblings were not, which made the silence
     * mean something it does not: `llmCalls`, `tokensPerSession` and `modelsUsed` come off the SAME
     * per-session usage records the row cap dropped sessions from, so all five are short by the same
     * sessions. A caption on two of them and silence on three teaches a reader that silence is a clean
     * bill — the exact misreading the clause was added to fix, arrived at from the other side.
     *
     * The three are asserted WHOLE rather than by substring, because what has to hold is that the
     * clause lands after the figure's own coverage rather than in place of it.
     */
    const audited = withAudit({ usageShortfall: SHORTFALL });
    expect(subOf('llmCalls', audited))
      .toBe(`49.0k tokens per call · ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions`
        + ' · 13.3M of 36.8M tokens discarded');
    expect(subOf('tokensPerSession', audited))
      .toBe(`over ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions`
        + ' · 13.3M of 36.8M tokens discarded');
    // Every one of the five, so a sibling added later cannot quietly join the silent half.
    for (const id of ['tokens', 'cost', 'llmCalls', 'tokensPerSession', 'modelsUsed']) {
      expect(subOf(id, audited), id).toMatch(/discarded/);
    }
  });

  it('tells a SET count what it may be missing, and never a magnitude it cannot be short by', () => {
    /*
     * `modelsUsed` is the size of a set, so it has no shortfall to add back: a discarded session
     * either used a model already counted — and the figure is exactly right — or one nobody counted,
     * and it is short by an unknown number. A token magnitude under it would be a number in the wrong
     * unit inviting the wrong repair, so this clause names the discarded SESSIONS and says `may`.
     */
    const audited = withAudit({ usageShortfall: SHORTFALL });
    expect(subOf('modelsUsed', audited))
      .toBe(`over ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions · `
        + `${DISCARDED_SESSIONS} of ${MEASURED_SESSIONS} sessions discarded, any of which may have `
        + 'used a model not counted here');
    // The one thing it must not say: the token figure the four sums carry.
    expect(subOf('modelsUsed', audited)).not.toMatch(/13\.3M|36\.8M/);
    // ...nor claim the count IS short, which nothing measured.
    expect(subOf('modelsUsed', audited)).not.toMatch(/\bis short\b/);
  });

  it('says the harvest did not audit itself rather than reading as a clean one', () => {
    /*
     * A sentinel is the producer stating that it never looked, which `?? 0` would turn into "nothing
     * was dropped" — the one reading this block was added to prevent, arrived at from the other side.
     * Silence is not available either: an uncaptioned sum reads as complete.
     */
    const unaudited = withAudit({ usageShortfall: NO_AUDIT });
    expect(subOf('tokens', unaudited))
      .toBe(`${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions · tokens discarded not recorded`);
    expect(subOf('cost', unaudited)).toMatch(/tokens discarded not recorded$/);
    expect(subOf('llmCalls', unaudited)).toMatch(/tokens discarded not recorded$/);
    expect(subOf('tokensPerSession', unaudited)).toMatch(/tokens discarded not recorded$/);
    // The set count says it in ITS unit, and says what the consequence would be rather than a
    // magnitude: an unaudited harvest cannot tell it whether a model went uncounted.
    expect(subOf('modelsUsed', unaudited))
      .toMatch(/sessions discarded not recorded, so this count may be short$/);
  });

  it('stays silent on a measured zero, and on a bundle written before the block', () => {
    /*
     * Two silences, and both are required. A measured `0` is the cap not binding — a
     * `0 tokens discarded` clause under every figure would be noise on every well-windowed harvest
     * and would teach a reader to skip the line that matters. An ABSENT block is the deploy-order
     * case: the wire never sent it, which is `shortfall`'s own rule beside it, and is what the live
     * item is in today.
     */
    const clean = withAudit({ usageShortfall: { ...SHORTFALL, tokensDiscarded: 0 } });
    expect(subOf('tokens', clean)).toBe(`${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions`);
    expect(subOf('tokens', livePriced)).toBe(`${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions`);
    expect(livePriced.scorecard?.spend.usageShortfall).toBeUndefined();
    // Both silences hold on every figure that gained the clause, or five captions become noise on
    // every well-windowed harvest instead of two.
    const quietModels = withAudit({ usageShortfall: { ...SHORTFALL, sessionsDiscarded: 0 } });
    for (const id of ['llmCalls', 'tokensPerSession']) {
      expect(subOf(id, clean), id).not.toMatch(/discarded/);
      expect(subOf(id, livePriced), id).not.toMatch(/discarded/);
    }
    expect(subOf('modelsUsed', quietModels)).not.toMatch(/discarded/);
    expect(subOf('modelsUsed', livePriced)).not.toMatch(/discarded/);
  });

  it('will not state a magnitude whose denominator the harvest did not record', () => {
    // Half a pair is a number with nothing to read it against, which is this file's third rule. It
    // degrades to the words rather than to a bare `13.3M discarded`.
    const half = withAudit({
      usageShortfall: { ...SHORTFALL, tokensMeasured: 'UNMEASURED' },
    });
    expect(subOf('tokens', half)).toMatch(/tokens discarded not recorded$/);
    expect(subOf('tokens', half)).not.toMatch(/13\.3M/);
  });

  it('states the sample the shared figures are drawn from, in sessions', () => {
    /*
     * Every tile at this scope carries a coverage fraction over the population the producer ADMITTED,
     * and all three bounds that narrow a harvest to that population are invisible on a tile: the span
     * floor, the population cap, and the harvest's own row cap. So `25 of 25 sessions` is true and
     * complete about a quarter of a day, and this line is where that stops being unsayable.
     *
     * One unit for all three, so the line is a single statement about how much of the harness these
     * numbers cover rather than three facts in two units. `more` because the floor's sessions are
     * outside `discoverable` rather than inside it.
     */
    expect(describeBasis(withAudit({ sessions: DISCOVERED, usageShortfall: SHORTFALL }), EVERYONE))
      .toBe('25 of 36 sessions scored · 3 more too short to judge · '
        + `spend dropped from ${DISCARDED_SESSIONS} of ${MEASURED_SESSIONS} sessions`);
    // `admitted` is what `population.total` counts, so the first fraction is the scorecard's own
    // denominator against the harvest it was drawn from.
    expect(DISCOVERED.discoverable + DISCOVERED.belowMinSpans).toBe(39);
  });

  it('states each bound alone, since a bundle can carry either block without the other', () => {
    expect(describeBasis(withAudit({ sessions: DISCOVERED }), EVERYONE))
      .toBe('25 of 36 sessions scored · 3 more too short to judge');
    expect(describeBasis(withAudit({ usageShortfall: SHORTFALL }), EVERYONE))
      .toBe(`spend dropped from ${DISCARDED_SESSIONS} of ${MEASURED_SESSIONS} sessions`);
  });

  it('says nothing about a bound that did not bind, or that nobody measured', () => {
    /*
     * A measured `0` is the floor or the cap not binding, and a clause for it under every reading is
     * the noise that teaches a reader to skip the line on the day a tenth of the harvest went unjudged.
     * A sentinel discard count is a harvest that did not audit itself, which the token footnote already
     * states against the figure it qualifies — saying it twice would be one fact in two places.
     */
    const whole = { discoverable: 36, admitted: 36, belowMinSpans: 0 };
    expect(describeBasis(withAudit({ sessions: whole, usageShortfall: NO_AUDIT }), EVERYONE))
      .toBe('36 of 36 sessions scored');
    // And a population with no discovery step at all — the labelled benchmark, whose membership is
    // fixed — states nothing rather than zeros, which would claim a step that never ran.
    expect(describeBasis(withAudit({ usageShortfall: { ...SHORTFALL, sessionsDiscarded: 0 } }),
                         EVERYONE)).toBeNull();
  });
});

// --- which model the money went to --------------------------------------------
/**
 * The split `modelsSeen` flattens away.
 *
 * `2 models were used` is the whole of what the screen could say about a population where one of the
 * two carries 97% of the tokens — so the count was true and the actionable fact behind it, which step
 * to move onto a cheaper model, was unreadable. These pin the split that says it, and the two rules it
 * cannot break: a share is over the ROSTER'S OWN sum, and if any part of that sum is unmeasured then no
 * share is drawn at all.
 */
/** A share as the registry rounds it. Computed here rather than transcribed, so a test cannot pass by
 *  agreeing with a percentage that was typed in wrong. */
const share = (part: number, whole: number) => `${Math.round((part / whole) * 100)}%`;

describe('the per-model split', () => {
  it('names the biggest spender and its share in the footnote', () => {
    const reading = readOf('modelsUsed', roster) as ScalarReading;
    expect(reading.available).toBe(true);
    // The count is still the figure. What changed is that the footnote says which model it is about.
    expect(reading.value).toBe(2);
    expect(reading.sub).toBe(`${ROSTER_OPUS} is ${share(ROSTER_OPUS_TOKENS, ROSTER_TOKENS)} of `
      + `${compactNumber(ROSTER_TOKENS)} tokens · over ${ROSTER_RUNS} of ${ROSTER_RUNS} sessions`);
    // 97%, measured. A renderer dividing by `modelsSeen`, or by the population, gets neither.
    expect(reading.sub).toContain('97%');
  });

  it('carries the whole roster on the channel that has room for it', () => {
    const reading = readOf('modelsUsed', roster) as ScalarReading;
    expect(reading.caveat).toBe(
      'Tokens by model, biggest first. '
      + `${ROSTER_OPUS}: ${compactNumber(ROSTER_OPUS_TOKENS)} tokens `
      + `(${share(ROSTER_OPUS_TOKENS, ROSTER_TOKENS)}) in ${ROSTER_OPUS_RUNS} of ${ROSTER_RUNS} runs`
      + `, ${ROSTER_OPUS_CALLS} calls in ${ROSTER_OPUS_RUNS} of ${ROSTER_RUNS} runs; `
      + `${ROSTER_SONNET}: ${compactNumber(ROSTER_SONNET_TOKENS)} tokens `
      + `(${share(ROSTER_SONNET_TOKENS, ROSTER_TOKENS)}) `
      + `in ${ROSTER_SONNET_RUNS} of ${ROSTER_RUNS} runs`
      + `, ${ROSTER_SONNET_CALLS} calls in ${ROSTER_SONNET_RUNS} of ${ROSTER_RUNS} runs. `
      + 'A row is over the runs that recorded any usage, not over the whole population.',
    );
  });

  it('gives every per-model figure its own denominator', () => {
    // A row's 10.0M tokens are over the 14 runs the model appears in, not over the 25 the population
    // holds — so a magnitude with no fraction beside it would read as a platform-wide figure.
    const reading = readOf('modelsUsed', roster) as ScalarReading;
    expect(reading.caveat).toContain(`in ${ROSTER_OPUS_RUNS} of ${ROSTER_RUNS} runs`);
    expect(reading.caveat).toContain(`in ${ROSTER_SONNET_RUNS} of ${ROSTER_RUNS} runs`);
    // `runs` and not `sessions`: the sibling fraction on the same footnote is over a different
    // population, and one noun for both would invite a reader to line them up.
    expect(reading.sub).toContain(`${ROSTER_RUNS} of ${ROSTER_RUNS} sessions`);
  });

  it('takes the share over the roster’s own sum and never over the token total', () => {
    /*
     * The two agree on every real bundle and are separate folds, so they CAN diverge. Here the token
     * total is doubled and the rows are left alone: a share taken against it would report the biggest
     * model at 48%, which understates its share of the roster by half and reads as reassuring.
     */
    const reading = readOf('modelsUsed',
      rosterWith({ tokens: sum(ROSTER_TOKENS * 2, ROSTER_RUNS, ROSTER_RUNS) })) as ScalarReading;
    expect(reading.sub).toContain(share(ROSTER_OPUS_TOKENS, ROSTER_TOKENS));
    expect(reading.sub).not.toContain(share(ROSTER_OPUS_TOKENS, ROSTER_TOKENS * 2));
  });

  it('is silent about a remainder that stands for no model', () => {
    // `models: 0` is the measured "the bound withheld nothing", which is the live reading. Naming it
    // would put a clause under every figure and teach a reader to skip the one that matters.
    const reading = readOf('modelsUsed', roster) as ScalarReading;
    expect(reading.sub).not.toContain('folded');
    expect(reading.caveat).not.toContain('folded');
    // And the sentinel sitting beside that zero must not be read as a fold whose sum was lost.
    expect(reading.sub).not.toContain('not measured');
  });
});

describe('a roster whose bound withheld models', () => {
  it('names the fold in the footnote, not only in the roster', () => {
    /*
     * The footnote is the clause always on screen; the roster rides a tooltip and a picker row. A
     * reader who never hovers must still see that the named models are not all of them, or the biggest
     * spender's share reads as a share of everything.
     */
    const reading = readOf('modelsUsed', folded) as ScalarReading;
    expect(reading.sub).toContain(`${ROSTER_WITHHELD_MODELS} more models folded in`);
  });

  it('gives the remainder its own row, with its magnitude and its denominator', () => {
    const reading = readOf('modelsUsed', folded) as ScalarReading;
    expect(reading.caveat).toContain(`${ROSTER_WITHHELD_MODELS} further models folded together: `
      + `${compactNumber(ROSTER_WITHHELD_TOKENS)} tokens`);
    expect(reading.caveat).toContain('in 5 of 25 runs');
  });

  it('folds the withheld tokens into the denominator every share is over', () => {
    // Left out, the named rows would sum to less than the roster and each would be reported as a
    // larger share of the spend than it is.
    const whole = ROSTER_TOKENS + ROSTER_WITHHELD_TOKENS;
    const reading = readOf('modelsUsed', folded) as ScalarReading;
    expect(reading.sub).toContain(`of ${compactNumber(whole)} tokens`);
    expect(reading.sub).toContain(share(ROSTER_OPUS_TOKENS, whole));
    expect(share(ROSTER_OPUS_TOKENS, whole)).not.toBe(share(ROSTER_OPUS_TOKENS, ROSTER_TOKENS));
  });
});

describe('a roster missing part of its own total', () => {
  it('draws no share at all rather than one over the readable part', () => {
    /*
     * ALL-OR-NOTHING, the rule `coverageOf` applies to a coverage fraction. With one model's tokens
     * unmeasured the roster's sum is a lower bound, so every percentage over it OVERSTATES by a
     * different unknown amount — and a share wrong in the reassuring direction is worse than none.
     */
    const reading = readOf('modelsUsed', rosterShort) as ScalarReading;
    expect(reading.sub).toContain("no model's share can be drawn");
    expect(reading.sub).not.toContain('%');
    expect(reading.caveat).not.toContain('%');
    expect(reading.caveat).toContain('denominator short by an unknown amount');
  });

  it('shows the sentinel as words and never as a zero, keeping its denominator', () => {
    const reading = readOf('modelsUsed', rosterShort) as ScalarReading;
    // `0 tokens` would read as a model that ran and spent nothing.
    expect(reading.caveat).toContain(`${ROSTER_SONNET}: tokens not measured in 0 of 25 runs`);
    expect(reading.caveat).not.toContain(`${ROSTER_SONNET}: 0 tokens`);
    // The term the producer DID measure on that row is still stated.
    expect(reading.caveat).toContain(`${ROSTER_SONNET_CALLS} calls`);
  });

  it('still names the models, because which they are is the fact that survives', () => {
    const reading = readOf('modelsUsed', rosterShort) as ScalarReading;
    expect(reading.caveat).toContain(ROSTER_OPUS);
    expect(reading.caveat).toContain(ROSTER_SONNET);
  });

  it('withholds every share when the FOLD is the unmeasured part', () => {
    // The remainder stands for three models and cannot say what they spent, so the roster's sum is
    // short by an unknown amount even though every named row is measured.
    const reading = readOf('modelsUsed', rosterWith({
      byModelOther: { models: 3, tokens: sum('UNMEASURED', 0, ROSTER_RUNS) },
    })) as ScalarReading;
    expect(reading.sub).toContain("no model's share can be drawn");
    expect(reading.sub).toContain('3 more models folded in');
    expect(reading.caveat).toContain('3 further models folded together: tokens not measured');
  });
});

describe('a bundle that names no model at all', () => {
  it('keeps the footnote it has always had when the key is absent', () => {
    // Deploy order: the SPA ships separately from the producer, so an item stored before `byModel`
    // existed is served back unchanged. Absent is not an empty roster, and a clause claiming the
    // producer named no model would assert a measurement nobody made.
    const reading = readOf('modelsUsed',
      rosterWith({ byModel: undefined, byModelOther: undefined })) as ScalarReading;
    expect(reading.sub).toBe(`over ${ROSTER_RUNS} of ${ROSTER_RUNS} sessions`);
    expect(reading.caveat).toBeUndefined();
  });

  it('answers the same way for an empty roster, because there is no split to draw', () => {
    // `[]` IS a measurement — the producer looked and no session named a model — and what it measures
    // is already the figure on the tile. `modelsSeen: 0` beside it is the statement; a roster with no
    // rows in it has no biggest spender and no denominator.
    const reading = readOf('modelsUsed',
      rosterWith({ byModel: [], modelsSeen: 0 })) as ScalarReading;
    expect(reading.value).toBe(0);
    expect(reading.sub).toBe(`over ${ROSTER_RUNS} of ${ROSTER_RUNS} sessions`);
    expect(reading.caveat).toBeUndefined();
  });

  it('offers no split on the reader’s own days, which carry no per-model magnitude', () => {
    /*
     * The personal figure is summed off day rows whose `models` is a list of ids and nothing else, so
     * there is no share to take. Asked at the personal scope this reads as a count and stops, on the
     * very bundle whose shared half has a full roster.
     */
    const reading = readOf('modelsUsed', roster, YOURS);
    if (reading.available) expect(reading.caveat).toBeUndefined();
    expect(readOf('modelsUsed', roster, YOURS)).not.toMatchObject({ caveat: expect.any(String) });
  });
});

describe('a roster that is not the shape the type promises', () => {
  it('drops a row it cannot place or caption rather than half-rendering it', () => {
    /*
     * A row with no id cannot be named and a row with no denominator cannot be captioned, so neither
     * can appear — and neither may quietly join the share denominator either, or the readable rows
     * would be reported as smaller fractions of a bigger whole than the wire states.
     */
    const reading = readOf('modelsUsed', rosterWith({
      byModel: [
        ROSTER_ROWS[0],
        { ...ROSTER_ROWS[1], model: '' },
        { ...ROSTER_ROWS[1], tokens: { sum: 999_999 } as MeasuredSum },
      ],
    })) as ScalarReading;
    expect(reading.caveat).toContain(ROSTER_OPUS);
    expect(reading.caveat).not.toContain(ROSTER_SONNET);
    // One readable row, so it is the whole of the roster and 100% of it.
    expect(reading.sub).toContain(`${ROSTER_OPUS} is 100% of `
      + `${compactNumber(ROSTER_OPUS_TOKENS)} tokens`);
  });

  it('keeps a fold whose tokens are unreadable, rather than hiding that it happened', () => {
    // Dropping it would leave the named rows reading as the whole roster, which is the one thing the
    // remainder exists to prevent.
    const reading = readOf('modelsUsed', rosterWith({
      byModelOther: { models: 4, tokens: 'later' as unknown as MeasuredSum },
    })) as ScalarReading;
    expect(reading.sub).toContain('4 more models folded in');
    expect(reading.sub).toContain("no model's share can be drawn");
  });

  it('does not throw on any of those shapes, at either scope', () => {
    const shapes: ImproveData[] = [
      roster,
      folded,
      rosterShort,
      rosterWith({ byModel: 'opus' as unknown as ModelSpend[] }),
      rosterWith({ byModel: [null as unknown as ModelSpend, 7 as unknown as ModelSpend] }),
      rosterWith({ byModelOther: { models: Number.NaN } as ModelSpendRemainder }),
      rosterWith({ byModelOther: -1 as unknown as ModelSpendRemainder }),
      rosterWith({ byModel: [{ model: ROSTER_OPUS } as ModelSpend] }),
      // Every row unmeasured, so the roster has no measured part at all.
      rosterWith({
        byModel: ROSTER_ROWS.map((row) => ({ ...row, tokens: sum('UNMEASURED', 0, ROSTER_RUNS) })),
      }),
      // A roster that adds to nothing: `0/0` is not `0%`, and a share drawn from it would assert a
      // split nothing measured.
      rosterWith({ byModel: ROSTER_ROWS.map((row) => ({ ...row, tokens: sum(0, 1, ROSTER_RUNS) })) }),
    ];
    for (const data of shapes) {
      for (const view of [EVERYONE, YOURS]) {
        for (const metric of METRICS) {
          expect(() => metric.read(data, view), metric.id).not.toThrow();
        }
      }
    }
  });

  it('draws no share over a roster that adds to nothing', () => {
    const reading = readOf('modelsUsed', rosterWith({
      byModel: ROSTER_ROWS.map((row) => ({ ...row, tokens: sum(0, 1, ROSTER_RUNS) })),
    })) as ScalarReading;
    // Every row measured `0`, so the roster is a measured zero — and a percentage of it is not `0%`,
    // it is undefined. The magnitudes stay, since `0 tokens` there IS the measurement.
    expect(reading.sub).toContain("no model's share can be drawn");
    expect(reading.caveat).toContain('0 tokens in 1 of 25 runs');
  });
});

// --- money is not a count -----------------------------------------------------
describe('the cost formatter', () => {
  it('spells a population total, a cheap session, a sub-cent price and zero', () => {
    expect(formatUsd(PRICED_TOTAL_USD)).toBe('$2.98');
    expect(formatUsd(PRICED_ONE_MODEL_USD)).toBe('$0.23');
    // A real cost that rounds to nothing is stated as a bound. `$0.00` on money that was spent
    // reads as free, which is the one thing a price must never say.
    expect(formatUsd(0.004)).toBe('<$0.01');
    expect(formatUsd(9e-7)).toBe('<$0.01');
    // Reserved for exactly zero, which a session that used a priced model and no tokens really is.
    expect(formatUsd(0)).toBe('$0.00');
  });

  it('rounds to the cent rather than reaching for the bound', () => {
    // The other side of that boundary: half a cent IS a cent to two places, and reporting
    // `<$0.01` for it would understate a figure the format can carry exactly.
    expect(formatUsd(0.005)).toBe('$0.01');
    expect(formatUsd(0.006)).toBe('$0.01');
    // Two places, always. A harvest total is tens of dollars and the cents are free to keep.
    expect(formatUsd(34.821)).toBe('$34.82');
    expect(formatUsd(12_345.678)).toBe('$12345.68');
  });

  it('refuses a figure that cannot be a price at all', () => {
    // The same argument `compactNumber` makes for `not a count`: a negative price is a producer
    // bug, and `$-51.70` launders it into something a reader will believe.
    expect(formatUsd(-0.01)).toBe('not a cost');
    expect(formatUsd(Number.NaN)).toBe('not a cost');
    expect(formatUsd(Number.POSITIVE_INFINITY)).toBe('not a cost');
  });
});

// --- no NaN, no Infinity ------------------------------------------------------
describe('divisions that have no denominator', () => {
  it('never produces a non-finite figure or share, on any of the bundles above', () => {
    for (const bundle of [
      NO_SCORECARD_BUNDLE, wireBundle, harvestBundle, pricedBundle, livePriced, partlyPriced,
      mineUnpriced, minePriced, mineWide, myDaysUnpriced, myDaysWide, myDaysNone,
    ]) {
      for (const view of [EVERYONE, YOURS, { scope: 'yours', period: 'trailing7' } as const]) {
        for (const metric of METRICS) {
          const reading = metric.read(bundle, view);
          if (!reading.available) continue;
          for (const field of [reading.value, (reading as { ratio?: unknown }).ratio]) {
            if (typeof field === 'number') expect(Number.isFinite(field), metric.id).toBe(true);
          }
        }
      }
    }
  });

  it('refuses a per-session figure when the sum exists but the divisor is zero', () => {
    // Arithmetically inconsistent, and only a wire can produce it — but the guard has to sit on
    // the divisor, not on the sum, or this divides by 0 and reports Infinity.
    const reading = readOf('tokensPerSession',
      withSpend({ tokens: sum(HARVEST_TOKENS, 0, HARVEST_SESSIONS) }));
    expect(reading.available).toBe(false);
    expect(reading).toMatchObject({ code: 'noDenominator' });
  });

  it('withholds the per-call figure when no call was made', () => {
    const reading = readOf('llmCalls', withSpend({
      llmCalls: sum(0, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    })) as ScalarReading;
    expect(reading.value).toBe(0);
    expect(reading.sub).not.toMatch(/per call/);
  });

  it('withholds the per-call figure when the two terms rest on different sessions', () => {
    // The producer reads each usage key independently, so one term measured on 350 sessions and
    // the other on 354 is reachable. Their quotient is not tokens per call.
    const reading = readOf('llmCalls', withSpend({
      llmCalls: sum(HARVEST_CALLS, HARVEST_SESSIONS_WITH_USAGE - 4, HARVEST_SESSIONS),
    })) as ScalarReading;
    expect(reading.sub).not.toMatch(/per call/);
    expect(reading.sub).toContain('not measured on the same sessions');
    expect(reading.sub).toContain(`350 of ${HARVEST_SESSIONS} sessions`);
  });

  it('refuses a share when every category measured zero', () => {
    // A measurement, not an absence — and 0/0 is still not 0%, so no bar may be drawn from it.
    const zero = sum(0, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS);
    const reading = readOf('cacheReuse', withSpend({
      byCategory: { input: zero, output: zero, cacheRead: zero, cacheWrite: zero },
    }));
    expect(reading.available).toBe(false);
    expect(reading).toMatchObject({ code: 'noDenominator' });
  });
});

// --- every footnote carries its denominator -----------------------------------
describe('footnotes', () => {
  it('states what every available reading rests on, at either scope', () => {
    /*
     * A sum over part of a population, captioned bare, understates it while looking authoritative.
     * `553` alone would not say 553 of what.
     *
     * The two scopes say it in different units, and that is not an inconsistency — it is what the two
     * wires can support. A scorecard term carries `measuredOf`/`total`, so the shared footnote is a
     * fraction of SESSIONS. A day row carries a sum and a run count and NOT how many of that day's
     * runs recorded the term, so a personal footnote states the runs the total is drawn from and,
     * separately, the days that measured it — a fraction of runs there would be a completeness nobody
     * counted, which is the exact failure the shared denominators exist to expose.
     */
    for (const bundle of [
      wireBundle, harvestBundle, pricedBundle, livePriced, partlyPriced,
      withActor([actorRun(10, 2)], 4), mineUnpriced, minePriced, mineWide,
      myDaysUnpriced, myDaysWide, myDaysNone,
    ]) {
      for (const view of [EVERYONE, YOURS]) {
        for (const metric of METRICS) {
          const reading = metric.read(bundle, view);
          if (!reading.available) continue;
          expect(reading.sub, `${metric.id} ${view.scope}`).toMatch(
            view.scope === 'everyone'
              ? /\d+ of \d+ (sessions|runs)|nothing to measure|not a count/
              : /across \d+ runs?|run count not recorded|no runs in this period/);
        }
      }
    }
  });

  /**
   * Every bundle shape in this file that a caveat could come off, plus the two that must NOT produce
   * one: a basis the wire never carried, and one short enough for the footnote to hold whole.
   *
   * Local rather than shared with the sweeps above, which each admit a different set on purpose —
   * one adds deliberately corrupt rows, another a bare actor bundle — and folding them together
   * would quietly change what those are asserting.
   */
  const CAVEAT_BUNDLES: readonly ImproveData[] = [
    NO_SCORECARD_BUNDLE, wireBundle, harvestBundle, pricedBundle, livePriced, partlyPriced,
    mineUnpriced, minePriced, mineWide, myDaysUnpriced, myDaysWide, myDaysNone,
    withSpend({ costBasis: '' }), withSpend({ costBasis: 'rates re-sourced 2026-12-01' }),
  ];

  it('never offers a caveat that is blank, or that only repeats the footnote', () => {
    /*
     * The two ways a caveat could be worse than none, swept over every metric and every bundle in
     * this file rather than spot-checked on the one metric that has a caveat today. A blank or
     * whitespace string reaches `MetricTile` as a `title` attribute with nothing behind it — a
     * tooltip-shaped affordance that answers nothing. A caveat equal to the footnote is the same
     * sentence twice, in a tooltip and a picker row, which teaches a reader that neither is worth
     * opening.
     */
    for (const bundle of CAVEAT_BUNDLES) {
      for (const view of [EVERYONE, YOURS]) {
        for (const metric of METRICS) {
          const reading: Reading = metric.read(bundle, view);
          if (!reading.available || reading.caveat === undefined) continue;
          expect(reading.caveat.trim(), metric.id).not.toBe('');
          expect(reading.caveat, metric.id).not.toBe(reading.sub);
        }
      }
    }
  });

  it('carries a caveat on exactly the figures whose footnote had to abridge one', () => {
    /*
     * The inventory, so that adding or losing one is a deliberate edit rather than a silent change.
     * `cost` alone, at either scope, and the whole point of the field: the basis is 839 characters
     * against a footnote's 64.
     *
     * The personal reading carries it only when it has NO figure, which is the one line of this rule
     * that moved. It used to be refused outright: `costBasis` rides the shared bundle while the
     * reader's days ride their own, and the two refresh independently, so attaching a rate table's
     * date to a priced personal total could describe a different pricing run than the one that
     * produced it. That argument is about provenance FOR a figure — with no figure there is nothing to
     * mis-attribute, and the basis is the only thing on the wire that explains the absence.
     */
    const withCaveat = new Set<string>();
    for (const bundle of CAVEAT_BUNDLES) {
      for (const view of [EVERYONE, YOURS]) {
        for (const metric of METRICS) {
          const reading: Reading = metric.read(bundle, view);
          if (reading.available && reading.caveat !== undefined) withCaveat.add(metric.id);
        }
      }
    }
    expect([...withCaveat]).toEqual(['cost']);
  });

  it('names the tokens the reader’s own cumulative bill leaves out', () => {
    /*
     * The clause a personal cost could not state at all until the counts landed on the day row, and the
     * reason it matters MORE on a cumulative figure than on a single window: a total that quietly
     * accumulates an unstated shortfall for a month is a worse under-report than one window's, not a
     * smaller one.
     *
     * Both halves sum, so the arithmetic is over the days in the period — and pricing skips a model with
     * no rate row rather than failing the session, so the run extent reads exactly as complete on a
     * short bill as on a whole one. Nothing else on the tile can tell them apart.
     */
    const short = withDays([
      wideDay(0, pricingCounts(0, UNPRICED_TOKENS)),
      wideDay(1),
    ]);
    const reading = readOf('cost', short, YOURS) as ScalarReading;
    expect(reading.value).toBe(PRICED_TOTAL_USD);
    expect(reading.sub)
      .toBe(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions · `
        + '90.6k of 6.6M tokens unpriced');
    // Spelled exactly as the population footnote spells it — one rule, whoever's tokens went unpriced.
    const population = readOf('cost', partlyPriced) as ScalarReading;
    expect(population.sub).toContain('90.6k of');
  });

  it('stays silent on a measured zero, and silent again on rows that predate the counts', () => {
    /*
     * Two different silences, and both are required. Every model these days spent on is priced, so both
     * counts are a measured `0` — and a `0 tokens unpriced` clause under every figure would be noise on
     * the common case and would teach a reader to skip the one line that matters on the rare one.
     *
     * A row from before the counts existed carries neither, and that is not a complete bill. Asserted on
     * the same metric and the same period, so the two cannot come to share a branch.
     *
     * What remains in both is the extent and the coverage, which are NOT conditional: the shortfall is
     * the only clause here that may be absent, so its absence is what these two assertions can see.
     */
    const whole = `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`;
    expect((readOf('cost', myDaysWide, YOURS) as ScalarReading).sub).toBe(whole);
    const older = withDays([
      wideDay(0, { costTokensPriced: undefined, costTokensUnpriced: undefined }),
      wideDay(1, { costTokensPriced: undefined, costTokensUnpriced: undefined }),
    ]);
    expect((readOf('cost', older, YOURS) as ScalarReading).sub).toBe(whole);
  });

  it('will not claim a complete bill from the counts alone when nothing was priced', () => {
    /*
     * The producer's own semantic, and the trap in it. The counts are DENOMINATORS, so a session that
     * recorded no usage contributes `0` to both halves — meaning a fully unmeasured day carries
     * `cost: "UNMEASURED"` beside `0` and `0`. Read on its own that pair says "nothing was skipped",
     * which under a figure that is not a figure would be a completeness claim about nothing at all.
     *
     * So the clause rides a NUMBER only. What this reading gets instead is the producer's account of why
     * nothing is priced, which is the fact that actually explains the absence.
     */
    const unmeasured = withDays([
      mineDay(0, { cost: 'UNMEASURED', costTokensPriced: 0, costTokensUnpriced: 0 }),
    ]);
    const reading = readOf('cost', unmeasured, YOURS) as ScalarReading;
    expect(reading.value).toBe('UNMEASURED');
    expect(reading.sub).not.toMatch(/unpriced/);
    expect(reading.sub).toMatch(/cost not priced/);
  });

  it('carries the shortfall over the rolled-up days too, so a total cannot outgrow the clause', () => {
    // Both halves sum, so the evicted bucket carries them — which is the whole reason the clause works
    // at every period. A bucket left out would understate the missing money by however much it held.
    const bucket = {
      fromDate: '2026-08-04', toDate: '2026-08-12', days: 9, runs: 12,
      cost: 4.5, tokens: 3_120_004, costTokensPriced: 3_000_000, costTokensUnpriced: 120_004,
    };
    const evicted = withDays([wideDay(0), wideDay(1)], {
      // The bucket's coverage counts are the evicted days' own, summed — twelve requests over nine days,
      // none of which fanned out, so twelve sessions and every one of them priced.
      rolledUp: { ...bucket, coverage: dayCoverage(bucket, 12) },
      historyFirstDate: '2026-08-04',
    });
    const reading = readOf('cost', evicted, YOURS) as ScalarReading;
    expect(reading.sub).toBe('across 14 runs · 14 of 14 sessions · 120.0k of 9.7M tokens unpriced');
    // A trailing window cannot contain the bucket, so the clause there is the retained days' alone —
    // silent, because those are priced in full.
    expect((readOf('cost', evicted, { scope: 'yours', period: 'trailing7' }) as ScalarReading).sub)
      .toBe(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`);
  });

  it('gives a priced personal total no borrowed provenance, and an unpriced one the reason', () => {
    // Priced: the figure is the reader's own and the shared basis may describe a later pricing run,
    // so nothing about the rates is claimed under it.
    const priced = readOf('cost', myDaysWide, YOURS) as ScalarReading;
    expect(priced.value).toBe(PRICED_TOTAL_USD);
    expect(priced.sub).not.toMatch(/ESTIMATE/);
    expect(priced.caveat).toBeUndefined();
    // Unpriced: there is no figure to mis-attribute and the basis is the only sentence that says why.
    const unpriced = readOf('cost', myDaysUnpriced, YOURS);
    expect(unpriced).toMatchObject({ available: false, code: 'notInBundle' });
  });

  it('drops the unit rather than gluing it onto a diagnostic sentence', () => {
    // `fractionLabel` answers with a sentence when the pair is not a fraction, and
    // `nothing to measure sessions` is not a caption.
    const reading = readOf('tokens', withSpend({ tokens: sum('UNMEASURED', 0, 0) }));
    expect(reading).toEqual({ available: true, value: 'UNMEASURED', sub: 'nothing to measure' });
    /*
     * The personal footnote degrades the same way, through the same helper, because it now states the
     * same kind of fraction. A day whose block counts no session at all is `0 of 0`, which invites a
     * reader to compute 0% — so it says there was nothing to measure instead, and never `0 of 0
     * sessions`. One spelling for both scopes is the point: a second copy of the check is how one of
     * them would come to print the pair.
     */
    const empty = withDays([dayRow(MINE_DATES[0], {
      runs: 1,
      tokens: 100,
      coverage: dayCoverage({ tokens: 100 }, 0),
    })]);
    expect((readOf('tokens', empty, YOURS) as ScalarReading).sub)
      .toBe('across 1 run · nothing to measure');
  });

  it('captions a split by its worst-covered term, in both directions', () => {
    /*
     * The four categories come off one usage record per session and the producer hands them one
     * shared `total`, so today they always agree. Should one ever lag, both halves of the caption
     * move the same conservative way — the SMALLEST count of sessions that contributed and the
     * LARGEST population any term names — so the footnote can only understate coverage, never
     * overstate it. Overstating is the failure it exists to prevent.
     */
    const lagging = HARVEST_SESSIONS_WITH_USAGE - 20;
    const data = withSpend({
      byCategory: {
        ...HARVEST_SPEND.byCategory,
        cacheWrite: sum(HARVEST_CACHE_WRITE, lagging, HARVEST_SESSIONS - 30),
      },
    });
    expect(readOf('contextVsGenerated', data))
      .toMatchObject({ sub: `${lagging} of ${HARVEST_SESSIONS} sessions` });
  });

  it('degrades visibly when a denominator is not a number at all', () => {
    const reading = readOf('tokens',
      withSpend({ tokens: sum(HARVEST_TOKENS, HARVEST_SESSIONS_WITH_USAGE, Number.NaN) }));
    // Passed through from `fractionLabel` rather than hand-spelled here, so a corrupt total reads
    // as a producer bug instead of as `354 of NaN`.
    expect(reading).toMatchObject({ sub: 'not a count' });
  });
});

// --- the actor's own days, which accumulate ----------------------------------
describe('your own cumulative figures', () => {
  it('are unavailable, and offer no number, when you have no bundle at all', () => {
    for (const id of IDS) {
      const reading = readOf(id, harvestBundle, YOURS);
      expect(reading.available, id).toBe(false);
      expect(reading, id).toMatchObject({ code: 'noActorBundle' });
      if (reading.available) continue;
      // The sentence has to refuse a zero out loud, because a zero is exactly what the other
      // absence produces and the two used to look identical.
      expect(reading.reason).toMatch(/not even as zero/);
      expect(reading).not.toHaveProperty('value');
    }
  });

  it('are unavailable, not zero, on a bundle that carries runs and no day rows', () => {
    /*
     * The live shape, not a defensive one. The day rows are newer than the bundles and an item is
     * rewritten only on its actor's next producer pass, so a bundle with run rows and no history is
     * served for as long as that pass has not happened — and it is present enough to tempt a reading
     * of "no history" as "no activity".
     */
    for (const id of IDS) {
      expect(readOf(id, minePriced, YOURS), id)
        .toMatchObject({ available: false, code: 'noActorHistory' });
    }
    // The run rows are still there, and still the evidence half. Nothing above read them.
    expect(minePriced.actor?.runs).toHaveLength(MINE_RUNS);
  });

  it('sum the day rows rather than one harvest window', () => {
    /*
     * The defect this whole change is about. These figures used to come off `actor.runs`, which is one
     * harvest window — so on a day the reader had no session all three default tiles reported the same
     * absence and the pane was one sentence. Summed over days, a reader who has ever run anything has
     * a number.
     *
     * Every one of the three reads the same two days, so a `pick` wired to the wrong key shows up here
     * as one figure wearing another's magnitude rather than as a plausible number.
     */
    const whole = `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`;
    expect(readOf('tokens', myDaysUnpriced, YOURS))
      .toEqual({ available: true, value: 6_573_271, sub: whole });
    expect(readOf('llmCalls', myDaysUnpriced, YOURS))
      .toEqual({ available: true, value: 127, sub: whole });
    expect(readOf('totalTime', myDaysUnpriced, YOURS))
      .toEqual({ available: true, value: 9_104_430, sub: whole });
    // The transcribed per-day figures really do add to those three totals.
    expect(total(MINE_TOKENS)).toBe(6_573_271);
    expect(total(MINE_CALLS)).toBe(127);
    expect(total(MINE_LATENCY_MS)).toBe(9_104_430);
  });

  it('states the runs a total is drawn from and the sessions that recorded it', () => {
    /*
     * Both counts, because they answer different questions. `across 14 runs` says how much of the
     * reader's own activity the figure covers, and the fraction after it says how much of that was
     * MEASURED — the day rows now carry a per-term count of the sessions that recorded each term, so
     * the caption states a coverage instead of the day-spanned proxy it could support before.
     *
     * These two days attributed every session they held, so the two counts agree; the test below is the
     * shape where they do not.
     */
    const busy = withDays([
      dayRow(MINE_DATES[0], { runs: 5, tokens: 100 }),
      dayRow(MINE_DATES[1], { runs: 9, tokens: 250 }),
    ]);
    expect(readOf('tokens', busy, YOURS))
      .toEqual({ available: true, value: 350, sub: 'across 14 runs · 14 of 14 sessions' });
    // The run count is pluralised, so a one-run reader is not told `1 runs`. The fraction is not: it is
    // a pair of figures rather than a count with a noun after it.
    const one = withDays([dayRow(MINE_DATES[0], { runs: 1, tokens: 7 })]);
    expect(readOf('tokens', one, YOURS)).toMatchObject({ sub: 'across 1 run · 1 of 1 sessions' });
  });

  it('counts sessions in the fraction and requests in the extent, which really do differ', () => {
    /*
     * A day row's `runs` counts REQUESTS and its `coverage.total` counts SESSIONS, and a request that
     * fanned out into plan sub-sessions is one run and several sessions — the producer sums a day's
     * spend over every contributing session and counts the same set here, so a real day reads
     * `runs: 17, coverage.total: 19`.
     *
     * So the two numbers in a footnote are not one fact stated twice, and reading the coverage off `runs`
     * would caption a figure measured on 19 sessions as measured on 17 of them.
     */
    const fanned = withDays([dayRow(MINE_DATES[0], {
      runs: 17,
      tokens: 100,
      coverage: dayCoverage({ tokens: 100 }, 19),
    })]);
    expect(readOf('tokens', fanned, YOURS))
      .toEqual({ available: true, value: 100, sub: 'across 17 runs · 19 of 19 sessions' });
  });

  it('says so rather than guessing when no day recorded how many runs it held', () => {
    const unsaid = withDays([dayRow(MINE_DATES[0], { runs: 'UNMEASURED', tokens: 100 })]);
    // A `?? 0` here would caption a real total `across 0 runs`, which reads as a figure from nothing.
    // The coverage is unaffected: the sessions were counted whether or not the requests were.
    expect(readOf('tokens', unsaid, YOURS))
      .toEqual({ available: true, value: 100, sub: 'run count not recorded · 1 of 1 sessions' });
  });

  it('states no extent at all when one day in the fold will not say how many runs it held', () => {
    /*
     * ALL-OR-NOTHING, the rule the coverage fraction beside it has always followed. An extent summed
     * over the days that happened to carry a run count, beside a figure summed over all of them, is a
     * partial numerator with nothing to declare it partial: `across 1 run` under both days' tokens
     * reads as the whole of this reader's activity and understates it by half. The fraction is
     * unaffected, because both days counted their sessions.
     */
    const mixed = withDays([mineDay(0), mineDay(1, { runs: 'UNMEASURED' })]);
    expect(readOf('tokens', mixed, YOURS)).toEqual({
      available: true,
      value: total(MINE_TOKENS),
      sub: `run count not recorded · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
    // The extent it must not state is the one drawn from the day that did say.
    expect((readOf('tokens', mixed, YOURS) as ScalarReading).sub).not.toMatch(/across 1 run/);
  });

  it('never captions real spend `across 0 runs`, which a day row can genuinely report', () => {
    /*
     * A day row counts REQUESTS, and a request that fanned out into plan sub-sessions can fall
     * outside the producer's scoring cap while those sub-sessions' spend still lands on the row — so
     * `runs: 0` beside real tokens is a shape the wire produces (measured: 1 of 11 sub-sessions at the
     * deployed cap). `across 0 runs` under a seven-figure total reads as a defect in the figure; what
     * is true is that the spend is there and the requests behind it were not all attributed.
     */
    const unattributed = withDays([dayRow(MINE_DATES[0], { runs: 0, tokens: MINE_TOKENS[0] })]);
    expect(readOf('tokens', unattributed, YOURS))
      .toEqual({ available: true,
                 value: MINE_TOKENS[0],
                 sub: 'runs not attributed · 1 of 1 sessions' });
    // And it is NOT the zero state: the day is there, so the figure is a figure rather than a `0`.
    expect(readOf('tokens', unattributed, YOURS)).not.toMatchObject({ value: 0 });
  });

  it('replaces a day recorded twice instead of adding it to the total', () => {
    /*
     * Idempotence over a wide reseed, which is the property that makes re-running the producer safe:
     * a 168-hour manual pass covers many dates and rewrites each one's row, so the same day mined
     * twice must move no figure. The producer's storage rule is that the current date REPLACES any
     * prior row for that day, and the fold applies the same rule — LAST occurrence wins — so a wire
     * that carried both would still total one day once.
     */
    const twice = withDays([
      dayRow(MINE_DATES[0], { runs: 1, tokens: 100 }),
      dayRow(MINE_DATES[0], { runs: 2, tokens: 250 }),
    ]);
    expect(readOf('tokens', twice, YOURS))
      .toEqual({ available: true, value: 250, sub: 'across 2 runs · 2 of 2 sessions' });
    // Summing both would be 350 over 3 runs — a total that grew because the producer ran again.
    expect(readOf('tokens', twice, YOURS)).not.toMatchObject({ value: 350 });
  });

  it('reads the rows in date order whatever order the wire sent them', () => {
    // The contract says oldest first; the fold sorts anyway, because a total and a trailing window
    // both depend on order and removing the assumption costs nothing.
    const backwards = withDays([mineDay(1), mineDay(0)]);
    expect(readOf('tokens', backwards, YOURS)).toMatchObject({ value: total(MINE_TOKENS) });
    expect(describeBasis(backwards, YOURS)).toBe(`since ${formatIsoDay(MINE_DATES[0])}`);
  });

  it('skips a day that measured nothing, and says how many sessions did', () => {
    const partial = withDays([mineDay(0), mineDay(1, { tokens: 'UNMEASURED' })]);
    expect(readOf('tokens', partial, YOURS)).toEqual({
      available: true,
      value: MINE_TOKENS[0],
      // The extent alone would read as complete over a total missing a whole day's tokens, and so would
      // silence. The fraction is the only thing here that says half of it is missing.
      sub: `across ${MINE_RUNS} runs · 1 of ${MINE_RUNS} sessions`,
    });
  });

  it('states a fraction over the sessions a term was measured on, never over the days', () => {
    /*
     * The distinction the whole caption rests on. A day-spanned fraction says how many of the days a
     * total covers measured the term, and says NOTHING about how much of the work inside those days was
     * recorded — measured on the harvest behind these figures, tokens are recorded on 354 of 553
     * sessions, so a figure captioned by its days can read as complete over two thirds of a population.
     *
     * This day spans one day and holds five sessions, two of which recorded tokens. Every day-spanned
     * caption for it says the whole of it was measured; only the session fraction says it was not.
     */
    const inside = withDays([dayRow(MINE_DATES[0], {
      runs: 5,
      tokens: 100,
      coverage: dayCoverage({ tokens: 100 }, 5),
    })]);
    const partly = withDays([dayRow(MINE_DATES[0], {
      runs: 5,
      tokens: 100,
      coverage: { ...dayCoverage({ tokens: 100 }, 5), tokens: 2 },
    })]);
    expect(readOf('tokens', inside, YOURS)).toMatchObject({ sub: 'across 5 runs · 5 of 5 sessions' });
    expect(readOf('tokens', partly, YOURS)).toMatchObject({ sub: 'across 5 runs · 2 of 5 sessions' });
    // Both figures are the same number over the same single day, so nothing but the fraction can tell a
    // reader that one of them is a fifth of the story.
    for (const data of [inside, partly]) {
      expect(readOf('tokens', data, YOURS)).toMatchObject({ value: 100 });
    }
  });

  it('states no fraction at all for a day written before the coverage counts existed', () => {
    /*
     * The ordinary case, not an edge one, and it stays ordinary for a while: the counts are newer than the
     * rows, a row is written once and carried forward untouched, and nothing back-fills them. So a total
     * that folds in one such row cannot say how much of itself was measured.
     *
     * Three things it must not do, and each would be a completeness claim. It must not scale the fraction
     * to the rows that DO carry the block — that puts a partial numerator over a partial denominator. It
     * must not print a zero, which asserts that no session measured the term. And it must not go silent,
     * which reads exactly like a complete figure. So it says which count is missing, in words.
     */
    const older = withDays([mineDay(0), mineDay(1, { coverage: undefined })]);
    const reading = readOf('tokens', older, YOURS) as ScalarReading;
    // The sum is unaffected — the rows carry their tokens, and only the caption is short.
    expect(reading.value).toBe(total(MINE_TOKENS));
    expect(reading.sub).toBe(`across ${MINE_RUNS} runs · session coverage not recorded`);
    expect(reading.sub).not.toMatch(/\d+ of \d+/);
    // And a row with NO block reads differently from one whose block counted nothing, which is the
    // distinction the words exist to hold.
    const measuredNone = withDays([mineDay(0), mineDay(1, { tokens: 'UNMEASURED' })]);
    expect((readOf('tokens', measuredNone, YOURS) as ScalarReading).sub)
      .toBe(`across ${MINE_RUNS} runs · 1 of ${MINE_RUNS} sessions`);
  });

  it('reports nothing measured as unmeasured, never as zero spend', () => {
    const none = withDays([mineDay(0, { tokens: 'UNMEASURED' })]);
    // `0` here reads as "you spent nothing", which is the opposite of "nobody recorded it". The `0` in
    // the footnote is a different statement: this day's one session did not record the term.
    expect(readOf('tokens', none, YOURS))
      .toEqual({ available: true, value: 'UNMEASURED', sub: 'across 1 run · 0 of 1 sessions' });
  });

  it('does not sum a value that is not a number, whatever it claims to be', () => {
    // `isSentinel` matches three strings. A `null` off a malformed wire would pass that filter and
    // contribute 0 to a `reduce`, turning one absent day into a measured one.
    const broken = withDays([
      mineDay(0),
      dayRow(MINE_DATES[1], { runs: 1, tokens: null as unknown as number }),
    ]);
    expect(readOf('tokens', broken, YOURS)).toEqual({
      available: true, value: MINE_TOKENS[0], sub: `across ${MINE_RUNS} runs · 1 of 2 sessions`,
    });
  });

  it('reports a term no day carries as absent, never as a zero', () => {
    /*
     * The three widest terms are newer than the rows, and a row is written once and carried forward —
     * so an actor with rows from before a term keeps the shorter shape. A `?? 0` on those would report
     * a free bill, 0% cache reuse and no models to someone whose days were fully measured.
     */
    for (const id of ['cost', 'cacheReuse', 'contextVsGenerated', 'modelsUsed']) {
      const reading = readOf(id, myDaysUnpriced, YOURS);
      expect(reading, id).toMatchObject({ available: false, code: 'notInBundle' });
      if (reading.available) continue;
      expect(reading.reason).toMatch(/Your own daily rows carry no/);
      expect(reading.reason).toMatch(/written once and carried forward/);
    }
    // And the terms that ARE on those rows still read, on the very same bundle.
    expect(readOf('tokens', myDaysUnpriced, YOURS)).toMatchObject({ value: total(MINE_TOKENS) });
  });

  it('drops a row that cannot be placed in time rather than folding it in', () => {
    /*
     * A row with no date cannot be put inside or outside a window, cannot be ordered and cannot be
     * counted as a day — so folding its numbers in would put spend in a total whose basis label could
     * not account for it.
     */
    const undated = withDays([
      mineDay(0),
      { runs: 4, tokens: 999_999 } as unknown as ActorDay,
      { date: '30-08-2026', runs: 4, tokens: 999_999 } as unknown as ActorDay,
    ]);
    expect(readOf('tokens', undated, YOURS))
      .toEqual({ available: true,
                 value: MINE_TOKENS[0],
                 sub: 'across 1 run · 1 of 1 sessions' });
  });
});

// --- the evicted days ---------------------------------------------------------
describe('the rolled-up bucket', () => {
  /** The days retention has dropped, as one bucket: nine days of the reader's earlier spend. */
  const ROLLED_DAYS = 9;
  const ROLLED_RUNS = 12;
  const ROLLED_TOKENS = 3_120_004;
  /**
   * The sessions those nine days held.
   *
   * Equal to the request count because none of these twelve requests spawned a plan sub-session, and a
   * bucket's coverage counts are the per-day counts SUMMED — the producer folds the block field by field
   * along with the spend, so a bucket built from rows that carried it carries it too.
   */
  const ROLLED_SESSIONS = ROLLED_RUNS;
  const rolledUp = {
    fromDate: '2026-08-04', toDate: '2026-08-12', days: ROLLED_DAYS,
    runs: ROLLED_RUNS, tokens: ROLLED_TOKENS,
    coverage: dayCoverage({ tokens: ROLLED_TOKENS }, ROLLED_SESSIONS),
  };

  it('is folded into a cumulative total so it does not silently shrink to retention', () => {
    const data = withDays([mineDay(0), mineDay(1)],
      { rolledUp, historyFirstDate: rolledUp.fromDate });
    expect(readOf('tokens', data, YOURS)).toEqual({
      available: true,
      value: total(MINE_TOKENS) + ROLLED_TOKENS,
      sub: `across ${MINE_RUNS + ROLLED_RUNS} runs · ${MINE_RUNS + ROLLED_SESSIONS} of `
        + `${MINE_RUNS + ROLLED_SESSIONS} sessions`,
    });
    // "All time" over the retained rows alone would present a figure that shrinks as old rows fall
    // off the end, which is what a total is supposed to be immune to.
    expect(readOf('tokens', data, YOURS)).not.toMatchObject({ value: total(MINE_TOKENS) });
  });

  it('is left out of a trailing window, which cannot contain days it has no dates for', () => {
    const data = withDays([mineDay(0), mineDay(1)],
      { rolledUp, historyFirstDate: rolledUp.fromDate });
    for (const period of ['trailing30', 'trailing7'] as const) {
      expect(readOf('tokens', data, { scope: 'yours', period }), period).toMatchObject({
        value: total(MINE_TOKENS),
        sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
      });
    }
  });

  it('contributes every session it holds to the coverage fraction, not one day', () => {
    /*
     * The bucket stands for nine days and twelve sessions, and its coverage counts sum along with its
     * spend — so a bucket that measured nothing costs the fraction all twelve rather than the one day it
     * is stored as. Counting it as a day would have understated the denominator by eight; counting it as
     * one row would understate it by eleven, and report a total missing most of the reader's history as
     * two thirds complete.
     */
    const data = withDays([mineDay(0), mineDay(1)], {
      rolledUp: { ...rolledUp, tokens: 'UNMEASURED', coverage: dayCoverage({}, ROLLED_SESSIONS) },
      historyFirstDate: rolledUp.fromDate,
    });
    expect(readOf('tokens', data, YOURS)).toEqual({
      available: true,
      value: total(MINE_TOKENS),
      sub: `across ${MINE_RUNS + ROLLED_RUNS} runs · ${MINE_RUNS} of `
        + `${MINE_RUNS + ROLLED_SESSIONS} sessions`,
    });
  });

  it('is not a period at all before anything has been evicted', () => {
    /*
     * The producer writes the bucket from its FIRST pass, with `days: 0` and no spend terms on it
     * until retention actually drops something. So a reader who was swept and found idle has an empty
     * history AND that bucket — and folding it in would make their figures read "the wire carries no
     * such term" instead of the `0` the producer went to the trouble of recording. The zero state is
     * the whole point, and an empty bucket must not take it away.
     */
    const idle = withDays([], {
      rolledUp: { days: 0, fromDate: 'UNMEASURED', toDate: 'UNMEASURED' },
      historyFirstDate: 'UNMEASURED',
    });
    expect(readOf('tokens', idle, YOURS))
      .toEqual({ available: true, value: 0, sub: 'no runs in this period' });
    // And the basis label does not name a span the bucket does not stand for.
    expect(describeBasis(idle, YOURS)).toBe('all time, no day of activity recorded');
  });

  it('is folded in on a day count it will not state, rather than dropped', () => {
    // Absent `days` is not `days: 0`: the first says the bucket will not say how wide it is, and its
    // spend is still real. Dropping it would silently shrink a total. How WIDE it is no longer decides
    // anything the caption says, because the sessions it holds are counted on the block regardless.
    const unsaid = withDays([mineDay(0)], {
      rolledUp: { runs: 4, tokens: 500, coverage: dayCoverage({ tokens: 500 }, 4) },
      historyFirstDate: '2026-08-04',
    });
    expect(readOf('tokens', unsaid, YOURS))
      .toMatchObject({ value: MINE_TOKENS[0] + 500, sub: 'across 5 runs · 5 of 5 sessions' });
  });

  it('is no period either when it states neither a day count nor a figure', () => {
    /*
     * The other end of the test above, and the case a default day count gets wrong whichever way it
     * defaults. A bucket carrying only its own bookkeeping stands for nothing — there is no figure in
     * it to fold and no width claimed for it — so an idle reader must still get the `0` the producer
     * recorded. Admitted on an assumed day, the same reader's tiles read "your daily rows carry no
     * token total", which is the absence-as-measurement this surface exists to refuse.
     */
    const idle = withDays([], { rolledUp: { fromDate: 'UNMEASURED', toDate: 'UNMEASURED' } });
    expect(readOf('tokens', idle, YOURS))
      .toEqual({ available: true, value: 0, sub: 'no runs in this period' });
    expect(readOf('tokens', idle, YOURS)).not.toMatchObject({ available: false });
    // And the empty bucket does not become a span the label can name.
    expect(describeBasis(idle, YOURS)).toBe('all time, no day of activity recorded');
  });

  it('states no fraction at all when it was rolled up before the coverage counts existed', () => {
    /*
     * The bucket is the oldest thing in a bundle by construction, so it is the FIRST period to have been
     * written without the counts and the last to be rewritten with them — the producer folds a bucket
     * forward field by field rather than recomputing it, so one that predates the block keeps predating
     * it for as long as the bundle lives.
     *
     * Its spend is still folded in, and the caption states which count is missing. A fraction over the
     * retained rows alone would put those two sessions over those two sessions and read as complete over
     * a total most of which came from days it cannot account for.
     */
    const data = withDays([mineDay(0), mineDay(1)], {
      rolledUp: { ...rolledUp, coverage: undefined },
      historyFirstDate: rolledUp.fromDate,
    });
    const reading = readOf('tokens', data, YOURS) as ScalarReading;
    expect(reading.value).toBe(total(MINE_TOKENS) + ROLLED_TOKENS);
    expect(reading.sub)
      .toBe(`across ${MINE_RUNS + ROLLED_RUNS} runs · session coverage not recorded`);
    expect(reading.sub).not.toMatch(/\d+ of \d+/);
  });
});

// --- the two trailing windows -------------------------------------------------
describe('a trailing window', () => {
  /** A day well outside both windows, so `allTime` and a trailing sum genuinely differ. */
  const OLD_DATE = '2026-06-01';
  const OLD_TOKENS = 8_400_000;
  const spread: ImproveData = withDays([
    dayRow(OLD_DATE, { runs: 3, tokens: OLD_TOKENS }),
    mineDay(0),
    mineDay(1),
  ]);

  it('covers the anchor day and the days before it, and no more', () => {
    // The two transcribed days are four apart, so both fall inside seven; the June day does not fall
    // inside thirty.
    expect(readOf('tokens', spread, YOURS))
      .toMatchObject({ value: OLD_TOKENS + total(MINE_TOKENS) });
    for (const period of ['trailing30', 'trailing7'] as const) {
      expect(readOf('tokens', spread, { scope: 'yours', period }), period).toMatchObject({
        value: total(MINE_TOKENS),
        sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
      });
    }
  });

  it('includes its anchor day, so seven days is the anchor and the six before it', () => {
    /*
     * Off by one here is the difference between seven days and eight. The anchor is 3 Sep, so a
     * seven-day window opens on 28 Aug: a row dated 28 Aug is in and one dated 27 Aug is out.
     */
    const edge = (date: string) => withDays([dayRow(date, { runs: 1, tokens: 5 }), mineDay(1)]);
    const week = { scope: 'yours', period: 'trailing7' } as const;
    expect(readOf('tokens', edge('2026-08-28'), week)).toMatchObject({ value: 5 + MINE_TOKENS[1] });
    expect(readOf('tokens', edge('2026-08-27'), week)).toMatchObject({ value: MINE_TOKENS[1] });
  });

  it('is anchored to the latest recorded day rather than to the clock', () => {
    /*
     * A figure that changed depending on when it was looked at would not be a measurement, and these
     * tests could then not be written without freezing a date. The cost is that a reader who last ran
     * a month ago sees the week ending THEN — true, and unreadable without the date, which is why the
     * basis label names it.
     */
    const stale = withDays([dayRow('2026-05-30', { runs: 2, tokens: 11 }),
                            dayRow(OLD_DATE, { runs: 3, tokens: OLD_TOKENS })]);
    expect(readOf('tokens', stale, { scope: 'yours', period: 'trailing7' }))
      .toMatchObject({ value: 11 + OLD_TOKENS, sub: 'across 5 runs · 5 of 5 sessions' });
    expect(describeBasis(stale, { scope: 'yours', period: 'trailing7' }))
      .toBe(`7 days to ${formatIsoDay(OLD_DATE)}`);
  });

  it('offers exactly the three periods, each named separately from the others', () => {
    expect(PERIODS).toEqual(['allTime', 'trailing30', 'trailing7']);
    // The cumulative total FIRST: it is the reading that cannot be empty for a reader who has ever
    // run anything, which is the defect the switch was added around.
    expect(PERIODS[0]).toBe('allTime');
    expect(PERIODS.map((period) => PERIOD_LABEL[period])).toEqual(['All time', '30 days', '7 days']);
    // Two bounds, named apart. They are the same kind of thing and not the same number, and the
    // longer one coinciding with the producer's day-row retention is a coincidence rather than a
    // relationship.
    expect(new Set(PERIODS.map((period) => PERIOD_LABEL[period])).size).toBe(3);
  });
});

// --- 0, the sentinel, and nothing at all --------------------------------------
/**
 * THE THREE STATES, which is the whole of this task.
 *
 * Two different facts used to produce the same blank: a reader who really spent nothing and a reader
 * nothing was known about. The producer now writes a bundle for every actor it has seen, including
 * one with no day of activity, so the two are distinguishable — and each has its own render.
 */
describe('a reader who ran nothing, against a reader nothing is known about', () => {
  it('shows 0 for a bundle whose history is present and empty', () => {
    // The producer swept this actor and recorded that there was nothing. That IS a measurement, and
    // hiding it behind a sentinel would throw away a fact it went to the trouble of writing.
    expect(myDaysNone.actor?.history).toEqual([]);
    for (const id of ['tokens', 'llmCalls', 'totalTime', 'modelsUsed']) {
      expect(readOf(id, myDaysNone, YOURS), id)
        .toEqual({ available: true, value: 0, sub: 'no runs in this period' });
    }
  });

  it('never infers 0 from a missing key', () => {
    // The two absences, neither of which is a zero: no bundle at all, and a bundle with no history.
    for (const data of [harvestBundle, minePriced]) {
      for (const id of IDS) {
        const reading = readOf(id, data, YOURS);
        expect(reading.available, id).toBe(false);
        expect(reading).not.toHaveProperty('value');
      }
    }
  });

  it('shows the price as the sentinel and the reason, never as $0.00', () => {
    /*
     * Cost's exception, and `formatUsd`'s own argument for it: `$0.00` is reserved for a session that
     * used a priced model and no tokens, so a rendered zero on a price reads as a session that ran and
     * was free. Nothing priced anything here, so it would assert a bill nobody computed.
     */
    const reading = readOf('cost', myDaysNone, YOURS) as ScalarReading;
    expect(reading.value).toBe('UNMEASURED');
    expect(reading.value).not.toBe(0);
    expect(formatUsd(0)).toBe('$0.00');
    // Both facts in the footnote: that there were no runs, and the producer's own sentence on why
    // nothing is priced. The whole of that sentence rides the caveat.
    expect(reading.sub).toMatch(/^no runs in this period · /);
    expect(reading.sub).toMatch(/cost not priced/);
    expect(reading.caveat).toBe(NO_RATE_TABLE_BASIS);
  });

  it('refuses a share and an average rather than drawing one from nothing', () => {
    /*
     * A total can be zero; a share and an average cannot. The reader spent no tokens, which is a real
     * zero — but `0/0` is not `0%`, and `0 tokens per session` claims a session that used none. Both
     * say there was nothing to divide over rather than dividing anyway.
     */
    for (const id of ['cacheReuse', 'contextVsGenerated', 'tokensPerSession']) {
      const reading = readOf(id, myDaysNone, YOURS);
      expect(reading, id).toMatchObject({ available: false, code: 'noDenominator' });
      if (reading.available) continue;
      expect(reading.reason, id).toMatch(/no runs in this period/);
    }
  });

  it('reads a day that ran and measured zero as the zero it is', () => {
    // Distinct from every case above: a day WITH runs whose spend really was 0. A measured 0 is a
    // measurement and is set as one.
    const idle = withDays([dayRow(MINE_DATES[0], { runs: 2, tokens: 0, llmCalls: 0 })]);
    expect(readOf('tokens', idle, YOURS))
      .toEqual({ available: true, value: 0, sub: 'across 2 runs · 2 of 2 sessions' });
  });
});

// --- the four figures the widened day row makes personal -----------------------
/**
 * What the producer measured per session all along and used to drop on the way to a row.
 *
 * These four are the whole point of widening it: a reader could see the harness's cache reuse, the
 * harness's context split, the harness's model count and the harness's tokens per session, and
 * nothing at all about their own — while the measurement behind each existed per run and was
 * projected away.
 */
describe('your own cache reuse, context split, models and per-session tokens', () => {
  it('splits the reader’s own cache reads against their own cache writes', () => {
    // Reads against writes ALONE, as on the population: 69% here, and folding the other two
    // categories in would answer a different question with a plausible-looking number.
    expect(readOf('cacheReuse', myDaysWide, YOURS)).toEqual({
      available: true,
      value: total(MINE_CACHE_READ),
      ratio: total(MINE_CACHE_READ) / (total(MINE_CACHE_READ) + total(MINE_CACHE_WRITE)),
      // Captioned by the LEAST-covered of the split's two sides, exactly as the shared split is: both
      // days recorded both, so the fraction is whole.
      sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
    const reading = readOf('cacheReuse', myDaysWide, YOURS) as RatioReading;
    expect(reading.ratio).toBeGreaterThan(0.68);
    expect(reading.ratio).toBeLessThan(0.70);
  });

  it('splits the reader’s own context from what their days generated, with no residual', () => {
    // The four per-day categories reconstruct each day's own total, so the split's denominator IS
    // the reader's token sum and the two personal tiles cannot disagree about it.
    expect(MINE_CONTEXT + total(MINE_OUTPUT)).toBe(total(MINE_TOKENS));
    expect(readOf('contextVsGenerated', myDaysWide, YOURS)).toEqual({
      available: true,
      value: MINE_CONTEXT,
      ratio: MINE_CONTEXT / total(MINE_TOKENS),
      sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
  });

  it('counts the distinct models the reader’s own days spent on, not the count per day', () => {
    // Both days name both ids, so a metric that summed per-day roster lengths would report 4.
    // Captioned by the TOKEN term's coverage, because the ids come off the same per-session record the
    // token counts do — a roster has no coverage entry of its own and needs none.
    expect(readOf('modelsUsed', myDaysWide, YOURS)).toEqual({
      available: true,
      value: 2,
      sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
    // One day on each model: still two distinct, and still not the sum of two ones by accident.
    const oneEach = withDays([wideDay(0, { models: [MODEL_IDS[0]] }),
                              wideDay(1, { models: [MODEL_IDS[1]] })]);
    expect(readOf('modelsUsed', oneEach, YOURS)).toMatchObject({ value: 2 });
    // Both days on the SAME model is one, which is the reading a `sum` would get wrong.
    const same = withDays([wideDay(0, { models: [MODEL_IDS[0]] }),
                           wideDay(1, { models: [MODEL_IDS[0]] })]);
    expect(readOf('modelsUsed', same, YOURS)).toMatchObject({ value: 1 });
  });

  it('divides the reader’s own tokens by exactly the sessions that recorded them', () => {
    expect(readOf('tokensPerSession', myDaysWide, YOURS)).toEqual({
      available: true,
      value: Math.round(total(MINE_TOKENS) / MINE_RUNS),
      sub: `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
    expect(compactNumber(Math.round(total(MINE_TOKENS) / MINE_RUNS))).toBe('3.3M');
    /*
     * Neither day here fanned out, so the requests and the sessions coincide and this figure cannot
     * tell the two divisors apart — which is what the day below is for. What it CAN say is that an
     * unstated run count moves the extent clause and not the figure: it used to drop such a day from
     * both halves, so a reader whose second day did not say how many requests it held had their
     * average halved by a count the average does not depend on.
     */
    const unsaid = withDays([wideDay(0), wideDay(1, { runs: 'UNMEASURED' })]);
    expect(readOf('tokensPerSession', unsaid, YOURS)).toEqual({
      available: true,
      value: Math.round(total(MINE_TOKENS) / MINE_RUNS),
      sub: `run count not recorded · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
    });
  });

  it('divides by the sessions and not by the requests, on the day where those differ', () => {
    /*
     * The defect's own signature, and the only fixture shape that shows it: a day whose requests
     * fanned out into plan sub-sessions. `runs` counts the REQUESTS (`dto._day_row`: `len(requests)`)
     * while `coverage.total` counts every session whose spend landed on the day — a real day reads
     * `runs: 17, coverage.total: 19` — so dividing by `runs` reported 11.8% more tokens per session
     * than the reader spent, captioned by the very fraction that says 19 sessions recorded them.
     *
     * Both counts are in the footnote for the reason they always were, and neither is the other: the
     * extent says how much of the reader's activity the figure is drawn from, and the fraction is what
     * the figure is divided by. A reader can now do the arithmetic in the caption and get the tile.
     */
    const fanned = withDays([dayRow(MINE_DATES[0], {
      runs: 17, tokens: 1_900_000, coverage: dayCoverage({ tokens: 1_900_000 }, 19),
    })]);
    expect(readOf('tokensPerSession', fanned, YOURS)).toEqual({
      available: true, value: 100_000, sub: 'across 17 runs · 19 of 19 sessions',
    });
    // 1,900,000 / 17 is 111,765 — the old figure, and plausible enough that only the fraction beside
    // it could ever have contradicted it.
    expect(readOf('tokensPerSession', fanned, YOURS)).not.toMatchObject({ value: 111_765 });
  });

  it('will not divide by the sessions a period held when only some of them recorded tokens', () => {
    /*
     * The other divisor a session count could be, and it is a zero in disguise. Tokens measured on one
     * of two sessions over `coverage.total` asserts that the unmeasured session spent nothing, where
     * what is known is that nobody measured it — so the average comes out half what the measured
     * session actually spent. Divided by the sessions that RECORDED the term it is exact across them,
     * which is the arithmetic the shared figure does one population up.
     */
    const partly = withDays([dayRow(MINE_DATES[0], {
      runs: 2, tokens: 400, coverage: { ...dayCoverage({ tokens: 400 }, 2), tokens: 1 },
    })]);
    expect(readOf('tokensPerSession', partly, YOURS)).toEqual({
      available: true, value: 400, sub: 'across 2 runs · 1 of 2 sessions',
    });
    expect(readOf('tokensPerSession', partly, YOURS)).not.toMatchObject({ value: 200 });
  });

  it('refuses a per-session figure when no day says how many sessions it held', () => {
    /*
     * ALL-OR-NOTHING, as the fraction is. A row written before the coverage counts existed cannot say
     * how many sessions recorded its tokens, and a divisor summed over the rows that can would put the
     * whole fold's numerator over part of its denominator — an average over more sessions than it
     * admits to, wearing an average's shape. The token TOTAL is still shown, captioned in words.
     */
    const older = withDays([mineDay(0, { coverage: undefined })]);
    expect(readOf('tokensPerSession', older, YOURS)).toMatchObject({
      available: false, code: 'noDenominator',
    });
    expect(readOf('tokens', older, YOURS))
      .toMatchObject({ value: MINE_TOKENS[0], sub: 'across 1 run · session coverage not recorded' });
  });

  it('excludes a day from a split rather than taking its two sides over different days', () => {
    /*
     * If a day measured cache reads and not cache writes, admitting it puts a numerator over a
     * denominator drawn from a different set of days — not a share of anything, wearing a share's
     * shape. Excluded, the ratio is exact over the days that contributed, and the footnote counts the
     * sessions behind the worst-covered side of the split, which is the same conservative direction.
     */
    const halfMeasured = withDays([
      wideDay(0),
      wideDay(1, { byCategory: { ...categories(1), cacheWrite: 'UNMEASURED' } }),
    ]);
    expect(readOf('cacheReuse', halfMeasured, YOURS)).toEqual({
      available: true,
      value: MINE_CACHE_READ[0],
      ratio: MINE_CACHE_READ[0] / (MINE_CACHE_READ[0] + MINE_CACHE_WRITE[0]),
      sub: `across ${MINE_RUNS} runs · 1 of ${MINE_RUNS} sessions`,
    });
    // The second day's cache reads are NOT in the numerator, which is what a per-term filter would
    // have done — and the figure would have looked entirely plausible.
    expect(readOf('cacheReuse', halfMeasured, YOURS))
      .not.toMatchObject({ value: total(MINE_CACHE_READ) });
  });

  it('reports a split no day measured as unmeasured, and one measuring zero as no share', () => {
    const nothing = withDays([wideDay(0, { byCategory: {
      input: 'UNMEASURED', output: 'UNMEASURED', cacheRead: 'UNMEASURED',
      cacheWrite: 'UNMEASURED' } })]);
    // A bare `ratio: 0` would draw an empty track, which reads as "no cache reuse at all".
    expect(readOf('cacheReuse', nothing, YOURS)).toEqual({
      available: true, value: 'UNMEASURED', ratio: 'UNMEASURED',
      sub: 'across 1 run · 0 of 1 sessions',
    });
    // Measured and adding to nothing is a third answer: 0/0 is not 0%, so no bar may be drawn.
    const zeroes = withDays([wideDay(0, { byCategory: {
      input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } })]);
    expect(readOf('cacheReuse', zeroes, YOURS)).toMatchObject({
      available: false, code: 'noDenominator',
    });
  });

  it('reports a roster nothing named as unmeasured rather than as zero models', () => {
    // The producer sends the sentinel and never `[]` for a period that named no model, and a `0` here
    // would say the reader's runs used none — where the `0` on an empty history says they had none.
    for (const models of ['UNMEASURED' as const, [] as string[]]) {
      const data = withDays([wideDay(0, { models })]);
      expect(readOf('modelsUsed', data, YOURS), JSON.stringify(models)).toEqual({
        // The FIGURE is the sentinel while the fraction is whole, and the two are not in conflict: the
        // day's session recorded tokens, which is what the coverage counts, and no roster arrived with
        // them. A caption derived from the value would have hidden that by agreeing with it.
        available: true, value: 'UNMEASURED', sub: 'across 1 run · 1 of 1 sessions',
      });
    }
  });
});

// --- what the figures are figures OF ------------------------------------------
describe('the basis label', () => {
  it('names the first day a cumulative total reaches back to', () => {
    // "All time" over rows that reach back a fortnight is not all time, and the reader cannot tell
    // the difference from the number.
    expect(describeBasis(myDaysUnpriced, YOURS)).toBe(`since ${formatIsoDay(MINE_DATES[0])}`);
    expect(formatIsoDay(MINE_DATES[0])).toBe('30 Aug');
  });

  it('prefers the producer’s own first date, which is the only one that counts the bucket', () => {
    // With days evicted, the first ROW is where the rows start and not where the figure does.
    const data = withDays([mineDay(0), mineDay(1)], {
      rolledUp: { fromDate: '2026-08-04', days: 9, runs: 12, tokens: 1 },
      historyFirstDate: '2026-08-04',
    });
    expect(describeBasis(data, YOURS)).toBe('since 4 Aug');
  });

  it('names the day a trailing window is anchored to, which its button cannot', () => {
    expect(describeBasis(myDaysUnpriced, { scope: 'yours', period: 'trailing30' }))
      .toBe(`30 days to ${formatIsoDay(MINE_DATES[1])}`);
    expect(describeBasis(myDaysUnpriced, { scope: 'yours', period: 'trailing7' }))
      .toBe(`7 days to ${formatIsoDay(MINE_DATES[1])}`);
  });

  it('carries the bundle’s own stamp, because a stale total reads exactly like a current one', () => {
    /*
     * A quiet actor's bundle is not deleted: the producer skips the write and the previous item
     * persists, carrying an older stamp. This date is the only thing on the wire that says so.
     */
    const stamped = withDays([mineDay(0), mineDay(1)], { createdAt: '2026-09-03T04:55:09Z' });
    expect(describeBasis(stamped, YOURS)).toBe('since 30 Aug · recorded 3 Sep');
    // Absent when the item carried none, and then not claimed at all.
    expect(describeBasis(myDaysUnpriced, YOURS)).not.toMatch(/recorded/);
  });

  it('says there was no day of activity rather than naming a span with nothing in it', () => {
    expect(describeBasis(myDaysNone, YOURS)).toBe('all time, no day of activity recorded');
    expect(describeBasis(myDaysNone, { scope: 'yours', period: 'trailing7' }))
      .toBe('7 days, no day of activity recorded');
  });

  it('says the personal history is absent rather than inventing a span for it', () => {
    // The bundle IS here and its day rows are not, so the figures beside it can come off its other
    // halves — which is why this line still has work to do on this shape.
    expect(describeBasis(minePriced, YOURS)).toBe('no personal history in this data');
  });

  it('says nothing where the cause standing in for the figures is itself a phrase', () => {
    /*
     * The reader the producer has never seen. That cause carries a phrase of its own (see
     * `Unavailable.brief`), printed where the figures would be — so it is already the short form of
     * this absence, and a second short form beside the switches is the same fact twice on adjacent
     * lines. Nothing is hidden by the silence: with no bundle at all no personal figure is readable,
     * so there is no span this line could be qualifying.
     */
    expect(harvestBundle.actor).toBeUndefined();
    expect(unavailable('tokens', harvestBundle, YOURS).brief).toBeTruthy();
    for (const period of PERIODS) {
      expect(describeBasis(harvestBundle, { scope: 'yours', period }), period).toBeNull();
    }
  });

  it('names no span at the shared scope, and nothing at all the wire has not stated', () => {
    /*
     * The shared figures are not a cumulative total over a span a reader has to be told — every one of
     * them comes off the scorecard's single window whichever period is pressed — so there is no
     * ambiguity here of the kind the personal line resolves, and the denominator a reader needs is on
     * each tile against the figure it qualifies.
     *
     * `null` on every shape below, the two that used to produce prose included: a scorecard with a
     * window, and one with none. What may appear here is one thing and it is not a span — how much of
     * the harness the tiles' own denominators are themselves a sample of, which nothing on a tile can
     * carry (see the discard tests above). None of these bundles states it, which is the live case.
     */
    const attributed = {
      ...harvestBundle,
      scorecard: { ...harvestBundle.scorecard!, attribution: { runs: 9, attributed: 2, actors: 1 } },
    } as ImproveData;
    for (const data of [harvestBundle, livePriced, minePriced, attributed, NO_SCORECARD_BUNDLE]) {
      for (const period of PERIODS) {
        expect(describeBasis(data, { scope: 'everyone', period }), period).toBeNull();
      }
    }
  });

  it('keeps the personal line, which is the one a total cannot be read without', () => {
    /*
     * Guarded from the other end, and against the sweep rather than the wording — the tests above pin
     * what each shape says. The shared line went because a cumulative total is not what the shared
     * scope shows; the personal one is exactly that, and `5.7M` over a fortnight and over a year are
     * different facts the tile cannot tell apart. So every personal shape has something to say at every
     * period, the empty history included.
     *
     * Every shape but one, and the exception is named rather than swept: the reader with no bundle at
     * all, whose absence is said as a phrase where the figures would be. That one is pinned as `null`
     * two tests up, so this list going quiet for any OTHER reason still fails here.
     */
    for (const data of [myDaysUnpriced, myDaysWide, myDaysNone, minePriced]) {
      for (const period of PERIODS) {
        expect(describeBasis(data, { scope: 'yours', period }), period).not.toBeNull();
      }
    }
  });
});

// --- the one combination the wire cannot answer -------------------------------
describe('the period the shared wire cannot answer', () => {
  it('refuses every period at the shared scope and none at the personal one', () => {
    /*
     * It comes down to ONE count no shared row carries yet. The shared bundle carries a row per day and
     * those sums add up, so a period total is arithmetically available. What makes a shared figure
     * readable is the denominator beside it: how many SESSIONS recorded the term. Tokens are measured on
     * 354 of this population's 553 sessions, so a total captioned by anything coarser reads as complete
     * over two thirds of a population.
     *
     * The producer now writes that count onto both row kinds. What it cannot do is back-fill: a day row
     * is written once and carried forward, so the shared rows the wire serves are still the pre-count
     * shape. The personal footnotes state the absence in words and still show a figure, because their
     * alternative is no figure; a shared figure has the scorecard's own window, which states its
     * coverage exactly, so declining the period claims nothing and costs nothing.
     */
    for (const period of PERIODS) {
      const refusal = periodRefusal({ scope: 'everyone', period });
      expect(refusal, period).not.toBeNull();
      expect(refusal!.code).toBe('noPeriod');
      expect(periodRefusal({ scope: 'yours', period }), period).toBeNull();
    }
  });

  it('is one cause the block can say once, rather than eight', () => {
    // Structural, because it is a fact about the wire and not about any figure: every metric would
    // report the same sentence, and printing it eight times would bury its single cause.
    expect(isStructural('noPeriod')).toBe(true);
    const reasons = PERIODS.map((period) => periodRefusal({ scope: 'everyone', period })!.reason);
    expect(new Set(reasons).size).toBe(1);
  });

  it('says what is missing and what is shown instead, in prose rather than a code name', () => {
    const { reason } = periodRefusal({ scope: 'everyone', period: 'allTime' })!;
    // The missing count, and the magnitude that makes it matter: a caption over two thirds of a
    // population that reads as complete is the whole objection, and the number is what carries it.
    expect(reason).toMatch(/how many sessions recorded it/);
    expect(reason).toMatch(/two thirds of this population/);
    expect(reason).toMatch(/latest harvest window/);
    expect(reason).not.toMatch(/noPeriod|undefined|null/);
    // And it points at no line beside it. This sentence reaches the reader as the disabled period
    // button's tooltip and nothing else, so a cross-reference to a caption would send them looking for
    // something that is not on screen — which is what it used to do.
    expect(reason).not.toMatch(/beside them|the basis/);
  });

  it('states a reason that is still true after the producer widens its day rows', () => {
    /*
     * The refusal must not rest on a fact about to change. The population history now gains the same
     * rolled-up bucket and first-date as the per-actor one, and its rows now carry cost, uncached input
     * and a model roster — so a sentence citing any of those as the obstacle would be false the day
     * that deploys, while still being what a reader is shown.
     *
     * It has already had to be corrected twice. The priced/unpriced token pair was a second reason until
     * the producer put both counts on the day row. And the session count itself was cited as a thing a
     * day row HAS NO FORM FOR, which stopped being true the moment the producer started writing one —
     * so the sentence now rests on what is on the WIRE rather than on what the producer can express, and
     * says which of the two it means.
     */
    const { reason } = periodRefusal({ scope: 'everyone', period: 'trailing30' })!;
    for (const landed of ['rolled-up', 'uncached input', 'which models', 'ten scalars', 'unpriced']) {
      expect(reason, landed).not.toContain(landed);
    }
    // It must not claim the count cannot be recorded, which would be false, and must say why a row that
    // exists does not have one: rows are written once, so the shape they were written with is the shape
    // they keep.
    expect(reason).toMatch(/records one per day now/);
    expect(reason).toMatch(/written once and then carried forward/);
  });

  it('leaves the shared readings themselves total over every view', () => {
    /*
     * The refusal is a property of the VIEW and lives in one place; the reads are not made partial by
     * it. Keeping them total is what stops the refusal being written into all eight of them — and the
     * shared reading genuinely ignores the period, because the scorecard describes one window
     * whichever button is pressed.
     */
    for (const metric of METRICS) {
      const readings = PERIODS.map((period) => metric.read(harvestBundle, { scope: 'everyone', period }));
      expect(new Set(readings.map((r) => JSON.stringify(r))).size, metric.id).toBe(1);
    }
  });

  it('is blind to BOTH day-row sources, not merely to the one the fixture lacks', () => {
    /*
     * The assertion above is on a bundle with no day rows at all, so it holds for a reason weaker than
     * the one that matters: it cannot tell a read that IGNORES day rows from a read that had none to
     * find. These three bundles carry the SAME scorecard and differ only in their day rows — one has
     * the population's own, one has the reader's, one has none — so any shared reading that consumed
     * either would move between them.
     *
     * What that pins. The registry used to carry a normaliser lifting the population's flat rows into
     * the per-actor shape, and a `daysOf` branch fetching them; neither had a render path, because
     * every entry routes its shared side to a scorecard reader and `describeBasis` answers the shared
     * scope before the day fold. `cumulate` now takes a period rather than a view, so the personal-only
     * reading is structural — and this is what catches a read being wired back to either source.
     */
    const withPopulationDays: ImproveData = {
      ...harvestBundle,
      history: [
        { date: '2026-08-27', population: 4, tokens: 111, llmCalls: 7, input: 1, output: 2 },
        { date: '2026-08-28', population: 5, tokens: 222, llmCalls: 8, input: 3, output: 4 },
        { date: '2026-09-02', population: 6, tokens: 333, llmCalls: 9, input: 5, output: 6 },
      ],
      historyFirstDate: '2026-08-01',
    } as unknown as ImproveData;

    // `myDaysWide` is built on `harvestBundle`, so its scorecard is the same one: the only difference
    // is the reader's two priced days, which carry every term a shared figure could be summed from.
    for (const bundle of [withPopulationDays, myDaysWide]) {
      for (const metric of METRICS) {
        const bare = metric.read(harvestBundle, EVERYONE);
        for (const period of PERIODS) {
          expect(metric.read(bundle, { scope: 'everyone', period }), `${metric.id} ${period}`)
            .toEqual(bare);
        }
      }
      // And the basis line still refuses a span rather than naming one off rows it does not read.
      for (const period of PERIODS) {
        expect(describeBasis(bundle, { scope: 'everyone', period }), period)
          .toBe(describeBasis(harvestBundle, EVERYONE));
      }
    }
  });
});

// --- a sentinel must never reach a formatter ----------------------------------
describe('an unmeasured value and the formatter that would misprint it', () => {
  /**
   * What each formatter answers when handed something that is not a number.
   *
   * These are BACKSTOPS, and none of them is a reader-facing statement about an absent measurement:
   * `not a cost` under "Your cost" reads as a producer bug, and `$0.00` — which is what a formatter
   * over a coerced sentinel or a `?? 0` would produce — reads as free. So a sentinel has to be
   * branched on BEFORE any formatter sees it, which `MeasuredValue` does and this pins the reason
   * for.
   */
  it('would misprint every sentinel, which is why the branch comes first', () => {
    for (const sentinel of ['UNMEASURED', 'NO_DATA', 'UNKNOWN'] as const) {
      const asNumber = sentinel as unknown as number;
      expect(formatUsd(asNumber), sentinel).toBe('not a cost');
      expect(compactNumber(asNumber), sentinel).toBe('not a count');
      expect(formatExtent(asNumber), sentinel).toBe('not a duration');
    }
    // And the one a `?? 0` would produce instead, which is worse because it is believable.
    expect(formatUsd(0)).toBe('$0.00');
  });

  it('never hands a formatter anything but a finite number or a sentinel', () => {
    /*
     * The sweep that makes the rule structural rather than spot-checked. Every metric over every
     * bundle in this file: a reading's `value` is either a number a formatter can set, or one of
     * exactly three strings a consumer knows to branch on. A `null`, an `undefined` or a stray
     * string reaching `MeasuredValue` would be formatted — `formatUsd(null as never)` is
     * `not a cost` — and printed in display type beside real figures.
     */
    const bundles = [
      NO_SCORECARD_BUNDLE, wireBundle, harvestBundle, pricedBundle, livePriced, partlyPriced,
      mineUnpriced, minePriced, mineWide, withActor([], 0),
      myDaysUnpriced, myDaysWide, myDaysNone,
      withActor([actorRun(null as unknown as number, undefined as unknown as number,
        null as unknown as number, 'not a duration' as unknown as number)], 3),
      // Every widened term corrupt at once: a roster that is not a list, a split that is not an
      // object, and a pricing count that is a string off a malformed wire.
      withActor([actorRun(10, 2, 1, 1, {
        models: 'claude' as unknown as string[],
        byCategory: 7 as unknown as NonNullable<ActorRun['spend']['byCategory']>,
        costTokensPriced: '10' as unknown as number,
        costTokensUnpriced: null as unknown as number,
      })], 3),
      // The same corruption a day row down, plus a run count that is not one — which is the divisor
      // of a per-session figure and the numerator of every cumulative footnote.
      withDays([dayRow(MINE_DATES[0], {
        runs: null as unknown as number,
        tokens: 'not a count' as unknown as number,
        cost: Number.NaN,
        latencyMs: Number.POSITIVE_INFINITY,
        models: 'claude' as unknown as string[],
        byCategory: 7 as unknown as ActorDay['byCategory'],
      })]),
    ];
    let sentinels = 0;
    for (const bundle of bundles) {
      for (const view of [EVERYONE, YOURS, { scope: 'yours', period: 'trailing7' } as const]) {
        for (const metric of METRICS) {
          const reading = metric.read(bundle, view);
          if (!reading.available) continue;
          for (const [what, field] of Object.entries({
            value: reading.value, ratio: (reading as { ratio?: unknown }).ratio,
          })) {
            if (field === undefined) continue;
            if (typeof field === 'number') {
              expect(Number.isFinite(field), `${metric.id}.${what}`).toBe(true);
              continue;
            }
            expect(SENTINELS as readonly string[], `${metric.id}.${what}`).toContain(field);
            sentinels += 1;
          }
        }
      }
    }
    // The sweep has to actually MEET sentinels, or it would pass on a registry that had quietly
    // started returning zeros.
    expect(sentinels).toBeGreaterThan(0);
  });
});

// --- a wire that is missing what the type promises ----------------------------
describe('a scorecard missing a field the type declares required', () => {
  it('reports the terms it cannot read and keeps the ones it can', () => {
    type ByCategory = Scorecard['spend']['byCategory'];
    const data = withSpend({ byCategory: undefined as unknown as ByCategory });
    for (const id of ['contextVsGenerated', 'cacheReuse']) {
      expect(readOf(id, data), id).toMatchObject({ available: false, code: 'notInBundle' });
    }
    // One absent term must not take down a pane of figures that are sound.
    expect(readOf('tokens', data)).toMatchObject({ available: true, value: HARVEST_TOKENS });
    expect(readOf('totalTime', data)).toMatchObject({ available: true });
  });

  it('does not throw on any metric, for any of the shapes above', () => {
    const shapes: ImproveData[] = [
      NO_SCORECARD_BUNDLE,
      wireBundle,
      harvestBundle,
      pricedBundle,
      livePriced,
      partlyPriced,
      // A cost term carrying one half of the token pair, and one carrying a corrupt count.
      liveWith({ cost: { ...sum(LIVE_COST_USD, 1, LIVE_RUNS), tokensUnpriced: UNPRICED_TOKENS } }),
      liveWith({ cost: costSum(LIVE_COST_USD, 1, LIVE_RUNS, Number.NaN, Number.NaN) }),
      withSpend({ modelsSeen: undefined as unknown as number }),
      withSpend({ cost: undefined as unknown as MeasuredSum }),
      withSpend({ costBasis: undefined as unknown as string }),
      withSpend({ tokens: undefined as unknown as MeasuredSum }),
      withSpend({ latencyMs: undefined as unknown as MeasuredSum }),
      { ...NO_SCORECARD_BUNDLE, scorecard: {} as Scorecard },
      withActor([], 0),
      mineUnpriced,
      minePriced,
      mineWide,
      // A widened row whose every new term is the wrong shape, which one malformed wire would send
      // all at once rather than one term at a time.
      withActor([actorRun(1, 1, 1, 1, {
        models: {} as unknown as string[],
        byCategory: null as unknown as NonNullable<ActorRun['spend']['byCategory']>,
        costTokensPriced: Number.NaN,
        costTokensUnpriced: Number.POSITIVE_INFINITY,
      })], 1),
      // A run with no `spend` object at all. The type declares one, and the emitted bundle has
      // already been caught omitting a field `Scorecard` declares required.
      withActor([{ ...actorRun(1, 1), spend: undefined as unknown as ActorRun['spend'] }], 1),
      myDaysUnpriced,
      myDaysWide,
      myDaysNone,
      // A history that is not a list, and one whose rows are not objects: both are what one
      // malformed wire sends, and neither may reach a `reduce`.
      withActor([], 0, { history: 'yesterday' as unknown as ActorDay[] }),
      withDays([null as unknown as ActorDay, 7 as unknown as ActorDay]),
      // A rolled-up bucket that is not an object, and one whose own day count is corrupt.
      withDays([mineDay(0)], { rolledUp: 'earlier' as unknown as ActorBundle['rolledUp'] }),
      withDays([mineDay(0)], { rolledUp: { days: Number.NaN, runs: 'UNMEASURED', tokens: 1 } }),
      // A first date the label cannot parse, which must not become part of a caption.
      withDays([mineDay(0)], { historyFirstDate: 'last Tuesday' }),
    ];
    for (const data of shapes) {
      for (const view of [EVERYONE, YOURS, { scope: 'yours', period: 'trailing30' } as const]) {
        for (const metric of METRICS) {
          // Throwing would take the whole pane down over one malformed term, which is the argument
          // `compactNumber` makes for returning a word instead of raising.
          expect(() => metric.read(data, view), metric.id).not.toThrow();
        }
        // The basis label reads the same rows and must survive the same shapes: a caption that threw
        // would take down a pane of figures that are sound.
        expect(() => describeBasis(data, view)).not.toThrow();
      }
    }
  });

  it('will not show a sum whose denominator the bundle never carried', () => {
    // A term with a `sum` and no `measuredOf`/`total`. The figure is right there and is still not
    // shown: a sum captioned `not a count` reads as authoritative to anyone who does not stop to
    // parse the footnote, which is exactly what carrying the denominator exists to prevent.
    const data = withSpend({ tokens: { sum: HARVEST_TOKENS } as MeasuredSum });
    expect(readOf('tokens', data)).toMatchObject({ available: false, code: 'notInBundle' });
    expect(readOf('tokensPerSession', data)).toMatchObject({ available: false });
  });

  it('gives a value that is not a number the sentinel treatment, not its own spelling', () => {
    const data = withSpend({
      tokens: sum(null as unknown as number, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    });
    // Left as-is, `MeasuredValue` would set the word `null` in the same display type as `211.7M`.
    expect(readOf('tokens', data)).toMatchObject({ value: 'UNMEASURED' });
  });

  it('keeps the flavour of absence the producer actually sent', () => {
    // `NO_DATA`, `UNKNOWN` and `UNMEASURED` carry different tooltips, so collapsing them all to
    // one would tell a reader "no measurement exists" where the truth is "it was never captured".
    const data = withSpend({
      tokens: sum('NO_DATA', 0, HARVEST_SESSIONS),
    });
    expect(readOf('tokens', data)).toMatchObject({ value: 'NO_DATA' });
  });

  it('cannot caption a model count with no population to count it over', () => {
    expect(readOf('modelsUsed', withSpend({ tokens: undefined as unknown as MeasuredSum })))
      .toMatchObject({ available: false, code: 'notInBundle' });
  });
});

// --- reads are pure ----------------------------------------------------------
describe('reads', () => {
  it('answer the same thing twice and leave the bundle untouched', () => {
    // Rows deliberately NEWEST first, which is what a sort could be tempted to fix in place: the wire
    // array belongs to the caller, and mutating it would reorder the evidence half of the same object.
    const data = withDays([mineDay(1), mineDay(0)]);
    const before = JSON.stringify(data);
    for (const view of [EVERYONE, YOURS, { scope: 'yours', period: 'trailing7' } as const]) {
      const first = METRICS.map((m) => m.read(data, view));
      const second = METRICS.map((m) => m.read(data, view));
      expect(second).toEqual(first);
    }
    // Nothing is cached, nothing is sorted in place, and nothing depends on the clock — which is
    // what lets these be tested without a DOM or a fixed date, and what makes a trailing window
    // anchored to the latest row rather than to today.
    expect(JSON.stringify(data)).toBe(before);
  });

  it('produce a share for every ratio metric and for no other, at either scope', () => {
    for (const view of [EVERYONE, YOURS]) {
      for (const metric of METRICS) {
        const reading = metric.read(view.scope === 'yours' ? myDaysWide : harvestBundle, view);
        if (!reading.available) continue;
        expect('ratio' in reading, `${metric.id} ${view.scope}`).toBe(metric.kind === 'ratio');
      }
    }
  });
});
