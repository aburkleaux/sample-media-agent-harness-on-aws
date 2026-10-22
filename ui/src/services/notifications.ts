/**
 * Completion notifications — passive "job finished" surface.
 *
 * Replaces the disruptive auto-inject-into-chat behavior for standalone
 * (non-plan) jobs. When a job reaches a terminal state we push a notification
 * here instead of hijacking the chat; a transient banner nudges the user and a
 * persistent bell keeps the recent list so a dismissed/missed banner is never
 * lost. Clicking a notification opens the originating session and renders the
 * results (see `renderJobResults` in `autoRenderOnComplete`).
 *
 * Plan jobs (those with a `runId`) are intentionally NOT pushed here — they are
 * grouped in the Activity Panel with its own badge. This store is the unified
 * home for single-job completion, mirroring that passive model.
 *
 * State lives in a module-level store (same shape as `jobsStore`) so multiple
 * consumers share one instance via `useSyncExternalStore`, and is mirrored to
 * localStorage so the bell survives reloads.
 */

import type { Job } from '../types/jobs';
import { noteTerminalForMomentClip } from './momentClipAutoOpen';

const ITEMS_KEY = 'lab-notifications';
const FLAG_KEY = 'lab-completion-notifications-enabled';
const MAX_ITEMS = 50;
// A job that has EVER produced a completion notification is recorded here, PERSISTED separately
// from the (clearable) items list, so it can never notify again — even after the user hits
// "Clear all" (which empties `items`) or the item ages out past MAX_ITEMS. Without it a
// recently-terminal job re-notified on the next page load ("cleared notification keeps coming
// back after refresh").
const NOTIFIED_KEY = 'lab-notified-job-ids';
const MAX_NOTIFIED = 500;
/**
 * How long a completion stays in the bell. Without an age cap the list only shrank at
 * MAX_ITEMS (50), so day-old completions kept accumulating and the unread badge grew
 * unbounded across sessions — the "notifications I already handled keep coming back on
 * every login" report. History older than this is not actionable; the Activity panel and
 * the session itself remain the durable record.
 */
const MAX_ITEM_AGE_MS = 48 * 60 * 60 * 1000;
/**
 * A terminal job older than this never raises a notification, whatever path discovered it.
 * `useJobs.notifyTerminalTransitions` already applies a 10-minute window on the REST path,
 * but the SSE handler (`useJobsSubscription`) called `pushFromTerminal` with NO age check —
 * so a redelivered/backlogged `job.terminal` frame for old work could still announce itself.
 * Enforcing it HERE makes the store the single choke point for both paths.
 *
 * Deliberately lenient about a MISSING/unparseable `updatedAt`: those still notify, so a
 * genuinely-just-finished job with a malformed timestamp is never silently swallowed.
 */
const MAX_NOTIFY_AGE_MS = 60 * 60 * 1000;

/** Drop history the bell should no longer show or count. */
function pruneAged(list: JobNotification[], now: number = Date.now()): JobNotification[] {
  return list.filter((it) => {
    const ts = typeof it.ts === 'number' ? it.ts : NaN;
    return !Number.isFinite(ts) || now - ts <= MAX_ITEM_AGE_MS;
  });
}

export type NotificationKind = 'success' | 'failure';

export interface JobNotification {
  jobId: string;
  sessionId: string;
  trioId: string;
  /** Run id when this is a plan/flow (its members share it), so the click can roll up their
   *  renderable deliverables. Absent for standalone jobs. */
  runId?: string;
  kind: NotificationKind;
  /** Prettified workflow label, e.g. "Face recognition". */
  label: string;
  /** One-line message shown in the banner/bell. */
  message: string;
  /** Whether clicking should attempt a render (success + artifacts). */
  renderable: boolean;
  /** Resolved (durable) artifact URLs, so the click can render deterministically
   *  from them without an LLM re-fetch turn. Populated for renderable successes. */
  artifactUrls?: string[];
  /** Failure cause, when kind === 'failure'. */
  error: string | null;
  ts: number;
  seen: boolean;
  /** True once the transient banner has been shown-and-dismissed (× or auto-expire).
   *  PERSISTED and distinct from `seen`: it stops the one-time banner from re-firing on
   *  every reload, while the bell still shows the item as unread (`!seen`) until opened.
   *  Before this, dismissal lived only in in-memory React state, so a dismissed-but-unopened
   *  banner re-appeared after every login/refresh. */
  bannered?: boolean;
}

// ---------------------------------------------------------------------------
// Settings flag — new behavior on by default; toggle to fall back to the
// legacy auto-inject path (see useJobsSubscription).
// ---------------------------------------------------------------------------
export function getCompletionNotificationsEnabled(): boolean {
  try {
    const v = localStorage.getItem(FLAG_KEY);
    return v === null ? true : v === 'true';
  } catch {
    return true;
  }
}

export function setCompletionNotificationsEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(FLAG_KEY, enabled ? 'true' : 'false');
  } catch {
    /* localStorage unavailable — no-op */
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
interface Snapshot {
  items: JobNotification[];
  unseen: number;
}

function load(): JobNotification[] {
  try {
    const raw = localStorage.getItem(ITEMS_KEY);
    const arr = raw ? (JSON.parse(raw) as JobNotification[]) : [];
    return Array.isArray(arr) ? pruneAged(arr) : [];
  } catch {
    return [];
  }
}

let items: JobNotification[] = load();
let snapshot: Snapshot = computeSnapshot();
const listeners = new Set<() => void>();

function loadNotified(): string[] {
  try {
    const raw = localStorage.getItem(NOTIFIED_KEY);
    const arr = raw ? (JSON.parse(raw) as string[]) : [];
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

let notifiedJobIds: string[] = loadNotified();

/** Record that `jobId` has notified, so it never re-fires after a Clear all / reload. */
function recordNotified(jobId: string): void {
  if (notifiedJobIds.includes(jobId)) return;
  notifiedJobIds = [jobId, ...notifiedJobIds].slice(0, MAX_NOTIFIED);
  try {
    localStorage.setItem(NOTIFIED_KEY, JSON.stringify(notifiedJobIds));
  } catch {
    /* quota — ignore */
  }
}

function computeSnapshot(): Snapshot {
  return { items, unseen: items.reduce((n, it) => n + (it.seen ? 0 : 1), 0) };
}

function persist(): void {
  try {
    localStorage.setItem(ITEMS_KEY, JSON.stringify(items.slice(0, MAX_ITEMS)));
  } catch {
    /* quota — ignore */
  }
}

function commit(next: JobNotification[]): void {
  items = pruneAged(next).slice(0, MAX_ITEMS);
  snapshot = computeSnapshot();
  persist();
  for (const l of listeners) l();
}

/** Prettify a trioId ("face-recognition|recognize_faces") into a label. */
export function labelForTrio(trioId: string): string {
  const workflow = (trioId || '').split('|')[0] || 'Job';
  return workflow
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim() || 'Job';
}

const TERMINAL_FAILURE = new Set(['FAILED', 'TIMED_OUT', 'ABORTED']);

/**
 * Human label for a plan run, from a `plan|plan-<name>` trioId.
 *
 * `labelForTrio` would return "Plan" for every DAG (it takes the segment before
 * the `|`), so a DAG notification would read "Plan finished" with no indication
 * of which one. This uses the plan NAME instead.
 */
export function labelForPlan(trioId: string): string {
  const name = (trioId || '').split('|')[1] || '';
  const cleaned = name.replace(/^plan[-_]?/i, '').replace(/[-_]/g, ' ').trim();
  if (!cleaned) return 'Workflow';
  return cleaned.replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Push a notification from a terminal job. Dedupes by jobId (also the
 * reload-persistence key) and only accepts terminal states. Returns the created
 * notification, or null if skipped.
 */
export function pushFromTerminal(job: Job): JobNotification | null {
  if (!job || typeof job.jobId !== 'string' || !job.jobId) return null;
  // A DAG must produce exactly ONE notification, not one per node.
  //
  // Registry rows for a plan run share a `runId`:
  //   - the plan PARENT has runId === jobId  → notifies once, for the whole DAG
  //   - each CHILD node has runId !== jobId  → Activity/Workflow panel only
  // Standalone jobs have runId === null and notify as before.
  if (job.runId && job.runId !== job.jobId) return null;
  // The SSE `job.terminal` frame for a plan node is not guaranteed to carry
  // runId (some node-row writes — e.g. the changefeed bridge FAILED path —
  // don't stamp it), so the runId guard above cannot be the only member filter.
  // A plan NODE row always carries a nodeId; drop it here too, mirroring
  // useJobs.notifyTerminalTransitions. Only the plan parent (nodeId null,
  // runId === jobId) and true standalone jobs (no runId, no nodeId) notify.
  if (job.nodeId) return null;
  const isSuccess = job.status === 'SUCCEEDED';
  const isFailure = TERMINAL_FAILURE.has(job.status);
  if (!isSuccess && !isFailure) return null;
  // Stale-work guard (both paths). See MAX_NOTIFY_AGE_MS: the SSE handler had no age check,
  // so a redelivered frame for old work could announce itself as new. A missing/unparseable
  // timestamp still notifies rather than being dropped.
  const terminalTs = Date.parse(job.updatedAt || '');
  if (Number.isFinite(terminalTs) && Date.now() - terminalTs > MAX_NOTIFY_AGE_MS) return null;
  // Dedupe — SSE redelivery / reconnect resync must not double-notify.
  if (items.some((it) => it.jobId === job.jobId)) return null;
  // Persistent tombstone: once a job has notified it must never notify again, even after the user
  // cleared the list ("Clear all" empties `items`, so the in-list dedup above no longer catches
  // it) or the item aged out past MAX_ITEMS. This is what stops the cleared notification from
  // coming back on the next page refresh.
  if (notifiedJobIds.includes(job.jobId)) return null;

  const isPlanParent = job.runId === job.jobId || (job.trioId || '').startsWith('plan|');
  const label = isPlanParent ? labelForPlan(job.trioId) : labelForTrio(job.trioId);
  const renderable = isSuccess && job.hasRenderableArtifacts === true;
  // Fast / non-viewable STANDALONE success → NO notification. A metadata probe (answered
  // inline in the turn) or any standalone success with no renderable artifact has nothing
  // to OPEN, so a bell + a click that re-summarizes an answer already shown in the chat is
  // pure noise (the fast-probe friction the user hit). We still notify for: renderable
  // successes (a result to view → click renders it), failures (need attention), and plan
  // PARENTS (a completed DAG is a meaningful async event even if the parent row itself
  // carries no renderable artifact).
  if (isSuccess && !renderable && !isPlanParent) return null;
  // Deterministic "summary of the answer" (B3): fold the artifact count into the
  // completion notification — "Transcode finished — 2 files ready" — so a long job
  // summarizes what it produced without an LLM turn or a chat interruption. No ids.
  const artifactCount = Array.isArray(job.artifactUrls) ? job.artifactUrls.length : 0;
  const ready = renderable && artifactCount > 0
    ? ` — ${artifactCount} file${artifactCount === 1 ? '' : 's'} ready`
    : '';
  const notification: JobNotification = {
    jobId: job.jobId,
    sessionId: job.sessionId || '',
    trioId: job.trioId || '',
    runId: job.runId || undefined,
    kind: isSuccess ? 'success' : 'failure',
    label,
    message: isSuccess
      ? (isPlanParent ? `${label} workflow finished${ready}` : renderable ? `${label} finished${ready}` : `${label} completed`)
      : `${label} ${job.status.toLowerCase().replace('_', ' ')}`,
    renderable,
    artifactUrls: renderable && Array.isArray(job.artifactUrls) ? job.artifactUrls.filter(u => typeof u === 'string') : undefined,
    error: isFailure ? (job.lastError ?? null) : null,
    ts: Date.now(),
    seen: false,
  };
  commit([notification, ...items]);
  recordNotified(job.jobId);
  // A Moment card's Clip icon waits for exactly this job (session + start-time link); see momentClipAutoOpen.
  noteTerminalForMomentClip(job, notification);
  return notification;
}

/**
 * The renderable deliverable URLs a completion notification should render — matching the
 * contract "any async job OR flow → the final-completion notification fetches the result and
 * renders it." For a STANDALONE job that's its own resolved artifactUrls. For a plan/FLOW whose
 * deliverable lives on a MEMBER (e.g. a fal video generated under a plan, or shots/faces under a
 * DAG), the parent's notification is the ONE completion signal, so we roll up the members'
 * renderable outputs (a member's `runId` equals the parent job's id; by click time members are
 * done and in the store). Returns [] when nothing renderable is resolvable (caller may then do a
 * last-resort LLM fetch). Pure — exported for tests.
 */
export function rollupRenderUrls(
  n: { jobId: string; runId?: string; artifactUrls?: string[] },
  jobs: readonly Job[],
): string[] {
  const http = (u: unknown): u is string => typeof u === 'string' && /^https?:\/\//.test(u);
  const own = (n.artifactUrls || []).filter(http);
  if (own.length) return own;
  // Only a FLOW (has a runId) rolls up members; a standalone job with no own urls has nothing to
  // roll up (and must NOT cross-match other standalone jobs, which also have runId == null).
  if (!n.runId) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const j of jobs) {
    if (j.runId === n.runId && j.hasRenderableArtifacts && Array.isArray(j.artifactUrls)) {
      for (const u of j.artifactUrls) {
        if (http(u) && !seen.has(u)) { seen.add(u); out.push(u); }
      }
    }
  }
  return out;
}

export function markSeen(jobId: string): void {
  if (!items.some((it) => it.jobId === jobId && !it.seen)) return;
  commit(items.map((it) => (it.jobId === jobId ? { ...it, seen: true } : it)));
}

/** Mark the one-time banner for `jobId` as already shown (persisted), so it never
 *  re-fires on reload. Does NOT mark the item read — the bell keeps it unread until
 *  opened. Called when a banner is dismissed (× or auto-expire). */
export function markBannered(jobId: string): void {
  if (!items.some((it) => it.jobId === jobId && !it.bannered)) return;
  commit(items.map((it) => (it.jobId === jobId ? { ...it, bannered: true } : it)));
}

export function markAllSeen(): void {
  if (snapshot.unseen === 0) return;
  commit(items.map((it) => (it.seen ? it : { ...it, seen: true })));
}

export function clearAll(): void {
  if (items.length === 0) return;
  commit([]);
}

export const notificationsStore = {
  getSnapshot: (): Snapshot => snapshot,
  subscribe: (listener: () => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
  pushFromTerminal,
  markSeen,
  markBannered,
  markAllSeen,
  clearAll,
};

/** Test-only reset. */
export function __resetNotificationsForTests(): void {
  items = [];
  snapshot = computeSnapshot();
  notifiedJobIds = [];
  try { localStorage.removeItem(ITEMS_KEY); } catch { /* ignore */ }
  try { localStorage.removeItem(NOTIFIED_KEY); } catch { /* ignore */ }
}
