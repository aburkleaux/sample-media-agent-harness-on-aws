/**
 * Renderer result tabs: one tab per result, labelled by plan node name, with the
 * manifest composed lazily on first open.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));

import { authFetch } from '../auth/authFetch';
import { buildResultTabs, tabLabel, renderTab } from './renderTabs';
import type { Job } from '../types/jobs';

const mockAuthFetch = authFetch as unknown as ReturnType<typeof vi.fn>;

function job(over: Partial<Job> = {}): Job {
  return {
    jobId: 'j1',
    trioId: 'shot-segmentation|segment_shots',
    status: 'SUCCEEDED',
    sessionId: 's1',
    runId: null,
    nodeId: null,
    dependsOn: [],
    artifactUrls: ['s3://b/k/out.mp4'],
    ...over,
  } as unknown as Job;
}

describe('tabLabel', () => {
  it('uses the TASK name, not the planner\'s internal node id', () => {
    // sg2_stems is bookkeeping and means nothing to the person reading the tabs.
    expect(tabLabel(job({ nodeId: 'sg2_stems', trioId: 'audio-stem|separate_audio_stems' })))
      .toBe('Separate Audio Stems');
  });

  it('titleizes the tool name for each M2C task', () => {
    expect(tabLabel(job({ trioId: 'shot-segmentation|segment_shots' }))).toBe('Segment Shots');
    expect(tabLabel(job({ trioId: 'speech-diarization|diarize_speech' }))).toBe('Diarize Speech');
    expect(tabLabel(job({ trioId: 'face-recognition|recognize_faces' }))).toBe('Recognize Faces');
    expect(tabLabel(job({ trioId: 'media-processing|process_media' }))).toBe('Process Media');
  });

  it('falls back to the gateway, then the node id, when there is no tool', () => {
    expect(tabLabel(job({ trioId: 'audio-stem' }))).toBe('Audio Stem');
    expect(tabLabel(job({ trioId: '', nodeId: 'sg9_x' }))).toBe('sg9_x');
  });
});

describe('buildResultTabs', () => {
  it('makes one unrendered tab for a standalone job', () => {
    const tabs = buildResultTabs(job(), [job()]);
    expect(tabs).toHaveLength(1);
    // Unrendered: no blocks yet, but the artifacts needed to render are carried.
    expect(tabs[0].blocks).toEqual([]);
    expect(tabs[0].artifactUrls).toEqual(['s3://b/k/out.mp4']);
    expect(tabs[0].jobId).toBe('j1');
  });

  it('makes one tab per DAG node that produced artifacts', () => {
    const plan = job({ jobId: 'run-1', runId: 'run-1', trioId: 'plan|plan-abc', artifactUrls: [] });
    const all = [
      plan,
      // node| rows are bookkeeping — never their own tab
      job({ jobId: 'run-1#n1', runId: 'run-1', trioId: 'node|sg1_trim', artifactUrls: [] }),
      job({ jobId: 'a', runId: 'run-1', nodeId: 'sg1_trim',
             trioId: 'media-processing|process_media', artifactUrls: ['s3://b/clip.mp4'] }),
      job({ jobId: 'b', runId: 'run-1', nodeId: 'sg2_stems',
             trioId: 'audio-stem|separate_audio_stems', artifactUrls: ['s3://b/s1.wav', 's3://b/s2.wav'] }),
      // no artifacts -> no tab
      job({ jobId: 'c', runId: 'run-1', nodeId: 'sg3_none',
             trioId: 'speech-diarization|diarize_speech', artifactUrls: [] }),
    ];
    const tabs = buildResultTabs(plan, all);
    // Task names, not node ids.
    expect(tabs.map(t => t.label)).toEqual(['Process Media', 'Separate Audio Stems']);
    expect(tabs[1].artifactUrls).toHaveLength(2);
  });

  it('numbers repeated task names so duplicate tabs are distinguishable', () => {
    // A plan may run the same tool twice; two tabs both reading "Process Media"
    // would be impossible to tell apart.
    const plan = job({ jobId: 'run-1', runId: 'run-1', trioId: 'plan|p', artifactUrls: [] });
    const all = [
      plan,
      job({ jobId: 'a', runId: 'run-1', nodeId: 'n1',
             trioId: 'media-processing|process_media', artifactUrls: ['s3://b/1.mp4'] }),
      job({ jobId: 'b', runId: 'run-1', nodeId: 'n2',
             trioId: 'media-processing|process_media', artifactUrls: ['s3://b/2.mp4'] }),
      job({ jobId: 'c', runId: 'run-1', nodeId: 'n3',
             trioId: 'audio-stem|separate_audio_stems', artifactUrls: ['s3://b/3.wav'] }),
    ];
    const labels = buildResultTabs(plan, all).map(t => t.label);
    expect(labels).toEqual(['Process Media 1', 'Process Media 2', 'Separate Audio Stems']);
  });

  it('excludes jobs from other runs', () => {
    const plan = job({ jobId: 'run-1', runId: 'run-1', trioId: 'plan|p', artifactUrls: [] });
    const all = [plan, job({ jobId: 'x', runId: 'run-2', nodeId: 'other', artifactUrls: ['s3://b/x.mp4'] })];
    expect(buildResultTabs(plan, all)).toHaveLength(0);
  });
});

describe('renderTab', () => {
  beforeEach(() => mockAuthFetch.mockReset());

  it('sends a single url as { url }', async () => {
    mockAuthFetch.mockResolvedValue({ ok: true, json: async () => ({ manifestUrl: '/api/artifact?key=m' }) });
    const out = await renderTab(['s3://b/a.mp4']);
    expect(out).toBe('/api/artifact?key=m');
    expect(JSON.parse(mockAuthFetch.mock.calls[0][1].body)).toEqual({ url: 's3://b/a.mp4' });
  });

  it('sends one node\'s several artifacts as { urls } and never a sourceUrl', async () => {
    // sourceUrl would push the Render Agent onto its annotation branch, which
    // renders only that source and drops every other track.
    mockAuthFetch.mockResolvedValue({ ok: true, json: async () => ({ manifestUrl: '/m' }) });
    await renderTab(['s3://b/s1.wav', 's3://b/s2.wav']);
    const body = JSON.parse(mockAuthFetch.mock.calls[0][1].body);
    expect(body.urls).toHaveLength(2);
    expect(body.sourceUrl).toBeUndefined();
  });

  it('forwards the session_id so the manifest is written under artifacts/<sid>/render/', async () => {
    // Session 6dffd3ec fix: without session_id, /render invents a random render-<ts> session, so
    // the uploaded SOURCE video's manifest is orphaned and analyses of the original never merge.
    mockAuthFetch.mockResolvedValue({ ok: true, json: async () => ({ manifestUrl: '/m' }) });
    const sid = '6dffd3ec-52bf-4557-b021-873ca7d5a0ce'; // 36 chars
    await renderTab(['s3://b/a.mp4'], sid);
    expect(JSON.parse(mockAuthFetch.mock.calls[0][1].body).session_id).toBe(sid);
  });

  it('omits a too-short session_id (the /render handler ignores <33 chars anyway)', async () => {
    mockAuthFetch.mockResolvedValue({ ok: true, json: async () => ({ manifestUrl: '/m' }) });
    await renderTab(['s3://b/a.mp4'], 'short');
    expect(JSON.parse(mockAuthFetch.mock.calls[0][1].body).session_id).toBeUndefined();
  });

  it('returns null on failure rather than throwing', async () => {
    mockAuthFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
    expect(await renderTab(['s3://b/a.mp4'])).toBeNull();
    expect(await renderTab([])).toBeNull();
  });

  it('surfaces only the nodes that produced artifacts in a partially-failed run', () => {
    // Observed on staging: plan-trimshotdiar01 finished 2/3 — sg1_trim and
    // sg2_shots produced artifacts, sg3_diarize failed with none. The two
    // successful nodes' results must still be reachable.
    const plan = job({ jobId: 'run-1', runId: 'run-1', trioId: 'plan|plan-trimshotdiar01', artifactUrls: [] });
    const all = [
      plan,
      job({ jobId: 'a', runId: 'run-1', nodeId: 'sg1_trim',
             trioId: 'media-processing|process_media', artifactUrls: ['s3://b/clip.mp4'] }),
      job({ jobId: 'b', runId: 'run-1', nodeId: 'sg2_shots',
             trioId: 'shot-segmentation|segment_shots', artifactUrls: ['s3://b/shots.json'] }),
      job({ jobId: 'c', runId: 'run-1', nodeId: 'sg3_diarize', status: 'FAILED',
             trioId: 'speech-diarization|diarize_speech', artifactUrls: [] }),
    ];
    const tabs = buildResultTabs(plan, all);
    expect(tabs.map(t => t.label)).toEqual(['Process Media', 'Segment Shots']);
    // the failed, artifact-less node contributes no tab
    expect(tabs.some(t => t.jobId === 'c')).toBe(false);
  });
});
