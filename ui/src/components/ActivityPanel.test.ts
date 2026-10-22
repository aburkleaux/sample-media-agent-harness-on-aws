import { describe, expect, it } from 'vitest';
import type { Job } from '../types/jobs';
import { dedupeJobsForDisplay, dedupeRunNodeJobs, groupJobsBySession } from './ActivityPanel';

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
    runId: null,
    nodeId: null,
    dependsOn: [],
    ...overrides,
  };
}

describe('dedupeRunNodeJobs', () => {
  it('drops raw underlying-gateway-job rows when node| rows exist for the same run', () => {
    // Mirrors the real DDB shape observed for a stem-separation ->
    // diarization -> translate plan: each async node produces both a
    // canonical `node|<id>` row and a raw SFN-execution row.
    const jobs = [
      job({ jobId: 'plan#sg1_stem_separation', trioId: 'node|sg1_stem_separation', nodeId: 'sg1_stem_separation', status: 'SUCCEEDED' }),
      job({ jobId: 'arn:aws:states:...:stem-sep', trioId: 'audio-stem-separation|stem_separation', gatewayId: '__aws_sfn__', status: 'SUCCEEDED' }),
      job({ jobId: 'plan#sg2_diarization', trioId: 'node|sg2_diarization', nodeId: 'sg2_diarization', dependsOn: ['sg1_stem_separation'], status: 'SUCCEEDED' }),
      job({ jobId: 'arn:aws:states:...:diarize', trioId: 'speech-diarization|speech_diarization', gatewayId: '__aws_sfn__', status: 'SUCCEEDED' }),
    ];
    const result = dedupeRunNodeJobs(jobs);
    expect(result).toHaveLength(2);
    expect(result.every(j => j.trioId.startsWith('node|'))).toBe(true);
  });

  it('leaves jobs untouched when no node| rows exist (non-plan / standalone jobs)', () => {
    const jobs = [
      job({ trioId: 'face-recognition|recognize_faces' }),
      job({ trioId: 'audio-stem-separation|stem_separation' }),
    ];
    expect(dedupeRunNodeJobs(jobs)).toHaveLength(2);
  });
});

describe('dedupeJobsForDisplay', () => {
  it('dedupes per-run and preserves the plan-parent row and standalone jobs', () => {
    const jobs = [
      job({ trioId: 'plan|plan-netflixmeridian-transcribe-translate', runId: 'run-1', status: 'RUNNING' }),
      job({ jobId: 'a', trioId: 'node|sg1_stem_separation', nodeId: 'sg1_stem_separation', runId: 'run-1', status: 'SUCCEEDED' }),
      job({ jobId: 'b', trioId: 'audio-stem-separation|stem_separation', gatewayId: '__aws_sfn__', runId: 'run-1', status: 'SUCCEEDED' }),
      job({ jobId: 'c', trioId: 'face-recognition|recognize_faces', runId: null }),
    ];
    const result = dedupeJobsForDisplay(jobs);
    // plan row + deduped node row + standalone job = 3 (the raw SFN
    // duplicate for run-1 is dropped).
    expect(result).toHaveLength(3);
    expect(result.some(j => j.jobId === 'b')).toBe(false);
    expect(result.some(j => j.jobId === 'a')).toBe(true);
    expect(result.some(j => j.jobId === 'c')).toBe(true);
  });

  it('does not affect jobs from different runs', () => {
    const jobs = [
      job({ jobId: 'a', trioId: 'node|t1', nodeId: 't1', runId: 'run-1' }),
      job({ jobId: 'b', trioId: 'node|t1', nodeId: 't1', runId: 'run-2' }),
    ];
    expect(dedupeJobsForDisplay(jobs)).toHaveLength(2);
  });
});

describe('groupJobsBySession ordering', () => {
  it('orders workflow groups strictly by execution start time, not jumping active groups over finished ones', () => {
    // Run 1: started at 10:00, completed/SUCCEEDED
    // Run 2: started at 10:05, currently RUNNING
    const jobs = [
      job({
        jobId: 'run-1',
        runId: 'run-1',
        trioId: 'plan|older-completed-run',
        status: 'SUCCEEDED',
        startAt: '2026-08-10T10:00:00.000Z',
        updatedAt: '2026-08-10T10:02:00.000Z',
      }),
      job({
        jobId: 'run-1#node1',
        runId: 'run-1',
        trioId: 'node|node1',
        status: 'SUCCEEDED',
        startAt: '2026-08-10T10:00:05.000Z',
        updatedAt: '2026-08-10T10:01:50.000Z',
      }),
      job({
        jobId: 'run-2',
        runId: 'run-2',
        trioId: 'plan|newer-running-run',
        status: 'RUNNING',
        startAt: '2026-08-10T10:05:00.000Z',
        updatedAt: '2026-08-10T10:06:00.000Z',
      }),
      job({
        jobId: 'run-2#node1',
        runId: 'run-2',
        trioId: 'node|node1',
        status: 'RUNNING',
        startAt: '2026-08-10T10:05:05.000Z',
        updatedAt: '2026-08-10T10:06:00.000Z',
      }),
    ];

    const groups = groupJobsBySession(jobs);
    expect(groups).toHaveLength(2);
    // Newer execution start time (run-2 at 10:05) comes first
    expect(groups[0].runId).toBe('run-2');
    expect(groups[1].runId).toBe('run-1');

    // When run-2 completes, execution start times remain 10:05 vs 10:00
    jobs[2].status = 'SUCCEEDED';
    jobs[3].status = 'SUCCEEDED';
    const groupsAfterCompletion = groupJobsBySession(jobs);
    expect(groupsAfterCompletion[0].runId).toBe('run-2');
    expect(groupsAfterCompletion[1].runId).toBe('run-1');
  });
});

