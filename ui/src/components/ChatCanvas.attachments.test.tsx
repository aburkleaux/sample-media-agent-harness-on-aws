/**
 * @vitest-environment jsdom
 *
 * Chat multi-attach: `Attach files` / drop / paste take 1 to 20 files that upload as soon as they are chosen.
 * With the upload sheet's `Add to <Active_Project>` switch ON (the default) each file goes into the project through
 * the upload queue and the chat then REFERENCES that asset (`chat-copy`, which copies no bytes since
 * asset-lineage Phase 2); OFF ("this chat only") the same upload registers a hidden session asset (D2).
 * Each finished file is a removable chip; Send waits while anything is uploading; the turn's
 * prompt carries one `[File attached: …]` block per file (which `promptNormalize` strips) and the body carries
 * `attachment` (the first) and `attachments` (all).
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
  getIngestionCapabilities: vi.fn(),
  createUploads: vi.fn(),
  completeUpload: vi.fn(),
  abortUpload: vi.fn(),
  registerAsset: vi.fn(),
  chatCopyAsset: vi.fn(),
  listAssets: vi.fn(),
  searchAssets: vi.fn(),
  findDuplicatesBatch: vi.fn(),
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
  getIngestionCapabilities: mocks.getIngestionCapabilities,
  createUploads: mocks.createUploads,
  completeUpload: mocks.completeUpload,
  abortUpload: mocks.abortUpload,
  registerAsset: mocks.registerAsset,
  chatCopyAsset: mocks.chatCopyAsset,
  listAssets: mocks.listAssets,
  searchAssets: mocks.searchAssets,
  findDuplicatesBatch: mocks.findDuplicatesBatch,
}));

import ChatCanvas from './ChatCanvas';
import '../projects/upload/registerUploadUi';
import { PLUS_MENU_LABEL } from './chat/PlusMenu';
import { __resetSessionProjectStoreForTests, seedSessionProject } from '../projects/sessionProjectStore';
import { __resetChatAttachmentsForTests } from '../projects/upload/chatAttachments';
import { __resetUploadQueueForTests, __setTransferDepsForTests, getProjectEntries } from '../projects/upload/uploadQueue';
import { DEFAULT_CAPABILITIES } from '../projects/upload/kinds';
import { normalizeUserTurnContent } from '../promptNormalize';
import type { PutFn } from '../projects/upload/transfer';

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub;

const TRAILER = { projectId: 'pTrailer', name: 'Trailer cuts', role: 'owner', personal: false };

async function flush() {
  await act(async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    for (let i = 0; i < 20; i++) await Promise.resolve();
  });
}
/**
 * The attachment REFERENCE the chat-copy route answers for a project asset (asset-lineage Phase 2: no bytes are
 * copied, so the key is the asset's own object and the url is a short-lived presigned GET of it).
 */
function assetRef(assetId: string, name: string, contentType = 'image/png', size = 10) {
  const key = `projects/pTrailer/${assetId}/${name}`;
  return {
    assetId,
    projectId: 'pTrailer',
    displayName: name,
    kind: contentType.split('/')[0],
    sizeBytes: size,
    url: `https://s3.example/${key}?presigned`,
    expiresAt: 1_700_000_000,
    sourceUri: `s3://ingest/${key}`,
    bucket: 'ingest',
    key,
    name,
    size,
    contentType,
    filename: name,
  };
}
function file(name: string, size = 10, type = 'image/png'): File {
  return new File([new Uint8Array(size)], name, { type });
}
function pick(container: HTMLElement, files: File[]) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files } });
}
function sentBody(): Record<string, unknown> {
  return mocks.invokeAgentStreaming.mock.calls[0][0] as Record<string, unknown>;
}
const sendButton = () => screen.getByRole('button', { name: 'Send message' });
function type(text: string) {
  fireEvent.change(screen.getByLabelText('Chat message input'), { target: { value: text } });
}

let put: ReturnType<typeof vi.fn<PutFn>>;
let grantSeq = 0;

beforeEach(async () => {
  vi.clearAllMocks();
  localStorage.clear();
  grantSeq = 0;
  __resetSessionProjectStoreForTests();
  __resetChatAttachmentsForTests();
  __resetUploadQueueForTests();
  put = vi.fn<PutFn>().mockImplementation(async (_url, body, opts) => {
    opts.onProgress?.(body.size);
    return { etag: '"e"' };
  });
  __setTransferDepsForTests({ put, sleep: () => Promise.resolve() });
  mocks.useLabContext.mockReturnValue({ sessionId: 'S1', refreshCategory: vi.fn() });
  mocks.createSession.mockResolvedValue(true);
  mocks.updateSessionActivity.mockResolvedValue(undefined);
  mocks.getConfig.mockResolvedValue({});
  mocks.invokeAgentStreaming.mockResolvedValue(undefined);
  mocks.listProjects.mockResolvedValue({ ok: true, value: [TRAILER] });
  mocks.patchSessionProject.mockResolvedValue({ ok: true, value: undefined });
  mocks.getIngestionCapabilities.mockResolvedValue({ ok: true, value: DEFAULT_CAPABILITIES });
  mocks.createUploads.mockImplementation(async (_pid: string, specs: { name: string }[]) => ({
    ok: true,
    value: {
      uploads: specs.map((s, index) => {
        const assetId = `image:${++grantSeq}`;
        return { index, assetId, key: `projects/pTrailer/${assetId}/${s.name}`, mode: 'single', url: `https://s3/${assetId}`, headers: { 'Content-Type': 'image/png' } };
      }),
      errors: {},
    },
  }));
  const names = new Map<string, string>();
  mocks.registerAsset.mockImplementation(async (_pid: string, f: { assetId: string; fileName: string }) => {
    names.set(f.assetId, f.fileName);
    return { ok: true, value: { assetId: f.assetId, displayName: f.fileName, kind: 'image', status: 'queued' } };
  });
  mocks.chatCopyAsset.mockImplementation(async (_pid: string, assetId: string) =>
    ({ ok: true, value: assetRef(assetId, names.get(assetId) ?? 'x.png') }));
  mocks.upload.mockImplementation(async (f: File) => ({
    key: `artifacts/S1/input/1-${f.name}`, bucket: 'art', url: '', cloudfrontUrl: `https://cdn.example/artifacts/S1/input/1-${f.name}`,
    size: f.size, filename: f.name,
  }));
  mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [] } });
  mocks.searchAssets.mockResolvedValue({ ok: true, value: { assets: [], truncated: false } });
  mocks.findDuplicatesBatch.mockResolvedValue({ ok: true, value: { duplicates: [] } });
  mocks.projectsEnabled.mockResolvedValue(true);
  await seedSessionProject('S1', { activeProjectId: TRAILER.projectId, activeProjectName: TRAILER.name, webSearch: false });
});

describe('Attach files', () => {
  it('opens a multi-file picker from the + menu', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toHaveAttribute('multiple');
    const click = vi.spyOn(input, 'click');
    fireEvent.click(screen.getByRole('button', { name: PLUS_MENU_LABEL }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Attach files/ }));
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('switch ON (default): each file goes into the Active_Project, then is copied into this chat as a chip', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png'), file('b.png')]);
    await flush();

    expect(mocks.createUploads).toHaveBeenCalledWith('pTrailer', [
      { name: 'a.png', size: 10, contentType: 'image/png' },
      { name: 'b.png', size: 10, contentType: 'image/png' },
    ]);
    expect(put).toHaveBeenCalledTimes(2);
    expect(mocks.registerAsset).toHaveBeenCalledTimes(2);
    expect(mocks.chatCopyAsset).toHaveBeenCalledWith('pTrailer', 'image:1', 'S1');
    expect(mocks.chatCopyAsset).toHaveBeenCalledWith('pTrailer', 'image:2', 'S1');
    expect(mocks.upload).not.toHaveBeenCalled();
    // The session row is created before the first upload, as the first turn would.
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    expect(screen.getAllByTestId('attached-file-chip').map((c) => c.textContent)).toEqual([
      'a.png (10 B)',
      'b.png (10 B)',
    ]);
    // Every file finished: the sheet is gone, the uploads are the Project's too.
    expect(screen.queryByTestId('chat-upload-sheet')).toBeNull();
    expect(getProjectEntries('pTrailer').map((e) => e.status)).toEqual(['ready', 'ready']);
  });

  it('switch OFF ("this chat only") uploads into the project as a HIDDEN session asset, never POST /upload', async () => {
    // The first transfer never finishes (so the sheet is on screen to flip); the restarted one finishes on release.
    let release: () => void = () => {};
    put.mockImplementationOnce(() => new Promise(() => {}));
    put.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({ etag: '"e"' }); }));
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png')]);
    await flush();
    const sheet = screen.getByTestId('chat-upload-sheet');
    const sw = within(sheet).getByRole('switch', { name: 'Add to Trailer cuts' });
    expect(sw).toHaveAttribute('aria-checked', 'true');
    expect(sheet).toHaveTextContent('Files are added to Trailer cuts and attached to this chat.');
    expect(within(sheet).getByRole('progressbar', { name: 'Uploading a.png' })).toBeInTheDocument();

    fireEvent.click(sw);
    await flush();
    expect(within(screen.getByTestId('chat-upload-sheet')).getByRole('switch', { name: 'Add to Trailer cuts' }))
      .toHaveAttribute('aria-checked', 'false');
    expect(screen.getByTestId('chat-upload-sheet')).toHaveTextContent('This chat only');
    // The project transfer was cancelled and the file restarted as a session-visibility upload (D2): only the
    // cancelled project attempt is listed among the Project's uploads — the hidden one never is.
    expect(getProjectEntries('pTrailer').map((e) => e.status)).toEqual(['canceled']);
    expect(mocks.upload).not.toHaveBeenCalled();
    expect(sendButton()).toBeDisabled();

    await act(async () => release());
    await flush();
    expect(mocks.registerAsset).toHaveBeenCalledWith('pTrailer', {
      assetId: 'image:2',
      fileName: 'a.png',
      contentType: 'image/png',
      size: 10,
      visibility: 'session',
      sessionId: 'S1',
    });
    expect(screen.getByTestId('attached-file-chip')).toHaveTextContent('a.png');
    expect(sendButton()).toBeEnabled();

    // The next file stays on the chat-only path too.
    pick(container, [file('b.png')]);
    await flush();
    expect(mocks.createUploads).toHaveBeenCalledTimes(3);
    expect(mocks.upload).not.toHaveBeenCalled();
    expect((mocks.registerAsset.mock.calls[1][1] as { visibility?: string }).visibility).toBe('session');
    expect(screen.getAllByTestId('attached-file-chip')).toHaveLength(2);
  });

  it('disables Send while any file is uploading, then sends every attachment in order', async () => {
    let release: () => void = () => {};
    put.mockImplementationOnce(
      (_url, body, opts) =>
        new Promise((resolve) => {
          opts.onProgress?.(body.size / 2);
          release = () => resolve({ etag: '"e"' });
        }),
    );
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png'), file('b.png')]);
    await flush();
    type('compare these');
    expect(sendButton()).toBeDisabled();
    expect(within(screen.getByTestId('chat-upload-sheet')).getByRole('progressbar', { name: 'Uploading a.png' }))
      .toHaveAttribute('aria-valuenow', '50');
    // Enter does not send either.
    fireEvent.keyDown(screen.getByLabelText('Chat message input'), { key: 'Enter' });
    await flush();
    expect(mocks.invokeAgentStreaming).not.toHaveBeenCalled();

    await act(async () => release());
    await flush();
    expect(sendButton()).toBeEnabled();
    fireEvent.click(sendButton());
    await flush();

    const body = sentBody();
    const prompt = body.prompt as string;
    // One block per file, in order, then the message; promptNormalize strips them all.
    expect(prompt.match(/\[File attached:/g)).toHaveLength(2);
    expect(prompt.indexOf('a.png')).toBeLessThan(prompt.indexOf('b.png'));
    // The block names the project asset (asset-lineage R1.1) and its own location — not a session-path copy.
    expect(prompt).toContain('project_id: pTrailer\nasset_id: image:1\nS3 bucket: ingest\n'
      + 'S3 key: projects/pTrailer/image:1/a.png\nFull S3 URI: s3://ingest/projects/pTrailer/image:1/a.png');
    expect(prompt).not.toContain('artifacts/S1/input');
    expect(prompt).toContain('Public HTTPS URL');
    expect(prompt.endsWith('\n\ncompare these')).toBe(true);
    expect(normalizeUserTurnContent(prompt)).toBe('compare these');
    expect(body.display_text).toBe('📎 a.png (10 B)\n📎 b.png (10 B)\ncompare these');
    expect(normalizeUserTurnContent(body.display_text as string)).toBe('compare these');
    const refs = [
      { url: 'https://s3.example/projects/pTrailer/image:1/a.png?presigned', name: 'a.png' },
      { url: 'https://s3.example/projects/pTrailer/image:2/b.png?presigned', name: 'b.png' },
    ];
    expect(body.attachment).toEqual(refs[0]);
    expect(body.attachments).toEqual(refs);
    expect(body.active_project_id).toBe('pTrailer');
    // The chips are consumed by the turn.
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
  });

  it('a chip removed before send is not sent', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png'), file('b.png')]);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Remove attached file a.png' }));
    expect(screen.getAllByTestId('attached-file-chip')).toHaveLength(1);
    fireEvent.click(sendButton());
    await flush();
    const body = sentBody();
    expect(body.attachments).toEqual([{ url: 'https://s3.example/projects/pTrailer/image:2/b.png?presigned', name: 'b.png' }]);
    expect((body.prompt as string).match(/\[File attached:/g)).toHaveLength(1);
    // Only attachments, no text: the message is empty and the turn still goes.
    expect(body.display_text).toBe('📎 b.png (10 B)\n');
  });

  it('a drop of several files attaches each of them', async () => {
    render(<ChatCanvas />);
    await flush();
    fireEvent.drop(screen.getByLabelText('Chat message input'), {
      dataTransfer: { files: [file('one.png'), file('two.png'), file('three.png')] },
    });
    await flush();
    expect(mocks.createUploads.mock.calls[0][1]).toHaveLength(3);
    expect(screen.getAllByTestId('attached-file-chip')).toHaveLength(3);
  });

  it('refuses more than 20 files with the count and the limit, uploading nothing', async () => {
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, Array.from({ length: 21 }, (_, i) => file(`f${i}.png`)));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('You selected 21 files. Up to 20 files can be uploaded at once.');
    expect(mocks.createUploads).not.toHaveBeenCalled();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it('shows a failed chat copy in the sheet with Retry, which copies again without re-uploading', async () => {
    mocks.chatCopyAsset.mockResolvedValueOnce({ ok: false, kind: 'error', status: 500, code: 'copy_failed', message: 'copy failed' });
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png')]);
    await flush();
    const sheet = screen.getByTestId('chat-upload-sheet');
    expect(sheet).toHaveTextContent('Added to Trailer cuts, but not to this chat (copy failed).');
    fireEvent.click(within(sheet).getByRole('button', { name: 'Retry a.png' }));
    await flush();
    expect(mocks.chatCopyAsset).toHaveBeenCalledTimes(2);
    expect(mocks.createUploads).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('attached-file-chip')).toHaveTextContent('a.png');
  });

  it('with Projects disabled there is no switch and files take POST /upload', async () => {
    mocks.projectsEnabled.mockResolvedValue(false);
    mocks.upload.mockImplementationOnce(() => new Promise(() => {}));
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('a.png')]);
    await flush();
    expect(within(screen.getByTestId('chat-upload-sheet')).queryByRole('switch')).toBeNull();
    expect(mocks.upload).toHaveBeenCalledTimes(1);
    expect(mocks.createUploads).not.toHaveBeenCalled();
    // Cancel from the sheet.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel a.png' }));
    await flush();
    expect(screen.getByTestId('chat-upload-sheet')).toHaveTextContent('Canceled');
    expect(sendButton()).toBeDisabled(); // no text, no ready attachment
  });
});

describe('Recent in <Active_Project>', () => {
  const POSTER = { assetId: 'image:77', displayName: 'poster.png', kind: 'image', status: 'ready', sourceSize: 10, contentType: 'image/png' };

  beforeEach(() => {
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [POSTER] } });
    mocks.chatCopyAsset.mockImplementation(async (_pid: string, assetId: string) =>
      ({ ok: true, value: assetRef(assetId, 'poster.png') }));
  });

  async function pickRecent(name: RegExp) {
    fireEvent.click(screen.getByRole('button', { name: PLUS_MENU_LABEL }));
    await flush();
    const menu = screen.getByRole('menu', { name: PLUS_MENU_LABEL });
    fireEvent.click(within(menu).getByRole('menuitem', { name }));
    await flush();
  }

  it('attaches a project file without re-uploading: session row first, chat-copy into this session, the same chip and prompt block', async () => {
    render(<ChatCanvas />);
    await flush();
    await pickRecent(/^poster\.png/);

    expect(mocks.listAssets).toHaveBeenCalledWith('pTrailer');
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    expect(mocks.chatCopyAsset).toHaveBeenCalledTimes(1);
    expect(mocks.chatCopyAsset).toHaveBeenCalledWith('pTrailer', 'image:77', 'S1');
    // The session row exists before the copy, as for an upload.
    expect(mocks.createSession.mock.invocationCallOrder[0]).toBeLessThan(mocks.chatCopyAsset.mock.invocationCallOrder[0]);
    // Nothing uploads.
    expect(mocks.createUploads).not.toHaveBeenCalled();
    expect(mocks.upload).not.toHaveBeenCalled();
    expect(put).not.toHaveBeenCalled();
    // The chip an uploaded file gets.
    const chip = screen.getByTestId('attached-file-chip');
    expect(chip).toHaveTextContent('poster.png (10 B)');
    expect(screen.getByRole('button', { name: 'Remove attached file poster.png' })).toBeInTheDocument();
    expect(screen.queryByTestId('chat-upload-sheet')).toBeNull();

    type('what is in this');
    fireEvent.click(sendButton());
    await flush();
    const body = sentBody();
    const prompt = body.prompt as string;
    expect(prompt.match(/\[File attached:/g)).toHaveLength(1);
    expect(prompt).toContain('[File attached: poster.png (10 B)');
    expect(prompt).toContain('project_id: pTrailer\nasset_id: image:77\nS3 bucket: ingest\n'
      + 'S3 key: projects/pTrailer/image:77/poster.png\nFull S3 URI: s3://ingest/projects/pTrailer/image:77/poster.png');
    expect(prompt.endsWith('\n\nwhat is in this')).toBe(true);
    expect(normalizeUserTurnContent(prompt)).toBe('what is in this');
    expect(body.display_text).toBe('📎 poster.png (10 B)\nwhat is in this');
    const ref = { url: 'https://s3.example/projects/pTrailer/image:77/poster.png?presigned', name: 'poster.png' };
    expect(body.attachment).toEqual(ref);
    expect(body.attachments).toEqual([ref]);
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
  });

  it('a project video named "x.mp4 (2)" still renders in the panel (session fd4ce1bd)', async () => {
    const COPY = { assetId: 'video:9', displayName: 'Meridian.mp4 (2)', kind: 'video', status: 'ready',
      sourceSize: 10, contentType: 'video/mp4' };
    mocks.listAssets.mockResolvedValue({ ok: true, value: { assets: [COPY] } });
    mocks.chatCopyAsset.mockImplementation(async (_pid: string, assetId: string) =>
      ({ ok: true, value: { ...assetRef(assetId, 'Meridian.mp4', 'video/mp4'), name: 'Meridian.mp4 (2)',
                            displayName: 'Meridian.mp4 (2)' } }));
    const rendered: string[] = [];
    const onRender = (e: Event) => rendered.push((e as CustomEvent).detail.url);
    window.addEventListener('lab:render-media', onRender);
    try {
      render(<ChatCanvas />);
      await flush();
      await pickRecent(/^Meridian\.mp4 \(2\)/);
      type('probe it');
      fireEvent.click(sendButton());
      await flush();
    } finally {
      window.removeEventListener('lab:render-media', onRender);
    }
    expect(rendered).toEqual(['https://s3.example/projects/pTrailer/video:9/Meridian.mp4?presigned']);
  });

  it('an attached project image opens in the panel as one tab (lab:render-urls)', async () => {
    const tabs: Array<{ urls: string[]; label: string; sessionId: string }> = [];
    const media: string[] = [];
    const onUrls = (e: Event) => tabs.push((e as CustomEvent).detail);
    const onMedia = (e: Event) => media.push((e as CustomEvent).detail.url);
    window.addEventListener('lab:render-urls', onUrls);
    window.addEventListener('lab:render-media', onMedia);
    try {
      render(<ChatCanvas />);
      await flush();
      await pickRecent(/^poster\.png/);
      type('look at this');
      fireEvent.click(sendButton());
      await flush();
    } finally {
      window.removeEventListener('lab:render-urls', onUrls);
      window.removeEventListener('lab:render-media', onMedia);
    }
    expect(tabs).toEqual([{ urls: ['https://s3.example/projects/pTrailer/image:77/poster.png?presigned'], label: 'poster.png', sessionId: 'S1' }]);
    expect(media).toEqual([]); // the video-only NLE path is not used for an image
  });

  it('holds Send while the reference is taken, then enables it once the chip appears', async () => {
    let release: () => void = () => {};
    mocks.chatCopyAsset.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ ok: true, value: assetRef('image:77', 'poster.png') });
        }),
    );
    render(<ChatCanvas />);
    await flush();
    type('hi');
    await pickRecent(/^poster\.png/);
    expect(sendButton()).toBeDisabled();
    expect(within(screen.getByTestId('chat-upload-sheet')).getByText('Attaching')).toBeInTheDocument();
    await act(async () => release());
    await flush();
    expect(screen.getByTestId('attached-file-chip')).toHaveTextContent('poster.png');
    expect(sendButton()).toBeEnabled();
  });

  it('a failed reference shows in the sheet with Retry, which tries again', async () => {
    mocks.chatCopyAsset.mockResolvedValueOnce({ ok: false, kind: 'error', status: 502, code: 'copy_failed', message: 'copy failed' });
    render(<ChatCanvas />);
    await flush();
    await pickRecent(/^poster\.png/);
    const sheet = screen.getByTestId('chat-upload-sheet');
    expect(sheet).toHaveTextContent('poster.png could not be attached to this chat (copy failed).');
    fireEvent.click(within(sheet).getByRole('button', { name: 'Retry poster.png' }));
    await flush();
    expect(mocks.chatCopyAsset).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('attached-file-chip')).toHaveTextContent('poster.png');
  });

  it('picking the same file twice attaches it once', async () => {
    render(<ChatCanvas />);
    await flush();
    await pickRecent(/^poster\.png/);
    await pickRecent(/^poster\.png/);
    expect(mocks.chatCopyAsset).toHaveBeenCalledTimes(1);
    expect(screen.getAllByTestId('attached-file-chip')).toHaveLength(1);
  });

  it('attaches nothing while the session is locked', async () => {
    render(<ChatCanvas locked />);
    await flush();
    await pickRecent(/^poster\.png/);
    expect(mocks.chatCopyAsset).not.toHaveBeenCalled();
    expect(screen.queryByTestId('attached-file-chip')).toBeNull();
  });
});

describe('a file already in the Active_Project', () => {
  const existing = { assetId: 'image:old', displayName: 'still.png', kind: 'image', status: 'stored', sourceSize: 10 };

  it('Use existing attaches the project asset instead of uploading it; new files still upload', async () => {
    mocks.findDuplicatesBatch.mockImplementation(async (_pid: string, files: { name: string }[]) => ({
      ok: true, value: { duplicates: files.flatMap((f, index) => (f.name === 'still.png' ? [{ index, asset: existing }] : [])) } }));
    const { container } = render(<ChatCanvas />);
    await flush();
    pick(container, [file('still.png', 10), file('new.png', 10)]);
    await flush();
    const dialog = screen.getByTestId('duplicates-dialog');
    expect(dialog).toHaveTextContent('still.png');
    expect(mocks.createUploads).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use existing' }));
    await flush();
    expect(mocks.createUploads).toHaveBeenCalledTimes(1);
    expect((mocks.createUploads.mock.calls[0][1] as { name: string }[]).map((f) => f.name)).toEqual(['new.png']);
    expect(mocks.chatCopyAsset).toHaveBeenCalledWith(TRAILER.projectId, 'image:old', 'S1');
  });

  it('a chat-only attach (switch OFF) never asks', async () => {
    mocks.findDuplicatesBatch.mockResolvedValue({ ok: true, value: { duplicates: [{ index: 0, asset: existing }] } });
    const { container } = render(<ChatCanvas />);
    await flush();
    const { setAddToProject } = await import('../projects/upload/chatAttachments');
    await act(async () => { await setAddToProject('S1', false, { project: null, maxChatBytes: 1e9, ensureSession: async () => {} } as never); });
    pick(container, [file('still.png', 10)]);
    await flush();
    expect(screen.queryByTestId('duplicates-dialog')).toBeNull();
    expect(mocks.findDuplicatesBatch).not.toHaveBeenCalled();
  });
});
