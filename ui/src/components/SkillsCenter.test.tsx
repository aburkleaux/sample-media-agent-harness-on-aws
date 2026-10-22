// @vitest-environment jsdom
//
// The Skills centre — promoted out of Settings → Capabilities.
//
// The first four cases are ported from `settings/CapabilitiesSettings.test.tsx`
// unchanged in intent: they cover the three things that lived ONLY in the
// settings tab (built-in browser, folder import, skill detail view) plus the
// nested harness. Moving a surface without moving its tests is how a feature
// quietly disappears — the Agents consolidation left that warning in
// `WorkspaceView.tsx` and this is the same move.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const context = vi.hoisted(() => ({
  skills: [
    {
      assetId: 'skill-1', name: 'audit-skill', category: 'skill', status: 'archived',
      createdAt: 1, updatedAt: 1, s3Prefix: '', skillMd: '', supportingFiles: [],
    },
  ],
  // Typed loosely so a test can install published fixtures; the real shape is
  // `PublishedAssetSummary[]` and is exercised by the component under test.
  publishedSkills: [] as any[],
  loadingCategories: { skill: false, 'specialty-agent': false, 'tool-collection': false, workflow: false, hook: false },
  loadedCategories: { skill: true, 'specialty-agent': true, 'tool-collection': true, workflow: false, hook: false },
  categoryErrors: { skill: null, 'specialty-agent': null, 'tool-collection': null, workflow: null, hook: null } as Record<string, string | null>,
  toolCollections: [],
  ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
  refreshCategory: vi.fn().mockResolvedValue(undefined),
  setEditingTarget: vi.fn(),
  optimisticRemoveSkill: vi.fn(),
  optimisticRemoveToolCollection: vi.fn(),
  optimisticRemoveSpecialtyAgent: vi.fn(),
  setHarnessTarget: vi.fn(),
}));

const api = vi.hoisted(() => ({
  deleteSkillDraft: vi.fn().mockResolvedValue(undefined),
  editAsset: vi.fn().mockResolvedValue({ assetId: 'wc-1' }),
  publishAsset: vi.fn().mockResolvedValue({ assetId: 'skill-1', version: 1 }),
}));

const purgeApi = vi.hoisted(() => ({
  previewPurge: vi.fn(),
  purgeAsset: vi.fn(),
}));

vi.mock('../LabContext', () => ({ useLabContext: () => context }));
vi.mock('../lab-api', () => ({
  deleteSkillDraft: api.deleteSkillDraft,
  editAsset: api.editAsset,
  publishAsset: api.publishAsset,
  deleteSpecialtyAgentDraft: vi.fn(),
  deleteToolCollectionDraft: vi.fn(),
}));
vi.mock('./BuiltInSkillsBrowser', () => ({
  default: () => <div data-testid="built-in-skills">Built-in Skills</div>,
}));
/*
 * Renders a back control and a Run, because the real panel does — it owns both now,
 * on one toolbar. A stub without them would let a host drop its own back control and
 * still pass, leaving the user with no way out of the harness.
 */
vi.mock('./TestHarnessPanel', () => ({
  default: ({ onClose, backLabel = '\u2190 Back' }: { onClose: () => void; backLabel?: string }) => (
    <div data-testid="test-harness">
      Test Harness
      <button onClick={onClose}>{backLabel}</button>
      <button>Run</button>
    </div>
  ),
}));
vi.mock('./SkillDetailsView', () => ({
  default: ({ target, onBack, onEdit }: any) => (
    <div data-testid="skill-detail">
      <button onClick={onBack}>Back from detail</button>
      {/* Exposes onEdit so the caller's draft-vs-published routing is testable:
          a published target must fork, not open the editor on the lineage id. */}
      {onEdit && <button onClick={() => onEdit(target.assetId)}>Detail edit</button>}
      {target.kind}:{target.assetId ?? target.name}
      {target.version !== undefined && `:v${target.version}`}
    </div>
  ),
}));
vi.mock('./SkillPackageImport', () => ({
  default: ({ onCancel }: any) => (
    <div data-testid="skill-import">
      <button onClick={onCancel}>Cancel import</button>
    </div>
  ),
}));
// The real reader walks a FileList; the component under test only needs to know
// a candidate came back.
vi.mock('../utils/skillPackage', () => ({
  readSkillPackageFolder: vi.fn().mockResolvedValue({ name: 'imported-skill', files: [] }),
}));
vi.mock('../../auth/isGatewayAdmin', () => ({ useIsGatewayAdmin: () => false }));
// `CapabilityApiError` is mocked as a real class: the dialog branches on
// `instanceof` to turn a typed code into advice ("retry to finish").
vi.mock('../capability-api', () => ({
  previewPurge: purgeApi.previewPurge,
  purgeAsset: purgeApi.purgeAsset,
  CapabilityApiError: class CapabilityApiError extends Error {
    constructor(readonly status: number, message: string, readonly code?: string) {
      super(message);
      this.name = 'CapabilityApiError';
    }
  },
}));

import SkillsCenter from './SkillsCenter';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const renderCenter = (onOpenEditor = vi.fn()) => {
  render(<SkillsCenter onOpenEditor={onOpenEditor} />);
  return onOpenEditor;
};

// --- Ported from the settings tab -----------------------------------------

describe('features that existed ONLY in Settings', () => {
  it('defaults built-in Skills off and reveals them only on request', () => {
    renderCenter();
    const toggle = screen.getByRole('switch', { name: 'Built-in skills' });
    expect(toggle.getAttribute('aria-checked')).toBe('false');
    expect(screen.queryByTestId('built-in-skills')).toBeNull();

    fireEvent.click(toggle);
    expect(screen.getByTestId('built-in-skills')).toBeTruthy();
  });

  it('does not offer folder import here — it is a create action', () => {
    // The importer used to sit in this header beside Search, as though it were a
    // way to VIEW the list. It is the second way to CREATE a skill, so it moved
    // onto the create path in `SkillEditor`. Asserted as an absence so it cannot
    // quietly come back and give the surface two create entry points again.
    renderCenter();
    expect(screen.queryByRole('button', { name: 'Upload folder' })).toBeNull();
    expect(screen.queryByLabelText('Choose Skill folder')).toBeNull();
  });

  it('opens a custom Skill in the nested read-first detail view', () => {
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'View audit-skill' }));
    expect(screen.getByTestId('skill-detail').textContent).toContain('draft:skill-1');
    // The detail view REPLACES the INVENTORY rather than stacking on it. Anchored
    // on the search box: it is still on this surface, so its absence genuinely
    // proves replacement. The surface header is a separate matter — see below.
    expect(screen.queryByLabelText('Search skills')).toBeNull();
  });

  // The header — heading, description, and the rule under them — stays put when you
  // drill in. It used to be inside the branch that the detail and harness views
  // replaced, so opening a skill took it off screen and the detail ran to the very
  // top of the pane, reading as a page navigation when none had happened.
  it.each([
    ['the detail view', 'View audit-skill'],
    ['the nested harness', 'Test audit-skill'],
  ])('keeps the Skills header while %s is open', (_label, action) => {
    renderCenter();
    expect(screen.getByRole('heading', { name: 'Skills' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: action }));

    expect(screen.getByRole('heading', { name: 'Skills' })).toBeTruthy();
    expect(screen.getByText(/Capabilities an agent can load/)).toBeTruthy();
  });

  it('renders the Skills heading exactly once when drilled in', () => {
    // The header is now assembled once and reused across branches. Building it per
    // branch instead would be the obvious refactor and would double it here.
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'View audit-skill' }));
    expect(screen.getAllByRole('heading', { name: 'Skills' })).toHaveLength(1);
  });

  it('replaces the Skills inventory with a contextual nested Harness view', () => {
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'Test audit-skill' }));

    expect(context.setHarnessTarget).toHaveBeenCalledWith({ category: 'skill', assetId: 'skill-1' });
    expect(screen.getByRole('region', { name: 'Test Harness view' })).toBeTruthy();
    // One way out, supplied by the panel beside Run. The surface used to add its own
    // "← Back to Skills" above a panel that already had a back control and a ✕.
    expect(screen.getByRole('button', { name: '← Back' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Back to Skills/ })).toBeNull();
    expect(screen.getByTestId('test-harness')).toBeTruthy();
    expect(screen.queryByLabelText('Search skills')).toBeNull();
  });
});

// --- New behaviour in the promoted surface ---------------------------------

describe('SkillsCenter', () => {
  it('routes edit through onOpenEditor', () => {
    const onOpenEditor = renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'Edit audit-skill' }));
    expect(onOpenEditor).toHaveBeenCalledWith({ category: 'skill', assetId: 'skill-1' });
  });

  it('has no header create button — the sidebar action is the one', () => {
    // Two identical primary buttons a glance apart (sidebar "New skill" + a
    // header one) was the duplication reported on the Agents tab. Removed here
    // too rather than leaving the surface inconsistent.
    renderCenter();
    expect(screen.queryByRole('button', { name: '+ New skill' })).toBeNull();
  });

  it('deletes a draft optimistically', async () => {
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'Delete audit-skill' }));
    // ListRow's delete is a two-step confirm, same as everywhere else.
    const confirm = screen.queryByRole('button', { name: /Confirm delete/i });
    if (confirm) fireEvent.click(confirm);

    await waitFor(() => expect(api.deleteSkillDraft).toHaveBeenCalledWith('skill-1'));
    expect(context.optimisticRemoveSkill).toHaveBeenCalledWith('skill-1');
  });

  it('publishes then refreshes so the row moves state', async () => {
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: 'Publish audit-skill' }));
    await waitFor(() => expect(api.publishAsset).toHaveBeenCalledWith('skill', 'skill-1'));
    await waitFor(() => expect(context.refreshCategory).toHaveBeenCalledWith('skill'));
  });

  it('empty state names the sidebar create action instead of adding a button', () => {
    // The surface has exactly ONE create affordance: the sidebar "New skill"
    // action. It previously had two more at different times — a header button
    // (removed, see above) and a button inside this empty state (removed here) —
    // and both read as duplicates of the sidebar one a glance away.
    //
    // So the empty state NAMES the affordance rather than being one. The copy
    // must quote the real label: `EmptyState` defaults to `+ Create`, a button
    // this surface has never had, which is the bug this assertion pins.
    const original = context.skills;
    context.skills = [];
    try {
      renderCenter();
      expect(screen.getByText(/No skills yet/)).toBeTruthy();
      expect(screen.queryByRole('button', { name: '+ New skill' })).toBeNull();
      expect(screen.getByText(/Click "\+ New skill" to add one/)).toBeTruthy();
      expect(screen.queryByText(/\+ Create/)).toBeNull();
    } finally {
      context.skills = original;
    }
  });

  it('scopes the inventory to the picked All / Drafts / Published state', () => {
    renderCenter();
    // Names carry the count ("All 1"), so match on the label prefix.
    const tab = (label: string) => screen.getByRole('button', { name: new RegExp(`^${label}`) });

    // Default is All: the drafts section is listed.
    expect(tab('All')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Edit audit-skill' })).toBeTruthy();

    // Published: drafts go away. There are no published skills in this fixture,
    // so the section still renders and SAYS so — picking a filter must never
    // blank the pane without a reason.
    fireEvent.click(tab('Published'));
    expect(screen.queryByRole('button', { name: 'Edit audit-skill' })).toBeNull();
    expect(screen.getByText('No published skills yet.')).toBeTruthy();

    // Drafts: back to the draft, and no Published section at all.
    fireEvent.click(tab('Drafts'));
    expect(screen.getByRole('button', { name: 'Edit audit-skill' })).toBeTruthy();
    expect(screen.queryByText('No published skills yet.')).toBeNull();
  });

  it('opens a published version read-only from its View action', async () => {
    const originalPublished = context.publishedSkills;
    context.publishedSkills = [
      { assetId: 'skill-pub-1', name: 'shipped-skill', version: 2, current: true, scope: 'private', createdAt: 1, updatedAt: 2 },
    ] as any;
    try {
      const onOpenEditor = renderCenter();

      fireEvent.click(screen.getByRole('button', { name: 'View shipped-skill' }));

      // Published target, carrying the version off the row that was clicked.
      const detail = screen.getByTestId('skill-detail');
      expect(detail.textContent).toContain('published:skill-pub-1:v2');

      // Its Edit forks rather than opening the editor on the lineage id — that
      // read is draft-only and would render a blank form.
      fireEvent.click(screen.getByRole('button', { name: 'Detail edit' }));
      await waitFor(() => expect(api.editAsset).toHaveBeenCalledWith('skill', 'skill-pub-1'));
      await waitFor(() => expect(onOpenEditor).toHaveBeenCalledWith({ category: 'skill', assetId: 'wc-1' }));
    } finally {
      context.publishedSkills = originalPublished;
    }
  });

  it('puts the state badge beside the name, not in the action group', () => {
    const originalPublished = context.publishedSkills;
    context.publishedSkills = [
      { assetId: 'skill-pub-1', name: 'shipped-skill', version: 2, current: true, scope: 'private', createdAt: 1, updatedAt: 2 },
    ] as any;
    try {
      renderCenter();

      // Anchor on the name — "Published" also appears as a section header and a
      // filter tab. The badge must sit with the NAME rather than inside the action
      // cluster; both now share the name line (which is what lets the description
      // below span the full card), so the invariant is ORDER, not separate parents:
      // name, then badge, then the buttons pinned right.
      const publishedName = screen.getByText('shipped-skill');
      const publishedCluster = publishedName.parentElement!;
      const publishedBadge = screen.getAllByText('Published')
        .find(node => publishedCluster.contains(node))!;
      const firstPublishedButton = publishedCluster.querySelector('button')!;
      // Order, not adjacency: a muted qualifier (the version, `v2`) sits between the
      // name and the badge, so asserting the exact string would break on a change
      // that keeps the invariant this test is about.
      for (const [before, after] of [[publishedName, publishedBadge], [publishedBadge, firstPublishedButton]] as const) {
        expect(before.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      }
      // And the version is on this line rather than on a row of its own.
      expect(publishedCluster.textContent).toContain('v2');

      // Draft row gets the same treatment. Its badge reads Active or Draft
      // depending on status, so assert on either rather than on the fixture.
      const draftCluster = screen.getByText('audit-skill').parentElement!;
      expect(draftCluster.textContent).toMatch(/^audit-skill.*(Active|Draft)/);
    } finally {
      context.publishedSkills = originalPublished;
    }
  });

  it('scrolls the inventory body, header pinned', () => {
    renderCenter();

    const scroller = screen.getByTestId('skills-center-scroll');
    expect(scroller.style.overflowY).toBe('auto');
    expect(scroller.style.minHeight).toBe('0px');
    // Search stays put while the list scrolls.
    expect(scroller.contains(screen.getByLabelText('Search skills'))).toBe(false);
  });

  it('filters by name', () => {
    renderCenter();
    expect(screen.getByRole('button', { name: 'Edit audit-skill' })).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Search skills'), { target: { value: 'zzz' } });
    expect(screen.queryByRole('button', { name: 'Edit audit-skill' })).toBeNull();
  });

  it('surfaces a category load error with a retry', () => {
    const original = context.categoryErrors.skill;
    context.categoryErrors = { ...context.categoryErrors, skill: 'Registry unavailable' };
    try {
      renderCenter();
      expect(screen.getByRole('alert').textContent).toContain('Registry unavailable');
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
      expect(context.refreshCategory).toHaveBeenCalledWith('skill');
    } finally {
      context.categoryErrors = { ...context.categoryErrors, skill: original };
    }
  });

});

// --- Deleting a published skill -------------------------------------------
//
// The one irreversible action on this surface. It destroys every published
// version of the lineage, the working copy if one exists, and their S3 prefixes,
// so the tests are about the gates: a dry-run preview first, the name typed, and
// nothing lost from the screen if it fails.

describe('deleting a published skill', () => {
  const PUBLISHED = {
    assetId: 'skill-lineage-1',
    name: 'retired-skill',
    version: 2,
    current: true,
    scope: 'private' as const,
    createdAt: 1,
    updatedAt: 2,
  };

  const PREFLIGHT = {
    assetId: PUBLISHED.assetId,
    category: 'skill',
    versions: [1, 2],
    scopes: ['private'],
    workingCopyExists: false,
    externalAccessLive: false,
    prefixes: ['users/u-jane/published/skills/skill-lineage-1/v2/'],
    references: [],
    partial: [],
  };

  beforeEach(() => {
    context.publishedSkills = [PUBLISHED, { ...PUBLISHED, version: 1, current: false }];
    purgeApi.previewPurge.mockResolvedValue(PREFLIGHT);
    purgeApi.purgeAsset.mockResolvedValue({
      dryRun: false,
      assetId: PUBLISHED.assetId,
      category: 'skill',
      versionsDeleted: [2, 1],
      workingCopyDeleted: false,
      prefixesDeleted: PREFLIGHT.prefixes,
      prefixesOrphaned: [],
      outboxEvents: 0,
      preflight: PREFLIGHT,
    });
  });

  afterEach(() => {
    context.publishedSkills = [];
  });

  /** Opens the dialog and waits for the blast radius, which gates confirming. */
  const openDialog = async () => {
    renderCenter();
    fireEvent.click(screen.getByRole('button', { name: `Delete ${PUBLISHED.name}` }));
    const confirm = await screen.findByRole('button', { name: 'Delete permanently' });
    await screen.findByText(/2 published versions/);
    return confirm;
  };

  const typeTheName = () => fireEvent.change(
    screen.getByLabelText(`Type ${PUBLISHED.name} to confirm`),
    { target: { value: PUBLISHED.name } },
  );

  it('previews with a dry run, against the skill category, before destroying anything', async () => {
    await openDialog();

    await waitFor(() => expect(purgeApi.previewPurge).toHaveBeenCalled());
    // The category is what selects the reference scan server-side: pointed at
    // the wrong one, the blast radius comes back empty for a skill agents use.
    expect(purgeApi.previewPurge).toHaveBeenCalledWith('skill', PUBLISHED.assetId);
    expect(purgeApi.purgeAsset).not.toHaveBeenCalled();
  });

  it('lists the versions it will destroy, not just the one on the row', async () => {
    await openDialog();

    expect(await screen.findByText(/2 published versions/)).toBeTruthy();
  });

  it('keeps the confirm disabled until the exact name is typed', async () => {
    const confirm = await openDialog();

    // Blast radius rendered, but nothing typed yet.
    expect(confirm).toBeDisabled();

    const phrase = screen.getByLabelText(`Type ${PUBLISHED.name} to confirm`);
    fireEvent.change(phrase, { target: { value: 'retired' } });
    expect(confirm).toBeDisabled();

    fireEvent.change(phrase, { target: { value: PUBLISHED.name } });
    expect(confirm).not.toBeDisabled();
  });

  it('drops the row and refreshes once the purge succeeds', async () => {
    const confirm = await openDialog();
    typeTheName();
    fireEvent.click(confirm);

    await waitFor(() => expect(purgeApi.purgeAsset).toHaveBeenCalledWith('skill', PUBLISHED.assetId));
    // Tombstoned locally: `optimisticRemoveSkill` only filters DRAFTS, so
    // without this the row would sit there until the refresh landed.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: `Delete ${PUBLISHED.name}` })).toBeNull(),
    );
    expect(context.refreshCategory).toHaveBeenCalledWith('skill');
  });

  it('stays open with the error when the purge half-finishes', async () => {
    const { CapabilityApiError } = await import('../capability-api');
    purgeApi.purgeAsset.mockRejectedValueOnce(
      new (CapabilityApiError as any)(409, 'some versions remain', 'conflict'),
    );

    const confirm = await openDialog();
    typeTheName();
    fireEvent.click(confirm);

    // A 409 means "retry to finish", which is unactionable if the dialog and its
    // reference list have vanished.
    expect(await screen.findByText(/try again to finish/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Delete permanently' })).toBeTruthy();
  });

  it('does not offer Delete on a draft row, which has its own two-step confirm', () => {
    renderCenter();

    // `audit-skill` is the draft fixture; its Delete is the inline confirm, not
    // the purge dialog, and must not be routed here.
    fireEvent.click(screen.getByRole('button', { name: 'Delete audit-skill' }));
    expect(screen.queryByRole('button', { name: 'Delete permanently' })).toBeNull();
  });
});
