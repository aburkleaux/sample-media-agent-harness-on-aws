// @vitest-environment jsdom
//
// A Moment card's Clip auto-open: a click's pending entry is consumed by ITS job (same session, started
// after the click) and only that job opens, through the real notifications choke point.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { renderHook } from '@testing-library/react';

import {
  MOMENT_CLIP_READY_EVENT, ENTRY_TTL_MS, START_WINDOW_MS,
  requestMomentClipAutoOpen, pendingMomentClips, __resetMomentClipAutoOpenForTests,
} from './momentClipAutoOpen';
import { notificationsStore, __resetNotificationsForTests, type JobNotification } from './notifications';
import { useMomentClipAutoOpen } from '../hooks/useMomentClipAutoOpen';
import type { Job } from '../types/jobs';

const T0 = Date.parse('2026-10-11T12:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();
let seq = 0;

function job(over: Partial<Job> = {}): Job {
  return {
    jobId: `arn:job:${++seq}`,
    actorId: 'a1',
    sessionId: 'sess-1',
    trioId: 'media-processing|clip',
    gatewayId: '__aws_sfn__',
    status: 'SUCCEEDED',
    startAt: iso(Date.now()),
    updatedAt: iso(Date.now()),
    terminalAt: iso(Date.now()),
    hasRenderableArtifacts: true,
    artifactUrls: ['https://cdn/clip.mp4'],
    lastError: null,
    runId: null,
    nodeId: null,
    dependsOn: [],
    ...over,
  } as Job;
}

let ready: JobNotification[];
const onReady = (e: Event) => ready.push((e as CustomEvent).detail.notification);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
  __resetNotificationsForTests();
  __resetMomentClipAutoOpenForTests();
  ready = [];
  window.addEventListener(MOMENT_CLIP_READY_EVENT, onReady);
});
afterEach(() => {
  window.removeEventListener(MOMENT_CLIP_READY_EVENT, onReady);
  vi.useRealTimers();
});

/** The click, then `afterMs` later the job (started `startOffsetMs` after the click) finishes. */
function clickThenFinish(over: Partial<Job> = {}, startOffsetMs = 2_000, afterMs = 60_000) {
  requestMomentClipAutoOpen('sess-1', 'nba.mp4');
  vi.setSystemTime(T0 + afterMs);
  return notificationsStore.pushFromTerminal(job({ startAt: iso(T0 + startOffsetMs), ...over }));
}

describe('momentClipAutoOpen', () => {
  it('a click records one pending entry for the session', () => {
    requestMomentClipAutoOpen('sess-1', 'nba.mp4');
    expect(pendingMomentClips()).toEqual([{ sessionId: 'sess-1', requestedAt: T0, label: 'nba.mp4' }]);
    requestMomentClipAutoOpen(null, 'x');
    expect(pendingMomentClips()).toHaveLength(1);
  });

  it('the matching SUCCEEDED job in the same session opens once and consumes the entry', () => {
    const n = clickThenFinish();
    expect(ready).toEqual([n]);
    expect(pendingMomentClips()).toHaveLength(0);
    // A second renderable job afterwards is NOT auto-opened: one job per click.
    notificationsStore.pushFromTerminal(job({ startAt: iso(T0 + 3_000) }));
    expect(ready).toHaveLength(1);
  });

  it('two clicks open two jobs, one each', () => {
    requestMomentClipAutoOpen('sess-1', 'a');
    requestMomentClipAutoOpen('sess-1', 'b');
    notificationsStore.pushFromTerminal(job({ startAt: iso(T0 + 1_000) }));
    notificationsStore.pushFromTerminal(job({ startAt: iso(T0 + 2_000) }));
    notificationsStore.pushFromTerminal(job({ startAt: iso(T0 + 3_000) }));
    expect(ready).toHaveLength(2);
  });

  it('does not open a job that started before the click', () => {
    clickThenFinish({}, -60_000);
    expect(ready).toEqual([]);
    expect(pendingMomentClips()).toHaveLength(1);
  });

  it('does not open a job of another session', () => {
    clickThenFinish({ sessionId: 'sess-2' });
    expect(ready).toEqual([]);
  });

  it('does not open anything without a pending entry', () => {
    notificationsStore.pushFromTerminal(job());
    expect(ready).toEqual([]);
  });

  it('ignores plan members, plan parents and non-renderable successes', () => {
    clickThenFinish({ runId: 'run-1' });
    clickThenFinish({ jobId: 'plan-x', runId: 'plan-x', trioId: 'plan|plan-reel' });
    clickThenFinish({ hasRenderableArtifacts: false });
    expect(ready).toEqual([]);
  });

  it('expires: a job starting after the window does not match, and the entry is dropped after its TTL', () => {
    clickThenFinish({}, START_WINDOW_MS + 1_000, START_WINDOW_MS + 60_000);
    expect(ready).toEqual([]);
    vi.setSystemTime(T0 + ENTRY_TTL_MS + 1);
    expect(pendingMomentClips()).toHaveLength(0);
  });

  it('a FAILED job clears the entry and still raises its failure notification', () => {
    const n = clickThenFinish({ status: 'FAILED', hasRenderableArtifacts: false, lastError: 'boom' });
    expect(n?.kind).toBe('failure');
    expect(ready).toEqual([]);
    expect(pendingMomentClips()).toHaveLength(0);
    expect(notificationsStore.getSnapshot().items.map((i) => i.jobId)).toContain(n!.jobId);
  });
});

describe('useMomentClipAutoOpen (WorkspaceView)', () => {
  it('opens the ready clip like a bell click, marking it handled, in the session on screen', () => {
    const open = vi.fn();
    renderHook(() => useMomentClipAutoOpen('sess-1', open));
    const n = clickThenFinish();
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith(n);
    const item = notificationsStore.getSnapshot().items.find((i) => i.jobId === n!.jobId)!;
    expect(item).toMatchObject({ seen: true, bannered: true });
  });

  it('does not yank a user viewing another session; the bell notification stays unread', () => {
    const open = vi.fn();
    renderHook(() => useMomentClipAutoOpen('sess-other', open));
    const n = clickThenFinish();
    expect(open).not.toHaveBeenCalled();
    expect(notificationsStore.getSnapshot().items.find((i) => i.jobId === n!.jobId)).toMatchObject({ seen: false });
  });
});

describe('auto-open with completion notifications switched off', () => {
  it('a pending card clip still lets its session terminal frame through; nothing else does', async () => {
    const { shouldSurfaceTerminal } = await import('../hooks/useJobsSubscription');
    expect(shouldSurfaceTerminal({ sessionId: 's-clip' }, false)).toBe(false);
    requestMomentClipAutoOpen('s-clip', 'nba.mp4');
    expect(shouldSurfaceTerminal({ sessionId: 's-clip' }, false)).toBe(true);
    expect(shouldSurfaceTerminal({ sessionId: 's-other' }, false)).toBe(false);
    expect(shouldSurfaceTerminal({ sessionId: 's-other' }, true)).toBe(true);
    expect(shouldSurfaceTerminal(null, false)).toBe(false);
  });
});
