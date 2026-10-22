/**
 * Where am I on the timeline: the frame under the cursor, with its exact timecode, in a bubble above the ruler.
 *
 * Two hidden helper videos, created lazily per source and kept OFF-screen (not `display:none`, which stops a
 * browser decoding frames):
 *   - the SAMPLER seeks to the hovered time and draws that frame onto the bubble's canvas, latest request wins,
 *     so a fast sweep never queues stale seeks;
 *   - the RATE PROBE plays a fraction of a second muted and measures the frame rate from the presented frames
 *     (`requestVideoFrameCallback`), cached per source. No rate (an audio file, a browser without the API) → the
 *     timeline keeps its millisecond timecodes.
 *
 * No `crossOrigin` on either: drawing a cross-origin frame onto a canvas only TAINTS it, and a tainted canvas still
 * displays. Nothing ever reads the pixels back, so presigned S3 media needs no CORS for this.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ThemeTokens } from '../renderer/types';
import { formatTimecode } from './formatTimecode';
import { formatFrameTimecode, fpsFromFrameTimes, frameAt } from './frameTime';

type RVFCVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: (now: number, meta: { mediaTime: number }) => void) => number;
};

/** A source's identity without its signature: a re-signed URL is the same media. */
const sourceKey = (src: string) => src.split('?')[0];

function offscreenVideo(src: string): HTMLVideoElement {
  const v = document.createElement('video');
  v.muted = true;
  v.playsInline = true;
  v.preload = 'auto';
  v.src = src;
  v.setAttribute('aria-hidden', 'true');
  Object.assign(v.style, { position: 'fixed', left: '-10000px', top: '0', width: '2px', height: '2px', opacity: '0', pointerEvents: 'none' });
  document.body.appendChild(v);
  return v;
}

const fpsCache = new Map<string, number | null>();

/** The video's frame rate, measured once per source; `null` until known or when it cannot be measured. */
export function useVideoFps(src: string | undefined, enabled: boolean): number | null {
  const key = src ? sourceKey(src) : '';
  const [fps, setFps] = useState<number | null>(() => (key && fpsCache.has(key) ? fpsCache.get(key) ?? null : null));
  useEffect(() => {
    if (!enabled || !src || typeof document === 'undefined') return;
    if (fpsCache.has(key)) { setFps(fpsCache.get(key) ?? null); return; }
    const v = offscreenVideo(src) as RVFCVideo;
    if (typeof v.requestVideoFrameCallback !== 'function') { v.remove(); fpsCache.set(key, null); return; }
    const times: number[] = [];
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      const measured = fpsFromFrameTimes(times);
      fpsCache.set(key, measured);
      setFps(measured);
      v.pause();
      v.removeAttribute('src');
      v.remove();
    };
    const onFrame = (_now: number, meta: { mediaTime: number }) => {
      times.push(meta.mediaTime);
      if (times.length >= 14) finish();
      else v.requestVideoFrameCallback!(onFrame);
    };
    v.requestVideoFrameCallback(onFrame);
    v.addEventListener('loadeddata', () => { void v.play().catch(finish); }, { once: true });
    v.addEventListener('error', finish, { once: true });
    const timer = setTimeout(finish, 4000);
    return () => { clearTimeout(timer); if (!done) { done = true; v.pause(); v.remove(); } };
  }, [src, key, enabled]);
  return fps;
}

/** Draw the frame at a time onto a canvas, from a lazily created hidden copy of the source. */
export function useFrameSampler(src: string | undefined) {
  const ref = useRef<{ v: HTMLVideoElement; busy: boolean; next: { t: number; c: HTMLCanvasElement } | null } | null>(null);
  useEffect(() => () => { ref.current?.v.remove(); ref.current = null; }, [src]);
  return useCallback((t: number, canvas: HTMLCanvasElement | null) => {
    if (!src || !canvas || typeof document === 'undefined') return;
    if (!ref.current) ref.current = { v: offscreenVideo(src), busy: false, next: null };
    const s = ref.current;
    s.next = { t, c: canvas };
    const run = () => {
      if (s.busy || !s.next) return;
      s.busy = true;
      const job = s.next;
      s.next = null;
      const done = () => {
        // Letterbox: draw the frame at ITS aspect inside the canvas (a vertical crop must not stretch to 16:9).
        try {
          const ctx = job.c.getContext('2d');
          const vw = s.v.videoWidth, vh = s.v.videoHeight;
          if (ctx && vw > 0 && vh > 0) {
            const scale = Math.min(job.c.width / vw, job.c.height / vh);
            const w = vw * scale, h = vh * scale;
            ctx.clearRect(0, 0, job.c.width, job.c.height);
            ctx.drawImage(s.v, (job.c.width - w) / 2, (job.c.height - h) / 2, w, h);
          }
        } catch { /* not decodable yet */ }
        s.busy = false;
        run();
      };
      s.v.addEventListener('seeked', done, { once: true });
      try { s.v.currentTime = job.t; } catch { s.busy = false; }
    };
    run();
  }, [src]);
}

/** The editor's timecode for a time: frames when the rate is known, else milliseconds. */
export function timecodeFor(seconds: number, fps: number | null | undefined): string {
  return fps ? formatFrameTimecode(seconds, fps) : formatTimecode(seconds);
}

export interface HoverPeekProps {
  src?: string;
  /** The hovered time, or `null` when the pointer is off the timeline. */
  time: number | null;
  fps: number | null;
  /** Viewport x of the hover line and the top of the ruler, for the bubble's fixed position. */
  anchor: { x: number; top: number } | null;
  /** Video: show the frame. Audio: the timecode alone. */
  showFrame: boolean;
  theme: ThemeTokens;
}

const W = 176;
const H = 99;

export function HoverPeek({ src, time, fps, anchor, showFrame, theme: t }: HoverPeekProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sample = useFrameSampler(showFrame ? src : undefined);
  useEffect(() => {
    if (time === null || !showFrame) return;
    // Seek to the middle of the frame so the sampler lands on it, not on the boundary before it.
    sample(fps ? (frameAt(time, fps) + 0.5) / fps : time, canvasRef.current);
  }, [time, fps, showFrame, sample]);
  if (time === null || !anchor || typeof document === 'undefined') return null;
  // Wide enough for the timecode, never narrower than the frame; centred on the hover line, kept on screen.
  const approxWidth = showFrame ? W + 10 : 170;
  const left = Math.max(8, Math.min(window.innerWidth - approxWidth - 8, anchor.x - approxWidth / 2));
  return createPortal(
    <div data-testid="timeline-hover-peek" role="presentation"
      style={{ position: 'fixed', left, top: anchor.top - (showFrame ? H + 36 : 32), minWidth: showFrame ? W + 10 : undefined,
        zIndex: t.zIndex.toast, pointerEvents: 'none', boxSizing: 'border-box',
        background: t.colors.surfaceRaised, color: t.colors.textPrimary, border: `1px solid ${t.colors.border}`,
        borderRadius: t.radius.panel, padding: 4, boxShadow: t.elevation.elevated }}>
      {showFrame && <canvas ref={canvasRef} width={W * 2} height={H * 2} style={{ display: 'block', width: W, height: H, margin: '0 auto', borderRadius: t.radius.button, background: t.colors.surfaceSubtle }} />}
      <div data-testid="timeline-hover-timecode" style={{ fontFamily: t.typography.fontMono, fontVariantNumeric: 'tabular-nums', fontSize: 'var(--fs-2xs)', fontWeight: 600, textAlign: 'center', padding: '3px 6px 1px', whiteSpace: 'nowrap' }}>
        {timecodeFor(time, fps)}{fps ? ` · frame ${frameAt(time, fps)}` : ''}
      </div>
    </div>,
    document.body,
  );
}
