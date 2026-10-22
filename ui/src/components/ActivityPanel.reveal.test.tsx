/**
 * @vitest-environment jsdom
 *
 * ActivityPanel "reveal" behavior: opening a run (from the Jobs panel or the
 * Workflow sidebar) must keep EVERY flow on screen and just expand + scroll to
 * the opened run — NOT narrow the canvas to a single run (the old focusRunId
 * behavior, which hid all other flows and desynced the filter counts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import '@testing-library/jest-dom';
import React from 'react';
import { render, cleanup, waitFor } from '@testing-library/react';
import type { Job } from '../types/jobs';

const jobsMock = vi.fn();
vi.mock('../hooks/useJobs', () => ({
  useJobs: () => jobsMock(),
}));

import ActivityPanel from './ActivityPanel';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'arn:job:' + Math.random().toString(36).slice(2),
    actorId: 'actor-1',
    sessionId: 'session-A',
    trioId: 'face-recognition|recognize_faces',
    gatewayId: '__aws_sfn__',
    status: 'SUCCEEDED', // terminal → not auto-expanded, so reveal-expand is observable
    startAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    terminalAt: new Date().toISOString(),
    hasRenderableArtifacts: true,
    lastError: null,
    runId: null,
    nodeId: null,
    dependsOn: [],
    ...overrides,
  };
}

// Two independent plan runs (A and B), both terminal.
function twoRuns(): Job[] {
  return [
    job({ jobId: 'runA', trioId: 'plan|flow-a', runId: 'runA', sessionId: 'sA' }),
    job({ jobId: 'runA#n1', trioId: 'node|seg', runId: 'runA', nodeId: 'n1', sessionId: 'sA' }),
    job({ jobId: 'runB', trioId: 'plan|flow-b', runId: 'runB', sessionId: 'sB' }),
    job({ jobId: 'runB#n1', trioId: 'node|seg', runId: 'runB', nodeId: 'n1', sessionId: 'sB' }),
  ];
}

beforeEach(() => {
  jobsMock.mockReturnValue({
    jobs: twoRuns(),
    connectionState: 'open',
    deleteJob: vi.fn(),
    deleteRun: vi.fn(),
  });
  // jsdom has no scrollIntoView; spy so the reveal can call it.
  (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('ActivityPanel reveal (no narrowing)', () => {
  it('keeps every flow visible and expands + scrolls the revealed run', async () => {
    const onRevealed = vi.fn();
    const { container } = render(<ActivityPanel revealRunId="runB" onRevealed={onRevealed} />);

    // Both run groups remain on screen — the canvas is NOT narrowed to runB.
    const groups = container.querySelectorAll('[data-group-id]');
    const ids = Array.from(groups).map(g => g.getAttribute('data-group-id'));
    expect(ids).toContain('run:runA');
    expect(ids).toContain('run:runB');

    // Only the revealed run is expanded (runA, also terminal, stays collapsed).
    const headerAria = (gid: string) =>
      container.querySelector(`[data-group-id="${gid}"] button[aria-expanded]`)?.getAttribute('aria-expanded');
    expect(headerAria('run:runB')).toBe('true');
    expect(headerAria('run:runA')).toBe('false');

    // It scrolled the revealed group into view and reset the reveal target.
    await waitFor(() => expect(onRevealed).toHaveBeenCalled());
    expect((Element.prototype as unknown as { scrollIntoView: ReturnType<typeof vi.fn> }).scrollIntoView)
      .toHaveBeenCalled();
  });

  it('shows all flows and none specially expanded when nothing is revealed', () => {
    const { container } = render(<ActivityPanel />);
    const ids = Array.from(container.querySelectorAll('[data-group-id]'))
      .map(g => g.getAttribute('data-group-id'));
    expect(ids).toEqual(expect.arrayContaining(['run:runA', 'run:runB']));
    // Terminal runs are collapsed by default (no reveal, no active jobs).
    const anyExpanded = container.querySelector('button[aria-expanded="true"]');
    expect(anyExpanded).toBeNull();
  });
});
