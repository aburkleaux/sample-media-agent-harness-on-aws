/**
 * A short series drawn small, against the figure it is the history of. No axes, no ticks, no
 * gridlines and no labels of its own.
 *
 * It is the SHAPE of the series and nothing else, so whatever mounts it owns the numbers in words.
 * `label` is required for that reason: a picture of data with no text beside it is unreadable to
 * anyone who cannot see it, and the same argument makes `RatioBar`'s two side names required.
 *
 * **One reading draws as one point.** A single value joined to itself is a horizontal line, and a
 * horizontal line is exactly what a steady series looks like — so a one-day history rendered that
 * way would claim a stability nobody measured. Two or more readings draw as a polyline.
 *
 * A genuinely FLAT series (two or more equal readings) does draw flat, and in the middle of the
 * band: there is no spread to show, and pinning it to the floor or the ceiling would invent one.
 *
 * Colour comes only from `theme.colors`, one hue, so it survives every theme in
 * `styles/global.css`.
 */
import React from 'react';
import { theme } from '../../styles/tokens';

/**
 * The drawing box, in CSS pixels.
 *
 * Wide enough that a rolling month of daily readings still lands about two pixels apart, and small
 * enough to sit against a tile's figure without competing with it — the figure is the value, this is
 * the context for it. A ceiling rather than a fixed size: see the `maxWidth` below.
 */
const WIDTH = 64;
const HEIGHT = 18;

/** Kept off the edges so a stroke or a dot at the extreme of the range is not half-clipped. */
const PAD = 2;

const STROKE = 1.5;

/** The single-reading mark. Big enough to read as a deliberate point rather than as a speck of
 *  dust, and no bigger — it stands for one day, not for a trend. */
const DOT_RADIUS = 2;

/**
 * The most readings drawn, oldest dropped first.
 *
 * A drawing bound and not a retention policy: the producer already retains a rolling 30 day rows
 * (`improve/aws_lambda._HISTORY_ROWS`) and folds what it evicts into a separate bucket, so more than
 * this cannot normally arrive. It is here so that if that bound ever moves, the line stays legible
 * instead of silently becoming a smear at sub-pixel spacing.
 */
const MAX_POINTS = 30;

export function Sparkline({ points, label }: {
  /**
   * The readings, OLDEST FIRST. Measured values only — a caller holding an unmeasured day must
   * leave it out and say so in words, because a gap drawn as a line through it asserts a reading
   * that was never taken.
   */
  points: readonly number[];
  /** What the line is, for a reader who cannot see it. Required, never empty. */
  label: string;
}) {
  // Non-finite values cannot be placed on an axis, and one would take the whole geometry with it
  // (`Math.min` of a NaN is NaN). Dropped here rather than trusted, for the reason every figure on
  // this surface is checked: this arrives as fetched JSON that nothing validates.
  const usable = points.filter((n) => Number.isFinite(n)).slice(-MAX_POINTS);

  // Nothing to draw is not a flat line and not an empty box. The caller states the absence in
  // words — see the trend caption beside every mount of this.
  if (usable.length === 0) return null;

  const min = Math.min(...usable);
  const max = Math.max(...usable);
  const span = max - min;

  const x = (index: number): number => (usable.length === 1
    ? WIDTH / 2
    : PAD + (index * (WIDTH - 2 * PAD)) / (usable.length - 1));
  const y = (value: number): number => (span === 0
    ? HEIGHT / 2
    : HEIGHT - PAD - ((value - min) * (HEIGHT - 2 * PAD)) / span);

  return (
    <svg
      role="img"
      aria-label={label}
      data-testid="sparkline"
      // How many readings are behind the picture, so a test can hold the one-point rule without
      // reading geometry, and so a screenshot review can tell one day from thirty.
      data-points={usable.length}
      width={WIDTH}
      height={HEIGHT}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      style={{
        display: 'block',
        // Vertically rigid: in a stacked box this is what stops the picture being squashed to
        // nothing when whatever holds it is short.
        flexShrink: 0,
        /*
         * Horizontally it yields instead. `WIDTH` is CSS pixels and the columns these sit in have a
         * minimum of zero, so a narrow enough viewport gives the line less room than it asks for —
         * and then a fixed width does not fit anywhere, it just draws past the edge of its column.
         * With a `viewBox` this scales the drawing down rather than clipping or overflowing it, so
         * the worst case is a smaller line and never a stray one.
         */
        maxWidth: '100%',
      }}
    >
      {usable.length === 1 ? (
        <circle
          data-testid="sparkline-point"
          cx={x(0)}
          cy={y(usable[0])}
          r={DOT_RADIUS}
          fill={theme.colors.primary}
        />
      ) : (
        <polyline
          data-testid="sparkline-line"
          fill="none"
          stroke={theme.colors.primary}
          strokeWidth={STROKE}
          strokeLinejoin="round"
          strokeLinecap="round"
          points={usable.map((value, index) => `${x(index)},${y(value)}`).join(' ')}
        />
      )}
    </svg>
  );
}

export default Sparkline;
