/**
 * `graph-view` — the agent's "↳ Graph" result tab (render/graph_view.py; the knowledge-graph extension of the
 * Moments gallery): every node and relationship the graph tools touched this turn, drawn with the same Sigma
 * canvas as the Projects Graph tab. Focus nodes (what the question was about) are emphasised; "Open in project
 * graph" switches to the Projects Graph tab with them highlighted in the whole workspace graph.
 *
 * The manifest carries ids, types and names only — no URL. Sigma/graphology load lazily (the same chunk as the
 * Graph tab), so they stay out of the main bundle.
 */
import React, { Suspense, lazy, useMemo } from 'react';
import type { RendererProps } from '../types';
import { theme } from '../../../styles/tokens';
import { NODE_TYPES } from '../../projects/graph/graphTheme';
import { openProjectGraph, overviewFromResult } from '../../projects/graph/graphResult';
import type { GraphResultNode } from '../../projects/graph/graphResult';

const GraphCanvas = lazy(() => import('../../projects/graph/GraphCanvas'));

export interface GraphViewData {
  project_id?: string;
  title?: string;
  truncated?: boolean;
  nodes?: GraphResultNode[];
  edges?: [string, string, string][];
}

export default function GraphView({ data }: RendererProps<GraphViewData>) {
  const nodes = Array.isArray(data?.nodes) ? data.nodes : [];
  const edges = Array.isArray(data?.edges) ? data.edges : [];
  const projectId = typeof data?.project_id === 'string' ? data.project_id : '';
  const focusIds = useMemo(() => nodes.filter((n) => n.focus).map((n) => n.id), [nodes]);
  const overview = useMemo(() => overviewFromResult(nodes, edges, NODE_TYPES), [nodes, edges]);
  const byType = useMemo(() => {
    const m = new Map<string, number>();
    for (const n of nodes) m.set(n.type, (m.get(n.type) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [nodes]);

  if (!nodes.length || !projectId) {
    return <p style={emptyStyle} data-testid="graph-view-empty">No graph result to show.</p>;
  }
  const openButton = (
    <button type="button" style={openStyle} data-testid="graph-view-open-project"
      onClick={() => openProjectGraph(projectId, focusIds.length ? focusIds : nodes.slice(0, 20).map((n) => n.id))}>
      Open in project graph
    </button>
  );
  return (
    <div style={wrapStyle} data-testid="graph-view">
      <div style={summaryStyle}>
        {nodes.length} nodes · {edges.length} relationships
        {focusIds.length > 0 && ` · ${focusIds.length} in focus`}
        {byType.length > 0 && ` · ${byType.map(([t, n]) => `${n} ${t}`).join(', ')}`}
        {data.truncated && ' · showing part of the result'}
      </div>
      <div style={canvasStyle}>
        <Suspense fallback={<p style={emptyStyle}>Loading the graph viewer…</p>}>
          <GraphCanvas projectId={projectId} overview={overview} focusIds={focusIds} dimOthers={false}
            toolbarExtra={openButton} />
        </Suspense>
      </div>
    </div>
  );
}

const wrapStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 420 };
const summaryStyle: React.CSSProperties = {
  padding: '6px 12px', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary,
  borderBottom: `1px solid ${theme.colors.border}`,
};
const canvasStyle: React.CSSProperties = { flex: 1, minHeight: 0, display: 'flex' };
const emptyStyle: React.CSSProperties = { margin: 0, padding: 16, color: theme.colors.textSecondary, fontSize: 'var(--fs-sm)' };
const openStyle: React.CSSProperties = {
  pointerEvents: 'auto', padding: '5px 10px', fontSize: 'var(--fs-xs)', color: theme.colors.onPrimary,
  background: theme.colors.primary, border: 'none', borderRadius: theme.radius.button, cursor: 'pointer',
};
