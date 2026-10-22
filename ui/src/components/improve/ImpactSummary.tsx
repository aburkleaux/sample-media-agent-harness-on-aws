/**
 * What the two tiles above do not say — and, behind a disclosure, what the redone work actually was.
 *
 * **The sentence carries no figure at all, deliberately.** Every number this component could print is
 * already a reading on a tile one line higher, and printing them a second time is what got this report
 * filed as a bug: the clean-finish rate is taken over the judged requests alone while the scan looks at
 * the whole population, so the two fractions have different denominators, and met as adjacent clauses
 * in one sentence they read as a mismatch to debug. Both denominators are right and both are still on
 * screen — on the tiles, where a fraction is a reading rather than prose. What is left for prose is the
 * part no figure carries: what the two readings do not mean together, and whether any of it is the
 * reader's to act on.
 *
 * It reads the SHARED bundle (`scorecard`, `waste`), never the reader's own, for the reason the tiles
 * do: a per-actor bundle exists only for someone who had a session in the harvest window, so a figure
 * taken from one is absent for most readers most days.
 *
 * The line only appears when its own source can be read, and the scorecard is read for one thing
 * only — whether anything in this report was judged at all, which is what decides whether "finished
 * cleanly" has a reading here to contrast with. The two sources are separate keys that can arrive in
 * either state, so "the scorecard is here and the scan is not" is a real payload.
 *
 * **The row labels come off the wire, not from here.** `waste.byKind.<kind>.plainSummary` is written
 * in the producer from a closed table; a kind that carries none is LEFT OUT rather than shown by its
 * key, because the keys are engine identifiers (`repeated_sequence`) and reach a reader as one. The
 * field is optional and the SPA and the producer deploy independently, so that omission is a state
 * that really occurs rather than a defensive branch.
 *
 * Rows are ordered by TIME LOST, which is not cosmetic: the kind with the most findings can be the
 * cheapest one by two orders of magnitude, so ordering by count would lead the table with noise.
 */
import React from 'react';
import { numericStyle, theme } from '../../styles/tokens';
import {
  type Measured, type PopulationCounts, type Scorecard, type WasteKindTotals, type WasteTotals,
} from '../../improve-api';
import {
  MeasuredValue, SentinelValue, formatExtent, metaTextStyle, pluralise,
} from './improveChrome';

/** A count that could really have been counted — see `OutcomeBand`'s copy for why a figure is
 *  checked at all when the type says it is a number. */
function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/**
 * How a figure is set inside PROSE, as against inside the table below it.
 *
 * Weight, and deliberately not `numericStyle`. That token is mono plus `tabular-nums`, and it earns
 * both in a tile and in a column of the impact table, where equal-width digits are what make one row
 * line up under the next. Nothing in a flowing line is in a column, so the tabular figures buy
 * nothing and the mono face costs something real: a run of one face inside a line set in another
 * reads as a rendering fault, which is what it was taken for when a whole phrase — `16 of 23`, the
 * word `of` included — was set this way.
 *
 * Semibold rather than nothing at all, so the count is still the part of the line a reader's eye
 * lands on first.
 *
 * The only figure left in prose here is the count of unnamed kinds below; the sentence itself carries
 * none. The body family is NAMED rather than left to inherit, so the choice is visible where it is
 * made and a test can read it — an inherited face and a chosen one look identical on screen, and only
 * one of them survives being moved into a context that sets mono.
 */
const figureStyle: React.CSSProperties = {
  fontFamily: theme.typography.fontFamily,
  fontWeight: theme.typography.weights.semibold,
};

interface ImpactRow {
  /** The producer's key. Kept for a stable React key and a testid; never rendered. */
  kind: string;
  /** The producer's sentence for the kind, verbatim. */
  summary: string;
  /** Wall time charged to this kind, or the sentinel when no contributing span was timed.
   *  `undefined` when the row carries no total at all. */
  wallMs: Measured<number> | undefined;
  calls: unknown;
}

/** The figure a row is ranked on, or `null` when it has none. */
function rankable(row: ImpactRow): number | null {
  return typeof row.wallMs === 'number' && Number.isFinite(row.wallMs) && row.wallMs >= 0
    ? row.wallMs : null;
}

/**
 * The kinds that can be named, biggest first.
 *
 * A kind with nothing to report is left out: the producer emits every kind it HAS, with zeros when it
 * found nothing, and three zero rows under a column headed "What happened" would claim three things
 * happened. A kind whose finding count is unreadable is kept, because "cannot tell" must not become
 * "none".
 *
 * An untimed row sorts after every timed one AND shows the hatched sentinel in its time cell, so its
 * position is visibly not a claim that it was the smallest. Sorting it in as a zero would be that
 * claim.
 */
function impactRows(byKind: unknown): { rows: ImpactRow[]; unnamed: number } {
  if (typeof byKind !== 'object' || byKind === null) return { rows: [], unnamed: 0 };
  const rows: ImpactRow[] = [];
  let unnamed = 0;
  for (const [kind, value] of Object.entries(byKind as Record<string, unknown>)) {
    if (typeof value !== 'object' || value === null) continue;
    const totals = value as Partial<WasteKindTotals>;
    if (isCount(totals.findings) && totals.findings === 0) continue;
    if (typeof totals.plainSummary !== 'string' || totals.plainSummary.trim() === '') {
      unnamed += 1;
      continue;
    }
    rows.push({
      kind,
      summary: totals.plainSummary,
      wallMs: totals.wallMs?.sum,
      calls: totals.calls,
    });
  }
  rows.sort((a, b) => {
    const left = rankable(a);
    const right = rankable(b);
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    return right - left;
  });
  return { rows, unnamed };
}

/** The scan's counts, once they can be read. `wallMs` is `undefined` when the block carries no totals
 *  at all, and the producer's sentinel when it carries totals nothing timed. */
interface Redone {
  runsWithWaste: number;
  wallMs: Measured<number> | undefined;
}

/**
 * The one sentence, or nothing at all.
 *
 * Four states and one wording each, and not one of them prints a figure — see this file's opening
 * note for what that dissolves.
 *
 *  - **nothing readable about the scan.** No sentence. The tile above states that absence in its own
 *    words, and a second telling of it is what this surface's budget exists to refuse.
 *  - **the scan looked and found nothing.** Said, because the tile cannot say it: at zero the tile
 *    shows either a duration, which reads as instant, or the hatched sentinel, which reads as unknown.
 *    Neither of them reads as "we looked".
 *  - **findings, and no totals block behind them.** The finding itself, because nothing else on the
 *    surface reports one — `OutcomeBand` needs `totalWallMs` to render that tile at all, so with the
 *    block missing the band shows an absence and no count. No duration is claimed and none is
 *    invented; no count either, so the line has no plural to get wrong. It is the TOTAL that is
 *    missing, and said that way: a kind's own `wallMs` is a separate term, so the table below can
 *    carry times for the rows while no figure sums them.
 *  - **findings, with a totals block.** The thing two tiles side by side invite a reader to get
 *    wrong. The requests that redid work are not the requests that failed: the producer's scan is
 *    handed the runs and never the verdicts (`find_waste(runs)` in `improve/waste.py` — a verdict is
 *    not in scope there, so no finding of its can depend on one), and a request that repeated a step
 *    can still be judged a clean finish. Said only where something WAS judged, because with no
 *    verdict in the report "finished cleanly" has no reading here to be contrasted with, and the tile
 *    beside it says as much.
 */
function summaryLine(redone: Redone | undefined, judgedSomething: boolean): string | undefined {
  if (redone === undefined) return undefined;
  if (redone.runsWithWaste === 0) return 'Nothing was redone or abandoned.';
  if (redone.wallMs === undefined) {
    return 'Work was redone or abandoned, and nothing recorded how long all of it took.';
  }
  return judgedSomething ? 'A request can redo work and still finish cleanly.' : undefined;
}

export function ImpactSummary({ scorecard, waste }: {
  /** The GLOBAL scorecard, never the actor's. Read for one bit only — see `judgedSomething`. */
  scorecard?: Scorecard;
  /** The GLOBAL waste totals, never the actor's. */
  waste?: WasteTotals;
}) {
  const [open, setOpen] = React.useState(false);

  // Read wider than the types declare: stored bundles are served back unchanged and really do omit
  // keys these interfaces mark as required.
  const counts = scorecard?.population as PopulationCounts | undefined;
  const pass: unknown = counts?.pass;
  const fail: unknown = counts?.fail;
  /*
   * Whether this report judged ANYTHING, which is the whole of what the scorecard is read for here.
   *
   * The JUDGED set, not the population: an unscored run says something about the evidence and nothing
   * about the agent, so `passRate` is taken over pass + fail alone and so is this. A judged set of
   * zero is not a judged set — `0 of 0` is nothing to contrast a scan against, and the tile above
   * says separately that nothing was judged.
   *
   * The rate itself is deliberately not read. This decides whether a clean finish is a thing this
   * report has a reading for, which a scorecard carrying counts and no rate still answers.
   */
  const judged = isCount(pass) && isCount(fail) ? pass + fail : undefined;
  const judgedSomething = judged !== undefined && judged > 0;

  const runsWithWaste: unknown = waste?.runsWithWaste;
  const scanned: unknown = waste?.runs;
  const totalWallMs = waste?.totalWallMs;
  const totalCalls: unknown = waste?.totalCalls;
  /*
   * The population has to be readable even though no figure here is taken from it: `runsWithWaste`
   * on its own is not a count of anything — it is meaningless without what it was counted over, which
   * is the producer's own rule for this block — and the tile above is where that fraction is printed.
   * So it gates the line and does not appear in it.
   */
  const redone: Redone | undefined = isCount(runsWithWaste) && isCount(scanned)
    ? { runsWithWaste, wallMs: totalWallMs?.sum }
    : undefined;

  const { rows, unnamed } = impactRows(waste?.byKind);
  const line = summaryLine(redone, judgedSomething);

  /** With nothing lost there is nothing to attribute, so the second sentence has no subject. */
  const blameable = redone !== undefined && redone.runsWithWaste > 0;

  /*
   * Nothing to say and nothing to open. The table is kept on its own terms rather than on the
   * sentence's: a scan whose counts cannot be read can still name the kinds it found, and that table
   * is the only place on this surface those appear.
   */
  if (line === undefined && rows.length === 0) return null;

  return (
    <div data-testid="impact-summary" style={metaTextStyle}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 0, color: theme.colors.textPrimary }}>
          {line !== undefined && <span data-testid="impact-sentence">{line}</span>}
          {/* The gap between sentences sits BETWEEN the spans, not inside one. Each span is exactly
              its own sentence, so a test — and a reader counting the prose on this surface — can hold
              one of them without the join. */}
          {line !== undefined && blameable && ' '}
          {/*
            What the reader wants to know, which is whether any of this is theirs to do something
            about. It is not: the efficiency path names no owner for a finding, so nothing it reports
            can arrive as a task for whoever is reading. `blameable` is the gate because a report with
            nothing lost has nothing to answer for in the first place.

            The claim rests entirely on that lack of attribution. The day a finding can be traced to
            something the reader publishes and owns, this line becomes false and has to change with
            it — and it will fail silently, because nothing about a new attribution path makes a
            sentence stop rendering.
          */}
          {blameable && <span data-testid="impact-blame">Nothing here for you to fix.</span>}
        </div>
        {rows.length > 0 && (
          <button
            type="button"
            data-testid="impact-details-toggle"
            aria-expanded={open}
            onClick={() => setOpen((was) => !was)}
            style={toggleStyle}
          >
            {open ? 'Details ▴' : 'Details ▾'}
          </button>
        )}
      </div>

      {open && rows.length > 0 && (
        <table data-testid="impact-table" style={tableStyle}>
          <thead>
            <tr>
              <th scope="col" style={{ ...headCellStyle, textAlign: 'left' }}>What happened</th>
              <th scope="col" style={headCellStyle}>Time lost</th>
              <th scope="col" style={headCellStyle}>Calls</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.kind} data-testid={`impact-row-${row.kind}`}>
                {/* Wraps. The producer's sentences run to sixty characters and clipping one would
                    lose the only words on this surface that say what the row IS. */}
                <th scope="row" style={rowHeadStyle}>{row.summary}</th>
                <td style={numberCellStyle}>
                  {row.wallMs === undefined
                    ? <SentinelValue value="UNMEASURED" />
                    : <MeasuredValue value={row.wallMs} format={formatExtent} />}
                </td>
                <td style={numberCellStyle}>
                  {isCount(row.calls) ? row.calls : <SentinelValue value="UNKNOWN" />}
                </td>
              </tr>
            ))}
          </tbody>
          {redone !== undefined && (
            <tfoot>
              <tr data-testid="impact-total">
                <th scope="row" style={{ ...rowHeadStyle, color: theme.colors.textSecondary }}>
                  All of it
                </th>
                <td style={numberCellStyle}>
                  {redone.wallMs === undefined
                    ? <SentinelValue value="UNMEASURED" />
                    : <MeasuredValue value={redone.wallMs} format={formatExtent} />}
                </td>
                <td
                  style={numberCellStyle}
                  // A call two kinds both charged is counted once here and once in each of their
                  // rows, so the rows can add up to more than this. Said on the total rather than
                  // as a line of prose under the table.
                  title={'Distinct calls. A call that two kinds both charged is counted once here, '
                    + 'so the rows above can add up to more.'}
                >
                  {isCount(totalCalls) ? totalCalls : <SentinelValue value="UNKNOWN" />}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      )}

      {/* Stated, never silently dropped: a table short of a kind is a table whose rows do not add
          up to the total above it, and the reader is owed the reason. */}
      {open && unnamed > 0 && (
        <div data-testid="impact-unnamed" style={unnamedStyle}>
          {/* `figureStyle`, not the numeric spine: this is a sentence, and the count sits in the
              middle of it rather than in the column below. A bare numeral in mono is a milder version
              of the same fault the sentence above was fixed for, and one spelling of "a figure in
              prose" per file is what keeps the two from drifting. */}
          <span style={figureStyle}>{unnamed}</span>
          {` more ${pluralise(unnamed, 'kind')} of redone work `}
          {unnamed === 1 ? 'has' : 'have'}
          {' no plain-English name in this report yet, so '}
          {unnamed === 1 ? 'it is' : 'they are'}
          {' left out of the table above rather than shown by an internal name.'}
        </div>
      )}
    </div>
  );
}

// --- styles ----------------------------------------------------------------
const toggleStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  padding: 0,
  border: 'none',
  background: 'none',
  color: theme.colors.textSecondary,
  textDecoration: 'underline',
  textUnderlineOffset: 3,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
};

const tableStyle: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  marginTop: 8,
  fontSize: theme.typography.sizes.meta,
};

const headCellStyle: React.CSSProperties = {
  textAlign: 'right',
  fontWeight: theme.typography.weights.regular,
  color: theme.colors.textTertiary,
  borderBottom: `1px solid ${theme.colors.border}`,
  padding: '3px 0 4px',
  whiteSpace: 'nowrap',
};

const rowHeadStyle: React.CSSProperties = {
  textAlign: 'left',
  fontWeight: theme.typography.weights.regular,
  color: theme.colors.textPrimary,
  padding: '4px 10px 4px 0',
  lineHeight: theme.typography.lineHeight,
};

/** Right-aligned and in the numeric spine: these are the one place on this surface where figures
 *  line up down a column, which is what `numericStyle`'s tabular digits exist for. */
const numberCellStyle: React.CSSProperties = {
  ...numericStyle,
  textAlign: 'right',
  padding: '4px 0 4px 10px',
  whiteSpace: 'nowrap',
};

const unnamedStyle: React.CSSProperties = {
  marginTop: 5,
  color: theme.colors.textTertiary,
  lineHeight: theme.typography.lineHeight,
};

export default ImpactSummary;
