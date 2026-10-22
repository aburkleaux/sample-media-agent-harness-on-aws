import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({
  getAccessToken: vi.fn(), signinSilent: vi.fn(), signinRedirect: vi.fn(),
}));
vi.mock('./auth/authManager', () => auth);
vi.mock('./auth/devAuth', () => ({ isDevBypassEnabled: () => false }));
// Exercise the real authFetch wrapper so a raw fetch regression loses its header.
import { fetchModelCatalog, fetchModels } from './api';
const catalog = { models: [{ id: 'model-id', name: 'Test model' }], defaultModelId: 'model-id' };
let network: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.clearAllMocks();
  auth.getAccessToken.mockResolvedValue('signed-in-token');
  network = vi.fn().mockResolvedValue(new Response(JSON.stringify(catalog)));
  vi.stubGlobal('fetch', network);
});
afterEach(() => vi.unstubAllGlobals());

describe('authenticated model catalog', () => {
  it('includes the signed-in user token and preserves the catalog default', async () => {
    expect(await fetchModelCatalog()).toEqual(catalog);
    const [url, init] = network.mock.calls[0];
    expect(url).toBe('/models');
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer signed-in-token');
  });
  it('refreshes an expired token before retrying the model list', async () => {
    network.mockResolvedValueOnce(new Response('', { status: 401 }));
    auth.signinSilent.mockResolvedValue({ access_token: 'renewed-token' });
    expect(await fetchModels()).toEqual(catalog.models);
    expect(network).toHaveBeenCalledTimes(2);
    expect(new Headers(network.mock.calls[1][1].headers).get('Authorization')).toBe('Bearer renewed-token');
    expect(auth.signinRedirect).not.toHaveBeenCalled();
  });
});
