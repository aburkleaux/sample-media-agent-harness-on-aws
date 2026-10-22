import React, { useMemo } from 'react';
import { theme } from '../styles/tokens';
import type { Job } from '../types/jobs';
import { extractToolName, isActive, isTerminalFailure, isTerminalSuccess, statusColor, statusLabel, formatElapsed } from '../utils/jobStatus';

/**
 * PlanDagView — renders a run's jobs as the actual execution DAG
 * (nodes positioned by dependency depth, edges drawn between
 * dependent nodes) instead of a flat, timestamp-ordered list.
 *
 * Layout is a simple longest-path layering: a node's column is
 * `1 + max(column of its dependencies)`, with roots (no dependencies)
 * in column 0. Pixel positions are derived analytically from
 * (column, row-within-column) so no DOM measurement is needed to
 * draw edges — the SVG overlay and the node `<div>`s share the same
 * coordinate math.
 *
 * Requires `job.nodeId` to be populated (added alongside `dependsOn`
 * so the Activity Panel can reconstruct the plan's dependency graph —
 * see `agent/src/jobs/models.py`). Jobs from older records or
 * non-plan runs won't have it; callers should fall back to the flat
 * list in that case (see `ActivityPanel.tsx`).
 */

interface PlanDagViewProps {
  jobs: Job[];
  selectedJobId?: string | null;
  onSelectNode?: (jobId: string) => void;
}

interface LayoutNode {
  /** Null for the synthetic Start node, which represents the request itself. */
  job: Job | null;
  nodeId: string;
  /** Display label. For real nodes this is the nodeId; Start uses "Start". */
  label: string;
  /** True for the presentational Start node (no work, no job row). */
  synthetic?: boolean;
  col: number;
  row: number;
  x: number;
  y: number;
}

interface LayoutEdge {
  from: LayoutNode;
  to: LayoutNode;
}

const NODE_WIDTH = 176;
const NODE_HEIGHT = 60;
const COL_WIDTH = 224;
const ROW_HEIGHT = 80;
const PAD_X = 20;
const PAD_Y = 20;

/**
 * Pure layout function — exported for unit testing. Returns `null`
 * when no job in `jobs` carries a `nodeId` (nothing to lay out as a
 * graph; caller should fall back to a flat list).
 */
export function computeDagLayout(jobs: Job[]): { nodes: LayoutNode[]; edges: LayoutEdge[]; width: number; height: number } | null {
  const withNodeId = jobs.filter((j) => !!j.nodeId);
  if (withNodeId.length === 0) return null;

  const byNodeId = new Map<string, Job>();
  for (const job of withNodeId) {
    if (job.nodeId) byNodeId.set(job.nodeId, job);
  }

  // Depth = longest-path distance from a root (no in-set dependencies).
  // Cycle guard: a node currently being resolved that's revisited
  // (shouldn't happen for a valid DAG) falls back to depth 0 rather
  // than recursing forever.
  const depthCache = new Map<string, number>();
  const resolving = new Set<string>();

  function depthOf(nodeId: string): number {
    const cached = depthCache.get(nodeId);
    if (cached !== undefined) return cached;
    if (resolving.has(nodeId)) return 0;
    resolving.add(nodeId);

    const job = byNodeId.get(nodeId);
    const deps = (job?.dependsOn ?? []).filter((d) => byNodeId.has(d));
    const depth = deps.length === 0 ? 0 : 1 + Math.max(...deps.map(depthOf));

    resolving.delete(nodeId);
    depthCache.set(nodeId, depth);
    return depth;
  }

  const nodeIds = Array.from(byNodeId.keys()).sort();
  for (const id of nodeIds) depthOf(id);

  // Group into columns, sorted for stable layout across re-renders.
  const columns = new Map<number, string[]>();
  for (const id of nodeIds) {
    const d = depthCache.get(id) ?? 0;
    const col = columns.get(d) ?? [];
    col.push(id);
    columns.set(d, col);
  }

  const nodesById = new Map<string, LayoutNode>();
  const nodes: LayoutNode[] = [];
  const maxCol = Math.max(...Array.from(columns.keys()));
  let maxRows = 1;

  for (const [col, ids] of columns.entries()) {
    ids.sort();
    maxRows = Math.max(maxRows, ids.length);
    ids.forEach((id, row) => {
      const job = byNodeId.get(id)!;
      const node: LayoutNode = {
        job,
        nodeId: id,
        label: id,
        col,
        row,
        x: PAD_X + col * COL_WIDTH,
        y: PAD_Y + row * ROW_HEIGHT,
      };
      nodesById.set(id, node);
      nodes.push(node);
    });
  }

  const edges: LayoutEdge[] = [];
  for (const node of nodes) {
    for (const dep of node.job?.dependsOn ?? []) {
      const from = nodesById.get(dep);
      if (from) edges.push({ from, to: node });
    }
  }

  // A request that fans out to several independent nodes would otherwise draw as
  // N disconnected boxes, which reads as unrelated jobs rather than one unit of
  // work. Prepend a synthetic "Start" node — purely presentational: no job row,
  // no execution, no notification — so the graph is visibly ONE request fanning
  // out. Skipped when there is a single root, since that already reads as a
  // chain (e.g. trim -> analyse).
  const roots = nodes.filter((n) => (n.job?.dependsOn ?? []).filter((d) => byNodeId.has(d)).length === 0);
  let allNodes = nodes;
  let colOffset = 0;
  if (roots.length > 1) {
    colOffset = 1;
    for (const n of nodes) {
      n.col += 1;
      n.x = PAD_X + n.col * COL_WIDTH;
    }
    const startRow = Math.floor((roots.length - 1) / 2);
    const start: LayoutNode = {
      job: null,
      nodeId: '__start__',
      label: 'Start',
      synthetic: true,
      col: 0,
      row: startRow,
      x: PAD_X,
      y: PAD_Y + startRow * ROW_HEIGHT,
    };
    for (const r of roots) edges.push({ from: start, to: r });
    allNodes = [start, ...nodes];
  }

  const width = PAD_X * 2 + (maxCol + 1 + colOffset) * COL_WIDTH - (COL_WIDTH - NODE_WIDTH);
  const height = PAD_Y * 2 + maxRows * ROW_HEIGHT - (ROW_HEIGHT - NODE_HEIGHT);

  return { nodes: allNodes, edges, width: Math.max(width, NODE_WIDTH + PAD_X * 2), height: Math.max(height, NODE_HEIGHT + PAD_Y * 2) };
}

export default function PlanDagView({ jobs, selectedJobId, onSelectNode }: PlanDagViewProps) {
  const layout = useMemo(() => computeDagLayout(jobs), [jobs]);

  if (!layout) return null;
  const { nodes, edges, width, height } = layout;

  return (
    <div style={dagScrollStyle}>
      <div style={{ position: 'relative', width, height, minWidth: '100%' }}>
        <svg
          width={width}
          height={height}
          style={{ position: 'absolute', top: 0, left: 0, pointerEvents: 'none' }}
        >
          <defs>
            <marker id="dag-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill={theme.colors.textSecondary} />
            </marker>
          </defs>
          {edges.map((edge, i) => {
            const x1 = edge.from.x + NODE_WIDTH;
            const y1 = edge.from.y + NODE_HEIGHT / 2;
            const x2 = edge.to.x;
            const y2 = edge.to.y + NODE_HEIGHT / 2;
            const midX = (x1 + x2) / 2;
            // The Start node has no status, so an edge out of it reflects only
            // the downstream node.
            const active = (!!edge.to.job && isActive(edge.to.job.status)) || (!!edge.from.job && isActive(edge.from.job.status));
            const failed = !!edge.from.job && isTerminalFailure(edge.from.job.status);
            const stroke = failed
              ? theme.colors.error
              : active
                ? theme.colors.primary
                : theme.colors.border;
            return (
              <path
                key={`${edge.from.nodeId}->${edge.to.nodeId}-${i}`}
                d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke={stroke}
                strokeWidth={active ? 2 : 1.5}
                strokeDasharray={failed ? '4 3' : undefined}
                markerEnd="url(#dag-arrow)"
              />
            );
          })}
        </svg>
        {nodes.map((node) => (
          <DagNodeCard
            key={node.nodeId}
            node={node}
            selected={!!node.job && selectedJobId === node.job.jobId}
            onClick={node.job ? () => onSelectNode?.(node.job!.jobId) : undefined}
          />
        ))}
      </div>
    </div>
  );
}

function DagNodeCard({ node, selected, onClick }: { node: LayoutNode; selected: boolean; onClick?: () => void }) {
  const { job } = node;

  // Start node: represents the request itself. No status, not clickable — it
  // exists so a fan-out reads as ONE unit of work instead of N loose boxes.
  if (!job) {
    return (
      <div
        title="Request start"
        style={{
          ...dagNodeStyle,
          left: node.x,
          top: node.y,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          borderStyle: 'dashed',
          borderColor: theme.colors.border,
          cursor: 'default',
          opacity: 0.85,
        }}
      >
        <span style={dagNodeTextWrapStyle}>
          <span style={dagNodeLabelStyle}>{node.label}</span>
          <span style={{ ...dagNodeMetaStyle, color: theme.colors.textSecondary }}>request</span>
        </span>
      </div>
    );
  }

  const active = isActive(job.status);
  const success = isTerminalSuccess(job.status);
  // Node names (t1, t2 …) are what the plan and the Jobs panel use, so lead with
  // the nodeId and keep the tool name as the secondary line.
  const label = node.nodeId || extractToolName(job.trioId) || 'node';

  return (
    <button
      type="button"
      onClick={onClick}
      title={`${label} — ${statusLabel(job.status)}`}
      style={{
        ...dagNodeStyle,
        left: node.x,
        top: node.y,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        borderColor: selected ? theme.colors.primary : theme.colors.border,
        boxShadow: selected ? `0 0 0 2px ${theme.colors.primary}33` : undefined,
      }}
    >
      <span style={{
        ...dagNodeDotStyle,
        background: statusColor(job.status),
        ...(active ? dagNodePulseStyle : undefined),
      }} />
      <span style={dagNodeTextWrapStyle}>
        <span style={dagNodeLabelStyle}>{label}</span>
        <span style={{ ...dagNodeMetaStyle, color: success ? theme.colors.success : theme.colors.textSecondary }}>
          {statusLabel(job.status)}
          {job.startAt && (job.terminalAt || active) ? ` · ${formatElapsed(job.startAt, job.updatedAt, job.status)}` : ''}
        </span>
      </span>
    </button>
  );
}

const dagScrollStyle: React.CSSProperties = {
  overflowX: 'auto',
  overflowY: 'hidden',
  borderBottom: `1px solid ${theme.colors.border}`,
  background: theme.colors.background,
  padding: '4px 0',
};

const dagNodeStyle: React.CSSProperties = {
  position: 'absolute',
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '0 10px',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  background: theme.colors.surface,
  cursor: 'pointer',
  textAlign: 'left',
  transition: 'border-color 120ms ease, box-shadow 120ms ease',
};

const dagNodeDotStyle: React.CSSProperties = {
  width: 8,
  height: 8,
  borderRadius: '50%',
  flexShrink: 0,
};

const dagNodePulseStyle: React.CSSProperties = {
  animation: 'activity-dot-pulse 1.5s infinite ease-in-out',
};

const dagNodeTextWrapStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
  overflow: 'hidden',
};

const dagNodeLabelStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.medium,
  color: theme.colors.textPrimary,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const dagNodeMetaStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};
