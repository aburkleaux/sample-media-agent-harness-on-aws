/**
 * Auto-open for a Moment card's Clip icon (2026-10-11).
 *
 * The Clip icon sends a visible chat turn; the agent submits an async media-processing job whose result
 * normally waits behind the completion bell. For a CARD-STARTED clip the user asked for the clip to open in
 * a new timeline tab on its own, so this store links the click to its job — and ONLY that job; nothing
 * else auto-renders.
 *
 * Jobs carry no turn id, so the link is session + time:
 *   - each click records one pending entry `{sessionId, requestedAt}`;
 *   - a terminal STANDALONE job (no `runId`/`nodeId`, not a `plan|` parent) in that session whose
 *     `startAt` falls in `[requestedAt - START_SKEW_MS, requestedAt + START_WINDOW_MS]` consumes the
 *     OLDEST such entry — one job per click, at most;
 *   - a renderable success then emits `lab:moment-clip-ready` with its notification (WorkspaceView opens
 *     it exactly as a bell click does, if that session is on screen); a failure only clears the entry,
 *     and its normal failure notification still shows.
 * `startAt` is the server's clock and `requestedAt` the browser's, hence the small skew allowance.
 * An entry no job claimed is dropped after ENTRY_TTL_MS.
 *
 * Fed from `notifications.pushFromTerminal`, the one choke point both the SSE and REST paths go through
 * (and which already dedupes a job), so a job is never matched twice.
 */
import type { Job } from '../types/jobs';
import type { JobNotification } from './notifications';

export const MOMENT_CLIP_READY_EVENT = 'lab:moment-clip-ready';
/** A clip job must START within this long after the click to be the click's job. */
export const START_WINDOW_MS = 5 * 60 * 1000;
/** Browser/server clock allowance: a job may appear to start this long BEFORE the click. */
export const START_SKEW_MS = 10 * 1000;
/** A pending entry lives this long, so a clip that started in the window may still finish. */
export const ENTRY_TTL_MS = 30 * 60 * 1000;

export interface PendingClip {
  sessionId: string;
  requestedAt: number;
  label: string;
}

export interface MomentClipReadyDetail {
  notification: JobNotification;
  label: string;
}

let pending: PendingClip[] = [];

function prune(now: number): void {
  pending = pending.filter((p) => now - p.requestedAt <= ENTRY_TTL_MS);
}

/** Record one card-started clip request. No-op without a session (nothing could be linked). */
export function requestMomentClipAutoOpen(sessionId: string | null | undefined, label: string, now: number = Date.now()): void {
  if (!sessionId) return;
  prune(now);
  pending.push({ sessionId, requestedAt: now, label });
}

/** The pending entries (oldest first). For tests and diagnostics. */
export function pendingMomentClips(now: number = Date.now()): readonly PendingClip[] {
  prune(now);
  return pending;
}

/**
 * Offer a terminal job (and the notification just raised for it). Consumes at most one entry; on a
 * renderable success dispatches {@link MOMENT_CLIP_READY_EVENT}. Returns what happened.
 */
export function noteTerminalForMomentClip(
  job: Job, notification: JobNotification, now: number = Date.now(),
): 'opened' | 'cleared' | null {
  prune(now);
  if (pending.length === 0) return null;
  if (job.runId || job.nodeId || (job.trioId || '').startsWith('plan|')) return null;
  const startAt = Date.parse(job.startAt || '');
  if (!Number.isFinite(startAt)) return null;
  const i = pending.findIndex((p) => p.sessionId === job.sessionId
    && startAt >= p.requestedAt - START_SKEW_MS && startAt <= p.requestedAt + START_WINDOW_MS);
  if (i < 0) return null;
  if (notification.kind === 'success' && !notification.renderable) return null; // not the clip's output
  const [entry] = pending.splice(i, 1);
  if (notification.kind !== 'success') return 'cleared';
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<MomentClipReadyDetail>(MOMENT_CLIP_READY_EVENT, {
      detail: { notification, label: entry.label },
    }));
  }
  return 'opened';
}

/** Test-only reset. */
export function __resetMomentClipAutoOpenForTests(): void {
  pending = [];
}
