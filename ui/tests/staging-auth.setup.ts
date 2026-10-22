import { expect, test as setup } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CREATED_SESSIONS_PATH } from './capability-audit.helpers';

const STAGING_URL = process.env.STAGING_URL ?? 'https://dexamplecdn004.cloudfront.net';
const SESSION_PATH = fileURLToPath(new URL('../test-results/staging-session-storage.json', import.meta.url));

setup('authenticate through staging Cognito Hosted UI', async ({ page }) => {
  const suiteStartedAt = new Date().toISOString();
  const email = process.env.STAGING_EMAIL;
  const password = process.env.STAGING_PASSWORD;
  if (!email || !password) {
    throw new Error(
      'Direct-staging capability tests require STAGING_EMAIL and STAGING_PASSWORD. ' +
      'Credentials must be supplied externally and must never be committed.',
    );
  }

  await page.goto(STAGING_URL);
  await page.waitForLoadState('domcontentloaded');
  const signIn = page.getByRole('button', { name: /sign in/i }).first();
  await expect(signIn).toBeVisible({ timeout: 15_000 });
  await signIn.click();

  await expect(page).toHaveURL(/amazoncognito\.com/, { timeout: 30_000 });
  const emailInput = page.getByLabel(/email address/i);
  const passwordInput = page.getByLabel(/^password$/i);
  await emailInput.fill(email);
  await passwordInput.fill(password);
  await expect(emailInput).toHaveValue(email);
  await expect(passwordInput).toHaveValue(password);
  await page.getByRole('button', { name: /^sign in$/i }).click();

  await page.waitForURL(url => url.origin === new URL(STAGING_URL).origin, { timeout: 60_000 });
  await expect(page.getByLabel('Chat message input')).toBeVisible({ timeout: 30_000 });

  const entries = await page.evaluate(() => Object.entries(sessionStorage));
  if (!entries.some(([key]) => key.startsWith('oidc.user:'))) {
    throw new Error('Cognito callback completed without an oidc-client-ts sessionStorage record');
  }
  mkdirSync(path.dirname(SESSION_PATH), { recursive: true });
  // Reset the record of sessions THIS run creates, so the teardown deletes ONLY our own
  // sessions (recorded via attachSessionRecorder) and never real user sessions that merely
  // happen to be created during the run.
  writeFileSync(CREATED_SESSIONS_PATH, '', { encoding: 'utf8', mode: 0o600 });
  writeFileSync(SESSION_PATH, JSON.stringify({
    origin: new URL(STAGING_URL).origin,
    entries,
    suiteStartedAt,
  }), {
    encoding: 'utf8',
    mode: 0o600,
  });
});
