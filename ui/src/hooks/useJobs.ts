/**
 * useJobs — React hook for the async-job-tracking sidebar/inline UI surfaces.
 *
 * Returns the visible list of `Job` records for the authenticated Actor
 * along with imperative knobs the SSE subscription hook (`useJobsSubscription`,
 * task 12.2) uses to push wire-format `JobUpdateEvent`s into the same store.
 *
 * Architecture
 * ------------
 * The hook is a thin React binding over a module-level store accessed via
 * `useSyncExternalStore`. That layout is what the design's
 * "Subscribe to the same `useJobs` store" wording requires (design.md
 * "UI Components" section): every consumer in the tree — the Sidebar's
 * Jobs section (task 13.1) and the inline `JobReference` chip (task 13.2) —
 * needs to read the same record set, and the SSE subscription needs a way
 * to dispatch updates from outside the React tree.
 *
 * The store is intentionally kept here (not in a separate `services/`
 * module) so the hook and its imperative API land in one place. It exposes:
 *
 *   - `getSnapshot()` / `subscribe()` for `useSyncExternalStore`.
 *   - `refresh(ids?)` — full reseed via `GET /jobs` when `ids` is omitted,
 *     or per-id resync via `GET /jobs?ids=...` for the reconnect-resync
 *     flow (R8.3 / design "Reconnect resync").
 *   - `applyEvent(event)` — upsert from an SSE frame; `useJobsSubscription`
 *     calls this for every `job.update` / `job.terminal` it receives.
 *   - `setConnectionState(state)` — the SSE hook owns this transition;
 *     `useJobs` only reads it (the design left the separation open and we
 *     pick the "setter exposed by useJobs" branch from task 12.1's note).
 *
 * Visible-list filter (R6.1)
 * --------------------------
 * The sidebar shows every active job plus terminal jobs whose `terminalAt`
 * is within `JOBS_RECENCY_WINDOW_HOURS` (default 24, design's
 * "configurable in client config"). The store keeps every record the proxy
 * has ever returned during this session — `useJobs` is responsible for
 * narrowing that down to the visible window before returning. Terminal
 * jobs that age out of the window are still in the store (they may be
 * needed if the user scrolls a long-lived inline reference back into view),
 * they just don't render in the sidebar list.
 *
 * Auth
 * ----
 * `authFetch` attaches the JWT bearer token; `/jobs` is in its
 * `PROTECTED_PREFIXES` list so this works automatically. The proxy enforces
 * actor-scoping server-side (R11.1, R11.2) — there is no client-side
 * filtering by `actorId` here because every record returned by `/jobs` is
 * already scoped to the requesting Actor.
 */

import { useEffect, useMemo, useSyncExternalStore } from "react";
import { authFetch } from "../auth/authFetch";
import type { Job, JobStatus, JobUpdateEvent } from "../types/jobs";
import { notificationsStore, getCompletionNotificationsEnabled } from "../services/notifications";

/**
 * How long a terminal job stays visible in the sidebar after its
 * `terminalAt` timestamp. The design's recommended default is 24 hours;
 * exported as a const so consumers and tests can reference the same
 * window without drift.
 */
export const JOBS_RECENCY_WINDOW_HOURS = 24;

/**
 * The two `JobStatus` values that count as "active" for the sidebar
 * filter. Mirrors `agent/src/jobs/models.ACTIVE_STATUSES` exactly so the
 * client and the polling worker agree on the active set.
 */
const ACTIVE_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "PENDING",
  "RUNNING",
]);

/**
 * SSE connection state, observed by `useJobs` and mutated by
 * `useJobsSubscription` (task 12.2). Kept narrow so the sidebar can
 * surface a small connection indicator without inventing extra states.
 */
export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "error";

/**
 * Internal shape of the module-level store. Mutations always replace the
 * full state object so `useSyncExternalStore` sees a fresh reference.
 */
interface StoreState {
  /**
   * Every job we know about, keyed by `jobId`. The value side is replaced
   * (not mutated) on every update so consumers using `Object.is`-comparison
   * see a different reference when anything changes.
   */
  readonly jobsById: ReadonlyMap<string, Job>;
  /**
   * Most recent error from a `refresh()` call. Cleared on the next
   * successful refresh. SSE-side errors are reported through
   * `connectionState === "error"` and the subscription hook's own error
   * channel.
   */
  readonly lastError: Error | null;
  /** SSE connection state (see `ConnectionState` above). */
  readonly connectionState: ConnectionState;
}

const INITIAL_STATE: StoreState = {
  jobsById: new Map(),
  lastError: null,
  connectionState: "idle",
};

let state: StoreState = INITIAL_STATE;
const listeners = new Set<() => void>();

/**
 * Listeners for "a job the store didn't already know about just showed up
 * via SSE" — distinct from `listeners` (which fire on every store mutation,
 * including terminal-status updates to jobs already visible). `WorkspaceView`
 * uses this to auto-open the Jobs tab the moment a freshly-dispatched job's
 * `job.update` frame lands, without needing a new chat-stream wire event.
 * Only fired from `applyEvent` (live SSE), never from `refresh()` — the
 * initial `GET /jobs` seed and reconnect resync populate pre-existing jobs,
 * which shouldn't yank the user into the Jobs tab on every page load/reconnect.
 */
const newJobListeners = new Set<(job: Job) => void>();

/**
 * In-flight full-refresh promise. Concurrent `refresh()` calls without
 * `ids` collapse onto this single promise so we never issue duplicate
 * `GET /jobs` requests on a render storm. Per-id resyncs (`refresh(ids)`)
 * intentionally do NOT dedupe — each call is a different ID set.
 */
let inflightFullRefresh: Promise<void> | null = null;

/**
 * Module-level seed flag. The first `useJobs()` mount triggers a
 * `refresh()`; subsequent mounts (e.g. a second consumer of the same store)
 * see the already-seeded state and skip the redundant fetch.
 */
let hasSeeded = false;

function notify(): void {
  for (const listener of listeners) listener();
}

function setState(next: StoreState): void {
  state = next;
  notify();
}

async function fetchAllJobs(): Promise<readonly Job[]> {
  const res = await authFetch("/jobs");
  if (!res.ok) {
    throw new Error(`GET /jobs failed: ${res.status} ${res.statusText}`);
  }
  const body = (await res.json()) as { jobs?: unknown };
  if (!body || !Array.isArray(body.jobs)) return [];
  return body.jobs as Job[];
}

/**
 * Result of a delete call — thrown as an `Error` with this shape attached
 * (via `Object.assign`) so callers that want the structured code/message
 * (e.g. to render "job still active, force?" inline) can inspect it, while
 * callers that just want a boolean can catch-and-ignore like any other
 * `Error`.
 */
export interface JobDeleteError extends Error {
  code?: string;
}

async function parseDeleteError(res: Response, fallback: string): Promise<JobDeleteError> {
  let code: string | undefined;
  let message = fallback;
  try {
    const body = (await res.json()) as { error?: string; message?: string };
    if (typeof body.error === "string") code = body.error;
    if (typeof body.message === "string") message = body.message;
  } catch { /* non-JSON error body — use fallback */ }
  const err = new Error(message) as JobDeleteError;
  err.code = code;
  return err;
}

/**
 * Ids per `GET /jobs?ids=` request. Job ids are Step Functions execution ARNs (~150 chars URL-encoded), and
 * one request carrying every id of a busy user 414'd at CloudFront (URI too long) — the reconnect resync then
 * failed every time. 20 keeps a request near 3 KB, well under the 8 KB URL limit.
 */
export const JOB_IDS_PER_REQUEST = 20;

async function fetchJobsByIds(ids: readonly string[]): Promise<readonly Job[]> {
  if (ids.length === 0) return [];
  const chunks: string[][] = [];
  for (let i = 0; i < ids.length; i += JOB_IDS_PER_REQUEST) chunks.push(ids.slice(i, i + JOB_IDS_PER_REQUEST));
  const pages = await Promise.all(chunks.map(async (chunk) => {
    const qs = chunk.map(encodeURIComponent).join(",");
    const res = await authFetch(`/jobs?ids=${qs}`);
    if (!res.ok) {
      throw new Error(`GET /jobs?ids= failed: ${res.status} ${res.statusText}`);
    }
    const body = (await res.json()) as { jobs?: unknown };
    return body && Array.isArray(body.jobs) ? (body.jobs as Job[]) : [];
  }));
  return pages.flat();
}

/**
 * Reseed (or per-id resync) the store from the proxy.
 *
 *   - `refresh()`         — full reseed: replaces the entire `jobsById`
 *                           map with the proxy's current view.
 *   - `refresh([id, …])`  — resync the given ids only (used by
 *                           `useJobsSubscription` on every reconnect to
 *                           heal any drift while the EventSource was
 *                           closed). Existing records for other ids are
 *                           preserved.
 *
 * Errors are captured in `state.lastError`; the promise still resolves so
 * callers (the subscription hook in particular) don't have to wrap every
 * call in try/catch. The `Error` is exposed through `useJobs().lastError`.
 */
const NOTIFY_TERMINAL: ReadonlySet<string> = new Set(["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]);

/** How recently a newly-discovered job must have finished to be announced. */
const RECENT_TERMINAL_MS = 10 * 60 * 1000;

/**
 * Announce jobs that reached a terminal state via a REST path (mount seed,
 * reconnect resync, or the 30s reconcile).
 *
 * `pushFromTerminal` was called from exactly ONE place: the live `job.terminal`
 * SSE frame handler. If that frame was missed - dropped connection, backgrounded
 * tab, registry data changing under a live tab - the job healed into the store on
 * the next refresh but stayed SILENT forever, so the user never learned the work
 * had finished. Observed on staging: a job succeeded in 14s with a correct
 * registry row, and produced no bell and no card until a manual reload.
 *
 * SSE remains the low-latency path; REST is the source of truth that converges.
 *
 * `pushFromTerminal` dedupes on jobId against the persisted list, so firing from
 * both paths cannot double-notify. Two guards stop a page load replaying history:
 * a job already known-terminal is skipped, and a newly-discovered one must have
 * finished within RECENT_TERMINAL_MS.
 */
function notifyTerminalTransitions(
  prevMap: ReadonlyMap<string, Job>,
  fetched: readonly Job[],
): void {
  if (!getCompletionNotificationsEnabled()) return;
  const now = Date.now();
  for (const job of fetched) {
    if (!NOTIFY_TERMINAL.has(job.status)) continue;
    // Only a completion row fires the "done, fetch results" notification. A DAG emits one
    // terminal per node PLUS the run row; a node finishing early raises a premature banner whose
    // click fetches partial results. Two shapes of plan-member row must be suppressed:
    //   - the plan NODE row (jobId=`<run>#<node>`), which carries nodeId; and
    //   - the underlying GATEWAY job (e.g. `face-recognition:...`), which carries runId but an
    //     EMPTY nodeId -- the case that fired here (session 28df6f8f: face + audio gateway jobs
    //     terminal 16:20:19, a full minute before shots 16:21:19 and the run row 16:21:25).
    // A gateway member is identified by runId set AND jobId !== runId; the run row has
    // jobId === runId, and a standalone job has no runId. Members still flow into the store for
    // sidebar/DAG progress -- they just do not raise the banner. Only the run row and a true
    // standalone job (both nodeId null, and not a runId-tagged member) do.
    if (job.nodeId) continue;
    if (job.runId && job.runId !== job.jobId) continue;
    const prev = prevMap.get(job.jobId);
    if (prev && NOTIFY_TERMINAL.has(prev.status)) continue;
    if (!prev) {
      const ts = Date.parse(job.updatedAt || "");
      if (!Number.isFinite(ts) || now - ts > RECENT_TERMINAL_MS) continue;
    }
    notificationsStore.pushFromTerminal(job);
  }
}

async function refresh(ids?: readonly string[]): Promise<void> {
  const isFullRefresh = !ids || ids.length === 0;

  if (isFullRefresh && inflightFullRefresh) {
    return inflightFullRefresh;
  }

  const op = (async () => {
    try {
      if (!isFullRefresh) {
        const fetched = await fetchJobsByIds(ids!);
        const prevMap = state.jobsById;
        const nextMap = new Map(state.jobsById);
        for (const job of fetched) {
          nextMap.set(job.jobId, job);
        }
        setState({ ...state, jobsById: nextMap, lastError: null });
        notifyTerminalTransitions(prevMap, fetched);
        return;
      }
      const fetched = await fetchAllJobs();
      const prevMap = state.jobsById;
      const nextMap = new Map<string, Job>();
      for (const job of fetched) {
        nextMap.set(job.jobId, job);
      }
      setState({ ...state, jobsById: nextMap, lastError: null });
      notifyTerminalTransitions(prevMap, fetched);
    } catch (err: unknown) {
      const error = err instanceof Error ? err : new Error(String(err));
      setState({ ...state, lastError: error });
    }
  })();

  if (isFullRefresh) {
    inflightFullRefresh = op.finally(() => {
      inflightFullRefresh = null;
    });
    return inflightFullRefresh;
  }
  return op;
}

/**
 * Apply a single SSE frame to the store. `useJobsSubscription` calls this
 * for every `job.update` / `job.terminal` event it receives. Both event
 * types upsert by `jobId`; the `event` discriminator is informational
 * (consumers may use it for terminal-state side effects but the store
 * doesn't branch on it — the `Job.status` and `Job.terminalAt` fields
 * already encode the transition).
 */
/**
 * Remove the given job ids from the local store immediately, without a
 * round trip to `GET /jobs`. Called after a successful delete so the
 * Activity Panel reflects the removal without waiting on the next
 * SSE/poll cycle (deletes don't emit their own stream frame — the DDB
 * Stream only fires on INSERT/MODIFY, not REMOVE-by-the-proxy, since the
 * proxy's delete is a direct DynamoDB call outside the job lifecycle the
 * changefeed-fanout Lambda watches for).
 */
/**
 * Fetch every job for ONE session via `GET /jobs?sessionId=…`.
 *
 * The default `GET /jobs` is actor-wide and bounded to the newest ~50 runs, so
 * opening an OLDER session showed an empty right panel even though its jobs are
 * still in the registry. This session-scoped read returns that session's jobs
 * regardless of recency (see the proxy handler).
 */
async function fetchJobsForSession(sessionId: string): Promise<readonly Job[]> {
  if (!sessionId) return [];
  const res = await authFetch(`/jobs?sessionId=${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    throw new Error(`GET /jobs?sessionId= failed: ${res.status} ${res.statusText}`);
  }
  const body = (await res.json()) as { jobs?: unknown };
  if (!body || !Array.isArray(body.jobs)) return [];
  return body.jobs as Job[];
}

/**
 * Load an opened session's jobs into the shared store and MERGE them (upsert by
 * jobId) — never replacing the actor-wide seed, so the newest-runs view and any
 * other loaded session stay intact. Called when a session is opened, so a
 * previously-viewed conversation's Jobs panel is populated even when its runs
 * fell outside the newest-runs window. Errors are captured in `lastError`; the
 * promise still resolves so callers need no try/catch.
 */
export async function loadSession(sessionId: string): Promise<void> {
  if (!sessionId) return;
  try {
    const fetched = await fetchJobsForSession(sessionId);
    if (fetched.length === 0) return;
    const prevMap = state.jobsById;
    const nextMap = new Map(state.jobsById);
    for (const job of fetched) nextMap.set(job.jobId, job);
    setState({ ...state, jobsById: nextMap, lastError: null });
    notifyTerminalTransitions(prevMap, fetched);
  } catch (err: unknown) {
    const error = err instanceof Error ? err : new Error(String(err));
    setState({ ...state, lastError: error });
  }
}

function removeJobs(ids: readonly string[]): void {
  if (ids.length === 0) return;
  const nextMap = new Map(state.jobsById);
  let changed = false;
  for (const id of ids) {
    if (nextMap.delete(id)) changed = true;
  }
  if (changed) setState({ ...state, jobsById: nextMap });
}

/**
 * Delete a single job record via `DELETE /jobs/:jobId`.
 *
 * Active jobs (PENDING/RUNNING) are refused server-side unless `force` is
 * true — deleting the tracking row does not stop the underlying gateway
 * job or plan node, it only orphans the UI's view of it, so this is a
 * deliberate guard against accidentally losing visibility into something
 * still running. On success, removes the id from the local store so the
 * UI updates immediately.
 */
async function deleteJob(jobId: string, opts?: { force?: boolean }): Promise<void> {
  const qs = opts?.force ? "?force=true" : "";
  const res = await authFetch(`/jobs/${encodeURIComponent(jobId)}${qs}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw await parseDeleteError(res, `DELETE /jobs/${jobId} failed: ${res.status} ${res.statusText}`);
  }
  let body: { deleted?: string[] };
  try {
    body = (await res.json()) as { deleted?: string[] };
  } catch {
    // A 2xx with a non-JSON body means the request was misrouted (e.g. served the
    // SPA index.html) rather than handled by the /jobs DELETE route. Surface a clear
    // error instead of the opaque `Unexpected token '<'` JSON parse failure.
    throw new Error(`DELETE /jobs/${jobId} returned a non-JSON response (status ${res.status}); the delete route may be unavailable.`);
  }
  removeJobs(body.deleted ?? [jobId]);
}

/**
 * Delete every job record sharing a `runId` via `DELETE /jobs?runId=...`
 * (the plan-parent row plus all node/raw-gateway-job rows for that run,
 * in one server-side call). Same active-job guard as `deleteJob`.
 */
async function deleteRun(runId: string, opts?: { force?: boolean }): Promise<void> {
  const params = new URLSearchParams({ runId });
  if (opts?.force) params.set("force", "true");
  const res = await authFetch(`/jobs?${params.toString()}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw await parseDeleteError(res, `DELETE /jobs?runId=${runId} failed: ${res.status} ${res.statusText}`);
  }
  let body: { deleted?: string[] };
  try {
    body = (await res.json()) as { deleted?: string[] };
  } catch {
    throw new Error(`DELETE /jobs?runId=${runId} returned a non-JSON response (status ${res.status}); the delete route may be unavailable.`);
  }
  removeJobs(body.deleted ?? []);
}

function applyEvent(event: JobUpdateEvent): void {
  const job = event?.data;
  if (!job || typeof job.jobId !== "string" || job.jobId === "") return;
  const isNewJob = !state.jobsById.has(job.jobId);
  const nextMap = new Map(state.jobsById);
  nextMap.set(job.jobId, job);
  setState({ ...state, jobsById: nextMap });
  if (isNewJob) {
    for (const listener of newJobListeners) listener(job);
  }
}

/**
 * Subscribe to "new job appeared via live SSE" notifications. Returns an
 * unsubscribe function. See `newJobListeners` for why this is separate from
 * the `useSyncExternalStore`-facing `subscribe`.
 */
function subscribeNewJob(listener: (job: Job) => void): () => void {
  newJobListeners.add(listener);
  return () => {
    newJobListeners.delete(listener);
  };
}

function setConnectionState(connectionState: ConnectionState): void {
  if (state.connectionState === connectionState) return;
  setState({ ...state, connectionState });
}

/**
 * Module-level imperative API. Exposed as a single object so the
 * subscription hook (and tests) can grab a stable reference without
 * re-wiring on every render. The functions themselves are stable across
 * renders because they live at module scope.
 */
export const jobsStore = {
  getSnapshot: (): StoreState => state,
  subscribe: (listener: () => void): (() => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  subscribeNewJob,
  refresh,
  applyEvent,
  setConnectionState,
  deleteJob,
  deleteRun,
};

/**
 * Pure visibility predicate for a single job.
 *
 * Active jobs (`PENDING`, `RUNNING`) are always visible. Terminal jobs
 * are visible iff `terminalAt` parses to a finite timestamp and
 * `now - terminalAt < windowHours`. A terminal job without a
 * `terminalAt` (legitimately missing or malformed) is hidden — the
 * sidebar would have no useful timestamp to display anyway, and the
 * polling worker's `set_terminal` always stamps it on real terminal
 * transitions.
 *
 * Exported for unit-test coverage (R6.1).
 */
export function isJobVisible(
  job: Job,
  now: number,
  windowHours: number = JOBS_RECENCY_WINDOW_HOURS,
): boolean {
  if (ACTIVE_STATUSES.has(job.status)) return true;
  if (typeof job.terminalAt !== "string" || job.terminalAt === "") return false;
  const terminalMs = Date.parse(job.terminalAt);
  if (!Number.isFinite(terminalMs)) return false;
  const windowMs = windowHours * 60 * 60 * 1000;
  return now - terminalMs < windowMs;
}

/**
 * Project the full store map down to the visible list, sorted newest-first
 * by `updatedAt`. ISO-8601 strings sort lexicographically, so a string
 * comparison gives the same order as parsing each timestamp.
 *
 * Exported for unit-test coverage (R6.1) and so consumers that want the
 * un-sorted full set can opt out by reading `jobsStore.getSnapshot()`
 * directly.
 */
export function filterVisibleJobs(
  jobsById: ReadonlyMap<string, Job>,
  now: number = Date.now(),
  windowHours: number = JOBS_RECENCY_WINDOW_HOURS,
): Job[] {
  const out: Job[] = [];
  for (const job of jobsById.values()) {
    if (isJobVisible(job, now, windowHours)) out.push(job);
  }
  out.sort((a, b) => {
    if (a.updatedAt === b.updatedAt) return 0;
    return a.updatedAt < b.updatedAt ? 1 : -1;
  });
  return out;
}

/**
 * Reconcile-backstop selection (issue-2 fix): the job ids that are still shown
 * as ACTIVE (PENDING/RUNNING) among the currently-visible jobs. The SSE hook
 * periodically re-fetches exactly these so a dropped `job.terminal` frame
 * self-heals — a terminal job never needs re-polling, and off-window jobs are
 * already hidden. Pure + exported for unit coverage.
 */
export function activeJobIdsForReconcile(
  jobsById: ReadonlyMap<string, Job>,
  now: number = Date.now(),
): string[] {
  return filterVisibleJobs(jobsById, now)
    .filter((job) => job.status === "PENDING" || job.status === "RUNNING")
    .map((job) => job.jobId);
}

/**
 * Result type of `useJobs()`. Includes both read fields and the
 * imperative knobs the subscription hook uses (`applyEvent`,
 * `setConnectionState`). The subscription hook (task 12.2) will call
 * `useJobs()` itself, pick those off, and never touch the read fields,
 * so the surface stays narrow despite carrying writers.
 */
export interface UseJobsResult {
  /**
   * Visible jobs for the authenticated Actor, sorted newest-first by
   * `updatedAt`. Filter is `active OR (terminal && within window)`.
   */
  jobs: Job[];
  /**
   * Reseed (no args) or per-id resync (with `ids`) from the proxy. Errors
   * land in `lastError`; the promise itself never rejects.
   */
  refresh: (ids?: readonly string[]) => Promise<void>;
  /** Most recent `refresh()` error, or `null` after a successful refresh. */
  lastError: Error | null;
  /** SSE connection state, mutated by `useJobsSubscription`. */
  connectionState: ConnectionState;
  /**
   * Setter for `connectionState`. `useJobsSubscription` is the canonical
   * caller; other consumers should treat this as private.
   */
  setConnectionState: (state: ConnectionState) => void;
  /**
   * Apply an SSE frame. `useJobsSubscription` is the canonical caller;
   * other consumers should treat this as private.
   */
  applyEvent: (event: JobUpdateEvent) => void;
  /**
   * Delete a single job record. Throws a {@link JobDeleteError} (with an
   * optional `code` of `"job_active"`) if the job is still active and
   * `force` was not passed. Removes the job from the store on success.
   */
  deleteJob: (jobId: string, opts?: { force?: boolean }) => Promise<void>;
  /**
   * Delete every job record sharing a `runId` in one call. Throws a
   * {@link JobDeleteError} (with an optional `code` of `"run_active"`) if
   * any job in the run is still active and `force` was not passed.
   * Removes all deleted jobs from the store on success.
   */
  deleteRun: (runId: string, opts?: { force?: boolean }) => Promise<void>;
}

/**
 * Subscribe to the shared jobs store, seed it from `GET /jobs` on first
 * mount, and return the visible-filtered view.
 *
 * Multiple consumers in the same React tree share a single store and a
 * single seed call; the seed is keyed off a module-level flag so a second
 * `useJobs()` call doesn't re-fetch.
 *
 * @param windowHours How far back terminal jobs stay visible in the returned
 *   list. Defaults to {@link JOBS_RECENCY_WINDOW_HOURS} (24h) — right for the
 *   compact sidebar. The dedicated Workflow page (`ActivityPanel`) passes
 *   `Infinity` so a completed flow doesn't vanish from the history view a day
 *   after it finished: the store already holds the proxy's newest ~100 records
 *   regardless of age (`GET /jobs` applies no time filter), so this is purely a
 *   per-consumer projection width — it does not change the store, the seed, or
 *   what any other consumer (the sidebar) sees.
 */
export function useJobs(windowHours: number = JOBS_RECENCY_WINDOW_HOURS): UseJobsResult {
  const snapshot = useSyncExternalStore(
    jobsStore.subscribe,
    jobsStore.getSnapshot,
    jobsStore.getSnapshot,
  );

  useEffect(() => {
    if (!hasSeeded) {
      hasSeeded = true;
      void jobsStore.refresh();
    }
  }, []);

  const jobs = useMemo(
    () => filterVisibleJobs(snapshot.jobsById, Date.now(), windowHours),
    [snapshot.jobsById, windowHours],
  );

  return {
    jobs,
    refresh: jobsStore.refresh,
    lastError: snapshot.lastError,
    connectionState: snapshot.connectionState,
    setConnectionState: jobsStore.setConnectionState,
    applyEvent: jobsStore.applyEvent,
    deleteJob: jobsStore.deleteJob,
    deleteRun: jobsStore.deleteRun,
  };
}

/**
 * Test-only: reset every piece of module-level state. Mirrors the
 * `__resetXForTests` convention used by `auth/devAuth.ts`.
 */
export function __resetJobsStoreForTests(): void {
  state = INITIAL_STATE;
  listeners.clear();
  newJobListeners.clear();
  inflightFullRefresh = null;
  hasSeeded = false;
}

/** Test-only surface. Kept minimal and clearly marked. */
export const __testables = { notifyTerminalTransitions };
