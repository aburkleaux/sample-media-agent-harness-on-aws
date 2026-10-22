/**
 * @vitest-environment jsdom
 *
 * Graph tab states (the Sigma canvas is mocked: jsdom has no WebGL) and the pure graph model.
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getProjectGraph: vi.fn(),
  getProjectGraphStats: vi.fn(),
  enableProjectGraph: vi.fn(),
}));
vi.mock('../../../projects/api', () => mocks);
vi.mock('./GraphCanvas', () => ({
  default: ({ overview }: { overview: { nodes?: unknown[] } }) => <div data-testid="graph-canvas-mock">{overview.nodes?.length} nodes</div>,
}));

import GraphTab from './GraphTab';
import { buildGraph, findNode, neighbourhoodIds, relationshipCounts, typeCounts } from './graphModel';
import type { GraphOverview } from '../../../projects/api';

const PID = 'p' + 'a'.repeat(32);
const project = { id: PID, name: 'Finals', personal: false };
const READY: GraphOverview = {
  state: 'ready', generation: 2, truncated: false, totals: { nodes: 3, edges: 2 },
  types: { nodes: ['Project', 'User', 'Tag', 'Asset', 'Scene', 'Shot', 'Person'], edges: ['OWNS', 'HAS_ASSET'] },
  nodes: [[`project:${PID}`, 0, 'Finals', 0, 2], ['user:alice', 1, 'Alice', 0, 1], ['sample:Person:0', 6, 'Ava Adler', 1, 0]],
  edges: [[1, 0, 0], [0, 0, 1]],
};

afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

describe('GraphTab', () => {
  it('renders the lazily loaded canvas when ready', async () => {
    mocks.getProjectGraph.mockResolvedValue({ ok: true, value: READY });
    render(<GraphTab project={project} role="owner" onNotFound={vi.fn()} />);
    expect(await screen.findByTestId('graph-canvas-mock')).toHaveTextContent('3 nodes');
    expect(screen.getByTestId('graph-ready')).toHaveTextContent('3 nodes · 2 relationships');
  });

  it('off + owner: enable is confirmed, then the tab shows the build in progress', async () => {
    mocks.getProjectGraph.mockResolvedValue({ ok: true, value: { state: 'off' } });
    mocks.enableProjectGraph.mockResolvedValue({ ok: true, value: { projectId: PID, graph: true } });
    mocks.getProjectGraphStats.mockResolvedValue({ ok: true, value: { state: 'seeding' } });
    render(<GraphTab project={project} role="owner" onNotFound={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Enable knowledge graph' }));
    fireEvent.click(screen.getByRole('button', { name: 'Turn on knowledge graph' }));
    expect(await screen.findByTestId('graph-seeding')).toBeInTheDocument();
    expect(mocks.enableProjectGraph).toHaveBeenCalledWith(PID);
  });

  it('off + member: explains, offers no enable', async () => {
    mocks.getProjectGraph.mockResolvedValue({ ok: true, value: { state: 'off' } });
    render(<GraphTab project={project} role="member" onNotFound={vi.fn()} />);
    expect(await screen.findByText('Only the project owner can turn on the knowledge graph.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Enable knowledge graph' })).toBeNull();
  });

  it('seeding polls stats and loads the graph once the first build lands', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.getProjectGraph.mockResolvedValueOnce({ ok: true, value: { state: 'seeding' } })
      .mockResolvedValue({ ok: true, value: READY });
    mocks.getProjectGraphStats.mockResolvedValue({ ok: true, value: { state: 'ready', generation: 1 } });
    render(<GraphTab project={project} role="owner" onNotFound={vi.fn()} />);
    expect(await screen.findByTestId('graph-seeding')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(await screen.findByTestId('graph-canvas-mock')).toBeInTheDocument();
    expect(mocks.getProjectGraphStats).toHaveBeenCalledWith(PID);
  });

  it('service unavailable → message + retry', async () => {
    mocks.getProjectGraph.mockResolvedValueOnce({ ok: false, kind: 'error', status: 503, code: 'graph_service_unavailable', message: 'x' })
      .mockResolvedValue({ ok: true, value: READY });
    render(<GraphTab project={project} role="owner" onNotFound={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable right now');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByTestId('graph-canvas-mock')).toBeInTheDocument();
  });

  it('a lost project reports not-found to the host', async () => {
    mocks.getProjectGraph.mockResolvedValue({ ok: false, kind: 'not_found' });
    const onNotFound = vi.fn();
    render(<GraphTab project={project} role="owner" onNotFound={onNotFound} />);
    await waitFor(() => expect(onNotFound).toHaveBeenCalled());
  });
});

describe('graphModel', () => {
  it('builds nodes and edges from the compact payload, skipping self loops', () => {
    const g = buildGraph(READY);
    expect(g.order).toBe(3);
    expect(g.size).toBe(1);                       // [0,0,1] is a self loop
    expect(g.getNodeAttribute('sample:Person:0', 'sample')).toBe(true);
    expect(g.getNodeAttribute(`project:${PID}`, 'nodeType')).toBe('Project');
    expect(buildGraph(READY).getNodeAttribute('user:alice', 'x')).toBe(g.getNodeAttribute('user:alice', 'x'));
  });

  it('counts types with and without sample rows, and finds real nodes first', () => {
    const g = buildGraph(READY);
    expect(typeCounts(g, true).get('Person')).toBe(1);
    expect(typeCounts(g, false).has('Person')).toBe(false);
    expect(findNode(g, 'ali')).toBe('user:alice');
    expect(findNode(g, 'nobody')).toBeNull();
  });

  it('collects neighbourhood ids and relationship counts', () => {
    const nb = { state: 'ready' as const, node: { id: 'a', type: 'User', name: 'A', sample: false, props: {} },
      nodes: [], edges: [['a', 'b', 'OWNS'], ['a', 'c', 'MEMBER_OF'], ['a', 'd', 'MEMBER_OF']] as [string, string, string][] };
    expect([...neighbourhoodIds(nb)].sort()).toEqual(['a', 'b', 'c', 'd']);
    expect(relationshipCounts(nb)).toEqual([['MEMBER_OF', 2], ['OWNS', 1]]);
  });
});
