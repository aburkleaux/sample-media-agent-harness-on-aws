/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

const h = vi.hoisted(() => ({
  config: null as Record<string, unknown> | null,
  project: null as { webSearch: boolean } | null,
  setWebSearch: vi.fn(async () => ({ ok: true, value: undefined })),
}));

vi.mock('../../config/runtimeConfig', () => ({ peekRuntimeConfig: () => h.config }));
vi.mock('../../projects/sessionProjectStore', () => ({
  useSessionProject: () => h.project,
  setWebSearch: h.setWebSearch,
}));

import WebSearchToggle, { WEB_SEARCH_UNAVAILABLE } from './WebSearchToggle';

beforeEach(() => {
  h.config = null;
  h.project = null;
  h.setWebSearch.mockClear();
});

describe('WebSearchToggle (the + menu Web_Search_Toggle)', () => {
  it('where Web Search is not deployed: disabled, says why, never writes', () => {
    h.config = { webSearchAvailable: false };
    h.project = { webSearch: true };                       // a stale row value never shows as on
    render(<WebSearchToggle sessionId="s1" locked={false} />);
    const btn = screen.getByTestId('web-search-toggle');
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(WEB_SEARCH_UNAVAILABLE)).toBeInTheDocument();
    fireEvent.click(btn);
    expect(h.setWebSearch).not.toHaveBeenCalled();
  });

  it('deployed, off by default: one click turns it on for THIS chat', async () => {
    h.config = { webSearchAvailable: true };
    h.project = { webSearch: false };
    render(<WebSearchToggle sessionId="s1" locked={false} />);
    const btn = screen.getByTestId('web-search-toggle');
    expect(btn).not.toBeDisabled();
    expect(btn).toHaveAttribute('aria-checked', 'false');
    await act(async () => { fireEvent.click(btn); });
    expect(h.setWebSearch).toHaveBeenCalledWith('s1', true);
  });

  it('deployed and on: shows checked, a click turns it off', async () => {
    h.config = { webSearchAvailable: true };
    h.project = { webSearch: true };
    render(<WebSearchToggle sessionId="s1" locked={false} />);
    const btn = screen.getByTestId('web-search-toggle');
    expect(btn).toHaveAttribute('aria-checked', 'true');
    await act(async () => { fireEvent.click(btn); });
    expect(h.setWebSearch).toHaveBeenCalledWith('s1', false);
  });

  it('a locked chat, or no chat yet, cannot change it', () => {
    h.config = { webSearchAvailable: true };
    h.project = { webSearch: false };
    const { rerender } = render(<WebSearchToggle sessionId="s1" locked />);
    expect(screen.getByTestId('web-search-toggle')).toBeDisabled();
    rerender(<WebSearchToggle sessionId={null} locked={false} />);
    expect(screen.getByTestId('web-search-toggle')).toBeDisabled();
  });
});
