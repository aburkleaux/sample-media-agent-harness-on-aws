import { expect, test } from '@playwright/test';
import { openCapabilityTab, stagingLogin } from './capability-audit.helpers';

type ObservedRequest = { url: string; action?: string; startedAt: number };

function observeCapabilityRequests(page: import('@playwright/test').Page): ObservedRequest[] {
  const observed: ObservedRequest[] = [];
  page.on('request', request => {
    const url = request.url();
    if (!/\/(lab|catalog|api\/invocations)(\?|$)/.test(url)) return;
    let action: string | undefined;
    try { action = request.postDataJSON()?.action; } catch { /* no JSON body */ }
    observed.push({ url, action, startedAt: Date.now() });
  });
  return observed;
}

const DRAFT_ACTIONS = [
  'ListSkillDrafts',
  'ListSpecialtyAgentDrafts',
  'ListToolCollectionDrafts',
  'ListWorkflowDrafts',
  'ListHooks',
];

async function openSettingsShell(page: import('@playwright/test').Page): Promise<number> {
  const button = page.getByRole('button', { name: 'Capabilities', exact: true }).first();
  await button.waitFor({ state: 'visible', timeout: 30_000 });
  const startedAt = Date.now();
  await button.click();
  await expect(page.getByRole('button', { name: 'Skills', exact: true }).last()).toBeVisible({ timeout: 500 });
  return Date.now() - startedAt;
}

test('NFR: empty initial and repeated New sessions remain ephemeral', async ({ page }) => {
  test.setTimeout(60_000);
  const sessionCreates: string[] = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (request.method() === 'POST' && url.pathname === '/sessions') {
      sessionCreates.push(request.postData() ?? '');
    }
  });

  await stagingLogin(page);
  await page.waitForTimeout(500);
  expect(sessionCreates, 'initial empty workspace must not persist').toEqual([]);

  const newSession = page.getByRole('button', { name: 'New session', exact: true });
  await newSession.click();
  await newSession.click();
  await newSession.click();
  await page.waitForTimeout(500);

  expect(sessionCreates, 'empty New session clicks must not persist').toEqual([]);

  await page.getByLabel('Chat message input').fill('session persistence smoke');
  const firstCreate = page.waitForRequest(request => {
    const url = new URL(request.url());
    return request.method() === 'POST' && url.pathname === '/sessions';
  });
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  const createRequest = await firstCreate;
  expect(createRequest.postDataJSON()).toMatchObject({ title: 'session persistence smoke' });
  expect(sessionCreates, 'first valid input must persist exactly once').toHaveLength(1);
  const stop = page.getByRole('button', { name: 'Stop agent' });
  if (await stop.isVisible({ timeout: 2_000 }).catch(() => false)) await stop.click();
});

test('NFR: capability controls stay usable while inventory loads and Create opens inside Settings', async ({ page }) => {
  test.setTimeout(90_000);
  let delayedSkillInventory = false;
  await page.route('**/lab', async route => {
    let action = '';
    try { action = route.request().postDataJSON()?.action ?? ''; } catch { /* no JSON body */ }
    if (!delayedSkillInventory && action === 'ListSkillDrafts') {
      delayedSkillInventory = true;
      await new Promise(resolve => setTimeout(resolve, 3_000));
    }
    await route.continue();
  });

  await stagingLogin(page);
  await openSettingsShell(page);
  await openCapabilityTab(page, 'Skills');

  const settings = page.getByRole('dialog', { name: 'Settings' });
  await expect(settings.getByRole('switch', { name: 'Built-in skills' })).toBeVisible({ timeout: 1_000 });
  await expect(settings.getByRole('button', { name: '+ Create', exact: true })).toBeVisible({ timeout: 1_000 });
  await expect(settings.getByRole('status', { name: 'Loading your skills' })).toBeVisible({ timeout: 1_000 });

  const builtInSkills = settings.getByRole('switch', { name: 'Built-in skills' });
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'false');
  await builtInSkills.click();
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'true');
  await builtInSkills.click();
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'false');

  await settings.getByRole('button', { name: '+ Create', exact: true }).click();
  await expect(settings.getByText('New Skill', { exact: true })).toBeVisible();
  await expect(settings.getByRole('button', { name: 'Back to Skills' })).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(settings).toBeVisible();
  await expect(settings.getByRole('button', { name: '+ Create', exact: true })).toBeVisible();

  await openCapabilityTab(page, 'Agents');
  await expect(settings.getByRole('switch', { name: 'Built-in agents' })).toHaveAttribute('aria-checked', 'false');
  await openCapabilityTab(page, 'Tools');
  await expect(settings.getByRole('switch', { name: 'Gateway tools' })).toHaveAttribute('aria-checked', 'false');
  await openCapabilityTab(page, 'Hooks');
  await expect(settings.getByRole('switch', { name: 'Built-in hooks' })).toHaveAttribute('aria-checked', 'false');
  await expect(settings.getByLabel('Search hooks')).toBeVisible();
});

test('NFR: workspace is lazy; Skills is deduplicated and warm reopen is cached', async ({ page }) => {
  test.setTimeout(120_000);
  const requests = observeCapabilityRequests(page);
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await stagingLogin(page);
  await page.waitForTimeout(1_000);

  const startupInventory = requests.filter(r =>
    DRAFT_ACTIONS.includes(r.action ?? '') ||
    r.action === 'ListMyPublishedAssets' ||
    r.action === 'list_skills' ||
    r.action === 'mcp_list_agents' ||
    r.url.includes('/catalog'),
  );
  expect(startupInventory, 'workspace startup must not prefetch capability inventory').toHaveLength(0);

  const shellMs = await openSettingsShell(page);
  expect(shellMs, 'settings shell should be perceivable within 500 ms').toBeLessThanOrEqual(500);

  await openCapabilityTab(page, 'Skills');
  const coldStartedAt = Date.now();
  const coldBuiltIns = page.getByRole('switch', { name: 'Built-in skills' });
  await expect(coldBuiltIns).toHaveAttribute('aria-checked', 'false');
  await coldBuiltIns.click();
  await expect(page.getByText(/Bundled \(filesystem\)/i).first()).toBeVisible({ timeout: 20_000 });
  const coldMs = Date.now() - coldStartedAt;
  expect(coldMs, 'cold Skills target is 20 seconds').toBeLessThanOrEqual(20_000);

  expect(requests.filter(r => r.action === 'ListSkillDrafts')).toHaveLength(1);
  expect(requests.filter(r => r.action === 'ListMyPublishedAssets')).toHaveLength(1);
  expect(requests.filter(r => r.action === 'list_skills')).toHaveLength(1);
  for (const action of ['ListSpecialtyAgentDrafts', 'ListToolCollectionDrafts', 'ListWorkflowDrafts', 'ListHooks', 'mcp_list_agents']) {
    expect(requests.filter(r => r.action === action), `Skills must not load ${action}`).toHaveLength(0);
  }
  expect(requests.filter(r => r.url.includes('/catalog')), 'Skills must not load the gateway catalog').toHaveLength(0);

  const beforeWarm = requests.length;
  await page.keyboard.press('Escape');
  const warmStartedAt = Date.now();
  await openSettingsShell(page);
  await openCapabilityTab(page, 'Skills');
  const warmBuiltIns = page.getByRole('switch', { name: 'Built-in skills' });
  if (await warmBuiltIns.getAttribute('aria-checked') === 'false') await warmBuiltIns.click();
  await expect(page.getByText(/Bundled \(filesystem\)/i).first()).toBeVisible({ timeout: 2_000 });
  expect(Date.now() - warmStartedAt, 'warm Skills target is 2 seconds').toBeLessThanOrEqual(2_000);

  const warmRequests = requests.slice(beforeWarm).filter(r =>
    r.action === 'ListSkillDrafts' || r.action === 'ListMyPublishedAssets' || r.action === 'list_skills',
  );
  expect(warmRequests, 'warm reopen must not duplicate Skills inventory calls').toHaveLength(0);
  expect(pageErrors, 'warm navigation must not raise uncaught page errors').toEqual([]);
});

test('NFR: gateway catalog reports MISS then HIT and UI Refresh bypasses caches', async ({ page }) => {
  test.setTimeout(120_000);
  const requests = observeCapabilityRequests(page);
  await stagingLogin(page);

  const cacheHeaders = await page.evaluate(async () => {
    let accessToken = '';
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith('oidc.user:')) continue;
      const raw = sessionStorage.getItem(key);
      if (!raw) continue;
      try { accessToken = JSON.parse(raw).access_token ?? ''; } catch { /* continue */ }
      if (accessToken) break;
    }
    if (!accessToken) throw new Error('Missing Cognito access token');
    const headers = { Authorization: `Bearer ${accessToken}` };
    const miss = await fetch('/catalog?refresh=1', { headers, cache: 'no-store' });
    if (!miss.ok) throw new Error(`Forced catalog request failed: ${miss.status}`);
    await miss.json();
    const hit = await fetch('/catalog', { headers, cache: 'no-store' });
    if (!hit.ok) throw new Error(`Warm catalog request failed: ${hit.status}`);
    await hit.json();
    return {
      miss: miss.headers.get('x-capability-cache'),
      hit: hit.headers.get('x-capability-cache'),
    };
  });
  expect(cacheHeaders).toEqual({ miss: 'MISS', hit: 'HIT' });

  await openSettingsShell(page);
  await openCapabilityTab(page, 'Tools');
  const gatewayTools = page.getByRole('switch', { name: 'Gateway tools' });
  await expect(gatewayTools).toHaveAttribute('aria-checked', 'false');
  await gatewayTools.click();
  await expect(page.getByText('Loading tools...')).toHaveCount(0, { timeout: 20_000 });
  const catalogRequestsBeforeRevisit = requests.filter(r => r.url.includes('/catalog')).length;
  await openCapabilityTab(page, 'Agents');
  await openCapabilityTab(page, 'Tools');
  await expect(page.getByRole('switch', { name: 'Gateway tools' })).toBeVisible();
  expect(requests.filter(r => r.url.includes('/catalog')).length,
    'Tools tab revisit must reuse the client catalog cache').toBe(catalogRequestsBeforeRevisit);

  await page.getByRole('button', { name: 'Refresh gateway tools' }).click();
  await expect(page.getByText('Loading tools...')).toHaveCount(0, { timeout: 20_000 });
  expect(requests.some(r => r.url.includes('/catalog?refresh=1')),
    'manual Refresh must bypass both client and server caches').toBeTruthy();
});


test('NFR: Settings remains semantic, keyboard closable, and contained at narrow width', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 390, height: 844 });
  await stagingLogin(page);
  await openSettingsShell(page);

  const dialog = page.getByRole('dialog', { name: 'Settings' });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  expect(box).not.toBeNull();
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, 'Settings must not create horizontal document overflow').toBeLessThanOrEqual(1);

  await openCapabilityTab(page, 'Hooks');
  await expect(page.getByLabel('Search hooks')).toBeVisible({ timeout: 2_000 });
  await expect(page.getByRole('switch', { name: 'Built-in hooks' })).toHaveAttribute('aria-checked', 'false');
  await expect(page.getByRole('button', { name: 'Create hook' })).toBeVisible({ timeout: 2_000 });

  await page.getByRole('button', { name: 'Create hook' }).click();
  await expect(page.getByRole('region', { name: 'Hook editor' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create hook' })).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden({ timeout: 2_000 });
});
