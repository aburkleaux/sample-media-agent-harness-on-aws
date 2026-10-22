/**
 * `labAction` sends each action to the host that can serve it.
 *
 * Two independent conditions decide, and getting either wrong is quiet:
 *
 * - **Is the action control-plane?** A data-plane action sent to the Capability
 *   API is refused with a 400, turning a working call into a failure.
 * - **Does this environment run the service?** Staging deploys it with zero tasks
 *   and no `capabilitySources`, so routing there replaces a slow path with a
 *   broken one. There is no automatic fallback, which is why the check is made
 *   before the request rather than recovered from after it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  authFetch: vi.fn(),
  controlPlaneServesLabActions: vi.fn(),
}));

vi.mock('./auth/authFetch', () => ({ authFetch: mocks.authFetch }));

vi.mock('./capability-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./capability-api')>();
  return {
    ...actual, // keep CONTROL_PLANE_LAB_PATH so the expected URL is not restated
    controlPlaneServesLabActions: mocks.controlPlaneServesLabActions,
  };
});

import { labAction, LabApiError } from './lab-api';
import { CONTROL_PLANE_LAB_PATH } from './capability-api';

function ok(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  };
}

function refused() {
  return {
    ok: false,
    status: 400,
    statusText: 'Bad Request',
    text: async () =>
      JSON.stringify({
        detail: {
          code: 'action_not_available_here',
          message: 'needs model inference; the control plane has no Bedrock invoke path',
          action: 'ChatMessage',
          host: 'agent-runtime',
        },
      }),
  };
}

function urlOf(call: unknown[]): string {
  return String(call[0]);
}

describe('labAction host routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.controlPlaneServesLabActions.mockResolvedValue(true);
    mocks.authFetch.mockResolvedValue(ok({ assetId: 's-1' }));
  });

  it('sends a control-plane action to the Capability API', async () => {
    await labAction('ReadSkillDraft', { assetId: 's-1' });
    expect(urlOf(mocks.authFetch.mock.calls[0])).toBe(CONTROL_PLANE_LAB_PATH);
  });

  it('sends a write to the Capability API too — verb is not the criterion', async () => {
    await labAction('PublishAsset', { assetId: 'a-1', category: 'specialty-agent' });
    expect(urlOf(mocks.authFetch.mock.calls[0])).toBe(CONTROL_PLANE_LAB_PATH);
  });

  it('keeps a data-plane action on /lab even where the service is available', async () => {
    await labAction('ResolveAgentConfig', { assetId: 'a-1' });
    expect(urlOf(mocks.authFetch.mock.calls[0])).toBe('/lab');
  });

  it('keeps the tool-collection writes on /lab', async () => {
    await labAction('CreateToolCollectionDraft', { name: 'c' });
    await labAction('UpdateToolCollectionDraft', { assetId: 'c-1' });
    expect(mocks.authFetch.mock.calls.map(urlOf)).toEqual(['/lab', '/lab']);
  });

  it('keeps everything on /lab where the service is not in use', async () => {
    // The staging shape: service deployed with zero tasks, no capabilitySources,
    // so the config read resolves to all-legacy and this returns false.
    mocks.controlPlaneServesLabActions.mockResolvedValue(false);

    await labAction('ReadSkillDraft', { assetId: 's-1' });
    await labAction('PublishAsset', { assetId: 'a-1' });

    expect(mocks.authFetch.mock.calls.map(urlOf)).toEqual(['/lab', '/lab']);
  });

  it('surfaces a refusal message rather than a bare status text', async () => {
    // Without reading FastAPI's `detail`, this is "Bad Request" — which hides the
    // one sentence that says which host to use.
    mocks.authFetch.mockResolvedValue(refused());

    await expect(labAction('ReadSkillDraft', {})).rejects.toMatchObject({
      statusCode: 400,
      message: expect.stringContaining('Bedrock invoke path'),
    });
    await expect(labAction('ReadSkillDraft', {})).rejects.toBeInstanceOf(LabApiError);
  });

  it('still reads the runtime\'s 200-with-error envelope from either host', async () => {
    // `EditAsset` reports an existing working copy this way, so it is not a
    // failure mode peculiar to one host.
    mocks.authFetch.mockResolvedValue(
      ok({ error: 'ValueError', message: 'draft already exists' }),
    );

    await expect(labAction('EditAsset', { assetId: 'a-1' })).rejects.toMatchObject({
      message: 'draft already exists',
    });
  });
});
