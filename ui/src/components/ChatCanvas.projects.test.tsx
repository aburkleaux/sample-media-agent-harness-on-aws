/**
 * @vitest-environment jsdom
 *
 * ChatCanvas ↔ Plus_Menu integration (media-harness-projects-access task 13.3): the `+` menu replaces the
 * standalone `Upload file` button with `Attach files`, drop/paste share the one attachment path (multi-file attach,
 * the project switch, and attaching a recent project file are covered in `ChatCanvas.attachments.test.tsx`), the
 * menu's `Project` section is the one place the chat changes its Active_Project, the status line is plain text
 * that describes the textarea, each turn carries the submit-time Project values, and a `409 project_access_lost`
 * keeps the text. With Projects disabled the menu offers only `Attach files`, there is no status line, and the body
 * is unchanged.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  useLabContext: vi.fn(),
  invokeAgentStreaming: vi.fn(),
  selectedOrchestratorModelId: vi.fn(() => 'model-x'),
  createSession: vi.fn(),
  updateSessionActivity: vi.fn(),
  getConfig: vi.fn(),
  getSessionTranscript: vi.fn(() => Promise.resolve([] as unknown[])),
  upload: vi.fn(),
  projectsEnabled: vi.fn(),
  listProjects: vi.fn(),
  patchSessionProject: vi.fn(),
  listAssets: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: mocks.useLabContext }));
vi.mock('../api', () => ({
  invokeAgentStreaming: mocks.invokeAgentStreaming,
  selectedOrchestratorModelId: mocks.selectedOrchestratorModelId,
  selectedReasoningEffort: () => undefined,
}));
vi.mock('../lab-api', () => ({
  createSession: mocks.createSession,
  updateSessionActivity: mocks.updateSessionActivity,
  getConfig: mocks.getConfig,
  getSessionTranscript: mocks.getSessionTranscript,
}));
vi.mock('../services/agent-service', () => ({ AgentService: { upload: mocks.upload } }));
vi.mock('./chat/JobReference', () => ({ default: () => null }));
vi.mock('../projects/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../projects/api')>()),
  projectsEnabled: mocks.projectsEnabled,
  listProjects: mocks.listProjects,
  patchSessionProject: mocks.patchSessionProject,
  listAssets: mocks.listAssets,
}));

import ChatCanvas, { SESSION_PROJECT_FIXED_NOTICE } from './ChatCanvas';
import { PLUS_MENU_LABEL } from './chat/PlusMenu';
import { __resetSessionProjectStoreForTests, seedSessionProject } from '../projects/sessionProjectStore';
import { __resetChatAttachmentsForTests } from '../projects/upload/chatAttachments';
import { __resetUploadQueueForTests } from '../projects/upload/uploadQueue';
import { EMOJI_OR_GLYPH } from '../test-utils/glyphs';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

const PERSONAL = { projectId: 'pPersonal', name: 'Personal', role: 'owner', personal: true };
const TRAILER = { projectId: 'pTrailer', name: 'Trailer cuts', role: 'member', personal: false };

async function flush() {
  await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
}
function file(name: string, size = 10): File {
  return new File([new Uint8Array(size)], name, { type: 'image/png' });
}
function sentBody(): Record<string, unknown> {
  return mocks.invokeAgentStreaming.mock.calls[0][0] as Record<string, unknown>;
}
async function send(text: string) {
  const textarea = screen.getByLabelText('Chat message input');
  fireEvent.change(textarea, { target: { value: text } });
  fireEvent.keyDown(textarea, { key: 'Enter' });
  await flush();
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  __resetSessionProjectStoreForTests();
  __resetChatAttachmentsForTests();
  __resetUploadQueueForTests();
  mocks.upload.mockImplementation(async (f: File) => ({
    key: `artifacts/S1/input/1-${f.name}`, bucket: 'b', url: '', cloudfrontUrl: `https://cdn.example/artifacts/S1/input/1-${f.name}`,
    size: f.size, filename: f.name,
  }));
  mocks.useLabContext.mockReturnValue({ sessionId: 'S1', refreshCategory: vi.fn() });
  mocks.createSession.mockResolvedValue(true);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  mocks.invokeAgentStreaming.mockResolvedValue(undefined);
  mocks.listProjects.mockResolvedValue({ ok: true, value: [PERSONAL, TRAILER] });
  mocks.patchSessionProject.mockResolvedValue({ ok: true, value: undefined });
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [] } });
});

/** Open the + menu and wait for its open-time reads. */
async function openPlusMenu() {
  fireEvent.click(screen.getByRole('button', { name: PLUS_MENU_LABEL }));
  await flush();
  return screen.getByRole('menu', { name: PLUS_MENU_LABEL });
}

describe('ChatCanvas composer with Projects disabled', () => {
  beforeEach(() => mocks.projectsEnabled.mockResolvedValue(false));

  it('has no Upload file button or status line, and the + menu still attaches through the same picker', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    expect(screen.queryByRole('button', { name: 'Upload file' })).toBeNull();
    expect(screen.queryByTestId('composer-project-status')).toBeNull();

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const click = vi.spyOn(input, 'click');
    fireEvent.click(screen.getByRole('button', { name: PLUS_MENU_LABEL }));
    expect(screen.queryByText('Manage projects')).toBeNull();
    expect(screen.queryByRole('menuitemradio')).toBeNull();
    expect(screen.queryByText(/^Recent in/)).toBeNull();
    fireEvent.click(screen.getByRole('menuitem', { name: /Attach files/ }));
    expect(click).toHaveBeenCalledTimes(1);
    expect(mocks.listProjects).not.toHaveBeenCalled();
    expect(mocks.listAssets).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { files: [file('a.png')] } });
    await flush();
    // Projects off: this chat only, through the existing `POST /upload` path, in this session.
    expect(mocks.upload).toHaveBeenCalledWith(expect.any(File), 'S1', expect.any(Function), expect.any(AbortSignal));
    const chip = screen.getByTestId('attached-file-chip');
    expect(chip).toHaveTextContent('a.png');
    expect(screen.getByRole('button', { name: 'Remove attached file a.png' })).toBeInTheDocument();
  });

  it('draws the attached-file chip and its dismiss buttons with SVG icons, not emoji', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file('a.png')] } });
    await flush();
    const chip = screen.getByTestId('attached-file-chip');
    expect(chip.textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
    // Paperclip + close glyph, both decorative.
    expect(chip.querySelectorAll('svg[aria-hidden="true"]')).toHaveLength(2);
    const remove = screen.getByRole('button', { name: 'Remove attached file a.png' });
    expect(remove.textContent).toBe('');
    fireEvent.click(remove);
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
  });

  it('sends no project fields with a turn', async () => {
    render(<ChatCanvas />);
    await flush();
    await send('hello');
    expect(sentBody()).not.toHaveProperty('active_project_id');
    expect(sentBody()).not.toHaveProperty('web_search');
  });
});

describe('ChatCanvas composer with Projects enabled', () => {
  beforeEach(async () => {
    mocks.projectsEnabled.mockResolvedValue(true);
    await seedSessionProject('S1', { activeProjectId: TRAILER.projectId, activeProjectName: TRAILER.name, webSearch: true });
  });

  it('shows the status line as the textarea description and sends the snapshot with the turn', async () => {
    render(<ChatCanvas />);
    await flush();
    const status = screen.getByTestId('composer-project-status');
    expect(status).toHaveTextContent('Project: Trailer cuts · Web search on');
    expect(screen.getByLabelText('Chat message input')).toHaveAttribute('aria-describedby', status.id);
    expect(status.textContent ?? '').not.toMatch(EMOJI_OR_GLYPH);
    // Plain text: the project is changed from the + menu, so the line holds no control.
    expect(status.textContent).toBe('Project: Trailer cuts · Web search on');
    expect(status.querySelector('button, [role="button"], [tabindex]')).toBeNull();

    await send('hello');
    expect(sentBody()).toMatchObject({ active_project_id: TRAILER.projectId, web_search: true });
  });

  it('on 409 project_access_lost keeps the text, switches to Personal, and shows the notice', async () => {
    mocks.invokeAgentStreaming.mockRejectedValueOnce(Object.assign(new Error('Agent request failed: 409 Conflict'), {
      status: 409,
      body: { error: { code: 'project_access_lost', personalProject: { id: PERSONAL.projectId, name: 'Personal' } } },
    }));
    render(<ChatCanvas />);
    await flush();
    await send('find the trailer');

    expect(screen.getByLabelText('Chat message input')).toHaveValue('find the trailer');
    expect(screen.getByRole('alert')).toHaveTextContent(/no longer have access to "Trailer cuts"/);
    expect(screen.getByTestId('composer-project-status')).toHaveTextContent('Project: Personal');
    // The gate already moved the row; the client does not write it again.
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();
  });

  it('on 409 session_project_fixed keeps the text and explains instead of a generic failure (asset-lineage R2.1)', async () => {
    mocks.invokeAgentStreaming.mockRejectedValueOnce(Object.assign(new Error('Agent request failed: 409 Conflict'), {
      status: 409,
      body: { error: { code: 'session_project_fixed', message: 'fixed', projectId: PERSONAL.projectId } },
    }));
    render(<ChatCanvas />);
    await flush();
    await send('trim the clip');

    expect(screen.getByLabelText('Chat message input')).toHaveValue('trim the clip');
    expect(screen.getByText(SESSION_PROJECT_FIXED_NOTICE)).toBeInTheDocument();
    expect(screen.queryByText(/Agent request failed/)).toBeNull();
    expect(mocks.patchSessionProject).not.toHaveBeenCalled();
  });

  it('a text paste is untouched', async () => {
    render(<ChatCanvas />);
    await flush();
    const textarea = screen.getByLabelText('Chat message input');
    const paste = new Event('paste', { bubbles: true, cancelable: true });
    Object.assign(paste, { clipboardData: { files: [] } });
    textarea.dispatchEvent(paste);
    expect(paste.defaultPrevented).toBe(false);
  });

  it('attaches nothing dropped while the session is locked', async () => {
    render(<ChatCanvas locked />);
    await flush();
    fireEvent.drop(screen.getByLabelText('Chat message input'), { dataTransfer: { files: [file('x.png')] } });
    await flush();
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it('the + menu Project section lists projects and a pick goes through selectActiveProject (PATCH, then the label)', async () => {
    render(<ChatCanvas />);
    await flush();
    const menu = await openPlusMenu();
    expect(within(menu).getAllByRole('menuitemradio').map((el) => el.getAttribute('aria-label'))).toEqual([
      'Personal, private, owner',
      'Trailer cuts, member',
    ]);
    expect(within(menu).getByRole('menuitemradio', { name: 'Trailer cuts, member' })).toHaveAttribute('aria-checked', 'true');

    fireEvent.click(within(menu).getByRole('menuitemradio', { name: /^Personal/ }));
    await flush();
    expect(mocks.patchSessionProject).toHaveBeenCalledWith('S1', { activeProjectId: PERSONAL.projectId });
    expect(screen.getByTestId('composer-project-status')).toHaveTextContent('Project: Personal · Web search on');
  });

  it('keeps the label when the access check refuses the pick', async () => {
    mocks.patchSessionProject.mockResolvedValueOnce({ ok: false, kind: 'not_found' });
    render(<ChatCanvas />);
    await flush();
    await openPlusMenu();
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^Personal/ }));
    await flush();
    expect(screen.getByTestId('composer-project-status')).toHaveTextContent('Project: Trailer cuts');
  });

  it('switches to Personal with the notice when the open-time read no longer has the Active_Project', async () => {
    mocks.listProjects.mockResolvedValue({ ok: true, value: [PERSONAL] });
    render(<ChatCanvas />);
    await flush();
    await openPlusMenu();
    expect(screen.getByRole('alert')).toHaveTextContent(/no longer have access to "Trailer cuts"/);
    expect(screen.getByTestId('composer-project-status')).toHaveTextContent('Project: Personal');
    expect(mocks.patchSessionProject).toHaveBeenCalledWith('S1', { activeProjectId: PERSONAL.projectId });
  });

  it('Manage projects in the + menu opens the Projects surface on the Active_Project', async () => {
    const onManageProjects = vi.fn();
    render(<ChatCanvas onManageProjects={onManageProjects} />);
    await flush();
    await openPlusMenu();
    fireEvent.click(screen.getByRole('menuitem', { name: 'Manage projects' }));
    expect(onManageProjects).toHaveBeenCalledWith(TRAILER.projectId);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('the + menu holds Attach files, Recent in <Active_Project>, then the Project section, in that order', async () => {
    mocks.listAssets.mockResolvedValue({
      ok: true,
      value: { assets: [{ assetId: 'image:9', displayName: 'poster.png', kind: 'image', status: 'ready', sourceSize: 2048 }] },
    });
    const onManageProjects = vi.fn();
    render(<ChatCanvas onManageProjects={onManageProjects} />);
    await flush();
    const menu = await openPlusMenu();
    expect(mocks.listAssets).toHaveBeenCalledWith(TRAILER.projectId);
    expect(Array.from(menu.querySelectorAll('[role^="menuitem"]')).map((el) => el.getAttribute('aria-label') ?? el.textContent)).toEqual([
      'Attach files',
      'poster.png, 2 KB',
      'View all',
      'Personal, private, owner',
      'Trailer cuts, member',
      'Manage projects',
    ]);
    expect(within(menu).getByRole('group', { name: 'Recent in Trailer cuts' })).toBeInTheDocument();
    expect(within(menu).getByRole('group', { name: 'Project' })).toBeInTheDocument();
    expect(menu).not.toHaveTextContent(/Add files|create|rename|invite|delete/i);
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'View all' }));
    expect(onManageProjects).toHaveBeenCalledWith(TRAILER.projectId);
  });
});
