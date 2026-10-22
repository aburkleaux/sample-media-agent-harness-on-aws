/**
 * sessionLayout — where a session's objects live (asset-lineage Phase 3 (a), design §9.1).
 *
 * Two session roots:
 *   - `artifacts/<sid>/`               legacy outputs + ALL session state (render manifests,
 *                                      transcript, input/), whatever the layout;
 *   - `projects/<pid>/sessions/<sid>/` new outputs (`output/` only) in an env with
 *                                      `SESSION_OUTPUT_LAYOUT=project`.
 * Readers accept both, always. Only the `projects/<pid>/sessions/<sid>/output/…` sub-tree is ever an
 * ARTIFACT key; project ASSET bytes (`projects/<pid>/<assetId>/…`) stay asset-id-gated.
 *
 * SHARED between the proxy and the SPA (like renderViewState). Pure, no runtime deps. The Python twin
 * is agent/src/projects/storage_layout.py; both are pinned by
 * agent/tests/fixtures/session_layout_cases.json.
 */

export interface SessionKey {
  layout: 'artifacts' | 'project';
  sid: string;
  pid: string | null;
  rel: string;
}

/** Namespaces directly under artifacts/ that are not chat sessions. */
export const SHARED_SESSION_NAMESPACES: ReadonlySet<string> = new Set(['external', 'durable', 'imports', 'uploads', 'm2c']);

const SID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/;
const PID_RE = /^p[0-9a-f]{32}$/;

/**
 * A projects-session OUTPUT key at the start of a path (no leading slash). Group 1 = pid, 2 = sid.
 * For embedding in URL parsers; validate the match with `isProjectSessionOutputKey`.
 */
export const SESSION_OUTPUT_KEY_RE = /^projects\/(p[0-9a-f]{32})\/sessions\/([A-Za-z0-9][A-Za-z0-9._-]{7,127})\/output\/[^?#]+/;

export function isSessionId(sid: unknown): sid is string {
  return typeof sid === 'string' && SID_RE.test(sid) && !SHARED_SESSION_NAMESPACES.has(sid);
}

function safe(key: unknown): key is string {
  return typeof key === 'string' && !!key && !key.includes('..') && !key.includes('\\') && !key.startsWith('/');
}

/** The session a key belongs to under either root, else null (shared namespaces, flat keys, asset keys, malformed). */
export function parseSessionKey(key: unknown): SessionKey | null {
  if (!safe(key)) return null;
  const parts = key.split('/');
  if (parts[0] === 'artifacts' && parts.length >= 3) {
    const sid = parts[1];
    const rel = parts.slice(2).join('/');
    return isSessionId(sid) && rel ? { layout: 'artifacts', sid, pid: null, rel } : null;
  }
  if (parts[0] === 'projects' && parts.length >= 5 && parts[2] === 'sessions') {
    const pid = parts[1];
    const sid = parts[3];
    const rel = parts.slice(4).join('/');
    if (PID_RE.test(pid) && isSessionId(sid) && rel) return { layout: 'project', sid, pid, rel };
  }
  return null;
}

/** True for a FILE under `projects/<pid>/sessions/<sid>/output/`. */
export function isProjectSessionOutputKey(key: unknown): boolean {
  const sk = parseSessionKey(key);
  return !!sk && sk.layout === 'project' && sk.rel.startsWith('output/') && sk.rel.length > 'output/'.length && !sk.rel.endsWith('/');
}

export function projectSessionOutputPrefix(pid: string, sid: string): string {
  return `projects/${pid}/sessions/${sid}/output/`;
}
