import { test, expect, Page, Route } from '@playwright/test';

/**
 * render-stability-uat — REAL-browser proof of the server-authoritative render architecture:
 * the proxy `/sessions/:id/renders` composes ONE finished composite per source, and the UI just
 * RENDERS it (no client-side merge). This spec mocks `/renders` to return server composites and
 * asserts the UI is STABLE — no tabs appearing/disappearing/reordering when the composite updates
 * (a new analysis folding in server-side). This is the guard for the churn the user reported.
 */
const BASE = 'http://localhost:3000';
const CDN = 'https://dexamplecdn002.cloudfront.net';
// Per-test unique ids (reset in beforeEach) so tests don't share one server-side session row — the
// shared-id state bleed made later tests in the file flake when run serially. Helpers read these at
// call time, so regenerating them before each test isolates every test's session/renders.
let RUN = `rs-${Date.now()}`;
let SID = `${RUN}-11111111-2222-3333-444444444444`;
let SK = `${RUN}/meridian.mp4`;

// A server COMPOSITE render entry (what /sessions/:id/renders returns after composing per source).
const compositeManifest = (extraTracks: unknown[] = [], extra: Record<string, unknown> = {}) => ({
  id: 'c', template: 'composite-timeline', duration: 60, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK,
  clips: [], peaks: { 'audio-full': [0.1, 0.2, 0.15] },
  tracks: [
    { id: 'video-src', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian.mp4`, start: 0, end: 60 },
    { id: 'audio-full', type: 'audio', src: `${CDN}/artifacts/${SID}/output/audio.wav`, start: 0, end: 60 },
    ...extraTracks,
  ],
  ...extra,
});
const compositeEntry = (manifest: unknown, memberKeys: string[]) => ({
  key: `composite:${SK}`, composite: true, sourceKey: SK, memberKeys,
  createdAt: '2026-08-27T10:00:00Z', label: 'Meridian', manifest,
});

const json = (route: Route, body: unknown) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

const jobsBody = () => ({ jobs: [{
  jobId: `${RUN}-job`, sessionId: SID, status: 'SUCCEEDED', hasRenderableArtifacts: true,
  trioId: 'media-processing|process_media', runId: null, nodeId: null,
  updatedAt: new Date().toISOString(), terminalAt: new Date().toISOString(), artifactUrls: [],
}] });

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(800);
  const enter = p.locator("button:has-text('Enter the harness'), button:has-text('Explore the harness'), button:has-text('Sign in')").first();
  if (await enter.isVisible({ timeout: 5000 }).catch(() => false)) { await enter.click(); await p.waitForTimeout(1000); }
  const dev = p.locator("button:has-text('Dev Login')");
  if (await dev.isVisible({ timeout: 6000 }).catch(() => false)) { await dev.click(); await p.waitForTimeout(2000); }
}
async function openSession(p: Page) {
  await p.request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: SID } }).catch(() => {});
  await devLogin(p);
  const link = p.locator(`text=${SID}`).first();
  if (await link.isVisible({ timeout: 12000 }).catch(() => false)) await link.click();
  await p.waitForTimeout(1000);
}
async function mockJobs(p: Page) {
  await p.route('**/jobs**', (route) => {
    const u = route.request().url();
    if (/\/jobs(\?|$|\/)/.test(u) && !u.includes('/jobs/stream')) return json(route, jobsBody());
    return route.continue();
  });
  await p.route('**/jobs/stream**', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
}

test.describe('render stability (server composes; UI renders finished)', () => {
  test.beforeEach(() => {
    RUN = `rs-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    SID = `${RUN}-11111111-2222-3333-444444444444`;
    SK = `${RUN}/meridian.mp4`;
  });
  test.afterEach(async ({ request }) => { await request.delete(`${BASE}/sessions/${encodeURIComponent(SID)}`).catch(() => {}); });

  test('a server composite renders as ONE tab with video + audio lane', async ({ page }) => {
    test.setTimeout(60_000);
    await mockJobs(page);
    await page.route(`**/sessions/${SID}/renders**`, (route) =>
      json(route, { sessionId: SID, renders: [compositeEntry(compositeManifest(), ['a.json', 'b.json'])] }));
    await openSession(page);
    await expect(page.locator('video').first()).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[data-testid="lane-audio"]').first()).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[role="tab"]')).toHaveCount(1, { timeout: 20000 });
  });

  test('4 stem lanes render compactly and labeled (all four visible, not just 2)', async ({ page }) => {
    test.setTimeout(60_000);
    await mockJobs(page);
    // A composite with the 4 stem tracks (speech/music/sfx/mixdown) — the exact case the user hit.
    const stemsManifest = {
      id: 'c', template: 'composite-timeline', duration: 73, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK, clips: [],
      peaks: { 'audio-speech': [0.2, 0.4], 'audio-music': [0.3], 'audio-sfx': [0.1], 'audio-mix': [0.5] },
      tracks: [
        { id: 'video-src', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian_trim.mp4`, start: 0, end: 73 },
        { id: 'audio-speech', type: 'audio', label: 'Audio Speech', src: `${CDN}/x/audio_speech.wav`, start: 0, end: 73 },
        { id: 'audio-music', type: 'audio', label: 'Audio Music', src: `${CDN}/x/audio_music.wav`, start: 0, end: 73 },
        { id: 'audio-sfx', type: 'audio', label: 'Audio Sfx', src: `${CDN}/x/audio_sfx.wav`, start: 0, end: 73 },
        { id: 'audio-mix', type: 'audio', label: 'Audio 48k Mono', src: `${CDN}/x/audio_48k_mono.wav`, start: 0, end: 73 },
      ],
    };
    await page.route(`**/sessions/${SID}/renders**`, (route) =>
      json(route, { sessionId: SID, renders: [compositeEntry(stemsManifest, ['a.json'])] }));
    await openSession(page);
    await expect(page.locator('[data-testid="unified-timeline"]').first()).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[data-testid="lane-audio"]')).toHaveCount(4, { timeout: 20000 });
    // ALL 4 stem lanes present + labeled
    for (const lbl of ['Audio Speech', 'Audio Music', 'Audio Sfx', 'Audio 48k Mono']) {
      await expect(page.getByText(lbl, { exact: true }).first()).toBeVisible({ timeout: 20000 });
    }
    // The lane must actually RENDER a sized waveform canvas. The regression put the waveform in a
    // flex-item parent, which zeroed the <canvas role="img"> height so it drew as a broken-image
    // icon. Assert each of the 4 lane canvases has non-zero rendered width AND height (real Chromium
    // renders canvas; this is the guard the label-only checks missed).
    const canvases = page.locator('[data-testid="lane-audio"] canvas');
    await expect(canvases).toHaveCount(4, { timeout: 20000 });
    for (let i = 0; i < 4; i++) {
      const dims = await canvases.nth(i).evaluate((el) => {
        const c = el as HTMLCanvasElement;
        return { clientW: c.clientWidth, clientH: c.clientHeight, bitmapH: c.height };
      });
      expect(dims.clientH, `lane ${i} canvas rendered height`).toBeGreaterThan(10);
      expect(dims.clientW, `lane ${i} canvas rendered width`).toBeGreaterThan(10);
      expect(dims.bitmapH, `lane ${i} canvas bitmap height`).toBeGreaterThan(0);
    }
  });

  test('ANTI-CHURN: composite updates (a lane folds in) → SAME tab, count/order stable, content grows', async ({ page }) => {
    test.setTimeout(90_000);
    // First list calls: composite with video+audio. Later calls: composite WITH a shots lane
    // (markers) — simulating a new analysis composed server-side. The tab must stay stable.
    let call = 0;
    await mockJobs(page);
    await page.route(`**/sessions/${SID}/renders**`, (route) => {
      const n = call++;
      const manifest = n < 2
        ? compositeManifest()
        : compositeManifest([], { markers: [{ id: 'm0', time: 0, label: 'shot 1' }, { id: 'm1', time: 20, label: 'shot 2' }] });
      return json(route, { sessionId: SID, renders: [compositeEntry(manifest, ['a.json', 'b.json', 'shots.json'])] });
    });
    await openSession(page);
    await expect(page.locator('video').first()).toBeVisible({ timeout: 20000 });
    await expect(page.locator('[role="tab"]')).toHaveCount(1, { timeout: 20000 });
    // let the reconcile ladder re-fetch and pick up the updated composite (with the shots lane)
    await page.waitForTimeout(8000);
    // STILL exactly one tab (no churn — no new tab, no disappear), video still present
    await expect(page.locator('[role="tab"]')).toHaveCount(1);
    await expect(page.locator('video').first()).toBeVisible();
    // content grew in place: the shots lane appeared (markers with shot labels render as "Shots")
    await expect(page.getByText(/shots?/i).first()).toBeVisible({ timeout: 15000 });
  });

  test('late source: solo audio then a composite → the solo is absorbed into ONE tab (not two)', async ({ page }) => {
    test.setTimeout(90_000);
    let call = 0;
    await mockJobs(page);
    await page.route(`**/sessions/${SID}/renders**`, (route) => {
      const n = call++;
      if (n < 2) {
        // only the audio exists yet → a solo audio render
        return json(route, { sessionId: SID, renders: [{
          key: `artifacts/${SID}/render/audio.json`, label: 'Audio', createdAt: '2026-08-27T10:05:00Z',
          manifest: { id: 'a', template: 'nle-timeline', duration: 60, clips: [], sourceKey: SK,
            tracks: [{ id: 'audio-full', type: 'audio', src: `${CDN}/artifacts/${SID}/output/audio.wav`, start: 0, end: 60 }],
            peaks: { 'audio-full': [0.1, 0.2] } },
        }] });
      }
      // source landed → server now returns the composite, absorbing the audio solo
      return json(route, { sessionId: SID, renders: [compositeEntry(compositeManifest(), [`artifacts/${SID}/render/audio.json`])] });
    });
    await page.route('**/api/artifact**', (route) => {
      if (route.request().url().includes('audio.json')) return json(route, { id: 'a', template: 'nle-timeline', duration: 60, clips: [], sourceKey: SK, tracks: [{ id: 'audio-full', type: 'audio', src: `${CDN}/artifacts/${SID}/output/audio.wav`, start: 0, end: 60 }], peaks: { 'audio-full': [0.1, 0.2] } });
      return route.continue();
    });
    await openSession(page);
    // once the composite arrives, the video appears and there is exactly ONE tab (audio absorbed)
    await expect(page.locator('video').first()).toBeVisible({ timeout: 45000 });
    await expect(page.locator('[role="tab"]')).toHaveCount(1, { timeout: 45000 });
  });

  test('(auto-follow) a newly composed tab (e.g. a trim) is auto-selected, not left in the background', async ({ page }) => {
    test.setTimeout(90_000);
    const SK2 = `${RUN}/trim.mp4`;
    const comp = (sk: string, ts: string, label: string) => ({
      key: `composite:${sk}`, composite: true, sourceKey: sk, memberKeys: ['a'], createdAt: ts, label,
      manifest: { id: sk, template: 'composite-timeline', duration: 60, sourceKey: sk, clips: [], peaks: { 'audio-full': [0.1, 0.2] },
        tracks: [{ id: `video-${sk}`, type: 'video', src: `${CDN}/artifacts/${SID}/input/${label}.mp4`, start: 0, end: 60 }] },
    });
    let call = 0;
    await mockJobs(page);
    // First list: only the source composite. Later lists: a NEW trim composite appears too.
    await page.route(`**/sessions/${SID}/renders**`, (route) => {
      const n = call++;
      const renders = n < 1
        ? [comp(SK, '2026-08-27T10:00:00Z', 'Meridian')]
        : [comp(SK, '2026-08-27T10:00:00Z', 'Meridian'), comp(SK2, '2026-08-27T10:05:00Z', 'Trim')];
      return json(route, { sessionId: SID, renders });
    });
    await openSession(page);
    await expect(page.locator('video').first()).toBeVisible({ timeout: 20000 });
    // The trim composite arrives via the reconcile → 2 tabs, and the NEW one is auto-selected.
    await expect(page.locator('[role="tab"]')).toHaveCount(2, { timeout: 30000 });
    const selected = page.locator('[role="tab"][aria-selected="true"]');
    await expect(selected).toHaveCount(1);
    await expect(selected).toHaveText(/Trim/, { timeout: 10000 });
  });
});
