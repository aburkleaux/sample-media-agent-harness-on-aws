import { afterEach, describe, expect, it, vi } from 'vitest';
import { __resetRuntimeConfigForTests, loadRuntimeConfig, peekRuntimeConfig } from './runtimeConfig';

afterEach(() => {
  __resetRuntimeConfigForTests();
  vi.unstubAllGlobals();
});

describe('runtimeConfig', () => {
  it('shares one request among concurrent callers and caches the answer', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ projectsEnabled: true })));
    vi.stubGlobal('fetch', fetchMock);
    expect(peekRuntimeConfig()).toBeNull();
    const [a, b] = await Promise.all([loadRuntimeConfig(), loadRuntimeConfig()]);
    await loadRuntimeConfig();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toBe(b);
    expect(peekRuntimeConfig()).toEqual({ projectsEnabled: true });
  });

  it('does not cache a failure, so the next caller retries', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('down', { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ artifactsDomain: 'cdn.example' })));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadRuntimeConfig()).rejects.toThrow(/503/);
    expect(peekRuntimeConfig()).toBeNull();
    await expect(loadRuntimeConfig()).resolves.toEqual({ artifactsDomain: 'cdn.example' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe('appVersionOf', () => {
  it('reads /config appVersion; short drops the build metadata', async () => {
    const { appVersionOf } = await import('./runtimeConfig');
    expect(appVersionOf({ appVersion: '0.9.0-rc.870+abc1234' })).toEqual({ full: '0.9.0-rc.870+abc1234', short: '0.9.0-rc.870' });
    expect(appVersionOf({ appVersion: '0.9.0' })).toEqual({ full: '0.9.0', short: '0.9.0' });
    expect(appVersionOf({})).toBeNull();
    expect(appVersionOf(null)).toBeNull();
  });
});
