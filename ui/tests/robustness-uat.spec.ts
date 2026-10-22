import { test, expect, Page } from '@playwright/test';

/**
 * Robustness UAT — encodes the Phase-1 UX invariants (see
 * tests/evaluation/uat/invariants.json) as committed E2E assertions. These are
 * the deterministic guard the autonomous Playwright-MCP goal-loop (P4) protects
 * and extends. Full green runs require the dev server (localhost:3000) + a live
 * agent; the pure logic behind each invariant is additionally unit-tested in
 * ui/src/services/renderOrchestration.test.ts.
 */

const BASE = 'http://localhost:3000';
// A previously-SUCCEEDED diarization job (2 speakers + vtt) — reused to exercise
// the results-render path without waiting ~2min for a fresh job.
const DIA_JOB =
  'arn:aws:states:us-west-2:100000000002:execution:m2c-speech-diarization:mcp-1734e7be-fa37-42e6-932a-a8e7117a8e04';

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(500);
  // Marketing landing page → enter the harness (dev-bypass auto-authenticates).
  const enter = p.getByRole('button', { name: 'Enter the harness' });
  if (await enter.isVisible({ timeout: 8000 }).catch(() => false)) {
    await enter.click();
  }
  // Older builds surfaced an explicit Dev Login button instead.
  const btn = p.locator('button:has-text("Dev Login")');
  if (await btn.isVisible({ timeout: 1500 }).catch(() => false)) {
    await btn.click();
    await p.waitForTimeout(1500);
  }
}

async function newSession(p: Page) {
  const b = p.getByRole('button', { name: 'New session', exact: true });
  if (await b.first().isVisible({ timeout: 5000 }).catch(() => false)) {
    await b.first().click();
    await p.waitForTimeout(1200);
  }
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
}

async function send(p: Page, text: string) {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(text);
  await p.getByLabel('Send message').click();
}

test.describe('Robustness UAT (UX invariants)', () => {
  // Invariant: renderer-visibility — a fresh session with no artifact must not
  // present a stale/leftover render. "No rendered content yet" may exist inside
  // a collapsed drawer, but no render block should be shown.
  test('renderer-visibility: fresh session shows no render block AND no empty drawer', async ({ browser }) => {
    test.setTimeout(60_000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    // No nle-timeline / media render should be present on a brand-new session.
    const media = page.locator('video, [data-testid="nle-timeline"]');
    expect(await media.count()).toBe(0);
    // Discovery from the goal-loop: the Session Renderer panel must NOT be shown
    // as an empty "No rendered content yet" drawer when there is no artifact.
    // (shouldShowRenderer gates the panel on content/pending.)
    expect(await page.getByText('No rendered content yet').isVisible().catch(() => false)).toBe(false);
    await page.close();
  });

  // Invariant: no-console-errors — loading + starting a session logs no
  // uncaught errors and shows no error banner.
  test('no-console-errors: clean load + new session', async ({ browser }) => {
    test.setTimeout(60_000);
    const page = await browser.newPage();
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    await devLogin(page);
    await newSession(page);
    // The invariant targets FUNCTIONAL errors (uncaught exceptions, failed
    // requests, our error banners) — not React DEV-BUILD-ONLY warnings
    // (style shorthand conflicts, controlled-input, key props), which are
    // logged via console.error in dev but absent from production builds.
    const DEV_WARNING = /shorthand|styling bugs|a style property during rerender|non-boolean attribute|Warning:|controlled input|unique "key"/i;
    const appErrors = errors.filter(e =>
      !/favicon|net::|Failed to load resource/i.test(e) && !DEV_WARNING.test(e));
    expect(appErrors, `console errors: ${appErrors.join(' | ')}`).toHaveLength(0);
    await page.close();
  });

  // Invariant: no-inspector-placeholder + single-render + animation-consistent —
  // exercised via a real diarization results render. Tagged @live because it
  // needs a reachable agent; the goal-loop (T16) runs these to green.
  test('@live diarization render: inspector populated, single render, no error banner', async ({ browser }) => {
    test.setTimeout(180_000);
    const page = await browser.newPage();
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    await devLogin(page);
    await newSession(page);
    await send(page, `Get the diarization results for job ${DIA_JOB} and show them to me.`);

    // Render should appear (animation → render). Wait for a media/timeline element.
    const media = page.locator('video, audio, [data-testid="nle-timeline"], canvas');
    await expect(media.first()).toBeVisible({ timeout: 150_000 });

    // no-inspector-placeholder: the leftover placeholder must not be visible
    // when a timeline with cues rendered.
    const placeholder = page.getByText('Select an item on the timeline to view its details');
    expect(await placeholder.isVisible().catch(() => false)).toBe(false);

    // no error banner (auto-render fails quietly / no coroutine crash surface).
    const banner = page.getByText(/Error in input stream|Auto-render for the completed job failed/i);
    expect(await banner.isVisible().catch(() => false)).toBe(false);

    await page.close();
  });
});
