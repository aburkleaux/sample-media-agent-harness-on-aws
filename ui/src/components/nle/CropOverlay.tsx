/**
 * CropOverlay — an interactive rectangle selector drawn over a <video>, for cropping.
 *
 * The interactive sibling of NleTimeline's read-only face overlay: it establishes the same
 * frame-aligned coordinate space over the exact video element and lets the user draw / move /
 * resize a rectangle. On confirm it emits a box NORMALIZED to the video CONTENT rect (0..1,
 * letterbox-corrected) — the same units as an annotation box — which the agent's crop_video.py
 * resolves to real pixels.
 *
 * Resize convention (image-editor standard): CORNER handles preserve aspect (the locked preset
 * ratio, else the box's current ratio); EDGE handles resize one axis freely (unless a preset is
 * locked, which keeps all resizes proportional). The geometry (letterbox mapping, normalize,
 * aspect fit) is exported as pure functions and unit-tested without a DOM.
 *
 * Under components/nle/** (lint:colors-exempt): the selection/dim are editorial overlay chrome.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { CropBox, ThemeTokens } from '../renderer/types';
import CropIcon from '../icons/CropIcon';
import CloseIcon from '../icons/CloseIcon';

const MIN_BOX_PX = 24;           // in overlay pixels — a smaller drag is treated as a mis-click
const MIN_OUTPUT_PX = 16;        // guard mirrored by crop_video.py's resolve_crop

export interface Rect { x: number; y: number; w: number; h: number; }

/** The video CONTENT rect within an element box, assuming object-fit: contain (letterbox). */
export function contentRect(elemW: number, elemH: number, videoW: number, videoH: number): Rect {
  if (elemW <= 0 || elemH <= 0 || videoW <= 0 || videoH <= 0) return { x: 0, y: 0, w: elemW, h: elemH };
  const scale = Math.min(elemW / videoW, elemH / videoH);
  const w = videoW * scale, h = videoH * scale;
  return { x: (elemW - w) / 2, y: (elemH - h) / 2, w, h };
}

/** Clamp a box (element px) to sit within the content rect, keeping a minimum size. */
export function clampToContent(box: Rect, content: Rect, minPx = MIN_BOX_PX): Rect {
  const w = Math.max(minPx, Math.min(box.w, content.w));
  const h = Math.max(minPx, Math.min(box.h, content.h));
  const x = Math.max(content.x, Math.min(box.x, content.x + content.w - w));
  const y = Math.max(content.y, Math.min(box.y, content.y + content.h - h));
  return { x, y, w, h };
}

/** Element-px box → normalized 0..1 of the CONTENT rect (what the agent consumes). */
export function toNormalized(box: Rect, content: Rect): CropBox {
  if (content.w <= 0 || content.h <= 0) return { x: 0, y: 0, w: 0, h: 0 };
  return {
    x: (box.x - content.x) / content.w,
    y: (box.y - content.y) / content.h,
    w: box.w / content.w,
    h: box.h / content.h,
  };
}

/** Largest box of pixel-aspect `ar` (w:h) that fits the content, keeping the box's center. */
export function fitAspect(box: Rect, ar: number, content: Rect): Rect {
  if (!ar) return box;
  const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
  let w = box.w, h = w / ar;
  if (h > content.h) { h = content.h; w = h * ar; }
  if (w > content.w) { w = content.w; h = w / ar; }
  return { x: cx - w / 2, y: cy - h / 2, w, h };
}

/** Even-snapped output pixels for the readout (mirrors crop_video.py). */
export function outputPixels(box: Rect, content: Rect, videoW: number, videoH: number): { w: number; h: number } {
  const nx = toNormalized(box, content);
  const even = (v: number) => { const n = Math.round(v); return n - (n % 2); };
  return { w: even(nx.w * videoW), h: even(nx.h * videoH) };
}

const PRESETS: { label: string; ar: number }[] = [
  { label: 'Free', ar: 0 }, { label: '16:9', ar: 16 / 9 }, { label: '1:1', ar: 1 }, { label: '9:16', ar: 9 / 16 },
];

type DragKind = 'move' | 'draw' | { corner: string } | { edge: string };

export interface CropOverlayProps {
  /** The video element being cropped (for videoWidth/Height + pausing). */
  videoRef: React.RefObject<HTMLMediaElement | null>;
  theme: ThemeTokens;
  onCommit: (box: CropBox) => void;
  onCancel: () => void;
  /** Fires as the box settles (drag/resize end, preset pick) — lets the gallery commit the box
   *  LIVE so "Crop all" enables from drawing alone, with no separate confirm click. Optional:
   *  the single-video (NleTimeline) crop leaves it unset and keeps confirm-to-commit. */
  onChange?: (box: CropBox) => void;
  /** Small-card variant (gallery): one aspect-preserving corner handle, no edge handles. */
  compact?: boolean;
  /** Start from an existing normalized box (re-adjust / apply-to-all) instead of a default. */
  initialBox?: CropBox | null;
}

export default function CropOverlay({ videoRef, theme: t, onCommit, onCancel, onChange, compact = false, initialBox = null }: CropOverlayProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<Rect | null>(null);
  const [ar, setAr] = useState(0);
  const arRef = useRef(0); arRef.current = ar;
  const boxRef = useRef<Rect | null>(null); boxRef.current = box;
  const dragRef = useRef<{ kind: DragKind; sx: number; sy: number; o: Rect } | null>(null);
  const initialBoxRef = useRef<CropBox | null>(initialBox);
  initialBoxRef.current = initialBox;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  const geom = useCallback(() => {
    const r = rootRef.current?.getBoundingClientRect();
    const v = videoRef.current as HTMLVideoElement | null;
    const ow = r?.width ?? 0, oh = r?.height ?? 0;
    const vw = (v && v.videoWidth) || 16, vh = (v && v.videoHeight) || 9;
    return { content: contentRect(ow, oh, vw, vh), vw, vh };
  }, [videoRef]);

  // Enter crop mode: pause + drop a sensible default box once laid out.
  useEffect(() => {
    const v = videoRef.current; if (v) { try { v.pause(); } catch { /* noop */ } }
    const id = requestAnimationFrame(() => {
      const { content: c } = geom();
      const init = initialBoxRef.current;
      const b0 = init
        ? { x: c.x + init.x * c.w, y: c.y + init.y * c.h, w: init.w * c.w, h: init.h * c.h }
        : { x: c.x + c.w * 0.2, y: c.y + c.h * 0.15, w: c.w * 0.55, h: c.h * 0.6 };
      setBox(clampToContent(b0, c));
    });
    return () => cancelAnimationFrame(id);
  }, [geom, videoRef]);

  const commit = useCallback(() => {
    const b = boxRef.current; if (!b) return;
    const { content, vw, vh } = geom();
    const out = outputPixels(b, content, vw, vh);
    if (out.w < MIN_OUTPUT_PX || out.h < MIN_OUTPUT_PX) return; // too small — ignore
    onCommit(toNormalized(b, content));
  }, [geom, onCommit]);

  // Commit the box LIVE (gallery): fires as the box settles (drag/resize end, preset pick) so
  // "Crop all" enables from drawing alone — no separate confirm. Skips a sub-minimum box (a
  // mis-drag), exactly like commit. No-op when onChange is unset (single-video crop).
  const emitChange = useCallback(() => {
    const b = boxRef.current; if (!b || !onChangeRef.current) return;
    const { content, vw, vh } = geom();
    const out = outputPixels(b, content, vw, vh);
    if (out.w < MIN_OUTPUT_PX || out.h < MIN_OUTPUT_PX) return;
    onChangeRef.current(toNormalized(b, content));
  }, [geom]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
      else if (e.key === 'Enter') { e.preventDefault(); commit(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel, commit]);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const d = dragRef.current; if (!d) return;
      const r = rootRef.current?.getBoundingClientRect(); if (!r) return;
      const { content } = geom();
      const lx = e.clientX - r.left, ly = e.clientY - r.top;
      const dx = lx - d.sx, dy = ly - d.sy, o = d.o;
      let nb: Rect;
      if (d.kind === 'move') {
        nb = { ...o, x: o.x + dx, y: o.y + dy };
      } else if (d.kind === 'draw') {
        nb = { x: Math.min(d.sx, lx), y: Math.min(d.sy, ly), w: Math.abs(dx), h: Math.abs(dy) };
      } else if ('corner' in d.kind) {
        const h = d.kind.corner;
        const ratio = arRef.current || (o.w / o.h);
        const w = Math.max(MIN_BOX_PX, o.w + (h.includes('e') ? dx : -dx));
        const ht = w / ratio;
        nb = { w, h: ht, x: h.includes('w') ? o.x + (o.w - w) : o.x, y: h.includes('n') ? o.y + (o.h - ht) : o.y };
      } else {
        const h = d.kind.edge;
        nb = { ...o };
        if (h === 'e') nb.w = o.w + dx;
        if (h === 's') nb.h = o.h + dy;
        if (h === 'w') { nb.x = o.x + dx; nb.w = o.w - dx; }
        if (h === 'n') { nb.y = o.y + dy; nb.h = o.h - dy; }
        if (arRef.current) nb.h = nb.w / arRef.current;
      }
      setBox(clampToContent(nb, content));
    };
    const onUp = () => { const dragging = dragRef.current !== null; dragRef.current = null; if (dragging) emitChange(); };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); };
  }, [geom, emitChange]);

  const startDrag = (e: React.PointerEvent, kind: DragKind) => {
    e.stopPropagation();
    const r = rootRef.current?.getBoundingClientRect(); if (!r) return;
    const b = boxRef.current; if (!b && kind !== 'draw') return;
    dragRef.current = { kind, sx: e.clientX - r.left, sy: e.clientY - r.top, o: b ?? { x: 0, y: 0, w: 0, h: 0 } };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };
  const onRootPointerDown = (e: React.PointerEvent) => {
    if (e.target !== rootRef.current) return; // only empty-area starts a fresh draw
    const r = rootRef.current!.getBoundingClientRect();
    const lx = e.clientX - r.left, ly = e.clientY - r.top;
    dragRef.current = { kind: 'draw', sx: lx, sy: ly, o: { x: 0, y: 0, w: 0, h: 0 } };
    setBox({ x: lx, y: ly, w: 1, h: 1 });
    rootRef.current!.setPointerCapture?.(e.pointerId);
  };

  const pickPreset = (nextAr: number) => {
    setAr(nextAr);
    const b = boxRef.current; if (!b) return;
    const { content } = geom();
    const nb = nextAr ? clampToContent(fitAspect(b, nextAr, content), content) : b;
    if (nextAr) setBox(nb);
    onChangeRef.current?.(toNormalized(nb, content)); // live-commit the aspect pick (gallery)
  };

  const { content, vw, vh } = geom();
  const out = box ? outputPixels(box, content, vw, vh) : { w: 0, h: 0 };
  const aboveRoom = box ? box.y >= 44 : true;

  const handle = (style: React.CSSProperties): React.CSSProperties => ({
    position: 'absolute', width: 12, height: 12, background: '#fff',
    border: `1px solid ${t.colors.primary}`, borderRadius: 2, ...style,
  });

  return (
    <div
      ref={rootRef}
      data-testid="crop-overlay"
      onPointerDown={onRootPointerDown}
      style={{ position: 'absolute', inset: 0, overflow: 'hidden', cursor: 'crosshair', zIndex: 5, touchAction: 'none' }}
    >
      {box && (
        <>
          <div
            data-testid="crop-selection"
            onPointerDown={(e) => startDrag(e, 'move')}
            style={{ position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h, cursor: 'move',
              border: `1.5px solid ${t.colors.primary}`, boxShadow: '0 0 0 9999px rgba(0,0,0,0.55)' }}
          >
            {/* corners (aspect-preserving) — compact shows only the SE corner */}
            {(compact ? ['se'] : ['nw', 'ne', 'sw', 'se']).map((c) => (
              <div key={c} data-testid={`crop-handle-${c}`} onPointerDown={(e) => startDrag(e, { corner: c })}
                style={handle({
                  left: c.includes('w') ? -6 : undefined, right: c.includes('e') ? -6 : undefined,
                  top: c.includes('n') ? -6 : undefined, bottom: c.includes('s') ? -6 : undefined,
                  cursor: c === 'nw' || c === 'se' ? 'nwse-resize' : 'nesw-resize',
                })} />
            ))}
            {/* edges (free) — omitted in compact (too small to hit on a card) */}
            {!compact && ['n', 's', 'e', 'w'].map((c) => (
              <div key={c} data-testid={`crop-edge-${c}`} onPointerDown={(e) => startDrag(e, { edge: c })}
                style={handle({
                  left: c === 'n' || c === 's' ? '50%' : (c === 'w' ? -6 : undefined),
                  right: c === 'e' ? -6 : undefined,
                  top: c === 'e' || c === 'w' ? '50%' : (c === 'n' ? -6 : undefined),
                  bottom: c === 's' ? -6 : undefined,
                  marginLeft: c === 'n' || c === 's' ? -6 : undefined,
                  marginTop: c === 'e' || c === 'w' ? -6 : undefined,
                  cursor: c === 'n' || c === 's' ? 'ns-resize' : 'ew-resize',
                })} />
            ))}
          </div>
          {/* readout */}
          <div data-testid="crop-readout" style={{ position: 'absolute', left: box.x,
            top: box.y - 24 > content.y ? box.y - 24 : box.y + box.h + 6,
            background: 'rgba(13,16,23,0.8)', border: `1px solid ${t.colors.border}`, borderRadius: 6,
            padding: '2px 7px', fontSize: t.typography.sizes.badge, color: t.colors.textSecondary, pointerEvents: 'none' }}>
            {out.w} × {out.h}
          </div>
          {/* contextual toolbar anchored to the selection */}
          <div data-testid="crop-toolbar"
            style={{ position: 'absolute', left: box.x + box.w / 2, top: aboveRoom ? box.y - 8 : box.y + 8,
              transform: aboveRoom ? 'translate(-50%,-100%)' : 'translate(-50%,0)', display: 'flex', gap: 6,
              background: t.colors.surfaceRaised, border: `1px solid ${t.colors.border}`, borderRadius: t.radius.button,
              padding: 5, whiteSpace: 'nowrap', boxShadow: t.elevation.elevated, zIndex: 6 }}>
            {PRESETS.map((p) => (
              <button key={p.label} data-testid={`crop-preset-${p.label}`} onPointerDown={(e) => e.stopPropagation()}
                onClick={() => pickPreset(p.ar)}
                style={{ fontSize: t.typography.sizes.badge, padding: '3px 8px', borderRadius: t.radius.pill,
                  border: `1px solid ${ar === p.ar ? t.colors.primary : t.colors.border}`,
                  background: ar === p.ar ? t.colors.primary : t.colors.surface,
                  color: ar === p.ar ? t.colors.onPrimary : t.colors.textSecondary, cursor: 'pointer', fontFamily: 'inherit' }}>
                {p.label}
              </button>
            ))}
            {/* Per-overlay confirm. Hidden ONLY on a gallery card (`compact`): there `onChange` live-commits the box
                and "Crop all" applies them. Keyed on `onChange` it vanished from the single-video timeline too, once
                that began saving the box in the tab's view state — leaving no way to crop but Enter (staging). */}
            {!(compact && onChange) && (
              <button data-testid="crop-confirm" onPointerDown={(e) => e.stopPropagation()} onClick={commit}
                style={{ fontSize: t.typography.sizes.meta, padding: '3px 10px', borderRadius: t.radius.button,
                  border: 'none', background: t.colors.primary, color: t.colors.onPrimary,
                  fontWeight: t.typography.weights.semibold, cursor: 'pointer', fontFamily: 'inherit',
                  display: 'inline-flex', alignItems: 'center', gap: 6 }}><CropIcon size={14} />Crop</button>
            )}
            <button data-testid="crop-cancel" aria-label="Cancel crop" title="Cancel crop" onPointerDown={(e) => e.stopPropagation()} onClick={onCancel}
              style={{ padding: '3px 7px', borderRadius: t.radius.button,
                border: `1px solid ${t.colors.borderStrong}`, background: t.colors.surface,
                color: t.colors.textPrimary, cursor: 'pointer', fontFamily: 'inherit',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}><CloseIcon size={14} /></button>
          </div>
        </>
      )}
    </div>
  );
}
