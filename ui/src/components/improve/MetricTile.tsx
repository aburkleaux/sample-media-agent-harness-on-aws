/**
 * A summary tile for one figure, and the ratio bar it can carry.
 *
 * Both are purely presentational: they take a value that may not have been measured and set it,
 * and they take a proportion and draw it. Neither decides WHICH figure is shown, computes one,
 * or knows where it came from.
 *
 * They inherit the rule the rest of this surface is built on — **absent data must never look
 * like measured data** — and for a tile that rule has one specific consequence, because the
 * formatters a caller passes in do not enforce it. `compactNumber`, `formatWindow` and
 * `fractionLabel` each return a plain STRING for input they cannot render (`not a count`,
 * `extent not recorded`, `nothing to measure`). Those are backstops for a producer bug, not the
 * display path for absent data: a tile that handed a sentinel to a formatter and set the result
 * would print the words `not a count` in the same semibold display type as `211.7M`, which is
 * exactly the confusion the sentinel treatment exists to prevent. So every possibly-unmeasured
 * value here is branched on FIRST and only ever formatted once it is known to be a number —
 * which is what routing through `MeasuredValue` buys.
 *
 * Colour comes only from `theme.colors`, and the bar is one hue on two steps of the same ramp
 * (`primary` on `primarySubtle`), so it survives all three themes in `styles/global.css`.
 */
import React from 'react';
import { theme, tint } from '../../styles/tokens';
import { isSentinel, type Measured } from '../../improve-api';
import {
  MeasuredValue, SentinelValue, sharePercent, shareSplit, tileValueStyle,
} from './improveChrome';

// --- ratio bar --------------------------------------------------------------
/**
 * What the label says when the number cannot be a share at all.
 *
 * Same reasoning as `improveChrome`'s `not a count`: a figure outside 0–100% is a producer bug,
 * and neither of the two obvious renderings is honest about it. Clamping `1.4` to a full track
 * asserts 100%, a completeness nothing computed; drawing nothing asserts 0%. So the fill is
 * withheld and the real figure stays in the label, where it is diagnosable.
 */
const NOT_A_PROPORTION = 'not a proportion';

/** How tall the track is. Thin on purpose: the bar reinforces the label, it is not the value. */
const TRACK_HEIGHT = 6;

/** The share before rounding, at enough significant figures to stay non-zero at any
 *  magnitude — `toFixed` would render the 0.01% term as `0.00`. */
function exactPercent(ratio: number): string {
  return String(Number((ratio * 100).toPrecision(3)));
}

/** Hatching, reusing `SentinelValue`'s idiom so an undrawable track reads as "no value here"
 *  rather than as an empty one, even in a monochrome screenshot. */
function hatched(colour: string): string {
  return `repeating-linear-gradient(45deg, transparent, transparent 3px, ${
    colour} 3px, ${colour} 5px)`;
}

/**
 * A proportion as a track with a fill, direct-labelled.
 *
 * The label is not optional decoration — it is the value. The track sits below 3:1 contrast
 * against the surface (it has to: a track that competed with the fill would read as a second
 * share), so the bar alone cannot carry the number for anyone who cannot separate the two
 * colours. Naming both sides in text is also the accessible representation: the proportion is
 * real text in the accessible tree, so the geometry is `aria-hidden` rather than being given a
 * `progressbar` role that would announce the same figure a second time — and `progressbar` would
 * be the wrong word anyway, since this is a share of a total, not progress towards one.
 *
 * `part` and `rest` are required for that reason: an unlabelled bar must not be constructible.
 */
export function RatioBar({ ratio, part, rest }: {
  /** The share the fill represents, 0–1. May not have been measurable. */
  ratio: Measured<number>;
  /** What the fill is, in the reader's words — e.g. `context`. */
  part: string;
  /** What the remainder is — e.g. `generated`. Not the whole: the complement. */
  rest: string;
}) {
  let state: 'measured' | 'unmeasured' | 'impossible';
  let label: React.ReactNode;
  let title: string | undefined;
  /** `null` means draw no fill at all. Any number here is already known to be within the
   *  track, so the geometry cannot overflow and nothing has to be clamped. */
  let fill: number | null = null;

  if (isSentinel(ratio)) {
    // A share computed from an unmeasured term is unmeasured. It must not fall through to a
    // bare track, which reads as 0%.
    state = 'unmeasured';
    label = <>{`${part} share `}<SentinelValue value={ratio} /></>;
  } else if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1) {
    state = 'impossible';
    // A non-finite ratio gets no percentage: `NaN%` is the same laundering of a bad figure that
    // `compactNumber` refuses when it declines to print `NaNk`.
    label = Number.isFinite(ratio)
      // One side, not the pair: a figure that is not a proportion has no complement to name.
      ? `${sharePercent(ratio)} ${part} (${NOT_A_PROPORTION})`
      : `${part} share (${NOT_A_PROPORTION})`;
    title = 'A share must fall between 0% and 100%. This one does not, so no share is drawn. '
      + 'The figure is a producer bug, not a measurement.';
  } else {
    state = 'measured';
    fill = ratio;
    const [partShare, restShare] = shareSplit(ratio);
    label = `${partShare} ${part} / ${restShare} ${rest}`;
    title = `${part} is ${exactPercent(ratio)}% of the total, before rounding.`;
  }

  return (
    <div data-testid="ratio-bar" data-state={state} title={title}>
      <div
        // Carries no information the label below it does not, so it is not announced.
        aria-hidden="true"
        data-testid="ratio-bar-track"
        style={{
          height: TRACK_HEIGHT,
          borderRadius: theme.radius.pill,
          background: theme.colors.primarySubtle,
          backgroundImage: state === 'unmeasured' ? hatched(tint(theme.colors.textTertiary, 18))
            : state === 'impossible' ? hatched(tint(theme.colors.warning, 40))
            : undefined,
          // The fill is clipped to the track rather than the ratio being clamped to fit it:
          // a rendering artifact is the renderer's problem, a clamped number is a lie.
          overflow: 'hidden',
        }}
      >
        {fill !== null && (
          <div
            data-testid="ratio-bar-fill"
            style={{
              height: '100%',
              width: `${fill * 100}%`,
              // A share that exists must be visible. 0.01% of a 200px track is a fifth of a
              // pixel, which draws as nothing and so reads as none.
              minWidth: fill > 0 ? 2 : 0,
              borderRadius: theme.radius.pill,
              background: theme.colors.primary,
            }}
          />
        )}
      </div>
      <div
        data-testid="ratio-bar-label"
        style={{
          fontSize: theme.typography.sizes.meta,
          color: theme.colors.textSecondary,
          marginTop: 3,
        }}
      >
        {label}
      </div>
    </div>
  );
}

// --- tile -------------------------------------------------------------------
/**
 * One figure with its name, an optional qualifying line, and an optional share.
 *
 * `sub` is where the denominator or the period goes — `354 of 553`, or a window caption. It sits
 * last, as the tile's footnote: it qualifies how much of the population the figure rests on,
 * which is the reader's last question rather than their first.
 *
 * `ratio` carries its two side names with it so that a bar can never be rendered without them,
 * and it is a `Measured<number>` of its own: "this metric has no split" (prop absent) and "this
 * metric has a split nothing could measure" (prop present, unmeasured) are different facts, and
 * collapsing them would be the same mistake as rendering an absent value as `0`.
 */
export function MetricTile({ label, value, format, sub, ratio, title }: {
  /** Sentence case, no trailing colon. */
  label: string;
  value: Measured<number>;
  /** How to render a measured value, e.g. `compactNumber` or `formatMs`. `MeasuredValue` only
   *  ever calls it on a real number — see this file's opening note. */
  format?: (n: number) => string;
  sub?: string;
  ratio?: { value: Measured<number>; part: string; rest: string };
  /**
   * A qualifying sentence too long for `sub`, whole. Cost's pricing basis is 839 characters — the
   * page the rates came off, the date they were read by hand, and that a one-hour cache TTL would
   * cost up to 1.6x what is shown — where the footnote has room for about a line of it.
   *
   * The same treatment the metric block's figure-LESS slot already gives its full reason, and for
   * the same reason: a reader hovering a figure is asking what it does not cover, and the answer
   * belongs on the whole tile rather than on any one part of it. Passed through untouched — a tile
   * that abridged this would leave the text with no home at all, which is the state this prop
   * exists to end.
   *
   * Never the only channel. A `title` is mouse-only, so whatever supplies this must also put it
   * somewhere a keyboard reaches; the metric picker's own row is where that happens today.
   *
   * It sits OUTSIDE the two tooltips already in a tile — the sentinel badge's explanation of an
   * unmeasured value, and the ratio bar's unrounded share — so the nearer element wins where they
   * overlap and this answers everywhere else. That is the wanted order: those two explain one part
   * of the tile each, and this qualifies the whole figure.
   */
  title?: string;
}) {
  return (
    // A flex child's default `min-width: auto` refuses to shrink below its content, so without
    // this a long `sub` line widens the whole row rather than wrapping inside its own tile.
    // `title` is left off entirely when there is none: `title=""` gives a tile a tooltip-shaped
    // affordance with nothing behind it.
    <div data-testid="metric-tile" title={title} style={{ minWidth: 0 }}>
      <div
        data-testid="metric-tile-label"
        style={{ fontSize: theme.typography.sizes.meta, color: theme.colors.textSecondary }}
      >
        {label}
      </div>
      <div
        data-testid="metric-tile-value"
        style={{
          /*
           * The size lives here, not on `tileValueStyle`: how loud a display figure is depends
           * on the tile it sits in. `sectionTitle` and not the top rung — the figure has to
           * out-weigh everything else in the tile (its label and footnote are `meta`) without
           * competing with the heading of the pane the tiles sit in. Inheritance carries it to
           * the figure, which declares no size of its own.
           */
          fontSize: theme.typography.sizes.sectionTitle,
          lineHeight: theme.typography.lineHeight,
        }}
      >
        {/*
          The rest of the display treatment goes THROUGH `MeasuredValue` rather than onto the div
          above, because the figure is set on a span that declares `numericStyle` inline and inline
          beats inherited. Spread here, `tileValueStyle` reached the tile and not the number, and
          the digits came out in exactly the mono, tabular type that token exists to avoid.

          The sentinel is unaffected by design: `MeasuredValue` branches to its badge before the
          style is applied, so an unmeasured tile still shows the ordinary hatched badge.
        */}
        <MeasuredValue value={value} format={format} style={tileValueStyle} />
      </div>
      {ratio && (
        <div style={{ marginTop: 5 }}>
          <RatioBar ratio={ratio.value} part={ratio.part} rest={ratio.rest} />
        </div>
      )}
      {sub && (
        <div
          data-testid="metric-tile-sub"
          style={{
            fontSize: theme.typography.sizes.meta,
            color: theme.colors.textTertiary,
            marginTop: 3,
          }}
        >
          {sub}
        </div>
      )}
    </div>
  );
}
