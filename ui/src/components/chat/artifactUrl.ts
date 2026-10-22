// Pure, dependency-free URL classification + download helpers for chat rendering.
//
// EXTENSION-DRIVEN and host-agnostic: fal.media, CloudFront, and any public media URL all
// classify by their file extension. `trustedHost` / `ourArtifact` do NOT gate the classification
// — they only influence whether we auto-embed (security: don't auto-load arbitrary hosts) and
// how we download. Keeping this file free of React/DOM-config imports makes it fully unit-testable.

import { isProjectSessionOutputKey } from '../../shared/sessionLayout';

export type ArtifactKind = 'image' | 'video' | 'audio' | 'document' | 'link';

const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif'];
const VIDEO_EXTS = ['mp4', 'mov', 'webm', 'mkv', 'm4v'];
const AUDIO_EXTS = ['mp3', 'wav', 'flac', 'm4a', 'ogg', 'aac'];
// NB: `json` is intentionally NOT here — raw JSON is plumbing (manifests, analysis sidecars, data
// the renderer consumes), never a standalone viewer. It classifies as `link` (download chip), so
// it never opens a generic-viewer tab. Mirrors render/types.py RENDERABLE_MIMES (json excluded).
const DOC_EXTS = [
  'pdf', 'csv', 'txt', 'md', 'html', 'htm', 'zip',
  'xlsx', 'docx', 'pptx', 'vtt', 'srt', 'yaml', 'yml', 'log',
];

export interface UrlInfo {
  url: string;
  kind: ArtifactKind;
  ext: string;
  filename: string;
  /** Safe to auto-load without a click (avoids arbitrary-host tracking-pixel / IP-leak). */
  trustedHost: boolean;
  /** Served by our own bucket/CDN or the auth proxy — eligible for proxy key derivation. */
  ourArtifact: boolean;
}

function extOf(pathname: string): string {
  const base = pathname.split('#')[0].split('?')[0];
  const seg = base.split('/').pop() || '';
  const i = seg.lastIndexOf('.');
  return i > 0 ? seg.slice(i + 1).toLowerCase() : '';
}

export function filenameOf(url: string): string {
  const clean = (url || '').split('#')[0].split('?')[0];
  const seg = clean.replace(/\/+$/, '').split('/').pop() || '';
  try {
    return decodeURIComponent(seg) || 'file';
  } catch {
    return seg || 'file';
  }
}

export function classifyUrl(url: string, opts: { artifactsDomain?: string } = {}): UrlInfo {
  const raw = (url || '').trim();
  let host = '';
  let pathname = raw;
  const origin = typeof window !== 'undefined' && window.location ? window.location.origin : 'http://localhost';
  try {
    const u = new URL(raw, origin);
    host = u.hostname.toLowerCase();
    pathname = u.pathname;
  } catch {
    // Non-parseable — treat the whole string as the path.
  }

  const ext = extOf(pathname || raw);
  let kind: ArtifactKind = 'link';
  if (IMAGE_EXTS.includes(ext)) kind = 'image';
  else if (VIDEO_EXTS.includes(ext)) kind = 'video';
  else if (AUDIO_EXTS.includes(ext)) kind = 'audio';
  else if (DOC_EXTS.includes(ext)) kind = 'document';

  const isRelative = raw.startsWith('/') || (!/^[a-z][a-z0-9+.-]*:/i.test(raw));
  const dom = (opts.artifactsDomain || '').toLowerCase();
  const trustedHost =
    isRelative ||
    host.endsWith('.cloudfront.net') ||
    host.endsWith('.fal.media') ||
    (!!dom && host === dom);
  const ourArtifact =
    raw.startsWith('/api/artifact') ||
    (!!dom && host === dom) ||
    /(^|\/)artifacts\//.test(pathname) ||
    /(^|\/)projects\/p[0-9a-f]{32}\/sessions\/[^/]+\/output\//.test(pathname);

  // A document on another site (a docs page `….html`, a public PDF) is a WEB PAGE: a plain link the browser opens.
  // Only our own files get the document card, whose preview loads them through the authenticated proxy -- for an
  // external page that card could neither preview nor open (web-search / AWS-docs citations, staging 2026-10-20).
  if (kind === 'document' && !trustedHost && !ourArtifact) kind = 'link';

  return { url: raw, kind, ext, filename: filenameOf(raw), trustedHost, ourArtifact };
}

/**
 * The object key for the `/api/artifact?key=…` proxy: strips the CloudFront domain and keeps the
 * S3 key. Mirrors the regex used by DocumentViewer/DataTable. Returns null when the URL is not one
 * of our artifacts (so callers fall back to using the raw URL directly).
 */
export function deriveArtifactKey(url: string, artifactsDomain?: string): string | null {
  const raw = (url || '').split('#')[0];
  if (raw.startsWith('/api/artifact')) {
    try {
      return new URL(raw, 'http://x').searchParams.get('key');
    } catch {
      return null;
    }
  }
  const m = raw.match(/^https?:\/\/[^/]+\/(.+)$/);
  if (!m) return null;
  const key = m[1].split('?')[0];
  if (/^artifacts\//.test(key)) return key;
  // A projects-session OUTPUT key (asset-lineage Phase 3 (a)); a project ASSET path is not ours here.
  if (/^projects\//.test(key)) {
    let decoded = key;
    try { decoded = decodeURIComponent(key); } catch { /* keep as-is */ }
    return isProjectSessionOutputKey(decoded) ? key : null;
  }
  if (artifactsDomain && raw.toLowerCase().includes(artifactsDomain.toLowerCase())) return key;
  return null;
}

/**
 * Force a real download cross-origin: fetch → blob → a[download]. CloudFront serves
 * `access-control-allow-origin: *`, so this works for our artifacts with the correct filename
 * (the HTML `download` attribute is ignored cross-origin). Falls back to opening the URL in a new
 * tab when the fetch is blocked (a non-CORS host). Whole-file-in-memory, so intended for
 * images/pdf/csv/json — large video should use the player's native download.
 */
export async function downloadFile(url: string, filename?: string): Promise<void> {
  const name = filename || filenameOf(url);
  try {
    const resp = await fetch(url, { mode: 'cors' });
    if (!resp.ok) throw new Error(`fetch failed: ${resp.status}`);
    const blob = await resp.blob();
    const obj = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = obj;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(obj), 4000);
  } catch {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}
