/**
 * Folder input for project uploads: what a dropped or picked folder contributes to an upload, and the folder path
 * each file goes into.
 *
 * A file's `folder` is its relative directory inside what the user chose, without the file name: dropping the
 * folder `Footage` that holds `Day 1/a.mp4` gives `Footage/Day 1`; a loose file has none. The client sends that
 * raw path with the file to `POST /projects/:pid/uploads`, and the server normalizes it. {@link validateFolder}
 * applies the server's rules first, so a file whose folder the server would refuse (`400 invalid_folder`) is shown
 * as rejected with the reason instead of failing its whole request.
 *
 * Where the folder rides: on the `File` object itself, through a WeakMap ({@link setFolderOf} / {@link folderOf}).
 * Every existing step between the drop and the upload queue (duplicate check, duplicate dialog, the chat's
 * attachment path) passes the same `File` objects along, so the folder reaches `startBatch` without changing any
 * of their signatures. A file from `<input webkitdirectory>` carries it natively in `webkitRelativePath`.
 */

const folders = new WeakMap<File, string>();

/** Attach a raw relative folder path to a file (no-op for an empty path). */
export function setFolderOf(file: File, folder: string | undefined): File {
  if (folder) folders.set(file, folder);
  return file;
}

/** A file's raw relative folder path: set by a folder drop, else from `webkitRelativePath`, else undefined. */
export function folderOf(file: File): string | undefined {
  const set = folders.get(file);
  if (set) return set;
  const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
  if (typeof rel === 'string' && rel.includes('/')) return rel.slice(0, rel.lastIndexOf('/')) || undefined;
  return undefined;
}

// ------------------------------------------------------------------------------------------ hidden files

const SYSTEM_FILES = new Set(['thumbs.db', 'desktop.ini']);

/** Hidden or OS bookkeeping files a folder upload skips: dot-files, `Thumbs.db`, `desktop.ini`. */
export function isHiddenOrSystem(name: string): boolean {
  return name.startsWith('.') || SYSTEM_FILES.has(name.toLowerCase());
}

// ------------------------------------------------------------------------------------------ validation

export const FOLDER_MAX_DEPTH = 8;
export const FOLDER_SEGMENT_MAX = 64;
export const FOLDER_PATH_MAX = 255;
const FORBIDDEN = /[[\]@#]/u;
const CONTROL = /\p{Cc}/u;

export type FolderCheck = { ok: true; folder: string } | { ok: false; reason: string };

/**
 * The server's folder rules: segments split on `/` and trimmed; none empty, `.` or `..`; each 1 to 64 characters
 * without `[ ] @ #` or control characters; at most 8 deep and 255 characters in all. `folder` is the normalized
 * path (what the server will store); the client still sends the raw one.
 */
export function validateFolder(raw: string): FolderCheck {
  const segments = raw.split('/').map((s) => s.trim());
  if (segments.length > FOLDER_MAX_DEPTH) {
    return { ok: false, reason: `Folders can be at most ${FOLDER_MAX_DEPTH} levels deep.` };
  }
  for (const s of segments) {
    if (s === '') return { ok: false, reason: 'A folder name is empty.' };
    if (s === '.' || s === '..') return { ok: false, reason: `"${s}" is not a folder name.` };
    if ([...s].length > FOLDER_SEGMENT_MAX) {
      return { ok: false, reason: `A folder name is longer than ${FOLDER_SEGMENT_MAX} characters.` };
    }
    if (FORBIDDEN.test(s)) return { ok: false, reason: `Folder "${s}" contains [ ] @ or #.` };
    if (CONTROL.test(s)) return { ok: false, reason: 'A folder name contains a control character.' };
  }
  const folder = segments.join('/');
  if ([...folder].length > FOLDER_PATH_MAX) {
    return { ok: false, reason: `The folder path is longer than ${FOLDER_PATH_MAX} characters.` };
  }
  return { ok: true, folder };
}

// ------------------------------------------------------------------------------------------ directory picker

/**
 * The files of an `<input type="file" webkitdirectory>` selection, hidden and system files (and anything inside a
 * hidden folder) left out. Each keeps its `webkitRelativePath`, which {@link folderOf} reads.
 */
export function filesFromDirectoryInput(list: ArrayLike<File>): File[] {
  return Array.from(list).filter((f) => {
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
    return !rel.split('/').some((seg) => isHiddenOrSystem(seg));
  });
}

// ------------------------------------------------------------------------------------------ drop walker

/** The parts of the File and Directory Entries API the walker uses (fakeable in tests). */
export interface FsEntry {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
}
export interface FsFileEntry extends FsEntry {
  file(success: (f: File) => void, error?: (e: unknown) => void): void;
}
export interface FsDirectoryEntry extends FsEntry {
  createReader(): { readEntries(success: (entries: FsEntry[]) => void, error?: (e: unknown) => void): void };
}

function readFile(entry: FsFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    try {
      entry.file((f) => resolve(f), () => resolve(null));
    } catch {
      resolve(null);
    }
  });
}

function readBatch(reader: ReturnType<FsDirectoryEntry['createReader']>): Promise<FsEntry[]> {
  return new Promise((resolve) => {
    try {
      reader.readEntries((batch) => resolve(batch), () => resolve([]));
    } catch {
      resolve([]);
    }
  });
}

/** Every entry of a directory. `readEntries` answers in batches (Chrome: 100), so call it until one is empty. */
async function readAll(dir: FsDirectoryEntry): Promise<FsEntry[]> {
  const reader = dir.createReader();
  const out: FsEntry[] = [];
  for (;;) {
    const batch = await readBatch(reader);
    if (batch.length === 0) return out;
    out.push(...batch);
  }
}

/**
 * Walk dropped entries recursively into files, each tagged with its relative directory ({@link setFolderOf}):
 * a top-level directory `Footage` yields `Footage/...` paths, a top-level file none. Hidden and system files, and
 * hidden directories, are skipped. An entry that cannot be read is skipped too.
 */
export async function walkEntries(entries: readonly FsEntry[]): Promise<File[]> {
  const out: File[] = [];
  const visit = async (entry: FsEntry, dir: string): Promise<void> => {
    if (isHiddenOrSystem(entry.name)) return;
    if (entry.isFile) {
      const f = await readFile(entry as FsFileEntry);
      if (f) out.push(setFolderOf(f, dir || undefined));
      return;
    }
    if (entry.isDirectory) {
      const path = dir ? `${dir}/${entry.name}` : entry.name;
      for (const child of await readAll(entry as FsDirectoryEntry)) await visit(child, path);
    }
  };
  for (const e of entries) await visit(e, '');
  return out;
}

/** Whether a drag carries files (or folders). */
export function dragHasFiles(dt: DataTransfer | null | undefined): boolean {
  if (!dt) return false;
  if (Array.from(dt.types ?? []).includes('Files')) return true;
  return (dt.files?.length ?? 0) > 0;
}

/**
 * The files of a drop, folders walked. MUST be called synchronously inside the `drop` handler: the
 * DataTransferItemList is only readable during the event, so the entries are taken before the first `await`.
 * Falls back to `dataTransfer.files` when the browser offers no entries. A drop shows no browser confirmation.
 */
export function readDroppedFiles(dt: DataTransfer): Promise<File[]> {
  const items = dt.items ? Array.from(dt.items) : [];
  const entries: FsEntry[] = [];
  const loose: File[] = [];
  let usedItems = false;
  for (const item of items) {
    if (item.kind !== 'file') continue;
    usedItems = true;
    const entry = typeof item.webkitGetAsEntry === 'function' ? (item.webkitGetAsEntry() as FsEntry | null) : null;
    if (entry) entries.push(entry);
    else {
      const f = item.getAsFile();
      if (f) loose.push(f);
    }
  }
  if (!usedItems) return Promise.resolve(Array.from(dt.files ?? []));
  return walkEntries(entries).then((walked) => [...walked, ...loose]);
}
