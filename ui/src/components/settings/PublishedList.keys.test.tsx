// @vitest-environment jsdom
//
// Published rows are keyed per VERSION, not per lineage.
//
// `assetId` IS the version-lineage id (v2 has no separate published_id), so every
// published version of one skill carries the SAME assetId. Keying the list rows by
// it gave React duplicate keys, and React's reconciliation is undefined in that
// case: re-rendering the list with a different array left the previous rows
// mounted. Typing in the search box and clearing it again re-derives that array on
// every keystroke, so the Published section grew a stale copy each pass — the
// filter chip counted the array (3) while the DOM held eight rows.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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

/** Two versions of ONE lineage — they share `assetId`, which is the whole point. */
const PUBLISHED = [
  { assetId: 'skill-lineage-1', name: 'published-view-e2e', version: 2, current: true, scope: 'private' as const, createdAt: 2, updatedAt: 2 },
  { assetId: 'skill-lineage-1', name: 'published-view-e2e', version: 1, current: false, scope: 'private' as const, createdAt: 1, updatedAt: 1 },
  { assetId: 'skill-lineage-2', name: 'testskill', version: 1, current: true, scope: 'private' as const, createdAt: 1, updatedAt: 1 },
];

/**
 * Skills render one row per LINEAGE, so the three versions above are two rows.
 * The rows-equal-the-chip invariant below is the real assertion — it held for
 * neither the duplicate-key bug (chip 3, DOM 8) nor a miscounted chip.
 */
const LINEAGES = 2;

function Harness() {
  const [search, setSearch] = React.useState('');
  return (
    <>
      <input aria-label="Search skills" value={search} onChange={e => setSearch(e.target.value)} />
      <SkillsTabBody
        createLabel="+ New skill"
        skills={[]}
        publishedSkills={PUBLISHED}
        search={search}
        loading={false}
        onView={vi.fn()} onViewPublished={vi.fn()} onEdit={vi.fn()} onDelete={vi.fn()}
        onTest={vi.fn()} onPublish={vi.fn()} onForkPublished={vi.fn()}
      />
    </>
  );
}

const publishedRowCount = () =>
  screen.getAllByRole('button', { name: /^Test / }).length;

afterEach(cleanup);

describe('Published skills list — searching then clearing', () => {
  it('renders one row per published lineage, collapsing its versions', () => {
    render(<Harness />);

    expect(publishedRowCount()).toBe(LINEAGES);
    // The representative is the `current` version (v2, not the highest-by-luck
    // ordering), and the row says how many sit behind it so Delete visibly
    // reaches more than the version named.
    const rendered = document.body.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(rendered).toContain('v2 · 2 versions');
    // A single-version lineage says just the version, with no count to explain.
    expect(rendered).not.toContain('1 versions');
  });

  it('does not accumulate rows as the search box is typed into and cleared', () => {
    render(<Harness />);
    const box = screen.getByRole('textbox', { name: 'Search skills' });
    const before = publishedRowCount();

    // Three search-then-clear cycles, as reported.
    for (const term of ['published', '', 'test', '', 'e2e', '']) {
      fireEvent.change(box, { target: { value: term } });
    }

    expect(publishedRowCount()).toBe(before);
  });

  it('agrees with the count shown on the Published filter chip', () => {
    render(<Harness />);
    const box = screen.getByRole('textbox', { name: 'Search skills' });
    fireEvent.change(box, { target: { value: 'published' } });
    fireEvent.change(box, { target: { value: '' } });

    // The chip counts the array; the rows are the DOM. They drifted apart because
    // only the DOM carried the orphans.
    const chip = screen.getByRole('button', { name: /^Published / });
    expect(chip.textContent).toMatch(new RegExp(`${LINEAGES}$`));
    expect(publishedRowCount()).toBe(LINEAGES);
  });
});
