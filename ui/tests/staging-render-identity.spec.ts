import { test, expect, request } from '@playwright/test';

/**
 * STAGING verification of the render-identity work, against real manifests.
 *
 * Encodes the checks that were run by hand on 2026-08-19 after deploying `472b17c`, so the
 * same verification is repeatable instead of living in a transcript. Each assertion maps to a
 * specific defect observed in a captured session:
 *
 *   session a2ebd1e9  shots    -> render-image / image-annotated, 0 tracks, empty source
 *                     diarize  -> render-audio / nle-timeline (a bare audio timeline)
 *   session ee8b405b  a bad artifact url rendered the harness UI inside the document viewer
 *
 * Needs no login: `/api/artifact` is a deliberately public, key-validated read-through, which
 * is also what makes these assertions cheap to re-run.
 *
 * The session under test defaults to the one produced by that verification run. Point it at a
 * newer one with RENDER_SESSION=<session-id>, having asked the harness to run shot
 * segmentation + speaker diarization on a video.
 */
const BASE = process.env.STAGING_URL ?? 'https://dexamplecdn004.cloudfront.net';
const SESSION = process.env.RENDER_SESSION ?? 'b37cc008-88fd-43f6-934b-f31d743ae1bc';

const manifestUrl = (name: string) =>
  `${BASE}/api/artifact?key=${encodeURIComponent(`artifacts/${SESSION}/render/${name}.json`)}`;

async function manifest(name: string) {
  const ctx = await request.newContext();
  const res = await ctx.get(manifestUrl(name));
  expect(res.status(), `${name} should be served`).toBe(200);
  expect(res.headers()['content-type'] ?? '').toContain('application/json');
  const body = await res.json();
  await ctx.dispose();
  return body;
}

test.describe('staging · render identity + view selection', () => {
  test('shot segmentation renders as a MARKER view over its source, not an image gallery', async () => {
    // The a2ebd1e9 defect: this manifest read render-image / image-annotated with 0 tracks.
    const m = await manifest('sg1_shots--other');
    expect(m.renderSkill).toBe('render-shot-segmentation');
    expect(m.template).toBe('video-markers');
    expect((m.markers ?? []).length).toBeGreaterThan(10);
    const tracks = m.tracks ?? [];
    expect(tracks.some((t: any) => t.type === 'video' && t.src)).toBe(true);
  });

  test("the shot view's source is the uploaded INPUT, proving the upload→render chain", async () => {
    // Upload placement (artifacts/<session>/input/) -> path-derived source role -> attached to
    // the render. Previously the source was recovered from a job row and came back empty.
    const m = await manifest('sg1_shots--other');
    const src = (m.tracks ?? []).find((t: any) => t.type === 'video')?.src ?? '';
    expect(src).toContain(`/artifacts/${SESSION}/input/`);
    expect(src.startsWith('https://')).toBe(true);
  });

  test('speaker diarization renders as a SPEAKER view, not a bare audio timeline', async () => {
    // The a2ebd1e9 defect: render-audio / nle-timeline, with no speaker structure.
    const m = await manifest('sg2_diarize--other');
    expect(m.renderSkill).toBe('render-speaker-diarization');
    expect(m.renderSkill).not.toBe('render-audio');
    expect((m.segments ?? []).length).toBeGreaterThan(10);
  });

  test('one node with two views gets two DISTINCT manifests, neither overwriting the other', async () => {
    // The sub-key. With a single key per node, the second view silently replaced the first and
    // which one survived was a race.
    const a = await manifest('sg1_shots--other');
    const b = await manifest('sg1_shots--video');
    expect(a.id).not.toBe(b.id);
  });

  test('/api/artifact never answers with the SPA, whatever the key', async () => {
    // The ee8b405b symptom, from its second cause: CloudFront maps 403/404 -> 200 /index.html
    // distribution-wide, so a refused or missing key used to arrive as 200 text/html and an
    // iframe rendered the harness inside itself.
    const ctx = await request.newContext();
    for (const [key, expected] of [
      ['secrets/creds.json', 400],
      [`artifacts/${SESSION}/render/definitely-not-here.json`, 410],
    ] as const) {
      const res = await ctx.get(`${BASE}/api/artifact?key=${encodeURIComponent(key)}`);
      expect(res.status(), `key=${key}`).toBe(expected);
      expect(res.headers()['content-type'] ?? '', `key=${key}`).not.toContain('text/html');
    }
    await ctx.dispose();
  });

  test('the auth exemption for /api/artifact stops at a path boundary', async () => {
    // `startsWith("/api/artifact")` also exempted /api/artifacts, /api/artifact-registry, ...
    // The middleware's own 401 message is what proves IT gated the request rather than a
    // downstream handler's defensive check.
    const ctx = await request.newContext();
    for (const p of ['/api/artifactsomething', '/api/artifacts', '/api/artifact-registry']) {
      const res = await ctx.get(`${BASE}${p}`);
      expect(res.status(), p).toBe(401);
      const body = await res.json().catch(() => ({}));
      expect(String(body?.error?.message ?? ''), p).toContain('Authorization header');
    }
    await ctx.dispose();
  });
});
