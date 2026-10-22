/**
 * @vitest-environment jsdom
 *
 * The findings below are the REAL ones served to one actor, read out of the staging table
 * `mediaworks-harness-improve` (`actorId 68e153d0-f0b1-7072-91bc-84425b517126`, `sk SNAPSHOT`,
 * `createdAt 2026-09-03T08:01:59Z`) and transcribed field for field, including the three
 * `confidence` paragraphs — which are identical across every finding of a kind, so they are named
 * once here as the producer effectively emits them.
 *
 * Nothing here is invented. The two cases the live data does not contain — a finding whose `wallMs`
 * is the `UNMEASURED` sentinel, and a non-zero `runsNotScanned` — are each built by changing ONE
 * field of the real block, and each test says which field it changed and why the rest is untouched.
 *
 * `vitest.setup.ts` only stubs `fetch`, so the jsdom pragma above and the jest-dom import below are
 * both required. `@testing-library/user-event` is not a dependency, so this uses `fireEvent`.
 */
import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { WasteBlock } from './WasteBlock';
import { ImproveCenter, type ImproveData } from './ImproveCenter';
import fixture from './__fixtures__/improveData.fixture.json';
import type { ActorBundle, ActorWasteTotals, WasteFinding } from '../../improve-api';

vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));

const REPEAT_CONFIDENCE =
  'OBSERVED: this ordered block of tool calls ran more than once in one session, and the '
  + 'cost charged is the earlier occurrence(s). NOT ESTABLISHED: that the earlier work was '
  + 'discarded. Spans record no tool arguments for harness builtins (NS-5), so a second run '
  + 'over DIFFERENT inputs, or one whose output was also needed, is indistinguishable from '
  + 'a redo. Read this as repeated, not as wasted.';

const RETRY_CONFIDENCE =
  'OBSERVED: a tool call recorded status=ERROR and a later call of the same tool did not, '
  + 'so the failed attempt produced nothing that was kept. NOT ESTABLISHED: that the retry '
  + 'was avoidable — spans carry no arguments, so a corrected re-call and a blind repeat '
  + 'look identical, and an ERROR recovered inside the tool would look the same from '
  + 'outside. Restricted to TOOL spans: every ERROR in the golden harvest is an AWS-SDK '
  + 'internal retry, and `had_error` is anti-correlated with run failure (DF-8/DF-10).';

const ABANDON_CONFIDENCE =
  'OBSERVED: this session retrieved results for earlier dispatches, so its dispatches do '
  + 'hand back handles that need fetching, and yet its last dispatch was never followed by '
  + 'a retrieval before the session ended (the condition is '
  + '`signature.session_ended_on_unretrieved_dispatch`). The cost charged is the dispatch '
  + "call's OWN duration, which is a FLOOR and not a total: the async job it started runs "
  + 'behind the gateway and emits no span in this session, so whatever it spent is '
  + 'invisible here.';

/** Verbatim, in the order the producer wrote them — which is NOT time order, deliberately: the
 *  ranking is what several assertions below are about. */
const LIVE_FINDINGS: WasteFinding[] = [
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'repeated_sequence', calls: 5,
    wallMs: 5686, occurrences: 2,
    detail: 'http_request x3 -> code_interpreter x2', confidence: REPEAT_CONFIDENCE },
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'error_then_retry', calls: 1,
    wallMs: 71, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '39cc626b-acd6-489c-a4b8-f6161ef2d9fd', kind: 'error_then_retry', calls: 1,
    wallMs: 48, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '3e6f1fd7-676f-4389-a2b6-f22ae2307912', kind: 'abandoned_dispatch', calls: 1,
    wallMs: 68500, occurrences: 1, detail: 'dispatch_tool', confidence: ABANDON_CONFIDENCE },
  { runId: '3e6f1fd7-676f-4389-a2b6-f22ae2307912', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '3e6f1fd7-676f-4389-a2b6-f22ae2307912', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '5659aea2-cddb-4d31-adad-ee735dbd8cd5', kind: 'repeated_sequence', calls: 3,
    wallMs: 2437, occurrences: 2,
    detail: 'code_interpreter x2 -> artifact', confidence: REPEAT_CONFIDENCE },
  { runId: '6f976f3f-103e-43f2-8cf7-86cb5a4f5d89', kind: 'repeated_sequence', calls: 6,
    wallMs: 61923, occurrences: 2,
    detail: 'run_model x3 -> skills -> code_interpreter -> dispatch_tool',
    confidence: REPEAT_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'repeated_sequence', calls: 5,
    wallMs: 12405, occurrences: 2,
    detail: 'run_model x3 -> skills -> code_interpreter', confidence: REPEAT_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'error_then_retry', calls: 1,
    wallMs: 1, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'error_then_retry', calls: 1,
    wallMs: 1, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '7ccb4ed1-01b6-46ea-ae78-8509b3a06c07', kind: 'error_then_retry', calls: 1,
    wallMs: 1, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: '97a703bd-c296-4893-b8ed-7e250001b091', kind: 'repeated_sequence', calls: 8,
    wallMs: 127872, occurrences: 3,
    detail: 'run_model x3 -> dispatch_tool', confidence: REPEAT_CONFIDENCE },
  { runId: 'b1effa60-a043-43fc-b484-5538283c26c9', kind: 'repeated_sequence', calls: 4,
    wallMs: 42162, occurrences: 2,
    detail: 'run_model x3 -> dispatch_tool', confidence: REPEAT_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'repeated_sequence', calls: 4,
    wallMs: 15208, occurrences: 2,
    detail: 'get_job_status -> get_download_url x2 -> dispatch_tool',
    confidence: REPEAT_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 5, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 2, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 1, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'c6c64754-9f2a-4acb-b2b3-620cc0e95127', kind: 'error_then_retry', calls: 1,
    wallMs: 1, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE },
  { runId: 'e05279f2-f754-4b20-99aa-77e3cae64ae5', kind: 'repeated_sequence', calls: 3,
    wallMs: 1277, occurrences: 2,
    detail: 'process_media -> get_job_status x2', confidence: REPEAT_CONFIDENCE },
  { runId: 'e05279f2-f754-4b20-99aa-77e3cae64ae5', kind: 'abandoned_dispatch', calls: 1,
    wallMs: 75209, occurrences: 1,
    detail: 'plan_and_execute_tool', confidence: ABANDON_CONFIDENCE },
];

/**
 * The same actor's totals, verbatim — with ONE documented departure, in `byKind`'s denominators.
 *
 * `235f071` changed every waste `wallMs` from counting FINDINGS to counting RUNS and is not
 * deployed, so the served item still carries the old counts: `error_then_retry` reads
 * `{measuredOf: 18, total: 18}` beside `runs: 4`. Transcribed here as the COMMITTED producer writes
 * it — `{measuredOf: 4, total: 4}` — because that is the shape this file's consumer will meet, and
 * because the producer's own invariant ("each kind's `wallMs.total` equals its own `runs`") only
 * holds in that one. The sums are identical either way: every finding here recorded a duration, and
 * folding per run before summing changes the denominator and not the total. The other two kinds
 * happen to have one finding per run, so live and committed agree on them.
 *
 * `totalWallMs` is left as the live item spells it (`28 of 28`), which the same commit moves to
 * `9 of 9` — it is the count of runs with waste. Not corrected here because the assertions that read
 * it belong to the header line rather than to the split, and moving it would restate a committed
 * test's expected string on a surface this change does not touch.
 */
const LIVE_WASTE: ActorWasteTotals = {
  runsWithWaste: 9,
  runs: 13,
  runsNotScanned: 0,
  totalCalls: 58,
  totalWallMs: { sum: 412828, measuredOf: 28, total: 28 },
  byKind: {
    repeated_sequence: { findings: 8, runs: 8, calls: 38,
                         wallMs: { sum: 268970, measuredOf: 8, total: 8 } },
    abandoned_dispatch: { findings: 2, runs: 2, calls: 2,
                          wallMs: { sum: 143709, measuredOf: 2, total: 2 } },
    error_then_retry: { findings: 18, runs: 4, calls: 18,
                        wallMs: { sum: 149, measuredOf: 4, total: 4 } },
  },
  findings: LIVE_FINDINGS,
};

/**
 * A second real actor, `185163b0-d061-70a2-02af-1e7b5a65cbc5`, `createdAt 2026-09-02T08:01:42Z`,
 * whose one run carries nine findings of two kinds — so `repeated_sequence` is a MEASURED ZERO,
 * present with zeros because the producer emits every kind it has.
 *
 * The case the largest bundle above cannot show, and it needed no invention to get: every field is
 * this actor's own. `wallMs` denominators are again the committed producer's (RUNS): all nine
 * findings sit on the one run `87be9510`, so both kinds read `1 of 1` where the served item says
 * `8 of 8` for the retries. `7969` is the sum of the eight retry durations below and `22568` of all
 * nine, in both shapes.
 */
const SPARSE_FINDINGS: WasteFinding[] = [
  { runId: '87be9510-7c5a-4792-b2df-4c3862aee6e5', kind: 'abandoned_dispatch', calls: 1,
    wallMs: 14599, occurrences: 1, detail: 'dispatch_tool', confidence: ABANDON_CONFIDENCE },
  ...[2, 2, 3, 2, 2, 2, 2].map((wallMs) => ({
    runId: '87be9510-7c5a-4792-b2df-4c3862aee6e5', kind: 'error_then_retry', calls: 1,
    wallMs, occurrences: 1, detail: 'code_interpreter', confidence: RETRY_CONFIDENCE,
  })),
  { runId: '87be9510-7c5a-4792-b2df-4c3862aee6e5', kind: 'error_then_retry', calls: 1,
    wallMs: 7954, occurrences: 1, detail: 'dispatch_tool', confidence: RETRY_CONFIDENCE },
];

const SPARSE_WASTE: ActorWasteTotals = {
  runsWithWaste: 1,
  runs: 1,
  runsNotScanned: 0,
  totalCalls: 9,
  totalWallMs: { sum: 22568, measuredOf: 1, total: 1 },
  // In the producer's own order, which is `waste.WASTE_KINDS`, and NOT the order the table renders
  // them in — so a table that echoed the wire's key order would fail the ordering assertion below.
  byKind: {
    repeated_sequence: { findings: 0, runs: 0, calls: 0,
                         wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 } },
    abandoned_dispatch: { findings: 1, runs: 1, calls: 1,
                          wallMs: { sum: 14599, measuredOf: 1, total: 1 } },
    error_then_retry: { findings: 8, runs: 1, calls: 8,
                        wallMs: { sum: 7969, measuredOf: 1, total: 1 } },
  },
  findings: SPARSE_FINDINGS,
};

/**
 * The zero case, and it is a REAL zero rather than a trimmed copy of the one above: actor
 * `c8b1f3e0-b0c1-7031-d4dc-85c52ff0eba8` in the same table, whose two runs were scanned and carried
 * nothing. Note `totalWallMs.sum` is the sentinel and not `0` — the producer refuses to call an
 * absent sum "free", and neither does any of the three `byKind` entries.
 *
 * `byKind` here used to read `{}`, which this actor's item does not contain: it carries all three
 * kinds with zeros, as `waste_to_wire` says every bundle does. An empty map would have been the one
 * shape this fixture claimed to transcribe and did not.
 */
const LIVE_ZERO_WASTE: ActorWasteTotals = {
  runsWithWaste: 0,
  runs: 2,
  runsNotScanned: 0,
  totalCalls: 0,
  totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 },
  byKind: {
    repeated_sequence: { findings: 0, runs: 0, calls: 0,
                         wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 } },
    abandoned_dispatch: { findings: 0, runs: 0, calls: 0,
                          wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 } },
    error_then_retry: { findings: 0, runs: 0, calls: 0,
                        wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 0 } },
  },
  findings: [],
};

/** Everything on the bundle except `waste`, taken off the same live item. The other keys are what
 *  they are because nothing here reads them; the shape is what matters. `createdAt: null` OMITS the
 *  key, which is a state the type allows and no live item is in. */
function bundle(
  waste: ActorWasteTotals | undefined,
  createdAt: string | null = '2026-09-03T08:01:59Z',
): ActorBundle {
  const base: ActorBundle = {
    runs: [], runsTotal: 13, evidence: {},
    ...(createdAt === null ? {} : { createdAt }),
    patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [], unattributed: [] },
  };
  // No cast: `ActorBundle` declares `waste` OPTIONAL, so the pre-efficiency-path case — an item
  // stored before the key existed, served back unchanged — is spellable as what it is. It needed one
  // for as long as the type said a bundle must carry a key the wire can omit.
  return waste === undefined ? base : { ...base, waste };
}

/** Composite figures are set as several spans (the digits carry `numericStyle`), so a phrase that
 *  spans them is matched on `textContent` rather than with `getByText`. */
function rowText(testId: string): string {
  return screen.getByTestId(testId).textContent ?? '';
}

function rowTimes(pattern: RegExp): string[] {
  return screen.getAllByTestId(pattern).map((row) => row.firstElementChild?.textContent ?? '');
}

describe('WasteBlock, on the 28-finding live bundle', () => {
  it('ranks by time recovered, descending, and not by the order the producer wrote them', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // The producer's first finding is 5,686ms; the costliest is 127,872ms. Ordering by anything
    // other than time — array order, kind, run id — puts a different string in row 0.
    expect(rowTimes(/^waste-row-\d+$/)).toEqual([
      '2m 8s', '1m 15s', '1m 9s', '1m 2s', '42.2s', '15.2s', '12.4s', '5.7s', '2.4s', '1.3s',
    ]);
  });

  it('states the total against the runs it was measured over, never as a bare figure', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // 412,828ms over 9 of the actor's 13 runs. A bare `6m 53s`, or `9 runs` with no denominator,
    // fails here.
    expect(rowText('waste-total')).toContain('6m 53s charged across 9 of 13 of your runs');
    expect(rowText('waste-total')).toContain('28 findings');
    // Every finding was timed, so there is no shortfall clause to print — `28 of 28` would only
    // restate the count beside it.
    expect(rowText('waste-total')).not.toContain('timed');
  });

  it('carries the as-of date, because a skipped actor keeps the previous run\'s bundle', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // Off `createdAt: 2026-09-03T08:01:59Z`. Ignoring the field, or reading `Date.now()`, fails.
    expect(screen.getByTestId('waste-as-of')).toHaveTextContent('recorded 3 Sep');
  });

  it('says so when the bundle carries no recording date at all', () => {
    // The one field changed: `createdAt` removed from the live bundle. Every actor item in staging
    // today has one, so this is the case that cannot be photographed.
    render(<WasteBlock actor={bundle(LIVE_WASTE, null)} />);

    expect(screen.getByTestId('waste-as-of-absent'))
      .toHaveTextContent('no recording date on this bundle, so its age is unknown');
    expect(screen.queryByTestId('waste-as-of')).toBeNull();
  });

  it('collapses the sub-second tail into one line that keeps its count and its total', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // 18 of the 28 findings are under 1s and total 149ms between them. Removing the fold renders
    // 28 rows; dropping the tail loses the count; changing the threshold moves both numbers.
    expect(screen.getAllByTestId(/^waste-row-\d+$/)).toHaveLength(10);
    expect(rowText('waste-tail'))
      .toContain('18 more under 1s · 149ms combined · too small to act on');
    expect(screen.queryByTestId('waste-tail-row-0')).toBeNull();
  });

  it('folds at one second exactly, and the fold says the bound it used', () => {
    /*
     * Two findings changed, one field each: the 2,437ms repeat becomes exactly 1000ms and the
     * 1,277ms repeat becomes 999ms. That pins the boundary as INCLUSIVE at the threshold and ties
     * the printed bound to the behaviour on one render — move the threshold without the label and
     * the 999ms row appears above the fold; move the label without the threshold and the text below
     * stops matching. Both drifts fail here, which is why this is not two tests.
     */
    const findings = LIVE_FINDINGS.map((finding) => {
      if (finding.wallMs === 2437) return { ...finding, wallMs: 1000 };
      if (finding.wallMs === 1277) return { ...finding, wallMs: 999 };
      return finding;
    });
    render(<WasteBlock actor={bundle({ ...LIVE_WASTE, findings })} />);

    expect(rowTimes(/^waste-row-\d+$/)).toEqual([
      '2m 8s', '1m 15s', '1m 9s', '1m 2s', '42.2s', '15.2s', '12.4s', '5.7s', '1.0s',
    ]);
    expect(rowText('waste-tail')).toContain('19 more under 1s');
    fireEvent.click(screen.getByTestId('waste-tail-toggle'));
    expect(rowTimes(/^waste-tail-row-\d+$/)[0]).toBe('999ms');
  });

  it('names the reason the tail threshold is where it is, on the fold itself', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // A bound with no reason beside it is a magic number. Deleting the title fails this.
    const fold = screen.getByTestId('waste-tail').querySelector('[title]');
    expect(fold?.getAttribute('title')).toContain(
      '1s is the shortest delay a reader experiences as a wait');
    expect(fold?.getAttribute('title')).toContain('rather than dropped: it is still a measurement');
  });

  it('expands the tail on demand, and every finding is then on screen', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    fireEvent.click(screen.getByTestId('waste-tail-toggle'));

    // 10 + 18 = 28: the fold hides rows and loses none. A tail that silently dropped anything
    // fails here even though the collapsed line above would still read correctly.
    expect(screen.getAllByTestId(/^waste-(row|tail-row)-\d+$/)).toHaveLength(LIVE_FINDINGS.length);
    expect(screen.getByTestId('waste-tail-toggle')).toHaveAttribute('aria-expanded', 'true');
    expect(rowTimes(/^waste-tail-row-\d+$/)[0]).toBe('71ms');
  });

  it('translates each kind into a phrase, with the numbers taken off the finding', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // The two repeats differ in BOTH numbers (3 occurrences / 8 calls, and 2 / 4), so a hardcoded
    // pair passes one row and fails the other.
    expect(rowText('waste-row-0')).toContain('ran this block 3 times, 8 calls charged');
    expect(rowText('waste-row-4')).toContain('ran this block 2 times, 4 calls charged');
    expect(rowText('waste-row-1')).toContain('dispatched work, never read it');
    expect(rowText('waste-row-2')).toContain('dispatched work, never read it');

    fireEvent.click(screen.getByTestId('waste-tail-toggle'));
    expect(rowText('waste-tail-row-0')).toContain('a call failed and was retried');
    // Raw wire kinds must not reach the screen.
    expect(screen.getByTestId('waste-block').textContent).not.toContain('repeated_sequence');
  });

  it('renders `detail` exactly as the producer wrote it', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // Including the multiplicities and the arrows — this is the one field a reader can act on, and
    // it arrives already abridged. Any re-formatting, truncation or prettifying fails.
    expect(rowText('waste-row-3'))
      .toContain('run_model x3 -> skills -> code_interpreter -> dispatch_tool');
    expect(rowText('waste-row-5')).toContain('get_job_status -> get_download_url x2 -> dispatch_tool');
  });

  it('never renders `confidence` inline, and keeps the producer\'s wording on hover', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);
    const onScreen = screen.getByTestId('waste-block').textContent ?? '';

    // The internal identifiers are what make this prose the wrong register for a reading line.
    for (const jargon of ['OBSERVED:', 'NS-5', 'DF-8/DF-10',
                          'signature.session_ended_on_unretrieved_dispatch']) {
      expect(onScreen).not.toContain(jargon);
    }
    // And it is not merely dropped: the full text is one hover away on the row it belongs to.
    const phrase = screen.getByTestId('waste-row-0').querySelector('[title]');
    expect(phrase?.getAttribute('title')).toBe(REPEAT_CONFIDENCE);
  });

  it('states one short caveat per kind that is on screen', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // Ordered by the ranking, so the costliest kind's caveat comes first.
    expect(screen.getByTestId('waste-caveat-repeated_sequence'))
      .toHaveTextContent('repeats — not proven waste');
    expect(screen.getByTestId('waste-caveat-abandoned_dispatch'))
      .toHaveTextContent('a floor: only the dispatch call is priced');
    expect(screen.getByTestId('waste-caveat-error_then_retry'))
      .toHaveTextContent('the retry may not have been avoidable');
    expect(screen.getByTestId('waste-charged-note'))
      .toHaveTextContent('Times are what was charged, not what could be saved.');
  });

  it('does not claim runs were unscanned when none were', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);
    // Live value is 0, and 0 is a measurement: every run was searched, so there is nothing to warn
    // about. A line that always renders fails here.
    expect(screen.queryByTestId('waste-not-scanned')).toBeNull();
  });
});

describe('WasteBlock, the split between kinds', () => {
  /** The rows in DOM order, each as `kind|findings|runs|calls|charged`. */
  function kindRows(): string[] {
    return screen.getAllByTestId(/^waste-kind-/).map((row) => (
      [...row.children].map((cell) => cell.textContent ?? '').join('|')));
  }

  it('splits the findings between the kinds, costliest category first', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // The whole reason this table exists is in these three rows: `retries` is 18 of the 28 findings
    // and 149 MILLISECONDS, while the two kinds worth 412 seconds are 10 findings between them. A
    // reader scanning the ranked list below sees eighteen rows of noise and no way to know it is
    // noise. Ordered by charged time, so the category worth attention is first — array order, wire
    // key order (which is `repeated_sequence, abandoned_dispatch, error_then_retry` and happens to
    // match here) and findings order all put a different string somewhere.
    expect(kindRows()).toEqual([
      'repeats|8 of 28|8 of 13|38|4m 29s',
      'abandoned dispatches|2 of 28|2 of 13|2|2m 24s',
      'retries|18 of 28|4 of 13|18|149ms',
    ]);
  });

  it('reads the runs denominator off the population, not off the runs that carried something', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // 8 of the actor's 13 runs, not 8 of the 9 that carried anything. The two are both on this
    // object and only one of them is the population every other figure on this block is over.
    expect(kindRows()[0]).toContain('8 of 13');
    expect(screen.getByTestId('waste-by-kind').textContent).not.toContain('8 of 9');
  });

  it('never lets the columns read as a partition, because two of the three do not sum', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // Computed here rather than asserted as a constant, because it is the fact the caption is FOR:
    // the runs column sums to 14 over a population of 13, and the calls column to 58 against a
    // `totalCalls` that unions per run. Both are correct and neither adds up.
    const byKind = Object.values(LIVE_WASTE.byKind);
    expect(byKind.reduce((n, k) => n + k.runs, 0)).toBeGreaterThan(LIVE_WASTE.runs);
    expect(screen.getByTestId('waste-by-kind-note').textContent).toBe(
      'Each kind is an independent reading of the same sessions, so only the findings column adds '
      + 'up: two kinds can charge one call, and one run can carry several kinds. '
      + 'Across all kinds, 58 calls were charged over 9 of 13 runs, each counted once — '
      + 'so this is not the calls column’s sum.');
  });

  it('answers the calls column with the union, and refuses to be read as its total', () => {
    /*
     * The caption above has always said the calls column does not add up, and the figure that DOES
     * answer it was on the wire and on no screen: a reader was told the arithmetic fails and given
     * nothing that works.
     *
     * `totalCalls` is a UNION per run — a call two kinds both charged counts once — so it is smaller
     * than the column whenever a run carried two kinds. On THIS bundle 38 + 2 + 18 also equals 58,
     * which is a coincidence of a bundle where no run overlapped and not an identity. The assertion
     * below is what stops the line being worded off that coincidence: the sum is computed here and
     * proved equal, and the rendered sentence still refuses to be the column's total.
     */
    const columnSum = Object.values(LIVE_WASTE.byKind).reduce((n, k) => n + k.calls, 0);
    expect(columnSum).toBe(LIVE_WASTE.totalCalls);

    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);
    const union = screen.getByTestId('waste-calls-union').textContent ?? '';
    expect(union).toContain('58 calls');
    // The denominator rides the figure, and it is the population every other number here is over.
    expect(union).toContain('9 of 13 runs');
    expect(union).toContain('each counted once');
    // The words that make the agreement above unusable as arithmetic.
    expect(union).toMatch(/not the calls column’s sum/);
    expect(screen.queryByTestId('waste-calls-union-absent')).toBeNull();
  });

  it('says a bundle records no charged-call figure rather than printing a zero', () => {
    /*
     * `totalCalls` is declared required and can still be absent at runtime — the item is served back
     * as it was stored — and `0` there would claim a scan that charged nothing, over 28 findings that
     * plainly did. Absent and zero are two facts; this is the first.
     */
    const { totalCalls: _dropped, ...withoutCalls } = LIVE_WASTE;
    render(<WasteBlock actor={bundle(withoutCalls as ActorWasteTotals)} />);

    expect(screen.queryByTestId('waste-calls-union')).toBeNull();
    expect(screen.getByTestId('waste-calls-union-absent').textContent)
      .toContain('not a scan that charged nothing');
    expect(screen.getByTestId('waste-by-kind-note').textContent).not.toContain('0 calls');
  });

  it('spells a kind the same way in the table as in the caveat under the list', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    // One spelling per kind on one surface. A table that said `repeated sequences` over a caveat
    // that says `repeats` would be two names for one thing three inches apart.
    for (const [kind, noun] of [['repeated_sequence', 'repeats'],
                                ['abandoned_dispatch', 'abandoned dispatches'],
                                ['error_then_retry', 'retries']] as const) {
      expect(screen.getByTestId(`waste-kind-${kind}`).firstElementChild?.textContent).toBe(noun);
      expect(screen.getByTestId(`waste-caveat-${kind}`).textContent).toContain(`${noun} — `);
    }
  });

  it('sits above the list it splits, so the category is read before the rows', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);

    const order = [...screen.getByTestId('waste-block').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    expect(order.indexOf('waste-by-kind')).toBeGreaterThan(-1);
    expect(order.indexOf('waste-by-kind')).toBeLessThan(order.indexOf('waste-row-0'));
  });

  it('lists a kind that found nothing rather than dropping its row', () => {
    // The second real actor: one run, nine findings, and `repeated_sequence` found none of them.
    render(<WasteBlock actor={bundle(SPARSE_WASTE, '2026-09-02T08:01:42Z')} />);

    // Three rows for two kinds that found something. Dropping the zero row would say the scan
    // covered two things when it covered three, and `0 of 9` is a measurement.
    expect(kindRows()).toEqual([
      'abandoned dispatches|1 of 9|1 of 1|1|14.6s',
      'retries|8 of 9|1 of 1|8|8.0s',
      'repeats|0 of 9|0 of 1|0|unavailable',
    ]);
    expect(screen.getByTestId('waste-kind-repeated_sequence')
      .querySelector('[title]')?.getAttribute('title'))
      .toContain('every kind the producer has is always present');
  });

  it('tells a kind that found nothing from a kind whose time nothing could measure', () => {
    /*
     * ONE field changed on the real bundle above: the single `abandoned_dispatch` finding's `wallMs`
     * becomes the sentinel, which is what the producer emits when a contributing span recorded no
     * duration. Every derived figure follows from it, because all nine findings sit on one run —
     * that kind's sum loses its only run, and `totalWallMs` loses the run entirely. `error_then_retry`
     * is summed over its own findings and is untouched.
     */
    const findings = SPARSE_FINDINGS.map((finding) => (
      finding.kind === 'abandoned_dispatch' ? { ...finding, wallMs: 'UNMEASURED' as const }
        : finding));
    render(<WasteBlock actor={bundle({
      ...SPARSE_WASTE,
      findings,
      totalWallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 1 },
      byKind: {
        ...SPARSE_WASTE.byKind,
        abandoned_dispatch: { findings: 1, runs: 1, calls: 1,
                              wallMs: { sum: 'UNMEASURED', measuredOf: 0, total: 1 } },
      },
    })} />);

    // Three absences, three renderings, and collapsing any two of them is the defect: `unavailable`
    // says there was never a duration to sum, `not measured` says the producer summed and could get
    // nothing, and a figure says it summed. A `0ms` for either absence would read as free.
    const charged = kindRows().map((row) => row.split('|')[4]);
    expect(charged).toEqual(['8.0s', 'not measured', 'unavailable']);
    expect(new Set(charged).size).toBe(3);
    expect(screen.getByTestId('waste-by-kind').textContent).not.toContain('0ms');
    expect(screen.getByTestId('waste-by-kind').textContent).not.toContain('UNMEASURED');
  });

  it('says when a kind\'s charged time covers fewer runs than the kind touched', () => {
    /*
     * One field changed: `error_then_retry.wallMs.measuredOf` from 4 to 3, the shape the producer
     * writes when one of the four runs carrying retries recorded no duration for any of them. The
     * pair counts RUNS after `235f071`, so `3 of 4` is against the `runs` column beside it.
     */
    render(<WasteBlock actor={bundle({
      ...LIVE_WASTE,
      byKind: {
        ...LIVE_WASTE.byKind,
        error_then_retry: { findings: 18, runs: 4, calls: 18,
                            wallMs: { sum: 149, measuredOf: 3, total: 4 } },
      },
    })} />);

    expect(screen.getByTestId('waste-kind-error_then_retry').textContent)
      .toContain('149ms3 of 4 runs timed');
    // And it is stated only when it IS a shortfall: the other two kinds are complete, so a `2 of 2`
    // there would restate the runs column next to it.
    expect(screen.getByTestId('waste-kind-abandoned_dispatch').textContent)
      .not.toContain('timed');
  });

  it('names a kind it has no phrase for as the producer spells it', () => {
    /*
     * One key added to the real split, carrying the same numbers as the retries row it replaces: the
     * producer's kind list can grow before this screen does, and a blank cell reads as a render
     * fault. Deliberately the identifier and not a prettified version of it — nothing here can know
     * what a fourth kind should be called.
     */
    render(<WasteBlock actor={bundle({
      ...LIVE_WASTE,
      byKind: {
        repeated_sequence: LIVE_WASTE.byKind.repeated_sequence,
        abandoned_dispatch: LIVE_WASTE.byKind.abandoned_dispatch,
        polled_without_backoff: LIVE_WASTE.byKind.error_then_retry,
      },
    })} />);

    const row = screen.getByTestId('waste-kind-polled_without_backoff');
    expect(row.firstElementChild?.textContent).toBe('polled_without_backoff');
    expect(row.querySelector('[title]')?.getAttribute('title'))
      .toContain('This screen has no phrase for this kind yet');
  });

  it('counts the findings whose kind the split does not name', () => {
    /*
     * One finding's `kind` changed to a fourth kind the split does not carry — the same mutation the
     * phrase test below uses, seen from the other side. The findings column has a denominator only
     * because every finding has exactly one kind, so a kind missing from the split has to be
     * countable rather than quietly shrink the column.
     */
    const findings = LIVE_FINDINGS.map((finding) => (
      finding.wallMs === 127872 ? { ...finding, kind: 'polled_without_backoff' } : finding));
    render(<WasteBlock actor={bundle({
      ...LIVE_WASTE,
      findings,
      byKind: {
        ...LIVE_WASTE.byKind,
        repeated_sequence: { findings: 7, runs: 7, calls: 30,
                             wallMs: { sum: 268970 - 127872, measuredOf: 7, total: 7 } },
      },
    })} />);

    expect(screen.getByTestId('waste-by-kind-short').textContent).toBe(
      '1 of 28 findings are of a kind this split does not name, so the rows above do not account '
      + 'for all of them.');
  });

  it('does not claim a shortfall when every finding has a row', () => {
    render(<WasteBlock actor={bundle(LIVE_WASTE)} />);
    // 8 + 2 + 18 is 28, which is the findings list. A line that always renders fails here.
    expect(screen.queryByTestId('waste-by-kind-short')).toBeNull();
  });

  it('says a bundle carries no per-kind split, rather than showing an empty table', () => {
    /*
     * A CAST, and the only one in this file: `WasteTotals` declares `byKind` required, so an item
     * stored before that key existed — served back unchanged, the same case `waste?` exists for —
     * is not spellable without one. `?? {}` in the component would render a headed table with no
     * rows, which reads as a render fault, and `Object.keys(undefined)` would throw.
     */
    const { byKind, ...withoutByKind } = LIVE_WASTE;
    void byKind;
    render(<WasteBlock actor={bundle(withoutByKind as ActorWasteTotals)} />);

    expect(screen.getByTestId('waste-by-kind-absent').textContent).toContain(
      'This bundle carries no per-kind split, so which category each finding below falls into was '
      + 'never recorded.');
    expect(screen.queryAllByTestId(/^waste-kind-/)).toHaveLength(0);
    // The findings themselves are unaffected: the split is missing, the list is not.
    expect(screen.getAllByTestId(/^waste-row-\d+$/)).toHaveLength(10);
  });

  it('tells a split that names no kinds from one that was never written', () => {
    // `{}` is a shape no producer so far emits — every one writes its whole kind list — so it must
    // not borrow the sentence above, which would report a bundle predating the key.
    render(<WasteBlock actor={bundle({ ...LIVE_WASTE, byKind: {} })} />);

    expect(screen.getByTestId('waste-by-kind-empty').textContent).toContain(
      'The per-kind split on this bundle names no kinds at all');
    expect(screen.queryByTestId('waste-by-kind-absent')).toBeNull();
  });

  it('leaves the split out of the nothing-found state, where one sentence already carries it', () => {
    // The real zero bundle carries all three kinds with zeros, and three all-zero rows would
    // restate `0 of 2 of your runs` in a table. The claim being made there is not per-kind.
    render(<WasteBlock actor={bundle(LIVE_ZERO_WASTE, '2026-09-02T08:01:42Z')} />);

    expect(screen.getByTestId('waste-none')).toBeInTheDocument();
    expect(screen.queryByTestId('waste-by-kind')).toBeNull();
    expect(screen.queryAllByTestId(/^waste-kind-/)).toHaveLength(0);
  });
});

describe('WasteBlock, on cases the live data does not contain', () => {
  it('gives a finding with no recorded duration a labelled place, not the bottom of the list', () => {
    /*
     * Built from the real block by changing ONE field on ONE finding: the 42,162ms repeat becomes
     * `wallMs: "UNMEASURED"`, which is what the producer emits when any contributing call recorded
     * no duration (`waste._sum_durations` propagates absence rather than partially summing).
     * `totalWallMs` is moved to what the producer would then have written. Its denominator counts
     * RUNS, not findings, and `_wall_ms_by_run` unmeasures a whole run if ANY of its findings is —
     * so one untimed finding costs its run, and 9 of 9 becomes 8 of 9. Leaving it at 9 of 9 would
     * be a shape no run produces.
     */
    const findings = LIVE_FINDINGS.map((finding) => (
      finding.wallMs === 42162 ? { ...finding, wallMs: 'UNMEASURED' as const } : finding));
    render(<WasteBlock actor={bundle({
      ...LIVE_WASTE,
      findings,
      totalWallMs: { sum: 412828 - 42162, measuredOf: 8, total: 9 },
    })} />);

    // Its own labelled group, and the sentinel treatment rather than a number.
    expect(screen.getByTestId('waste-untimed'))
      .toHaveTextContent('1 finding recorded no duration, so it cannot be ranked by time.');
    expect(screen.getByTestId('waste-untimed-row-0')).toHaveTextContent('not measured');
    expect(rowText('waste-untimed-row-0')).toContain('ran this block 2 times, 4 calls charged');

    // NOT ranked as a zero: it is absent from both the ranked rows and the sub-second tail, and the
    // 9 remaining timed rows keep their order.
    expect(rowTimes(/^waste-row-\d+$/)).toEqual([
      '2m 8s', '1m 15s', '1m 9s', '1m 2s', '15.2s', '12.4s', '5.7s', '2.4s', '1.3s',
    ]);
    expect(rowText('waste-tail')).toContain('18 more under 1s');

    // And the header total now says it is short by the run that finding was on.
    expect(rowText('waste-total')).toContain('8 of 9 timed');
  });

  it('reports runs nobody scanned, because that is not the same as runs found clean', () => {
    /*
     * One field changed: `runsNotScanned: 0` becomes `2`. The producer sets it when a run held too
     * many tool calls for the repeated-block search, and the live value is 0 for every actor in
     * staging today — so this case has no screenshot and needs a test.
     */
    render(<WasteBlock actor={bundle({ ...LIVE_WASTE, runsNotScanned: 2 })} />);

    expect(rowText('waste-not-scanned')).toContain(
      '2 of 13 runs were not scanned for repeats — too many tool calls to search — so nothing '
      + 'here is about them.');
  });

  it('reports runs nobody scanned even when nothing was found, which is where it matters most', () => {
    /*
     * The real zero bundle with ONE field changed: `runsNotScanned: 0` becomes `1`. A clean bill of
     * health over 2 runs, one of which was never searched, is the exact reading this field exists to
     * prevent — so the warning has to survive into the nothing-found state and not only the list.
     */
    render(<WasteBlock actor={bundle({ ...LIVE_ZERO_WASTE, runsNotScanned: 1 })} />);

    expect(rowText('waste-none')).toContain('Nothing found: 0 of 2 of your runs');
    expect(rowText('waste-not-scanned')).toContain('1 of 2 runs were not scanned for repeats');
  });

  it('renders a kind it has no phrase for, naming it, rather than an empty row', () => {
    /*
     * One field changed: the costliest finding's `kind` becomes a fourth kind. The producer's kind
     * list can grow before this screen does, and a blank cell would read as a render fault.
     */
    const findings = LIVE_FINDINGS.map((finding) => (
      finding.wallMs === 127872 ? { ...finding, kind: 'polled_without_backoff' } : finding));
    render(<WasteBlock actor={bundle({ ...LIVE_WASTE, findings })} />);

    expect(rowText('waste-row-0'))
      .toContain('a finding kind this screen has no phrase for: polled_without_backoff');
    // Still ranked first on its time, and still given a caveat slot that admits it has none.
    expect(rowTimes(/^waste-row-\d+$/)[0]).toBe('2m 8s');
    expect(screen.getByTestId('waste-caveat-polled_without_backoff'))
      .toHaveTextContent('this screen carries no caveat for this kind yet');
  });
});

describe('WasteBlock, the three absences', () => {
  it('states a scanned-and-clean result against its denominator, and does not vanish', () => {
    // The real zero bundle: 2 runs scanned, nothing found.
    render(<WasteBlock actor={bundle(LIVE_ZERO_WASTE, '2026-09-02T08:01:42Z')} />);

    expect(screen.getByTestId('waste-block')).toHaveTextContent('Repeated and abandoned work');
    expect(rowText('waste-none')).toContain(
      'Nothing found: 0 of 2 of your runs carried repeated or abandoned work.');
    // Dated too: a clean result from yesterday's pass is not a clean result from today's.
    expect(rowText('waste-none')).toContain('recorded 2 Sep');
    expect(screen.queryByTestId('waste-total')).toBeNull();
  });

  it('reads differently from a clean result when the bundle has no `waste` key at all', () => {
    // Every bundle written before the efficiency path existed. `?? []` or `|| {}` on the key would
    // render the clean-result sentence above and claim a scan nobody ran.
    render(<WasteBlock actor={bundle(undefined)} />);

    expect(rowText('waste-absent')).toContain(
      'This bundle predates the efficiency-path scan, so nobody looked. Not a clean result.');
    expect(screen.queryByTestId('waste-none')).toBeNull();
    expect(screen.getByTestId('waste-block').textContent).not.toContain('Nothing found');
  });

  it('reads differently again when the actor has no bundle of their own', () => {
    // The ordinary state: the producer harvests a bounded window and writes a bundle only for an
    // actor who had a session in it.
    render(<WasteBlock actor={undefined} />);

    expect(rowText('waste-no-bundle')).toContain(
      'No bundle of your own in this report, so nothing of yours was scanned.');
    expect(screen.queryByTestId('waste-absent')).toBeNull();
    expect(screen.queryByTestId('waste-none')).toBeNull();
  });

  it('does not invite a rate out of an empty population', () => {
    // `0 of 0` would read as 0%, a result, when the truth is that nothing was looked at.
    render(<WasteBlock actor={bundle({
      ...LIVE_ZERO_WASTE, runs: 0,
    })} />);

    expect(rowText('waste-none')).toContain('No runs of yours are in this bundle');
    expect(rowText('waste-none')).not.toContain('0 of 0');
  });
});

describe('the mount', () => {
  it('sits behind Advanced, under the reader\'s own runs', async () => {
    // The block renders for the fixture payload, which carries no actor bundle — so what proves the
    // mount is the absence sentence, not a row. Removing the mount fails this.
    const data = fixture as unknown as ImproveData;
    render(<ImproveCenter load={() => Promise.resolve(data)} solo={null} />);

    // Not on the default view. This block is the per-ACTOR reading, so for a reader with no bundle
    // of their own its whole content is the sentence saying so — which is why the default view's
    // figures come off the shared `waste` totals instead.
    const advanced = await screen.findByTestId('view-components');
    expect(screen.queryByTestId('waste-block')).toBeNull();
    fireEvent.click(advanced);

    expect(screen.getByTestId('waste-block')).toBeInTheDocument();
    expect(screen.getByTestId('waste-no-bundle')).toBeInTheDocument();

    // Under the run rows and above the component table. Moving it out of the Advanced view, or
    // below the registry, fails this.
    const order = [...screen.getByTestId('improve-center').querySelectorAll('[data-testid]')]
      .map((node) => node.getAttribute('data-testid') ?? '');
    const mine = order.indexOf('waste-block');
    expect(mine).toBeGreaterThan(order.indexOf('runs-block'));
    expect(mine).toBeLessThan(order.indexOf('section-fix-first'));
  });
});
