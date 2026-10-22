/**
 * @vitest-environment jsdom
 *
 * The agent's "↳ Graph" result tab (`graph-view`): routing, the summary, focus nodes passed to the canvas without
 * dimming, and "Open in project graph" dispatching the workspace event. The Sigma canvas is mocked (no WebGL).
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

const canvasProps = vi.hoisted(() => ({ last: null as any }));
vi.mock('../../projects/graph/GraphCanvas', () => ({
  default: (p: any) => { canvasProps.last = p; return <div data-testid="graph-canvas-mock">{p.toolbarExtra}</div>; },
}));

import GraphView from './GraphView';
import { resolveTemplate } from './ManifestRouter';
import { OPEN_PROJECT_GRAPH_EVENT, overviewFromResult } from '../../projects/graph/graphResult';

const PID = 'p' + 'a'.repeat(32);
const DATA = {
  project_id: PID, title: 'Graph · Finals', truncated: false,
  nodes: [
    { id: 'person:uma', type: 'Person', name: 'Uma', focus: true },
    { id: 'sample:Scene:1', type: 'Scene', name: 'Kitchen', sample: true },
    { id: 'person:dev', type: 'Person', name: 'Dev' },
  ],
  edges: [['person:uma', 'sample:Scene:1', 'APPEARS_IN'], ['person:dev', 'sample:Scene:1', 'APPEARS_IN'],
          ['person:dev', 'gone', 'KNOWS']] as [string, string, string][],
};

afterEach(() => { cleanup(); canvasProps.last = null; });

describe('graph-view template', () => {
  it('is registered with the manifest router', () => {
    expect(resolveTemplate('graph-view')).toBe(GraphView);
  });

  it('summarises the result and emphasises the focus nodes without dimming the rest', async () => {
    render(<GraphView data={DATA as any} title="t" theme={{} as any} />);
    expect(screen.getByTestId('graph-view')).toHaveTextContent('3 nodes · 3 relationships · 1 in focus');
    await screen.findByTestId('graph-canvas-mock');
    expect(canvasProps.last.focusIds).toEqual(['person:uma']);
    expect(canvasProps.last.dimOthers).toBe(false);
    expect(canvasProps.last.overview.nodes).toHaveLength(3);
  });

  it('"Open in project graph" asks the workspace to focus those nodes', async () => {
    const seen: any[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(OPEN_PROJECT_GRAPH_EVENT, on);
    render(<GraphView data={DATA as any} title="t" theme={{} as any} />);
    fireEvent.click(await screen.findByTestId('graph-view-open-project'));
    window.removeEventListener(OPEN_PROJECT_GRAPH_EVENT, on);
    expect(seen).toEqual([{ projectId: PID, nodeIds: ['person:uma'] }]);
  });

  it('renders an empty state for a malformed manifest', () => {
    render(<GraphView data={{} as any} title="t" theme={{} as any} />);
    expect(screen.getByTestId('graph-view-empty')).toBeInTheDocument();
  });
});

describe('overviewFromResult', () => {
  it('indexes nodes and drops dangling edges', () => {
    const ov = overviewFromResult(DATA.nodes, DATA.edges, ['Project', 'Person', 'Scene']);
    expect(ov.nodes?.[0]).toEqual(['person:uma', 1, 'Uma', 0, 1]);
    expect(ov.nodes?.[1][3]).toBe(1);                       // sample flag
    expect(ov.edges).toEqual([[0, 1, 0], [2, 1, 0]]);       // the edge to "gone" is dropped
  });
});
