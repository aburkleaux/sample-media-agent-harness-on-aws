/**
 * The reader's OWN runs, and why each one failed: `ActorBundle.runs`.
 *
 * Reads that bundle and nothing else. The rows carry session ids and the judge's sentence about one
 * tenant's request, so they exist ONLY on the reader's own item and this block must never be handed
 * the shared `__system__` bundle — its prop is an `ActorBundle` for that reason, and the shared
 * scorecard's counterpart (`QualityBlock`) deliberately has no per-run list at all.
 *
 * It is the only place on this surface where a reader can find out WHY one of their requests failed.
 * `TierScore.explanation` is the judge's own sentence, written for a person, and is the one piece of
 * producer prose here shown as written; everything else the row carries is an identifier and is
 * phrased in `verdictWording`.
 *
 * Four absences reach this component and each says which one it is:
 *
 *   - no actor bundle: the producer harvests a bounded window and writes a bundle only for an actor
 *     who had a session in it, so most readers on most days have none.
 *   - a bundle with no `runs` key: written before the per-run records existed. Nobody looked.
 *   - `runs: []` with `runsTotal: 0`: the producer swept this actor and found no run. A measurement.
 *   - `runs: []` with a positive `runsTotal`: every row was dropped to fit the item. Not "no runs".
 *
 * FAILURES FIRST, then the runs nothing could judge, then the passes behind a toggle. That ordering
 * is the design and not a preference: a reader opens this to find out what went wrong, and the live
 * bundle is 5 failures among 13 runs — a chronological list buries them among passes that need no
 * reading. Nothing is dropped: the passes keep their count and their rows one click away.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';
import {
  isSentinel,
  type ActorBundle, type ActorRun, type Measured, type Overall, type RunWindow, type TierScore,
} from '../../improve-api';
import {
  MeasuredValue, compactNumber, formatWindow, fractionLabel, metaTextStyle, pluralise,
} from './improveChrome';
import { formatUsd } from './metricRegistry';
import {
  VERDICT, checkLabel, classLabel, explainVerdict, formatScore, outcomeWord, verdictWord,
} from './verdictWording';

/** Reading order for the verdicts. Grouped rather than sorted: the three answer different
 *  questions and are not three points on one scale. */
const VERDICT_ORDER: readonly Overall[] = ['FAIL', 'UNSCORED', 'PASS'];

/**
 * A UUID cut to its first block, anything else left whole.
 *
 * Same rule as the Inbox's diverted rows: a fabricated or prefixed id is not a UUID, so truncating
 * one to 8 characters would hide what it is.
 */
function shortRunId(runId: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(runId) ? runId.slice(0, 8) : runId;
}

/**
 * The judge's sentence about a run.
 *
 * On every run in the live bundle the two tiers carry the SAME sentence — the judge is asked for one
 * sentence grading the session and it is recorded against both tiers — so printing it per tier would
 * print the same 200-character paragraph twice on every row. It is printed once when they agree and
 * labelled per tier when they do not, rather than one of the two being picked and the difference
 * silently discarded.
 *
 * An empty string is the producer recording no sentence, which is said rather than rendered as a
 * blank line.
 */
function JudgeNote({ run, testId }: { run: ActorRun; testId: string }) {
  const dispatch = run.tiers?.dispatch?.explanation ?? '';
  const delivery = run.tiers?.delivery?.explanation ?? '';
  if (!dispatch && !delivery) {
    return (
      <div data-testid={`${testId}-note-absent`} style={{ color: theme.colors.textTertiary }}>
        The judge recorded no sentence about this run.
      </div>
    );
  }
  if (dispatch === delivery) {
    return (
      <div data-testid={`${testId}-note`} title="The judge’s own sentence about this session.">
        {dispatch}
      </div>
    );
  }
  return (
    <div data-testid={`${testId}-note`} title="The judge’s own sentence about this session.">
      <div>{`${checkLabel('dispatch_correct')}: ${dispatch || '(no sentence)'}`}</div>
      <div>{`${checkLabel('results_delivered')}: ${delivery || '(no sentence)'}`}</div>
    </div>
  );
}

/**
 * The two judged tiers as scores with their outcomes.
 *
 * The outcome is not inferable from the score, which is why both are here: on the live bundle a
 * delivery score of 0.9 is met and one of 0.6 is not, and the threshold is nowhere on the wire. A
 * score of `0` is a measurement and is set as `0.00`; an unmeasured one keeps the surface's hatched
 * sentinel.
 */
function TierScores({ run }: { run: ActorRun }) {
  const tiers: ActorRun['tiers'] | undefined = run.tiers;
  if (tiers === undefined) {
    return (
      <span style={{ color: theme.colors.textTertiary }}>no judged scores on this run</span>
    );
  }
  // Widened to optional for the reason the bundle's own keys are: a stored item can predate a key,
  // and the declaration would have this code walk into an absent tier.
  const scored: { name: string; tier: TierScore | undefined }[] = [
    { name: 'dispatch_correct', tier: tiers.dispatch },
    { name: 'results_delivered', tier: tiers.delivery },
  ];
  return (
    <>
      {scored.map(({ name, tier }, index) => (
        <span key={name}>
          {index > 0 && ' · '}
          {tier === undefined ? (
            <span style={{ color: theme.colors.textTertiary }}>
              {`${checkLabel(name)}: not scored`}
            </span>
          ) : (
            <>
              {`${checkLabel(name)} `}
              <MeasuredValue value={tier.score} format={formatScore} />
              {` ${outcomeWord(tier.outcome)}`}
            </>
          )}
        </span>
      ))}
    </>
  );
}

/**
 * What the run spent, beside whether it worked.
 *
 * The whole reason a spend figure belongs on this row: `$4.70` means something different on a request
 * that failed to deliver anything. Absent terms keep the sentinel — a run that recorded no usage is
 * not a free run.
 */
function RunSpend({ run }: { run: ActorRun }) {
  const spend: ActorRun['spend'] | undefined = run.spend;
  if (spend === undefined) {
    return <span style={{ color: theme.colors.textTertiary }}>nothing recorded what this spent</span>;
  }
  return (
    <>
      <MeasuredValue value={spend.tokens} format={compactNumber} />
      {' tokens · '}
      <MeasuredValue value={spend.cost} format={formatUsd} />
    </>
  );
}

/**
 * One run: verdict, id, tier scores, the judge's sentence, and why the verdict is what it is.
 *
 * `verdict.reason` is structural (`"gating: results_delivered:FAIL"`), so it is translated rather
 * than shown. When the string does not match the producer's grammar the row says the report gives no
 * reader-facing reason and keeps the raw string on hover — printing it would put the engine's
 * identifiers on screen, which is what the translation exists to prevent.
 */
function RunRow({ run, colour, testId, showClass }: {
  run: ActorRun;
  colour: string;
  testId: string;
  /** True when the listed runs are NOT all one workload class, in which case each row has to name
   *  its own — a verdict cannot be read without it, since gating is per class. */
  showClass: boolean;
}) {
  const overall = run.verdict?.overall;
  const reason = run.verdict?.reason;
  const explained = explainVerdict(typeof reason === 'string' ? reason : '');
  const runWindow: Measured<RunWindow> | undefined = run.window;
  return (
    <div data-testid={testId} style={rowStyle}>
      <div style={headLineStyle}>
        <span
          data-testid={`${testId}-verdict`}
          title={VERDICT[overall ?? '']?.title
            ?? 'This report records no verdict for this run, which is not a pass.'}
          style={{
            fontSize: theme.typography.sizes.badge,
            color: colour,
            background: tint(colour, 12),
            border: `1px solid ${tint(colour, 30)}`,
            borderRadius: theme.radius.badge,
            padding: '1px 6px',
            whiteSpace: 'nowrap',
            cursor: 'help',
          }}
        >
          {overall === undefined ? 'no verdict recorded' : verdictWord(overall)}
        </span>
        <code style={{ ...numericStyle, color: theme.colors.textSecondary }}>
          {typeof run.runId === 'string' && run.runId ? shortRunId(run.runId) : 'no run id'}
        </code>
        {showClass && (
          <span style={{ color: theme.colors.textTertiary }}>
            {typeof run.workloadClass === 'string' && run.workloadClass
              ? classLabel(run.workloadClass)
              : 'kind of work not recorded'}
          </span>
        )}
        <span style={{ marginLeft: 'auto', textAlign: 'right' }}>
          <TierScores run={run} />
        </span>
      </div>

      <JudgeNote run={run} testId={testId} />

      {explained.sentence !== null && (
        <div data-testid={`${testId}-reason`} style={{ color: theme.colors.textSecondary }}>
          {explained.sentence}
        </div>
      )}
      {!explained.recognised && (
        <div
          data-testid={`${testId}-reason-unreadable`}
          title={typeof reason === 'string' && reason ? reason : undefined}
          style={{ color: theme.colors.textTertiary }}
        >
          This report gives no reason for the verdict that can be put in words here.
        </div>
      )}
      {explained.partial.length > 0 && (
        <div data-testid={`${testId}-partial`} style={{ color: theme.colors.warning }}>
          {`Established on partial evidence: nothing measured whether `
            + `${explained.partial.join('; ')}.`}
        </div>
      )}
      {explained.informational.length > 0 && (
        <div data-testid={`${testId}-informational`} style={{ color: theme.colors.textTertiary }}>
          {'Recorded, and did not decide this verdict: '}
          {explained.informational
            .map((check) => `${checkLabel(check.name)} — ${outcomeWord(check.outcome)}`)
            .join('; ')}
          {'.'}
        </div>
      )}

      <div data-testid={`${testId}-spend`} style={{ color: theme.colors.textTertiary }}>
        <span style={numericStyle}>
          {runWindow === undefined ? 'no window recorded' : formatWindow(runWindow)}
        </span>
        {' · '}
        <RunSpend run={run} />
      </div>
    </div>
  );
}

/** A stated absence, never a hidden section: a region that vanishes reads as a render failure. Same
 *  treatment as the waste band's, so the two read as one register. */
function Absence({ testId, children }: { testId: string; children: React.ReactNode }) {
  return (
    <div data-testid={testId} style={{ ...metaTextStyle, color: theme.colors.textSecondary }}>
      {children}
    </div>
  );
}

/**
 * How the runs partition by verdict, keeping any verdict this screen does not model.
 *
 * A value outside `Overall` can only come off the wire, and it must not be folded into one of the
 * three — filing an unrecognised verdict under "not judged" would assert something nobody measured.
 * It gets its own group, named as unrecognised.
 */
function partition(runs: readonly ActorRun[]): { known: Map<Overall, ActorRun[]>; other: ActorRun[] } {
  const known = new Map<Overall, ActorRun[]>(VERDICT_ORDER.map((overall) => [overall, []]));
  const other: ActorRun[] = [];
  for (const run of runs) {
    const bucket = known.get(run.verdict?.overall as Overall);
    if (bucket) bucket.push(run);
    else other.push(run);
  }
  return { known, other };
}

export function RunsBlock({ actor }: {
  /** The reader's OWN bundle, absent for most actors on most days. Never the shared bundle: see this
   *  file's opening note. */
  actor?: ActorBundle;
}) {
  const [passesOpen, setPassesOpen] = React.useState(false);
  /*
   * Read as optional. `ActorBundle` declares `runs` and `runsTotal` required, and that holds for
   * every bundle today's producer writes, but an item stored before a key existed is served back
   * unchanged — so `?? 0` on the total would report a trim that nobody measured, and `?? []` on the
   * rows would report a swept actor with no runs.
   */
  const runs: ActorRun[] | undefined = actor?.runs;
  const runsTotal: number | undefined = actor?.runsTotal;
  const groups = partition(runs ?? []);
  const failures = groups.known.get('FAIL') ?? [];
  const unjudged = groups.known.get('UNSCORED') ?? [];
  const passes = groups.known.get('PASS') ?? [];
  // One class across every listed run is the live case and probably the permanent one — only
  // conversational runs can have an owner at all — so it is stated once in the header instead of
  // repeated down thirteen rows. A mixed bundle names it per row, where it is the difference
  // between two things being counted together.
  const classes = new Set((runs ?? []).map((run) => run.workloadClass));
  const showClass = classes.size !== 1;

  return (
    <section data-testid="runs-block" style={metaTextStyle}>
      <h3 style={headingStyle}>Your runs, and what the judge said</h3>

      {actor === undefined ? (
        <Absence testId="runs-no-bundle">
          {/* Two things this deliberately does not say. Not "none of your runs was judged" — they
              may well have been, and be counted in the population further down. And not the waste
              band's opening clause, which states the same cause: three bands on this surface report
              the missing personal bundle, and each has to be worth reading rather than the same
              sentence three times. */}
          None of your runs is in this report. The producer writes a bundle of your own only for an
          actor who had a session in the harvest window.
        </Absence>
      ) : runs === undefined ? (
        <Absence testId="runs-absent">
          This bundle predates the per-run records, so how your runs landed was not recorded. Not a
          clean result.
        </Absence>
      ) : runs.length === 0 ? (
        <Absence testId="runs-none">
          {typeof runsTotal === 'number' && runsTotal > 0 ? (
            <>
              {'All '}
              <span style={numericStyle}>{runsTotal}</span>
              {` of your ${pluralise(runsTotal, 'run')} were dropped to fit this report’s size `
                + 'limit, so none is listed. Not "no runs".'}
            </>
          ) : (
            'The producer swept your runs and found none in this window.'
          )}
        </Absence>
      ) : (
        <>
          <div data-testid="runs-total" style={{ marginBottom: 6 }}>
            <span style={numericStyle}>{runs.length}</span>
            {/* `N runs of yours` rather than `N of your runs`, which reads `1 of your run` on the
                single-run bundle that is the common case for most actors. */}
            {` ${pluralise(runs.length, 'run')} of yours · `}
            <span style={numericStyle}>{failures.length}</span>
            {' failed · '}
            <span style={numericStyle}>{passes.length}</span>
            {' passed'}
            {/* Stated only when it happened, and never folded into either of the two above it:
                could-not-measure is neither a pass nor a failure. */}
            {unjudged.length > 0 && (
              <>
                {' · '}
                <span style={numericStyle}>{unjudged.length}</span>
                {' could not be judged'}
              </>
            )}
            {/* Without this the three counts above would silently fail to add up to the total. */}
            {groups.other.length > 0 && (
              <>
                {' · '}
                <span style={numericStyle}>{groups.other.length}</span>
                {' with a verdict this screen cannot read'}
              </>
            )}
            {/* Stated only when it IS a shortfall: `13 of 13` restates the count beside it, while
                `10 of 13` says three of this reader's runs are missing from everything below. */}
            {typeof runsTotal === 'number' && runs.length < runsTotal && (
              <>
                {' · '}
                <span style={numericStyle}>{fractionLabel(runs.length, runsTotal)}</span>
                {' listed, the rest dropped to fit this report’s size limit'}
              </>
            )}
            {!showClass && (
              <>
                {' · all of one kind: '}
                {classLabel([...classes][0] ?? '')}
              </>
            )}
          </div>

          {failures.map((run, index) => (
            <RunRow
              key={`${run.runId}-${index}`}
              run={run}
              colour={theme.colors.error}
              testId={`runs-row-${index}`}
              showClass={showClass}
            />
          ))}

          {unjudged.length > 0 && (
            <div data-testid="runs-unjudged" style={{ marginTop: 6 }}>
              <div style={{ color: theme.colors.warning }}>
                <span style={numericStyle}>{unjudged.length}</span>
                {` ${pluralise(unjudged.length, 'run')} could not be judged. That is a gap in the `
                  + 'evidence, not a verdict on the work.'}
              </div>
              {unjudged.map((run, index) => (
                <RunRow
                  key={`${run.runId}-${index}`}
                  run={run}
                  colour={theme.colors.warning}
                  testId={`runs-unjudged-row-${index}`}
                  showClass={showClass}
                />
              ))}
            </div>
          )}

          {groups.other.length > 0 && (
            <div data-testid="runs-other" style={{ marginTop: 6 }}>
              <div style={{ color: theme.colors.warning }}>
                <span style={numericStyle}>{groups.other.length}</span>
                {` ${pluralise(groups.other.length, 'run')} carry a verdict this screen has no `
                  + 'phrase for, so what happened to them is not stated here.'}
              </div>
              {groups.other.map((run, index) => (
                <RunRow
                  key={`${run.runId}-${index}`}
                  run={run}
                  colour={theme.colors.textTertiary}
                  testId={`runs-other-row-${index}`}
                  showClass={showClass}
                />
              ))}
            </div>
          )}

          {passes.length > 0 && (
            <div data-testid="runs-passed" style={{ marginTop: 6 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                <span style={{ color: theme.colors.textTertiary }}>
                  <span style={numericStyle}>{passes.length}</span>
                  {` ${pluralise(passes.length, 'run')} passed every check that decides the `
                    + 'verdict'}
                </span>
                <button
                  type="button"
                  onClick={() => setPassesOpen((open) => !open)}
                  aria-expanded={passesOpen}
                  data-testid="runs-passed-toggle"
                  style={toggleStyle}
                >
                  {passesOpen ? 'hide' : 'show'}
                </button>
              </div>
              {passesOpen && passes.map((run, index) => (
                <RunRow
                  key={`${run.runId}-${index}`}
                  run={run}
                  colour={theme.colors.success}
                  testId={`runs-passed-row-${index}`}
                  showClass={showClass}
                />
              ))}
            </div>
          )}

          {/* Only where there IS a price to caveat. A run whose cost is the sentinel was never
              priced, and a note about estimated prices over rows that show none would claim a
              figure nobody computed. The rates' own provenance travels on the shared scorecard and
              nowhere on this bundle, so this says what a reader must not forget — the figure is an
              estimate — and claims nothing further. */}
          {runs.some((run) => !isSentinel(run.spend?.cost) && run.spend?.cost !== undefined) && (
            <div
              data-testid="runs-price-note"
              style={{ marginTop: 8, color: theme.colors.textTertiary }}
            >
              Prices are estimated per run from a rate table, not billed.
            </div>
          )}
        </>
      )}
    </section>
  );
}

/** One run is several lines, so a row is a block with a rule above it rather than a grid: the judge's
 *  sentence is a paragraph and will not sit in a column. */
const rowStyle: React.CSSProperties = {
  padding: '6px 0',
  borderTop: `1px solid ${theme.colors.border}`,
};

const headLineStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  alignItems: 'baseline',
  flexWrap: 'wrap',
  marginBottom: 2,
};

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

export default RunsBlock;
