/**
 * The whole-workspace graph, drawn with Sigma.js (WebGL) and laid out by ForceAtlas2 in a web worker.
 *
 * Loaded lazily by `GraphTab` (`React.lazy`), so sigma/graphology are a separate chunk that only users who
 * open the Graph tab download. Legend toggles hide node types; "Show sample data" hides the demo sub-graph;
 * search pans to a node; clicking a node fetches its neighbourhood (Cypher on the reader Lambda) and
 * highlights it.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Sigma from 'sigma';
import forceAtlas2 from 'graphology-layout-forceatlas2';
import FA2Layout from 'graphology-layout-forceatlas2/worker';
import { theme } from '../../../styles/tokens';
import { getGraphNeighbourhood } from '../../../projects/api';
import type { GraphNeighbourhood, GraphOverview } from '../../../projects/api';
import { buildGraph, findNode, neighbourhoodIds, typeCounts } from './graphModel';
import type { EdgeAttrs, NodeAttrs } from './graphModel';
import { NODE_TYPES, useGraphPalette } from './graphTheme';
import NeighbourhoodPanel from './NeighbourhoodPanel';

export interface GraphCanvasProps {
  projectId: string;
  overview: GraphOverview;
  /** Nodes to call out (an agent's graph result, or "Open in project graph"); the camera goes to the first. */
  focusIds?: string[];
  /** `true` (Projects tab): dim everything but the focus nodes. `false` (a result view): only emphasise them. */
  dimOthers?: boolean;
  /** Extra toolbar content (e.g. the result view's "Open in project graph"). */
  toolbarExtra?: React.ReactNode;
}

const LAYOUT_MS = 9000;

export default function GraphCanvas({ projectId, overview, focusIds, dimOthers = true, toolbarExtra }: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma<NodeAttrs, EdgeAttrs> | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const graph = useMemo(() => buildGraph(overview), [overview]);
  const palette = useGraphPalette();

  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [showSample, setShowSample] = useState(true);
  const [layoutRunning, setLayoutRunning] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [nb, setNb] = useState<GraphNeighbourhood | null>(null);
  const [nbLoading, setNbLoading] = useState(false);
  const [nbError, setNbError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [searchMiss, setSearchMiss] = useState(false);

  // The reducers read the latest view state through a ref, so a toggle is a cheap `refresh()`.
  const focusSet = useMemo(() => new Set((focusIds ?? []).filter((id) => graph.hasNode(id))), [focusIds, graph]);
  const view = useRef({ hidden, showSample, highlight: null as Set<string> | null, selected, palette, emphasis: focusSet });
  view.current = {
    hidden, showSample, selected, palette,
    // A clicked node's neighbourhood wins; otherwise focus nodes are the highlight (Projects tab) or just emphasised.
    highlight: nb ? neighbourhoodIds(nb) : (dimOthers && focusSet.size ? focusSet : null),
    emphasis: focusSet,
  };

  const color = useCallback((type: string) => palette.types[type] ?? palette.dim, [palette]);

  // Mount Sigma once per graph.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;
    const sigma = new Sigma<NodeAttrs, EdgeAttrs>(graph, el, {
      renderEdgeLabels: false,
      labelRenderedSizeThreshold: 8,
      labelFont: 'inherit',
      zIndex: true,
      nodeReducer: (node, a) => {
        const v = view.current;
        if (v.hidden.has(a.nodeType) || (!v.showSample && a.sample)) return { ...a, hidden: true };
        const out = { ...a, color: v.palette.types[a.nodeType] ?? v.palette.dim };
        if (v.emphasis.has(node)) Object.assign(out, { size: a.size + 5, forceLabel: true, zIndex: 2, highlighted: !v.highlight });
        if (v.highlight) {
          if (!v.highlight.has(node)) return { ...out, color: v.palette.dim, label: '', zIndex: 0 };
          return { ...out, zIndex: 1, forceLabel: v.highlight.size <= 60, highlighted: node === v.selected };
        }
        return out;
      },
      edgeReducer: (edge, a) => {
        const v = view.current;
        const [s, t] = graph.extremities(edge);
        const sa = graph.getNodeAttributes(s);
        const ta = graph.getNodeAttributes(t);
        if (v.hidden.has(sa.nodeType) || v.hidden.has(ta.nodeType) || (!v.showSample && (sa.sample || ta.sample))) {
          return { ...a, hidden: true };
        }
        if (v.highlight) {
          return v.highlight.has(s) && v.highlight.has(t)
            ? { ...a, color: v.palette.edgeActive, size: 1.2, zIndex: 1 }
            : { ...a, hidden: true };
        }
        return { ...a, color: v.palette.edge };
      },
    });
    sigma.setSetting('labelColor', { color: view.current.palette.label });
    sigma.on('clickNode', ({ node }) => setSelected(node));
    sigma.on('clickStage', () => setSelected(null));
    sigmaRef.current = sigma;

    const layout = new FA2Layout(graph, {
      settings: { ...forceAtlas2.inferSettings(graph), barnesHutOptimize: graph.order > 2000, slowDown: 5, gravity: 0.8 },
    });
    layoutRef.current = layout;
    layout.start();
    setLayoutRunning(true);
    const stop = window.setTimeout(() => { layout.stop(); setLayoutRunning(false); }, LAYOUT_MS);
    return () => {
      window.clearTimeout(stop);
      layout.kill();
      sigma.kill();
      sigmaRef.current = null;
      layoutRef.current = null;
    };
  }, [graph]);

  // Re-colour on theme change and re-filter on toggles.
  useEffect(() => {
    sigmaRef.current?.setSetting('labelColor', { color: palette.label });
    sigmaRef.current?.refresh();
  }, [palette, hidden, showSample, nb, selected, focusSet, dimOthers]);

  // Selection → neighbourhood.
  const loadNeighbourhood = useCallback(async (nodeId: string, hops: 1 | 2) => {
    setNbLoading(true);
    setNbError(null);
    const r = await getGraphNeighbourhood(projectId, nodeId, hops);
    setNbLoading(false);
    if (r.ok && r.value.state === 'ready') setNb(r.value);
    else setNbError(r.ok ? 'The graph is being rebuilt; try again shortly' : 'Could not load this node');
  }, [projectId]);

  useEffect(() => {
    if (!selected) {
      setNb(null);
      setNbError(null);
      return;
    }
    void loadNeighbourhood(selected, 1);
  }, [selected, loadNeighbourhood]);

  const focus = useCallback((nodeId: string) => {
    const sigma = sigmaRef.current;
    const pos = sigma?.getNodeDisplayData(nodeId);
    if (sigma && pos) void sigma.getCamera().animate({ x: pos.x, y: pos.y, ratio: 0.3 }, { duration: 450 });
  }, []);

  // Move the camera to the first focus node once the layout has spread the graph (and again when it stops).
  useEffect(() => {
    const first = [...focusSet][0];
    if (!first || !dimOthers) return undefined;
    const t1 = window.setTimeout(() => focus(first), 1500);
    const t2 = window.setTimeout(() => focus(first), LAYOUT_MS + 200);
    return () => { window.clearTimeout(t1); window.clearTimeout(t2); };
  }, [focusSet, dimOthers, focus]);

  const runSearch = () => {
    const hit = findNode(graph, search);
    setSearchMiss(!hit);
    if (hit) {
      setSelected(hit);
      focus(hit);
    }
  };

  const toggleLayout = () => {
    const l = layoutRef.current;
    if (!l) return;
    if (l.isRunning()) { l.stop(); setLayoutRunning(false); } else { l.start(); setLayoutRunning(true); }
  };

  const counts = typeCounts(graph, true);
  const sampleCount = useMemo(() => { let n = 0; graph.forEachNode((_k, a) => { if (a.sample) n++; }); return n; }, [graph]);

  return (
    <div style={wrapStyle} data-testid="graph-canvas">
      <div style={stageStyle}>
        <div style={toolbarStyle}>
          <input
            style={searchStyle} value={search} placeholder="Find a node…" aria-label="Find a node"
            onChange={(e) => { setSearch(e.target.value); setSearchMiss(false); }}
            onKeyDown={(e) => { if (e.key === 'Enter') runSearch(); }}
          />
          {searchMiss && <span style={missStyle}>No match</span>}
          <span style={{ flex: 1 }} />
          {toolbarExtra}
          <button type="button" style={toolButtonStyle} onClick={toggleLayout} aria-pressed={layoutRunning}>
            {layoutRunning ? 'Stop layout' : 'Run layout'}
          </button>
        </div>
        <div ref={containerRef} style={sigmaStyle} data-testid="graph-sigma" />
        <div style={legendStyle} aria-label="Node types">
          {NODE_TYPES.filter((t) => counts.has(t)).map((t) => (
            <label key={t} style={legendRowStyle}>
              <input type="checkbox" checked={!hidden.has(t)}
                onChange={() => setHidden((h) => { const n = new Set(h); if (n.has(t)) n.delete(t); else n.add(t); return n; })} />
              <span style={{ ...swatchStyle, background: color(t) }} />
              <span style={{ flex: 1 }}>{t}</span>
              <span style={countStyle}>{counts.get(t)?.toLocaleString()}</span>
            </label>
          ))}
          {sampleCount > 0 && (
            <label style={{ ...legendRowStyle, marginTop: 6 }}>
              <input type="checkbox" checked={showSample} onChange={() => setShowSample((v) => !v)} />
              <span style={{ flex: 1 }}>Show sample data</span>
              <span style={countStyle}>{sampleCount.toLocaleString()}</span>
            </label>
          )}
        </div>
      </div>
      {selected && (
        <NeighbourhoodPanel
          data={nb} loading={nbLoading} error={nbError} color={color}
          onExpand={() => void loadNeighbourhood(selected, 2)}
          onSelect={(id) => { if (graph.hasNode(id)) { setSelected(id); focus(id); } }}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

const wrapStyle: React.CSSProperties = { display: 'flex', flex: 1, minHeight: 0, height: '100%' };
const stageStyle: React.CSSProperties = { position: 'relative', flex: 1, minWidth: 0, background: theme.colors.background };
const sigmaStyle: React.CSSProperties = { position: 'absolute', inset: 0 };
const toolbarStyle: React.CSSProperties = {
  position: 'absolute', top: 10, left: 10, right: 10, zIndex: 2, display: 'flex', alignItems: 'center', gap: 8,
  pointerEvents: 'none',
};
const searchStyle: React.CSSProperties = {
  pointerEvents: 'auto', width: 220, padding: '6px 9px', fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary,
  background: theme.colors.surface, border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
};
const missStyle: React.CSSProperties = { pointerEvents: 'auto', fontSize: 'var(--fs-xs)', color: theme.colors.textSecondary };
const toolButtonStyle: React.CSSProperties = {
  pointerEvents: 'auto', padding: '5px 10px', fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary,
  background: theme.colors.surface, border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button,
  cursor: 'pointer',
};
const legendStyle: React.CSSProperties = {
  position: 'absolute', left: 10, bottom: 10, zIndex: 2, padding: '8px 10px', minWidth: 170,
  background: theme.colors.surface, border: `1px solid ${theme.colors.border}`, borderRadius: 8,
  fontSize: 'var(--fs-xs)', color: theme.colors.textPrimary,
};
const legendRowStyle: React.CSSProperties = { display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', padding: '1px 0' };
const swatchStyle: React.CSSProperties = { width: 10, height: 10, borderRadius: '50%', display: 'inline-block' };
const countStyle: React.CSSProperties = { color: theme.colors.textSecondary, fontVariantNumeric: 'tabular-nums' };
