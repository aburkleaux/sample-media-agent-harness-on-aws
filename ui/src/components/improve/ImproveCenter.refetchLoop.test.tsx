/**
 * @vitest-environment jsdom
 *
 * Regression: ImproveCenter must not re-fetch in a loop.
 *
 * Observed in sandbox 2026-09-01: opening Improve issued ~8 requests/second to
 * `GET /control-plane/improve/data` indefinitely, with the panel stuck on "Loading
 * improvement data…". The store was empty (a fresh deploy), so every request 404'd —
 * which is the route's documented "no data yet", not an error.
 *
 * Mechanism:
 *   1. `load = () => loadImproveData()` is a DEFAULT PARAMETER, so it is a new function
 *      identity on every render.
 *   2. `refresh = useCallback(..., [load])` therefore also changes every render.
 *   3. `useEffect(() => { void refresh(); }, [refresh])` therefore fires every render.
 *   4. `refresh` sets state twice: `setLoading(true)/setError(null)` up front, then
 *      `setError(msg)/setLoading(false)` after the await.
 *   5. With real latency those two batches commit in SEPARATE renders, so `error`
 *      genuinely toggles null → msg → null each cycle. That is a state change, so the
 *      cycle re-renders, re-creates `load`, and re-fires the effect. Self-sustaining at
 *      roughly one request per round trip.
 *
 * Why the existing suite cannot see it: every other test passes an explicit `load`
 * (stable identity) AND resolves it synchronously. A synchronous settle lets React batch
 * both state updates into ONE commit, so the count converges at 3 and looks fine. The
 * delay below is therefore load-bearing — remove it and this test passes while the bug
 * is still present.
 *
 * `vitest.setup.ts` only stubs fetch, so the jsdom pragma and jest-dom import are both
 * required. Run from `ui/`.
 */
import '@testing-library/jest-dom';
import { render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));

// The mock factory is an explicit allowlist — every export ImproveCenter imports from
// '../../improve-data' must be named here or the import fails.
const loadImproveData = vi.fn();
vi.mock('../../improve-data', () => ({
  loadImproveData: (...args: unknown[]) => loadImproveData(...args),
  peekImproveData: () => undefined,
  dismissSuggestion: vi.fn(),
  undoDismiss: vi.fn(),
}));

const { ImproveCenter } = await import('./ImproveCenter');

/** The exact string `fetchImproveData` throws on the live 404 (improve-data.ts:93). */
const LIVE_404 = 'No improvement data (404). The pipeline has not written a bundle for '
  + 'this account yet, or the Improve store is not configured.';

afterEach(() => {
  loadImproveData.mockReset();
});

describe('ImproveCenter does not re-fetch in a loop', () => {
  it('issues a bounded number of requests when the load keeps failing', async () => {
    // Latency is essential: it splits the two setState batches across renders, which is
    // what makes the cycle self-sustaining in a browser.
    loadImproveData.mockImplementation(() => new Promise((_, reject) =>
      setTimeout(() => reject(new Error(LIVE_404)), 20)));

    // No `load` prop — this is what production renders.
    const view = render(<ImproveCenter solo={null} />);
    await new Promise(r => setTimeout(r, 1000));
    view.unmount();

    // One initial attempt; a couple more would still be acceptable. Dozens is a loop.
    expect(loadImproveData.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it('issues a bounded number of requests when the load succeeds', async () => {
    loadImproveData.mockImplementation(() => new Promise(resolve =>
      setTimeout(() => resolve({
        suggestions: [], components: [], patterns: { patterns: [] },
      }), 20)));

    const view = render(<ImproveCenter solo={null} />);
    await new Promise(r => setTimeout(r, 1000));
    view.unmount();

    expect(loadImproveData.mock.calls.length).toBeLessThanOrEqual(3);
  });
});
