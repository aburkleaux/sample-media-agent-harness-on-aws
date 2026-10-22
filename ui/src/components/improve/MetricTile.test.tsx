/**
 * @vitest-environment jsdom
 *
 * What these pin is honesty, not layout: a value nothing measured must not be set in the same
 * type as one that was measured, a measured `0` must stay distinguishable from an absent one, a
 * share must be readable without separating two colours, and a figure that cannot be a share
 * must not be quietly bent into one.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below
 * are both required. `@testing-library/user-event` is not a dependency; use fireEvent.
 */
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { MetricTile, RatioBar } from './MetricTile';
import {
  compactNumber, formatMs, fractionLabel, shareSplit, tileValueStyle,
} from './improveChrome';
import { numericStyle, theme } from '../../styles/tokens';

/*
 * Every magnitude below is measured, transcribed from two read-only artifacts outside the repo
 * tree. A tile tested only on round numbers passes while mis-setting every real one.
 *
 * `HARVEST_*`: sums over a 336-hour staging harvest of 553 sessions, 354 of which carry a
 * `usage` block. The four category sums add to the total exactly (Bedrock's own arithmetic), so
 * a share taken over them has no residual.
 * `WIRE_*`: a real emitted bundle's `scorecard.spend`, where latency is measured on 6 of 6 runs
 * and every token term is `UNMEASURED` with `measuredOf: 0`.
 */
const HARVEST_SESSIONS = 553;
const HARVEST_SESSIONS_WITH_USAGE = 354;
const HARVEST_TOKENS = 211_662_174;
const HARVEST_CALLS = 5_406;
const HARVEST_OUTPUT_TOKENS = 3_616_525;
const HARVEST_INPUT_TOKENS = 22_163;
/** Uncached input + cache reads + cache writes: everything sent TO a model, as opposed to
 *  everything it produced. `22_163 + 157_490_856 + 50_532_630`. */
const HARVEST_CONTEXT_TOKENS = 208_045_649;

const WIRE_RUNS = 6;
const WIRE_LATENCY_MS = 12_182_621;

/**
 * A qualifying sentence far longer than a footnote, carrying the four facts that make a hand-entered
 * rate auditable: the page, the date it was read off it, the region check, and the cache-write TTL
 * the figure assumes.
 *
 * Shaped like the producer's real 839-character `costBasis` rather than transcribed from it. What is
 * under test here is that the component passes a long string through untouched, and the character-
 * exact match against the producer's own sentence is pinned in `metricRegistry.test.ts` — a second
 * copy of 839 characters would be two things to keep in step and would test nothing more.
 */
const LONG_CAVEAT = 'ESTIMATE, not billed truth: computed per model at rates from '
  + 'https://aws.amazon.com/bedrock/pricing/, read by hand on 2026-08-31 — that page quotes these '
  + 'models identically in every region, checked, and the cache-write figure is the 5-MINUTE-TTL '
  + 'one, so a run that opted into the 1-hour TTL costs up to 1.6x what is reported here.';

/** 0.9829…, the real context-vs-generated split. Computed, not transcribed, so the test cannot
 *  disagree with its own inputs. */
const CONTEXT_SHARE = HARVEST_CONTEXT_TOKENS / HARVEST_TOKENS;
/** 0.0001047…: a share that is real, priced ten times its neighbours, and rounds to nothing. */
const INPUT_SHARE = HARVEST_INPUT_TOKENS / HARVEST_TOKENS;

const value = () => screen.getByTestId('metric-tile-value');
const label = () => screen.getByTestId('ratio-bar-label');
const fill = () => screen.getByTestId('ratio-bar-fill');

describe('MetricTile', () => {
  it('sets a measured population sum through the formatter it was handed', () => {
    render(
      <MetricTile
        label="Tokens"
        value={HARVEST_TOKENS}
        format={compactNumber}
        sub={fractionLabel(HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS)}
      />,
    );
    expect(value().textContent).toBe('211.7M');
    expect(screen.getByTestId('metric-tile-sub').textContent).toBe('354 of 553');
    expect(screen.getByTestId('metric-tile-label').textContent).toBe('Tokens');
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('formats a measured duration with whatever formatter the caller chose', () => {
    // The same tile, a different metric: nothing about the unit lives in the component.
    render(<MetricTile label="Total latency" value={WIRE_LATENCY_MS} format={formatMs}
                       sub={fractionLabel(WIRE_RUNS, WIRE_RUNS)} />);
    expect(value().textContent).toBe('203m 3s');
    expect(screen.getByTestId('metric-tile-sub').textContent).toBe('6 of 6');
  });

  it('sends an unmeasured value to the sentinel badge and never through the formatter', () => {
    /*
     * The trap, spelled out: `compactNumber` answers a sentinel with a plain string, so a tile
     * that formatted first would set the words `not a count` in display type and the reader
     * would have no way to tell that from a measurement.
     */
    expect(compactNumber('UNMEASURED' as unknown as number)).toBe('not a count');

    const format = vi.fn(compactNumber);
    render(<MetricTile label="Tokens" value="UNMEASURED" format={format}
                       sub={fractionLabel(0, WIRE_RUNS)} />);

    expect(format).not.toHaveBeenCalled();
    expect(screen.getByTestId('sentinel')).toHaveTextContent('not measured');
    expect(value().textContent).not.toContain('not a count');
    // No digit at all in the value, so it cannot be read as a figure of any size.
    expect(value().textContent).not.toMatch(/\d/);
    // The denominator still shows: 0 of 6 is the fact that makes the sentinel legible.
    expect(screen.getByTestId('metric-tile-sub').textContent).toBe('0 of 6');
  });

  it('keeps a measured 0 visibly distinct from an unmeasured value', () => {
    const { rerender } = render(
      <MetricTile label="LLM calls" value={0} format={compactNumber} />,
    );
    expect(value().textContent).toBe('0');
    expect(screen.queryByTestId('sentinel')).toBeNull();

    rerender(<MetricTile label="LLM calls" value="UNMEASURED" format={compactNumber} />);
    expect(screen.getByTestId('sentinel')).toBeInTheDocument();
    expect(value().textContent).not.toBe('0');
  });

  it('supplies its own display size, and it reaches the figure by inheritance', () => {
    render(<MetricTile label="LLM calls" value={HARVEST_CALLS} format={compactNumber} />);
    // A `fontSize: 20` here would opt the figure out of Settings → General → Font Size.
    expect(value().style.fontSize).toMatch(/^var\(--fs-/);
    expect(value().style.fontSize).toBe(theme.typography.sizes.sectionTitle);
    /*
     * The size is the one part of the treatment that stays on the tile's own element, because
     * `tileValueStyle` deliberately carries none. So the figure declares no size and can only be
     * getting the tile's — read off the rendered element rather than assumed, since this file
     * exists because a wrapper and the figure inside it do not always agree.
     */
    const figure = value().firstElementChild as HTMLElement;
    expect(figure.style.fontSize).toBe('');
    expect(window.getComputedStyle(figure).fontSize).toBe(theme.typography.sizes.sectionTitle);
  });

  it('sets the figure itself in the display token, not in the numeric spine', () => {
    /*
     * `tileValueStyle` has to be handed to `MeasuredValue` to reach the digits at all: the number
     * is set on a span of `MeasuredValue`'s own that declares `numericStyle` inline, and inline
     * beats inherited. Spread onto the wrapper instead, the token delivered weight and size while
     * the figure still came out mono and tabular — the type that token exists to avoid. Read off
     * the rendered element, because that defect was invisible both to type-checking and to
     * reading the component: every prop was correct.
     */
    render(<MetricTile label="Tokens" value={HARVEST_TOKENS} format={compactNumber} />);
    const figure = value().firstElementChild as HTMLElement;
    expect(figure.textContent).toBe('211.7M');
    expect(figure.style.fontFamily).toBe(tileValueStyle.fontFamily);
    expect(figure.style.fontVariantNumeric).toBe(tileValueStyle.fontVariantNumeric);
    expect(figure.style.fontWeight).toBe(String(theme.typography.weights.semibold));
    expect(figure.style.fontFamily).not.toBe(numericStyle.fontFamily);
    expect(figure.style.fontVariantNumeric).not.toBe(numericStyle.fontVariantNumeric);
  });

  it('leaves an unmeasured figure in the ordinary sentinel badge, not the display style', () => {
    /*
     * The display treatment stops at the figure. `SentinelValue` is badge-sized and hatched on
     * purpose, and given a display style it would set a large slab where every other absent value
     * on this surface shows the same small badge.
     */
    render(<MetricTile label="Tokens" value="UNMEASURED" format={compactNumber} />);
    const badge = screen.getByTestId('sentinel');
    // The badge IS the tile's figure element: nothing styled wraps it on the way through.
    expect(badge).toBe(value().firstElementChild);
    expect(badge.style.fontSize).toBe(theme.typography.sizes.badge);
    expect(badge.style.fontFamily).toBe(numericStyle.fontFamily);
    expect(badge.style.fontVariantNumeric).toBe(numericStyle.fontVariantNumeric);
    expect(badge.style.fontFamily).not.toBe(tileValueStyle.fontFamily);
  });

  it('omits the footnote and the bar when it was given neither', () => {
    render(<MetricTile label="LLM calls" value={HARVEST_CALLS} format={compactNumber} />);
    expect(screen.queryByTestId('metric-tile-sub')).toBeNull();
    expect(screen.queryByTestId('ratio-bar')).toBeNull();
  });

  it('carries a caveat too long for the footnote whole, and leaves the footnote alone', () => {
    /*
     * Cost's pricing basis is 839 characters — the page the rates came off, the date they were read
     * by hand, the region check, and that a one-hour cache TTL costs up to 1.6x what is shown —
     * against a footnote with room for about a line. The abridged head goes in `sub` and the whole
     * sentence has to survive here UNTOUCHED: a tile that trimmed this would leave the remainder with
     * no home anywhere, which is the state the prop exists to end.
     *
     * The two are asserted together on purpose. Nothing stops a future edit from "tidying" the
     * footnote by lengthening it, and the short form is deliberate — a dozen lines of meta text in a
     * tile a third of a row wide pushes the two measured tiles beside it down the pane.
     */
    render(
      <MetricTile
        label="Everyone's cost"
        value={2.9842120}
        format={(n) => `$${n.toFixed(2)}`}
        sub="ESTIMATE, not billed truth · 2 of 553 sessions priced"
        title={LONG_CAVEAT}
      />,
    );
    expect(screen.getByTestId('metric-tile')).toHaveAttribute('title', LONG_CAVEAT);
    expect(screen.getByTestId('metric-tile').getAttribute('title')).toHaveLength(
      LONG_CAVEAT.length);
    // The footnote is exactly what it was handed: the tooltip is an addition, not a rewrite.
    expect(screen.getByTestId('metric-tile-sub').textContent)
      .toBe('ESTIMATE, not billed truth · 2 of 553 sessions priced');
    expect(screen.getByTestId('metric-tile-sub').textContent).not.toContain('bedrock/pricing');
  });

  it('grows no tooltip at all when it was given no caveat', () => {
    // `title=""` gives a tile a tooltip-shaped affordance with nothing behind it, and every tile on
    // this surface but one has nothing more to say than its footnote says.
    render(<MetricTile label="Tokens" value={HARVEST_TOKENS} format={compactNumber} sub="354 of 553" />);
    expect(screen.getByTestId('metric-tile')).not.toHaveAttribute('title');
  });

  it('lets the sentinel keep its own explanation beside the tile’s', () => {
    /*
     * A population nothing could price is the shape that puts both tooltips in one tile: the badge
     * explains what an unmeasured value is, and the tile explains what the producer said about
     * pricing. The nearer element wins where they overlap, which is the wanted order — the badge is
     * about that one span, the caveat about the whole figure — so neither may swallow the other.
     */
    render(
      <MetricTile label="Everyone's cost" value="UNMEASURED" format={compactNumber}
                  sub="tokens measured, cost not priced · 0 of 6 sessions priced"
                  title={LONG_CAVEAT} />,
    );
    expect(screen.getByTestId('metric-tile')).toHaveAttribute('title', LONG_CAVEAT);
    expect(screen.getByTestId('sentinel')).toHaveAttribute('title', expect.stringContaining(
      'No measurement exists'));
    // Still the hatched badge and still no formatter output: a tooltip changes nothing about the
    // figure's treatment.
    expect(value().textContent).not.toContain('not a count');
  });

  it('carries a share beneath the figure when one is given', () => {
    render(
      <MetricTile
        label="Tokens"
        value={HARVEST_TOKENS}
        format={compactNumber}
        sub={fractionLabel(HARVEST_SESSIONS_WITH_USAGE, HARVEST_SESSIONS)}
        ratio={{ value: CONTEXT_SHARE, part: 'context', rest: 'generated' }}
      />,
    );
    expect(value().textContent).toBe('211.7M');
    expect(label().textContent).toBe('98% context / 2% generated');
    expect(screen.getByTestId('metric-tile-sub').textContent).toBe('354 of 553');
  });

  it('passes an unmeasured share through as unmeasured rather than dropping the bar', () => {
    // A metric with no split at all and a metric whose split nothing could measure are
    // different facts; the second one still owes the reader a bar that says so.
    render(
      <MetricTile label="Tokens" value="UNMEASURED" format={compactNumber}
                  ratio={{ value: 'UNMEASURED', part: 'context', rest: 'generated' }} />,
    );
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'unmeasured');
    expect(screen.getAllByTestId('sentinel')).toHaveLength(2);
  });
});

describe('RatioBar', () => {
  it('draws the real context-vs-generated split at its true width and names both sides', () => {
    render(<RatioBar ratio={CONTEXT_SHARE} part="context" rest="generated" />);
    expect(label().textContent).toBe('98% context / 2% generated');
    // The geometry uses the unrounded share, not the rounded label.
    expect(parseFloat(fill().style.width)).toBeCloseTo(CONTEXT_SHARE * 100, 10);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'measured');
  });

  it('keeps the unrounded share reachable, so the rounded label hides nothing', () => {
    render(<RatioBar ratio={CONTEXT_SHARE} part="context" rest="generated" />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute(
      'title', 'context is 98.3% of the total, before rounding.',
    );
  });

  it('never lets a real share round away to 0%', () => {
    render(<RatioBar ratio={INPUT_SHARE} part="uncached input" rest="cached or generated" />);
    expect(label().textContent).toBe('<1% uncached input / >99% cached or generated');
    expect(label().textContent).not.toContain('0%');
    // 0.0105%, not `0.00` — the reader can still find the real figure.
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute(
      'title', 'uncached input is 0.0105% of the total, before rounding.',
    );
    // And a share this thin is still drawn wide enough to be seen.
    expect(fill().style.minWidth).toBe('2px');
    expect(parseFloat(fill().style.width)).toBeCloseTo(INPUT_SHARE * 100, 10);
  });

  it('makes the two named shares add up to 100', () => {
    // A rounding-boundary probe rather than a real magnitude: rounding the complement
    // independently prints `1% ... / 100% ...`, which invites the reader to distrust both.
    render(<RatioBar ratio={0.005} part="output" rest="context" />);
    expect(label().textContent).toBe('1% output / 99% context');
  });

  it('names both sides through the surface-wide share rule, not arithmetic of its own', () => {
    /*
     * The LINKAGE rather than the wording, which the cases above already pin. Every expectation here
     * is `shareSplit`'s live answer and never a literal, so a copy of the rule reintroduced in this
     * component fails the moment it disagrees with the one the rate tile beside it uses. That
     * disagreement is the defect this guards: the rule was implemented twice, and two spellings of
     * "a share that exists must not print 0%" is two chances to lose one of them.
     *
     * Swept across every input the rule turns on — none, thinnest measured, the rounding boundary, a
     * real split, a hair short of all, all — so a private copy would have to reproduce the whole rule
     * to survive rather than just the case a single assertion happened to probe.
     */
    for (const ratio of [0, INPUT_SHARE, 0.005, CONTEXT_SHARE, 1 - 1e-9, 1]) {
      const [part, rest] = shareSplit(ratio);
      const { unmount } = render(<RatioBar ratio={ratio} part="context" rest="generated" />);
      expect(label().textContent, `ratio ${ratio}`).toBe(`${part} context / ${rest} generated`);
      unmount();
    }
  });

  it('says 0%, rather than drawing nothing and letting it read as absent', () => {
    render(<RatioBar ratio={0} part="generated" rest="context" />);
    expect(label().textContent).toBe('0% generated / 100% context');
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'measured');
    // The fill element is present and honest about being empty, and there is no sentinel:
    // nothing here says "unmeasured".
    expect(fill().style.width).toBe('0%');
    expect(screen.queryByTestId('sentinel')).toBeNull();
  });

  it('fills the whole track at 1 without exceeding it', () => {
    render(<RatioBar ratio={1} part="context" rest="generated" />);
    expect(label().textContent).toBe('100% context / 0% generated');
    expect(parseFloat(fill().style.width)).toBe(100);
  });

  it('refuses to draw a share above 1 and keeps the real figure in the label', () => {
    render(<RatioBar ratio={1.4} part="context" rest="generated" />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'impossible');
    // Neither clamped to a full track (which would assert 100%) nor drawn 140% wide.
    expect(screen.queryByTestId('ratio-bar-fill')).toBeNull();
    expect(label().textContent).toBe('140% context (not a proportion)');
  });

  it('treats a negative share the same way', () => {
    render(<RatioBar ratio={-0.2} part="context" rest="generated" />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'impossible');
    expect(label().textContent).toBe('-20% context (not a proportion)');
  });

  it('prints no percentage at all for a non-finite share', () => {
    render(<RatioBar ratio={Number.NaN} part="context" rest="generated" />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'impossible');
    expect(label().textContent).toBe('context share (not a proportion)');
    expect(label().textContent).not.toMatch(/NaN/);
  });

  it('gives an unmeasured share the hatched badge, not a bare track', () => {
    // An undrawn fill reads as 0%. The three sentinels each get their own word.
    const { rerender } = render(<RatioBar ratio="UNMEASURED" part="context" rest="generated" />);
    expect(screen.getByTestId('ratio-bar')).toHaveAttribute('data-state', 'unmeasured');
    expect(screen.queryByTestId('ratio-bar-fill')).toBeNull();
    expect(label().textContent).toBe('context share not measured');

    rerender(<RatioBar ratio="NO_DATA" part="context" rest="generated" />);
    expect(label().textContent).toBe('context share no data');
  });

  it('carries the proportion as text, and does not announce the geometry a second time', () => {
    render(<RatioBar ratio={CONTEXT_SHARE} part="context" rest="generated" />);
    // The bar is not the only carrier of the value: the label is real text, outside any
    // aria-hidden subtree...
    expect(label().closest('[aria-hidden="true"]')).toBeNull();
    // ...and the track, which duplicates it, is hidden from assistive technology rather than
    // being given a `progressbar` role that would read the same figure out twice — and which
    // would be the wrong word for a share of a total in any case.
    expect(screen.getByTestId('ratio-bar-track')).toHaveAttribute('aria-hidden', 'true');
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('draws one hue on two steps of the same ramp, from tokens', () => {
    // A hex here would be correct in one of the three themes in global.css and wrong in two.
    render(<RatioBar ratio={CONTEXT_SHARE} part="context" rest="generated" />);
    expect(fill().style.background).toBe(theme.colors.primary);
    expect(screen.getByTestId('ratio-bar-track').style.background)
      .toBe(theme.colors.primarySubtle);
  });

  it('is the same component whichever metric it describes', () => {
    // Generated-vs-context is the same split read from the other end; the component holds no
    // opinion about which side is interesting.
    render(<RatioBar ratio={HARVEST_OUTPUT_TOKENS / HARVEST_TOKENS}
                     part="generated" rest="context" />);
    expect(label().textContent).toBe('2% generated / 98% context');
  });
});
