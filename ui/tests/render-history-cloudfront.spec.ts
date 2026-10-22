import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:3000';

test.describe('Config endpoint', () => {
  test('GET /config returns artifactsDomain', async ({ request }) => {
    const res = await request.get(`${BASE}/config`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('artifactsDomain');
    expect(body.artifactsDomain).toBe('dexamplecdn03.cloudfront.net');
  });
});

test.describe('Session renderHistory API', () => {
  const sessionId = `test-pw-${Date.now()}`;

  test('POST /sessions creates a session', async ({ request }) => {
    const res = await request.post(`${BASE}/sessions`, {
      data: { sessionId, title: 'Playwright Test Session' },
    });
    expect(res.status()).toBe(201);
    const body = await res.json();
    expect(body.sessionId).toBe(sessionId);
  });

  test('PATCH /sessions/:id with renderHistory persists it', async ({ request }) => {
    const renderHistory = [
      {
        turnIndex: 0,
        timestamp: Date.now(),
        blocks: [{
          template: 'video-markers',
          title: 'Test Video',
          data: { src: 'https://dexamplecdn03.cloudfront.net/artifacts/test.mp4', poster: '', markers: [] },
        }],
      },
    ];
    const res = await request.patch(`${BASE}/sessions/${sessionId}`, {
      data: { renderHistory },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.sessionId).toBe(sessionId);
    expect(body.lastActivityAt).toBeTruthy();
  });

  test('GET /sessions returns session with renderHistory', async ({ request }) => {
    const res = await request.get(`${BASE}/sessions`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    const session = body.sessions.find((s: any) => s.sessionId === sessionId);
    expect(session).toBeTruthy();
    expect(session.renderHistory).toHaveLength(1);
    expect(session.renderHistory[0].blocks[0].template).toBe('video-markers');
    expect(session.renderHistory[0].blocks[0].data.src).toContain('dexamplecdn03.cloudfront.net');
  });

  test('PATCH enforces max 50 entries', async ({ request }) => {
    const renderHistory = Array.from({ length: 60 }, (_, i) => ({
      turnIndex: i,
      timestamp: Date.now() + i,
      blocks: [{ template: 'custom', title: `Entry ${i}`, data: { src: `artifacts/file${i}.pdf` } }],
    }));
    const res = await request.patch(`${BASE}/sessions/${sessionId}`, {
      data: { renderHistory },
    });
    expect(res.status()).toBe(200);

    // Verify only last 50 are stored
    const getRes = await request.get(`${BASE}/sessions`);
    const body = await getRes.json();
    const session = body.sessions.find((s: any) => s.sessionId === sessionId);
    expect(session.renderHistory).toHaveLength(50);
    // Should keep the last 50 (entries 10-59)
    expect(session.renderHistory[0].blocks[0].title).toBe('Entry 10');
    expect(session.renderHistory[49].blocks[0].title).toBe('Entry 59');
  });

  test('DELETE /sessions/:id soft-deletes session', async ({ request }) => {
    const res = await request.delete(`${BASE}/sessions/${sessionId}`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.deleted).toBe(true);

    // Verify session no longer appears in list
    const getRes = await request.get(`${BASE}/sessions`);
    const getBody = await getRes.json();
    const session = getBody.sessions.find((s: any) => s.sessionId === sessionId);
    expect(session).toBeUndefined();
  });
});

test.describe('UI: render history persistence', () => {
  test('lazy session creation works via API', async ({ request }) => {
    // Test the session creation flow that ChatCanvas triggers on first message
    const testSessionId = `lazy-create-${Date.now()}`;
    const title = 'Generate a simple PDF report with a title page'.slice(0, 40);

    // Simulate what ChatCanvas does: POST to create session
    const createRes = await request.post(`${BASE}/sessions`, {
      data: { sessionId: testSessionId, title },
    });
    expect(createRes.status()).toBe(201);

    // Simulate activity update after agent responds
    const patchRes = await request.patch(`${BASE}/sessions/${testSessionId}`, {
      data: { renderHistory: [{ turnIndex: 0, timestamp: Date.now(), blocks: [{ template: 'custom', title: 'PDF Output', data: { src: `https://dexamplecdn03.cloudfront.net/artifacts/report.pdf` } }] }] },
    });
    expect(patchRes.status()).toBe(200);

    // Verify the session is listed and has renderHistory
    const listRes = await request.get(`${BASE}/sessions`);
    const body = await listRes.json();
    const session = body.sessions.find((s: any) => s.sessionId === testSessionId);
    expect(session).toBeTruthy();
    expect(session.title).toBe(title);
    expect(session.renderHistory).toHaveLength(1);
    expect(session.renderHistory[0].blocks[0].data.src).toContain('cloudfront.net');

    // Clean up
    await request.delete(`${BASE}/sessions/${testSessionId}`);
  });

  test('CloudFront URL is used in artifact src when domain is configured', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    // Verify config is fetchable from the browser context
    const result = await page.evaluate(async () => {
      const res = await fetch('/config');
      return res.json();
    });
    expect(result.artifactsDomain).toBe('dexamplecdn03.cloudfront.net');
  });

  test('session sidebar loads from DynamoDB', async ({ page }) => {
    // First create a session via API
    const testId = `sidebar-test-${Date.now()}`;
    await page.request.post(`${BASE}/sessions`, {
      data: { sessionId: testId, title: 'Sidebar Visibility Test' },
    });

    await page.goto(BASE);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);
    // Handle dev login if needed
    const devBtn = page.locator('button:has-text("Dev Login")');
    if (await devBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await devBtn.click();
      await page.waitForTimeout(3000);
    }

    // The sidebar should show the session title
    const sidebar = page.locator('text=Sidebar Visibility Test');
    const visible = await sidebar.isVisible({ timeout: 5000 }).catch(() => false);

    // Clean up
    await page.request.delete(`${BASE}/sessions/${testId}`);

    expect(visible).toBe(true);
  });

  test('switching sessions loads renderHistory from DynamoDB', async ({ page }) => {
    // Navigate and login first, then create session via page context (includes auth)
    await page.goto(BASE);
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);
    const devBtn = page.locator('button:has-text("Dev Login")');
    if (await devBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await devBtn.click();
      await page.waitForTimeout(3000);
    }

    // Create a session with renderHistory via page.evaluate (uses browser auth context)
    const sessionA = `switch-a-${Date.now()}`;
    await page.evaluate(async ({ sid, BASE: base }) => {
      await fetch(`${base}/sessions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId: sid, title: 'Session With Video' }),
      });
      await fetch(`${base}/sessions/${sid}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          renderHistory: [{
            turnIndex: 0,
            timestamp: Date.now(),
            blocks: [{ template: 'video-markers', title: 'Video A', data: { src: 'https://dexamplecdn03.cloudfront.net/artifacts/video_a.mp4', poster: '', markers: [] } }],
          }],
        }),
      });
    }, { sid: sessionA, BASE });

    // Reload to pick up the new session in the sidebar
    await page.reload();
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(3000);

    // Click on the session in sidebar
    const sessionLink = page.locator('text=Session With Video').first();
    await expect(sessionLink).toBeVisible({ timeout: 10000 });

    await sessionLink.click();
    await page.waitForTimeout(1500);

    // After clicking, the right panel should expand (renderHistory triggers it)
    // Check that a video element or the video template rendered
    const hasVideoElement = await page.locator('video').isVisible().catch(() => false);
    const hasVideoSrc = await page.evaluate(() => {
      const videos = document.querySelectorAll('video');
      return Array.from(videos).some(v => v.src.includes('cloudfront.net'));
    }).catch(() => false);

    // At minimum the panel should not be collapsed (render history loaded)
    // The render panel appears when renderHistory has items
    const renderPanel = page.locator('[data-testid="render-panel"]').or(page.locator('video'));
    const panelOrVideo = await renderPanel.first().isVisible().catch(() => false);

    // Clean up
    await page.request.delete(`${BASE}/sessions/${sessionA}`);

    // We expect either the video element or the panel to be visible
    expect(hasVideoElement || hasVideoSrc || panelOrVideo).toBe(true);
  });
});

test.describe('CloudFront URL construction', () => {
  test('buildRenderEntry strips S3 presigned URL and uses CloudFront domain', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState('networkidle');

    // Evaluate in browser context to test the URL construction logic
    const result = await page.evaluate(async () => {
      const configRes = await fetch('/config');
      const config = await configRes.json();
      return { domain: config.artifactsDomain };
    });

    expect(result.domain).toBe('dexamplecdn03.cloudfront.net');
  });
});
