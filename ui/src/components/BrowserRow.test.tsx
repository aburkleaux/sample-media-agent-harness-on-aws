// @vitest-environment jsdom
//
// The `BrowserRow` component (and the built-in browsers' use of it).
//
// This row was once shared by the Skills and Agents built-in browsers behind
// "kept in sync" comments — and had already drifted (Skills clamped its
// description in CSS *and* pre-truncated it at 80 chars in JS; its name was a
// `div` where Agents' was a `span`). Skills moved to the `InventoryRow` card
// first; Agents has now followed, so BOTH built-in browsers render the same card
// and the cross-browser parity the comments always claimed is finally real.
//
// `BrowserRow` itself survives (its `browserRowStyle` still backs the loading
// skeleton), so its own behaviour is still guarded below; what changed is that
// the built-in browsers are fingerprinted against the `InventoryRow` card, not
// against `BrowserRow`.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listSkills: vi.fn(),
  peekSkills: vi.fn(),
}));
vi.mock('../api', () => api);

// The Agents panel reads its rows from context now rather than fetching them, so
// this file supplies a context instead of an `api` mock for it.
const lab = vi.hoisted(() => ({ useLabContext: vi.fn() }));
vi.mock('../LabContext', () => ({ useLabContext: lab.useLabContext }));

const NO_CATEGORIES = {
  skill: false,
  'specialty-agent': false,
  'tool-collection': false,
  workflow: false,
  hook: false,
  tool: false,
};

import { BrowserRow } from './BrowserRow';
import BuiltInSkillsBrowser from './BuiltInSkillsBrowser';
import BuiltInAgentsBrowser from './BuiltInAgentsBrowser';

// Real built-in skill descriptions run 200–700 characters — they are model-facing
// instructions, not UI copy — which is what made the old 80-char slice visible.
const LONG_DESCRIPTION =
  'Use for CREATING media with fal.ai: generate or edit images, video, and audio, '
  + 'including text-to-image, image-to-video, upscaling, background removal, and '
  + 'voice synthesis. Routes each request to the appropriate model and returns a '
  + 'presigned artifact URL the caller can render inline.';

beforeEach(() => {
  vi.clearAllMocks();
  api.peekSkills.mockReturnValue(undefined);
  lab.useLabContext.mockReturnValue({
    builtInAgents: [],
    // `null` = no catalog, so the Skills panel falls back to the live list these
    // tests already supply through `api.listSkills`.
    builtInSkills: null,
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    loadingCategories: { ...NO_CATEGORIES },
    categoryErrors: { ...NO_CATEGORIES, 'specialty-agent': null },
  });
});

afterEach(cleanup);

describe('BrowserRow', () => {
  it('keeps the whole description in the DOM and clamps it in CSS', () => {
    render(<BrowserRow name="media-references" description={LONG_DESCRIPTION} />);

    const desc = screen.getByText(LONG_DESCRIPTION);
    // Not sliced: the row must not decide how much text fits, because it cannot
    // know the pane width. A hardcoded 80 chars cut the text off with room spare.
    expect(desc.textContent).toBe(LONG_DESCRIPTION);
    expect(desc.textContent).not.toMatch(/…/);
    // The visual truncation, so one line stays one line at any width.
    expect(desc.style.textOverflow).toBe('ellipsis');
    expect(desc.style.whiteSpace).toBe('nowrap');
    expect(desc.style.overflow).toBe('hidden');
    // Full text still reachable.
    expect(desc).toHaveAttribute('title', LONG_DESCRIPTION);
  });

  it('omits the badge, description and meta lines when there is nothing to say', () => {
    const { container } = render(<BrowserRow name="bare" />);

    // One header line only — no empty divs reserving vertical space.
    const row = container.firstElementChild!;
    expect(row.children).toHaveLength(1);
    expect(screen.getByText('bare')).toBeTruthy();
  });

  it('tones the badge by meaning, not position', () => {
    const { rerender } = render(<BrowserRow name="a" badge={{ label: 'configured', tone: 'success' }} />);
    expect(screen.getByText('configured').style.color).toBe('var(--color-success)');

    rerender(<BrowserRow name="a" badge={{ label: 'not configured', tone: 'warning' }} />);
    expect(screen.getByText('not configured').style.color).toBe('var(--color-warning)');
  });
});

describe('built-in Skills and Agents rows are formatted identically', () => {
  // Both built-in browsers now render the SAME `InventoryRow` card (Agents was the
  // last holdout on the flat `BrowserRow`; converting it restored the parity the
  // "kept in sync" comments always claimed). The guard is that each renders the
  // bordered card chrome — panel radius, 12x16 padding, a --color-border edge —
  // rather than two different row idioms under matching headers.
  async function cardStyle(name: string): Promise<string> {
    const card = (await screen.findByText(name)).closest('[style*="border-radius"]')!;
    return card.getAttribute('style')!;
  }

  it('renders the Agents browser through the shared InventoryRow card', async () => {
    lab.useLabContext.mockReturnValue({
      builtInAgents: [{
        key: 'render-agent', label: 'Render Agent', purpose: LONG_DESCRIPTION,
        invokedVia: 'dispatch_tool', configured: true, runtimeId: 'r',
      }],
      refreshCategory: vi.fn().mockResolvedValue(undefined),
      loadingCategories: { ...NO_CATEGORIES },
      categoryErrors: { ...NO_CATEGORIES, 'specialty-agent': null },
    });
    render(<BuiltInAgentsBrowser />);

    const agents = await cardStyle('Render Agent');
    expect(agents).toContain('--color-border');
    expect(agents).toContain('border-radius: 8px'); // radius.panel
    expect(agents).toContain('padding: 12px 16px');
  });

  it('renders the Skills browser through the same InventoryRow card', async () => {
    api.listSkills.mockResolvedValueOnce([
      { name: 'media-references', description: LONG_DESCRIPTION, source: 'local' },
    ]);
    render(<BuiltInSkillsBrowser />);

    const skills = await cardStyle('media-references');
    expect(skills).toContain('--color-border');
    expect(skills).toContain('border-radius: 8px');
    expect(skills).toContain('padding: 12px 16px');
  });

  it('does not pre-truncate the skill description any more', async () => {
    api.listSkills.mockResolvedValueOnce([
      { name: 'media-references', description: LONG_DESCRIPTION, source: 'local' },
    ]);
    render(<BuiltInSkillsBrowser />);

    // The old row rendered `description.slice(0, 80) + '…'`, so the full string
    // was absent from the DOM entirely.
    expect(await screen.findByText(LONG_DESCRIPTION)).toBeTruthy();
  });
});
