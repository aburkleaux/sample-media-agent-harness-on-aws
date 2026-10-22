/**
 * Where each project file is filed, for every view that shows the file outside its folder (Moments cards, the
 * expanded preview, a Renderer timeline, an image gallery). One store, so they all agree.
 *
 * Fed for free: every `assets/urls` response carries each id's `folder` (`assetUrlBatcher` calls
 * {@link noteAssetFolders}), and a card already asks for its URLs. A move made here updates it at once
 * ({@link noteMoved} / {@link noteFolderRenamed}, from `api.moveAssets` / `api.renameFolder`); a move made by the
 * chat agent marks every entry stale ({@link invalidateAssetFolders}), and a mounted view re-asks.
 *
 * `undefined` = not known yet (show nothing, never a placeholder); `""` = the top level.
 */
import { useEffect, useState } from 'react';

interface Entry { folder: string; at: number }

const STALE_MS = 60_000;
const byProject = new Map<string, Map<string, Entry>>();
const listeners = new Set<() => void>();
let fetcher: ((projectId: string, assetIds: string[]) => Promise<unknown>) | null = null;
const inflight = new Set<string>();
// When an id was last asked for, so one the route cannot resolve (removed, no access) is not re-asked on every change.
const asked = new Map<string, number>();

const emit = () => { for (const fn of [...listeners]) fn(); };
const table = (projectId: string) => {
  let t = byProject.get(projectId);
  if (!t) { t = new Map(); byProject.set(projectId, t); }
  return t;
};

/** How the store asks for unknown ids (the URL batcher, which then calls {@link noteAssetFolders}). */
export function setAssetFolderFetcher(fn: typeof fetcher): void { fetcher = fn; }

export function noteAssetFolders(projectId: string, folders: Record<string, string>): void {
  const t = table(projectId);
  const now = Date.now();
  let changed = false;
  for (const [id, folder] of Object.entries(folders)) {
    if (typeof folder !== 'string') continue;
    if (t.get(id)?.folder !== folder) changed = true;
    t.set(id, { folder, at: now });
  }
  if (changed) emit();
}

export function noteMoved(projectId: string, assetIds: string[], folder: string): void {
  noteAssetFolders(projectId, Object.fromEntries(assetIds.map((id) => [id, folder])));
}

/** A renamed folder (and its sub-folders) moves every known file under it. */
export function noteFolderRenamed(projectId: string, from: string, to: string): void {
  const t = byProject.get(projectId);
  if (!t || !from) return;
  const moved: Record<string, string> = {};
  for (const [id, e] of t) {
    if (e.folder === from) moved[id] = to;
    else if (e.folder.startsWith(`${from}/`)) moved[id] = to ? `${to}${e.folder.slice(from.length)}` : e.folder.slice(from.length + 1);
  }
  noteAssetFolders(projectId, moved);
}

/** Something else moved files (the chat agent): every entry is re-asked by the views showing it. */
export function invalidateAssetFolders(): void {
  for (const t of byProject.values()) for (const e of t.values()) e.at = 0;
  asked.clear();
  emit();
}

export function peekAssetFolder(projectId: string, assetId: string): string | undefined {
  return byProject.get(projectId)?.get(assetId)?.folder;
}

function ensure(projectId: string, assetId: string): void {
  const e = byProject.get(projectId)?.get(assetId);
  const key = `${projectId}\u0000${assetId}`;
  const now = Date.now();
  if ((e && now - e.at < STALE_MS) || !fetcher || inflight.has(key)) return;
  if (now - (asked.get(key) ?? 0) < STALE_MS) return; // asked lately (a failed ask is not repeated on every change)
  inflight.add(key);
  asked.set(key, now);
  // A location is decoration: a failed lookup shows nothing, it never surfaces as an error.
  void fetcher(projectId, [assetId]).catch(() => undefined).finally(() => inflight.delete(key));
}

/** The folder `assetId` is filed in (`""` = top level), or `undefined` while unknown. */
export function useAssetFolder(projectId: string | null | undefined, assetId: string | null | undefined): string | undefined {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!projectId || !assetId) return;
    const fn = () => { bump((n) => n + 1); ensure(projectId, assetId); };
    listeners.add(fn);
    ensure(projectId, assetId);
    return () => { listeners.delete(fn); };
  }, [projectId, assetId]);
  return projectId && assetId ? peekAssetFolder(projectId, assetId) : undefined;
}

/** {@link useAssetFolder} for several ids at once: id → folder, an id absent while unknown. */
export function useAssetFolders(projectId: string | null | undefined, assetIds: readonly string[]): Record<string, string> {
  const [, bump] = useState(0);
  const sig = assetIds.join('\n');
  useEffect(() => {
    if (!projectId || assetIds.length === 0) return;
    const fn = () => { bump((n) => n + 1); for (const id of assetIds) ensure(projectId, id); };
    listeners.add(fn);
    for (const id of assetIds) ensure(projectId, id);
    return () => { listeners.delete(fn); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, sig]);
  const out: Record<string, string> = {};
  if (projectId) for (const id of assetIds) { const f = peekAssetFolder(projectId, id); if (f !== undefined) out[id] = f; }
  return out;
}

/** Test seam. */
export function _resetAssetFolders(): void { byProject.clear(); inflight.clear(); asked.clear(); emit(); }
