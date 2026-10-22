import { test, expect, Page } from '@playwright/test';

/**
 * Meridian end-to-end UAT — drives a brand-new chat session through the full
 * media pipeline like the user's example:
 *   trim first 30s → extract audio → stem separation → speech diarization → render.
 *
 * Serial: one shared session; each step is its own test for granular evidence.
 * Assertions key off durable UI signals (step-panel labels, the render panel's
 * media elements, and job status text) rather than the assistant's final prose,
 * because the orchestrator often completes tool work without echoing a URL.
 * Async M2C jobs are polled and re-submitted on the known cold-start transient.
 */

const BASE = 'http://localhost:3000';
const MERIDIAN_KEY = 'artifacts/meridian_3min_720p.mp4';
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

/** Send a prompt; resolve when body text matches `re`, else return final text. */
async function sendAndAwait(p: Page, prompt: string, re: RegExp, timeoutMs: number): Promise<string> {
  await send(p, prompt);
  const deadline = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < deadline) {
    last = await bodyText(p);
    if (re.test(last)) return last;
    await p.waitForTimeout(4000);
  }
  return last;
}

/**
 * Poll an async job to success, re-submitting on the M2C cold-start transient.
 * `startPrompt` is re-sent when a failure signal appears (mirrors the user's
 * "can you try again?"). Resolves on `successRe`, else returns final text.
 * `baseLen` scopes matching to text produced AFTER the job was started, so a
 * stale "SUCCEEDED"/stem name from earlier steps can't cause a spurious pass.
 */
async function pollJobWithRetry(
  p: Page, startPrompt: string, successRe: RegExp, failRe: RegExp,
  rounds: number, everyMs: number, baseLen: number,
): Promise<string> {
  const newText = async () => (await bodyText(p)).slice(baseLen);
  for (let r = 0; r < rounds; r++) {
    const deadline = Date.now() + everyMs;
    while (Date.now() < deadline) {
      const nt = await newText();
      if (successRe.test(nt)) return nt;
      await p.waitForTimeout(6000);
    }
    const nt = await newText();
    if (successRe.test(nt)) return nt;
    if (failRe.test(nt)) {
      await send(p, `That job failed with a transient error — please try again: ${startPrompt}`);
    } else {
      await send(p, 'Check the status; if it succeeded fetch the results, otherwise start it again.');
    }
  }
  return await newText();
}

test.describe.serial('Meridian pipeline UAT (new session)', () => {
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

  test('1. trim first 30s (video-processing skill) + render', async () => {
    test.setTimeout(5 * 60 * 1000);
    await send(page,
      `Trim the first 30 seconds of the video at S3 key ${MERIDIAN_KEY}, then render the trimmed clip so I can watch it.`);
    // Robust functional proof: the trimmed clip renders as a <video> element.
    await expect(page.locator('video').first()).toBeVisible({ timeout: 4 * 60 * 1000 });
  });

  test('2. extract audio (video-processing skill) + render', async () => {
    test.setTimeout(4 * 60 * 1000);
    await send(page,
      `Extract the audio from that trimmed clip as a mono 48kHz WAV, then render the audio so I can listen to it.`);
    // The extracted audio renders as an <audio> element.
    await expect(page.locator('audio').first()).toBeVisible({ timeout: 3.5 * 60 * 1000 });
  });

  test('3. stem separation → 3 stems (with retry)', async () => {
    test.setTimeout(14 * 60 * 1000);
    const start = `Run audio stem separation on that mono 48kHz WAV.`;
    const base = (await bodyText(page)).length;
    await sendAndAwait(page, `${start} Tell me the job id when it has started.`,
      /(started|running|pending|submitted|in progress|job\s*id|execution)/i, 3 * 60 * 1000);
    const txt = await pollJobWithRetry(
      page, start,
      /audio_(speech|music|sfx)\.wav|separated stems?|\bstems?\b.*(ready|complete|succeeded)|SUCCEEDED/i,
      /\bfailed\b|error|InvalidArn|transient/i,
      6, 130 * 1000, base,
    );
    expect(/audio_(speech|music|sfx)\.wav|separated stems?|SUCCEEDED/i.test(txt),
      `expected stems in new text; tail: ${txt.slice(-500)}`).toBeTruthy();
  });

  test('4. speech diarization → transcript (with retry)', async () => {
    test.setTimeout(14 * 60 * 1000);
    const start = `Run speech diarization on that mono 48kHz WAV.`;
    const base = (await bodyText(page)).length;
    await sendAndAwait(page, `${start} Tell me the job id when it has started.`,
      /(started|running|pending|submitted|in progress|job\s*id|execution)/i, 3 * 60 * 1000);
    const txt = await pollJobWithRetry(
      page, start,
      /transcript|speaker\s*[:0-9]|diariz\w*\s+(complete|succeeded|done|results)|SUCCEEDED/i,
      /\bfailed\b|error|InvalidArn|transient/i,
      6, 150 * 1000, base,
    );
    expect(/transcript|speaker\s*[:0-9]|SUCCEEDED/i.test(txt),
      `expected transcript in new text; tail: ${txt.slice(-500)}`).toBeTruthy();
  });
});
