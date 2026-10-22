/**
 * @vitest-environment jsdom
 *
 * A completion notification the user has cleared must NOT reappear as a banner after a page
 * refresh (reported for "Watercolor FIx 8shot"). Once a job has notified, no clear action followed
 * by a reload may resurface a banner for it.
 *
 * Reload is modeled with vi.resetModules() + re-import: a fresh module instance re-runs the
 * store's load() against the persisted localStorage, exactly as a new page would. The REST refresh
 * on that new page re-sees the still-terminal job (empty prevMap) and calls pushFromTerminal again.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { Job, JobStatus } from '../types/jobs';

function mkJob(id: string): Job {
  const ts = new Date(Date.now() - 5_000).toISOString();
  return {
    jobId: id, status: 'SUCCEEDED' as JobStatus, trioId: 'watercolor-fix|edit', sessionId: 's1',
    runId: null, nodeId: null, startAt: ts, updatedAt: ts, terminalAt: ts,
    hasRenderableArtifacts: true, artifactUrls: ['https://cdn.example/x/a.png'],
  } as unknown as Job;
}

async function reloadStore() {
  vi.resetModules();
  return await import('./notifications');
}

type Snap = { items: Array<{ jobId: string; seen: boolean; bannered?: boolean }> };
const bannerEligible = (snap: Snap, jobId: string) =>
  snap.items.some((n) => n.jobId === jobId && !n.seen && !n.bannered);

const JOB = 'watercolor-8shot';

describe('cleared completion notifications do not re-fire after refresh', () => {
  beforeEach(() => { localStorage.clear(); });

  it('does not re-banner after the banner is dismissed (markBannered) + reload', async () => {
    const s1 = await reloadStore();
    s1.__resetNotificationsForTests();
    expect(s1.pushFromTerminal(mkJob(JOB))).not.toBeNull();
    s1.notificationsStore.markBannered(JOB);
    const s2 = await reloadStore();
    s2.pushFromTerminal(mkJob(JOB));
    expect(bannerEligible(s2.notificationsStore.getSnapshot(), JOB)).toBe(false);
  });

  it('does not re-banner after Mark all seen + reload', async () => {
    const s1 = await reloadStore();
    s1.__resetNotificationsForTests();
    expect(s1.pushFromTerminal(mkJob(JOB))).not.toBeNull();
    s1.notificationsStore.markAllSeen();
    const s2 = await reloadStore();
    s2.pushFromTerminal(mkJob(JOB));
    expect(bannerEligible(s2.notificationsStore.getSnapshot(), JOB)).toBe(false);
  });

  it('does not re-banner after Clear all + reload', async () => {
    const s1 = await reloadStore();
    s1.__resetNotificationsForTests();
    expect(s1.pushFromTerminal(mkJob(JOB))).not.toBeNull();
    s1.notificationsStore.clearAll();
    const s2 = await reloadStore();
    s2.pushFromTerminal(mkJob(JOB));
    expect(bannerEligible(s2.notificationsStore.getSnapshot(), JOB)).toBe(false);
  });
});
