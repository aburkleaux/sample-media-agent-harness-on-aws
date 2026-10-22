import { test, expect, Page } from '@playwright/test';

/**
 * Live UI diarization run — mirrors the user's Meridian flow to verify two
 * feedback items end-to-end:
 *   #1 the FIRST turn surfaces its reasoning steps (step indicator present)
 *   #3 fetched diarization results actually RENDER (audio element in the panel)
 *
 * One brand-new session. Turn 1 asks for first-minute diarization (skill →
 * trim/extract → start_diarization). We then poll to completion and ask for
 * the results, asserting the renderer panel shows a media element.
 */

const BASE = 'http://localhost:3000';
const VIDEO_KEY = 'artifacts/meridian_3min_720p.mp4';
let page: Page;

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);
  const btn = p.locator('button:has-text("Dev Login")');
  if (await btn.isVisible({ timeout: 5000 }).catch(() => false)) {
    await btn.click();
    await p.waitForTimeout(3000);
  }
}

async function bodyText(p: Page): Promise<string> {
  return (await p.locator('body').innerText()).replace(/\s+/g, ' ');
}

async function send(p: Page, text: string) {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(text);
  await p.getByLabel('Send message').click();
  await p.waitForTimeout(1500);
}

async function awaitText(p: Page, re: RegExp, timeoutMs: number, baseLen = 0): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    last = (await bodyText(p)).slice(baseLen);
    if (re.test(last)) return last;
    await p.waitForTimeout(5000);
  }
  return last;
}

test.describe.serial('Meridian diarization: steps + render', () => {
  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    await devLogin(page);
    const newBtn = page.getByLabel('New Session');
    if (await newBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await newBtn.click();
      await page.waitForTimeout(1500);
    }
    await page.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15000 });
  });
  test.afterAll(async () => { await page?.close(); });

  test('turn-1 steps (#1) + results render (#3)', async () => {
    test.setTimeout(14 * 60 * 1000);
    await send(page, `Get the speech diarization of the first minute of the video at S3 key ${VIDEO_KEY} (bucket mediaworks-harness-skills-store-100000000002-us-east-1).`);

    // Wait for the ACTUAL job to start — an SFN execution ARN or RUNNING status.
    // (Not the loose word "diarization", which appears in the intro narration
    // before any tool/step has run — checking steps then would be premature.)
    const started = await awaitText(page, /execution:m2c-speech-diarization|RUNNING|job.?id/i, 6 * 60 * 1000);
    expect(started).toMatch(/execution:m2c-speech-diarization|RUNNING|job.?id/i);

    // #1: with the full skill flow having run (activate skill → trim/extract →
    // start), the first turn must surface step evidence. Soft so #3 still runs.
    const stepsToggle = await page.locator('button[aria-label="Collapse steps"], button[aria-label="Expand steps"]').count();
    const stepLabels = await page.getByText(/Activating skill|Running code|Dispatching|Calling start_diarization/i).count();
    const hasSteps = stepsToggle + stepLabels > 0;
    console.log('TURN1_STEP_EVIDENCE_VISIBLE =', hasSteps, `(toggle=${stepsToggle} labels=${stepLabels})`);
    expect.soft(hasSteps, 'turn-1 should surface reasoning steps').toBe(true);

    // #3: poll to completion, re-asking for results (mirrors the user), and
    // assert the renderer panel shows a media element (speaker audio track).
    let rendered = false;
    for (let round = 0; round < 6 && !rendered; round++) {
      const base = (await bodyText(page)).length;
      await send(page, 'Get my diarization results and render the speaker audio and transcript.');
      await awaitText(page, /speaker|transcript|disappeared|El Matador|still (running|processing|in progress)/i, 3 * 60 * 1000, base);
      // #3: a render block appeared in the panel — either a media element or the
      // render block's "Media Timeline" title (diarization renders as an
      // audio/transcript timeline whose exact element varies).
      const media = await page.locator('audio, video').first().isVisible({ timeout: 8000 }).catch(() => false);
      const timeline = await page.getByText(/Media Timeline/i).first().isVisible({ timeout: 2000 }).catch(() => false);
      rendered = media || timeline;
      console.log(`ROUND ${round}: rendered =`, rendered, `(media=${media} timeline=${timeline})`);
      if (!rendered) await page.waitForTimeout(15000);
    }
    expect(rendered, 'fetched diarization results should render a media element').toBe(true);
  });
});
