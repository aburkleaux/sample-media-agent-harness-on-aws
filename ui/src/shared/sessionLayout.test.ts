import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  isProjectSessionOutputKey, parseSessionKey, projectSessionOutputPrefix, SESSION_OUTPUT_KEY_RE,
} from './sessionLayout';

// The SAME fixture the Python helper and the Lambda copies load (asset-lineage Phase 3 (a)).
const CASES = JSON.parse(readFileSync(
  resolve(__dirname, '../../../agent/tests/fixtures/session_layout_cases.json'), 'utf8'));

describe('sessionLayout (shared fixture)', () => {
  for (const c of CASES.keys as { key: string; parsed: unknown; isProjectSessionOutput: boolean }[]) {
    it(`parses ${JSON.stringify(c.key)}`, () => {
      expect(parseSessionKey(c.key)).toEqual(c.parsed);
      expect(isProjectSessionOutputKey(c.key)).toBe(c.isProjectSessionOutput);
    });
  }

  it('builds the project output prefix the Python writer uses', () => {
    const w = (CASES.writers as { sid: string; pid: string | null; flag: string; expectedPrefix: string }[])
      .find((x) => x.flag === 'project' && x.pid && x.expectedPrefix.startsWith('projects/'))!;
    expect(projectSessionOutputPrefix(w.pid!, w.sid)).toBe(w.expectedPrefix);
  });

  it('SESSION_OUTPUT_KEY_RE captures pid and sid', () => {
    const m = SESSION_OUTPUT_KEY_RE.exec(`projects/${CASES.pid}/sessions/${CASES.sid}/output/a.mp4`);
    expect(m?.[1]).toBe(CASES.pid);
    expect(m?.[2]).toBe(CASES.sid);
    expect(SESSION_OUTPUT_KEY_RE.exec(`projects/${CASES.pid}/sessions/${CASES.sid}/render/a.json`)).toBeNull();
  });
});

import { classifyUrl, deriveArtifactKey } from '../components/chat/artifactUrl';

describe('chat artifactUrl — projects-session outputs', () => {
  const KEY = `projects/${CASES.pid}/sessions/${CASES.sid}/output/clip.mp4`;
  it('deriveArtifactKey keeps a session-output CDN path, drops a project asset path', () => {
    expect(deriveArtifactKey(`https://d1.cloudfront.net/${KEY}?x=1`)).toBe(KEY);
    expect(deriveArtifactKey(`https://d1.cloudfront.net/projects/${CASES.pid}/video:x/a.mp4`)).toBeNull();
  });
  it('classifies it as ours', () => {
    expect(classifyUrl(`https://d1.cloudfront.net/${KEY}`).ourArtifact).toBe(true);
  });
});
