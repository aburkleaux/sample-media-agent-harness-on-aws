/**
 * Uploads must land in the session-scoped INPUT prefix.
 *
 * `artifacts/<session>/input/<name>` is the documented convention
 * (docs/handoff-and-render-redesign.md §7 "KEEP: session-scoped locations
 * `artifacts/<session>/{input,output}/`"), and it is what makes an artifact's ROLE
 * derivable from its PATH — `/input/` is a source, `/output/` is a deliverable. That
 * matters because role is currently inferred from the `s3://` SCHEME
 * (`extract_source_uris`), which breaks the moment references become canonical
 * CloudFront https.
 *
 * Reality before this change: ZERO `/input/` objects existed and 269 uploads sat flat at
 * `artifacts/<timestamp>-<name>`, session-less. The agent path (`main.upload_file` ->
 * `_artifact_input_prefix`) implemented the convention correctly, but nothing called it:
 * the SPA uploads via ChatCanvas -> AgentService.upload -> `POST /upload`, the multipart
 * route, which built `artifacts/${Date.now()}-${filename}` and ignored the session.
 *
 * Back-compat is a hard requirement: the 269 existing flat keys must keep resolving, so
 * this only changes where NEW uploads are written. Reads are by key, so nothing migrates.
 */
import { describe, it, expect } from 'vitest';
import { uploadKeyFor } from './uploadKey';

describe('uploadKeyFor', () => {
  it('scopes an upload to the session INPUT prefix', () => {
    const key = uploadKeyFor('sess-abc', 'clip.mp4', 1787000000000);
    expect(key).toBe('artifacts/sess-abc/input/1787000000000-clip.mp4');
  });

  it('derives role from the path: /input/ marks a source', () => {
    // The property the whole change exists to enable.
    expect(uploadKeyFor('s1', 'a.mp4', 1)).toContain('/input/');
  });

  it('falls back to a session-less INPUT prefix when no session is known', () => {
    // Still under an `input/` segment so role stays path-derivable, and still unique.
    const key = uploadKeyFor('', 'clip.mp4', 1787000000000);
    expect(key).toBe('artifacts/uploads/input/1787000000000-clip.mp4');
    expect(key).toContain('/input/');
  });

  it('scopes a session-less upload to its uploader when given an owner tag', () => {
    const tag = 'u' + 'a'.repeat(32);
    expect(uploadKeyFor('', 'clip.mp4', 1787000000000, tag)).toBe(`artifacts/uploads/${tag}/input/1787000000000-clip.mp4`);
    // A session still wins: the tag only places an upload that has no session.
    expect(uploadKeyFor('s1', 'clip.mp4', 1, tag)).toBe('artifacts/s1/input/1-clip.mp4');
    // A tag cannot escape the prefix either.
    expect(uploadKeyFor('', 'clip.mp4', 1, '../../x')).toBe('artifacts/uploads/x/input/1-clip.mp4');
  });

  it('keeps names unique within a session so two same-named files cannot clobber', () => {
    // Deliberate tradeoff: a re-upload is NOT idempotent, but a different file with the
    // same name can never overwrite one the user already has. See uploadKey.ts.
    const a = uploadKeyFor('s1', 'upload', 1);
    const b = uploadKeyFor('s1', 'upload', 2);
    expect(a).not.toBe(b);
  });

  it('sanitises a session id that could escape the prefix', () => {
    const key = uploadKeyFor('../../etc', 'x.mp4', 1);
    expect(key.startsWith('artifacts/')).toBe(true);
    expect(key).not.toContain('..');
  });

  it('sanitises a filename that could escape the prefix', () => {
    const key = uploadKeyFor('s1', '../../../evil.sh', 1);
    expect(key).toBe('artifacts/s1/input/1-evil.sh');
    expect(key).not.toContain('..');
  });

  it('strips a leading slash and directory components from the filename', () => {
    expect(uploadKeyFor('s1', '/tmp/nested/clip.mp4', 1)).toBe('artifacts/s1/input/1-clip.mp4');
  });

  it('never produces an empty filename segment', () => {
    const key = uploadKeyFor('s1', '', 1787000000000);
    expect(key).toBe('artifacts/s1/input/1787000000000-upload');
  });
});
