import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Job } from '../types/jobs';

// authFetch is the only I/O boundary in useJobs.ts — mock it so these tests
// exercise the real store logic (removeJobs / deleteJob / deleteRun /
// applyEvent) without a network or DOM dependency.
const authFetchMock = vi.fn();
vi.mock('../auth/authFetch', () => ({ authFetch: (...args: unknown[]) => authFetchMock(...args) }));

import { __resetJobsStoreForTests, jobsStore, loadSession } from './useJobs';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'job-' + Math.random().toString(36).slice(2),
    actorId: 'actor-1',
    sessionId: 'session-A',
    trioId: 'node|t1_stub',
    gatewayId: '__coordinator__',
    status: 'SUCCEEDED',
    startAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    terminalAt: new Date().toISOString(),
    hasRenderableArtifacts: false,
    lastError: null,
    runId: null,
    nodeId: null,
    dependsOn: [],
    ...overrides,
  };
}

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    statusText: ok ? 'OK' : 'Error',
    json: async () => body,
  } as unknown as Response;
}

// Seed the module-level store directly via a live SSE-style applyEvent —
// there is no exported "set" API, and that's intentional (mirrors how the
// real app populates the store), so tests seed the same way.
function seedJob(j: Job): void {
  jobsStore.applyEvent({ event: 'job.update', data: j });
}

describe('useJobs store — deleteJob / deleteRun', () => {
  beforeEach(() => {
    __resetJobsStoreForTests();
    authFetchMock.mockReset();
  });
  afterEach(() => {
    __resetJobsStoreForTests();
  });

  it('deleteJob removes the job from the store on success', async () => {
    const j = job({ jobId: 'job-a' });
    seedJob(j);
    expect(jobsStore.getSnapshot().jobsById.has('job-a')).toBe(true);

    authFetchMock.mockResolvedValue(jsonResponse({ deleted: ['job-a'] }));

    await jobsStore.deleteJob('job-a');

    expect(authFetchMock).toHaveBeenCalledWith('/jobs/job-a', { method: 'DELETE' });
    expect(jobsStore.getSnapshot().jobsById.has('job-a')).toBe(false);
  });

  it('deleteJob appends ?force=true when force is requested', async () => {
    seedJob(job({ jobId: 'job-b', status: 'RUNNING' }));
    authFetchMock.mockResolvedValue(jsonResponse({ deleted: ['job-b'] }));

    await jobsStore.deleteJob('job-b', { force: true });

    expect(authFetchMock).toHaveBeenCalledWith('/jobs/job-b?force=true', { method: 'DELETE' });
  });

  it('deleteJob throws a JobDeleteError with the server code on 409 (job_active)', async () => {
    seedJob(job({ jobId: 'job-c', status: 'RUNNING' }));
    authFetchMock.mockResolvedValue(
      jsonResponse({ error: 'job_active', message: 'Job is still active.' }, false, 409),
    );

    await expect(jobsStore.deleteJob('job-c')).rejects.toMatchObject({
      code: 'job_active',
      message: 'Job is still active.',
    });
    // Not removed — the delete was refused server-side.
    expect(jobsStore.getSnapshot().jobsById.has('job-c')).toBe(true);
  });

  it('deleteJob surfaces a CLEAR error (not a JSON SyntaxError) when a 200 body is not JSON (misrouted to SPA HTML)', async () => {
    seedJob(job({ jobId: 'job-html' }));
    // A 2xx whose body is index.html — .json() throws the opaque
    // `Unexpected token '<', "<!doctype "... is not valid JSON`.
    authFetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => { throw new SyntaxError(`Unexpected token '<', "<!doctype "... is not valid JSON`); },
    } as unknown as Response);

    await expect(jobsStore.deleteJob('job-html')).rejects.toThrow(/non-JSON response/);
    // Could not confirm the delete → keep the job in the store.
    expect(jobsStore.getSnapshot().jobsById.has('job-html')).toBe(true);
  });

  it('deleteRun removes every returned id from the store', async () => {
    seedJob(job({ jobId: 'run-1', trioId: 'plan|my-plan', runId: 'run-1' }));
    seedJob(job({ jobId: 'run-1#t1', trioId: 'node|t1', runId: 'run-1' }));
    seedJob(job({ jobId: 'run-1#t2', trioId: 'node|t2', runId: 'run-1' }));
    seedJob(job({ jobId: 'unrelated', runId: 'run-2' }));

    authFetchMock.mockResolvedValue(
      jsonResponse({ deleted: ['run-1', 'run-1#t1', 'run-1#t2'] }),
    );

    await jobsStore.deleteRun('run-1');

    expect(authFetchMock).toHaveBeenCalledWith('/jobs?runId=run-1', { method: 'DELETE' });
    const remaining = jobsStore.getSnapshot().jobsById;
    expect(remaining.has('run-1')).toBe(false);
    expect(remaining.has('run-1#t1')).toBe(false);
    expect(remaining.has('run-1#t2')).toBe(false);
    expect(remaining.has('unrelated')).toBe(true);
  });

  it('deleteRun throws a JobDeleteError with run_active code on 409', async () => {
    seedJob(job({ jobId: 'run-1', trioId: 'plan|my-plan', runId: 'run-1', status: 'RUNNING' }));
    authFetchMock.mockResolvedValue(
      jsonResponse({ error: 'run_active', message: '1 job(s) in this run are still active.' }, false, 409),
    );

    await expect(jobsStore.deleteRun('run-1')).rejects.toMatchObject({ code: 'run_active' });
    expect(jobsStore.getSnapshot().jobsById.has('run-1')).toBe(true);
  });

  it('deleteJob throws a plain Error (no code) when the server returns a non-JSON error body', async () => {
    seedJob(job({ jobId: 'job-d' }));
    authFetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => { throw new Error('not json'); },
    } as unknown as Response);

    await expect(jobsStore.deleteJob('job-d')).rejects.toThrow(/DELETE \/jobs\/job-d failed/);
  });
});


describe('useJobs — loadSession (per-session fetch + merge)', () => {
  beforeEach(() => {
    __resetJobsStoreForTests();
    authFetchMock.mockReset();
  });
  afterEach(() => {
    __resetJobsStoreForTests();
  });

  it('fetches /jobs?sessionId= and MERGES, preserving other sessions in the store', async () => {
    // A recent session's job is already in the store (from the actor-wide seed).
    seedJob(job({ jobId: 'existing-A', sessionId: 'session-A' }));
    // Opening an older session returns ITS jobs (outside the newest-runs window).
    authFetchMock.mockResolvedValue(jsonResponse({
      jobs: [
        job({ jobId: 'old-B1', sessionId: 'session-B' }),
        job({ jobId: 'old-B2', sessionId: 'session-B' }),
      ],
    }));

    await loadSession('session-B');

    expect(authFetchMock).toHaveBeenCalledWith('/jobs?sessionId=session-B');
    const map = jobsStore.getSnapshot().jobsById;
    expect(map.has('existing-A')).toBe(true);  // not dropped by the merge
    expect(map.has('old-B1')).toBe(true);        // merged in
    expect(map.has('old-B2')).toBe(true);
  });

  it('is a no-op for a blank session id (never fetches)', async () => {
    await loadSession('');
    expect(authFetchMock).not.toHaveBeenCalled();
  });
});
