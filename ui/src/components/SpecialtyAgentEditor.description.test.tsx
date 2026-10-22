/**
 * @vitest-environment jsdom
 *
 * Tests for the description field in SpecialtyAgentEditor (Task 7).
 * Verifies that:
 *   1. A description input is rendered with the correct aria-label
 *   2. The description is sent to createSpecialtyAgentDraft on save (create mode)
 *   3. The description is loaded from a draft and sent on update (edit mode)
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';

const mocks = vi.hoisted(() => ({
  fetchModels: vi.fn(),
  readSpecialtyAgentDraft: vi.fn(),
  updateSpecialtyAgentDraft: vi.fn(),
  createSpecialtyAgentDraft: vi.fn(),
  renameAsset: vi.fn(),
  publishAsset: vi.fn(),
  listAssetVersions: vi.fn(),
  runHarness: vi.fn(),
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
    ...actual, // keep LabApiError (runtime value) and the domain types
    readSpecialtyAgentDraft: mocks.readSpecialtyAgentDraft,
    updateSpecialtyAgentDraft: mocks.updateSpecialtyAgentDraft,
    createSpecialtyAgentDraft: mocks.createSpecialtyAgentDraft,
    renameAsset: mocks.renameAsset,
    publishAsset: mocks.publishAsset,
    listAssetVersions: mocks.listAssetVersions,
    runHarness: mocks.runHarness,
  };
});

import SpecialtyAgentEditor from './SpecialtyAgentEditor';

const AGENT_ID = 'agent-1';

function labContextValue(overrides: Record<string, unknown> = {}) {
  return {
    skills: [{ assetId: 's1', name: 'Skill One' }],
    toolCollections: [{ assetId: 't1', name: 'Tool One' }],
    refreshCategory: vi.fn().mockResolvedValue(undefined),
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    setHarnessTarget: vi.fn(),
    ...overrides,
  };
}

function draftOnlyAgent(overrides: Record<string, unknown> = {}) {
  return {
    assetId: AGENT_ID,
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
    ...overrides,
  } as never;
}

describe('SpecialtyAgentEditor description', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetchModels.mockResolvedValue([{ id: 'model-a', name: 'Model A' }]);
    mocks.listAssetVersions.mockResolvedValue([]);
    mocks.readSpecialtyAgentDraft.mockResolvedValue(draftOnlyAgent());
    mocks.useLabContext.mockReturnValue(labContextValue());
    mocks.createSpecialtyAgentDraft.mockResolvedValue(undefined);
    mocks.updateSpecialtyAgentDraft.mockResolvedValue(undefined);
  });

  it('renders a description input in create mode', () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);
    expect(screen.getByLabelText('Agent description')).toBeInTheDocument();
  });

  it('sends the description on create', async () => {
    render(<SpecialtyAgentEditor assetId={null} onClose={vi.fn()} />);

    // Fill required fields
    fireEvent.change(screen.getByLabelText('Agent name'), {
      target: { value: 'Transcript Cleaner' },
    });
    fireEvent.change(screen.getByLabelText('Agent description'), {
      target: { value: 'Cleans up ASR transcripts' },
    });
    // Persona is required for save to be enabled
    const personaTextarea = screen.getByPlaceholderText('Enter the agent persona prompt…');
    fireEvent.change(personaTextarea, {
      target: { value: 'You clean transcripts.' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Create Agent' }));

    await waitFor(() => {
      expect(mocks.createSpecialtyAgentDraft).toHaveBeenCalledWith(
        'Transcript Cleaner',
        'You clean transcripts.',
        undefined,
        expect.objectContaining({ description: 'Cleans up ASR transcripts' }),
      );
    });
  });

  it('loads description from draft and sends it on update', async () => {
    // Draft with a description
    mocks.readSpecialtyAgentDraft.mockResolvedValue(
      draftOnlyAgent({ description: 'Existing description' })
    );

    render(<SpecialtyAgentEditor assetId={AGENT_ID} onClose={vi.fn()} />);

    // Wait on the loaded value itself rather than a proxy heading — this used to
    // wait for "Test Harness", which no longer exists (the editor hosts no test
    // surface), and a stale proxy signal fails for a reason unrelated to the
    // assertion.
    const descInput = await screen.findByLabelText('Agent description') as HTMLInputElement;
    await waitFor(() => expect(descInput.value).toBe('Existing description'));

    // Update it
    fireEvent.change(descInput, {
      target: { value: 'Updated description' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(mocks.updateSpecialtyAgentDraft).toHaveBeenCalledWith(
        AGENT_ID,
        expect.objectContaining({ description: 'Updated description' }),
      );
    });
  });
});
