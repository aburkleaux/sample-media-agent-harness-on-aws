// @vitest-environment jsdom
//
// Loading behaviour of the built-in skills browser. Both states are asserted
// because they are different problems: the FIRST load has nothing to show and
// needs a shaped placeholder, while a REFRESH already has rows on screen and
// must not throw them away — previously the only signal there was the Refresh
// button relabelling itself.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ listSkills: vi.fn(), peekSkills: vi.fn() }));
vi.mock('../api', () => api);

// The panel now merges a control-plane catalog with the runtime's live session
// state. `builtInSkills: null` is the "no catalog" case, which is exactly the
// behaviour the tests below were written against — so they keep asserting the live
// path, and the catalog-first behaviour is asserted separately at the bottom.
const lab = vi.hoisted(() => ({ useLabContext: vi.fn() }));
vi.mock('../LabContext', () => ({ useLabContext: lab.useLabContext }));

import BuiltInSkillsBrowser from './BuiltInSkillsBrowser';

const SKILL = {
  name: 'bundled-skill',
  description: 'A bundled skill.',
  source: 'local' as const,
  skillMd: '# x',
  supportingFiles: [],
  tools: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  lab.useLabContext.mockReturnValue({ builtInSkills: null });
  // No cache → the component starts unloaded and must fetch.
  api.peekSkills.mockReturnValue(undefined);
});

afterEach(cleanup);

describe('BuiltInSkillsBrowser loading states', () => {
  it('shows a shaped skeleton on first load, not a bare text line', async () => {
    let release: (v: unknown) => void = () => {};
    api.listSkills.mockReturnValue(new Promise(resolve => { release = resolve; }));

    render(<BuiltInSkillsBrowser />);

    // Announced, and shaped like the list rather than the old italic
    // "Loading skills..." string.
    const skeleton = screen.getByRole('status', { name: 'Loading built-in skills' });
    expect(skeleton).toBeTruthy();
    expect(screen.queryByText('Loading skills...')).toBeNull();

    release([SKILL]);
    await waitFor(() => expect(screen.getByText('bundled-skill')).toBeTruthy());
    expect(screen.queryByRole('status', { name: 'Loading built-in skills' })).toBeNull();
  });

  it('keeps the rows and says it is refreshing, rather than blanking the list', async () => {
    api.listSkills.mockResolvedValueOnce([SKILL]);
    render(<BuiltInSkillsBrowser />);
    await waitFor(() => expect(screen.getByText('bundled-skill')).toBeTruthy());

    let release: (v: unknown) => void = () => {};
    api.listSkills.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh built-in skills' }));

    // The row the user was looking at is still there…
    expect(screen.getByText('bundled-skill')).toBeTruthy();
    // …and there is a live status line, not just the button's own label.
    expect(await screen.findByText('Getting latest skill status…')).toBeTruthy();
    // First-load skeleton must NOT appear over existing content.
    expect(screen.queryByRole('status', { name: 'Loading built-in skills' })).toBeNull();

    release([SKILL]);
    await waitFor(() => expect(screen.queryByText('Getting latest skill status…')).toBeNull());
  });

  it('surfaces a load failure with a dismissable message', async () => {
    api.listSkills.mockRejectedValueOnce(new Error('gateway down'));
    render(<BuiltInSkillsBrowser />);

    expect(await screen.findByText('gateway down')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Loading built-in skills' })).toBeNull();
  });

  /* This panel used to be held to `BuiltInAgentsBrowser`'s hairline rows, then to a
   * table of its own. It now draws the SAME card as the Yours/Published sections
   * below it, which is the parity that matters on this screen: a user scrolling the
   * Skills surface sees one kind of row for one kind of thing. `BuiltInAgentsBrowser`
   * now draws the same card too, so the two built-in panels match. */
  it('draws the same bordered card the Yours and Published rows use', async () => {
    api.listSkills.mockResolvedValueOnce([SKILL]);
    render(<BuiltInSkillsBrowser />);

    const card = (await screen.findByText('bundled-skill')).closest('[style*="border-radius"]')!;
    const style = card.getAttribute('style')!;
    expect(style).toContain('--color-border');
    expect(style).toContain('border-radius: 8px'); // radius.panel, as InventoryRow
    expect(style).toContain('padding: 12px 16px');
  });
});

// --- Layout: one card per skill, description clamped ------------------------
//
// The defects that drove the previous table are still asserted, because the card has
// to keep answering them: the description must not be cut to one line, and the action
// must not crowd the text.

describe('BuiltInSkillsBrowser layout', () => {
  // Trimmed: `findByText` normalises whitespace, so a trailing space would never
  // match the rendered node.
  const LONG = 'Use for CREATING media with fal.ai -- generate or edit images, video, music, speech, and combine video with audio (mux). '.repeat(3).trim();

  it('lists skills as cards, not as a table', async () => {
    // The table's fixed name and action columns took width from the only content that
    // matters here. Asserting the absence keeps a reflexive revert visible.
    api.listSkills.mockResolvedValueOnce([SKILL]);
    render(<BuiltInSkillsBrowser />);

    await screen.findByText('bundled-skill');
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('columnheader')).toBeNull();
    // Name and description both on the row.
    expect(screen.getByText('A bundled skill.')).toBeInTheDocument();
  });

  it('puts View on the name line, with the description on its own line below', async () => {
    // Same intent as the old "View in its own column to the right of the description":
    // the action belongs to the ROW and must not sit inside the description text. In a
    // card that means the name line owns it, which is also what lets the description
    // span the full width.
    api.listSkills.mockResolvedValueOnce([SKILL]);
    render(<BuiltInSkillsBrowser onOpenSkill={() => {}} />);

    const view = await screen.findByRole('button', { name: 'View bundled-skill' });
    const nameLine = screen.getByText('bundled-skill').parentElement!;
    expect(nameLine.contains(view)).toBe(true);
    expect(nameLine.contains(screen.getByText('A bundled skill.'))).toBe(false);
    // And the description precedes nothing of the action cluster in the flow.
    const description = screen.getByText('A bundled skill.');
    expect(view.compareDocumentPosition(description) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('starts the panel title and the rows on one left edge', async () => {
    // A single box owns the inset. When the rows held it instead, the title — and every
    // message this panel can show — stayed hard against the bordered edge, an inset
    // away from the content beneath it.
    //
    // Asserted structurally rather than by position: jsdom does no layout, so the real
    // invariant is "one ancestor supplies the edge and nothing inside adds its own".
    api.listSkills.mockResolvedValueOnce([SKILL]);
    render(<BuiltInSkillsBrowser onOpenSkill={() => {}} />);

    const name = await screen.findByText('bundled-skill');
    const title = screen.getByText('Built-in Skills');
    const panel = title.closest('div')!.parentElement!;

    expect(parseFloat(panel.style.paddingLeft)).toBeGreaterThan(0);
    expect(panel.contains(title)).toBe(true);
    expect(panel.contains(name)).toBe(true);
  });

  it('shows no source group heading when every skill is bundled', async () => {
    // "Bundled (filesystem)" was a heading made out of a constant, and the control
    // it wrapped could only hide the entire list.
    api.listSkills.mockResolvedValueOnce([SKILL, { ...SKILL, name: 'second-skill' }]);
    render(<BuiltInSkillsBrowser />);

    await screen.findByText('bundled-skill');
    expect(screen.queryByText(/Bundled \(filesystem\)/i)).toBeNull();
    // And nothing collapsible is left to hide the rows behind.
    expect(screen.queryByRole('button', { expanded: true })).toBeNull();
    expect(screen.queryByRole('button', { expanded: false })).toBeNull();
  });

  it('still identifies a skill that is NOT bundled', async () => {
    // Dropping the grouping must not silently erase the distinction it carried.
    api.listSkills.mockResolvedValueOnce([{ ...SKILL, name: 's3-skill', source: 's3' as const }]);
    render(<BuiltInSkillsBrowser />);

    await screen.findByText('s3-skill');
    expect(screen.getByText(/Loaded from S3/i)).toBeTruthy();
  });

  it('clamps a long description to three lines WITHOUT truncating the text', async () => {
    // The original defect was a one-line clamp that cut every description
    // mid-sentence — and the clause it removed was the one saying when the skill
    // fires. Three lines plus `more` keeps that clause, and the clamp is visual only:
    // the full string stays in the DOM, so it is still searchable and copyable. A JS
    // pre-truncation (this panel once cut at 80 characters) is what must not come back.
    api.listSkills.mockResolvedValueOnce([{ ...SKILL, description: LONG }]);
    render(<BuiltInSkillsBrowser />);

    const text = await screen.findByText(LONG);
    expect(text.style.webkitLineClamp).toBe('3');
    expect(text.textContent).toBe(LONG);
  });
});

/*
 * The catalog / live-state split.
 *
 * The list comes from the Capability API so it paints without waking the runtime;
 * `activated` and `source` describe the agent's live session, which only the runtime
 * knows. The point of these tests is that a slow or failed runtime costs badges, not
 * the list — and that a row with no live state yet makes no claim about activation.
 */
describe('BuiltInSkillsBrowser catalog and live state', () => {
  const CATALOG = [
    { name: 'aws-research', description: 'Looks up AWS facts.' },
    { name: 'pdf', description: 'Does PDF things.' },
  ];

  it('paints the catalog before the runtime answers', () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    // A runtime call that never resolves — the list must not wait for it.
    api.listSkills.mockReturnValue(new Promise(() => {}));

    render(<BuiltInSkillsBrowser />);

    expect(screen.getByText('aws-research')).toBeTruthy();
    expect(screen.getByText('pdf')).toBeTruthy();
    expect(screen.queryByRole('status', { name: 'Loading built-in skills' })).toBeNull();
  });

  it('makes no activation claim until live state arrives', () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockReturnValue(new Promise(() => {}));

    render(<BuiltInSkillsBrowser />);

    // Neither the `active` badge nor a false negative in its place.
    expect(screen.queryByText('active')).toBeNull();
  });

  it('merges activation in when the runtime answers, without reordering', async () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockResolvedValueOnce([
      { name: 'aws-research', description: 'stale runtime copy', source: 'local', activated: true },
    ]);

    render(<BuiltInSkillsBrowser />);

    expect(await screen.findByText('active')).toBeTruthy();
    // The catalog's description wins: it is parsed from the shipped SKILL.md, so it
    // cannot be staler than the runtime's copy.
    expect(screen.getByText('Looks up AWS facts.')).toBeTruthy();
    expect(screen.queryByText('stale runtime copy')).toBeNull();
  });

  it('keeps listing a shipped skill the runtime did not report', async () => {
    // The runtime's view can lag a deploy. Catalog membership decides what exists.
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockResolvedValueOnce([{ name: 'aws-research', source: 'local' }]);

    render(<BuiltInSkillsBrowser />);

    await waitFor(() => expect(screen.getByText('aws-research')).toBeTruthy());
    expect(screen.getByText('pdf')).toBeTruthy();
  });

  it('keeps a runtime-loaded skill that the catalog does not know', async () => {
    // The regression this covers: iterating the catalog alone dropped every skill that
    // exists only in the runtime. `list_skills` returns more than the shipped set — an
    // S3-pulled or in-memory skill lives only there, and the panel has a badge for it.
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockResolvedValueOnce([
      { name: 'aws-research', source: 'local', activated: true },
      { name: 'pulled-from-s3', description: 'Loaded at runtime.', source: 's3' },
    ]);

    render(<BuiltInSkillsBrowser />);

    expect(await screen.findByText('pulled-from-s3')).toBeTruthy();
    // ...and it keeps the badge that says where it came from.
    expect(screen.getByText('Loaded from S3')).toBeTruthy();
    // The shipped set is still there alongside it.
    expect(screen.getByText('aws-research')).toBeTruthy();
    expect(screen.getByText('pdf')).toBeTruthy();
  });

  it('lists a skill present on both sides exactly once', async () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockResolvedValueOnce([{ name: 'aws-research', source: 'local' }]);

    render(<BuiltInSkillsBrowser />);

    await waitFor(() => expect(screen.getAllByText('aws-research')).toHaveLength(1));
  });

  it('does not dim the list while it waits for status', async () => {
    // The rows are catalog-final; only the badges are outstanding. Fading them to 55%
    // (the old `staleListStyle`) claimed the content was stale, which is how a ~60ms
    // panel came to look like a page reload — the status call takes ~9s on a cold
    // runtime, and the whole list sat greyed out for it.
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockReturnValue(new Promise(() => {})); // never resolves

    render(<BuiltInSkillsBrowser />);

    const row = screen.getByText('aws-research');
    // No ancestor of the row may be carrying an opacity while status is pending.
    for (let el: HTMLElement | null = row; el; el = el.parentElement) {
      expect(el.style.opacity, `${el.tagName} dims the list`).toBe('');
    }
    // The busy state is still announced — just not by fading the content.
    const busy = document.querySelector('[aria-busy="true"]');
    expect(busy).not.toBeNull();
    expect(busy!.contains(row)).toBe(true);
  });

  it('says it is getting status, not refreshing the list', async () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockReturnValue(new Promise(() => {}));

    render(<BuiltInSkillsBrowser />);

    expect(await screen.findByText('Getting latest skill status…')).toBeTruthy();
    // "Refreshing" described the old behaviour, where the runtime owned the rows.
    expect(screen.queryByText(/Refreshing built-in skills/)).toBeNull();
  });

  it('does not report a runtime failure as a panel failure', async () => {
    lab.useLabContext.mockReturnValue({ builtInSkills: CATALOG });
    api.listSkills.mockRejectedValueOnce(new Error('runtime unreachable'));

    render(<BuiltInSkillsBrowser />);

    // The catalog answered, so the rows stand and the missing badges are not an error.
    await waitFor(() => expect(screen.getByText('aws-research')).toBeTruthy());
    expect(screen.queryByText('runtime unreachable')).toBeNull();
  });
});
