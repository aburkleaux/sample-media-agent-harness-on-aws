import { test, expect } from '@playwright/test';
import {
  stagingLogin, newSession, openCapabilities, openCapabilityTab, send, waitForAgentDone,
} from './capability-audit.helpers';

/**
 * Task 6 — Hooks capability audit (live stack).
 *
 * Dimensions:
 *   (a) works (engine) — the built-in render hook auto-renders artifact-
 *                        producing tool results (verified out-of-band: /render
 *                        = 200; exactly-once enforced in hooks/defaults.py).
 *   (c) shows up       — HooksPanel lists the built-in "render-artifacts" hook
 *                        + the user's own hooks.
 *   (b) reflected      — create a user hook → appears under "Yours"; built-in
 *                        offers Clone (clone-to-customize) + same-name override.
 *
 * CORRECTION vs first-pass audit: the built-in hook is named **render-artifacts**
 * (not "render-on-results" — that came from a stale spec), and `ListHooks`
 * returns in ~8s warm. The earlier "30s timeout" was main-agent COLD-START on
 * the first /lab call exceeding the UI's old 30s labAction timeout (now raised
 * to 90s). See AUDIT-FINDINGS §5.
 */

test('Hooks (c) panel lists the built-in render-artifacts hook + offers create', async ({ page }) => {
  test.setTimeout(120_000);
  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Hooks');

  await expect(page.getByLabel('Search hooks')).toBeVisible({ timeout: 2_000 });
  const builtInHooks = page.getByRole('switch', { name: 'Built-in hooks' });
  await expect(builtInHooks).toHaveAttribute('aria-checked', 'false');
  await builtInHooks.click();
  await expect(builtInHooks).toHaveAttribute('aria-checked', 'true');

  // The expensive inventory resolves behind an immediately usable control row.
  await expect(page.getByText(/render-artifacts/i).first(),
    'built-in render hook should be listed').toBeVisible({ timeout: 90000 });

  await expect(page.getByRole('button', { name: 'Create hook' })).toBeVisible({ timeout: 10000 });
  // Built-in is read-only → offers Clone (clone-to-customize / override path).
  await expect(page.getByRole('button', { name: /Clone render-artifacts/i })).toBeVisible({ timeout: 10000 });
});

test('Hooks (b) create a user hook → appears under Yours', async ({ page }) => {
  test.setTimeout(120_000);
  const stamp = Date.now();
  const hookName = `pw-audit-hook-${stamp}`;

  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Hooks');
  // Wait for the initial inventory so a late response cannot overwrite the new row.
  await expect(page.getByRole('status', { name: 'Loading hooks' })).toHaveCount(0, { timeout: 90000 });
  await expect(page.getByLabel('Search hooks')).toBeVisible();

  await page.getByRole('button', { name: 'Create hook' }).click();
  await page.getByLabel('Hook name').fill(hookName);
  await page.getByLabel('Hook condition').fill('tool.role == "results"');
  await page.getByRole('button', { name: 'Save hook' }).click();

  try {
    await expect(page.getByText(hookName).first(),
      'new user hook should appear under Yours').toBeVisible({ timeout: 30000 });

    const enabledSwitch = page.getByRole('switch', { name: `Disable ${hookName}` });
    await expect(enabledSwitch).toHaveAttribute('aria-checked', 'true');
    await enabledSwitch.click();
    await expect(page.getByRole('switch', { name: `Enable ${hookName}` }))
      .toHaveAttribute('aria-checked', 'false');
    await expect(page.getByRole('switch', { name: `Enable ${hookName}` }))
      .not.toHaveAttribute('aria-busy', 'true', { timeout: 30_000 });
    await page.getByRole('switch', { name: `Enable ${hookName}` }).click();
    await expect(page.getByRole('switch', { name: `Disable ${hookName}` }))
      .toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('switch', { name: `Disable ${hookName}` }))
      .not.toHaveAttribute('aria-busy', 'true', { timeout: 30_000 });
    await expect(page.getByRole('button', { name: `Delete ${hookName}` })).toBeVisible();
  } finally {
    const deleteButton = page.getByRole('button', { name: `Delete ${hookName}` });
    if (await deleteButton.isVisible().catch(() => false)) {
      await deleteButton.click();
      await page.getByRole('button', { name: `Confirm delete ${hookName}` }).click();
      await expect(page.getByText(hookName, { exact: true })).toHaveCount(0, { timeout: 30000 });
    }
  }
});


test('Hooks (b) capability editor creates a hook from chat and it is reflected', async ({ page }) => {
  test.setTimeout(5 * 60 * 1000);
  const hookName = `pw-chat-hook-${Date.now()}`;

  await stagingLogin(page);
  await newSession(page);
  await send(page,
    `Use the capability-editor skill and the create_hook tool to create a disabled hook named "${hookName}". ` +
    `Use event after_tool_result, condition tool.role == "results", action_type notify, and ` +
    `action_params {"message":"audit"}. Keep enabled=false and report the returned assetId.`);
  await waitForAgentDone(page, 3 * 60 * 1000);

  // Chat CRUD does not currently live-refresh an already-mounted capability
  // list, so reload before checking the persisted backend state.
  try {
    await page.reload();
    await stagingLogin(page);
    await openCapabilities(page);
    await openCapabilityTab(page, 'Hooks');

    await expect(page.getByText(hookName, { exact: true }),
      'chat-created hook should appear under Yours').toBeVisible({ timeout: 90000 });
    await expect(page.getByRole('switch', { name: `Enable ${hookName}` }))
      .toHaveAttribute('aria-checked', 'false');
  } finally {
    const deleteButton = page.getByRole('button', { name: `Delete ${hookName}` });
    if (await deleteButton.isVisible().catch(() => false)) {
      await deleteButton.click();
      await page.getByRole('button', { name: `Confirm delete ${hookName}` }).click();
      await expect(page.getByText(hookName, { exact: true })).toHaveCount(0, { timeout: 30000 });
    }
  }
});
