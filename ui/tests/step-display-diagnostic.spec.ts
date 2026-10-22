import { test, expect, Page } from '@playwright/test';

/** Fast diagnostic for feedback #1: after a tool-using turn, are the reasoning
 * steps actually present in the DOM? Uses the pre-made WAV so the turn is short. */

const BASE = 'http://localhost:3000';
const WAV = 'artifacts/Netflix_Open_Content_Meridian_first30s_mono48k.wav';

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);
  const btn = p.locator('button:has-text("Dev Login")');
  if (await btn.isVisible({ timeout: 5000 }).catch(() => false)) { await btn.click(); await p.waitForTimeout(3000); }
}

test('feedback #1: steps present in DOM after a tool turn', async ({ browser }) => {
  test.setTimeout(4 * 60 * 1000);
  const page = await browser.newPage();
  await devLogin(page);
  const newBtn = page.getByLabel('New Session');
  if (await newBtn.isVisible({ timeout: 5000 }).catch(() => false)) { await newBtn.click(); await page.waitForTimeout(1500); }

  const input = page.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15000 });
  await input.fill(`Run speech diarization on the audio at S3 key ${WAV} (bucket mediaworks-harness-skills-store-100000000002-us-east-1). Just start it.`);
  await page.getByLabel('Send message').click();

  // Wait until the turn actually finishes work: the SFN execution ARN only
  // appears AFTER start_diarization runs (the echoed prompt contains the word
  // "diarization", so we must NOT key off that). Fall back to a generous cap.
  const deadline = Date.now() + 3 * 60 * 1000;
  while (Date.now() < deadline) {
    const t = (await page.locator('body').innerText());
    if (/execution:m2c-speech-diarization|mcp-[0-9a-f]{8}-/i.test(t)) break;
    await page.waitForTimeout(4000);
  }
  // Let the stream fully finalize the assistant message.
  await page.waitForTimeout(8000);

  // Clean, single-engine checks:
  const stepsCountText = await page.getByText(/\d+ steps?/).count();
  const activating = await page.getByText(/Activating skill|Running code|Dispatching|Calling start_diarization/i).count();
  const toggle = await page.locator('button[aria-label="Collapse steps"], button[aria-label="Expand steps"]').count();
  console.log('STEPS_TEXT_COUNT =', stepsCountText, '| STEP_LABELS =', activating, '| TOGGLE_BTNS =', toggle);

  // Dump the assistant area HTML for inspection (first 1500 chars).
  const html = await page.locator('body').innerHTML();
  const idx = html.search(/step/i);
  console.log('HTML_HAS_STEP_TOKEN =', idx >= 0, '| snippet:', idx >= 0 ? html.slice(idx - 40, idx + 200).replace(/\s+/g, ' ') : 'NONE');

  expect(stepsCountText + activating + toggle).toBeGreaterThan(0);
  await page.close();
});
