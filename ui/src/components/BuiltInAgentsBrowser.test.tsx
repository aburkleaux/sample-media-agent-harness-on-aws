// @vitest-environment jsdom
//
// The built-in agents panel, which no longer fetches anything.
//
// These four agents ride along on the `specialty-agent` page the Agents tab already
// requests, so the panel reads them from context. It used to call the agent
// runtime's `mcp_list_agents` and pay an AgentCore cold start to be told what is
// essentially a constant — the first assertion here is that no request happens at
// all, because that is the whole point of the change and it is invisible otherwise.
//
// The presentation assertions are unchanged from when this panel fetched: the badge
// placement and card chrome are what keep it looking like its Skills / Tools / Hooks
// siblings, and they should not drift just because the data arrives differently.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ useLabContext: vi.fn() }));
vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));

import BuiltInAgentsBrowser from './BuiltInAgentsBrowser';
import type { BuiltInAgentRow } from '../capability-api';

const AGENT: BuiltInAgentRow = {
  key: 'render-agent',
  label: 'Render Agent',
  purpose: 'Renders artifacts.',
  invokedVia: 'dispatch_tool',
  configured: true,
  runtimeId: 'mediaworks_harness_render_agent',
};

const NO_CATEGORIES = {
  skill: false,
  'specialty-agent': false,
  'tool-collection': false,
  workflow: false,
  hook: false,
  tool: false,
};

function context(overrides: Record<string, unknown> = {}) {
  return {
    builtInAgents: [AGENT] as BuiltInAgentRow[] | null,
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    loadingCategories: { ...NO_CATEGORIES },
    categoryErrors: { ...NO_CATEGORIES, 'specialty-agent': null },
    ...overrides,
  };
}

function renderWith(ctx: Record<string, unknown>) {
  mocks.useLabContext.mockReturnValue(ctx);
  return render(<BuiltInAgentsBrowser />);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('the panel must not fetch'))));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('BuiltInAgentsBrowser data source', () => {
  it('renders from context without issuing a single request', () => {
    const fetchSpy = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

    renderWith(context());

    expect(screen.getByText('Render Agent')).toBeTruthy();
    // The assertion the change exists for: the panel is free, not merely fast.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('distinguishes "not loaded yet" from "there are none"', () => {
    // `null` is pre-load. Rendering "No built-in agents are available." here would
    // state as fact something not yet known.
    renderWith(context({
      builtInAgents: null,
      loadingCategories: { ...NO_CATEGORIES, 'specialty-agent': true },
    }));
    expect(screen.getByRole('status', { name: 'Loading built-in agents' })).toBeTruthy();
    expect(screen.queryByText('No built-in agents are available.')).toBeNull();

    cleanup();

    // `[]` is loaded-and-empty, which IS a statement of fact.
    renderWith(context({ builtInAgents: [] }));
    expect(screen.getByText('No built-in agents are available.')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Loading built-in agents' })).toBeNull();
  });

  it('keeps the rows while refreshing rather than blanking the list', () => {
    renderWith(context({
      loadingCategories: { ...NO_CATEGORIES, 'specialty-agent': true },
    }));

    expect(screen.getByText('Render Agent')).toBeTruthy();
    expect(screen.getByText('Refreshing built-in agents…')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Loading built-in agents' })).toBeNull();
  });

  it('refreshes by re-reading the category, not by calling the runtime', () => {
    const ctx = context();
    renderWith(ctx);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh built-in agents' }));

    expect(ctx.refreshCategory).toHaveBeenCalledWith('specialty-agent');
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('surfaces the category error', () => {
    renderWith(context({
      categoryErrors: { ...NO_CATEGORIES, 'specialty-agent': 'Partial results — unavailable: agent-registry' },
    }));

    expect(screen.getByText('Partial results — unavailable: agent-registry')).toBeTruthy();
  });

  it('reports an agent whose runtime is not wired as unconfigured', () => {
    // `configured: false` is a real answer — four healthy runtimes reading as
    // unconfigured is how a missing ARN env var on the Capability API task would
    // present, so the badge must reflect it rather than being omitted.
    renderWith(context({
      builtInAgents: [{ ...AGENT, configured: false, runtimeId: undefined }],
    }));

    expect(screen.getByText('Render Agent')).toBeTruthy();
    expect(screen.queryByText('configured')).toBeNull();
  });
});

describe('BuiltInAgentsBrowser presentation', () => {
  /*
   * The state badge belongs beside the name, the way a published agent's badge does in
   * the Agents center — InventoryRow places it on the name line, not flung to the far
   * edge by `space-between` where a row reads as two unrelated columns.
   */
  it('places the configured badge immediately after the name', () => {
    renderWith(context());

    const name = screen.getByText('Render Agent');
    const badge = screen.getByText('configured');

    expect(badge.parentElement).toBe(name.parentElement);
    expect(name.nextElementSibling).toBe(badge);
    expect(name.parentElement!.style.justifyContent).not.toBe('space-between');
  });

  it('pins Refresh to the panel’s right edge and takes its colours from the theme', () => {
    renderWith(context());

    const refresh = screen.getByRole('button', { name: 'Refresh built-in agents' });
    const header = screen.getByText('Built-in Agents').parentElement!;

    expect(refresh.parentElement).toBe(header);
    expect(header.style.justifyContent).toBe('space-between');
    // Tokens, not literals — the old button hardcoded its border and the label
    // carried an emoji that no theme could recolour.
    expect(refresh.style.background).toBe('var(--color-surface)');
    expect(refresh.style.color).toBe('var(--color-text)');
    expect(refresh.textContent).not.toMatch(/🔄/);
  });

  it('draws the same bordered card the Skills / Tools / Hooks rows use', () => {
    renderWith(context());

    // The parity that matters is that both built-in panels draw the identical
    // InventoryRow chrome (bordered card, panel radius, 12x16 padding), not two
    // different row idioms under matching headers.
    const card = screen.getByText('Render Agent').closest('[style*="border-radius"]')!;
    const style = card.getAttribute('style')!;
    expect(style).toContain('--color-border');
    expect(style).toContain('border-radius: 8px'); // radius.panel, as InventoryRow
    expect(style).toContain('padding: 12px 16px');
  });
});
