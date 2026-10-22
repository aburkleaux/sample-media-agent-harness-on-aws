import { test, expect, Page } from '@playwright/test';

/**
 * fixes-uat.spec.ts — deterministic UAT for the recently shipped fixes.
 *
 * These run against the local dev server (localhost:3000, DEV_BYPASS_AUTH) and
 * MOCK the backend (`/api/invocations`, `/jobs`, `/jobs/stream`) so each fix is
 * exercised repeatably without a live agent:
 *   - item 1  full-width top-strip completion banner (portaled, 12s)
 *   - item 3  agent steps default-collapsed
 *   - item 4  instant user bubble + "Working…" on send
 *   - item 5  collapsed rail: Capabilities in the bottom cluster
 *   - steering result-JSON no longer overflows the chat bubble
 */

const BASE = 'http://localhost:3000';

async function login(page: Page): Promise<void> {
  await page.goto(BASE + '/');
  await page.waitForLoadState('domcontentloaded');
  // Marketing landing page → enter the harness (dev-bypass auto-authenticates).
  const enter = page.getByRole('button', { name: 'Enter the harness' });
  if (await enter.isVisible({ timeout: 8000 }).catch(() => false)) {
    await enter.click();
  }
  // Some builds surface an explicit Dev Login button post-redirect.
  const dev = page.locator('button:has-text("Dev Login")');
  if (await dev.isVisible({ timeout: 1500 }).catch(() => false)) {
    await dev.click();
  }
  await page.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 20000 });
}

async function newSession(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'New session', exact: true }).first().click();
  await page.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
}

// ---------------------------------------------------------------------------

test.describe('Fixes UAT (deterministic, mocked backend)', () => {
  test('item4 instant feedback + item3 steps collapsed + result JSON does not overflow', async ({ page }) => {
    test.setTimeout(60_000);
    const longArn =
      'arn:aws:states:us-west-2:100000000002:execution:mediaworks-media-processing:c4931d1a-0dcb-58f6-974a-73c07b257c25';
    const jsonText =
      'Result\n\n```json\n{\n  "job_id": "' + longArn + '",\n  "status": "SUCCEEDED",\n' +
      '  "output_s3_prefix": "s3://mediaworks-proxy-100000000002-us-west-2/micro-workflow/media-processing/c4931d1a-0dcb-58f6-974a-73c07b257c25"\n}\n```\n\nDone.';

    await page.route('**/api/invocations', async (route) => {
      // Latency so the instant-feedback assertion runs BEFORE the stream lands.
      await new Promise((r) => setTimeout(r, 900));
      const frames = [
        `data: ${JSON.stringify({ type: 'step', status: 'running', label: 'Activating skill...', node_id: 'n1' })}`,
        `data: ${JSON.stringify({ type: 'step', status: 'done', label: 'Activating skill...', node_id: 'n1', duration_ms: 1200 })}`,
        `data: ${JSON.stringify(jsonText)}`,
        `data: ${JSON.stringify({ type: 'execution_summary', total_duration_ms: 1500, usage: { inputTokens: 100, outputTokens: 20 } })}`,
      ].join('\n') + '\n';
      await route.fulfill({ status: 200, contentType: 'text/event-stream', body: frames });
    });

    await login(page);
    await newSession(page);

    await page.getByLabel('Chat message input').fill('spec this video please');
    await page.getByLabel('Send message').click();

    // item 4: user bubble + Working… appear immediately (before the 900ms mock).
    await expect(page.getByText('spec this video please')).toBeVisible({ timeout: 600 });
    await expect(page.getByText('Working…')).toBeVisible({ timeout: 600 });

    // Stream completes.
    await expect(page.getByText('Working…')).toBeHidden({ timeout: 15_000 });

    // item 3: steps rendered default-collapsed (an "Expand steps" toggle, and
    // the step list is NOT expanded).
    const expandSteps = page.getByRole('button', { name: 'Expand steps' });
    await expect(expandSteps).toBeVisible();

    // result JSON overflow fix: the <pre> scrolls internally (contained), not
    // clipped/overflowing the bubble; and the page has no horizontal overflow.
    const pre = page.locator('.lab-md pre').first();
    await expect(pre).toBeVisible();
    const metrics = await pre.evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
      overflowX: getComputedStyle(el).overflowX,
    }));
    expect(metrics.overflowX).toBe('auto');
    expect(metrics.scrollWidth).toBeGreaterThan(metrics.clientWidth);
    const noPageOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    );
    expect(noPageOverflow).toBe(true);

    // item 3 (cont): expanding reveals the step row.
    await expandSteps.click();
    await expect(page.getByRole('button', { name: 'Collapse steps' })).toBeVisible();
  });

  test('item1 completion banner is a full-width top strip portaled to body', async ({ page }) => {
    test.setTimeout(45_000);
    const job = {
      jobId: 'arn:aws:states:us-west-2:100000000002:execution:mediaworks-face-recognition:uat-' + Date.now(),
      status: 'SUCCEEDED',
      trioId: 'face-recognition|recognize_faces',
      sessionId: 'uat-session',
      startAt: new Date().toISOString(),
      terminalAt: new Date().toISOString(),
      hasRenderableArtifacts: true,
    };
    // Empty initial jobs seed so nothing else competes.
    await page.route(/\/jobs(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) }),
    );
    // SSE stream delivers one terminal frame.
    await page.route(/\/jobs\/stream/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: `event: job.terminal\ndata: ${JSON.stringify(job)}\n\n`,
      }),
    );

    await login(page);

    const banner = page.locator('.lab-notif-banner');
    await expect(banner).toBeVisible({ timeout: 15_000 });
    const geo = await banner.evaluate((el) => {
      const r = el.getBoundingClientRect();
      return { top: r.top, width: r.width, vw: window.innerWidth, parentIsBody: el.parentElement === document.body };
    });
    expect(geo.parentIsBody).toBe(true);           // portaled to <body>
    expect(geo.top).toBeLessThanOrEqual(2);        // pinned to the very top
    expect(geo.width).toBeGreaterThan(geo.vw * 0.9); // full-width strip
    // Countdown bar present.
    await expect(page.locator('.lab-notif-countdown')).toHaveCount(1);
  });

  test('item5 collapsed rail: Capabilities sits in the bottom cluster', async ({ page }) => {
    test.setTimeout(45_000);
    await login(page);
    // Collapse the sidebar via the logo (expanded logo title = "Collapse sidebar").
    await page.locator('img[title="Collapse sidebar"]').first().click();

    const agents = page.getByRole('button', { name: 'Agents' });
    const caps = page.getByRole('button', { name: 'Capabilities' });
    const expandSidebar = page.getByRole('button', { name: 'Expand sidebar' });
    await expect(caps).toBeVisible();
    await expect(expandSidebar).toBeVisible();

    const topOf = (loc: ReturnType<Page['getByRole']>) =>
      loc.evaluate((el) => el.getBoundingClientRect().top);
    const aY = await topOf(agents);
    const cY = await topOf(caps);
    const eY = await topOf(expandSidebar);

    // Capabilities is pushed to the bottom cluster: a large gap below the
    // primary nav (Agents), and above the Expand control.
    expect(cY).toBeGreaterThan(aY + 100);
    expect(cY).toBeLessThan(eY);
  });

  test('issue1 session-hijack: auto-render for a DIFFERENT session is ignored', async ({ page }) => {
    test.setTimeout(45_000);
    await login(page);
    await newSession(page);
    // Fire a chat:auto-render aimed at an unrelated session id. ChatCanvas must
    // ignore it — no synthetic user bubble, no "Working…", no queued message.
    await page.evaluate(() => {
      window.dispatchEvent(new CustomEvent('chat:auto-render', {
        detail: {
          prompt: 'HIJACK_PROMPT_SHOULD_NOT_APPEAR',
          sessionId: 'unrelated-00000000-0000-0000-0000-000000000000',
        },
      }));
    });
    await page.waitForTimeout(1500);
    await expect(page.getByText('HIJACK_PROMPT_SHOULD_NOT_APPEAR')).toHaveCount(0);
    await expect(page.getByText('Working…')).toHaveCount(0);
    await expect(page.getByText(/message.*queued/i)).toHaveCount(0);
    // The composer is still usable (Send control present).
    await expect(page.getByLabel('Send message')).toBeVisible();
  });

  test('item2 click notification injects the results turn into chat', async ({ page }) => {
    test.setTimeout(45_000);
    // Empty seed; SSE delivers one terminal SUCCEEDED renderable frame. Empty
    // sessionId keeps the auto-render in the current session (no switch), so the
    // click path is exercised deterministically.
    const job = {
      jobId: 'arn:aws:states:us-west-2:100000000002:execution:mediaworks-face-recognition:uat2-' + Date.now(),
      status: 'SUCCEEDED',
      trioId: 'face-recognition|recognize_faces',
      sessionId: '',
      startAt: new Date().toISOString(),
      terminalAt: new Date().toISOString(),
      hasRenderableArtifacts: true,
    };
    await page.route(/\/jobs(\?|$)/, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ jobs: [] }) }),
    );
    await page.route(/\/jobs\/stream/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: `event: job.terminal\ndata: ${JSON.stringify(job)}\n\n`,
      }),
    );
    // The auto-render turn: a render step + a short text + summary.
    await page.route('**/api/invocations', (route) => {
      const frames = [
        `data: ${JSON.stringify({ type: 'step', status: 'done', label: 'Rendered artifacts', agent: 'render', render_manifest_key: 'artifacts/uat/render/x.json' })}`,
        `data: ${JSON.stringify('Here are your results — download links below.')}`,
        `data: ${JSON.stringify({ type: 'execution_summary', total_duration_ms: 500, usage: {} })}`,
      ].join('\n') + '\n';
      route.fulfill({ status: 200, contentType: 'text/event-stream', body: frames });
    });
    await page.route(/\/api\/artifact/, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }),
    );

    await login(page);
    const banner = page.locator('.lab-notif-banner');
    await expect(banner).toBeVisible({ timeout: 15_000 });
    // Click the banner body (same onOpen path as the bell dropdown item).
    await banner.locator('button.lab-notif-banner-body').click();

    // item 2: the results prompt is injected into chat as a user turn, and the
    // assistant response streams in.
    await expect(page.getByText(/Fetch the results for the Face Recognition job/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText('Here are your results — download links below.')).toBeVisible({ timeout: 15_000 });
  });
});
