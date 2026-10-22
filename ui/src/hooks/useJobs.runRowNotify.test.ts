/**
 * Completion notifications fire on the run row, not on each node finishing.
 *
 * changefeed emits a terminal per row that gains terminalAt, so a DAG produces N node
 * terminals + 1 run terminal. Notifying on any of them fired the "results ready" banner a
 * minute early (session 917e7221: #t1 terminal 21:12:24, run row 21:13:29), so clicking
 * fetched partial results. A node row carries nodeId; the run row and a standalone job do not.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const pushed: string[] = [];
vi.mock('../services/notifications', () => ({
  notificationsStore: { pushFromTerminal: (j: { jobId: string }) => pushed.push(j.jobId) },
  getCompletionNotificationsEnabled: () => true,
}));

import { __testables } from './useJobs';

const job = (over: Record<string, unknown>): any => ({
  jobId: 'x', actorId: 'a', sessionId: 's', trioId: '', gatewayId: '', status: 'SUCCEEDED',
  startAt: '', updatedAt: new Date().toISOString(), terminalAt: new Date().toISOString(),
  hasRenderableArtifacts: false, lastError: null, runId: null, nodeId: null,
  dependsOn: [], artifactUrls: [], ...over,
});

describe('completion notification fires only on the run/standalone row', () => {
  beforeEach(() => { pushed.length = 0; });

  it('does NOT notify when a single DAG node finishes', () => {
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'run#t1', nodeId: 't1', runId: 'run' }),
    ]);
    expect(pushed).toEqual([]);
  });

  it('notifies on the run row (nodeId null)', () => {
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'run', nodeId: null, runId: 'run' }),
    ]);
    expect(pushed).toEqual(['run']);
  });

  it('notifies on a standalone job (no run, no node)', () => {
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'sfn-arn', nodeId: null, runId: null }),
    ]);
    expect(pushed).toEqual(['sfn-arn']);
  });

  it('a full DAG (2 nodes + run row) notifies exactly once, on the run row', () => {
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'run#t1', nodeId: 't1', runId: 'run' }),
      job({ jobId: 'run#t2', nodeId: 't2', runId: 'run' }),
      job({ jobId: 'run', nodeId: null, runId: 'run' }),
    ]);
    expect(pushed).toEqual(['run']);
  });

  it('does NOT notify for a plan-member GATEWAY job (runId set, empty nodeId)', () => {
    // The actual premature-banner bug (session 28df6f8f): the faces/audio gateway jobs finished
    // a minute before shots + the run row. They carry runId but an EMPTY nodeId, so the nodeId
    // check alone let them through. jobId !== runId marks them as members.
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'face-recognition:ecdce234', nodeId: null, runId: 'run' }),
      job({ jobId: 'media-processing:b10a7c51', nodeId: null, runId: 'run' }),
    ]);
    expect(pushed).toEqual([]);
  });

  it('realistic DAG (node rows + gateway members + run row) notifies once, on the run row', () => {
    __testables.notifyTerminalTransitions(new Map(), [
      job({ jobId: 'run#t1', nodeId: 't1', runId: 'run', updatedAt: '2026-08-10T16:20:20Z', terminalAt: '2026-08-10T16:20:20Z' }),
      job({ jobId: 'face-recognition:x', nodeId: null, runId: 'run', updatedAt: '2026-08-10T16:20:19Z', terminalAt: '2026-08-10T16:20:19Z' }),
      job({ jobId: 'media-processing:y', nodeId: null, runId: 'run', updatedAt: '2026-08-10T16:20:19Z', terminalAt: '2026-08-10T16:20:19Z' }),
      job({ jobId: 'run', nodeId: null, runId: 'run' }),
    ]);
    expect(pushed).toEqual(['run']);
  });
});
