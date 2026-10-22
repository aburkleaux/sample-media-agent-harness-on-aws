/**
 * @vitest-environment jsdom
 *
 * The 13 runs below are the REAL ones served to one actor, read out of the staging table
 * `mediaworks-harness-improve` (`actorId 68e153d0-f0b1-7072-91bc-84425b517126`, `sk SNAPSHOT`,
 * `createdAt 2026-09-03T08:01:59Z`) and transcribed field for field — the same bundle
 * `WasteBlock.test.tsx` takes its findings from. Every run id, verdict, reason, score, judge
 * sentence, window bound and spend figure here is the producer's own.
 *
 * Two things the transcription compresses, both stated because they are facts about the wire:
 *
 *  - the judge's sentence is recorded against BOTH tiers, identically, on all 13 runs, so `run()`
 *    below takes it once and stamps it on both — as the producer effectively emits it.
 *  - `window.provenance` is `derived_plan_and_span_bounds` on 9 runs and `derived_span_bounds` on
 *    the other 4, and nothing in this block reads it (only `derived`, which is `true` on all 13), so
 *    the helper stamps the first of the two.
 *
 * Nothing is invented. Every case the live data does not contain — an unmeasurable tier, an UNSCORED
 * verdict, a trimmed bundle, a reason from a future producer — is built by changing NAMED fields of a
 * real row, and each test says which ones and why the rest stays put.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required. `@testing-library/user-event` is not a dependency, so this uses `fireEvent`.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RunsBlock } from './RunsBlock';
import { ImproveCenter, type ImproveData } from './ImproveCenter';
import fixture from './__fixtures__/improveData.fixture.json';
import type { ActorBundle, ActorRun, Measured, Outcome, Overall } from '../../improve-api';

vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));

/** One live row, in the order the fields appear on the wire. `workloadClass` is `conversational` on
 *  all 13 — only that class can have an owner at all, so a personal bundle holds no other. */
function run(
  runId: string,
  overall: Overall,
  reason: string,
  dispatch: [Measured<number>, Outcome],
  delivery: [Measured<number>, Outcome],
  explanation: string,
  window: { start: string; end: string },
  spend: { latencyMs: number; tokens: number; cost: number; llmCalls: number },
): ActorRun {
  return {
    runId,
    workloadClass: 'conversational',
    window: { ...window, derived: true, provenance: 'derived_plan_and_span_bounds' },
    verdict: { overall, reason },
    tiers: {
      dispatch: { score: dispatch[0], outcome: dispatch[1], explanation },
      delivery: { score: delivery[0], outcome: delivery[1], explanation },
    },
    spend,
  };
}

const LIVE_RUNS: ActorRun[] = [
  run('e05279f2-f754-4b20-99aa-77e3cae64ae5', 'FAIL',
      'gating: results_delivered:FAIL',
      [0.95, 'PASS'], [0, 'FAIL'],
      'The anime restyle plan was correctly dispatched in parallel across the 8 frames, but the '
      + "session ends with only the 'job running' announcement and never delivers the actual styled "
      + 'frame artifacts to the user.',
      { start: '2026-09-03T04:58:05.618000+00:00', end: '2026-09-03T05:51:50.585000+00:00' },
      { latencyMs: 2870941, tokens: 2038135, cost: 4.701307, llmCalls: 51 }),
  run('5dec2a43-10e2-433a-a47c-9a6d9cd7682f', 'PASS',
      'gating passed',
      [1, 'PASS'], [1, 'PASS'],
      'Agent correctly diagnosed the failed render step, retrieved the completed research/summary, '
      + 'regenerated and uploaded a working PDF, and delivered a clear cited summary.',
      { start: '2026-09-03T04:57:22.160000+00:00', end: '2026-09-03T05:18:26.239000+00:00' },
      { latencyMs: 3010815, tokens: 434169, cost: 1.09353675, llmCalls: 13 }),
  run('97a703bd-c296-4893-b8ed-7e250001b091', 'FAIL',
      'gating: results_delivered:FAIL',
      [0.85, 'PASS'], [0, 'FAIL'],
      'The agent correctly interpreted the multi-step animate+crossfade request and dispatched a '
      + 'tracked plan, but the DAG shows animate_b failed and the stitch was skipped, so no final '
      + "video was delivered despite the confident 'on its way' message.",
      { start: '2026-09-03T04:51:56.310000+00:00', end: '2026-09-03T05:03:31.587000+00:00' },
      { latencyMs: 1777818, tokens: 477761, cost: 1.2032677500000002, llmCalls: 16 }),
  run('8f70329b-b5d4-4781-bb56-31714fa08c04', 'PASS',
      'gating passed',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly built and delivered a playable keyboard-controlled Snake game artifact '
      + 'as requested.',
      { start: '2026-09-03T03:40:57.545000+00:00', end: '2026-09-03T03:42:39.463000+00:00' },
      { latencyMs: 228360, tokens: 128828, cost: 0.24789275, llmCalls: 4 }),
  run('3e6f1fd7-676f-4389-a2b6-f22ae2307912', 'PASS',
      'gating passed (informational: AsyncJobFlowValidator:FAIL)',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly diagnosed the failed PDF node, reused the successful research, '
      + 'regenerated the PDF, and delivered a working download link with summary and citations.',
      { start: '2026-09-03T03:33:32.922000+00:00', end: '2026-09-03T03:49:43.466000+00:00' },
      { latencyMs: 1689365, tokens: 420868, cost: 1.1588847500000001, llmCalls: 12 }),
  run('b1effa60-a043-43fc-b484-5538283c26c9', 'PASS',
      'gating passed',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly identified the multi-step animate+stitch workflow, executed it fully, '
      + 'and the final summary accurately reported the completed, delivered crossfade video without '
      + 're-fetching artifacts as instructed.',
      { start: '2026-09-03T03:33:01.451000+00:00', end: '2026-09-03T03:49:26.208000+00:00' },
      { latencyMs: 1064020, tokens: 465481, cost: 1.3220525, llmCalls: 14 }),
  run('7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', 'FAIL',
      'gating: results_delivered:FAIL',
      [0.9, 'PASS'], [0.6, 'FAIL'],
      'The agent correctly generated the requested images and camera angles, but for the final '
      + 'query about the 52 steps it only explained the inefficiency rather than fixing it or '
      + 'confirming the deliverables were still intact without issue.',
      { start: '2026-09-02T21:47:56.747000+00:00', end: '2026-09-02T21:54:37.879000+00:00' },
      { latencyMs: 1893106, tokens: 532018, cost: 1.109115, llmCalls: 24 }),
  run('35634de3-6a8c-4e31-9913-b305e1913129', 'FAIL',
      'gating: dispatch_correct:FAIL; results_delivered:FAIL',
      [0.55, 'FAIL'], [0, 'FAIL'],
      'The agent used appropriate tools to fetch plan results but misreported the '
      + 'render_pdf_report node as failed even though the DAG shows it completed, and ultimately '
      + 'delivered no PDF artifact as requested.',
      { start: '2026-09-02T21:45:48.541000+00:00', end: '2026-09-02T21:54:25.694000+00:00' },
      { latencyMs: 428453, tokens: 160829, cost: 0.32196400000000003, llmCalls: 5 }),
  run('cc823dba-ce6c-454c-9997-1ce8c8576472', 'PASS',
      'gating passed (informational: DagCompletionValidator:NO_DATA)',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly used the media-references skill and image generation tools to produce '
      + 'two wolf-howling images and delivered working links to the user.',
      { start: '2026-09-02T21:45:12.947000+00:00', end: '2026-09-02T21:47:26.779000+00:00' },
      { latencyMs: 452391, tokens: 156803, cost: 0.35020975, llmCalls: 7 }),
  run('c6c64754-9f2a-4acb-b2b3-620cc0e95127', 'PASS',
      'gating passed',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly fetched and reported the audio-stem separation results with working '
      + 'CloudFront download links for speech, music, SFX, and source audio.',
      { start: '2026-09-02T20:58:12.487000+00:00', end: '2026-09-02T21:44:43.488000+00:00' },
      { latencyMs: 3729492, tokens: 2814685, cost: 6.2689544999999995, llmCalls: 59 }),
  run('5659aea2-cddb-4d31-adad-ee735dbd8cd5', 'PASS',
      'gating passed (informational: AsyncJobFlowValidator:FAIL)',
      [0.9, 'PASS'], [0.9, 'PASS'],
      'The agent correctly built and delivered a playable keyboard-controlled Space Invaders HTML '
      + 'game via artifact link with clear controls and instructions.',
      { start: '2026-09-02T20:54:14.641000+00:00', end: '2026-09-02T20:56:29.913000+00:00' },
      { latencyMs: 310787, tokens: 198115, cost: 0.31085, llmCalls: 6 }),
  run('39cc626b-acd6-489c-a4b8-f6161ef2d9fd', 'FAIL',
      'gating: results_delivered:FAIL',
      [1, 'PASS'], [0.3, 'FAIL'],
      'The agent correctly identified and dispatched the fan-out shot segmentation job with correct '
      + 'tools/params, and the DAG shows all nodes completed, but the final response only reports '
      + 'the job was kicked off without presenting or summarizing the actual segmentation results '
      + 'to the user.',
      { start: '2026-09-02T20:52:38.454000+00:00', end: '2026-09-02T21:16:52.832000+00:00' },
      { latencyMs: 871923, tokens: 1609298, cost: 2.789755, llmCalls: 20 }),
  run('6f976f3f-103e-43f2-8cf7-86cb5a4f5d89', 'PASS',
      'gating passed',
      [1, 'PASS'], [1, 'PASS'],
      'The agent correctly read the resolved plan results without re-fetching artifacts and '
      + 'delivered a concise accurate summary with the final video link.',
      { start: '2026-09-02T20:44:53.070000+00:00', end: '2026-09-02T20:53:28.700000+00:00' },
      { latencyMs: 1303867, tokens: 518387, cost: 1.16415775, llmCalls: 18 }),
];

/**
 * Everything else on that live item. `waste` and `patterns` are what they are because nothing in
 * this block reads them; the shape is what matters, and `waste` is the same block
 * `WasteBlock.test.tsx` asserts against, trimmed to its totals.
 *
 * `runs === null` OMITS the key, which is the pre-per-run-records state — `ActorBundle` declares
 * `runs` and `runsTotal` REQUIRED, so that state cannot be spelled without the cast, and that
 * mismatch between the declaration and what the wire can carry is exactly why the component reads
 * them as optional.
 */
function bundle(runs: ActorRun[] | null, runsTotal: number = LIVE_RUNS.length): ActorBundle {
  const base = {
    evidence: {},
    createdAt: '2026-09-03T08:01:59Z',
    waste: {
      runsWithWaste: 9, runs: 13, runsNotScanned: 0, totalCalls: 58,
      totalWallMs: { sum: 412828, measuredOf: 28, total: 28 }, byKind: {}, findings: [],
    },
    patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
  };
  return (runs === null ? base : { ...base, runs, runsTotal }) as unknown as ActorBundle;
}

/** A copy of the live rows with ONE row replaced. Named so every derived case can say which row it
 *  changed and leave the other twelve alone. */
function withRow(index: number, over: Partial<ActorRun>): ActorRun[] {
  return LIVE_RUNS.map((row, at) => (at === index ? { ...row, ...over } : row));
}

function text(testId: string): string {
  return screen.getByTestId(testId).textContent ?? '';
}

describe('RunsBlock, on the 13-run live bundle', () => {
  it('leads with the failures, not with the order the producer wrote them', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // The wire's order is newest first and interleaves passes; these five are its FAIL rows in that
    // order. Rendering the array as it arrives puts `5dec2a43`, a pass, in row 1.
    expect(screen.getAllByTestId(/^runs-row-\d+$/).map((row) => row.querySelector('code')?.textContent))
      .toEqual(['e05279f2', '97a703bd', '7ccb4ed1', '35634de3', '39cc626b']);
  });

  it('counts the runs, the failures and the passes, against the reader\'s own total', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    expect(text('runs-total')).toContain('13 runs of yours · 5 failed · 8 passed');
    // Every run in a personal bundle is conversational, so the kind is stated once here rather than
    // repeated down thirteen rows.
    expect(text('runs-total')).toContain('all of one kind: requests somebody made');
    // Nothing was unjudged and no verdict was unreadable, so neither clause is printed — they would
    // otherwise be two zeros in a line whose other numbers are real.
    expect(text('runs-total')).not.toContain('could not be judged');
    expect(text('runs-total')).not.toContain('cannot read');
  });

  it('says nothing about a trim when nothing was trimmed', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // `13 of 13` restates the count beside it. The clause exists for the case below, not for this one.
    expect(text('runs-total')).not.toContain('13 of 13');
    expect(text('runs-total')).not.toContain('dropped');
  });

  it('shows the judge\'s own sentence on a failing run, as the judge wrote it', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // The one piece of producer prose on this surface that is shown verbatim: it is a sentence
    // written for a person, and it is the only per-run explanation that exists anywhere.
    expect(text('runs-row-0-note')).toBe(
      'The anime restyle plan was correctly dispatched in parallel across the 8 frames, but the '
      + "session ends with only the 'job running' announcement and never delivers the actual styled "
      + 'frame artifacts to the user.');
  });

  it('prints that sentence ONCE, though the wire carries it against both tiers', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // Identical on both tiers on all 13 live runs. Rendering per tier prints the same 200-character
    // paragraph twice on every row.
    const note = text('runs-row-0-note');
    expect(note.indexOf('The anime restyle plan')).toBe(note.lastIndexOf('The anime restyle plan'));
    expect(note).not.toContain('started the right work');
  });

  it('translates the structural verdict reason instead of showing it', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    expect(text('runs-row-0-reason')).toBe('no usable result reached the user');
    // The identifiers the wire actually carries appear nowhere on screen.
    const whole = text('runs-block');
    expect(whole).not.toContain('results_delivered');
    expect(whole).not.toContain('gating:');
    expect(whole).not.toContain('FAIL');
  });

  it('names both checks on the run that failed two of them', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // `35634de3` is the only run in the harvest whose reason names two gating failures.
    expect(text('runs-row-3-reason'))
      .toBe('the wrong work was started, or none; no usable result reached the user');
  });

  it('sets each tier score with its outcome, because the threshold is on neither', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // 0.6 is a failure and 0.9 is a pass on the SAME tier in this bundle, and no threshold travels
    // on the wire — so a score without its outcome cannot be read, and vice versa.
    expect(text('runs-row-0')).toContain('started the right work 0.95 met');
    expect(text('runs-row-0')).toContain('delivered a usable result in the session 0.00 not met');
    expect(text('runs-row-2')).toContain('delivered a usable result in the session 0.60 not met');
  });

  it('sets a measured zero as a score rather than as an absence', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // The judge scored delivery at exactly 0 on this run. `0.00` is that measurement; a blank or a
    // sentinel would claim nothing measured it.
    expect(text('runs-row-0')).toContain('0.00');
    expect(screen.getByTestId('runs-row-0').querySelector('[data-testid="sentinel"]')).toBeNull();
  });

  it('states the wall clock and its reconstruction, beside what the run spent', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    // Every window in the harvest is reconstructed from span bounds, which is a caveat carried into
    // the caption rather than dropped. The spend is beside it because `$4.70` means something
    // different on a request that delivered nothing.
    expect(text('runs-row-0-spend'))
      .toBe('3 Sep 04:58 → 05:51 (53m 45s, reconstructed) · 2.0M tokens · $4.70');
  });

  it('caveats the prices as estimates, since the rate table is not a bill', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    expect(text('runs-price-note')).toContain('estimated');
    expect(text('runs-price-note')).toContain('not billed');
  });

  it('folds the passing runs behind a toggle that keeps their count', () => {
    render(<RunsBlock actor={bundle(LIVE_RUNS)} />);

    expect(text('runs-passed')).toContain('8 runs passed every check that decides the verdict');
    expect(screen.queryByTestId('runs-passed-row-0')).toBeNull();

    fireEvent.click(screen.getByTestId('runs-passed-toggle'));
    expect(screen.getAllByTestId(/^runs-passed-row-\d+$/)).toHaveLength(8);
    // And a pass carries no reason line: `gating passed` says exactly what the badge says.
    expect(screen.queryByTestId('runs-passed-row-0-reason')).toBeNull();
  });

  it('reports an informational check on a passing run without letting it read as the verdict',
     () => {
       render(<RunsBlock actor={bundle(LIVE_RUNS)} />);
       fireEvent.click(screen.getByTestId('runs-passed-toggle'));

       // `3e6f1fd7` passed gating with `AsyncJobFlowValidator:FAIL` recorded informationally. The
       // row must say the check did not decide the verdict, or it contradicts the badge above it.
       expect(text('runs-passed-row-2-informational'))
         .toBe('Recorded, and did not decide this verdict: every job it started was followed '
           + 'up — not met.');
       // The other informational form in the harvest: a check with no evidence, not a failure.
       expect(text('runs-passed-row-4-informational'))
         .toContain('every planned step finished, and after what it depends on — no evidence');
     });
});

describe('RunsBlock, on cases the live bundle does not contain', () => {
  it('hatches a tier the judge could not measure, and never scores it 0', () => {
    // ONE field pair changed on the live row `35634de3`: its delivery tier's `score` becomes the
    // `UNMEASURED` sentinel and its `outcome` becomes `NO_DATA`, which is the state
    // `TierScore`/`gate` model for missing evidence. Its verdict, dispatch tier and spend are the
    // real ones — the point is that the row still reads as a failure with one dimension unmeasured.
    const changed = withRow(7, {
      tiers: {
        dispatch: LIVE_RUNS[7].tiers.dispatch,
        delivery: { ...LIVE_RUNS[7].tiers.delivery, score: 'UNMEASURED', outcome: 'NO_DATA' },
      },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    const row = screen.getByTestId('runs-row-3');
    expect(row.querySelector('[data-testid="sentinel"]')).toHaveTextContent('not measured');
    expect(row.textContent).toContain('delivered a usable result in the session not measured '
      + 'no evidence');
    expect(row.textContent).not.toContain('0.00 no evidence');
  });

  it('counts a run nothing could judge as neither a pass nor a failure', () => {
    /*
     * TWO fields changed on the live row `35634de3`, and both are needed for the state to be
     * coherent: `verdict.overall` becomes UNSCORED and `verdict.reason` becomes the string
     * `gate()` builds on that branch (`agent/src/improve/evaluate.py`). No run in this harvest
     * reached it — every one of the 25 in the population was scored.
     */
    const changed = withRow(7, {
      verdict: {
        overall: 'UNSCORED',
        reason: 'could not measure: results_delivered:NO_DATA. Absent evidence is not a failure; '
          + 'this run is neither a defect nor a negative control',
      },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    // Out of the failures, into a group of its own — and out of the pass count too.
    expect(screen.getAllByTestId(/^runs-row-\d+$/)).toHaveLength(4);
    expect(text('runs-total')).toContain('4 failed · 8 passed · 1 could not be judged');
    expect(text('runs-unjudged')).toContain('That is a gap in the evidence, not a verdict on the '
      + 'work');
    expect(text('runs-unjudged-row-0-verdict')).toBe('not judged');
    expect(text('runs-unjudged-row-0-reason'))
      .toBe('nothing measured whether delivered a usable result in the session');
  });

  it('reads a trim as a trim and not as a smaller population', () => {
    // ONE field changed: `runsTotal` becomes 40 over the same 13 rows, which is what the producer
    // writes when a bundle had to be cut to fit one DynamoDB item.
    render(<RunsBlock actor={bundle(LIVE_RUNS, 40)} />);

    expect(text('runs-total')).toContain('13 of 40 listed, the rest dropped');
  });

  it('says nobody looked when the bundle predates the per-run records', () => {
    // The `runs` key omitted entirely — see `bundle()`. Not the same as an empty list.
    render(<RunsBlock actor={bundle(null)} />);

    expect(text('runs-absent')).toContain('predates the per-run records');
    expect(text('runs-absent')).toContain('Not a clean result');
    expect(screen.queryByTestId('runs-total')).toBeNull();
  });

  it('tells a swept actor with no runs from one whose rows were all trimmed away', () => {
    // `runs: []` is a MEASUREMENT — the producer looked and this actor had nothing in the window.
    const swept = render(<RunsBlock actor={bundle([], 0)} />);
    expect(text('runs-none')).toContain('swept your runs and found none');
    swept.unmount();

    // The same empty list over a positive total is a different fact entirely: the rows existed and
    // were dropped. Reporting it as "no runs" would be a clean bill over data nobody can see.
    render(<RunsBlock actor={bundle([], 3)} />);
    expect(text('runs-none'))
      .toContain('All 3 of your runs were dropped to fit this report’s size limit');
  });

  it('says there is no bundle of the reader\'s own, rather than showing nothing', () => {
    render(<RunsBlock />);

    expect(text('runs-no-bundle')).toContain('None of your runs is in this report');
    // Deliberately not the waste band's opening clause, which states the same cause one band below.
    expect(text('runs-no-bundle')).not.toContain('No bundle of your own in this report');
  });

  it('keeps a verdict it has no phrase for out of all three groups', () => {
    // ONE field changed on the live row `35634de3`: `verdict.overall` becomes a value outside
    // `Overall`, which only the wire can produce — a future producer adding a fourth verdict. Filing
    // it under "not judged" would assert something nobody measured.
    const changed = withRow(7, {
      verdict: { overall: 'ABANDONED' as Overall, reason: LIVE_RUNS[7].verdict.reason },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    expect(text('runs-other')).toContain('carry a verdict this screen has no phrase for');
    expect(text('runs-other-row-0-verdict'))
      .toContain('a verdict this screen has no phrase for (ABANDONED)');
    expect(text('runs-total')).toContain('1 with a verdict this screen cannot read');
  });

  it('refuses to print a reason it cannot translate, and keeps it on hover', () => {
    // ONE field changed on the live row `e05279f2`: `verdict.reason` becomes a string outside
    // `gate()`'s grammar. Falling back to the raw text is what would put `gating: …:FAIL` on screen.
    const changed = withRow(0, {
      verdict: { overall: 'FAIL', reason: 'blocked by policy engine rev 4' },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    expect(text('runs-row-0-reason-unreadable'))
      .toContain('no reason for the verdict that can be put in words here');
    expect(screen.getByTestId('runs-row-0-reason-unreadable'))
      .toHaveAttribute('title', 'blocked by policy engine rev 4');
    expect(screen.queryByTestId('runs-row-0-reason')).toBeNull();
  });

  it('labels the two sentences per tier when the judge does not repeat itself', () => {
    // ONE field changed on the live row `e05279f2`: the dispatch tier's `explanation`. The wire
    // carries one sentence against both tiers today; if that ever stops being true, neither may be
    // silently dropped.
    const changed = withRow(0, {
      tiers: {
        dispatch: { ...LIVE_RUNS[0].tiers.dispatch, explanation: 'Routing was correct.' },
        delivery: LIVE_RUNS[0].tiers.delivery,
      },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    expect(text('runs-row-0-note')).toContain('started the right work: Routing was correct.');
    expect(text('runs-row-0-note'))
      .toContain('delivered a usable result in the session: The anime restyle plan');
  });

  it('says the judge recorded nothing rather than leaving a blank line', () => {
    // ONE field per tier changed on the live row `e05279f2`: both `explanation`s emptied.
    const changed = withRow(0, {
      tiers: {
        dispatch: { ...LIVE_RUNS[0].tiers.dispatch, explanation: '' },
        delivery: { ...LIVE_RUNS[0].tiers.delivery, explanation: '' },
      },
    });
    render(<RunsBlock actor={bundle(changed)} />);

    expect(text('runs-row-0-note-absent')).toContain('The judge recorded no sentence');
    expect(screen.queryByTestId('runs-row-0-note')).toBeNull();
  });

  it('drops the price caveat when nothing on the bundle was priced', () => {
    // ONE field changed, on a bundle cut to the single row that carries it: `spend.cost` becomes the
    // sentinel, which is what the producer writes for a run no model of which the rate table can
    // price. A note about estimated prices over a row showing none would caveat a figure nobody
    // computed.
    const unpriced = { ...LIVE_RUNS[0], spend: { ...LIVE_RUNS[0].spend, cost: 'UNMEASURED' as const } };
    render(<RunsBlock actor={bundle([unpriced], 1)} />);

    expect(screen.queryByTestId('runs-price-note')).toBeNull();
    expect(text('runs-row-0-spend')).toContain('2.0M tokens');
    expect(screen.getByTestId('runs-row-0-spend'))
      .toContainElement(screen.getByTestId('sentinel'));
  });
});

describe('where the run rows are mounted', () => {
  const data = fixture as unknown as ImproveData;

  it('is fed the reader\'s OWN bundle, behind Advanced', async () => {
    /*
     * The isolation that matters: these rows carry session ids and the judge's sentence about one
     * tenant's request, so they may only come from `data.actor`. A run id only that bundle carries is
     * what proves the mount reads it rather than the shared item.
     */
    const withRuns: ImproveData = { ...data, actor: bundle(LIVE_RUNS) };
    render(<ImproveCenter load={() => Promise.resolve(withRuns)} solo={null} />);

    // Behind the link, not on the surface a reader arrives on. This block speaks the harness's own
    // vocabulary and states four different kinds of absence to be honest about a personal bundle,
    // which is an operator's reading — the default view answers the same question in two figures.
    const advanced = await screen.findByTestId('view-components');
    expect(screen.queryByTestId('runs-block')).toBeNull();
    fireEvent.click(advanced);

    expect(screen.getByTestId('runs-block')).toBeInTheDocument();
    expect(screen.getByTestId('runs-row-0').querySelector('code')).toHaveTextContent('e05279f2');

    // First of the three engine blocks and above the component table, so the reader reads whether
    // their work landed before what it repeated, and both before the registry.
    const order = [...screen.getByTestId('improve-center').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    const mine = order.indexOf('runs-block');
    expect(mine).toBeLessThan(order.indexOf('waste-block'));
    expect(mine).toBeLessThan(order.indexOf('section-fix-first'));
  });

  it('states the absence on the payload most readers are served', async () => {
    // The committed fixture carries no `actor` key at all, which is the ordinary case: the producer
    // writes a personal bundle only for an actor who had a session in the harvest window, and seven
    // actors have one in staging today.
    expect(data.actor).toBeUndefined();
    render(<ImproveCenter load={() => Promise.resolve(data)} solo={null} />);

    fireEvent.click(await screen.findByTestId('view-components'));
    await waitFor(() => expect(screen.getByTestId('runs-no-bundle')).toBeInTheDocument());
  });
});
