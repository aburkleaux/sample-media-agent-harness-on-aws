// @vitest-environment jsdom
//
// Descriptions on the inventory rows — the reason the DTO grew the field.
//
// The Skills surface listed a name, a state chip and a row of buttons, so a list of
// ten skills said what each one was CALLED and nothing about what it was FOR. The
// built-in table beneath it had shown descriptions all along, which is what made the
// gap obvious. Asserted at the LIST level rather than on the row primitive because
// the failure was never in the primitive: nothing passed a description INTO it, and
// nothing carried one from the server, so every layer could look correct alone.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../LabContext', () => ({ useLabContext: () => ({}) }));
vi.mock('../../lab-api', () => ({
  deleteSkillDraft: vi.fn(), deleteSpecialtyAgentDraft: vi.fn(), deleteToolCollectionDraft: vi.fn(),
  editAsset: vi.fn(), publishAsset: vi.fn(),
}));
vi.mock('../GatewayBrowser', () => ({ default: () => <div /> }));
vi.mock('../BuiltInSkillsBrowser', () => ({ default: () => <div /> }));
vi.mock('./HooksPanel', () => ({ default: () => <div /> }));
vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => true }));

import { SkillsTabBody } from './CapabilitiesSettings';

const DRAFT_DESCRIPTION = 'Turn a script into a shot-by-shot storyboard with generated frames.';
const PUBLISHED_DESCRIPTION = 'Screen a cut for brand-safety issues before delivery.';

const DRAFTS = [
  {
    assetId: 'skill-1',
    name: 'ai-storyboarding',
    description: DRAFT_DESCRIPTION,
    category: 'skill' as const,
    status: 'active' as const,
    createdAt: 1,
    updatedAt: 1,
    s3Prefix: '',
    skillMd: '',
    supportingFiles: ['SKILL.md'],
  },
  // No description at all — the Registry-only / pre-field case.
  {
    assetId: 'skill-2',
    name: 'undescribed',
    category: 'skill' as const,
    status: 'active' as const,
    createdAt: 1,
    updatedAt: 1,
    s3Prefix: '',
    skillMd: '',
    supportingFiles: ['SKILL.md'],
  },
];

const PUBLISHED = [
  {
    assetId: 'skill-3',
    name: 'brand-safety-review',
    description: PUBLISHED_DESCRIPTION,
    version: 1,
    current: true,
    scope: 'private' as const,
    createdAt: 1,
    updatedAt: 1,
  },
];

function renderBody() {
  render(
    <SkillsTabBody
      createLabel="+ New skill"
      skills={DRAFTS}
      publishedSkills={PUBLISHED}
      search=""
      loading={false}
      onView={vi.fn()} onViewPublished={vi.fn()} onEdit={vi.fn()} onDelete={vi.fn()}
      onTest={vi.fn()} onPublish={vi.fn()} onForkPublished={vi.fn()} onPurge={vi.fn()}
    />,
  );
}

afterEach(cleanup);

describe('SkillsTabBody — descriptions', () => {
  it("shows a draft's description on its row", () => {
    renderBody();
    expect(screen.getByText(DRAFT_DESCRIPTION)).toBeInTheDocument();
  });

  it("shows a published version's description on its row", () => {
    // This one needed the server: `PublishedAssetSummary` had no description field,
    // so the PUBLISHED section could only ever render names however it was styled.
    renderBody();
    expect(screen.getByText(PUBLISHED_DESCRIPTION)).toBeInTheDocument();
  });

  it('renders no description line for a skill that has none', () => {
    // Absent ≠ empty: `?? ''` here would give the row an empty element that still
    // takes its margin, and would claim the author left the field blank.
    renderBody();
    const name = screen.getByText('undescribed');
    const card = name.closest('div')!.parentElement!;
    expect(card.textContent).toContain('undescribed');
    expect(card.textContent).not.toContain(DRAFT_DESCRIPTION);
  });

  it('keeps the draft lifecycle actions on the row beside the name', () => {
    // The description must not have displaced the buttons: they share the name line
    // precisely so the description can span the full card width.
    renderBody();
    const name = screen.getByText('ai-storyboarding');
    const nameLine = name.parentElement!;
    for (const label of ['View ai-storyboarding', 'Edit ai-storyboarding', 'Test ai-storyboarding', 'Publish ai-storyboarding', 'Delete ai-storyboarding']) {
      expect(nameLine.contains(screen.getByRole('button', { name: label }))).toBe(true);
    }
    expect(nameLine.contains(screen.getByText(DRAFT_DESCRIPTION))).toBe(false);
  });

  it('keeps Test, Edit and Delete on a published row', () => {
    renderBody();
    for (const label of ['View brand-safety-review', 'Test brand-safety-review', 'Edit brand-safety-review', 'Delete brand-safety-review']) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument();
    }
    // A published version is immutable — no Publish on it.
    expect(screen.queryByRole('button', { name: 'Publish brand-safety-review' })).toBeNull();
  });
});
