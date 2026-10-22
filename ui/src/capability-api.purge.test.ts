/**
 * The purge client — the one call in this module that destroys something.
 *
 * Two things are worth pinning:
 *
 *  - `dryRun` must be sent explicitly on both paths. The server defaults to a
 *    preview, so an omitted flag would silently turn a purge into a no-op that
 *    reports success — worse than an error, because the user believes it worked.
 *  - unlike the read helpers, failures must surface the server's typed code.
 *    "failed (409)" and "partially deleted, retry to finish" call for opposite
 *    actions from the user.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({ authFetch: vi.fn() }));
vi.mock('./auth/authFetch', () => ({ authFetch: mocks.authFetch }));

import {
  CapabilityApiError,
  previewAgentPurge,
  purgeAgent,
  type PurgePreflight,
} from './capability-api';

const preflight: PurgePreflight = {
  assetId: 'p-agent-1',
  category: 'specialty-agent',
  versions: [1, 2],
  scopes: ['private'],
  workingCopyExists: false,
  externalAccessLive: true,
  prefixes: ['users/u/published/specialty-agents/p-agent-1/v1/'],
  references: [],
  partial: [],
};

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

function fail(status: number, body: unknown) {
  return {
    ok: false,
    status,
    json: async () => {
      if (body instanceof Error) throw body;
      return body;
    },
  };
}

beforeEach(() => mocks.authFetch.mockReset());
afterEach(() => vi.restoreAllMocks());

function lastCall() {
  const calls = mocks.authFetch.mock.calls;
  const [url, init] = calls[calls.length - 1] as [string, RequestInit];
  return { url, body: JSON.parse(String(init.body)), method: init.method };
}

describe('previewAgentPurge', () => {
  it('posts dryRun: true and unwraps the preflight', async () => {
    mocks.authFetch.mockResolvedValue(ok({ dryRun: true, preflight }));

    await expect(previewAgentPurge('p-agent-1')).resolves.toEqual(preflight);
    const call = lastCall();
    expect(call.method).toBe('POST');
    expect(call.url).toBe('/control-plane/assets/specialty-agent/p-agent-1/purge');
    expect(call.body).toEqual({ dryRun: true });
  });

  it('encodes the asset id into the path', async () => {
    mocks.authFetch.mockResolvedValue(ok({ dryRun: true, preflight }));
    await previewAgentPurge('weird/id');
    expect(lastCall().url).toContain('weird%2Fid');
  });
});

describe('purgeAgent', () => {
  it('posts dryRun: false explicitly', async () => {
    // The server previews unless it sees a literal `false`, so omitting this
    // would return success while deleting nothing.
    mocks.authFetch.mockResolvedValue(
      ok({ dryRun: false, assetId: 'p-agent-1', versionsDeleted: [2, 1] }),
    );

    const result = await purgeAgent('p-agent-1');
    expect(lastCall().body).toEqual({ dryRun: false });
    expect(result.versionsDeleted).toEqual([2, 1]);
  });
});

describe('purge error surfacing', () => {
  it('carries the typed code and the server message', async () => {
    mocks.authFetch.mockResolvedValue(
      fail(409, { detail: { code: 'conflict', message: 'removed [3]; retry to finish' } }),
    );

    await expect(purgeAgent('p-agent-1')).rejects.toMatchObject({
      status: 409,
      code: 'conflict',
      message: 'removed [3]; retry to finish',
    });
  });

  it.each([
    [403, 'authority_denied'],
    [404, 'not_found'],
    [503, 'artifacts_bucket_not_configured'],
  ])('preserves the code on %i', async (status, code) => {
    mocks.authFetch.mockResolvedValue(fail(status, { detail: { code } }));
    await expect(purgeAgent('p-agent-1')).rejects.toMatchObject({ status, code });
  });

  it('falls back to a status message when the body is not JSON', async () => {
    // CloudFront rewrites some errors into the SPA shell, so the body can be
    // HTML. That must not turn into an unhandled parse error.
    mocks.authFetch.mockResolvedValue(fail(500, new SyntaxError('Unexpected token <')));

    const err = await purgeAgent('p-agent-1').catch((e) => e);
    expect(err).toBeInstanceOf(CapabilityApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBeUndefined();
    expect(err.message).toContain('500');
  });

  it('falls back when detail is a bare string', async () => {
    // FastAPI's own validation errors use `detail: "..."`, not `{code}`.
    mocks.authFetch.mockResolvedValue(fail(422, { detail: 'nope' }));
    const err = await previewAgentPurge('p-agent-1').catch((e) => e);
    expect(err.code).toBeUndefined();
    expect(err.message).toContain('purge preview failed (422)');
  });
});
