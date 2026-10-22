/** Dragging asset cards onto folders. The payload type keeps an asset drag apart from a file drag (upload). */

/** The drag payload type of asset cards: a JSON array of asset ids. */
export const ASSET_DRAG_TYPE = 'application/x-harness-asset-ids';

/** `true` while the drag carries asset cards (during dragover only the TYPES are readable, not the data). */
export function isAssetDrag(dt: DataTransfer | null): boolean {
  return !!dt && Array.from(dt.types ?? []).includes(ASSET_DRAG_TYPE);
}

/** The asset ids an asset drag carries, or `null` for any other drag (files from the desktop, text, …). */
export function draggedAssetIds(dt: DataTransfer | null): string[] | null {
  if (!dt || !isAssetDrag(dt)) return null;
  try {
    const ids = JSON.parse(dt.getData(ASSET_DRAG_TYPE) || '[]');
    return Array.isArray(ids) ? ids.filter((i): i is string => typeof i === 'string') : [];
  } catch {
    return [];
  }
}
