import { test, expect, Page, Route } from '@playwright/test';

/**
 * timeline-sync-uat (#1) — every lane shares ONE horizontal time scale, and one zoom control scales
 * them all together. Seeds a composite with video + audio + shots (markers) + speaker lanes and
 * asserts: (a) all lane TRACK areas have the same left + width (they line up), and (b) clicking
 * zoom-in widens the shared inner container (every lane scales together), keeping them aligned.
 */
const BASE = 'http://localhost:3000';
const CDN = 'https://dexamplecdn002.cloudfront.net';
const RUN = `ts-${Date.now()}`;
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

test('all timeline lanes share one scale and zoom together', async ({ page }) => {
  test.setTimeout(60000);
  await page.route('**/jobs**', (r) => { const u = r.request().url(); if (/\/jobs(\?|$|\/)/.test(u) && !u.includes('/jobs/stream')) return json(r, { jobs: [] }); return r.continue(); });
  await page.route('**/jobs/stream**', (r) => r.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
  const manifest = {
    id: 'c', template: 'composite-timeline', duration: 80, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK, clips: [],
    peaks: { 'audio-music': [0.2, 0.5, 0.3, 0.6] },
    tracks: [
      { id: 'video-1', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian.mp4`, start: 0, end: 80 },
      { id: 'audio-music', type: 'audio', label: 'Audio Music', src: `${CDN}/x/music.wav`, start: 0, end: 80 },
    ],
    markers: [
      { id: 's0', time: 0, endTime: 20, label: '0', category: 'content' },
      { id: 's1', time: 20, endTime: 45, label: '1', category: 'content' },
      { id: 's2', time: 45, endTime: 80, label: '2', category: 'black' },
    ],
    segments: [
      { start: 2, end: 18, group: 'SPEAKER_00' },
      { start: 22, end: 40, group: 'SPEAKER_01' },
    ],
  };
  await page.route(`**/sessions/${SID}/renders**`, (r) => json(r, { sessionId: SID, renders: [{ key: `composite:${SK}`, composite: true, sourceKey: SK, memberKeys: ['a'], createdAt: '2026-08-27T10:00:00Z', label: 'Meridian', manifest }] }));
  await page.request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: SID } }).catch(() => {});
  await devLogin(page);
  const link = page.locator(`text=${SID}`).first(); if (await link.isVisible({ timeout: 12000 }).catch(() => false)) await link.click();
  await expect(page.locator('[data-testid="unified-timeline"]').first()).toBeVisible({ timeout: 20000 });

  // #2 — shot numbers match the extracted shot files (0-based). The first shot is "0", not "1".
  await expect(page.locator('[data-testid="lane-shots"]').getByText('0', { exact: true })).toBeVisible({ timeout: 20000 });
  await expect(page.locator('[data-testid="lane-shots"]').getByText('1', { exact: true })).toBeVisible();

  // We have a ruler + audio + shots + 2 speaker lanes = 5 lane tracks.
  const trackBoxes = async () => page.locator('[data-testid="lane-track"]').evaluateAll(
    (els) => els.map((e) => { const r = e.getBoundingClientRect(); return { left: Math.round(r.left), width: Math.round(r.width) }; }),
  );
  const before = await trackBoxes();
  expect(before.length).toBeGreaterThanOrEqual(5); // ruler, audio, shots, speaker_00, speaker_01
  // (a) ALL lane tracks share the same left + width → they line up on one scale.
  for (const b of before) {
    expect(Math.abs(b.left - before[0].left), 'lane track left aligned').toBeLessThanOrEqual(1);
    expect(Math.abs(b.width - before[0].width), 'lane track width aligned').toBeLessThanOrEqual(1);
  }

  // (b) Zoom in → the shared inner container widens (every lane scales together), still aligned.
  const innerBefore = await page.locator('[data-testid="lanes-inner"]').evaluate((e) => Math.round(e.getBoundingClientRect().width));
  await page.getByTestId('zoom-in').click();
  await expect(page.getByTestId('zoom-level')).toHaveText('2×');
  const innerAfter = await page.locator('[data-testid="lanes-inner"]').evaluate((e) => Math.round(e.getBoundingClientRect().width));
  expect(innerAfter, 'zoom widens the shared timeline').toBeGreaterThan(innerBefore);
  const after = await trackBoxes();
  for (const b of after) {
    expect(Math.abs(b.left - after[0].left), 'lanes still aligned after zoom').toBeLessThanOrEqual(1);
    expect(Math.abs(b.width - after[0].width), 'lane widths still equal after zoom').toBeLessThanOrEqual(1);
  }
  expect(after[0].width, 'every lane widened with zoom').toBeGreaterThan(before[0].width);
});
