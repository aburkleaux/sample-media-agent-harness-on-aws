/**
 * @vitest-environment jsdom
 *
 * WorkspaceView in `projects` mode, with the real sidebar and the real Projects_Surface: the sidebar shows
 * the project inventory only in this mode, a row selects the Project the surface shows and the selection
 * survives a mode switch (sessionStorage `lab-projects-selected`), and the sidebar's `+ New project` — the
 * only create entry — opens the surface's inline form and selects the created Project.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { act, render, screen, fireEvent, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  updateSessionRenderHistory: vi.fn(),
  updateSessionActivity: vi.fn(),
  projectsEnabled: vi.fn(),
  listProjects: vi.fn(),
  createProject: vi.fn(),
  patchSessionProject: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../lab-api', () => ({
  listSessions: mocks.listSessions,
  createSession: mocks.createSession,
  deleteSession: vi.fn(),
  updateSessionRenderHistory: mocks.updateSessionRenderHistory,
  updateSessionActivity: mocks.updateSessionActivity,
  editAsset: vi.fn(),
  discardChanges: vi.fn(),
  deleteSpecialtyAgentDraft: vi.fn(),
  deleteSkillDraft: vi.fn(),
  publishAsset: vi.fn(),
  resolveAgentConfig: vi.fn(() => new Promise(() => {})),
}));
vi.mock('../capability-api', () => ({
  previewPurge: vi.fn(),
  purgeAsset: vi.fn(),
  previewAgentPurge: vi.fn(),
  purgeAgent: vi.fn(),
  CapabilityApiError: class CapabilityApiError extends Error {},
}));
vi.mock('../hooks/useJobs', () => ({
  jobsStore: { subscribeNewJob: vi.fn(() => () => {}) },
  useJobs: () => ({ jobs: [] }),
}));
vi.mock('../auth/useCurrentUser', () => ({
  useCurrentUser: () => ({ mode: 'dev', displayName: 'Local Dev', initials: 'LD', sub: 'u', signOut: vi.fn() }),
}));
vi.mock('../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
vi.mock('../config/branding', () => ({
  BRAND_NAME: 'TestBrand',
  getActiveLogo: () => ({ src: 'logo.png', alt: 'logo', borderRadius: 0 }),
}));
vi.mock('../components/ChatCanvas', () => ({ default: () => <div data-testid="chat-canvas">chat</div> }));
vi.mock('../components/CenterEditor', () => ({ default: () => <div data-testid="center-editor">center-editor</div> }));
vi.mock('../components/SessionRendererPanel', () => ({ default: () => <div data-testid="renderer-panel" /> }));
vi.mock('../components/BuiltInAgentsBrowser', () => ({ default: () => <div data-testid="built-in-agents" /> }));
vi.mock('../components/SkillsCenter', () => ({ default: () => <div data-testid="skills-center" /> }));
vi.mock('../components/JobsPanel', () => ({ default: () => <div data-testid="jobs-panel" />, rollUpRuns: () => [] }));
vi.mock('../components/SpecialtyAgentEditor', () => ({ default: () => <div data-testid="specialty-agent-editor" /> }));
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  projectsEnabled: mocks.projectsEnabled,
  listProjects: mocks.listProjects,
  createProject: mocks.createProject,
  patchSessionProject: mocks.patchSessionProject,
}));

import WorkspaceView from './WorkspaceView';
import { __resetProjectsListForTests } from '../projects/useProjectsList';
import { __resetSessionProjectStoreForTests, getSessionProject } from '../projects/sessionProjectStore';

const PERSONAL = { projectId: 'pP', name: 'Personal', role: 'owner', personal: true };
const LAUNCH = { projectId: 'pL', name: 'Launch Q3', role: 'owner', personal: false };

function labContextValue() {
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
  };
}

// A full WorkspaceView render plus a handful of role queries is ~0.3-0.5 s alone, but measured 5.7 s
// with three vitest shards running on one machine. The default 5 s budget is a CPU-contention
// timeout, not a hang detector, for a test this size.
const HEAVY_RENDER_TIMEOUT_MS = 20_000;

async function flush() {
  await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); });
}

const surfaceHeading = () => within(screen.getByTestId('project-header')).getByRole('heading');
// Scoped to the inventory: a role query computes the accessible name of every candidate in its
// subtree, which costs tens of ms per query on a full WorkspaceView. Scoping is also the stricter
// assertion, since the row must be in the inventory.
const projectRow = (label: string) =>
  within(screen.getByTestId('project-inventory')).getByRole('button', { name: label });

describe('WorkspaceView — Projects center mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    __resetProjectsListForTests();
    __resetSessionProjectStoreForTests();
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockResolvedValue(undefined);
    mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
    mocks.updateSessionActivity.mockResolvedValue(undefined);
    mocks.projectsEnabled.mockResolvedValue(true);
    mocks.listProjects.mockResolvedValue({ ok: true, value: [LAUNCH, PERSONAL] });
    mocks.patchSessionProject.mockResolvedValue({ ok: true, value: undefined });
    mocks.useLabContext.mockReturnValue(labContextValue());
  });

  it('shows the inventory only in projects mode, and a selection persists across mode switches', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    const nav = await screen.findByRole('button', { name: 'Projects' });
    expect(screen.queryByTestId('project-inventory')).toBeNull();

    fireEvent.click(nav);
    await flush();
    expect(screen.getByTestId('project-inventory')).toBeInTheDocument();
    expect(screen.getByTestId('projects-surface')).toBeInTheDocument();
    // Nothing persisted yet: Personal is selected, highlighted, and recorded.
    expect(surfaceHeading()).toHaveTextContent('Personal');
    expect(projectRow('Personal (private), owner')).toHaveAttribute('data-active', 'true');
    expect(sessionStorage.getItem('lab-projects-selected')).toBe('pP');

    // Selecting a row is a synchronous state update (fireEvent runs inside act), so no flush.
    fireEvent.click(projectRow('Launch Q3, owner'));
    expect(surfaceHeading()).toHaveTextContent('Launch Q3');
    expect(projectRow('Launch Q3, owner')).toHaveAttribute('data-active', 'true');
    expect(sessionStorage.getItem('lab-projects-selected')).toBe('pL');
    // Selecting a Project does not change any chat's Active_Project.
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();

    // Leave and come back: the inventory goes with the mode, the selection stays.
    fireEvent.click(screen.getByRole('button', { name: /^Chat(?! in)/ }));
    expect(screen.queryByTestId('project-inventory')).toBeNull();
    // The rows come from the module-level projects store, so re-entering the mode renders them
    // synchronously; no flush needed.
    fireEvent.click(screen.getByRole('button', { name: 'Projects' }));
    expect(surfaceHeading()).toHaveTextContent('Launch Q3');
    expect(projectRow('Launch Q3, owner')).toHaveAttribute('data-active', 'true');
  }, HEAVY_RENDER_TIMEOUT_MS);

  it('the one + New project entry opens the inline form and selects the created project', async () => {
    mocks.createProject.mockResolvedValue({ ok: true, value: { projectId: 'pN', name: 'Trailer cuts', personal: false } });
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Projects' }));
    await flush();

    const entries = screen.getAllByRole('button', { name: /new project/i });
    expect(entries).toHaveLength(1);
    expect(screen.queryByTestId('project-create-form')).toBeNull();
    fireEvent.click(entries[0]);
    const form = screen.getByTestId('project-create-form');

    mocks.listProjects.mockResolvedValue({
      ok: true,
      value: [LAUNCH, PERSONAL, { projectId: 'pN', name: 'Trailer cuts', role: 'owner', personal: false }],
    });
    fireEvent.change(within(form).getByLabelText('New project name'), { target: { value: 'Trailer cuts' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Create project' }));
    await flush();

    expect(mocks.createProject).toHaveBeenCalledWith('Trailer cuts', { indexing: false });
    expect(screen.queryByTestId('project-create-form')).toBeNull();
    expect(surfaceHeading()).toHaveTextContent('Trailer cuts');
    expect(projectRow('Trailer cuts, owner')).toHaveAttribute('data-active', 'true');
    expect(sessionStorage.getItem('lab-projects-selected')).toBe('pN');
  }, HEAVY_RENDER_TIMEOUT_MS);

  it('New chat opens a NEW chat session working in that project and leaves the open chat alone', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Projects' }));
    await flush();
    fireEvent.click(projectRow('Launch Q3, owner'));
    const uuid = vi.spyOn(crypto, 'randomUUID').mockReturnValue('00000000-0000-4000-8000-00000000c4a7');
    fireEvent.click(screen.getByRole('button', { name: 'Chat in Launch Q3' }));
    await flush();
    uuid.mockRestore();

    expect(screen.queryByTestId('projects-surface')).toBeNull();  // back in chat mode
    expect(getSessionProject('00000000-0000-4000-8000-00000000c4a7')?.activeProjectId).toBe('pL');
    expect(getSessionProject('00000000-0000-4000-8000-00000000c4a7')?.activeProjectName).toBe('Launch Q3');
    // The new session has no row yet: nothing is PATCHed; its first turn carries the project.
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();
  }, HEAVY_RENDER_TIMEOUT_MS);
});
