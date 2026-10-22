import { test, expect } from '@playwright/test';
import path from 'node:path';
import {
  stagingLogin, newSession, send, bodyText, waitForAgentDone, labActionInPage,
  openCapabilities, openCapabilityTab,
} from './capability-audit.helpers';

/**
 * Task 3 — Skills capability audit (live stack).
 *
 * Dimensions:
 *   (a) works     — an owned draft skill AUTO-LOADS and is usable in a chat
 *                   turn (load_owned_drafts_into_skills_manager)
 *   (b) reflected — create a draft (via the main-agent lab tool, i.e. chat)
 *                   and it appears under "Yours (drafts)"; delete removes it
 *   (c) shows up  — the built-in browser lists the bundled filesystem skills
 *
 * Backend: built-in list via `listSkills()` (SkillsManager.list_skills);
 * draft CRUD via `create_skill_draft` / `delete_skill_draft` @tools on the
 * main agent (main_lab_tools.py), which also auto-loads owned drafts per turn.
 */

// 8 bundled filesystem skills on disk (agent/skills/).
const EXPECTED_BUILTIN = [
  'skill-creator', 'capability-editor', 'pdf',
  'render-video', 'render-audio', 'render-document',
  'video-processing', 'aws-research',
];

test('Skills (c) built-in browser lists the bundled filesystem skills', async ({ page }) => {
  test.setTimeout(120_000);
  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Skills');

  // Built-in inventories are intentionally opt-in so authored drafts stay primary.
  const builtInSkills = page.getByRole('switch', { name: 'Built-in skills' });
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'false');
  await builtInSkills.click();
  await expect(builtInSkills).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByText(/Bundled \(filesystem\)/i).first()).toBeVisible({ timeout: 60000 });

  // Spot-check a few representative bundled skills are listed.
  for (const name of ['video-processing', 'render-document', 'pdf', 'skill-creator', 'capability-editor']) {
    await expect(page.getByText(name, { exact: true }).first(),
      `built-in skill "${name}" should be listed`).toBeVisible({ timeout: 10000 });
  }

  await page.getByRole('button', { name: 'View video-processing' }).click();
  await expect(page.getByRole('region', { name: 'Skill detail view' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Video & Audio Processing' })).toBeVisible({ timeout: 60_000 });
  await page.getByRole('tab', { name: /Package/ }).click();
  await expect(page.getByText('references/face_recognition_agent.md', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '← Back to Skills' }).click();
});

test('Skills (a)(b) create a draft via chat → appears under Yours → auto-loads → delete', async ({ page }) => {
  test.setTimeout(6 * 60 * 1000);
  const stamp = Date.now();
  const skillName = `pw-audit-skill-${stamp}`;

  await stagingLogin(page);
  await newSession(page);

  try {
    // (b) create via the main-agent lab tool (chat-driven CRUD — also the
    // Phase-3 "create from chat" path).
    await send(page,
      `Use the create_skill_draft tool to create a skill draft named "${skillName}". ` +
      `The SKILL.md body should be: "Skill ${skillName}: a benign audit marker skill for ` +
      `verifying dynamic skill auto-load. It performs no action." Confirm with the returned assetId.`);
    await waitForAgentDone(page, 3 * 60 * 1000);
    const createTxt = await bodyText(page);
    expect(createTxt.toLowerCase(), 'agent should acknowledge creating the draft')
      .toMatch(/created|assetid|draft|skill/i);

    // (b) reflected: the new draft appears under "Yours (drafts)" in the Skills tab.
    await openCapabilities(page);
    await openCapabilityTab(page, 'Skills');
    await expect(page.getByText(skillName, { exact: false }).first(),
      'new draft should appear under Yours (drafts)').toBeVisible({ timeout: 30000 });
    await page.keyboard.press('Escape').catch(() => {});
    await page.waitForTimeout(500);

    // (a) auto-load usable: in a FRESH session the owned draft is loaded into
    // the agent's skills and is discoverable. We ask it to enumerate its
    // available skills (benign — avoids the prompt-injection defense that
    // (correctly) trips on "respond with token X" style skill bodies).
    await newSession(page);
    await send(page,
      `List the names of every skill you currently have available to you (including any of my own draft skills). ` +
      `Just list the names.`);
    await waitForAgentDone(page, 3 * 60 * 1000);
    const useTxt = await bodyText(page);
    expect(useTxt, `the owned draft "${skillName}" should be available/loaded for the agent`)
      .toContain(skillName);
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


test('Skills UI imports a validated folder, renders SKILL.md, and keeps editor creation minimal', async ({ page }) => {
  test.setTimeout(3 * 60 * 1000);
  const skillName = `pw-ui-skill-${Date.now()}`;

  await stagingLogin(page);
  await openCapabilities(page);
  await openCapabilityTab(page, 'Skills');

  try {
    const folderInput = page.getByLabel('Choose Skill folder');
    await folderInput.setInputFiles(path.resolve('tests/fixtures/skill-package'));

    await expect(page.getByRole('region', { name: 'Import Skill package' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Uploaded Skill Preview' })).toBeVisible({ timeout: 60_000 });
    await page.getByLabel('Imported Skill name').fill(skillName);
    await expect(page.getByText(/SKILL.md name .* will be updated/)).toBeVisible();

    await page.getByRole('tab', { name: /Package/ }).click();
    await expect(page.getByText('references/guide.md', { exact: true })).toBeVisible();
    await page.getByRole('tab', { name: 'Preview' }).click();
    await page.getByRole('button', { name: 'Import Skill' }).click();

    await expect(page.getByRole('region', { name: 'Skill detail view' })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('heading', { name: 'Uploaded Skill Preview' })).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText('2 files')).toBeVisible();
    await page.getByRole('tab', { name: 'Source' }).click();
    await expect(page.getByText(new RegExp(`name: ${skillName}`))).toBeVisible();
    await page.getByRole('button', { name: '← Back to Skills' }).click();

    await page.getByRole('button', { name: '+ Create', exact: true }).click();
    await expect(page.getByText('New Skill', { exact: true })).toBeVisible();
    await expect(page.getByText(/Import a folder to create a Skill with references/)).toBeVisible();
    await expect(page.getByRole('button', { name: '+ Add file' })).toHaveCount(0);
  } finally {
    const drafts = await labActionInPage<Array<{ assetId: string; name: string }>>(page, 'ListSkillDrafts');
    for (const draft of drafts.filter(item => item.name === skillName)) {
      await labActionInPage(page, 'DeleteSkillDraft', { assetId: draft.assetId });
    }
  }
});
