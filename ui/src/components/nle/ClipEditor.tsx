/**
 * ClipEditor — one INDEPENDENT clip's inline editor for a clip-gallery.
 *
 * Each clip (an audio take or a video take) owns its OWN timeline: its own player,
 * its own waveform (audio), and its own mark-in/out ruler scoped to its own duration.
 * The editor never assumes clips are time-aligned — that is the whole point of the
 * gallery vs a shared-axis timeline.
 *
 * Contract: the editor only MARKS a keep-range and reports it up via `onMarkChange`.
 * The gallery composes the (possibly batched) agent request. So a mark here always
 * carries this clip's identity — there is no ambiguity about which clip a trim hits.
 *
 * Interaction:
 *   - click the ruler → move the playhead (and the media currentTime)
 *   - drag the ruler → mark a keep-range [in,out]
 *   - In / Out → set the boundary at the current playhead (keyboard/AT friendly,
 *     and the deterministic path unit tests drive)
 *   - Clear (always rendered, disabled with no mark, so the row never reflows) clears the mark
 *
 * `embedded` (a Moment card): the media sits in a fixed 16:9 box with the edit controls as an overlay
 * (`data-card-edit`, revealed by the host's CSS on hover / focus / edit) and the ruler in a fixed 14px
 * slot (`data-card-ruler`), so the card's height never depends on its state.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { ThemeTokens, CropBox } from '../renderer/types';
import type { ClipGalleryClip } from '../renderer/types';
import WaveformVisualizer from './WaveformVisualizer';
import CropOverlay from './CropOverlay';
import { formatTimecode } from './formatTimecode';
import CardTransport from './CardTransport';
import CropIcon from '../icons/CropIcon';
import MarkInIcon from '../icons/MarkInIcon';
import MarkOutIcon from '../icons/MarkOutIcon';
import CloseIcon from '../icons/CloseIcon';
import ExpandIcon from '../icons/ExpandIcon';
import AudioIcon from '../icons/AudioIcon';

const IN_COLOR = '#3FBF77';
const OUT_COLOR = '#E05D52';
const DRAG_THRESHOLD_PX = 4;

export interface ClipMark { in: number; out: number; }

export interface ClipEditorProps {
  clip: ClipGalleryClip;
  theme: ThemeTokens;
  /** Reports this clip's keep-range up to the gallery; null when cleared/invalid. */
  onMarkChange: (clipId: string, mark: ClipMark | null) => void;
  accent?: string;
  /** Report this clip's media element up so the gallery can drive "play all in sequence". */
  registerMedia?: (clipId: string, el: HTMLMediaElement | null) => void;
  /** True while this clip is the one the gallery is auto-playing (highlights the card). */
  playing?: boolean;
  /** VIDEO only: open this clip in its own Renderer timeline tab for frame-accurate analysis. */
  onExpand?: (clip: ClipGalleryClip) => void;
  /** VIDEO only: report this clip's chosen crop region (normalized) up to the gallery; null clears it. */
  onCropChange?: (clipId: string, box: CropBox | null) => void;
  /** VIDEO only: the clip's current crop region (controlled by the gallery), shown persistently. */
  cropBox?: CropBox | null;

  // --- Opt-in props for Moment cards (Component 7). Every default below reproduces today's
  //     behavior exactly, so existing clip-gallery callers are unaffected.

  /** Override the media `src` (the gallery mints a presigned URL at view time and swaps it on a
   *  refresh). Default: `clip.src`, i.e. unchanged. */
  src?: string;
  /** Seed the mark-in/out from a Moment's span so the card counts as marked from its first render.
   *  `in`/`out` are seconds. Default undefined → no initial mark (today). */
  initialMark?: ClipMark;
  /** Seconds to seek to (paused) on the FIRST loadedmetadata. If `>= duration`, calls
   *  `onError('beyond_duration')` instead; otherwise seeks and clamps an over-long `initialMark.out`
   *  to the duration (until the Actor edits the marks). Default undefined → no seek (today). */
  seekOnFirstLoad?: number;
  /** Ignored: playback needs no CORS and the waveform is drawn from `clip.peaks` (mediaCors.ts). Kept so
   *  existing callers still type-check. */
  mediaCrossOrigin?: boolean;
  /** Observe this clip's media element events (`play`, `pause`, `timeupdate`, `seeking`,
   *  `loadedmetadata`, `error`) so the gallery can drive a URL refresh. Default undefined → no-op. */
  onMediaEvent?: (type: 'play' | 'pause' | 'timeupdate' | 'seeking' | 'loadedmetadata' | 'error', el: HTMLMediaElement) => void;
  /** Hide the Expand button even for a video clip. Default false → Expand shows as today. */
  hideExpand?: boolean;
  /** Report a non-recoverable condition. Currently only `'beyond_duration'` (seek past the end,
   *  Req 3.15). Default undefined → ignored. */
  onError?: (reason: 'beyond_duration') => void;
  /** VIDEO/AUDIO (Moment cards): confine playback to [start,end] SECONDS — the slice the Actor is
   *  allowed to view. Seeks there on load, snaps an out-of-window seek back in, and pauses at `end`.
   *  Default undefined → the whole asset plays (clip-gallery behaviour, unchanged). */
  playWindow?: { start: number; end: number };
  /** Render WITHOUT the card chrome (outer border / top accent) and WITHOUT the label+status
   *  header, for embedding inside a host card that already shows the title (a Moment card).
   *  Default false → the standalone clip-gallery card, unchanged. */
  embedded?: boolean;
  /** Reports whether the Actor has EDITED this clip (marks moved/cleared, a crop box, or the crop
   *  editor open) — the host card keeps its edit controls revealed while true. Default no-op. */
  onEditedChange?: (clipId: string, edited: boolean) => void;
}

const FLAGGED = new Set(['flagged', 'failed', 'regenerate', 'reject', 'rejected', 'unverified']);

export default function ClipEditor({ clip, theme: t, onMarkChange, accent, registerMedia, playing, onExpand, onCropChange, cropBox = null, src, initialMark, seekOnFirstLoad, onMediaEvent, hideExpand = false, onError, playWindow, embedded = false, onEditedChange }: ClipEditorProps) {
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const anchorRef = useRef<number | null>(null);
  const anchorXRef = useRef<number>(0);
  const draggedRef = useRef(false);
  // Component 7: the first-load seek runs once, and the over-long-markOut clamp is skipped once
  // the Actor has edited the marks, so a late metadata load never overwrites an edit.
  const didFirstSeekRef = useRef(false);
  const editedRef = useRef(false);
  // The same fact as state, for `data-edited` (the host reveals its edit controls while it is true).
  const [marksEdited, setMarksEdited] = useState(false);
  const markEdited = () => { editedRef.current = true; setMarksEdited(true); };

  // The media the element loads: a gallery-supplied presigned URL when present, else the clip's own.
  const mediaSrc = src ?? clip.src;

  const [duration, setDuration] = useState(clip.durationHint && clip.durationHint > 0 ? clip.durationHint : 0);
  const [currentTime, setCurrentTime] = useState(0);
  // Embedded (Moment card) transport state, mirrored from the media element's own events.
  const [paused, setPaused] = useState(true);
  const [muted, setMuted] = useState(false);
  // True while a playback that STARTED inside the play window runs: it pauses (and parks) at the window
  // end. A playback started outside the window -- the Actor scrubbed elsewhere on the full timeline -- runs
  // freely: the moment is a highlighted range, not a fence.
  const windowArmedRef = useRef(false);
  const [markIn, setMarkIn] = useState<number | null>(initialMark ? initialMark.in : null);
  const [markOut, setMarkOut] = useState<number | null>(initialMark ? initialMark.out : null);
  const [showCrop, setShowCrop] = useState(false);
  // The box that existed when the crop editor opened — restored on cancel (true-cancel), since
  // the box is now committed LIVE as it's drawn (so "Crop all" enables without a confirm click).
  const cropSnapshotRef = useRef<CropBox | null>(null);

  // Report up whenever a COMPLETE, valid range exists; null otherwise.
  useEffect(() => {
    if (markIn !== null && markOut !== null && markOut > markIn) {
      onMarkChange(clip.id, { in: markIn, out: markOut });
    } else {
      onMarkChange(clip.id, null);
    }
  }, [markIn, markOut, clip.id, onMarkChange]);

  const dur = duration > 0 ? duration : 0;
  const pct = (s: number) => (dur > 0 ? Math.max(0, Math.min(100, (s / dur) * 100)) : 0);

  const timeFromClientX = useCallback((clientX: number): number => {
    const el = rulerRef.current;
    if (!el) return 0;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return 0;
    const ratio = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    return ratio * dur;
  }, [dur]);

  const seek = useCallback((tv: number) => {
    setCurrentTime(tv);
    const el = mediaRef.current;
    if (el && Number.isFinite(tv)) el.currentTime = tv;
  }, []);

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      if (anchorRef.current === null) return;
      if (!draggedRef.current && Math.abs(e.clientX - anchorXRef.current) > DRAG_THRESHOLD_PX) {
        draggedRef.current = true;
      }
      if (draggedRef.current) {
        markEdited(); // Component 7: an Actor edit stops the late-metadata markOut clamp.
        const tv = timeFromClientX(e.clientX);
        setMarkIn(Math.min(anchorRef.current!, tv));
        setMarkOut(Math.max(anchorRef.current!, tv));
      }
    };
    const onUp = () => { anchorRef.current = null; draggedRef.current = false; };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => { window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); };
  }, [timeFromClientX]);

  const onRulerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const tv = timeFromClientX(e.clientX);
    anchorRef.current = tv;
    anchorXRef.current = e.clientX;
    draggedRef.current = false;
    seek(tv); // a plain click just seeks; a range forms only on drag
  };

  const captureTime = () => {
    const el = mediaRef.current;
    const tv = el && Number.isFinite(el.currentTime) ? el.currentTime : currentTime;
    return Math.max(0, tv);
  };
  const setInHere = () => { markEdited(); const v = captureTime(); setMarkIn(v); if (markOut !== null && markOut <= v) setMarkOut(null); };
  const setOutHere = () => { markEdited(); const v = captureTime(); if (markIn !== null && v <= markIn) return; setMarkOut(v); };
  const clearMark = () => { markEdited(); setMarkIn(null); setMarkOut(null); };

  const handleMediaRef = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
    registerMedia?.(clip.id, el); // null on unmount — the gallery drops it from its map
    if (!el) return;
    // Play the Moment's [start,end] window (opt-in via playWindow). The timeline is the FULL video and the
    // Actor may scrub anywhere; the window rules are only these (2026-10-11, replacing a confine-to-window
    // that snapped every outside seek back in):
    //   - play while parked at the window END (or before its start) restarts at the window START;
    //   - a playback that starts inside the window stops at its end and parks the playhead there;
    //   - a playback that starts outside the window plays on like any player.
    const parkedAtEnd = (tv: number) => !!playWindow && tv >= playWindow.end - 0.05 && tv <= playWindow.end + 0.25;
    const inWindow = (tv: number) => !!playWindow && tv >= playWindow.start - 0.25 && tv < playWindow.end - 0.05;
    const onWindowPlay = () => {
      if (!playWindow || !Number.isFinite(el.currentTime)) return;
      let tv = el.currentTime;
      if (parkedAtEnd(tv)) { try { el.currentTime = playWindow.start; } catch { /* not seekable */ } tv = playWindow.start; }
      windowArmedRef.current = inWindow(tv);
    };
    const stopAtWindowEnd = () => {
      if (!playWindow || !windowArmedRef.current || !Number.isFinite(el.currentTime)) return;
      if (playWindow.end > playWindow.start && el.currentTime >= playWindow.end) {
        windowArmedRef.current = false;
        if (!el.paused) el.pause();
        try { el.currentTime = playWindow.end; } catch { /* not seekable */ }
      }
    };
    const report = () => { if (Number.isFinite(el.duration) && el.duration > 0) setDuration(el.duration); };
    const onLoadedMeta = () => {
      report();
      // Component 7: first-load seek + clamp (once). A seek past the end is unplayable.
      if (seekOnFirstLoad !== undefined && !didFirstSeekRef.current) {
        didFirstSeekRef.current = true;
        const d = el.duration;
        if (Number.isFinite(d) && d > 0 && seekOnFirstLoad >= d) {
          onError?.('beyond_duration');
        } else {
          if (Number.isFinite(seekOnFirstLoad)) { try { el.currentTime = seekOnFirstLoad; } catch { /* not seekable */ } setCurrentTime(seekOnFirstLoad); }
          // Clamp an over-long initial markOut to the real duration (the lesser of end_ms and the
          // duration), unless the Actor has already edited the marks.
          if (!editedRef.current && Number.isFinite(d) && d > 0) {
            setMarkOut(prev => (prev !== null && prev > d ? d : prev));
          }
        }
      }
      onMediaEvent?.('loadedmetadata', el);
    };
    el.addEventListener('loadedmetadata', onLoadedMeta);
    el.addEventListener('durationchange', report);
    if (playWindow) {
      el.addEventListener('play', onWindowPlay);
      // A scrub OUT of the window (the Actor chose another part of the video) cancels the stop-at-end.
      el.addEventListener('seeking', () => { if (Number.isFinite(el.currentTime) && !inWindow(el.currentTime)) windowArmedRef.current = false; });
    }
    el.addEventListener('play', () => setPaused(false));
    el.addEventListener('pause', () => setPaused(true));
    el.addEventListener('ended', () => setPaused(true));
    el.addEventListener('volumechange', () => setMuted(el.muted));
    el.addEventListener('timeupdate', () => { stopAtWindowEnd(); setCurrentTime(el.currentTime); onMediaEvent?.('timeupdate', el); });
    if (onMediaEvent) {
      el.addEventListener('play', () => onMediaEvent('play', el));
      el.addEventListener('pause', () => onMediaEvent('pause', el));
      el.addEventListener('seeking', () => onMediaEvent('seeking', el));
      el.addEventListener('error', () => onMediaEvent('error', el));
    }
    report();
  };

  const status = String(clip.status || '').trim();
  const flagged = FLAGGED.has(status.toLowerCase());
  const barAccent = flagged ? OUT_COLOR : (accent || t.colors.primary);
  const hasMark = markIn !== null && markOut !== null && markOut > markIn;
  const edited = marksEdited || !!cropBox || showCrop;

  useEffect(() => { onEditedChange?.(clip.id, edited); }, [clip.id, edited, onEditedChange]);

  const toggleCrop = () => setShowCrop(v => { if (!v) cropSnapshotRef.current = cropBox; return !v; });
  const cropPressed = showCrop || !!cropBox;
  const keepText = hasMark ? `${formatTimecode(markIn!)}–${formatTimecode(markOut!)}` : `${formatTimecode(currentTime)} / ${formatTimecode(dur)}`;
  const inLabel = `Set mark-in for ${clip.label} at playhead`;
  const outLabel = `Set mark-out for ${clip.label} at playhead`;

  const smallBtn: React.CSSProperties = {
    display: 'inline-flex', alignItems: 'center', gap: 4,
    padding: '3px 8px', fontSize: t.typography.sizes.badge, borderRadius: t.radius.badge,
    border: `1px solid ${t.colors.borderStrong}`, background: t.colors.surface,
    color: t.colors.textPrimary, cursor: 'pointer', fontFamily: 'inherit', whiteSpace: 'nowrap',
  };
  const disabledSmall: React.CSSProperties = { ...smallBtn, cursor: 'not-allowed', opacity: t.opacity.disabled };

  const cropIndicator = clip.kind === 'video' && cropBox && !showCrop && (
    <div data-testid={`clip-crop-box-${clip.id}`} style={{ position: 'absolute',
      left: `${cropBox.x * 100}%`, top: `${cropBox.y * 100}%`, width: `${cropBox.w * 100}%`, height: `${cropBox.h * 100}%`,
      border: `2px solid ${barAccent}`, boxShadow: '0 0 0 9999px rgba(0,0,0,0.35)', pointerEvents: 'none' }}>
      <button data-testid={`clip-crop-clear-${clip.id}`} onClick={() => onCropChange?.(clip.id, null)}
        title="Clear crop region" aria-label={`Clear crop for ${clip.label}`}
        style={{ position: 'absolute', top: 2, right: 2, width: 16, height: 16, padding: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', borderRadius: t.radius.badge,
          border: 'none', background: t.colors.overlay, color: '#fff', cursor: 'pointer', pointerEvents: 'auto' }}><CloseIcon size={12} /></button>
    </div>
  );
  const cropEditor = clip.kind === 'video' && showCrop && (
    <CropOverlay
      compact
      initialBox={cropBox}
      videoRef={mediaRef}
      theme={t}
      onChange={(box) => onCropChange?.(clip.id, box)}
      onCommit={(box) => { onCropChange?.(clip.id, box); setShowCrop(false); }}
      onCancel={() => { onCropChange?.(clip.id, cropSnapshotRef.current); setShowCrop(false); }}
    />
  );
  const rangeFill = hasMark && (
    <div data-testid={`clip-range-${clip.id}`} style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(markIn!)}%`, width: `${pct(markOut!) - pct(markIn!)}%`, background: t.colors.activeBg, borderLeft: `2px solid ${IN_COLOR}`, borderRight: `2px solid ${OUT_COLOR}` }} />
  );
  const playhead = <div style={{ position: 'absolute', top: 0, bottom: 0, left: `${pct(currentTime)}%`, width: 2, background: t.colors.textPrimary, opacity: 0.8, pointerEvents: 'none' }} />;

  if (embedded) {
    // Moments-mode card: a 16:9 media box (no native controls) over a compact CardTransport row -- play /
    // pause, the FULL-duration track with the Moment's range highlighted, a playhead, time and mute
    // (2026-10-11). Per-card edit affordances stay gone (ISSUE 4d). The media still seeks to the Moment's
    // start, plays its window (see the window rules above), reports media events up, and (via the seeded
    // `initialMark`) reports the Moment's span through `onMarkChange` so Expand / Clip preselect that range.
    const range = playWindow ?? (initialMark ? { start: initialMark.in, end: initialMark.out } : { start: 0, end: dur });
    const togglePlay = () => {
      const el = mediaRef.current;
      if (!el) return;
      if (el.paused) {
        try {
          const p = el.play();
          if (p && typeof (p as Promise<void>).catch === 'function') (p as Promise<void>).catch(() => { /* blocked */ });
        } catch { /* jsdom / blocked playback */ }
      } else {
        el.pause();
      }
    };
    const toggleMute = () => { const el = mediaRef.current; if (el) el.muted = !el.muted; };
    return (
      <article data-testid={`clip-card-${clip.id}`} data-playing={playing ? 'true' : undefined} data-edited={edited ? 'true' : 'false'}
        style={{ display: 'flex', flexDirection: 'column' }}>
        <div data-testid={`clip-media-box-${clip.id}`} style={{ position: 'relative', width: '100%', aspectRatio: '16 / 9', background: '#000', overflow: 'hidden' }}>
          {clip.kind === 'video' ? (
            <video ref={handleMediaRef as (el: HTMLVideoElement | null) => void} src={mediaSrc} preload="metadata" playsInline
              onClick={togglePlay} style={{ width: '100%', height: '100%', display: 'block', objectFit: 'contain', cursor: 'pointer' }} />
          ) : (
            <>
              <div onClick={togglePlay} style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', color: t.colors.textTertiary, background: t.colors.surfaceSubtle, cursor: 'pointer' }}>
                <AudioIcon size={32} />
              </div>
              <audio ref={handleMediaRef as (el: HTMLAudioElement | null) => void} src={mediaSrc} preload="metadata"
                style={{ display: 'none' }} />
            </>
          )}
        </div>
        <CardTransport
          id={clip.id}
          label={clip.label}
          theme={t}
          duration={dur}
          currentTime={currentTime}
          paused={paused}
          muted={muted}
          range={range}
          onTogglePlay={togglePlay}
          onSeek={seek}
          onToggleMute={toggleMute}
        />
      </article>
    );
  }

  const outerStyle: React.CSSProperties = { background: t.colors.surface, border: `1px solid ${playing ? barAccent : t.colors.border}`, borderTop: `4px solid ${barAccent}`, borderRadius: 12, overflow: 'hidden', display: 'flex', flexDirection: 'column', outline: playing ? `2px solid ${barAccent}` : 'none' };
  return (
    <article data-testid={`clip-card-${clip.id}`} data-playing={playing ? 'true' : undefined} data-edited={edited ? 'true' : 'false'} style={outerStyle}>
      <div style={{ padding: '10px 12px 6px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontWeight: 700, fontSize: 13, color: t.colors.textPrimary }}>{clip.label}</span>
          {status && <span data-testid={`clip-status-${clip.id}`} style={{ padding: '1px 8px', borderRadius: 999, background: barAccent, color: '#09090b', fontSize: t.typography.sizes.badge, fontWeight: 700 }}>{status}</span>}
        </div>
        {clip.text && <span style={{ display: 'block', marginTop: 4, color: t.colors.textSecondary, fontSize: 12, lineHeight: 1.35 }}>{clip.text}</span>}
      </div>

      {clip.kind === 'video' ? (
        <div style={{ position: 'relative' }}>
          <video ref={handleMediaRef as (el: HTMLVideoElement | null) => void} src={mediaSrc} controls preload="metadata" playsInline style={{ width: '100%', display: 'block', background: '#000', aspectRatio: '16/9', objectFit: 'contain' }} />
          {/* Persistent indicator of the set crop region (shown when not actively editing). */}
          {cropIndicator}
          {cropEditor}
        </div>
      ) : (
        <div style={{ padding: '0 12px' }}>
          <audio ref={handleMediaRef as (el: HTMLAudioElement | null) => void} src={mediaSrc} controls preload="metadata" style={{ width: '100%', display: 'block' }} />
          {clip.peaks && clip.peaks.length > 0 && dur > 0 && (
            <div style={{ height: 44, overflow: 'hidden', marginTop: 6 }}>
              <WaveformVisualizer peaks={clip.peaks} duration={dur} currentTime={currentTime} zoomLevel={1} scrollOffset={0} onSeek={seek} />
            </div>
          )}
        </div>
      )}

      {/* Mark-in/out ruler for THIS clip's own duration. */}
      <div style={{ padding: '8px 12px 10px', display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div
          ref={rulerRef}
          data-testid={`clip-ruler-${clip.id}`}
          onPointerDown={onRulerPointerDown}
          title="Drag to mark a keep-range · click to move the playhead"
          style={{ position: 'relative', height: 22, background: t.colors.surfaceSubtle ?? t.colors.background, border: `1px solid ${t.colors.border}`, borderRadius: 6, cursor: 'pointer', touchAction: 'none', userSelect: 'none' }}
        >
          {rangeFill}
          {playhead}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span data-testid={`clip-timecode-${clip.id}`} style={{ fontFamily: t.typography.fontMono, fontVariantNumeric: 'tabular-nums', fontSize: t.typography.sizes.badge, color: t.colors.textSecondary }}>
            {hasMark ? `keep ${keepText}` : keepText}
          </span>
          <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
            {clip.kind === 'video' && onExpand && !hideExpand && (
              <button data-testid={`clip-expand-${clip.id}`} onClick={() => onExpand(clip)} style={smallBtn} title="Open this clip in its own timeline tab for frame-accurate analysis" aria-label={`Expand ${clip.label} to its own timeline`}><ExpandIcon size={12} />Expand</button>
            )}
            {clip.kind === 'video' && (
              <button data-testid={`clip-crop-${clip.id}`} onClick={toggleCrop} aria-pressed={cropPressed}
                style={{ ...smallBtn, ...(cropBox ? { background: t.colors.primarySubtle, color: t.colors.primary, borderColor: t.colors.primary } : {}) }}
                title={cropBox ? 'Crop region set — click to adjust' : 'Crop this clip to a region'} aria-label={`Crop ${clip.label}`}>
                <CropIcon size={12} />Crop
              </button>
            )}
            <button data-testid={`clip-set-in-${clip.id}`} onClick={setInHere} style={smallBtn} title="Set the keep-range start at the playhead" aria-label={inLabel}><MarkInIcon size={12} />In</button>
            <button data-testid={`clip-set-out-${clip.id}`} onClick={setOutHere} style={smallBtn} title="Set the keep-range end at the playhead" aria-label={outLabel}>Out<MarkOutIcon size={12} /></button>
            <button data-testid={`clip-clear-${clip.id}`} onClick={clearMark} disabled={!hasMark} style={hasMark ? smallBtn : disabledSmall} title="Clear this clip's mark" aria-label={`Clear mark for ${clip.label}`}><CloseIcon size={12} /></button>
          </div>
        </div>
      </div>
    </article>
  );
}
