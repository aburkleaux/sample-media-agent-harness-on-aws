import { describe, it, expect } from 'vitest';
import { activeJobIdsForReconcile } from './useJobs';
import type { Job, JobStatus } from '../types/jobs';

const NOW = Date.parse('2026-07-31T02:00:00.000Z');

function mkJob(id: string, status: JobStatus, terminalAt?: string): Job {
  return {
    jobId: id,
    status,
    trioId: 'media-processing|process_media',
    sessionId: 's1',
    startAt: '2026-07-31T01:58:00.000Z',
    updatedAt: terminalAt ?? '2026-07-31T01:58:30.000Z',
    terminalAt: terminalAt ?? '',
  } as Job;
}

function mapOf(...jobs: Job[]): Map<string, Job> {
  const m = new Map<string, Job>();
  for (const j of jobs) m.set(j.jobId, j);
  return m;
}

describe('activeJobIdsForReconcile (issue-2 reconcile backstop)', () => {
  it('selects only PENDING/RUNNING visible jobs', () => {
    const jobs = mapOf(
      mkJob('j-run', 'RUNNING'),
      mkJob('j-pend', 'PENDING'),
      // terminal + recent → visible but NOT re-polled
      mkJob('j-done', 'SUCCEEDED', '2026-07-31T01:59:00.000Z'),
    );
    const ids = activeJobIdsForReconcile(jobs, NOW).sort();
    expect(ids).toEqual(['j-pend', 'j-run']);
  });

  it('excludes terminal jobs (nothing to re-poll)', () => {
    const jobs = mapOf(
      mkJob('j-done', 'SUCCEEDED', '2026-07-31T01:59:00.000Z'),
      mkJob('j-fail', 'FAILED', '2026-07-31T01:59:30.000Z'),
    );
    expect(activeJobIdsForReconcile(jobs, NOW)).toEqual([]);
  });

  it('excludes off-window terminal jobs entirely', () => {
    const jobs = mapOf(
      mkJob('j-old', 'SUCCEEDED', '2026-07-25T00:00:00.000Z'), // days ago → not visible
    );
    expect(activeJobIdsForReconcile(jobs, NOW)).toEqual([]);
  });

  it('returns [] for an empty store', () => {
    expect(activeJobIdsForReconcile(new Map(), NOW)).toEqual([]);
  });
});
