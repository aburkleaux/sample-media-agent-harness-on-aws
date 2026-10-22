/**
 * @vitest-environment jsdom
 *
 * The band a non-technical reader meets first after what the work cost.
 *
 * Every fixture here is SHAPED on the live staging bundle and none of its figures is asserted as a
 * live figure. Those numbers moved substantially inside 24 hours — the pass rate went 0.80 → 0.696,
 * the runs carrying repeated work 9 → 4, the wall time 6m 53s → 3m 29s — so a test pinning one would
 * fail on the producer's next nightly pass and say nothing about this component. What is asserted is
 * the FORMATTING of a given input and WHICH BRANCH a given shape selects.
 *
 * The two shapes that matter most, and both are live rather than hypothetical: `unscored` is
 * non-zero, so the rate's denominator is not the population; and `history` has more than one row, so
 * the prior-period comparison is a real path and not a defensive one.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required.
 */
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { OutcomeBand } from './OutcomeBand';
import { sharePercent } from './improveChrome';
import type { PopulationDay, Scorecard, WasteTotals } from '../../improve-api';

/**
 * The live scorecard's outcome half, cast for the reason every other test of this surface casts one:
 * `Scorecard` declares `spend`, `tiers`, `outcomes` and more as required and the stored item omits
 * whatever a reading does not need. The wire and the declaration are not the same thing.
 *
 * `unscored: 2` is the live value and the reason this fixture exists rather than a tidy one: 16 + 7 +
 * 2 is 25, so `pass / total` is 64% while the producer's own rate is 16/23 — and a label taken over
 * the population would disagree with the bar beside it.
 */
function scorecard(over: Record<string, unknown> = {}): Scorecard {
  return {
    population: { total: 25, pass: 16, fail: 7, unscored: 2 },
    passRate: 16 / 23,
    ...over,
  } as unknown as Scorecard;
}

/** The live waste totals' shape. `totalWallMs` is a sum term with its own two denominators, not a
 *  bare number. */
function waste(over: Record<string, unknown> = {}): WasteTotals {
  return {
    runsWithWaste: 4,
    runs: 25,
    runsNotScanned: 0,
    totalCalls: 18,
    totalWallMs: { sum: 209_290, measuredOf: 9, total: 9 },
    byKind: {},
    ...over,
  } as unknown as WasteTotals;
}

/** Population day rows, OLDEST FIRST as the wire carries them. The two live rows point in OPPOSITE
 *  directions — the pass rate fell while the repeated work halved — which is why each tile compares
 *  its own metric and nothing here reports one verdict for both. */
const TWO_DAYS: PopulationDay[] = [
  { date: '2026-09-03', passRate: 0.8, runsWithWaste: 9 } as unknown as PopulationDay,
  { date: '2026-09-04', passRate: 16 / 23, runsWithWaste: 4 } as unknown as PopulationDay,
];

const ONE_DAY: PopulationDay[] = [TWO_DAYS[1]];

/** More recorded days than the line will draw, so its placement is exercised past its drawing bound
 *  as well as at one reading and at two. Real dates, so no caption falls back to a phrase. */
const MANY_DAYS: PopulationDay[] = Array.from({ length: 40 }, (_, index) => ({
  date: new Date(Date.UTC(2026, 7, 1 + index)).toISOString().slice(0, 10),
  passRate: 0.7,
  runsWithWaste: index,
} as unknown as PopulationDay));

const THREE_DAYS: PopulationDay[] = [
  { date: '2026-09-02', passRate: 0.5, runsWithWaste: 2 } as unknown as PopulationDay,
  ...TWO_DAYS,
];

const text = (testId: string): string => screen.getByTestId(testId).textContent ?? '';

/** The tiles in render order, by their own sub-caption — the line carrying each one's denominator. */
const subs = (): string[] => screen.getAllByTestId('metric-tile-sub')
  .map((node) => node.textContent ?? '');

/** Whether `a` comes before `b` in the document, which is the order a reader meets them in. */
const before = (a: Element, b: Element): boolean => Boolean(
  a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
);

/**
 * Whether a box lays its children out SIDE BY SIDE.
 *
 * The one thing about the placement that the DOM cannot say on its own, and the reason a structural
 * check alone is not enough: a line in a flex ROW next to a tile holding all of the row's slack is
 * that tile's sibling in the markup and half a screen from it on the page. jsdom lays nothing out, so
 * the arrangement is read off the box instead of measured.
 */
const laysOutInARow = (box: Element): boolean => {
  const { display, flexDirection } = getComputedStyle(box);
  return (display === 'flex' || display === 'inline-flex') && flexDirection.startsWith('row');
};

// --- which bundle the figures come off --------------------------------------
describe('OutcomeBand reads the shared bundle', () => {
  it('shows a figure with no per-actor bundle anywhere in the payload', () => {
    // The state most readers are in: the producer writes a personal bundle only for an actor who had
    // a session in the harvest window. The per-actor blocks answer this case with a sentence saying
    // they cannot answer, which is why these two tiles take the shared keys instead.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);

    expect(screen.getAllByTestId('metric-tile-value').map((n) => n.textContent))
      .toEqual(['70%', '3m 29s']);
    expect(screen.queryByTestId('unavailable-badge')).toBeNull();
  });

  it('labels neither figure as the reader\'s own', () => {
    // Every run of every tenant is in these numbers, so a possessive label would be false. The
    // per-actor reading of the same two questions is behind Advanced and says whose it is.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);

    const whole = text('outcome-band');
    expect(whole).toContain('Finished cleanly');
    expect(whole).toContain('Time lost to redone work');
    expect(whole).not.toMatch(/\byour\b/i);
    expect(whole).not.toMatch(/\bmine\b/i);
  });
});

// --- the rate, its denominator and its bar ----------------------------------
describe('the clean-finish rate', () => {
  it('draws the bar from the published rate and never from a share of its own', () => {
    /*
     * The two-denominators defect, forced: a rate that does NOT agree with `pass / (pass + fail)`.
     * A component recomputing the share would draw the bar at 70% and set 70% above it; one reading
     * `passRate` draws 50%. Only the second can be checked against the producer.
     */
    render(<OutcomeBand scorecard={scorecard({ passRate: 0.5 })} waste={waste()} />);

    expect(screen.getByTestId('ratio-bar-fill').style.width).toBe('50%');
    expect(text('ratio-bar-label')).toBe('50% finished cleanly / 50% did not');
    // And the fraction beside it is still the counts, unchanged by the rate: the two readings are
    // both on screen precisely so a disagreement is visible rather than resolved silently.
    expect(subs()[0]).toContain('16 of 23');
  });

  it('takes the fraction over pass + fail, not over the population', () => {
    /*
     * `dto._pass_rate` divides by `passed + failed` and leaves UNSCORED out of both terms, because a
     * run whose gating evaluator could not measure says something about the evidence and nothing
     * about the agent. Captioned with `population.total` the label would read `16 of 25` — 64% —
     * beside a bar filled to 70%.
     */
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} />);

    expect(subs()[0]).toContain('16 of 23 requests');
    expect(subs()[0]).not.toContain('16 of 25');
  });

  it('accounts for the unjudged runs rather than letting them vanish into the denominator', () => {
    // They are excluded from the rate and they still happened. Absence must not read as a pass.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} />);
    expect(subs()[0]).toContain('2 could not be judged');
  });

  it('says nothing about unjudged runs when there were none', () => {
    // `0 could not be judged` is noise: the fraction beside it already says every request was.
    render(<OutcomeBand
      scorecard={scorecard({ population: { total: 23, pass: 16, fail: 7, unscored: 0 } })}
      waste={waste()}
    />);
    expect(subs()[0]).toBe('16 of 23 requests');
  });

  it('keeps 0% and 100% for exactly none and exactly all', () => {
    // A rate that exists and rounds to nothing must not read as "nothing worked", and one a hair
    // short of everything must not read as complete.
    const { rerender } = render(<OutcomeBand
      scorecard={scorecard({ population: { total: 250, pass: 1, fail: 249, unscored: 0 },
                             passRate: 1 / 250 })}
      waste={waste()}
    />);
    expect(screen.getAllByTestId('metric-tile-value')[0]).toHaveTextContent('<1%');

    rerender(<OutcomeBand
      scorecard={scorecard({ population: { total: 250, pass: 249, fail: 1, unscored: 0 },
                             passRate: 249 / 250 })}
      waste={waste()}
    />);
    expect(screen.getAllByTestId('metric-tile-value')[0]).toHaveTextContent('>99%');

    rerender(<OutcomeBand
      scorecard={scorecard({ population: { total: 25, pass: 25, fail: 0, unscored: 0 },
                             passRate: 1 })}
      waste={waste()}
    />);
    expect(screen.getAllByTestId('metric-tile-value')[0]).toHaveTextContent('100%');
  });

  it('sets the rate through the surface-wide share rule, not a formatter of its own', () => {
    /*
     * The LINKAGE rather than the wording, which the case above already pins. This file used to hold
     * its own copy of the `<1%` / `>99%` rule, a second spelling of what the bar directly below the
     * figure draws by — so the expectation is `sharePercent`'s live answer and never a literal, and a
     * copy reintroduced here fails as soon as it disagrees with the bar.
     *
     * Every input the rule turns on: none passed, thinnest that did, a hair short of all, all, and
     * the live rate. `pass / judged` rather than the population's total, because that is the
     * denominator the producer's own rate is taken over.
     */
    for (const [pass, fail] of [[0, 25], [1, 249], [249, 1], [25, 0], [16, 7]]) {
      const judged = pass + fail;
      const { unmount } = render(<OutcomeBand
        scorecard={scorecard({ population: { total: judged, pass, fail, unscored: 0 },
                               passRate: pass / judged })}
        waste={waste()}
      />);
      expect(screen.getAllByTestId('metric-tile-value')[0].textContent, `${pass} of ${judged}`)
        .toBe(sharePercent(pass / judged));
      unmount();
    }
  });

  it('states the absence when nothing was judged, rather than drawing an empty bar', () => {
    // `passRate` is `null` on the wire exactly when pass + fail is 0. An empty track reads as 0%,
    // which is a result, and the truth is that there is no rate at all.
    render(<OutcomeBand
      scorecard={scorecard({ population: { total: 4, pass: 0, fail: 0, unscored: 4 },
                             passRate: null })}
      waste={waste()}
    />);

    expect(text('outcome-cleanly-unreadable-reason')).toContain('No request in this report was judged');
    expect(screen.queryByTestId('ratio-bar')).toBeNull();
    expect(screen.getByTestId('outcome-cleanly-unreadable'))
      .toContainElement(screen.getByTestId('unavailable-badge'));
  });

  it('says a different thing again when there is no scorecard at all', () => {
    // Two different facts with two different lifetimes: nothing was judged resolves with more
    // traffic, no scorecard resolves on the producer's next pass.
    render(<OutcomeBand waste={waste()} />);

    expect(text('outcome-cleanly-unreadable-reason'))
      .toContain('carries no population scorecard');
    expect(text('outcome-cleanly-unreadable-reason')).not.toContain('was judged');
  });

  it('shows no figure for a scorecard whose counts did not survive the round trip', () => {
    // A stored bundle is served back unchanged, so a scorecard really can arrive without the keys
    // the type marks required. Reaching through one would take the whole band down.
    render(<OutcomeBand scorecard={{} as unknown as Scorecard} waste={waste()} />);

    expect(text('outcome-cleanly-unreadable-reason'))
      .toContain('carries no readable clean-finish rate');
    expect(text('outcome-cleanly-unreadable-reason')).toContain('rather than a zero');
  });
});

// --- the time lost ----------------------------------------------------------
describe('the time lost to redone work', () => {
  it('formats the duration through the shared formatter at every magnitude', () => {
    /*
     * `formatMs` has twice been fixed for carrying a rounded remainder into the next unit, and a
     * hand-written duration reintroduces that. `formatExtent` is the entry point because the same
     * figure is minutes on one population and hours on another; under an hour it delegates.
     */
    const { rerender } = render(<OutcomeBand scorecard={scorecard()} waste={waste()} />);
    expect(screen.getAllByTestId('metric-tile-value')[1]).toHaveTextContent('3m 29s');

    // A millisecond short of the hour. The bug the formatter carries a fix for prints `59m 60s`.
    rerender(<OutcomeBand scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 3_599_999, measuredOf: 9, total: 9 },
    })} />);
    expect(screen.getAllByTestId('metric-tile-value')[1]).toHaveTextContent('60m 0s');
    expect(text('outcome-band')).not.toContain('59m 60s');

    // Past the hour, where `formatMs`'s largest unit would print `1441m 0s`.
    rerender(<OutcomeBand scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 86_460_000, measuredOf: 9, total: 9 },
    })} />);
    expect(screen.getAllByTestId('metric-tile-value')[1]).toHaveTextContent('1d 0h');
  });

  it('carries its own denominator, which is the scan\'s population', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);
    expect(subs()[1]).toBe('4 of 25 requests');
  });

  it('shows the sentinel, never a zero, when nothing timed the findings', () => {
    // A `0` here reads as instant. The producer writes the sentinel when no contributing span
    // recorded a duration, and the count beside it still says the findings exist.
    render(<OutcomeBand scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 9 },
    })} />);

    expect(screen.getByTestId('sentinel')).toHaveTextContent('not measured');
    expect(screen.getAllByTestId('metric-tile-value')[1].textContent).not.toContain('0');
  });

  it('states a partial timing as a shortfall, and says nothing when there is none', () => {
    // `9 of 9` restates the count beside it. `8 of 9` says the total above is short by one finding
    // of unknown size, which is the only case worth a reader's attention.
    const { rerender } = render(<OutcomeBand scorecard={scorecard()} waste={waste({
      totalWallMs: { sum: 209_290, measuredOf: 8, total: 9 },
    })} />);
    expect(subs()[1]).toContain('8 of 9 timed');

    rerender(<OutcomeBand scorecard={scorecard()} waste={waste()} />);
    expect(subs()[1]).not.toContain('timed');
  });

  it('reports runs the scan skipped, which are not clean runs', () => {
    // Nobody looked at them, so the total above is short by whatever they held.
    render(<OutcomeBand scorecard={scorecard()} waste={waste({ runsNotScanned: 3 })} />);
    expect(subs()[1]).toContain('3 not scanned');
  });

  it('says nobody looked when the bundle carries no scan, and does not call it clean', () => {
    // One sentence, and it has to carry both halves: that nobody looked, and that this is not the
    // same fact as a scan that found nothing. Only one of the two is good news.
    render(<OutcomeBand scorecard={scorecard()} />);

    const reason = text('outcome-redone-unreadable-reason');
    expect(reason).toContain('nobody looked');
    expect(reason).toContain('rather than looked and found nothing');
    expect(screen.queryByTestId('sparkline')).toBeNull();
  });
});

// --- the day-over-day half --------------------------------------------------
describe('the prior period', () => {
  it('states the absence in words with fewer than two recorded days', () => {
    // A hidden or omitted comparison reads as "no change", which is a claim nobody measured.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={ONE_DAY} />);

    expect(text('outcome-cleanly-prior')).toBe('No prior data');
    expect(text('outcome-redone-prior')).toBe('No prior data');
    expect(screen.getByTestId('outcome-cleanly-prior')).toHaveAttribute('data-state', 'none');
  });

  it('says the same with no history key at all, which is not an empty history', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} />);
    expect(text('outcome-cleanly-prior')).toBe('No prior data');
  });

  it('names the previous day\'s own figure once there is one', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);

    expect(text('outcome-cleanly-prior')).toBe('Previously 80% on 3 Sep');
    expect(text('outcome-redone-prior')).toBe('Previously 9 requests on 3 Sep');
    expect(screen.getByTestId('outcome-cleanly-prior')).toHaveAttribute('data-state', 'figure');
  });

  it('compares each tile against its own metric, which can move the other way', () => {
    /*
     * The live pair: the pass rate fell from 80% to 70% while the repeated work halved from 9
     * requests to 4. One "improving" or "declining" verdict over both would be false about one of
     * them, so each caption names only its own series.
     */
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);

    expect(screen.getAllByTestId('metric-tile-value')[0]).toHaveTextContent('70%');
    expect(text('outcome-cleanly-prior')).toContain('80%');
    expect(subs()[1]).toContain('4 of 25');
    expect(text('outcome-redone-prior')).toContain('9 requests');
  });

  it('compares against the immediately previous row, not the oldest one', () => {
    // Oldest first on the wire, so it is the second row from the END. Comparing against the first
    // would silently reach further back the longer the history got.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={THREE_DAYS} />);

    expect(text('outcome-cleanly-prior')).toBe('Previously 80% on 3 Sep');
    expect(text('outcome-cleanly-prior')).not.toContain('50%');
  });

  it('inflects the noun for a previous day with exactly one request', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', passRate: 0.8, runsWithWaste: 1 } as unknown as PopulationDay,
      TWO_DAYS[1],
    ]} />);
    expect(text('outcome-redone-prior')).toBe('Previously 1 request on 3 Sep');
  });

  it('keeps the hatched badge for a previous day the producer could not measure', () => {
    // The day WAS recorded and this figure was not measurable on it. Distinct from having no
    // earlier day, and it keeps the treatment every unmeasured value on this surface gets.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', passRate: 'UNMEASURED', runsWithWaste: 9 } as unknown as PopulationDay,
      TWO_DAYS[1],
    ]} />);

    const caption = screen.getByTestId('outcome-cleanly-prior');
    expect(caption).toHaveAttribute('data-state', 'unmeasured');
    expect(caption).toContainElement(screen.getByTestId('sentinel'));
    // The day is still named: the reader can tell which reading is missing.
    expect(caption.textContent).toContain('3 Sep');
  });

  it('says something different again for a row written before the figure existed', () => {
    // Absent is not unmeasured. Nobody looked, as against looked and could not measure.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03' } as unknown as PopulationDay,
      TWO_DAYS[1],
    ]} />);

    const caption = screen.getByTestId('outcome-cleanly-prior');
    expect(caption).toHaveAttribute('data-state', 'unwritten');
    expect(caption.textContent).toBe('Nothing recorded on 3 Sep');
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('finishes the caption when a row\'s date cannot be read', () => {
    // `formatIsoDay` returns null rather than a date it cannot vouch for, and a caption ending in
    // `on ` would read as a rendering fault.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '3rd September', passRate: 0.8, runsWithWaste: 9 } as unknown as PopulationDay,
      TWO_DAYS[1],
    ]} />);
    expect(text('outcome-cleanly-prior')).toBe('Previously 80% on the previous recorded day');
  });

  it('refuses a previous figure that cannot be what it claims to be', () => {
    // A rate above 1 and a negative count are producer bugs. Rendered anyway they read as `140%`
    // and `-3 requests`, which a reader has no way to distrust.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', passRate: 1.4, runsWithWaste: -3 } as unknown as PopulationDay,
      TWO_DAYS[1],
    ]} />);

    expect(text('outcome-cleanly-prior')).toContain('not a rate');
    expect(text('outcome-cleanly-prior')).not.toContain('140');
    expect(text('outcome-redone-prior')).toContain('not a count');
  });
});

// --- the line ---------------------------------------------------------------
describe('the sparkline', () => {
  it('draws one reading as one point, never as a line implying stability', () => {
    // A single value joined to itself is a horizontal line, and a horizontal line is what a steady
    // series looks like — so this would claim a stability nobody measured.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={ONE_DAY} />);

    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', '1');
    expect(screen.getByTestId('sparkline-point')).toBeInTheDocument();
    expect(screen.queryByTestId('sparkline-line')).toBeNull();
  });

  it('draws as many points as it is handed', () => {
    const { rerender } = render(
      <OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);
    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', '2');
    expect(screen.getByTestId('sparkline-line').getAttribute('points')?.split(' '))
      .toHaveLength(2);

    rerender(<OutcomeBand scorecard={scorecard()} waste={waste()} history={THREE_DAYS} />);
    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', '3');
    expect(screen.getByTestId('sparkline-line').getAttribute('points')?.split(' '))
      .toHaveLength(3);
  });

  it('draws nothing at all when no day carries a measured reading', () => {
    // Not a flat line and not an empty box: the caption beside it is what says there is no series.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', runsWithWaste: 'UNMEASURED' } as unknown as PopulationDay,
      { date: '2026-09-04' } as unknown as PopulationDay,
    ]} />);

    expect(screen.queryByTestId('sparkline')).toBeNull();
    // The caption still distinguishes WHY: the earlier day was recorded and this figure on it was
    // not measurable, which is a different fact from there being no earlier day.
    expect(text('outcome-redone-prior')).toBe('Previously not measured on 3 Sep');
  });

  it('drops an unmeasured day rather than interpolating through it', () => {
    // A gap drawn as a line through it asserts a reading nobody took. Three rows, one unmeasured,
    // so two points are drawn and the picture is honest about how many readings it has.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      TWO_DAYS[0],
      { date: '2026-09-04', runsWithWaste: 'UNMEASURED' } as unknown as PopulationDay,
      { date: '2026-09-05', passRate: 0.7, runsWithWaste: 4 } as unknown as PopulationDay,
    ]} />);

    expect(screen.getByTestId('sparkline')).toHaveAttribute('data-points', '2');
  });

  it('is named for a reader who cannot see it', () => {
    // A picture of data with no text beside it is unreadable to anyone using a screen reader, the
    // same argument that makes the ratio bar's two side names required.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);

    const line = screen.getByTestId('sparkline');
    expect(line).toHaveAttribute('role', 'img');
    expect(line.getAttribute('aria-label')).toBe('requests that redid work, over 2 recorded days');
  });

  it('draws a genuinely level series level, in the middle of the band', () => {
    // Two equal readings ARE flat, and there is no spread to show — pinning them to the floor or
    // the ceiling would invent one.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', runsWithWaste: 4 } as unknown as PopulationDay,
      { date: '2026-09-04', runsWithWaste: 4 } as unknown as PopulationDay,
    ]} />);

    const ys = (screen.getByTestId('sparkline-line').getAttribute('points') ?? '')
      .split(' ').map((pair) => pair.split(',')[1]);
    expect(new Set(ys).size).toBe(1);
    expect(ys[0]).toBe('9');
  });
});

// --- and where it sits ------------------------------------------------------
/**
 * That the line belongs to the tile, and not merely that it exists.
 *
 * A line CAN be on the page and be unreadable: given a row and a tile that takes the whole track,
 * the line lands at the far edge of the column, a couple of hundred pixels from the figure it is the
 * history of and level with that figure's label rather than the figure. Every assertion above about
 * the line — how many points, what it is named, that a single reading is a dot — passes in that
 * state, which is how it survived. So what is pinned here is the relationship: whose box the line is
 * in, what it follows, and that nothing lays it out beside a sibling that can push it away.
 */
describe('where the line sits', () => {
  /** The second column's parts. The tiles render in a fixed order, so the redone one is the second. */
  const redoneParts = () => ({
    tile: screen.getAllByTestId('metric-tile')[1],
    value: screen.getAllByTestId('metric-tile-value')[1],
    sub: screen.getAllByTestId('metric-tile-sub')[1],
    line: screen.getByTestId('sparkline'),
    prior: screen.getByTestId('outcome-redone-prior'),
  });

  it('puts the line in the tile\'s own box rather than at the edge of the column', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);
    const { tile, line } = redoneParts();

    // One box holds both, so there is no third element between them to be handed the column's
    // slack — which is what put the line at the far edge.
    expect(tile.parentElement).toContainElement(line);
    // And still outside `MetricTile` itself, which renders a figure, a bar and two captions and
    // takes no children: eight registered spend metrics share it.
    expect(tile).not.toContainElement(line);
    expect(laysOutInARow(tile.parentElement!)).toBe(false);
  });

  it('draws it under the count it is the history of, and above the previous day\'s', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);
    const { value, sub, line, prior } = redoneParts();

    // The reader's order: the duration, the requests behind it, those requests over the recorded
    // days, then what the last recorded day held. The line plots the FOOTNOTE'S count and not the
    // duration above it, so it follows the footnote.
    expect(before(value, line)).toBe(true);
    expect(before(sub, line)).toBe(true);
    expect(before(line, prior)).toBe(true);
  });

  it('stays with its tile at every series length', () => {
    // One reading (drawn as a dot), two, three, and more than the line will draw.
    for (const history of [ONE_DAY, TWO_DAYS, THREE_DAYS, MANY_DAYS]) {
      const { unmount } = render(
        <OutcomeBand scorecard={scorecard()} waste={waste()} history={history} />);
      const { tile, sub, line } = redoneParts();
      const days = `${history.length} recorded days`;

      expect(tile.parentElement, days).toContainElement(line);
      expect(before(sub, line), days).toBe(true);
      expect(laysOutInARow(tile.parentElement!), days).toBe(false);
      unmount();
    }
  });

  it('leaves no empty box behind on a day with nothing to draw', () => {
    // `Sparkline` renders nothing at all when no day carries a measured reading, so a wrapper
    // holding it would hold its own spacing open on exactly the days there is no line.
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={[
      { date: '2026-09-03', runsWithWaste: 'UNMEASURED' } as unknown as PopulationDay,
      { date: '2026-09-04' } as unknown as PopulationDay,
    ]} />);

    expect(screen.queryByTestId('sparkline')).toBeNull();
    expect(screen.getAllByTestId('metric-tile')[1].parentElement!.children).toHaveLength(1);
  });

  it('leaves the tile beside it alone, so the two columns still read as a pair', () => {
    render(<OutcomeBand scorecard={scorecard()} waste={waste()} history={TWO_DAYS} />);
    const [cleanly, redone] = screen.getAllByTestId('metric-tile');
    const line = screen.getByTestId('sparkline');

    // The tile with no line is still a direct child of its own column: nothing about the pair's
    // alignment was bought by wrapping it to match. Two columns and no third, and the line is
    // inside the second rather than between them.
    const row = cleanly.parentElement!.parentElement!;
    expect(row.children).toHaveLength(2);
    expect(cleanly.parentElement).toBe(row.children[0]);
    expect(cleanly.parentElement).not.toContainElement(line);
    expect(row.children[1]).toContainElement(redone);
    expect(row.children[1]).toContainElement(line);
  });
});
