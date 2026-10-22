/**
 * useTimelineView — ONE zoom + horizontal scroll shared by every time-scaled surface of a render:
 * the editor's ruler and the analysis lanes (shots, speakers, people, stems) below it.
 *
 * Each surface registers its scroll container with the width of the fixed label gutter in front of its
 * time track (0 for the ruler, the lanes' sticky label column otherwise). Zoom is one number; scroll is
 * synced as a FRACTION of each container's scroll range, so a time sits at the same x in every surface
 * as long as their tracks have the same width — which NleTimeline arranges by giving the ruler the
 * lanes' gutter. Without the sync, zooming the ruler left the lanes at 1× and a shot was impossible to
 * pick on a long film.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

export const MIN_ZOOM = 1;
export const MAX_ZOOM = 32;
/** One wheel notch. */
export const WHEEL_FACTOR = 1.25;

export interface TimelineView {
  zoom: number;
  /** Multiply the zoom, keeping the time under `clientX` of `el` (a registered container) in place. */
  zoomBy: (factor: number, el?: HTMLElement | null, clientX?: number) => void;
  setZoom: (z: number) => void;
  /** A ref callback for a scroll container whose time track starts `gutter` px in. */
  register: (key: string, gutter?: number) => (el: HTMLElement | null) => void;
  /** Bring `t` (seconds of `duration`) into view if it is outside the visible window. */
  follow: (t: number, duration: number) => void;
}

const clampZoom = (z: number) => Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, +z.toFixed(2)));

interface Entry { el: HTMLElement; gutter: number }

function trackWidth(e: Entry): number { return Math.max(1, e.el.scrollWidth - e.gutter); }
function maxScroll(e: Entry): number { return Math.max(0, e.el.scrollWidth - e.el.clientWidth); }

export function useTimelineView(): TimelineView {
  const [zoom, setZoomState] = useState(1);
  const entries = useRef(new Map<string, Entry>());
  const refFns = useRef(new Map<string, (el: HTMLElement | null) => void>());
  // Set by zoomBy, applied once the new width has laid out: the time fraction to hold, and its x offset
  // from the start of the visible track.
  const anchor = useRef<{ frac: number; x: number } | null>(null);

  const syncFrom = useCallback((src: Entry) => {
    const m = maxScroll(src);
    const f = m > 0 ? src.el.scrollLeft / m : 0;
    for (const e of entries.current.values()) {
      if (e === src) continue;
      const target = f * maxScroll(e);
      if (Math.abs(e.el.scrollLeft - target) > 1) e.el.scrollLeft = target;
    }
  }, []);

  const register = useCallback((key: string, gutter = 0) => {
    const cached = refFns.current.get(`${key}:${gutter}`);
    if (cached) return cached;
    let bound: { el: HTMLElement; onScroll: () => void } | null = null;
    const fn = (el: HTMLElement | null) => {
      if (bound) { bound.el.removeEventListener('scroll', bound.onScroll); entries.current.delete(key); bound = null; }
      if (!el) return;
      const entry: Entry = { el, gutter };
      const onScroll = () => syncFrom(entry);
      el.addEventListener('scroll', onScroll, { passive: true });
      entries.current.set(key, entry);
      bound = { el, onScroll };
      // A surface that mounts while zoomed in (lanes arriving with a result) joins at the others' scroll.
      const other = [...entries.current.values()].find((e) => e !== entry);
      if (other) syncFrom(other);
    };
    refFns.current.set(`${key}:${gutter}`, fn);
    return fn;
  }, [syncFrom]);

  const zoomBy = useCallback((factor: number, el?: HTMLElement | null, clientX?: number) => {
    const entry = el ? [...entries.current.values()].find((e) => e.el === el) : [...entries.current.values()][0];
    if (entry) {
      const r = entry.el.getBoundingClientRect();
      const visible = Math.max(1, entry.el.clientWidth - entry.gutter);
      const x = clientX === undefined
        ? visible / 2
        : Math.max(0, Math.min(visible, clientX - r.left - entry.el.clientLeft - entry.gutter));
      anchor.current = { frac: (entry.el.scrollLeft + x) / trackWidth(entry), x };
    }
    setZoomState((z) => clampZoom(z * factor));
  }, []);

  const setZoom = useCallback((z: number) => {
    const first = [...entries.current.values()][0];
    if (first) {
      const visible = Math.max(1, first.el.clientWidth - first.gutter);
      anchor.current = { frac: (first.el.scrollLeft + visible / 2) / trackWidth(first), x: visible / 2 };
    }
    setZoomState(clampZoom(z));
  }, []);

  // After a zoom has laid out, hold the anchored time in place in EVERY surface.
  useLayoutEffect(() => {
    const a = anchor.current;
    anchor.current = null;
    for (const e of entries.current.values()) {
      e.el.scrollLeft = a ? Math.max(0, a.frac * trackWidth(e) - a.x) : Math.min(e.el.scrollLeft, maxScroll(e));
    }
  }, [zoom]);

  const follow = useCallback((t: number, duration: number) => {
    const e = [...entries.current.values()][0];
    if (!e || duration <= 0 || maxScroll(e) <= 0) return;
    const w = trackWidth(e);
    const visible = e.el.clientWidth - e.gutter;
    const x = (t / duration) * w;
    if (x >= e.el.scrollLeft && x <= e.el.scrollLeft + visible) return;
    e.el.scrollLeft = Math.max(0, x - visible * 0.1);
    syncFrom(e);
  }, [syncFrom]);

  return useMemo(() => ({ zoom, zoomBy, setZoom, register, follow }), [zoom, zoomBy, setZoom, register, follow]);
}

/** Wheel over a registered container zooms the shared view about the pointer; horizontal wheel scrolls. */
export function useWheelZoom(view: TimelineView, el: HTMLElement | null): void {
  const { zoomBy } = view;
  useEffect(() => {
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      // A mostly-horizontal gesture (trackpad swipe, shift+wheel) is a scroll — leave it native.
      if (!e.deltaY || Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? WHEEL_FACTOR : 1 / WHEEL_FACTOR, el, e.clientX);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomBy, el]);
}
