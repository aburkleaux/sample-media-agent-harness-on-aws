/**
 * @vitest-environment jsdom
 *
 * useCurrentUser must degrade gracefully when the auth config is unavailable
 * (`getUserManager()` rejects because `GET /config` is unreachable). It should
 * return the anonymous view and, critically, NOT leak an unhandled promise
 * rejection — an unhandled rejection fails the whole vitest run even when every
 * test passes (regression: ChatCanvas began calling this hook, and the many
 * ChatCanvas tests that don't provide /config emitted 38 unhandled rejections).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getUser: vi.fn(),
  getUserManager: vi.fn(),
  signoutRedirect: vi.fn(),
  isDevBypassEnabled: vi.fn(() => false),
  getDevAccessToken: vi.fn(() => null),
  signOutDev: vi.fn(),
}));

vi.mock('./authManager', () => ({
  getUser: mocks.getUser,
  getUserManager: mocks.getUserManager,
  signoutRedirect: mocks.signoutRedirect,
}));
vi.mock('./devAuth', () => ({
  getDevAccessToken: mocks.getDevAccessToken,
  isDevBypassEnabled: mocks.isDevBypassEnabled,
  signOutDev: mocks.signOutDev,
}));

import { useCurrentUser } from './useCurrentUser';

describe('useCurrentUser graceful degradation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.isDevBypassEnabled.mockReturnValue(false);
  });

  it('returns anonymous (no unhandled rejection) when auth config is unavailable', async () => {
    mocks.getUserManager.mockRejectedValue(
      new Error('GET /config did not return region/userPoolId/userPoolClientId/userPoolDomain'),
    );
    const { result } = renderHook(() => useCurrentUser());
    // Anonymous immediately (user is null until an effect resolves)...
    expect(result.current.mode).toBe('anonymous');
    expect(result.current.displayName).toBe('Not signed in');
    // ...and the effect's rejected getUserManager() is swallowed by .catch (no throw),
    // leaving the view anonymous rather than surfacing an unhandled rejection.
    await waitFor(() => expect(mocks.getUserManager).toHaveBeenCalled());
    expect(result.current.mode).toBe('anonymous');
  });

  it('surfaces the signed-in user when the manager resolves with a valid user', async () => {
    const fakeUser = {
      expired: false,
      profile: { name: 'Dana Lee', email: 'dana@example.com', sub: 'sub-1' },
    };
    mocks.getUserManager.mockResolvedValue({
      events: { addUserLoaded: vi.fn(), addUserUnloaded: vi.fn(), removeUserLoaded: vi.fn(), removeUserUnloaded: vi.fn() },
      getUser: vi.fn().mockResolvedValue(fakeUser),
    });
    const { result } = renderHook(() => useCurrentUser());
    await waitFor(() => expect(result.current.mode).toBe('cognito'));
    expect(result.current.displayName).toBe('Dana Lee');
  });
});
