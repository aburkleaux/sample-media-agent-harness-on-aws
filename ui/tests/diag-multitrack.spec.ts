import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3000';
const DIA_JOB =
  'arn:aws:states:us-west-2:100000000002:execution:m2c-speech-diarization:mcp-1734e7be-fa37-42e6-932a-a8e7117a8e04';

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);
  const btn = p.locator('button:has-text("Dev Login")');
  if (await btn.isVisible({ timeout: 5000 }).catch(() => false)) { await btn.click(); await p.waitForTimeout(3000); }
}

test('diagnose multi-track visibility', async ({ browser }) => {
  test.setTimeout(3 * 60 * 1000);
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1400, height: 900 });
  await devLogin(page);
  await page.getByRole('button', { name: 'New Session', exact: true }).first().click({ timeout: 10000 });
  await page.waitForTimeout(1200);
  await page.getByLabel('Chat message input').fill(`Get my diarization results for job ${DIA_JOB}.`);
  await page.getByLabel('Send message').click();
  await expect(page.locator('audio').first()).toBeVisible({ timeout: 2.5 * 60 * 1000 });
  await page.waitForTimeout(3000);

  const report = await page.evaluate(() => {
    const audios = Array.from(document.querySelectorAll('audio'));
    // All elements taller than 1000px (the culprit for the gap).
    const tall = Array.from(document.querySelectorAll('*'))
      .map((el) => {
        const r = (el as HTMLElement).getBoundingClientRect();
        return { tag: el.tagName, cls: (el as HTMLElement).className?.toString?.().slice(0, 40) || '', h: Math.round(r.height), w: Math.round(r.width) };
      })
      .filter((e) => e.h > 1000)
      .sort((a, b) => b.h - a.h)
      .slice(0, 8);
    const canvases = Array.from(document.querySelectorAll('canvas')).map((c) => {
      const r = c.getBoundingClientRect();
      return { attrH: (c as HTMLCanvasElement).height, attrW: (c as HTMLCanvasElement).width, cssH: Math.round(r.height), cssW: Math.round(r.width) };
    });
    return { audioCount: audios.length, tall, canvases };
  });
  console.log('DIAG', JSON.stringify(report, null, 2));
  // Regression guard for the waveform-balloon bug: both speaker tracks present
  // AND no element is absurdly tall (the 3000px canvas that pushed SPEAKER_01
  // off-screen behind the panel's overflow:hidden, so only SPEAKER_00 showed).
  expect(report.audioCount).toBe(2);
  expect(report.tall, `oversized elements present: ${JSON.stringify(report.tall)}`).toHaveLength(0);
  for (const c of report.canvases) {
    expect(c.cssH, 'waveform canvas height must be bounded').toBeLessThan(200);
  }
  await page.close();
});
