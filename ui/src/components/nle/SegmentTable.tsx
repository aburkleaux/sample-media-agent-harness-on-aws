import React from 'react';
import type { ManifestSegment } from './manifestParser';

interface SegmentTableProps {
  segments: ManifestSegment[];
  currentTime: number;
  onSeek: (time: number) => void;
}

function tc(s: number): string {
  const m = Math.floor(s / 60);
  const sec = (s % 60);
  return `${String(m).padStart(2, '0')}:${sec.toFixed(3).padStart(6, '0')}`;
}

/**
 * SegmentTable — interactive list of timestamped segments. Clicking a row
 * seeks the shared playhead; the active row (containing currentTime) is
 * highlighted. Data-driven: shown only when the manifest has `segments`.
 */
export default function SegmentTable({ segments, currentTime, onSeek }: SegmentTableProps) {
  // Collect any extra field keys for columns.
  const extraKeys = Array.from(
    segments.reduce((set, s) => {
      Object.keys(s.fields || {}).forEach((k) => set.add(k));
      return set;
    }, new Set<string>()),
  ).slice(0, 3);

  return (
    <div style={{ padding: '8px 12px' }}>
      <span style={label}>Segments ({segments.length})</span>
      <div style={tableWrap}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr>
              <th style={th}>#</th>
              <th style={{ ...th, textAlign: 'left' }}>Name</th>
              <th style={th}>Start</th>
              <th style={th}>End</th>
              <th style={th}>Dur</th>
              {extraKeys.map((k) => <th key={k} style={{ ...th, textAlign: 'left' }}>{k}</th>)}
            </tr>
          </thead>
          <tbody>
            {segments.map((s, i) => {
              const active = currentTime >= s.start && currentTime < s.end;
              return (
                <tr
                  key={s.id || i}
                  onClick={() => onSeek(s.start)}
                  style={{ ...row, background: active ? 'var(--color-active-bg)' : 'transparent', cursor: 'pointer' }}
                  title={`Seek to ${tc(s.start)}`}
                >
                  <td style={{ ...td, color: 'var(--color-text-secondary)' }}>{i + 1}</td>
                  <td style={{ ...td, textAlign: 'left', color: 'var(--color-text)' }}>{s.name}</td>
                  <td style={tdMono}>{tc(s.start)}</td>
                  <td style={tdMono}>{tc(s.end)}</td>
                  <td style={tdMono}>{(s.end - s.start).toFixed(1)}s</td>
                  {extraKeys.map((k) => <td key={k} style={{ ...td, textAlign: 'left' }}>{String(s.fields?.[k] ?? '')}</td>)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const label: React.CSSProperties = { fontSize: 11, fontWeight: 600, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.4 };
const tableWrap: React.CSSProperties = { marginTop: 6, maxHeight: 220, overflowY: 'auto', border: '1px solid var(--color-border)', borderRadius: 6 };
const th: React.CSSProperties = { position: 'sticky', top: 0, background: 'var(--color-surface)', color: 'var(--color-text-secondary)', fontWeight: 600, fontSize: 10, textAlign: 'center', padding: '6px 8px', textTransform: 'uppercase', letterSpacing: 0.3 };
const row: React.CSSProperties = { borderTop: '1px solid var(--color-border)' };
const td: React.CSSProperties = { padding: '6px 8px', textAlign: 'center', color: 'var(--color-text-secondary)' };
const tdMono: React.CSSProperties = { ...td, fontFamily: 'ui-monospace, monospace', color: 'var(--color-primary)' };
