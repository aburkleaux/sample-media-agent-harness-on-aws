import { describe, expect, it } from 'vitest';
import type { Job } from '../types/jobs';
import { computeDagLayout } from './PlanDagView';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'job-' + Math.random().toString(36).slice(2),
    actorId: 'actor-1',
    sessionId: 'session-A',
    trioId: 'node|t1_stub',
    gatewayId: '__coordinator__',
    status: 'PENDING',
    startAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    terminalAt: null,
    hasRenderableArtifacts: false,
    lastError: null,
    runId: 'plan-1',
    nodeId: null,
    dependsOn: [],
    ...overrides,
  };
}

describe('computeDagLayout', () => {
  it('returns null when no job carries a nodeId (nothing to graph)', () => {
    const jobs = [job({ nodeId: null }), job({ nodeId: null })];
    expect(computeDagLayout(jobs)).toBeNull();
  });

  it('fans several independent roots out of a synthetic Start node', () => {
    // Without this, a parallel request draws as N disconnected boxes, which
    // reads as unrelated jobs rather than one unit of work. Start is
    // presentational only: no job, no job row, no notification.
    const jobs = [
      job({ nodeId: 't1_a', dependsOn: [] }),
      job({ nodeId: 't2_b', dependsOn: [] }),
    ];
    const layout = computeDagLayout(jobs)!;
    const start = layout.nodes.find((n) => n.synthetic);
    expect(start).toBeDefined();
    expect(start!.job).toBeNull();
    expect(start!.col).toBe(0);
    expect(start!.label).toBe('Start');
    // Real nodes shift right so Start owns column 0.
    const real = layout.nodes.filter((n) => !n.synthetic);
    expect(real).toHaveLength(2);
    expect(real.every((n) => n.col === 1)).toBe(true);
    // One edge from Start to each root.
    expect(layout.edges).toHaveLength(2);
    expect(layout.edges.every((e) => e.from.synthetic)).toBe(true);
  });

  it('does NOT add a Start node for a single-root chain', () => {
    // trim -> analyse already reads as one connected flow.
    const jobs = [
      job({ nodeId: 't1_trim', dependsOn: [] }),
      job({ nodeId: 't2_diarize', dependsOn: ['t1_trim'] }),
    ];
    const layout = computeDagLayout(jobs)!;
    expect(layout.nodes.some((n) => n.synthetic)).toBe(false);
    expect(layout.nodes).toHaveLength(2);
    expect(layout.edges).toHaveLength(1);
  });

  it('places a dependent node one column after its dependency', () => {
    const jobs = [
      job({ nodeId: 't1_a', dependsOn: [] }),
      job({ nodeId: 't2_b', dependsOn: ['t1_a'] }),
    ];
    const layout = computeDagLayout(jobs)!;
    const a = layout.nodes.find((n) => n.nodeId === 't1_a')!;
    const b = layout.nodes.find((n) => n.nodeId === 't2_b')!;
    expect(a.col).toBe(0);
    expect(b.col).toBe(1);
    expect(layout.edges).toHaveLength(1);
    expect(layout.edges[0].from.nodeId).toBe('t1_a');
    expect(layout.edges[0].to.nodeId).toBe('t2_b');
  });

  it('fans out two siblings into parallel rows in the same column (diarize -> translate_fr/translate_es)', () => {
    const jobs = [
      job({ nodeId: 'sg1_separate_stems', dependsOn: [] }),
      job({ nodeId: 'sg2_diarize_speech', dependsOn: ['sg1_separate_stems'] }),
      job({ nodeId: 'translate_fr', dependsOn: ['sg2_diarize_speech'] }),
      job({ nodeId: 'translate_es', dependsOn: ['sg2_diarize_speech'] }),
    ];
    const layout = computeDagLayout(jobs)!;
    const fr = layout.nodes.find((n) => n.nodeId === 'translate_fr')!;
    const es = layout.nodes.find((n) => n.nodeId === 'translate_es')!;
    expect(fr.col).toBe(2);
    expect(es.col).toBe(2);
    expect(fr.row).not.toBe(es.row);
    expect(layout.edges).toHaveLength(3);
  });

  it('ignores dependsOn edges pointing to nodes outside the job set', () => {
    const jobs = [job({ nodeId: 't2_b', dependsOn: ['missing_node'] })];
    const layout = computeDagLayout(jobs)!;
    expect(layout.nodes).toHaveLength(1);
    expect(layout.nodes[0].col).toBe(0);
    expect(layout.edges).toHaveLength(0);
  });

  it('does not infinite-loop on a cyclic dependsOn (defensive guard)', () => {
    const jobs = [
      job({ nodeId: 'a', dependsOn: ['b'] }),
      job({ nodeId: 'b', dependsOn: ['a'] }),
    ];
    expect(() => computeDagLayout(jobs)).not.toThrow();
  });
});
