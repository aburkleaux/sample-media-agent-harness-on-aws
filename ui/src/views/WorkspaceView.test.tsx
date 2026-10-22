/**
 * @vitest-environment jsdom
 *
 * Example test for WorkspaceView center-mode switching (task 6.7).
 * Covers the WorkspaceView-owned pieces of the Custom Agent center mode:
 *   - Req 1.1  Clicking the Agents nav switches centerMode to 'custom-agent'
 *              and renders the custom-agent center panel (the agent list).
 *   - Req 1.6  The shared "+ New agent" action mounts the editor in create
 *              mode (assetId === null).
 *   - Req 1.5  Selecting an agent row mounts the editor for that agent
 *              (assetId === the selected agent's id).
 *
 * WorkspaceView pulls in a large subtree (chat, jobs, renderer, workflows,
 * the real editor, plus lab-api session calls and the LabContext). The heavy
 * children and the data dependencies are stubbed so the test can mount the
 * view and exercise ONLY the center-mode switching wiring. The real
 * SessionSidebar is kept so the Agents nav click is exercised end-to-end.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionRenderHistory: vi.fn(),
  updateSessionActivity: vi.fn(),
  editAsset: vi.fn(),
  discardChanges: vi.fn(),
  deleteSpecialtyAgentDraft: vi.fn(),
  publishAsset: vi.fn(),
  previewAgentPurge: vi.fn(),
  purgeAgent: vi.fn(),
}));

// --- LabContext: WorkspaceView reads sessionId/switchSession/editingTarget/
//     setEditingTarget/specialtyAgents. Provide a controllable stub. ---
vi.mock('../LabContext', () => ({
  useLabContext: mocks.useLabContext,
}));

// --- lab-api: WorkspaceView fires session CRUD on mount; resolve them all
//     so effects settle without touching a backend. ---
vi.mock('../lab-api', () => ({
  listSessions: mocks.listSessions,
  createSession: mocks.createSession,
  deleteSession: mocks.deleteSession,
  updateSessionRenderHistory: mocks.updateSessionRenderHistory,
  updateSessionActivity: mocks.updateSessionActivity,
  editAsset: mocks.editAsset,
  discardChanges: mocks.discardChanges,
  deleteSpecialtyAgentDraft: mocks.deleteSpecialtyAgentDraft,
  deleteSkillDraft: vi.fn(),
  publishAsset: mocks.publishAsset,
  // PublishedAgentView embeds AgentTestPanel, which resolves the agent's
  // config on mount. Left unresolved so these tests observe only the
  // read-only/fork behavior they're about — AgentTestPanel has its own suite.
  resolveAgentConfig: vi.fn(() => new Promise(() => {})),
}));

// projects/api: WorkspaceView reads `projectsEnabled()` on mount (task 13.8). Keep every other export
// real, force Projects OFF so the sidebar nav entry and surface never appear in these tests.
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  projectsEnabled: vi.fn().mockResolvedValue(false),
}));

// --- Jobs store + hook: WorkspaceView subscribes to new jobs; SessionSidebar
//     reads useJobs(). Stub both from the shared module. ---
// --- capability-api: the purge dialog's only dependency. Real `CapabilityApiError`
//     class, because the dialog branches on `instanceof` to turn a typed code
//     into advice ("retry to finish" vs "you are not allowed"). ---
vi.mock('../capability-api', () => ({
  // The dialog is category-generic now, so it calls the parameterized pair. The
  // agent-bound aliases stay mocked for any other caller.
  previewPurge: (_category: string, assetId: string) => mocks.previewAgentPurge(assetId),
  purgeAsset: (_category: string, assetId: string) => mocks.purgeAgent(assetId),
  previewAgentPurge: mocks.previewAgentPurge,
  purgeAgent: mocks.purgeAgent,
  CapabilityApiError: class CapabilityApiError extends Error {
    constructor(readonly status: number, message: string, readonly code?: string) {
      super(message);
      this.name = 'CapabilityApiError';
    }
  },
}));

vi.mock('../hooks/useJobs', () => ({
  jobsStore: { subscribeNewJob: vi.fn(() => () => {}) },
  useJobs: () => ({ jobs: [] }),
}));

// --- SessionSidebar's app-level deps (unrelated to center-mode switching). ---
vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({
    mode: 'dev',
    displayName: 'Local Dev',
    email: undefined,
    initials: 'LD',
    sub: 'local-dev-user',
    signOut: vi.fn(),
  }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));

// --- Heavy center/right-panel children stubbed to recognizable markers. ---
// The first-run tour opens its own dialog on a fresh localStorage; it has its own tests.
vi.mock('../components/tour/HarnessTour', () => ({ default: () => null }));
vi.mock('../components/ChatCanvas', () => ({
  default: () => <div data-testid="chat-canvas">chat</div>,
}));
vi.mock('../components/CenterEditor', () => ({
  default: () => <div data-testid="center-editor">center-editor</div>,
}));
// Projects_Surface center mode (task 13.8). A marker stub keeps these tests off the real surface and
// its `/projects` + session-store traffic; projects stay disabled below so the nav entry never renders.
vi.mock('../components/projects/ProjectsSurface', () => ({
  default: () => <div data-testid="projects-surface">projects</div>,
}));
vi.mock('../components/SessionRendererPanel', () => ({
  default: () => <div data-testid="renderer-panel">renderer</div>,
}));
// Moved here from the retired Settings > Capabilities > Agents tab. It resolves
// the built-in sub-agent runtimes on mount, so it is stubbed to a marker.
vi.mock('../components/BuiltInAgentsBrowser', () => ({
  default: () => <div data-testid="built-in-agents">built-in agents</div>,
}));
/*
 * Consumes `openDetail` the way the real component does — applying it and calling
 * `onDetailOpened` from an effect — because the host CLEARING it on handover is the
 * contract under test. A stub that merely displayed the prop would have reported the
 * bug as healthy: the value only resurfaced on a later MOUNT.
 *
 * `data-opendetail` is the live prop; `data-opendetail-seen` is sticky per mount, so
 * a test can tell "was handed a target" from "is still holding one".
 */
vi.mock('../components/SkillsCenter', () => ({
  default: ({ onOpenEditor, openDetail, onDetailOpened }: {
    onOpenEditor: (t: { category: 'skill'; assetId: string | null }) => void;
    openDetail?: { kind: string; assetId?: string; version?: number } | null;
    onDetailOpened?: () => void;
  }) => {
    const describe_ = (d?: { kind: string; assetId?: string; version?: number } | null) =>
      d ? `${d.kind}:${d.assetId}:${d.version ?? ''}` : '';
    const [seen, setSeen] = React.useState('');
    React.useEffect(() => {
      if (!openDetail) return;
      setSeen(describe_(openDetail));
      onDetailOpened?.();
    }, [openDetail, onDetailOpened]);
    return (
    <div
      data-testid="skills-center"
      data-opendetail={describe_(openDetail)}
      data-opendetail-seen={seen}
    >
      skills-center
      <button onClick={() => onOpenEditor({ category: 'skill', assetId: 'skill-1' })}>
        stub-edit-skill
      </button>
    </div>
    );
  },
}));
vi.mock('../components/JobsPanel', () => ({
  default: () => <div data-testid="jobs-panel">jobs</div>,
  // SessionSidebar consolidates jobs into run rows via this helper.
  rollUpRuns: () => [],
}));
// NOTE: `WorkflowsCenter` was deleted on staging (the center aggregate view
// replaced it), so there is nothing left to mock.

// SpecialtyAgentEditor stub records the assetId it was mounted with so the
// test can distinguish create mode (null) from edit mode (an agent id). It also
// exposes close-saved / close-unsaved buttons so tests can drive the parent's
// discard-on-close behavior.
vi.mock('../components/SpecialtyAgentEditor', () => ({
  default: ({ assetId, onClose }: { assetId: string | null; onClose: (saved: boolean) => void }) => (
    <div data-testid="specialty-agent-editor" data-asset-id={assetId === null ? 'null' : String(assetId)}>
      {assetId === null ? 'editor:create' : `editor:${assetId}`}
      <button onClick={() => onClose(false)}>stub-close-unsaved</button>
      <button onClick={() => onClose(true)}>stub-close-saved</button>
    </div>
  ),
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
    loadingCategories: { 'skill': false, 'specialty-agent': false, 'tool-collection': false, 'workflow': false, 'hook': false },
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    // Category loading is lazy on staging; the Agents nav pulls its own
    // inventory via this, so the mock must provide it.
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    // Delete on an agent tile removes the row optimistically before the
    // refresh reconciles.
    optimisticRemoveSpecialtyAgent: vi.fn(),
    // Skills moved into the main nav, so this view now reads the skill
    // inventory too (sidebar list + the Skills centre). BOTH halves: the sidebar
    // merges published lineages in, because publishing consumes the working copy
    // and a drafts-only list reports "0" once you have published everything.
    skills: [],
    publishedSkills: [],
    ...overrides,
  };
}

const samplePublishedAgent = {
  assetId: 'pub-agent-1',
  name: 'Shipped Agent',
  version: 1,
  current: true,
  scope: 'private' as const,
  createdAt: Date.now() - 3 * 24 * 60 * 60 * 1000,
  updatedAt: Date.now() - 24 * 60 * 60 * 1000,
};

const sampleAgent = {
  assetId: 'agent-1',
  name: 'Research Agent',
  updatedAt: Date.now() - 2 * 24 * 60 * 60 * 1000,
};

/** The purge preview for `samplePublishedAgent`: one private version, nothing else. */
const samplePreflight = {
  assetId: 'pub-agent-1',
  category: 'specialty-agent',
  versions: [1],
  scopes: ['private'],
  workingCopyExists: false,
  externalAccessLive: false,
  prefixes: ['users/u/published/specialty-agents/pub-agent-1/v1/'],
  references: [],
  partial: [],
};

/**
 * The center Agents panel, scoped.
 *
 * The real SessionSidebar is kept in these tests and it lists the same agents,
 * so an unscoped `getByText(<agent name>)` matches twice. Every assertion about
 * the center list has to be scoped to it.
 */
function centerAgents(): HTMLElement {
  return screen.getByTestId('agents-surface');
}

describe('WorkspaceView — Custom Agent center-mode switching', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockResolvedValue(undefined);
    mocks.deleteSession.mockResolvedValue(undefined);
    mocks.deleteSpecialtyAgentDraft.mockResolvedValue(undefined);
    mocks.publishAsset.mockResolvedValue({ assetId: 'agent-1', version: 1 });
    mocks.previewAgentPurge.mockResolvedValue(samplePreflight);
    mocks.purgeAgent.mockResolvedValue({
      dryRun: false, assetId: 'pub-agent-1', category: 'specialty-agent',
      versionsDeleted: [1], workingCopyDeleted: false,
      prefixesDeleted: ['users/u/published/specialty-agents/pub-agent-1/v1/'],
      prefixesOrphaned: [], outboxEvents: 0, preflight: samplePreflight,
    });
    mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
    mocks.updateSessionActivity.mockResolvedValue(undefined);
    mocks.editAsset.mockResolvedValue({ assetId: 'wc-forked-1', category: 'specialty-agent', name: 'Shipped Agent' });
    mocks.discardChanges.mockResolvedValue({ discarded: true, assetId: 'wc-forked-1' });
    mocks.useLabContext.mockReturnValue(labContextValue());
  });

  it('switches to the custom-agent center and renders the agent list when the Agents nav is clicked (Req 1.1)', () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    // Chat is the initial center mode.
    expect(screen.getByTestId('chat-canvas')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // The custom-agent center panel is now mounted (its subtitle is unique to
    // the CustomAgentCenterList), and the chat surface is hidden.
    expect(
      screen.getByText('Create and configure custom agents, then test and publish them.'),
    ).toBeTruthy();
    // Chat stays MOUNTED but display:none — staging keeps it alive across nav
    // so switching centers doesn't tear down in-flight chat state. Assert it is
    // hidden rather than absent: walk up from the stub to the pane that owns
    // the display toggle.
    let node: HTMLElement | null = screen.getByTestId('chat-canvas');
    let hidden = false;
    while (node) {
      if (node.style?.display === 'none') { hidden = true; break; }
      node = node.parentElement;
    }
    expect(hidden).toBe(true);
    // No agents yet → empty state prompt. It NAMES the sidebar create action
    // rather than repeating it as a button. Match the center's own sentence:
    // the SIDEBAR renders "No custom agents yet. Create one to get started."
    // too, so a looser matcher hits both panes.
    expect(screen.getByText(/Click "\+ New agent" to add one/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '+ New agent' })).toBeNull();
    // No editor open while on the list view.
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
  });

  it('shows a loading state (not the empty state) while the agent list is still fetching', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [],
      loadingCategories: { 'skill': false, 'specialty-agent': true, 'tool-collection': false, 'workflow': false, 'hook': false },
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // While loading, the reassuring loading copy shows and the "empty" prompt is suppressed.
    expect(screen.getByText('Loading agents…')).toBeTruthy();
    expect(screen.queryByText(/Click "\+ New agent" to add one/)).toBeNull();
  });

  it('mounts the editor in create mode when the sidebar New agent action is clicked (Req 1.6)', () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // The sidebar action is the ONE create affordance on this surface — the
    // header and empty-state buttons were both removed as duplicates of it.
    fireEvent.click(screen.getByRole('button', { name: 'New agent' }));

    const editor = screen.getByTestId('specialty-agent-editor');
    expect(editor).toBeTruthy();
    // Create mode → assetId is null.
    expect(editor.getAttribute('data-asset-id')).toBe('null');
    expect(editor.textContent).toContain('editor:create');
  });

  it('mounts the editor for the selected agent when its row is clicked (Req 1.5)', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // Scope to the center-panel list (the sidebar also lists the agent) via
    // its unique heading, then click the agent tile.
    const centerList = centerAgents();
    fireEvent.click(within(centerList).getByText('Research Agent'));

    const editor = screen.getByTestId('specialty-agent-editor');
    expect(editor).toBeTruthy();
    // Edit mode → assetId is the selected agent's id.
    expect(editor.getAttribute('data-asset-id')).toBe('agent-1');
    expect(editor.textContent).toContain('editor:agent-1');
  });

  it('lists a PUBLISHED agent (no draft) and opens it read-only on click (BUG-6)', async () => {
    // Only a published agent exists — the drafts partition is empty (the state a
    // fully-published agent leaves behind). It must still appear in the list.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    const centerList = centerAgents();
    // The published agent is visible (not the "No custom agents yet" empty state).
    expect(within(centerList).getByText('Shipped Agent')).toBeTruthy();
    expect(screen.queryByText(/Click "\+ New agent" to add one/)).toBeNull();

    // Clicking it opens a READ-ONLY view (no fork, no state change).
    fireEvent.click(within(centerList).getByText('Shipped Agent'));
    expect(screen.getByText('Published agent (read-only). Edit forks it into a working copy.')).toBeTruthy();
    expect(mocks.editAsset).not.toHaveBeenCalled();
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
  });

  it('scopes the inventory to the picked All / Drafts / Published state', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [sampleAgent],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    const centerList = centerAgents();
    // Scope to the center: the sidebar lists the same agent names.
    // Filter names carry counts ("All 2"), so match on the label prefix.
    const tab = (label: string) => screen.getByRole('button', { name: new RegExp(`^${label}`) });

    // All → both sections.
    expect(tab('All')).toHaveAttribute('aria-pressed', 'true');
    expect(within(centerList).getByText('Research Agent')).toBeTruthy();
    expect(within(centerList).getByText('Shipped Agent')).toBeTruthy();

    // Published → drafts hidden.
    fireEvent.click(tab('Published'));
    expect(within(centerList).queryByText('Research Agent')).toBeNull();
    expect(within(centerList).getByText('Shipped Agent')).toBeTruthy();

    // Drafts → published hidden.
    fireEvent.click(tab('Drafts'));
    expect(within(centerList).getByText('Research Agent')).toBeTruthy();
    expect(within(centerList).queryByText('Shipped Agent')).toBeNull();
  });

  it('says so rather than blanking when the picked state is empty', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [sampleAgent],
      publishedSpecialtyAgents: [],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(screen.getByRole('button', { name: /^Published/ }));

    expect(screen.getByText('No published agents yet.')).toBeTruthy();
  });

  it('forks a published agent into a working copy only when Edit is clicked (BUG-6 / option 2)', async () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    const centerList = centerAgents();
    fireEvent.click(within(centerList).getByText('Shipped Agent'));

    // Now click Edit → forks and opens the editor on the returned working copy.
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(mocks.editAsset).toHaveBeenCalledWith('specialty-agent', 'pub-agent-1');
    const editor = await screen.findByTestId('specialty-agent-editor');
    expect(editor.getAttribute('data-asset-id')).toBe('wc-forked-1');
  });

  it('forks from the LIST ROW Edit too, not only from the read-only view', async () => {
    // The row's Edit used to route to `onSelect`, opening the editor on a
    // published lineage id — and publishing consumes the working copy, so there
    // is no draft to read. The draft-only read returned nothing, giving a blank
    // form and "Agent not found", with no way to iterate on a published agent
    // from the list at all. The test above passed throughout, because it clicks
    // the detail view's Edit, which was already correct.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // The ROW's Edit, addressed by its per-agent label so this cannot
    // accidentally hit the detail view's generic "Edit".
    fireEvent.click(screen.getByRole('button', { name: 'Edit Shipped Agent' }));

    expect(mocks.editAsset).toHaveBeenCalledWith('specialty-agent', 'pub-agent-1');
    const editor = await screen.findByTestId('specialty-agent-editor');
    // The forked WORKING COPY, never the published lineage id.
    expect(editor.getAttribute('data-asset-id')).toBe('wc-forked-1');
    expect(editor.getAttribute('data-asset-id')).not.toBe('pub-agent-1');
  });

  it('opens the EXISTING working copy when EditAsset reports one already exists', async () => {
    // `EditAsset` surfaces this as a 200 carrying an `error` field rather than
    // throwing, so `assetId` is absent. Opening the editor on `undefined` would
    // reproduce the very blank form this path exists to prevent; the working copy
    // reuses the lineage id, so that is the correct target.
    mocks.editAsset.mockResolvedValue({
      error: 'working_copy_exists',
      message: 'Working copy already exists for specialty-agent#pub-agent-1.',
    });
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit Shipped Agent' }));

    const editor = await screen.findByTestId('specialty-agent-editor');
    expect(editor.getAttribute('data-asset-id')).toBe('pub-agent-1');

    // And closing unsaved must NOT discard it: this click did not create it, so
    // it may hold edits from an earlier session.
    fireEvent.click(screen.getByRole('button', { name: 'stub-close-unsaved' }));
    await Promise.resolve();
    expect(mocks.discardChanges).not.toHaveBeenCalled();
  });

  it('discards the forked working copy when the editor closes WITHOUT saving (no lingering draft)', async () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    const centerList = centerAgents();
    fireEvent.click(within(centerList).getByText('Shipped Agent'));
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await screen.findByTestId('specialty-agent-editor');

    // Close WITHOUT saving → the just-forked working copy is discarded.
    fireEvent.click(screen.getByRole('button', { name: 'stub-close-unsaved' }));
    await waitFor(() => expect(mocks.discardChanges).toHaveBeenCalledWith('specialty-agent', 'wc-forked-1'));
  });

  it('KEEPS the working copy when the editor closes after a save/publish', async () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    const centerList = centerAgents();
    fireEvent.click(within(centerList).getByText('Shipped Agent'));
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await screen.findByTestId('specialty-agent-editor');

    // Close reporting saved=true → no discard.
    fireEvent.click(screen.getByRole('button', { name: 'stub-close-saved' }));
    await Promise.resolve();
    expect(mocks.discardChanges).not.toHaveBeenCalled();
  });

  // --- Consolidated Agents surface -------------------------------------
  //
  // These three capabilities used to live ONLY in Settings > Capabilities >
  // Agents, which duplicated this view. That tab is gone, so its unique
  // behaviour is asserted here — it was untested before the move, which is
  // exactly why removing the tab looked safe when it was not.

  it('filters the agent tiles by search', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [sampleAgent, { assetId: 'agent-2', name: 'Editing Agent', updatedAt: Date.now() }],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    const search = screen.getByRole('textbox', { name: 'Search agents' });
    fireEvent.change(search, { target: { value: 'research' } });

    const center = centerAgents();
    expect(within(center).getByText('Research Agent')).toBeTruthy();
    expect(within(center).queryByText('Editing Agent')).toBeNull();
  });

  it('distinguishes "no match" from "no agents at all"', () => {
    // A typo in the search box must not read as the inventory being wiped.
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.change(screen.getByRole('textbox', { name: 'Search agents' }), {
      target: { value: 'zzzz' },
    });
    expect(screen.getByText(/No agents match/)).toBeTruthy();
    expect(screen.queryByText(/Click "\+ New agent" to add one/)).toBeNull();
  });

  it('reveals the built-in agents browser only when toggled on', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // Off by default: these are read-only platform runtimes, not authored assets.
    expect(screen.queryByTestId('built-in-agents')).toBeNull();
    fireEvent.click(screen.getByRole('switch', { name: 'Built-in agents' }));
    expect(screen.getByTestId('built-in-agents')).toBeTruthy();
  });

  it('deletes a working copy behind a two-step confirm', async () => {
    const optimisticRemoveSpecialtyAgent = vi.fn();
    const refreshCategory = vi.fn().mockResolvedValue(undefined);
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [sampleAgent], optimisticRemoveSpecialtyAgent, refreshCategory,
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    // First click only arms the confirm — nothing is deleted yet.
    fireEvent.click(screen.getByRole('button', { name: 'Delete Research Agent' }));
    expect(mocks.deleteSpecialtyAgentDraft).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete Research Agent' }));
    await waitFor(() => expect(mocks.deleteSpecialtyAgentDraft).toHaveBeenCalledWith('agent-1'));
    await waitFor(() => expect(refreshCategory).toHaveBeenCalledWith('specialty-agent'));

    // Deliberately NOT optimistic. Dropping the row before the refresh settles
    // re-flows the list under the pointer, and a click can then land on the row
    // that moves up — observed in the deployed app as a stray Edit that forked a
    // published agent. The refresh is what removes the row.
    expect(optimisticRemoveSpecialtyAgent).not.toHaveBeenCalled();
  });

  it('does not open the editor when the delete controls are clicked', () => {
    // The buttons sit inside a click-to-open tile; without stopPropagation,
    // cancelling a delete would drop the user into the editor for the agent
    // they were about to remove.
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(screen.getByRole('button', { name: 'Delete Research Agent' }));
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
  });

  it('has no header create button once agents exist', () => {
    // Two identical primary buttons a glance apart — the sidebar's shared "New
    // agent" and a header one — was pure duplication. The sidebar keeps it; the
    // empty-state CTA below is the only in-pane path, and only when the list is
    // empty.
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expect(screen.queryByRole('button', { name: '+ New agent' })).toBeNull();
    // The sidebar's morphing action is still there.
    expect(screen.getByRole('button', { name: 'New agent' })).toBeTruthy();
  });

  it('offers no delete on the core Task Agent', () => {
    // The Lab service appends the built-in Task Agent to list results as a
    // VIRTUAL row (`assetId: "core-task-agent"`, no AssetIndex record), so a
    // delete addressed to it can only ever fail. Verified against sandbox: the
    // owner's OWNED partition has no such row. The retired settings tab DID
    // render a Delete here — a dead destructive button.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [{
        assetId: 'core-task-agent', name: 'Task Agent', isCoreAgent: true,
        updatedAt: Date.now(),
      }],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expect(within(centerAgents()).getByText('Task Agent')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Delete Task Agent' })).toBeNull();
  });

  it('does not two-step-confirm a published agent in the row', () => {
    // Delete on a published row is a PURGE, a different operation from the draft
    // delete beside it: it destroys immutable versions and their S3 artifacts and
    // cannot be undone. So it must not reuse the row's inline "Confirm delete",
    // which sits under the pointer — it opens a dialog instead.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expect(within(centerAgents()).getByText('Shipped Agent')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Shipped Agent' }));

    expect(screen.queryByRole('button', { name: 'Confirm delete Shipped Agent' })).toBeNull();
    expect(mocks.deleteSpecialtyAgentDraft).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('surfaces a delete failure on the tile instead of losing it', async () => {
    mocks.deleteSpecialtyAgentDraft.mockRejectedValue(new Error('draft is locked'));
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(screen.getByRole('button', { name: 'Delete Research Agent' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete Research Agent' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('draft is locked');
    // The tile survives a failed delete rather than vanishing optimistically.
    expect(within(centerAgents()).getByText('Research Agent')).toBeTruthy();
  });

  // --- Per-state row actions ---------------------------------------------
  //
  // Actions are pinned to the far right of each row and offered only where they
  // can actually work. Every "absent" assertion below is a button that would
  // fail if rendered — the same class of bug as the dead Delete this file
  // already removed from the core Task Agent.

  it('offers Edit, Publish and Delete on a working copy', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expect(screen.getByRole('button', { name: 'Edit Research Agent' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Publish Research Agent' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete Research Agent' })).toBeTruthy();
    // A working copy has nothing to "view" — View is the published read-only surface.
    expect(screen.queryByRole('button', { name: 'View Research Agent' })).toBeNull();
  });

  it('offers View, Edit and Delete on a published row, but not Publish', () => {
    // Publishing an already-published version is meaningless. Delete here is the
    // purge, which the dialog gates.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expect(screen.getByRole('button', { name: 'View Shipped Agent' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Edit Shipped Agent' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete Shipped Agent' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Publish Shipped Agent' })).toBeNull();
  });

  it('offers no actions at all on the core Task Agent', () => {
    // A virtual row appended by the service layer with no AssetIndex record, so
    // read, edit, publish and delete all fail against it.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [{
        assetId: 'core-task-agent', name: 'Task Agent', isCoreAgent: true, updatedAt: Date.now(),
      }],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    for (const verb of ['View', 'Edit', 'Publish', 'Delete']) {
      expect(screen.queryByRole('button', { name: `${verb} Task Agent` })).toBeNull();
    }
    expect(within(centerAgents()).getByText('Built-in')).toBeTruthy();
  });

  it('does not open the editor when the core Task Agent row is clicked', () => {
    // Clicking it used to mount an editor that immediately failed its read.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [{
        assetId: 'core-task-agent', name: 'Task Agent', isCoreAgent: true, updatedAt: Date.now(),
      }],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(within(centerAgents()).getByText('Task Agent'));
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
  });

  it('publishes a working copy from its row', async () => {
    const refreshCategory = vi.fn().mockResolvedValue(undefined);
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [sampleAgent], refreshCategory,
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(screen.getByRole('button', { name: 'Publish Research Agent' }));
    await waitFor(() =>
      expect(mocks.publishAsset).toHaveBeenCalledWith('specialty-agent', 'agent-1'),
    );
    await waitFor(() => expect(refreshCategory).toHaveBeenCalledWith('specialty-agent'));
    // The row opens nothing — publishing is not editing.
    expect(screen.queryByTestId('specialty-agent-editor')).toBeNull();
  });

  it('surfaces a publish failure on the row', async () => {
    mocks.publishAsset.mockRejectedValue(new Error('persona is empty'));
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(screen.getByRole('button', { name: 'Publish Research Agent' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('persona is empty');
    expect(within(centerAgents()).getByText('Research Agent')).toBeTruthy();
  });

  it('shows the published version on a row shadowed by a working copy', () => {
    // A live working copy hides its published versions in this list. Without
    // carrying the version through, a lineage that has shipped looks like it
    // never did.
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [{ ...sampleAgent, assetId: 'pub-agent-1', name: 'Shipped Agent' }],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    const center = centerAgents();
    expect(within(center).getByText(/v1 published/)).toBeTruthy();
    // It is still a working copy, so it keeps the working-copy action set.
    expect(screen.getByRole('button', { name: 'Publish Shipped Agent' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete Shipped Agent' })).toBeTruthy();
  });

  it('keeps the row mounted while a delete is in flight', async () => {
    // The re-flow guard: if the row unmounted before the refresh settled, the
    // list would shift under the pointer.
    let release: (() => void) | undefined;
    mocks.deleteSpecialtyAgentDraft.mockImplementation(
      () => new Promise<void>((resolve) => { release = () => resolve(); }),
    );
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    fireEvent.click(screen.getByRole('button', { name: 'Delete Research Agent' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete Research Agent' }));

    await waitFor(() => expect(screen.getByText('Deleting…')).toBeTruthy());
    expect(within(centerAgents()).getByText('Research Agent')).toBeTruthy();
    release?.();
  });

  // --- Purge (published lineages) -----------------------------------------
  //
  // The only irreversible action in the app. These tests are about the gate, not
  // the mechanics: the preview must render before anything can be confirmed, the
  // agent's name must be typed, and a failure must leave the dialog standing.

  const openPurgeDialog = async () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Shipped Agent' }));
    return screen.getByRole('dialog');
  };

  const typePhrase = (phrase: string) => {
    fireEvent.change(screen.getByLabelText(`Type ${phrase} to confirm`), {
      target: { value: phrase },
    });
  };

  it('previews the purge with dryRun before anything is destroyed', async () => {
    await openPurgeDialog();
    await waitFor(() =>
      expect(mocks.previewAgentPurge).toHaveBeenCalledWith('pub-agent-1'),
    );
    expect(mocks.purgeAgent).not.toHaveBeenCalled();
  });

  it('keeps Confirm disabled until the agent name is typed', async () => {
    await openPurgeDialog();
    const confirm = screen.getByRole('button', { name: 'Delete permanently' });
    // Even once the preview lands, the phrase still gates it.
    await waitFor(() => expect(screen.getByText(/1 published version/)).toBeTruthy());
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Type Shipped Agent to confirm'), {
      target: { value: 'Shipped' },   // a prefix is not the phrase
    });
    expect(confirm).toBeDisabled();

    typePhrase('Shipped Agent');
    expect(confirm).toBeEnabled();
  });

  it('cannot be confirmed while the preview is still loading', async () => {
    // Otherwise the user could type the name and destroy the lineage without the
    // blast radius ever having been shown.
    mocks.previewAgentPurge.mockImplementation(() => new Promise(() => {}));
    await openPurgeDialog();
    typePhrase('Shipped Agent');
    expect(screen.getByRole('button', { name: 'Delete permanently' })).toBeDisabled();
  });

  it('purges and drops the row, then refreshes', async () => {
    const refreshCategory = vi.fn().mockResolvedValue(undefined);
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [], publishedSpecialtyAgents: [samplePublishedAgent], refreshCategory,
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete Shipped Agent' }));
    await waitFor(() => expect(screen.getByText(/1 published version/)).toBeTruthy());

    typePhrase('Shipped Agent');
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));

    await waitFor(() => expect(mocks.purgeAgent).toHaveBeenCalledWith('pub-agent-1'));
    await waitFor(() => expect(refreshCategory).toHaveBeenCalledWith('specialty-agent'));
    // The context stub still returns the row — the Registry projection converges
    // asynchronously — so the session-local tombstone is what removes it.
    await waitFor(() =>
      expect(within(centerAgents()).queryByText('Shipped Agent')).toBeNull(),
    );
  });

  it('keeps the dialog open on failure so a partial purge can be retried', async () => {
    const { CapabilityApiError } = await import('../capability-api');
    mocks.purgeAgent.mockRejectedValue(
      new CapabilityApiError(409, 'cancelled after removing [2]', 'conflict'),
    );
    await openPurgeDialog();
    await waitFor(() => expect(screen.getByText(/1 published version/)).toBeTruthy());

    typePhrase('Shipped Agent');
    fireEvent.click(screen.getByRole('button', { name: 'Delete permanently' }));

    expect(await screen.findByText(/try again to finish/)).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();
    // The row is still there: the purge did not complete.
    expect(within(centerAgents()).getByText('Shipped Agent')).toBeTruthy();
  });

  it('says the reference list is incomplete when the scan was partial', async () => {
    // Rendering "nothing references this" off an unscanned org tier would be a
    // false reassurance before an irreversible delete.
    mocks.previewAgentPurge.mockResolvedValue({
      ...samplePreflight,
      partial: ['org-shared workflows (no org id on this request)'],
    });
    await openPurgeDialog();

    expect(await screen.findByText(/covers only what you can see/)).toBeTruthy();
    expect(screen.queryByText(/Nothing you can see references/)).toBeNull();
  });

  it('warns when external access is live', async () => {
    mocks.previewAgentPurge.mockResolvedValue({
      ...samplePreflight, externalAccessLive: true,
    });
    await openPurgeDialog();
    expect(await screen.findByText(/External access is live/)).toBeTruthy();
  });

  it('surfaces a failed preview and refuses to confirm', async () => {
    const { CapabilityApiError } = await import('../capability-api');
    mocks.previewAgentPurge.mockRejectedValue(
      new CapabilityApiError(403, 'nope', 'authority_denied'),
    );
    await openPurgeDialog();

    expect(await screen.findByText(/not allowed to delete/)).toBeTruthy();
    typePhrase('Shipped Agent');
    expect(screen.getByRole('button', { name: 'Delete permanently' })).toBeDisabled();
  });

  it('cancels without purging', async () => {
    await openPurgeDialog();
    await waitFor(() => expect(screen.getByText(/1 published version/)).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.purgeAgent).not.toHaveBeenCalled();
    expect(within(centerAgents()).getByText('Shipped Agent')).toBeTruthy();
  });

  // --- The persistent Agents surface header ------------------------------
  //
  // The heading, its description and the rule under them used to belong to
  // `CustomAgentCenterList` alone. Going one level in replaced the whole surface —
  // the published view swapped in its own header, the editor swapped in a bare
  // "Edit Agent" row — so the one thing naming the surface disappeared exactly when
  // the user needed to know they were still on it, and the pane's contents jumped
  // up by the header's height on the way in and back down on the way out. Skills
  // already had this; these three assert Agents matches, branch for branch.
  //
  // `AgentsSurfaceHeader` is NOT stubbed in this file, so these exercise the real
  // one. Each asserts document ORDER too: present-somewhere would also pass if the
  // header were rendered under the content.

  /** The heading, the description, and the rule — the rule is what was missing. */
  const expectAgentsHeaderAbove = (content: HTMLElement) => {
    const heading = screen.getByRole('heading', { name: 'Agents', level: 2 });
    expect(screen.getByText('Create and configure custom agents, then test and publish them.')).toBeTruthy();
    expect(heading.closest('div')!.parentElement!.style.borderBottom)
      .toBe('1px solid var(--color-border)');
    expect(heading.compareDocumentPosition(content) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  };

  it('heads the agent INVENTORY, and only once', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));

    expectAgentsHeaderAbove(within(centerAgents()).getByText('Research Agent'));
    // The list used to render its own copy. Two would stack a duplicate heading
    // above the search row, and imply the header belongs to the list.
    expect(screen.getAllByRole('heading', { name: 'Agents', level: 2 })).toHaveLength(1);
  });

  it('stays above a PUBLISHED agent’s read-only view', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({
      specialtyAgents: [],
      publishedSpecialtyAgents: [samplePublishedAgent],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(within(centerAgents()).getByText('Shipped Agent'));

    // The agent's own header is a SECOND tier under the surface one, not a
    // replacement for it: both headings are on screen, in that order.
    const detailHeading = screen.getByRole('heading', { name: 'Shipped Agent', level: 2 });
    expectAgentsHeaderAbove(detailHeading);
    // ...and the nested header draws no rule of its own — two hairlines a heading
    // apart read as a broken table.
    expect(detailHeading.parentElement!.parentElement!.style.borderBottom).toBe('');
  });

  it('stays above the agent EDITOR', () => {
    mocks.useLabContext.mockReturnValue(labContextValue({ specialtyAgents: [sampleAgent] }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(within(centerAgents()).getByText('Research Agent'));

    expectAgentsHeaderAbove(screen.getByTestId('specialty-agent-editor'));
  });

  // --- Skills centre (promoted out of Settings) --------------------------

  it('opens the Skills centre from the nav and pulls its own inventory', async () => {
    const ensureCategoryLoaded = vi.fn().mockResolvedValue(undefined);
    mocks.useLabContext.mockReturnValue(labContextValue({ ensureCategoryLoaded }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));

    expect(screen.getByTestId('skills-center')).toBeTruthy();
    // Loading is lazy, so the centre must request its category or it renders an
    // empty state over assets the Owner actually has.
    await waitFor(() => expect(ensureCategoryLoaded).toHaveBeenCalledWith('skill'));
  });

  it('lists a published skill that has no working copy, and opens it read-only', () => {
    /*
     * Publishing CONSUMES the working copy, so a sidebar built from drafts alone
     * emptied itself the moment the Owner published everything: two published skills
     * read as "Skills 0" over "No skills yet. Create one to get started."
     */
    const setEditingTarget = vi.fn();
    mocks.useLabContext.mockReturnValue(labContextValue({
      setEditingTarget,
      skills: [],
      publishedSkills: [
        { assetId: 'lineage-1', name: 'shipped-skill', version: 2, current: true, scope: 'private', createdAt: 1, updatedAt: 10 },
        // An older version of the SAME lineage must not become a second row.
        { assetId: 'lineage-1', name: 'shipped-skill', version: 1, current: false, scope: 'private', createdAt: 1, updatedAt: 5 },
      ],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /^Skills/ }));

    expect(screen.getAllByText('shipped-skill')).toHaveLength(1);
    expect(screen.getByText(/Published v2/)).toBeTruthy();
    expect(screen.queryByText(/No skills yet/)).toBeNull();

    fireEvent.click(screen.getByText('shipped-skill').closest('[data-skill-row]')!);

    // NOT the editor: it reads the draft-only OWNED partition, which publishing
    // emptied, so it would render a blank form for a skill that plainly exists.
    expect(setEditingTarget).not.toHaveBeenCalledWith(
      expect.objectContaining({ assetId: 'lineage-1' }),
    );
    const centre = screen.getByTestId('skills-center');
    expect(centre).toHaveAttribute('data-opendetail-seen', 'published:lineage-1:2');
    // Handed over and dropped. Anything still held here would be re-applied on the
    // centre's next mount — see the navigation case below.
    expect(centre).toHaveAttribute('data-opendetail', '');
  });

  it('returns to the Skills list, not a published skill, when the nav is clicked again', () => {
    /*
     * Reported: opening the Skills nav went straight into a published skill. The
     * sidebar's detail request was held in host state, and `SkillsCenter` applies
     * that prop on mount — so every remount reopened it. Leaving Skills and coming
     * back is the shortest route to a remount; closing a skill editor is another.
     */
    mocks.useLabContext.mockReturnValue(labContextValue({
      skills: [],
      publishedSkills: [
        { assetId: 'lineage-1', name: 'shipped-skill', version: 2, current: true, scope: 'private', createdAt: 1, updatedAt: 10 },
      ],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: /^Skills/ }));
    fireEvent.click(screen.getByText('shipped-skill').closest('[data-skill-row]')!);
    expect(screen.getByTestId('skills-center'))
      .toHaveAttribute('data-opendetail-seen', 'published:lineage-1:2');

    // Away and back — a fresh mount of the centre. Agents rather than Chat only
    // because computing the Chat button's accessible name trips a jsdom bug in
    // `background` shorthand cloning; any other centre exercises the same remount.
    fireEvent.click(screen.getByRole('button', { name: 'Agents' }));
    fireEvent.click(screen.getByRole('button', { name: /^Skills/ }));

    // Nothing to apply, so the centre opens on its list.
    const centre = screen.getByTestId('skills-center');
    expect(centre).toHaveAttribute('data-opendetail', '');
    expect(centre).toHaveAttribute('data-opendetail-seen', '');
  });

  it('still routes a skill that HAS a working copy to the editor', () => {
    // The guard above must not swallow the ordinary case.
    const setEditingTarget = vi.fn();
    mocks.useLabContext.mockReturnValue(labContextValue({
      setEditingTarget,
      skills: [{ assetId: 'draft-1', name: 'wip-skill', updatedAt: 9 }],
      publishedSkills: [],
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /^Skills/ }));

    expect(screen.getByText(/Draft/)).toBeTruthy();
    fireEvent.click(screen.getByText('wip-skill').closest('[data-skill-row]')!);
    expect(setEditingTarget).toHaveBeenCalledWith({ category: 'skill', assetId: 'draft-1' });
  });

  it('keeps the user in Skills when the skill editor opens', () => {
    // Regression: `CenterEditor` renders only in chat mode and an effect forced
    // centerMode='chat' for ANY editingTarget. That silently threw the user out
    // of Skills on every Edit, and Back returned them to Chat rather than the
    // list they came from. Skill targets are exempt.
    const setEditingTarget = vi.fn();
    mocks.useLabContext.mockReturnValue(labContextValue({ setEditingTarget }));
    const { rerender } = render(<WorkspaceView onOpenSettings={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));
    fireEvent.click(screen.getByRole('button', { name: 'stub-edit-skill' }));
    expect(setEditingTarget).toHaveBeenCalledWith({ category: 'skill', assetId: 'skill-1' });

    // Re-render as the provider would once the target is set.
    mocks.useLabContext.mockReturnValue(labContextValue({
      setEditingTarget,
      editingTarget: { category: 'skill', assetId: 'skill-1' },
    }));
    rerender(<WorkspaceView onOpenSettings={vi.fn()} />);

    // Exactly ONE editor. The chat pane stays mounted (hidden with
    // display:none) whatever the centre is, so without an explicit guard a skill
    // target mounts CenterEditor twice — once there, once in the Skills branch —
    // duplicating its mount and its config fetch.
    expect(screen.getAllByTestId('center-editor')).toHaveLength(1);
    // The editor replaces the Skills list rather than stacking on it.
    expect(screen.queryByTestId('skills-center')).toBeNull();
  });

  it('keeps the Skills header above the editor', () => {
    // The editor REPLACES `SkillsCenter`, so it is the one Skills branch that does
    // not inherit the header — and it was the one place the heading and its rule
    // still vanished, dropping the form to the very top of the pane. `SkillsCenter`
    // is stubbed here but the header module is not, so this asserts the real one.
    mocks.useLabContext.mockReturnValue(labContextValue());
    const { rerender } = render(<WorkspaceView onOpenSettings={vi.fn()} />);
    // The nav click is what puts centerMode on 'skills'; a bare editingTarget
    // renders the editor in the chat pane instead, where no Skills header belongs.
    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));
    mocks.useLabContext.mockReturnValue(labContextValue({
      editingTarget: { category: 'skill', assetId: 'skill-1' },
    }));
    rerender(<WorkspaceView onOpenSettings={vi.fn()} />);

    const heading = screen.getByRole('heading', { name: 'Skills', level: 2 });
    expect(heading).toBeTruthy();
    expect(screen.getByText(/Capabilities an agent can load/)).toBeTruthy();
    // The rule under the header, not just the text — its absence is what made the
    // content look like it had run off the top.
    expect(heading.closest('div')!.parentElement!.style.borderBottom)
      .toBe('1px solid var(--color-border)');

    // Above the editor in document order, not merely present somewhere.
    const editor = screen.getByTestId('center-editor');
    expect(heading.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it('still forces chat mode for a non-skill editor target', () => {
    // The original behaviour must survive for tool/workflow targets, which have
    // no centre of their own to render into.
    mocks.useLabContext.mockReturnValue(labContextValue({
      editingTarget: { category: 'tool-collection', assetId: 'tc-1' },
    }));
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    expect(screen.getByTestId('center-editor')).toBeTruthy();
  });

  it('morphs the shared New action into New skill', () => {
    mocks.useLabContext.mockReturnValue(labContextValue());
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));
    expect(screen.getByRole('button', { name: 'New skill' })).toBeTruthy();
  });
});
