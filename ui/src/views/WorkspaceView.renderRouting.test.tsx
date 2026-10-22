/**
 * @vitest-environment jsdom
 *
 * WorkspaceView render routing — the parent side of the cross-session leak fix.
 *
 * ChatCanvas now addresses every render block to the session that ORIGINATED
 * the turn (captured at dispatch time). WorkspaceView owns the single Renderer
 * for the session ON SCREEN, so it must route on that id:
 *
 *   - a render from a BACKGROUND (not-on-screen) session is persisted into that
 *     session (localStorage + DynamoDB + the in-memory row) and NEVER added as
 *     a tab to the viewed session's Renderer — this is the reported bug where
 *     other sessions' results showed up as extra "Run Model" tabs;
 *   - a render from the ACTIVE session shows in the Renderer as before.
 *
 * ChatCanvas and the heavy panels are stubbed; the ChatCanvas stub captures the
 * props so the test can invoke `onRenderBlock(entry, originatingSessionId)`
 * exactly as the real component would from its stream callback.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { nameRenderEntry, batchSourcesLine } from './WorkspaceView';
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
  renderTab: vi.fn(),
}));

// Captured ChatCanvas props — the stub records them each render so the test can
// call onRenderBlock directly.
let capturedChatProps: {
  onRenderBlock?: (entry: RenderHistoryEntry, originatingSessionId: string) => void;
  onRenderPending?: (pending: boolean, originatingSessionId: string) => void;
} = {};

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
  resolveAgentConfig: vi.fn(() => new Promise(() => {})),
}));
// projects/api: WorkspaceView reads `projectsEnabled()` on mount (task 13.8). Keep other exports real,
// force Projects OFF.
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  projectsEnabled: vi.fn().mockResolvedValue(false),
}));
vi.mock('../hooks/useJobs', () => ({
  jobsStore: { subscribeNewJob: vi.fn(() => () => {}), getSnapshot: () => ({ jobsById: new Map() }) },
  useJobs: () => ({ jobs: [] }),
}));
// renderTab is the one network call a composing tab makes; spy it so a test can see WHICH tabs
// were composed. Everything else in the module stays real.
vi.mock('../services/renderTabs', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../services/renderTabs')>()),
  renderTab: mocks.renderTab,
  // WorkspaceView composes through renderTabOutcome; route it via the same spy (null = transport failure).
  renderTabOutcome: (urls: string[], sid?: string) => Promise.resolve(mocks.renderTab(urls, sid))
    .then((u: string | null) => (u ? { kind: 'manifest', manifestUrl: u } : { kind: 'unavailable' })),
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
vi.mock('../components/ChatCanvas', () => ({
  default: (props: typeof capturedChatProps) => {
    capturedChatProps = props;
    return <div data-testid="chat-canvas">chat</div>;
  },
}));
vi.mock('../components/CenterEditor', () => ({ default: () => <div data-testid="center-editor" /> }));
// Projects_Surface center mode (task 13.8) — marker stub; Projects stay disabled below.
vi.mock('../components/projects/ProjectsSurface', () => ({
  default: () => <div data-testid="projects-surface">projects</div>,
}));
vi.mock('../components/SessionRendererPanel', () => ({
  default: () => <div data-testid="renderer-panel">renderer</div>,
}));
vi.mock('../components/JobsPanel', () => ({
  default: () => <div data-testid="jobs-panel" />,
  rollUpRuns: () => [],
}));
vi.mock('../components/SpecialtyAgentEditor', () => ({
  default: () => <div data-testid="specialty-agent-editor" />,
}));

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
    // The real context types both as required arrays, and the view now reads them
    // directly to build the sidebar's Skills list. Omitting them made this stub
    // looser than the thing it stands for, which is how it kept passing.
    skills: [],
    publishedSkills: [],
    ...overrides,
  };
}

function makeEntry(label: string, key: string): RenderHistoryEntry {
  return {
    turnIndex: 0,
    timestamp: Date.now(),
    label,
    blocks: [{ template: 'manifest-router', title: label, data: { manifestUrl: `/api/artifact?key=${key}` } }],
  };
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  capturedChatProps = {};
  mocks.listSessions.mockResolvedValue([]);
  mocks.createSession.mockResolvedValue(undefined);
  mocks.deleteSession.mockResolvedValue(undefined);
  mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getSessionRenders.mockResolvedValue([]);
  mocks.renderTab.mockResolvedValue(null);
  mocks.useLabContext.mockReturnValue(labContextValue());
});

describe('nameRenderEntry', () => {
  it('passes a labeled entry through unchanged', () => {
    const e = makeEntry('Shot Segmentation', 'k1');
    expect(nameRenderEntry([], e)).toBe(e);
  });

  it('numbers unlabeled entries against existing history', () => {
    const unlabeled: RenderHistoryEntry = {
      turnIndex: 0, timestamp: Date.now(),
      blocks: [{ template: 'manifest-router', title: 'Render Output', data: {} }],
    };
    const first = nameRenderEntry([], unlabeled);
    expect(first.label).toBe('Render Output 1');
    const second = nameRenderEntry([first], unlabeled);
    expect(second.label).toBe('Render Output 2');
    expect(second.blocks[0].title).toBe('Render Output 2');
  });
});

describe('WorkspaceView — render routing by originating session', () => {
  it('(c) persists a BACKGROUND session render into that session, not the active Renderer', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    // Active session is 'session-1'. Renderer is empty (nothing to show yet).
    expect(screen.queryByTestId('renderer-panel')).toBeNull();
    expect(capturedChatProps.onRenderBlock).toBeTypeOf('function');

    await act(async () => {
      capturedChatProps.onRenderBlock!(makeEntry('Job on other', 'kbg'), 'other-session');
    });

    // Routed to the ORIGINATING session's persistence.
    expect(mocks.updateSessionRenderHistory).toHaveBeenCalledWith(
      'other-session',
      expect.arrayContaining([expect.objectContaining({ label: 'Job on other' })]),
    );
    const bg = JSON.parse(localStorage.getItem('lab-render-other-session') || '[]');
    expect(bg).toHaveLength(1);

    // The viewed session's Renderer gained NOTHING — no leaked tab.
    expect(screen.queryByTestId('renderer-panel')).toBeNull();
    expect(localStorage.getItem('lab-render-session-1')).toBeNull();
    expect(mocks.updateSessionRenderHistory).not.toHaveBeenCalledWith('session-1', expect.anything());
  });

  it('(c) an ACTIVE session render triggers a server reconcile and shows in the Renderer', async () => {
    // Server-authoritative tabs: an active render block no longer authors a client tab. It signals
    // "a render for this session just landed on S3", so WorkspaceView re-lists via getSessionRenders
    // and renders the server-composed result. This is what removes the flash / DAG-fan-out extra tabs.
    // Start with an EMPTY server list so the panel is empty after mount …
    mocks.getSessionRenders.mockResolvedValue([]);
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    expect(screen.queryByTestId('renderer-panel')).toBeNull();

    // … then the render lands on S3 (server now lists it) and the stream block fires the reconcile.
    mocks.getSessionRenders.mockResolvedValue([
      { key: 'artifacts/session-1/render/active.json', label: 'Active job', createdAt: new Date().toISOString(),
        manifest: { id: 'a', template: 'nle-timeline', duration: 10, clips: [], tracks: [{ id: 'v', type: 'video', src: 'https://cdn/x.mp4', start: 0, end: 10 }] } },
    ]);
    await act(async () => {
      capturedChatProps.onRenderBlock!(makeEntry('Active job', 'kact'), 'session-1');
    });
    // Let the reconcile ladder's immediate timer fire + the getSessionRenders promise resolve.
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    await flush();

    // The active render was reconciled from the SERVER (not authored client-side) …
    expect(mocks.getSessionRenders).toHaveBeenCalledWith('session-1');
    // … and the server-listed render populated the Renderer for the on-screen session.
    expect(screen.getByTestId('renderer-panel')).toBeTruthy();
    // The background persistence path was NOT taken for the active session.
    expect(mocks.updateSessionRenderHistory).not.toHaveBeenCalledWith('other-session', expect.anything());
  });

  it('(flicker) a notification click does NOT author a client URL tab (no lab:render-urls dispatch)', async () => {
    // Root of the reported notification→render flicker: handleOpenNotification used to ALSO fire
    // lab:render-urls → renderUrlsInPanel, appending a duplicate 'composing' tab that the server
    // reconcile then removed (a tab appearing then vanishing). The tab now comes purely from the
    // server render list; the click must not author a client URL tab.
    const { notificationsStore, __resetNotificationsForTests } = await import('../services/notifications');
    __resetNotificationsForTests();
    mocks.getSessionRenders.mockResolvedValue([]);
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();

    const dispatchSpy = vi.spyOn(window, 'dispatchEvent');
    await act(async () => {
      notificationsStore.pushFromTerminal({
        jobId: 'jflick', actorId: 'a', sessionId: 'session-1', trioId: 'face-recognition|recognize_faces',
        gatewayId: '__aws_sfn__', status: 'SUCCEEDED', startAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(), terminalAt: new Date().toISOString(),
        hasRenderableArtifacts: true, artifactUrls: ['https://cdn/x.mp4'], lastError: null,
        runId: null, nodeId: null, dependsOn: [],
      } as never);
    });
    // Click the completion banner → onOpen → handleOpenNotification.
    await act(async () => { fireEvent.click(screen.getByText(/finished/i)); });

    const urlTabEvents = dispatchSpy.mock.calls.filter(
      ([e]) => e instanceof CustomEvent && e.type === 'lab:render-urls',
    );
    expect(urlTabEvents, 'notification click must NOT author a client URL tab (that caused the flicker)').toHaveLength(0);
    dispatchSpy.mockRestore();
    __resetNotificationsForTests();
  });

  it('(41b19dd4) a multi-video upload composes EVERY video, not just the last', async () => {
    // ChatCanvas fires one lab:render-media per uploaded video in the same tick. A single
    // pending-tab slot kept only the last index under React batching, so only the second
    // video (tidal) composed and Meridian sat at 'composing' forever.
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    await flush();
    const a = 'https://cdn/artifacts/session-1/input/1-Meridian.mp4';
    const b = 'https://cdn/artifacts/session-1/input/2-tidal.mp4';
    await act(async () => {
      window.dispatchEvent(new CustomEvent('lab:render-media', { detail: { url: a, name: 'Meridian.mp4', sessionId: 'session-1' } }));
      window.dispatchEvent(new CustomEvent('lab:render-media', { detail: { url: b, name: 'tidal.mp4', sessionId: 'session-1' } }));
    });
    await flush();
    const composed = mocks.renderTab.mock.calls.map(([urls]) => (urls as string[])[0]);
    expect(new Set(composed)).toEqual(new Set([a, b]));
  });
});


describe('batchSourcesLine — labeled sources appended to a batch clip action', () => {
  it('names each clip with its url, no capture point for trim/clip', () => {
    const line = batchSourcesLine([
      { label: 'Segment 1', src: 'https://cdn/a.wav' },
      { label: 'Segment 2', src: 'https://cdn/b.wav' },
    ]);
    expect(line).toBe('"Segment 1"=https://cdn/a.wav; "Segment 2"=https://cdn/b.wav');
  });

  it('appends @<t>s when a clip carries a frame-extract capture point', () => {
    const line = batchSourcesLine([
      { label: 'Take 1', src: 'https://cdn/v1.mp4', at: 2.5 },
      { label: 'Take 2', src: 'https://cdn/v2.mp4', at: 1 },
    ]);
    expect(line).toBe('"Take 1"=https://cdn/v1.mp4@2.50s; "Take 2"=https://cdn/v2.mp4@1.00s');
  });

  it('is empty-safe', () => {
    expect(batchSourcesLine([])).toBe('');
  });
});
