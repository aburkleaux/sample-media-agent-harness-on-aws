/**
 * @vitest-environment jsdom
 *
 * Example/unit tests for the Custom Agent editor surface (task 6.5):
 *   - Req 1.6  Editor opens in create mode for a new draft
 *   - Req 1.7  Editor presents name / persona / skills / model / tool_collections
 *              fields plus the integration panel
 *   - Req 1.8  A non-empty name is required to submit; submitting creates a draft
 *   - Req 4.1  Integration panel is disabled (off) by default
 *   - Req 4.4  Invoke URL "POST /invoke/{agentId}" is surfaced when integration is on
 *   - Req 4.6  Save failure preserves the owner's unsaved input and shows an error
 *   - Req 8.5  Version history list renders published versions with their state
 *
 * The editor's data dependencies (model catalog, LabContext inventory, and the
 * lab-api CRUD calls) are mocked; the real LabApiError class is preserved so the
 * component's `instanceof` branch keeps working.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { BELL_SAFE_AREA } from '../utils/layout';

const mocks = vi.hoisted(() => ({
  fetchModels: vi.fn(),
  readSpecialtyAgentDraft: vi.fn(),
  updateSpecialtyAgentDraft: vi.fn(),
  createSpecialtyAgentDraft: vi.fn(),
  renameAsset: vi.fn(),
  publishAsset: vi.fn(),
  listAssetVersions: vi.fn(),
  useLabContext: vi.fn(),
}));

vi.mock('../api', () => ({
  fetchModels: mocks.fetchModels,
}));

vi.mock('../LabContext', () => ({
  useLabContext: mocks.useLabContext,
}));

vi.mock('../lab-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lab-api')>();
  return {
    ...actual, // keep LabApiError (a runtime value) and the domain types
    readSpecialtyAgentDraft: mocks.readSpecialtyAgentDraft,
    updateSpecialtyAgentDraft: mocks.updateSpecialtyAgentDraft,
    createSpecialtyAgentDraft: mocks.createSpecialtyAgentDraft,
    renameAsset: mocks.renameAsset,
    publishAsset: mocks.publishAsset,
    listAssetVersions: mocks.listAssetVersions,
  };
});

import SpecialtyAgentEditor from './SpecialtyAgentEditor';

function labContextValue(overrides: Record<string, unknown> = {}) {
  return {
    skills: [{ assetId: 's1', name: 'Skill One' }],
    publishedSkills: [],
    toolCollections: [{ assetId: 't1', name: 'Tool One' }],
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    // staging added lazy category loading; the editor calls it on mount.
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe('SpecialtyAgentEditor — Custom Agent editor surface', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetchModels.mockResolvedValue([{ id: 'model-a', name: 'Model A' }]);
    mocks.listAssetVersions.mockResolvedValue([]);
    mocks.createSpecialtyAgentDraft.mockResolvedValue({ assetId: 'new-1', s3Prefix: 'p/' });
    mocks.useLabContext.mockReturnValue(labContextValue());
  });

  it('opens in create mode with a "New Agent" title (Req 1.6)', () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);
    expect(screen.getByText('New Agent')).toBeTruthy();
  });

  it('presents the configuration fields and integration panel (Req 1.7)', async () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

    // Core fields
    expect(screen.getByText('Agent Name')).toBeTruthy();
    expect(screen.getByText('Persona')).toBeTruthy();
    expect(screen.getByText('Compatible Skills')).toBeTruthy();
    expect(screen.getByText('Model')).toBeTruthy();
    expect(screen.getByText('Tool Collections')).toBeTruthy();

    // Inventory from LabContext is rendered as selectable options
    expect(screen.getByText('Skill One')).toBeTruthy();
    expect(screen.getByText('Tool One')).toBeTruthy();

    // Integration panel present
    expect(screen.getByLabelText('Enable external integration')).toBeTruthy();

    // Model catalog populates the picker
    expect(await screen.findByText('Model A')).toBeTruthy();
  });

  describe('Compatible Skills offers published skills, not only drafts', () => {
    // Publishing a skill CONSUMES its working copy, so a drafts-only picker made
    // publishing a skill remove it from this list — while the runtime resolves
    // the published version FIRST and only falls back to the draft. The editor
    // was offering the fallback and hiding the primary.

    it('lists a published skill and lets it be selected', async () => {
      mocks.useLabContext.mockReturnValue(labContextValue({
        skills: [],
        publishedSkills: [{ assetId: 'pub-1', name: 'Shipped Skill', version: 3 }],
      }));
      mocks.createSpecialtyAgentDraft.mockResolvedValue({ assetId: 'new-1', s3Prefix: 'p/' });
      render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

      expect(screen.getByText('Shipped Skill')).toBeTruthy();
      // Badged with the version the runtime will load.
      expect(screen.getByText('published v3')).toBeTruthy();
      // Not reported as missing inventory.
      expect(screen.queryByText('No skills available.')).toBeNull();

      // Selecting it persists the lineage id — the same id a draft would store,
      // since in v2 `asset_id` IS the lineage id.
      fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'A' } });
      fireEvent.change(screen.getByPlaceholderText('Enter the agent persona prompt…'), {
        target: { value: 'You are helpful.' },
      });
      fireEvent.click(screen.getByRole('checkbox', { name: /Shipped Skill/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Create Agent' }));

      await waitFor(() => expect(mocks.createSpecialtyAgentDraft).toHaveBeenCalled());
      // Signature is (name, persona, compatibleSkills, extras).
      const [, , compatibleSkills] = mocks.createSpecialtyAgentDraft.mock.calls[0];
      expect(compatibleSkills).toEqual(['pub-1']);
    });

    it('shows a lineage with both a draft and a published version once, badged published', () => {
      // What an EditAsset fork leaves behind. The runtime prefers published, so
      // that is what the row must claim.
      mocks.useLabContext.mockReturnValue(labContextValue({
        skills: [{ assetId: 'same-1', name: 'Forked Skill' }],
        publishedSkills: [{ assetId: 'same-1', name: 'Forked Skill', version: 2 }],
      }));
      render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

      expect(screen.getAllByText('Forked Skill')).toHaveLength(1);
      expect(screen.getByText('published v2')).toBeTruthy();
      expect(screen.queryByText('draft')).toBeNull();
    });

    it('does not call a published reference "unknown"', async () => {
      // The second half of the bug: the orphan test checked drafts only, so a
      // published reference rendered as `unknown` with a bare uuid and a working
      // agent looked broken.
      mocks.readSpecialtyAgentDraft.mockResolvedValue({
        assetId: 'a1', name: 'Agent', persona: 'p', compatibleSkills: ['pub-1'],
        model: null, toolCollections: [], description: '',
      });
      mocks.useLabContext.mockReturnValue(labContextValue({
        skills: [],
        publishedSkills: [{ assetId: 'pub-1', name: 'Shipped Skill', version: 1 }],
      }));
      render(<SpecialtyAgentEditor assetId="a1" onClose={vi.fn()} />);

      expect(await screen.findByText('Shipped Skill')).toBeTruthy();
      expect(screen.queryByText('unknown')).toBeNull();
      expect(screen.queryByText('pub-1')).toBeNull();
    });

    it('says no skills rather than no skill DRAFTS when the inventory is empty', () => {
      mocks.useLabContext.mockReturnValue(labContextValue({ skills: [], publishedSkills: [] }));
      render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

      expect(screen.getByText('No skills available.')).toBeTruthy();
      expect(screen.queryByText('No skill drafts available.')).toBeNull();
    });
  });

  it('puts the actions bottom-left with the primary first', () => {
    // Matches ToolCollectionEditor, the other left-aligned editor. Asserted
    // because both halves are easy to undo by reflex: an author adding a button
    // appends it, and `justifyContent` reads like boilerplate.
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

    const primary = screen.getByRole('button', { name: 'Create Agent' });
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    const bar = primary.parentElement!;

    expect(bar).toBe(cancel.parentElement);
    expect(bar.style.justifyContent).toBe('flex-start');
    // Primary precedes Cancel in the DOM, so it is leftmost and tabbed first.
    expect(primary.compareDocumentPosition(cancel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('has the integration toggle off by default (Req 4.1)', () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);
    const toggle = screen.getByLabelText('Enable external integration') as HTMLInputElement;
    expect(toggle.checked).toBe(false);
  });

  it('requires a non-empty name before the draft can be created (Req 1.8)', async () => {
    const onClose = vi.fn();
    render(<SpecialtyAgentEditor assetId={null} onClose={onClose} />);

    const createBtn = screen.getByRole('button', { name: 'Create Agent' }) as HTMLButtonElement;
    // No name/persona yet → submission blocked.
    expect(createBtn.disabled).toBe(true);

    // Whitespace-only name is still treated as empty.
    fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: '   ' } });
    fireEvent.change(screen.getByPlaceholderText('Enter the agent persona prompt…'), {
      target: { value: 'You are helpful.' },
    });
    expect(createBtn.disabled).toBe(true);

    // A real name enables submission and creates a draft.
    fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'Research Agent' } });
    expect(createBtn.disabled).toBe(false);

    fireEvent.click(createBtn);

    await waitFor(() => expect(mocks.createSpecialtyAgentDraft).toHaveBeenCalledTimes(1));
    expect(mocks.createSpecialtyAgentDraft.mock.calls[0][0]).toBe('Research Agent');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('preserves unsaved input and surfaces an error when the save fails (Req 4.6)', async () => {
    const onClose = vi.fn();
    mocks.createSpecialtyAgentDraft.mockRejectedValueOnce(new Error('Save failed: backend unavailable'));
    render(<SpecialtyAgentEditor assetId={null} onClose={onClose} />);

    fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'Research Agent' } });
    fireEvent.change(screen.getByPlaceholderText('Enter the agent persona prompt…'), {
      target: { value: 'You are helpful.' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Create Agent' }));

    // The error is shown...
    expect(await screen.findByText('Save failed: backend unavailable')).toBeTruthy();
    // ...the editor stays open...
    expect(onClose).not.toHaveBeenCalled();
    // ...and the owner's unsaved input is preserved.
    expect((screen.getByLabelText('Agent name') as HTMLInputElement).value).toBe('Research Agent');
    expect(
      (screen.getByPlaceholderText('Enter the agent persona prompt…') as HTMLTextAreaElement).value,
    ).toBe('You are helpful.');
  });

  it('surfaces the invoke URL when integration is enabled in edit mode (Req 4.4)', async () => {
    mocks.readSpecialtyAgentDraft.mockResolvedValue({
      assetId: 'agent-1',
      name: 'Research Agent',
      category: 'specialty-agent',
      status: 'active',
      createdAt: 0,
      updatedAt: 0,
      s3Prefix: 'p/',
      persona: 'You are helpful.',
      compatibleSkills: [],
      model: '',
      toolCollections: [],
      integrationConfig: {
        enabled: true,
        issuer: 'https://issuer.example.com',
        audience: 'custom-agent-aud',
        allowedClientIds: ['client-123'],
      },
    } as never);

    render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} />);

    expect(await screen.findByText('POST /invoke/agent-1')).toBeTruthy();
  });

  it('renders the published version history with version number and state (Req 8.5)', async () => {
    mocks.readSpecialtyAgentDraft.mockResolvedValue({
      assetId: 'agent-1',
      name: 'Research Agent',
      category: 'specialty-agent',
      status: 'active',
      createdAt: 0,
      updatedAt: 0,
      s3Prefix: 'p/',
      persona: 'You are helpful.',
      compatibleSkills: [],
      model: '',
      toolCollections: [],
      integrationConfig: null,
    } as never);
    mocks.listAssetVersions.mockResolvedValue([
      { version: 1, deprecated: false, current: false, scope: 'private', createdAt: 0, updatedAt: 0, name: 'Research Agent' },
      { version: 2, deprecated: false, current: true, scope: 'private', createdAt: 0, updatedAt: 0, name: 'Research Agent' },
    ]);

    render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} />);

    expect(await screen.findByText('v1')).toBeTruthy();
    expect(screen.getByText('v2')).toBeTruthy();
    expect(screen.getByText('published (current)')).toBeTruthy();
  });

  /**
   * `WorkspaceView` floats the notification bell at `zIndex.rail` over every
   * non-chat center mode, so it paints on top of whatever a pane puts in its
   * top-right. This header did not reserve that space, and the result was not a
   * cosmetic overlap: the bell covered the close button, so a click on the
   * visible ✕ hit the bell and the editor would not close. Measured in the
   * browser at the time — ✕ spanned x1017–1042, bell x1021–1051.
   *
   * jsdom has no layout, so this asserts the reservation rather than the
   * geometry. `BELL_SAFE_AREA` is shared with the three center headers that had
   * been copy-pasting `52`.
   */
  it('reserves the floating bell’s safe area so the close button stays clickable', async () => {
    mocks.readSpecialtyAgentDraft.mockResolvedValue({
      assetId: 'agent-1',
      name: 'Research Agent',
      description: '',
      category: 'specialty-agent',
      status: 'draft',
      version: null,
      scope: 'private',
      createdAt: 0,
      updatedAt: 0,
      s3Prefix: 'p/',
      persona: 'You are helpful.',
      compatibleSkills: [],
      model: '',
      toolCollections: [],
      integrationConfig: null,
    } as never);
    mocks.listAssetVersions.mockResolvedValue([]);

    render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} />);

    const close = await screen.findByRole('button', { name: 'Close editor' });
    const header = close.parentElement!;
    expect(header.style.paddingRight).toBe(`${BELL_SAFE_AREA}px`);
  });

  /**
   * The three close buttons and two dismiss buttons in this editor previously
   * had either only a `title` or NO accessible name at all — a screen reader
   * announced "times" or nothing, because the label was the `✕` glyph itself.
   */
  it('gives the close control a real accessible name, not just a tooltip', async () => {
    mocks.readSpecialtyAgentDraft.mockResolvedValue({
      assetId: 'agent-1', name: 'A', description: '', category: 'specialty-agent',
      status: 'draft', version: null, scope: 'private', createdAt: 0, updatedAt: 0,
      s3Prefix: 'p/', persona: 'p', compatibleSkills: [], model: '',
      toolCollections: [], integrationConfig: null,
    } as never);
    mocks.listAssetVersions.mockResolvedValue([]);

    render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} />);
    expect(await screen.findByRole('button', { name: 'Close editor' })).toBeTruthy();
  });

  /** Hosted inside a frame that already supplies a title and a way out. */
  it('renders no header of its own when the host owns the chrome', async () => {
    mocks.readSpecialtyAgentDraft.mockResolvedValue({
      assetId: 'agent-1', name: 'A', description: '', category: 'specialty-agent',
      status: 'draft', version: null, scope: 'private', createdAt: 0, updatedAt: 0,
      s3Prefix: 'p/', persona: 'p', compatibleSkills: [], model: '',
      toolCollections: [], integrationConfig: null,
    } as never);
    mocks.listAssetVersions.mockResolvedValue([]);

    render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} showHeader={false} />);

    await waitFor(() => expect(screen.getByLabelText('Agent name')).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Close editor' })).toBeNull();
    expect(screen.queryByText('Edit Agent')).toBeNull();
  });

  describe('hosts no test surface — testing lives on the published view', () => {
    // The editor used to carry TWO ways to run the agent: an `AgentTestPanel`
    // ("Test agent" — resolved config + live invoke of the draft) and a separate
    // sample-input "Test Harness" (`runHarness`). Both are gone; `AgentTestPanel`
    // survives only in `PublishedAgentView`. Asserted as ABSENCE because the
    // failure mode is additive — someone re-adds a test block to the editor and
    // nothing else complains, leaving two surfaces that resolve an agent's
    // config by different paths and can disagree about what binds.
    async function renderLoadedEditor() {
      mocks.readSpecialtyAgentDraft.mockResolvedValue({
        assetId: 'agent-1', name: 'A', description: '', category: 'specialty-agent',
        status: 'draft', version: null, scope: 'private', createdAt: 0, updatedAt: 0,
        s3Prefix: 'p/', persona: 'p', compatibleSkills: [], model: '',
        toolCollections: [], integrationConfig: null,
      } as never);
      mocks.listAssetVersions.mockResolvedValue([]);
      render(<SpecialtyAgentEditor assetId="agent-1" onClose={vi.fn()} />);
      // Wait for the draft to land, so absence is measured on a LOADED editor —
      // everything is trivially absent before the fetch resolves.
      await waitFor(() => expect(screen.getByLabelText('Agent name')).toBeTruthy());
    }

    it('renders neither test panel heading', async () => {
      await renderLoadedEditor();
      expect(screen.queryByText('Test agent')).toBeNull();
      expect(screen.queryByText('Test Harness')).toBeNull();
    });

    it('offers no way to run the agent from the editor', async () => {
      await renderLoadedEditor();
      expect(screen.queryByRole('button', { name: 'Run Test' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Run' })).toBeNull();
      expect(screen.queryByLabelText('Sample input')).toBeNull();
      expect(screen.queryByText('Resolved configuration')).toBeNull();
      expect(screen.queryByText('Send a prompt')).toBeNull();
    });

    it('still renders the configuration and publish controls it does own', async () => {
      await renderLoadedEditor();
      // Guards the removal from having taken the surrounding panel with it.
      expect(screen.getByText('Persona')).toBeTruthy();
      expect(screen.getByText('Versions')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
    });
  });
});

/*
 * The Tool Collections empty state.
 *
 * This editor used to declare an `onCreateToolCollection` prop and render a
 * "+ Create a tool collection" button behind it — but NEITHER host ever passed the
 * prop (`CenterEditor` and `WorkspaceView` both omit it), so the button never
 * rendered and the state stayed the dead end its own comment claimed to have fixed.
 * The prop and button are gone; the empty state now names where collections are
 * actually made. Creating one from inside an unsaved agent draft would need a
 * save-first or modal flow that does not exist, so a button here would either lose
 * the draft or be dead again.
 */
describe('SpecialtyAgentEditor — Tool Collections empty state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetchModels.mockResolvedValue([{ id: 'model-a', name: 'Model A' }]);
    mocks.listAssetVersions.mockResolvedValue([]);
    mocks.useLabContext.mockReturnValue(labContextValue({ toolCollections: [] }));
  });

  it('says what a collection is and where to create one', () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

    expect(screen.getByText(/No tool collections yet/i)).toBeTruthy();
    // The pointer is the whole affordance now, so it has to be present.
    expect(screen.getByText(/Settings . Tools/i)).toBeTruthy();
  });

  it('offers no create button, so the path cannot be dead again', () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

    expect(screen.queryByRole('button', { name: /Create a tool collection/i })).toBeNull();
  });
});
