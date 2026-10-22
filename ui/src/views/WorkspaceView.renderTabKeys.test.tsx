/**
 * @vitest-environment jsdom
 *
 * Render tabs are addressed by their stable KEY, never their index (lineage spec R8.3).
 *
 * Regression: filling a composing tab when its compose settles, and removing a tab (which also
 * DELETEs its S3 manifest, permanently), both used the tab's ARRAY INDEX captured earlier. A server
 * render-list merge that lands in between reorders the list (an earlier render sorts in front), so
 * the index pointed at a different tab: the fill painted the wrong tab, and the remove deleted the
 * wrong render. These tests reorder the list between the capture and the action.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, act } from '@testing-library/react';
import { renderTabKey } from '../components/renderer/renderTabKey';
import type { RenderHistoryEntry } from '../components/renderer/types';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionRenderHistory: vi.fn(),
  updateSessionActivity: vi.fn(),
  editAsset: vi.fn(),
  discardChanges: vi.fn(),
  getSessionRenders: vi.fn(),
  createSessionRender: vi.fn(),
  deleteSessionRender: vi.fn(),
  renderTab: vi.fn(),
  renderTabOutcome: vi.fn(),
}));

// SessionRendererPanel stub records the props it was handed each render.
let rendererHistory: RenderHistoryEntry[] | null = null;
let rendererProps: { onRemoveTab?: (key: string) => void; onRenderTab?: (key: string) => void } = {};

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../lab-api', () => ({
  listSessions: mocks.listSessions,
  createSession: mocks.createSession,
  deleteSession: mocks.deleteSession,
  updateSessionRenderHistory: mocks.updateSessionRenderHistory,
  updateSessionActivity: mocks.updateSessionActivity,
  editAsset: mocks.editAsset,
  discardChanges: mocks.discardChanges,
  getSessionRenders: mocks.getSessionRenders,
  createSessionRender: mocks.createSessionRender,
  deleteSessionRender: mocks.deleteSessionRender,
  logPersistFailure: vi.fn(),
  resolveAgentConfig: vi.fn(() => new Promise(() => {})),
}));
vi.mock('../hooks/useJobs', () => ({
  jobsStore: { subscribeNewJob: vi.fn(() => () => {}), getSnapshot: () => ({ jobsById: new Map() }) },
  useJobs: () => ({ jobs: [] }),
}));
vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({
    mode: 'dev', displayName: 'Local Dev', email: undefined, initials: 'LD',
    sub: 'local-dev-user', signOut: vi.fn(),
  }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));
vi.mock('../components/ChatCanvas', () => ({ default: () => <div data-testid="chat-canvas">chat</div> }));
vi.mock('../components/CenterEditor', () => ({ default: () => <div data-testid="center-editor" /> }));
vi.mock('../components/SessionRendererPanel', () => ({
  default: (props: { history: RenderHistoryEntry[]; onRemoveTab?: (key: string) => void; onRenderTab?: (key: string) => void }) => {
    rendererHistory = props.history;
    rendererProps = props;
    return <div data-testid="renderer-panel" />;
  },
}));
vi.mock('../components/JobsPanel', () => ({
  default: () => <div data-testid="jobs-panel" />,
  rollUpRuns: () => [],
}));
vi.mock('../services/renderTabs', async () => {
  const actual = await vi.importActual<typeof import('../services/renderTabs')>('../services/renderTabs');
  return {
    ...actual,
    renderTab: mocks.renderTab,
    // WorkspaceView composes through renderTabOutcome; route it via the same spy (null = transport failure).
    renderTabOutcome: (urls: string[], sid?: string) => mocks.renderTabOutcome(urls, sid),
  };
});
vi.mock('../components/SpecialtyAgentEditor', () => ({ default: () => <div data-testid="specialty-agent-editor" /> }));

import WorkspaceView from './WorkspaceView';

function labContextValue(overrides: Record<string, unknown> = {}) {
  return {
    sessionId: 'session-1',
    switchSession: vi.fn(),
    editingTarget: null,
    setEditingTarget: vi.fn(),
    specialtyAgents: [],
    publishedSpecialtyAgents: [],
    loadingCategories: { skill: false, 'specialty-agent': false, 'tool-collection': false, workflow: false, hook: false },
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    skills: [],
    publishedSkills: [],
    ...overrides,
  };
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}


beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  rendererHistory = null;
  rendererProps = {};
  mocks.listSessions.mockResolvedValue([]);
  mocks.createSession.mockResolvedValue(undefined);
  mocks.deleteSession.mockResolvedValue(undefined);
  mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.deleteSessionRender.mockResolvedValue(undefined);
  mocks.useLabContext.mockReturnValue(labContextValue());
  // Default: WorkspaceView's renderTabOutcome goes through the renderTab spy (null = transport failure).
  mocks.renderTabOutcome.mockImplementation((urls: string[], sid?: string) => Promise.resolve(mocks.renderTab(urls, sid))
    .then((u: string | null) => (u ? { kind: 'manifest', manifestUrl: u } : { kind: 'unavailable' })));
});

const urlOf = (key: string) => `/api/artifact?key=${encodeURIComponent(key)}`;
const solo = (key: string, label: string, ts: number): RenderHistoryEntry => ({
  turnIndex: 0, timestamp: ts, label,
  blocks: [{ template: 'manifest-router', title: label, data: { manifestUrl: urlOf(key) } }],
});
const listed = (key: string, label: string, iso: string) => ({ key, label, createdAt: iso });
const manifestUrlOf = (e: RenderHistoryEntry) => (e.blocks[0]?.data as { manifestUrl?: string } | undefined)?.manifestUrl;

describe('render tabs are addressed by key, not index (R8.3)', () => {
  it('a compose that settles after the list reordered fills ITS tab, not the one now at its old index', async () => {
    let resolveList: (v: unknown[]) => void = () => {};
    mocks.getSessionRenders.mockImplementation(() => new Promise((r) => { resolveList = r; }));
    let resolveCompose: (url: string | null) => void = () => {};
    mocks.renderTab.mockImplementation(() => new Promise((r) => { resolveCompose = r; }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();

    // An uploaded clip opens a composing tab at index 0 and starts composing it.
    await act(async () => {
      window.dispatchEvent(new CustomEvent('lab:render-urls', {
        detail: { urls: ['https://cdn.example/clip.mp4'], label: 'Clip', sessionId: 'session-1' },
      }));
    });
    await flush();
    expect(mocks.renderTab).toHaveBeenCalledTimes(1);
    expect(rendererHistory!.map(e => e.label)).toEqual(['Clip']);

    // The server list lands: two earlier renders sort in FRONT of the composing tab.
    await act(async () => {
      resolveList([
        listed('artifacts/session-1/render/x.json', 'X', '2026-01-01T00:00:01Z'),
        listed('artifacts/session-1/render/y.json', 'Y', '2026-01-01T00:00:02Z'),
      ]);
    });
    await flush();
    expect(rendererHistory!.map(e => e.label)).toEqual(['X', 'Y', 'Clip']);

    await act(async () => { resolveCompose(urlOf('artifacts/session-1/render/clip.json')); });
    await flush();

    const [x, y, clip] = rendererHistory!;
    expect(manifestUrlOf(x)).toBe(urlOf('artifacts/session-1/render/x.json'));
    expect(manifestUrlOf(y)).toBe(urlOf('artifacts/session-1/render/y.json'));
    expect(clip.label).toBe('Clip');
    expect(manifestUrlOf(clip)).toBe(urlOf('artifacts/session-1/render/clip.json'));
    // The filled tab adopts its manifest key, so the server's listing of it supersedes it in place.
    expect(clip.key).toBe('artifacts/session-1/render/clip.json');
  });

  it('removing a tab after the list reordered removes (and deletes) THAT tab', async () => {
    const a = solo('artifacts/session-1/render/a.json', 'A', Date.parse('2026-01-01T00:00:02Z'));
    const b = solo('artifacts/session-1/render/b.json', 'B', Date.parse('2026-01-01T00:00:03Z'));
    localStorage.setItem('lab-render-session-1', JSON.stringify([a, b]));
    let resolveList: (v: unknown[]) => void = () => {};
    mocks.getSessionRenders.mockImplementation(() => new Promise((r) => { resolveList = r; }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();

    // Not mounted yet (the panel renders once there are tabs) — read B's key from the cache shape.
    const keyOfB = renderTabKey(b);

    // A merge brings in an EARLIER render: B moves from index 1 to index 2, A to index 1.
    await act(async () => {
      resolveList([
        listed('artifacts/session-1/render/x.json', 'X', '2026-01-01T00:00:01Z'),
        listed('artifacts/session-1/render/a.json', 'A', '2026-01-01T00:00:02Z'),
        listed('artifacts/session-1/render/b.json', 'B', '2026-01-01T00:00:03Z'),
      ]);
    });
    await flush();
    expect(rendererHistory!.map(e => e.label)).toEqual(['X', 'A', 'B']);

    await act(async () => { rendererProps.onRemoveTab!(keyOfB); });
    await flush();

    expect(rendererHistory!.map(e => e.label)).toEqual(['X', 'A']);
    expect(mocks.deleteSessionRender).toHaveBeenCalledTimes(1);
    expect(mocks.deleteSessionRender).toHaveBeenCalledWith('session-1', 'artifacts/session-1/render/b.json');
  });
});


// a2d759c2: a placeholder whose render has nothing to show used to sit on "Composing…" forever (or fill with an
// error record). A definitive outcome removes it; a transport failure keeps it, clickable again.
describe('a composing tab resolves to its outcome', () => {
  const openComposing = async () => {
    // One real tab keeps the renderer panel mounted (it unmounts with zero tabs, freezing the captured props).
    mocks.getSessionRenders.mockResolvedValue([listed('artifacts/session-1/render/x.json', 'X', '2026-01-01T00:00:01Z')]);
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    await act(async () => {
      window.dispatchEvent(new CustomEvent('lab:render-urls', {
        detail: { urls: ['https://cdn.example/clip.mp4'], label: 'Clip', sessionId: 'session-1' },
      }));
    });
    await flush();
  };

  it.each(['no_view', 'failed'] as const)('%s removes the placeholder', async (kind) => {
    mocks.renderTabOutcome.mockResolvedValue({ kind });
    await openComposing();
    await flush();
    expect((rendererHistory ?? []).map(e => e.label)).toEqual(['X']);
  });

  it('a transport failure keeps the tab and makes it clickable again', async () => {
    mocks.renderTabOutcome.mockResolvedValue({ kind: 'unavailable' });
    await openComposing();
    await flush();
    const clip = (rendererHistory ?? []).find(e => e.label === 'Clip');
    expect(clip).toBeDefined();
    expect(clip!.status).not.toBe('composing');
  });
});
