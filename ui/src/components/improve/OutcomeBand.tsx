/**
 * Two figures for the question the report's title asks: did the work land, and how much time went
 * into doing it twice.
 *
 * Both read the SHARED bundle — `scorecard` and `waste` off the one global item — and never the
 * reader's own. That is the whole reason this band exists beside the personal blocks rather than
 * among them: a per-actor bundle is written only for someone who had a session in the harvest
 * window, so a figure taken from it is absent for most readers most days, and a band of empty tiles
 * is worse than no band. These two always have a number when the producer has run at all.
 *
 * The consequence to keep in mind when reading them: the figures are the WHOLE harness's, every
 * tenant's runs mixed together, so neither is a statement about the reader's own agent. The labels
 * are unpossessed for that reason — "Finished cleanly", not "Yours".
 *
 * The day-over-day half comes from `history`, the population's day rows, whose `passRate` and
 * `runsWithWaste` are carried per day for exactly this and had no reader before. Two branches and
 * both are real: with one row there is nothing to compare against and the caption SAYS so, because a
 * hidden or omitted comparison reads as "no change"; with two or more the previous day's own figure
 * is named. Nothing here infers a delta, and nothing draws a flat line through a single reading.
 */
import React from 'react';
import { theme } from '../../styles/tokens';
import {
  isSentinel,
  type Measured, type PopulationCounts, type PopulationDay, type Scorecard, type WasteTotals,
} from '../../improve-api';
import { MetricTile } from './MetricTile';
import { Sparkline } from './Sparkline';
import {
  SentinelValue, UnavailableBadge, formatExtent, formatIsoDay, fractionLabel, metaTextStyle,
  pluralise, sharePercent,
} from './improveChrome';

/** A count that could really have been counted. Guards against a producer bug reaching a
 *  denominator, where `NaN` or a negative would print as a figure a reader has no way to distrust. */
function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/** A share that could really be one. `passRate` is `number | null` on the wire — `null` when nothing
 *  was judged — and anything outside 0–1 is a producer bug rather than a rate. */
function isRate(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
}

// --- the day-over-day caption -----------------------------------------------
/**
 * What the previous recorded day held for one figure, or a stated absence.
 *
 * FOUR states, because the wire really produces four and collapsing any two of them would say
 * something false:
 *
 *  - fewer than two day rows: there is no earlier day at all. `No prior data`, in words, never a
 *    hidden chip — an omitted comparison is read as "unchanged".
 *  - the earlier row carries a number: name it, and name its date. No arrow and no delta: the two
 *    figures are on screen and the reader can do the subtraction, where a computed "up 5 points"
 *    would have to decide what counts as a move.
 *  - the earlier row carries the producer's sentinel: the day was recorded and this figure was not
 *    measurable on it. Keeps the hatched badge every unmeasured value on this surface gets.
 *  - the earlier row omits the field: a row written before the producer put this figure on one. Not
 *    the same as unmeasured, and said differently.
 */
type Prior =
  | { state: 'none' }
  | { state: 'figure'; text: string; day: string }
  | { state: 'unmeasured'; sentinel: string; day: string }
  | { state: 'unwritten'; day: string };

/** `2 Sep`, or a phrase for a row whose date cannot be read — `formatIsoDay` returns `null` rather
 *  than a date it cannot vouch for, and a caption has to finish its sentence either way. */
function dayLabel(date: unknown): string {
  const day = typeof date === 'string' ? formatIsoDay(date) : null;
  return day ?? 'the previous recorded day';
}

function priorFor(
  history: readonly PopulationDay[] | undefined,
  read: (row: PopulationDay) => Measured<number> | undefined,
  format: (value: number) => string,
): Prior {
  if (history === undefined || history.length < 2) return { state: 'none' };
  // Oldest first on the wire, so the row before the latest is the second from the end.
  const previous = history[history.length - 2];
  const day = dayLabel(previous.date);
  const value = read(previous);
  if (value === undefined) return { state: 'unwritten', day };
  if (isSentinel(value)) return { state: 'unmeasured', sentinel: value, day };
  if (!Number.isFinite(value)) return { state: 'unwritten', day };
  return { state: 'figure', text: format(value), day };
}

/**
 * The caption is anchored by its WORDING, not by a lead-in mark.
 *
 * It used to open on an em dash, which was doing two jobs at once: holding the line off the figure
 * above it, and marking what followed as a continuation of that figure rather than a second reading
 * of its own. `Previously` states that relation outright, so the mark has nothing left to carry — and
 * the separation was never the mark's to do anyway. The caption is already its own block, a size down
 * and a colour back (`captionStyle`), which is what actually sets it apart from the number above.
 *
 * Each state opens with a capital for the same reason: a line beginning mid-phrase reads as the tail
 * of the one above it, which is exactly the impression the dash was there to give.
 *
 * No full stop on any of them. These are captions rather than prose, and the default view's sentence
 * budget counts anything ending in one.
 */
function PriorCaption({ prior, testId }: { prior: Prior; testId: string }) {
  return (
    <div data-testid={testId} data-state={prior.state} style={captionStyle}>
      {prior.state === 'none' ? 'No prior data'
        : prior.state === 'unwritten' ? `Nothing recorded on ${prior.day}`
        : prior.state === 'unmeasured' ? (
          <>
            {'Previously '}<SentinelValue value={prior.sentinel} />{` on ${prior.day}`}
          </>
        ) : `Previously ${prior.text} on ${prior.day}`}
    </div>
  );
}

// --- a tile with no reading behind it ---------------------------------------
/**
 * The label, the neutral badge and the one sentence saying why there is no figure.
 *
 * `UnavailableBadge` rather than the hatched sentinel, and the difference is the point: the sentinel
 * means the producer measured and came back empty-handed, this means there was never anything in the
 * bundle to read. Both are defined in `improveChrome` so the two absences cannot come to look alike.
 *
 * It deliberately does NOT reuse `MetricTile`'s testids. A tile with no value is not a tile with a
 * value, and a test counting figures on screen must not have to tell them apart by their contents.
 */
function UnreadableTile({ label, reason, testId }: {
  label: string; reason: string; testId: string;
}) {
  return (
    <div data-testid={testId} style={{ minWidth: 0 }}>
      <div style={labelStyle}>{label}</div>
      <div style={{ marginTop: 3 }}><UnavailableBadge /></div>
      <div data-testid={`${testId}-reason`} style={captionStyle}>{reason}</div>
    </div>
  );
}

// --- why a figure can be missing --------------------------------------------
const NO_SCORECARD = 'This report carries no population scorecard, so whether the work finished '
  + 'cleanly is not recorded here.';
const NOTHING_JUDGED = 'No request in this report was judged, so a missing clean-finish rate here '
  + 'is a gap in the evidence rather than a result.';
/**
 * A scorecard that IS here and carries no rate this can read: a bundle written before the producer
 * published one, one whose counts did not survive the round trip, or a figure outside 0–1, which is a
 * producer bug rather than a rate. Its own sentence, because the reader's next step differs from the
 * unjudged case — this resolves on the next producer pass, not on more traffic.
 */
const NO_RATE = 'This report’s scorecard carries no readable clean-finish rate, so no figure is '
  + 'shown here rather than a zero that would claim nothing worked.';
/** One sentence, not two. "Nobody looked" and "not a clean result" are the same point stated twice,
 *  and the second reading of it is what this surface's budget is for. */
const NO_WASTE_SCAN = 'This report carries no scan for repeated work, so nobody looked rather than '
  + 'looked and found nothing.';
const NO_WASTE_TOTALS = 'This report’s scan for repeated work recorded no totals, so the time '
  + 'lost to it cannot be read here.';

export function OutcomeBand({ scorecard, waste, history }: {
  /** The GLOBAL scorecard. See this file's opening note for why it is not the actor's. */
  scorecard?: Scorecard;
  /** The GLOBAL waste totals, likewise. */
  waste?: WasteTotals;
  /** The population's day rows, oldest first. The trend half; absent on a bundle written before
   *  the producer kept them, which is not an empty history. */
  history?: PopulationDay[];
}) {
  /*
   * Read WIDER than the types declare, all the way down. Every field here arrives as fetched JSON
   * that nothing validates on the way in, and stored bundles are served back unchanged — so a
   * bundle really does omit keys `Scorecard` and `WasteTotals` mark as required. Reaching through
   * one would throw and take the band down with it.
   */
  const counts = scorecard?.population as PopulationCounts | undefined;
  const passRate: unknown = scorecard?.passRate;
  const pass: unknown = counts?.pass;
  const fail: unknown = counts?.fail;
  const unscored: unknown = counts?.unscored;
  const judged = isCount(pass) && isCount(fail) ? pass + fail : undefined;

  const days = Array.isArray(history) ? history : undefined;

  // The rate's OWN denominator, not the population's. `passRate` is taken over pass + fail alone —
  // an unscored run says something about the evidence and nothing about the agent — so captioning
  // the rate with `population.total` would invite a division that does not produce it.
  const cleanly = isRate(passRate) && judged !== undefined && judged > 0 && isCount(pass)
    ? {
      rate: passRate,
      // The third count, stated only when there is one. The fraction alone is the whole story when
      // every request was judged, and a trailing "0 could not be judged" is noise; a positive count
      // is runs that are neither a pass nor a failure, and hiding it would let absence read as a pass.
      sub: `${fractionLabel(pass, judged)} requests`
        + (isCount(unscored) && unscored > 0 ? ` · ${unscored} could not be judged` : ''),
    }
    : undefined;

  const runsWithWaste: unknown = waste?.runsWithWaste;
  const scanned: unknown = waste?.runs;
  const totalWallMs = waste?.totalWallMs;
  const notScanned: unknown = waste?.runsNotScanned;
  const redone = isCount(runsWithWaste) && isCount(scanned) && totalWallMs !== undefined
    ? {
      wallMs: totalWallMs.sum,
      sub: `${fractionLabel(runsWithWaste, scanned)} requests`
        // Runs the repeated-block search skipped because they held too many tool calls. Not clean
        // runs: nobody looked at them, so the figure above is short by whatever they held.
        + (isCount(notScanned) && notScanned > 0 ? ` · ${notScanned} not scanned` : '')
        // Said only when it IS a shortfall. `9 of 9` restates the count beside it; `8 of 9` says
        // the total is short by one finding of unknown size.
        + (isCount(totalWallMs.measuredOf) && isCount(totalWallMs.total)
          && totalWallMs.measuredOf < totalWallMs.total
          ? ` · ${fractionLabel(totalWallMs.measuredOf, totalWallMs.total)} timed` : ''),
    }
    : undefined;

  /*
   * Both formatters refuse a figure that cannot be what it claims to be, in the idiom the shared
   * formatters already use (`not a count`, `not a proportion`): a rate above 1 or a negative count is
   * a producer bug, and rendered anyway it reads as `140%` or `-3 requests`, which a reader has no
   * way to distrust.
   */
  const cleanlyPrior = priorFor(days, (row) => row.passRate,
    (rate) => (isRate(rate) ? sharePercent(rate) : 'not a rate'));
  const redonePrior = priorFor(days, (row) => row.runsWithWaste,
    (count) => (isCount(count) ? `${count} ${pluralise(count, 'request')}` : 'not a count'));

  /*
   * The sparkline's own series, and it is NOT the tile's headline figure: the wire carries no
   * per-day duration, so what a day row can show is how many requests redid work. That is the
   * tile's sub-figure, and the line is drawn directly under it for that reason — the picture and the
   * number it sits against are the same quantity. Unmeasured days are dropped rather than
   * interpolated, and the caption below it is what says how far back the readings reach.
   */
  const redoneSeries = (days ?? [])
    .map((row) => row.runsWithWaste)
    .filter((value): value is number => isCount(value));

  return (
    <section data-testid="outcome-band" style={metaTextStyle}>
      <h3 style={headingStyle}>How it went</h3>
      <div style={rowStyle}>
        <div>
          {cleanly === undefined ? (
            <UnreadableTile
              label="Finished cleanly"
              testId="outcome-cleanly-unreadable"
              reason={scorecard === undefined ? NO_SCORECARD
                : passRate === null || judged === 0 ? NOTHING_JUDGED
                : NO_RATE}
            />
          ) : (
            <MetricTile
              label="Finished cleanly"
              value={cleanly.rate}
              /*
               * A percentage, which this surface normally refuses because it hides the denominator —
               * allowed here because the fraction it was taken over is on the line directly below it,
               * so both readings are on screen and the shorter one can lead.
               */
              format={sharePercent}
              sub={cleanly.sub}
              /*
               * The bar is drawn from `passRate` itself, the figure the producer published. Taking
               * it from `pass / judged` here would be a second computation of the same share that
               * could disagree with the number set above it — and the two would have no way to say
               * which was right.
               */
              ratio={{ value: cleanly.rate, part: 'finished cleanly', rest: 'did not' }}
            />
          )}
          <PriorCaption prior={cleanlyPrior} testId="outcome-cleanly-prior" />
        </div>

        <div>
          {redone === undefined ? (
            <UnreadableTile
              label="Time lost to redone work"
              testId="outcome-redone-unreadable"
              reason={waste === undefined ? NO_WASTE_SCAN : NO_WASTE_TOTALS}
            />
          ) : (
            /*
             * The line sits OUTSIDE the tile and UNDER it. Outside because `MetricTile` renders a
             * figure, a bar and two captions and takes no children, and it is shared with the spend
             * band's eight metrics. Under rather than alongside because what the line plots is the
             * tile's footnote — the requests that redid work — and not the duration set above it, so
             * it belongs against the count it is the history of. What laying the two side by side
             * cost instead is in `trendStyle`.
             */
            <div style={trendStyle}>
              <MetricTile
                label="Time lost to redone work"
                value={redone.wallMs}
                // `formatExtent` rather than `formatMs`: the same figure is minutes on one
                // population and hours on another, and `formatMs`'s largest unit is the minute.
                // Under an hour it delegates there, so the short case is unchanged.
                format={formatExtent}
                sub={redone.sub}
              />
              <Sparkline
                points={redoneSeries}
                label={`requests that redid work, over ${redoneSeries.length} `
                  + `${pluralise(redoneSeries.length, 'recorded day')}`}
              />
            </div>
          )}
          <PriorCaption prior={redonePrior} testId="outcome-redone-prior" />
        </div>
      </div>
    </section>
  );
}

// --- styles ----------------------------------------------------------------
/** Matches the other summary blocks' own headings, so the bands read as one register. */
const headingStyle: React.CSSProperties = {
  margin: '0 0 4px',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
};

/**
 * Two tracks, `minmax(0, 1fr)` rather than a bare `1fr` for the reason the spend row uses it: a
 * track's default minimum is its own content, so one long footnote widens its column and squeezes
 * the other. The tiles differ in height, so they align to the top rather than stretching.
 */
const rowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
  gap: 12,
  marginTop: 6,
  alignItems: 'start',
};

/**
 * One tile and the line that belongs to it, stacked.
 *
 * A COLUMN and never a row, for two separate reasons:
 *
 *  - a row hands the fixed-width line whatever the tile leaves over, which — the tile being as wide
 *    as the track — is the far right edge of the column, a reader's width away from the figure it
 *    describes and level with the label rather than the number;
 *  - a row's minimum width is the tile's plus the line's plus the gap, and these sit in
 *    `minmax(0, 1fr)` tracks whose minimum is zero, so a narrow viewport had the pair spilling out
 *    of its own column. Stacked, the line asks for no horizontal room the tile is not already
 *    asking for, and the tile gets the whole track to wrap its footnote in instead of the track
 *    less the line.
 *
 * `gap` rather than a margin on a wrapper: `Sparkline` renders NOTHING for a series with no measured
 * reading, and a wrapper would leave an empty box and its spacing behind on exactly the days there
 * is no line. 5 is `MetricTile`'s own offset for the ratio bar, so the line sits off the text by the
 * same step the bar does in the tile beside it. `alignItems` stays at `stretch` deliberately — the
 * tile then takes the track's full width exactly as it does with no line under it, and the line,
 * having a width of its own, keeps it and sits at the left edge of the space it now has to itself.
 */
const trendStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 5,
};

/** `MetricTile`'s own label treatment, so a tile with no reading lines up with one beside it that
 *  has one. */
const labelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const captionStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textTertiary,
  lineHeight: theme.typography.lineHeight,
  marginTop: 3,
};

export default OutcomeBand;
