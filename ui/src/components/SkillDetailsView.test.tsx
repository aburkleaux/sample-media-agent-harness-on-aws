// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ getSkill: vi.fn() }));
// The mock is an explicit allowlist — a named export the component imports but
// this object omits fails as "No <name> export is defined on the mock".
const labApi = vi.hoisted(() => ({ readSkillDraft: vi.fn(), readPublishedSkill: vi.fn() }));

// A built-in skill is read from the control-plane catalog first, with `getSkill` (the
// agent runtime) as the fallback for skills the RUNTIME loaded and a deploy-time catalog
// therefore cannot know. Defaulting this to `null` means the existing built-in tests below
// keep exercising that fallback, which is the path their `getSkill` mocks describe.
const capabilityApi = vi.hoisted(() => ({ fetchBuiltInSkill: vi.fn() }));

vi.mock('../api', () => api);
vi.mock('../lab-api', () => labApi);
vi.mock('../capability-api', () => capabilityApi);

import SkillDetailsView from './SkillDetailsView';

const SKILL_MD = `---
name: rendered-skill
description: A rendered Skill used for preview testing.
allowed-tools: read_file shell
---

# Rendered workflow

Use **carefully** and read [the guide](references/guide.md).

| Input | Output |
|---|---|
| file | report |

<script>window.evil = true</script>
`;

beforeEach(() => {
  vi.clearAllMocks();
  // Catalog miss by default → the runtime fallback, which is what the built-in tests
  // below assert against. The catalog-hit path is asserted in its own describe block.
  capabilityApi.fetchBuiltInSkill.mockResolvedValue(null);
  labApi.readSkillDraft.mockResolvedValue({
    assetId: 'skill-1',
    name: 'rendered-skill',
    skillMd: SKILL_MD,
    supportingFiles: ['references/guide.md', 'assets/schema.json'],
  });
  labApi.readPublishedSkill.mockResolvedValue({
    assetId: 'skill-1',
    name: 'rendered-skill',
    skillMd: SKILL_MD,
    supportingFiles: ['references/guide.md'],
  });
  api.getSkill.mockResolvedValue({
    name: 'rendered-skill',
    description: 'A rendered Skill used for preview testing.',
    instructions: '# Rendered workflow',
    skillMd: SKILL_MD,
    supportingFiles: ['references/guide.md'],
    tools: [],
    source: 'local',
  });
});

afterEach(cleanup);

describe('SkillDetailsView', () => {
  it('renders only the Markdown body and keeps raw HTML inert', async () => {
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={vi.fn()} onEdit={vi.fn()} />);

    const previewTab = await screen.findByRole('tab', { name: 'Preview' });
    const previewPanel = screen.getByRole('tabpanel', { name: 'Preview' });
    expect(previewTab.getAttribute('aria-controls')).toBe(previewPanel.id);
    expect(previewPanel.getAttribute('aria-labelledby')).toBe(previewTab.id);
    expect(await screen.findByRole('heading', { name: 'Rendered workflow' })).toBeTruthy();
    expect(screen.getByText('Input')).toBeTruthy();
    expect(screen.getByText('report')).toBeTruthy();
    expect(screen.queryByText('name: rendered-skill')).toBeNull();
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText('2 allowed tools')).toBeTruthy();
  });

  it('shows full source and the package inventory without rendering supporting Markdown', async () => {
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={vi.fn()} />);
    await screen.findByText('A rendered Skill used for preview testing.');

    fireEvent.click(screen.getByRole('tab', { name: 'Source' }));
    expect(screen.getByText(/name: rendered-skill/)).toBeTruthy();

    fireEvent.click(screen.getByRole('tab', { name: 'Package (3)' }));
    expect(screen.getByText('references/guide.md')).toBeTruthy();
    expect(screen.getByText('assets/schema.json')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Guide' })).toBeNull();
  });

  it('routes relative reference links into the package view', async () => {
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={vi.fn()} />);
    const guide = await screen.findByRole('link', { name: 'the guide' });
    fireEvent.click(guide);

    expect(screen.getByRole('tab', { name: 'Package (3)' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByText('references/guide.md')).toBeTruthy();
  });

  it('loads a published version through readPublishedSkill, not the draft read', async () => {
    // The draft read is scoped to the OWNED partition, which publishing empties,
    // so reaching for it here is the bug this pins.
    render(<SkillDetailsView target={{ kind: 'published', assetId: 'skill-1', version: 2 }} onBack={vi.fn()} />);

    await waitFor(() => expect(labApi.readPublishedSkill).toHaveBeenCalledWith('skill-1', 2));
    expect(labApi.readSkillDraft).not.toHaveBeenCalled();
    expect(await screen.findByText('Published v2 · read only')).toBeTruthy();
    // Same viewer as drafts and built-ins: rendered body plus the file list.
    expect(await screen.findByRole('heading', { name: 'Rendered workflow' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Package (2)' })).toBeTruthy();
  });

  it('labels the published Edit as a fork, and omits the version when unspecified', async () => {
    const onEdit = vi.fn();
    render(<SkillDetailsView target={{ kind: 'published', assetId: 'skill-1' }} onBack={vi.fn()} onEdit={onEdit} />);

    await waitFor(() => expect(labApi.readPublishedSkill).toHaveBeenCalledWith('skill-1', undefined));
    expect(await screen.findByText('Published · read only')).toBeTruthy();

    // "Edit SKILL.md" would imply editing the immutable version in place.
    expect(screen.queryByRole('button', { name: 'Edit SKILL.md' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(onEdit).toHaveBeenCalledWith('skill-1');
  });

  it('groups Back with Edit in the header rather than a text link above the title', async () => {
    const onBack = vi.fn();
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={onBack} onEdit={vi.fn()} />);
    await screen.findByText('A rendered Skill used for preview testing.');

    // The old chrome rendered "← Back to Skills" as a bare link above the title.
    expect(screen.queryByRole('button', { name: '← Back to Skills' })).toBeNull();

    const back = screen.getByRole('button', { name: 'Back' });
    const edit = screen.getByRole('button', { name: 'Edit SKILL.md' });
    expect(back.parentElement).toBe(edit.parentElement);

    fireEvent.click(back);
    expect(onBack).toHaveBeenCalled();
  });

  it('scrolls the body rather than letting a long SKILL.md run off the pane', async () => {
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={vi.fn()} />);
    await screen.findByText('A rendered Skill used for preview testing.');

    const scroller = screen.getByTestId('skill-detail-scroll');
    expect(scroller.style.overflowY).toBe('auto');
    // `minHeight: 0` is the load-bearing half — without it the flex child never
    // shrinks below its content and `overflowY` does nothing.
    expect(scroller.style.minHeight).toBe('0px');
    // The header must NOT scroll away with the content.
    expect(scroller.contains(screen.getByRole('button', { name: 'Back' }))).toBe(false);
  });

  it('does not let the content panel shrink and clip a long SKILL.md', async () => {
    // The body is a flex column, and a flex item whose `overflow` is not
    // `visible` gets an automatic minimum size of ZERO — so the panel shrank to
    // the body height and clipped instead of overflowing, leaving nothing to
    // scroll. Short skills fit inside the body and hid the bug entirely, which
    // is why this asserts the style contract rather than a rendered height
    // (jsdom does no layout).
    render(<SkillDetailsView target={{ kind: 'builtin', name: 'rendered-skill' }} onBack={vi.fn()} />);
    await screen.findByText('A rendered Skill used for preview testing.');

    // Only the active panel is mounted. Queried by role alone: the section sets
    // both `aria-labelledby` and `aria-label`, and labelledby wins, so its
    // accessible name is the tab's text rather than the aria-label.
    const panel = screen.getByRole('tabpanel');
    expect(panel.style.flexShrink).toBe('0');
    expect(panel.style.overflow).toBe('hidden');

    const scroller = screen.getByTestId('skill-detail-scroll');
    expect(scroller.style.display).toBe('flex');
    expect(scroller.style.overflowY).toBe('auto');
  });

  it('offers Back even while loading and when the Skill is missing', async () => {
    labApi.readSkillDraft.mockRejectedValueOnce(new Error('boom'));
    render(<SkillDetailsView target={{ kind: 'draft', assetId: 'skill-1' }} onBack={vi.fn()} />);

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Back' })).toBeTruthy();
  });

  it('loads bundled Skills through getSkill', async () => {
    render(<SkillDetailsView target={{ kind: 'builtin', name: 'rendered-skill' }} onBack={vi.fn()} />);
    await waitFor(() => expect(api.getSkill).toHaveBeenCalledWith('rendered-skill'));
    expect(await screen.findByText('Built-in')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit SKILL.md' })).toBeNull();
  });
});

/*
 * Where a built-in skill's package comes from.
 *
 * This used to be `get_skill` on the agent runtime, uncached, so every open paid a cold
 * start — the second open of a skill was as slow as the first. A shipped skill now comes
 * from the deploy-time catalog; only a runtime-LOADED skill still needs the runtime, and
 * it must keep working, because the same panel lists both.
 */
describe('SkillDetailsView built-in source', () => {
  const PACKAGE = {
    name: 'rendered-skill',
    description: 'From the catalog.',
    skillMd: SKILL_MD,
    supportingFiles: ['references/guide.md'],
  };

  it('reads a shipped skill from the catalog and never wakes the runtime', async () => {
    capabilityApi.fetchBuiltInSkill.mockResolvedValue(PACKAGE);

    render(<SkillDetailsView target={{ kind: 'builtin', name: 'rendered-skill' }} onBack={() => {}} />);

    expect(await screen.findByText('Rendered workflow')).toBeTruthy();
    expect(capabilityApi.fetchBuiltInSkill).toHaveBeenCalledWith('rendered-skill');
    // The assertion the change exists for.
    expect(api.getSkill).not.toHaveBeenCalled();
    // Package count is supportingFiles + SKILL.md itself.
    expect(screen.getByRole('tab', { name: 'Package (2)' })).toBeTruthy();
  });

  it('falls back to the runtime for a skill the catalog cannot know', async () => {
    // An S3- or memory-loaded skill is listed in this panel but is absent from a catalog
    // written at deploy time. Without the fallback, opening one would fail outright.
    capabilityApi.fetchBuiltInSkill.mockResolvedValue(null);
    api.getSkill.mockResolvedValue({
      name: 'pulled-from-s3',
      description: 'Loaded at runtime.',
      instructions: '',
      skillMd: SKILL_MD,
      supportingFiles: [],
      tools: [],
      source: 's3',
    });

    render(<SkillDetailsView target={{ kind: 'builtin', name: 'pulled-from-s3' }} onBack={() => {}} />);

    expect(await screen.findByText('Loaded from S3')).toBeTruthy();
    expect(api.getSkill).toHaveBeenCalledWith('pulled-from-s3');
  });

  it('surfaces a catalog outage rather than silently falling back', async () => {
    // A 503 means the catalog is not deployed. Falling back would hide that behind a
    // slow-but-working view, so the failure has to reach the user.
    capabilityApi.fetchBuiltInSkill.mockRejectedValue(new Error('built-in skill failed (503)'));

    render(<SkillDetailsView target={{ kind: 'builtin', name: 'rendered-skill' }} onBack={() => {}} />);

    // Plain textContent: this file has no `@testing-library/jest-dom` import, so the
    // `toHaveTextContent` matcher is not registered here.
    expect((await screen.findByRole('alert')).textContent).toContain('built-in skill failed (503)');
    expect(api.getSkill).not.toHaveBeenCalled();
  });
});
