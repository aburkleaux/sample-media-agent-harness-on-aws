// @vitest-environment jsdom
/**
 * The Tools list paints from the fast path, then completes from the data plane.
 *
 * `/catalog`'s fast path reads gateway tools from the AgentCore control plane,
 * which cannot describe a cross-account gateway or an MCP-server target — sandbox
 * has one of each. It used to return null in that case, so EVERY catalog read
 * threw away the built-in tools and the readable gateways to cold-start the agent
 * runtime. The tab showed nothing until that finished.
 *
 * Now the fast path degrades per gateway and names what it missed, and the panel
 * fills those in afterwards. Two properties matter enough to pin:
 *
 *   1. When nothing is outstanding, the runtime is NOT touched. That is the whole
 *      latency win, and it is invisible in a screenshot.
 *   2. When something IS outstanding, the completing pass runs and its tools
 *      replace the partial list. Without this the panel — and the tool PICKER that
 *      shares this cache — would silently offer fewer tools than before the
 *      change, and someone could save a tool collection missing them.
 */
import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listGatewayCatalog: vi.fn(),
  loadLiveGatewayCatalog: vi.fn(),
  peekGatewayTools: vi.fn(() => undefined),
  refreshGatewayTools: vi.fn(),
}));

vi.mock('../lab-api', () => api);

import GatewayBrowser from './GatewayBrowser';

const NATIVE = { name: 'artifact', description: 'Manage files', version: '1.0', source: 'Native Tools' };
const FOREIGN = {
  name: 'graph___query', description: 'Query the graph', version: '1.0',
  source: 'Gateway: Video Graph',
};

beforeEach(() => {
  vi.clearAllMocks();
  api.peekGatewayTools.mockReturnValue(undefined);
});

afterEach(cleanup);

/*
 * Tool ROWS are collapsed until a group is opened (70 expanded rows made this a
 * scroll-forever page), so these assert on the group headers — which is what a user
 * actually sees on open, and what "the panel painted" means here.
 */
describe('GatewayBrowser two-phase load', () => {
  it('paints the fast catalog and never wakes the runtime when nothing is outstanding', async () => {
    api.listGatewayCatalog.mockResolvedValue({ tools: [NATIVE], unreadable: [] });

    render(<GatewayBrowser />);

    expect(await screen.findByText('Native Tools')).toBeTruthy();
    // The assertion the change exists for: an empty `unreadable` is a positive
    // statement of completeness, so there is nothing to ask the data plane for.
    expect(api.loadLiveGatewayCatalog).not.toHaveBeenCalled();
    expect(screen.queryByText(/Loading tools from/)).toBeNull();
  });

  it('names the outstanding gateway, then adds its tools when they arrive', async () => {
    api.listGatewayCatalog.mockResolvedValue({ tools: [NATIVE], unreadable: ['Video Graph'] });
    let release: (v: unknown) => void = () => {};
    api.loadLiveGatewayCatalog.mockReturnValue(
      new Promise(resolve => { release = resolve; }),
    );

    render(<GatewayBrowser />);

    // Phase 1 is on screen while phase 2 is still running — what we already have is
    // shown, not withheld behind a whole-list spinner.
    expect(await screen.findByText('Native Tools')).toBeTruthy();
    const notice = await screen.findByText(/Loading tools from Video Graph/);
    expect(notice.getAttribute('aria-busy')).toBe('true');

    release({ tools: [NATIVE, FOREIGN], unreadable: [] });

    await waitFor(() => expect(screen.getByText('Gateway: Video Graph')).toBeTruthy());
    expect(screen.queryByText(/Loading tools from/)).toBeNull();
  });

  it('keeps what it has when completing fails, and says which gateway is missing', async () => {
    // A failed completion must not clear the list or raise the error banner: the
    // list is usable, just short. Withholding what we have would be worse.
    api.listGatewayCatalog.mockResolvedValue({ tools: [NATIVE], unreadable: ['Video Graph'] });
    api.loadLiveGatewayCatalog.mockRejectedValue(new Error('runtime unavailable'));

    render(<GatewayBrowser />);

    expect(await screen.findByText(/Could not reach Video Graph/)).toBeTruthy();
    // Still there — the failure is reported beside the list, not instead of it.
    expect(screen.getByText('Native Tools')).toBeTruthy();
    expect(screen.queryByText('runtime unavailable')).toBeNull();
  });

  it('does not dim what it already has while completing', async () => {
    // The built-in Skills panel got this wrong first: it dimmed the whole list at
    // 0.55 opacity while only per-row badges were pending, so a fast panel looked
    // stale for ~9s. The same mistake is available here, so it is pinned.
    api.listGatewayCatalog.mockResolvedValue({ tools: [NATIVE], unreadable: ['Video Graph'] });
    api.loadLiveGatewayCatalog.mockReturnValue(new Promise(() => {}));

    render(<GatewayBrowser />);

    const header = await screen.findByText('Native Tools');
    for (let el: HTMLElement | null = header; el; el = el.parentElement) {
      const opacity = el.style?.opacity;
      if (opacity) expect(Number(opacity)).toBeGreaterThan(0.95);
    }
  });
});
