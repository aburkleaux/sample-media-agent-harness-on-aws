import { test, expect } from '@playwright/test';
import {
  stagingLogin, labActionInPage, newSession, send, waitForAgentDone,
  openCapabilities, openCapabilityTab,
} from './capability-audit.helpers';

/**
 * Phase 2 — the in-product PRE-PUBLISH test loop.
 *
 * User story: I create/upload a draft (skill, custom agent, tool collection),
 * I want to TRY it and see it work, and then *I* decide when to publish (or
 * leave it as a draft). Publishing is only blocked by a configuration error.
 *
 * Before this phase the loop was unreachable: `onTest` was threaded down into
 * `ListRow` but no Test button was ever rendered, `setHarnessOpen(true)` was
 * never called (so the inline harness could not open), the drawers that wired
 * the harness were orphaned, and there was no Publish action in the UI at all
 * (`publishAsset` didn't exist in lab-api).
 */

test('Pre-publish loop: draft row exposes Test + Publish, and Test opens the harness bound to that draft', async ({ page }) => {
  test.setTimeout(6 * 60 * 1000);
  const stamp = Date.now();
  const skillName = `pw-prepublish-${stamp}`;

  await stagingLogin(page);
  await newSession(page);

  // Arrange: create a draft skill so there is something to test/publish.
  await send(page,
    `Use the create_skill_draft tool to create a skill draft named "${skillName}". ` +
    `SKILL.md body: "Skill ${skillName}: benign pre-publish test-loop fixture. It performs no action."`);
  await waitForAgentDone(page, 3 * 60 * 1000);

  try {
    // NOTE (finding): a draft created from CHAT does not live-refresh the
  // Capabilities lists (LabContext fetches per category on mount/refresh), so
  // reload to pick it up. Worth wiring a refresh on chat-driven CRUD later.
  await page.reload();
  await stagingLogin(page);

  await openCapabilities(page);
  await openCapabilityTab(page, 'Skills');

  // The draft row is present with BOTH lifecycle actions reachable.
  const row = page.locator('div').filter({ hasText: skillName }).last();
  await expect(page.getByText(skillName).first()).toBeVisible({ timeout: 30000 });
  await expect(page.getByRole('button', { name: `Test ${skillName}` }),
    'draft row should expose a Test action').toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: `Publish ${skillName}` }),
    'draft row should expose a Publish action (user decides when)').toBeVisible({ timeout: 15000 });

  // Act: Test opens a nested Harness view, pre-bound to THIS draft.
  await page.getByRole('button', { name: `Test ${skillName}` }).click();
  await expect(page.getByRole('region', { name: 'Test Harness view' })).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: '← Back to Skills' })).toBeVisible();
  await expect(page.getByRole('button', { name: '+ Create', exact: true })).toHaveCount(0);
  await expect(page.getByText('Test Harness')).toBeVisible({ timeout: 15000 });
  await expect(page.getByText('Target Draft')).toBeVisible({ timeout: 10000 });
  // Target shows the draft's NAME (not a raw assetId) plus its category.
  await expect(page.getByText(skillName).first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText('skill', { exact: true }).first()).toBeVisible({ timeout: 10000 });

  // The harness is runnable once sample input is supplied (Run is gated on input,
  // never on "did you test before publishing").
  const runBtn = page.getByRole('button', { name: 'Run', exact: true });
    await expect(runBtn).toBeVisible({ timeout: 10000 });
  } finally {
    const drafts = await labActionInPage<Array<{ assetId: string; name: string }>>(
      page, 'ListSkillDrafts',
    );
    for (const draft of drafts.filter(item => item.name === skillName)) {
      await labActionInPage(page, 'DeleteSkillDraft', { assetId: draft.assetId });
    }
    const remaining = await labActionInPage<Array<{ name: string }>>(page, 'ListSkillDrafts');
    expect(remaining.filter(item => item.name === skillName),
      `cleanup should remove ${skillName}`).toHaveLength(0);
  }
});
