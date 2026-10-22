/**
 * Stable render-tab identity (R8.3). Filling a tab with its manifest and removing a tab must hit
 * THE tab the user meant even after the list reorders (a reconcile inserts an earlier render, a
 * composite absorbs a solo), so both address a tab by key, never by array index.
 */
import type { RenderHistoryEntry } from './types';

const RENDER_KEY_RE = /^artifacts\/([^/]+)\/render\/[\w.\-]+\.json$/i;

/** The S3 key inside an `/api/artifact?key=<key>` manifest URL, or ''. */
export function manifestKeyOfUrl(url: unknown): string {
  const m = String(url ?? '').match(/[?&]key=([^&]+)/);
  if (!m) return '';
  try { return decodeURIComponent(m[1]); } catch { return ''; }
}

/** The manifest key a tab's first block points at, or ''. */
export function entryManifestKey(entry: RenderHistoryEntry): string {
  return manifestKeyOfUrl((entry.blocks?.[0]?.data as { manifestUrl?: unknown } | undefined)?.manifestUrl);
}

/** The session a render-manifest key belongs to (`artifacts/<session>/render/<id>.json`), or null. */
export function sessionOfRenderKey(key: string): string | null {
  if (!key || key.includes('..')) return null;
  const m = RENDER_KEY_RE.exec(key);
  return m ? m[1] : null;
}

/**
 * A tab's identity: its explicit `key`, else (legacy entries) the manifest key it points at, its
 * composite key, its render request id, or its artifact urls. Deterministic for a given entry.
 */
export function renderTabKey(entry: RenderHistoryEntry): string {
  if (entry.key) return entry.key;
  const mk = entryManifestKey(entry);
  if (mk) return mk;
  const data = (entry.blocks?.[0]?.data ?? {}) as { compositeKey?: unknown; sourceKey?: unknown };
  if (typeof data.compositeKey === 'string' && data.compositeKey) return data.compositeKey;
  if (typeof data.sourceKey === 'string' && data.sourceKey) return `composite:${data.sourceKey}`;
  if (entry.renderRequestId) return `req:${entry.renderRequestId}`;
  if (entry.artifactUrls?.length) return `urls:${entry.artifactUrls.join('|')}`;
  return `legacy:${entry.timestamp}:${entry.jobId ?? ''}:${entry.label ?? ''}`;
}

/** A fresh id for a client-authored tab or view. */
export function newClientId(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}
