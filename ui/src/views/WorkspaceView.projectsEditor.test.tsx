/**
 * @vitest-environment jsdom
 *
 * Single-CenterEditor contract for the Projects_Surface center mode (media-harness-projects-access task
 * 13.9; design Component 11 "The two traps", Req 10.11, 10.12, 10.13).
 *
 * `PROJECTS_HOSTED_EDITOR_CATEGORIES` is empty in v1, so the Projects_Surface hosts no CenterEditor. This
 * test pins that, and is written to generalise: for every category the surface DOES host (when the list
 * grows), opening its editor in `projects` mode must leave exactly ONE `center-editor` mounted — the
 * hidden chat-pane instance is suppressed by the guard — and the mode must stay `projects`, asserted by
 * `projects-surface` (never by the absence of `chat-canvas`, which stays mounted via display:none).
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  listSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionRenderHistory: vi.fn(),
  updateSessionActivity: vi.fn(),
  editAsset: vi.fn(),
  discardChanges: vi.fn(),
  projectsEnabled: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../lab-api', () => ({
  listSessions: mocks.listSessions,
  createSession: mocks.createSession,
  deleteSession: mocks.deleteSession,
  updateSessionRenderHistory: mocks.updateSessionRenderHistory,
  updateSessionActivity: mocks.updateSessionActivity,
  editAsset: mocks.editAsset,
  discardChanges: mocks.discardChanges,
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
// Projects_Surface stub: renders the surface marker so the mode assertion holds without the real
// surface's `/projects` traffic.
vi.mock('../components/projects/ProjectsSurface', () => ({
  default: () => <div data-testid="projects-surface">projects</div>,
}));
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  projectsEnabled: mocks.projectsEnabled,
}));

import WorkspaceView, { PROJECTS_HOSTED_EDITOR_CATEGORIES } from './WorkspaceView';

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

describe('WorkspaceView — Projects_Surface single CenterEditor contract', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    try { sessionStorage.clear(); } catch { /* ignore */ }
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockResolvedValue(undefined);
    mocks.updateSessionRenderHistory.mockResolvedValue(undefined);
    mocks.updateSessionActivity.mockResolvedValue(undefined);
    mocks.projectsEnabled.mockResolvedValue(true);
    mocks.useLabContext.mockReturnValue(labContextValue());
  });

  it('hosts no editor categories in v1', () => {
    expect(PROJECTS_HOSTED_EDITOR_CATEGORIES).toHaveLength(0);
  });

  it('shows the Projects surface in projects mode, with one CenterEditor per hosted category', async () => {
    render(<WorkspaceView onOpenSettings={vi.fn()} />);

    // The nav entry appears once `/config.projectsEnabled` resolves true.
    const projectsNav = await screen.findByRole('button', { name: 'Projects' });
    fireEvent.click(projectsNav);

    // The mode is projects — asserted by the surface's own test id, never by chat-canvas absence.
    expect(screen.getByTestId('projects-surface')).toBeTruthy();
    // v1 hosts no editor, so none is mounted.
    expect(screen.queryAllByTestId('center-editor')).toHaveLength(0);

    // Generalises: when a hosted category exists, opening it must leave exactly one CenterEditor and
    // keep the mode on projects. Empty today, so this loop is a guard for the future.
    for (const category of PROJECTS_HOSTED_EDITOR_CATEGORIES) {
      mocks.useLabContext.mockReturnValue(labContextValue({ editingTarget: { category, assetId: 'x' } }));
      render(<WorkspaceView onOpenSettings={vi.fn()} />);
      fireEvent.click(await screen.findByRole('button', { name: 'Projects' }));
      expect(screen.getAllByTestId('center-editor')).toHaveLength(1);
      expect(screen.getByTestId('projects-surface')).toBeTruthy();
    }
  });
});
