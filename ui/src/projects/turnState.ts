/**
 * turnState — "is a chat turn of this session streaming right now?" (Component 7,
 * media-harness-search-qa-rendering design "Find_Similar"; Req 3.24).
 *
 * A Moment card's Find similar is disabled while a turn of its session is in progress, so the card
 * needs a cheap, synchronous read of per-session streaming state plus a subscription for live updates.
 * `ChatCanvas` owns the authoritative `streamingSessions` set; it calls {@link setSessionTurnInProgress}
 * whenever that set gains or loses a session. This module keeps a module-level mirror of the in-progress
 * sessions (so a gallery mounted mid-turn is correct at FIRST paint, before any event arrives) and
 * dispatches a `lab:turn-state` window event on every change (so already-mounted cards update live).
 *
 * Task 9.10 deferred this signal; 9.9 implements the minimal version its Find similar gate needs.
 */
import { useEffect, useState } from 'react';

/** The window event name both the dispatch and {@link useSessionTurnInProgress} use. */
export const TURN_STATE_EVENT = 'lab:turn-state';

/** The `lab:turn-state` event detail: which session changed, and whether a turn is now streaming. */
export interface TurnStateDetail {
  sessionId: string;
  inProgress: boolean;
}

/** The module-level mirror the dispatch updates; read for a correct first paint. */
const inProgress = new Set<string>();

/**
 * Record whether a turn of `sessionId` is streaming, and (on a real change) dispatch `lab:turn-state`.
 * Called by `ChatCanvas` as its `streamingSessions` set gains / loses a session. A no-op when the value
 * is unchanged, so a repeated stream event does not spam listeners.
 */
export function setSessionTurnInProgress(sessionId: string, value: boolean): void {
  if (!sessionId) return;
  const had = inProgress.has(sessionId);
  if (value === had) return;
  if (value) inProgress.add(sessionId);
  else inProgress.delete(sessionId);
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent<TurnStateDetail>(TURN_STATE_EVENT, { detail: { sessionId, inProgress: value } }),
    );
  }
}

/** Synchronous read of the mirror: is a turn of `sessionId` streaming right now? */
export function isSessionTurnInProgress(sessionId: string | null | undefined): boolean {
  return !!sessionId && inProgress.has(sessionId);
}

/** The whole set of in-progress sessions (read-only); the mirror the dispatch keeps current. */
export function getActiveTurnState(): ReadonlySet<string> {
  return inProgress;
}

/**
 * React binding: `true` while a turn of `sessionId` is streaming. Initializes from the module mirror
 * (`isSessionTurnInProgress`) so a card mounted mid-turn is correct before any event, then follows
 * `lab:turn-state` for this session.
 */
export function useSessionTurnInProgress(sessionId: string | null | undefined): boolean {
  const [value, setValue] = useState<boolean>(() => isSessionTurnInProgress(sessionId));
  useEffect(() => {
    setValue(isSessionTurnInProgress(sessionId));
    if (typeof window === 'undefined') return;
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<TurnStateDetail>).detail;
      if (detail && detail.sessionId === sessionId) setValue(detail.inProgress);
    };
    window.addEventListener(TURN_STATE_EVENT, handler as EventListener);
    return () => window.removeEventListener(TURN_STATE_EVENT, handler as EventListener);
  }, [sessionId]);
  return value;
}

/** Test-only reset, following the `__resetXForTests` convention. */
export function __resetTurnStateForTests(): void {
  inProgress.clear();
}
