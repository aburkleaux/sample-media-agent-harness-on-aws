import { test, expect } from '@playwright/test';
import { stagingLogin, openCapabilities, openCapabilityTab, BASE } from './capability-audit.helpers';

/**
 * Task 2 — Model capability audit (live stack).
 *
 * Audit dimensions:
 *   (a) works        — a model switch is accepted by the backend
 *   (b) reflected    — the selection persists (reload) + surfaces on the
 *                      composer's active-model badge
 *   (c) shows up     — the Model tab renders the selector + full catalog
 *
 * Backend: GET /models (model_catalog.discover_models → Bedrock
 * ListInferenceProfiles); switch persists via `mcp_set_model` on the main
 * agent + localStorage['lab-orchestrator-model-id'].
 *
 * FIX (this branch): (a)/(b) were broken on the deployed main agent —
 * `mcp_set_model` raised `NameError: name '_boto_cfg' is not defined`
 * (main.py: `_boto_cfg` is function-local at ~L456 but the action handler
 * referenced it at ~L1360). The handler now constructs its own boto config.
 * Both tests below are live verification of list + switch + persistence.
 */

const MODEL_KEY = 'lab-orchestrator-model-id';
const TARGET_ID = 'us.anthropic.claude-3-haiku-20240307-v1:0';

test('Model (c) catalog lists and the selector renders every model', async ({ page }) => {
  test.setTimeout(90_000);

  const modelsResp = await page.request.get(`${BASE}/models`);
  expect(modelsResp.ok(), 'GET /models should return 200').toBeTruthy();
  const catalog = (await modelsResp.json()).models as Array<{ id: string; name: string }>;
  expect(catalog.length, 'model catalog should be non-empty').toBeGreaterThan(1);
  expect(catalog.some((m) => m.id === TARGET_ID), `catalog should include ${TARGET_ID}`).toBeTruthy();

  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Model');

  // Cold fetchModels can sit on "Loading available models..." (latency
  // finding — see report). Wait it out, then assert the selector + options.
  await expect(page.getByText('Loading available models...')).toHaveCount(0, { timeout: 45000 });
  const selector = page.getByLabel('Select orchestrator model');
  await expect(selector).toBeVisible({ timeout: 15000 });
  expect(await selector.locator('option').count(), 'selector lists all models').toBe(catalog.length);
});

test('Model (a)(b) switching is accepted, persists, and reflects on the composer badge', async ({ page }) => {
  test.setTimeout(120_000);
  // FIXED (this branch): mcp_set_model previously raised
  // `NameError: name '_boto_cfg' is not defined` because `_boto_cfg` is
  // function-local in main.py. The handler now builds its own boto config.
  // This test is the verification gate for that fix.

  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Model');
  await expect(page.getByText('Loading available models...')).toHaveCount(0, { timeout: 45000 });

  const selector = page.getByLabel('Select orchestrator model');
  await expect(selector).toBeVisible({ timeout: 15000 });

  // (a) switch — must be accepted with no error banner.
  await selector.selectOption(TARGET_ID);
  await page.waitForTimeout(1500);
  await expect(page.getByText('Switching...')).toHaveCount(0, { timeout: 30000 });
  await expect(page.getByText(/Failed to switch model/i), 'no switch error banner').toHaveCount(0);

  // (b) persists to localStorage and survives a reload.
  expect(await page.evaluate((k) => localStorage.getItem(k), MODEL_KEY)).toBe(TARGET_ID);
  await page.reload();
  await stagingLogin(page);
  expect(await page.evaluate((k) => localStorage.getItem(k), MODEL_KEY)).toBe(TARGET_ID);

  // (b) composer active-model badge = formatModelId(activeModel) → "...Haiku...".
  await expect(page.getByText(/Haiku/i).first()).toBeVisible({ timeout: 15000 });
});
