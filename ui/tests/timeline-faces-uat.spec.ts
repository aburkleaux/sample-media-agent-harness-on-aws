import { test, expect, Page, Route } from '@playwright/test';

/**
 * timeline-faces-uat (#6) — face detection boxes overlay the composite video (toggle on/off) and a
 * "people" lane per detected person appears on the unified timeline (screen-time intervals).
 */
const BASE = 'http://localhost:3000';
const CDN = 'https://dexamplecdn002.cloudfront.net';
const RUN = `fc-${Date.now()}`;
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

test('face boxes overlay the video (toggle) + a people lane per person', async ({ page }) => {
  test.setTimeout(60000);
  await page.route('**/jobs**', (r) => { const u = r.request().url(); if (/\/jobs(\?|$|\/)/.test(u) && !u.includes('/jobs/stream')) return json(r, { jobs: [] }); return r.continue(); });
  await page.route('**/jobs/stream**', (r) => r.fulfill({ status: 200, contentType: 'text/event-stream', body: '' }));
  const manifest = {
    id: 'c', template: 'composite-timeline', duration: 40, createdAt: '2026-08-27T10:00:00Z', sourceKey: SK, clips: [], peaks: {},
    tracks: [{ id: 'video-1', type: 'video', src: `${CDN}/artifacts/${SID}/input/meridian.mp4`, start: 0, end: 40 }],
    annotations: [
      { entityId: 'entity-0', start: 0, end: 30, box: { x: 0.1, y: 0.1, width: 0.2, height: 0.3 }, confidence: 0.9 },
      { entityId: 'entity-1', start: 0, end: 20, box: { x: 0.55, y: 0.2, width: 0.2, height: 0.3 }, confidence: 0.9 },
      { entityId: 'entity-2', name: 'Kevin', start: 0, end: 25, box: { x: 0.35, y: 0.42, width: 0.15, height: 0.25 }, confidence: 0.95 },
    ],
  };
  await page.route(`**/sessions/${SID}/renders**`, (r) => json(r, { sessionId: SID, renders: [{ key: `composite:${SK}`, composite: true, sourceKey: SK, memberKeys: ['a'], createdAt: '2026-08-27T10:00:00Z', label: 'Meridian', manifest }] }));
  await page.request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: SID } }).catch(() => {});
  await devLogin(page);
  const link = page.locator(`text=${SID}`).first(); if (await link.isVisible({ timeout: 12000 }).catch(() => false)) await link.click();
  await expect(page.locator('video').first()).toBeVisible({ timeout: 20000 });

  // Boxes overlay the video by default (at t=0 all three people are on screen).
  await expect(page.locator('[data-testid="face-overlay"]')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('[data-testid="face-box-entity-0"]')).toBeVisible();
  await expect(page.locator('[data-testid="face-box-entity-2"]')).toBeVisible();

  // A people lane per detected person. Anonymous entity-N → friendly 'Person N'; a RESOLVED name
  // (from face recognition) is displayed as-is ('Kevin'), not 'Person 3'.
  await expect(page.locator('[data-testid="lane-person"]')).toHaveCount(3, { timeout: 20000 });
  await expect(page.getByText('Person 1', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Person 2', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Kevin', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('entity-0', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Person 3', { exact: true })).toHaveCount(0);
  // The named box also shows the name on the video overlay.
  await expect(page.locator('[data-testid="face-box-entity-2"]').getByText('Kevin', { exact: true })).toBeVisible();

  // Toggle OFF → overlay removed (boxes hidden); toggle back ON → overlay returns.
  await page.getByTestId('toggle-faces').click();
  await expect(page.locator('[data-testid="face-overlay"]')).toHaveCount(0);
  await page.getByTestId('toggle-faces').click();
  await expect(page.locator('[data-testid="face-overlay"]')).toBeVisible();
});
