import { test, expect, Page } from '@playwright/test';

/**
 * Feature UAT — multi-turn, human-input-style verification of the recent work:
 *   - streaming "Working…" animation during a turn
 *   - render EXACTLY ONCE with 2 speaker tracks (diarization multi-track)
 *   - subtitle display (known gap in the audio-only layout)
 *   - renderer follows the (light) app theme, not hardcoded dark
 *   - Hooks capability tab lists the built-in render-artifacts hook
 *
 * Assertions key off durable UI signals (media elements, visible text, computed
 * styles), mirroring meridian-uat.spec.ts.
 */

const BASE = 'http://localhost:3000';
// A previously-SUCCEEDED diarization job (2 speakers + vtt). Reused so we don't
// wait ~2min for a fresh job just to exercise the results-render path.
const DIA_JOB =
  'arn:aws:states:us-west-2:100000000002:execution:m2c-speech-diarization:mcp-1734e7be-fa37-42e6-932a-a8e7117a8e04';

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

async function newSession(p: Page) {
  const b = p.getByRole('button', { name: 'New Session', exact: true });
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

test.describe('Feature UAT', () => {
  test('A. Hooks tab lists the built-in render-artifacts hook', async ({ browser }) => {
    test.setTimeout(90_000);
    const page = await browser.newPage();
    await devLogin(page);
    // "Capabilities" is a text button under the sidebar's "Settings" section
    // (expand it first if collapsed).
    const capBtn = page.getByRole('button', { name: 'Capabilities', exact: true });
    if (!(await capBtn.isVisible({ timeout: 3000 }).catch(() => false))) {
      await page.getByRole('button', { name: /^Settings/ }).first().click({ timeout: 10000 });
      await page.waitForTimeout(500);
    }
    await capBtn.click({ timeout: 15000 });
    await page.waitForTimeout(1000);
    await page.locator('button:has-text("Hooks")').first().click({ timeout: 15000 });
    await expect(page.getByText('render-artifacts').first()).toBeVisible({ timeout: 20000 });
    await page.close();
  });

  test('B. Working animation shows during a streaming turn', async ({ browser }) => {
    test.setTimeout(120_000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    await send(page, 'In one sentence, what can you help me with?');
    await expect(page.getByText('Working…').first()).toBeVisible({ timeout: 20000 });
    await page.close();
  });

  test('C. Diarization results render TWO speaker audio tracks (multi-track)', async ({ browser }) => {
    test.setTimeout(3 * 60 * 1000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    await send(page, `Get my diarization results for job ${DIA_JOB}.`);
    await expect(page.locator('audio')).toHaveCount(2, { timeout: 2.5 * 60 * 1000 });
    await page.close();
  });

  test('D. Renderer timeline follows the light app theme (not hardcoded dark)', async ({ browser }) => {
    test.setTimeout(3 * 60 * 1000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    await send(page, `Get my diarization results for job ${DIA_JOB}.`);
    const firstAudio = page.locator('audio').first();
    await expect(firstAudio).toBeVisible({ timeout: 2.5 * 60 * 1000 });
    const cardBg = await firstAudio.evaluate((el) => {
      const card = el.closest('div');
      return card ? getComputedStyle(card).backgroundColor : '';
    });
    const m = cardBg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    const lum = m ? (Number(m[1]) + Number(m[2]) + Number(m[3])) / 3 : 0;
    expect(lum, `card bg was ${cardBg} (expected light)`).toBeGreaterThan(180);
    await page.close();
  });

  test('E. Subtitles display as a clickable transcript for the audio-only render', async ({ browser }) => {
    test.setTimeout(3 * 60 * 1000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    await send(page, `Get my diarization results for job ${DIA_JOB}.`);
    await expect(page.locator('audio').first()).toBeVisible({ timeout: 2.5 * 60 * 1000 });
    // The parsed VTT should surface as a transcript panel with the known line.
    await expect(page.getByTestId('subtitle-transcript')).toBeVisible({ timeout: 30000 });
    await expect(page.getByText('So these guys just disappeared.').first()).toBeVisible({ timeout: 10000 });
    await page.close();
  });

  test('F. Switching sessions mid-stream preserves the turn in its origin session', async ({ browser }) => {
    test.setTimeout(150_000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    // "Paris" is NOT in the prompt, so an assistant message containing it in
    // the ORIGIN session's cache proves the turn was preserved (not lost or
    // mis-filed) after we navigated away mid-stream.
    await send(page, 'What is the capital of France? Answer with one word only.');
    await expect(page.getByText('Working…').first()).toBeVisible({ timeout: 20000 });
    // Identify the origin session id via the cache key holding the prompt.
    const sid = await page.evaluate(() => {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i) as string;
        if (k.startsWith('lab-session-') && (localStorage.getItem(k) || '').includes('capital of France')) {
          return k.slice('lab-session-'.length);
        }
      }
      return '';
    });
    expect(sid, 'origin session id resolved from cache').not.toBe('');
    // Navigate AWAY mid-stream to a brand-new session.
    await page.getByRole('button', { name: 'New Session', exact: true }).first().click({ timeout: 10000 });
    // The background turn must complete and persist an ASSISTANT reply into the
    // ORIGIN session's cache (this is exactly what the Bug-1 fix guarantees).
    await expect.poll(async () => page.evaluate((s) => {
      try {
        const arr = JSON.parse(localStorage.getItem('lab-session-' + s) || '[]') as Array<{ role: string; content: string }>;
        return arr.some((m) => m.role === 'assistant' && /paris/i.test(m.content || ''));
      } catch { return false; }
    }, sid), { timeout: 90_000, intervals: [3000] }).toBe(true);
    await page.close();
  });

  test('G. Implicit auto-render fires when a job completes while the app is open', async ({ browser }) => {
    test.setTimeout(8 * 60 * 1000);
    const page = await browser.newPage();
    await devLogin(page);
    await newSession(page);
    // Start a real diarization job — but DO NOT ask for results. If the implicit
    // trigger works, the job's completion auto-renders into this session.
    await send(page, 'Run speech diarization on the audio at S3 key artifacts/Meridian_first60s_mono48k.wav. Just start the job and tell me the job id — I will wait, do not fetch the results.');
    await page.waitForTimeout(8000);
    const sid = await page.evaluate(() => {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i) as string;
        if (k.startsWith('lab-session-') && (localStorage.getItem(k) || '').includes('Meridian_first60s_mono48k')) {
          return k.slice('lab-session-'.length);
        }
      }
      return '';
    });
    expect(sid, 'origin session id resolved from cache').not.toBe('');
    // Without ANY further user action, a render must appear in this session's
    // render history once the job finishes (the implicit auto-render path).
    await expect.poll(async () => page.evaluate((s) => {
      try {
        return (JSON.parse(localStorage.getItem('lab-render-' + s) || '[]') as unknown[]).length;
      } catch { return 0; }
    }, sid), { timeout: 7 * 60 * 1000, intervals: [5000] }).toBeGreaterThan(0);
    await page.close();
  });
});
