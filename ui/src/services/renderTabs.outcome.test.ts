import { describe, it, expect, vi, beforeEach } from 'vitest';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));

import { renderTabOutcome } from './renderTabs';

const res = (status: number, body?: unknown) =>
  ({ ok: status >= 200 && status < 300, status, json: async () => body }) as unknown as Response;

beforeEach(() => authFetch.mockReset());

describe('renderTabOutcome', () => {
  it('maps the /render outcomes', async () => {
    authFetch.mockResolvedValueOnce(res(200, { manifestUrl: '/api/artifact?key=a.json' }));
    expect(await renderTabOutcome(['https://x/v.mp4'])).toEqual({ kind: 'manifest', manifestUrl: '/api/artifact?key=a.json' });
    authFetch.mockResolvedValueOnce(res(200, { noView: true, reason: 'data_only' }));
    expect(await renderTabOutcome(['https://x/a.json'])).toEqual({ kind: 'no_view' });
    authFetch.mockResolvedValueOnce(res(422, { error: 'VALIDATION_FAILED' }));
    expect(await renderTabOutcome(['https://x/a.json'])).toEqual({ kind: 'failed' });
    authFetch.mockResolvedValueOnce(res(502, { error: 'upstream' }));
    expect(await renderTabOutcome(['https://x/v.mp4'])).toEqual({ kind: 'unavailable' });
    authFetch.mockRejectedValueOnce(new Error('network'));
    expect(await renderTabOutcome(['https://x/v.mp4'])).toEqual({ kind: 'unavailable' });
  });
});
