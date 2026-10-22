/**
 * Column sort for the Assets List view (header row: Name · Type · Size). Pure: the section sorts what it
 * has LOADED (the list pages newest-first, so with "Load more" pending a sort orders the loaded files only),
 * and folder groups keep their own order, so a sort applies within every folder.
 *
 * `null` is the default order (newest first, as the server lists them). Clicking a column sorts it
 * ascending, again descending, a third time back to the default.
 */
import type { AssetView } from '../../projects/api';
import { KIND_LABEL } from './assetDisplay';

export type AssetSortKey = 'name' | 'type' | 'size';
export interface AssetSort { key: AssetSortKey; dir: 'asc' | 'desc' }

export const ASSETS_SORT_KEY = 'projects-assets-sort';

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const byName = (a: AssetView, b: AssetView) => collator.compare(a.displayName ?? '', b.displayName ?? '');
const typeOf = (a: AssetView) => KIND_LABEL[a.kind as keyof typeof KIND_LABEL] ?? String(a.kind ?? '');

/** `assets` in `sort` order (a new array; ties broken by name, so the order is stable and readable). */
export function sortAssets(assets: readonly AssetView[], sort: AssetSort | null): AssetView[] {
  if (!sort) return [...assets];
  const sign = sort.dir === 'asc' ? 1 : -1;
  const cmp = (a: AssetView, b: AssetView): number => {
    if (sort.key === 'size') {
      const sa = typeof a.sourceSize === 'number' ? a.sourceSize : null;
      const sb = typeof b.sourceSize === 'number' ? b.sourceSize : null;
      // An unknown size sorts LAST in both directions -- it is not "smallest".
      if (sa === null || sb === null) return sa === sb ? byName(a, b) : sa === null ? 1 : -1;
      return sa === sb ? byName(a, b) : sign * (sa - sb);
    }
    if (sort.key === 'type') return sign * collator.compare(typeOf(a), typeOf(b)) || byName(a, b);
    return sign * byName(a, b);
  };
  return [...assets].sort(cmp);
}

/** The next sort when `key`'s header is clicked: asc → desc → default. */
export function nextSort(cur: AssetSort | null, key: AssetSortKey): AssetSort | null {
  if (!cur || cur.key !== key) return { key, dir: 'asc' };
  return cur.dir === 'asc' ? { key, dir: 'desc' } : null;
}

export function readStoredSort(): AssetSort | null {
  try {
    const v = JSON.parse(localStorage.getItem(ASSETS_SORT_KEY) ?? 'null');
    if (v && (v.key === 'name' || v.key === 'type' || v.key === 'size') && (v.dir === 'asc' || v.dir === 'desc')) return v;
  } catch { /* unreadable: default order */ }
  return null;
}

export function storeSort(sort: AssetSort | null): void {
  try {
    if (sort) localStorage.setItem(ASSETS_SORT_KEY, JSON.stringify(sort));
    else localStorage.removeItem(ASSETS_SORT_KEY);
  } catch { /* storage full / disabled: in-memory only */ }
}
