/**
 * Graph results outside the canvas — deliberately free of sigma/graphology so the chat renderer and the workspace
 * can import it without pulling the graph libraries into the main bundle (GraphCanvas stays a lazy chunk).
 */
import type { GraphOverview } from '../../../projects/api';

/** A `graph-view` manifest's node (the agent's "↳ Graph" result: render/graph_view.py). */
export interface GraphResultNode {
  id: string;
  type: string;
  name: string;
  sample?: boolean;
  focus?: boolean;
}

/** The compact overview shape GraphCanvas draws, built from a graph result's nodes and edges. */
export function overviewFromResult(nodes: GraphResultNode[], edges: [string, string, string][], typeOrder: readonly string[]): GraphOverview {
  const types = [...typeOrder, ...[...new Set(nodes.map((n) => n.type))].filter((t) => !typeOrder.includes(t))];
  const rels = [...new Set(edges.map((e) => e[2]))];
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const degree = new Map<string, number>();
  for (const [s, d] of edges) {
    degree.set(s, (degree.get(s) ?? 0) + 1);
    degree.set(d, (degree.get(d) ?? 0) + 1);
  }
  return {
    state: 'ready',
    totals: { nodes: nodes.length, edges: edges.length },
    types: { nodes: types, edges: rels },
    nodes: nodes.map((n) => [n.id, types.indexOf(n.type), n.name, n.sample ? 1 : 0, degree.get(n.id) ?? 0]),
    edges: edges.filter(([s, d]) => index.has(s) && index.has(d))
      .map(([s, d, r]) => [index.get(s)!, index.get(d)!, rels.indexOf(r)]),
  };
}

/** Ask the workspace to open the Projects Graph tab focused on `nodeIds` (handled in WorkspaceView). */
export const OPEN_PROJECT_GRAPH_EVENT = 'lab:open-project-graph';

export function openProjectGraph(projectId: string, nodeIds: string[]): void {
  window.dispatchEvent(new CustomEvent(OPEN_PROJECT_GRAPH_EVENT, { detail: { projectId, nodeIds } }));
}
