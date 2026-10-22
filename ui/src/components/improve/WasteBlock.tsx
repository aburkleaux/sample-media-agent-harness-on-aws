/**
 * The efficiency path, rendered: work the reader's own runs were charged for and threw away.
 *
 * Reads `ActorBundle.waste` and nothing else. The findings on that bundle name tool sequences, so
 * they exist ONLY on the reader's own item and this block may never move to a shared surface — a
 * tool name can come from a user-registered MCP server, and here the reader owns the session it
 * came out of.
 *
 * Four states, all of which say in words which one they are, because three different absences
 * reach this component and collapsing any two of them would report a fact nobody measured:
 *
 *   - no actor bundle at all: the producer harvests a bounded window and writes a bundle only for
 *     an actor who had a session in it, so most readers on most days have none.
 *   - a bundle with NO `waste` key: written before the efficiency path existed. Nobody looked.
 *   - `waste` with no findings: somebody looked at a named number of runs and found nothing.
 *   - findings: the list below.
 *
 * `waste` is optional because an item stored before the key existed is served back unchanged, so
 * the field can be absent at runtime even though every bundle today's producer writes carries one
 * (`_REQUIRED_ACTOR_BUNDLE_KEYS`).
 *
 * Between the total and the list sits the SPLIT BETWEEN KINDS, because neither of those can say
 * which category of discarded work is worth reading: the same bundle whose 28 findings are ranked
 * below has one kind accounting for 18 of them and 149ms. See `WasteKindTable`.
 *
 * RANKED BY TIME, descending, and that ordering is the whole design rather than a preference. On
 * the largest live bundle 10 of 28 findings carry 99.96% of the charged time and the other 18 carry
 * 0.04% — 149 ms between them — so a flat chronological list buries a two-minute finding under
 * eighteen worth milliseconds. Everything below the threshold collapses into one line that keeps
 * its count and its total, and nothing is ever dropped.
 *
 * What it deliberately does NOT render is `WasteFinding.confidence` inline. That is a ~300–500
 * character engineering paragraph naming internal identifiers
 * (`signature.session_ended_on_unretrieved_dispatch`, `NS-5`, `DF-8/DF-10`); it is the right text
 * and the wrong register, so each kind gets one short caveat on screen and the producer's own
 * wording on hover.
 */
import React from 'react';
import { theme, numericStyle } from '../../styles/tokens';
import {
  isSentinel, type ActorBundle, type ActorWasteTotals, type MeasuredSum, type WasteFinding,
  type WasteKindTotals,
} from '../../improve-api';
import {
  MeasuredValue, SentinelValue, UnavailableBadge, formatExtent, formatIsoDay, fractionLabel,
  metaTextStyle, pluralise, tileValueStyle,
} from './improveChrome';

/**
 * Below this a finding is counted and totalled but not given a row of its own: **one second**.
 *
 * A judgement about what is worth a reader's attention, so it is stated on screen and not only
 * here. One second is the shortest delay a person experiences as a wait at all — under it there is
 * no felt cost, so a finding cannot on its own justify changing anything, however real it is. That
 * reason is about the reader rather than about this dataset, which is why the bound is not tuned to
 * the live distribution: on the busiest bundle today it happens to fold 18 of 28 findings worth
 * 149ms in total, and it would still be the right bound on a bundle shaped nothing like that one.
 *
 * The tail is collapsed, never discarded: a measurement too small to act on is still a
 * measurement, and its count and combined total are on screen whether it is expanded or not.
 *
 * Note what this does NOT bound. There is no cap on the rows ABOVE it — every finding worth a
 * second of anybody's time gets a row, because that is what the threshold means.
 */
const TAIL_THRESHOLD_MS = 1000;

/** Spelled out where the fold is, so the bound is readable without opening this file. */
const TAIL_THRESHOLD_LABEL = '1s';

/**
 * The reader's phrase for each kind the producer emits, with its numbers taken off the finding.
 *
 * `repeated_sequence` deliberately does NOT claim a step count, and the reason is in the producer:
 * matching is on the ordered tool NAMES and not on their multiplicities, and `calls` is charged
 * over every occurrence but the last — so `calls / (occurrences - 1)` is a MEAN across occurrences
 * that genuinely differ in length (`waste._repeated_sequence`: 56 of 94 findings over the full
 * population have different multiplicities in different occurrences). Printing a mean as a block
 * size would be exactly the laundering this surface exists to prevent. The two numbers that ARE
 * exact — how many times it ran, and what was charged — travel as themselves, and `detail` beside
 * them names the block.
 *
 * A kind this screen has no phrase for still gets a row naming itself. The producer can add a
 * fourth kind and ship before this file does; a blank cell would read as a render fault.
 */
function findingPhrase(finding: WasteFinding): React.ReactNode {
  const { kind, calls, occurrences } = finding;
  switch (kind) {
    case 'repeated_sequence':
      return (
        <>
          {'ran this block '}
          <span style={numericStyle}>{occurrences}</span>
          {' times, '}
          <span style={numericStyle}>{calls}</span>
          {` ${pluralise(calls, 'call')} charged`}
        </>
      );
    case 'abandoned_dispatch':
      return 'dispatched work, never read it';
    case 'error_then_retry':
      return 'a call failed and was retried';
    default:
      return (
        <>
          {'a finding kind this screen has no phrase for: '}
          <span style={numericStyle}>{kind || '(unnamed)'}</span>
        </>
      );
  }
}

/**
 * The reader's NOUN for each kind — what a group of these findings is called.
 *
 * `findingPhrase` above says what one finding did, in a verb; the split table and the caveats below
 * both need to name the category itself. One map for that, because two spellings of one kind on one
 * screen read as two different things: the caveats are built from these rather than repeating them.
 */
const KIND_NOUN: Record<string, string> = {
  repeated_sequence: 'repeats',
  abandoned_dispatch: 'abandoned dispatches',
  error_then_retry: 'retries',
};

/**
 * One clause per kind saying what a finding of it cannot establish.
 *
 * Shared per KIND rather than per finding, because the producer's `confidence` is identical across
 * every finding of a kind (verified against the live bundle: 28 findings, three distinct strings)
 * and repeating a paragraph on nine rows would bury the rows in it. The full text stays reachable
 * on hover, so nothing is lost — only moved out of the reading line.
 */
const KIND_CAVEAT: Record<string, string> = {
  repeated_sequence: `${KIND_NOUN.repeated_sequence} — not proven waste: spans carry no tool `
    + 'arguments, so a second run over different inputs looks the same as a redo.',
  abandoned_dispatch: `${KIND_NOUN.abandoned_dispatch} — a floor: only the dispatch call is `
    + 'priced, never the job it started.',
  error_then_retry: `${KIND_NOUN.error_then_retry} — the retry may not have been avoidable.`,
};

/** A finding whose `wallMs` is a real number, so it can be ranked. */
interface TimedFinding {
  finding: WasteFinding;
  wallMs: number;
}

/**
 * Split the findings into the three groups that can be READ differently, ranked within the first.
 *
 * An unmeasured `wallMs` is not a zero and must not sort as one. The producer propagates absence
 * rather than partially summing (`waste._sum_durations` returns the sentinel if any contributing
 * call lacked a duration), so such a finding is real, is of unknown size, and belongs in a labelled
 * group of its own instead of at the bottom of a list ordered by size.
 */
function partition(findings: readonly WasteFinding[]): {
  head: TimedFinding[];
  tail: TimedFinding[];
  untimed: WasteFinding[];
  tailMs: number;
} {
  const timed: TimedFinding[] = [];
  const untimed: WasteFinding[] = [];
  for (const finding of findings) {
    const ms = finding.wallMs;
    if (isSentinel(ms)) untimed.push(finding);
    else timed.push({ finding, wallMs: ms });
  }
  timed.sort((a, b) => b.wallMs - a.wallMs);
  const head = timed.filter((entry) => entry.wallMs >= TAIL_THRESHOLD_MS);
  const tail = timed.filter((entry) => entry.wallMs < TAIL_THRESHOLD_MS);
  return { head, tail, untimed, tailMs: tail.reduce((sum, entry) => sum + entry.wallMs, 0) };
}

/**
 * The kinds on screen, in the order the ranking first reaches them.
 *
 * Ordered off the data rather than off a declared list so the costliest kind's caveat comes first,
 * and so a kind the producer adds appears here without this file being edited.
 */
function kindsInRankOrder(groups: ReturnType<typeof partition>): string[] {
  const ordered: string[] = [];
  const seen = new Set<string>();
  const all = [
    ...groups.head.map((entry) => entry.finding),
    ...groups.untimed,
    ...groups.tail.map((entry) => entry.finding),
  ];
  for (const finding of all) {
    if (seen.has(finding.kind)) continue;
    seen.add(finding.kind);
    ordered.push(finding.kind);
  }
  return ordered;
}

// --- the three-way split ----------------------------------------------------
/** One kind's totals with its key, so a row can name itself. */
interface KindRow {
  kind: string;
  totals: WasteKindTotals;
}

/** A kind whose charged time is a real number, so it can be ranked. */
interface TimedKind extends KindRow {
  charged: number;
}

/**
 * The kinds in three tiers: sized, unsizeable, and found nothing.
 *
 * Within the first, by what it charged — the same rule as `partition`, and for the same reason: this
 * table's whole job is that findings and time disagree (18 of 28 findings are worth 149ms between
 * them), so the column to scan first is time and the order has to be time's.
 *
 * The other two tiers exist because a kind with no sum must not sort as a zero, and because the two
 * reasons a kind has no sum are not equally interesting. A kind that found something nothing could
 * time is a gap in the evidence and belongs directly under the sized rows; a kind that found nothing
 * has no gap at all and belongs at the bottom, which is where a reader looks for "nothing here". The
 * wire's own order is kept inside each tier, which `Array.sort`'s stability gives for free.
 */
function kindRowsByCost(byKind: Record<string, WasteKindTotals>): KindRow[] {
  const sized: TimedKind[] = [];
  const unsizeable: KindRow[] = [];
  const foundNothing: KindRow[] = [];
  for (const kind of Object.keys(byKind)) {
    const totals = byKind[kind];
    const sum = totals.wallMs.sum;
    if (!isSentinel(sum)) sized.push({ kind, totals, charged: sum });
    else if (totals.findings > 0) unsizeable.push({ kind, totals });
    else foundNothing.push({ kind, totals });
  }
  sized.sort((a, b) => b.charged - a.charged);
  return [...sized, ...unsizeable, ...foundNothing];
}

/**
 * Why a kind is on screen with nothing against its name.
 *
 * The producer emits EVERY kind it has, zeros included (`waste.WASTE_KINDS`), precisely so a reader
 * can tell a signal that looked and found nothing from a signal this producer does not have. A
 * dropped row would throw that away: three rows where one reads zero says the scan covered three
 * things, two rows says nothing about the third.
 */
const KIND_FOUND_NOTHING = 'This signal ran over the same runs and found nothing. Listed rather '
  + 'than dropped: every kind the producer has is always present, so a signal that found nothing '
  + 'stays tellable apart from a signal the producer does not have.';

/** Why the charged column can be empty on a row whose counts are a measured zero. */
const KIND_NOTHING_TO_SUM = 'No finding of this kind, so there was never a duration to sum. Not a '
  + 'measurement that came back empty — there was nothing to measure.';

const kindRowStyle: React.CSSProperties = {
  display: 'grid',
  // Fixed tracks, so the figures line up down the page instead of to the width of each kind's
  // noun — the comparison between the columns is the whole point of the table.
  gridTemplateColumns: 'minmax(0, 1fr) 84px 74px 52px minmax(76px, auto)',
  gap: 12,
  alignItems: 'baseline',
  padding: '2px 0',
};

const kindFigureStyle: React.CSSProperties = { ...numericStyle, textAlign: 'right' };

/** The kind's noun, or the identifier the producer used when this screen has no word for it. */
function KindName({ kind, found }: { kind: string; found: boolean }) {
  const noun = KIND_NOUN[kind];
  if (noun === undefined) {
    // The producer can add a kind and ship before this file does. Naming it as the producer
    // spells it beats a blank cell, which would read as a render fault — same rule as
    // `findingPhrase`'s own fallback.
    return (
      <span title={'This screen has no phrase for this kind yet, so it is named as the producer '
        + 'spells it.'}>
        <span style={numericStyle}>{kind || '(unnamed)'}</span>
      </span>
    );
  }
  if (found) return <span>{noun}</span>;
  return (
    <span style={{ color: theme.colors.textTertiary }} title={KIND_FOUND_NOTHING}>{noun}</span>
  );
}

/**
 * What one kind charged, or which of the two absences applies.
 *
 * They are different facts and must not share a treatment. `findings: 0` means nothing was ever
 * summed — no duration existed to measure, which is `UnavailableBadge`'s case. A kind WITH findings
 * whose sum is still the sentinel means the producer summed and every contributing run lacked a
 * duration, which is `SentinelValue`'s. Both are equally not a zero.
 */
function KindCharged({ wallMs, findings }: { wallMs: MeasuredSum; findings: number }) {
  const { sum, measuredOf, total } = wallMs;
  if (isSentinel(sum)) {
    if (findings === 0) {
      return <span title={KIND_NOTHING_TO_SUM}><UnavailableBadge /></span>;
    }
    return <SentinelValue value={sum} />;
  }
  return (
    <>
      <span style={numericStyle}>{formatExtent(sum)}</span>
      {/* Stated only when it is a shortfall, as on the total above: `4 of 4` restates the runs
          column, `3 of 4` says this figure is short by one run of unknown size. The pair counts
          RUNS, not findings, and equals this kind's own `runs`. */}
      {measuredOf < total && (
        <div style={{ color: theme.colors.textTertiary }}>
          <span style={numericStyle}>{fractionLabel(measuredOf, total)}</span>
          {` ${pluralise(total, 'run')} timed`}
        </div>
      )}
    </>
  );
}

/**
 * The split between kinds — the table that says which CATEGORY of discarded work is worth reading.
 *
 * The findings list below ranks individual findings and the total above sums them, and between them
 * they cannot answer the first question a reader has: on the largest live bundle `error_then_retry`
 * is 18 of 28 findings and 149 MILLISECONDS, while the other two kinds are 10 findings and 412
 * seconds. Scanning findings alone, one whole category of noise looks like two thirds of the
 * problem. So findings and time sit in one row per kind, and the order is time's.
 *
 * What this deliberately does NOT do is present the columns as a partition. The kinds are
 * independent observations of one trajectory and can overlap — an errored call can sit inside a
 * repeated block — so `calls` is exact per kind and sums to no total; `WasteTotals.totalCalls` is a
 * union per run and is smaller than the column's sum whenever any run was double-charged. The one
 * denominator that IS sound comes off the findings list this table heads.
 *
 * Rendered in the findings state only. In the nothing-found state the sentence there already
 * reports the whole result against its denominator, and three all-zero rows would restate it.
 */
function WasteKindTable({ waste, findings }: {
  waste: ActorWasteTotals;
  /** The very list this table heads, passed in rather than re-read off `waste`: it is the
   *  findings column's denominator, and one read of a key the wire can omit is enough. */
  findings: readonly WasteFinding[];
}) {
  // Annotated wider than the type declares, for `waste`'s own reason: an item stored before this
  // key existed is served back unchanged, so it can be absent at runtime. Reading it as declared
  // and calling `Object.keys` on it would throw on such a bundle.
  const byKind: Record<string, WasteKindTotals> | undefined = waste.byKind;
  if (byKind === undefined) {
    return (
      <Absence testId="waste-by-kind-absent">
        This bundle carries no per-kind split, so which category each finding below falls into was
        never recorded. Not a report that they are all of one kind.
      </Absence>
    );
  }
  const rows = kindRowsByCost(byKind);
  if (rows.length === 0) {
    // Distinct from the absence above: the producer wrote the key and named no kind in it, which
    // no producer shipped so far can do — every one of them emits its whole kind list.
    return (
      <Absence testId="waste-by-kind-empty">
        The per-kind split on this bundle names no kinds at all, so nothing here says which
        signal found what.
      </Absence>
    );
  }

  const totalFindings = findings.length;
  const rowed = rows.reduce((sum, row) => sum + row.totals.findings, 0);

  return (
    <div data-testid="waste-by-kind" style={{ marginBottom: 8 }}>
      <div style={{ ...kindRowStyle, color: theme.colors.textTertiary }}>
        <span>which kind</span>
        <span style={{ textAlign: 'right' }}>findings</span>
        <span style={{ textAlign: 'right' }}>runs</span>
        <span style={{ textAlign: 'right' }}>calls</span>
        <span style={{ textAlign: 'right' }}>charged</span>
      </div>
      {rows.map(({ kind, totals }) => (
        <div key={kind} data-testid={`waste-kind-${kind}`} style={kindRowStyle}>
          <KindName kind={kind} found={totals.findings > 0} />
          <span style={kindFigureStyle}>{fractionLabel(totals.findings, totalFindings)}</span>
          <span style={kindFigureStyle}>{fractionLabel(totals.runs, waste.runs)}</span>
          <span style={kindFigureStyle}>{totals.calls}</span>
          <span style={{ textAlign: 'right' }}>
            <KindCharged wallMs={totals.wallMs} findings={totals.findings} />
          </span>
        </div>
      ))}
      {/* The findings column has a denominator only because every finding carries exactly one kind.
          A kind the producer emits on a finding but leaves out of the split is therefore countable,
          and says so here rather than quietly shrinking the column. */}
      {rowed < totalFindings && (
        <div data-testid="waste-by-kind-short" style={{ color: theme.colors.warning }}>
          <span style={numericStyle}>{fractionLabel(totalFindings - rowed, totalFindings)}</span>
          {' findings are of a kind this split does not name, so the rows above do not account '
            + 'for all of them.'}
        </div>
      )}
      {/* Two of the three columns visibly refuse to sum — 8 + 2 + 4 runs over a population of 13 —
          and a reader who adds them without this line has found what looks like a defect. */}
      <div data-testid="waste-by-kind-note" style={{ color: theme.colors.textTertiary }}>
        Each kind is an independent reading of the same sessions, so only the findings column adds
        up: two kinds can charge one call, and one run can carry several kinds.
        {' '}
        <ChargedCalls waste={waste} />
      </div>
    </div>
  );
}

/**
 * The calls column's actual answer — `WasteTotals.totalCalls`, the UNION of calls charged.
 *
 * This block's own caption has always said that two kinds can charge one call, and until now it said
 * so with the number that resolves it absent from the screen: a reader could be told the column does
 * not add up and be given nothing that does.
 *
 * NOT A TOTAL OF THE COLUMN, and it is worded to make that unsayable. The producer folds per RUN
 * first, so a call two kinds both charged counts once — which makes this figure smaller than the
 * column's sum whenever any run was double-charged, and EQUAL to it whenever none was. On the live
 * actor bundle the column happens to be 38 + 2 + 18 and this reads 58: a coincidence of a bundle
 * where no run overlapped, not an identity (11 of 107 runs overlap over the full population). So the
 * line never prints the column's sum beside it and never invites the comparison — presenting the
 * agreement as arithmetic is how the union would come to be read as a total, and then a real overlap
 * would look like a producer bug.
 *
 * Read WIDER than the type declares, for `waste`'s own reason: a bundle stored before the key existed
 * is served back unchanged. Absent says so rather than printing `0`, which would claim a scan that
 * charged nothing.
 */
function ChargedCalls({ waste }: { waste: ActorWasteTotals }) {
  const total: number | undefined = waste.totalCalls;
  if (typeof total !== 'number' || !Number.isFinite(total)) {
    return (
      <span data-testid="waste-calls-union-absent">
        This bundle records no charged-call figure of its own, so the column above is all there is —
        not a scan that charged nothing.
      </span>
    );
  }
  return (
    <span
      data-testid="waste-calls-union"
      title={'Counted once per call per run, so a call two kinds both charged is charged once here. '
        + 'That makes it smaller than the column above whenever a run carried two kinds, and equal '
        + 'to it when none did — it is not that column’s total either way.'}
    >
      {'Across all kinds, '}
      <span style={numericStyle}>{total}</span>
      {` ${pluralise(total, 'call')} ${total === 1 ? 'was' : 'were'} charged over `}
      <span style={numericStyle}>{fractionLabel(waste.runsWithWaste, waste.runs)}</span>
      {' runs, each counted once — so this is not the calls column’s sum.'}
    </span>
  );
}

/**
 * When the producer last wrote this bundle, or a sentence saying it did not stamp one.
 *
 * The date is load-bearing rather than decoration: a quiet actor's bundle is SKIPPED rather than
 * rewritten, so the previous run's item persists and a stale scan reads identically to a fresh one
 * — 5 of the 7 actor bundles live today are from an earlier pass. This is the only thing on the
 * wire that says so.
 *
 * Spelled with `formatIsoDay` and the word `recorded`, matching `metricRegistry.describeBasis`,
 * which already prints this same field for the same reason. A minute-resolution stamp would be a
 * second spelling of one fact on one surface.
 */
function AsOf({ createdAt }: { createdAt?: string }) {
  const day = typeof createdAt === 'string' ? formatIsoDay(createdAt.slice(0, 10)) : null;
  if (day === null) {
    return (
      <span data-testid="waste-as-of-absent" style={{ color: theme.colors.textTertiary }}>
        no recording date on this bundle, so its age is unknown
      </span>
    );
  }
  return <span data-testid="waste-as-of">{`recorded ${day}`}</span>;
}

const rowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'minmax(56px, auto) minmax(0, 1fr) minmax(0, auto)',
  gap: 12,
  alignItems: 'baseline',
  padding: '3px 0',
};

const timeCellStyle: React.CSSProperties = { ...numericStyle, textAlign: 'right' };

/** The named sequence, mono because it is tool names, and wrapped rather than cut: it is the one
 *  field a reader can act on and it arrives already abridged by the producer. */
const detailCellStyle: React.CSSProperties = {
  ...numericStyle,
  color: theme.colors.textSecondary,
  wordBreak: 'break-word',
};

function FindingRow({ finding, time, testId }: {
  finding: WasteFinding;
  /** The ranked time, or the sentinel for a finding that recorded none. */
  time: React.ReactNode;
  testId: string;
}) {
  return (
    <div data-testid={testId} style={rowStyle}>
      <span style={timeCellStyle}>{time}</span>
      {/* The producer's own wording, on hover only — see this file's opening note. */}
      <span title={finding.confidence || undefined}>{findingPhrase(finding)}</span>
      {finding.detail ? (
        <code style={detailCellStyle}>{finding.detail}</code>
      ) : (
        <span style={{ color: theme.colors.textTertiary }}>sequence not recorded</span>
      )}
    </div>
  );
}

/** A stated absence, never a hidden section: a region that vanishes reads as a render failure. */
function Absence({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div data-testid={testId} style={{ ...metaTextStyle, color: theme.colors.textSecondary }}>
      {children}
    </div>
  );
}

/**
 * Runs nobody searched, whenever there were any.
 *
 * Its entire purpose is that "looked and found nothing" differs from "did not look", so it is
 * stated wherever it is non-zero and never folded into the denominator beside it. Rendered in the
 * NOTHING-FOUND state as well as the list state, and that is the case it matters most in: "0 of 2
 * of your runs carried anything" with an unsearched run inside those 2 is a clean bill of health
 * over evidence that was never gathered.
 */
function NotScanned({ waste }: { waste: ActorWasteTotals }) {
  if (waste.runsNotScanned <= 0) return null;
  return (
    <div data-testid="waste-not-scanned"
         style={{ color: theme.colors.warning, marginTop: 4, marginBottom: 6 }}>
      <span style={numericStyle}>{fractionLabel(waste.runsNotScanned, waste.runs)}</span>
      {' runs were not scanned for repeats — too many tool calls to search — so nothing '
        + 'here is about them.'}
    </div>
  );
}

export function WasteBlock({ actor }: {
  /** The reader's own bundle, absent for most actors on most days. */
  actor?: ActorBundle;
}) {
  const [tailOpen, setTailOpen] = React.useState(false);
  // Read as optional: see this file's opening note on the declared type.
  const waste: ActorWasteTotals | undefined = actor?.waste;
  const findings: readonly WasteFinding[] = waste?.findings ?? [];
  const groups = partition(findings);

  return (
    <section data-testid="waste-block" style={metaTextStyle}>
      <h3 style={headingStyle}>Repeated and abandoned work</h3>

      {actor === undefined ? (
        <Absence testId="waste-no-bundle">
          No bundle of your own in this report, so nothing of yours was scanned.
        </Absence>
      ) : waste === undefined ? (
        <Absence testId="waste-absent">
          This bundle predates the efficiency-path scan, so nobody looked. Not a clean result.
        </Absence>
      ) : findings.length === 0 ? (
        <Absence testId="waste-none">
          {waste.runs === 0 ? (
            'No runs of yours are in this bundle, so nothing was scanned.'
          ) : (
            <>
              {'Nothing found: '}
              <span style={numericStyle}>{fractionLabel(waste.runsWithWaste, waste.runs)}</span>
              {' of your runs carried repeated or abandoned work.'}
            </>
          )}
          {' · '}
          <AsOf createdAt={actor.createdAt} />
          <NotScanned waste={waste} />
        </Absence>
      ) : (
        <>
          <div data-testid="waste-total" style={{ marginBottom: 6 }}>
            <MeasuredValue
              value={waste.totalWallMs.sum}
              format={formatExtent}
              style={{ ...tileValueStyle, fontSize: theme.typography.sizes.body }}
            />
            {' charged across '}
            <span style={numericStyle}>
              {fractionLabel(waste.runsWithWaste, waste.runs)}
            </span>
            {' of your runs · '}
            <span style={numericStyle}>{findings.length}</span>
            {` ${pluralise(findings.length, 'finding')}`}
            {/* Stated only when it is a shortfall: `28 of 28` restates the count beside it, but
                `27 of 28` says the total above is short by one finding of unknown size. */}
            {waste.totalWallMs.measuredOf < waste.totalWallMs.total && (
              <>
                {' · '}
                <span style={numericStyle}>
                  {fractionLabel(waste.totalWallMs.measuredOf, waste.totalWallMs.total)}
                </span>
                {' timed'}
              </>
            )}
            {' · '}
            <AsOf createdAt={actor.createdAt} />
          </div>

          <NotScanned waste={waste} />

          {/* Above the list it splits: which category is worth reading decides which rows the
              reader then goes looking for. */}
          <WasteKindTable waste={waste} findings={findings} />

          {groups.head.map((entry, index) => (
            <FindingRow
              key={`${entry.finding.runId}-${entry.finding.kind}-${index}`}
              finding={entry.finding}
              time={formatExtent(entry.wallMs)}
              testId={`waste-row-${index}`}
            />
          ))}

          {/* Unrankable, not small. Labelled and listed in place rather than sorted to the bottom,
              which would present an unknown duration as the least of these. */}
          {groups.untimed.length > 0 && (
            <div data-testid="waste-untimed" style={{ marginTop: 6 }}>
              <div style={{ color: theme.colors.textTertiary }}>
                <span style={numericStyle}>{groups.untimed.length}</span>
                {` ${pluralise(groups.untimed.length, 'finding')} recorded no duration, so `}
                {groups.untimed.length === 1 ? 'it cannot' : 'they cannot'}
                {' be ranked by time.'}
              </div>
              {groups.untimed.map((finding, index) => (
                <FindingRow
                  key={`${finding.runId}-${finding.kind}-${index}`}
                  finding={finding}
                  time={<SentinelValue value="UNMEASURED" />}
                  testId={`waste-untimed-row-${index}`}
                />
              ))}
            </div>
          )}

          {groups.tail.length > 0 && (
            <div data-testid="waste-tail" style={{ marginTop: 6 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                <span
                  style={{ color: theme.colors.textTertiary }}
                  title={`${TAIL_THRESHOLD_LABEL} is the shortest delay a reader experiences as a `
                    + 'wait, so a finding under it cannot on its own justify a change. Counted and '
                    + 'totalled here rather than dropped: it is still a measurement.'}
                >
                  <span style={numericStyle}>{groups.tail.length}</span>
                  {` more under ${TAIL_THRESHOLD_LABEL} · `}
                  <span style={numericStyle}>{formatExtent(groups.tailMs)}</span>
                  {' combined · too small to act on'}
                </span>
                <button
                  type="button"
                  onClick={() => setTailOpen((open) => !open)}
                  aria-expanded={tailOpen}
                  data-testid="waste-tail-toggle"
                  style={toggleStyle}
                >
                  {tailOpen ? 'hide' : 'show'}
                </button>
              </div>
              {tailOpen && groups.tail.map((entry, index) => (
                <FindingRow
                  key={`${entry.finding.runId}-${entry.finding.kind}-${index}`}
                  finding={entry.finding}
                  time={formatExtent(entry.wallMs)}
                  testId={`waste-tail-row-${index}`}
                />
              ))}
            </div>
          )}

          <div style={{ marginTop: 8, color: theme.colors.textTertiary }}>
            <div data-testid="waste-charged-note">
              Times are what was charged, not what could be saved.
            </div>
            {kindsInRankOrder(groups).map((kind) => (
              <div key={kind} data-testid={`waste-caveat-${kind}`}>
                {KIND_CAVEAT[kind] ?? `${kind} — this screen carries no caveat for this kind yet; `
                  + 'hover a row for the producer’s own wording.'}
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

const headingStyle: React.CSSProperties = {
  margin: '0 0 4px',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
};

const toggleStyle: React.CSSProperties = {
  marginLeft: 'auto',
  fontSize: theme.typography.sizes.meta,
  padding: 0,
  border: 'none',
  background: 'none',
  color: theme.colors.textSecondary,
  textDecoration: 'underline',
  textUnderlineOffset: 3,
  cursor: 'pointer',
};

export default WasteBlock;
