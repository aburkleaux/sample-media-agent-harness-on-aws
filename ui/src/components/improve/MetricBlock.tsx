/**
 * Three summary figures, two switches saying what they are figures OF, and a picker to swap which
 * three.
 *
 * The block owns WHICH metrics are on screen, whose runs they cover, how far back they reach, that
 * there are never more than three, and that all of it survives a reload. It computes no figure and
 * formats none — `metricRegistry` reads the bundle, `MetricTile` draws a reading, and the formatter
 * comes off the entry. So a new metric is a new registry entry and no change here.
 *
 * Three, hard, rather than three-then-more: eight tiles is a dashboard, and a dashboard is what
 * this design is a reaction to. A summary is something a reader takes in at once.
 *
 * **The switches are on the header, not in the picker**, and that is the one layout decision here
 * worth defending. A figure whose basis is hidden behind a gear cannot be read at all: `5.7M` over a
 * fortnight and over a year are different facts and the tile cannot tell them apart. Period is also a
 * comparison action — a reader wants it one click from the numbers, not two clicks and a menu. And a
 * picker-only switch would still need the basis stated on the header, so it would cost a control and
 * keep the label. The gear keeps only the eight figures, and STATES the basis rather than setting it.
 *
 * It inherits the surface's rule — **absent data must never look like measured data** — and adds one
 * case of its own, because a picker puts three outcomes on screen where the tile only ever sees two.
 * A value of `UNMEASURED` IS a reading (the producer ran and could not measure) and keeps
 * `MetricTile`'s hatched sentinel. An `Unavailable` read is no reading at all (nothing in the bundle
 * to read) and gets `improveChrome`'s neutral `UnavailableBadge`, which is defined there so both
 * absences are spelled the same way everywhere. Collapsing the two would claim a figure was attempted
 * when nothing ever tried. A measured `0` is the third and is set as the number it is.
 *
 * Colour comes only from `theme.colors`, and neutral furniture grounds on `surfaceSubtle` —
 * nothing here is a status, so nothing here tints. No heading and no card of its own: the pane
 * that mounts it owns its framing.
 */
import React from 'react';
import { theme } from '../../styles/tokens';
import type { ImproveData } from '../../improve-data';
import { MetricTile } from './MetricTile';
import { UnavailableBadge, abridge, metaTextStyle } from './improveChrome';
import {
  METRICS, PERIODS, PERIOD_LABEL, SCOPES, SCOPE_LABEL,
  describeBasis, isStructural, metricLabel, periodRefusal,
  type MetricEntry, type MetricId, type MetricPeriod, type MetricScope, type MetricView,
  type RatioReading, type ScalarReading, type Unavailable,
} from './metricRegistry';

/** Whatever a registry entry's `read` can answer. */
type Reading = ScalarReading | RatioReading | Unavailable;

/**
 * The registry under its DECLARED type rather than its inferred one.
 *
 * `METRICS` is `as const`, so its element type is a union of one literal shape per entry rather
 * than one `MetricEntry`. Widening once here gives the row below a single shape to map over: every
 * entry carries a `format` today, but the field is OPTIONAL on the type, and a member that dropped
 * it would otherwise be a type error at this one call site instead of the `undefined` the tile
 * already accepts.
 */
const ALL_METRICS: readonly MetricEntry[] = METRICS;

/** How many tiles are on screen. See this file's opening note for why it does not grow. */
const VISIBLE = 3;

/**
 * What the block shows before anyone chooses: what the reader spent, how many calls it took, and
 * what it cost.
 *
 * Three that answer different questions — a token total, a call count and a price, not three views
 * of the same spend. Read at `DEFAULT_VIEW` they are the reader's OWN, because that is the question
 * someone opening this pane is asking and the shared scorecard cannot answer it: it aggregates every
 * tenant, so no figure in it is about them.
 *
 * These used to collapse. The personal figures were summed off one harvest window, so on a day the
 * reader had no session all three reported the same absence and the pane was one sentence — which on
 * a shared harness is most readers most days. They are cumulative now, so a reader who has ever run
 * anything has three numbers, and a reader who has run nothing has three zeros off a bundle that
 * positively says so.
 */
export const DEFAULT_METRIC_IDS: readonly MetricId[] = ['tokens', 'llmCalls', 'cost'];

/**
 * What the switches show before anyone touches them: the reader's own runs, all time.
 *
 * Cumulative by default rather than a trailing window, because the trailing windows are the
 * comparison and the total is the thing being compared — and because a total is the one reading that
 * cannot be empty for a reader who has ever run anything, which is the defect these switches were
 * added around.
 */
export const DEFAULT_VIEW: MetricView = { scope: 'yours', period: 'allTime' };

/**
 * `lab-` prefixed, matching every other stored preference (`lab-theme`, `lab-font-size`).
 *
 * ONE key for all three preferences, widened from the bare id array it used to hold, and the shape
 * is a record with three OPTIONAL fields for a reason that is not tidiness. Each field is written
 * only when its own control changed it, so the ABSENCE of a field means "never chose it" and its
 * presence means "chose" — which is what makes changing `DEFAULT_METRIC_IDS` or `DEFAULT_VIEW` safe
 * with no migration. A reader who never picked gets the new default on their next load; one who
 * picked keeps what they picked. Store all three together on any change and flipping the scope switch
 * would silently freeze that reader's metric list at whatever the default was that day.
 *
 * And it is why a migration would be the wrong answer rather than a missing one. The only migration
 * available would rewrite a stored value that matches the OLD default — but a reader who deliberately
 * chose those same three ids stores exactly that, so nothing distinguishes them and the rewrite would
 * silently discard a choice someone made.
 */
const STORAGE_KEY = 'lab-improve-metrics';

// --- the stored choice ------------------------------------------------------
function isMetricId(value: unknown): value is MetricId {
  return typeof value === 'string' && METRICS.some((entry) => entry.id === value);
}

function isScope(value: unknown): value is MetricScope {
  return SCOPES.some((scope) => scope === value);
}

function isPeriod(value: unknown): value is MetricPeriod {
  return PERIODS.some((period) => period === value);
}

/**
 * Bring a selection up to the cap: the default set first, then registry order.
 *
 * The REMAINDER, rather than discarding the whole stored list — retiring one metric must not throw
 * away the two choices beside it that are still good. Registry order behind the defaults means this
 * cannot run short if the cap is ever raised above the default set's size.
 *
 * It is also what made collapsing sixteen entries into eight need no migration of its own. Eight ids
 * went away with the personal family; a stored selection holding one of them drops it here and is
 * topped up, so the worst a reader who had picked `yourTokens` sees is the default in its place.
 */
function topUp(kept: readonly MetricId[]): MetricId[] {
  const filled = [...kept];
  for (const id of [...DEFAULT_METRIC_IDS, ...METRICS.map((entry) => entry.id)]) {
    if (filled.length === VISIBLE) break;
    if (!filled.includes(id)) filled.push(id);
  }
  return filled;
}

/**
 * What is on the wire to `localStorage`. Every field optional — see `STORAGE_KEY`.
 *
 * Kept as the stored shape rather than resolved-with-defaults, because the difference between
 * "absent" and "the default value" is exactly what must survive a round trip.
 */
interface StoredChoice {
  metrics?: MetricId[];
  scope?: MetricScope;
  period?: MetricPeriod;
}

/**
 * The stored preferences, or none of them.
 *
 * Stored state is untrusted input, so every hostile shape has an answer here rather than a
 * `JSON.parse` and a hope: absent, unparseable or a scalar yields nothing; a retired metric id, a
 * non-string or a repeat is dropped, since a stale id would paint a tile with no label and no figure;
 * a longer list is cut to the cap, which is the block's rule and not a stored preference's to raise;
 * and a scope or period that is not one of the ones offered is ignored rather than rendered.
 *
 * A BARE ARRAY is the shape this key held before the switches existed, and it is read as the metric
 * list with no scope or period — which is the correct reading of it, since that reader never chose
 * either. That is the whole of the migration.
 *
 * Reads are wrapped as well as writes: `localStorage` throws in Safari private mode and under quota,
 * and a pane that dies because a preference could not be read is worse than one that ignores it.
 */
function readChoice(): StoredChoice {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    return {};
  }
  if (stored === null) return {};

  let parsed: unknown;
  try {
    parsed = JSON.parse(stored);
  } catch {
    return {};
  }
  if (Array.isArray(parsed)) return { metrics: readMetrics(parsed) };
  if (!parsed || typeof parsed !== 'object') return {};

  const record = parsed as Record<string, unknown>;
  const choice: StoredChoice = {};
  if (Array.isArray(record.metrics)) choice.metrics = readMetrics(record.metrics);
  if (isScope(record.scope)) choice.scope = record.scope;
  if (isPeriod(record.period)) choice.period = record.period;
  return choice;
}

/** The live, distinct, capped ids in a stored list. Shared by the record and the bare-array shape,
 *  so the legacy read cannot come to admit an id the current one rejects. */
function readMetrics(candidates: readonly unknown[]): MetricId[] {
  const kept: MetricId[] = [];
  for (const candidate of candidates) {
    if (kept.length === VISIBLE) break;
    if (!isMetricId(candidate) || kept.includes(candidate)) continue;
    kept.push(candidate);
  }
  return kept;
}

function writeChoice(choice: StoredChoice): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(choice));
  } catch {
    /* ignore — the choice still applies for this session */
  }
}

// --- reasons ---------------------------------------------------------------
/**
 * The most of a reason that fits under a figure without setting the row's height: about two lines of
 * meta text across a third of the row. The registry surfaces the producer's own sentence VERBATIM
 * and one of them (why cost is unpriced) runs to some five hundred characters, which is eight lines
 * here and pushes the measured tiles beside it down the pane. Nothing is lost, only deferred — the
 * whole sentence is on the tile's tooltip and printed unabridged in the picker.
 *
 * The cut itself is `improveChrome`'s `abridge`, not a copy of it here: this file and the registry
 * both preview a producer sentence under a figure, at different lengths and by identical mechanics,
 * and the surface keeps one spelling per idea.
 */
const REASON_PREVIEW = 120;

/**
 * Whether a reading carries a share. A predicate rather than a bare `'ratio' in reading`, because
 * `RatioReading` extends `ScalarReading` and an `in` check against the union leaves the property
 * typed `unknown` — the share would reach the bar unchecked.
 */
function carriesRatio(reading: ScalarReading | RatioReading): reading is RatioReading {
  return 'ratio' in reading;
}

/**
 * What a split's two sides are called, or `null` when the label already says it. `Your cache reuse`
 * gains a lot from `cache reads vs cache writes`; `Your context vs generated` gains a stutter.
 *
 * Matched on the label's END rather than on the whole of it, because every label carries a scope word
 * in front of the question it asks: an equality check would find `everyone's context vs generated`
 * different from `context vs generated` and print the hint anyway, producing exactly the stutter this
 * exists to prevent.
 */
function sidesHint(label: string, sides: { part: string; rest: string }): string | null {
  const hint = `${sides.part} vs ${sides.rest}`;
  return label.toLowerCase().endsWith(hint.toLowerCase()) ? null : hint;
}

/** `Tokens, LLM calls and Cost`. Prose, because the collapsed line is a sentence. */
function sentenceList(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/**
 * The one cause behind every visible metric being unavailable, when there is one. `null` unless
 * EVERY reading is unavailable, all carry the SAME code, and that code is structural — any
 * available reading, `UNMEASURED` included, is a figure on screen and nothing to collapse.
 */
function sharedStructuralCause(readings: readonly Reading[]): Unavailable | null {
  const first = readings[0];
  if (first === undefined || first.available || !isStructural(first.code)) return null;
  return readings.every((r) => !r.available && r.code === first.code) ? first : null;
}

// --- pieces ----------------------------------------------------------------
/**
 * The slot a metric with no reading at all gets — see this file's opening note for why it is not
 * `MetricTile` with a sentinel value. The badge is the surface's, so it cannot drift into a second
 * look for the same fact; what is local is the tile geometry it has to line up with, and the
 * reason, which only a registry read can supply.
 *
 * The tooltip is on the whole slot rather than on the badge: the sentence is what a reader hovering
 * anywhere on a figure-less tile is asking for, and the badge is the part of it they are most
 * likely to be pointing at.
 */
function UnavailableTile({ label, reason }: { label: string; reason: string }) {
  return (
    // `minWidth: 0` for the same reason `MetricTile` sets it: a long reason wraps inside its own
    // tile instead of widening the row.
    <div data-testid="metric-unavailable" title={reason} style={{ minWidth: 0 }}>
      <div data-testid="metric-unavailable-label" style={tileLabelStyle}>{label}</div>
      <div>
        <UnavailableBadge />
      </div>
      <div data-testid="metric-unavailable-reason" style={tileFootnoteStyle}>
        {abridge(reason, REASON_PREVIEW)}
      </div>
    </div>
  );
}

/**
 * One cause, said once, in place of tiles that would each repeat it.
 *
 * TWO renders, and which one a cause gets is the cause's own decision rather than this file's — a
 * `brief` on the reading is what asks for the short one, and `metricRegistry` explains what earns it.
 * The long render names the figures and then states the producer's account of why they are missing.
 * The short one is the phrase alone, with that account hung on the line as its tooltip.
 *
 * The named labels go with the short render deliberately, and that is the piece that stops rendering.
 * A cause with a phrase is a fact about the whole bundle — no figure of the reader's own is readable,
 * whichever three are selected — so listing them says a second time what "your own figures" already
 * said, in three times the words. Which figures they are is still on every picker row. The labels
 * clause is what the long render keeps, because a cause can be structural and still leave a reader
 * needing to know which of eight this row happens to be showing.
 */
function CollapsedCause({ labels, cause }: { labels: readonly string[]; cause: Unavailable }) {
  if (cause.brief !== undefined) {
    /*
     * `title` and not a second line: the whole reason is four sentences, and putting them under the
     * phrase would rebuild the paragraph the phrase replaced. The keyboard-reachable copy is the
     * picker's own row, which prints every unavailable reading's reason unabridged already.
     */
    return (
      <div data-testid="metric-block-collapsed" style={metaTextStyle} title={cause.reason}>
        {cause.brief}
      </div>
    );
  }
  return (
    <div data-testid="metric-block-collapsed" style={metaTextStyle}>
      <span>
        {`${sentenceList(labels)} ${labels.length === 1 ? 'is' : 'are'} unavailable in this data.`}
      </span>{' '}
      <span style={{ color: theme.colors.textTertiary }}>{cause.reason}</span>
    </div>
  );
}

function GearIcon() {
  return (
    <svg
      width="14" height="14" viewBox="0 0 16 16" fill="none"
      stroke="currentColor" strokeWidth="1.5" aria-hidden="true" focusable="false"
    >
      <circle cx="8" cy="8" r="2" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M13 3l-1.5 1.5M4.5 11.5L3 13" />
    </svg>
  );
}

/**
 * One of the two header switches: a short row of options, one of them current.
 *
 * `aria-pressed` toggle buttons inside a plain `role="group"`, and NOT a `radiogroup`. A radio group
 * promises a keyboard contract — roving focus, arrow keys, one tab stop — that this file does not
 * implement, and the picker beside it already declines `role="menu"` on the same grounds. Every
 * option here is a button that tabs and activates on its own, and its pressed state is announced.
 *
 * `refusal` is a sentence rather than a boolean, because a control that a reader can see and cannot
 * use has to say why. It disables the option AND hangs the reason on it; the caller is responsible
 * for putting that reason somewhere a pointer is not needed, since a disabled button is not
 * focusable and its tooltip is unreachable by keyboard.
 */
function Switch<T extends string>({ group, options, current, name, refusal, onPick, testid }: {
  /** The switch's own accessible name, e.g. `Whose runs`. */
  group: string;
  options: readonly T[];
  current: T;
  name: (option: T) => string;
  /** Why an option cannot be chosen, or `null`. */
  refusal: (option: T) => string | null;
  onPick: (option: T) => void;
  testid: string;
}) {
  return (
    <div role="group" aria-label={group} data-testid={testid} style={switchStyle}>
      {options.map((option) => {
        const blocked = refusal(option);
        const pressed = option === current;
        return (
          <button
            key={option}
            type="button"
            data-testid={`${testid}-${option}`}
            /* The persisted choice stays pressed even when it is refused, rather than the switch
               showing nothing chosen. A reader who picked 7 days and switched scope should see that
               their choice is still theirs and is greyed out here — a switch with no pressed option
               reads as one that lost the setting. */
            aria-pressed={pressed}
            disabled={blocked !== null}
            title={blocked ?? undefined}
            onClick={() => onPick(option)}
            style={{
              ...switchOptionStyle,
              background: pressed ? theme.colors.surface : 'transparent',
              color: pressed ? theme.colors.textPrimary : theme.colors.textSecondary,
              boxShadow: pressed ? theme.elevation.subtle : undefined,
              opacity: blocked === null ? 1 : theme.opacity.disabled,
              cursor: blocked === null ? 'pointer' : 'not-allowed',
            }}
          >
            {name(option)}
          </button>
        );
      })}
    </div>
  );
}

// --- the block -------------------------------------------------------------
/**
 * What to render when every visible metric is unavailable for the same structural reason — which
 * is the deployed state today, because the producer serving the bundle predates the scorecard.
 *
 * `collapse` replaces the tiles with the single shared cause. `tiles` keeps three tiles, each
 * carrying that cause. `hide` renders nothing at all, gear included.
 */
export type StructuralFallback = 'collapse' | 'tiles' | 'hide';

export function MetricBlock({ data, structuralFallback = 'collapse' }: {
  data: ImproveData;
  structuralFallback?: StructuralFallback;
}) {
  const [choice, setChoice] = React.useState<StoredChoice>(readChoice);
  /**
   * The selection in effect, HELD rather than derived from `choice` on every render.
   *
   * `topUp` is a mount-time rule: it fills the gaps a stale or short stored list leaves, so a reader
   * always opens on three. Re-deriving it per render would apply it to a list the reader is midway
   * through editing — unchecking a row would refill that slot with the default before they could
   * choose what goes in it, and the row would appear not to respond.
   */
  const [selection, setSelection] = React.useState<MetricId[]>(
    () => topUp(choice.metrics ?? []));
  const [pickerOpen, setPickerOpen] = React.useState(false);
  const pickerId = React.useId();
  const captionId = React.useId();

  /*
   * Escape closes it, following `ConfirmDialog`: bubble phase on `document`, no
   * `stopImmediatePropagation`. `HooksPanel` uses the capture phase and stops propagation because
   * it is a full editor that must win Escape outright; a picker anchored to one control must NOT
   * swallow Escape from a dialog it might be sitting inside.
   */
  React.useEffect(() => {
    if (!pickerOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPickerOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [pickerOpen]);

  // Resolved from the stored record and the defaults, never stored resolved — see `STORAGE_KEY`.
  const view: MetricView = {
    scope: choice.scope ?? DEFAULT_VIEW.scope,
    period: choice.period ?? DEFAULT_VIEW.period,
  };

  /** One writer for all three preferences, so none of them can come to persist differently. */
  const change = (over: StoredChoice) => {
    const next = { ...choice, ...over };
    setChoice(next);
    writeChoice(next);
  };

  const choose = (id: MetricId, checked: boolean) => {
    // Already in the state being asked for — and adding an id the selection holds would render one
    // metric twice.
    if (selection.includes(id) === checked) return;
    const next = checked ? [...selection, id] : selection.filter((chosen) => chosen !== id);
    /*
     * The cap and the floor again, in state rather than only in markup. `disabled` on the row is
     * what a reader sees refusing them; this is what holds when something reaches the handler
     * anyway — a dispatched event, or a row that is one day not a native control.
     */
    if (next.length > VISIBLE || next.length === 0) return;
    setSelection(next);
    change({ metrics: next });
  };

  /*
   * Why a period cannot be picked at this scope. It is a property of the scope and the wire rather
   * than of any figure, so a block that asked each metric would get one cause three times — and the
   * reads themselves are total over every view, which is what keeps the refusal out of all eight of
   * them.
   *
   * It reaches the reader as the disabled button carrying the reason as its tooltip, and as nothing
   * else. The greyed-out control IS the message; a paragraph beside it restating why was the wire's
   * own reasoning printed where a figure should be.
   */
  const periodRefusalFor = (period: MetricPeriod) => periodRefusal({ ...view, period })?.reason
    ?? null;

  const shown = selection
    .map((id) => ALL_METRICS.find((entry) => entry.id === id))
    // `readChoice` only yields live ids, so nothing is dropped here. Filtering rather than
    // asserting keeps that true by construction instead of by trust.
    .filter((entry): entry is MetricEntry => entry !== undefined);
  const readings = shown.map((entry) => ({ entry, reading: entry.read(data, view) }));
  const collapsible = sharedStructuralCause(readings.map(({ reading }) => reading));

  if (collapsible !== null && structuralFallback === 'hide') return null;

  /**
   * The cause standing in for the figures, or `null` while the figures are showing.
   *
   * The decision named once rather than made twice in the markup below, because the reserved height
   * and the render it governs have to agree: a band that centred its content while still drawing
   * three tiles would float them, and one that reserved room for tiles it was not drawing without
   * centring would leave the room hanging under the line. Carrying the cause rather than a boolean
   * also keeps the narrowing, so the render below needs no assertion that it is there.
   */
  const collapsed: Unavailable | null = collapsible !== null && structuralFallback === 'collapse'
    ? collapsible : null;

  /*
   * Both refusals, one sentence each, carried in the picker's own accessible name. The cap DISABLES
   * unchecked rows rather than swapping the oldest choice out: a selection the user did not make is
   * worse than a refusal they can see, and a silent swap is unattributable — the tile that vanished
   * is the one they were reading. The floor is that argument from the other end, and is what makes an
   * empty stored array unambiguously corrupt rather than deliberate.
   */
  const atCap = selection.length >= VISIBLE;
  const atFloor = selection.length === 1;
  const basis = describeBasis(data, view);
  /*
   * The picker STATES the basis and does not set it. Which is the whole reason the switches are on
   * the header: a reader who opens this menu to swap a figure needs to know what the figures are
   * figures of, and a control here would put that answer behind a click for everyone who never
   * opens it.
   *
   * `null` where there is no span to name, and then the scope alone finishes the sentence — a
   * trailing comma with nothing after it would be the accessible name of this whole group.
   */
  // The cap is stated once, in the opening clause. `3 is the most` after it was the same fact a
  // second time, so what is left when the cap bites is the part a reader cannot deduce: the way out.
  const caption = `Up to ${VISIBLE} metrics at once.`
    + (atCap ? ' Uncheck one to swap.' : '')
    + (atFloor ? ' At least one stays shown.' : '')
    + ` Showing ${SCOPE_LABEL[view.scope].toLowerCase()}${basis === null ? '' : `, ${basis}`}.`;

  return (
    <div data-testid="metric-block">
      {/* `relative`, so the panel below anchors to this row rather than to the page. */}
      <div style={headerStyle}>
        <Switch
          group="Whose runs"
          testid="metric-scope"
          options={SCOPES}
          current={view.scope}
          name={(scope) => SCOPE_LABEL[scope]}
          // Never refused: the shared figures come off a scorecard that is either there or reported
          // absent by the readings themselves, and the personal ones off the reader's own item.
          refusal={() => null}
          onPick={(scope) => change({ scope })}
        />
        <Switch
          group="How far back"
          testid="metric-period"
          options={PERIODS}
          current={view.period}
          name={(period) => PERIOD_LABEL[period]}
          refusal={periodRefusalFor}
          onPick={(period) => change({ period })}
        />
        <div style={{ flex: 1 }} />
        <button
          type="button"
          data-testid="metric-picker-toggle"
          aria-label="Choose metrics"
          title="Choose metrics"
          aria-expanded={pickerOpen}
          aria-controls={pickerOpen ? pickerId : undefined}
          onClick={() => setPickerOpen((open) => !open)}
          style={gearStyle}
        >
          <GearIcon />
        </button>

        {pickerOpen && (
          <>
            {/* Click-away layer: a picker that only closes on re-click traps the user behind
                it. Copied from the create menu on the Capabilities settings surface. */}
            <div onClick={() => setPickerOpen(false)} style={scrimStyle} aria-hidden="true" />
            {/* A group of real checkboxes, NOT `role="menu"` with `menuitem` rows: `menuitem` is for
                commands, and menu semantics promise a keyboard contract (roving focus, typeahead)
                that neither this file nor the menu it borrows its scrim from implements. A native
                checkbox announces checked, unchecked and disabled on its own and tabs without
                help. */}
            <div
              id={pickerId}
              role="group"
              aria-labelledby={captionId}
              data-testid="metric-picker"
              style={panelStyle}
            >
              <div id={captionId} data-testid="metric-picker-caption" style={captionStyle}>
                {caption}
              </div>
              {/* `METRICS`, not the widened alias: this loop reads no `format`, and the const
                  tuple types each `id` as the literal a selection is made of. */}
              {METRICS.map((entry) => {
                /* Under the DECLARED union, not each entry's inferred one. `METRICS` is `as const`,
                   so a read that happens to return no `caveat` today is typed as a shape with no
                   such property — and the row below would then be a compile error on the seven
                   metrics that have nothing to qualify rather than the `undefined` it handles. */
                const reading: Reading = entry.read(data, view);
                const label = metricLabel(entry, view.scope);
                const checked = selection.includes(entry.id);
                const blocked = checked ? atFloor : atCap;
                const hint = entry.kind === 'ratio' ? sidesHint(label, entry.sides) : null;
                return (
                  <label
                    key={entry.id}
                    data-testid={`metric-option-row-${entry.id}`}
                    style={{
                      ...optionStyle,
                      opacity: blocked ? theme.opacity.disabled : 1,
                      cursor: blocked ? 'not-allowed' : 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      data-testid={`metric-option-${entry.id}`}
                      checked={checked}
                      disabled={blocked}
                      onChange={(event) => choose(entry.id, event.target.checked)}
                    />
                    <span style={{ minWidth: 0 }}>
                      <span style={optionLabelStyle}>{label}</span>
                      {/* The side names are static on the entry precisely so a picker can
                          describe a split it cannot currently read. */}
                      {hint !== null && <span style={optionHintStyle}>{` ${hint}`}</span>}
                      {!reading.available && (
                        // Verbatim and unabridged. This row is full-width, so the sentence the
                        // producer wrote fits here even when it does not fit a tile.
                        <span
                          data-testid={`metric-option-reason-${entry.id}`}
                          style={optionReasonStyle}
                        >
                          {reading.reason}
                        </span>
                      )}
                      {/*
                        And the same treatment for a reading that IS available but carries more
                        qualification than its footnote can hold — cost's pricing basis, whose 839
                        characters the tile shows the first 26 of.

                        This is the KEYBOARD-reachable copy, which is why it exists alongside the
                        tile's tooltip rather than instead of it: the picker is a button and a group
                        of checkboxes, so a reader who never uses a pointer still gets here. Its own
                        testid rather than the reason's, because the two are different facts — one
                        says nothing measured this, the other qualifies a number that was measured —
                        and this surface's whole discipline is refusing to spell those the same way.
                      */}
                      {reading.available && reading.caveat !== undefined && (
                        <span
                          data-testid={`metric-option-caveat-${entry.id}`}
                          style={optionReasonStyle}
                        >
                          {reading.caveat}
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </>
        )}
      </div>

      {/*
        What the figures are figures OF, always visible and never behind the gear. A cumulative total
        with no basis cannot be read; `since 2 Sep` is what makes it one. It also carries the bundle's
        own `createdAt`, because a quiet actor's bundle is skipped rather than rewritten — so a stale
        total reads exactly like a current one and that date is the only thing that says otherwise.

        Nothing at the shared scope, where the figures are not a cumulative total and the denominator
        a reader needs is already on each tile it qualifies. And nothing where the collapsed cause
        below is itself a phrase: there is no span to name, and a second short form of the same
        absence on the line above it is the duplication this whole band is trying not to be.
      */}
      {/*
        The caption and the figures are wrapped TOGETHER, and the reserved height is on the wrapper
        rather than on the row inside it, because both of them come and go with the scope: `Everyone`
        draws a caption and three tiles, `Yours` with no bundle of its own draws one line and no
        caption at all. Reserving room for the row alone would have left the caption's line free to
        appear and disappear underneath the reserve, which is the same jump one step smaller.
      */}
      <div
        data-testid="metric-figures"
        style={{
          ...figuresStyle,
          // The collapsed line sits in the middle of the room the tiles would have taken, rather
          // than at the top of it with the whole reserve hanging empty underneath.
          justifyContent: collapsed !== null ? 'center' : 'flex-start',
        }}
      >
        {basis !== null && <div data-testid="metric-basis" style={basisStyle}>{basis}</div>}

        {collapsed !== null ? (
          <CollapsedCause
            labels={shown.map((entry) => metricLabel(entry, view.scope))}
            cause={collapsed}
          />
        ) : (
          <div data-testid="metric-row" style={rowStyle}>
            {readings.map(({ entry, reading }) => (reading.available ? (
              <MetricTile
                key={entry.id}
                label={metricLabel(entry, view.scope)}
                value={reading.value}
                // Straight through. The right formatter for a metric is a property of the metric,
                // and the block knows nothing about units.
                format={entry.format}
                sub={reading.sub}
                /* The whole of a caveat the footnote could only show the head of — cost's
                 * 839-character pricing basis today. Straight through and unabridged: the tile's own
                 * footnote is the abridgement, and this is the copy that makes that abridgement
                 * honest. Undefined on every metric that has nothing more to say than its footnote
                 * already says, which is every metric but one. */
                title={reading.caveat}
                /* The bridge. The registry keeps the share on the reading and the side names on the
                 * entry — the names describe the metric, not any payload, so an unavailable read
                 * does not have to invent them. The tile wants the three together. */
                ratio={entry.kind === 'ratio' && carriesRatio(reading)
                  ? { value: reading.ratio, part: entry.sides.part, rest: entry.sides.rest }
                  : undefined}
              />
            ) : (
              <UnavailableTile
                key={entry.id}
                label={metricLabel(entry, view.scope)}
                reason={reading.reason}
              />
            )))}
          </div>
        )}
      </div>
    </div>
  );
}

// --- styles ----------------------------------------------------------------
/** The switches, then the gear pushed to the far end. `relative` so the picker panel anchors to
 *  this row rather than to the page. */
const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  position: 'relative',
  // The two switches wrap onto a second line before they squeeze each other, since an option whose
  // label is clipped is an option a reader cannot read.
  flexWrap: 'wrap',
};

/** A segmented control: one tinted trough, options sitting in it. `surfaceSubtle` because it is
 *  furniture, not a status. */
const switchStyle: React.CSSProperties = {
  display: 'inline-flex',
  gap: 2,
  padding: 2,
  background: theme.colors.surfaceSubtle,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
};

const switchOptionStyle: React.CSSProperties = {
  padding: '3px 9px',
  border: 'none',
  borderRadius: theme.radius.button,
  fontSize: theme.typography.sizes.meta,
  lineHeight: theme.typography.lineHeight,
  whiteSpace: 'nowrap',
};

/** Right-aligned under the switches it qualifies, one step quieter than a tile's own footnote so it
 *  reads as the row's caption rather than as a fourth figure. */
const basisStyle: React.CSSProperties = {
  ...metaTextStyle,
  color: theme.colors.textTertiary,
  textAlign: 'right',
  marginTop: 2,
};

/**
 * How much vertical room the band keeps for its caption and figures, whichever scope is showing.
 *
 * `Everyone` fills it with a caption and three tiles; `Yours` collapses to one line when no figure of
 * the reader's own can be read, which is most readers. With nothing reserved the pane below moved
 * every time the scope switch was flipped, so a reader comparing the two scopes lost their place in
 * the list underneath on every press.
 *
 * A `minHeight` and never a `height`: a tile whose footnote wraps onto a second line still grows the
 * band, it simply cannot shrink it below the room a one-line tile needs. Which is also the limit of
 * what this buys — see the note above `figuresStyle`.
 *
 * Spelled as a SUM OF THE TYPE it has to hold rather than as a pixel count, because the size tokens
 * are `var()` references wired to the General → Font Size control: a literal `85px` would be right at
 * one setting and wrong at the other two. The terms are read top to bottom off what the band renders.
 */
const RESERVED_FIGURE_HEIGHT = 'calc('
  // Three lines of meta type: the basis caption, then a tile's label and its footnote.
  + `${theme.typography.sizes.meta} * ${theme.typography.lineHeight} * 3`
  // And one line of display type: the figure itself, set at `sectionTitle` inside the tile.
  + ` + ${theme.typography.sizes.sectionTitle} * ${theme.typography.lineHeight}`
  // The offsets between them: 2 above the caption, 6 above the row, 3 above the tile's footnote.
  + ' + 11px)';

/**
 * The band's own box: what holds the reserve, and what centres the collapsed line inside it.
 *
 * A flex COLUMN so that one `justifyContent` can move its contents within the reserve, which is the
 * whole reason the wrapper exists — the alternative was padding computed per branch, and padding
 * added to the collapsed case would be room the tiles then had to be given too.
 *
 * What this does NOT do, stated because the mechanism invites the opposite reading: it equalises the
 * two scopes at the reserve, not at each other's true heights. Content taller than the reserve wins,
 * so a tile whose footnote wraps still makes `Everyone` the taller of the two. Reserving for the
 * wrapped case instead would leave the slack visible under the tiles on every ordinary payload, which
 * trades a jump nobody sees for a gap everybody does.
 */
const figuresStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  minHeight: RESERVED_FIGURE_HEIGHT,
};

const rowStyle: React.CSSProperties = {
  display: 'grid',
  /*
   * `minmax(0, 1fr)` rather than a bare `1fr`: a track's default minimum is its own content, so one
   * long footnote (`39.2k tokens per call · 354 of 553 sessions` is a real one) widens its column and
   * squeezes the other two. `MetricTile` sets `minWidth: 0` on itself for the same reason a level
   * down — the track floor and the item floor are separate constraints and both are needed.
   *
   * Three tracks even when fewer tiles are shown, so swapping a metric does not resize its
   * neighbours.
   */
  gridTemplateColumns: `repeat(${VISIBLE}, minmax(0, 1fr))`,
  gap: 12,
  marginTop: 6,
  // Tiles differ in height (a bar, a wrapped footnote); stretching them to the tallest would float
  // the short ones' figures away from their labels.
  alignItems: 'start',
};

/** Matches `MetricTile`'s own label treatment, so an unavailable tile lines up with a measured
 *  one beside it. */
const tileLabelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const tileFootnoteStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textTertiary,
  marginTop: 3,
  lineHeight: theme.typography.lineHeight,
};

const gearStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: 3,
  background: 'none',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};

const scrimStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: theme.zIndex.popover,
};

const panelStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 6px)',
  right: 0,
  zIndex: theme.zIndex.popover + 1,
  minWidth: 320,
  maxWidth: 420,
  // Eight rows, some carrying a long reason, still outgrow the pane. Scroll the panel rather than
  // the page: the scrim is fixed, so the page behind it must not move.
  maxHeight: '60vh',
  overflowY: 'auto',
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 4,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  textAlign: 'left',
};

const captionStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
  padding: '5px 8px',
};

const optionStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '5px 8px',
  borderRadius: theme.radius.button,
};

const optionLabelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  color: theme.colors.textPrimary,
};

const optionHintStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

const optionReasonStyle: React.CSSProperties = {
  display: 'block',
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textTertiary,
  lineHeight: theme.typography.lineHeight,
  marginTop: 2,
};
