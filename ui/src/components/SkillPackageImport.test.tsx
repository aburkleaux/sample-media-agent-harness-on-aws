// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { validateImportCandidate, type SkillPackageCandidate } from '../utils/skillPackage';

import SkillPackageImport from './SkillPackageImport';

/**
 * This panel is now PRESENTATIONAL. Name state, validation and the import call live in
 * `SkillEditor`, which hosts it inline in the "Upload folder" tab and owns the single primary
 * action at the bottom — so the behaviour that used to be asserted here (seeding the name,
 * performing the import, disabling the submit) is asserted in `SkillEditor.test.tsx` instead.
 *
 * `validation` is built with the REAL `validateImportCandidate` rather than a hand-made object, so
 * these tests still exercise the actual schema rules and cannot drift from them.
 */

const candidate: SkillPackageCandidate = {
  folderName: 'My Folder Skill',
  suggestedName: 'my-folder-skill',
  declaredName: 'old-skill',
  skillMd: `---
name: old-skill
description: Use when testing folder imports.
---

# Folder workflow
`,
  files: [{ path: 'references/guide.md', contentBase64: 'IyBHdWlkZQ==' }],
  filePaths: ['references/guide.md', 'SKILL.md'],
  totalBytes: 120,
  errors: [],
};

function renderPanel(overrides: {
  name?: string;
  existingNames?: string[];
  onNameChange?: (name: string) => void;
  onCancel?: () => void;
  importError?: string | null;
} = {}) {
  const name = overrides.name ?? candidate.suggestedName;
  const validation = validateImportCandidate(candidate, name, overrides.existingNames ?? []);
  const onNameChange = overrides.onNameChange ?? vi.fn();
  const onCancel = overrides.onCancel ?? vi.fn();
  render(
    <SkillPackageImport
      candidate={candidate}
      name={name}
      onNameChange={onNameChange}
      validation={validation}
      onCancel={onCancel}
      importError={overrides.importError ?? null}
    />,
  );
  return { onNameChange, onCancel, validation };
}

afterEach(cleanup);

describe('SkillPackageImport — review panel', () => {
  it('wires each review tab to its panel', () => {
    renderPanel();
    const previewTab = screen.getByRole('tab', { name: 'Preview' });
    const previewPanel = screen.getByRole('tabpanel', { name: 'Preview' });
    expect(previewTab.getAttribute('aria-controls')).toBe(previewPanel.id);
    expect(previewPanel.getAttribute('aria-labelledby')).toBe(previewTab.id);
  });

  it('shows the host-owned name and reports edits upward', () => {
    // Controlled: it renders what it is given and does not keep its own copy, or the footer's
    // import could use a different name than the one on screen.
    const { onNameChange } = renderPanel({ name: 'my-folder-skill' });
    expect(screen.getByLabelText('Imported Skill name')).toHaveValue('my-folder-skill');
    fireEvent.change(screen.getByLabelText('Imported Skill name'), { target: { value: 'custom-name' } });
    expect(onNameChange).toHaveBeenCalledWith('custom-name');
  });

  it('warns that the SKILL.md name will be rewritten', () => {
    renderPanel();
    expect(screen.getByText(/SKILL.md name/)).toBeInTheDocument();
  });

  it('shows the rewritten frontmatter on the Source tab', () => {
    renderPanel({ name: 'custom-name' });
    fireEvent.click(screen.getByRole('tab', { name: 'Source' }));
    expect(screen.getByText(/name: custom-name/)).toBeInTheDocument();
  });

  it('surfaces validation issues and reports NOT ready', () => {
    renderPanel({ existingNames: ['my-folder-skill'] });
    expect(screen.getByRole('alert').textContent).toContain('already exists');
    expect(screen.getByText('Validation required')).toBeInTheDocument();
  });

  it('reports ready when the package is clean', () => {
    renderPanel();
    expect(screen.getByText('Ready to import')).toBeInTheDocument();
  });

  it('surfaces a failed import from the host', () => {
    renderPanel({ importError: 'Failed to import Skill package' });
    expect(screen.getByRole('alert').textContent).toContain('Failed to import');
  });

  it('offers a way to drop the chosen folder', () => {
    const { onCancel } = renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Clear folder' }));
    expect(onCancel).toHaveBeenCalled();
  });

  it('renders NO submit of its own', () => {
    // The host owns the single primary action. Two submits on one screen is the regression this
    // refactor removes, so assert the absence rather than trusting it.
    renderPanel();
    expect(screen.queryByRole('button', { name: /^Import Skill$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Create Skill/ })).not.toBeInTheDocument();
  });
});
