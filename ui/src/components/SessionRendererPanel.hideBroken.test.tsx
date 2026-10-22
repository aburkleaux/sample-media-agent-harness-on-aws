// @vitest-environment jsdom
/**
 * "If it can't render, it shouldn't show": a tab whose renderer throws, whose manifest fails for good, or whose
 * template is unknown is dropped from the strip and the panel moves to a neighbour (renderer/renderFailure.tsx).
 * Session 61deed9b showed a "Shot Segmentation" tab stuck on "Cannot read properties of undefined (reading
 * 'toFixed')"; the same class of failure kept surfacing as broken tabs.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

const authFetch = vi.hoisted(() => vi.fn());
vi.mock('../auth/authFetch', () => ({ authFetch }));
vi.mock('../lab-api', () => ({ getConfig: async () => ({ artifactsDomain: '' }) }));

const Boom = () => { throw new Error('boom'); };
const Fine = ({ data }: { data: { text?: string } }) => <div data-testid="fine">{data?.text ?? 'ok'}</div>;
vi.mock('./renderer/RendererRegistry', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./renderer/RendererRegistry')>();
  return {
    ...actual,
    lookupRenderer: (t: string) => (t === 'test-boom' ? Boom : t === 'test-fine' ? Fine : actual.lookupRenderer(t)),
  };
});

import SessionRendererPanel, { tabSignature } from './SessionRendererPanel';
import type { RenderHistoryEntry } from './renderer/types';
import { normalizeAnnotations } from './renderer/annotations';

function tab(label: string, template: string, data: Record<string, unknown> = {}): RenderHistoryEntry {
  return { turnIndex: 0, timestamp: 1, label, key: `k-${label}`, blocks: [{ template, title: label, data }] } as unknown as RenderHistoryEntry;
}

beforeEach(() => {
  sessionStorage.clear();
  authFetch.mockReset();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

const tabs = () => screen.queryAllByRole('tab').map((t) => t.textContent?.replace(/[○●]/g, '').trim());

describe('SessionRendererPanel — broken tabs are hidden', () => {
  it('drops a tab whose renderer throws and moves to the next tab', async () => {
    const onNavigate = vi.fn();
    render(<SessionRendererPanel history={[tab('Video', 'test-fine'), tab('Shots', 'test-boom'), tab('Audio', 'test-fine')]}
      currentIndex={1} onNavigate={onNavigate} onClose={vi.fn()} />);
    await waitFor(() => expect(tabs()).toEqual(['Video', 'Audio']));
    expect(onNavigate).toHaveBeenCalledWith(2);
    expect(screen.queryByText(/error occurred/i)).toBeNull();
    expect(screen.getByText('Tab · 2 of 2')).toBeInTheDocument();  // Audio, the neighbour
  });

  it('falls back to the previous tab when the broken one is last', async () => {
    const onNavigate = vi.fn();
    render(<SessionRendererPanel history={[tab('Video', 'test-fine'), tab('Shots', 'test-boom')]}
      currentIndex={1} onNavigate={onNavigate} onClose={vi.fn()} />);
    await waitFor(() => expect(onNavigate).toHaveBeenCalledWith(0));
  });

  it('drops an unknown-template tab', async () => {
    render(<SessionRendererPanel history={[tab('Ok', 'test-fine'), tab('Mystery', 'no-such-template')]}
      currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(tabs()).toEqual(['Ok']));
    expect(screen.queryByText(/Unknown template/)).toBeNull();
  });

  it('drops a manifest tab whose manifest is invalid, after the router gives up', async () => {
    authFetch.mockResolvedValue({ ok: true, status: 200, text: async () => JSON.stringify({ template: 'nle-timeline' }), json: async () => ({}) });
    render(<SessionRendererPanel history={[tab('Ok', 'test-fine'), tab('Bad', 'manifest-router', { manifestUrl: '/api/artifact?key=bad.json' })]}
      currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(tabs()).toEqual(['Ok']));
    expect(screen.queryByTestId('manifest-router-error')).toBeNull();
  });

  it('shows the empty state when every tab is broken', async () => {
    render(<SessionRendererPanel history={[tab('A', 'test-boom')]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('No rendered content yet')).toBeInTheDocument());
  });

  it('remembers a failure for the browser session, but a re-rendered tab gets a fresh chance', async () => {
    const broken = tab('Shots', 'test-boom', { manifestUrl: '/v1.json' });
    const { unmount } = render(<SessionRendererPanel history={[tab('Video', 'test-fine'), broken]} currentIndex={1} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(tabs()).toEqual(['Video']));
    unmount();
    render(<SessionRendererPanel history={[tab('Video', 'test-fine'), broken]} currentIndex={0} onNavigate={vi.fn()} onClose={vi.fn()} />);
    expect(tabs()).toEqual(['Video']);  // hidden before it is ever clicked again
    const rerendered = tab('Shots', 'test-fine', { manifestUrl: '/v2.json' });
    expect(tabSignature(rerendered)).not.toBe(tabSignature(broken));
  });

  it('keeps healthy tabs fully interactive', async () => {
    const onNavigate = vi.fn();
    render(<SessionRendererPanel history={[tab('One', 'test-fine'), tab('Two', 'test-fine')]} currentIndex={0} onNavigate={onNavigate} onClose={vi.fn()} />);
    expect(tabs()).toEqual(['One', 'Two']);
    fireEvent.click(screen.getAllByRole('tab')[1]);
    expect(onNavigate).toHaveBeenCalledWith(1);
  });
});

describe('normalizeAnnotations', () => {
  it('maps the flat detection-sample shape (session 61deed9b) to timed boxes', () => {
    const out = normalizeAnnotations([{ x: 0.67, y: 0.19, w: 0.05, h: 0.11, label: 'Person_0', time: 16, frame: 'f.jpg' }]);
    expect(out).toEqual([{ entityId: 'Person_0', start: 16, end: 17, box: { x: 0.67, y: 0.19, width: 0.05, height: 0.11 }, label: 'Person_0' }]);
  });

  it('passes the canonical shape through and drops rows it cannot place', () => {
    const canonical = { entityId: 'e1', start: 1, end: 2, box: { x: 0, y: 0, width: 1, height: 1 } };
    const out = normalizeAnnotations([canonical, { label: 'x' }, { time: 'soon', x: 0, y: 0, w: 1, h: 1 }, null, 5]);
    expect(out).toEqual([canonical]);
    expect(normalizeAnnotations(undefined)).toEqual([]);
  });
});
