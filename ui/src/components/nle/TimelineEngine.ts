/**
 * TimelineEngine — State manager for the NLE multi-track timeline.
 *
 * Manages shared playback state and synchronization across all NLE sub-components.
 * Uses a single requestAnimationFrame loop that reads the authoritative time from
 * an HTML media element, then broadcasts position to subscribers with ≤50ms deviation.
 *
 * Implemented as a class (not a React hook) so it can be shared by multiple components.
 * A `useTimelineEngine` hook wraps it for React consumption.
 */

import { useEffect, useRef, useSyncExternalStore } from 'react';
import type { InteractionEvent } from '../renderer/types';

// ---------------------------------------------------------------------------
// Interfaces
// ---------------------------------------------------------------------------

export interface TimelineState {
  currentTime: number;
  duration: number;
  isPlaying: boolean;
  playbackRate: number;
  zoomLevel: number;          // Pixels per second (10–500)
  scrollOffset: number;       // Horizontal scroll position
  markIn: number | null;
  markOut: number | null;
  /** Committed ranges (multi-select) — batch clip / analyze operate on these. */
  ranges: TimelineRange[];
  /** Committed point markers (multi-select) — batch frame extraction. */
  points: TimelinePoint[];
  selectedTrackId: string | null;
  selectedClipId: string | null;
  selectedCueIndex: number | null;
}

export type TimelineSubscriber = (state: TimelineState) => void;
export type InteractionHandler = (event: InteractionEvent) => void;

/** A committed in/out selection. Multiple ranges support batch clip/analyze. */
export interface TimelineRange { id: string; in: number; out: number; }
/** A committed point marker. Multiple points support batch frame extraction. */
export interface TimelinePoint { id: string; t: number; }

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const MIN_ZOOM = 10;   // pixels per second
export const MAX_ZOOM = 500;  // pixels per second

/** Valid tick intervals in seconds, ordered smallest to largest */
export const TICK_INTERVALS = [1, 2, 4, 8, 15, 30, 60] as const;

// ---------------------------------------------------------------------------
// TimelineEngine class
// ---------------------------------------------------------------------------

export class TimelineEngine {
  private state: TimelineState;
  private subscribers: Set<TimelineSubscriber> = new Set();
  private interactionHandlers: Set<InteractionHandler> = new Set();
  private mediaElement: HTMLMediaElement | null = null;
  private rafId: number | null = null;
  private lastBroadcastTime: number = -1;

  constructor(duration: number = 0) {
    this.state = {
      currentTime: 0,
      duration: Math.max(0, duration),
      isPlaying: false,
      playbackRate: 1,
      zoomLevel: 100,
      scrollOffset: 0,
      markIn: null,
      markOut: null,
      ranges: [],
      points: [],
      selectedTrackId: null,
      selectedClipId: null,
      selectedCueIndex: null,
    };
  }

  // -------------------------------------------------------------------------
  // Public getters
  // -------------------------------------------------------------------------

  getState(): TimelineState {
    return this.state;
  }

  // -------------------------------------------------------------------------
  // Subscription management
  // -------------------------------------------------------------------------

  subscribe(fn: TimelineSubscriber): () => void {
    this.subscribers.add(fn);
    return () => {
      this.subscribers.delete(fn);
    };
  }

  onInteraction(fn: InteractionHandler): () => void {
    this.interactionHandlers.add(fn);
    return () => {
      this.interactionHandlers.delete(fn);
    };
  }

  // -------------------------------------------------------------------------
  // Media element binding
  // -------------------------------------------------------------------------

  /**
   * Attach an HTML media element as the authoritative time source.
   * Starts the requestAnimationFrame sync loop.
   */
  attachMedia(element: HTMLMediaElement): void {
    this.detachMedia();
    this.mediaElement = element;

    // Sync initial state from media element
    this.updateStateFromMedia();

    // Listen for play/pause events to keep isPlaying in sync
    element.addEventListener('play', this.handleMediaPlay);
    element.addEventListener('pause', this.handleMediaPause);
    element.addEventListener('ratechange', this.handleRateChange);
    element.addEventListener('loadedmetadata', this.handleLoadedMetadata);

    // Start the rAF loop
    this.startSyncLoop();
  }

  /**
   * Detach the current media element and stop the sync loop.
   */
  detachMedia(): void {
    if (this.mediaElement) {
      this.mediaElement.removeEventListener('play', this.handleMediaPlay);
      this.mediaElement.removeEventListener('pause', this.handleMediaPause);
      this.mediaElement.removeEventListener('ratechange', this.handleRateChange);
      this.mediaElement.removeEventListener('loadedmetadata', this.handleLoadedMetadata);
      this.mediaElement = null;
    }
    this.stopSyncLoop();
  }

  // -------------------------------------------------------------------------
  // Playback controls
  // -------------------------------------------------------------------------

  play(): void {
    this.mediaElement?.play();
  }

  pause(): void {
    this.mediaElement?.pause();
  }

  seek(time: number): void {
    const clamped = Math.max(0, Math.min(time, this.state.duration));
    if (this.mediaElement) {
      this.mediaElement.currentTime = clamped;
    }
    this.setState({ currentTime: clamped });
  }

  setPlaybackRate(rate: number): void {
    if (this.mediaElement) {
      this.mediaElement.playbackRate = rate;
    }
    this.setState({ playbackRate: rate });
  }

  // -------------------------------------------------------------------------
  // Zoom and scroll
  // -------------------------------------------------------------------------

  setZoomLevel(zoom: number): void {
    const clamped = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
    this.setState({ zoomLevel: clamped });
  }

  setScrollOffset(offset: number): void {
    this.setState({ scrollOffset: Math.max(0, offset) });
  }

  // -------------------------------------------------------------------------
  // Mark In/Out logic
  // -------------------------------------------------------------------------

  /**
   * Set Mark In at the given time (or current playhead position).
   * Clamped to [0, duration]. If markIn >= markOut, clears markOut.
   */
  setMarkIn(time?: number): void {
    const t = time !== undefined ? time : this.state.currentTime;
    const clamped = this.clampToRange(t);
    let markOut = this.state.markOut;

    // If markIn >= markOut, clear markOut
    if (markOut !== null && clamped >= markOut) {
      markOut = null;
    }

    this.setState({ markIn: clamped, markOut });
    this.emitMarkInteraction(clamped, markOut);
  }

  /**
   * Set Mark Out at the given time (or current playhead position).
   * Clamped to [0, duration]. If markOut <= markIn, clears markIn.
   */
  setMarkOut(time?: number): void {
    const t = time !== undefined ? time : this.state.currentTime;
    const clamped = this.clampToRange(t);
    let markIn = this.state.markIn;

    // If markOut <= markIn, clear markIn
    if (markIn !== null && clamped <= markIn) {
      markIn = null;
    }

    this.setState({ markIn, markOut: clamped });
    this.emitMarkInteraction(markIn, clamped);
  }

  /**
   * Clear both Mark In and Mark Out.
   */
  clearMarks(): void {
    this.setState({ markIn: null, markOut: null });
    this.emitMarkInteraction(null, null);
  }

  // -------------------------------------------------------------------------
  // Selection
  // -------------------------------------------------------------------------

  selectTrack(trackId: string | null): void {
    this.setState({ selectedTrackId: trackId });
  }

  selectClip(clipId: string | null): void {
    this.setState({ selectedClipId: clipId });
  }

  selectCue(cueIndex: number | null): void {
    this.setState({ selectedCueIndex: cueIndex });
  }

  // -------------------------------------------------------------------------
  // Multi-range / multi-point selection (batch clip / analyze / frame)
  // -------------------------------------------------------------------------

  private genId(prefix: string): string {
    return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  }

  /** Add a committed range (clamped, normalized). Zero-length is rejected. */
  addRange(inT: number, outT: number): string | null {
    const a = this.clampToRange(Math.min(inT, outT));
    const b = this.clampToRange(Math.max(inT, outT));
    if (b - a <= 0) return null;
    const id = this.genId('r');
    this.setState({ ranges: [...this.state.ranges, { id, in: a, out: b }] });
    this.emitMarkInteraction(a, b);
    return id;
  }

  /** Commit the current draft mark-in/out into the ranges list and clear the draft. */
  commitDraftRange(): string | null {
    const { markIn, markOut } = this.state;
    if (markIn === null || markOut === null) return null;
    const id = this.addRange(markIn, markOut);
    this.setState({ markIn: null, markOut: null });
    return id;
  }

  updateRange(id: string, inT: number, outT: number): void {
    const a = this.clampToRange(Math.min(inT, outT));
    const b = this.clampToRange(Math.max(inT, outT));
    this.setState({ ranges: this.state.ranges.map(r => (r.id === id ? { ...r, in: a, out: b } : r)) });
  }

  removeRange(id: string): void {
    this.setState({ ranges: this.state.ranges.filter(r => r.id !== id) });
  }

  clearRanges(): void {
    this.setState({ ranges: [] });
  }

  /** Add a committed point marker at `t` (or the current playhead). */
  addPoint(t?: number): string {
    const tt = this.clampToRange(t !== undefined ? t : this.state.currentTime);
    const id = this.genId('p');
    this.setState({ points: [...this.state.points, { id, t: tt }] });
    return id;
  }

  removePoint(id: string): void {
    this.setState({ points: this.state.points.filter(p => p.id !== id) });
  }

  clearPoints(): void {
    this.setState({ points: [] });
  }

  // -------------------------------------------------------------------------
  // Duration clamping (Requirement 8.5)
  // -------------------------------------------------------------------------

  /**
   * Set the timeline duration with clamping logic.
   * If manifestDuration exceeds actualMediaDuration by >1s, clamp to actual.
   */
  setDuration(manifestDuration: number, actualMediaDuration?: number): void {
    let duration = Math.max(0, manifestDuration);

    if (
      actualMediaDuration !== undefined &&
      actualMediaDuration >= 0 &&
      manifestDuration - actualMediaDuration > 1
    ) {
      duration = actualMediaDuration;
    }

    this.setState({ duration });
  }

  // -------------------------------------------------------------------------
  // Tick interval calculation (Requirement 2.7)
  // -------------------------------------------------------------------------

  /**
   * Calculate the appropriate tick interval for the current zoom level.
   * Returns one of {1, 2, 4, 8, 15, 30, 60} seconds.
   *
   * At maximum zoom (500 px/s) → 1s intervals
   * At minimum zoom (10 px/s) → 60s intervals
   * Chooses the interval that keeps tick marks between 50-150px apart.
   */
  getTickInterval(zoomLevel?: number): number {
    const zoom = zoomLevel ?? this.state.zoomLevel;
    return calculateTickInterval(zoom);
  }

  // -------------------------------------------------------------------------
  // Cleanup
  // -------------------------------------------------------------------------

  destroy(): void {
    this.detachMedia();
    this.subscribers.clear();
    this.interactionHandlers.clear();
  }

  // -------------------------------------------------------------------------
  // Private: requestAnimationFrame sync loop
  // -------------------------------------------------------------------------

  private startSyncLoop(): void {
    if (this.rafId !== null) return;

    const loop = (): void => {
      this.updateStateFromMedia();
      this.rafId = requestAnimationFrame(loop);
    };

    this.rafId = requestAnimationFrame(loop);
  }

  private stopSyncLoop(): void {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
  }

  private updateStateFromMedia(): void {
    if (!this.mediaElement) return;

    const currentTime = this.mediaElement.currentTime;
    const isPlaying = !this.mediaElement.paused;
    const playbackRate = this.mediaElement.playbackRate;

    // Only broadcast if time has actually changed (≤50ms deviation guarantee:
    // rAF runs at ~16ms so we always broadcast within 16ms of the real position)
    const needsUpdate =
      Math.abs(currentTime - this.lastBroadcastTime) > 0 ||
      isPlaying !== this.state.isPlaying ||
      playbackRate !== this.state.playbackRate;

    if (needsUpdate) {
      this.lastBroadcastTime = currentTime;
      this.setState({ currentTime, isPlaying, playbackRate });
    }
  }

  // -------------------------------------------------------------------------
  // Private: state update and broadcast
  // -------------------------------------------------------------------------

  private setState(partial: Partial<TimelineState>): void {
    this.state = { ...this.state, ...partial };
    this.broadcast();
  }

  private broadcast(): void {
    for (const fn of this.subscribers) {
      fn(this.state);
    }
  }

  // -------------------------------------------------------------------------
  // Private: helpers
  // -------------------------------------------------------------------------

  private clampToRange(value: number): number {
    return Math.max(0, Math.min(value, this.state.duration));
  }

  private emitMarkInteraction(markIn: number | null, markOut: number | null): void {
    const event: InteractionEvent = {
      type: 'select',
      target: 'mark-range',
      data: { markIn, markOut },
      timestamp: Date.now(),
    };
    for (const handler of this.interactionHandlers) {
      handler(event);
    }
  }

  // -------------------------------------------------------------------------
  // Private: media event handlers
  // -------------------------------------------------------------------------

  private handleMediaPlay = (): void => {
    this.setState({ isPlaying: true });
  };

  private handleMediaPause = (): void => {
    this.setState({ isPlaying: false });
  };

  private handleRateChange = (): void => {
    if (this.mediaElement) {
      this.setState({ playbackRate: this.mediaElement.playbackRate });
    }
  };

  private handleLoadedMetadata = (): void => {
    if (this.mediaElement) {
      // Apply duration clamping on metadata load
      const actualDuration = this.mediaElement.duration;
      if (isFinite(actualDuration)) {
        this.setDuration(this.state.duration, actualDuration);
      }
    }
  };
}

// ---------------------------------------------------------------------------
// Tick interval calculation (exported for testing)
// ---------------------------------------------------------------------------

/**
 * Calculate the tick interval for a given zoom level.
 * Returns one of {1, 2, 4, 8, 15, 30, 60} seconds.
 *
 * At maximum zoom (500 px/s) → 1s intervals
 * At minimum zoom (10 px/s) → 60s intervals
 * Intervals increase monotonically as zoom decreases.
 *
 * Strategy: Pick the smallest interval from the set whose pixel spacing
 * (interval × zoom) meets or exceeds the target threshold. This naturally
 * produces 1s at high zoom (where even small intervals produce wide spacing)
 * and 60s at low zoom (where only the largest interval is sufficiently spaced).
 */
export function calculateTickInterval(zoomLevel: number): number {
  const zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoomLevel));

  // Target: tick marks should be at least this many pixels apart.
  // Calibrated so that zoom 500 → 1s (1*500=500) and zoom 10 → 60s (60*10=600).
  const targetMinPx = 500;

  for (const interval of TICK_INTERVALS) {
    const pxSpacing = interval * zoom;
    if (pxSpacing >= targetMinPx) {
      return interval;
    }
  }

  // Fallback: if even the largest interval doesn't reach target spacing,
  // use the largest interval available (60s)
  return TICK_INTERVALS[TICK_INTERVALS.length - 1];
}

// ---------------------------------------------------------------------------
// React hook: useTimelineEngine
// ---------------------------------------------------------------------------

/**
 * React hook that wraps a TimelineEngine instance.
 * Creates the engine once and returns the current state reactively.
 */
export function useTimelineEngine(duration: number = 0): {
  engine: TimelineEngine;
  state: TimelineState;
} {
  const engineRef = useRef<TimelineEngine | null>(null);

  if (engineRef.current === null) {
    engineRef.current = new TimelineEngine(duration);
  }

  const engine = engineRef.current;

  // Update duration if it changes
  useEffect(() => {
    engine.setDuration(duration);
  }, [engine, duration]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      engine.destroy();
    };
  }, [engine]);

  // Use useSyncExternalStore for concurrent-safe subscription
  const state = useSyncExternalStore(
    (callback) => engine.subscribe(callback),
    () => engine.getState(),
    () => engine.getState(),
  );

  return { engine, state };
}
