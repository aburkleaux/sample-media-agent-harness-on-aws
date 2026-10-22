/**
 * Completion notifications must fire for jobs that reach a terminal state via a
 * REST path, not only via a live SSE `job.terminal` frame.
 *
 * Observed on staging: a dispatched job succeeded in 14s, its registry row was
 * correct and `GET /jobs` returned it, yet the UI produced no notification and no
 * job row until a manual reload — because `pushFromTerminal` was only ever called
 * from the SSE handler. One missed frame silently cost the user any signal that
 * their work had finished.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));

import { authFetch } from '../auth/authFetch';
import { jobsStore } from './useJobs';
import { notificationsStore, __resetNotificationsForTests } from '../services/notifications';
import type { Job, JobStatus } from '../types/jobs';

const mockAuthFetch = authFetch as unknown as ReturnType<typeof vi.fn>;

function mkJob(id: string, status: JobStatus, updatedAt: string, extra: Partial<Job> = {}): Job {
  return {
    jobId: id,
    status,
    trioId: 'media-processing|process_media',
    sessionId: 's1',
    runId: null,
    startAt: updatedAt,
    updatedAt,
    terminalAt: updatedAt,
    ...extra,
  } as unknown as Job;
}

/** The store unwraps `{ jobs: [...] }`, not a bare array. */
function respondWith(jobs: Job[]) {
  mockAuthFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ jobs }) });
}

const recent = () => new Date(Date.now() - 5_000).toISOString();
const notifiedIds = () => notificationsStore.getSnapshot().items.map(n => n.jobId);

describe('completion notifications from the REST refresh path', () => {
  beforeEach(() => {
    mockAuthFetch.mockReset();
    __resetNotificationsForTests();
  });

  it('announces a job first seen already-terminal (missed SSE frame)', async () => {
    respondWith([mkJob('j-missed', 'SUCCEEDED', recent(), { hasRenderableArtifacts: true })]);
    await jobsStore.refresh();
    expect(notifiedIds()).toContain('j-missed');
  });

  it('announces failures too, not just successes', async () => {
    respondWith([mkJob('j-fail', 'FAILED', recent())]);
    await jobsStore.refresh();
    expect(notifiedIds()).toContain('j-fail');
  });

  it('does not replay stale history as fresh notifications on page load', async () => {
    respondWith([mkJob('j-old', 'SUCCEEDED', new Date(Date.now() - 6 * 3600_000).toISOString())]);
    await jobsStore.refresh();
    expect(notifiedIds()).not.toContain('j-old');
  });

  it('does not double-announce across repeated refreshes', async () => {
    respondWith([mkJob('j-dupe', 'SUCCEEDED', recent(), { hasRenderableArtifacts: true })]);
    await jobsStore.refresh();
    await jobsStore.refresh();
    expect(notifiedIds().filter(id => id === 'j-dupe')).toHaveLength(1);
  });

  it('keeps DAG child nodes out of the notification list', async () => {
    // A plan CHILD (runId set and != jobId) must stay in the Activity view only,
    // otherwise an N-node DAG would fire N notifications.
    respondWith([mkJob('j-child', 'SUCCEEDED', recent(), { runId: 'run-1' })]);
    await jobsStore.refresh();
    expect(notifiedIds()).not.toContain('j-child');
  });

  it('announces the plan parent once for a completed DAG', async () => {
    // A DAG must produce exactly ONE notification. The parent carries
    // runId === jobId; children (runId !== jobId) are excluded above.
    respondWith([mkJob('run-1', 'SUCCEEDED', recent(), { runId: 'run-1', trioId: 'plan|plan-abc' })]);
    await jobsStore.refresh();
    expect(notifiedIds()).toContain('run-1');
  });
});
