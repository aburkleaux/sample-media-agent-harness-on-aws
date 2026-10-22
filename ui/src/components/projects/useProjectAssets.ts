/**
 * The Selected_Project's asset list for the Projects_Surface: newest-first pages of `GET /projects/:pid/assets`
 * with `Load more`, an optimistic hide for a delete, and a 10 s poll while any asset is still on its way to a
 * final status. The poll stops when every asset is final and when the surface unmounts.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { listAssets, type AssetIngestionStatus, type AssetView } from '../../projects/api';

/** While any listed asset is in one of these, the list refreshes every {@link ASSET_POLL_MS}. */
const NON_TERMINAL: ReadonlySet<AssetIngestionStatus> = new Set(['uploading', 'queued', 'analyzing', 'indexing']);
export const ASSET_POLL_MS = 10_000;

export function isNonTerminalAsset(status: AssetIngestionStatus): boolean {
  return NON_TERMINAL.has(status);
}

export interface ProjectAssetsState {
  assets: AssetView[];
  /** Absent once the last page is loaded. */
  cursor?: string;
  loading: boolean;
  /** The first page has answered at least once. */
  loaded: boolean;
  error: string | null;
  /** The Project's search collection state from the last first-page read (`rebuilding`, `rebuild_failed`, …). */
  collectionState?: string;
}

export interface ProjectAssetsApi extends ProjectAssetsState {
  reload(): Promise<void>;
  loadMore(): Promise<void>;
  /** Refetch the first page and merge it into what is loaded. */
  refresh(): Promise<void>;
  /** Hide one asset now (an optimistic delete). */
  hide(assetId: string): void;
  /** Undo {@link hide} (the delete failed); the next refresh shows it again. */
  unhide(assetId: string): void;
}

const visible = (list: AssetView[]) => list.filter((a) => a.status !== 'removed');

export function useProjectAssets(projectId: string, onNotFound: () => void): ProjectAssetsApi {
  const [state, setState] = useState<ProjectAssetsState>({ assets: [], loading: true, loaded: false, error: null });
  const pagesLoaded = useRef(0);
  const hidden = useRef<Set<string>>(new Set());
  const live = useRef(true);
  const pidRef = useRef(projectId);
  pidRef.current = projectId;
  const onNotFoundRef = useRef(onNotFound);
  onNotFoundRef.current = onNotFound;

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const filter = useCallback((list: AssetView[]) => visible(list).filter((a) => !hidden.current.has(a.assetId)), []);

  const reload = useCallback(async () => {
    const pid = projectId;
    setState((s) => ({ ...s, loading: true, error: null }));
    const r = await listAssets(pid);
    if (!live.current || pidRef.current !== pid) return;
    if (r.ok) {
      pagesLoaded.current = 1;
      setState({ assets: filter(r.value.assets), cursor: r.value.cursor, loading: false, loaded: true, error: null,
        collectionState: r.value.collectionState });
      return;
    }
    if (r.kind === 'not_found') onNotFoundRef.current();
    setState({
      assets: [],
      loading: false,
      loaded: true,
      error: r.kind === 'error' ? `Couldn't load assets: ${r.message}` : null,
    });
  }, [projectId, filter]);

  const loadMore = useCallback(async () => {
    const pid = projectId;
    const cursor = state.cursor;
    if (!cursor) return;
    setState((s) => ({ ...s, loading: true }));
    const r = await listAssets(pid, cursor);
    if (!live.current || pidRef.current !== pid) return;
    if (r.ok) {
      pagesLoaded.current += 1;
      setState((s) => {
        const seen = new Set(s.assets.map((a) => a.assetId));
        const more = filter(r.value.assets).filter((a) => !seen.has(a.assetId));
        return { ...s, assets: [...s.assets, ...more], cursor: r.value.cursor, loading: false, error: null };
      });
      return;
    }
    if (r.kind === 'not_found') onNotFoundRef.current();
    setState((s) => ({ ...s, loading: false, error: r.kind === 'error' ? `Couldn't load more assets: ${r.message}` : s.error }));
  }, [projectId, state.cursor, filter]);

  const refresh = useCallback(async () => {
    const pid = projectId;
    const r = await listAssets(pid);
    if (!live.current || pidRef.current !== pid) return;
    if (!r.ok) {
      if (r.kind === 'not_found') onNotFoundRef.current();
      return;
    }
    // A delete has landed once the server stops listing the asset; forget the optimistic hide then.
    const listed = new Set(r.value.assets.map((a) => a.assetId));
    for (const id of [...hidden.current]) if (!listed.has(id)) hidden.current.delete(id);
    const page = filter(r.value.assets);
    const collectionState = r.value.collectionState;
    setState((s) => {
      if (pagesLoaded.current <= 1) {
        return { ...s, assets: page, cursor: r.value.cursor, loading: false, loaded: true, error: null, collectionState };
      }
      // Later pages are loaded: update and prepend from the fresh first page, keep the rest.
      const fresh = new Map(page.map((a) => [a.assetId, a]));
      const removedNow = new Set(
        r.value.assets.filter((a) => a.status === 'removed').map((a) => a.assetId),
      );
      const kept = s.assets
        .filter((a) => !removedNow.has(a.assetId) && !hidden.current.has(a.assetId))
        .map((a) => fresh.get(a.assetId) ?? a);
      const keptIds = new Set(kept.map((a) => a.assetId));
      const added = page.filter((a) => !keptIds.has(a.assetId));
      return { ...s, assets: [...added, ...kept], loading: false, error: null, collectionState };
    });
  }, [projectId, filter]);

  const hide = useCallback((assetId: string) => {
    hidden.current.add(assetId);
    setState((s) => ({ ...s, assets: s.assets.filter((a) => a.assetId !== assetId) }));
  }, []);

  const unhide = useCallback((assetId: string) => {
    hidden.current.delete(assetId);
  }, []);

  // A different Project starts over.
  useEffect(() => {
    hidden.current.clear();
    pagesLoaded.current = 0;
    void reload();
  }, [reload]);

  // Poll while anything is still on its way to a final status, or the search collection is being rebuilt.
  const polling = state.assets.some((a) => isNonTerminalAsset(a.status)) || state.collectionState === 'rebuilding';
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(() => {
      void refresh();
    }, ASSET_POLL_MS);
    return () => clearInterval(t);
  }, [polling, refresh]);

  return { ...state, reload, loadMore, refresh, hide, unhide };
}
