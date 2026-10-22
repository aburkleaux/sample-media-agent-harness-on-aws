// A busy user's reconnect resync put every job ARN in one `GET /jobs?ids=` and CloudFront answered 414 (URI too
// long, staging 2026-10-11 with 43 runs). The ids are now fetched in chunks and merged.
import { describe, it, expect, vi, beforeEach } from 'vitest';

const authFetchMock = vi.fn();
vi.mock('../auth/authFetch', () => ({ authFetch: (...args: unknown[]) => authFetchMock(...args) }));

import { __resetJobsStoreForTests, jobsStore, JOB_IDS_PER_REQUEST } from './useJobs';

const arn = (i: number) => `arn:aws:states:us-west-2:100000000002:execution:m2cv6-0e0000000002-shot-metadata-enrichment:${String(i).padStart(8, '0')}-2f55-5434-9257-177f71dcb230`;

beforeEach(() => {
  __resetJobsStoreForTests();
  authFetchMock.mockReset();
  authFetchMock.mockImplementation(async (url: string) => {
    const ids = decodeURIComponent(url.split('ids=')[1]).split(',');
    return { ok: true, status: 200, json: async () => ({ jobs: ids.map((jobId) => ({ jobId, status: 'RUNNING', actorId: 'a', sessionId: 's', startAt: new Date().toISOString(), updatedAt: new Date().toISOString() })) }) } as unknown as Response;
  });
});

describe('jobs resync by id', () => {
  it('splits many ids into short requests and merges every job', async () => {
    const ids = Array.from({ length: 47 }, (_, i) => arn(i));
    await jobsStore.refresh(ids);
    expect(authFetchMock).toHaveBeenCalledTimes(Math.ceil(47 / JOB_IDS_PER_REQUEST));
    for (const [url] of authFetchMock.mock.calls) expect(String(url).length).toBeLessThan(6000);
    const have = jobsStore.getSnapshot().jobsById;
    expect(ids.every((id) => have.has(id))).toBe(true);
    expect(jobsStore.getSnapshot().lastError ?? null).toBeNull();
  });

  it('a short list is still one request', async () => {
    await jobsStore.refresh([arn(1), arn(2)]);
    expect(authFetchMock).toHaveBeenCalledTimes(1);
  });
});
