import React from 'react';
import type { ManifestSegment } from './manifestParser';

interface SegmentLanesProps {
  segments: ManifestSegment[];
  duration: number;
  currentTime: number;
  onSeek: (time: number) => void;
}

/** Exported for testing: bucket segments into one lane per `group`, ordered by first appearance. */
export function groupIntoLanes(
  segments: ManifestSegment[],
): { name: string; segments: ManifestSegment[] }[] {
  const lanes = new Map<string, ManifestSegment[]>();
  for (const s of segments) {
    const key = typeof s.group === 'string' && s.group ? s.group : 'Segments';
    const bucket = lanes.get(key);
    if (bucket) bucket.push(s);
    else lanes.set(key, [s]);
  }
  return Array.from(lanes, ([name, segs]) => ({
    name,
    segments: [...segs].sort((a, b) => a.start - b.start),
  }));
}

/** Deterministic per-lane colour so a speaker keeps the same colour across renders. */
export function laneColor(name: string, index: number): string {
  const hues = [205, 145, 35, 280, 0, 175, 60, 320];
  return `hsl(${hues[index % hues.length]}, 62%, 52%)`;
}

/**
 * SegmentLanes — one horizontal row per group, each segment a block placed by time.
 *
 * This is the view that answers "who spoke when": diarization output rendered as a
 * flat list collapses every speaker into one row, which is precisely the
 * information diarization exists to produce. Grouping is driven by the segment's
 * `group` field, so any tool that labels its spans (speakers, faces, scenes) gets
 * lanes without new code here.
 *
 * Clicking a block seeks the shared playhead; the block containing `currentTime`
 * is highlighted.
 */
export default function SegmentLanes({ segments, duration, currentTime, onSeek }: SegmentLanesProps) {
  const lanes = React.useMemo(() => groupIntoLanes(segments), [segments]);
  // Fall back to the last segment's end so blocks still lay out when the manifest
  // reports no duration; never divide by zero.
  const span = duration > 0
    ? duration
    : Math.max(...segments.map((s) => s.end), 1);

  if (lanes.length === 0) return null;

  return (
    <div style={{ padding: '8px 12px' }}>
      <span style={label}>
        {lanes.length > 1 ? `${lanes.length} lanes` : 'Timeline'} ({segments.length} segments)
      </span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 6 }}>
        {lanes.map((lane, li) => {
          const color = laneColor(lane.name, li);
          const total = lane.segments.reduce((n, s) => n + (s.end - s.start), 0);
          return (
            <div key={lane.name} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={laneLabel} title={`${lane.name} — ${total.toFixed(1)}s across ${lane.segments.length} segments`}>
                <span style={{ ...swatch, background: color }} aria-hidden="true" />
                <span style={laneName}>{lane.name}</span>
              </div>
              <div
                style={laneTrack}
                role="group"
                aria-label={`${lane.name}: ${lane.segments.length} segments totalling ${total.toFixed(1)} seconds`}
              >
                {lane.segments.map((s, i) => {
                  const active = currentTime >= s.start && currentTime < s.end;
                  const left = Math.max(0, Math.min(100, (s.start / span) * 100));
                  // Keep very short turns clickable rather than sub-pixel.
                  const width = Math.max(0.6, Math.min(100 - left, ((s.end - s.start) / span) * 100));
                  return (
                    <button
                      key={s.id || i}
                      type="button"
                      onClick={() => onSeek(s.start)}
                      title={`${lane.name} ${s.start.toFixed(3)}s–${s.end.toFixed(3)}s`}
                      aria-label={`${lane.name} from ${s.start.toFixed(1)} to ${s.end.toFixed(1)} seconds`}
                      style={{
                        position: 'absolute',
                        left: `${left}%`,
                        width: `${width}%`,
                        top: 3,
                        bottom: 3,
                        background: color,
                        opacity: active ? 1 : 0.72,
                        outline: active ? '2px solid var(--color-text)' : 'none',
                        border: 'none',
                        borderRadius: 3,
                        padding: 0,
                        cursor: 'pointer',
                      }}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

const label: React.CSSProperties = { fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--color-text-secondary)' };
const laneLabel: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 5, width: 108, flexShrink: 0, overflow: 'hidden' };
const laneName: React.CSSProperties = { fontSize: 11, color: 'var(--color-text)', whiteSpace: 'nowrap', textOverflow: 'ellipsis', overflow: 'hidden' };
const swatch: React.CSSProperties = { width: 8, height: 8, borderRadius: 2, flexShrink: 0 };
const laneTrack: React.CSSProperties = { position: 'relative', flex: 1, height: 24, background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 4 };
