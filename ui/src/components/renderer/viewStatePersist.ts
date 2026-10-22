/**
 * Persist a render view's timeline state (R8.2) to its manifest: debounced 1 s per manifest key,
 * one PATCH /sessions/:id/renders?key= carrying the full state.
 *
 * The latest local state per key is also remembered in memory, so a tab that unmounts (the user
 * switched tabs) and remounts within the debounce window — or before a list re-fetch — restores what
 * the user last set, not the manifest's older copy.
 */
import { patchSessionRenderView } from '../../lab-api';
import { normalizeViewState, viewStatePatch, type RenderViewState } from '../../shared/renderViewState';

export const VIEW_STATE_DEBOUNCE_MS = 1000;

const latest = new Map<string, RenderViewState>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();

type Patch = (sessionId: string, key: string, viewState: Record<string, unknown>) => Promise<void>;

/** The state a view should restore: this client's latest for `key`, else the manifest's own. */
export function restoredViewState(key: string, manifestViewState: unknown): RenderViewState | undefined {
  return latest.get(key) ?? normalizeViewState(manifestViewState);
}

/** Record `viewState` for `key` and PATCH it once the user has stopped changing it for 1 s. */
export function saveViewState(
  sessionId: string,
  key: string,
  viewState: RenderViewState,
  patch: Patch = patchSessionRenderView,
): void {
  latest.set(key, viewState);
  const pending = timers.get(key);
  if (pending) clearTimeout(pending);
  timers.set(key, setTimeout(() => {
    timers.delete(key);
    const vs = latest.get(key);
    if (!vs) return;
    patch(sessionId, key, viewStatePatch(vs)).catch((e) => {
      // eslint-disable-next-line no-console
      console.warn(`[render-view] could not save the view state of ${key}:`, e);
    });
  }, VIEW_STATE_DEBOUNCE_MS));
}

/** Test seam: forget remembered states and pending PATCHes. */
export function resetViewStatePersist(): void {
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
  latest.clear();
}
