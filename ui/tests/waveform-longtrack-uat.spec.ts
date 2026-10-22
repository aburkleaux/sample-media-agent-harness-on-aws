import { test, expect, Page, Route } from '@playwright/test';

/**
 * Long-track waveform rendering (regression guard).
 *
 * A browser <canvas> has a hard max dimension (~32767px). WaveformVisualizer sized the canvas at
 * duration × zoomLevel (100px/s), so a ~12-min soundtrack → 71894px, which silently fails to
 * allocate and paints NOTHING — the reported "audio track view broken" (a blank/broken-image bar).
 * The real 010cc740 session's full-soundtrack composite (718.936s, one audio-meridian_soundtrack
 * track with 71891 peaks) is reproduced here. The fix caps canvasWidth (+ clamps DPR) so the canvas
 * always renders. Real Chromium renders canvas (unlike vitest/jsdom), so we assert the bitmap is
 * within the limit AND that the waveform actually painted pixels.
 */
const BASE = 'http://localhost:3000';
const CDN = 'https://dexamplecdn002.cloudfront.net';
const RUN = `lw-${Date.now()}`;
const SID = `${RUN}-11111111-2222-3333-444444444444`;
const SK = `${RUN}/meridian.mp4`;
const json = (r: Route, b: unknown) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });

async function devLogin(p: Page) {
  await p.goto(BASE); await p.waitForTimeout(700);
  const e = p.locator("button:has-text('Enter the harness'), button:has-text('Explore the harness'), button:has-text('Sign in')").first();
  if (await e.isVisible({ timeout: 5000 }).catch(() => false)) { await e.click(); await p.waitForTimeout(800); }
  const d = p.locator("button:has-text('Dev Login')");
  if (await d.isVisible({ timeout: 6000 }).catch(() => false)) { await d.click(); await p.waitForTimeout(1500); }
}

test.afterEach(async ({ request }) => { await request.delete(`${BASE}/sessions/${encodeURIComponent(SID)}`).catch(() => {}); });

test('a ~12-min soundtrack renders a waveform (canvas width capped under the browser max)', async ({ page }) => {
  test.setTimeout(60000);
  await page.route('**/jobs**', (r) => { const u = r.request().url(); if (/\/jobs(\?|$|\/)/.test(u) && !u.includes('/jobs/stream')) return json(r, { jobs: [] }); return r.continue(); });
  await page.route('**/jobs/stream**', (r) => r.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
  const peaks = Array.from({ length: 71891 }, (_, i) => Math.abs(Math.sin(i / 50)) * 0.8);
  const manifest = { id: 'c', template: 'composite-timeline', duration: 718.936, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK, clips: [], peaks: { 'audio-meridian_soundtrack': peaks }, tracks: [
    { id: 'video-1', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian.mp4`, start: 0, end: 718.936 },
    { id: 'audio-meridian_soundtrack', type: 'audio', src: `${CDN}/x/soundtrack.wav`, start: 0, end: 718.936 },
  ] };
  await page.route(`**/sessions/${SID}/renders**`, (r) => json(r, { sessionId: SID, renders: [{ key: `composite:${SK}`, composite: true, sourceKey: SK, memberKeys: ['a'], createdAt: '2026-08-27T10:00:00Z', label: 'Meridian', manifest }] }));
  await page.request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: SID } }).catch(() => {});
  await devLogin(page);
  const link = page.locator(`text=${SID}`).first(); if (await link.isVisible({ timeout: 12000 }).catch(() => false)) await link.click();
  await expect(page.locator('[data-testid="lane-audio"]').first()).toBeVisible({ timeout: 20000 });

  const info = await page.locator('[data-testid="lane-audio"] canvas').first().evaluate((el) => {
    const c = el as HTMLCanvasElement;
    let painted = false;
    try {
      const ctx = c.getContext('2d');
      const d = ctx?.getImageData(0, 0, Math.min(80, c.width || 1), Math.min(20, c.height || 1)).data;
      if (d) { for (let i = 3; i < d.length; i += 4) { if (d[i] > 0) { painted = true; break; } } }
    } catch { painted = false; }
    return { bitmapW: c.width, clientH: c.clientHeight, painted };
  });
  // Bitmap must stay under the browser's hard max, and the waveform must actually have painted.
  expect(info.bitmapW, 'canvas bitmap width under the ~32767px browser max').toBeLessThanOrEqual(32767);
  expect(info.clientH, 'lane canvas rendered height').toBeGreaterThan(10);
  expect(info.painted, 'waveform canvas painted pixels (not a blank/broken bar)').toBe(true);
});
