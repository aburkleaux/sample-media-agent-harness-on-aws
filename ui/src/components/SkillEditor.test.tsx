// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  createSkillDraft: vi.fn(),
  deleteSupportingFile: vi.fn(),
  importSkillPackage: vi.fn(),
  listGatewayTools: vi.fn(),
  peekGatewayTools: vi.fn(),
  putSupportingFile: vi.fn(),
  readSkillDraft: vi.fn(),
  renameAsset: vi.fn(),
  updateSkillMd: vi.fn(),
}));
const refreshCategory = vi.hoisted(() => vi.fn());

vi.mock('../lab-api', () => api);
// Renders its OWN back control, because the real component does (and its heading).
// A mock without them hid a duplicate-chrome regression: the importer was wrapped
// in an `EditorFrame` it did not need, so the screen carried two back controls.
// A stub looser than the component it stands in for cannot catch that.
// Stubbed to keep these tests about the EDITOR's hosting of the panel, not the panel's own
// rendering (its own test covers that). The stub mirrors the real prop surface: presentational,
// no submit of its own, and a Clear-folder that hands control back. A stub that still rendered a
// heading, a back control and an "Import" button — as the old one did — could no longer catch the
// duplicate-chrome regressions those props existed to prevent, because the props are gone.
vi.mock('./SkillPackageImport', () => ({
  default: ({ onCancel, name, validation }: any) => (
    <div data-testid="skill-import">
      <span data-testid="import-name">{name}</span>
      <span data-testid="import-errors">{validation?.errors?.length ?? 0}</span>
      <button onClick={onCancel}>Clear folder</button>
    </div>
  ),
}));
// `skills` is read for the folder importer's name-collision check. The factory is
// an explicit allowlist, so omitting it makes the editor crash on `.map`.
vi.mock('../LabContext', () => ({
  useLabContext: () => ({ refreshCategory, skills: [] }),
}));

import SkillEditor, {
  composeFrontmatter,
  metadataEntriesFromRecord,
  metadataRecordFromEntries,
  parseAllowedTools,
  parseFrontmatter,
} from './SkillEditor';

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.peekGatewayTools.mockReturnValue([
    { name: 'read_file', description: 'Read', version: '1.0', source: 'Built-in Tools' },
    { name: 'shell', description: 'Shell', version: '1.0', source: 'Built-in Tools' },
  ]);
  api.listGatewayTools.mockResolvedValue(api.peekGatewayTools());
  api.createSkillDraft.mockResolvedValue({ assetId: 'skill-123', s3Prefix: 'drafts/skills/skill-123/' });
  api.putSupportingFile.mockResolvedValue({ key: 'drafts/skills/skill-123/scripts/helper.py' });
  api.updateSkillMd.mockResolvedValue({ updatedAt: Date.now() });
  refreshCategory.mockResolvedValue(undefined);
});

describe('SkillEditor frontmatter helpers', () => {
  it('round-trips selected tools and stable metadata values', () => {
    const entries = metadataEntriesFromRecord({ author: 'example', version: '1.0' });
    expect(entries[0].id).toBeTruthy();
    const content = composeFrontmatter(
      'document-review',
      'Reviews documents. Use when a user requests document review.',
      ['read_file', 'shell'],
      metadataRecordFromEntries(entries),
      '# Instructions',
    );
    const parsed = parseFrontmatter(content);
    expect(parsed.allowedTools).toEqual(['read_file', 'shell']);
    expect(parsed.metadata).toEqual({ author: 'example', version: '1.0' });
    expect(parseAllowedTools('read_file  shell read_file')).toEqual(['read_file', 'shell']);
  });
});

describe('SkillEditor', () => {
  it('keeps metadata key focus while the editable key changes', () => {
    render(<SkillEditor assetId={null} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '+ Add metadata' }));
    const keyInput = screen.getByLabelText('Metadata key 1');
    keyInput.focus();
    fireEvent.change(keyInput, { target: { value: 'author' } });

    expect(screen.getByLabelText('Metadata key 1')).toBe(keyInput);
    expect(document.activeElement).toBe(keyInput);
  });

  it('creates only SKILL.md and keeps the editor open', async () => {
    render(<SkillEditor assetId={null} onClose={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('Skill name'), { target: { value: 'document-review' } });
    fireEvent.change(screen.getByLabelText('Skill description'), {
      target: { value: 'Reviews documents. Use when a user requests document review.' },
    });
    fireEvent.change(screen.getByLabelText('Add allowed tool'), { target: { value: 'shell' } });
    fireEvent.change(screen.getByLabelText('Skill instructions'), { target: { value: '# Review workflow' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create Skill' }));

    await waitFor(() => expect(api.createSkillDraft).toHaveBeenCalledTimes(1));
    const skillMd = api.createSkillDraft.mock.calls[0][1] as string;
    expect(skillMd).toContain('allowed-tools: shell');
    expect(await screen.findByText(/SKILL.md created/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '+ Add file' })).toBeNull();
    expect(api.putSupportingFile).not.toHaveBeenCalled();
    // Still open, and now in edit mode: the form is intact and the toolbar's Save
    // has flipped from create to save. This used to be asserted via a `Done` button
    // in the form's foot, which moved out — Back does that job now.
    expect(screen.getByLabelText('Skill name')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Save Skill' })).toBeTruthy();
  });
});

// --- Toolbar: Back and Save on one row at the top ---------------------------

describe('SkillEditor toolbar', () => {
  const DRAFT = {
    assetId: 'skill-123',
    name: 'audit-skill',
    category: 'skill' as const,
    status: 'archived' as const,
    createdAt: 1,
    updatedAt: 2,
    s3Prefix: 'drafts/skills/skill-123/',
    skillMd: '---\nname: audit-skill\ndescription: Audits things. Use when auditing.\n---\nDo the audit.',
    supportingFiles: [],
  };

  it('puts the primary action BELOW the form, in the flow rather than floating over it', async () => {
    // Reverses an earlier decision, deliberately. Save had been moved INTO the top toolbar because
    // "Save sat at the foot of a form long enough to scroll, so the control reached for most was
    // the furthest away". A sticky footer was tried to satisfy both and it floated the panel OVER
    // the form while scrolling, so the action is now simply the last thing in the flow. Assert the
    // absence of pinning: re-adding it is the regression.
    api.readSkillDraft.mockResolvedValue(DRAFT);
    render(<SkillEditor assetId="skill-123" onClose={() => {}} />);

    const save = await screen.findByRole('button', { name: 'Save Skill' });
    const nameField = screen.getByLabelText('Skill name');
    // Below the form.
    expect(nameField.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // In the flow — not pinned over the content.
    expect(save.parentElement?.style.position).not.toBe('sticky');
    expect(save.parentElement?.style.position).not.toBe('fixed');
    // And NOT in the toolbar any more — Back stays there alone, because every branch of this
    // editor (including Loading and load-failure) renders the toolbar and needs a way out.
    const back = screen.getByRole('button', { name: '← Back' });
    expect(back.parentElement).not.toBe(save.parentElement);
  });

  it('offers exactly one way out, with no "Back to Skills" restating the header', async () => {
    // The Skills surface header already names the surface; the host frame's
    // "← Back to Skills / Edit SKILL.md" said it twice more.
    api.readSkillDraft.mockResolvedValue(DRAFT);
    render(<SkillEditor assetId="skill-123" onClose={() => {}} />);

    await screen.findByLabelText('Skill name');
    expect(screen.getByRole('button', { name: '← Back' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Back to Skills/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Done' })).toBeNull();
  });

  it.each([
    {
      branch: 'while the draft is still loading',
      arrange: () => api.readSkillDraft.mockReturnValue(new Promise(() => {})),
      settle: async () => { await screen.findByText('Loading SKILL.md…'); },
    },
    {
      branch: 'when the draft fails to load',
      arrange: () => api.readSkillDraft.mockRejectedValue(new Error('draft gone')),
      settle: async () => { await screen.findByText('draft gone'); },
    },
  ])('keeps a way out $branch', async ({ arrange, settle }) => {
    // The load and failure branches are exactly where a stranded user has no other
    // exit: the host frame supplies no back control for skills any more, so a
    // toolbar rendered only by the form would trap them here.
    arrange();
    render(<SkillEditor assetId="skill-123" onClose={() => {}} />);

    await settle();
    expect(screen.getByRole('button', { name: '← Back' })).toBeTruthy();
  });

  it('leaves when Back is clicked', async () => {
    api.readSkillDraft.mockResolvedValue(DRAFT);
    const onClose = vi.fn();
    render(<SkillEditor assetId="skill-123" onClose={onClose} />);

    await screen.findByLabelText('Skill name');
    fireEvent.click(screen.getByRole('button', { name: '← Back' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

// --- Folder import, moved here from the Skills surface header ---------------
//
// It sat beside Search as though it were a way to view the list. It is the second
// way to CREATE a skill, so it belongs on the create path next to the blank form
// it is an alternative to. These tests came with it.

describe('SkillEditor folder import', () => {
  const openFolderTab = () => fireEvent.click(screen.getByRole('tab', { name: 'Upload folder' }));

  // A REAL folder pick: `readSkillPackageFolder` derives the folder (and therefore the suggested
  // name) from `webkitRelativePath`, which a bare `new File(...)` does not have. Without it the
  // candidate has no folder, validation fails, and the tests below would be asserting against an
  // invalid package rather than the happy path.
  const folderFile = (path: string, body: string) => {
    const file = new File([body], path.split('/').pop() ?? path, { type: 'text/markdown' });
    Object.defineProperty(file, 'webkitRelativePath', { value: path });
    return file;
  };

  const VALID_SKILL_MD = ['---', 'name: my-skill', 'description: Does a thing. Use when asked.', '---', '', '# Steps', ''].join('\n');

  const pickFolder = (files = [folderFile('my-skill/SKILL.md', VALID_SKILL_MD)]) =>
    fireEvent.change(screen.getByLabelText('Choose Skill folder'), { target: { files } });

  it('offers the two creation paths as tabs, defaulting to the form', () => {
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map(tab => tab.textContent)).toEqual(['Write SKILL.md', 'Upload folder']);
    // Default is the form, so the common path costs no clicks.
    expect(screen.getByRole('tab', { name: 'Write SKILL.md' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByLabelText('Skill name')).toBeInTheDocument();
  });

  it('swaps the form for the folder picker when the tab is chosen', () => {
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    expect(screen.getByRole('button', { name: 'Choose folder' })).toBeInTheDocument();
    // One path at a time: two name fields on one screen is what the tabs remove.
    expect(screen.queryByLabelText('Skill name')).toBeNull();
  });

  it('is a FOLDER picker, not a file picker', () => {
    // `webkitdirectory` is absent from React's DOM typings and set imperatively from a ref callback.
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    expect(screen.getByLabelText('Choose Skill folder').hasAttribute('webkitdirectory')).toBe(true);
  });

  it('does not offer it when editing an existing skill', async () => {
    // Importing over a persisted draft would create a SECOND skill and orphan the one being
    // edited, so neither the tab NOR the hidden input may exist here.
    api.readSkillDraft.mockResolvedValue({
      assetId: 'skill-123',
      name: 'audit-skill',
      category: 'skill',
      status: 'archived',
      createdAt: 1,
      updatedAt: 2,
      s3Prefix: 'drafts/skills/skill-123/',
      skillMd: '---\nname: audit-skill\ndescription: Audits things. Use when auditing.\n---\nDo the audit.',
      supportingFiles: [],
    });

    render(<SkillEditor assetId="skill-123" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByLabelText('Skill name')).toBeInTheDocument());
    expect(screen.queryByRole('tab', { name: 'Upload folder' })).toBeNull();
    expect(screen.queryByLabelText('Choose Skill folder')).toBeNull();
  });

  it('reviews the package INLINE, without leaving the screen', async () => {
    // The review used to replace the whole editor. Inline, the tabs stay on screen, so the user can
    // still see (and return to) the other path.
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    pickFolder();

    expect(await screen.findByTestId('skill-import')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Upload folder' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Write SKILL.md' })).toBeInTheDocument();
  });

  it('reveals the folder tab even if the pick happened while the form was showing', async () => {
    // Otherwise the candidate sits invisible behind the form while the footer's primary silently
    // switches to importing it.
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    pickFolder();

    expect(await screen.findByTestId('skill-import')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Upload folder' })).toHaveAttribute('aria-selected', 'true');
  });

  it('seeds the import name from the folder', async () => {
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    pickFolder();
    // The panel no longer owns this state, so the host has to seed it — an unseeded name imports
    // under the empty string.
    await waitFor(() => expect(screen.getByTestId('import-name')).toHaveTextContent('my-skill'));
  });

  it('returns to the picker when the folder is cleared', async () => {
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    pickFolder();
    await screen.findByTestId('skill-import');

    fireEvent.click(screen.getByRole('button', { name: 'Clear folder' }));
    expect(screen.queryByTestId('skill-import')).toBeNull();
    // Back to the picker, still on the folder tab — not thrown out to the form.
    expect(screen.getByRole('button', { name: 'Choose folder' })).toBeInTheDocument();
  });

  it('imports from the single bottom action, then refreshes and closes', async () => {
    const onClose = vi.fn();
    api.importSkillPackage.mockResolvedValue({ assetId: 'imported-1', s3Prefix: 'p/', supportingFiles: [] });
    render(<SkillEditor assetId={null} onClose={onClose} />);
    openFolderTab();
    pickFolder();
    await screen.findByTestId('skill-import');

    // Same generic primary as the form path — one action, whichever tab is open.
    fireEvent.click(screen.getByRole('button', { name: 'Create Skill' }));

    await waitFor(() => expect(api.importSkillPackage).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(refreshCategory).toHaveBeenCalledWith('skill'));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('holds the preview space with a placeholder before a folder is chosen', () => {
    // Otherwise the grey picker card and the action row sit crammed together, with nothing showing
    // where the review will appear.
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    expect(screen.getByText(/Choose a folder to see its SKILL.md/)).toBeInTheDocument();
  });

  it('replaces the placeholder with the real review once a folder is chosen', async () => {
    // Both at once would stack an empty box above the actual preview.
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    pickFolder();

    await screen.findByTestId('skill-import');
    expect(screen.queryByText(/Choose a folder to see its SKILL.md/)).toBeNull();
  });

  it('will not import before a folder is chosen, and says why', () => {
    render(<SkillEditor assetId={null} onClose={() => {}} />);
    openFolderTab();
    expect(screen.getByRole('button', { name: 'Create Skill' })).toBeDisabled();
    // Disabled with no explanation reads as broken.
    expect(screen.getByText(/Choose a folder to continue/)).toBeInTheDocument();
  });
});
