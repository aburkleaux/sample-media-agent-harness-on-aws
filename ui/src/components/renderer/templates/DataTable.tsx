import React, { useState, useMemo, useEffect } from 'react';
import type { RendererProps } from '../types';
import { authFetch } from '../../../auth/authFetch';

interface Column { key: string; label: string; sortable?: boolean; }
interface DataTableData { src?: string; columns: Column[]; rows: Record<string, unknown>[]; }

function deriveColumnsAndRows(json: unknown): { columns: Column[]; rows: Record<string, unknown>[] } {
  if (Array.isArray(json) && json.length > 0 && typeof json[0] === 'object') {
    const keys = Object.keys(json[0] as object);
    return {
      columns: keys.map(k => ({ key: k, label: k, sortable: true })),
      rows: json as Record<string, unknown>[],
    };
  }
  if (typeof json === 'object' && json !== null && !Array.isArray(json)) {
    const entries = Object.entries(json as Record<string, unknown>);
    return {
      columns: [{ key: 'key', label: 'Key', sortable: true }, { key: 'value', label: 'Value', sortable: true }],
      rows: entries.map(([k, v]) => ({ key: k, value: typeof v === 'object' ? JSON.stringify(v) : String(v ?? '') })),
    };
  }
  return { columns: [], rows: [] };
}

export default function DataTable({ data, theme: t, onInteraction }: RendererProps<DataTableData>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortAsc, setSortAsc] = useState(true);
  const [fetched, setFetched] = useState<{ columns: Column[]; rows: Record<string, unknown>[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (data.src && data.columns.length === 0) {
      const src = data.src.includes('cloudfront.net/')
        ? `/api/artifact?key=${encodeURIComponent(data.src.replace(/https:\/\/[\w\-]+\.cloudfront\.net\//, ''))}`
        : data.src;
      // `/api/artifact` is a protected proxy path (R2). For absolute
      // CloudFront URLs, `authFetch` is a no-op pass-through, so this is
      // safe in both cases.
      authFetch(src)
        .then(res => { if (!res.ok) throw new Error(`${res.status}`); return res.json(); })
        .then(json => setFetched(deriveColumnsAndRows(json)))
        .catch(err => setError(err.message));
    }
  }, [data.src, data.columns.length]);

  const columns = fetched?.columns ?? data.columns;
  const rows = fetched?.rows ?? data.rows;

  const sortedRows = useMemo(() => {
    if (!sortKey) return rows;
    return [...rows].sort((a, b) => {
      const av = String(a[sortKey] ?? '');
      const bv = String(b[sortKey] ?? '');
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
  }, [rows, sortKey, sortAsc]);

  const toggleSort = (key: string) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(true); }
  };

  if (error) {
    return <div style={{ padding: 16, color: t.colors.textSecondary, fontSize: 12 }}>Failed to load data: {error}</div>;
  }

  if (columns.length === 0) {
    return <div style={{ padding: 16, color: t.colors.textSecondary, fontSize: 12 }}>Loading...</div>;
  }

  return (
    <div style={{ border: `1px solid ${t.colors.border}`, borderRadius: t.radius.panel, overflow: 'hidden' }}>
      <div style={{ overflowX: 'auto', maxHeight: 300, overflowY: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: t.colors.background }}>
              {columns.map(col => (
                <th key={col.key}
                  onClick={() => col.sortable && toggleSort(col.key)}
                  style={{ padding: '8px 12px', textAlign: 'left', color: t.colors.textSecondary, fontWeight: t.typography.weights.medium, borderBottom: `1px solid ${t.colors.border}`, cursor: col.sortable ? 'pointer' : 'default', whiteSpace: 'nowrap', userSelect: 'none' }}>
                  {col.label} {sortKey === col.key ? (sortAsc ? '↑' : '↓') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, idx) => (
              <tr key={idx} onClick={() => onInteraction?.({ type: 'select', target: `row-${idx}`, data: row, timestamp: Date.now() })}
                style={{ cursor: 'pointer', background: idx % 2 === 0 ? t.colors.surface : t.colors.background }}>
                {columns.map(col => (
                  <td key={col.key} style={{ padding: '8px 12px', color: t.colors.textPrimary, borderBottom: `1px solid ${t.colors.border}` }}>
                    {String(row[col.key] ?? '')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
