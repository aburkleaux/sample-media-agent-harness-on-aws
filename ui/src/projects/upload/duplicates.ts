// Duplicate detection before an upload: "this file is already in the project".
//
// A file matches an existing (not removed) asset when its derived Display_Name (ignoring the ` (n)` suffix the
// registry adds to a second copy) and its size in bytes are both equal. Name + size catches the common case (the
// same file uploaded again) without hashing gigabytes in the browser. The server does the matching
// (`POST /projects/:pid/assets/duplicates`), so a folder of a thousand files costs one request rather than one name
// search per file. The caller then asks once per batch: "Use existing" (skip the upload; from chat, attach the
// existing asset) or "Keep both".
//
// `displayBase` / `withoutSuffix` stay as the TS twin of `agent/src/ingestion/names.py::derive_display_name`.

import {
  findDuplicatesBatch,
  MAX_DUPLICATE_CHECK_FILES,
  type AssetView,
  type DuplicateMatch,
  type ProjectsResult,
} from '../api';

const FORBIDDEN = /[[\]@#]/gu;
const OTHER = /\p{C}/gu; // control, format, surrogate, private-use, unassigned
const SUFFIX = / \((\d+)\)$/u;
const DISPLAY_NAME_MAX = 255;

/** The base Display_Name the registry derives from a file name (before any ` (n)`). */
export function displayBase(fileName: string): string {
  const s = fileName.replace(FORBIDDEN, '').replace(OTHER, '').replace(/\s+/gu, ' ').trim();
  return [...s].slice(0, DISPLAY_NAME_MAX).join('').trimEnd();
}

/** An existing Display_Name without the registry's ` (n)` suffix. */
export function withoutSuffix(displayName: string): string {
  return displayName.replace(SUFFIX, '');
}

export interface Duplicate {
  file: File;
  existing: AssetView;
}

type Batch = (pid: string, files: { name: string; size: number }[]) => Promise<ProjectsResult<{ duplicates: DuplicateMatch[] }>>;

/**
 * Which of `files` are already in the Project: ONE `POST …/assets/duplicates` per 1000 files (the server derives the
 * name, matches name + size, and prefers the unsuffixed original). A request that fails is treated as "no
 * duplicate" for its files: the check must never block an upload.
 */
export async function findDuplicates(projectId: string, files: File[], batch: Batch = findDuplicatesBatch): Promise<Duplicate[]> {
  const chunks: File[][] = [];
  for (let i = 0; i < files.length; i += MAX_DUPLICATE_CHECK_FILES) chunks.push(files.slice(i, i + MAX_DUPLICATE_CHECK_FILES));
  const results = await Promise.all(chunks.map(async (chunk) => {
    let r: Awaited<ReturnType<Batch>> | null = null;
    try { r = await batch(projectId, chunk.map((f) => ({ name: f.name, size: f.size }))); } catch { r = null; }
    if (!r || !r.ok) return [] as Duplicate[];
    const found: Duplicate[] = [];
    const seen = new Set<number>();
    for (const m of r.value.duplicates) {
      const file = Number.isInteger(m?.index) ? chunk[m.index] : undefined;
      if (!file || seen.has(m.index) || !m.asset || m.asset.status === 'removed') continue;
      seen.add(m.index);
      found.push({ file, existing: m.asset });
    }
    return found;
  }));
  // In the order of `files`, whatever order the server answered in.
  const order = new Map(files.map((f, i) => [f, i]));
  return results.flat().sort((a, b) => (order.get(a.file) ?? 0) - (order.get(b.file) ?? 0));
}
