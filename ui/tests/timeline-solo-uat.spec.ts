import { test, expect, Page, Route } from '@playwright/test';

/**
 * timeline-solo-uat (#2) — per-stem mute/solo. Each audio lane has a Solo button + a hidden <audio>
 * synced to the video. Soloing a stem unmutes ONLY that stem and mutes the video + other stems, so
 * you hear just the chosen stem; toggling it off restores the full mix (video audible, stems muted).
 */
const BASE = 'http://localhost:3000';
const CDN = 'https://dexamplecdn002.cloudfront.net';
const RUN = `so-${Date.now()}`;
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

const muted = (page: Page, sel: string) => page.locator(sel).first().evaluate((el) => (el as HTMLMediaElement).muted);

test.afterEach(async ({ request }) => { await request.delete(`${BASE}/sessions/${encodeURIComponent(SID)}`).catch(() => {}); });

test('soloing a stem plays only that stem (video + other stems muted); toggling restores the mix', async ({ page }) => {
  test.setTimeout(60000);
  await page.route('**/jobs**', (r) => { const u = r.request().url(); if (/\/jobs(\?|$|\/)/.test(u) && !u.includes('/jobs/stream')) return json(r, { jobs: [] }); return r.continue(); });
  await page.route('**/jobs/stream**', (r) => r.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
  const manifest = {
    id: 'c', template: 'composite-timeline', duration: 60, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK, clips: [],
    peaks: { 'audio-speech': [0.2, 0.4], 'audio-music': [0.3, 0.6] },
    tracks: [
      { id: 'video-1', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian.mp4`, start: 0, end: 60 },
      { id: 'audio-speech', type: 'audio', label: 'Audio Speech', src: `${CDN}/x/speech.wav`, start: 0, end: 60 },
      { id: 'audio-music', type: 'audio', label: 'Audio Music', src: `${CDN}/x/music.wav`, start: 0, end: 60 },
    ],
  };
  await page.route(`**/sessions/${SID}/renders**`, (r) => json(r, { sessionId: SID, renders: [{ key: `composite:${SK}`, composite: true, sourceKey: SK, memberKeys: ['a'], createdAt: '2026-08-27T10:00:00Z', label: 'Meridian', manifest }] }));
  await page.request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: SID } }).catch(() => {});
  await devLogin(page);
  const link = page.locator(`text=${SID}`).first(); if (await link.isVisible({ timeout: 12000 }).catch(() => false)) await link.click();
  await expect(page.locator('[data-testid="unified-timeline"]').first()).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId('solo-audio-speech')).toBeVisible({ timeout: 20000 });

  // Default (no solo): stems muted, video audible.
  expect(await muted(page, '[data-testid="stem-audio-audio-speech"]'), 'speech muted by default').toBe(true);
  expect(await muted(page, '[data-testid="stem-audio-audio-music"]'), 'music muted by default').toBe(true);
  expect(await muted(page, 'video'), 'video audible by default').toBe(false);

  // Solo speech → only speech audible; video + music muted.
  await page.getByTestId('solo-audio-speech').click();
  await expect.poll(() => muted(page, '[data-testid="stem-audio-audio-speech"]')).toBe(false);
  expect(await muted(page, '[data-testid="stem-audio-audio-music"]'), 'music muted while soloing speech').toBe(true);
  expect(await muted(page, 'video'), 'video muted while soloing a stem').toBe(true);

  // Toggle solo off → back to the full mix (video audible, stems muted).
  await page.getByTestId('solo-audio-speech').click();
  await expect.poll(() => muted(page, 'video')).toBe(false);
  expect(await muted(page, '[data-testid="stem-audio-audio-speech"]'), 'speech muted again after un-solo').toBe(true);
});
