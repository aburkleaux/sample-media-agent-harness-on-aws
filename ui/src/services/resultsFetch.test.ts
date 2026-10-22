import { describe, it, expect } from 'vitest';
import { decideResultsAction, failedNotificationTurn, summaryNotificationTurn } from './resultsFetch';

const base = { jobId: 'j1', now: 100_000, inFlightWindowMs: 4000 };

describe('decideResultsAction — fetch/focus + recovery', () => {
  it('FOCUS when a render tab for the job already exists (success cache)', () => {
    const r = decideResultsAction({ ...base, renderHistory: [{ jobId: 'jX' }, { jobId: 'j1' }] });
    expect(r).toEqual({ kind: 'focus', index: 1 });
  });

  it('FETCH on the first click (no tab, never fetched)', () => {
    expect(decideResultsAction({ ...base, renderHistory: [], lastFetchAt: undefined })).toEqual({ kind: 'fetch' });
  });

  it('SUPPRESS a repeat click within the in-flight window (dedup, no duplicate tab)', () => {
    expect(decideResultsAction({ ...base, renderHistory: [], lastFetchAt: 100_000 - 1000 })).toEqual({ kind: 'suppress' });
  });

  it('RETRY (fetch) after a failed fetch: no tab, window elapsed', () => {
    // A failed/empty fetch left no tab; the in-flight window has passed → a click re-fetches.
    expect(decideResultsAction({ ...base, renderHistory: [], lastFetchAt: 100_000 - 5000 })).toEqual({ kind: 'fetch' });
  });

  it('prefers FOCUS over suppress even if a fetch was just dispatched (tab landed)', () => {
    const r = decideResultsAction({ ...base, renderHistory: [{ jobId: 'j1' }], lastFetchAt: 100_000 - 500 });
    expect(r).toEqual({ kind: 'focus', index: 0 });
  });

  it('ignores tabs with no/blank jobId (never focuses the wrong tab)', () => {
    expect(decideResultsAction({ ...base, renderHistory: [{}, { jobId: '' }] })).toEqual({ kind: 'fetch' });
  });

  it('does not match a different job id', () => {
    expect(decideResultsAction({ ...base, renderHistory: [{ jobId: 'other' }] })).toEqual({ kind: 'fetch' });
  });

  it('exact window boundary is treated as elapsed (fetch), not in-flight', () => {
    expect(decideResultsAction({ ...base, renderHistory: [], lastFetchAt: 100_000 - 4000 })).toEqual({ kind: 'fetch' });
  });
});


describe('failedNotificationTurn — opening a FAILED notification asks the agent why', () => {
  it('asks why the job failed, carrying the id, and requests a recovery attempt', () => {
    const t = failedNotificationTurn('e2faa83c-54f0-49dd-9b29-741e3506312f#separate_stems', 'Stem separation');
    expect(t.prompt).toContain('why this job failed');
    expect(t.prompt).toContain('e2faa83c-54f0-49dd-9b29-741e3506312f#separate_stems');
    expect(t.prompt.toLowerCase()).toContain('recoverable');
    expect(t.displayText).toBe('Why did Stem separation fail?');
  });

  it('falls back to a generic label when none is given', () => {
    expect(failedNotificationTurn('j1').displayText).toBe('Why did the job fail?');
  });
});

describe('summaryNotificationTurn', () => {
  const id = '6d488ab2-9673-432a-8211-4aac7a310f49';

  it('permits ONE read of the stored result, naming the real tools and the id', () => {
    // 9936cbc3: "do NOT re-fetch … just read the resolved result" left the agent with no way to
    // read a result that is only in the UI, so it refused to summarize a SUCCEEDED plan.
    const { prompt } = summaryNotificationTurn(id, 'Sunset Video Trim');
    expect(prompt).toContain(`get_plan_results("${id}")`);
    expect(prompt).toContain(`get_job("${id}")`);
    expect(prompt).toMatch(/read the\s+stored result ONCE/);
    expect(prompt).not.toMatch(/do NOT re-fetch/i);
  });

  it('still forbids the expensive steps', () => {
    const { prompt } = summaryNotificationTurn(id, 'Sunset Video Trim');
    expect(prompt).toMatch(/Do NOT presign, re-render, or\s+re-feature/);
  });

  it('keeps the visible chat text short and labelled', () => {
    expect(summaryNotificationTurn(id, 'Sunset Video Trim').displayText).toBe('Summarize the Sunset Video Trim results');
    expect(summaryNotificationTurn(id).displayText).toBe('Summarize the job results');
  });
});
