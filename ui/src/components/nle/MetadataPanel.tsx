import React from 'react';

interface MetadataPanelProps {
  metadata: Record<string, string | number | boolean>;
}

function humanize(key: string): string {
  return key.replace(/[_-]/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * MetadataPanel — key/value display of structured artifact metadata.
 * Data-driven: shown only when the manifest has a non-empty `metadata`.
 */
export default function MetadataPanel({ metadata }: MetadataPanelProps) {
  const entries = Object.entries(metadata || {});
  if (entries.length === 0) return null;
  return (
    <div style={{ padding: '8px 12px' }}>
      <span style={label}>Metadata</span>
      <div style={grid}>
        {entries.map(([k, v]) => (
          <div key={k} style={cell}>
            <div style={keyStyle}>{humanize(k)}</div>
            <div style={valStyle}>{String(v)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

const label: React.CSSProperties = { fontSize: 11, fontWeight: 600, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.4 };
const grid: React.CSSProperties = { marginTop: 6, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 8 };
const cell: React.CSSProperties = { background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 6, padding: '6px 10px' };
const keyStyle: React.CSSProperties = { fontSize: 10, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.3, marginBottom: 2 };
const valStyle: React.CSSProperties = { fontSize: 13, color: 'var(--color-text)', wordBreak: 'break-word' };
