/**
 * Folders a user created that hold nothing yet ("New folder", or "Create … and move" before the move lands).
 * The server lists only folders that hold a file, so these live here, per project, on this device — and EVERY
 * folder picker reads them (the project grid, a chat preview's Move to folder, a Renderer tab's Save to folder,
 * the Moments tab's Save), so a folder made in the project is offered in chat at once.
 */
const key = (projectId: string) => `projects-pending-folders:${projectId}`;
const EVENT = 'projects:pending-folders';

export function readPendingFolders(projectId: string): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(key(projectId)) || '[]');
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

export function writePendingFolders(projectId: string, folders: string[]): void {
  try { localStorage.setItem(key(projectId), JSON.stringify(folders)); } catch { /* storage full or off */ }
  try { window.dispatchEvent(new CustomEvent(EVENT, { detail: { projectId } })); } catch { /* no window */ }
}

/** Calls `fn` when another surface changes a project's pending folders. Returns the unsubscribe. */
export function onPendingFoldersChange(fn: (projectId: string) => void): () => void {
  const h = (e: Event) => fn(String((e as CustomEvent).detail?.projectId ?? ''));
  window.addEventListener(EVENT, h);
  return () => window.removeEventListener(EVENT, h);
}
