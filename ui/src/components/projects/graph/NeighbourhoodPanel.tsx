/**
 * The Graph tab's selection panel: the clicked node's properties and its neighbours grouped by relationship,
 * with "Expand to 2 hops". Data comes from `GET /projects/:pid/graph/nodes/:nodeId` (lance-graph Cypher on
 * the reader Lambda).
 */
import React from 'react';
import { theme } from '../../../styles/tokens';
import Button from '../../ui/Button';
import type { GraphNeighbourhood, GraphNode } from '../../../projects/api';
import { relationshipCounts } from './graphModel';

export interface NeighbourhoodPanelProps {
  data: GraphNeighbourhood | null;
  loading: boolean;
  error: string | null;
  color: (type: string) => string;
  onExpand(): void;
  onSelect(nodeId: string): void;
  onClose(): void;
}

const HIDDEN_PROPS = new Set(['assetId']);

function propRows(node: GraphNode): [string, string][] {
  const rows: [string, string][] = [];
  if (node.kind) rows.push(['kind', node.kind]);
  if (node.status && node.status !== 'sample') rows.push(['status', node.status]);
  for (const [k, v] of Object.entries(node.props ?? {})) {
    if (!HIDDEN_PROPS.has(k) && v !== null && v !== undefined && v !== '') rows.push([k, String(v)]);
  }
  return rows;
}

export default function NeighbourhoodPanel({ data, loading, error, color, onExpand, onSelect, onClose }: NeighbourhoodPanelProps) {
  const node = data?.node;
  const byId = new Map((data?.nodes ?? []).map((n) => [n.id, n]));
  const neighbours = (data?.nodes ?? []).filter((n) => n.id !== node?.id).slice(0, 60);
  return (
    <aside style={panelStyle} aria-label="Selected node" data-testid="graph-selection">
      <div style={headRowStyle}>
        <span style={eyebrowStyle}>Selection</span>
        <button type="button" onClick={onClose} style={closeStyle} aria-label="Clear selection">×</button>
      </div>
      {loading && !node && <p style={mutedStyle}>Loading…</p>}
      {error && <p style={errorStyle} role="alert">{error}</p>}
      {node && (
        <>
          <div style={titleRowStyle}>
            <span style={{ ...chipStyle, background: color(node.type) }} />
            <span style={typeStyle}>{node.type}{node.sample ? ' · sample' : ''}</span>
          </div>
          <div style={nameStyle}>{node.name}</div>
          {propRows(node).length > 0 && (
            <table style={tableStyle}>
              <tbody>
                {propRows(node).map(([k, v]) => (
                  <tr key={k}><td style={keyCellStyle}>{k}</td><td style={valCellStyle}>{v}</td></tr>
                ))}
              </tbody>
            </table>
          )}
          <div style={relRowStyle}>
            {relationshipCounts(data!).map(([rel, n]) => (
              <span key={rel} style={relChipStyle}>{rel} {n}</span>
            ))}
          </div>
          {(data?.hops ?? 1) < 2 && (
            <Button variant="secondary" size="sm" onClick={onExpand} disabled={loading}>Expand to 2 hops</Button>
          )}
          {data?.truncated && <p style={mutedStyle}>Showing the first {data.edges?.length} connections.</p>}
          <ul style={listStyle} aria-label="Neighbours">
            {neighbours.map((n) => (
              <li key={n.id}>
                <button type="button" style={neighbourStyle} onClick={() => onSelect(n.id)} title={n.id}>
                  <span style={{ ...dotStyle, background: color(byId.get(n.id)?.type ?? n.type) }} />
                  <span style={neighbourNameStyle}>{n.name}</span>
                  <span style={mutedInlineStyle}>{n.type}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </aside>
  );
}

const panelStyle: React.CSSProperties = {
  width: 300, flexShrink: 0, borderLeft: `1px solid ${theme.colors.border}`, background: theme.colors.surface,
  padding: 14, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0,
};
const headRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', justifyContent: 'space-between' };
const eyebrowStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', textTransform: 'uppercase', letterSpacing: '0.06em', color: theme.colors.textSecondary,
};
const closeStyle: React.CSSProperties = {
  background: 'none', border: 'none', color: theme.colors.textSecondary, cursor: 'pointer', fontSize: 18, lineHeight: 1,
};
const titleRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 6 };
const chipStyle: React.CSSProperties = { width: 10, height: 10, borderRadius: '50%', display: 'inline-block' };
const typeStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const nameStyle: React.CSSProperties = {
  fontSize: 'var(--fs-md)', fontWeight: 600, color: theme.colors.textPrimary, overflowWrap: 'anywhere',
};
const tableStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', borderCollapse: 'collapse' };
const keyCellStyle: React.CSSProperties = { color: theme.colors.textSecondary, padding: '1px 8px 1px 0', verticalAlign: 'top' };
const valCellStyle: React.CSSProperties = { color: theme.colors.textPrimary, overflowWrap: 'anywhere' };
const relRowStyle: React.CSSProperties = { display: 'flex', flexWrap: 'wrap', gap: 4 };
const relChipStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)', padding: '1px 7px', borderRadius: 10, background: theme.colors.surfaceRaised,
  color: theme.colors.textSecondary, border: `1px solid ${theme.colors.border}`,
};
const listStyle: React.CSSProperties = { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column' };
const neighbourStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left', background: 'none', border: 'none',
  padding: '3px 2px', cursor: 'pointer', color: theme.colors.textPrimary, fontSize: 'var(--fs-sm)',
};
const dotStyle: React.CSSProperties = { width: 8, height: 8, borderRadius: '50%', flexShrink: 0 };
const neighbourNameStyle: React.CSSProperties = { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const mutedInlineStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary, flexShrink: 0 };
const mutedStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const errorStyle: React.CSSProperties = { margin: 0, fontSize: 'var(--fs-sm)', color: theme.colors.error };
