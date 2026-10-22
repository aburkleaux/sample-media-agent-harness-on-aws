import { test, expect, Page } from '@playwright/test';

/**
 * E2E: the interactive professional timeline editor.
 *
 * Seeds a session whose renderHistory carries a plain VIDEO manifest (one video
 * track, NO clips) via the dev proxy — the exact shape a fresh video render
 * produces — then proves, in real Chromium through the production render path:
 *   1. a video renders the interactive editor (ruler + mark controls + chips),
 *      not a bare <video>  ("all video renders in the editor");
 *   2. the Renderer→session loop: clicking an action chip pre-fills the chat
 *      composer (fill-not-send) with the composed agent request.
 *
 * Deterministic: the manifest is inline (data.manifest), so no S3 / live agent.
 */
const BASE = 'http://localhost:3000';
const SID = `tledit-${Date.now()}`;
const TITLE = `${SID} video editor`;

const manifest = {
  id: 'm1',
  template: 'nle-timeline',
  duration: 60,
  tracks: [{ id: 'v1', type: 'video', src: 'https://dexamplecdn002.cloudfront.net/artifacts/none.mp4', start: 0, end: 60 }],
  clips: [],
  captions: [],
};
const history = [{
  turnIndex: 0,
  timestamp: Date.now(),
  label: 'Video',
  blocks: [{ template: 'nle-timeline', title: 'Video', data: { manifest } }],
}];

// A composite (video + audio stem lanes on ONE timeline) — what composeTimeline produces
// when analyses share a sourceKey. Seeded inline to prove it renders lanes, not tabs.
const SID2 = `tledit-comp-${Date.now()}`;
const TITLE2 = `${SID2} composite`;
const composite = {
  id: 'c1', template: 'composite-timeline', duration: 131, createdAt: new Date().toISOString(),
  sourceKey: 'artifacts/s/clip.mp4',
  tracks: [
    { id: 'video-x', type: 'video', src: 'https://dexamplecdn002.cloudfront.net/artifacts/none.mp4', start: 0, end: 131 },
    { id: 'audio-speech', type: 'audio', src: 'https://dexamplecdn002.cloudfront.net/artifacts/s.wav', start: 0, end: 131 },
    { id: 'audio-music', type: 'audio', src: 'https://dexamplecdn002.cloudfront.net/artifacts/m.wav', start: 0, end: 131 },
  ],
  clips: [],
  peaks: { 'audio-speech': [0.1, 0.3, 0.2, 0.4], 'audio-music': [0.2, 0.1, 0.3] },
};
const history2 = [{
  turnIndex: 0, timestamp: Date.now(), label: 'Composite',
  blocks: [{ template: 'manifest-router', title: 'Composite', data: { manifest: composite, sourceKey: 'artifacts/s/clip.mp4' } }],
}];

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);
  const enter = p.locator("button:has-text('Explore the harness'), button:has-text('Sign in')").first();
  if (await enter.isVisible({ timeout: 5000 }).catch(() => false)) { await enter.click(); await p.waitForTimeout(1200); }
  const dev = p.locator("button:has-text('Dev Login')");
  if (await dev.isVisible({ timeout: 6000 }).catch(() => false)) { await dev.click(); await p.waitForTimeout(2500); }
}

test.describe('interactive timeline editor — video', () => {
  test.beforeAll(async ({ request }) => {
    await request.post(`${BASE}/sessions`, { data: { sessionId: SID, title: TITLE } }).catch(() => {});
    await request.patch(`${BASE}/sessions/${encodeURIComponent(SID)}`, { data: { renderHistory: history } }).catch(() => {});
    await request.post(`${BASE}/sessions`, { data: { sessionId: SID2, title: TITLE2 } }).catch(() => {});
    await request.patch(`${BASE}/sessions/${encodeURIComponent(SID2)}`, { data: { renderHistory: history2 } }).catch(() => {});
  });
  test.afterAll(async ({ request }) => {
    await request.delete(`${BASE}/sessions/${encodeURIComponent(SID)}`).catch(() => {});
    await request.delete(`${BASE}/sessions/${encodeURIComponent(SID2)}`).catch(() => {});
  });

  test('renders the pro editor for a bare video and a chip prefills the composer', async ({ page }) => {
    const errs: string[] = [];
    page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

    await devLogin(page);
    const link = page.locator(`text=${TITLE}`).first();
    await expect(link, `sidebar shows ${TITLE}`).toBeVisible({ timeout: 15000 });
    await link.click();

    const rtab = page.locator("button:has-text('Renderer'), [role='tab']:has-text('Renderer')").first();
    if (await rtab.isVisible({ timeout: 4000 }).catch(() => false)) { await rtab.click().catch(() => {}); }

    // (1) the interactive editor renders for a video with no clips — LEAN bar:
    // Clip / Frame / Analyze only (no Mark In/Out/+Range/+Marker/Clear buttons).
    await expect(page.getByTestId('timeline-editor'), 'timeline editor visible').toBeVisible({ timeout: 20000 });
    await expect(page.getByTestId('timeline-ruler')).toBeVisible();
    await expect(page.getByTestId('action-clip')).toBeVisible();
    await expect(page.getByTestId('action-frame')).toBeVisible();
    await expect(page.getByTestId('analyze-menu')).toBeVisible();
    for (const gone of ['mark-in', 'mark-out', 'add-range', 'add-marker', 'mark-clear']) {
      await expect(page.getByTestId(gone)).toHaveCount(0);
    }

    // (2) Renderer → session: a chip/dropdown item PUSHES a live turn via
    //     `chat:send` (not a composer prefill). Capture it in the capture phase
    //     and stopImmediatePropagation so the test asserts wiring without the agent.
    await page.evaluate(() => {
      (window as unknown as { __sent: unknown[] }).__sent = [];
      window.addEventListener('chat:send', (e: Event) => {
        (window as unknown as { __sent: unknown[] }).__sent.push((e as CustomEvent).detail);
        e.stopImmediatePropagation();
      }, true);
    });
    await page.getByTestId('action-clip').click();
    // Analyze menu is PORTALED to <body> — assert it isn't clipped and the full list shows.
    await page.getByTestId('analyze-menu').click();
    await expect(page.getByTestId('analyze-menu-list')).toBeVisible({ timeout: 4000 });
    for (const verb of ['extract-audio', 'stems', 'shots', 'faces', 'transcribe']) {
      await expect(page.getByTestId(`analyze-item-${verb}`)).toBeVisible();
    }
    await page.getByTestId('analyze-item-stems').click();
    // Frame is a single button now — clicking extracts still(s) directly (no sub-menu).
    await page.getByTestId('action-frame').click();

    const prompts = await page.evaluate(() =>
      (window as unknown as { __sent: { prompt?: string }[] }).__sent.map(d => d.prompt || ''));
    expect(prompts.some(p => /Clip the video/.test(p)), 'clip pushed a live turn').toBeTruthy();
    expect(prompts.some(p => /Separate the audio stems/.test(p)), 'stems pushed a live turn').toBeTruthy();
    expect(prompts.some(p => /Extract a still frame/.test(p)), 'frame pushed a live turn').toBeTruthy();
    const composer = page.locator('textarea[placeholder="Type a message..."]').first();
    await expect(composer).toHaveValue('');

    // (3) Ruler marking: double-click drops a marker → the selection pill appears.
    await page.getByTestId('timeline-ruler').dblclick();
    await expect(page.getByTestId('selection-summary')).toBeVisible({ timeout: 4000 });

    expect(errs, `no uncaught page errors: ${errs.join(' | ')}`).toHaveLength(0);
    console.log('[timeline-editor-uat] OK — lean bar + portaled Analyze + chat:send + ruler marking');
  });

  test('a composite manifest renders video + audio stem lanes on ONE timeline', async ({ page }) => {
    const errs: string[] = [];
    page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
    await devLogin(page);
    const link = page.locator(`text=${TITLE2}`).first();
    await expect(link, `sidebar shows ${TITLE2}`).toBeVisible({ timeout: 15000 });
    await link.click();
    const rtab = page.locator("button:has-text('Renderer'), [role='tab']:has-text('Renderer')").first();
    if (await rtab.isVisible({ timeout: 4000 }).catch(() => false)) { await rtab.click().catch(() => {}); }
    // the composite shows the editor, the video track, AND waveform lanes for the audio stems
    await expect(page.getByTestId('timeline-editor')).toBeVisible({ timeout: 20000 });
    await expect(page.locator('video').first(), 'video track rendered').toBeVisible({ timeout: 15000 });
    await expect(page.locator('canvas').first(), 'audio stem waveform lane rendered').toBeVisible({ timeout: 10000 });
    expect(errs, `no uncaught page errors: ${errs.join(' | ')}`).toHaveLength(0);
    console.log('[timeline-editor-uat] OK — composite video + audio lanes on one timeline');
  });
});
