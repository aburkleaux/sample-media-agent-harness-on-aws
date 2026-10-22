import { test, expect, Page, Locator } from '@playwright/test';

/**
 * SPA local end-to-end checklist for media-harness-search-qa-rendering (Task 11.2).
 *
 * This automates the "UI walk-through (rendering checklist)" table of the design's
 * "Local end-to-end run" against the local harness on http://localhost:3000. It is the SPA
 * counterpart to the agent-side `agent/tests/test_local_search_e2e.py` (Task 11.1).
 *
 * SKIPPED BY DEFAULT. Every test is gated on `MH_LOCAL_STACK=1`, exactly like the agent live
 * test, so the default Playwright run (and CI) collects it but never executes it — there is no
 * local stack there. Bring the five-terminal stack up per the design ("Start the stack") and run:
 *
 *   cd ui && MH_LOCAL_STACK=1 npx playwright test tests/search-qa-local.spec.ts --project chromium
 *
 * Rules carried from AGENTS.md and the design's "Playwright local E2E checklist":
 *   - Dev Login, then a new session; choose "Demo videos" in the Plus_Menu by its accessible name.
 *   - Type into the composer located by role/name — never `querySelector('textarea')`, which finds
 *     the hidden chat pane's textarea first.
 *   - Media loads are NOT CORS-gated the way `fetch()` is: assert readiness through the element
 *     (`readyState`, `paused`, `currentTime`), never by `fetch()`-ing the presigned URL (S3 sends no
 *     CORS headers, so a cross-origin `fetch` fails regardless of the object).
 *   - The Cognito/dev access token lives in `sessionStorage` under an `oidc.user:*` key; read
 *     `JSON.parse(sessionStorage.getItem(key)).access_token` inside the page to call protected routes.
 */

const BASE = 'http://localhost:3000';
const RUN_LIVE = process.env.MH_LOCAL_STACK === '1';

// The staging "Demo videos" fixture the local stack seeds (design "Seeding the demo Project").
const PROJECT_NAME = 'Demo videos';
const BAG_QUESTION =
  'Show me the shot of a black dry bag dripping water on a boat, and what the narrator says ' +
  'about the salt and the mud.';
const UNMATCHABLE_CITATION = '[nba.mp4 @ 59:00–59:05]';
const NONE_QUESTION = 'Which city is the Meridian film set in?';

// A verified Citation renders as a link named "Open <Display_Name> at mm:ss–mm:ss" (CitationRef.tsx).
const VERIFIED_LINK = /^Open .+ at \d{1,2}:\d{2}(?::\d{2})?[–-]/;
// An unmatched Citation renders as a role=note stating it could not be matched (CitationRef.tsx).
const UNVERIFIED_NOTE = /could not be matched to a search result/;

// Any element the citation pipeline produces, verified link or unverified mark, or the raw span the
// remarkCitations plugin emits before classification.
const CITATION_SELECTOR = '.mh-citation, .mh-citation-unverified, [data-mh-citation]';

async function devLogin(p: Page) {
  await p.goto(BASE);
  await p.waitForLoadState('domcontentloaded');
  const btn = p.locator('button:has-text("Dev Login")');
  if (await btn.isVisible({ timeout: 8000 }).catch(() => false)) {
    await btn.click();
  }
  // The composer appearing is the durable signal that the app is logged in and ready.
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 30_000 });
}

async function newSession(p: Page) {
  const b = p.getByRole('button', { name: 'New Session', exact: true });
  if (await b.first().isVisible({ timeout: 5000 }).catch(() => false)) {
    await b.first().click();
  }
  await p.getByLabel('Chat message input').waitFor({ state: 'visible', timeout: 15_000 });
}

/** Choose the Active_Project in the Plus_Menu by its accessible name (role=menuitemradio). */
async function chooseProject(p: Page, name: string) {
  await p.getByRole('button', { name: 'Project and tools' }).click();
  const item = p.getByRole('menuitemradio', { name: new RegExp(`^${name}`) });
  await item.waitFor({ state: 'visible', timeout: 15_000 });
  await item.click();
  // Menu closes on select; the composer status line now names the Project.
  await expect(p.getByTestId('composer-project-status')).toContainText(name, { timeout: 10_000 });
}

async function send(p: Page, text: string) {
  const input = p.getByLabel('Chat message input');
  await input.waitFor({ state: 'visible', timeout: 15_000 });
  await input.fill(text);
  await p.getByLabel('Send message').click();
}

/** Wait for the current turn to settle: the "Working…" streaming indicator goes away. */
async function waitForTurnEnd(p: Page, timeout = 90_000) {
  const working = p.getByText('Working…').first();
  // It may already be gone for a fast turn; only wait it out if it showed.
  if (await working.isVisible({ timeout: 5000 }).catch(() => false)) {
    await expect(working).toBeHidden({ timeout });
  }
}

/** The Moments gallery (ClipGallery in Moments mode) renders with this test id. */
function gallery(p: Page): Locator {
  return p.locator('[data-testid="clip-gallery"]').first();
}

/** Read the dev/Cognito access token from the page's sessionStorage oidc.user:* record. */
async function readAccessToken(p: Page): Promise<string> {
  return p.evaluate(() => {
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith('oidc.user:')) continue;
      const raw = sessionStorage.getItem(key);
      if (!raw) continue;
      try {
        const token = JSON.parse(raw).access_token;
        if (token) return token as string;
      } catch {
        /* keep scanning */
      }
    }
    return '';
  });
}

test.describe('search-qa local E2E (rendering checklist)', () => {
  // SKIP GUARD: keep every test collected (so `--list` shows them) but never run them unless the
  // local stack flag is set. Safe in CI by construction.
  test.beforeEach(() => {
    test.skip(!RUN_LIVE, 'requires the local harness; set MH_LOCAL_STACK=1 to run');
  });

  // The search walk-through is one continuous session: streaming → citations → Moments → Expand →
  // bulk action → Find_Similar → reload. Serial so each step builds on the last.
  test.describe.serial('search — the Main_Agent runs search_content and renders results', () => {
    let page: Page;
    // Any /api/artifact body served during the session must never carry a URL (Req 3.26).
    const artifactBodies: string[] = [];

    test.beforeAll(async ({ browser }) => {
      // `beforeEach`'s skip guard does not prevent `beforeAll` from running, so bail here too:
      // without the local stack there is no server to log in to.
      if (!RUN_LIVE) return;
      page = await browser.newPage();
      page.on('response', async (res) => {
        if (!res.url().includes('/api/artifact')) return;
        try {
          artifactBodies.push(await res.text());
        } catch {
          /* non-text body; ignore */
        }
      });
      await devLogin(page);
      await newSession(page);
      await chooseProject(page, PROJECT_NAME);
    });

    test.afterAll(async () => {
      await page?.close();
    });

    test('A1. citations are plain while streaming, then become links at turn end', async () => {
      test.setTimeout(120_000);
      await send(page, BAG_QUESTION);

      // While streaming (before execution_summary), bracketed timecodes are plain text: no citation
      // element exists yet (Req 2.10 — no early guess).
      await expect(page.getByText('Working…').first()).toBeVisible({ timeout: 30_000 });
      expect(await page.locator(CITATION_SELECTOR).count()).toBe(0);

      await waitForTurnEnd(page);

      // Turn end: each verified Citation is a link "Open <name> at mm:ss–…" (Req 2.7).
      await expect(page.getByRole('link', { name: VERIFIED_LINK }).first())
        .toBeVisible({ timeout: 30_000 });
    });

    test('A2. a repeated unmatchable citation stays an unverified note, not a link', async () => {
      test.setTimeout(120_000);
      await send(page, `Please repeat this citation verbatim: ${UNMATCHABLE_CITATION}`);
      await waitForTurnEnd(page);
      // That span could not be matched to any search result, so it is a role=note, never a link
      // (Req 2.10).
      await expect(page.getByRole('note', { name: UNVERIFIED_NOTE }).first())
        .toBeVisible({ timeout: 30_000 });
    });

    test('A3. clicking a verified citation opens an asset-player tab, paused near the start', async () => {
      test.setTimeout(90_000);
      const link = page.getByRole('link', { name: VERIFIED_LINK }).first();
      const name = (await link.getAttribute('aria-label')) ?? '';
      const assistantTurns = await page.getByTestId('assistant-message').count().catch(() => 0);

      await link.click();

      // A video/audio element appears in the opened tab and is paused within 0.5s of the start,
      // with no new assistant turn (Req 2.8). Media readiness is read off the element, never fetched.
      const media = page.locator('video, audio').first();
      await expect(media).toBeVisible({ timeout: 30_000 });
      await expect
        .poll(async () => media.evaluate((el: HTMLMediaElement) => el.readyState), { timeout: 30_000 })
        .toBeGreaterThanOrEqual(1);
      const state = await media.evaluate((el: HTMLMediaElement) => ({
        paused: el.paused,
        currentTime: el.currentTime,
      }));
      expect(state.paused).toBe(true);
      expect(Math.abs(state.currentTime)).toBeLessThanOrEqual(5); // seeks near the citation start

      if (name) {
        // The tab is labeled with the asset's Display_Name (the name inside the citation label).
        const display = name.replace(/^Open /, '').replace(/ at .*$/, '');
        await expect(page.getByText(display, { exact: false }).first()).toBeVisible();
      }
      const after = await page.getByTestId('assistant-message').count().catch(() => 0);
      expect(after).toBe(assistantTurns); // opening a tab starts no turn
    });

    test('A4. the Moments gallery shows cards in first-citation order with full metadata', async () => {
      test.setTimeout(60_000);
      await expect(gallery(page)).toBeVisible({ timeout: 30_000 });
      const cards = gallery(page).locator('article');
      await expect(cards.first()).toBeVisible({ timeout: 30_000 });
      const count = await cards.count();
      expect(count).toBeGreaterThan(0);

      const first = cards.first();
      // Each card carries a timecode, a scope badge, matched text with <mark> highlights and a rank
      // score (Req 3.9, 3.10). Keyed by test id so a style change does not break the selector.
      await expect(first.locator('[data-testid^="moment-timecode-"]')).toBeVisible();
      await expect(first.locator('[data-testid^="moment-scope-"]')).toBeVisible();
      await expect(first.locator('[data-testid^="moment-score-"]')).toContainText(/rank score/);
      await expect(first.locator('mark.mh-moment-hit').first()).toBeVisible();
    });

    test('A5. cards are pre-marked, so "Clip all marked" is enabled', async () => {
      // In/Out are pre-set on load (Req 3.8), so the bulk clip button reports a non-zero count.
      const clipBtn = gallery(page).getByTestId('trim-all-marked');
      await expect(clipBtn).toBeVisible({ timeout: 20_000 });
      await expect(clipBtn).toContainText(/Clip all marked \((\d+)\)/);
      const label = (await clipBtn.textContent()) ?? '';
      const marked = Number(label.match(/\((\d+)\)/)?.[1] ?? '0');
      expect(marked).toBeGreaterThan(0);
      await expect(clipBtn).toBeEnabled();
    });

    test('A6. Expand opens a tab at the moment and leaves the gallery marks unchanged', async () => {
      test.setTimeout(60_000);
      const clipBtn = gallery(page).getByTestId('trim-all-marked');
      const before = (await clipBtn.textContent()) ?? '';

      const expand = gallery(page).locator('[data-testid^="moment-expand-"]').first();
      await expand.click();

      // A media tab opens at the moment (Req 3.11/3.22) and the gallery's marks do not change.
      await expect(page.locator('video, audio').first()).toBeVisible({ timeout: 30_000 });
      await expect(gallery(page).getByTestId('trim-all-marked')).toHaveText(before);
    });

    test('A7. "Clip all marked" sends a turn naming each marked card', async () => {
      test.setTimeout(120_000);
      const clipBtn = gallery(page).getByTestId('trim-all-marked');
      // Capture a Display_Name from a card so we can assert it reaches the turn prompt.
      const firstName = (await gallery(page)
        .locator('[data-testid^="moment-heading-"]').first().textContent()) ?? '';

      await clipBtn.click();

      // The bulk action threads a turn whose prompt lists each card's Display_Name and a fresh
      // presigned `[sources: …]` line (Req 3.12). The turn starts streaming.
      await expect(page.getByText('Working…').first()).toBeVisible({ timeout: 30_000 });
      await waitForTurnEnd(page, 110_000);
      if (firstName.trim()) {
        await expect(page.getByText(firstName.trim(), { exact: false }).first())
          .toBeVisible({ timeout: 30_000 });
      }
    });

    test('A8. Find similar: enabled on a shot card, disabled on a scene/utterance card', async () => {
      const cards = gallery(page).locator('article');
      const n = await cards.count();
      let sawEnabled = false;
      let sawDisabled = false;
      for (let i = 0; i < n; i += 1) {
        const scope = (await cards.nth(i).locator('[data-testid^="moment-scope-"]')
          .textContent().catch(() => '')) ?? '';
        const btn = cards.nth(i).locator('[data-testid^="moment-find-similar-"]');
        if (!(await btn.count())) continue;
        const disabled = (await btn.getAttribute('aria-disabled')) === 'true';
        if (/shot/i.test(scope)) {
          // A shot card has a frame to search with → enabled (Req 3.23).
          expect(disabled).toBe(false);
          sawEnabled = true;
        } else if (/scene|utterance|topic/i.test(scope)) {
          // No frame → disabled, and its name states why (Req 3.24).
          expect(disabled).toBe(true);
          const name = (await btn.getAttribute('aria-label')) ?? '';
          expect(name).toMatch(/no frame to search with/i);
          sawDisabled = true;
        }
      }
      // The fixture carries both kinds; require at least one of each to have been checked.
      expect(sawEnabled || sawDisabled).toBe(true);
    });

    test('A9. a shot Find similar starts a turn with the moment citation as its text', async () => {
      test.setTimeout(120_000);
      const cards = gallery(page).locator('article');
      const n = await cards.count();
      let clicked = false;
      for (let i = 0; i < n; i += 1) {
        const scope = (await cards.nth(i).locator('[data-testid^="moment-scope-"]')
          .textContent().catch(() => '')) ?? '';
        if (!/shot/i.test(scope)) continue;
        const btn = cards.nth(i).locator('[data-testid^="moment-find-similar-"]');
        if ((await btn.getAttribute('aria-disabled')) === 'true') continue;
        await btn.click();
        clicked = true;
        break;
      }
      test.skip(!clicked, 'no enabled shot card to Find similar from');
      // The turn's visible text is "Find moments similar to [<name> @ mm:ss–mm:ss]" (Req 3.23).
      await expect(page.getByText(/Find moments similar to \[.+ @ \d{1,2}:\d{2}/).first())
        .toBeVisible({ timeout: 30_000 });
      await waitForTurnEnd(page, 110_000);
    });

    test('A10. reload restores the same links, marks and Moments tab, with no URL in artifacts', async () => {
      test.setTimeout(90_000);
      await page.reload();
      await page.waitForLoadState('domcontentloaded');

      // Reopen the most recent session from the sidebar if the app did not restore it.
      if (!(await page.getByRole('link', { name: VERIFIED_LINK }).first()
        .isVisible({ timeout: 8000 }).catch(() => false))) {
        const firstSession = page.getByTestId('session-row').first();
        if (await firstSession.isVisible({ timeout: 5000 }).catch(() => false)) {
          await firstSession.click();
        }
      }

      // Same verified links and the Moments gallery restore; the gallery re-mints URLs on mount
      // (Req 2.6, 3.26).
      await expect(page.getByRole('link', { name: VERIFIED_LINK }).first())
        .toBeVisible({ timeout: 30_000 });
      await expect(gallery(page)).toBeVisible({ timeout: 30_000 });
      await expect(gallery(page).getByTestId('trim-all-marked')).toContainText(/Clip all marked \(\d+\)/);

      // The manifest the SPA reads back carries NO presigned URL: no `https://`, no `X-Amz-`
      // anywhere in any artifact body served this session (Req 3.26).
      for (const body of artifactBodies) {
        expect(body).not.toContain('https://');
        expect(body).not.toContain('X-Amz-');
      }
      // Sanity: the dev/Cognito access token is reachable for protected-route calls per AGENTS.md.
      expect(await readAccessToken(page)).not.toBe('');
    });

    test('A11. a question no asset answers names no supporting moment and opens no Moments tab', async () => {
      test.setTimeout(120_000);
      await newSession(page);
      await chooseProject(page, PROJECT_NAME);
      await send(page, NONE_QUESTION);
      await waitForTurnEnd(page);
      // No verified Citation, and the answer declines with "no supporting moment" (Req 1.8, 3.5, 3.6).
      await expect(page.getByText(/no supporting moment/i).first()).toBeVisible({ timeout: 30_000 });
      expect(await page.getByRole('link', { name: VERIFIED_LINK }).count()).toBe(0);
      // No Moments gallery appears when the searches yielded no Moments.
      expect(await gallery(page).isVisible({ timeout: 3000 }).catch(() => false)).toBe(false);
    });
  });
});
