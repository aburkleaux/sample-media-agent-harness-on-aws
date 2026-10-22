/**
 * A picture dropped or pasted into the Moments tab's "Search by image" (design 2026-10-18 §2): uploaded through
 * the project upload queue as a "this chat only" asset (`visibility: 'session'`, asset-lineage D2 — hidden from
 * the project, deleted with the chat), the same path a chat composer's "this chat only" file takes. Resolves
 * with its asset id once registered; its search rendition may still be in the making (the search retries).
 */
import * as queue from './uploadQueue';

export const QUERY_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];

export type QueryImageUpload = { ok: true; assetId: string; name: string } | { ok: false; message: string };

export function isQueryImageFile(file: File | null | undefined): file is File {
  return !!file && QUERY_IMAGE_TYPES.includes(file.type);
}

export async function uploadQueryImage(projectId: string, sessionId: string, file: File): Promise<QueryImageUpload> {
  if (!isQueryImageFile(file)) return { ok: false, message: 'Use a JPEG, PNG, WebP or GIF image.' };
  if (!sessionId) return { ok: false, message: 'Open a chat first: the picture is kept with that chat.' };
  const started = await queue.startBatch(projectId, [file], { visibility: 'session', sessionId });
  if (!started.ok) return { ok: false, message: started.message };
  const id = started.fileIds[0];
  return new Promise<QueryImageUpload>((resolve) => {
    const check = (): boolean => {
      const e = queue.getEntry(id);
      if (!e || !queue.isTerminal(e.status)) return false;
      const assetId = e.asset?.assetId ?? e.assetId;
      resolve(e.status === 'ready' && assetId ? { ok: true, assetId, name: file.name }
        : { ok: false, message: e.error || "Couldn't upload the picture." });
      return true;
    };
    if (check()) return;
    const stop = queue.subscribe(() => { if (check()) stop(); });
  });
}
