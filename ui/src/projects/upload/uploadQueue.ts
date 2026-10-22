/**
 * The Upload_Client queue: a module-level store (not React state) of every file the tab is uploading into a
 * Project. Closing the Projects_Surface or the chat's upload sheet, sending a chat turn, or switching surfaces
 * never stops a transfer, and reopening either host shows the same files. It is not persisted, so a reload loses
 * in-flight files; that is why a `beforeunload` confirmation is armed while anything is in progress.
 *
 * Per-file state machine:
 *
 *   selected → failed (rejected: empty, or over the kind's limit; no request is sent)
 *   selected → queued → uploading(pct) → registering → ready
 *                 ↘ failed (grant refused)  ↘ failed (transfer, stall, parts, completion)  ↘ failed (unverified)
 *   queued | uploading → canceled (Cancel: transfer stopped, multipart aborted, never registered)
 *   failed (not rejected) → queued (Retry: a NEW grant, so a new asset id)
 *
 * A batch is every file selected in one action, any number of them (a dropped folder can hold thousands). Grants
 * are requested lazily, {@link MAX_FILES_PER_UPLOAD_REQUEST} files per `POST /projects/:pid/uploads` (the route's cap,
 * or the Project's `maxFilesPerBatch` when lower), and only when the files already granted are about to run out.
 * Requesting every grant up front would let presigned URLs expire before a long batch reached them, and open a
 * multipart upload on S3 for each large file long before its bytes move.
 *
 * A file can carry a `folder` (its relative directory in a dropped or picked folder, see `./folders`). The raw path
 * goes to the server, which normalizes it; a path the server's rules refuse is rejected here first.
 */
import { useSyncExternalStore } from 'react';
import {
  abortUpload,
  completeUpload,
  createUploads,
  getIngestionCapabilities,
  registerAsset,
  type AssetView,
  type IngestionCapabilities,
  type ProjectAssetKind,
  type ProjectsFailure,
  type UploadGrant,
} from '../api';
import { contentTypeOf, DEFAULT_CAPABILITIES, formatBytes, kindOf } from './kinds';
import { folderOf, validateFolder } from './folders';
import { isAbort, TransferError, uploadMultipart, uploadSingle, type TransferDeps } from './transfer';

export type UploadFileStatus = 'queued' | 'uploading' | 'registering' | 'ready' | 'failed' | 'canceled';

export interface UploadEntry {
  id: string;
  projectId: string;
  batchId: string;
  name: string;
  size: number;
  contentType: string;
  kind: ProjectAssetKind;
  /** The folder the file goes into (normalized for display), when it came from a folder. */
  folder?: string;
  status: UploadFileStatus;
  /** Whole-number transfer percentage, 0 to 100. */
  pct: number;
  /** The asset id of the current grant (a Retry gets a new one). */
  assetId?: string;
  /** The registered asset, once `ready`. */
  asset?: AssetView;
  /** Why the file failed, shown beside it. */
  error?: string;
  /** Refused before any request (empty or too large): no Retry. */
  rejected?: boolean;
  /**
   * A "this chat only" upload (asset-lineage D2): it registers as a HIDDEN asset of its session, so it is not
   * listed among the Project's uploads ({@link getProjectEntries}) — only in the chat that asked for it.
   */
  hidden?: boolean;
}

export interface BatchSummary {
  total: number;
  ready: number;
  failed: number;
  rejected: number;
  canceled: number;
  /** Every file of the batch has a final result. */
  done: boolean;
}

export type StartBatchResult = { ok: true; batchId: string; fileIds: string[] } | { ok: false; message: string };

/**
 * Extra registration fields for a batch (asset-lineage D2): a "this chat only" upload registers its asset with
 * `visibility: 'session'` and the session id, so it is a hidden asset of that session. Omitted for an ordinary
 * project upload — the transfer is identical either way, only `registerAsset` differs.
 */
export interface BatchRegisterOptions {
  visibility: 'session';
  sessionId: string;
}

/** At most this many files of the tab transfer at the same time; the rest wait `queued`. */
export const MAX_PARALLEL_FILES = 3;

/** `POST /projects/:pid/uploads` accepts at most this many files per request; larger batches are chunked. */
export const MAX_FILES_PER_UPLOAD_REQUEST = 20;

/** Ask for the next chunk of grants once fewer than this many granted files wait for a transfer slot. */
const GRANT_LOW_WATER = MAX_PARALLEL_FILES;

const IN_PROGRESS: ReadonlySet<UploadFileStatus> = new Set(['queued', 'uploading', 'registering']);
const TERMINAL: ReadonlySet<UploadFileStatus> = new Set(['ready', 'failed', 'canceled']);

export function isInProgress(status: UploadFileStatus): boolean {
  return IN_PROGRESS.has(status);
}
export function isTerminal(status: UploadFileStatus): boolean {
  return TERMINAL.has(status);
}

// ------------------------------------------------------------------------------------------ store

interface Runtime {
  file: File;
  /** The raw relative folder sent to the server. */
  folder?: string;
  /** Extra fields this file's `registerAsset` carries (a session-visibility upload). */
  register?: BatchRegisterOptions;
  grant?: UploadGrant;
  controller?: AbortController;
}

const entries = new Map<string, UploadEntry>();
const runtimes = new Map<string, Runtime>();
const batches = new Map<string, { projectId: string; fileIds: string[] }>();
const listeners = new Set<() => void>();
/** Granted files waiting for a transfer slot. */
const pendingTransfers: string[] = [];
/** Queued files with no grant yet, oldest first. */
const awaitingGrant: string[] = [];
let grantInFlight = false;
let activeTransfers = 0;
/** Bumped by the test reset so a transfer from before it cannot decrement the new count. */
let generation = 0;
let version = 0;
const projectSnapshots = new Map<string, { version: number; list: UploadEntry[] }>();
const capabilityCache = new Map<string, Promise<IngestionCapabilities>>();

let transferDeps: TransferDeps = {};

function newId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `u-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function emit(): void {
  version++;
  syncBeforeUnload();
  for (const l of [...listeners]) l();
}

function patch(id: string, changes: Partial<UploadEntry>): UploadEntry | undefined {
  const cur = entries.get(id);
  if (!cur) return undefined;
  const next = { ...cur, ...changes };
  entries.set(id, next);
  emit();
  return next;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** One file, or undefined once dismissed. */
export function getEntry(id: string): UploadEntry | undefined {
  return entries.get(id);
}

/**
 * Every file of the tab for one Project, oldest first. A stable array between changes. A `hidden` ("this chat
 * only") file is left out: the user asked for it not to be part of the Project.
 */
export function getProjectEntries(projectId: string): UploadEntry[] {
  const hit = projectSnapshots.get(projectId);
  if (hit && hit.version === version) return hit.list;
  const list = [...entries.values()].filter((e) => e.projectId === projectId && !e.hidden);
  // Reuse the previous array when nothing of this Project changed, so unrelated changes do not re-render.
  if (hit && hit.list.length === list.length && hit.list.every((e, i) => e === list[i])) {
    hit.version = version;
    return hit.list;
  }
  projectSnapshots.set(projectId, { version, list });
  return list;
}

/** React: the Project's files, re-rendering on every change of them. */
export function useProjectUploads(projectId: string | null): UploadEntry[] {
  return useSyncExternalStore(
    subscribe,
    () => (projectId ? getProjectEntries(projectId) : EMPTY),
    () => EMPTY,
  );
}
const EMPTY: UploadEntry[] = [];

/** Counts for one batch. */
export function batchSummary(batchId: string): BatchSummary {
  const b = batches.get(batchId);
  const list = (b?.fileIds ?? []).map((id) => entries.get(id)).filter((e): e is UploadEntry => !!e);
  const s: BatchSummary = { total: list.length, ready: 0, failed: 0, rejected: 0, canceled: 0, done: true };
  for (const e of list) {
    if (e.status === 'ready') s.ready++;
    else if (e.status === 'canceled') s.canceled++;
    else if (e.status === 'failed') {
      if (e.rejected) s.rejected++;
      else s.failed++;
    } else s.done = false;
  }
  return s;
}

/** True while any file of the tab is queued, transferring, or registering. */
export function isTransferring(): boolean {
  for (const e of entries.values()) if (IN_PROGRESS.has(e.status)) return true;
  return false;
}

// ------------------------------------------------------------------------------------------ beforeunload

let unloadArmed = false;
function onBeforeUnload(e: BeforeUnloadEvent): string {
  e.preventDefault();
  // Legacy browsers need returnValue set; the text itself is never shown.
  e.returnValue = '';
  return '';
}
function syncBeforeUnload(): void {
  if (typeof window === 'undefined') return;
  const want = isTransferring();
  if (want === unloadArmed) return;
  unloadArmed = want;
  if (want) window.addEventListener('beforeunload', onBeforeUnload);
  else window.removeEventListener('beforeunload', onBeforeUnload);
}

// ------------------------------------------------------------------------------------------ capabilities

/** The Project's upload capabilities, read once per Project per tab; the defaults when unreadable. */
export function capabilitiesFor(projectId: string): Promise<IngestionCapabilities> {
  let p = capabilityCache.get(projectId);
  if (!p) {
    p = getIngestionCapabilities(projectId)
      .then((r) => (r.ok && r.value && typeof r.value.maxFilesPerBatch === 'number' ? normalizeCaps(r.value) : DEFAULT_CAPABILITIES))
      .catch(() => DEFAULT_CAPABILITIES);
    capabilityCache.set(projectId, p);
  }
  return p;
}

function normalizeCaps(c: IngestionCapabilities): IngestionCapabilities {
  return {
    maxFilesPerBatch: c.maxFilesPerBatch || DEFAULT_CAPABILITIES.maxFilesPerBatch,
    limits: { ...DEFAULT_CAPABILITIES.limits, ...(c.limits ?? {}) },
    multipartThresholdBytes: c.multipartThresholdBytes || DEFAULT_CAPABILITIES.multipartThresholdBytes,
    partSizeBytes: c.partSizeBytes || DEFAULT_CAPABILITIES.partSizeBytes,
  };
}

/** The message for a batch over the file limit. */
export function tooManyFilesMessage(count: number, max: number): string {
  return `You selected ${count} files. Up to ${max} files can be uploaded at once.`;
}

function failureMessage(r: ProjectsFailure): string {
  if (r.kind === 'not_found' || r.kind === 'access_lost') return 'You no longer have access to this project.';
  if (r.kind === 'disabled') return 'Projects are not enabled.';
  if (r.status === 401) return 'Sign in again to upload.';
  if (r.code === 'invalid_folder') return `A folder name in this upload was not accepted${r.message ? `: ${r.message}` : '.'}`;
  return r.message || 'The request failed.';
}

function refusalMessage(code: string, entry: UploadEntry, caps: IngestionCapabilities | null): string {
  if (code === 'too_large') {
    const limit = caps?.limits[entry.kind];
    return limit ? `Too large: the limit for ${entry.kind} files is ${formatBytes(limit)}.` : 'Too large to upload.';
  }
  if (code === 'bad_request') return 'The upload request was not accepted.';
  if (code === 'invalid_folder') return 'The folder name was not accepted.';
  return `The upload was refused (${code}).`;
}

// ------------------------------------------------------------------------------------------ actions

/**
 * Start a batch of any number of files into `projectId`. Resolves `{ok: false, message}` and starts nothing for an
 * empty selection. Otherwise every file gets an entry: rejected files (empty, over the kind's limit, or a folder
 * the server would refuse) fail at once, the rest are queued and transfer in the background.
 *
 * `register` adds fields to each file's registration ({@link BatchRegisterOptions}); the transfer is unchanged.
 */
export async function startBatch(
  projectId: string,
  files: ArrayLike<File>,
  register?: BatchRegisterOptions,
  /** The folder open in the Assets grid: every file lands in it (a picked folder's own tree nests under it). */
  baseFolder?: string,
): Promise<StartBatchResult> {
  const list = Array.from(files);
  if (list.length < 1) return { ok: false, message: 'Choose at least one file.' };
  const caps = await capabilitiesFor(projectId);
  const batchId = newId();
  const fileIds: string[] = [];
  for (const file of list) {
    const id = newId();
    const contentType = contentTypeOf(file);
    const kind = kindOf(contentType, file.name);
    const limit = caps.limits[kind];
    const own = folderOf(file);
    const rawFolder = [baseFolder, own].filter((p): p is string => !!p && !!p.trim()).join('/') || undefined;
    const folderCheck = rawFolder ? validateFolder(rawFolder) : null;
    const base: UploadEntry = {
      id,
      projectId,
      batchId,
      name: file.name,
      size: file.size,
      contentType,
      kind,
      ...(rawFolder ? { folder: folderCheck?.ok ? folderCheck.folder : rawFolder } : {}),
      ...(register ? { hidden: true } : {}),
      status: 'queued',
      pct: 0,
    };
    if (folderCheck && !folderCheck.ok) {
      entries.set(id, { ...base, status: 'failed', rejected: true, error: `Folder not allowed: ${folderCheck.reason}` });
    } else if (file.size === 0) {
      entries.set(id, { ...base, status: 'failed', rejected: true, error: 'The file is empty.' });
    } else if (typeof limit === 'number' && file.size > limit) {
      entries.set(id, {
        ...base,
        status: 'failed',
        rejected: true,
        error: `${formatBytes(file.size)} is over the ${formatBytes(limit)} limit for ${kind} files.`,
      });
    } else {
      entries.set(id, base);
      runtimes.set(id, { file, ...(rawFolder ? { folder: rawFolder } : {}), ...(register ? { register } : {}) });
      awaitingGrant.push(id);
    }
    fileIds.push(id);
  }
  batches.set(batchId, { projectId, fileIds });
  emit();
  pump();
  return { ok: true, batchId, fileIds };
}

/** Grants for the next chunk of waiting files of one Project, when the granted ones are about to run out. */
function requestMoreGrants(): void {
  if (grantInFlight || pendingTransfers.length >= GRANT_LOW_WATER) return;
  while (awaitingGrant.length > 0 && entries.get(awaitingGrant[0])?.status !== 'queued') awaitingGrant.shift();
  if (awaitingGrant.length === 0) return;
  const projectId = (entries.get(awaitingGrant[0]) as UploadEntry).projectId;
  grantInFlight = true;
  const gen = generation;
  void capabilitiesFor(projectId)
    .then((caps) => {
      if (gen !== generation) return undefined;
      const size = Math.max(1, Math.min(MAX_FILES_PER_UPLOAD_REQUEST, caps.maxFilesPerBatch || MAX_FILES_PER_UPLOAD_REQUEST));
      const chunk: string[] = [];
      for (let i = 0; i < awaitingGrant.length && chunk.length < size; ) {
        const id = awaitingGrant[i];
        const e = entries.get(id);
        if (!e || e.status !== 'queued') {
          awaitingGrant.splice(i, 1);
        } else if (e.projectId !== projectId) {
          i++;
        } else {
          chunk.push(id);
          awaitingGrant.splice(i, 1);
        }
      }
      return chunk.length > 0 ? requestGrants(projectId, chunk) : undefined;
    })
    .catch(() => undefined)
    .finally(() => {
      if (gen !== generation) return;
      grantInFlight = false;
      pump();
    });
}

async function requestGrants(projectId: string, ids: string[]): Promise<void> {
  const specs = ids.map((id) => {
    const e = entries.get(id) as UploadEntry;
    const folder = runtimes.get(id)?.folder;
    return { name: e.name, size: e.size, contentType: e.contentType, ...(folder ? { folder } : {}) };
  });
  const r = await createUploads(projectId, specs).catch(
    (err: unknown): ProjectsFailure => ({
      ok: false,
      kind: 'error',
      status: 0,
      code: 'network_error',
      message: err instanceof Error ? err.message : 'The request failed.',
    }),
  );
  if (!r.ok) {
    const message = failureMessage(r);
    for (const id of ids) {
      if (entries.get(id)?.status === 'queued') patch(id, { status: 'failed', error: message });
    }
    return;
  }
  const caps = await capabilitiesFor(projectId);
  const byIndex = new Map<number, UploadGrant>();
  for (const g of r.value.uploads) byIndex.set(g.index, g);
  ids.forEach((id, index) => {
    const entry = entries.get(id);
    const grant = byIndex.get(index);
    if (!entry) return;
    if (entry.status !== 'queued') {
      // Cancelled while the grant was being issued: release a multipart upload that now exists.
      if (grant?.mode === 'multipart') void abortUpload(projectId, grant.assetId, grant.uploadId).catch(() => {});
      return;
    }
    if (grant) {
      const rt = runtimes.get(id);
      if (rt) rt.grant = grant;
      patch(id, { assetId: grant.assetId });
      pendingTransfers.push(id);
      return;
    }
    const refusal = r.value.errors[String(index)];
    if (refusal) {
      patch(id, {
        status: 'failed',
        error: refusalMessage(refusal.code, entry, caps),
        rejected: refusal.code === 'too_large' || undefined,
      });
    } else {
      patch(id, { status: 'failed', error: 'No upload was issued for this file.' });
    }
  });
}

function pump(): void {
  while (activeTransfers < MAX_PARALLEL_FILES && pendingTransfers.length > 0) {
    const id = pendingTransfers.shift() as string;
    if (entries.get(id)?.status !== 'queued') continue;
    activeTransfers++;
    const gen = generation;
    void runTransfer(id).finally(() => {
      if (gen !== generation) return;
      activeTransfers--;
      pump();
    });
  }
  requestMoreGrants();
}

async function runTransfer(id: string): Promise<void> {
  const entry = entries.get(id);
  const rt = runtimes.get(id);
  if (!entry || !rt?.grant) return;
  const { projectId } = entry;
  const grant = rt.grant;
  const controller = new AbortController();
  rt.controller = controller;
  patch(id, { status: 'uploading', pct: 0 });

  let lastPct = 0;
  const onProgress = (loaded: number) => {
    const size = entry.size || 1;
    const pct = Math.max(0, Math.min(100, Math.floor((loaded / size) * 100)));
    if (pct !== lastPct && entries.get(id)?.status === 'uploading') {
      lastPct = pct;
      patch(id, { pct });
    }
  };

  try {
    if (grant.mode === 'multipart') {
      await uploadMultipart(
        grant,
        rt.file,
        {
          onProgress,
          signal: controller.signal,
          complete: async (parts) => {
            const done = await completeUpload(projectId, grant.assetId, grant.uploadId, parts);
            if (!done.ok) throw new TransferError(`The upload could not be completed: ${failureMessage(done)}`);
          },
        },
        transferDeps,
      );
    } else {
      await uploadSingle(grant, rt.file, { onProgress, signal: controller.signal }, transferDeps);
    }
  } catch (err) {
    rt.controller = undefined;
    if (grant.mode === 'multipart') void abortUpload(projectId, grant.assetId, grant.uploadId).catch(() => {});
    if (isAbort(err) || entries.get(id)?.status === 'canceled') return;
    patch(id, { status: 'failed', error: err instanceof Error ? err.message : 'The upload failed.' });
    return;
  }
  rt.controller = undefined;
  if (entries.get(id)?.status !== 'uploading') return; // cancelled at the last moment: never register

  patch(id, { status: 'registering', pct: 100 });
  const reg = await registerAsset(projectId, {
    assetId: grant.assetId,
    fileName: entry.name,
    contentType: entry.contentType,
    size: entry.size,
    ...(rt.register ?? {}),
  }).catch(
    (e: unknown): ProjectsFailure => ({
      ok: false,
      kind: 'error',
      status: 0,
      code: 'network_error',
      message: e instanceof Error ? e.message : 'The request failed.',
    }),
  );
  if (reg.ok) {
    runtimes.delete(id); // the bytes are in the Project; Retry no longer applies
    patch(id, { status: 'ready', asset: reg.value });
    return;
  }
  const unverified = reg.kind === 'error' && (reg.code === 'object_missing' || reg.code === 'size_mismatch');
  patch(id, {
    status: 'failed',
    error: unverified ? 'The upload could not be verified.' : failureMessage(reg),
  });
}

/** Cancel a queued or transferring file: its transfer stops and it is never registered. */
export function cancel(id: string): void {
  const entry = entries.get(id);
  if (!entry || (entry.status !== 'queued' && entry.status !== 'uploading')) return;
  const rt = runtimes.get(id);
  const idx = pendingTransfers.indexOf(id);
  if (idx >= 0) pendingTransfers.splice(idx, 1);
  patch(id, { status: 'canceled', error: undefined });
  if (rt?.controller) {
    rt.controller.abort(); // runTransfer aborts a multipart upload on its way out
  } else if (rt?.grant?.mode === 'multipart') {
    void abortUpload(entry.projectId, rt.grant.assetId, rt.grant.uploadId).catch(() => {});
  }
  runtimes.delete(id);
}

/** Retry a failed (not rejected) file with a new grant and a new asset id. */
export function retry(id: string): void {
  const entry = entries.get(id);
  const rt = runtimes.get(id);
  if (!entry || entry.status !== 'failed' || entry.rejected || !rt) return;
  rt.grant = undefined;
  rt.controller = undefined;
  patch(id, { status: 'queued', pct: 0, error: undefined, assetId: undefined, asset: undefined });
  awaitingGrant.push(id);
  pump();
}

/** Retry every failed (not rejected) file of a Project. */
export function retryFailed(projectId: string): void {
  for (const e of [...entries.values()]) {
    if (e.projectId === projectId && e.status === 'failed' && !e.rejected) retry(e.id);
  }
}

/** Drop a finished file from the list. A file still in progress is left alone. */
export function dismiss(id: string): void {
  const entry = entries.get(id);
  if (!entry || !TERMINAL.has(entry.status)) return;
  entries.delete(id);
  runtimes.delete(id);
  emit();
}

/** Drop every finished file of a Project from the list. */
export function clearFinished(projectId: string): void {
  let changed = false;
  for (const e of [...entries.values()]) {
    if (e.projectId === projectId && TERMINAL.has(e.status)) {
      entries.delete(e.id);
      runtimes.delete(e.id);
      changed = true;
    }
  }
  if (changed) emit();
}

// ------------------------------------------------------------------------------------------ test seams

/** Test-only: replace the PUT / sleep the transfers use (no XHR in unit tests). */
export function __setTransferDepsForTests(deps: TransferDeps): void {
  transferDeps = deps;
}

/** Test-only: forget every file, batch, cached capability, and the beforeunload listener. */
export function __resetUploadQueueForTests(): void {
  for (const rt of runtimes.values()) rt.controller?.abort();
  entries.clear();
  runtimes.clear();
  batches.clear();
  pendingTransfers.length = 0;
  awaitingGrant.length = 0;
  grantInFlight = false;
  activeTransfers = 0;
  projectSnapshots.clear();
  capabilityCache.clear();
  transferDeps = {};
  generation++;
  if (unloadArmed && typeof window !== 'undefined') window.removeEventListener('beforeunload', onBeforeUnload);
  unloadArmed = false;
  version++;
}
