/**
 * @vitest-environment jsdom
 *
 * What these pin is the block's three jobs and the honesty rule it adds to them: which metrics are
 * on screen, that there are never more than three, that the choice survives a reload — and that a
 * metric with NO reading never looks like one whose reading was `UNMEASURED`.
 *
 * Nothing here re-tests the registry or the tile. Where a figure is asserted it is asserted to
 * prove the block handed the right things over: the entry's own formatter rather than one of its
 * own, and a split's share together with the two side names the entry carries.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below
 * are both required. `@testing-library/user-event` is not a dependency; use fireEvent.
 * `localStorage` persists across tests in a file, so it is cleared before each one — otherwise
 * these pass or fail depending on the order they run in.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_METRIC_IDS, DEFAULT_VIEW, MetricBlock } from './MetricBlock';
import {
  METRICS, PERIODS, PERIOD_LABEL, SCOPES, SCOPE_LABEL, metricLabel,
  type MetricView, type Unavailable,
} from './metricRegistry';
import fixture from './__fixtures__/improveData.fixture.json';
import type {
  ActorBundle, ActorDay, ActorRun, DayCoverage, MeasuredSum, Scorecard,
} from '../../improve-api';
import type { ImproveData } from '../../improve-data';

/** The key every stored preference here is prefixed with (`lab-theme`, `lab-font-size`, …). */
const STORAGE_KEY = 'lab-improve-metrics';

/**
 * A real payload the loop emitted, with NO `scorecard` key at all.
 *
 * It is NOT what production serves today: the live staging SNAPSHOT carries a full scorecard — a
 * population, both judged tiers, every spend term, and a per-model roster (read 2026-09-03). What this
 * bundle is is a bundle the producer WROTE and still serves back unchanged wherever one was stored
 * before the scorecard existed, which is the case the block has to answer for: every scorecard metric
 * reads `noScorecard` and both personal ones read `noActorBundle`, the whole block structurally
 * unavailable from one cause per group. Imported rather than transcribed, because the omission is the
 * point and a hand-written bundle would be free to forget it.
 */
const noScorecardBundle = fixture as unknown as ImproveData;

/*
 * Every magnitude below is measured, transcribed from two read-only artifacts outside the repo
 * tree. The registry's own tests carry the same transcription; a block tested on round numbers
 * would pass while mis-setting every real one.
 *
 * `HARVEST_*`: sums over a 336-hour staging harvest of 553 sessions, 354 of which carry a `usage`
 * block. The four category sums add to the token total exactly (Bedrock's own arithmetic), so a
 * split over them has no residual. Busy time is measured on all 553, which is what makes this
 * bundle useful here: two different denominators in one row.
 * `WIRE_*`: a real emitted bundle's `scorecard.spend`, where latency is measured on 6 of 6 and
 * every token term is `UNMEASURED` with `measuredOf: 0`.
 */
const HARVEST_SESSIONS = 553;
const HARVEST_SESSIONS_WITH_USAGE = 354;
const HARVEST_TOKENS = 211_662_174;
const HARVEST_CALLS = 5_406;
const HARVEST_INPUT = 22_163;
const HARVEST_OUTPUT = 3_616_525;
const HARVEST_CACHE_READ = 157_490_856;
const HARVEST_CACHE_WRITE = 50_532_630;
/** Distinct Bedrock model ids the harvest's tokens were spent on. */
const HARVEST_MODELS = 5;
/** Every harvested span's `duration_ms`, summed per session and then over the population. */
const HARVEST_LATENCY_MS = 412_524_975;
/** Everything sent TO a model: uncached input plus both cache directions. */
const HARVEST_CONTEXT = HARVEST_INPUT + HARVEST_CACHE_READ + HARVEST_CACHE_WRITE;

const WIRE_RUNS = 6;
const WIRE_LATENCY_MS = 12_182_621;
/**
 * A stand-in for the producer's `costBasis`, deliberately short and obviously not its wording.
 *
 * The real sentences run to five and six hundred characters and are transcribed, in full and
 * asserted in full, in the REGISTRY's own tests — one copy, in the file whose job is verbatim
 * fidelity. A second copy here would read as a fixture while being an unchecked snapshot of a
 * producer string, free to go on describing a producer that has moved on.
 */
const COST_BASIS = 'estimate from hand-entered rates';

/**
 * The same stand-in, long enough that the footnote must abridge it — which is one of the two
 * conditions under which a caveat exists at all. The other is a per-model roster, below.
 *
 * Shaped like the producer's real 839-character sentence and carrying the four facts that live past
 * the footnote's cut: the price page, the date the rates were read off it by hand, the region check,
 * and the cache-write TTL the figure assumes. Not transcribed, for `COST_BASIS`'s reason above — the
 * character-exact match against the producer belongs in the registry's tests, and what is under test
 * here is that a long string survives the trip to a tooltip and a picker row untouched.
 */
const LONG_COST_BASIS = 'ESTIMATE, not billed truth: computed per model at rates from '
  + 'https://aws.amazon.com/bedrock/pricing/, read by hand on 2026-08-31 — that page quotes these '
  + 'models identically in every region, checked, and the cache-write figure is the 5-MINUTE-TTL '
  + 'one, so a run that opted into the 1-hour TTL costs up to 1.6x what is reported here.';

/** The four facts a reader needs to audit a hand-entered rate rather than merely believe it. Each
 *  sits past character 26, where the tile's footnote stops. */
const AUDIT_FACTS = [
  'https://aws.amazon.com/bedrock/pricing/',
  'read by hand on 2026-08-31',
  'identically in every region, checked',
  '1.6x what is reported here',
];

/**
 * Two real priced sessions from the same harvest, at the rates the producer ships: an Opus-4.8
 * session (4 uncached input tokens, 831 output, 60,671 cache read, 29,064 cache write) and a
 * two-model one. Priced on TWO of the 553 sessions, against 354 that recorded tokens — a session no
 * model of which the rate table can price is not priced at all, so the two denominators diverge on
 * one bundle.
 */
const PRICED_TOTAL_USD = 0.2327805 + 2.7514315;
const PRICED_SESSIONS = 2;

/**
 * Today's live staging population: 9 runs, 8 of them with usage, $16.74 over 6,813,939 tokens on
 * two models the rate table prices in full.
 *
 * And the same population with one model it has no row for. SYNTHETIC, because every model the live
 * population uses is priced — only the model id is invented; the 90,570 tokens charged to it are
 * one real session's own counts (the Opus-4.8 session above). Every session that recorded usage is
 * still priced, so the RUN denominator is identical on both, and one of the two figures is short.
 */
const LIVE_RUNS = 9;
const LIVE_RUNS_WITH_USAGE = 8;
const LIVE_TOKENS = 6_813_939;
const LIVE_COST_USD = 16.74;
const UNPRICED_TOKENS = 90_570;

function sum(value: MeasuredSum['sum'], measuredOf: number, total: number): MeasuredSum {
  return { sum: value, measuredOf, total };
}

const UNMEASURED_OVER_WIRE = sum('UNMEASURED', 0, WIRE_RUNS);

/**
 * The signed-in reader's own two runs, transcribed from a staging bundle.
 *
 * Constructed HERE rather than added to the committed fixture, which genuinely has no `actor` key —
 * that omission is the shape production serves and several tests below rest on it, so a fixture
 * quietly given a personal bundle would misrepresent the wire and disarm them.
 *
 * Real magnitudes, because the block's job is to hand each figure to the right formatter and round
 * numbers hide two of the three answers: a busy-time sum in the millions of ms is what needs days
 * and hours rather than minutes, and a price of a few dollars is what needs cents rather than the
 * count formatter's raw float.
 */
const MINE_TOKENS = [1_961_718, 4_611_553];
const MINE_CALLS = [44, 83];
const MINE_LATENCY_MS = [1_705_441, 7_398_989];
const MINE_RUNS = 2;
/** The two real per-session prices from this same harvest, one per run. */
const MINE_COST_USD = [0.2327805, 2.7514315];
/** The two days those two sessions ran on. Four apart rather than adjacent, so a trailing window is
 *  testable at all — two consecutive days fall inside every span. */
const MINE_DATES = ['2026-08-30', '2026-09-03'];

/** One run, carrying only the spend the personal metrics read. */
function actorRun(spend: ActorRun['spend']): ActorRun {
  return {
    runId: 'run',
    workloadClass: 'conversational',
    window: 'UNKNOWN',
    verdict: { overall: 'PASS', reason: '' },
    tiers: {
      dispatch: { score: 1, outcome: 'PASS', explanation: '' },
      delivery: { score: 1, outcome: 'PASS', explanation: '' },
    },
    spend,
  };
}

/**
 * How many of a day's sessions recorded each term, in the shape `improve/dto._day_row` emits: one
 * session count for the whole day, and a per-term count beside it.
 *
 * DERIVED from the day's spend, which is what the producer does — it counts the sessions whose record
 * carried the term — so a fixture cannot state a sum and forget to count it. These days hold ONE session
 * each, since neither transcribed session spawned a plan sub-session, and `total` is a SESSION count
 * rather than the row's `runs`, which counts requests.
 *
 * A term nothing recorded is `0`, a measurement, and the whole block being absent is a different fact:
 * that is a row written before the counts existed, and the footnote states no fraction for it at all.
 */
function dayCoverage(spend: ActorRun['spend'] | undefined): DayCoverage {
  const counted = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? 1 : 0);
  return {
    total: 1,
    tokens: counted(spend?.tokens),
    llmCalls: counted(spend?.llmCalls),
    latencyMs: counted(spend?.latencyMs),
    cost: counted(spend?.cost),
    costTokensPriced: counted(spend?.costTokensPriced),
    costTokensUnpriced: counted(spend?.costTokensUnpriced),
    byCategory: {
      input: counted(spend?.byCategory?.input),
      output: counted(spend?.byCategory?.output),
      cacheRead: counted(spend?.byCategory?.cacheRead),
      cacheWrite: counted(spend?.byCategory?.cacheWrite),
    },
  };
}

/**
 * One day per run, carrying that run's own spend.
 *
 * Which is what the transcribed pair really is — those two staging sessions ran on two different days
 * — so nothing here is apportioned. Derived from the same runs rather than written out beside them,
 * so a fixture cannot describe a reader whose days and whose runs disagree, and so every figure
 * asserted below is the same magnitude whichever half it came off.
 *
 * Terms the run row does not carry come through as `undefined`, which is the third state and the one
 * the personal figures must report as absent rather than as zero.
 */
function daysFrom(runs: readonly ActorRun[]): ActorDay[] {
  return runs.map((run, i) => {
    const spend = run.spend;
    return {
      date: MINE_DATES[i],
      runs: 1,
      tokens: spend?.tokens,
      llmCalls: spend?.llmCalls,
      latencyMs: spend?.latencyMs,
      cost: spend?.cost,
      costTokensPriced: spend?.costTokensPriced,
      costTokensUnpriced: spend?.costTokensUnpriced,
      byCategory: spend?.byCategory,
      models: spend?.models,
      coverage: dayCoverage(spend),
    };
  });
}

/**
 * An actor bundle around a list of runs, and the day rows those runs make up.
 *
 * BOTH halves, because the wire carries both and they are read for different things: the day rows are
 * what the personal totals are summed from, and the run rows are the evidence half the suggestion
 * cards read. A bundle with only one of them would let a figure quietly go back to the other source
 * and still pass.
 *
 * `runs` and `runsTotal` are separate because a bundle is trimmed to fit one DynamoDB item.
 */
function actorWith(runs: ActorRun[], over: Partial<ActorBundle> = {}): ActorBundle {
  return {
    runs,
    runsTotal: MINE_RUNS,
    evidence: {},
    waste: {
      runsWithWaste: 0, runs: MINE_RUNS, runsNotScanned: 0, totalCalls: 0,
      totalWallMs: sum('UNMEASURED', 0, MINE_RUNS), byKind: {}, findings: [],
    },
    patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
    history: daysFrom(runs),
    ...over,
  };
}

/** The staging pair, priced. */
const pricedRuns = MINE_TOKENS.map((tokens, i) => actorRun({
  latencyMs: MINE_LATENCY_MS[i], tokens, cost: MINE_COST_USD[i], llmCalls: MINE_CALLS[i],
}));

/**
 * The same pair with the four WIDER spend terms the producer now projects onto a run row.
 *
 * Each run's four categories sum to its own token total, `input` being the residual exactly as the
 * producer derives it. The share between the categories is apportioned by the test in the
 * population's own proportions — the transcription carries these runs' totals and the population's
 * category sums, not each run's — and the magnitudes are the reader's real ones. The registry's tests
 * carry the same numbers and the same note; what is at stake here is only whether a personal split's
 * share and its two side names reach the bar.
 */
const MINE_INPUT = [61, 173];
const MINE_OUTPUT = [33_000, 84_000];
const MINE_CACHE_READ = [1_330_000, 3_125_000];
const MINE_CACHE_WRITE = [598_657, 1_402_380];
const MINE_MODELS = ['global.anthropic.claude-opus-4-8', 'global.anthropic.claude-sonnet-5'];

const wideRuns = MINE_TOKENS.map((tokens, i) => actorRun({
  latencyMs: MINE_LATENCY_MS[i],
  tokens,
  cost: MINE_COST_USD[i],
  llmCalls: MINE_CALLS[i],
  byCategory: {
    input: MINE_INPUT[i],
    output: MINE_OUTPUT[i],
    cacheRead: MINE_CACHE_READ[i],
    cacheWrite: MINE_CACHE_WRITE[i],
  },
  models: MINE_MODELS,
  costTokensPriced: tokens,
  costTokensUnpriced: 0,
}));

const sumOf = (values: readonly number[]) => values.reduce((a, b) => a + b, 0);

/**
 * A bundle carrying a given `spend`, and nothing else a metric can reach.
 *
 * Only `scorecard.spend` is filled: these metrics read no other part of a scorecard, and the real
 * emitted bundle already omits a field `Scorecard` declares as required, so a partial object is
 * the shape that arrives rather than a shortcut. Hence the double cast — the compiler will not
 * bridge that omission, which is the fact worth recording.
 */
function bundleWith(spend: Scorecard['spend'], actor?: ActorBundle): ImproveData {
  return { ...noScorecardBundle, scorecard: { spend } as unknown as Scorecard, actor };
}

/** A real emitted bundle: busy time measured on all 6 runs, every token term UNMEASURED. */
const wireBundle = bundleWith({
  tokens: UNMEASURED_OVER_WIRE,
  cost: UNMEASURED_OVER_WIRE,
  costBasis: COST_BASIS,
  latencyMs: sum(WIRE_LATENCY_MS, WIRE_RUNS, WIRE_RUNS),
  byCategory: {
    input: UNMEASURED_OVER_WIRE,
    output: UNMEASURED_OVER_WIRE,
    cacheRead: UNMEASURED_OVER_WIRE,
    cacheWrite: UNMEASURED_OVER_WIRE,
  },
  llmCalls: UNMEASURED_OVER_WIRE,
  modelsSeen: 0,
});

/**
 * The 336-hour harvest's own sums, plus the reader's own priced slice of it.
 *
 * Both halves, because the block's default three come off the ACTOR bundle while the picker offers
 * eight that come off the scorecard — so this is the one shape where every row in the picker is
 * readable, and it is what most tests below need in order to be about the block rather than about
 * an absence. Tokens on 354 of 553 sessions, busy time on all 553.
 */
const harvestBundle = bundleWith({
  tokens: sum(HARVEST_TOKENS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  cost: sum('UNMEASURED', 0, HARVEST_SESSIONS),
  costBasis: COST_BASIS,
  latencyMs: sum(HARVEST_LATENCY_MS, HARVEST_SESSIONS, HARVEST_SESSIONS),
  byCategory: {
    input: sum(HARVEST_INPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    output: sum(HARVEST_OUTPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheRead: sum(HARVEST_CACHE_READ, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheWrite: sum(HARVEST_CACHE_WRITE, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  },
  llmCalls: sum(HARVEST_CALLS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  modelsSeen: HARVEST_MODELS,
}, actorWith(pricedRuns));

/**
 * The same harvest, with a personal bundle whose one run recorded no usage at all.
 *
 * A real shape rather than an awkward one: a run with no `usage` block gets the sentinel on tokens,
 * cost and call count together, which is exactly the three default metrics. So all three are
 * AVAILABLE and all three read `UNMEASURED` — the row that looks as empty as a collapsed one and is
 * a different fact.
 */
const unmeasuredMineBundle = bundleWith(harvestBundle.scorecard!.spend, actorWith([
  actorRun({ latencyMs: 'UNMEASURED', tokens: 'UNMEASURED', cost: 'UNMEASURED',
             llmCalls: 'UNMEASURED' }),
]));

/** The same harvest with cost priced, so the row carries a figure in dollars beside two in
 *  tokens — three units in one row, which is what catches a block that formats them itself. */
const pricedBundle = bundleWith({
  tokens: sum(HARVEST_TOKENS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  cost: sum(PRICED_TOTAL_USD, PRICED_SESSIONS, HARVEST_SESSIONS),
  costBasis: COST_BASIS,
  latencyMs: sum(HARVEST_LATENCY_MS, HARVEST_SESSIONS, HARVEST_SESSIONS),
  byCategory: {
    input: sum(HARVEST_INPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    output: sum(HARVEST_OUTPUT, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheRead: sum(HARVEST_CACHE_READ, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
    cacheWrite: sum(HARVEST_CACHE_WRITE, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  },
  llmCalls: sum(HARVEST_CALLS, HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS),
  modelsSeen: HARVEST_MODELS,
});

/**
 * The live population's cost term, in full and short, and nothing else.
 *
 * Only `cost` and `costBasis` are filled: the one metric these two bundles are for reads no other
 * part of a spend, and a fixture carrying terms nobody reads would invite a reader to check them
 * against a population they do not come from. `bareScorecard` below takes the same shape.
 */
const livePricedBundle = bundleWith({
  cost: { ...sum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
          tokensPriced: LIVE_TOKENS, tokensUnpriced: 0 },
  costBasis: COST_BASIS,
} as Scorecard['spend']);

const partlyPricedBundle = bundleWith({
  cost: { ...sum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
          tokensPriced: LIVE_TOKENS, tokensUnpriced: UNPRICED_TOKENS },
  costBasis: COST_BASIS,
} as Scorecard['spend']);

/** The live population again, with a basis too long for the footnote — the shape production actually
 *  serves, and one of the two where a caveat has anything to carry. */
const longBasisBundle = bundleWith({
  cost: { ...sum(LIVE_COST_USD, LIVE_RUNS_WITH_USAGE, LIVE_RUNS),
          tokensPriced: LIVE_TOKENS, tokensUnpriced: 0 },
  costBasis: LONG_COST_BASIS,
} as Scorecard['spend']);

/**
 * The live staging population's per-model roster, transcribed from the SNAPSHOT on 2026-09-03.
 *
 * The other caveat-carrying shape, and the reason it needs one: the roster names an id, a magnitude, a
 * share and a denominator per model, which is a sentence per row and cannot be a line under a figure.
 * So the footnote takes the head — which model is most of the bill — and the roster rides the two
 * channels a long caveat has always ridden.
 *
 * Real magnitudes, and lopsided ones: one of two ids is 97% of the tokens over 14 of the 25 runs that
 * recorded usage. A 50/50 pair would pass a block that handed over the wrong reading.
 */
const ROSTER_RUNS = 25;
const ROSTER_TOKENS = 10_320_108;
const ROSTER_BIGGEST = 'global.anthropic.claude-opus-4-8';
const rosterBundle = bundleWith({
  tokens: sum(ROSTER_TOKENS, ROSTER_RUNS, ROSTER_RUNS),
  modelsSeen: 2,
  byModel: [
    { model: ROSTER_BIGGEST,
      tokens: sum(9_984_406, 14, ROSTER_RUNS),
      byCategory: {
        input: sum(500, 14, ROSTER_RUNS),
        output: sum(154_077, 14, ROSTER_RUNS),
        cacheRead: sum(7_487_665, 14, ROSTER_RUNS),
        cacheWrite: sum(2_342_164, 14, ROSTER_RUNS),
      },
      llmCalls: sum(250, 14, ROSTER_RUNS) },
    { model: 'global.anthropic.claude-sonnet-5',
      tokens: sum(335_702, 11, ROSTER_RUNS),
      byCategory: {
        input: sum(44, 11, ROSTER_RUNS),
        output: sum(38_042, 11, ROSTER_RUNS),
        cacheRead: sum(19_338, 11, ROSTER_RUNS),
        cacheWrite: sum(278_278, 11, ROSTER_RUNS),
      },
      llmCalls: sum(22, 11, ROSTER_RUNS) },
  ],
  // A measured zero count beside an unmeasured sum, exactly as the live item carries it: the bound
  // withheld nothing, so there were no values to add.
  byModelOther: { models: 0, tokens: sum('UNMEASURED', 0, ROSTER_RUNS) },
} as Scorecard['spend']);

// --- helpers ----------------------------------------------------------------
/** The view the block opens on, so a helper reading the registry directly asks it the same question
 *  the block does. */
const OPENS_ON: MetricView = { scope: 'yours', period: 'allTime' };

/**
 * The whole of what the registry says about a metric it cannot read from this bundle, at the view the
 * block is showing.
 *
 * Asked of the registry rather than transcribed, for the reason `caveatFor` gives below: a cause has
 * two spellings now — the sentence and the phrase — and a copy typed in here would pass while the
 * block printed the other one.
 */
function causeFor(id: string, data: ImproveData, view: MetricView = OPENS_ON): Unavailable {
  const entry = METRICS.find((m) => m.id === id);
  if (!entry) throw new Error(`no metric ${id}`);
  const reading = entry.read(data, view);
  if (reading.available) throw new Error(`${id} is available in this bundle`);
  return reading;
}

/** The reason the registry gives for a metric it cannot read from this bundle, at the view the block
 *  is showing. */
function reasonFor(id: string, data: ImproveData, view: MetricView = OPENS_ON): string {
  return causeFor(id, data, view).reason;
}

/**
 * The whole qualification the registry hangs on a readable figure.
 *
 * Asked of the registry rather than transcribed, so what is under test is that the block hands the
 * SAME string to both channels — a copy typed in here would pass while the tooltip and the picker row
 * drifted apart, which is the one thing having two channels is supposed to prevent.
 */
function caveatFor(id: string, data: ImproveData, view: MetricView = OPENS_ON): string {
  const entry = METRICS.find((m) => m.id === id);
  if (!entry) throw new Error(`no metric ${id}`);
  const reading = entry.read(data, view);
  if (!reading.available || reading.caveat === undefined) {
    throw new Error(`${id} carries no caveat in this bundle`);
  }
  return reading.caveat;
}

function store(choice: unknown): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(choice));
}

function stored(): unknown {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw === null ? null : JSON.parse(raw);
}

/**
 * Every slot's label, in the order the row lays them out.
 *
 * Both kinds of slot are collected — a figure and a bare reason are both a metric on screen, and
 * the whole point of several tests below is which kind each one is.
 */
function slotLabels(): string[] {
  return Array.from(
    screen.getByTestId('metric-row').querySelectorAll(
      '[data-testid="metric-tile-label"],[data-testid="metric-unavailable-label"]',
    ),
  ).map((el) => el.textContent ?? '');
}

const openPicker = () => fireEvent.click(screen.getByTestId('metric-picker-toggle'));
const option = (id: string) => screen.getByTestId(`metric-option-${id}`) as HTMLInputElement;
/** A plain click is the whole interaction: jsdom flips `checked` and dispatches the change React
 *  listens for. Overriding `target.checked` on the event fights that flip and lands the opposite
 *  value in the handler. */
const toggle = (id: string) => fireEvent.click(option(id));

const DEFAULT_LABELS = ['Your tokens', 'Your calls', 'Your cost'];

/** The shared scope, for a helper that has to ask the registry the same question the block is
 *  showing when the switch has been moved. */
const SHARED_VIEW: MetricView = { scope: 'everyone', period: 'allTime' };

/**
 * The reader's days with the price term absent from every row, which is a live shape: a day row is
 * written once and carried forward, so rows from before the producer priced anything keep the shorter
 * shape. It is also the longest per-tile reason on the surface, which is what several tests below
 * need to exercise abridging at all.
 */
const noPriceDaysBundle = bundleWith(harvestBundle.scorecard!.spend, actorWith(pricedRuns, {
  history: daysFrom(pricedRuns).map(({ cost, ...rest }) => rest),
}));

/** The two transcribed days as the basis label spells them. */
const MINE_DAY_ONE = '30 Aug';
const MINE_DAY_TWO = '3 Sep';

/** The harvest, with `attribution` as today's live population reports it: 9 runs, 2 owned, by one
 *  actor. The one shape on which the scope sentence has anything to say. */
const scopedBundleWithAttribution: ImproveData = {
  ...noScorecardBundle,
  actor: actorWith(wideRuns),
  scorecard: {
    spend: harvestBundle.scorecard!.spend,
    attribution: { actors: 1, attributed: 2, unattributable: 0, ownable: 2, runs: 9 },
  } as unknown as Scorecard,
};

beforeEach(() => {
  localStorage.clear();
});

// --- which three ------------------------------------------------------------
describe('MetricBlock — the selection', () => {
  it('opens on the reader’s own three, in order, when nothing has been stored', () => {
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
    // Three that answer different questions — a total, a count and a price — rather than three views
    // of the same spend. The scope word in front of each comes off the switch, not off the id.
    expect(DEFAULT_METRIC_IDS).toEqual(['tokens', 'llmCalls', 'cost']);
  });

  it('opens on the reader’s own figures, not on the platform-wide sums beside them', () => {
    /*
     * The whole point of the default view. This bundle carries BOTH — a 553-session platform
     * scorecard and the reader's own two days — and the figures on screen are the reader's: 6.6M
     * tokens against the population's 211.7M, over 2 of 2 of the reader's own sessions rather than
     * 354 of 553 of everyone's. A block still defaulting to the shared scorecard passes every other
     * test in this file and fails here.
     */
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['6.6M', '127', '$2.98']);
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent))
      .toEqual(Array(3).fill(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`));
    // And the platform-wide figures are one CLICK away rather than a menu away: the same eight rows
    // answer for either scope, so nothing has to be chosen to reach them.
    expect(screen.getByTestId('metric-scope-everyone')).toBeEnabled();
  });

  it('renders a stored selection instead, in the order it was stored', () => {
    // Registry order puts `tokens` ahead of `totalTime`, so a block that sorted by the registry
    // rather than honouring the stored order would put tokens first.
    store({ metrics: ['totalTime', 'modelsUsed', 'tokens'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your time', 'Your models', 'Your tokens']);
  });

  it('persists the choice under lab-improve-metrics, as registry ids', () => {
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    toggle('cost');
    toggle('totalTime');

    expect(stored()).toEqual({ metrics: ['tokens', 'llmCalls', 'totalTime'] });
    expect(slotLabels()).toEqual(['Your tokens', 'Your calls', 'Your time']);
  });

  it('reads back what it wrote, so a reload shows the same three', () => {
    // Read and write going to different keys, or writing labels and reading ids, would each pass
    // the two tests above and fail this one.
    const first = render(<MetricBlock data={harvestBundle} />);
    openPicker();
    toggle('tokens');
    toggle('totalTime');
    first.unmount();

    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your calls', 'Your cost', 'Your time']);
  });
});

// --- changing the default set, for readers who never chose one ---------------
describe('MetricBlock — never chose against chose', () => {
  /*
   * The trap in changing `DEFAULT_METRIC_IDS` at all: `readSelection` honours a stored list, so a
   * new default reaches nobody who has one. What makes that correct rather than a missing migration
   * is that the key is written ONLY by a deliberate choice — so absent means "never chose", and a
   * reader who never chose gets the new three. These pin the invariant that distinction rests on,
   * because a stray write anywhere in the block would freeze every existing reader on whatever
   * default was current at their next mount, silently and permanently.
   */
  it('writes nothing at all until a row is actually checked or unchecked', () => {
    const { unmount } = render(<MetricBlock data={harvestBundle} />);
    expect(stored()).toBeNull();
    // Opening the picker is not choosing. Nor is closing it, nor unmounting.
    openPicker();
    expect(stored()).toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });
    unmount();
    expect(stored()).toBeNull();
  });

  it('writes nothing when the cap or the floor refuses the toggle', () => {
    // A refused toggle must not record the selection it left alone: that would turn the accident of
    // clicking a disabled row into a permanent opt-out of every future default.
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    toggle('totalTime');
    expect(stored()).toBeNull();

    toggle('llmCalls');
    toggle('cost');
    expect(slotLabels()).toEqual(['Your tokens']);
    toggle('tokens');
    expect(stored()).toEqual({ metrics: ['tokens'] });
  });

  it('honours a stored list that happens to equal a former default set', () => {
    /*
     * Why no migration ships. A reader who deliberately picked the three platform-wide metrics
     * stores exactly the list an old default would have written, so nothing can tell the two apart
     * — and a migration that rewrote it would silently discard a choice someone made. Keeping it is
     * the only answer that is never wrong about intent.
     */
    store({ metrics: ['cacheReuse', 'totalTime', 'contextVsGenerated'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels())
      .toEqual(['Your cache reuse', 'Your time', 'Your context vs generated']);
    expect(stored()).toEqual({ metrics: ['cacheReuse', 'totalTime', 'contextVsGenerated'] });
  });
});

// --- stored state is untrusted input ----------------------------------------
describe('MetricBlock — stored state it cannot trust', () => {
  it('drops an id that is no longer in the registry and tops up the remainder', () => {
    // `yourTokens` is one of the eight ids the sixteen-entry registry had and this one does not, so
    // this is the migration as well as a hostile-shape check: an id that went away is dropped and
    // topped up rather than migrated to its surviving twin.
    store({ metrics: ['totalTime', 'yourTokens', 'promptCacheDepth'] });
    render(<MetricBlock data={harvestBundle} />);
    // The surviving choice is kept — discarding the whole list would lose it — and the two gaps
    // are filled from the default set in its own order.
    expect(slotLabels()).toEqual(['Your time', 'Your tokens', 'Your calls']);
    // No slot rendered blank: a stale id must never reach the row.
    for (const label of slotLabels()) expect(label.length).toBeGreaterThan(0);
  });

  it('ignores stored text that is not JSON at all', () => {
    localStorage.setItem(STORAGE_KEY, '{"metrics": [');
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
  });

  it('ignores a stored value that is valid JSON but not an array', () => {
    // A bare string would survive a `parsed.filter`-shaped guard by iterating its characters.
    store('tokens');
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
  });

  it('drops members that are not strings', () => {
    store({ metrics: [1, null, 'totalTime', { id: 'tokens' }] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your time', 'Your tokens', 'Your calls']);
  });

  it('keeps only the first three of a longer stored list', () => {
    // The cap is the block's rule; a stored preference does not get to raise it.
    store({ metrics: ['totalTime', 'modelsUsed', 'tokensPerSession', 'tokens', 'cacheReuse'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your time', 'Your models', 'Your tokens per session']);
  });

  it('drops a repeated id rather than showing one metric twice', () => {
    store({ metrics: ['totalTime', 'totalTime', 'llmCalls'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your time', 'Your calls', 'Your tokens']);
    expect(slotLabels().filter((label) => label === 'Your time')).toHaveLength(1);
  });

  it('falls back when the stored list is empty', () => {
    // Indistinguishable from a truncated or half-finished write, and a metric block with no
    // metrics in it is not one — so this is treated as corrupt, which is also why the picker
    // refuses to let a user reach it.
    store({ metrics: [] });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
  });

  it('still renders when localStorage cannot be read', () => {
    // Safari private mode, or quota. A pane that dies because a preference could not be read is
    // worse than one that ignores the preference.
    // On the prototype, not on `window.localStorage`: jsdom's storage is a proxy and a spy
    // installed on the instance is never the method the component's call resolves to.
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
    expect(getItem).toHaveBeenCalled();
    getItem.mockRestore();
  });

  it('still applies a choice when localStorage cannot be written', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError');
    });
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    toggle('cost');
    toggle('totalTime');

    try {
      expect(setItem).toHaveBeenCalled();
      // The choice holds for this session even though nothing recorded it.
      expect(slotLabels()).toEqual(['Your tokens', 'Your calls', 'Your time']);
    } finally {
      // In a `finally`, because a spy on `Storage.prototype` left installed by a failing assertion
      // makes every later test in the file throw `QuotaExceededError` from its own `store()` call —
      // one broken expectation reads as twenty unrelated ones.
      setItem.mockRestore();
    }
  });
});

// --- the cap ----------------------------------------------------------------
describe('MetricBlock — three, and not four', () => {
  it('disables the unchosen rows once three are chosen, and leaves the chosen ones alone', () => {
    render(<MetricBlock data={harvestBundle} />);
    openPicker();

    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.filter((box) => box.checked)).toHaveLength(3);
    expect(boxes.filter((box) => box.disabled).every((box) => !box.checked)).toBe(true);
    expect(boxes.filter((box) => box.disabled)).toHaveLength(METRICS.length - 3);
  });

  it('refuses rather than swapping one out silently', () => {
    /*
     * The distinction the cap exists for. A swap would be indistinguishable from a bug from where
     * the reader sits: the tile that disappeared is the one they were looking at, and nothing on
     * screen says which of their three choices paid for the fourth.
     */
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    // The visible refusal.
    expect(option('totalTime').disabled).toBe(true);
    /*
     * And the one underneath it. A dispatched click reaches a disabled input's handler in jsdom
     * where a real browser would never deliver one, which makes this the only place the state
     * guard can be exercised — and the guard is what keeps the rule true if the row ever stops
     * being a native control.
     */
    toggle('totalTime');
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
    expect(stored()).toBeNull();
  });

  it('frees a slot as soon as one is unchecked', () => {
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    expect(option('totalTime').disabled).toBe(true);

    toggle('tokens');
    expect(option('totalTime').disabled).toBe(false);
    toggle('totalTime');
    expect(slotLabels()).toEqual(['Your calls', 'Your cost', 'Your time']);
  });

  it('will not let the last metric be unchecked, so the row can never empty', () => {
    /*
     * The floor is the cap's other end, and it is what makes an empty stored array unambiguously
     * corrupt rather than possibly deliberate: nothing a user can do in here produces one.
     */
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    toggle('llmCalls');
    toggle('cost');

    expect(slotLabels()).toEqual(['Your tokens']);
    expect(option('tokens').disabled).toBe(true);
    // Same pair as the cap: the row says no, and the handler holds even when something gets past
    // it. Nothing a user can do here writes an empty selection.
    toggle('tokens');
    expect(slotLabels()).toEqual(['Your tokens']);
    expect(stored()).toEqual({ metrics: ['tokens'] });
  });

  it('keeps three tracks in the row however few metrics are shown', () => {
    // Deriving the template from the slot count would let a single tile stretch across the pane
    // and make the row's geometry jump every time a metric is swapped.
    render(<MetricBlock data={harvestBundle} />);
    const row = () => screen.getByTestId('metric-row');
    // A bare `1fr` is the layout bug: a track's default minimum is its own content, so the real
    // footnote `39.2k tokens per call · 354 of 553 sessions` would widen its column.
    expect(row().style.gridTemplateColumns).toBe('repeat(3, minmax(0, 1fr))');

    openPicker();
    toggle('llmCalls');
    expect(slotLabels()).toHaveLength(2);
    expect(row().style.gridTemplateColumns).toBe('repeat(3, minmax(0, 1fr))');
  });
});

// --- no reading is not an unmeasured reading --------------------------------
describe('MetricBlock — absent against unmeasured', () => {
  it('marks a metric with no reading differently from one nothing could measure', () => {
    /*
     * Both look empty and they are not the same fact. `tokens` and `cost` on this bundle both HAVE
     * a reading — the producer ran and could measure neither — and get the tile's hatched sentinel.
     * `tokensPerSession` has no reading at all, because there is no denominator to divide over, and
     * must not be routed through a tile with `UNMEASURED` in place of a value: that would claim an
     * arithmetic was attempted where none could be.
     */
    store({ metrics: ['tokens', 'cost', 'tokensPerSession'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);

    expect(screen.getAllByTestId('metric-tile')).toHaveLength(2);
    const unavailable = screen.getByTestId('metric-unavailable');
    expect(unavailable).toHaveTextContent("Everyone's tokens per session");
    expect(unavailable.querySelector('[data-testid="sentinel"]')).toBeNull();
    expect(screen.getByTestId('unavailable-badge')).toHaveTextContent('unavailable');
    // The sentinel is still used where it belongs: neither term was measured on any of 6 sessions.
    expect(screen.getAllByTestId('sentinel').map((el) => el.textContent))
      .toEqual(['not measured', 'not measured']);
  });

  it('puts no number of any kind on a metric that has no reading', () => {
    // `compactNumber` answers a non-number with the words `not a count`. A slot that reached for a
    // formatter at all would print those in display type beside a real figure.
    store({ metrics: ['tokensPerSession', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);
    const unavailable = screen.getByTestId('metric-unavailable');
    expect(unavailable.textContent).not.toContain('not a count');
    expect(unavailable.querySelector('[data-testid="metric-tile-value"]')).toBeNull();
  });

  it('hangs the reason on the whole slot, not on the shared badge inside it', () => {
    /*
     * The badge is the surface's marker and says one word for every metric, so the sentence has to
     * come from here. On the SLOT rather than on the badge: hovering the one part of a figure-less
     * tile that says `unavailable` is how a reader asks why, and a generic title on the shared badge
     * would answer that with something no read decided.
     */
    store({ metrics: ['tokensPerSession', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);
    expect(screen.getByTestId('metric-unavailable'))
      .toHaveAttribute('title', reasonFor('tokensPerSession', wireBundle, SHARED_VIEW));
    expect(screen.getByTestId('unavailable-badge')).not.toHaveAttribute('title');
    // The badge sits inside the element carrying the sentence, so pointing at it resolves to it.
    expect(screen.getByTestId('metric-unavailable'))
      .toContainElement(screen.getByTestId('unavailable-badge'));
  });

  it('keeps a long reason reachable in full while abridging what it shows', () => {
    /*
     * `cost` on day rows that predate it, because that is the longest reason a single tile gets: a row
     * is written once and carried forward, so saying why an old one is short takes 215 characters
     * against the 120 a slot has room for. Two available metrics beside it, so the row does not
     * collapse onto one shared cause instead.
     */
    store({ metrics: ['cost', 'tokens', 'llmCalls'] });
    render(<MetricBlock data={noPriceDaysBundle} />);

    const reason = reasonFor('cost', noPriceDaysBundle);
    const preview = screen.getByTestId('metric-unavailable-reason').textContent ?? '';
    // Verbatim, character for character, on the slot — nothing is lost, only deferred.
    expect(screen.getByTestId('metric-unavailable')).toHaveAttribute('title', reason);
    expect(reason.length).toBeGreaterThan(120);
    expect(preview.length).toBeLessThan(reason.length);
    expect(preview.endsWith('…')).toBe(true);
    // What is shown is a prefix of what was said, not a rewrite of it, and it stops on a word.
    expect(reason.startsWith(preview.slice(0, -1))).toBe(true);
    expect(preview.slice(0, -1).endsWith(' ')).toBe(false);
  });

  it('shows a reason that fits without abridging it', () => {
    // Truncation is conditional. `tokensPerSession` cannot divide over zero measured sessions, and
    // saying so takes one short line.
    store({ metrics: ['tokensPerSession', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);
    const reason = reasonFor('tokensPerSession', wireBundle, SHARED_VIEW);
    expect(reason).toContain('0 of 6');
    expect(screen.getByTestId('metric-unavailable-reason')).toHaveTextContent(reason);
    expect(screen.getByTestId('metric-unavailable-reason').textContent).not.toContain('…');
  });

  it('prints every reason unabridged in the picker, where a full-width row has room', () => {
    render(<MetricBlock data={noPriceDaysBundle} />);
    openPicker();
    const reason = reasonFor('cost', noPriceDaysBundle);
    // The same sentence the tile above had to abridge, whole here.
    expect(screen.getByTestId('metric-option-reason-cost').textContent).toBe(reason);
    expect(reason.length).toBeGreaterThan(120);
    // And says nothing about a metric it CAN read: a reason line under a working figure would
    // read as a warning about it.
    expect(screen.queryByTestId('metric-option-reason-tokens')).toBeNull();
    expect(screen.queryByTestId('metric-option-reason-llmCalls')).toBeNull();
  });

  it('prints an available figure’s caveat unabridged too, where the keyboard reaches it', () => {
    /*
     * The channel a priced cost had none of. While the producer could price nothing, this metric was
     * UNAVAILABLE and its basis was the `reason` the row above prints in full — so every word was
     * reachable. Pricing made the reading available and closed that channel by succeeding: the
     * footnote shows the first clause and there was nowhere left for the rest.
     *
     * The row rather than only the tile's tooltip, because a `title` is mouse-only. The picker is a
     * button and a group of native checkboxes, so this copy is what a keyboard or a screen reader
     * gets to, and it is the reason the tooltip is allowed to be a tooltip.
     */
    store({ metrics: ['cost'], scope: 'everyone' });
    render(<MetricBlock data={longBasisBundle} />);
    openPicker();
    const row = screen.getByTestId('metric-option-caveat-cost');
    expect(row.textContent).toBe(LONG_COST_BASIS);
    for (const fact of AUDIT_FACTS) expect(row.textContent, fact).toContain(fact);
    // Its OWN row, not the reason row: `cost` is readable here, and a reason line under a working
    // figure would read as a warning about it.
    expect(screen.queryByTestId('metric-option-reason-cost')).toBeNull();
    // And the metrics with nothing more to say than their footnote says get no row at all. On this
    // bundle that includes `modelsUsed`, whose caveat is a roster and whose roster is not on the wire.
    for (const id of ['tokens', 'llmCalls', 'totalTime', 'cacheReuse', 'modelsUsed']) {
      expect(screen.queryByTestId(`metric-option-caveat-${id}`), id).toBeNull();
    }
  });

  it('hands the per-model roster to both channels, and its head to the footnote', () => {
    /*
     * The second thing a caveat carries, and it is not a producer sentence: the roster is composed
     * from ids and counts off the wire because a sentence per model cannot be a line under a figure.
     * The footnote takes the head — which model is most of the bill, which is the actionable half —
     * and the whole roster goes where a long caveat has always gone.
     *
     * Both channels asserted here rather than one, for the reason the cost basis asserts both: a
     * `title` is mouse-only, and the picker row is what a keyboard and a screen reader reach.
     */
    store({ metrics: ['modelsUsed'], scope: 'everyone' });
    render(<MetricBlock data={rosterBundle} />);
    /*
     * Found by its label, not by taking the only tile: the block always shows three, and this bundle
     * carries the token term the model count is captioned against — so `Everyone's tokens` is readable
     * beside it and a bare `getByTestId` would be ambiguous or, worse, assert about the wrong figure.
     */
    const tile = screen.getAllByTestId('metric-tile').find((el) => el.querySelector(
      '[data-testid="metric-tile-label"]')?.textContent === metricLabel(
      METRICS.find((m) => m.id === 'modelsUsed')!, 'everyone'));
    expect(tile).toBeDefined();
    // The count is still the figure. What the footnote adds is which model it is about.
    expect(tile!.querySelector('[data-testid="metric-tile-value"]')?.textContent).toBe('2');
    const sub = tile!.querySelector('[data-testid="metric-tile-sub"]')?.textContent ?? '';
    expect(sub).toContain(`${ROSTER_BIGGEST} is 97% of 10.3M tokens`);
    // The tile's own coverage clause survives beside it: the roster's fractions are over a different
    // population, so the one saying how much of the harness the count covers cannot be dropped.
    expect(sub).toContain(`over ${ROSTER_RUNS} of ${ROSTER_RUNS} sessions`);

    const whole = caveatFor('modelsUsed', rosterBundle, SHARED_VIEW);
    expect(tile!).toHaveAttribute('title', whole);
    openPicker();
    expect(screen.getByTestId('metric-option-caveat-modelsUsed').textContent).toBe(whole);
    // Verbatim on both, so a reader following the footnote's head reaches the rest of that roster
    // rather than a second rendering of it.
    expect(whole).toContain('global.anthropic.claude-sonnet-5: 335.7k tokens (3%) in 11 of 25 runs');
  });

  it('offers no caveat row for a basis the footnote already carries whole', () => {
    // Same metric, same bundle shape, a basis short enough to fit under the figure. One sentence in a
    // footnote AND a picker row is the same sentence twice, which teaches a reader to skip both.
    store({ metrics: ['cost'], scope: 'everyone' });
    render(<MetricBlock data={livePricedBundle} />);
    expect(screen.getByTestId('metric-tile-sub').textContent).toContain(COST_BASIS);
    openPicker();
    expect(screen.queryByTestId('metric-option-caveat-cost')).toBeNull();
  });
});

// --- one cause, said once ---------------------------------------------------
describe('MetricBlock — every metric unavailable for one structural reason', () => {
  it('replaces the tiles with the single shared cause, named and said once', () => {
    /*
     * The long render, on the cause that keeps it: this reader's bundle IS here and its day rows are
     * not. One sentence in place of three tiles that would each repeat it, and the three figures it
     * takes named — which on a per-scope label is the only thing saying whose figures are missing.
     */
    const noHistory = bundleWith(harvestBundle.scorecard!.spend,
                                 actorWith(pricedRuns, { history: undefined }));
    render(<MetricBlock data={noHistory} />);

    const collapsed = screen.getByTestId('metric-block-collapsed');
    const reason = reasonFor('tokens', noHistory);
    expect(reason).toMatch(/no day-by-day history yet/);
    expect(collapsed).toHaveTextContent(reason);
    // Once. Three tiles carrying one sentence three times buries the single cause under its own
    // repetition, which is the reason the registry marks these codes structural at all.
    expect(collapsed.textContent?.split(reason)).toHaveLength(2);
    // Named, so the reader knows which figures they are not seeing.
    expect(collapsed).toHaveTextContent(
      'Your tokens, Your calls and Your cost are unavailable in this data.');
    expect(screen.queryByTestId('metric-tile')).toBeNull();
    expect(screen.queryByTestId('metric-unavailable')).toBeNull();
    // The gear stays. On this bundle every metric is unavailable, so hiding the only control
    // would strand the reader with no way to see what is on offer.
    expect(screen.getByTestId('metric-picker-toggle')).toBeInTheDocument();
  });

  it('agrees with one figure as well as three, since the row can be one', () => {
    /*
     * The floor is one metric, and `Your cost are unavailable` is what a sweep that dropped the
     * agreement leaves behind. Reached by unchecking rather than by storing one id, because `topUp`
     * fills a short stored list back to three at mount — so a one-metric row only exists after a
     * reader has made it one.
     */
    const noHistory = bundleWith(harvestBundle.scorecard!.spend,
                                 actorWith(pricedRuns, { history: undefined }));
    render(<MetricBlock data={noHistory} />);
    openPicker();
    toggle('tokens');
    toggle('llmCalls');
    expect(screen.getByTestId('metric-block-collapsed'))
      .toHaveTextContent('Your cost is unavailable in this data.');
  });

  it('says a cause that carries a phrase in that phrase alone, with the argument on the line', () => {
    /*
     * The short render, on the cause a reader meets FIRST and most often: the fixture carries no
     * `actor` key, so every personal figure is absent and the block is the top of the page. Four
     * sentences of the producer's own vocabulary there was 4 of the 7 the whole default view is
     * allowed, spent above the figures that do have numbers.
     *
     * What must not happen is the four sentences becoming a shorter four. The phrase stands ALONE:
     * no list of the figures it covers (it covers all of them, whichever three are selected) and no
     * sentence of the reason under it.
     */
    render(<MetricBlock data={noScorecardBundle} />);

    const cause = causeFor('tokens', noScorecardBundle);
    expect(cause.brief).toBeTruthy();
    const collapsed = screen.getByTestId('metric-block-collapsed');
    expect(collapsed.textContent).toBe(cause.brief);
    expect(collapsed.textContent).not.toContain('Your tokens,');
    expect(collapsed.textContent).not.toContain('unavailable in this data');
    // Gone from the page, not from the surface: the whole argument is on the line, and unabridged on
    // every picker row — which is the copy a reader with no pointer reaches.
    expect(collapsed.getAttribute('title')).toBe(cause.reason);
    openPicker();
    expect(screen.getByTestId('metric-option-reason-tokens').textContent).toBe(cause.reason);
    expect(screen.queryByTestId('metric-tile')).toBeNull();
    expect(screen.getByTestId('metric-picker-toggle')).toBeInTheDocument();
  });

  it('keeps a tile per metric when the host asks for tiles instead', () => {
    render(<MetricBlock data={noScorecardBundle} structuralFallback="tiles" />);
    expect(screen.getAllByTestId('metric-unavailable')).toHaveLength(3);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
  });

  it('renders nothing at all when the host asks it to hide', () => {
    const { container } = render(<MetricBlock data={noScorecardBundle} structuralFallback="hide" />);
    expect(container).toBeEmptyDOMElement();
    // And it hides only on the structural case: a row with figures in it still has figures to show.
    render(<MetricBlock data={harvestBundle} structuralFallback="hide" />);
    expect(screen.getByTestId('metric-row')).toBeInTheDocument();
  });

  it('does not collapse two different causes into one', () => {
    /*
     * `noScorecard` and `noActorBundle` are both structural, and they are not the same fact: one
     * says the bundle predates the scorecard, the other that this reader had no session in the
     * window. Printing either as the cause of all three would be wrong about two of them.
     */
    const noScorecardWithDays: ImproveData = { ...noScorecardBundle, actor: actorWith(pricedRuns) };
    store({ metrics: ['tokens', 'cacheReuse', 'llmCalls'] });
    render(<MetricBlock data={noScorecardWithDays} />);

    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
    // Two figures the day rows can supply, and one term those rows do not carry — which is a
    // per-metric cause and must not become the row's.
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(2);
    expect(screen.getAllByTestId('metric-unavailable')).toHaveLength(1);
    expect(screen.getByTestId('metric-unavailable'))
      .toHaveAttribute('title', reasonFor('cacheReuse', noScorecardWithDays));
  });

  it('does not collapse three per-metric causes that happen to share a code', () => {
    /*
     * `notInBundle` is one code covering three different sentences: this scorecard names no token
     * total, no call count and no model count, and each is its own missing term. Collapsing on the
     * code alone would print the first metric's sentence as the cause of all three and be wrong
     * about two — which is why only the codes that are properties of the whole bundle collapse.
     */
    const bareScorecard = bundleWith({ costBasis: COST_BASIS } as Scorecard['spend']);
    store({ metrics: ['tokens', 'llmCalls', 'modelsUsed'], scope: 'everyone' });
    render(<MetricBlock data={bareScorecard} />);

    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
    const reasons = screen.getAllByTestId('metric-unavailable')
      .map((slot) => slot.getAttribute('title'));
    expect(reasons).toHaveLength(3);
    expect(new Set(reasons).size).toBe(3);
    expect(reasons[0]).toContain('a token total');
    expect(reasons[1]).toContain('an LLM call count');
  });

  it('does not collapse a row whose figures were merely not measured', () => {
    /*
     * The strongest form of the rule. This reader HAS a bundle and their one run recorded no usage,
     * so all three defaults are AVAILABLE and every one reads `UNMEASURED` — the row looks as empty
     * as the collapsed case and is a completely different fact: we have their runs and nothing in
     * them was measured. An implementation that collapsed on "looks empty" passes every other test
     * in this file and fails here.
     */
    render(<MetricBlock data={unmeasuredMineBundle} />);
    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
    // Three figures, each hatched rather than shown as a 0 — and the price is the one that matters
    // most, because `$0.00` is the only one of the three a reader would believe.
    expect(screen.getAllByTestId('sentinel')).toHaveLength(3);
    const gap = 'across 1 run · 0 of 1 sessions';
    // The price gains one clause the other two do not: with no figure to mis-attribute, the
    // producer's own sentence on why nothing is priced is the only thing that explains the absence.
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent))
      .toEqual([gap, gap, `${gap} · ${COST_BASIS}`]);
  });
});

// --- handing readings to the tile ------------------------------------------
describe('MetricBlock — what it passes to the tile', () => {
  it('hands a split its share and both side names as one object', () => {
    /*
     * The bridge, and its two ways of going wrong. The registry keeps the share on the READING and
     * the side names on the ENTRY, and the tile wants the three together. Passing the reading's
     * headline `value` as the share instead of its `ratio` yields 208045649, which the bar reports
     * as `not a proportion`; swapping the sides names the wrong side of the split.
     */
    store({ metrics: ['tokens', 'llmCalls', 'contextVsGenerated'], scope: 'everyone' });
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getByTestId('ratio-bar-label').textContent)
      .toBe('98% context / 2% generated');
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'measured');
    expect(parseFloat(screen.getByTestId('ratio-bar-fill').style.width))
      .toBeCloseTo((HARVEST_CONTEXT / HARVEST_TOKENS) * 100, 10);
    // The headline figure is the part's magnitude, which the bar cannot express: a 98% share of
    // two hundred tokens and of two hundred million are different facts.
    expect(screen.getAllByTestId('metric-tile-value')[2].textContent).toBe('208.0M');
  });

  it('passes an unmeasured share through as unmeasured rather than dropping the bar', () => {
    store({ metrics: ['tokens', 'llmCalls', 'contextVsGenerated'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'unmeasured');
    expect(screen.getByTestId('ratio-bar-label').textContent).toBe('context share not measured');
  });

  it('formats each figure with the metric’s own formatter and never with one of its own', () => {
    /*
     * Same component, three units. A block that reached for `compactNumber` itself would render
     * busy time as `412.5M`, and one that dropped `format` would render the token total as
     * `211662174`.
     */
    store({ metrics: ['tokens', 'totalTime', 'modelsUsed'], scope: 'everyone' });
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['211.7M', '4d 19h', '5']);
    // And the footnotes carry the two DIFFERENT denominators this bundle really has, so a sum
    // over two thirds of the population is not captioned as a whole-population figure.
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent))
      .toEqual(['354 of 553 sessions', '553 of 553 sessions', 'over 354 of 553 sessions']);
    // No bar on a scalar: the tile draws one whenever it is given a `ratio` at all, so passing an
    // empty object rather than nothing would put `not a proportion` under three figures.
    expect(screen.queryByTestId('ratio-bar')).toBeNull();
  });

  it('sets a priced figure as money, in its own denominator', () => {
    /*
     * Money in the same row as two counts. The count formatter would set this figure as
     * `2.984212` — its own output for anything under a thousand — so a block that reached for one
     * formatter, or dropped `format` and stringified the value, prints the producer's raw float
     * where a price belongs.
     */
    store({ metrics: ['cost', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={pricedBundle} />);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['$2.98', '211.7M', '4d 19h']);
    // Three denominators, and cost's is its own: a session no model of which the rate table can
    // price is not priced at all, so this covers two of the sessions where tokens cover 354.
    // Quoting the token figure under a price would caption two sessions' bill as the population's.
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent)).toEqual([
      `${COST_BASIS} · ${PRICED_SESSIONS} of ${HARVEST_SESSIONS} sessions priced`,
      `${HARVEST_SESSIONS_WITH_USAGE} of ${HARVEST_SESSIONS} sessions`,
      `${HARVEST_SESSIONS} of ${HARVEST_SESSIONS} sessions`,
    ]);
    // A figure, not the hatched sentinel: the producer priced this population.
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('carries the tokens a short bill leaves out through to the footnote', () => {
    /*
     * Pricing skips a model with no rate row instead of failing the session, so a figure can be
     * short while every session that recorded usage was priced. The registry composes that clause
     * and its own tests pin the wording; what is at stake HERE is whether a reader ever sees it — a
     * footnote cut to its first clause, or a tile handed only the coverage half, drops exactly the
     * caveat that stops a short bill reading as the population's.
     */
    store({ metrics: ['cost'], scope: 'everyone' });
    render(<MetricBlock data={partlyPricedBundle} />);
    // The figure is untouched: the clause reports the gap, it does not adjust the money.
    expect(screen.getByTestId('metric-tile-value').textContent).toBe('$16.74');
    expect(screen.getByTestId('metric-tile-sub').textContent)
      .toBe(`${COST_BASIS} · ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions priced · `
        + '90.6k of 6.9M tokens unpriced');

    // The same population with nothing unpriced, in the same row: same money, same run denominator,
    // and no third clause. That the shorter footnote is a PREFIX of the longer one is the whole
    // point — nothing in the first two clauses distinguishes a complete bill from a short one.
    render(<MetricBlock data={livePricedBundle} />);
    const [short, whole] = screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent);
    expect(whole).toBe(`${COST_BASIS} · ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions priced`);
    expect(short?.startsWith(whole ?? '')).toBe(true);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['$16.74', '$16.74']);
  });

  it('hands the whole pricing basis to the tile while the footnote keeps its one line', () => {
    /*
     * The two halves of one sentence, in one render, because the failure is only visible as a pair. A
     * block that dropped the caveat leaves 813 of the producer's 839 characters reachable from
     * nowhere — the state this channel exists to end. A block that put the whole sentence in the
     * FOOTNOTE instead sets a dozen lines of meta text in a tile a third of a row wide and pushes its
     * neighbours down the pane, which is why the footnote is a head in the first place.
     *
     * The four audited facts are asserted on the tooltip AND against the footnote, so a change that
     * "helpfully" lengthened the footnote to include them fails here rather than passing as an
     * improvement.
     */
    store({ metrics: ['cost', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={longBasisBundle} />);
    const tile = screen.getAllByTestId('metric-tile')[0];
    expect(tile).toHaveAttribute('title', LONG_COST_BASIS);
    const footnote = screen.getAllByTestId('metric-tile-sub')[0].textContent ?? '';
    expect(footnote)
      .toBe(`ESTIMATE, not billed truth · ${LIVE_RUNS_WITH_USAGE} of ${LIVE_RUNS} sessions priced`);
    for (const fact of AUDIT_FACTS) {
      expect(tile.getAttribute('title'), fact).toContain(fact);
      expect(footnote, fact).not.toContain(fact);
    }
    // A prefix of the sentence, not a rewrite of it: following the footnote leads into the tooltip.
    expect(LONG_COST_BASIS.startsWith(footnote.split(' · ')[0])).toBe(true);
    // The two tiles beside it have nothing more to say than their footnotes say, and gain no
    // tooltip-shaped affordance with nothing behind it.
    for (const other of screen.getAllByTestId('metric-tile').slice(1)) {
      expect(other).not.toHaveAttribute('title');
    }
  });

  it('hatches a price nothing could compute rather than setting it as zero dollars', () => {
    // The producer priced nothing on this bundle and said why. `$0.00` here would read as a free
    // population, which is the opposite of an unpriced one.
    store({ metrics: ['cost', 'tokens', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={wireBundle} />);
    const value = screen.getAllByTestId('metric-tile-value')[0];
    expect(value).toHaveTextContent('not measured');
    // Neither `$0.00` nor `not a cost`: a sentinel is branched on before any formatter sees it, so
    // the money formatter's own backstop never reaches a tile either.
    expect(value.textContent).not.toContain('$');
    expect(value.textContent).not.toContain('not a cost');
    // And the producer's reason for pricing nothing still reaches the reader, in the footnote where
    // the caveat on a priced figure goes.
    expect(screen.getAllByTestId('metric-tile-sub')[0].textContent)
      .toBe(`${COST_BASIS} · 0 of ${WIRE_RUNS} sessions priced`);
  });

  it('sets the reader’s own bill as money, from the runs that were priced', () => {
    // Three units off ONE actor bundle, so a personal metric wired to the wrong formatter shows up
    // as a price in tokens or a duration in minutes rather than as a plausible number.
    store({ metrics: ['cost', 'tokens', 'totalTime'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['$2.98', '6.6M', '2h 32m']);
    // `formatMs` would set that busy-time sum as `151m 44s` — a real figure nobody sizes at a
    // glance, which is why a SUM over days takes the days-and-hours formatter.
    expect(screen.getAllByTestId('metric-tile-value')[2].textContent).not.toContain('m 44s');
  });

  it('never lets a sentinel reach a money formatter', () => {
    /*
     * The failure this exists to catch, and there are three ways in: a cumulative sum returning `0`
     * because no day was priced, a `?? 0` on the way to the tile, or a formatter called before the
     * sentinel is branched on. The first two print `$0.00`, which reads as "your runs were free" —
     * beside two seven-figure token counts on the same row, a reader would believe it. The third
     * prints `not a cost`, which reads as a producer bug.
     *
     * This is the shape the deployed producer emits: tokens and calls measured, nothing priced.
     */
    store({ metrics: ['cost', 'tokens', 'llmCalls'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['$2.98', '6.6M', '127']);

    render(<MetricBlock data={unmeasuredMineBundle} />);
    const values = screen.getAllByTestId('metric-tile-value').slice(3);
    expect(values[0]).toHaveTextContent('not measured');
    for (const value of values) {
      expect(value.textContent).not.toContain('$');
      expect(value.textContent).not.toContain('not a cost');
      expect(value.textContent).not.toContain('not a count');
      expect(value.textContent).not.toMatch(/\b0\b/);
    }
  });
});

// --- the picker ------------------------------------------------------------
describe('MetricBlock — the picker', () => {
  it('offers every registry metric as a real checkbox, not as a menu of commands', () => {
    /*
     * `menuitem` is for commands, and menu semantics promise a keyboard contract (roving focus,
     * typeahead) that nothing here implements. A native checkbox announces checked, unchecked and
     * disabled on its own, which is most of what a capped multi-select owes a reader.
     */
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    expect(screen.getAllByRole('checkbox')).toHaveLength(METRICS.length);
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0);
    expect(screen.queryByRole('menu')).toBeNull();
    // Eight rows for eight figures, not sixteen: scope is a switch, so a question takes one row.
    expect(METRICS).toHaveLength(8);
    // A loop over the registry, so a metric added there appears here with no change to this file.
    // The label is composed at the CURRENT scope, which is what makes the picker state the basis
    // rather than offering a second way to set it.
    for (const entry of METRICS) {
      expect(screen.getByTestId(`metric-option-row-${entry.id}`))
        .toHaveTextContent(metricLabel(entry, 'yours'));
    }
  });

  it('says which refusal is in force, in the group’s own name', () => {
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    const caption = () => screen.getByTestId('metric-picker-caption').textContent ?? '';
    expect(caption()).toContain('Uncheck one to swap');
    expect(screen.getByTestId('metric-picker'))
      .toHaveAttribute('aria-labelledby', screen.getByTestId('metric-picker-caption').id);

    toggle('tokens');
    expect(caption()).not.toContain('Uncheck one to swap');
    toggle('llmCalls');
    expect(caption()).toContain('At least one stays shown');
  });

  it('describes a split it cannot read, by name and by both sides', () => {
    // What the entry's static `sides` are for: a picker has to be able to describe a metric that
    // no bundle currently supports a bar for.
    render(<MetricBlock data={noScorecardBundle} />);
    openPicker();
    const row = screen.getByTestId('metric-option-row-cacheReuse');
    expect(row).toHaveTextContent('Your cache reuse');
    expect(row).toHaveTextContent('cache reads vs cache writes');
    expect(row).toHaveTextContent(reasonFor('cacheReuse', noScorecardBundle));

    // And not where the label already says it: `Context vs generated context vs generated` is a
    // stutter, and the hint is there to add a fact rather than to be present.
    expect(screen.getByTestId('metric-option-row-contextVsGenerated').textContent)
      .toBe(`Your context vs generated${reasonFor('contextVsGenerated', noScorecardBundle)}`);

    /*
     * The same rows describe the SHARED figures once the switch is moved — one row per question,
     * relabelled, rather than a second row per question.
     *
     * The picker stays open across the switch here only because `fireEvent` dispatches straight at
     * the button; in a browser the scrim is over the page and a click on a switch closes the picker
     * first, which is the click-away behaviour and not a bug.
     */
    fireEvent.click(screen.getByTestId('metric-scope-everyone'));
    expect(screen.getByTestId('metric-option-row-cacheReuse'))
      .toHaveTextContent("Everyone's cache reuse");
  });

  it('closes on Escape and on a click outside itself', () => {
    render(<MetricBlock data={harvestBundle} />);
    const toggle = screen.getByTestId('metric-picker-toggle');

    openPicker();
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByTestId('metric-picker')).toBeNull();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    openPicker();
    // The click-away layer, without which a picker that only closes on re-click traps the user
    // behind it.
    fireEvent.click(document.querySelector('[aria-hidden="true"][style*="fixed"]') as Element);
    expect(screen.queryByTestId('metric-picker')).toBeNull();
  });

  it('stops listening for Escape once it is closed', () => {
    // The listener is on `document`, so one left behind would keep swallowing Escape for anything
    // else on the page after the picker is gone.
    const add = vi.spyOn(document, 'addEventListener');
    const remove = vi.spyOn(document, 'removeEventListener');
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    const registered = add.mock.calls.filter(([type]) => type === 'keydown').length;
    expect(registered).toBeGreaterThan(0);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(remove.mock.calls.filter(([type]) => type === 'keydown')).toHaveLength(registered);
    add.mockRestore();
    remove.mockRestore();
  });
});

// --- the two switches on the header ------------------------------------------
/**
 * What stops a reader mistaking one scope's figure for the other's, and a total for a window.
 *
 * The ambiguity is measured rather than hypothetical: the signed-in reader's two runs are 96.5% of the
 * live population's tokens, so a shared total and a personal one differ by 3.5% and by nothing in the
 * caption. Two things carry the distinction now — the labels, and the switch that is on the HEADER
 * rather than in the picker, because a figure whose basis is hidden behind a gear cannot be read.
 */
describe('MetricBlock — the scope and period switches', () => {
  /** The harvest bundle with the reader's WIDER rows, so a personal split has something to read. */
  const wideBundle = bundleWith(harvestBundle.scorecard!.spend, actorWith(wideRuns));

  const scopeButton = (scope: string) => screen.getByTestId(`metric-scope-${scope}`);
  const periodButton = (period: string) => screen.getByTestId(`metric-period-${period}`);
  const basis = () => screen.getByTestId('metric-basis').textContent ?? '';

  it('puts both switches beside the numbers rather than behind the gear', () => {
    /*
     * The layout decision, said as an assertion. A figure whose basis is behind a menu cannot be read
     * — `5.7M` over a fortnight and over a year are different facts — and period is a comparison
     * action a reader wants one click from the numbers. So both are in the block with the picker
     * CLOSED, and the picker offers neither.
     */
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.queryByTestId('metric-picker')).toBeNull();
    for (const scope of SCOPES) expect(scopeButton(scope)).toBeInTheDocument();
    for (const period of PERIODS) expect(periodButton(period)).toBeInTheDocument();
    // Toggle buttons in a plain group, NOT a radiogroup: a radio group promises roving focus and
    // arrow keys, which nothing here implements — the same argument the picker makes for refusing
    // `role="menu"`.
    expect(screen.getByRole('group', { name: 'Whose runs' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'How far back' })).toBeInTheDocument();
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    expect(scopeButton('yours')).toHaveAttribute('aria-pressed', 'true');
    expect(scopeButton('everyone')).toHaveAttribute('aria-pressed', 'false');
  });

  it('opens on the reader’s own runs, all time', () => {
    // The default that fixes the defect: a cumulative personal total is the one reading that cannot
    // be empty for a reader who has ever run anything.
    expect(DEFAULT_VIEW).toEqual({ scope: 'yours', period: 'allTime' });
    render(<MetricBlock data={harvestBundle} />);
    expect(periodButton('allTime')).toHaveAttribute('aria-pressed', 'true');
    expect(slotLabels()).toEqual(DEFAULT_LABELS);
  });

  it('changes every tile and the basis label together when the scope is switched', () => {
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your tokens', 'Your calls', 'Your cost']);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['6.6M', '127', '$2.98']);
    expect(basis()).toContain(`since ${MINE_DAY_ONE}`);

    fireEvent.click(scopeButton('everyone'));
    // All three labels and all three figures in one move. The harvest's 211.7M against the reader's
    // 6.6M is what proves the source changed rather than the wording.
    expect(slotLabels()).toEqual(["Everyone's tokens", "Everyone's calls", "Everyone's cost"]);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['211.7M', '5.4k', 'not measured']);
    // And the basis goes with the scope rather than being reworded for it: a shared figure is not a
    // total over a span, so there is no span to name.
    expect(screen.queryByTestId('metric-basis')).toBeNull();
  });

  it('changes every tile and the basis label together when the period is switched', () => {
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('6.6M');
    expect(basis()).toBe(`since ${MINE_DAY_ONE}`);

    fireEvent.click(periodButton('trailing7'));
    // Both days fall inside seven, so the figure is unchanged and the LABEL is not — which is the
    // case that would let a period switch look like it worked while doing nothing.
    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('6.6M');
    expect(basis()).toBe(`7 days to ${MINE_DAY_TWO}`);
    expect(periodButton('trailing7')).toHaveAttribute('aria-pressed', 'true');
    expect(periodButton('allTime')).toHaveAttribute('aria-pressed', 'false');
  });

  it('drops a day the window excludes, rather than relabelling the same total', () => {
    // A day well outside both windows, so the figure really moves and the label is not the only
    // thing that changed.
    const older = bundleWith(harvestBundle.scorecard!.spend, actorWith(pricedRuns, {
      history: [{ date: '2026-06-01', runs: 3, tokens: 8_400_000 }, ...daysFrom(pricedRuns)],
    }));
    render(<MetricBlock data={older} />);
    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('15.0M');
    fireEvent.click(periodButton('trailing30'));
    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('6.6M');
    expect(basis()).toBe(`30 days to ${MINE_DAY_TWO}`);
  });

  it('refuses every period at the shared scope, on the control and nowhere else', () => {
    /*
     * A shared figure has to say how many sessions recorded it, and no shared daily row on the wire
     * carries that count yet — the producer writes one now, but a row is written once and carried
     * forward, so the rows already out there keep the shape they had. A period total over them could
     * only caption itself by the days it spans and would read as complete over two thirds of a
     * population. So the period is declined.
     *
     * The greyed-out button IS the refusal, with the cause on it for anyone who wants it. It used to
     * be a paragraph beside the switch as well, on the argument that a disabled button is not
     * focusable — which is true and still does not earn the wire's own reasoning a line of its own
     * next to the figures.
     */
    render(<MetricBlock data={harvestBundle} />);
    fireEvent.click(scopeButton('everyone'));

    for (const period of PERIODS) {
      expect(periodButton(period), period).toBeDisabled();
      // The whole sentence, unabridged, on the one control it is about.
      expect(periodButton(period).getAttribute('title'), period)
        .toContain('not totalled over a period');
    }
    expect(screen.queryByTestId('metric-period-refused')).toBeNull();
    // And the figures are still there — the period is refused, the scope is not.
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
  });

  it('keeps a refused period as the reader’s own choice rather than dropping it', () => {
    // A switch showing nothing pressed reads as one that lost the setting. The choice is still
    // theirs, still stored, and greyed out here.
    store({ metrics: ['tokens', 'llmCalls', 'cost'], period: 'trailing7' });
    render(<MetricBlock data={harvestBundle} />);
    fireEvent.click(scopeButton('everyone'));
    expect(periodButton('trailing7')).toHaveAttribute('aria-pressed', 'true');
    expect(periodButton('trailing7')).toBeDisabled();
    // Switching back restores it in effect, not merely in appearance: the personal basis is what the
    // stored period is spelled on, and it names the day the window is anchored to.
    fireEvent.click(scopeButton('yours'));
    expect(basis()).toBe(`7 days to ${MINE_DAY_TWO}`);
  });

  it('names both switches on their buttons, in the reader’s words', () => {
    render(<MetricBlock data={harvestBundle} />);
    expect(SCOPES.map((scope) => scopeButton(scope).textContent))
      .toEqual(SCOPES.map((scope) => SCOPE_LABEL[scope]));
    expect(PERIODS.map((period) => periodButton(period).textContent))
      .toEqual(PERIODS.map((period) => PERIOD_LABEL[period]));
    expect(SCOPES.map((scope) => SCOPE_LABEL[scope])).toEqual(['Yours', 'Everyone']);
  });

  it('persists each switch on its own, without freezing the metric selection', () => {
    /*
     * The trap in widening the stored record. Each field is written only by its own control, so the
     * ABSENCE of `metrics` still means "never chose" — store all three together on any change and
     * flipping the scope switch would pin that reader's metric list to whatever the default was that
     * day, silently and for good.
     */
    render(<MetricBlock data={harvestBundle} />);
    fireEvent.click(periodButton('trailing30'));
    expect(stored()).toEqual({ period: 'trailing30' });
    fireEvent.click(scopeButton('everyone'));
    expect(stored()).toEqual({ period: 'trailing30', scope: 'everyone' });
    openPicker();
    toggle('cost');
    expect(stored()).toEqual({
      period: 'trailing30', scope: 'everyone', metrics: ['tokens', 'llmCalls'],
    });
  });

  it('reads all three preferences back, so a reload shows the same figures', () => {
    const first = render(<MetricBlock data={harvestBundle} />);
    fireEvent.click(periodButton('trailing7'));
    fireEvent.click(scopeButton('everyone'));
    openPicker();
    toggle('tokens');
    toggle('totalTime');
    first.unmount();

    render(<MetricBlock data={harvestBundle} />);
    expect(scopeButton('everyone')).toHaveAttribute('aria-pressed', 'true');
    expect(periodButton('trailing7')).toHaveAttribute('aria-pressed', 'true');
    expect(slotLabels()).toEqual(["Everyone's calls", "Everyone's cost", "Everyone's time"]);
  });

  it('ignores a stored scope or period that is not one of the ones offered', () => {
    store({ scope: 'somebody', period: 'forever', metrics: ['tokens'] });
    render(<MetricBlock data={harvestBundle} />);
    expect(scopeButton('yours')).toHaveAttribute('aria-pressed', 'true');
    expect(periodButton('allTime')).toHaveAttribute('aria-pressed', 'true');
    expect(slotLabels()[0]).toBe('Your tokens');
  });

  it('reads the bare array this key used to hold as the metric list and nothing else', () => {
    // The whole of the migration: a reader who stored ids before the switches existed never chose a
    // scope or a period, and the defaults are the correct reading of that.
    store(['totalTime', 'modelsUsed', 'tokens']);
    render(<MetricBlock data={harvestBundle} />);
    expect(slotLabels()).toEqual(['Your time', 'Your models', 'Your tokens']);
    expect(scopeButton('yours')).toHaveAttribute('aria-pressed', 'true');
    expect(periodButton('allTime')).toHaveAttribute('aria-pressed', 'true');
  });
});

// --- the basis label ---------------------------------------------------------
describe('MetricBlock — the basis beside the numbers', () => {
  it('states the span the figures cover, always visible', () => {
    // A cumulative total with no basis cannot be read at all. `since 30 Aug` is what makes it one.
    render(<MetricBlock data={harvestBundle} />);
    expect(screen.getByTestId('metric-basis')).toHaveTextContent(`since ${MINE_DAY_ONE}`);
  });

  it('carries the bundle’s own stamp, because a stale total reads exactly like a current one', () => {
    /*
     * A quiet actor's bundle is not deleted: the producer skips the write and the previous item
     * persists, carrying an older stamp. That date is the only thing on the wire that says a total
     * is not current, so it belongs beside the basis rather than nowhere.
     */
    const stamped = bundleWith(harvestBundle.scorecard!.spend,
                               actorWith(pricedRuns, { createdAt: '2026-09-03T04:55:09Z' }));
    render(<MetricBlock data={stamped} />);
    expect(screen.getByTestId('metric-basis'))
      .toHaveTextContent(`since ${MINE_DAY_ONE} · recorded ${MINE_DAY_TWO}`);
  });

  it('states nothing beside the shared figures the wire cannot qualify', () => {
    /*
     * The shared scope carried two lines of prose and now carries none of it. The window and the
     * population's shape (`9 sessions in all: 2 owned by 1 person, 7 owned by nobody`) described the
     * wire rather than answering a question the numbers raise, and the period's refusal repeated a
     * control that is already greyed out beside it. What a reader needs from a shared figure is its
     * denominator, and that is on the tile.
     *
     * The one line that may appear is the test above's: what the caps discarded, which no tile can
     * carry and which this bundle does not state. So the removed prose cannot come back for the
     * bundles that happen to support it, and the fact that replaced it is not silence.
     *
     * On the bundle with the MOST to say — `attribution` present, which the live emitted bundle omits
     * — so this cannot pass merely because the fixture had nothing to state.
     */
    expect(scopedBundleWithAttribution.scorecard!.attribution).toBeDefined();
    render(<MetricBlock data={scopedBundleWithAttribution} />);
    fireEvent.click(screen.getByTestId('metric-scope-everyone'));
    expect(screen.queryByTestId('metric-basis')).toBeNull();
    expect(screen.queryByTestId('metric-period-refused')).toBeNull();
    // The figures and their own denominators are untouched by that removal.
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
    expect(screen.getAllByTestId('metric-tile-sub')[0].textContent).toContain('sessions');
  });

  it('shows a shared reader what the harvest discarded, on the tile and beside the row', () => {
    /*
     * Both halves of the same absence, and neither is visible in the other. The tile's own footnote
     * says what the figure covers OF the population the producer admitted — `354 of 553 sessions`,
     * which can read complete — and this line says how much of the harness that population is. The
     * registry composes both and its own tests pin the wording; what is at stake here is whether a
     * reader ever sees them, since the producer has been emitting these two blocks with no consumer at
     * all.
     *
     * `population.sessions` and `spend.usageShortfall` are transcribed from the same replays the
     * registry's tests use: 39 sessions harvested, 3 under the span floor, 25 of the 36 discoverable
     * admitted; and a usage query that measured 44 sessions of which the span cap dropped 24, taking
     * 13.3M of 36.8M measured tokens with them.
     */
    const audited: ImproveData = {
      ...noScorecardBundle,
      scorecard: {
        population: { total: 25,
                      pass: 20,
                      fail: 5,
                      unscored: 0,
                      sessions: { discoverable: 36, admitted: 25, belowMinSpans: 3 } },
        spend: {
          ...harvestBundle.scorecard!.spend,
          usageShortfall: { tokensDiscarded: 13_300_000,
                            tokensMeasured: 36_800_000,
                            sessionsDiscarded: 24,
                            sessionsMeasured: 44 },
        },
      } as unknown as Scorecard,
    };
    store({ metrics: ['tokens', 'llmCalls', 'totalTime'], scope: 'everyone' });
    render(<MetricBlock data={audited} />);

    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('211.7M');
    expect(screen.getAllByTestId('metric-tile-sub')[0].textContent)
      .toBe(`${HARVEST_SESSIONS_WITH_USAGE} of ${HARVEST_SESSIONS} sessions`
        + ' · 13.3M of 36.8M tokens discarded');
    // Busy time beside it gains no such clause: it is summed from spans, not from the usage records
    // the row cap discarded, so the clause rides the figures it is actually missing from.
    expect(screen.getAllByTestId('metric-tile-sub')[2].textContent)
      .toBe(`${HARVEST_SESSIONS} of ${HARVEST_SESSIONS} sessions`);
    expect(screen.getByTestId('metric-basis').textContent)
      .toBe('25 of 36 sessions scored · 3 more too short to judge · '
        + 'spend dropped from 24 of 44 sessions');
  });

  it('keeps the personal basis, which is the one a total cannot be read without', () => {
    /*
     * The other end of the removal above, so a later sweep cannot take this one quietly. `6.6M` over
     * a fortnight and over a year are different facts and the tile cannot tell them apart, so the
     * personal line stays at every period and in its zero-state — where it is the honest empty message
     * rather than a span.
     */
    const first = render(<MetricBlock data={harvestBundle} />);
    for (const period of PERIODS) {
      fireEvent.click(screen.getByTestId(`metric-period-${period}`));
      expect(screen.getByTestId('metric-basis').textContent, period).toBeTruthy();
    }
    first.unmount();

    // And in the zero-state, where it is the honest empty message rather than a span: this bundle IS
    // the reader's own and what it does not carry is the day rows a total is summed from.
    const noHistory = bundleWith(harvestBundle.scorecard!.spend,
                                 actorWith(pricedRuns, { history: undefined }));
    const second = render(<MetricBlock data={noHistory} />);
    expect(screen.getByTestId('metric-basis'))
      .toHaveTextContent('no personal history in this data');
    second.unmount();

    /*
     * The ONE shape it says nothing on: the fixture carries no `actor` at all, which is the reader
     * the producer has never seen — and there the line standing in for the figures is itself a
     * phrase. Two short forms of one absence on adjacent lines is the repetition this band exists to
     * avoid, and the one that stays is the one sitting where the figures would be.
     */
    expect(noScorecardBundle.actor).toBeUndefined();
    render(<MetricBlock data={noScorecardBundle} />);
    expect(screen.queryByTestId('metric-basis')).toBeNull();
    expect(screen.getByTestId('metric-block-collapsed').textContent)
      .toBe(causeFor('tokens', noScorecardBundle).brief);
  });

  it('states the basis in the picker without offering to set it there', () => {
    // The gear keeps the eight figures and states the basis. A control here would put the answer
    // behind a click for everyone who never opens the menu — and would still need this label.
    render(<MetricBlock data={harvestBundle} />);
    openPicker();
    const caption = screen.getByTestId('metric-picker-caption');
    expect(caption).toHaveTextContent(`Showing yours, since ${MINE_DAY_ONE}`);
    const panel = screen.getByTestId('metric-picker');
    expect(panel.querySelector('[data-testid^="metric-scope-"]')).toBeNull();
    expect(panel.querySelector('[data-testid^="metric-period-"]')).toBeNull();
  });

  it('names the scope alone in the picker where there is no span to name', () => {
    // The caption is this group's accessible name, so a basis that went absent without the sentence
    // closing over it would announce `Showing everyone, .` to a screen reader.
    render(<MetricBlock data={harvestBundle} />);
    fireEvent.click(screen.getByTestId('metric-scope-everyone'));
    openPicker();
    const caption = screen.getByTestId('metric-picker-caption').textContent ?? '';
    expect(caption).toContain('Showing everyone.');
    expect(caption).not.toContain('harvest window');
  });
});

// --- 0, the sentinel, and nothing at all -------------------------------------
/**
 * THE THREE RENDERS, which is the whole of this change.
 *
 * Two different facts used to produce the same blank: a reader who really spent nothing and a reader
 * nothing was known about. The producer now writes a bundle for every actor it has seen, including one
 * with no day of activity, so the two are distinguishable — and each gets its own render.
 */
describe('MetricBlock — a reader who ran nothing against a reader nothing is known about', () => {
  it('shows 0 in a tile when the bundle positively says there was no activity', () => {
    // The owner's ask, and it is honest here and only here: the history key is PRESENT and empty, so
    // the producer swept this actor and recorded that there was nothing.
    const swept = bundleWith(harvestBundle.scorecard!.spend, actorWith([], { history: [] }));
    render(<MetricBlock data={swept} />);
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
    // The token total and the call count are the zeros. The price is the exception below.
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent).slice(0, 2))
      .toEqual(['0', '0']);
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent).slice(0, 2))
      .toEqual(['no runs in this period', 'no runs in this period']);
    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
  });

  it('shows the hatched sentinel and no zero when there is no bundle at all', () => {
    /*
     * The empty state that used to be the common one: no personal bundle, so nothing is known. What it
     * must not do is render a zero — and what it must do is leave somewhere to go, since a reader
     * looking at one line has no reason to think eight other figures are behind the gear.
     */
    render(<MetricBlock data={noScorecardBundle} />);
    const collapsed = screen.getByTestId('metric-block-collapsed');
    expect(collapsed).toHaveTextContent("Your own figures aren't in this report yet");
    // Absence, and no figure of any kind: not a zero, and no digit that could be read as one.
    expect(collapsed.textContent).not.toMatch(/\d/);
    // The refusal of the zero is still ARGUED — on the line rather than four sentences up the page.
    expect(collapsed.getAttribute('title')).toMatch(/not even as zero/);
    expect(screen.queryByTestId('metric-tile')).toBeNull();
    // And the way out it names is really there: one click, not a menu. On a bundle that HAS a
    // scorecard, since this fixture carries neither key and the shared half would say so too.
    const shared = bundleWith(harvestBundle.scorecard!.spend);
    render(<MetricBlock data={shared} />);
    fireEvent.click(screen.getAllByTestId('metric-scope-everyone')[1]);
    expect(slotLabels()).toEqual(["Everyone's tokens", "Everyone's calls", "Everyone's cost"]);
  });

  it('shows the sentinel, not a zero, when the bundle carries runs and no day rows', () => {
    // The third shape, and the one that most invites a zero: the item IS there, and what is missing
    // is the history the totals are summed from.
    const noHistory = bundleWith(harvestBundle.scorecard!.spend,
                                 actorWith(pricedRuns, { history: undefined }));
    render(<MetricBlock data={noHistory} />);
    const collapsed = screen.getByTestId('metric-block-collapsed');
    expect(collapsed).toHaveTextContent(/no day-by-day history yet/);
    expect(collapsed).toHaveTextContent(/not a record of your having run nothing/);
    expect(screen.queryByTestId('metric-tile')).toBeNull();
  });

  it('never shows a price as $0.00, even where its neighbours show 0', () => {
    /*
     * Cost's exception. `$0.00` is reserved for a session that used a priced model and no tokens, so a
     * rendered zero on a price reads as a session that ran and was free — a bill nobody computed. The
     * tile hatches it and the footnote says both why there is no figure and why nothing was priced.
     */
    const swept = bundleWith(harvestBundle.scorecard!.spend, actorWith([], { history: [] }));
    store({ metrics: ['cost', 'tokens', 'llmCalls'] });
    render(<MetricBlock data={swept} />);
    const values = screen.getAllByTestId('metric-tile-value').map((el) => el.textContent);
    expect(values[0]).toBe('not measured');
    expect(values[0]).not.toContain('$');
    // Its neighbours DO show the zero, which is what makes this a deliberate exception rather than a
    // failure to read the same rows.
    expect(values.slice(1)).toEqual(['0', '0']);
    expect(screen.getAllByTestId('metric-tile-sub')[0].textContent)
      .toContain('no runs in this period');
  });
});

// --- what a personal split hands the bar -------------------------------------
describe('MetricBlock — a personal split', () => {
  it('carries the tokens the READER’s own short bill leaves out through to the footnote', () => {
    /*
     * Pricing skips a model with no rate row rather than failing the session, so a cumulative total can
     * be short while every day that recorded usage was priced — the run extent reads exactly as complete
     * either way. The registry composes the clause and its own tests pin the wording; what is at stake
     * HERE is whether a reader ever sees it, since a tile handed only the extent drops precisely the
     * caveat that stops a short bill reading as a whole one.
     *
     * It matters more on a cumulative figure than on a single window: a total that quietly accumulates an
     * unstated shortfall for a month is a worse under-report than one day's, not a smaller one.
     */
    const short = actorWith(wideRuns, {
      history: daysFrom(wideRuns).map((day, i) => (i === 0 ? {
        ...day,
        costTokensPriced: MINE_TOKENS[0] - UNPRICED_TOKENS,
        costTokensUnpriced: UNPRICED_TOKENS,
      } : day)),
    });
    store({ metrics: ['cost', 'tokens', 'llmCalls'] });
    render(<MetricBlock data={bundleWith(harvestBundle.scorecard!.spend, short)} />);
    // The figure is untouched: the clause reports the gap, it does not adjust the money.
    expect(screen.getAllByTestId('metric-tile-value')[0].textContent).toBe('$2.98');
    expect(screen.getAllByTestId('metric-tile-sub')[0].textContent)
      .toBe(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions · `
        + '90.6k of 6.6M tokens unpriced');
    // The token tile beside it is NOT adjusted: the clause reports the gap in the money, and the
    // reader's tokens are all still there.
    expect(screen.getAllByTestId('metric-tile-value')[1].textContent).toBe('6.6M');
    // And a fully priced set of days says nothing about a shortfall, over a measured zero.
    render(<MetricBlock data={bundleWith(harvestBundle.scorecard!.spend, actorWith(wideRuns))} />);
    expect(screen.getAllByTestId('metric-tile-sub')[3].textContent)
      .toBe(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`);
  });

  it('hands a personal split its share and both side names, as it does a shared one', () => {
    /*
     * The bridge, on figures that had no splits at all until the producer projected the per-category
     * counts. Passing the reading's headline `value` as the share instead of its `ratio` yields
     * 4455000, which the bar reports as `not a proportion`; swapping the sides names the wrong side of
     * a split whose two sides are both about caching.
     */
    store({ metrics: ['cacheReuse', 'contextVsGenerated', 'modelsUsed'] });
    render(<MetricBlock data={bundleWith(harvestBundle.scorecard!.spend, actorWith(wideRuns))} />);
    expect(screen.getAllByTestId('ratio-bar-label').map((el) => el.textContent))
      .toEqual(['69% cache reads / 31% cache writes', '98% context / 2% generated']);
    for (const bar of screen.getAllByTestId('ratio-bar')) {
      expect(bar).toHaveAttribute('data-state', 'measured');
    }
    // The headline figures are the PARTS' magnitudes, and the model count is a count.
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['4.5M', '6.5M', '2']);
    expect(sumOf(MINE_CACHE_READ)).toBe(4_455_000);
    // The extent in RUNS and the coverage in SESSIONS — two counts, because a day row says how many
    // requests it held and separately how many of its sessions recorded each term. Both days recorded
    // both sides of both splits, so both fractions are whole.
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent))
      .toEqual(Array(3).fill(`across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`));
  });
});

// --- the band's own height ---------------------------------------------------
/**
 * WHAT THIS PROVES AND WHAT IT DOES NOT.
 *
 * jsdom performs no layout: it computes no box, so `offsetHeight` is 0 for every element here and no
 * test in this file can measure that the two scopes come out the same number of pixels. What it can
 * do is pin the MECHANISM that makes them, which is a reserve declared on the band and a rule for
 * placing a short render inside it.
 *
 * So a passing test here says: the reserve is declared, it is the SAME declaration on both scopes, and
 * the collapsed render is the one that centres. It does not say the reserve is the right size, and it
 * cannot see the case its own comment admits — content taller than the reserve, where `minHeight`
 * yields and the two scopes differ again. A browser is the only thing that settles either.
 */
describe('MetricBlock — the band keeps its height when the scope changes', () => {
  /** The reserve as the band declares it, or `''` when nothing is reserved. */
  const reserved = () => screen.getByTestId('metric-figures').style.minHeight;
  const placement = () => screen.getByTestId('metric-figures').style.justifyContent;

  /**
   * The owner's own payload: a shared scorecard and no bundle of the reader's own, which is the state
   * the producer leaves most readers in. It is the one shape where the two scopes render differently
   * ENOUGH to move the page — three tiles against one line of text.
   */
  const shared = () => bundleWith(harvestBundle.scorecard!.spend);

  it('reserves the same height whether it draws three tiles or one line', () => {
    render(<MetricBlock data={shared()} />);

    // `Yours`, the scope it opens on: one line, no tile.
    expect(screen.getByTestId('metric-block-collapsed')).toBeInTheDocument();
    expect(screen.queryByTestId('metric-tile')).toBeNull();
    const collapsedReserve = reserved();
    // A declaration and not an accident: an empty string is what a band with no reserve reports, and
    // it is what this test exists to fail on.
    expect(collapsedReserve).not.toBe('');

    fireEvent.click(screen.getByTestId('metric-scope-everyone'));

    // `Everyone`: three tiles, and the same reserve as the line they replaced.
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
    expect(reserved()).toBe(collapsedReserve);
  });

  it('scales the reserve with the reader’s type size rather than fixing it in pixels', () => {
    /*
     * The size tokens are `var()` references onto the General → Font Size control, so a reserve
     * expressed in pixels would be right at one setting and wrong at the other two — the tiles would
     * grow and the reserve would not. Asserted as the presence of the tokens rather than as a
     * computed height, which is the same jsdom limit this describe block opens with.
     */
    render(<MetricBlock data={shared()} />);
    expect(reserved()).toContain('calc(');
    expect(reserved()).toContain('var(--fs-2xs)');
    expect(reserved()).toContain('var(--fs-lg)');
  });

  it('centres the collapsed line in the reserve, and leaves the tiles at the top of it', () => {
    // A reserve with the short render pinned to its top is the jump traded for a gap: the line sits
    // where the first tile's label was and the rest of the room hangs empty underneath it.
    render(<MetricBlock data={shared()} />);
    expect(placement()).toBe('center');

    fireEvent.click(screen.getByTestId('metric-scope-everyone'));
    // Tiles differ in height and align to the top of their row; centring them in the reserve would
    // float them off the switches above.
    expect(placement()).toBe('flex-start');
  });

  it('reserves the height with no filler in the accessible tree', () => {
    /*
     * The reserve is a style on a box that was already there, so the collapsed render is still exactly
     * the one line a reader is read. A spacer element would have been the easy version of this and
     * would have put an empty node — or worse, a repeated figure — into what a screen reader walks.
     */
    render(<MetricBlock data={shared()} />);
    const band = screen.getByTestId('metric-figures');
    expect(band.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0);
    expect(band.children).toHaveLength(1);
    expect(band.children[0]).toBe(screen.getByTestId('metric-block-collapsed'));
  });
});
