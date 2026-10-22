// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  notificationsStore,
  labelForTrio,
  getCompletionNotificationsEnabled,
  setCompletionNotificationsEnabled,
  __resetNotificationsForTests,
  labelForPlan,
  rollupRenderUrls,
} from './notifications';
import type { Job } from '../types/jobs';

function job(overrides: Partial<Job>): Job {
  return {
    jobId: 'arn:job:' + Math.random().toString(36).slice(2),
    actorId: 'a1',
    sessionId: 'sess-1',
    trioId: 'face-recognition|recognize_faces',
    gatewayId: '__aws_sfn__',
    status: 'SUCCEEDED',
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

describe('labelForTrio', () => {
  it('prettifies the workflow segment', () => {
    expect(labelForTrio('face-recognition|recognize_faces')).toBe('Face Recognition');
    expect(labelForTrio('audio-stem|separate')).toBe('Audio Stem');
    expect(labelForTrio('')).toBe('Job');
  });
});

describe('notifications store', () => {
  beforeEach(() => __resetNotificationsForTests());
  afterEach(() => __resetNotificationsForTests());

  it('pushes a renderable success notification', () => {
    const n = notificationsStore.pushFromTerminal(job({ jobId: 'j1', hasRenderableArtifacts: true }));
    expect(n).not.toBeNull();
    expect(n!.kind).toBe('success');
    expect(n!.renderable).toBe(true);
    expect(n!.message).toMatch(/finished/i);
    expect(notificationsStore.getSnapshot().unseen).toBe(1);
  });

  it('folds the artifact count into the summary (B3)', () => {
    const n = notificationsStore.pushFromTerminal(
      job({ jobId: 'jc', hasRenderableArtifacts: true, artifactUrls: ['a.mp4', 'b.mp4'] }),
    );
    expect(n!.message).toBe('Face Recognition finished — 2 files ready');
    // carries the resolved urls so the click can render deterministically (no LLM re-fetch)
    expect(n!.artifactUrls).toEqual(['a.mp4', 'b.mp4']);
    const one = notificationsStore.pushFromTerminal(
      job({ jobId: 'jc1', hasRenderableArtifacts: true, artifactUrls: ['only.wav'] }),
    );
    expect(one!.message).toBe('Face Recognition finished — 1 file ready');
    // no count when renderable but no urls carried yet → plain, no dangling dash
    const none = notificationsStore.pushFromTerminal(
      job({ jobId: 'jc2', hasRenderableArtifacts: true }),
    );
    expect(none!.message).toBe('Face Recognition finished');
  });

  it('does NOT notify for a non-renderable success (nothing to view — e.g. a metadata probe answered in-turn)', () => {
    const n = notificationsStore.pushFromTerminal(job({ jobId: 'j2', hasRenderableArtifacts: false }));
    expect(n).toBeNull();
    expect(notificationsStore.getSnapshot().unseen).toBe(0);
  });

  it('pushes failures with the error message', () => {
    const n = notificationsStore.pushFromTerminal(job({ jobId: 'j3', status: 'FAILED', lastError: 'boom' }));
    expect(n!.kind).toBe('failure');
    expect(n!.error).toBe('boom');
    expect(n!.renderable).toBe(false);
  });

  it('skips plan CHILD nodes so an N-node DAG fires one nudge, not N', () => {
    // Child: runId points at the plan and differs from its own jobId.
    expect(notificationsStore.pushFromTerminal(job({ jobId: 'j4', runId: 'plan-1' }))).toBeNull();
  });

  it('skips a plan CHILD node even when the SSE frame omits runId (node-row write gap)', () => {
    // A live `job.terminal` frame for a node can arrive without runId — some
    // node-row writes (e.g. the changefeed bridge FAILED path) don't stamp it.
    // The nodeId must still suppress it, or an N-node DAG fires a premature
    // per-node banner (observed: run f1fe59cd…#t_diarize) instead of one plan
    // nudge on the parent. Mirrors useJobs.notifyTerminalTransitions.
    expect(
      notificationsStore.pushFromTerminal(
        job({ jobId: 'run-1#t_diarize', nodeId: 't_diarize', runId: null, trioId: 'node|t_diarize' }),
      ),
    ).toBeNull();
  });

  it('announces the plan PARENT once for the whole DAG', () => {
    // Parent: runId === jobId. This is the single "workflow finished" nudge.
    const n = notificationsStore.pushFromTerminal(
      job({ jobId: 'run-9', runId: 'run-9', trioId: 'plan|plan-stemtranslate01' }));
    expect(n).not.toBeNull();
    expect(n!.message).toContain('workflow finished');
    expect(n!.label).toBe('Stemtranslate01');
  });

  it('skips non-terminal frames', () => {
    expect(notificationsStore.pushFromTerminal(job({ jobId: 'j5', status: 'RUNNING' }))).toBeNull();
  });

  it('dedupes by jobId across redelivery', () => {
    notificationsStore.pushFromTerminal(job({ jobId: 'dup' }));
    notificationsStore.pushFromTerminal(job({ jobId: 'dup' }));
    expect(notificationsStore.getSnapshot().items).toHaveLength(1);
  });

  it('markSeen and markAllSeen adjust the unread count', () => {
    notificationsStore.pushFromTerminal(job({ jobId: 'a' }));
    notificationsStore.pushFromTerminal(job({ jobId: 'b' }));
    expect(notificationsStore.getSnapshot().unseen).toBe(2);
    notificationsStore.markSeen('a');
    expect(notificationsStore.getSnapshot().unseen).toBe(1);
    notificationsStore.markAllSeen();
    expect(notificationsStore.getSnapshot().unseen).toBe(0);
  });

  it('persists across a reload (localStorage-backed)', () => {
    notificationsStore.pushFromTerminal(job({ jobId: 'persist-me' }));
    // Simulate reload: the module reads localStorage at import; assert the raw store.
    const raw = JSON.parse(localStorage.getItem('lab-notifications') || '[]');
    expect(raw.some((n: { jobId: string }) => n.jobId === 'persist-me')).toBe(true);
  });

  it('newest notifications come first', () => {
    notificationsStore.pushFromTerminal(job({ jobId: 'old' }));
    notificationsStore.pushFromTerminal(job({ jobId: 'new' }));
    expect(notificationsStore.getSnapshot().items[0].jobId).toBe('new');
  });
});

describe('completion notifications flag', () => {
  afterEach(() => { try { localStorage.removeItem('lab-completion-notifications-enabled'); } catch { /* ignore */ } });

  it('defaults to enabled', () => {
    expect(getCompletionNotificationsEnabled()).toBe(true);
  });

  it('can be turned off', () => {
    setCompletionNotificationsEnabled(false);
    expect(getCompletionNotificationsEnabled()).toBe(false);
  });
});

describe('labelForPlan', () => {
  it('uses the plan NAME, not the "plan" prefix', () => {
    // labelForTrio would return "Plan" for every DAG, making every workflow
    // notification read identically.
    expect(labelForPlan('plan|plan-meridian-facepipeline')).toBe('Meridian Facepipeline');
    expect(labelForPlan('plan|')).toBe('Workflow');
    expect(labelForPlan('')).toBe('Workflow');
  });
});

describe('rollupRenderUrls (any async job OR flow → notification renders the result)', () => {
  const j = (over: Partial<Job>): Job => job({ hasRenderableArtifacts: false, artifactUrls: [], ...over });

  it('returns a standalone job\'s own resolved artifactUrls', () => {
    expect(rollupRenderUrls({ jobId: 'a', artifactUrls: ['https://cdn/clip.mp4'] }, [])).toEqual(['https://cdn/clip.mp4']);
  });

  it('rolls up a FLOW\'s member deliverables when the parent has none (fal video + images under a plan)', () => {
    const jobs = [
      j({ jobId: 'm1', runId: 'run1', hasRenderableArtifacts: true, artifactUrls: ['https://fal/video.mp4'] }),
      j({ jobId: 'm2', runId: 'run1', hasRenderableArtifacts: true, artifactUrls: ['https://cdn/img.png'] }),
      j({ jobId: 'node', runId: 'run1', hasRenderableArtifacts: false, artifactUrls: [] }),          // non-renderable node
      j({ jobId: 'other', runId: 'run2', hasRenderableArtifacts: true, artifactUrls: ['https://cdn/nope.mp4'] }), // different run
    ];
    // parent notification carries runId=run1; its own artifactUrls empty → roll up run1 members
    expect(rollupRenderUrls({ jobId: 'parent-full-id', runId: 'run1' }, jobs))
      .toEqual(['https://fal/video.mp4', 'https://cdn/img.png']);
  });

  it('does NOT cross-match standalone jobs (a notification with no runId rolls up nothing)', () => {
    const jobs = [j({ jobId: 's1', runId: null, hasRenderableArtifacts: true, artifactUrls: ['https://cdn/z.mp4'] })];
    expect(rollupRenderUrls({ jobId: 'x' }, jobs)).toEqual([]);
  });
})
