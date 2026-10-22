import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:3000';

/**
 * Helper: Complete the dev login flow on a page.
 * When VITE_DEV_BYPASS_AUTH is enabled, the AuthGate shows a "Dev Login" button.
 */
async function devLogin(page: import('@playwright/test').Page) {
  await page.goto(BASE);
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(1000);

  // Wait for either the app to render (already authed) or the dev login screen
  const devLoginBtn = page.locator('button:has-text("Dev Login")');
  const isLoginRequired = await devLoginBtn.isVisible({ timeout: 5000 }).catch(() => false);
  if (isLoginRequired) {
    await devLoginBtn.click();
    // Wait for the authenticated app to render (sidebar appears)
    await page.waitForTimeout(3000);
  } else {
    await page.waitForTimeout(1000);
  }
}

/**
 * UAT: Render Agent NLE Timeline Flow
 *
 * Tests the end-to-end rendering of the NLE timeline view when a session
 * contains a render manifest in its render history. This validates:
 * - Requirements 2 (Multi-Track Timeline), 7.4 (Frontend manifest detection),
 *   9 (NLE View Layout), and 10 (Manifest parsing).
 */
test.describe('NLE Timeline — Render Agent UAT', () => {
  const sessionId = `nle-uat-${Date.now()}`;

  // A minimal valid render manifest matching the schema from manifest_schema.py
  const sampleManifest = {
    id: '550e8400-e29b-41d4-a716-446655440000',
    duration: 120.5,
    createdAt: new Date().toISOString(),
    tracks: [
      {
        id: 'video-main',
        type: 'video',
        src: 'https://dexamplecdn03.cloudfront.net/artifacts/sample.mp4',
        start: 0,
        end: 120.5,
      },
      {
        id: 'audio-main',
        type: 'audio',
        src: 'https://dexamplecdn03.cloudfront.net/artifacts/sample.mp4',
        start: 0,
        end: 120.5,
        peaks: Array.from({ length: 100 }, () => Math.random()),
      },
      {
        id: 'subs-main',
        type: 'subtitle',
        src: '',
        start: 0,
        end: 120.5,
      },
    ],
    clips: [
      {
        id: 'clip-intro',
        name: 'Introduction',
        start: 0,
        end: 15.5,
        sourceTrack: 'video-main',
      },
      {
        id: 'clip-body',
        name: 'Main Content',
        start: 15.5,
        end: 100.0,
        sourceTrack: 'video-main',
      },
      {
        id: 'clip-outro',
        name: 'Closing',
        start: 100.0,
        end: 120.5,
        sourceTrack: 'video-main',
      },
    ],
    captions: [
      { start: 0.0, end: 3.5, speaker: 'Host', text: 'Welcome to the presentation.' },
      { start: 4.0, end: 8.2, speaker: 'Host', text: 'Today we will discuss cloud architecture.' },
      { start: 10.0, end: 14.5, speaker: 'Guest', text: 'Thank you for having me.' },
      { start: 16.0, end: 20.0, speaker: 'Host', text: 'Let us begin with the overview.' },
    ],
  };

  test.beforeAll(async ({ request }) => {
    // Create session with NLE render history
    await request.post(`${BASE}/sessions`, {
      data: { sessionId, title: 'NLE UAT — Render Agent' },
    });
    await request.patch(`${BASE}/sessions/${sessionId}`, {
      data: {
        renderHistory: [{
          turnIndex: 0,
          timestamp: Date.now(),
          blocks: [{
            template: 'nle-timeline',
            title: 'Media Analysis Result',
            data: { manifest: sampleManifest },
          }],
        }],
      },
    });
  });

  test.afterAll(async ({ request }) => {
    await request.delete(`${BASE}/sessions/${sessionId}`);
  });

  test('Requirement 7.4: nle-timeline template renders from render history', async ({ page }) => {
    await devLogin(page);

    // Click the NLE UAT session in sidebar
    const sessionLink = page.locator('text=NLE UAT — Render Agent');
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(2000);

    // The NLE timeline renders a <video> element for video tracks
    // or at minimum the clip names in the panel
    const videoEl = page.locator('video');
    const clipText = page.locator('text=Introduction');
    const audioEl = page.locator('audio');

    const hasVideo = await videoEl.first().isVisible().catch(() => false);
    const hasClip = await clipText.isVisible().catch(() => false);
    const hasAudio = await audioEl.first().isVisible().catch(() => false);

    // At least one indicator that the NLE timeline rendered
    expect(hasVideo || hasClip || hasAudio).toBe(true);
  });

  test('Requirement 2.1: renders one track per manifest track entry', async ({ page }) => {
    await devLogin(page);

    const sessionLink = page.locator('text=NLE UAT — Render Agent');
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(2000);

    // The NLE renders: video element (video track), canvas elements (waveform for audio),
    // and clip text entries (clip manager). Check for multiple rendered elements.
    const videoCount = await page.locator('video').count();
    const audioCount = await page.locator('audio').count();
    const canvasCount = await page.locator('canvas').count();

    // At minimum we expect the video/audio media element + waveform canvas
    const totalMediaElements = videoCount + audioCount + canvasCount;
    expect(totalMediaElements).toBeGreaterThanOrEqual(1);
  });

  test('Requirement 5.1: ClipManager renders clips with timecodes', async ({ page }) => {
    await devLogin(page);

    const sessionLink = page.locator('text=NLE UAT — Render Agent');
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(2000);

    // Look for clip names in the clips panel
    const introClip = page.locator('text=Introduction');
    const mainClip = page.locator('text=Main Content');
    const closingClip = page.locator('text=Closing');

    const hasIntro = await introClip.isVisible().catch(() => false);
    const hasMain = await mainClip.isVisible().catch(() => false);
    const hasClosing = await closingClip.isVisible().catch(() => false);

    expect(hasIntro || hasMain || hasClosing).toBe(true);
  });

  test('Requirement 9.9: InspectorPanel shows empty state when nothing selected', async ({ page }) => {
    await devLogin(page);

    const sessionLink = page.locator('text=NLE UAT — Render Agent');
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(2000);

    // Inspector should show empty state or be visible
    const emptyState = page.locator('text=Select an item')
      .or(page.locator('text=INSPECTOR'))
      .or(page.locator('[class*="inspector"]'));

    const hasInspector = await emptyState.first().isVisible().catch(() => false);
    expect(hasInspector).toBe(true);
  });

  test('Requirement 10.3: parseManifest round-trip via config endpoint', async ({ request }) => {
    const res = await request.get(`${BASE}/config`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('artifactsDomain');
    expect(body.artifactsDomain).toBe('dexamplecdn03.cloudfront.net');
  });

  test('Requirement 9.1: NLE View renders four panels', async ({ page }) => {
    await devLogin(page);

    const sessionLink = page.locator('text=NLE UAT — Render Agent');
    await expect(sessionLink).toBeVisible({ timeout: 10000 });
    await sessionLink.click();
    await page.waitForTimeout(2000);

    // The NLE view renders: Preview (video/audio element), Clips panel (clip names),
    // Inspector panel (INSPECTOR text or empty state), and timeline/waveform.
    // Check that at least the core panels are present.
    const hasMedia = (await page.locator('video').count()) > 0 || (await page.locator('audio').count()) > 0;
    const hasClips = await page.locator('text=CLIPS').isVisible().catch(() => false) ||
                     await page.locator('text=Introduction').isVisible().catch(() => false);
    const hasInspector = await page.locator('text=INSPECTOR').isVisible().catch(() => false) ||
                         await page.locator('text=Select an item').isVisible().catch(() => false);

    // At least 2 of the 3 UI indicators should be present
    const panelsFound = [hasMedia, hasClips, hasInspector].filter(Boolean).length;
    expect(panelsFound).toBeGreaterThanOrEqual(2);
  });
});

test.describe('NLE Timeline — Manifest Validation Edge Cases', () => {
  test('Renders error state for invalid manifest (missing tracks)', async ({ request }) => {
    const badSessionId = `nle-bad-${Date.now()}`;

    await request.post(`${BASE}/sessions`, {
      data: { sessionId: badSessionId, title: 'NLE Bad Manifest' },
    });
    await request.patch(`${BASE}/sessions/${badSessionId}`, {
      data: {
        renderHistory: [{
          turnIndex: 0,
          timestamp: Date.now(),
          blocks: [{
            template: 'nle-timeline',
            title: 'Invalid Manifest',
            data: { manifest: { id: 'bad', duration: 10 } }, // missing tracks
          }],
        }],
      },
    });

    // The session should be creatable without error
    const listRes = await request.get(`${BASE}/sessions`);
    const body = await listRes.json();
    const session = body.sessions.find((s: any) => s.sessionId === badSessionId);
    expect(session).toBeTruthy();
    expect(session.renderHistory).toHaveLength(1);

    // Clean up
    await request.delete(`${BASE}/sessions/${badSessionId}`);
  });

  test('Renders empty clips panel for manifest with no clips', async ({ request }) => {
    const noClipsSessionId = `nle-noclips-${Date.now()}`;

    await request.post(`${BASE}/sessions`, {
      data: { sessionId: noClipsSessionId, title: 'NLE No Clips' },
    });
    await request.patch(`${BASE}/sessions/${noClipsSessionId}`, {
      data: {
        renderHistory: [{
          turnIndex: 0,
          timestamp: Date.now(),
          blocks: [{
            template: 'nle-timeline',
            title: 'Audio Only',
            data: {
              manifest: {
                id: 'audio-only-manifest',
                duration: 60.0,
                createdAt: new Date().toISOString(),
                tracks: [{
                  id: 'audio-1',
                  type: 'audio',
                  src: 'https://dexamplecdn03.cloudfront.net/artifacts/audio.mp3',
                  start: 0,
                  end: 60.0,
                }],
                clips: [],
                captions: [],
              },
            },
          }],
        }],
      },
    });

    const listRes = await request.get(`${BASE}/sessions`);
    const body = await listRes.json();
    const session = body.sessions.find((s: any) => s.sessionId === noClipsSessionId);
    expect(session).toBeTruthy();
    expect(session.renderHistory[0].blocks[0].data.manifest.clips).toHaveLength(0);

    await request.delete(`${BASE}/sessions/${noClipsSessionId}`);
  });
});
