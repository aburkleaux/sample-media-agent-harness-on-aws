/**
 * Shared visual vocabulary for the Improve surface.
 *
 * The rule these encode, and the reason they are shared rather than inlined: **absent
 * data must never look like passing data.** A score of `"UNMEASURED"` rendered as a blank
 * cell or a `0` is indistinguishable from a measurement, and the whole loop rests on
 * being able to tell those apart. `SentinelValue` gives every unmeasured number one
 * hatched, muted, tooltipped treatment; if it is defined once, it cannot drift into
 * looking green on one screen.
 *
 * There are TWO absences and so two treatments, both defined here: `SentinelValue` for a value
 * the producer measured and could not get, `UnavailableBadge` for a metric with no reading at
 * all. They have to stay tellable apart — one says the measurement is missing, the other that
 * there was never anything to measure, and those route to different actions — so the difference
 * between them is a rule this module keeps rather than a look each caller picks.
 *
 * Colour comes only from `theme.colors` and status grounds only from `tint()`. Baked
 * pastel tints stay pastel in dark mode, and a literal `#fff` label is nearly unreadable on
 * a light mint primary, hence `theme.colors.onPrimary` on filled controls.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import {
  isSentinel,
  type Disposition, type EvidenceQuality, type Measured, type Outcome, type Overall,
  type PlatformState, type RunWindow,
} from '../../improve-api';

// --- absent values ----------------------------------------------------------
const SENTINEL_TITLE: Record<string, string> = {
  NO_DATA: 'No data. Evidence this run should have carried is missing, so this dimension '
    + 'could not be judged. Not a pass.',
  UNKNOWN: 'Unknown. The value was never captured for this run.',
  UNMEASURED: 'Unmeasured. No measurement exists. Deliberately not shown as 0, which '
    + 'would read as "none" or "instant".',
};

/** An unmeasured value: hatched, muted, and never mistakable for a number. */
export function SentinelValue({ value }: { value: string }) {
  return (
    <span
      title={SENTINEL_TITLE[value] ?? value}
      aria-label={`${value.toLowerCase().replace('_', ' ')} (not a passing value)`}
      data-testid="sentinel"
      style={{
        ...numericStyle,
        fontSize: theme.typography.sizes.badge,
        color: theme.colors.textTertiary,
        border: `1px dashed ${theme.colors.borderStrong}`,
        borderRadius: theme.radius.badge,
        padding: '0 5px',
        // Hatching, so it reads as "no value here" even in a monochrome screenshot.
        backgroundImage: `repeating-linear-gradient(45deg, transparent, transparent 3px, ${
          tint(theme.colors.textTertiary, 18)} 3px, ${tint(theme.colors.textTertiary, 18)} 5px)`,
        cursor: 'help',
      }}
    >
      {value === 'UNMEASURED' ? 'not measured' : value === 'NO_DATA' ? 'no data' : 'unknown'}
    </span>
  );
}

/**
 * A metric with no reading at all, as against a reading nothing could measure.
 *
 * The sentinel above means the producer ran and came back empty-handed. This means there was never
 * anything to read: the bundle predates the scorecard, the term is not in it, cost is deliberately
 * unpriced, or a division has no denominator. Solid, unhatched and a different word, so the two
 * stay tellable apart in a monochrome screenshot as well as on screen — and grey rather than
 * tinted, because nothing here is a status.
 *
 * Carries no tooltip of its own, unlike every other badge in this file. WHY there is no reading is
 * a sentence the producer writes per metric, so only the caller has it — and a generic title here
 * would shadow that sentence for a reader hovering the badge. The caller hangs it on whatever
 * encloses this.
 *
 * One caller so far (`MetricBlock`'s unavailable slot). It lives here anyway because what it
 * encodes is the surface's rule and not that slot's geometry: a second consumer has to inherit
 * this look rather than invent a second one for the same fact.
 */
export function UnavailableBadge() {
  return (
    <span
      data-testid="unavailable-badge"
      style={{
        fontSize: theme.typography.sizes.badge,
        color: theme.colors.textTertiary,
        background: theme.colors.surfaceSubtle,
        border: `1px solid ${theme.colors.border}`,
        borderRadius: theme.radius.badge,
        padding: '0 5px',
      }}
    >
      unavailable
    </span>
  );
}

/**
 * Renders a possibly-unmeasured number, or its sentinel.
 *
 * `style` exists because the number is set on a span of this component's own, and an inline
 * declaration on that span beats anything it could inherit from a caller's wrapper. So a caller
 * whose figures are meant to look different — a display figure in `tileValueStyle`'s proportional
 * body sans, say — cannot get there by dressing the parent, and needs a way in through here.
 * Merged OVER the default rather than replacing it, so overriding one property keeps the rest of
 * the numeric treatment and a caller that passes nothing renders exactly as before.
 *
 * The sentinel deliberately does not take it. `SentinelValue` is a badge, sized and hatched for
 * "no value here"; a display-size figure style pushed into it would set a large hatched slab where
 * every other absent value on this surface shows the same small badge. The early return below is
 * the whole guard — keep the branch ahead of the style, not inside it.
 */
export function MeasuredValue({
  value, format, style,
}: {
  value: Measured<number>;
  format?: (n: number) => string;
  /** Merged over `numericStyle`. Reaches the figure only, never the sentinel. */
  style?: React.CSSProperties;
}) {
  if (isSentinel(value)) return <SentinelValue value={value} />;
  return <span style={{ ...numericStyle, ...style }}>{format ? format(value) : String(value)}</span>;
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  // Deciding the unit from the ROUNDED value, not from `ms`, promotes 59,999 out of `60.0s`.
  const tenths = Math.round(ms / 100);
  if (tenths < 600) return `${(tenths / 10).toFixed(1)}s`;
  // Both units come out of the ROUNDED total, as in `formatExtent`. Flooring the minutes and
  // rounding the remainder independently prints 59m 59.6s as `59m 60s`.
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

// --- population figures -----------------------------------------------------
/**
 * Whole-population figures: the sums, extents and denominators that head a summary rather
 * than sitting in a table column. Defined here so there is ONE spelling of each — a summary
 * that rounds `211,662,174` differently from the row it summarises reads as two measurements.
 */

/**
 * What these return when the input cannot be a figure at all.
 *
 * Not `0`, not a blank, and not the input rendered anyway: a negative token count or a `NaN`
 * denominator is a producer bug, and `-51.7k` or `NaNk` launders it into something a reader
 * will believe. Throwing would be worse — one corrupt figure would take the whole pane down
 * with it and hide the figures that are sound.
 */
const NOT_COUNTABLE = 'not a count';

/** The same, for a span of time. */
const NOT_A_DURATION = 'not a duration';

/**
 * A count at a glance: `553`, `51.7k`, `211.7M`.
 *
 * Counts only — tokens, LLM calls, sessions — so there is deliberately no sign handling and
 * this is the wrong function for a delta.
 *
 * Three tiers and no more, which has one visible consequence: a population past a billion
 * reads `1000.0M` rather than gaining a `B`. That beats rounding down to `999.9M`, which
 * would be a small lie about a real number, and beats a fourth tier nothing has needed yet —
 * the largest population measured to date is 211.7M tokens.
 */
export function compactNumber(n: number): string {
  if (!Number.isFinite(n) || n < 0) return NOT_COUNTABLE;
  if (n < 1000) return String(n);
  const thousands = (n / 1000).toFixed(1);
  // 999,950 renders as `1000.0` thousand, a figure that has outgrown the unit it is about to
  // be labelled with. Deciding the unit from the ROUNDED value, not from `n`, promotes it.
  if (Number(thousands) < 1000) return `${thousands}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Hand-rolled rather than `toLocaleDateString` so one caption cannot render `13 Aug` for
 *  one reader and `Aug 13` for another, and so tests do not depend on the runtime's ICU. */
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** `13 Aug`. UTC, matching `hhmm` — every window on the wire is stamped `+00:00`. */
function dayMonth(at: Date): string {
  return `${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]}`;
}

/**
 * `2026-09-02` as `2 Sep`, or `null` when the string is not a date at all.
 *
 * The wire spells a day two ways — a full timestamp on a window, a bare `YYYY-MM-DD` on a daily row
 * — and this is the second one. Its own entry point rather than `new Date(s)` at the call site, for
 * the reason `dayMonth` is hand-rolled: a caption built from a bare date string is read alongside one
 * built from a timestamp, and the two must agree on `2 Sep` down to the space.
 *
 * Parsed by FIELD rather than by `Date`. A bare `YYYY-MM-DD` is specified to parse as UTC midnight,
 * but a `2026-9-2` or a `2026-09-02T00:00:00` is not, and either would come out shifted by a
 * timezone for readers west of UTC — so the format is required exactly, and anything else is `null`
 * rather than a date this cannot vouch for. A caller with no date to print must say so in its own
 * words; there is no fallback string here that would read as one.
 */
export function formatIsoDay(date: string): string | null {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (parts === null) return null;
  const month = Number(parts[2]);
  const day = Number(parts[3]);
  // A month or day outside the calendar is a producer bug, and `MONTHS[12]` would print
  // `2 undefined` — which reads as a rendering fault rather than as the bad date it is.
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  return `${day} ${MONTHS[month - 1]}`;
}

/**
 * A span of time, coarse enough to read at a glance: `4d 19h`, `3h 23m`, `59m 59s`.
 *
 * Serves a window's extent and a SUM of many runs' durations alike — both are durations, and
 * both outgrow `formatMs`, whose largest unit is the minute: a real 14-day harvest window comes
 * out `20123m 21s` and a population's busy time `6875m 25s`. Under an hour `formatMs` is reused.
 */
export function formatExtent(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return NOT_A_DURATION;
  if (ms < 3_600_000) return formatMs(ms);
  const minutes = Math.round(ms / 60_000);
  // Both units come out of the ROUNDED total. Decomposing the raw value instead prints
  // 1h 59m 40s as `1h 60m`.
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  const hours = Math.round(ms / 3_600_000);
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/**
 * A population's wall-clock extent as one caption:
 * `13 Aug 17:20 \u2192 23:12 (5h 52m, reconstructed)`.
 *
 * A same-day window names the date once; a longer one names it on both ends, because
 * `00:55 \u2192 00:18` across a fortnight is otherwise indistinguishable from 23 minutes. The
 * duration is always spelled out, so the reader never has to subtract two timestamps to find
 * out whether a claim rests on an afternoon or on two weeks.
 *
 * The only window formatter here, and deliberately so: a second one that printed times without
 * a date made a window nine days long read as ten hours, and the harvest holds single sessions
 * that long.
 *
 * `derived` is carried into the caption rather than dropped. Every window measured so far is
 * reconstructed from span bounds, so a caption that omitted it would let inferred timing read
 * as stamped timing on every screen it appears on. One word, not the sentence
 * `EvidenceQualityBlock` uses — the rule stays stated in one place.
 */
export function formatWindow(w: Measured<RunWindow>): string {
  // A one-line caption has no room for the hatched sentinel treatment, and the three
  // sentinels differ only in WHY the extent is missing, which for a wall clock is one fact.
  if (isSentinel(w)) return 'extent not recorded';
  const from = new Date(w.start);
  const to = new Date(w.end);
  const marker = w.derived ? ', reconstructed' : '';
  // Echo what arrived rather than `Invalid Date`, so a malformed bound is diagnosable on sight.
  if (Number.isNaN(from.getTime()) || Number.isNaN(to.getTime())) {
    return `${w.start} \u2192 ${w.end} (extent unreadable${marker})`;
  }
  const sameDay = from.getUTCFullYear() === to.getUTCFullYear()
    && dayMonth(from) === dayMonth(to);
  const tail = sameDay ? hhmm(to) : `${dayMonth(to)} ${hhmm(to)}`;
  const extent = to.getTime() - from.getTime();
  // A window whose end precedes its start is a producer bug, and one a wall clock can name.
  // Said in words, because `-41m` is a duration a reader can accidentally take at face value.
  const lasted = extent < 0 ? 'ends before it starts' : formatExtent(extent);
  return `${dayMonth(from)} ${hhmm(from)} \u2192 ${tail} (${lasted}${marker})`;
}

/**
 * `17 of 20`.
 *
 * Prose, never `17/20` and never a percentage. A slash reads as division at a glance, and a
 * percentage hides the denominator — which is the number that says how much of the population
 * was measured at all. Spelled out, the fraction documents itself.
 */
export function fractionLabel(measuredOf: number, total: number): string {
  if (!Number.isFinite(measuredOf) || !Number.isFinite(total)) return NOT_COUNTABLE;
  // What an unpopulated bundle produces. `0 of 0` invites the reader to compute 0%, a
  // failure, when the truth is that there was nothing to measure.
  if (total === 0) return 'nothing to measure';
  // `21 of 20` is a producer bug and stays visible. Clamping it to `20 of 20` would assert
  // complete coverage that nothing computed, which is the one thing this surface must not do.
  return `${measuredOf} of ${total}`;
}

/**
 * A share as a whole percentage and its complement: `98%` beside `2%`, `<1%` beside `>99%`.
 *
 * A rule about honesty rather than about formatting. `0%` and `100%` are reserved for exactly none
 * and exactly all, so a share that EXISTS and rounds to nothing must not print `0%`: the harvest's
 * uncached `input` tokens are 0.01% of the population, and `0% uncached input` states that no
 * uncached token was ever sent — false, and about a term priced ten times the one beside it. The
 * mirror case is the same lie the other way round, so a share a hair short of everything reads
 * `>99%` and never `100%`.
 *
 * The complement is derived from the SAME rounded figure rather than rounded separately, so the two
 * sides always add up to 100 on screen. Rounding both independently prints `1% / 100%` for a ratio
 * of 0.005, and a reader who adds them up is entitled to conclude one of them is wrong.
 *
 * Both sides come back even for a caller that shows one, which is why `sharePercent` below is a
 * projection of this and not its own arithmetic: `100 - pct` is where `>99%` comes from, so a caller
 * that computed its own complement would be re-implementing the half of the rule that is easiest to
 * lose.
 *
 * Nothing here validates the input, deliberately. A figure outside 0–1 is a producer bug, and it
 * comes back as the plain rounded percentage — `140%` — because a caller holding one still wants the
 * real figure in its label rather than a clamped or a suppressed one. What else to do about it
 * differs by caller (`RatioBar` withholds its fill and says `not a proportion`; the outcome band
 * refuses the figure and says `not a rate`), so that check stays with them.
 */
export function shareSplit(ratio: number): [string, string] {
  const pct = Math.round(ratio * 100);
  if (ratio > 0 && pct === 0) return ['<1%', '>99%'];
  if (ratio < 1 && pct === 100) return ['>99%', '<1%'];
  return [`${pct}%`, `${100 - pct}%`];
}

/**
 * The leading side of `shareSplit`, for a caller that names one share and not its complement.
 *
 * Takes and returns exactly what `MetricTile`'s `format` wants, so a rate can be handed to a tile
 * without a wrapper that would be somewhere else for the rule to drift to.
 */
export function sharePercent(ratio: number): string {
  return shareSplit(ratio)[0];
}

/**
 * The NOUN for a count: `pluralise(1, 'run')` is `run`, `pluralise(2, 'run')` is `runs`.
 *
 * The noun alone, not `1 run`, because the count beside it is almost always set in
 * `numericStyle` in its own span — the surface's treatment for a figure — and a helper that
 * returned the whole phrase would take the digits out of that treatment. Callers building a
 * plain string interpolate both: `${n} ${pluralise(n, 'run')}`.
 *
 * Shared rather than inlined for the reason every other formatter here is: the surface had THREE
 * spellings of this one idea across twelve sites, and two of the three were wrong at one. `(s)`
 * (nine sites: six `run(s)`, two `failure(s)`, one `pattern(s)`) and a bare ` runs` (one) both
 * read as a defect to anybody who lands on a single-run component, which is the common case in a
 * single-digit population; `run{n === 1 ? '' : 's'}` (two sites) was right and still a second copy
 * of the rule.
 *
 * Only exactly 1 is singular. 0 is plural (`0 runs`), and so is anything a producer bug can
 * produce — a negative or a `NaN` count — because English has no singular for those and the
 * wrong figure beside the noun is the visible defect, not the noun. Regular `+s` plurals only;
 * an irregular noun needs an argument added here rather than a ternary at the call site.
 */
export function pluralise(count: number, singular: string): string {
  return count === 1 ? singular : `${singular}s`;
}

/**
 * The first `limit` characters of a sentence, cut on a word and marked — or the sentence itself
 * when it already fits.
 *
 * Shared for `pluralise`'s reason: this surface had TWO spellings of one idea. The metric block
 * previewed an unavailable metric's reason at 120 characters and the metric registry previewed a
 * cost basis at 64, each with its own copy of "slice, find the last space, trim, append an
 * ellipsis" — and the two limits are genuinely different (two lines under a figure-less tile
 * against one under a figure), while the mechanics are not different at all.
 *
 * The break is on a word because a preview that stops mid-token reads as corruption rather than
 * as abridgement. The ellipsis is the reader's signal that there is more, so it is never omitted
 * on a string that was actually cut — and never added to one that was not, which would claim an
 * abridgement that did not happen.
 *
 * A single token longer than `limit` has no space to break on and is cut where it must be: a
 * `https://` URL with no spaces in it is the real case, and hard-cutting one is better than
 * returning nothing or returning the whole thing.
 *
 * What it does NOT do is decide what a caller shows instead of the rest. An abridged preview is
 * only honest where the whole text is reachable somewhere else, and WHERE is the caller's to
 * arrange — a tooltip, a picker row, both.
 */
export function abridge(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  const lastSpace = cut.lastIndexOf(' ');
  return `${(lastSpace > 0 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

/**
 * A summary figure at display size: body sans, semibold, proportional figures.
 *
 * Deliberately NOT `numericStyle`, and deliberately not spread from it. That token is mono
 * with `tabular-nums` so that counts line up down a COLUMN; a standalone figure has no column
 * to line up with, and equal-width digits make a short number look loose — the `1` in `1.0M`
 * carries the same advance as an `8`. Proportional figures set it as the sentence it is.
 *
 * No `fontSize` on purpose: how large a display figure is depends on the tile it sits in, so
 * that stays with the component rather than being decided once here for every future caller.
 *
 * Hand it to `MeasuredValue`, not to the element wrapping it. The figure is set on a span that
 * declares `numericStyle` inline, and inline beats inherited — so a caller that dresses only the
 * wrapper gets the weight and the size it asked for and mono, tabular digits it did not.
 */
export const tileValueStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontFamily,
  fontWeight: theme.typography.weights.semibold,
  fontVariantNumeric: 'proportional-nums',
};

// --- outcomes ---------------------------------------------------------------
function outcomeColour(outcome: Outcome | Overall): string {
  switch (outcome) {
    case 'PASS': return theme.colors.success;
    case 'FAIL': return theme.colors.error;
    // UNSCORED and NO_DATA are emphatically not success. Warning, never green.
    case 'NO_DATA':
    case 'UNSCORED': return theme.colors.warning;
    default: return theme.colors.textTertiary;
  }
}

/**
 * An informational check. Dashed border because it must read as "does not decide the
 * verdict": a run can fail a process check while still passing overall, and a solid red
 * box would contradict the verdict beside it.
 *
 * NOTHING RENDERS THIS YET — no surface reads per-check rows off the wire, so its only callers
 * are its own tests. It is kept for the rule above, which whatever finally renders checks will
 * need; until then its tests are evidence about the rule and not about the shipped surface.
 */
export function ProcessCheckRow({
  name, outcome, explanation, gating,
}: { name: string; outcome: Outcome; explanation: string; gating: boolean }) {
  const colour = outcomeColour(outcome);
  return (
    <div
      data-testid={gating ? 'check-gating' : 'check-informational'}
      style={{
        display: 'flex', gap: 8, alignItems: 'baseline',
        padding: '5px 8px',
        border: `1px ${gating ? 'solid' : 'dashed'} ${tint(colour, 40)}`,
        borderRadius: theme.radius.button,
        background: gating ? tint(colour, 10) : 'transparent',
        marginBottom: 4,
      }}
    >
      <span style={{ ...numericStyle, fontSize: theme.typography.sizes.badge, color: colour }}>
        {outcome}
      </span>
      <span style={{ fontSize: theme.typography.sizes.meta, fontWeight: theme.typography.weights.medium }}>
        {name}
      </span>
      {!gating && (
        <span
          title="Informational, recorded but does not decide the verdict."
          style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
        >
          informational
        </span>
      )}
      <span style={{
        fontSize: theme.typography.sizes.meta, color: theme.colors.textSecondary,
        marginLeft: 'auto', textAlign: 'right', maxWidth: '60%',
      }}>
        {explanation}
      </span>
    </div>
  );
}

// --- dispositions -----------------------------------------------------------
const DISPOSITION_META: Record<Disposition, { label: string; colour: string; title: string }> = {
  publishable: {
    label: 'you can publish this',
    colour: theme.colors.primary,
    title: 'The target is a versioned asset you own, so this can be tried and published.',
  },
  advisory: {
    label: 'view only',
    colour: theme.colors.textTertiary,
    title: 'The component is implicated but has no write path today, either it is not '
      + 'versioned, or its permission has not been recorded.',
  },
  code_change: {
    label: 'needs an engineer',
    colour: theme.colors.warning,
    title: 'The fix lives in platform code, so it cannot be applied from here.',
  },
};

export function DispositionBadge({ disposition }: { disposition: Disposition }) {
  const meta = DISPOSITION_META[disposition];
  return (
    <span
      data-testid={`disposition-${disposition}`}
      title={meta.title}
      style={{
        fontSize: theme.typography.sizes.badge,
        color: meta.colour,
        background: tint(meta.colour, 12),
        border: `1px solid ${tint(meta.colour, 30)}`,
        borderRadius: theme.radius.badge,
        padding: '1px 6px',
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  );
}

/**
 * "Not seen in the latest run." Deliberately not "stale" or "resolved": the loop cannot tell a
 * fixed defect from one whose evidence expired or whose traffic did not recur, so the badge
 * reports the observation and leaves the conclusion to the reader.
 */
export function NotReproducingBadge({ lastSeenAt }: { lastSeenAt?: string }) {
  return (
    <span
      data-testid="not-reproducing"
      title={'The most recent pipeline run did not find this again. That is not the same as '
        + 'fixed: the traffic may not have recurred, or the evidence may have aged out of '
        + 'retention.'
        + (lastSeenAt ? ` Last seen ${lastSeenAt}.` : '')}
      style={{
        fontSize: theme.typography.sizes.badge,
        color: theme.colors.textSecondary,
        background: tint(theme.colors.textSecondary, 10),
        border: `1px dashed ${tint(theme.colors.textSecondary, 34)}`,
        borderRadius: theme.radius.badge,
        padding: '1px 6px',
        whiteSpace: 'nowrap',
      }}
    >
      not seen in the latest run
    </span>
  );
}

/** Version badge, absent for flat categories, which have no published form at all. */
export function VersionBadge({ version, versioned }: { version: number | null; versioned: boolean }) {
  if (!versioned) {
    return (
      <span
        title={'This category is flat, it has no versions, so it cannot be published '
          + 'or rolled back.'}
        style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
      >
        no versions
      </span>
    );
  }
  if (version === null) return <SentinelValue value="UNKNOWN" />;
  return <span style={{ ...numericStyle, fontSize: theme.typography.sizes.badge }}>v{version}</span>;
}

// --- evidence quality -------------------------------------------------------
const PLATFORM_META: Record<PlatformState, { label: string; colour: string; title: string }> = {
  healthy: {
    label: 'platform healthy',
    colour: theme.colors.success,
    title: 'No account-wide alarms were firing over this run\u2019s window.',
  },
  degraded: {
    label: 'platform degraded',
    colour: theme.colors.error,
    title: 'Alarms were firing account-wide during this run. Ambient rather than attributed, so it '
      + 'supports correlation and never per-run blame.',
  },
  unknown: {
    label: 'platform health unknown',
    // Warning, never grey and never green: nobody looked, or the lookup could not tell.
    colour: theme.colors.warning,
    title: 'Could not determine platform state. Not the same as healthy.',
  },
};

/** Three-state platform badge. No boolean path, `unknown` gets its own treatment. */
export function PlatformBadge({ state }: { state: PlatformState }) {
  const meta = PLATFORM_META[state];
  return (
    <span
      data-testid={`platform-${state}`}
      title={meta.title}
      style={{
        fontSize: theme.typography.sizes.badge,
        color: meta.colour,
        background: tint(meta.colour, 12),
        border: `1px solid ${tint(meta.colour, 30)}`,
        borderRadius: theme.radius.badge,
        padding: '1px 6px',
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  );
}

/** What naming the answering source buys a reader, said once for all three of its states. */
const PLATFORM_SOURCE_TITLE = 'Which alarm source answered for this window. A harvested snapshot '
  + 'may predate the run, where a live read cannot, so the same badge beside it means different '
  + 'amounts. Nothing else on the wire says which.';

/**
 * WHICH source answered the badge beside it — `PlatformHealth.source`.
 *
 * The badge says what the platform was doing and cannot say on whose authority. `harvested-alarm-history`
 * is a snapshot that may have been taken before the run; `cloudwatch-describe-alarm-history` is a live
 * read of the account. A `healthy` resting on the first is a weaker claim than the same word resting on
 * the second, and until this line the two rendered identically.
 *
 * THREE states, because absent, empty and named are three different facts here and the producer emits
 * all three. `''` is the measured absence — `infra_health_for_window` writes it when no run window was
 * stamped, so no interval existed to correlate against anything — and `undefined` is a bundle written
 * before the key. Collapsing them with `?? ''` would report a bundle nobody had audited as a lookup
 * that ran and found no source.
 *
 * Set in mono: these are producer class names, not the reader's words, and every other identifier on
 * this surface is set that way.
 */
function PlatformSource({ source }: { source?: string }) {
  if (source === undefined) {
    return (
      <span
        data-testid="platform-source-absent"
        title={`${PLATFORM_SOURCE_TITLE} This bundle was written before the field existed, so it `
          + 'names none rather than reporting a lookup that found none.'}
        style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
      >
        no alarm source recorded on this bundle
      </span>
    );
  }
  if (source === '') {
    return (
      <span
        data-testid="platform-source-none"
        title={`${PLATFORM_SOURCE_TITLE} No source is named because there was no interval to ask `
          + 'one about: nothing stamped this run a window.'}
        style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
      >
        no alarm source answered
      </span>
    );
  }
  return (
    <span
      data-testid="platform-source"
      title={PLATFORM_SOURCE_TITLE}
      style={{ fontSize: theme.typography.sizes.badge, color: theme.colors.textTertiary }}
    >
      {'via '}
      <span style={numericStyle}>{source}</span>
    </span>
  );
}

/**
 * How good the evidence behind a suggestion is: how many runs, over what wall clock, whether
 * that clock was measured or reconstructed, and what the platform was doing.
 *
 * Rendered whenever `evidenceQuality` is present. A suggestion resting on one run with a guessed
 * window and unknowable platform state has to look different from one resting on four verified
 * runs, or the card overstates what it knows.
 */
export function EvidenceQualityBlock({ quality }: { quality: EvidenceQuality }) {
  const { window: runWindow, platform, caveats, runCount, derivedWindows } = quality;
  // Read WIDER than the type declares — a bundle stored before the field existed is served back
  // unchanged, so `undefined` is reachable at runtime and is a different fact from the `''` the
  // producer writes when no window existed to correlate. Same widening `SourceLocation` does.
  const platformSource: string | undefined = platform.source;
  return (
    <div data-testid="evidence-quality">
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span style={numericStyle}>{runCount}</span>
        <span>{pluralise(runCount, 'run')}</span>
        {/* The sentinel is handled here rather than left to `formatWindow`, whose own wording
            for one is plain text: an unmeasured window has to keep the hatched, tooltipped
            badge that every other absent value on this surface gets. */}
        {isSentinel(runWindow) ? <SentinelValue value={runWindow} /> : (
          <span style={{ ...numericStyle, color: theme.colors.textSecondary }}>
            {/* Dated on both ends when the extent needs it: this window is the UNION over the
                member runs, so there is no one run whose date a bare clock could be read off. */}
            {formatWindow(runWindow)}
          </span>
        )}
        <PlatformBadge state={platform.state} />
        {/* Beside the badge rather than under it: the two are one claim, and the source is what
            bounds how much the word beside it means. */}
        <PlatformSource source={platformSource} />
      </div>

      {derivedWindows > 0 && (
        <div data-testid="derived-window-warning" style={{ marginTop: 3 }}>
          {/* When every window is reconstructed — which is every population measured so far —
              the caption above has already said so, and the CONSEQUENCE is the half it cannot
              carry. The count survives where it is the information: `3 of 6` says something
              "reconstructed" does not. */}
          {derivedWindows === runCount ? (
            <span style={{ color: theme.colors.warning }}>
              no collector recorded these timings, so the extent may be short
            </span>
          ) : (
            <>
              <span style={{ color: theme.colors.warning }}>
                {`${derivedWindows} of ${runCount} timings reconstructed`}
              </span>
              <span style={{ color: theme.colors.textTertiary }}>
                {' '}(no collector recorded them, so the extent may be short)
              </span>
            </>
          )}
        </div>
      )}

      {caveats.map((caveat) => (
        <div key={caveat} style={{ marginTop: 3, color: theme.colors.textTertiary }}>
          {caveat}
        </div>
      ))}
    </div>
  );
}

function hhmm(at: Date): string {
  return `${String(at.getUTCHours()).padStart(2, '0')}:`
    + `${String(at.getUTCMinutes()).padStart(2, '0')}`;
}

// --- layout -----------------------------------------------------------------
/**
 * Absolutely-positioned flex column. Copied from the Skills surface: without a definite
 * height a child's `height: 100%` does not resolve, `overflowY: auto` never engages, and
 * long content runs off the pane instead of scrolling.
 */
export const improveSurfaceStyle: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  flexDirection: 'column',
};

export const improveScrollStyle: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '0 16px 24px',
};

export const cardStyle: React.CSSProperties = {
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  padding: 12,
  marginBottom: 10,
};

export const metaTextStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
  lineHeight: theme.typography.lineHeight,
};

/** Filled action. `onPrimary` rather than `#fff`, see the module docstring. */
export function ActionButton({
  label, onClick, disabled, disabledReason, variant = 'primary', testId,
}: {
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  disabledReason?: string | null;
  variant?: 'primary' | 'secondary';
  testId?: string;
}) {
  const filled = variant === 'primary';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid={testId}
      title={disabled ? (disabledReason ?? undefined) : undefined}
      style={{
        fontSize: theme.typography.sizes.meta,
        padding: '5px 11px',
        borderRadius: theme.radius.button,
        border: `1px solid ${filled ? theme.colors.primary : theme.colors.border}`,
        background: filled ? theme.colors.primary : theme.colors.surface,
        color: filled ? theme.colors.onPrimary : theme.colors.textPrimary,
        opacity: disabled ? theme.opacity.disabled : 1,
        cursor: disabled ? 'not-allowed' : 'pointer',
      }}
    >
      {label}
    </button>
  );
}
