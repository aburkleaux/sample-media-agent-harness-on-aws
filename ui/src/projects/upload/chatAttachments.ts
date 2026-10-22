/**
 * Chat attachments: the files a chat composer is uploading or holding for its next turn, per session.
 *
 * Module-level like the upload queue, so switching sessions or surfaces never stops an upload, and coming back
 * shows the same files. Each file goes one of two ways, chosen by the composer's `Add to <Active_Project>` switch:
 *
 *   * `project` (switch ON, the default) — through the upload queue into the Active_Project, then the asset is
 *     referenced by this chat (`chat-copy`, which copies no bytes since asset-lineage Phase 2).
 *   * `chat` (switch OFF, "this chat only") — the same upload queue, but registered as a HIDDEN asset of this
 *     session (`visibility: 'session'`, asset-lineage D2): it is not shown in the project and is deleted with
 *     the session. With no project at all (Projects off, or not known yet) it takes the plain `POST /upload`.
 *
 * Either way a finished file is `ready` with the same fields `POST /upload` returns, and becomes an attachment
 * chip. {@link takeReady} hands the ready files to a turn and removes them. Flipping the switch moves every file
 * that is still queued or transferring onto the other path (its transfer restarts); finished files keep theirs.
 */
import { useSyncExternalStore } from 'react';
import { AgentService } from '../../services/agent-service';
import { chatCopyAsset } from '../api';
import { contentTypeOf, formatBytes } from './kinds';
import * as queue from './uploadQueue';
import type { BatchSummary, UploadFileStatus } from './uploadQueue';

export type ChatAttachmentTarget =
  | { kind: 'project'; projectId: string; projectName: string }
  /** "This chat only". `projectId` is set when the file became a hidden asset of the session (D2). */
  | { kind: 'chat'; projectId?: string; projectName?: string };

/** A finished attachment: what a turn needs to reference the file. */
export interface ReadyAttachment {
  key: string;
  /** The permanent HTTPS URL (artifacts domain) of a plain chat upload, else a short-lived presigned URL. */
  url: string;
  name: string;
  size: number;
  contentType?: string;
  bucket?: string;
  /** The project asset this attachment references (asset-lineage R1.1); absent for a plain chat upload. */
  assetId?: string;
  projectId?: string;
  /** `s3://<bucket>/<key>` of the file, when the server reported it. */
  sourceUri?: string;
}

export interface ChatAttachmentItem {
  id: string;
  batchId: string;
  name: string;
  size: number;
  contentType: string;
  target: ChatAttachmentTarget;
  status: UploadFileStatus;
  pct: number;
  error?: string;
  rejected?: boolean;
  /** Set once the file is `ready`. */
  result?: ReadyAttachment;
  /**
   * An asset already in the Project, attached from the Plus_Menu's `Recent in <project>` list: nothing
   * uploads, only the chat reference runs.
   */
  reused?: boolean;
}

export interface ChatAttachmentsState {
  items: ChatAttachmentItem[];
  /** The `Add to <Active_Project>` switch. ON unless the user turned it off in this session. */
  addToProject: boolean;
}

/** What adding files needs from the composer. */
export interface AddFilesContext {
  /** The Active_Project, or null when Projects are off or it is not known yet (then files go to the chat only). */
  project: { id: string; name: string } | null;
  /** `POST /upload`'s size cap. */
  maxChatBytes: number;
  /** Make sure the session row exists, as the composer does before any upload. Resolves even on failure. */
  ensureSession: () => Promise<unknown>;
}

export type AddFilesResult = { ok: true } | { ok: false; message: string };

/** Files per batch, as for project uploads. */
export const MAX_CHAT_FILES_PER_BATCH = 20;

interface Runtime {
  sessionId: string;
  /** The chosen file. Absent for a reused Project asset, which has no local bytes. */
  file?: File;
  ctx: AddFilesContext;
  /** The upload queue's file id (project target). */
  queueId?: string;
  /** The queued asset id, once the upload registered. */
  assetId?: string;
  /** Aborts a `POST /upload`. */
  controller?: AbortController;
  /** The chat reference failed after the asset registered: Retry references again rather than re-uploading. */
  copyFailed?: boolean;
  copying?: boolean;
}

const sessions = new Map<string, ChatAttachmentsState>();
const runtimes = new Map<string, Runtime>();
const listeners = new Set<() => void>();
let queueSubscribed = false;

const EMPTY_STATE: ChatAttachmentsState = Object.freeze({ items: [], addToProject: true }) as ChatAttachmentsState;

function newId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function emit(): void {
  for (const l of [...listeners]) l();
}

function stateOf(sessionId: string): ChatAttachmentsState {
  return sessions.get(sessionId) ?? EMPTY_STATE;
}

function setState(sessionId: string, next: ChatAttachmentsState): void {
  sessions.set(sessionId, next);
  emit();
}

function updateItem(sessionId: string, id: string, changes: Partial<ChatAttachmentItem>): void {
  const s = sessions.get(sessionId);
  if (!s) return;
  let changed = false;
  const items = s.items.map((it) => {
    if (it.id !== id) return it;
    const next = { ...it, ...changes };
    const keys = Object.keys(changes) as (keyof ChatAttachmentItem)[];
    if (keys.some((k) => next[k] !== it[k])) changed = true;
    return next;
  });
  if (changed) setState(sessionId, { ...s, items });
}

function findItem(sessionId: string, id: string): ChatAttachmentItem | undefined {
  return sessions.get(sessionId)?.items.find((it) => it.id === id);
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getChatAttachments(sessionId: string): ChatAttachmentsState {
  return stateOf(sessionId);
}

/** React: the session's attachments and switch value. */
export function useChatAttachments(sessionId: string | null): ChatAttachmentsState {
  return useSyncExternalStore(
    subscribe,
    () => (sessionId ? stateOf(sessionId) : EMPTY_STATE),
    () => EMPTY_STATE,
  );
}

/** True while any file of the session is queued, transferring, registering, or being attached to the chat. */
export function hasUploadsInProgress(sessionId: string): boolean {
  return stateOf(sessionId).items.some((it) => queue.isInProgress(it.status));
}

/** Counts for one of the session's batches. */
export function chatBatchSummary(sessionId: string, batchId: string): BatchSummary {
  const list = stateOf(sessionId).items.filter((it) => it.batchId === batchId);
  const s: BatchSummary = { total: list.length, ready: 0, failed: 0, rejected: 0, canceled: 0, done: true };
  for (const it of list) {
    if (it.status === 'ready') s.ready++;
    else if (it.status === 'canceled') s.canceled++;
    else if (it.status === 'failed') {
      if (it.rejected) s.rejected++;
      else s.failed++;
    } else s.done = false;
  }
  return s;
}

// ------------------------------------------------------------------------------------------ queue mirror

function ensureQueueSubscription(): void {
  if (queueSubscribed) return;
  queueSubscribed = true;
  queue.subscribe(syncFromQueue);
}

/** Mirror each queued file's entry; reference the asset in this chat once the upload registered. */
function syncFromQueue(): void {
  for (const [id, rt] of runtimes) {
    if (!rt.queueId || rt.copyFailed || rt.copying) continue;
    const item = findItem(rt.sessionId, id);
    if (!item || item.status === 'ready' || item.status === 'canceled') continue;
    const entry = queue.getEntry(rt.queueId);
    if (!entry) continue;
    if (entry.status === 'ready' && entry.assetId) {
      rt.assetId = entry.assetId;
      void attachToChat(id, rt);
      continue;
    }
    updateItem(rt.sessionId, id, {
      status: entry.status,
      pct: entry.pct,
      error: entry.error,
      rejected: entry.rejected,
    });
  }
}

/** The project an item's asset lives in, for either target (a chat-only file is a hidden asset of its project). */
function projectOf(target: ChatAttachmentTarget): { projectId: string; projectName?: string } | null {
  return target.projectId ? { projectId: target.projectId, projectName: target.projectName } : null;
}

/**
 * Reference the (registered) project asset in this chat: the server records the session association and answers
 * the attachment reference — the asset's ids, its `s3://` URI, and a short-lived presigned URL for preview. No
 * bytes are copied (asset-lineage Phase 2), so this is the same step for a project file and a chat-only one.
 */
async function attachToChat(id: string, rt: Runtime): Promise<void> {
  if (rt.copying || !rt.assetId) return;
  const item = findItem(rt.sessionId, id);
  const project = item ? projectOf(item.target) : null;
  if (!item || !project) return;
  rt.copying = true;
  rt.copyFailed = false;
  updateItem(rt.sessionId, id, { status: 'registering', pct: 100, error: undefined });
  await rt.ctx.ensureSession().catch(() => undefined);
  const r = await chatCopyAsset(project.projectId, rt.assetId, rt.sessionId).catch(() => null);
  rt.copying = false;
  if (!runtimes.has(id)) return; // removed meanwhile
  if (r && r.ok) {
    const v = r.value;
    updateItem(rt.sessionId, id, {
      status: 'ready',
      error: undefined,
      result: {
        key: v.key,
        url: v.url,
        name: v.name || rt.file?.name || item.name,
        size: typeof v.size === 'number' ? v.size : rt.file?.size ?? item.size,
        contentType: v.contentType,
        ...(v.bucket ? { bucket: v.bucket } : {}),
        assetId: v.assetId ?? rt.assetId,
        projectId: v.projectId ?? project.projectId,
        ...(v.sourceUri ? { sourceUri: v.sourceUri } : {}),
      },
    });
    return;
  }
  rt.copyFailed = true;
  const why = !r ? 'network error' : r.ok ? '' : r.kind === 'error' ? r.message : 'this chat could not be reached';
  updateItem(rt.sessionId, id, {
    status: 'failed',
    error: item.reused || item.target.kind === 'chat'
      ? `${item.name} could not be attached to this chat (${why}).`
      : `Added to ${project.projectName}, but not to this chat (${why}).`,
  });
}

// ------------------------------------------------------------------------------------------ chat-only upload

async function uploadToChat(id: string, rt: Runtime): Promise<void> {
  const file = rt.file;
  if (!file) return; // a reused Project asset has no bytes to upload
  const controller = new AbortController();
  rt.controller = controller;
  updateItem(rt.sessionId, id, { status: 'uploading', pct: 0, error: undefined });
  await rt.ctx.ensureSession().catch(() => undefined);
  if (controller.signal.aborted) return;
  try {
    const res = await AgentService.upload(
      file,
      rt.sessionId,
      (frac) => {
        if (findItem(rt.sessionId, id)?.status === 'uploading') {
          updateItem(rt.sessionId, id, { pct: Math.max(0, Math.min(100, Math.round(frac * 100))) });
        }
      },
      controller.signal,
    );
    rt.controller = undefined;
    if (findItem(rt.sessionId, id)?.status !== 'uploading') return;
    const url = res?.cloudfrontUrl || res?.url || '';
    updateItem(rt.sessionId, id, {
      status: 'ready',
      pct: 100,
      result: {
        key: res.key,
        url,
        name: file.name,
        size: typeof res.size === 'number' ? res.size : file.size,
        bucket: res.bucket,
        contentType: contentTypeOf(file),
      },
    });
  } catch (err) {
    rt.controller = undefined;
    if (controller.signal.aborted || findItem(rt.sessionId, id)?.status === 'canceled') return;
    const maxBytes = (err as { maxBytes?: unknown } | null)?.maxBytes;
    const message =
      typeof maxBytes === 'number'
        ? `Too large: the maximum upload size is ${formatBytes(maxBytes || rt.ctx.maxChatBytes)}.`
        : err instanceof Error
          ? err.message
          : 'The upload failed.';
    updateItem(rt.sessionId, id, { status: 'failed', error: message });
  }
}

// ------------------------------------------------------------------------------------------ actions

/**
 * Upload 1 to {@link MAX_CHAT_FILES_PER_BATCH} files for a session's next turn: into the Active_Project when the
 * session's switch is ON and a project is known, else into the chat only. A larger selection starts nothing.
 */
export async function addFiles(sessionId: string, files: ArrayLike<File>, ctx: AddFilesContext): Promise<AddFilesResult> {
  const list = Array.from(files);
  if (list.length < 1) return { ok: false, message: 'Choose at least one file.' };
  if (list.length > MAX_CHAT_FILES_PER_BATCH) {
    return { ok: false, message: queue.tooManyFilesMessage(list.length, MAX_CHAT_FILES_PER_BATCH) };
  }
  const toProject = stateOf(sessionId).addToProject && !!ctx.project;
  return toProject ? addToProjectTarget(sessionId, list, ctx) : addToChatTarget(sessionId, list, ctx);
}

/** Whether `addFiles` would put these files into the Active_Project (so a duplicate check applies). */
export function addsToProject(sessionId: string, ctx: AddFilesContext): boolean {
  return stateOf(sessionId).addToProject && !!ctx.project;
}

async function addToProjectTarget(sessionId: string, files: File[], ctx: AddFilesContext): Promise<AddFilesResult> {
  const project = ctx.project as { id: string; name: string };
  const target: ChatAttachmentTarget = { kind: 'project', projectId: project.id, projectName: project.name };
  return startQueuedBatch(sessionId, files, ctx, project, target);
}

/**
 * "This chat only" with a project in hand (asset-lineage D2): the same project upload, registered as a HIDDEN
 * asset of this session (`visibility: 'session'`). The file is attached to the chat and never shown in the
 * project, and the session's deletion removes it. With no project there is nowhere to register it, so it takes
 * the plain `POST /upload` into the session's input prefix.
 */
async function addToChatTarget(sessionId: string, files: File[], ctx: AddFilesContext): Promise<AddFilesResult> {
  const project = ctx.project;
  if (!project) return addToPlainChatUpload(sessionId, files, ctx);
  const target: ChatAttachmentTarget = { kind: 'chat', projectId: project.id, projectName: project.name };
  return startQueuedBatch(sessionId, files, ctx, project, target, { visibility: 'session', sessionId });
}

/** Start one upload-queue batch into `project` and mirror every file of it onto the composer. */
async function startQueuedBatch(
  sessionId: string,
  files: File[],
  ctx: AddFilesContext,
  project: { id: string; name: string },
  target: ChatAttachmentTarget,
  register?: queue.BatchRegisterOptions,
): Promise<AddFilesResult> {
  ensureQueueSubscription();
  const r = await queue.startBatch(project.id, files, register);
  if (!r.ok) return { ok: false, message: r.message };
  const added: ChatAttachmentItem[] = [];
  r.fileIds.forEach((queueId, i) => {
    const file = files[i];
    const entry = queue.getEntry(queueId);
    const id = newId();
    runtimes.set(id, { sessionId, file, ctx, queueId });
    added.push({
      id,
      batchId: r.batchId,
      name: file.name,
      size: file.size,
      contentType: contentTypeOf(file),
      target,
      status: entry?.status ?? 'queued',
      pct: entry?.pct ?? 0,
      error: entry?.error,
      rejected: entry?.rejected,
    });
  });
  const s = stateOf(sessionId);
  setState(sessionId, { ...s, items: [...s.items, ...added] });
  syncFromQueue();
  return { ok: true };
}

/** The pre-Projects path: `POST /upload` straight into `artifacts/<sid>/input/`, with no asset record. */
function addToPlainChatUpload(sessionId: string, files: File[], ctx: AddFilesContext): AddFilesResult {
  const batchId = newId();
  const added: ChatAttachmentItem[] = [];
  const start: string[] = [];
  for (const file of files) {
    const id = newId();
    const base: ChatAttachmentItem = {
      id,
      batchId,
      name: file.name,
      size: file.size,
      contentType: contentTypeOf(file),
      target: { kind: 'chat' },
      status: 'queued',
      pct: 0,
    };
    if (file.size > ctx.maxChatBytes) {
      added.push({
        ...base,
        status: 'failed',
        rejected: true,
        error: `${formatBytes(file.size)} is too large: the maximum upload size is ${formatBytes(ctx.maxChatBytes)}.`,
      });
      continue;
    }
    runtimes.set(id, { sessionId, file, ctx });
    added.push(base);
    start.push(id);
  }
  const s = stateOf(sessionId);
  setState(sessionId, { ...s, items: [...s.items, ...added] });
  for (const id of start) void uploadToChat(id, runtimes.get(id) as Runtime);
  return { ok: true };
}

/** The fields of a Project asset row that attaching it needs. */
export interface ProjectAssetToAttach {
  assetId: string;
  displayName: string;
  sourceSize?: number;
  contentType?: string;
}

/**
 * Attach an asset that is already in a Project to the session's next turn, without re-uploading it: make sure the
 * session row exists (as an upload does), then take the asset's chat REFERENCE (no bytes move). The item is
 * `registering` until the server answers, then `ready` with the same {@link ReadyAttachment} an upload produces,
 * so the chip, the prompt block, the bubble, and the turn body are identical. A failure offers Retry in the sheet.
 * Attaching an asset the composer already holds (and has not failed) does nothing.
 */
export function attachProjectAsset(
  sessionId: string,
  project: { id: string; name: string },
  asset: ProjectAssetToAttach,
  ctx: AddFilesContext,
): AddFilesResult {
  const s = stateOf(sessionId);
  const dup = s.items.some((it) => {
    if (!it.reused || it.status === 'failed' || it.status === 'canceled') return false;
    const rt = runtimes.get(it.id);
    return it.target.kind === 'project' && it.target.projectId === project.id && rt?.assetId === asset.assetId;
  });
  if (dup) return { ok: true };
  const id = newId();
  runtimes.set(id, { sessionId, ctx, assetId: asset.assetId });
  const item: ChatAttachmentItem = {
    id,
    batchId: newId(),
    name: asset.displayName,
    size: typeof asset.sourceSize === 'number' ? asset.sourceSize : 0,
    contentType: asset.contentType ?? '',
    target: { kind: 'project', projectId: project.id, projectName: project.name },
    status: 'registering',
    pct: 100,
    reused: true,
  };
  setState(sessionId, { ...s, items: [...s.items, item] });
  void attachToChat(id, runtimes.get(id) as Runtime);
  return { ok: true };
}

/** Stop a file that is still queued or transferring. */
export function cancelAttachment(sessionId: string, id: string): void {
  const item = findItem(sessionId, id);
  const rt = runtimes.get(id);
  if (!item || (item.status !== 'queued' && item.status !== 'uploading')) return;
  if (rt?.queueId) queue.cancel(rt.queueId);
  rt?.controller?.abort();
  updateItem(sessionId, id, { status: 'canceled', error: undefined });
}

/** Retry a failed file: the chat reference again when only that failed, else the whole upload. */
export function retryAttachment(sessionId: string, id: string): void {
  const item = findItem(sessionId, id);
  const rt = runtimes.get(id);
  if (!item || !rt || item.status !== 'failed' || item.rejected) return;
  if (rt.copyFailed) {
    void attachToChat(id, rt);
    return;
  }
  if (rt.queueId) {
    updateItem(sessionId, id, { status: 'queued', pct: 0, error: undefined });
    queue.retry(rt.queueId);
    return;
  }
  void uploadToChat(id, rt);
}

/**
 * Remove a file from the composer: an in-progress one is cancelled first. A file already in a Project stays there
 * (delete it from the Projects surface); this only takes it off the next turn.
 */
export function removeAttachment(sessionId: string, id: string): void {
  cancelAttachment(sessionId, id);
  runtimes.delete(id);
  const s = sessions.get(sessionId);
  if (!s) return;
  setState(sessionId, { ...s, items: s.items.filter((it) => it.id !== id) });
}

/** Drop every failed or cancelled file of the session (the sheet's close). */
export function dismissFinishedProblems(sessionId: string): void {
  const s = sessions.get(sessionId);
  if (!s) return;
  const keep = s.items.filter((it) => it.status !== 'failed' && it.status !== 'canceled');
  if (keep.length === s.items.length) return;
  for (const it of s.items) if (!keep.includes(it)) runtimes.delete(it.id);
  setState(sessionId, { ...s, items: keep });
}

/**
 * Set the session's `Add to <Active_Project>` switch. Files still queued or transferring on the other path are
 * cancelled there and restarted on the new one; finished files keep the path they took.
 */
export async function setAddToProject(sessionId: string, on: boolean, ctx: AddFilesContext): Promise<void> {
  const s = stateOf(sessionId);
  if (s.addToProject === on) return;
  const wantKind = on && ctx.project ? 'project' : 'chat';
  const moving = s.items.filter(
    (it) => (it.status === 'queued' || it.status === 'uploading') && it.target.kind !== wantKind,
  );
  const files: File[] = [];
  for (const it of moving) {
    const rt = runtimes.get(it.id);
    if (rt?.file) files.push(rt.file);
    removeAttachment(sessionId, it.id);
  }
  setState(sessionId, { ...stateOf(sessionId), addToProject: on });
  if (files.length > 0) await addFiles(sessionId, files, ctx);
}

/** Hand the session's ready files to a turn, in the order they were added, and remove them from the composer. */
export function takeReady(sessionId: string): ReadyAttachment[] {
  const s = sessions.get(sessionId);
  if (!s) return [];
  const ready = s.items.filter((it) => it.status === 'ready' && it.result);
  if (ready.length === 0) return [];
  for (const it of ready) runtimes.delete(it.id);
  setState(sessionId, { ...s, items: s.items.filter((it) => !ready.includes(it)) });
  return ready.map((it) => it.result as ReadyAttachment);
}

/** Put a turn's attachments back on the composer (the turn did not start, e.g. `409 project_access_lost`). */
export function restoreReady(sessionId: string, attachments: readonly ReadyAttachment[]): void {
  if (attachments.length === 0) return;
  const s = stateOf(sessionId);
  const batchId = newId();
  const restored: ChatAttachmentItem[] = attachments.map((a) => ({
    id: newId(),
    batchId,
    name: a.name,
    size: a.size,
    contentType: a.contentType ?? '',
    target: { kind: 'chat' },
    status: 'ready',
    pct: 100,
    result: a,
  }));
  setState(sessionId, { ...s, items: [...restored, ...s.items] });
}

/** Test-only: forget every session's attachments. */
export function __resetChatAttachmentsForTests(): void {
  for (const rt of runtimes.values()) rt.controller?.abort();
  sessions.clear();
  runtimes.clear();
}
