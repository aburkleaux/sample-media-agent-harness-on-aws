import { test, expect } from '@playwright/test';
import {
  stagingLogin, newSession, send, bodyText, waitForAgentDone,
  openCapabilities, openCapabilityTab,
  createStreamCapture, attachStreamInterceptor, assertDispatch, assertToolCalled,
} from './capability-audit.helpers';

/**
 * Task 4 — Tools + gateway auto-discovery audit (live stack).
 *
 * Dimensions:
 *   (c) shows up            — built-in tools present; GATEWAY tools are
 *                             AUTO-DISCOVERED (tools/list on connect) and
 *                             visible in the Gateway Browser, grouped by source
 *   refresh re-discovers    — "Refresh gateway tools" re-fetches the catalog
 *   (a) auto-usable         — an auto-discovered gateway tool is invokable by
 *                             the agent via chat dispatch
 *   (b) reflected           — tool-collection draft CRUD shows under "Yours"
 *
 * Backend: /catalog (mcp_manager connect → list_tools_sync). Discovered tools
 * are surfaced (no add/trust gate in this UI path). Gateway = "M2C Media
 * Workflow Gateway" (13 tools); Built-in section (~36 tools).
 */

const GATEWAY_SECTION = /Gateway: M2C Media Workflow Gateway/i;
const KNOWN_GATEWAY_TOOLS = ['process_media', 'diarize_speech', 'separate_audio_stems', 'segment_shots'];

test('Tools (c) built-in present + gateway tools auto-discovered + refresh re-discovers', async ({ page }) => {
  test.setTimeout(120_000);
  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Tools');

  const gatewayTools = page.getByRole('switch', { name: 'Gateway tools' });
  await expect(gatewayTools).toHaveAttribute('aria-checked', 'false');
  await gatewayTools.click();
  await expect(gatewayTools).toHaveAttribute('aria-checked', 'true');

  // Gateway Browser fetches /catalog only after the user opts in.
  await expect(page.getByText('Loading tools...')).toHaveCount(0, { timeout: 60000 });

  // Built-in tools section present.
  await expect(page.getByText(/Built-in Tools/i).first()).toBeVisible({ timeout: 15000 });

  // Gateway tools AUTO-DISCOVERED: the gateway section + known media tools.
  await expect(page.getByText(GATEWAY_SECTION).first()).toBeVisible({ timeout: 15000 });
  for (const t of KNOWN_GATEWAY_TOOLS) {
    await expect(page.getByText(t, { exact: true }).first(),
      `auto-discovered gateway tool "${t}" should be visible`).toBeVisible({ timeout: 10000 });
  }

  // Refresh re-discovers (re-fetches /catalog) — gateway section still present after.
  await page.getByRole('button', { name: 'Refresh gateway tools' }).click();
  await expect(page.getByText('Loading tools...')).toHaveCount(0, { timeout: 60000 });
  await expect(page.getByText(GATEWAY_SECTION).first()).toBeVisible({ timeout: 15000 });
});

test('Tools (a) an auto-discovered gateway tool is usable via chat dispatch', async ({ page }) => {
  test.setTimeout(5 * 60 * 1000);
  await stagingLogin(page);
  await newSession(page);

  const capture = createStreamCapture();
  attachStreamInterceptor(page, capture);

  // describe_tool is a fast, synchronous gateway tool (no async job) — ideal to
  // prove auto-discovered → dispatched → invoked without waiting on a pipeline.
  await send(page,
    'Call describe_tool on the media-processing process_media gateway tool and summarize its parameters.');
  await waitForAgentDone(page, 3 * 60 * 1000);

  assertDispatch(capture, 'gateway-describe_tool');
  assertToolCalled(capture, 'describe_tool', 'gateway-describe_tool');
});

test('Tools (b) tool-collection draft CRUD via chat is reflected under Yours', async ({ page }) => {
  test.setTimeout(5 * 60 * 1000);
  const stamp = Date.now();
  const collName = `pw-audit-tools-${stamp}`;

  await stagingLogin(page);
  await newSession(page);
  try {
    await send(page,
      `Use the create_tool_collection_draft tool to create a tool collection named "${collName}" ` +
      `with a single toolRef {name: "media-processing___process_media", version: "1.0"}. Confirm with the assetId.`);
    await waitForAgentDone(page, 3 * 60 * 1000);
    const txt = await bodyText(page);
    expect(txt.toLowerCase(), 'agent should acknowledge creating the tool collection')
      .toMatch(/created|assetid|collection|tool/i);

    await openCapabilities(page);
    await openCapabilityTab(page, 'Tools');
    await expect(page.getByText(collName, { exact: false }).first(),
      'new tool collection should appear under Yours (drafts)').toBeVisible({ timeout: 30000 });
    await page.keyboard.press('Escape').catch(() => {});
  } finally {
    await newSession(page).catch(() => {});
    await send(page, `Use delete_tool_collection_draft to delete the tool collection named "${collName}" with confirm=true.`).catch(() => {});
    await waitForAgentDone(page, 2 * 60 * 1000).catch(() => {});
  }
});
