import { defineConfig } from '@playwright/test';

const stagingURL = process.env.STAGING_URL ?? 'https://dexamplecdn004.cloudfront.net';
const capabilityAudits = '**/audit-*.spec.ts';
const stagingAuthSetup = '**/staging-auth.setup.ts';
const stagingAuthTeardown = '**/staging-auth.teardown.ts';

export default defineConfig({
  testDir: './tests',
  timeout: 120_000,
  fullyParallel: true,
  workers: process.env.CI ? 3 : 5,
  retries: 0,
  use: {
    baseURL: 'http://localhost:3000',
    headless: true,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'uat',
      testMatch: '**/m2c-gateway-tools-uat*',
      fullyParallel: false,
      use: { browserName: 'chromium' },
    },
    {
      name: 'routing-uat',
      testMatch: '**/routing-uat*',
      fullyParallel: false,
      use: { browserName: 'chromium' },
    },
    {
      name: 'chromium',
      testIgnore: ['**/m2c-gateway-tools-uat*', '**/routing-uat*', capabilityAudits, stagingAuthSetup, stagingAuthTeardown],
      use: { browserName: 'chromium' },
    },
    {
      name: 'capabilities-staging-auth',
      testMatch: stagingAuthSetup,
      teardown: 'capabilities-staging-cleanup',
      fullyParallel: false,
      use: { browserName: 'chromium', baseURL: stagingURL },
    },
    {
      name: 'capabilities-staging-cleanup',
      testMatch: stagingAuthTeardown,
      fullyParallel: false,
      use: { browserName: 'chromium', baseURL: stagingURL },
    },
    {
      name: 'capabilities-staging',
      testMatch: capabilityAudits,
      dependencies: ['capabilities-staging-auth'],
      fullyParallel: false,
      use: { browserName: 'chromium', baseURL: stagingURL },
    },
  ],
});
