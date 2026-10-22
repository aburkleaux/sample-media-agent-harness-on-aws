/**
 * Pure graph building for the Graph tab: the compact overview payload → a graphology `Graph` with seeded
 * positions, sizes, and the attributes the Sigma reducers read. No DOM, no WebGL — unit-testable in node.
 */
import Graph from 'graphology';
import type { GraphNeighbourhood, GraphOverview } from '../../../projects/api';

export interface NodeAttrs {
  label: string;
  nodeType: string;
  sample: boolean;
  degree: number;
  x: number;
  y: number;
  size: number;
  color?: string;
}

export interface EdgeAttrs {
  rel: string;
  size: number;
  color?: string;
}

export type ProjectGraph = Graph<NodeAttrs, EdgeAttrs>;

/** Deterministic [0, 1) from a string, so a reload lays the graph out the same way before the layout runs. */
function hash01(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 100000) / 100000;
}

export function nodeSize(degree: number): number {
  return Math.min(16, 2 + Math.sqrt(degree) * 1.1);
}

/** Build the whole-workspace graph. Each node type starts in its own sector so it reads before layout. */
export function buildGraph(ov: GraphOverview): ProjectGraph {
  const g: ProjectGraph = new Graph<NodeAttrs, EdgeAttrs>({ multi: true, type: 'directed', allowSelfLoops: false });
  const types = ov.types?.nodes ?? [];
  const rels = ov.types?.edges ?? [];
  const nodes = ov.nodes ?? [];
  nodes.forEach(([id, typeIdx, name, sample, degree]) => {
    const type = types[typeIdx] ?? 'Node';
    const angle = (typeIdx / Math.max(types.length, 1)) * 2 * Math.PI + (hash01(id) - 0.5) * 0.9;
    const r = 60 + hash01(`${id}#r`) * 340;
    g.addNode(id, {
      label: name, nodeType: type, sample: sample === 1, degree,
      x: Math.cos(angle) * r, y: Math.sin(angle) * r, size: nodeSize(degree),
    });
  });
  for (const [si, di, ti] of ov.edges ?? []) {
    const s = nodes[si]?.[0];
    const d = nodes[di]?.[0];
    if (s === undefined || d === undefined || s === d) continue;
    g.addEdge(s, d, { rel: rels[ti] ?? 'RELATED', size: 0.5 });
  }
  return g;
}

/** Ids of the centre and every node the neighbourhood reply mentions (what the canvas highlights). */
export function neighbourhoodIds(nb: GraphNeighbourhood): Set<string> {
  const ids = new Set<string>();
  if (nb.node) ids.add(nb.node.id);
  for (const n of nb.nodes ?? []) ids.add(n.id);
  for (const [s, d] of nb.edges ?? []) {
    ids.add(s);
    ids.add(d);
  }
  return ids;
}

/** `{ rel → count }` for the selection panel, sorted by count. */
export function relationshipCounts(nb: GraphNeighbourhood): [string, number][] {
  const counts = new Map<string, number>();
  for (const [, , rel] of nb.edges ?? []) counts.set(rel, (counts.get(rel) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** Count of nodes per type (the legend). */
export function typeCounts(g: ProjectGraph, includeSample: boolean): Map<string, number> {
  const out = new Map<string, number>();
  g.forEachNode((_n, a) => {
    if (!includeSample && a.sample) return;
    out.set(a.nodeType, (out.get(a.nodeType) ?? 0) + 1);
  });
  return out;
}

/** First node whose label contains `text` (case-insensitive), preferring real over sample nodes. */
export function findNode(g: ProjectGraph, text: string): string | null {
  const q = text.trim().toLowerCase();
  if (!q) return null;
  let sampleHit: string | null = null;
  let hit: string | null = null;
  g.forEachNode((n, a) => {
    if (hit || !String(a.label).toLowerCase().includes(q)) return;
    if (a.sample) sampleHit ??= n;
    else hit = n;
  });
  return hit ?? sampleHit;
}
