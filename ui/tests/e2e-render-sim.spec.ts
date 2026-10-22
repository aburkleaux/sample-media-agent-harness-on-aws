import { test, expect, Page } from '@playwright/test';

// LOCAL render simulation in real Chromium: render the REAL manifests the agent harness
// (scripts/e2e_render_local.py) wrote under artifacts/e2e-95f19fcf-c*/render/, via the
// production manifest-router path (data.manifestUrl -> /api/artifact -> fetch -> parse ->
// component). Proves the manifest -> PIXELS step the manifest-level harness cannot.
const BASE = 'http://localhost:3000';
const RUN = `rsim-${Date.now()}`;
const A = 'artifacts/e2e-95f19fcf';

const murl = (k: string) => `/api/artifact?key=${encodeURIComponent(k)}`;
const blk = (k: string, title: string) =>
  ({ turnIndex: 0, timestamp: Date.now(),
     blocks: [{ template: 'manifest-router', title, data: { manifestUrl: murl(k) } }] });

type Case = { name: string; sid: string; title: string; media: 'video'|'img'|'audio'; tab?: string; history: any[] };
const CASES: Case[] = [
  { name: 'c1 planflow', sid: `${RUN}-c1`, title: `${RUN} c1 planflow`, media: 'video', tab: 'Shot',
    history: [blk(`${A}-c1/render/sg_shots.json`, 'Shot Segmentation'),
              blk(`${A}-c1/render/sg_diarize.json`, 'Speaker Diarization')] },
  { name: 'c2 shots', sid: `${RUN}-c2`, title: `${RUN} c2 shots`, media: 'video',
    history: [blk(`${A}-c2/render/a829c01087f9ab526f13b299a2501a62.json`, 'Shot Segmentation')] },
  { name: 'c3 image', sid: `${RUN}-c3`, title: `${RUN} c3 image`, media: 'img',
    history: [blk(`${A}-c3/render/77501e687761c5ce2dbb054b8486da14.json`, 'Image')] },
  { name: 'c4 rule1', sid: `${RUN}-c4`, title: `${RUN} c4 rule1`, media: 'video',
    history: [blk(`${A}-c4/render/arn_aws_states_us-west-2_100000000002_execution_micro-workflow-shot-segmentation_e2e-rule1-exec.json`, 'Shot Segmentation')] },
  { name: 'c5 multi', sid: `${RUN}-c5`, title: `${RUN} c5 multi`, media: 'audio', tab: 'Audio',
    history: [blk(`${A}-c5/render/sg_diarize--audio.json`, 'Speaker Audio'),
              blk(`${A}-c5/render/sg_diarize--other.json`, 'Speaker Data')] },
];

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  await p.waitForTimeout(1000);
  const enter = p.locator("button:has-text('Explore the harness'), button:has-text('Sign in')").first();
  if (await enter.isVisible({ timeout: 5000 }).catch(() => false)) { await enter.click(); await p.waitForTimeout(1200); }
  const dev = p.locator("button:has-text('Dev Login')");
  if (await dev.isVisible({ timeout: 6000 }).catch(() => false)) { await dev.click(); await p.waitForTimeout(2500); }
}

test.describe('LOCAL render sim — real harness manifests -> pixels', () => {
  test.beforeAll(async ({ request }) => {
    for (const c of CASES) {
      await request.post(`${BASE}/sessions`, { data: { sessionId: c.sid, title: c.title } }).catch(() => {});
      await request.patch(`${BASE}/sessions/${encodeURIComponent(c.sid)}`, { data: { renderHistory: c.history } }).catch(() => {});
    }
  });
  test.afterAll(async ({ request }) => {
    for (const c of CASES) await request.delete(`${BASE}/sessions/${encodeURIComponent(c.sid)}`).catch(() => {});
  });

  for (const c of CASES) {
    test(`renders ${c.name}`, async ({ page }) => {
      const errs: string[] = [];
      page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

      await devLogin(page);
      const link = page.locator(`text=${c.title}`).first();
      await expect(link, `sidebar shows ${c.title}`).toBeVisible({ timeout: 15000 });
      await link.click();
      const rtab = page.locator("button:has-text('Renderer'), [role='tab']:has-text('Renderer')").first();
      if (await rtab.isVisible({ timeout: 4000 }).catch(() => false)) { await rtab.click().catch(() => {}); }
      // Select the render tab that carries the expected medium (multi-tab cases).
      if (c.tab) {
        const t = page.locator(`[role='tab']:has-text('${c.tab}'), button:has-text('${c.tab}')`).first();
        if (await t.isVisible({ timeout: 5000 }).catch(() => false)) { await t.click().catch(() => {}); }
      }
      // Poll for the actual media element (auto-waits through fetch + mount).
      const sel = c.media === 'img' ? 'img' : c.media; // 'video' | 'audio' | 'img'
      await expect(page.locator(sel).first(), `${c.media} element rendered`).toBeVisible({ timeout: 20000 });

      const body = (await page.locator('body').innerText()).replace(/\s+/g, ' ');
      expect(body, 'no error card').not.toContain('Could not display');
      expect(body, 'no unresolved-source error').not.toContain('Unable to resolve');
      expect(errs, `no uncaught page errors: ${errs.join(' | ')}`).toHaveLength(0);
      console.log(`[RSIM ${c.name}] OK — ${c.media} rendered`);
    });
  }
});
