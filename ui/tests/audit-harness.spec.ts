import { test, expect } from '@playwright/test';
import { stagingLogin, openCapabilities, openCapabilityTab } from './capability-audit.helpers';

/**
 * Task 1 smoke test — proves the capability-audit harness works end-to-end
 * against the live stack: dev-bypass login lands in the app, the Capabilities
 * settings modal opens, and the Skills tab renders the bundled built-in skills.
 *
 * If this fails, the shared helpers (login / settings-nav) are stale — fix
 * them before trusting the per-capability audit specs.
 */
test('harness smoke: login → Capabilities → Skills tab shows built-in skills', async ({ page }) => {
  test.setTimeout(90_000);
  await stagingLogin(page);

  await openCapabilities(page);
  await openCapabilityTab(page, 'Skills');

  // Proof the modal opened on the Skills tab: the modal-only "Model" tab,
  // the Skills description, and the "+ Create" affordance are present.
  await expect(page.getByRole('button', { name: 'Model', exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByText('Capabilities the agent can load per workspace.')).toBeVisible({ timeout: 10000 });
  await expect(page.getByRole('button', { name: '+ Create' })).toBeVisible({ timeout: 10000 });

  // Built-in inventory is intentionally hidden until requested.
  const builtInSkills = page.getByRole('switch', { name: 'Built-in skills' });
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'false');
  await builtInSkills.click();
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByText(/Built-in Skills/i).first()).toBeVisible({ timeout: 10000 });
});
