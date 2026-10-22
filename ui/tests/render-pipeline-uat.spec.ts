import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:3000';
const TEST_VIDEO = 'https://dexamplecdn03.cloudfront.net/artifacts/test_video.mp4';

async function devLogin(page: import('@playwright/test').Page) {
  await page.goto(BASE);
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(1000);
  const devBtn = page.locator('button:has-text("Dev Login")');
  if (await devBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await devBtn.click();
    await page.waitForTimeout(3000);
  }
}

/**
 * UAT: Render Agent as the single rendering pipeline.
 *
 * Validates the new architecture where ALL media rendering flows through
 * the Render Agent via the /render proxy endpoint (no frontend simple
 * templates). Media URL → /render → Render Agent → NLE manifest → timeline.
 */
test.describe('Render Agent Pipeline — /render endpoint', () => {
  test('POST /render returns a manifest URL for a video', async ({ request }) => {
    const res = await request.post(`${BASE}/render`, {
      data: { url: TEST_VIDEO },
      timeout: 30000,
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    // manifestUrl is a same-origin proxy path (avoids CORS on JSON fetch).
    expect(body).toHaveProperty('manifestUrl');
    expect(body.manifestUrl).toContain('/api/artifact');
    expect(body.manifestUrl).toContain('render');
    // cloudfrontUrl is the direct CDN link to the manifest.
    expect(body.cloudfrontUrl).toContain('cloudfront.net');
    expect(body.cloudfrontUrl).toMatch(/\.json$/);
    expect(body.error).toBeFalsy();
  });

  test('Render Agent manifest has nle-timeline template and a video track', async ({ request }) => {
    const res = await request.post(`${BASE}/render`, {
      data: { url: TEST_VIDEO },
      timeout: 30000,
    });
    expect(res.status()).toBe(200);
    const { manifestUrl } = await res.json();

    // Fetch the manifest the Render Agent produced.
    const manifestRes = await request.get(manifestUrl);
    expect(manifestRes.status()).toBe(200);
    const manifest = await manifestRes.json();

    expect(manifest.template).toBe('nle-timeline');
    expect(Array.isArray(manifest.tracks)).toBe(true);
    expect(manifest.tracks.length).toBeGreaterThanOrEqual(1);
    expect(manifest.tracks[0].type).toBe('video');
    expect(manifest.tracks[0].src).toBe(TEST_VIDEO);
    expect(manifest.mimeType).toBe('video/mp4');
  });

  test('POST /render classifies audio as an audio track', async ({ request }) => {
    // Use a .wav URL shape — the render agent classifies by extension.
    const audioUrl = 'https://dexamplecdn03.cloudfront.net/artifacts/test_audio.wav';
    const res = await request.post(`${BASE}/render`, {
      data: { url: audioUrl },
      timeout: 30000,
    });
    expect(res.status()).toBe(200);
    const { manifestUrl } = await res.json();
    const manifest = await (await request.get(manifestUrl)).json();
    expect(manifest.template).toBe('nle-timeline');
    expect(manifest.tracks[0].type).toBe('audio');
  });

  test('POST /render without url returns 400', async ({ request }) => {
    const res = await request.post(`${BASE}/render`, { data: {} });
    expect(res.status()).toBe(400);
  });
});

test.describe('Render Agent Pipeline — NLE timeline renders in UI', () => {
  test('A session with a Render-Agent manifest renders the NLE timeline', async ({ page, request }) => {
    // 1. Produce a real manifest via the Render Agent.
    const renderRes = await request.post(`${BASE}/render`, {
      data: { url: TEST_VIDEO },
      timeout: 30000,
    });
    expect(renderRes.status()).toBe(200);
    const { manifestUrl } = await renderRes.json();

    // 2. Create a session whose renderHistory points at that manifest.
    const uniqueTitle = `Render Flow UAT ${Date.now()}`;
    const sessionId = `render-flow-${Date.now()}`;
    await request.post(`${BASE}/sessions`, {
      data: { sessionId, title: uniqueTitle },
    });
    await request.patch(`${BASE}/sessions/${sessionId}`, {
      data: {
        renderHistory: [{
          turnIndex: 0,
          timestamp: Date.now(),
          blocks: [{ template: 'nle-timeline', title: 'Media Timeline', data: { manifestUrl } }],
        }],
      },
    });

    // 3. Load the UI and open the session.
    await devLogin(page);
    const sessionLink = page.locator(`text=${uniqueTitle}`).first();
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(3000);

    // 4. The NLE timeline should render the video element from the manifest.
    const hasVideo = (await page.locator('video').count()) > 0;
    const hasCanvas = (await page.locator('canvas').count()) > 0;
    expect(hasVideo || hasCanvas).toBe(true);

    // Clean up.
    await request.delete(`${BASE}/sessions/${sessionId}`);
  });
});
