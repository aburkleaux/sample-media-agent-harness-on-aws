/**
 * A renderHistory PATCH for a session the server has no row for must fail ONCE, not forever.
 *
 * Staging 2026-10-08: a session deleted in another tab left the SPA re-PATCHing it on every
 * renderHistory change — `404 Session not found` plus `[persist] session renderHistory failed`
 * repeating every few seconds. The first 404 now marks the session absent; later writes are skipped
 * until `createSession` succeeds for that id.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({ authFetch: vi.fn() }));
vi.mock('./auth/authFetch', () => ({ authFetch: mocks.authFetch }));

const res = (status: number, body: unknown = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

const SID = 'deleted-session-id';

let api: typeof import('./lab-api');

beforeEach(async () => {
  api = await import('./lab-api');
  api.__resetAbsentSessionsForTests();
  mocks.authFetch.mockReset();
});

afterEach(() => {
  api.__resetAbsentSessionsForTests();
});

describe('updateSessionRenderHistory on a missing session', () => {
  it('throws the first 404 and then stops calling the server', async () => {
    mocks.authFetch.mockResolvedValue(res(404, { error: 'Session not found' }));

    await expect(api.updateSessionRenderHistory(SID, [{ blocks: [] }])).rejects.toMatchObject({
      name: 'LabApiError',
      statusCode: 404,
    });
    expect(mocks.authFetch).toHaveBeenCalledTimes(1);

    // Every later change is a no-op: no request, no rejection, so no repeated console error.
    await expect(api.updateSessionRenderHistory(SID, [{ blocks: [] }])).resolves.toBeUndefined();
    await expect(api.updateSessionRenderHistory(SID, [{ blocks: [] }])).resolves.toBeUndefined();
    expect(mocks.authFetch).toHaveBeenCalledTimes(1);
  });

  it('keeps persisting other sessions, and resumes after the session is created', async () => {
    mocks.authFetch.mockResolvedValueOnce(res(404, { error: 'Session not found' }));
    await expect(api.updateSessionRenderHistory(SID, [])).rejects.toMatchObject({ statusCode: 404 });

    // A different session is unaffected.
    mocks.authFetch.mockResolvedValueOnce(res(200));
    await expect(api.updateSessionRenderHistory('other-session', [])).resolves.toBeUndefined();
    expect(mocks.authFetch).toHaveBeenCalledTimes(2);

    // Once the row exists, the marker is cleared and writes resume.
    mocks.authFetch.mockResolvedValueOnce(res(201));
    await api.createSession(SID, 'Recreated');
    mocks.authFetch.mockResolvedValueOnce(res(200));
    await expect(api.updateSessionRenderHistory(SID, [])).resolves.toBeUndefined();
    expect(mocks.authFetch).toHaveBeenCalledTimes(4);
  });

  it('a non-404 failure still retries on the next change', async () => {
    mocks.authFetch.mockResolvedValue(res(500, { error: 'boom' }));
    await expect(api.updateSessionRenderHistory(SID, [])).rejects.toMatchObject({ statusCode: 500 });
    await expect(api.updateSessionRenderHistory(SID, [])).rejects.toMatchObject({ statusCode: 500 });
    expect(mocks.authFetch).toHaveBeenCalledTimes(2);
  });
});
