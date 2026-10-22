/**
 * @vitest-environment jsdom
 *
 * A just-produced render manifest can be transiently missing: the proxy returns
 * 410 for an absent object (so CloudFront can't mask it as the SPA), and the render
 * key reaches the UI across runtimes around the same time the object lands. The old
 * loader failed on the FIRST non-OK, so the user saw an occasional "Failed to load
 * manifest (410)". This pins the fix: a transient 410 is retried with backoff, and
 * the error is only surfaced once the attempts are spent.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { theme } from '../../styles/tokens';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../../auth/authFetch', () => ({ authFetch }));

import ManifestRouter from './templates/ManifestRouter';

beforeEach(() => {
  authFetch.mockReset();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('ManifestRouter — transient manifest 410 is retried', () => {
  it('retries a persistent 410 four times (700/1400/2100ms) before surfacing the error', async () => {
    authFetch.mockResolvedValue({ ok: false, status: 410, text: async () => '' });

    render(<ManifestRouter data={{ manifestUrl: '/api/artifact?key=x' }} title="R" theme={theme} />);

    // First attempt fires on mount — and it does NOT immediately error.
    await vi.advanceTimersByTimeAsync(0);
    expect(authFetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('manifest-router-error')).toBeNull();

    await vi.advanceTimersByTimeAsync(700);
    expect(authFetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1400);
    expect(authFetch).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(2100);
    expect(authFetch).toHaveBeenCalledTimes(4);

    // Only after the attempts are spent does the terminal error appear.
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByTestId('manifest-router-error')).toBeTruthy();
    expect(screen.getByText(/Failed to load manifest \(410\)/)).toBeTruthy();
  });
});
