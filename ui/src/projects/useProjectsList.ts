/**
 * The Actor's project list (`GET /projects`), shared by the sidebar's project inventory and the
 * Projects_Surface.
 *
 * One module store, not per-component state: the sidebar and the surface are mounted side by side in
 * `projects` mode, and a create, rename, delete, or leave on the surface must show in the sidebar at the
 * same moment. Same store shape as `sessionProjectStore`: an immutable snapshot, a listener set,
 * `useSyncExternalStore`.
 *
 *   * `useProjectsList({ enabled })` — subscribe. The first enabled subscriber triggers a load when
 *     nothing has been read yet; later ones reuse the snapshot.
 *   * `refresh()` — re-read now. Concurrent calls share one request. Call it after every mutation.
 *
 * `projects` is `null` until the first answer. A failed read leaves the last good list (or `[]` when
 * there was none); a `disabled` answer (Projects off here) leaves it `null`.
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { listProjects, type ProjectSummary, type ProjectsResult } from './api';

export type ProjectsListStatus = 'idle' | 'loading' | 'ready' | 'error' | 'disabled';

export interface ProjectsListState {
  projects: readonly ProjectSummary[] | null;
  status: ProjectsListStatus;
}

const IDLE: ProjectsListState = Object.freeze({ projects: null, status: 'idle' });

let state: ProjectsListState = IDLE;
let inflight: Promise<ProjectsResult<ProjectSummary[]>> | null = null;
/** Bumped by the test reset, so an answer from before it cannot land after it. */
let generation = 0;
const listeners = new Set<() => void>();

function set(next: ProjectsListState): void {
  state = Object.freeze(next);
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getState(): ProjectsListState {
  return state;
}

/** Re-read `GET /projects`. Concurrent calls share one request; every subscriber sees the result. */
export function refreshProjectsList(): Promise<ProjectsResult<ProjectSummary[]>> {
  if (inflight) return inflight;
  const gen = generation;
  set({ projects: state.projects, status: 'loading' });
  const req: Promise<ProjectsResult<ProjectSummary[]>> = listProjects()
    .then((r) => {
      if (gen !== generation) return r;
      if (r.ok) set({ projects: r.value, status: 'ready' });
      else if (r.kind === 'disabled') set({ projects: state.projects, status: 'disabled' });
      else set({ projects: state.projects ?? [], status: 'error' });
      return r;
    })
    .catch((e: unknown): ProjectsResult<ProjectSummary[]> => {
      if (gen === generation) set({ projects: state.projects ?? [], status: 'error' });
      const message = e instanceof Error ? e.message : String(e);
      return { ok: false, kind: 'error', status: 0, code: 'network_error', message };
    })
    .finally(() => {
      if (inflight === req) inflight = null;
    });
  inflight = req;
  return req;
}

export interface UseProjectsListOptions {
  /** `false` keeps the hook inert: no request. Default `true`. */
  enabled?: boolean;
}

export interface UseProjectsList extends ProjectsListState {
  refresh: () => Promise<ProjectsResult<ProjectSummary[]>>;
}

export function useProjectsList({ enabled = true }: UseProjectsListOptions = {}): UseProjectsList {
  const snap = useSyncExternalStore(subscribe, getState, getState);
  useEffect(() => {
    if (enabled && getState().status === 'idle') void refreshProjectsList();
  }, [enabled]);
  const refresh = useCallback(() => refreshProjectsList(), []);
  return { ...snap, refresh };
}

/** Test-only reset, following the `__resetXForTests` convention. */
export function __resetProjectsListForTests(): void {
  generation += 1;
  inflight = null;
  state = IDLE;
  for (const l of listeners) l();
}
