/**
 * Folder paths in the SPA: the tree the Assets surface draws and the light clean-up a typed path gets before it is
 * sent. The server's `normalizeFolder` (ui/server/projects/folders.ts, twin of `ingestion/folders.py`) is the
 * authority — it NFC-normalizes and refuses dot segments, control and `[ ] @ #` characters, and over-long paths —
 * so this only does what makes a typed path comparable to the folders on screen.
 */
import type { FolderCount } from './api';

/** `" Boards // Board 07 "` → `"Boards/Board 07"`; `""` is the project root. */
export function cleanFolder(raw: string): string {
  return raw.split('/').map((s) => s.trim()).filter(Boolean).join('/');
}

/** The last segment: what a tree row shows. */
export function folderName(path: string): string {
  const i = path.lastIndexOf('/');
  return i < 0 ? path : path.slice(i + 1);
}

/** The parent path (`""` for a top-level folder). */
export function parentFolder(path: string): string {
  const i = path.lastIndexOf('/');
  return i < 0 ? '' : path.slice(0, i);
}

/** `true` when `path` is `folder` or below it. */
export function isWithin(path: string, folder: string): boolean {
  return path === folder || path.startsWith(`${folder}/`);
}

export interface FolderNode {
  path: string;
  name: string;
  /** Files filed directly here. */
  count: number;
  /** Files here and in every subfolder. */
  total: number;
  children: FolderNode[];
}

const byName = (a: FolderNode, b: FolderNode) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });

/**
 * The folder tree from the server's flat list plus folders the user created that hold nothing yet. Every
 * ancestor of a listed path becomes a node (a path `Boards/Board 07` implies `Boards`), so the tree never has a
 * gap. Siblings sort by name, numerically (`Board 2` before `Board 10`).
 */
export function buildFolderTree(folders: readonly FolderCount[], extra: readonly string[] = []): FolderNode[] {
  const nodes = new Map<string, FolderNode>();
  const roots: FolderNode[] = [];
  const ensure = (path: string): FolderNode => {
    const have = nodes.get(path);
    if (have) return have;
    const node: FolderNode = { path, name: folderName(path), count: 0, total: 0, children: [] };
    nodes.set(path, node);
    const parent = parentFolder(path);
    if (parent) ensure(parent).children.push(node);
    else roots.push(node);
    return node;
  };
  for (const f of folders) {
    const p = cleanFolder(f.folder);
    if (p) ensure(p).count += f.count;
  }
  for (const e of extra) {
    const p = cleanFolder(e);
    if (p) ensure(p);
  }
  const sum = (n: FolderNode): number => {
    n.children.sort(byName);
    n.total = n.count + n.children.reduce((acc, c) => acc + sum(c), 0);
    return n.total;
  };
  roots.sort(byName).forEach(sum);
  return roots;
}

/** Every folder path in the tree, depth-first in display order (what the folder picker lists). */
export function flattenFolders(tree: readonly FolderNode[]): string[] {
  const out: string[] = [];
  const walk = (ns: readonly FolderNode[]) => ns.forEach((n) => { out.push(n.path); walk(n.children); });
  walk(tree);
  return out;
}

const FORBIDDEN = /[[\]@#]/u;
const CONTROL = /\p{C}/u;
const cps = (s: string) => [...s].length;

/**
 * Why `name` cannot be a folder under `parent` (`''` = the top level), in words a user can act on — or `null`
 * when it can. The same rules as the server's `normalizeFolder`, checked before anything is sent, so a typo
 * never becomes a rejected request.
 */
export function folderNameProblem(name: string, parent = ''): string | null {
  const seg = name.trim();
  if (!seg) return 'Type a name.';
  if (seg.includes('/')) return 'A name can’t contain “/”.';
  if (seg === '.' || seg === '..') return 'Pick another name.';
  if (FORBIDDEN.test(seg)) return 'A name can’t contain [ ] @ or #.';
  if (CONTROL.test(seg)) return 'A name can’t contain control characters.';
  if (cps(seg) > 64) return 'Keep names to 64 characters.';
  const path = parent ? `${parent}/${seg}` : seg;
  if (path.split('/').length > 8) return 'Folders can be at most 8 levels deep.';
  if (cps(path) > 255) return 'That folder path is too long.';
  return null;
}

/** A server folder error code (`forbidden_character`, …) in words; `null` when `message` names none. */
export function folderErrorText(message: string): string | null {
  const codes: Record<string, string> = {
    forbidden_character: 'A folder name can’t contain [ ] @ or #.',
    control_character: 'A folder name can’t contain control characters.',
    dot_segment: 'A folder can’t be named “.” or “..”.',
    segment_too_long: 'Keep folder names to 64 characters.',
    too_deep: 'Folders can be at most 8 levels deep.',
    too_long: 'That folder path is too long.',
  };
  const hit = Object.keys(codes).find((c) => message.includes(c));
  return hit ? codes[hit] : null;
}
