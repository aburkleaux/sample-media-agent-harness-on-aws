/**
 * @vitest-environment jsdom
 *
 * The assertions that matter are the honesty ones: an unmeasured value must not render like a
 * measured one, a non-gating check must not look like it decided the verdict, and a control's
 * enabled state must come from `affordances` rather than from anything the component infers.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import
 * below are both required. `@testing-library/user-event` is not a dependency, use fireEvent.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ImproveCenter, readSoloVariant, type ImproveData } from './ImproveCenter';
import { ImproveInbox } from './ImproveInbox';
import { SuggestionCard } from './SuggestionCard';
import {
  EvidenceQualityBlock, MeasuredValue, ProcessCheckRow, SentinelValue, UnavailableBadge,
  abridge, compactNumber, formatExtent, formatMs, formatWindow, fractionLabel, pluralise,
  tileValueStyle,
} from './improveChrome';
import fixture from './__fixtures__/improveData.fixture.json';
import type {
  ActorBundle, MeasuredSum, RunWindow, Scorecard, Suggestion,
} from '../../improve-api';
import { numericStyle, theme } from '../../styles/tokens';

let isAdmin = false;
vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => isAdmin }));

/**
 * The REAL payload the loop emits, committed as a fixture.
 *
 * Same discipline as `agent/tests/fixtures/plan_state_golden.json`: the runtime payload is
 * generated and gitignored, so a committed slice of real output is what the tests run on. A
 * hand-written fixture would drift from the emitter and assert a shape nothing produces.
 *
 * Regenerate with:
 *   cd agent && uv run python -m improve.mine_local \
 *     --emit-wire ../ui/src/components/improve/__fixtures__/improveData.fixture.json \
 *     --created-at 2026-08-25T04:00:00Z
 */
const data = fixture as unknown as ImproveData;
const suggestions = data.suggestions;

/**
 * The same payload plus the signed-in actor's OWN bundle — the second DynamoDB item, served only
 * to the holder of that actor's token.
 *
 * The Inbox's two per-run sections read from here rather than from the shared bundle, because a
 * diverted row is one tenant's failing session and the shared copies carry no id. The run ids and
 * the facet evidence below are the REAL ones the fixture's own residue and suggestion were mined
 * from; they moved item, not shape.
 */
const ACTOR_RESIDUE_RUN = 'a3a8fac0-5b4b-4f4a-9a1e-4f0b0e0b1d2c';
const withActor = (over: Partial<ImproveData['actor']> = {}): ImproveData => ({
  ...data,
  actor: {
    runs: [],
    runsTotal: 1,
    evidence: {
      [suggestions[0].suggestionId]: {
        shapeFacets: ['fanout_parent_ran_early'],
        memberRunIds: ['e2cb300b-3fbf-4434-8d70-2007c241e4b2'],
        rootCauseHypo: 'sg2_diarize_speech was recorded finished at '
          + '2026-08-13T18:04:33.783333+00:00 before its dependency sg1_separate_stems',
      },
    },
    waste: {
      runsWithWaste: 0, runs: 1, runsNotScanned: 0, totalCalls: 0,
      totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 }, byKind: {}, findings: [],
    },
    patterns: {
      admitted: [], evidenceGaps: [], unattributed: [],
      residue: [{ runId: ACTOR_RESIDUE_RUN, symptom: ['tier_failed:dispatch_correct'],
                  bestShape: ['gateway_reported_failure'], passHits: 1,
                  reason: 'REQ-018: no shape conjunction (<= 4 facets) separates this failure '
                    + 'from 1 passing run(s)' }],
      environment: [],
    },
    ...over,
  } as ImproveData['actor'],
});

/**
 * The real payload has an empty environment bucket, so the divert path needs one built — on BOTH
 * items, which is what the producer does with one diverted failure: an anonymised row in the shared
 * bundle for the admin's coverage audit, and the owner's copy with the run id on their own bundle.
 */
const DEGRADED_SYMPTOM = 'tier_failed:results_delivered';
const DEGRADED_REASON = "platform was DEGRADED during this run's window: 2 alarms firing "
  + 'account-wide — this is NOT the agent. Attribution stood down; route to ops / incident review.';
const withEnvironment: ImproveData = {
  ...withActor({
    patterns: {
      admitted: [], evidenceGaps: [], unattributed: [], residue: [],
      environment: [{ runId: 'degraded-run-example', symptom: [DEGRADED_SYMPTOM],
                      bestShape: [], passHits: -1, reason: DEGRADED_REASON }],
    },
  }),
  patterns: {
    ...data.patterns,
    environment: [{ symptom: [DEGRADED_SYMPTOM], bestShape: [], passHits: -1,
                    reason: DEGRADED_REASON }],
  },
};

/**
 * The golden set attributes every admitted pattern, so the unowned bucket needs one built.
 *
 * Copied verbatim from a real harvest run's wire output (`mine_result_live.json`): the facet is
 * `dispatch_unpaired_result_unobserved`, which `propose._FACET_KEYWORDS` leaves unmapped
 * DELIBERATELY, and the reason is the producer's own sentence, jargon and all.
 */
const withUnattributed: ImproveData = {
  ...data,
  patterns: {
    ...data.patterns,
    unattributed: [{
      shape: 'dispatch_unpaired_result_unobserved',
      shapeFacets: ['dispatch_unpaired_result_unobserved'],
      runCount: 2,
      reason: 'no Σ component owns this pattern: no registry search terms are defined for '
        + "['dispatch_unpaired_result_unobserved']",
      plainReason: 'No part of the system has been catalogued as the owner of this kind of '
        + 'failure yet, so there is nothing here to recommend a change to.',
    }],
  },
};

/**
 * The same payload with a real emitted bundle's spend terms on it, and a personal bundle beside
 * them.
 *
 * TRANSCRIBED rather than imported: the bundles these came from live outside `ui/` and are not
 * committed, so a test that read them would pass here and fail on a fresh clone. Every scorecard
 * token term is `UNMEASURED` over `measuredOf: 0` of six runs, and the two personal runs are
 * measured — which is the shape that matters for a mount. This bundle shows figures where the
 * committed fixture, carrying neither key, shows the one cause behind having none.
 *
 * BOTH halves are here because the block reads both: the default three come off `actor` and the
 * other eight off `scorecard`, so a mount that handed over only one half would look right until a
 * reader opened the picker. Only the terms those metrics read are filled, hence the cast: the real
 * bundle already omits fields `Scorecard` declares as required, so a partial object is the shape
 * that arrives.
 */
const WIRE_RUNS = 6;
const unmeasuredOverWire: MeasuredSum = { sum: 'UNMEASURED', measuredOf: 0, total: WIRE_RUNS };
/**
 * The reader's own two runs and the two days they ran on, from a staging bundle.
 *
 * Their sums are 6,573,271 tokens and 127 calls, so the figure on screen is one no other surface
 * produces. BOTH halves, because the wire carries both and they are read for different things: the
 * day rows are what the personal totals are summed from, and the run rows are the evidence half the
 * suggestion cards read.
 */
const MINE_RUNS = 2;
/**
 * How many of a day's sessions recorded each term, as the producer writes it: one session count for the
 * whole day and a per-term count beside it.
 *
 * The same on both days, because both are the same shape — one session, which recorded busy time, tokens
 * and calls and no price. `0` for the price is a measurement, not an absence: the day was counted and its
 * session was not priced, which is what makes the cost footnote below read `0 of 2` rather than going
 * silent. `total` is a SESSION count and not the row's `runs`, which counts requests; neither of these
 * two requests fanned out, so the two happen to agree.
 */
const MINE_COVERAGE = {
  total: 1, tokens: 1, llmCalls: 1, latencyMs: 1,
  cost: 0, costTokensPriced: 0, costTokensUnpriced: 0,
  byCategory: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};
const mine = {
  runs: [
    { spend: { latencyMs: 1_705_441, tokens: 1_961_718, cost: 'UNMEASURED', llmCalls: 44 } },
    { spend: { latencyMs: 7_398_989, tokens: 4_611_553, cost: 'UNMEASURED', llmCalls: 83 } },
  ],
  history: [
    { date: '2026-08-30', runs: 1, latencyMs: 1_705_441, tokens: 1_961_718, cost: 'UNMEASURED',
      llmCalls: 44, coverage: MINE_COVERAGE },
    { date: '2026-09-03', runs: 1, latencyMs: 7_398_989, tokens: 4_611_553, cost: 'UNMEASURED',
      llmCalls: 83, coverage: MINE_COVERAGE },
  ],
  runsTotal: MINE_RUNS,
  evidence: {},
  waste: {
    runsWithWaste: 0, runs: MINE_RUNS, runsNotScanned: 0, totalCalls: 0,
    totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: MINE_RUNS }, byKind: {}, findings: [],
  },
  patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
} as unknown as ActorBundle;
const withScorecard: ImproveData = {
  ...data,
  actor: mine,
  scorecard: {
    spend: {
      tokens: unmeasuredOverWire,
      llmCalls: unmeasuredOverWire,
      byCategory: {
        input: unmeasuredOverWire,
        output: unmeasuredOverWire,
        cacheRead: unmeasuredOverWire,
        cacheWrite: unmeasuredOverWire,
      },
    },
  } as unknown as Scorecard,
};

const load = () => Promise.resolve(data);

beforeEach(() => {
  isAdmin = false;
  // The metric block reads its selection from `lab-improve-metrics` on mount, and jsdom keeps
  // storage for the whole file — so without this a test that changed the selection would decide
  // what the next one renders.
  localStorage.clear();
});

// --- sentinels --------------------------------------------------------------
describe('sentinel rendering', () => {
  it('renders an unmeasured number hatched and labelled, never as a value', () => {
    render(<MeasuredValue value="UNMEASURED" />);
    const node = screen.getByTestId('sentinel');
    expect(node).toBeInTheDocument();
    expect(node).toHaveTextContent('not measured');
    // Explicitly NOT a zero, which would read as "instant" or "free".
    expect(node).not.toHaveTextContent('0');
    expect(node.getAttribute('aria-label')).toContain('not a passing value');
  });

  it('renders a measured number plainly', () => {
    render(<MeasuredValue value={0.4} format={(n) => n.toFixed(2)} />);
    expect(screen.queryByTestId('sentinel')).toBeNull();
    expect(screen.getByText('0.40')).toBeInTheDocument();
  });

  it('gives NO_DATA and UNKNOWN their own words, not a shared blank', () => {
    const { rerender } = render(<MeasuredValue value="NO_DATA" />);
    expect(screen.getByTestId('sentinel')).toHaveTextContent('no data');
    rerender(<MeasuredValue value="UNKNOWN" />);
    expect(screen.getByTestId('sentinel')).toHaveTextContent('unknown');
  });
});

// --- the two absences -------------------------------------------------------
/*
 * Both badges say "no figure here" about different facts — the sentinel that the producer measured
 * and got nothing, `UnavailableBadge` that there was never anything to measure — and the two route
 * a reader to different actions: chase the measurement, or accept a property of the data. Now that
 * both treatments are defined in one module, nothing but the assertions below keeps them apart, and
 * a shared treatment that made them look alike would have defeated its own purpose.
 */
describe('an unmeasured value against no reading at all', () => {
  it('says a different word for each, in an element of its own', () => {
    const { rerender } = render(<SentinelValue value="UNMEASURED" />);
    expect(screen.getByTestId('sentinel').textContent).toBe('not measured');

    rerender(<UnavailableBadge />);
    expect(screen.getByTestId('unavailable-badge').textContent).toBe('unavailable');
    // Not the sentinel wearing another word: the two are separately addressable, which is what a
    // caller's test can assert about and a screen reader hears as two different statements.
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('hatches only the value something tried to measure', () => {
    // Hatching is what carries the distinction into a monochrome screenshot. Giving it to both, or
    // dropping it from the sentinel, collapses the two absences into one look.
    render(<><SentinelValue value="UNMEASURED" /><UnavailableBadge /></>);
    const sentinel = screen.getByTestId('sentinel');
    const badge = screen.getByTestId('unavailable-badge');
    expect(sentinel.style.backgroundImage).toContain('repeating-linear-gradient');
    expect(badge.style.backgroundImage).toBe('');
    // And the border says it again at any size, where 2px hatching is too fine to resolve.
    expect(sentinel.style.border).toContain('dashed');
    expect(badge.style.border).toContain('solid');
    expect(badge.style.border).not.toContain('dashed');
  });

  it('grounds the neutral badge on tokens, and on a ground that is not a status', () => {
    /*
     * A hex or an `rgba()` would be right in one of the three themes in `styles/global.css` and
     * wrong in two. `surfaceSubtle` specifically — the neutral furniture ground — because "no
     * reading" is not a verdict: a `tint()` of success, warning or error would grade the absence.
     */
    render(<UnavailableBadge />);
    const badge = screen.getByTestId('unavailable-badge');
    expect(badge.style.background).toBe(theme.colors.surfaceSubtle);
    expect(badge.style.background).not.toContain('color-mix');
    expect(badge.style.color).toBe(theme.colors.textTertiary);
    // Badge-sized, like every other absence marker here: a figure-sized one would read as the
    // value it is standing in for.
    expect(badge.style.fontSize).toBe(theme.typography.sizes.badge);
  });

  it('leaves the sentence explaining an absence to whatever renders the badge', () => {
    /*
     * Deliberately tooltip-less, where `SentinelValue` and every other badge in the module carry
     * their own `title`. Why a metric has no reading is prose the producer writes per metric, so a
     * fixed sentence here would be a generic tooltip shadowing a specific one — see
     * `MetricBlock.test.tsx`, which pins the enclosing slot carrying it instead.
     */
    render(<UnavailableBadge />);
    expect(screen.getByTestId('unavailable-badge')).not.toHaveAttribute('title');
    render(<SentinelValue value="UNMEASURED" />);
    expect(screen.getByTestId('sentinel').getAttribute('title')).toContain('No measurement exists');
  });
});

// --- how a measured figure is set -------------------------------------------
/*
 * The number is set on a span of `MeasuredValue`'s own, so an inline declaration there beats
 * anything a caller puts on a wrapper — which is the whole reason the `style` prop exists. Both
 * halves of it are pinned here: that passing nothing still gets the numeric spine, which is what
 * every caller but the metric tile relies on, and that a style is merged over that spine rather
 * than replacing it. A later tidy-up that dropped either would otherwise be silent.
 */
describe('measured figures', () => {
  it('sets a figure in the numeric spine when the caller passes no style', () => {
    render(<MeasuredValue value={0.4} format={(n) => n.toFixed(2)} />);
    const figure = screen.getByText('0.40');
    expect(figure.style.fontFamily).toBe(numericStyle.fontFamily);
    expect(figure.style.fontVariantNumeric).toBe(numericStyle.fontVariantNumeric);
  });

  it('merges a caller style over that default rather than replacing it', () => {
    // Overrides the family and names nothing else: the caller's family has to win, and the
    // tabular figures it never mentioned have to survive.
    render(<MeasuredValue value={0.4} format={(n) => n.toFixed(2)}
                          style={{ fontFamily: theme.typography.fontFamily }} />);
    const figure = screen.getByText('0.40');
    expect(figure.style.fontFamily).toBe(theme.typography.fontFamily);
    expect(figure.style.fontFamily).not.toBe(numericStyle.fontFamily);
    expect(figure.style.fontVariantNumeric).toBe(numericStyle.fontVariantNumeric);
  });

  it('keeps that style off the sentinel, which has a treatment of its own', () => {
    // A display-size body-sans style reaching `SentinelValue` would set a large hatched slab in
    // place of the small badge every other absent value on this surface gets.
    const { container } = render(<MeasuredValue value="UNMEASURED" style={tileValueStyle} />);
    const badge = screen.getByTestId('sentinel');
    // Nothing carrying the style wraps it either: the badge is the whole of what was rendered.
    expect(container.firstElementChild).toBe(badge);
    expect(badge.style.fontSize).toBe(theme.typography.sizes.badge);
    expect(badge.style.fontFamily).toBe(numericStyle.fontFamily);
    expect(badge.style.fontVariantNumeric).toBe(numericStyle.fontVariantNumeric);
  });
});

// --- non-gating checks ------------------------------------------------------
describe('process checks', () => {
  it('marks an informational check as informational and does not style it as gating', () => {
    render(<ProcessCheckRow name="DagCompletionValidator" outcome="FAIL"
                            explanation="ordering violated" gating={false} />);
    expect(screen.getByTestId('check-informational')).toBeInTheDocument();
    expect(screen.queryByTestId('check-gating')).toBeNull();
    expect(screen.getByText('informational')).toBeInTheDocument();
  });

  it('marks a gating check differently', () => {
    render(<ProcessCheckRow name="results_delivered" outcome="FAIL"
                            explanation="nothing delivered" gating />);
    expect(screen.getByTestId('check-gating')).toBeInTheDocument();
    expect(screen.queryByText('informational')).toBeNull();
  });
});

// --- population figures -----------------------------------------------------
/*
 * Every magnitude below is measured, not chosen to be convenient.
 *
 * `HARVEST_*` are sums over a 336-hour staging harvest of 553 sessions, 354 of which carry a
 * `usage` block. `WIRE_*` come from a real emitted bundle's `scorecard`. Both are read-only
 * artifacts outside the repo tree, so the numbers are transcribed here rather than imported;
 * a formatter tested only on round numbers passes while getting every real one wrong.
 */
const HARVEST_SESSIONS = 553;
const HARVEST_SESSIONS_WITH_USAGE = 354;
const HARVEST_TOKENS = 211_662_174;
const HARVEST_CALLS = 5_406;
/** Per-category sums over the same population. The last two are here for their shape: one
 *  rounds up across a unit boundary, one lands exactly on `.0`. */
const HARVEST_INPUT_TOKENS = 22_163;
const HARVEST_HAIKU_TOKENS = 49_977;
const HARVEST_LARGEST_SESSION_TOKENS = 14_137_760;
/**
 * The harvest's busy time: every span's `duration_ms` summed per session and then over all 553.
 *
 * A sum of durations, not a wall clock — nothing bounds it by the fortnight the sessions ran in,
 * and runs whose nodes worked in parallel contribute more than their own window.
 */
const HARVEST_BUSY_MS = 412_524_975;

/**
 * Union of all 553 session windows — a fortnight.
 *
 * `derived` is true because the harvest contains no stamped window at all: every session's
 * provenance is `derived_span_bounds` (522) or `derived_plan_and_span_bounds` (31).
 */
const HARVEST_WINDOW: RunWindow = {
  start: '2026-08-16T00:55:07.159000+00:00',
  end: '2026-08-30T00:18:28.385000+00:00',
  derived: true,
  provenance: 'derived_span_bounds',
};

/** A population window inside one afternoon, from a real bundle's `scorecard.window`. */
const WIRE_WINDOW: RunWindow = {
  start: '2026-08-13T17:20:56.699000+00:00',
  end: '2026-08-13T23:12:45.403000+00:00',
  derived: true,
  provenance: 'union_of_member_windows',
};

/** A real single session that ran past midnight: under a day, but over two dates. */
const OVERNIGHT_SESSION_WINDOW: RunWindow = {
  start: '2026-08-27T19:36:09.073000+00:00',
  end: '2026-08-28T15:11:00.421000+00:00',
  derived: true,
  provenance: 'derived_span_bounds',
};

/** The harvest's longest single session: 9 days, which is why per-run and per-population
 *  windows cannot use different formatters. */
const LONGEST_SESSION_WINDOW: RunWindow = {
  start: '2026-08-16T01:26:49.274000+00:00',
  end: '2026-08-25T11:29:15.835000+00:00',
  derived: true,
  provenance: 'derived_span_bounds',
};

describe('compactNumber', () => {
  it('renders the real population sums at their own magnitude', () => {
    expect(compactNumber(HARVEST_TOKENS)).toBe('211.7M');
    expect(compactNumber(HARVEST_LARGEST_SESSION_TOKENS)).toBe('14.1M');
    expect(compactNumber(HARVEST_CALLS)).toBe('5.4k');
    expect(compactNumber(HARVEST_INPUT_TOKENS)).toBe('22.2k');
    // Under a thousand stays exact: a session count is small enough to read whole.
    expect(compactNumber(HARVEST_SESSIONS)).toBe('553');
    expect(compactNumber(HARVEST_SESSIONS_WITH_USAGE)).toBe('354');
  });

  it('keeps a trailing .0 rather than dropping to a bare unit', () => {
    // 49,977 is 50.0k, not 50k. One decimal always present means two figures in a row are
    // the same width, and `50k` would claim a precision it does not have.
    expect(compactNumber(HARVEST_HAIKU_TOKENS)).toBe('50.0k');
    expect(compactNumber(1000)).toBe('1.0k');
    expect(compactNumber(1_000_000)).toBe('1.0M');
  });

  it('changes unit at the point rounding would otherwise print 1000.0k', () => {
    // The bug this pins: 999,950 / 1000 rounds to 1000.0, so choosing the unit from the raw
    // value yields `1000.0k` — a thousand thousand, spelled in thousands.
    expect(compactNumber(999_949)).toBe('999.9k');
    expect(compactNumber(999_950)).toBe('1.0M');
    expect(compactNumber(999)).toBe('999');
  });

  it('stays in M past a billion instead of inventing a unit nobody agreed on', () => {
    // No `B` tier exists. `1000.0M` is the honest consequence and is pinned here so that
    // adding one is a decision somebody makes on purpose, not a silent change of meaning.
    expect(compactNumber(999_950_000)).toBe('1000.0M');
    expect(compactNumber(999_949_999)).toBe('999.9M');
  });

  it('refuses a count that cannot exist rather than dressing it as one', () => {
    // A negative token count and a non-finite one are both producer bugs. Rendered anyway
    // they read as `-211.7M` and `NaNk`, which a reader has no way to distrust.
    for (const impossible of [-HARVEST_TOKENS, -1, Number.NaN,
                              Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(compactNumber(impossible)).toBe('not a count');
      expect(compactNumber(impossible)).not.toMatch(/\d/);
    }
    // Zero is a real measurement and is not swept in with them.
    expect(compactNumber(0)).toBe('0');
  });
});

/*
 * The smallest of the three formatters, and the one the other durations delegate to under an
 * hour — so a change here moves a window caption and a metric tile at once. Hence the boundaries
 * that must NOT move are pinned alongside the one that was fixed.
 */
describe('formatMs', () => {
  it('carries a rounded remainder into the minute instead of printing 60 seconds', () => {
    // Flooring the minutes and rounding the seconds independently reads 3_599_999 as `59m 60s`,
    // which is not a duration. Reachable from any window within half a second of the hour, and
    // from a sub-hour total-time figure.
    expect(formatMs(3_599_999)).toBe('60m 0s');
    expect(formatMs(3_599_999)).not.toMatch(/60s/);
    // The carry begins where the remainder starts rounding up, and the value below it holds.
    expect(formatMs(3_599_500)).toBe('60m 0s');
    expect(formatMs(3_599_499)).toBe('59m 59s');
    // What the caller shows, since `formatExtent` keeps its hour tier at exactly 3_600_000: a
    // window a millisecond short of an hour reads as sixty minutes, not as a 60-second minute.
    expect(formatExtent(3_599_999)).toBe('60m 0s');
    expect(formatExtent(3_600_000)).toBe('1h 0m');
  });

  it('promotes a second into the minute rather than printing sixty of them', () => {
    // The same defect one tier down: the seconds tier is entered on the RAW value and the
    // figure rounded inside it, so 59_999 rounded to `60.0s` — sixty seconds, in the unit
    // the tier exists to stay below. Reachable from a window or a total just under a minute.
    expect(formatMs(59_999)).toBe('1m 0s');
    // The promotion begins where the tenth starts rounding up, and the value below it holds.
    expect(formatMs(59_950)).toBe('1m 0s');
    expect(formatMs(59_949)).toBe('59.9s');
    // Swept rather than sampled: the promotion is a threshold, and sampling walks past it.
    const sixties: number[] = [];
    for (let ms = 1000; ms < 60_000; ms++) {
      if (/^60\.\ds$/.test(formatMs(ms))) sixties.push(ms);
    }
    expect(sixties).toEqual([]);
    // What the caller shows — `formatExtent` delegates here below the hour.
    expect(formatExtent(59_999)).toBe('1m 0s');
  });

  it('rounds an exact half-tenth up, at every magnitude alike', () => {
    // The price of choosing the tier from the rounded value, and it is the cheaper side of the
    // trade. `ms / 100` represents a half-tenth exactly where `ms / 1000` does not, so 1150ms
    // now rounds up to 1.2s where it used to round DOWN to 1.1s while 1250ms rounded up — a
    // split that came from binary representation and that no reader could account for.
    expect(formatMs(1150)).toBe('1.2s');
    expect(formatMs(1250)).toBe('1.3s');
    // Only the exact half moves: a millisecond either side of it rounds as it always did.
    expect(formatMs(1149)).toBe('1.1s');
    expect(formatMs(1151)).toBe('1.2s');
    expect(formatMs(1249)).toBe('1.2s');
  });

  it('leaves the pinned figures and the unit boundaries where they were', () => {
    // A real bundle's total latency over 6 of 6 measured runs — the figure `MetricTile.test`
    // pins on the tile — and the harvest busy time `formatExtent`'s docstring cites as the
    // reason it exists.
    expect(formatMs(12_182_621)).toBe('203m 3s');
    expect(formatMs(HARVEST_BUSY_MS)).toBe('6875m 25s');
    expect(formatMs(999)).toBe('999ms');
    expect(formatMs(1000)).toBe('1.0s');
    expect(formatMs(60_000)).toBe('1m 0s');
    expect(formatExtent(412)).toBe('412ms');
    expect(formatExtent(3_599_000)).toBe('59m 59s');
  });
});

/*
 * The same formatter serves a window's extent and a sum of many runs' durations, so the cases
 * below are the ones where those two quantities could come apart: the magnitude a sum reaches
 * that no window here does, the unit boundaries, and a total too small or too broken to be one.
 */
describe('formatExtent', () => {
  it('reads a population-scale total in days rather than in minutes', () => {
    // 114.6 hours of busy time, rounded to the hour it prints. `formatMs` renders the same
    // figure `6875m 25s`, which is true and unreadable.
    expect(formatExtent(HARVEST_BUSY_MS)).toBe('4d 19h');
    expect(formatExtent(HARVEST_BUSY_MS)).not.toMatch(/6875/);
  });

  it('changes unit exactly at the hour and at the day', () => {
    expect(formatExtent(3_600_000)).toBe('1h 0m');
    expect(formatExtent(3_599_000)).toBe('59m 59s');
    expect(formatExtent(86_400_000)).toBe('1d 0h');
    expect(formatExtent(86_340_000)).toBe('23h 59m');
  });

  it('keeps a small total in the unit it was measured in', () => {
    // A population of one short run. `0ms` is a measured zero — no busy time — and stays a
    // figure; the sentinels are for a total that was never computed.
    expect(formatExtent(412)).toBe('412ms');
    expect(formatExtent(0)).toBe('0ms');
  });

  it('refuses a duration that cannot exist rather than dressing it as one', () => {
    // A sum of latencies cannot be negative or non-finite, so either is a producer bug. Put
    // through the tiers they read `NaNd NaNh` and `Infinityd NaNh`, which look like durations.
    for (const impossible of [-1, -HARVEST_BUSY_MS, Number.NaN,
                              Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(formatExtent(impossible)).toBe('not a duration');
      expect(formatExtent(impossible)).not.toMatch(/\d/);
    }
  });

  it('leaves the wall-clock reading of a negative span to the window formatter', () => {
    // `ends before it starts` is a sentence about two timestamps. A sum has no start and no end
    // to be out of order, so the generic formatter must not borrow the diagnosis.
    expect(formatExtent(-1)).not.toMatch(/ends before/);
    expect(formatWindow({ ...WIRE_WINDOW, start: WIRE_WINDOW.end, end: WIRE_WINDOW.start }))
      .toMatch(/ends before it starts/);
  });
});

describe('formatWindow', () => {
  it('reads as one afternoon for a same-day population window', () => {
    expect(formatWindow(WIRE_WINDOW))
      .toBe('13 Aug 17:20 → 23:12 (5h 52m, reconstructed)');
    // The date is stated once, not on both ends: a same-day range does not need it twice.
    expect(formatWindow(WIRE_WINDOW).match(/Aug/g)).toHaveLength(1);
  });

  it('reads as a fortnight for the harvest window, in days rather than minutes', () => {
    expect(formatWindow(HARVEST_WINDOW))
      .toBe('16 Aug 00:55 → 30 Aug 00:18 (13d 23h, reconstructed)');
    // `formatMs` tops out at minutes, so reusing it here would print `20123m 21s`.
    expect(formatWindow(HARVEST_WINDOW)).not.toMatch(/20123/);
    // Without a date on both ends, `00:55 -> 00:18` would read as 23 minutes.
    expect(formatWindow(HARVEST_WINDOW).match(/Aug/g)).toHaveLength(2);
  });

  it('names both dates for a window that crosses midnight but lasts under a day', () => {
    expect(formatWindow(OVERNIGHT_SESSION_WINDOW))
      .toBe('27 Aug 19:36 → 28 Aug 15:11 (19h 35m, reconstructed)');
  });

  it('expresses a nine-day single session in days too', () => {
    expect(formatWindow(LONGEST_SESSION_WINDOW))
      .toBe('16 Aug 01:26 → 25 Aug 11:29 (9d 10h, reconstructed)');
  });

  it('takes the duration from the timestamps, not from the truncated clock', () => {
    // A real 1h 0m 31s session. The clock reads 13:31 -> 14:31, which looks like exactly an
    // hour; the extent says 1h 1m because it is computed at full precision. Recomputing it
    // from the displayed times would round the 31 seconds away.
    expect(formatWindow({
      start: '2026-08-29T13:31:26.434000+00:00',
      end: '2026-08-29T14:31:57.712000+00:00',
      derived: true,
      provenance: 'derived_plan_and_span_bounds',
    })).toBe('29 Aug 13:31 → 14:31 (1h 1m, reconstructed)');
  });

  it('carries minutes up into hours instead of printing 1h 60m', () => {
    // A boundary probe rather than an observation: 1h 59m 40s. Decomposing the raw value
    // gives hours=1 and a minute remainder that rounds to 60.
    expect(formatWindow({
      start: '2026-08-13T17:20:00.000000+00:00',
      end: '2026-08-13T19:19:40.000000+00:00',
      derived: false,
      provenance: 'collector_stamped',
    })).toBe('13 Aug 17:20 → 19:19 (2h 0m)');
  });

  it('says nothing about reconstruction when the window was stamped', () => {
    // No window in either data source is stamped, so the flag is the synthesised part here;
    // the timestamps are the real same-day ones. Without this the marker could be a constant.
    expect(formatWindow({ ...WIRE_WINDOW, derived: false }))
      .toBe('13 Aug 17:20 → 23:12 (5h 52m)');
  });

  it('reports an unmeasured window without producing anything numeric', () => {
    for (const sentinel of ['NO_DATA', 'UNKNOWN', 'UNMEASURED'] as const) {
      // Reading `.start` off a sentinel string yields `undefined`, which `new Date` turns
      // into an Invalid Date — so the guard has to come first.
      expect(formatWindow(sentinel)).toBe('extent not recorded');
      expect(formatWindow(sentinel)).not.toMatch(/\d/);
    }
  });

  it('echoes a bound it cannot parse instead of rendering Invalid Date', () => {
    // `-` is the harvester's spelling for an absent bound — 522 of the 553 window notes
    // record `plan bounds -..-` — so a window built from two absent bounds is reachable.
    const out = formatWindow({
      start: '-', end: '-', derived: true, provenance: 'derived_plan_and_span_bounds',
    });
    expect(out).toBe('- → - (extent unreadable, reconstructed)');
    expect(out).not.toMatch(/Invalid Date|NaN/);
  });

  it('says a window ends before it starts rather than printing a negative duration', () => {
    expect(formatWindow({ ...WIRE_WINDOW, start: WIRE_WINDOW.end, end: WIRE_WINDOW.start }))
      .toBe('13 Aug 23:12 → 17:20 (ends before it starts, reconstructed)');
  });
});

describe('fractionLabel', () => {
  it('spells the fraction out, so the denominator cannot be read away', () => {
    // The real bundle: latency measured on all 6 runs, tokens on none of them.
    expect(fractionLabel(6, 6)).toBe('6 of 6');
    expect(fractionLabel(0, 6)).toBe('0 of 6');
    expect(fractionLabel(HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS)).toBe('354 of 553');
    // Not a slash, which reads as division, and not a percentage, which drops the count.
    expect(fractionLabel(HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS)).not.toMatch(/[/%]/);
  });

  it('says there was nothing to measure rather than 0 of 0', () => {
    // What an unpopulated bundle produces — the actor bundle above carries exactly this on
    // `totalWallMs`. `0 of 0` invites the reader to compute 0%, which is a different claim.
    expect(fractionLabel(0, 0)).toBe('nothing to measure');
    expect(fractionLabel(0, 0)).not.toMatch(/\d/);
  });

  it('leaves an impossible fraction visible instead of clamping it to complete', () => {
    // A numerator over its denominator is a producer bug. Clamped to `6 of 6` it would
    // assert full coverage that nothing computed, which is the failure mode this whole
    // surface exists to prevent.
    expect(fractionLabel(7, 6)).toBe('7 of 6');
  });

  it('refuses a fraction whose terms are not numbers', () => {
    expect(fractionLabel(Number.NaN, 6)).toBe('not a count');
    expect(fractionLabel(6, Number.NaN)).toBe('not a count');
    expect(fractionLabel(Number.POSITIVE_INFINITY, 6)).toBe('not a count');
  });
});

describe('tileValueStyle', () => {
  it('sets a display figure in the body sans, semibold, from tokens', () => {
    expect(tileValueStyle.fontFamily).toBe(theme.typography.fontFamily);
    expect(tileValueStyle.fontWeight).toBe(theme.typography.weights.semibold);
  });

  it('uses proportional figures, unlike the numeric spine it must not reuse', () => {
    // `numericStyle` is mono with `tabular-nums` so counts align down a COLUMN. A display
    // figure stands alone, and equal-width digits make a short number look loose. A future
    // tidy-up that spreads `numericStyle` in here fails on both of these.
    expect(tileValueStyle.fontVariantNumeric).toBe('proportional-nums');
    expect(tileValueStyle.fontFamily).not.toBe(numericStyle.fontFamily);
  });

  it('carries no fontSize, leaving the scale to whatever renders it', () => {
    expect(tileValueStyle.fontSize).toBeUndefined();
  });
});

// --- affordances drive the controls ----------------------------------------
function renderCard(suggestion: Suggestion) {
  const onAction = vi.fn();
  render(<SuggestionCard suggestion={suggestion} expanded onToggle={() => {}}
                         onAction={onAction} />);
  return onAction;
}

describe('suggestion card actions', () => {
  const measured = suggestions.find((s) => s.targetId === 'chainer-readiness-rule')!;

  it('hides Publish entirely for a code_change target', () => {
    renderCard(measured);
    expect(screen.queryByTestId('action-publish')).toBeNull();
    expect(screen.getByTestId('disposition-code_change')).toBeInTheDocument();
  });

  it('disables Try both with the server-supplied reason', () => {
    renderCard(measured);
    const button = screen.getByTestId('action-try-both');
    expect(button).toBeDisabled();
    expect(button.getAttribute('title')).toContain('platform code');
  });

  it('offers Send to team when the fix needs an engineer', () => {
    const onAction = renderCard(measured);
    fireEvent.click(screen.getByTestId('action-send-to-team'));
    expect(onAction).toHaveBeenCalledWith('send-to-team', measured);
  });

  it('never offers Revert, because RollbackAsset is unwired', () => {
    renderCard(measured);
    expect(screen.queryByTestId('action-revert')).toBeNull();
  });




  it('says plainly when a suggestion has no text diff', () => {
    renderCard(measured);
    expect(screen.getByTestId('diff-empty')).toBeInTheDocument();
  });

  it('warns when attribution was inferred rather than declared by the registry', () => {
    renderCard(measured);
    expect(screen.getByText(/inferred attribution, not declared/)).toBeInTheDocument();
  });

  it('renders a sentinel for an unquantified claim, never bare prose', () => {
    // Every real proposal has `magnitude: null`, and the card used to render "results
    // delivered improves" with no number and no sentinel, confident prose with nothing
    // behind it, on the one genuinely-mined suggestion.
    renderCard(measured);
    const sentinels = screen.getAllByTestId('sentinel');
    expect(sentinels.length).toBeGreaterThan(0);
    expect(sentinels.some((n) => n.textContent?.includes('not measured'))).toBe(true);
  });

  it('shows how good the evidence is, not just what the finding says', () => {
    renderCard(measured);
    const block = screen.getByTestId('evidence-quality');
    // How many runs the finding rests on, agreeing in number with its noun. Taken from the wire
    // and matched next to the word: the block holds a bare `1` whatever the count is, because
    // the caption below has one in its date, its clock and its extent.
    expect(measured.evidenceQuality!.runCount).toBe(1);
    expect(block.textContent ?? '').toMatch(/1\s*run(?!s)/);
    // The wall clock, which is NOT the ~7min of tool time in `utility.latencyMs`. Exact, and
    // dated: a substring match on `18:02` alone also passes off the caveat text below, which
    // quotes the same start bound.
    expect(block).toHaveTextContent('13 Aug 18:02 → 18:43 (40m 40s, reconstructed)');
  });

  it('renders platform UNKNOWN as a caveat, never as healthy', () => {
    // A `degraded: false` default would have reported an expired lookup as a clean platform.
    renderCard(measured);
    expect(screen.getByTestId('platform-unknown')).toBeInTheDocument();
    expect(screen.queryByTestId('platform-healthy')).toBeNull();
    expect(screen.getByTestId('platform-unknown'))
      .toHaveAttribute('title', expect.stringContaining('Not the same as healthy'));
  });

  it('warns what a reconstructed window costs, and says "reconstructed" once', () => {
    // Nothing stamps a window today, and a derived one narrows during the outage it should
    // detect, so the reviewer has to see that the timing is inferred — and has to see it in the
    // caption, the warning or the caveat, not in all three. Every window here is derived, so
    // the warning carries the consequence and the caption carries the marker.
    renderCard(measured);
    expect(screen.getByTestId('derived-window-warning'))
      .toHaveTextContent(/no collector recorded these timings, so the extent may be short/);
    const block = screen.getByTestId('evidence-quality');
    expect(block.textContent!.match(/reconstructed/g)).toHaveLength(1);
  });

  it('states the caveats a reviewer should weigh', () => {
    renderCard(measured);
    const block = screen.getByTestId('evidence-quality');
    expect(block).toHaveTextContent(/one run/);
    expect(block).toHaveTextContent(/alarm history only covers back to/);
  });


  it('shows no forced-single-candidate note when several candidates exist', () => {
    // The real proposal offers two genuinely different remedies, so the note must be absent.
    // It appears only when the proposer could produce one and recorded why.
    expect(measured.candidates.length).toBeGreaterThan(1);
    expect(measured.singleCandidateForced).toBeNull();
    renderCard(measured);
    expect(screen.queryByTestId('single-candidate-forced')).toBeNull();
  });


});

// --- the evidence window on a card -----------------------------------------
/**
 * `evidenceQuality.window` is the UNION over the member runs, so the block is rendered
 * directly here: the fixture's own suggestion rests on a single same-day run, and the extents
 * that decide whether this caption is readable are the long ones.
 */
describe('EvidenceQualityBlock', () => {
  const real = suggestions
    .find((s) => s.targetId === 'chainer-readiness-rule')!.evidenceQuality!;

  it('dates both ends of a window longer than a day, and says how long in days', () => {
    // The harvest's longest single session, just over nine days. Only the window is swapped;
    // the rest of the record is the fixture's own. Times alone rendered this as
    // `01:26 -> 11:29`, which reads as ten hours, and its extent as a bare `13562m`.
    render(<EvidenceQualityBlock quality={{ ...real, window: LONGEST_SESSION_WINDOW }} />);
    const block = screen.getByTestId('evidence-quality');
    expect(block).toHaveTextContent('16 Aug 01:26 → 25 Aug 11:29 (9d 10h, reconstructed)');
    expect(block).not.toHaveTextContent('13562');
  });

  it('keeps the count when only some of the windows were reconstructed', () => {
    // Unreachable on today's data — every window in a 336-hour harvest of 553 sessions is
    // `derived_span_bounds` or `derived_plan_and_span_bounds`, none collector-stamped — so the
    // mix is synthesised. This is the branch where the number is the information: `3 of 6` says
    // what the caption's own marker cannot, so here the lead stays.
    render(<EvidenceQualityBlock quality={{ ...real, runCount: 6, derivedWindows: 3 }} />);
    const warning = screen.getByTestId('derived-window-warning');
    expect(warning).toHaveTextContent('3 of 6 timings reconstructed');
    expect(warning).toHaveTextContent(/no collector recorded them/);
  });

  it('keeps the hatched sentinel for an unmeasured window, never a plain caption', () => {
    // `formatWindow` answers a sentinel with the plain string `extent not recorded`, so routing
    // one through it would quietly demote absent data to prose on the surface whose whole job
    // is to keep the two apart.
    render(<EvidenceQualityBlock quality={{ ...real, window: 'UNKNOWN' }} />);
    expect(screen.getByTestId('sentinel')).toHaveTextContent('unknown');
    expect(screen.getByTestId('evidence-quality'))
      .not.toHaveTextContent('extent not recorded');
  });
});

// --- the surface ------------------------------------------------------------
describe('ImproveCenter', () => {
  it('shows a skeleton before the first load and content after', async () => {
    let resolve: (value: ImproveData) => void = () => {};
    const pending = new Promise<ImproveData>((r) => { resolve = r; });
    render(<ImproveCenter load={() => pending} />);
    expect(screen.getByTestId('improve-skeleton')).toBeInTheDocument();
    resolve(data);
    await waitFor(() => expect(screen.queryByTestId('improve-skeleton')).toBeNull());
  });

  it('keeps stale content visible when a refresh fails, with the banner over it', async () => {
    const flaky = vi.fn()
      .mockResolvedValueOnce(data)
      .mockRejectedValueOnce(new Error('network down'));
    render(<ImproveCenter load={flaky} />);
    await screen.findByTestId(`suggestion-${suggestions[0].suggestionId}`);
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));
    await screen.findByTestId('error-banner');
    // The point: the data the user was reading is still there.
    expect(screen.getByTestId(`suggestion-${suggestions[0].suggestionId}`))
      .toBeInTheDocument();
  });


  it('lists failures that produced no suggestion, so coverage is not implied', async () => {
    render(<ImproveCenter load={() => Promise.resolve(withActor())} />);
    const section = await screen.findByTestId('unexplained-failures');
    expect(section).toHaveTextContent(/produced no suggestion/);
    // The reader's OWN run, named. The shared bundle's copy of the same row carries no id.
    expect(section).toHaveTextContent(ACTOR_RESIDUE_RUN.slice(0, 8));
    // And the row has TEXT: the engine's reason survives on the wire because it states counts
    // rather than session ids. A row with only a lead would be a blank explanation.
    expect(section).toHaveTextContent(/no shape conjunction/);
    expect(section).not.toHaveTextContent(/passing run\(s\) [0-9a-f]{8}/);
  });

  it('shows no per-run sections at all when this actor has no bundle', async () => {
    // The NORMAL state, not an edge case: the producer is a daily cron over a bounded harvest
    // window, so between any deploy and the next run nobody has a bundle — and an actor with no
    // conversational session never gets one. The shared answer still renders; only the sections
    // that would have to name somebody's run are absent.
    expect(data.actor).toBeUndefined();
    render(<ImproveCenter load={load} />);
    expect(await screen.findByTestId(`suggestion-${suggestions[0].suggestionId}`))
      .toBeInTheDocument();
    expect(screen.queryByTestId('unexplained-failures')).toBeNull();
    expect(screen.queryByTestId('environment-failures')).toBeNull();
  });

  it("shows the reader's own facet evidence on a card, and nothing where it has none", async () => {
    // `rootCauseHypo` names plan node ids and timestamps out of one session, so it left the shared
    // suggestion. A reader who owns one of the pattern's runs sees it; a reader who owns none sees
    // the claim alone, which is the true state rather than a gap.
    render(<ImproveCenter load={() => Promise.resolve(withActor())} />);
    fireEvent.click((await screen.findAllByTestId('toggle-detail'))[0]);
    expect(screen.getByTestId('root-cause-hypo')).toHaveTextContent('sg2_diarize_speech');

    render(<ImproveCenter load={load} />);
    fireEvent.click((await screen.findAllByTestId('toggle-detail'))[0]);
    expect(screen.queryByTestId('root-cause-hypo')).toBeNull();
  });

  it('sources the honesty sections from the LOADED data, not a mock import', async () => {
    // The bug this pins: the Inbox used to import the two lists straight from `mockData`
    // while the loader put the same facts in `patterns`. Once the API landed, the surface
    // would have kept rendering the mock, silently. Loading empty patterns must empty
    // the sections.
    const empty: ImproveData = {
      ...withActor(),
      patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [] },
      actor: undefined,
    };
    render(<ImproveCenter load={() => Promise.resolve(empty)} />);
    await screen.findByTestId('metric-block');
    expect(screen.queryByTestId('unexplained-failures')).toBeNull();
    expect(screen.queryByTestId('environment-failures')).toBeNull();
  });

  it('separates a platform-degraded failure from an unexplained one', async () => {
    render(<ImproveCenter load={() => Promise.resolve(withEnvironment)} />);
    const section = await screen.findByTestId('environment-failures');
    // Not the agent's fault is a different claim from we-cannot-explain-it, so it must not
    // share the unexplained heading.
    expect(section).toHaveTextContent(/not the agent's fault/i);
    expect(section).toHaveTextContent(/platform degraded/i);
  });


  it('promises no work in the header when nothing is actionable, though a suggestion is open', async () => {
    // The real state: one open suggestion, zero the user can apply, because the only mined
    // recommendation targets platform code. A count over `status === 'open'` promised work here,
    // and the badge that carried it went with the tab bar — so the header must stay figureless
    // rather than acquire a count somewhere else.
    const open = suggestions.filter((s) => s.status === 'open').length;
    const actionable = suggestions.filter((s) => s.status === 'open' && (
      s.affordances.canPublish || s.affordances.canTryBoth)).length;
    expect(open).toBeGreaterThan(0);
    expect(actionable).toBe(0);
    render(<ImproveCenter load={load} />);
    await screen.findByTestId(`suggestion-${suggestions[0].suggestionId}`);
    const header = screen.getByRole('heading', { name: 'Agent report' }).parentElement!;
    expect(header.textContent).not.toMatch(/\d/);
  });

  it('shows the admin the platform-degraded bucket too', async () => {
    // Findings rendered 3 of the 4 mining outcomes: the USER inbox showed degraded failures
    // and the ADMIN view did not.
    isAdmin = true;
    render(<ImproveCenter load={() => Promise.resolve(withEnvironment)} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    expect(screen.getByText(/Platform degraded/)).toBeInTheDocument();
    // The row itself says why, from the engine — this reason names alarms, which are
    // account-wide infrastructure identifiers rather than anybody's session.
    expect(screen.getByTestId('environment-0')).toHaveTextContent(/alarms firing account-wide/);
  });

  it('moves a dismissed suggestion out of the list, writing the decision to the store', async () => {
    // `dismiss` is injected so the optimistic move is exercised without the network; the default
    // hits the authenticated proxy, which the test harness rejects.
    const dismiss = vi.fn().mockResolvedValue(undefined);
    render(<ImproveCenter load={load} dismiss={dismiss} />);
    const id = suggestions[0].suggestionId;
    const card = await screen.findByTestId(`suggestion-${id}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-dismiss'));
    // The write really fires now (was inert), AND the row leaves the list so the count moves and
    // the user can tell they finished.
    expect(dismiss).toHaveBeenCalledWith(id);
    expect(await screen.findByTestId('handled')).toBeInTheDocument();
    expect(screen.queryByTestId(`suggestion-${id}`)).toBeNull();
  });

  it('can undo a handled suggestion, retracting the stored decision', async () => {
    const dismiss = vi.fn().mockResolvedValue(undefined);
    const undo = vi.fn().mockResolvedValue(undefined);
    render(<ImproveCenter load={load} dismiss={dismiss} undo={undo} />);
    const id = suggestions[0].suggestionId;
    const card = await screen.findByTestId(`suggestion-${id}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-dismiss'));
    fireEvent.click(await screen.findByTestId(`undo-${id}`));
    expect(undo).toHaveBeenCalledWith(id);
    expect(await screen.findByTestId(`suggestion-${id}`)).toBeInTheDocument();
  });

  it('renders a dismissal stored on a previous session as an undoable handled row, not vanished', async () => {
    // The overlay that hides a just-dismissed row is per-mount, so a decision made earlier arrives
    // only as the server `status`. It must land in the handled list WITH an undo path — not fall
    // out of `open` and disappear, since the DELETE that would revive it is reachable only through
    // that button.
    const id = suggestions[0].suggestionId;
    const dismissedData: ImproveData = {
      ...data,
      suggestions: data.suggestions.map((s) => (
        s.suggestionId === id ? { ...s, status: 'dismissed' as const } : s)),
    };
    const undo = vi.fn().mockResolvedValue(undefined);
    render(<ImproveCenter load={() => Promise.resolve(dismissedData)} undo={undo} />);
    await screen.findByTestId('handled');
    // Not stranded in the open list...
    expect(screen.queryByTestId(`suggestion-${id}`)).toBeNull();
    // ...present as a handled row, and undoing it retracts the stored decision on the server.
    fireEvent.click(screen.getByTestId(`undo-${id}`));
    expect(undo).toHaveBeenCalledWith(id);
    expect(await screen.findByTestId(`suggestion-${id}`)).toBeInTheDocument();
  });

  it('keeps a dismissal after the inbox remounts, without a manual refresh', async () => {
    // Following the Advanced link unmounts the inbox and empties its optimistic overlay, so the
    // decision has to persist in the loaded data or the row springs back to open. Dismiss, follow
    // the link and come back: the row must still read as handled with no Refresh.
    const dismiss = vi.fn().mockResolvedValue(undefined);
    render(<ImproveCenter load={load} dismiss={dismiss} />);
    const id = suggestions[0].suggestionId;
    const card = await screen.findByTestId(`suggestion-${id}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-dismiss'));
    // Wait for the write to resolve so `data` is patched before the remount.
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith(id));
    await screen.findByTestId(`handled-${id}`);
    fireEvent.click(screen.getByTestId('view-components'));
    fireEvent.click(screen.getByTestId('view-components'));
    // Overlay is gone with the unmount, but the loaded data now carries the dismissal.
    expect(await screen.findByTestId(`handled-${id}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`suggestion-${id}`)).toBeNull();
  });

  it('surfaces platform-degraded failures with distinct, not-the-agent wording', async () => {
    render(<ImproveCenter load={() => Promise.resolve(withEnvironment)} />);
    const section = await screen.findByTestId('environment-failures');
    // Distinct from the unexplained set: not the agent, routed to ops.
    expect(section).toHaveTextContent(/not the agent/i);
    expect(section).toHaveTextContent(/ops/i);
    // And it is NOT folded into the unexplained section, which this payload leaves empty.
    expect(screen.queryByTestId('unexplained-failures')).toBeNull();
  });

  it('makes the suggestions the surface itself, with nothing to choose first', async () => {
    // No tab bar: the title names the surface and the list is its content, so a reader arrives on
    // the suggestions rather than selecting them from two equal halves of a navigation.
    render(<ImproveCenter load={load} />);
    expect(await screen.findByTestId(`suggestion-${suggestions[0].suggestionId}`))
      .toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Agent report' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Suggestions' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Components' })).toBeNull();
  });

  it('puts the components grid behind Advanced, and makes that link the way back', async () => {
    render(<ImproveCenter load={load} />);
    const advanced = await screen.findByTestId('view-components');
    expect(advanced).toHaveTextContent('Advanced');
    // Behind the link: the grid is not on the surface until somebody asks for it.
    expect(screen.queryByTestId('section-fix-first')).toBeNull();
    fireEvent.click(advanced);
    expect(screen.getByTestId('section-fix-first')).toBeInTheDocument();
    expect(screen.queryByTestId(`suggestion-${suggestions[0].suggestionId}`)).toBeNull();
    // And back out. The link is the only control that appeared, so if it did not return the reader
    // to the suggestions the grid would be a dead end.
    fireEvent.click(advanced);
    expect(screen.getByTestId(`suggestion-${suggestions[0].suggestionId}`)).toBeInTheDocument();
    expect(screen.queryByTestId('section-fix-first')).toBeNull();
  });

  it('gives that link a keyboard-reachable control that announces which view is showing', async () => {
    /*
     * A `div onClick` is neither focusable nor announced. Same shape as
     * `settings/CapabilitySwitch` — a real <button type="button"> carrying its state in an aria
     * attribute rather than in colour — but `aria-pressed`, not that file's `role="switch"`: this
     * changes which view is on screen, it does not turn a setting on.
     */
    render(<ImproveCenter load={load} />);
    const advanced = await screen.findByTestId('view-components');
    expect(advanced.tagName).toBe('BUTTON');
    expect(advanced).toHaveAttribute('type', 'button');
    expect(advanced).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(advanced);
    expect(advanced).toHaveAttribute('aria-pressed', 'true');
  });

  it('renders one variant alone, with nothing to navigate off it, in solo mode', async () => {
    isAdmin = true;
    render(<ImproveCenter load={load} solo="components" />);
    // Await the REPORT BODY, not the header: `solo-banner` is header chrome rendered immediately,
    // so awaiting it does not guarantee the async report resolved — under CI's parallel load the
    // body was still the loading skeleton and the sync getByTestId('section-fix-first') raced it.
    expect(await screen.findByTestId('section-fix-first')).toBeInTheDocument();
    expect(screen.getByTestId('solo-banner')).toBeInTheDocument();
    // The pin IS the mode: a link back to the suggestions, or on to Findings, would defeat it.
    expect(screen.queryByTestId('view-components')).toBeNull();
    expect(screen.queryByTestId('view-findings')).toBeNull();
  });

  it('offers the link when no variant is pinned', async () => {
    render(<ImproveCenter load={load} solo={null} />);
    expect(await screen.findByTestId('view-components')).toBeInTheDocument();
    expect(screen.queryByTestId('solo-banner')).toBeNull();
  });

  it('hides the admin Findings link from non-admins', async () => {
    render(<ImproveCenter load={load} />);
    await screen.findByTestId('view-components');
    expect(screen.queryByTestId('view-findings')).toBeNull();
  });

  it('shows Findings to an admin, including what mining rejected and why', async () => {
    isAdmin = true;
    render(<ImproveCenter load={load} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    expect(screen.getByText(/Rejected as non-discriminative/)).toBeInTheDocument();
    // Keyed by POSITION: a residue row has no identity on the shared wire, because a session id
    // is one tenant's and the ids of the passing runs that disqualified it are other tenants'.
    // What survives is the coverage claim — the shape tried, and how many passing runs matched.
    const residue = data.patterns.residue[0];
    const row = screen.getByTestId('residue-0');
    expect(row).toHaveTextContent(residue.bestShape.join(' AND '));
    // Singular, because the fixture's own `passHits` is 1 — this row read `1 passing run(s)`.
    // Asserted with a lookahead rather than a substring: `1 passing run` alone also passes off
    // `1 passing runs`, which is the defect.
    expect(residue.passHits).toBe(1);
    expect(row).toHaveTextContent(/1 passing run(?!s)/);
    // The reason travels; the ids in it do not. The committed fixture predates that change, so
    // this asserts on the shape of what is rendered rather than on the fixture's own wording.
    expect(row).toHaveTextContent(/no shape conjunction/);
  });

  it('tells an end user in the Inbox about a pattern nothing can act on yet', async () => {
    // The whole point of the task: before this, the ONLY rendering was the admin Findings tab,
    // so a user saw no change at all. Non-admin on purpose.
    render(<ImproveCenter load={() => Promise.resolve(withUnattributed)} />);
    const section = await screen.findByTestId('unowned-patterns');
    expect(section).toHaveTextContent(/not yet traced to a component/);
    // Plain wording, and specifically NOT the engine's: no facet name, no Σ, no bracketed list.
    expect(section).toHaveTextContent(/nothing in the catalogue of system components/);
    expect(section).toHaveTextContent(/No part of the system has been catalogued/);
    expect(section).not.toHaveTextContent('dispatch_unpaired_result_unobserved');
    expect(section).not.toHaveTextContent('Σ');
    // Its own heading, not folded into the unexplained set — a different claim.
    expect(screen.queryByTestId('unexplained-failures')).toBeNull();
  });

  it('falls back to the engine reason when the producer supplied no plain wording', async () => {
    // A bundle written before `propose_all` labelled the cause. Jargon on screen is the honest
    // degradation; a blank row, or a sentence guessed from three candidates, would not be.
    const { plainReason: _dropped, ...noPlain } = withUnattributed.patterns.unattributed![0];
    render(<ImproveCenter load={() => Promise.resolve({
      ...data, patterns: { ...data.patterns, unattributed: [noPlain] },
    })} />);
    const section = await screen.findByTestId('unowned-patterns');
    expect(section).toHaveTextContent(/no registry search terms are defined/);
  });

  it('hides the Inbox section at zero, unlike the same bucket on Findings', async () => {
    // Different audiences, different right answers: the Inbox is "specific recommendations
    // only", so an empty section is clutter; Findings is a coverage audit, where a vanished
    // section is a claim about what was checked. Both states asserted here so a later change
    // that unifies them fails loudly.
    render(<ImproveCenter load={() => Promise.resolve({
      ...data, patterns: { ...data.patterns, unattributed: [] },
    })} />);
    await screen.findByTestId('metric-block');
    expect(screen.queryByTestId('unowned-patterns')).toBeNull();
  });

  it('shows nothing in the Inbox when the bundle predates the key', async () => {
    // The committed fixture carries no `unattributed` at all. `undefined` and `[]` are the same
    // render HERE — the distinction is drawn on Findings, where it is actionable.
    expect(data.patterns.unattributed).toBeUndefined();
    render(<ImproveCenter load={load} />);
    await screen.findByTestId('metric-block');
    expect(screen.queryByTestId('unowned-patterns')).toBeNull();
  });

  it('shows an admitted pattern nothing owns, with the reason it is unowned', async () => {
    // The defect: the producer computed this record and `to_wire` dropped it, so a pattern that
    // mined cleanly and then found no owner was invisible on every screen. A count alone would
    // not be enough — the reason names the facets, which is what a map-or-refuse call rests on.
    isAdmin = true;
    render(<ImproveCenter load={() => Promise.resolve(withUnattributed)} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    const entry = screen.getByTestId('unattributed-0');
    expect(entry).toHaveTextContent('dispatch_unpaired_result_unobserved');
    expect(entry).toHaveTextContent(/no registry search terms are defined/);
    // Distinct from the residue heading: this pattern was NOT rejected, it was admitted.
    expect(screen.getByText(/Admitted, not yet traced to a component/)).toBeInTheDocument();
    expect(screen.queryByTestId('unattributed-none')).toBeNull();
  });

  it('renders two entries that share one shape as two rows, not one', async () => {
    // The real harvest emits exactly this: mining groups by (symptom, shape) and only `shape`
    // reaches the wire, so same-shape siblings are normal and the record has no id to key on.
    // Keying on shape collapsed them to one row and warned about duplicate keys.
    isAdmin = true;
    const twin = withUnattributed.patterns.unattributed![0];
    const pair: ImproveData = {
      ...data,
      patterns: { ...data.patterns, unattributed: [twin, { ...twin, runCount: 1 }] },
    };
    render(<ImproveCenter load={() => Promise.resolve(pair)} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    expect(screen.getByTestId('unattributed-0')).toBeInTheDocument();
    expect(screen.getByTestId('unattributed-1')).toBeInTheDocument();
  });

  it('says "none" explicitly when every admitted pattern found an owner', async () => {
    // The golden set's real state. A bucket that vanished at zero would be indistinguishable
    // from a render failure, and from a bundle that never carried the key.
    isAdmin = true;
    const attributed: ImproveData = {
      ...data,
      patterns: { ...data.patterns, unattributed: [] },
    };
    render(<ImproveCenter load={() => Promise.resolve(attributed)} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    expect(screen.getByTestId('unattributed-none')).toHaveTextContent(/every admitted pattern/);
    expect(screen.queryByTestId('unattributed-absent')).toBeNull();
  });

  it('distinguishes a bundle that predates the key from one reporting zero', async () => {
    // The committed fixture is a REAL pre-producer bundle: it carries no `unattributed` at all,
    // which is the state a UI build deployed ahead of a producer run sees. Absent ≠ empty, so it
    // must not render as "none" — that would claim a measurement nothing made.
    isAdmin = true;
    expect(data.patterns.unattributed).toBeUndefined();
    render(<ImproveCenter load={load} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    expect(screen.getByTestId('unattributed-absent')).toHaveTextContent(/not zero/);
    expect(screen.queryByTestId('unattributed-none')).toBeNull();
  });

  it('expands a suggestion in place instead of navigating to the Inbox', async () => {
    // Previously this set a filter and switched view to the Inbox: you lost your scroll position,
    // the honesty sections were suppressed by the filter, and in solo mode it broke the pin.
    render(<ImproveCenter load={load} />);
    fireEvent.click(await screen.findByTestId('view-components'));
    fireEvent.click(screen.getByTestId('select-chainer-readiness-rule'));
    expect(screen.getByTestId('inline-suggestion-chainer-readiness-rule')).toBeInTheDocument();
    expect(screen.getByTestId('view-components')).toHaveAttribute('aria-pressed', 'true');
  });

  it('cannot be navigated out of a pinned variant', async () => {
    render(<ImproveCenter load={load} solo="components" />);
    await screen.findByTestId('solo-banner');
    // Await the loaded rule selector — under slow CI the async `load` resolves
    // after solo-banner, so getByTestId raced the skeleton (flaky). findBy waits.
    fireEvent.click(await screen.findByTestId('select-chainer-readiness-rule'));
    // Still Components: the pin outranks the local view.
    expect(screen.getByTestId('section-fix-first')).toBeInTheDocument();
    expect(screen.queryByTestId('unexplained-failures')).toBeNull();
  });

  it('separates fix_first, admin and locked rather than lumping them together', async () => {
    render(<ImproveCenter load={load} />);
    fireEvent.click(await screen.findByTestId('view-components'));
    expect(screen.getByTestId('section-fix-first')).toBeInTheDocument();
    expect(screen.getByTestId('section-admin')).toBeInTheDocument();
    expect(screen.getByTestId('section-locked')).toBeInTheDocument();
    expect(screen.getByTestId('section-locked')).toHaveTextContent(/default-deny/);
  });

  it('shows permissions in words, not registry enum names', async () => {
    render(<ImproveCenter load={load} />);
    fireEvent.click(await screen.findByTestId('view-components'));
    const row = screen.getByTestId('component-chainer-readiness-rule');
    // LOCKED reads as "locked", never as the registry's enum spelling.
    expect(row).toHaveTextContent('locked');
    expect(row).not.toHaveTextContent('LOCKED');
  });

  it('puts a suggestion back and surfaces the error when the dismiss write fails', async () => {
    // A failed write must not leave the row in "handled" as if it stuck, and must not blank the
    // pane the way a failed load does, it shows over the content.
    const dismiss = vi.fn().mockRejectedValue(new Error('overlay write failed'));
    render(<ImproveCenter load={load} dismiss={dismiss} />);
    const id = suggestions[0].suggestionId;
    const card = await screen.findByTestId(`suggestion-${id}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-dismiss'));
    expect(dismiss).toHaveBeenCalledWith(id);
    expect(await screen.findByTestId('mutation-error-banner'))
      .toHaveTextContent(/overlay write failed/);
    // Reverted: the suggestion is back in the open list, not stranded in "handled".
    expect(await screen.findByTestId(`suggestion-${id}`)).toBeInTheDocument();
  });

  it('still reports a genuinely inert action honestly instead of pretending it worked', async () => {
    // Send-to-team has no write path yet, so it must say so rather than imply an escalation was
    // recorded. (Dismiss, by contrast, is now wired.)
    render(<ImproveCenter load={load} />);
    const card = await screen.findByTestId(`suggestion-${suggestions[0].suggestionId}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-send-to-team'));
    expect(await screen.findByTestId('notice-banner')).toHaveTextContent(/not wired yet/);
  });
});


// --- the population figures on the surface ----------------------------------
/**
 * Where the metric block sits, which surfaces it appears on, and what it is fed.
 *
 * Nothing here re-tests the block: which three metrics, the cap, the picker and the
 * absent-against-unmeasured rule are all pinned in `MetricBlock.test.tsx`. What is only checkable
 * from here is the mount — position relative to the Inbox, the tabs it is and is not on, and that
 * the bundle reaching it is the one this surface loaded. Both real shapes are covered, because they
 * render differently: the committed fixture collapses to one line, a scorecard-bearing bundle
 * does not.
 */
describe('the metric band above the Inbox', () => {
  it('sits above the suggestion list, collapsed, when a structural cause takes every tile',
     async () => {
    // Neither key: no `scorecard` and no `actor`. The three default metrics are the reader's OWN,
    // so the cause said once here is the one about them — one structural cause in place of three
    // tiles that would each repeat it.
    //
    // This is the COMMITTED FIXTURE's shape, not the deployed producer's: staging's shared bundle
    // carries a scorecard (25 runs, 20 passing) and seven actors have a bundle of their own. What
    // makes the state worth pinning is the personal half — a reader who had no session in the
    // harvest window has no `actor` key, which is most readers on most days.
    expect(data.scorecard).toBeUndefined();
    expect(data.actor).toBeUndefined();
    render(<ImproveCenter load={load} />);
    const block = await screen.findByTestId('metric-block');
    const firstCard = screen.getByTestId(`suggestion-${suggestions[0].suggestionId}`);
    // Above, not below: the first card FOLLOWS the block in document order.
    expect(block.compareDocumentPosition(firstCard) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
    const collapsed = screen.getByTestId('metric-block-collapsed');
    // One phrase, in the reader's own words. The producer's four-sentence account of why is on the
    // line as its tooltip and on every picker row — `MetricBlock.test.tsx` pins both. What is at
    // stake here is that the top of THIS page is the phrase.
    expect(collapsed).toHaveTextContent("Your own figures aren't in this report yet");
    expect(collapsed.getAttribute('title')).toMatch(/no bundle of your own in this data/);
    expect(screen.queryByTestId('metric-row')).toBeNull();
  });

  it('shows figures instead of that cause once the loaded bundle carries them', async () => {
    // What proves the block is handed the LOADED bundle rather than something standing in for it:
    // the fixture above yields no figures at all, and nothing else on this surface produces a
    // denominator of two runs.
    render(<ImproveCenter load={() => Promise.resolve(withScorecard)} />);
    await screen.findByTestId('metric-row');
    expect(screen.getAllByTestId('metric-tile')).toHaveLength(3);
    expect(screen.queryByTestId('metric-block-collapsed')).toBeNull();
    // Two days measured tokens and calls; neither was priced, which is what the deployed producer
    // emits — so the third footnote is its own, and the price beside it is hatched.
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent)).toEqual([
      `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
      `across ${MINE_RUNS} runs · ${MINE_RUNS} of ${MINE_RUNS} sessions`,
      // The price gains a third clause the other two do not. With no figure to mis-attribute, the
      // producer's own account of its rates is what explains the absence — and this bundle's
      // scorecard states none, which is itself said rather than left silent. Its fraction is `0 of 2`
      // where the other two are whole: the same two sessions, counted for a term neither recorded.
      `across ${MINE_RUNS} runs · 0 of ${MINE_RUNS} sessions · pricing basis not stated`,
    ]);
    expect(screen.getAllByTestId('metric-tile-value').map((el) => el.textContent))
      .toEqual(['6.6M', '127', 'not measured']);
    // The basis is beside the numbers rather than behind the gear, and names the first day the
    // total reaches back to.
    expect(screen.getByTestId('metric-basis')).toHaveTextContent('since 30 Aug');
  });

  it('hands over the shared half of the payload as well as the personal one', async () => {
    /*
     * The block opens on the reader's own days, so the test above would pass on a mount that dropped
     * `scorecard` — and every figure at the shared scope would then be unavailable for a reason
     * nothing on the wire supports. A stored scope is what puts the shared half under test, and
     * `0 of 6 sessions` is a denominator only this payload can produce.
     */
    localStorage.setItem('lab-improve-metrics', JSON.stringify({
      metrics: ['tokens', 'llmCalls', 'contextVsGenerated'], scope: 'everyone',
    }));
    render(<ImproveCenter load={() => Promise.resolve(withScorecard)} />);
    await screen.findByTestId('metric-row');
    expect(screen.getAllByTestId('metric-tile-sub').map((el) => el.textContent))
      .toEqual([`0 of ${WIRE_RUNS} sessions`, `0 of ${WIRE_RUNS} sessions`,
                `0 of ${WIRE_RUNS} sessions`]);
    /*
     * And the band says nothing else. The denominator above is what a shared figure needs, and it is
     * on each tile; the two block-wide lines that used to sit here — the harvest window with the
     * population's shape, and the period's refusal — described the wire to a reader who asked for
     * numbers.
     */
    expect(screen.queryByTestId('metric-basis')).toBeNull();
    expect(screen.queryByTestId('metric-period-refused')).toBeNull();
  });

  it('appears on the Inbox alone, not on Components and not on admin Findings', async () => {
    // On the scorecard-bearing bundle deliberately: with the collapsing fixture an absent block
    // could be the view or could be the data, and the assertion would prove neither.
    isAdmin = true;
    render(<ImproveCenter load={() => Promise.resolve(withScorecard)} />);
    expect(await screen.findByTestId('metric-block')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('view-components'));
    expect(screen.getByTestId('section-fix-first')).toBeInTheDocument();
    expect(screen.queryByTestId('metric-block')).toBeNull();

    fireEvent.click(screen.getByTestId('view-findings'));
    expect(screen.getByText(/Rejected as non-discriminative/)).toBeInTheDocument();
    expect(screen.queryByTestId('metric-block')).toBeNull();
  });

  it('stays in the soloed inbox variant, which shows that surface as it really is', async () => {
    // `?improve=inbox` strips the surface to its content and pins it there. The band is part of
    // the Inbox, not chrome around it, so dropping it here would review a surface nobody ships —
    // whereas the link out is chrome, and is correctly gone.
    render(<ImproveCenter load={() => Promise.resolve(withScorecard)} solo="inbox" />);
    await screen.findByTestId('solo-banner');
    expect(await screen.findByTestId('metric-block')).toBeInTheDocument();
    expect(screen.queryByTestId('view-components')).toBeNull();
  });

  it('stays out of the soloed components variant', async () => {
    render(<ImproveCenter load={() => Promise.resolve(withScorecard)} solo="components" />);
    await screen.findByTestId('section-fix-first');
    expect(screen.queryByTestId('metric-block')).toBeNull();
  });

  it('survives a dismiss patching the loaded data, rather than being rebuilt by it', async () => {
    // `setSuggestionStatus` replaces `data` with a new object, so the block is handed a fresh
    // reference on every successful dismiss. That has to be a re-render: the block holds which
    // three metrics are shown and whether its picker is open, and a mount keyed or re-wrapped per
    // payload would discard both while the reader was using them.
    const dismiss = vi.fn().mockResolvedValue(undefined);
    render(<ImproveCenter load={load} dismiss={dismiss} />);
    const block = await screen.findByTestId('metric-block');
    const id = suggestions[0].suggestionId;
    const card = screen.getByTestId(`suggestion-${id}`);
    fireEvent.click(card.querySelector('[data-testid="toggle-detail"]')!);
    fireEvent.click(screen.getByTestId('action-dismiss'));
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith(id));
    await screen.findByTestId(`handled-${id}`);
    // The same DOM node: React re-rendered it in place instead of mounting a replacement.
    expect(screen.getByTestId('metric-block')).toBe(block);
  });
});

describe('readSoloVariant', () => {
  it('accepts the three candidate variants', () => {
    expect(readSoloVariant('?improve=inbox')).toBe('inbox');
    expect(readSoloVariant('?improve=components')).toBe('components');
  });

  it('rejects anything else, including the admin surface and the dropped variant', () => {
    // `findings` is admin-gated and never was a candidate; `workloads` was dropped.
    expect(readSoloVariant('?improve=findings')).toBeNull();
    expect(readSoloVariant('?improve=workloads')).toBeNull();
    expect(readSoloVariant('?improve=nonsense')).toBeNull();
    expect(readSoloVariant('')).toBeNull();
  });
});

/**
 * The plural helper the whole surface now shares.
 *
 * It replaced three spellings of one idea across twelve sites, two of them wrong at a count of one:
 * `(s)` at nine, a bare ` runs` at one, and a correct-but-duplicated ternary at two. Single-digit
 * populations make one the ordinary case, not the corner — the real bundle's only measured
 * component health has exactly one run, and its residue row exactly one pass hit.
 */
describe('pluralise', () => {
  it('gives the singular at exactly one and the plural everywhere else', () => {
    expect(pluralise(1, 'run')).toBe('run');
    expect(pluralise(2, 'run')).toBe('runs');
    // Zero is plural in English, and this surface renders zeroes: `0 runs`, not `0 run`.
    expect(pluralise(0, 'run')).toBe('runs');
    expect(pluralise(MINE_RUNS, 'run')).toBe('runs');
  });

  it('inflects every noun this surface counts, not just runs', () => {
    // The other three nouns it is used on. One helper, so a fourth cannot arrive spelled `(s)`.
    expect(pluralise(1, 'failure')).toBe('failure');
    expect(pluralise(3, 'failure')).toBe('failures');
    expect(pluralise(1, 'pattern')).toBe('pattern');
    expect(pluralise(2, 'pattern')).toBe('patterns');
    expect(pluralise(1, 'suggestion')).toBe('suggestion');
    expect(pluralise(4, 'suggestion')).toBe('suggestions');
  });

  it('never emits the lazy `(s)` it exists to remove', () => {
    for (const count of [0, 1, 2, 7]) {
      expect(pluralise(count, 'run')).not.toContain('(');
    }
  });

  it('stays plural for a count no producer should emit, rather than inventing a singular', () => {
    // A negative or NaN count is a producer bug. English has no singular for either, and the
    // wrong FIGURE beside the noun is what a reader must see — not a noun that quietly agrees
    // with it. `fractionLabel` is where a non-finite count is named as such.
    expect(pluralise(Number.NaN, 'run')).toBe('runs');
    expect(pluralise(-1, 'run')).toBe('runs');
  });
});

/**
 * The other prose helper, and here for `pluralise`'s reason: the surface had TWO copies of one cut.
 * `MetricBlock` previewed an unavailable metric's reason at 120 characters and `metricRegistry`
 * previewed a cost basis at 64, each with its own "slice, find the last space, trim, append an
 * ellipsis". Different limits, identical mechanics — so the mechanics live in one place and the
 * limits stay at the two call sites, which is where they are actually different.
 */
describe('abridge', () => {
  it('returns a sentence that already fits, untouched and unmarked', () => {
    // The ellipsis is a promise that there is more. Added to a whole sentence it claims an
    // abridgement that did not happen, and a reader who goes looking finds nothing.
    expect(abridge('rates re-sourced 2026-12-01', 64)).toBe('rates re-sourced 2026-12-01');
    expect(abridge('exactly ten', 11)).toBe('exactly ten');
    expect(abridge('', 64)).toBe('');
  });

  it('cuts on a word and marks the cut', () => {
    const basis = 'rates were read by hand from the vendor pricing page on a date this sentence '
      + 'declines to name';
    const cut = abridge(basis, 64);
    expect(cut.endsWith('…')).toBe(true);
    // A prefix of what was said, not a rewrite of it — and it stops on a word, because a preview
    // that ends mid-token reads as corruption rather than as abridgement.
    expect(basis.startsWith(cut.slice(0, -1))).toBe(true);
    expect(cut.slice(0, -1).endsWith(' ')).toBe(false);
    expect(cut.length).toBeLessThanOrEqual(65);
  });

  it('hard-cuts a single token with no word boundary to break on', () => {
    /*
     * The real case is a URL: the producer's cost basis carries
     * `https://aws.amazon.com/bedrock/pricing/` and a pricing page could be longer than the whole
     * preview. Returning the sentence whole would defeat the limit and returning nothing would
     * drop it, so it is cut where it must be.
     */
    const url = 'https://aws.amazon.com/bedrock/pricing/reference/very/long/path';
    expect(abridge(url, 20)).toBe('https://aws.amazon.c…');
    expect(url.startsWith(abridge(url, 20).slice(0, -1))).toBe(true);
  });

  it('never trails whitespace in front of the ellipsis', () => {
    // `the rates …` reads as a gap rather than as a continuation.
    expect(abridge('one two three four', 8)).toBe('one two…');
    expect(abridge('one  two  three', 6)).toBe('one…');
  });
});

/**
 * The counts the helper now words, at the sites a reader actually meets them.
 *
 * Unit tests on `pluralise` prove the rule; these prove it reached the prose. Every heading and
 * lead below counted with `(s)` or a bare plural before, and the harvest's own numbers are ones
 * and twos, so each of these was reading wrong on the live surface.
 */
describe('counts read as English on the surface, not as `run(s)`', () => {
  it('agrees the noun in each diverted heading with the count it heads', async () => {
    // One residue row on the reader's own bundle, so the singular is what renders.
    render(<ImproveCenter load={() => Promise.resolve(withActor())} />);
    const section = await screen.findByTestId('unexplained-failures');
    expect(section).toHaveTextContent('1 failure produced no suggestion');
    expect(section).not.toHaveTextContent('failure(s)');
  });

  it('agrees the noun in the platform-degraded heading too', async () => {
    render(<ImproveCenter load={() => Promise.resolve(withEnvironment)} />);
    const section = await screen.findByTestId('environment-failures');
    expect(section).toHaveTextContent("1 failure, not the agent's fault");
    expect(section).not.toHaveTextContent('failure(s)');
  });

  it('counts patterns and their runs separately, and words each for its own number', async () => {
    // The one real unattributed entry spans TWO runs, so this row is singular and plural at once —
    // which is why one helper taking the count is the only thing that gets both right.
    expect(withUnattributed.patterns.unattributed![0].runCount).toBe(2);
    render(<ImproveCenter load={() => Promise.resolve(withUnattributed)} />);
    const section = await screen.findByTestId('unowned-patterns');
    expect(section).toHaveTextContent('1 pattern not yet traced to a component');
    expect(section).toHaveTextContent('2 runs');
    expect(section.textContent ?? '').not.toContain('(s)');
  });

  it('words the run count on an admin Findings row as well as on the Inbox one', async () => {
    // The same fact is rendered twice, by two components, and only one of them was tested. So the
    // admin row is asserted here too — a helper used at one of a pair is how the pair drifts.
    isAdmin = true;
    render(<ImproveCenter load={() => Promise.resolve(withUnattributed)} />);
    fireEvent.click(await screen.findByTestId('view-findings'));
    const entry = screen.getByTestId('unattributed-0');
    expect(entry).toHaveTextContent('2 runs');
    expect(entry.textContent ?? '').not.toContain('(s)');
  });

  it('words the judged-run count on a card, where the fixture has three', async () => {
    const measured = suggestions.find((s) => s.targetId === 'chainer-readiness-rule')!;
    expect(measured.provenance.judgeRuns).toBe(3);
    renderCard(measured);
    expect(screen.getByText(/A trial is a single sample/).parentElement)
      .toHaveTextContent('over 3 runs.');
  });
});

// --- the trim on the suggestion list ----------------------------------------
/**
 * The producer trims the suggestion list to fit one DynamoDB item and stamps how many there were
 * before the cut. `suggestions.length < suggestionsTotal` is the only trace that trim leaves.
 *
 * No live bundle carries the key yet — the producer stamps it and the deployed snapshot in staging
 * does not have it — so absence is the state to get right first: it means "nothing counted", never
 * "nothing was dropped".
 */
describe('the suggestion list\'s own denominator', () => {
  const inbox = (suggestionsTotal?: number) => (
    <ImproveInbox
      suggestions={suggestions}
      suggestionsTotal={suggestionsTotal}
      onAction={() => {}}
    />
  );

  it('says how short the list is when the producer cut it', () => {
    render(inbox(40));

    expect(screen.getByTestId('suggestions-trimmed').textContent)
      .toBe(`This report carries ${suggestions.length} of 40 suggestions, and the rest were dropped `
        + 'to fit its size limit.');
  });

  it('sets the fraction in the body face, so the word `of` is not monospace', () => {
    /*
     * `numericStyle` is mono plus `tabular-nums`. The span holds what `fractionLabel` returns, which
     * is `1 of 40` — the word `of` included — so in mono a word in the middle of a sentence came out
     * in another typeface and read as a rendering fault. Tabular digits earn a column and mono earns
     * an identifier; this line is neither.
     *
     * Asserted through the inline `style`, because jsdom resolves no `var(--font-mono)` and would
     * report both faces identically.
     */
    render(inbox(40));

    const figure = screen.getByTestId('suggestions-trimmed').querySelector('span') as HTMLElement;
    expect(figure.textContent).toBe(`${suggestions.length} of 40`);
    expect(figure.style.fontFamily).not.toContain('--font-mono');
    expect(figure.style.fontFamily).toContain('--font-body');
    expect(figure.style.fontWeight).toBe('600');
  });

  it('joins its two facts with a word rather than a dash', () => {
    // The dash was doing a job the words can do: it fronted the second half of the line without
    // saying how the two halves relate. `ImproveCenter.wordBudget.test.tsx` sweeps the whole rendered
    // surface for the character; this pins the one line that used to carry it.
    render(inbox(40));
    expect(screen.getByTestId('suggestions-trimmed').textContent).not.toContain('—');
  });

  it('says nothing when the total equals the length, which would only restate it', () => {
    render(inbox(suggestions.length));

    expect(screen.queryByTestId('suggestions-trimmed')).toBeNull();
  });

  it('says nothing when nothing counted the total, rather than a reassuring fraction', () => {
    // Absent is not `0` and not "complete". A bundle written before the key exists cannot report
    // whether anything was dropped, and must not imply that nothing was.
    render(inbox(undefined));

    expect(screen.queryByTestId('suggestions-trimmed')).toBeNull();
  });

  it('reads the total off the loaded payload, and ignores anything that is not a count', async () => {
    // `ImproveData` does not declare `suggestionsTotal`, so `ImproveCenter` reads it structurally and
    // validates it. A string, a negative or a NaN is not a denominator and must not become one.
    const withTotal = { ...data, suggestionsTotal: 40 } as ImproveData;
    const counted = render(<ImproveCenter load={() => Promise.resolve(withTotal)} />);
    expect((await screen.findByTestId('suggestions-trimmed')).textContent)
      .toContain(`${suggestions.length} of 40`);
    counted.unmount();

    for (const bad of ['40', -1, Number.NaN, null]) {
      const view = render(
        <ImproveCenter load={() => Promise.resolve({ ...data, suggestionsTotal: bad } as ImproveData)} />,
      );
      // The outcome band renders on every loaded payload — with figures or with the reason it has
      // none — so it is the signal that the surface finished loading and the absence below is the
      // reader's and not the clock's.
      await screen.findByTestId('outcome-band');
      expect(screen.queryByTestId('suggestions-trimmed')).toBeNull();
      view.unmount();
    }
  });

  it('keeps the caveat under a target filter, unlike the population sections below it', async () => {
    // Those sections are claims about the whole population and would misattribute the platform's
    // failures to the filtered component. This is a caveat about what is MISSING from the report:
    // narrowing the view does not make it less true, and one that hides when the reader narrows is
    // one nobody sees.
    render(
      <ImproveInbox
        suggestions={suggestions}
        suggestionsTotal={40}
        targetFilter={suggestions[0].targetId}
        onAction={() => {}}
      />,
    );

    expect(screen.getByTestId('clear-filter')).toBeInTheDocument();
    expect(screen.getByTestId('suggestions-trimmed')).toBeInTheDocument();
  });
});
