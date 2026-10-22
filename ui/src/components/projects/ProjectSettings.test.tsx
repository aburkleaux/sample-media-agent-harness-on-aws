/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const mocks = vi.hoisted(() => ({
  getProject: vi.fn(),
  updateProjectSettings: vi.fn(),
  enableIndexing: vi.fn(),
  peekGraphEnabled: vi.fn(() => false),
}));
vi.mock('../../projects/api', () => mocks);

import ProjectSettings from './ProjectSettings';
import ProjectCreateForm, { GRAPH_NOTE, INDEXING_NOTE, parseTags } from './ProjectCreateForm';

const PID = 'p' + 'a'.repeat(32);
/** The dialog's button (rendered after the page's own button of the same name). */
const lastButton = (name: string) => { const all = screen.getAllByRole('button', { name }); return all[all.length - 1]; };
const project = { id: PID, name: 'Promo', personal: false };

function meta(over: Record<string, unknown> = {}) {
  return { projectId: PID, name: 'Promo', personal: false, description: 'Launch cuts', tags: ['q4'], indexing: false,
    storedCount: 3, ...over };
}

beforeEach(() => {
  mocks.getProject.mockResolvedValue({ ok: true, value: { project: meta(), role: 'owner', owner: {}, members: [] } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('ProjectSettings', () => {
  it('owner edits description and tags', async () => {
    mocks.updateProjectSettings.mockResolvedValue({ ok: true, value: meta({ description: 'New', tags: ['a', 'b'] }) });
    const onChanged = vi.fn();
    render(<ProjectSettings project={project} role="owner" onNotFound={vi.fn()} onChanged={onChanged} />);
    const desc = await screen.findByLabelText('Description');
    expect(desc).toHaveValue('Launch cuts');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled(); // nothing changed yet
    fireEvent.change(desc, { target: { value: ' New ' } });
    fireEvent.change(screen.getByLabelText('Tags'), { target: { value: 'a, b,' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(mocks.updateProjectSettings).toHaveBeenCalledWith(PID, { description: 'New', tags: ['a', 'b'] }));
    expect(await screen.findByText('Saved')).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it('member sees settings read-only and cannot turn on indexing', async () => {
    render(<ProjectSettings project={project} role="member" onNotFound={vi.fn()} />);
    expect(await screen.findByLabelText('Description')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    const sw = screen.getByRole('switch', { name: 'Turn on indexing' });
    expect(sw).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(sw);
    expect(screen.queryByText(/stored files will be analyzed/)).toBeNull();
    expect(screen.getByText('Only the project owner can turn on indexing.')).toBeInTheDocument();
  });

  it('turning indexing on asks first, names the stored-file count, and reports what was queued', async () => {
    mocks.enableIndexing.mockResolvedValue({ ok: true, value: { indexing: true, changed: true, queued: 3 } });
    render(<ProjectSettings project={project} role="owner" onNotFound={vi.fn()} onChanged={vi.fn()} />);
    expect(await screen.findByTestId('indexing-state')).toHaveTextContent('Off.');
    expect(screen.getByRole('switch', { name: 'Turn on indexing' })).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(screen.getByRole('switch', { name: 'Turn on indexing' }));
    expect(screen.getByText(/3 stored files will be analyzed/)).toBeInTheDocument();
    expect(screen.getByText(/can't be turned off once it is on/)).toBeInTheDocument();
    expect(mocks.enableIndexing).not.toHaveBeenCalled();
    fireEvent.click(lastButton('Turn on indexing'));
    await waitFor(() => expect(mocks.enableIndexing).toHaveBeenCalledWith(PID));
    expect(await screen.findByText('Indexing is on. 3 files queued for analysis.')).toBeInTheDocument();
    expect(screen.getByTestId('indexing-state')).toHaveTextContent('On.');
    // The switch now shows on and is locked: there is no way back to off.
    const sw = screen.getByRole('switch', { name: "Indexing is on and can't be turned off" });
    expect(sw).toHaveAttribute('aria-checked', 'true');
    expect(sw).toHaveAttribute('aria-disabled', 'true');
  });

  it('a project with indexing on offers no way to turn it off', async () => {
    mocks.getProject.mockResolvedValue({ ok: true, value: { project: meta({ indexing: true }), role: 'owner' } });
    render(<ProjectSettings project={project} role="owner" onNotFound={vi.fn()} />);
    expect(await screen.findByTestId('indexing-state')).toHaveTextContent("can't be turned off");
    const sw = screen.getByRole('switch', { name: "Indexing is on and can't be turned off" });
    expect(sw).toHaveAttribute('aria-checked', 'true');
    expect(sw).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(sw);
    expect(screen.queryByText('Turn on indexing?')).toBeNull();
    expect(mocks.enableIndexing).not.toHaveBeenCalled();
  });

  it('shows the server refusal', async () => {
    mocks.enableIndexing.mockResolvedValue({ ok: false, kind: 'error', status: 503, code: 'catalog_missing',
      message: 'the M2C catalog is missing x' });
    render(<ProjectSettings project={project} role="owner" onNotFound={vi.fn()} />);
    fireEvent.click(await screen.findByRole('switch', { name: 'Turn on indexing' }));
    fireEvent.click(lastButton('Turn on indexing'));
    expect(await screen.findByRole('alert')).toHaveTextContent('the M2C catalog is missing x');
    expect(screen.getByTestId('indexing-state')).toHaveTextContent('Off.');
  });
});

describe('ProjectCreateForm', () => {
  it('sends description, tags, and indexing (default off)', async () => {
    const onCreate = vi.fn().mockResolvedValue(null);
    render(<ProjectCreateForm onCreate={onCreate} onCancel={vi.fn()} />);
    expect(screen.getByRole('switch', { name: 'Index files for search' })).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(INDEXING_NOTE)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: ' Promo ' } });
    fireEvent.change(screen.getByLabelText('New project description'), { target: { value: 'Launch cuts' } });
    fireEvent.change(screen.getByLabelText('New project tags'), { target: { value: 'q4, promo' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith('Promo',
      { indexing: false, description: 'Launch cuts', tags: ['q4', 'promo'] }));
  });

  it('can create with indexing on', async () => {
    const onCreate = vi.fn().mockResolvedValue(null);
    render(<ProjectCreateForm onCreate={onCreate} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'Searchable' } });
    fireEvent.click(screen.getByRole('switch', { name: 'Index files for search' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith('Searchable', { indexing: true }));
  });

  it('parseTags splits on commas and drops blanks', () => {
    expect(parseTags(' a , ,b,')).toEqual(['a', 'b']);
  });
});

describe('ProjectCreateForm knowledge graph switch', () => {
  it('is absent (and nothing is sent) where the graph is not deployed', async () => {
    mocks.peekGraphEnabled.mockReturnValue(false);
    const onCreate = vi.fn().mockResolvedValue(null);
    render(<ProjectCreateForm onCreate={onCreate} onCancel={vi.fn()} />);
    expect(screen.queryByRole('switch', { name: 'Build a knowledge graph' })).toBeNull();
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'Plain' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith('Plain', { indexing: false }));
  });

  it('defaults ON where deployed, and can be turned off', async () => {
    mocks.peekGraphEnabled.mockReturnValue(true);
    const onCreate = vi.fn().mockResolvedValue(null);
    render(<ProjectCreateForm onCreate={onCreate} onCancel={vi.fn()} />);
    const sw = screen.getByRole('switch', { name: 'Build a knowledge graph' });
    expect(sw).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText(GRAPH_NOTE)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'Graphed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(onCreate).toHaveBeenLastCalledWith('Graphed', { indexing: false, graph: true }));
    // Wait for the button to return from busy state before toggling the switch and submitting again
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create project' })).toBeInTheDocument());
    fireEvent.click(sw);
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(onCreate).toHaveBeenLastCalledWith('Graphed', { indexing: false, graph: false }));
  });
});
