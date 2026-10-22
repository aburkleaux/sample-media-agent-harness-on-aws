/**
 * Every figure a summary block can offer, described rather than drawn.
 *
 * One entry per FIGURE: what it is called, whether it is a single figure or a split, how to read
 * it off the served bundle, and what its footnote says. Adding a metric is adding an entry — so a
 * picker over these is a loop over DATA rather than a switch over markup, and a new figure needs
 * no component change.
 *
 * Eight entries, not sixteen, and that is the second thing this file decides. Whose runs a figure
 * is about used to be baked into its identity — `yourTokens` beside `tokens` — which is two rows
 * for one question, and adding a period the same way would have made it six. Scope and period are
 * READ-TIME parameters instead (`MetricView`): one entry knows how to answer its question of the
 * reader's own days and of the whole harness, and the block's two switches say which is on screen.
 *
 * No JSX and no React here, deliberately. An entry says WHAT to read and what to call it;
 * `MetricTile` decides how that looks. Kept apart, the whole registry is checkable against a real
 * payload with no DOM — which is where the rules below actually get enforced, because a rendering
 * test can only see what a read already decided.
 *
 * Those rules are one rule seen from three sides — **absent data must never look like measured
 * data**:
 *
 * 1. THREE outcomes, never two. `scorecard === undefined` means the bundle predates the
 *    scorecard, which is production today, and the metric is UNAVAILABLE: there is nothing to
 *    read. A term whose `sum` is `"UNMEASURED"` means the producer ran and could not measure it,
 *    and that is a VALUE — it passes through to the tile, which hatches it. A measured `0` is a
 *    measurement and is shown as one. Nothing below writes `?? 0` or `?? []`: on a field the
 *    source never populates, those turn "unknown" into "none".
 *
 *    The personal half is where all three are live at once, and where the third is newly
 *    reachable. A reader with NO bundle, or with one carrying no `history` key, is a reader nothing
 *    is known about: UNAVAILABLE. A reader whose bundle carries a history with no day inside the
 *    chosen period was swept and positively found to have run nothing: that is a measured `0`, and
 *    showing the sentinel there would hide a fact the producer went to the trouble of recording. A
 *    day row that carries a term it could not measure is the sentinel. `0` is never inferred from a
 *    missing key — only ever from a present, empty one.
 * 2. No division that can produce `NaN` or `Infinity`. `measuredOf: 0` is not hypothetical — it
 *    is what a real emitted bundle carries for every token term — so anything that divides
 *    checks its denominator first and reports that it has none.
 * 3. Every footnote carries its denominator, and it is the same denominator at both scopes. On the
 *    harvest these figures come from, tokens are measured on 354 of 553 sessions; a sum over two
 *    thirds of a population, captioned bare, understates it while looking authoritative. The
 *    reader's own totals are captioned by the SESSIONS that recorded each term, summed off the same
 *    day rows the figures come from, rather than by the days those rows span — a fraction of days
 *    says nothing about how much of the work inside them was recorded, so it can read as complete
 *    over a figure covering a fraction of the sessions. Where a row is too old to carry that count,
 *    the footnote says so in words: scaling the fraction to the rows that do carry it would put a
 *    partial numerator over a partial denominator, and going silent reads as complete.
 *
 * A `Run` IS a session — `improve/observe/build_run.py` builds every one with `run_id=session_id` —
 * so the scorecard's `total`, one verdict per run, counts SESSIONS, and a per-session figure taken
 * over `measuredOf` is exact arithmetic across the measured ones rather than an estimate.
 *
 * What does NOT follow, and is the trap this file fell into: that a `runs` COUNT is a session count.
 * A day row's `runs` counts the day's REQUESTS (`dto._day_row`: `len(requests)`) while its
 * `coverage.total` counts every session whose spend landed on the day, plan sub-sessions included —
 * so a real day reads `runs: 17, coverage.total: 19`. Every session count and every divisor here is
 * therefore read off `coverage`, at BOTH scopes; `runs` says only how much of the reader's own
 * activity a total is drawn from.
 */
import {
  abridge, compactNumber, formatExtent, formatIsoDay, fractionLabel, pluralise,
} from './improveChrome';
import {
  isSentinel,
  type ActorDay, type DayCoverage, type DaySpend, type DiscoveredSessions, type Measured,
  type MeasuredCost, type MeasuredSum, type ModelSpend, type ModelSpendRemainder,
  type RolledUpDays, type UsageShortfall,
} from '../../improve-api';
import type { ImproveData } from '../../improve-data';

// --- what a read can answer -------------------------------------------------
/**
 * Why a metric cannot be shown at all.
 *
 * Distinct codes rather than one flag, because a picker has to grey a row AND say why, and the
 * reasons are not equivalent. The two structural ones are properties of the whole bundle, so a
 * picker can collapse them: "this bundle predates the scorecard" is ONE fact about eight rows,
 * and printing eight metric-specific sentences for it buries the single cause in noise. The rest
 * are per-metric and their `reason` is the whole answer.
 */
export type UnavailableCode =
  /** No `scorecard` key at all — the bundle predates it. Structural, collapses across metrics. */
  | 'noScorecard'
  /** No `actor` key — this reader has no bundle of their own. Structural, collapses. */
  | 'noActorBundle'
  /** A personal bundle with no `history` key: nothing on it can be totalled. Structural. */
  | 'noActorHistory'
  /** The wire carries no per-day rows this scope can be totalled over at all. Structural, because
   *  it is one fact about the bundle rather than about any figure. */
  | 'noPeriod'
  /** The scorecard is here but this term is not. */
  | 'notInBundle'
  /** The arithmetic has no denominator: nothing measured to divide over, or a zero total. */
  | 'noDenominator';

/**
 * Which unavailability is a property of the whole BUNDLE rather than of one metric.
 *
 * With the codes rather than with a consumer, because it classifies them — a consumer that
 * re-derived it would be a second copy of the rule with nothing keeping the two in step. Total by
 * construction, so adding a code above fails to compile here until it is classified; left to a
 * default it would read as per-metric, and a structural code that quietly stopped collapsing shows
 * a reader eight unrelated failures instead of the one cause behind them.
 *
 * Private, and asked through `isStructural`: a map a consumer can spread is a map a consumer can
 * vary, which is the duplication this lives here to prevent.
 */
const STRUCTURAL_BY_CODE = {
  noScorecard: true,
  noActorBundle: true,
  noActorHistory: true,
  noPeriod: true,
  notInBundle: false,
  noDenominator: false,
} as const satisfies Record<UnavailableCode, boolean>;

export function isStructural(code: UnavailableCode): boolean {
  return STRUCTURAL_BY_CODE[code];
}

/** A metric that cannot be shown, and the sentence to show instead. */
export interface Unavailable {
  available: false;
  code: UnavailableCode;
  /** Reader-facing prose, ready to print beside a greyed row. Never a code name. */
  reason: string;
  /**
   * ONE phrase to print where this cause stands in for every figure at once, in place of `reason`.
   *
   * Not an abridgement of `reason` and not a summary of it — `abridge` already does that, and a head
   * of a paragraph reads as a paragraph. This is the whole of what a reader who opens the pane is
   * asked to take in, and it is a different sentence written for that position: above the figures
   * that DO have numbers, on a surface whose budget is a handful of sentences for the entire default
   * view.
   *
   * `reason` keeps every home it had. A cause carrying a phrase is still stated in full on its own
   * picker row (unabridged, and the copy a keyboard reaches), on a tile's tooltip wherever the tiles
   * are kept, and as the tooltip of the collapsed line itself — so the argument the producer's
   * sentence makes is one hover or one gear away, never gone.
   *
   * Optional, and expected to stay rare. Present on one cause today: the reader with no history of
   * their own, which is most readers, so its four sentences were the first thing on the page for
   * nearly everyone. A cause that a reader meets only after some other figure has already answered
   * them has no such position to defend and wants its whole reason on screen.
   *
   * Whoever adds one is choosing what a default view says, not shortening prose: a phrase here must
   * still refuse a measurement (absence, never a zero and never a failure), must not reassure past
   * what the wire supports, and must leave the reader somewhere to go.
   */
  brief?: string;
}

/** A figure to show. `value` may be a sentinel — that IS the reading, not a failure of it. */
export interface ScalarReading {
  available: true;
  value: Measured<number>;
  /** The footnote: the denominator this figure rests on, and any per-unit figure. */
  sub?: string;
  /**
   * The whole of a qualification the footnote has room for only a head of.
   *
   * `sub` is about a line of meta text in a tile a third of a row wide, so a metric whose
   * qualification runs longer than that can only put a head of it there. This is where the rest
   * lives. `MetricTile` hangs it on the tile as a tooltip and `MetricBlock`'s picker prints it
   * unabridged on the metric's own full-width row — two channels because a `title` is mouse-only,
   * and the row is what a keyboard reaches.
   *
   * TWO kinds of content reach it, and neither is optional-by-taste. The producer's own sentence,
   * VERBATIM and never composed here — cost's pricing basis, where a hedge the UI wrote itself would
   * go on asserting last quarter's terms after the rates are re-sourced. And a per-row roster the
   * footnote cannot hold whole: the model split names an id, a magnitude, a share and a denominator
   * per model, which is a sentence per row and cannot be a line under a figure. That one IS composed
   * here, out of counts and identifiers off the wire; what may never be composed is a claim about
   * where a number came from.
   *
   * Present only when `sub` genuinely says less than the whole. A qualification short enough to fit
   * the footnote is already wholly in front of the reader, and repeating it in a tooltip and a picker
   * row would be three copies of one sentence.
   *
   * The counterpart to `Unavailable.reason`, which the picker has always printed in full — that is
   * the prose explaining why there is NO figure, and this is the prose qualifying one there IS. Kept
   * as separate fields rather than one, because a reader must be able to tell "nothing measured
   * this" from "this was measured, and here is what the number does not cover".
   */
  caveat?: string;
}

/**
 * A figure plus the share it is of a whole.
 *
 * `ratio` is a `Measured<number>` of its own: a share computed from an unmeasured term is
 * unmeasured, and `RatioBar` hatches it rather than drawing a bare track that would read as 0%.
 */
export interface RatioReading extends ScalarReading {
  ratio: Measured<number>;
}

// --- what a read is asked for ------------------------------------------------
/**
 * Whose runs a figure is about.
 *
 * A READ-TIME parameter, not a property of the metric, and that is the difference between eight
 * entries and sixteen. It still decides the label's own words — `Your tokens` against `Everyone's
 * tokens` — and that is not cosmetic: the signed-in reader's runs are 96.5% of the live
 * population's tokens, so the two figures can sit within a few percent of each other and the label
 * is what tells them apart.
 *
 * What changed is where a reader chooses it. Baked into an id it was only reachable through the
 * gear, which puts a figure's basis behind a menu; as a parameter it is a switch beside the
 * numbers, and one entry answers its question of either population.
 */
export type MetricScope = 'yours' | 'everyone';

/**
 * What a scope is called on its switch.
 *
 * `Yours` / `Everyone` and not the longer sentences the picker used to head its two families with
 * (`Sessions you ran`, `Every session on the harness`). A switch is read beside the figures it
 * governs and the tile labels say the rest — `Your tokens`, `Everyone's tokens` — where a menu
 * heading had to carry the whole distinction alone. How wide "everyone" is now rides the basis
 * label, which is the line a reader is already looking at to know what they are seeing.
 */
export const SCOPE_LABEL = {
  yours: 'Yours',
  everyone: 'Everyone',
} as const satisfies Record<MetricScope, string>;

/** Switch order, and it opens on the reader's own runs — the question someone opening this pane is
 *  asking, and the one the shared scorecard cannot answer for them. */
export const SCOPES: readonly MetricScope[] = ['yours', 'everyone'];

/**
 * How far back a figure reaches.
 *
 * The other read-time parameter, and the reason the personal figures stopped collapsing: they used
 * to be summed off one harvest window, so a reader with no session yesterday had no figures at all.
 * A total over the reader's own day rows always has a number in it, and a trailing window is the
 * same sum over fewer of the same rows — so display is not a storage decision.
 */
export type MetricPeriod = 'allTime' | 'trailing30' | 'trailing7';

/** What a read is asked: whose figure, over how long. */
export interface MetricView {
  scope: MetricScope;
  period: MetricPeriod;
}

/**
 * The longer trailing window. A month, so a change landed a week ago reads as a slope rather than
 * as two adjacent points.
 *
 * Operational — how much to show — but NOT env-overridable, unlike the producer's own bounds: this
 * one is a label on a button, so changing the number without changing the word beside it would
 * caption the wrong span. It happens to equal the producer's day-row retention today, and that is a
 * coincidence rather than a relationship: retention decides how many rows exist and this decides how
 * many of them a figure covers. Named separately for exactly that reason.
 */
const TRAILING_LONG_DAYS = 30;

/** The shorter trailing window: about a working week, which is the span a reader comparing today
 *  against "recently" means. Named separately from the long one for the reason above. */
const TRAILING_SHORT_DAYS = 7;

/**
 * How many days each period covers, and the ONE place that decides it.
 *
 * `null` is cumulative — every day represented, the rolled-up bucket included — rather than a very
 * large number of days, because the two are different questions: a trailing window is anchored and
 * a total is not, and a total assembled by asking for ten thousand days would silently become a
 * window the day retention outgrew it.
 *
 * Total by construction, so a period added to the type fails to compile here until its span is
 * stated. A period whose span was left to a default would read as cumulative, which is the one
 * value that must never be reached by accident.
 */
const PERIOD_DAYS = {
  allTime: null,
  trailing30: TRAILING_LONG_DAYS,
  trailing7: TRAILING_SHORT_DAYS,
} as const satisfies Record<MetricPeriod, number | null>;

/** What a period is called on its switch. Sentence-leading, since the basis label reuses them. */
export const PERIOD_LABEL = {
  allTime: 'All time',
  trailing30: `${TRAILING_LONG_DAYS} days`,
  trailing7: `${TRAILING_SHORT_DAYS} days`,
} as const satisfies Record<MetricPeriod, string>;

/** Switch order, and it opens on the cumulative total — the figure that always has a number in
 *  it, which is the whole point of the change. */
export const PERIODS: readonly MetricPeriod[] = ['allTime', 'trailing30', 'trailing7'];

interface MetricCommon {
  id: string;
  /**
   * What the figure is, scope-free and lower case: `tokens`, `cache reuse`.
   *
   * Not a finished label. A tile says `Your tokens` or `Everyone's tokens` and the scope that
   * decides which is not known here, so the entry carries the noun and `metricLabel` composes it.
   * Held apart because a per-scope pair of labels on the entry would be two strings that have to be
   * kept saying the same thing, which is the duplication collapsing the registry set out to remove.
   */
  noun: string;
  /** How to render a measured value. `MetricTile` only ever calls it on a real number. */
  format?: (n: number) => string;
}

/**
 * One metric.
 *
 * A union on `kind` rather than one shape with optional fields, so the two halves cannot drift:
 * a `ratio` entry MUST name its two sides and its read MUST produce a share, and a `scalar` entry
 * can do neither. That is `RatioBar`'s own rule ("an unlabelled bar must not be constructible")
 * applied one level up, and here the compiler enforces it.
 *
 * `read` takes the view, so ONE entry answers both scopes. It is total over every view — including
 * the combinations the wire cannot honestly present, which `periodRefusal` is what refuses. Keeping
 * the refusal out of the reads is what stops it being written eight times.
 */
export type MetricEntry =
  | (MetricCommon & {
      kind: 'scalar';
      read: (data: ImproveData, view: MetricView) => ScalarReading | Unavailable;
    })
  | (MetricCommon & {
      kind: 'ratio';
      /**
       * What the bar's two sides are called, in the reader's words. `rest` is the COMPLEMENT,
       * not the whole.
       *
       * Static here rather than returned by `read`, for one reason that matters: a picker must be
       * able to describe a metric it cannot currently read. "Context vs generated — context
       * against generated, unavailable in this bundle" is a usable row; the same row with no side
       * names is not. The names are a property of the metric, not of any payload, so they do not
       * belong in a value that every unavailable branch would have to drop.
       */
      sides: { part: string; rest: string };
      read: (data: ImproveData, view: MetricView) => RatioReading | Unavailable;
    });

/**
 * `Your tokens`, `Everyone's tokens`.
 *
 * The one place the scope becomes words. Composed rather than stored so a figure cannot come to be
 * called one thing in the picker and another on the tile, and so a metric added tomorrow gets both
 * spellings for free.
 */
export function metricLabel(entry: MetricCommon, scope: MetricScope): string {
  const owner = scope === 'yours' ? 'Your' : "Everyone's";
  return `${owner} ${entry.noun}`;
}

// --- reading the wire safely -------------------------------------------------
const NO_SCORECARD: Unavailable = {
  available: false,
  code: 'noScorecard',
  reason: 'This data bundle carries no population scorecard, so there is nothing to read this '
    + 'figure from. It was written before the producer started emitting one.',
};

/**
 * No item of the reader's own at all: the one state in which a personal figure is genuinely unknown.
 *
 * It used to be the state a reader met most, because a bundle existed only for someone who had a
 * session inside the harvest window and most actors do not, most days. It is now the narrow case it
 * reads as: the producer writes a bundle for every actor it knows about, so this is a reader it has
 * never seen, or a deploy in which the writer has not run yet.
 *
 * The distinction it exists to hold is the whole of the zero-state problem. A reader who really spent
 * nothing and a reader nothing is known about used to produce the same blank; the first is now a `0`
 * off a bundle that positively says so, and this sentence is the second. Which is why it must not
 * reassure and must not offer a number.
 *
 * The one cause that carries a `brief`, and the reason that field exists. `reason` argues the whole
 * of the above and every clause of it is true, but it is four sentences in the harness's own words —
 * bundle, producer, actor, pass — and this is the state most readers are in, so those four sentences
 * were what a reader met first, above the figures that do have numbers. The phrase says the same
 * fact in the reader's words and no more of it than they need to keep reading; the argument is on the
 * picker row and the tooltip, where somebody asking why can reach it.
 */
const NO_ACTOR_BUNDLE: Unavailable = {
  available: false,
  code: 'noActorBundle',
  reason: 'There is no bundle of your own in this data, so none of your own totals can be read, '
    + 'not even as zero. A zero would claim a measurement nothing made. The producer writes one per '
    + "actor it has seen; yours arrives after its next pass. Everyone's figures are on the scope "
    + 'switch meanwhile.',
  /*
   * Absence, and nothing that could be read as a measurement: no zero, no "none", no failure. `yet`
   * is the weakest form of what `reason` states outright (the producer writes one per actor it has
   * seen), and it is the word that keeps "not recorded" from reading as "recorded as nothing".
   *
   * `Everyone's` is the way out, in the words the switch above and the tiles beside it already use —
   * so the sentence points at a control the reader can see instead of instructing them to use it. It
   * says those figures ARE here, not that they are being shown: the scope opens on `yours`, and a
   * phrase that claimed otherwise would describe a page the reader is not looking at.
   */
  brief: "Your own figures aren't in this report yet, but Everyone's are.",
};

/**
 * A personal bundle with no daily history on it.
 *
 * Its own sentence rather than `noActorBundle`'s, because it is a different fact with a different
 * lifetime: the bundle IS there and its other halves — the run rows behind the suggestion cards,
 * the waste findings — are readable, so this is not "we know nothing about you". It is a bundle
 * written before the producer kept day rows, and it resolves on that actor's next pass rather than
 * on their next session.
 *
 * Not a `0` either, and this is the case that most invites one: the bundle is present, so it is
 * tempting to read a missing history as an empty one. An empty history is `[]` and says the actor was
 * swept and had no day of activity; an absent one says nobody looked.
 */
const NO_ACTOR_HISTORY: Unavailable = {
  available: false,
  code: 'noActorHistory',
  reason: 'Your bundle carries no day-by-day history yet, so a total over your own runs cannot be '
    + 'summed from it. That is a bundle written before the producer started keeping daily rows, not '
    + 'a record of your having run nothing.',
};

/**
 * The shared figures are not offered over a period, and it comes down to ONE count no shared row
 * carries YET.
 *
 * The shared bundle carries a row per day and those sums add up, so a period total is arithmetically
 * available. What makes a shared figure readable at all is the denominator beside it — how many
 * sessions RECORDED the term — and on the harvest behind these figures tokens are measured on 354 of
 * 553 sessions, so a total captioned by anything coarser reads as complete over two thirds of a
 * population. `across 553 runs · 30 of 30 days measured` is the failure this file's opening rules
 * exist to prevent, stated with total confidence.
 *
 * The count now has a form. The producer writes a per-term session count onto both row kinds, spelled
 * identically, so one summing rule would serve both scopes. What it does not have is a ROW: a day row
 * is written once and carried forward untouched and nothing back-fills it, so the shared rows on the
 * wire are still the shape they had before the count existed. Read against the live shared history,
 * they carry no such block — and they are also short of the uncached-input term, the price, the model
 * roster and the evicted-days bucket that a cumulative shared total would need. So the refusal is
 * narrower than it was and it is not yet empty.
 *
 * For the reader's OWN figures the same absence is survivable, and that asymmetry is the whole reason
 * this is scoped rather than global: a personal footnote can say in words that the coverage was not
 * recorded and still show a figure, because the alternative there is no figure at all. A shared figure
 * has an alternative — the scorecard's one window, whose per-term session counts each tile's own
 * footnote states exactly — so declining the period costs a reader nothing and claims nothing.
 *
 * A HARD predicate and not a sniff at the payload, deliberately. A surface that switched itself on
 * when it spotted a coverage block on one row would total a period from rows that carry none and
 * caption it as if they all did. This changes when the shared row changes, in one place.
 */
const NO_PERIOD: Unavailable = {
  available: false,
  code: 'noPeriod',
  reason: "Everyone's figures are not totalled over a period. A shared figure has to say how many "
    + 'sessions recorded it, since tokens are measured on two thirds of this population and not all '
    + 'of it, and the shared daily rows carry no such count. The producer records one per day now, '
    + 'but a row is written once and then carried forward, so no shared row on the wire has one yet. '
    + 'They are shown over the latest harvest window instead.',
};

function notInBundle(what: string): Unavailable {
  return {
    available: false,
    code: 'notInBundle',
    reason: `This bundle's scorecard does not carry ${what}, so the figure cannot be read from `
      + 'it.',
  };
}

/**
 * A term missing from the day rows a scope reads.
 *
 * Live, not defensive. The widest terms on a day row are newer than the rows themselves, and a row is
 * written once and then carried forward untouched — so rows from before the producer recorded a term
 * keep the shorter shape for as long as they exist, and saying so is the difference between "the wire
 * never sent this" and "nothing measured it".
 *
 * `what` is a BARE noun — `token total`, not `a token total` — because this reads "carry no <what>".
 * Whose rows they are changes the subject of the sentence and nothing else, so the two spellings are
 * one string with one clause swapped rather than two reasons to keep in step.
 */
function notOnTheseDays(what: string, scope: MetricScope): Unavailable {
  const whose = scope === 'yours' ? 'Your own daily rows' : "The shared bundle's daily rows";
  return {
    available: false,
    code: 'notInBundle',
    reason: `${whose} carry no ${what}, so the figure cannot be summed from them. A row is written `
      + 'once and carried forward, so rows from before the producer recorded that term still have '
      + 'the shorter shape they had then.',
  };
}

function noDenominator(reason: string): Unavailable {
  return { available: false, code: 'noDenominator', reason };
}

/**
 * A `MeasuredSum` off the wire, or `undefined` when this bundle does not carry one.
 *
 * Shape-checked rather than reached through, because a field the type declares as required CAN be
 * absent in practice: a real emitted bundle has no `scorecard.attribution` at all. Reaching
 * through an absent term throws, and one missing term must not take down a pane of figures that
 * are sound — the same argument `compactNumber` makes for returning a word instead of raising.
 */
function term(raw: unknown): MeasuredSum | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const candidate = raw as MeasuredSum;
  return typeof candidate.measuredOf === 'number' && typeof candidate.total === 'number'
    ? candidate : undefined;
}

/**
 * The same check for `cost`, which carries two token counts its siblings do not.
 *
 * One shape check, reused rather than repeated: `MeasuredCost` adds only OPTIONAL fields, so a
 * value that satisfies the run denominator above already satisfies this type, and the two counts
 * are validated where they are read (`shortfall`) because a bundle predating them carries neither.
 * A second copy of the `measuredOf`/`total` check here is how cost's coverage and its siblings'
 * would come to disagree about what a readable term is.
 */
function costTerm(raw: unknown): MeasuredCost | undefined {
  return term(raw) as MeasuredCost | undefined;
}

/**
 * The number in a possibly-unmeasured value, or `undefined` when there is not one.
 *
 * `isSentinel` alone is not enough to filter a list before summing it. It matches exactly three
 * strings, so a `null` or a `"7"` off a malformed wire would survive the filter and land in a
 * `reduce`, where `null` contributes 0 — absence rendered as none, which is the one thing this
 * surface must not do.
 */
function usable(value: Measured<number>): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * A value ready for a tile: the number if there is one, otherwise a sentinel.
 *
 * The producer's own marker is preserved where it sent one, because `NO_DATA`, `UNKNOWN` and
 * `UNMEASURED` carry different tooltips. Anything else that is not a finite number collapses to
 * `UNMEASURED`: handing it to a formatter would print `not a count`, which reads as a producer
 * bug rather than as an absent measurement, and `null` would print as `null`.
 */
function measured(value: Measured<number>): Measured<number> {
  const number = usable(value);
  if (number !== undefined) return number;
  return isSentinel(value) ? value : 'UNMEASURED';
}

/**
 * `354 of 553 sessions`.
 *
 * The noun is what makes the fraction readable — under a tile labelled "Tokens", a bare
 * `354 of 553` does not say 553 of what, and the answer (sessions, not tokens) is the whole
 * point of the caption.
 *
 * `fractionLabel` answers with a SENTENCE when the pair is not a fraction at all
 * (`nothing to measure`, `not a count`), and appending a noun to one of those produces
 * `nothing to measure sessions`. So the noun is attached only when the label is literally the
 * fraction; if that spelling ever changes, this degrades to the bare diagnostic rather than to a
 * wrong number.
 */
function coverage(measuredOf: number, total: number, unit: string): string {
  const label = fractionLabel(measuredOf, total);
  return label === `${measuredOf} of ${total}` ? `${label} ${unit}` : label;
}

/**
 * The unit every coverage fraction counts in, at BOTH scopes.
 *
 * `354 of 553 sessions` read off the scorecard, whose `total` is one verdict per session, and the same
 * fraction summed off a reader's own `coverage` block, which counts the sessions whose spend landed on
 * each day, count the same thing. One word for it is what stops one figure being captioned two ways
 * depending on whose runs it is about — and it is `coverage` on both sides rather than `runs`, which
 * counts requests. See this file's opening note.
 */
const SESSIONS = 'sessions';

/** The four token categories, named once. The type comes off the SCORECARD's split and is used to
 *  index the per-run one too, so the day the two stop agreeing is a compile error here. */
type CategoryKey = keyof NonNullable<ImproveData['scorecard']>['spend']['byCategory'];

/** A whole-population sum with its coverage as the footnote. The plain case, and the common one. */
function populationSum(
  data: ImproveData, pick: (spend: NonNullable<ImproveData['scorecard']>['spend']) => unknown,
  what: string,
): ScalarReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const found = term(pick(data.scorecard.spend));
  if (found === undefined) return notInBundle(what);
  return {
    available: true,
    value: measured(found.sum),
    sub: coverage(found.measuredOf, found.total, SESSIONS),
  };
}

// --- the reader's own days ---------------------------------------------------
/**
 * A calendar day off the wire, or `undefined` when the row cannot be placed in time.
 *
 * The date is the whole check. A row with no `YYYY-MM-DD` on it cannot be put inside or outside a
 * trailing window, cannot be ordered, and cannot be counted as a day — so it is dropped rather than
 * folded in on the strength of the numbers it happens to carry, which would put spend in a total
 * whose basis label could not account for it.
 *
 * The spelling is required exactly, and `formatIsoDay` requires the same one, so a row this admits
 * is a row the basis label can name.
 */
function day(raw: unknown): ActorDay | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const candidate = raw as ActorDay;
  return typeof candidate.date === 'string' && formatIsoDay(candidate.date) !== null
    ? candidate : undefined;
}

/**
 * `YYYY-MM-DD` `days` earlier, or `undefined` when the date is not one.
 *
 * Field arithmetic through `Date.UTC` rather than string surgery, because a month boundary is not a
 * subtraction on the day field. UTC on both ends, matching the producer, which stamps a row's date
 * off the same `createdAt` it puts on the item.
 */
function dayBefore(date: string, days: number): string | undefined {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (parts === null) return undefined;
  const at = new Date(Date.UTC(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3])));
  at.setUTCDate(at.getUTCDate() - days);
  const month = String(at.getUTCMonth() + 1).padStart(2, '0');
  return `${at.getUTCFullYear()}-${month}-${String(at.getUTCDate()).padStart(2, '0')}`;
}

/**
 * The READER'S OWN day rows, and which absence to report when there are none.
 *
 * The reader's own and no one else's, because there is no other caller. Every `METRICS` entry routes
 * its shared side to a scorecard reader and `describeBasis` answers the shared scope before it gets
 * here, so `cumulate` — this function's only caller — is reached at the personal scope alone. It used
 * to take a scope and normalise the population's day rows into this shape as well; that branch had no
 * render path and its normaliser could not execute.
 */
function daysOf(data: ImproveData): {
  history: unknown; rolledUp: unknown; firstDate: unknown; absent: Unavailable;
} {
  return {
    history: data.actor?.history,
    rolledUp: data.actor?.rolledUp,
    firstDate: data.actor?.historyFirstDate,
    absent: data.actor === undefined ? NO_ACTOR_BUNDLE : NO_ACTOR_HISTORY,
  };
}

/**
 * The periods a reading covers: the day rows inside the chosen span, plus the bucket
 * standing for the days retention has dropped.
 *
 * ONE object rather than a row list, because a cumulative total is rows AND the bucket while a
 * trailing one is rows alone, and that difference has to be decided once. `firstDate` is what the
 * basis label prints, so it is derived here from the same rows the numbers come from and cannot
 * describe a different span from the figures beside it.
 */
interface Cumulation {
  /** Inside the period, oldest first, one per date. */
  rows: readonly ActorDay[];
  /** Present for a cumulative reading only — an anchored window cannot contain evicted days. */
  rolledUp?: RolledUpDays;
  /** Earliest day the reading covers, the bucket included. `undefined` when it covers none. */
  firstDate?: string;
  /** Latest day the rows reach, which is what a trailing window is anchored to. */
  lastDate?: string;
}

/**
 * A rolled-up bucket's own bookkeeping, as against the figures IN it. The producer strips exactly
 * these before folding a day into a bucket and rebuilds them after (`aws_lambda`), so a bucket
 * carrying nothing else is a bucket with nothing in it.
 */
const BUCKET_BOOKKEEPING: readonly string[] = ['days', 'fromDate', 'toDate'];

/**
 * Whether the evicted bucket is a PERIOD at all, which is what decides if a cumulative total folds
 * it in.
 *
 * The day count answers wherever the bucket states one. `days: 0` is the producer's measured "nothing
 * has been evicted" — it writes the bucket from its first pass with that count and no spend terms
 * beside it — so a reader who was swept and found idle has an empty `history` AND that bucket, and
 * admitting it makes their figures read "the wire carries no such term" instead of the `0` the
 * producer went to the trouble of recording.
 *
 * Where the count is absent or unmeasured, the bucket is asked what it CARRIES rather than handed a
 * default width. Defaulting to a day admits an empty bucket on a claim nobody wrote; defaulting to
 * none drops spend that is real and silently shrinks a total. Neither default is a measurement, and a
 * bucket standing for evicted days carries their figures while one standing for none carries only its
 * own dates. Today's producer always writes the count, so this decides only the buckets it did not
 * write.
 */
function standsForDays(evicted: RolledUpDays): boolean {
  const days = usable(evicted.days ?? 'UNMEASURED');
  if (days !== undefined) return days > 0;
  return Object.entries(evicted)
    .some(([key, value]) => value !== undefined && !BUCKET_BOOKKEEPING.includes(key));
}

/**
 * Which of the reader's days a PERIOD covers. The ONE place a period becomes a set of rows.
 *
 * Takes the period alone and not the whole view, which is what makes the personal-only reading
 * structural rather than a rule to remember: the rows are `data.actor`'s (see `daysOf`), so a caller
 * that could hand this a scope could be answered with one reader's days under everyone's label.
 *
 * Three things happen here and nowhere else. Rows are keyed by date, LAST occurrence winning, which
 * is the producer's own storage rule ("the current date REPLACES any prior row for that day") applied
 * to the fold — so a producer pass over an already-recorded day moves no total, which is the property
 * that makes a re-run safe. They are sorted, because a total and a window both depend on order and
 * trusting the wire for it costs nothing to remove. And a trailing window is anchored to the LATEST
 * row rather than to the browser clock: a figure that changed depending on when it was looked at
 * would not be a measurement, and the basis label names the anchor so the span is readable.
 *
 * Ordering rows by `date` is a string comparison, which is safe here for the reason it is banned on
 * timestamps: `YYYY-MM-DD` is fixed-width, zero-padded and carries no offset, so lexicographic order
 * IS chronological order. A wire timestamp is neither.
 */
function cumulate(data: ImproveData, period: MetricPeriod): Cumulation | Unavailable {
  const { history, rolledUp, firstDate: historyFirstDate, absent } = daysOf(data);
  // An absent history is a population nobody totalled; an empty one was swept and found idle. Only
  // the first is unavailable, and `[]` must reach the readings below as a measured nothing.
  if (!Array.isArray(history)) return absent;

  const byDate = new Map<string, ActorDay>();
  for (const raw of history) {
    const row = day(raw);
    if (row !== undefined) byDate.set(row.date, row);
  }
  const rows = [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1));

  const span = PERIOD_DAYS[period];
  if (span === null) {
    // A bucket that stands for no day is not a period, and folding it in breaks the zero state
    // outright — see `standsForDays`.
    const evicted = rolledUp && typeof rolledUp === 'object'
      ? (rolledUp as RolledUpDays) : undefined;
    const bucket = evicted !== undefined && standsForDays(evicted) ? evicted : undefined;
    return {
      rows,
      rolledUp: bucket,
      /*
       * The producer's own earliest date wins over the first retained row, because it is the only
       * one that accounts for the bucket: with days evicted, the first row is where the ROWS start
       * and not where the figure does. Falling back to the first row when the producer sent none
       * understates nothing — with no bucket the rows ARE the whole of it.
       */
      firstDate: typeof historyFirstDate === 'string' && formatIsoDay(historyFirstDate) !== null
        ? historyFirstDate : rows[0]?.date,
      lastDate: rows[rows.length - 1]?.date,
    };
  }

  const anchor = rows[rows.length - 1]?.date;
  // `span - 1` because the window INCLUDES its anchor day: seven days ending today is today and the
  // six before it, and subtracting seven would quietly make it eight.
  const from = anchor === undefined ? undefined : dayBefore(anchor, span - 1);
  const kept = from === undefined ? [] : rows.filter((row) => row.date >= from);
  return { rows: kept, firstDate: kept[0]?.date, lastDate: anchor };
}

/** Every period a reading folds in: the retained days, and the evicted bucket when there is one. */
function buckets(c: Cumulation): readonly DaySpend[] {
  return c.rolledUp === undefined ? c.rows : [...c.rows, c.rolledUp];
}

/**
 * A coverage fraction: how many of a span's sessions recorded a term, out of how many it held.
 *
 * Named because two functions pass it between them — the one that sums it off the rows and the one
 * that words it — and an anonymous pair spelled twice is how the numerator and the denominator would
 * come to be read in the wrong order at one of them.
 */
interface Covered {
  measuredOf: number;
  total: number;
}

/**
 * What a term was measured on across a set of periods, or `undefined` when the wire will not say.
 *
 * BOTH halves sum, which is the property that makes a period total captionable at all: a day's
 * `coverage.tokens` counts the sessions that recorded tokens and `coverage.total` counts the sessions
 * the day held, so summing each over the days in the period yields exactly the pair a scorecard term
 * carries. `354 of 553 sessions` over a month means the same thing it means over a window.
 *
 * ALL-OR-NOTHING, and that is the load-bearing decision. If one period in the fold carries no
 * `coverage` block, its session count is unknown — so a fraction built from the rest would divide a
 * partial numerator by a partial denominator and could read as complete over a figure that is not.
 * Better to state that the coverage was not recorded than to state a fraction that is wrong in the
 * safe-looking direction.
 *
 * The block never holds a sentinel and never reports `0 of 0`, so nothing here branches on either: a
 * value that is not a finite number means the row predates the block, which is the one case this
 * answers `undefined` for.
 */
function coverageOf(
  periods: readonly DaySpend[], pick: (of: DayCoverage) => number | undefined,
): Covered | undefined {
  let measuredOf = 0;
  let total = 0;
  for (const period of periods) {
    const block = period.coverage;
    if (!block || typeof block !== 'object') return undefined;
    const measured = pick(block);
    const held = block.total;
    if (!Number.isFinite(measured) || !Number.isFinite(held)) return undefined;
    measuredOf += measured as number;
    total += held as number;
  }
  return { measuredOf, total };
}

/**
 * How many runs a set of periods holds, or `undefined` when any one of them will not say.
 *
 * Deliberately NOT the coverage denominator, because on a per-actor row they are different counts:
 * `runs` counts REQUESTS and `coverage.total` counts SESSIONS, so a request that fanned out into plan
 * sub-sessions is one run and several sessions — a real day reads `runs: 17, coverage.total: 19`. Both
 * are rendered because they answer different questions: this says how much of the reader's own activity
 * a total is drawn from, and the coverage pair says how much of it was measured.
 *
 * ALL-OR-NOTHING, exactly as `coverageOf` is and for the same reason. An extent summed over the periods
 * that happened to carry a run count, beside a figure summed over all of them, is a partial numerator
 * with no denominator to declare it partial: `across 2 runs` under a total drawn from a month of days
 * reads as the whole of the reader's activity and understates it silently. A count nobody wrote is said
 * in words instead.
 */
function runsIn(periods: readonly DaySpend[]): number | undefined {
  let counted = 0;
  for (const period of periods) {
    const runs = usable(period.runs ?? 'UNMEASURED');
    if (runs === undefined) return undefined;
    counted += runs;
  }
  return counted;
}

/**
 * What a cumulative footnote says when the wire will not state its coverage at all.
 *
 * A row is written once and then carried forward untouched, and the per-term session counts are newer
 * than the rows — so a total that folds in one row from before them cannot say how much of itself was
 * measured, and that is the ordinary case for as long as such rows exist.
 *
 * Said in words because every number that could stand in for it would be a claim. A fraction over the
 * rows that DO carry the block puts a partial numerator over a partial denominator and can read as
 * complete; `0 of 0` reads as complete over nothing; and a zero numerator would assert that no session
 * measured the term, where the truth is that nobody counted.
 *
 * Phrased like the `run count not recorded` beside it, because the two are the same kind of statement
 * about the same row — a count the producer had not started writing when it was written.
 */
const COVERAGE_NOT_RECORDED = 'session coverage not recorded';

/**
 * `across 14 runs · 9 of 19 sessions` — what the figure is drawn from, and how much of that recorded
 * the term.
 *
 * TWO counts, and they are deliberately different numbers rather than one stated twice. The extent is
 * in RUNS, which count REQUESTS, so it says how much of the reader's own activity the figure is drawn
 * from. The coverage is in SESSIONS, which is what the sums were measured OVER — a request that fanned
 * out into plan sub-sessions is one run and several sessions, so a real day reads
 * `runs: 17, coverage.total: 19`.
 *
 * The coverage is stated even when every session recorded the term, which the day-spanned proxy it
 * replaced was silent about. Three reasons. A complete fraction is not a restatement of the extent
 * here, because the two counts differ. Silence on a partial figure is the exact failure every
 * denominator on this surface exists to prevent — an uncaptioned sum over two thirds of a population
 * reads as all of it, and a reader cannot tell silence-because-complete from silence-because-nobody-
 * said. And it is the spelling the shared footnotes have always used, so one figure cannot come to be
 * captioned two ways at the two scopes.
 */
function cumulativeSub(runs: number | undefined, covered: Covered | undefined): string {
  /*
   * `across 0 runs` is never printed, and that is not cosmetic. A day row counts REQUESTS, and a
   * request that fanned out into plan sub-sessions can fall outside the producer's scoring cap while
   * those sub-sessions' spend still lands on the row — so a day can carry real tokens against a run
   * count of zero. `across 0 runs` beside a seven-figure total reads as a defect in the figure; what
   * is actually true is that the spend is there and the requests behind it were not all attributed.
   */
  const extent = runs === undefined
    ? 'run count not recorded'
    : runs === 0 ? 'runs not attributed' : `across ${runs} ${pluralise(runs, 'run')}`;
  const clause = covered === undefined
    ? COVERAGE_NOT_RECORDED
    : coverage(covered.measuredOf, covered.total, SESSIONS);
  return `${extent} · ${clause}`;
}

/**
 * What a reader with a bundle and no activity in the period is told, beside the `0`.
 *
 * The footnote is what makes that `0` readable as a measurement rather than as a formatting
 * accident: it says the producer looked and found nothing, which is precisely the fact that
 * distinguishes this from the two absences above it.
 */
const NO_ACTIVITY = 'no runs in this period';

/**
 * One term summed over the reader's own days. The shared front half of every personal read.
 *
 * All four outcomes are decided here, once, so no metric can spell one of them differently:
 *
 *  - no period carries the term at all → the wire never sent it (`notOnTheseDays`).
 *  - no period at all → the producer swept and found nothing → a measured `0`.
 *  - periods carry it and none measured it → the sentinel.
 *  - otherwise → the sum of the measured ones, captioned with the sessions that recorded it.
 *
 * The order matters. Asking "did anything measure it" before "was there anything to measure" would
 * report a reader who ran nothing as a reader whose term is missing, which is the same conflation
 * between absence and zero the whole surface is built to refuse — from the other direction.
 */
interface DayTerm {
  /** The term's value on one period. */
  of: (period: DaySpend) => Measured<number> | undefined;
  /** How many of that period's sessions recorded it. */
  covered: (of: DayCoverage) => number | undefined;
  /** The term's name for a reader, as a BARE noun — see `notOnTheseDays`. */
  what: string;
}

/**
 * The four scalar spend terms, each paired with the coverage entry that says what it rests on.
 *
 * Paired HERE rather than at the eight call sites, because the pairing is the one thing that must not
 * be got wrong: a sum of tokens captioned by the sessions that recorded LATENCY is a fraction that
 * looks right and is about a different measurement. Latency is summed from spans and every token term
 * comes off the usage record, so the two genuinely diverge — `latencyMs 2/2` beside `tokens 1/2` on one
 * day is a measured shape, not a hypothetical.
 *
 * The category terms are not here: their two halves are both indexed by the same `CategoryKey`, so the
 * pairing is structural there and cannot be mismatched.
 */
const TERM = {
  tokens: { of: (p) => p.tokens, covered: (c) => c.tokens, what: 'token total' },
  llmCalls: { of: (p) => p.llmCalls, covered: (c) => c.llmCalls, what: 'LLM call count' },
  latencyMs: { of: (p) => p.latencyMs, covered: (c) => c.latencyMs, what: 'latency total' },
  cost: { of: (p) => p.cost, covered: (c) => c.cost, what: 'price' },
} as const satisfies Record<string, DayTerm>;

function cumulativeSum(
  data: ImproveData, view: MetricView, term: DayTerm,
  /**
   * What a period holding no days at all reads as. `0` for every count and duration, because the
   * producer positively recorded that the reader ran nothing and that IS the measurement.
   *
   * A parameter and not a branch inside, because exactly one metric differs and it must say so out
   * loud at its own call site: a price of `$0.00` reads as a session that ran and was free, which is
   * a claim nobody priced. Declared here so the exception is visible in the type rather than hidden
   * in a `sub === …` comparison downstream.
   */
  onNoActivity: Measured<number> = 0,
): ScalarReading | Unavailable {
  const found = cumulate(data, view.period);
  if (!('rows' in found)) return found;
  const periods = buckets(found);
  const carried = periods.filter((period) => term.of(period) !== undefined);
  if (periods.length > 0 && carried.length === 0) {
    return notOnTheseDays(term.what, view.scope);
  }
  if (periods.length === 0) return { available: true, value: onNoActivity, sub: NO_ACTIVITY };

  const measured: number[] = [];
  for (const period of carried) {
    const value = usable(term.of(period) as Measured<number>);
    if (value !== undefined) measured.push(value);
  }
  /*
   * The coverage is taken over EVERY period the figure folds in, not only the ones whose sum landed.
   * A period that measured the term on none of its sessions carries a `0` against its real session
   * count, so it belongs in the denominator — dropping it would report a partial total as complete,
   * which is the whole reason the count is on the row.
   */
  const sub = cumulativeSub(runsIn(periods), coverageOf(periods, term.covered));
  if (measured.length === 0) return { available: true, value: 'UNMEASURED', sub };
  return { available: true, value: measured.reduce((a, b) => a + b, 0), sub };
}

/**
 * The share arithmetic itself, written once for the population and for the reader's own runs.
 *
 * Both callers reach it having already decided which numbers are theirs and what the footnote says;
 * what is shared is the part they must not answer differently — that a share with nothing under it
 * is no share at all. `0/0` is not `0%`, and a bar drawn from it would assert a split nothing
 * measured, so an empty whole is `noDenominator` on both paths and cannot come to be a zero on one
 * of them.
 *
 * The headline `value` is the PART's magnitude, not the whole's. A 76% share of two hundred tokens
 * and of two hundred million are different facts, and the bar cannot tell them apart.
 */
function shareOf(part: number, whole: number, sub: string): RatioReading | Unavailable {
  if (whole <= 0) {
    // The footnote is parenthesised rather than run on, because the two callers word theirs
    // differently — one names sessions, the other opens with `across` — and a sentence built to read
    // around one of them reads as a stutter around the other.
    return noDenominator(`Every token category measured 0 (${sub}), so there is no share to draw.`);
  }
  return { available: true, value: part, ratio: part / whole, sub };
}

/** A split nothing measured. Both the figure and the share are unmeasured — a sum missing one of
 *  its addends is not a smaller sum — and `RatioBar` hatches it rather than drawing an empty
 *  track that would read as 0%. */
function unmeasuredShare(sub: string): RatioReading {
  return { available: true, value: 'UNMEASURED', ratio: 'UNMEASURED', sub };
}

/**
 * A share of a whole over the POPULATION's category sums, from terms that are all measured or not
 * at all.
 *
 * Three outcomes, matching this file's first rule. Any term absent from the bundle is a metric that
 * cannot be read; any term unmeasured makes the whole split unmeasured; and a measured split adding
 * to nothing has no share, which `shareOf` decides for this path and the personal one alike.
 */
function share(
  data: ImproveData,
  partTerms: readonly CategoryKey[],
  restTerms: readonly CategoryKey[],
  what: string,
): RatioReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const byCategory = data.scorecard.spend?.byCategory;
  const found = [...partTerms, ...restTerms].map((key) => term(byCategory?.[key]));
  if (found.some((t) => t === undefined)) return notInBundle(what);
  const terms = found as MeasuredSum[];

  /*
   * One coverage for the whole split, taken from the least-covered term and the largest
   * population any of them names. The terms come from one `usage` record per session, so today
   * they always agree; if they ever diverge, the sum spans more sessions than the smallest term
   * and understating the coverage is the only safe direction for a caption whose entire job is to
   * stop a partial sum reading as a complete one.
   */
  const sub = coverage(
    Math.min(...terms.map((t) => t.measuredOf)),
    Math.max(...terms.map((t) => t.total)),
    SESSIONS,
  );

  const numbers = terms.map((t) => usable(t.sum));
  if (numbers.some((n) => n === undefined)) return unmeasuredShare(sub);
  const values = numbers as number[];
  return shareOf(
    values.slice(0, partTerms.length).reduce((a, b) => a + b, 0),
    values.reduce((a, b) => a + b, 0),
    sub,
  );
}

/**
 * The same share, over the reader's OWN days.
 *
 * A day contributes to BOTH sides of the split or to NEITHER, which is the one thing this does
 * differently from `share` and it is not a shortcut. `share` reads four population sums that each
 * carry their own coverage and takes the worst; here the categories arrive per day, so admitting a
 * day that measured cache reads and not cache writes would put a numerator over a denominator taken
 * across different days — a number that is not a share of anything, wearing a share's shape.
 * Excluded, the ratio is exact across the days that contributed. The footnote then counts the SESSIONS
 * behind those days, exactly as `share` counts them off the population's own coverage, so the two
 * scopes caption one split the same way.
 *
 * A period with no split at all is a different answer again: `byCategory` is newer than the rows, so
 * a row from before it has none — reported as a term the wire does not carry, never as a split that
 * measured nothing.
 *
 * And a period-with-no-days is the one case a share cannot answer with `0` the way a total can. A
 * reader who ran nothing spent no tokens, which is a real zero; but `0 of 0` is not `0%`, and a bar
 * drawn from it would assert a split nothing measured. `shareOf` already refuses an empty whole, and
 * that refusal is reached here rather than special-cased, so the two paths cannot come to disagree.
 */
function cumulativeShare(
  data: ImproveData, view: MetricView,
  partTerms: readonly CategoryKey[], restTerms: readonly CategoryKey[],
): RatioReading | Unavailable {
  const found = cumulate(data, view.period);
  if (!('rows' in found)) return found;
  const periods = buckets(found);
  if (periods.length > 0 && periods.every((period) => period.byCategory === undefined)) {
    return notOnTheseDays('per-category token split', view.scope);
  }

  const keys = [...partTerms, ...restTerms];
  let part = 0;
  let whole = 0;
  let contributed = 0;
  for (const period of periods) {
    const values = keys.map((key) => usable(period.byCategory?.[key] ?? 'UNMEASURED'));
    if (values.some((value) => value === undefined)) continue;
    const numbers = values as number[];
    part += numbers.slice(0, partTerms.length).reduce((a, b) => a + b, 0);
    whole += numbers.reduce((a, b) => a + b, 0);
    contributed += 1;
  }

  /*
   * ONE coverage for the whole split, from the LEAST-covered of its categories — the same rule `share`
   * applies to the population's four sums, and for the same reason: the terms come off one usage record
   * per session so today they always agree, and if they ever diverge the sum spans more sessions than
   * the smallest term, so understating is the only safe direction for a caption whose entire job is to
   * stop a partial sum reading as a complete one.
   */
  const sub = cumulativeSub(runsIn(periods), coverageOf(periods, (of) => {
    const counts = keys.map((key) => of.byCategory?.[key]);
    // An absent count for any ONE side is no coverage for the split at all — the same all-or-nothing
    // rule the loop above applies to the arithmetic, so a fraction can never be built across sides the
    // wire counted differently.
    if (counts.some((n) => typeof n !== 'number')) return undefined;
    return Math.min(...counts as number[]);
  }));
  if (contributed === 0) {
    /*
     * No day measured the split — which on a period holding no days at all means there was nothing
     * to measure rather than a measurement that failed. Said as no-denominator in that case, since a
     * hatched bar would claim the producer tried.
     */
    if (periods.length === 0) {
      return noDenominator(`${NO_ACTIVITY}, so there is no split to draw. A period with no runs in `
        + 'it spent no tokens, but no share of nothing is 0%.');
    }
    return unmeasuredShare(sub);
  }
  return shareOf(part, whole, sub);
}

// --- money ------------------------------------------------------------------
/** What `formatUsd` answers for input that cannot be an amount of money, mirroring
 *  `compactNumber`'s `not a count`: a negative price is a producer bug, and `$-51.70` launders it
 *  into something a reader will believe. */
const NOT_A_COST = 'not a cost';

/**
 * `$34.82`, `$0.23`, `<$0.01`.
 *
 * Money and not a count, so `compactNumber` is the wrong formatter: below a thousand it prints the
 * number as given, so a real price arrives as the producer's raw float (`0.2327805`), and below a
 * millionth it arrives in exponent notation (`9e-7`). A price has a fixed unit and its resolution
 * is the cent, so two places always.
 *
 * `$0.00` is reserved for exactly zero — a session that used a priced model and no tokens really
 * did cost nothing. Anything that would merely ROUND to it is stated as a bound instead, because
 * `$0.00` on a real cost reads as free. The check is on the rendered string rather than on a
 * threshold, so no float that survives `toFixed` can get past it.
 */
export function formatUsd(usd: number): string {
  if (!Number.isFinite(usd) || usd < 0) return NOT_A_COST;
  if (usd === 0) return '$0.00';
  const dollars = usd.toFixed(2);
  return dollars === '0.00' ? '<$0.01' : `$${dollars}`;
}

/** How much of the producer's cost basis fits under a figure: about a line of meta text. */
const CAVEAT_HEAD = 64;

/** What the footnote says when a bundle priced a figure and said nothing about how. Never
 *  silence — a bare dollar amount with no provenance reads as billed truth. */
const NO_BASIS_STATED = 'pricing basis not stated';

/**
 * The head of `spend.costBasis`, for the footnote under a priced figure.
 *
 * The whole basis is 839 characters (measured on the sentence the producer ships today): the
 * pricing page, the date it was read by hand, the region check that justifies pricing a us-west-2
 * harness off one region's column, that the cache-write rate is the five-minute one — which
 * understates a one-hour TTL by up to 1.6x on the term that dominates this population's bill — and
 * that pricing is partial where a model has no rate row. All of that is a CAVEAT on a real number
 * rather than an explanation of an absent one, so it belongs beside the figure; but a tile is a
 * third of a row, so only the head of it goes there and `wholeCaveat` below routes the rest.
 *
 * Read off the wire and never composed here. A hedge the UI writes itself goes on asserting last
 * quarter's terms after the producer re-sources its rates, and where the rates came from and when
 * is the whole of this figure's trustworthiness.
 */
function costCaveat(basis: unknown): string {
  if (typeof basis !== 'string' || basis.trim() === '') return NO_BASIS_STATED;
  if (basis.length <= CAVEAT_HEAD) return basis;
  /*
   * The producer opens with a summary clause of its own (`ESTIMATE, not billed truth: …`), so break
   * at that colon when there is one: a clause the producer wrote to stand alone is a better footnote
   * than a mechanical cut, and it needs no ellipsis because it is a whole statement rather than a
   * truncated one. Failing that, `abridge` cuts on a word and marks it.
   */
  const head = basis.slice(0, CAVEAT_HEAD);
  const colon = head.indexOf(':');
  return colon > 0 ? head.slice(0, colon) : abridge(basis, CAVEAT_HEAD);
}

/**
 * The same basis WHOLE, for the tile's tooltip and the picker's row — or `undefined` when the
 * footnote above already carries every word of it.
 *
 * Asks `costCaveat` rather than re-deriving the cut, so the two can never come to disagree about
 * whether anything was dropped. A second copy of the length rule is how a short basis came to be
 * printed three times, or a long one to be silently abridged with nowhere to read the rest.
 *
 * `undefined` and not the empty string on a basis the wire did not carry: the footnote already says
 * `pricing basis not stated`, and a tooltip repeating that adds nothing while an empty `title`
 * attribute gives the tile a tooltip-shaped affordance with no content behind it.
 */
function wholeCaveat(basis: unknown): string | undefined {
  if (typeof basis !== 'string' || basis.trim() === '') return undefined;
  return basis === costCaveat(basis) ? undefined : basis;
}

/**
 * `90.6k of 6.9M tokens unpriced` — how much of the money the figure above it leaves out.
 *
 * Cost's SECOND denominator, and the one a run count cannot stand in for. Pricing is per model, so
 * a session using one model the rate table has no row for is still a fully measured run: the models
 * that can be priced are, that one is skipped, and the session contributes a SHORT price. Runs
 * alone would caption that `8 of 8 sessions priced` — complete-looking over a total missing
 * whatever the skipped model spent, which is the failure these counts exist to expose. Tokens are
 * the unit because they are what the producer can count without the rate row it is missing.
 *
 * SILENT when nothing is unpriced, which is every population priced to date: the live one spends on
 * two models, the table prices both, and all 6,813,939 of its tokens are priced. A `0 tokens
 * unpriced` clause under every figure would be noise on the common case and would teach a reader to
 * skip the one line that matters on the rare one.
 *
 * Silent too when the counts are ABSENT rather than zero, because a bundle written before them
 * carries neither and `?? 0` on those would assert a completeness nobody measured. Both halves are
 * needed before either is printed: tokens missing, without the count they are missing FROM, is a
 * magnitude with no denominator, which is this file's third rule. A count that is not a POSITIVE
 * number states no shortfall at all — `0` is the measured "nothing was skipped", and a negative or
 * non-finite one is corruption, which cannot be turned into a quantity of missing money.
 *
 * The denominator is the pair's OWN sum, never `spend.tokens`. The two really can differ: a session
 * carrying token counts but no per-model breakdown is unpriceable, so its tokens are in the token
 * term and in neither half of this one — and a shortfall taken against the larger number would
 * report a smaller share of the money as missing than actually is.
 *
 * Compact figures rather than `coverage`'s raw digits, because these are token counts in the
 * millions: `6813939` is the one thing in a footnote a reader would have to count by eye.
 *
 * Takes the two COUNTS rather than the term that holds them, so any population whose pair reaches
 * this produces the same clause from the same code. The rule does not depend on whose tokens went
 * unpriced, and a second copy of it would be how one footnote came to round differently or to print
 * a `0`.
 *
 * Two callers, one reading the population's pair off the scorecard and one summing the reader's own
 * off their day rows. The rule does not depend on whose tokens went unpriced, and a second copy of it
 * is how one of the two footnotes would come to round differently or to print a `0`.
 */
function shortfall(tokensPriced: unknown, tokensUnpriced: unknown): string {
  if (tokensPriced === undefined || tokensUnpriced === undefined) return '';
  const priced = usable(tokensPriced as Measured<number>);
  const unpriced = usable(tokensUnpriced as Measured<number>);
  if (priced === undefined || unpriced === undefined || unpriced <= 0) return '';
  return `${compactNumber(unpriced)} of ${compactNumber(priced + unpriced)} tokens unpriced`;
}

/**
 * The same clause for the reader's own bill, summed off their day rows.
 *
 * SUMMED rather than read, because the counts are per day — and both halves sum, so the rolled-up
 * bucket carries them and the clause works at every period including a cumulative total. Which is the
 * whole reason it exists: a cumulative cost that quietly accumulates an unstated shortfall for a month
 * is a worse under-report than a single window's, not a smaller one.
 *
 * A day contributes to BOTH halves or to NEITHER, exactly as in a share. The producer writes the pair
 * together, so a half-carrying day should not arise — but admitting one would put a numerator over a
 * denominator taken across different days, and the clause's whole job is to be a fraction of the
 * money that is genuinely missing.
 *
 * SILENT when no day carries the pair, because absent is not a measured zero and a row predating the
 * counts would otherwise assert a complete bill nobody counted. Silent too on a measured `0`, which is
 * `shortfall`'s own rule: nothing was skipped, and saying so under every figure would teach a reader
 * to skip the one line that matters on the rare occasion it is not zero.
 *
 * It re-asks `cumulate` rather than being handed the periods. That is reuse of the one decision about
 * what a period means, not a second copy of it: the fold is pure and cheap, and threading its result
 * through `cumulativeSum` purely so cost could reach it would put an argument on all eight reads for
 * the sake of one.
 */
function cumulativeShortfall(data: ImproveData, view: MetricView): string {
  const found = cumulate(data, view.period);
  if (!('rows' in found)) return '';
  let priced = 0;
  let unpriced = 0;
  let contributed = 0;
  for (const period of buckets(found)) {
    const seen = usable(period.costTokensPriced ?? 'UNMEASURED');
    const skipped = usable(period.costTokensUnpriced ?? 'UNMEASURED');
    if (seen === undefined || skipped === undefined) continue;
    priced += seen;
    unpriced += skipped;
    contributed += 1;
  }
  return contributed === 0 ? '' : shortfall(priced, unpriced);
}

// --- what the harvest never handed over -------------------------------------
/** What a token or cost footnote says when the producer ran and audited nothing. Never silence: a
 *  bare sum reads as complete, which is exactly what the live item's `25 of 25 sessions` already
 *  says while a third of the measured tokens are missing from the figure above it. */
const DISCARD_NOT_RECORDED = 'tokens discarded not recorded';

/**
 * `13.3M of 36.8M tokens discarded` — spend the harvest measured and never handed to the sum above.
 *
 * The population's terms are summed over the sessions it ADMITTED, so a session whose spans the row
 * cap dropped is absent from both halves of `measuredOf of total` rather than short within them: the
 * live item reads complete coverage on a harvest that threw a third of the measured tokens away
 * (measured 2026-08-29, 24h: 13.3M of 36.8M). This is the only clause that can say so.
 *
 * Three readings, three different facts. A positive count is a magnitude and is stated. A measured
 * `0` is the cap not binding, and is SILENT — `shortfall`'s own rule, since a clause under every
 * figure teaches a reader to skip the one that matters. A sentinel is a harvest that never audited
 * itself, and is said in words: `?? 0` there would report an unchecked harvest as a clean one.
 *
 * Silent too when the BLOCK is absent, which is every bundle served today. That is the deploy-order
 * case `shortfall` is already silent for: the wire never sent it, so there is nothing to state, where
 * a sentinel is the producer stating that it did not look.
 *
 * Both halves before either is printed — a magnitude with no denominator is this file's third rule —
 * and the denominator is `tokensMeasured`, what the usage query saw across the whole window, never
 * `spend.tokens.sum`, which is over the admitted sample and cannot account for what it never saw.
 */
function discarded(shortfall: UsageShortfall | undefined): string {
  if (shortfall === undefined) return '';
  const dropped = usable(shortfall.tokensDiscarded);
  const measured = usable(shortfall.tokensMeasured);
  if (dropped === undefined || measured === undefined) return DISCARD_NOT_RECORDED;
  if (dropped <= 0) return '';
  return `${compactNumber(dropped)} of ${compactNumber(measured)} tokens discarded`;
}

/** The same absence for a SET count, whose unit is sessions rather than tokens. */
const MODEL_DISCARD_NOT_RECORDED = 'sessions discarded not recorded, so this count may be short';

/**
 * `24 of 44 sessions discarded, any of which may have used a model not counted here` — the shortfall
 * as a SET count can carry it.
 *
 * Its own clause rather than `discarded` above, and the difference is a claim about arithmetic rather
 * than wording. A sum is short BY the discarded tokens: state the magnitude and a reader can add it
 * back. `modelsSeen` is the size of a SET collected off the same usage records, so a discarded session
 * either used a model already counted — and the figure is exactly right — or one nobody counted, and
 * it is short by one with no way to say by how many. A token magnitude under it would be a number in
 * the wrong unit inviting the wrong repair; naming the discarded SESSIONS says what could be missing
 * without claiming how much.
 *
 * `may` and not `is`, for the same reason: on the live population's two models a discarded session is
 * very likely a third sighting of one of them. The clause states the risk, never a shortfall it cannot
 * measure.
 *
 * The same three readings as `discarded`, on the sessions pair instead of the tokens one: a positive
 * count is stated, a measured `0` is silent because the cap did not bind, and a sentinel is a harvest
 * that never audited itself and is said in words. Silent too when the block is absent — the wire never
 * sent it.
 */
function discardedModels(shortfall: UsageShortfall | undefined): string {
  if (shortfall === undefined) return '';
  const dropped = usable(shortfall.sessionsDiscarded);
  const measured = usable(shortfall.sessionsMeasured);
  if (dropped === undefined || measured === undefined) return MODEL_DISCARD_NOT_RECORDED;
  if (dropped <= 0) return '';
  return `${coverage(dropped, measured, SESSIONS)} discarded, any of which may have used a model `
    + 'not counted here';
}

// --- the metrics ------------------------------------------------------------
/**
 * Which categories make up each side of each split, named once for both scopes.
 *
 * Named rather than spelled at the reads, because the two scopes of one metric must agree about them:
 * a reader should not have to learn that `context` means one thing about their own runs and another
 * about everyone's, and a literal array per scope is two places for that to drift apart.
 *
 * `input` is UNCACHED input, so the three prompt categories are the whole of what was sent to a model
 * and `output` is the whole remainder — the four sum to the token total exactly, which is why neither
 * split needs a residual term. Cache reuse is reads against WRITES alone: the uncached and generated
 * categories are neither, and folding them in would answer a different question with a
 * plausible-looking number.
 */
const CONTEXT: readonly CategoryKey[] = ['input', 'cacheRead', 'cacheWrite'];
const GENERATED: readonly CategoryKey[] = ['output'];
const CACHE_READS: readonly CategoryKey[] = ['cacheRead'];
const CACHE_WRITES: readonly CategoryKey[] = ['cacheWrite'];

/** What an absent category split is called, so one missing term reads the same whoever's tokens it
 *  would have been measured over. */
const SPLIT = 'a per-category token split';

/**
 * Everyone's tokens, and what the harvest discarded before they were summed.
 *
 * `populationSum` states what the figure covers of the population it was given; this states what
 * never reached that population at all. Two different absences, and the second is invisible in the
 * first: the coverage fraction is `25 of 25 sessions` on the live item, complete, over a harvest whose
 * row cap dropped 24 further sessions' usage.
 */
function populationTokens(data: ImproveData): ScalarReading | Unavailable {
  const reading = populationSum(data, (spend) => spend?.tokens, 'a token total');
  if (!reading.available) return reading;
  return withClause(reading, discarded(data.scorecard?.spend?.usageShortfall));
}

/**
 * A clause appended to a shared reading's footnote, or the reading untouched when there is none.
 *
 * Named because four of the shared readings need it and each of them has several return points: the
 * join is the same decision every time — only when there IS a clause, so a bundle with nothing
 * discarded keeps the one-clause footnote it has always had rather than gaining a trailing separator.
 */
function withClause(reading: ScalarReading, clause: string): ScalarReading {
  return clause === '' ? reading : { ...reading, sub: `${reading.sub} · ${clause}` };
}

/**
 * Everyone's LLM calls, with tokens per call in the footnote when that is genuinely what it is.
 *
 * Its own function rather than a `populationSum`, because the per-call line has two preconditions
 * and both are live on real bundles.
 *
 * Carries the DISCARD clause for the same reason `populationTokens` does, and it is the same
 * shortfall: `llmCalls` is read off the very usage records the row cap dropped sessions from
 * (`harvest_live`, one query per session), so this sum is short by exactly those sessions' calls while
 * its own `measuredOf`/`total` — taken over the sessions the population ADMITTED — read complete.
 * Silence here was the reading `9a96c4e` existed to prevent, left in place on the sibling figures.
 */
function populationCalls(data: ImproveData): ScalarReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const calls = term(data.scorecard.spend?.llmCalls);
  if (calls === undefined) return notInBundle('an LLM call count');
  const clause = discarded(data.scorecard.spend?.usageShortfall);
  const reading: ScalarReading = {
    available: true,
    value: measured(calls.sum),
    sub: coverage(calls.measuredOf, calls.total, SESSIONS),
  };

  /*
   * Both terms have to be measured, and measured on the SAME sessions. The producer reads each usage
   * key independently, so a session carrying one and not the other is reachable — and dividing two
   * sums taken over different populations yields a number that is not tokens per call, while looking
   * exactly like one. Rather than caption that, the line is withheld and the footnote says which
   * input is missing.
   */
  const tokens = term(data.scorecard.spend?.tokens);
  const perCall = tokens && usable(tokens.sum) !== undefined && usable(calls.sum) !== undefined
    && (calls.sum as number) > 0
    ? Math.round((tokens.sum as number) / (calls.sum as number))
    : undefined;
  if (perCall === undefined) return withClause(reading, clause);
  if (tokens!.measuredOf !== calls.measuredOf) {
    return withClause({
      ...reading,
      sub: `${reading.sub} · tokens and calls were not measured on the same sessions, `
        + 'so there is no per-call figure',
    }, clause);
  }
  // The per-call figure leads and the discard clause trails, so the footnote reads outwards from what
  // the sum IS to what it is missing — the order `populationCost` already sets.
  return withClause(
    { ...reading, sub: `${compactNumber(perCall)} tokens per call · ${reading.sub}` }, clause);
}

/**
 * Everyone's cost: what the producer priced, and never what this file prices.
 *
 * The rates, the per-model arithmetic and the decision to price a session at all are upstream; here
 * the figure is read and set, and `costBasis` beside it says on whose authority.
 *
 * The one term with TWO denominators, and neither does the other's work. `measuredOf`/`total` count
 * sessions, as on every sibling spend term, and answer a question of their own: a session NO model of
 * which can be priced is not priced at all, so cost's coverage can be narrower than the token term's
 * over the same population. But full session coverage is not a full bill — one partly-priced session
 * leaves `8 of 8 sessions priced` short by whatever its skipped model spent — so `shortfall` states
 * the missing TOKENS after it, and only when there are some.
 */
function populationCost(data: ImproveData): ScalarReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const spend = data.scorecard.spend;
  const cost = costTerm(spend?.cost);
  if (cost === undefined) return notInBundle('a cost total');
  return {
    available: true,
    // A sentinel here is a reading, not a failure of one: the producer ran and priced nothing, and
    // the tile hatches that. Withholding the metric instead would drop the statement.
    value: measured(cost.sum),
    // Assembled from clauses and joined rather than concatenated, so a clause that is not being
    // reported costs no separator: on a bundle with nothing unpriced and nothing discarded this is the
    // two-clause footnote it has always been. The last two are different facts and both belong here —
    // `shortfall` is money this population could not price, `discarded` money that never entered it.
    sub: [costCaveat(spend?.costBasis),
          coverage(cost.measuredOf, cost.total, 'sessions priced'),
          shortfall(cost.tokensPriced, cost.tokensUnpriced),
          discarded(spend?.usageShortfall)]
      .filter((clause) => clause !== '').join(' · '),
    // Verbatim, so a reader who follows the head of the sentence in the footnote reaches the rest of
    // that same sentence rather than a paraphrase of it.
    caveat: wholeCaveat(spend?.costBasis),
  };
}

// --- which model the money went to ------------------------------------------
/**
 * What a per-model figure reads as when nothing measured it.
 *
 * One phrase for all three sentinels, as `formatWindow` collapses them for a window: in a footnote
 * clause the fact is the same one — this row contributed no measured figure, so the roster it sits in
 * has no complete denominator. The hatched badge is not available here at all, since these channels
 * are strings and not elements.
 */
const MODEL_UNMEASURED = 'not measured';

/**
 * What every per-model denominator on this surface is actually out of, said once for the roster.
 *
 * Load-bearing rather than a nicety. A row's `total` is `spend.tokens.measuredOf` — the runs whose
 * usage was measured — and NOT the population, because a run with no usage block never recorded which
 * model it did not use. So `14 of 25` on a row and `354 of 553 sessions` on the tile beside it are
 * fractions over different populations, and a reader who lines them up concludes the wrong thing about
 * how much of the harness a model covers.
 */
const MODEL_DENOMINATOR = 'A row is over the runs that recorded any usage, not over the whole '
  + 'population.';

/**
 * The unit every per-model fraction counts in.
 *
 * `runs` and not `SESSIONS`, which every other coverage fraction here counts in, and the different
 * noun is doing work: a sibling term's `354 of 553 sessions` is measured-of-population while a model
 * row's fraction is appears-in-of-measured, so two fractions in one footnote would otherwise invite a
 * reader to line up numbers over different populations. `MODEL_DENOMINATOR` says which is which.
 */
const MODEL_RUNS = 'runs';

/** Where the roster and its shares come apart, said in the reader's words rather than left to a
 *  percentage that would be wrong in the safe-looking direction. */
const NO_MODEL_SHARE = "no model's share can be drawn: the roster's tokens are not all measured";

/**
 * A `byModel` row that can be read at all: an id, and a token term carrying its own denominator.
 *
 * A row missing either cannot be placed in a roster and cannot be captioned, so it is dropped rather
 * than half-rendered — the same judgement `day` makes about a row with no date. `llmCalls` is asked
 * for separately and may be absent: a model's call count is a second figure beside the tokens, not a
 * precondition for reading them.
 */
interface ModelRow {
  model: string;
  tokens: MeasuredSum;
  llmCalls?: MeasuredSum;
}

function modelRow(raw: unknown): ModelRow | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const candidate = raw as ModelSpend;
  const tokens = term(candidate.tokens);
  if (typeof candidate.model !== 'string' || candidate.model === '' || tokens === undefined) {
    return undefined;
  }
  return { model: candidate.model, tokens, llmCalls: term(candidate.llmCalls) };
}

/**
 * The folded remainder, when it stands for anything — `undefined` when it does not.
 *
 * THE COUNT DECIDES, never the sum. The producer emits this block on every pass and `models: 0` is
 * its measured "the bound withheld nothing", which is the live reading; the sum beside a zero count is
 * the sentinel rather than a `0`, because there were no values to add. So reading the sum to decide
 * whether the fold happened would report every live bundle as one whose remainder could not be
 * measured.
 *
 * A positive count with an unreadable token term is kept, deliberately. Dropping it would hide a fold
 * that really occurred and leave the named rows reading as the whole roster — which is the one thing
 * this block exists to prevent. It comes through with no term, and the roster then draws no shares.
 */
function modelRemainder(raw: unknown): { models: number; tokens?: MeasuredSum } | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const candidate = raw as ModelSpendRemainder;
  if (!Number.isFinite(candidate.models) || candidate.models <= 0) return undefined;
  return { models: candidate.models, tokens: term(candidate.tokens) };
}

/**
 * `10.0M tokens (97%) in 14 of 25` — one figure, its share of the roster, and the runs it is over.
 *
 * The denominator travels even where the figure does not: `calls not measured in 0 of 25` says the
 * producer looked at twenty-five runs and none of them recorded a call, which the phrase alone does
 * not. Where the wire carried no term at all there is no denominator to state and none is invented.
 *
 * `share` is `undefined` when the roster has no complete denominator, and then NO percentage is
 * printed rather than one taken over the readable part — see `modelSplit`.
 */
function modelFigure(figure: MeasuredSum | undefined, singular: string,
                     format: (n: number) => string,
                     share: ((value: number) => string) | undefined): string {
  // Plural for the absent cases, as `pluralise` gives anything that is not exactly one: there is no
  // count to agree with, and `token not measured` would read as a defect in the sentence.
  const absent = `${pluralise(0, singular)} ${MODEL_UNMEASURED}`;
  if (figure === undefined) return absent;
  const over = `in ${coverage(figure.measuredOf, figure.total, MODEL_RUNS)}`;
  const value = usable(figure.sum);
  if (value === undefined) return `${absent} ${over}`;
  const pct = share === undefined ? '' : ` (${share(value)})`;
  return `${format(value)} ${pluralise(value, singular)}${pct} ${over}`;
}

/**
 * WHICH models the population's tokens went to: a head clause for the footnote, and the whole roster
 * for the channels with room for it. `undefined` when the wire names no model.
 *
 * The one figure in this block a reader can act on with no rate table, and the distribution is nowhere
 * near even — on the live population one of two ids is 97% of the tokens — so `modelsSeen: 2` beside
 * it destroys the fact it exists to report.
 *
 * THE SHARE DENOMINATOR IS THE ROSTER'S OWN SUM, never `spend.tokens.sum`. The two agree today and are
 * separate folds, so a share taken against the larger one would report the roster as more complete
 * than it is — the identical argument `shortfall` makes for taking its fraction over the priced pair's
 * own sum.
 *
 * ALL-OR-NOTHING, the rule `coverageOf` applies to a coverage fraction and `share` to a category
 * split. One unreadable part — a model whose tokens are the sentinel, or a fold whose sum is — leaves
 * the denominator short by an unknown amount, so every percentage over it OVERSTATES, and each one
 * overstates by a different unknown. A share that is wrong in the reassuring direction is worse than
 * no share, so none is drawn and the head says so.
 *
 * The fold is named in the HEAD and not only in the roster, because the head is the clause that is
 * always on screen: a reader who never hovers a tile or opens the picker must still see that the named
 * models are not all of them.
 */
function modelSplit(spend: NonNullable<ImproveData['scorecard']>['spend'] | undefined):
{ head: string; whole: string } | undefined {
  const named = (Array.isArray(spend?.byModel) ? spend.byModel : [])
    .map(modelRow).filter((row): row is ModelRow => row !== undefined);
  if (named.length === 0) return undefined;
  const folded = modelRemainder(spend?.byModelOther);

  const parts = [
    ...named.map((row) => usable(row.tokens.sum)),
    ...(folded === undefined ? [] : [folded.tokens && usable(folded.tokens.sum)]),
  ];
  const roster = parts.some((part) => part === undefined)
    ? undefined : (parts as number[]).reduce((a, b) => a + b, 0);
  // A roster adding to nothing has no share either, for `shareOf`'s reason: `0/0` is not `0%`, and a
  // percentage drawn from it would assert a split nothing measured.
  const share = roster === undefined || roster <= 0
    ? undefined : (value: number) => `${Math.round((value / roster) * 100)}%`;

  // `id: figures`, the ordinary punctuation for a labelled row. The lead-in below ends in a full
  // stop for that reason, so the list's own colon does not land in front of every row's.
  const rows = named.map((row) => `${row.model}: `
    + `${modelFigure(row.tokens, 'token', compactNumber, share)}, `
    // No share on the call count. A share is over the roster's TOKENS, and a percentage beside a
    // second unit would read as that unit's share of something nothing here totals.
    + `${modelFigure(row.llmCalls, 'call', String, undefined)}`);
  if (folded !== undefined) {
    rows.push(`${folded.models} further ${pluralise(folded.models, 'model')} folded together: `
      + modelFigure(folded.tokens, 'token', compactNumber, share));
  }

  // The rows are ranked by measured tokens with ties broken by id (`dto._by_model_spend`), so the
  // first is the biggest spender. Read rather than re-sorted: sorting here would be a second ranking
  // rule, and one that could not order a row whose tokens are the sentinel.
  const biggest = named[0];
  const spent = usable(biggest.tokens.sum);
  const lead = share === undefined || spent === undefined
    ? NO_MODEL_SHARE
    : `${biggest.model} is ${share(spent)} of ${compactNumber(roster as number)} tokens`;
  const fold = folded === undefined
    ? ''
    : ` · ${folded.models} more ${pluralise(folded.models, 'model')} folded in`;
  return {
    head: `${lead}${fold}`,
    whole: `Tokens by model, biggest first. ${rows.join('; ')}. ${MODEL_DENOMINATOR}`
      + (share === undefined
        ? ' No share is drawn: a roster missing one part of its own total has a denominator short by '
          + 'an unknown amount.'
        : ''),
  };
}

/**
 * How many distinct models everyone's sessions spent on, and which of them the tokens went to.
 *
 * A plain count, NOT a `MeasuredSum`: it has no `sum`/`measuredOf` of its own, so `0` from it means
 * zero distinct models were seen rather than that nothing was measured. Which is why the footnote
 * comes from the token term — models are collected from the same per-session usage records, so
 * `0 models over 0 of 6 sessions` says what a bare `0` cannot.
 *
 * The SPLIT is what makes the count actionable, and it is offered at this scope alone. The per-day
 * rows a personal figure is summed from carry a roster of ids (`DaySpend.models`) and no per-model
 * magnitude, so there is nothing to split there — `myModels` counts the set and stops, and inventing
 * a share for it would be arithmetic over numbers the wire does not send.
 */
function populationModels(data: ImproveData): ScalarReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const spend = data.scorecard.spend;
  const seen = spend?.modelsSeen;
  const tokens = term(spend?.tokens);
  if (typeof seen !== 'number' || !Number.isFinite(seen)) {
    return notInBundle('a count of models seen');
  }
  // Without the token term there is no population to caption the count against, and an uncaptioned
  // `5` cannot be told from 5 models over the whole harness.
  if (tokens === undefined) return notInBundle('a token total to count models against');
  // Absent, not empty: a bundle predating `byModel` keeps the one-clause footnote it has always had
  // rather than gaining a clause claiming the producer named no model.
  const split = modelSplit(spend);
  const over = `over ${coverage(tokens.measuredOf, tokens.total, SESSIONS)}`;
  // The models come off the same usage records the row cap dropped sessions from, so this count can be
  // an UNDERCOUNT — which `discardedModels` states in sessions rather than as a token magnitude, since
  // a set has no shortfall to add back. See there.
  return withClause({
    available: true,
    value: seen,
    sub: split === undefined ? over : `${split.head} · ${over}`,
    caveat: split?.whole,
  }, discardedModels(spend?.usageShortfall));
}

/**
 * Everyone's average session, and exact rather than estimated: a run IS a session, so this divides
 * the measured sessions' tokens by exactly the sessions that contributed them.
 *
 * EXACT over the sessions it was given, and the discard clause is what says how few those are. Both
 * halves of the quotient come off the same usage records — the numerator is `spend.tokens` and the
 * denominator its own `measuredOf` — so a session the row cap dropped is absent from both and the
 * result is a per-session figure for a fraction of the harness. That the arithmetic is exact is
 * exactly why the clause is needed: nothing else about this figure looks partial.
 */
function populationTokensPerSession(data: ImproveData): ScalarReading | Unavailable {
  if (data.scorecard === undefined) return NO_SCORECARD;
  const tokens = term(data.scorecard.spend?.tokens);
  if (tokens === undefined) return notInBundle('a token total');
  const clause = discarded(data.scorecard.spend?.usageShortfall);
  const { measuredOf, total, sum } = tokens;
  /*
   * The denominator is checked before anything divides, and this branch is live: a real emitted
   * bundle carries `measuredOf: 0` for tokens. There is no per-session figure over zero sessions, and
   * `NaN`, `Infinity` and `0` would each read as one.
   */
  if (!Number.isFinite(measuredOf) || measuredOf <= 0) {
    return noDenominator('No session recorded a token count '
      + `(${fractionLabel(measuredOf, total)}), so there is no per-session figure.`);
  }
  const sub = `over ${coverage(measuredOf, total, SESSIONS)}`;
  const number = usable(sum);
  // A denominator with no numerator: `measuredOf` says sessions were measured while the sum says
  // nothing was. Contradictory, so the sentinel is shown rather than a quotient.
  if (number === undefined) return withClause({ available: true, value: measured(sum), sub }, clause);
  // Whole tokens, because it is a token count.
  return withClause({ available: true, value: Math.round(number / measuredOf), sub }, clause);
}

/**
 * The reader's own bill, summed over their days, and the two things that can qualify it.
 *
 * Which of the two it gets turns on whether there is a FIGURE, and that is the one rule here that is
 * not shared with the population reading.
 */
function myCost(data: ImproveData, view: MetricView): ScalarReading | Unavailable {
  // `'UNMEASURED'` and not `0` for a period with no days in it: `$0.00` reads as a session that ran
  // and was free, which is a claim nobody priced.
  const reading = cumulativeSum(data, view, TERM.cost, 'UNMEASURED');
  if (!reading.available) return reading;
  const basis = data.scorecard?.spend?.costBasis;
  if (usable(reading.value) === undefined) {
    /*
     * No figure, so there is nothing to mis-attribute and the producer's own account of its rates is
     * the only thing on the wire that explains the absence. The head of it goes in the footnote and
     * the whole of it rides the caveat, exactly as at the shared scope.
     */
    return { ...reading, sub: `${reading.sub} · ${costCaveat(basis)}`, caveat: wholeCaveat(basis) };
  }
  /*
   * A figure, and therefore NO basis line. `costBasis` rides the SHARED bundle while these days ride
   * the reader's own, and the two refresh independently — an actor's bundle is rewritten only when
   * they had a session in the harvest window — so the shared sentence can describe a later pricing
   * run than the one that priced these days. Provenance that might be about something else is worse
   * than none.
   *
   * The shortfall is different in kind: a count taken off these very rows rather than a claim about
   * where a rate came from.
   */
  return withClause(reading, cumulativeShortfall(data, view));
}

/**
 * How many distinct models the reader's own days spent on.
 *
 * A COUNT over the whole period and never a sum of per-day counts: both of the reader's days can name
 * both models, and adding the rosters' lengths would report four. Which is also why the wire carries
 * the ids and not a number — a set cannot be recovered from per-day counts.
 *
 * A count and not the names, even though the names are what arrived. A tile sets one figure and about
 * a line of footnote; these ids run to 33 characters, and shortening them to fit would invent an id
 * the wire does not carry.
 *
 * Absence is the SENTINEL here, where the population's `modelsSeen: 0` is a real count. The two are
 * different facts about different wires: `modelsSeen` is computed over the measured sessions, while a
 * day that names no model is a day nothing measured — so `0 models` from these rows would claim the
 * reader's runs used none.
 */
function myModels(data: ImproveData, view: MetricView): ScalarReading | Unavailable {
  const found = cumulate(data, view.period);
  if (!('rows' in found)) return found;
  const periods = buckets(found);
  const carried = periods.filter((period) => period.models !== undefined);
  if (periods.length > 0 && carried.length === 0) {
    return notOnTheseDays('record of which models they used', view.scope);
  }
  if (periods.length === 0) return { available: true, value: 0, sub: NO_ACTIVITY };

  const named = new Set<string>();
  let contributed = 0;
  for (const period of carried) {
    const roster = period.models;
    // A sentinel is a string and an absent term is `undefined`; neither is a roster, and neither may
    // be read as an empty one. An empty list is not a roster either — it would say the period's runs
    // used no model, where the producer sends the sentinel for a period that named none.
    if (!Array.isArray(roster)) continue;
    const ids = roster.filter((id): id is string => typeof id === 'string' && id !== '');
    if (ids.length === 0) continue;
    contributed += 1;
    for (const id of ids) named.add(id);
  }
  /*
   * Captioned by the TOKEN term's coverage, because a roster has none of its own and needs none: the
   * ids are collected from the same per-session usage record the token counts come off, so the sessions
   * that recorded tokens are exactly the sessions that could name a model. The shared model count is
   * captioned off the same term for the same reason.
   */
  const sub = cumulativeSub(runsIn(periods), coverageOf(periods, TERM.tokens.covered));
  return contributed === 0
    ? { available: true, value: 'UNMEASURED', sub }
    : { available: true, value: named.size, sub };
}

/**
 * The reader's own average session: their measured tokens over the SESSIONS that recorded them.
 *
 * The divisor is `coverage`'s own numerator, which makes this the same arithmetic the shared figure
 * does one population up — `sum / measuredOf`, exact across the measured sessions — so one label
 * cannot mean two things at the two scopes.
 *
 * It is NOT `runs`, which is what this read used to divide by. A day row counts REQUESTS there and
 * SESSIONS in `coverage.total`, so on a day reading `runs: 17, coverage.total: 19` the old divisor
 * overstated the average by 12% while the footnote beside it named 19 sessions. And it is not
 * `coverage.total` either: a session that recorded no token count spent an unknown amount, not
 * nothing, so dividing a partial sum by every session held would understate it by asserting zeroes
 * nobody measured.
 *
 * ALL-OR-NOTHING, as `coverageOf` is: a period carrying no `coverage` block cannot say how many
 * sessions it held, and a divisor summed over the rest would put the fold's whole numerator over part
 * of its denominator.
 *
 * A period with no days is the one case an average cannot answer with `0` the way a total can. A
 * reader who ran nothing spent no tokens, which is a real zero; `0 tokens per session` claims a
 * session that used none.
 */
function myTokensPerSession(data: ImproveData, view: MetricView): ScalarReading | Unavailable {
  const found = cumulate(data, view.period);
  if (!('rows' in found)) return found;
  const periods = buckets(found);
  const carried = periods.filter((period) => period.tokens !== undefined);
  if (periods.length > 0 && carried.length === 0) {
    return notOnTheseDays(TERM.tokens.what, view.scope);
  }
  if (periods.length === 0) {
    return noDenominator(`${NO_ACTIVITY}, so there is no per-session figure. A period with no runs `
      + 'in it spent no tokens, but nothing divided by no sessions is not 0 tokens a session.');
  }

  const covered = coverageOf(periods, TERM.tokens.covered);
  const sub = cumulativeSub(runsIn(periods), covered);
  if (covered === undefined) {
    return noDenominator('Your daily rows do not say how many sessions recorded these tokens, so '
      + 'there is no per-session figure to divide them by. A row is written once and carried '
      + 'forward, so rows from before the producer counted sessions keep the shorter shape.');
  }
  // The divisor is checked before anything divides, and this branch is reachable: a day whose sessions
  // all recorded nothing counts `0` against its real session count, and `NaN` would read as a figure.
  if (!Number.isFinite(covered.measuredOf) || covered.measuredOf <= 0) {
    return noDenominator('No session of yours recorded a token count '
      + `(${fractionLabel(covered.measuredOf, covered.total)}), so there is no per-session figure.`);
  }

  let tokens = 0;
  let contributed = 0;
  for (const period of carried) {
    const spent = usable(period.tokens ?? 'UNMEASURED');
    if (spent === undefined) continue;
    tokens += spent;
    contributed += 1;
  }
  // A denominator with no numerator: sessions were counted as recording tokens while no period states
  // a sum. Contradictory, so the sentinel is shown rather than a quotient — the same reading the
  // population's own per-session figure gives.
  if (contributed === 0) return { available: true, value: 'UNMEASURED', sub };
  // Whole tokens, because it is a token count.
  return { available: true, value: Math.round(tokens / covered.measuredOf), sub };
}

/**
 * The list. Order is the order a picker offers them in.
 *
 * EIGHT rows for eight figures. Whose runs a figure is about used to be baked into its identity —
 * `yourTokens` beside `tokens` — so one question took two rows, the two had to be kept saying the
 * same thing by hand, and adding a period the same way would have made it six. Scope is a read-time
 * parameter instead: each entry answers its own question of the reader's own days and of every
 * session on the harness, and there is no longer a per-family list for a question to be missing from.
 *
 * What that collapse pins, beyond the row count. One formatter per figure, so a price is a price and
 * a duration is a duration whoever's runs it came off. One noun per figure, so a reader cannot find a
 * figure called one thing in the picker and another on the tile. And one pair of side names per
 * split, so `context` cannot come to mean different things at the two scopes.
 *
 * What each read does NOT do is decide whether the view is answerable at all. That is one fact about
 * the wire rather than about any figure — `periodRefusal` — and asking it here would be writing it
 * eight times and printing one cause eight times.
 */
export const METRICS = [
  {
    kind: 'scalar',
    id: 'tokens',
    noun: 'tokens',
    format: compactNumber,
    read: (data, view) => (view.scope === 'yours'
      ? cumulativeSum(data, view, TERM.tokens)
      : populationTokens(data)),
  },
  {
    kind: 'scalar',
    id: 'llmCalls',
    /* `calls` and not `LLM calls`: the noun follows an owner word, and `Your LLM calls` is the one
     * label on the surface that would carry a second capital. The population figure it now shares an
     * entry with used to spell it the longer way. */
    noun: 'calls',
    format: compactNumber,
    read: (data, view) => (view.scope === 'yours'
      ? cumulativeSum(data, view, TERM.llmCalls)
      : populationCalls(data)),
  },
  {
    kind: 'ratio',
    id: 'contextVsGenerated',
    noun: 'context vs generated',
    format: compactNumber,
    sides: { part: 'context', rest: 'generated' },
    read: (data, view) => (view.scope === 'yours'
      ? cumulativeShare(data, view, CONTEXT, GENERATED)
      : share(data, CONTEXT, GENERATED, SPLIT)),
  },
  {
    kind: 'scalar',
    id: 'cost',
    noun: 'cost',
    format: formatUsd,
    read: (data, view) => (view.scope === 'yours' ? myCost(data, view) : populationCost(data)),
  },
  {
    kind: 'ratio',
    id: 'cacheReuse',
    noun: 'cache reuse',
    format: compactNumber,
    sides: { part: 'cache reads', rest: 'cache writes' },
    read: (data, view) => (view.scope === 'yours'
      ? cumulativeShare(data, view, CACHE_READS, CACHE_WRITES)
      : share(data, CACHE_READS, CACHE_WRITES, SPLIT)),
  },
  {
    kind: 'scalar',
    id: 'totalTime',
    noun: 'time',
    /*
     * Busy time, not wall clock: this sums each run's tool durations, and runs whose nodes worked in
     * parallel sum to more than their own window.
     *
     * Days and hours rather than `formatMs`, whose largest unit is the minute — it sets the harvest's
     * total as `6875m 25s` and the reader's own two days as `151m 44s`, real figures that nobody sizes
     * at a glance. The formatter earns its place at both scopes for the same reason, one population
     * apart.
     */
    format: formatExtent,
    read: (data, view) => (view.scope === 'yours'
      ? cumulativeSum(data, view, TERM.latencyMs)
      : populationSum(data, (spend) => spend?.latencyMs, 'a latency total')),
  },
  {
    kind: 'scalar',
    id: 'modelsUsed',
    noun: 'models',
    format: compactNumber,
    read: (data, view) => (view.scope === 'yours' ? myModels(data, view) : populationModels(data)),
  },
  {
    kind: 'scalar',
    id: 'tokensPerSession',
    noun: 'tokens per session',
    format: compactNumber,
    read: (data, view) => (view.scope === 'yours'
      ? myTokensPerSession(data, view)
      : populationTokensPerSession(data)),
  },
] as const satisfies readonly MetricEntry[];

/** The ids a picker can persist. A literal union, so a stored id that no longer exists is a
 *  compile error at the reader rather than a blank tile. */
export type MetricId = (typeof METRICS)[number]['id'];

// --- which views the wire can answer ----------------------------------------
/**
 * Why this scope cannot be shown over this period, or `null` when it can.
 *
 * Asked ONCE per view rather than inside each read, which is what keeps the refusal from being
 * written eight times and from drifting between them. It is a property of the wire and the view, not
 * of any figure — every metric would report the same sentence, and a block that asked each of them
 * would print one cause eight times.
 *
 * Only the shared scope is ever refused, and only because of what the population wire carries: see
 * `NO_PERIOD`. The personal scope is answerable at every period by construction, since a period there
 * is a subset of the same day rows.
 *
 * A hard predicate rather than a sniff at the payload, deliberately. What is missing is a coverage
 * count on the shared rows the wire actually serves, and a UI that switched itself on when it spotted
 * one such row would total a period across rows that carry none and caption it as if they all did. So
 * this changes when the shared rows change, in one place, and until then it says so.
 */
export function periodRefusal(view: MetricView): Unavailable | null {
  return view.scope === 'everyone' ? NO_PERIOD : null;
}

/**
 * How much of the harness the SHARED figures are drawn from — or `null` when the wire will not say.
 *
 * Every tile at this scope carries its own coverage fraction, and every one of those fractions is over
 * the population the producer ADMITTED. Three bounds narrow the harvest to that population and none of
 * them is visible on a tile: the span floor drops a session too short to judge, the population cap
 * takes the newest of what is left, and the harvest's own row cap discards sessions whose usage was
 * already measured. So `25 of 25 sessions` is true and complete about a quarter of a day.
 *
 * SESSIONS throughout — one unit for all three — so this is a single statement about how much of the
 * harness the numbers cover rather than three facts in two units. The token magnitude the row cap cost
 * rides the figures it is missing FROM (`discarded`), where it can be read against them.
 *
 * `null` rather than a reassuring sentence when neither block is on the wire, which is every bundle
 * served today: this states what the producer measured and never invents a denominator for it. A
 * measured `0` on either exclusion is silent for the reason `shortfall` is — a clause under every
 * reading teaches a reader to skip the one that matters — and `belowMinSpans` says `more` because those
 * sessions are outside `discoverable`, not inside it.
 */
function populationBasis(data: ImproveData): string | null {
  const clauses: string[] = [];
  const sessions: DiscoveredSessions | undefined = data.scorecard?.population?.sessions;
  const admitted = usable(sessions?.admitted ?? 'UNMEASURED');
  const discoverable = usable(sessions?.discoverable ?? 'UNMEASURED');
  if (admitted !== undefined && discoverable !== undefined && discoverable > 0) {
    clauses.push(`${coverage(admitted, discoverable, SESSIONS)} scored`);
  }
  const floor = usable(sessions?.belowMinSpans ?? 'UNMEASURED');
  if (floor !== undefined && floor > 0) clauses.push(`${floor} more too short to judge`);

  const shortfall = data.scorecard?.spend?.usageShortfall;
  const dropped = usable(shortfall?.sessionsDiscarded ?? 'UNMEASURED');
  const measured = usable(shortfall?.sessionsMeasured ?? 'UNMEASURED');
  if (dropped !== undefined && measured !== undefined && dropped > 0) {
    clauses.push(`spend dropped from ${coverage(dropped, measured, SESSIONS)}`);
  }
  return clauses.length === 0 ? null : clauses.join(' · ');
}

/**
 * What the figures on screen are a total OF, in the reader's words — or `null` when there is nothing
 * a reader needs told.
 *
 * Not decoration at the personal scope. A cumulative total with no basis beside it cannot be read at
 * all: `5.7M` over a fortnight and over a year are different facts and the tile cannot tell them
 * apart, and "all time" over rows that reach back two weeks is not all time. So the span the numbers
 * actually cover is stated, from the same rows the numbers came from — `cumulate` decides both, so
 * the label and the figures cannot describe different spans.
 *
 * The bundle's own `createdAt` rides it for a second reason, and a sharper one. A quiet actor's
 * bundle is not rewritten and not deleted: the producer skips the write and the previous item
 * persists, so a stale total reads exactly like a current one and this date is the only thing on the
 * wire that says otherwise. Absent when the item carried no stamp, and then it is simply not claimed.
 *
 * `null` at the personal scope too, in the one state where the cause standing in for the figures is
 * itself a phrase (see `Unavailable.brief`). Then that phrase is the short form and this line would be
 * a second one, on a page whose complaint was hearing the same absence four times.
 *
 * At the shared scope it is not a SPAN, and the asymmetry is the point rather than an omission. A
 * shared figure is not a cumulative total over a span the reader has to be told — every one of them
 * comes off the scorecard's single window whichever period is pressed — and the denominator a reader
 * needs is on each tile, against the figure it qualifies rather than once for the row. What is left is
 * the one fact no tile can carry: how much of the harness those denominators are themselves a sample
 * of. See `populationBasis`, which is `null` when the wire states none of it.
 */
export function describeBasis(data: ImproveData, view: MetricView): string | null {
  if (view.scope === 'everyone') return populationBasis(data);

  const found = cumulate(data, view.period);
  if (!('rows' in found)) {
    /*
     * The reason the tiles are showing is the whole answer here, and it is already on screen once.
     * Repeating a sentence of it would be the same cause said twice.
     *
     * And where that cause carries a phrase, this line says nothing at all: the phrase IS the short
     * form, printed in place of the figures where a reader is looking for them, so a second short
     * form beside the switches would be the one duplicate the rule above exists to prevent. Nothing
     * is lost with it — a bundle absent entirely leaves no personal figure readable, so there is no
     * span this line could be qualifying.
     */
    if (found.brief !== undefined) return null;
    return 'no personal history in this data';
  }

  const stamped = typeof data.actor?.createdAt === 'string'
    ? formatIsoDay(data.actor.createdAt.slice(0, 10)) : null;
  const asOf = stamped === null ? '' : ` · recorded ${stamped}`;
  if (found.rows.length === 0 && found.rolledUp === undefined) {
    return `${PERIOD_LABEL[view.period].toLowerCase()}, no day of activity recorded${asOf}`;
  }

  const since = found.firstDate === undefined ? null : formatIsoDay(found.firstDate);
  const to = found.lastDate === undefined ? null : formatIsoDay(found.lastDate);
  if (view.period === 'allTime') {
    // `since` and not a range: a cumulative total's open end is now, and naming a closing date
    // would invite it to be read as a window that has already ended.
    return since === null ? `all time${asOf}` : `since ${since}${asOf}`;
  }
  /*
   * The trailing windows name the day they are anchored to, which the button beside them cannot. The
   * anchor is the reader's latest recorded day rather than today, so "7 days" on a reader who last
   * ran a month ago covers the week ending then — true, and unreadable without the date.
   */
  const span = PERIOD_LABEL[view.period].toLowerCase();
  return to === null ? `${span}${asOf}` : `${span} to ${to}${asOf}`;
}
