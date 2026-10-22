/**
 * useJobsSubscription — SSE binding for the async-job-tracking sidebar/inline
 * UI surfaces.
 *
 * Opens a fetch-based SSE stream against `/jobs/stream` and dispatches every
 * `job.update` / `job.terminal` frame into the shared `jobsStore` defined
 * by `useJobs.ts` (task 12.1). Owns the SSE-side `connectionState` mutation
 * — `useJobs()` reads that state but never writes it.
 *
 * Reconnect-resync (R8.3 / design.md "Reconnect resync"):
 * On every successful (re)open AFTER the first, the hook calls
 * `jobsStore.refresh(visibleIds)` with the ids of jobs the sidebar/inline
 * UI is currently displaying. The first open is intentionally skipped —
 * `useJobs()`'s seed effect already does a full `refresh()` on mount, so a
 * second fetch in the same tick is wasted bandwidth. From the second open
 * onward, every reconnect heals any drift the stream missed while it
 * was disconnected.
 *
 * The design chose SSE for its transport
 * (design.md "Push_Channel" — "long-lived HTTP/1.1 + auto-reconnect baked
 * into EventSource"). The hook implements its own backoff (see handleError); it
 * tracks `connectionState` transitions and reopens with a fresh token.
 *
 * Auth
 * ----
 * The stream is read with `fetch` (see `sseStream.ts`), NOT a native `EventSource`, so the JWT
 * travels in the `Authorization` header like every other request. `EventSource` cannot set headers,
 * and the workaround it forced — `?access_token=<token>` on the stream URL — put the user's whole
 * identity in a URL (access logs, history). The proxy no longer accepts a query token. A null token
 * (dev bypass before `signInDev`) opens without the header; dev bypass skips validation.
 *
 * Every (re)connect mints the header from a fresh `getAccessToken()`, so a reconnect after the
 * token rotated uses the new one. The server ending the stream, a non-2xx answer, or a network
 * error all take the same backoff-and-reopen path.
 */

import { pendingMomentClips } from '../services/momentClipAutoOpen';
import { useEffect } from "react";
import { getAccessToken } from "../auth/authManager";
import { readSseStream, type SseEvent } from "./sseStream";
import { filterVisibleJobs, jobsStore, activeJobIdsForReconcile } from "./useJobs";
import { notificationsStore, getCompletionNotificationsEnabled } from "../services/notifications";
import type { Job, JobUpdateEvent } from "../types/jobs";

const STREAM_PATH = "/jobs/stream";

/**
 * Subscribe to `/jobs/stream` for the lifetime of the calling component.
 *
 * Mount once near the root of the React tree (e.g. alongside the
 * `SessionSidebar` consumer). Multiple mounts open multiple streams
 * — the design assumes a single SSE connection per browser tab so the
 * caller is responsible for keeping this hook's mount count to one.
 *
 * Returns nothing: all observable state lands in the shared `jobsStore`
 * and is read via `useJobs()` by every consumer in the tree.
 */
export function useJobsSubscription(): void {
  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;
    // Increments on every successful `open` event. `0 → 1` is the first
    // open (skip the resync — `useJobs` already seeded). `1 → 2`, `2 → 3`,
    // … are reconnects (run the resync per R8.3).
    let openCount = 0;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 5;

    const dispatchFrame = (
      eventName: JobUpdateEvent["event"],
    ): ((rawData: string) => void) => {
      return (rawData: string) => {
        let data: Job;
        try {
          data = JSON.parse(rawData) as Job;
        } catch (err) {
          console.error(
            `[useJobsSubscription] Failed to parse ${eventName} payload:`,
            err,
          );
          return;
        }
        if (typeof data?.jobId !== "string" || data.jobId === "") {
          // Drop malformed frames — `applyEvent` would no-op anyway, but
          // logging makes the failure visible during development.
          console.warn(
            `[useJobsSubscription] Dropping ${eventName} frame with missing jobId`,
          );
          return;
        }
        jobsStore.applyEvent({ event: eventName, data });

        // Completion surface. New behavior (default): push a passive
        // notification (banner + bell) for standalone jobs instead of
        // hijacking the chat. Legacy fallback (flag off): auto-inject a render
        // turn into the originating session. Plan jobs (runId) are handled by
        // the Activity panel path below in both modes.
        if (eventName === "job.terminal") {
          // A Moment card's Clip asked for THIS session's next clip job to open itself; that request stands even
          // when completion notifications are switched off (the user explicitly clicked Clip).
          if (shouldSurfaceTerminal(data, getCompletionNotificationsEnabled())) {
            notificationsStore.pushFromTerminal(data);
          }
          // Async jobs do NOT auto-render on completion. Auto-rendering silently
          // (render-only, no animation, sometimes the wrong/empty card) is what
          // broke things. The bell is the signal; the user clicks it and
          // WorkspaceView runs the fetch+render+summary as a live turn (with the
          // composing animation) via chat:auto-render. Fast/direct jobs already
          // rendered in their own agent turn through the render-artifacts hook and
          // never reach this path.
        }

        // Phase 4: Notify the Activity panel when a run-grouped job finishes.
        // Dispatches a DOM event so the sidebar can show a badge indicator.
        if (eventName === "job.terminal" && data.runId) {
          window.dispatchEvent(new CustomEvent('activity:run-job-terminal', {
            detail: { jobId: data.jobId, runId: data.runId, status: data.status },
          }));
        }
      };
    };

    const handleOpen = (): void => {
      const isReconnect = openCount > 0;
      openCount += 1;
      reconnectAttempts = 0;
      jobsStore.setConnectionState("open");
      if (!isReconnect) return;

      // R8.3: on every reconnect, request the current state of every job
      // currently displayed in the client. We read the visible set from
      // the store (not from React state) so this stays correct regardless
      // of when the open fires relative to the React commit phase.
      const snapshot = jobsStore.getSnapshot();
      const visibleIds = filterVisibleJobs(snapshot.jobsById).map(
        (job) => job.jobId,
      );
      if (visibleIds.length === 0) return;
      void jobsStore.refresh(visibleIds);
    };

    const handleError = (): void => {
      // Instead of relying on browser auto-reconnect (which reuses the stale
      // URL with a potentially expired JWT), close the stream and re-open
      // with a freshly-acquired token. Exponential backoff with a max of 5
      // attempts before giving up.
      if (cancelled) return;
      if (controller !== null) {
        controller.abort();
        controller = null;
      }
      reconnectAttempts += 1;
      if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        jobsStore.setConnectionState("closed");
        console.warn(
          `[useJobsSubscription] Giving up after ${MAX_RECONNECT_ATTEMPTS} reconnect attempts`,
        );
        return;
      }
      jobsStore.setConnectionState("error");
      const backoffMs = Math.min(1000 * 2 ** (reconnectAttempts - 1), 30000);
      setTimeout(() => {
        if (!cancelled) void open();
      }, backoffMs);
    };

    const open = async (): Promise<void> => {
      jobsStore.setConnectionState("connecting");

      // Token resolution. `getAccessToken()` returns `null` when the user
      // isn't signed in (or, in dev bypass mode, when `signInDev` hasn't
      // run yet). Treat that as "open without a token" — in dev bypass
      // the proxy bypasses validation; in prod the proxy will 401 and the
      // `error` listener will surface it.
      let token: string | null = null;
      try {
        token = await getAccessToken();
      } catch (err) {
        console.error(
          "[useJobsSubscription] getAccessToken() threw:",
          err,
        );
      }
      if (cancelled) return;

      const ac = new AbortController();
      controller = ac;
      const onUpdate = dispatchFrame("job.update");
      const onTerminal = dispatchFrame("job.terminal");
      const onEvent = (e: SseEvent): void => {
        if (e.event === "job.update") onUpdate(e.data);
        else if (e.event === "job.terminal") onTerminal(e.data);
      };
      try {
        const res = await fetch(STREAM_PATH, {
          headers: {
            accept: "text/event-stream",
            ...(token !== null ? { authorization: `Bearer ${token}` } : {}),
          },
          cache: "no-store",
          signal: ac.signal,
        });
        if (cancelled || ac.signal.aborted) return;
        if (!res.ok || !res.body) {
          handleError();
          return;
        }
        handleOpen();
        await readSseStream(res.body, onEvent, ac.signal);
      } catch (err) {
        if (cancelled || ac.signal.aborted) return;
        console.warn("[useJobsSubscription] stream error:", err);
      }
      // The server closed the stream (or it failed): reopen with a fresh token after backoff.
      if (!cancelled && !ac.signal.aborted) handleError();
    };

    void open();

    return () => {
      cancelled = true;
      if (controller !== null) {
        controller.abort();
        controller = null;
      }
      jobsStore.setConnectionState("closed");
    };
    // The empty dep array is intentional — the stream is owned for
    // the lifetime of the component. React StrictMode's double-invoke is
    // safe: the cleanup closes the first connection, the re-mount opens a
    // fresh one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reconcile backstop. The SSE stream resyncs on *reconnect* (handleOpen),
  // but if a `job.terminal` frame is dropped while the connection stays open
  // (or the browser tab was backgrounded), a job can appear stuck at
  // PENDING/RUNNING in the UI even though the backend already marked it
  // terminal. Periodically (and on tab focus) re-fetch ONLY the jobs the
  // client still shows as active, so any missed transition self-heals within
  // ~30s instead of looking hung until the next reconnect/remount. Cheap: it
  // no-ops whenever there are no active jobs on screen.
  useEffect(() => {
    let tick = 0;
    const reconcileActive = (): void => {
      tick += 1;
      const snapshot = jobsStore.getSnapshot();
      const activeIds = activeJobIdsForReconcile(snapshot.jobsById);
      if (activeIds.length > 0) void jobsStore.refresh(activeIds);
      // Discovery sweep. Per-id resync can only heal jobs the client ALREADY
      // knows about, so a job whose FIRST frame was missed is never discovered:
      // no row, no notification. A full refresh is the only way to learn of it.
      // Runs on alternate ticks (and whenever nothing is active) to keep the
      // request rate low.
      if (tick % 2 === 0 || activeIds.length === 0) void jobsStore.refresh();
    };
    const intervalId = window.setInterval(reconcileActive, 30000);
    const onVisible = (): void => {
      if (document.visibilityState === "visible") reconcileActive();
    };
    window.addEventListener("focus", reconcileActive);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("focus", reconcileActive);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
}

/**
 * Whether a `job.terminal` frame reaches the notifications store: when completion notifications are on, or when a
 * Moment card's Clip is waiting for this session's next clip job (the user explicitly asked for it to open).
 */
export function shouldSurfaceTerminal(data: unknown, notificationsEnabled: boolean): boolean {
  if (notificationsEnabled) return true;
  const sid = (data as { sessionId?: unknown } | null)?.sessionId;
  return typeof sid === 'string' && pendingMomentClips().some((p) => p.sessionId === sid);
}
