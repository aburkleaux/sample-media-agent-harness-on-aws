/**
 * Improve transport moved (Step 3): the old public static `improve-data.json` →
 * the `ui/server` proxy `/improve/*` → the always-on Capability API at
 * `/control-plane/improve/*`. Only the URLs changed, not the contract, so the
 * one thing worth pinning is that the URLs point at the new home AND that
 * `authFetch` attaches the token there — a repointed URL that isn't a protected
 * prefix would send every Improve request unauthenticated and 401.
 *
 * The dismiss block pins the write shape: the actor is never in the body (the
 * Capability API takes it from the token), and absent ≠ empty — a blank reason
 * is not sent as a reasoned dismissal (mirrors the Python route test).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

// Replace only `authFetch` (a real network call); keep `isProtectedPath` real so
// the protection assertions exercise the actual PROTECTED_PREFIXES logic.
vi.mock('./auth/authFetch', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./auth/authFetch')>();
  return { ...actual, authFetch: vi.fn() };
});

import { authFetch, isProtectedPath } from './auth/authFetch';
import {
  IMPROVE_DATA_URL, dismissSuggestion, loadImproveData, undoDismiss,
} from './improve-data';

const mockAuthFetch = authFetch as unknown as ReturnType<typeof vi.fn>;

function lastCall(): [string, RequestInit] {
  const calls = mockAuthFetch.mock.calls;
  const call = calls[calls.length - 1];
  if (!call) throw new Error('authFetch was not called');
  return call as [string, RequestInit];
}

describe('Improve transport URLs point at the Capability API', () => {
  it('reads the bundle from /control-plane/improve/data', () => {
    expect(IMPROVE_DATA_URL).toBe('/control-plane/improve/data');
  });

  it('protects the new Capability API paths so authFetch attaches the token', () => {
    expect(isProtectedPath(IMPROVE_DATA_URL)).toBe(true);
    expect(isProtectedPath('/control-plane/improve/suggestions/abc/dismiss')).toBe(true);
  });

  it('no longer protects the retired proxy path (removed from PROTECTED_PREFIXES)', () => {
    // Lockstep with `ui/server` authPaths (see authPaths.contract.test.ts): `/improve`
    // was dropped from both, so the old path is now an ordinary unauthenticated URL.
    expect(isProtectedPath('/improve/data')).toBe(false);
    expect(isProtectedPath('/improve')).toBe(false);
  });
});

describe('bundle keys: producer and SPA deploy in either order', () => {
  function respondWith(payload: unknown) {
    mockAuthFetch.mockResolvedValue({
      ok: true,
      // A plain stub rather than `new Headers`: this file runs in the node environment.
      headers: { get: () => 'application/json' },
      json: async () => payload,
    } as unknown as Response);
  }

  const threeKeys = { suggestions: [], components: {}, patterns: {} };

  beforeEach(() => {
    mockAuthFetch.mockReset();
  });

  it('accepts a bundle that predates the scorecard (the key is optional at runtime)', async () => {
    respondWith(threeKeys);
    // `force` bypasses the module-level promise cache, which the other tests also share.
    const data = await loadImproveData(true);
    expect(data.scorecard).toBeUndefined();
  });

  it('carries the scorecard through once the producer emits one', async () => {
    respondWith({
      ...threeKeys,
      scorecard: {
        population: { total: 9, pass: 3, fail: 1, unscored: 5 },
        passRate: 0.75,
        tiers: { dispatch: { mean: 0.83, measuredOf: 9, total: 9 },
                 delivery: { mean: 0.76, measuredOf: 4, total: 9 } },
        spend: { tokens: { sum: 6672108, measuredOf: 6, total: 9 },
                 cost: { sum: 'UNMEASURED', measuredOf: 0, total: 9 },
                 costBasis: 'tokens measured, cost not priced: no per-model Bedrock rate table',
                 latencyMs: { sum: 12182621, measuredOf: 9, total: 9 },
                 byCategory: { input: { sum: 12, measuredOf: 6, total: 9 },
                               output: { sum: 71667, measuredOf: 6, total: 9 },
                               cacheRead: { sum: 4655331, measuredOf: 6, total: 9 },
                               cacheWrite: { sum: 1945098, measuredOf: 6, total: 9 } },
                 llmCalls: { sum: 168, measuredOf: 6, total: 9 },
                 modelsSeen: 2 },
        outcomes: { failedGating: { dispatch_correct: 0, results_delivered: 1 },
                    unmeasured: { dispatch_correct: 5, results_delivered: 0 } },
        byWorkloadClass: {},
        window: 'UNKNOWN',
        derivedWindows: 0,
        evidenceComplete: { incomplete: 2, measuredOf: 9, total: 9 },
      },
    });
    const data = await loadImproveData(true);
    expect(data.scorecard?.population).toEqual({ total: 9, pass: 3, fail: 1, unscored: 5 });
    // The rate is over pass+fail only; the 5 unscored runs dilute neither side.
    expect(data.scorecard?.passRate).toBe(0.75);
    // Tokens are a real measurement, over 6 of 9 runs — the denominator travels with the sum
    // because a third of harvested sessions record no LLM usage at all.
    expect(data.scorecard?.spend.tokens.sum).toBe(6672108);
    expect(data.scorecard?.spend.tokens.measuredOf).toBe(6);
    // Cost is the sentinel STRING, so a chart cannot render it as free — and `costBasis` says why
    // rather than leaving a blank cell to guess at.
    expect(data.scorecard?.spend.cost.sum).toBe('UNMEASURED');
    expect(data.scorecard?.spend.cost.measuredOf).toBe(0);
    expect(data.scorecard?.spend.costBasis).toMatch(/rate table/);
    // The four categories reconstruct the total exactly, so a stacked chart has no residual.
    const cat = data.scorecard!.spend.byCategory;
    const summed = [cat.input, cat.output, cat.cacheRead, cat.cacheWrite]
      .reduce((n, term) => n + (term.sum as number), 0);
    expect(summed).toBe(data.scorecard?.spend.tokens.sum);
    // Cache reads dominate, which is exactly why one rate over `tokens` would be wrong.
    expect(cat.cacheRead.sum as number).toBeGreaterThan((cat.input.sum as number) * 1000);
    expect(data.scorecard?.spend.modelsSeen).toBe(2);
    // "Why" arrives as an evaluator NAME with a count, never as a reason string. A `0` is
    // "gated and never failed", which is why the key is present at all.
    expect(data.scorecard?.outcomes.failedGating).toEqual({
      dispatch_correct: 0, results_delivered: 1,
    });
    expect(data.scorecard?.outcomes.unmeasured.dispatch_correct).toBe(5);
    // How much of the population's own evidence is KNOWN missing, because a harvest window hit
    // the Insights row cap. All three numbers matter: `measuredOf: 0` would mean NOT CHECKED, so
    // `incomplete: 0` on its own cannot be rendered as an all-clear.
    expect(data.scorecard?.evidenceComplete).toEqual({
      incomplete: 2, measuredOf: 9, total: 9,
    });
  });

  it('accepts a bundle that predates the waste block, and carries one through when present',
    async () => {
      respondWith(threeKeys);
      expect((await loadImproveData(true)).waste).toBeUndefined();

      // Real numbers, from the complete 336h staging population.
      respondWith({
        ...threeKeys,
        waste: {
          runsWithWaste: 107,
          runs: 549,
          runsNotScanned: 0,
          totalCalls: 811,
          totalWallMs: { sum: 16498981, measuredOf: 262, total: 262 },
          byKind: {
            repeated_sequence: { findings: 94, runs: 94, calls: 666,
                                 wallMs: { sum: 7356291, measuredOf: 94, total: 94 } },
            abandoned_dispatch: { findings: 21, runs: 21, calls: 21,
                                  wallMs: { sum: 932384, measuredOf: 21, total: 21 } },
            error_then_retry: { findings: 147, runs: 50, calls: 147,
                                wallMs: { sum: 8210306, measuredOf: 147, total: 147 } },
          },
        },
      });
      const data = await loadImproveData(true);
      // `runsWithWaste` is meaningless without its denominator, so both travel.
      expect(data.waste?.runsWithWaste).toBe(107);
      expect(data.waste?.runs).toBe(549);
      // A skipped run is not a clean run; the count exists so it can never read as one.
      expect(data.waste?.runsNotScanned).toBe(0);
      // The per-kind calls do NOT sum to `totalCalls`: two kinds can charge the same call, and the
      // total unions them per run rather than double-charging.
      const byKind = data.waste!.byKind;
      const summed = Object.values(byKind).reduce((n, kind) => n + kind.calls, 0);
      expect(summed).toBe(834);
      expect(data.waste?.totalCalls).toBe(811);
      expect(data.waste?.totalCalls).toBeLessThan(summed);
      // Every kind the producer has is present, and none of them names a tool sequence or a run.
      expect(Object.keys(byKind).sort()).toEqual([
        'abandoned_dispatch', 'error_then_retry', 'repeated_sequence',
      ]);
      expect(JSON.stringify(data.waste)).not.toMatch(/->/);
    });

  it('accepts a payload with no per-actor bundle, which is the ordinary case', async () => {
    // Not an edge case: the producer is a daily cron over a bounded harvest window, so between any
    // deploy and the next run nobody has one — and an actor with no conversational session never
    // gets one. The three required keys are a complete answer without it, so the load must not
    // fail and the surface must not be blanked.
    respondWith(threeKeys);
    expect((await loadImproveData(true)).actor).toBeUndefined();
  });

  it("carries this actor's own evidence through when the API attaches it", async () => {
    respondWith({
      ...threeKeys,
      actor: {
        runs: [{
          runId: 'e2cb300b-3fbf-4434-8d70-2007c241e4b2',
          workloadClass: 'conversational',
          window: 'UNKNOWN',
          verdict: { overall: 'FAIL', reason: 'gating: results_delivered:FAIL' },
          tiers: {
            dispatch: { score: 0.9, outcome: 'PASS', explanation: 'dispatched the right work' },
            delivery: { score: 0.4, outcome: 'FAIL', explanation: 'no usable result arrived' },
          },
          spend: { latencyMs: 83925, tokens: 90570, cost: 'UNMEASURED', llmCalls: 1 },
        }],
        runsTotal: 1,
        evidence: {
          deb68df390ba: {
            shapeFacets: ['fanout_parent_ran_early'],
            memberRunIds: ['e2cb300b-3fbf-4434-8d70-2007c241e4b2'],
            rootCauseHypo: 'sg2_diarize_speech was recorded finished before sg1_separate_stems',
          },
        },
        waste: {
          runsWithWaste: 1, runs: 1, runsNotScanned: 0, totalCalls: 7,
          totalWallMs: { sum: 83925, measuredOf: 1, total: 1 },
          byKind: { repeated_sequence: { findings: 1, runs: 1, calls: 7,
                                         wallMs: { sum: 83925, measuredOf: 1, total: 1 } } },
          findings: [{ runId: 'e2cb300b-3fbf-4434-8d70-2007c241e4b2', kind: 'repeated_sequence',
                       calls: 7, wallMs: 83925, occurrences: 2,
                       detail: 'run_model x6 -> dispatch_tool', confidence: 'OBSERVED: ...' }],
        },
        patterns: { admitted: [], residue: [], evidenceGaps: [], environment: [],
                    unattributed: [] },
        createdAt: '2026-08-25T04:00:00Z',
      },
    });
    const actor = (await loadImproveData(true)).actor!;
    // The two things the shared bundle may not carry: the judge's sentence, and a tool sequence.
    expect(actor.runs[0].tiers.delivery.explanation).toBe('no usable result arrived');
    // `waste` is optional on the type — an item stored before the efficiency path is served back
    // unchanged — and this payload carries one, which is what the sequence below is read off.
    expect(actor.waste!.findings[0].detail).toBe('run_model x6 -> dispatch_tool');
    // Sentinels survive the transport as values, not as null or 0.
    expect(actor.runs[0].spend.cost).toBe('UNMEASURED');
    // `runsTotal` is the denominator for `runs.length`, so a trimmed bundle reads as trimmed.
    expect(actor.runsTotal).toBe(actor.runs.length);
    // Their own evidence is keyed by suggestion id, so a card can find its own detail.
    expect(actor.evidence.deb68df390ba.memberRunIds).toEqual([actor.runs[0].runId]);
  });

  it('still rejects a bundle missing one of the original three keys', async () => {
    respondWith({ suggestions: [], components: {} });
    await expect(loadImproveData(true)).rejects.toThrow(/missing one of/);
  });
});

describe('dismiss / undo transport', () => {
  beforeEach(() => {
    mockAuthFetch.mockReset();
    mockAuthFetch.mockResolvedValue({ ok: true } as Response);
  });

  it('POSTs a dismissal to the per-suggestion route with an encoded id', async () => {
    await dismissSuggestion('s 1', 'risky', 'revisit later');
    const [url, init] = lastCall();
    expect(url).toBe('/control-plane/improve/suggestions/s%201/dismiss');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ reason: 'risky', note: 'revisit later' });
  });

  it('omits an absent or blank reason/note (absent ≠ empty)', async () => {
    await dismissSuggestion('s1');
    expect(JSON.parse(lastCall()[1].body as string)).toEqual({});

    await dismissSuggestion('s1', '');
    expect(JSON.parse(lastCall()[1].body as string)).toEqual({});
  });

  it('DELETEs to undo, with no body', async () => {
    await undoDismiss('s1');
    const [url, init] = lastCall();
    expect(url).toBe('/control-plane/improve/suggestions/s1/dismiss');
    expect(init.method).toBe('DELETE');
    expect(init.body).toBeUndefined();
  });

  it('throws a legible error when the write fails', async () => {
    mockAuthFetch.mockResolvedValue({ ok: false, status: 503 } as Response);
    await expect(dismissSuggestion('s1')).rejects.toThrow(/dismiss this suggestion \(503\)/);
  });
});
