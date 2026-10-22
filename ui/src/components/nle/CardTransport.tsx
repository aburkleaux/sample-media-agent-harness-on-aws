/**
 * CardTransport — the compact play / scrub row under a Moment card's media (2026-10-11).
 *
 * Replaces the browser's native `controls` on the card player, whose scrubber spanned the whole video
 * with no sign of WHERE the moment is. The track is the FULL duration (so the moment sits in context)
 * with the moment's range drawn as a solid segment with end ticks, a playhead, a tabular time label and
 * a mute toggle. Playback rules live in the owner (ClipEditor embedded): this row only renders state and
 * reports intent (`onTogglePlay`, `onSeek`, `onToggleMute`).
 *
 * Keyboard: the track is a `slider` — ←/→ step 1 s (Shift: 5 s), Home / End jump to the moment's
 * start / end.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';

import PlayIcon from '../icons/PlayIcon';
import PauseIcon from '../icons/PauseIcon';
import VolumeIcon from '../icons/VolumeIcon';
import MuteIcon from '../icons/MuteIcon';
import type { ThemeTokens } from '../renderer/types';

/** Height of the transport row; the Moment card's other bodies reserve the same slot. */
export const CARD_TRANSPORT_HEIGHT = 30;

/** `m:ss` under an hour, `h:mm:ss` above — compact enough for a 250 px card. */
export function shortTime(seconds: number): string {
  const s = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

export interface CardTransportProps {
  id: string;
  label: string;
  theme: ThemeTokens;
  /** The media's duration in seconds; 0 while unknown (the owner falls back to the range end). */
  duration: number;
  currentTime: number;
  paused: boolean;
  muted: boolean;
  /** The moment, in seconds. */
  range: { start: number; end: number };
  onTogglePlay: () => void;
  onSeek: (seconds: number) => void;
  onToggleMute: () => void;
}

export default function CardTransport({
  id, label, theme: t, duration, currentTime, paused, muted, range, onTogglePlay, onSeek, onToggleMute,
}: CardTransportProps): React.ReactElement {
  const trackRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [hot, setHot] = useState(false); // hover / focus / drag: thicker track + knob
  const dur = duration > 0 ? duration : Math.max(range.end, 0.001);
  const pct = (s: number) => Math.max(0, Math.min(100, (s / dur) * 100));
  const startPct = pct(range.start);
  const widthPct = Math.max(0, pct(range.end) - startPct);

  const timeAt = useCallback((clientX: number) => {
    const el = trackRef.current;
    if (!el) return 0;
    const r = el.getBoundingClientRect();
    if (r.width <= 0) return 0;
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * dur;
  }, [dur]);

  useEffect(() => {
    const move = (e: PointerEvent) => { if (draggingRef.current) onSeek(timeAt(e.clientX)); };
    const up = () => { draggingRef.current = false; };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
  }, [onSeek, timeAt]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 5 : 1;
    let next: number | null = null;
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = currentTime + step;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = currentTime - step;
    else if (e.key === 'Home') next = range.start;
    else if (e.key === 'End') next = range.end;
    if (next === null) return;
    e.preventDefault();
    onSeek(Math.max(0, Math.min(dur, next)));
  };

  const iconBtn: React.CSSProperties = {
    width: 22, height: 22, flexShrink: 0, padding: 0, border: 'none', background: 'transparent',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', borderRadius: t.radius.button,
    color: t.colors.textSecondary, cursor: 'pointer', fontFamily: 'inherit',
  };
  const trackH = hot ? 5 : 3;
  const rangeName = `${shortTime(range.start)}–${shortTime(range.end)}`;

  return (
    <div
      data-testid={`card-transport-${id}`}
      style={{
        height: CARD_TRANSPORT_HEIGHT, display: 'flex', alignItems: 'center', gap: 6, padding: '0 6px 0 4px',
        boxSizing: 'border-box', background: t.colors.surface, borderBottom: `1px solid ${t.colors.border}`,
      }}
    >
      <button
        type="button"
        data-testid={`card-play-${id}`}
        onClick={onTogglePlay}
        aria-label={paused ? `Play ${label} ${rangeName}` : `Pause ${label}`}
        title={paused ? `Play the moment (${rangeName})` : 'Pause'}
        style={{ ...iconBtn, color: t.colors.textPrimary }}
      >
        {paused ? <PlayIcon size={14} /> : <PauseIcon size={14} />}
      </button>

      <div
        ref={trackRef}
        role="slider"
        tabIndex={0}
        data-testid={`card-track-${id}`}
        aria-label={`Seek ${label}`}
        aria-valuemin={0}
        aria-valuemax={Math.round(dur)}
        aria-valuenow={Math.round(currentTime)}
        aria-valuetext={`${shortTime(currentTime)} of ${shortTime(dur)}; moment ${rangeName}`}
        onPointerDown={(e) => { draggingRef.current = true; onSeek(timeAt(e.clientX)); }}
        onKeyDown={onKeyDown}
        onPointerEnter={() => setHot(true)}
        onPointerLeave={() => { if (!draggingRef.current) setHot(false); }}
        onFocus={() => setHot(true)}
        onBlur={() => setHot(false)}
        style={{ position: 'relative', flex: 1, minWidth: 40, height: 18, cursor: 'pointer', outline: 'none', touchAction: 'none' }}
      >
        {/* Full-duration rail. */}
        <div style={{
          position: 'absolute', left: 0, right: 0, top: '50%', height: trackH, transform: 'translateY(-50%)',
          borderRadius: 3, background: t.colors.borderStrong, transition: 'height 120ms ease',
        }} />
        {/* The moment: a solid segment with crisp end ticks, so it reads as a RANGE, not a buffer bar. */}
        <div
          data-testid={`card-range-${id}`}
          data-left={startPct.toFixed(3)}
          data-width={widthPct.toFixed(3)}
          style={{
            position: 'absolute', top: '50%', height: trackH + 4, transform: 'translateY(-50%)',
            left: `${startPct}%`, width: `max(${widthPct}%, 3px)`,
            background: `color-mix(in srgb, ${t.colors.primary} 70%, transparent)`,
            borderLeft: `1.5px solid ${t.colors.primary}`, borderRight: `1.5px solid ${t.colors.primary}`,
            borderRadius: 1, boxSizing: 'border-box', transition: 'height 120ms ease', pointerEvents: 'none',
          }}
        />
        {/* Playhead: a bright hairline, with a knob while the track is hot. */}
        <div data-testid={`card-playhead-${id}`} style={{
          position: 'absolute', top: '50%', left: `${pct(currentTime)}%`, transform: 'translate(-50%, -50%)',
          width: hot ? 9 : 2, height: hot ? 9 : 11, borderRadius: hot ? '50%' : 1,
          background: t.colors.textPrimary, boxShadow: hot ? `0 0 0 3px color-mix(in srgb, ${t.colors.textPrimary} 18%, transparent)` : 'none',
          transition: 'width 120ms ease, height 120ms ease, border-radius 120ms ease', pointerEvents: 'none',
        }} />
      </div>

      <span
        data-testid={`card-time-${id}`}
        aria-hidden="true"
        style={{
          flexShrink: 0, minWidth: 30, textAlign: 'right', fontFamily: t.typography.fontMono,
          fontVariantNumeric: 'tabular-nums', fontSize: t.typography.sizes.badge, color: t.colors.textTertiary,
        }}
      >
        {shortTime(currentTime)}
      </span>

      <button
        type="button"
        data-testid={`card-mute-${id}`}
        onClick={onToggleMute}
        aria-label={muted ? `Unmute ${label}` : `Mute ${label}`}
        aria-pressed={muted}
        title={muted ? 'Unmute' : 'Mute'}
        style={iconBtn}
      >
        {muted ? <MuteIcon size={14} /> : <VolumeIcon size={14} />}
      </button>
    </div>
  );
}
