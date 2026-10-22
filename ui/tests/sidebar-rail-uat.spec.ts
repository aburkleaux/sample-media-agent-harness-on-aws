import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:3000';

async function enterHarness(page: Page) {
  await page.goto(BASE);
  await page.waitForTimeout(600);
  const enter = page.locator("button:has-text('Explore the harness'), button:has-text('Enter the harness')").first();
  if (await enter.isVisible({ timeout: 5000 }).catch(() => false)) {
    await enter.click();
    await page.waitForTimeout(600);
  }
  const dev = page.locator("button:has-text('Dev Login')");
  if (await dev.isVisible({ timeout: 3000 }).catch(() => false)) {
    await dev.click();
    await page.waitForTimeout(1200);
  }
}

// The collapsed rail keeps regressing a stray divider (a leftover top-border by the Skills icon)
// and has been missing the bottom user item the expanded view shows. Lock both down.
test('collapsed rail has NO divider and mirrors the expanded bottom (incl. user avatar)', async ({ page }) => {
  test.setTimeout(60_000);
  await enterHarness(page);

  // Collapse via JS click — transient notification banners intercept real pointer clicks.
  await expect(page.locator('button[aria-label="Collapse sidebar"]')).toBeVisible({ timeout: 20000 });
  await page.evaluate(() => {
    const b = document.querySelector('button[aria-label="Collapse sidebar"]') as HTMLElement | null;
    b?.click();
  });
  await expect(page.locator('button[aria-label="Expand sidebar"]')).toBeVisible({ timeout: 10000 });

  // #1 — NO rail item may carry a visible border (the divider regression is a leftover top border).
  const borders = await page.evaluate(() => {
    const expand = document.querySelector('button[aria-label="Expand sidebar"]');
    const rail = expand?.parentElement;
    if (!rail) return { error: true, offenders: [] as string[] };
    const offenders: string[] = [];
    for (const el of Array.from(rail.children)) {
      const cs = getComputedStyle(el);
      const sides = [
        ['top', cs.borderTopWidth, cs.borderTopStyle],
        ['right', cs.borderRightWidth, cs.borderRightStyle],
        ['bottom', cs.borderBottomWidth, cs.borderBottomStyle],
        ['left', cs.borderLeftWidth, cs.borderLeftStyle],
      ] as const;
      for (const [side, w, st] of sides) {
        if (w !== '0px' && st !== 'none') {
          offenders.push(`${el.getAttribute('aria-label') || el.tagName}:${side}=${w} ${st}`);
        }
      }
    }
    return { error: false, offenders };
  });
  expect(borders.error, 'rail found').toBe(false);
  expect(borders.offenders, `no rail item has a visible border (divider); offenders=${JSON.stringify(borders.offenders)}`).toEqual([]);

  // #2 — the bottom cluster mirrors the expanded lower area: Capabilities, Settings, and the user.
  await expect(page.locator('button[aria-label="Capabilities"]')).toBeVisible();
  await expect(page.locator('button[aria-label="Settings"]')).toBeVisible();
  await expect(page.locator('[data-testid="rail-user"]')).toBeVisible();
});
