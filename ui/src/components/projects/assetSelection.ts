/**
 * The Assets grid's session structure and bulk delete (asset-lineage design §7; R10, R11).
 *
 *   * Session folders: a session's outputs sit in `Sessions/<session title> · <YYYY-MM-DD>` (an M2C job's in a
 *     `Sessions/<…>/<Job label>` subfolder). Rows registered before that (`Generated/…`) are ordinary folders.
 *   * `analysis` and `intermediate` files are hidden unless "Show analysis files" is on, or their `Sessions/…`
 *     folder is the one opened. A row with no role is a `source` (legacy) and always shows.
 *   * Bulk delete runs the per-asset remove for each selected file, at most {@link BULK_DELETE_CONCURRENCY} at a
 *     time, and reports each failure.
 */
import { assetRole, type AssetView, type ProjectsResult } from '../../projects/api';
import { assetFolder } from './AssetCard';

/** Where a chat's outputs are filed: `Chats/<chat title> · <date>` (twin of `ingestion.folders.SESSIONS_ROOT`). */
export const SESSIONS_FOLDER = 'Chats';
/** The same files' earlier roots (`Sessions/` until 2026-10), still read as chat folders until migrated. */
const LEGACY_SESSION_FOLDERS = ['Sessions'];

/** A chat folder, `Chats/…` (or the legacy `Sessions/…`), or a subfolder of one (e.g. an M2C job's). */
export function isSessionFolder(folder: string): boolean {
  return [SESSIONS_FOLDER, ...LEGACY_SESSION_FOLDERS].some((root) => folder === root || folder.startsWith(`${root}/`));
}

/** An `analysis` or `intermediate` file: hidden from the grid by default. */
export function isAnalysisFile(asset: AssetView): boolean {
  const role = assetRole(asset);
  return role === 'analysis' || role === 'intermediate';
}

/**
 * A metadata/analysis DATA file (`kind: 'data'`): JSON/CSV/text a session or workflow wrote (shots.json,
 * transcripts, probe.json, compliance JSON, …). The Type select's `Metadata` option filters to exactly these,
 * surfacing the `role: analysis` data files that the grid hides by default. Distinct from
 * {@link isAnalysisFile} (a role cut, which also catches intermediate image frames).
 */
export function isMetadataFile(asset: AssetView): boolean {
  return asset.kind === 'data';
}

/**
 * Whether the grid shows `asset`. `openFolder` is the `Sessions/…` folder the user opened (the Icons view's
 * folder filter, or an expanded List-view folder header), which shows everything in it.
 */
export function showsInGrid(asset: AssetView, showAnalysis: boolean, openFolder: string | null): boolean {
  if (showAnalysis || !isAnalysisFile(asset)) return true;
  return openFolder !== null && isSessionFolder(openFolder) && assetFolder(asset) === openFolder;
}

// ----------------------------------------------------------------------------------------- bulk delete

export const BULK_DELETE_CONCURRENCY = 6;
/** The confirmation names at most this many files, then `+ N more`. */
export const BULK_DELETE_NAMED = 5;

export function bulkDeleteTitle(n: number): string {
  return `Delete ${n.toLocaleString('en-US')} file${n === 1 ? '' : 's'}`;
}

export const BULK_DELETE_MESSAGE = 'This removes them from the project and from chats they were attached to.';

/** The names the confirmation lists, and how many more there are. */
export function bulkDeleteNames(names: readonly string[]): { named: string[]; more: number } {
  return { named: names.slice(0, BULK_DELETE_NAMED), more: Math.max(0, names.length - BULK_DELETE_NAMED) };
}

export interface BulkDeleteFailure {
  asset: AssetView;
  message: string;
}

/**
 * Remove every asset through `remove` (the per-asset route), at most `concurrency` in flight. `onSettled` runs once
 * per asset as it finishes (for the "Deleting k of N" progress). A `not_found` answer counts as deleted: the asset
 * (or the Project) is gone already, as the single delete treats it. Resolves with the failures, in input order.
 */
export async function deleteInParallel(
  assets: readonly AssetView[],
  remove: (assetId: string) => Promise<ProjectsResult<void>>,
  opts: { concurrency?: number; onSettled?: (asset: AssetView, ok: boolean) => void } = {},
): Promise<BulkDeleteFailure[]> {
  const concurrency = Math.max(1, opts.concurrency ?? BULK_DELETE_CONCURRENCY);
  const failures = new Map<number, BulkDeleteFailure>();
  let next = 0;
  const worker = async () => {
    while (next < assets.length) {
      const i = next++;
      const asset = assets[i];
      let message: string | null;
      try {
        const r = await remove(asset.assetId);
        message = r.ok || r.kind === 'not_found' ? null : r.kind === 'error' ? r.message : 'try again.';
      } catch (err) {
        message = err instanceof Error ? err.message : 'try again.';
      }
      if (message !== null) failures.set(i, { asset, message });
      opts.onSettled?.(asset, message === null);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, assets.length) }, worker));
  return [...failures.entries()].sort(([a], [b]) => a - b).map(([, f]) => f);
}
