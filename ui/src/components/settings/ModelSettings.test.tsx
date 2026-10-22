// @vitest-environment jsdom
//
// Settings → Model must render as fast as `/models` answers.
//
// The regression this guards: `loading` is cleared by a `.finally()` that waits
// for the `.then()` callback's promise, so ONE `await` on a runtime call inside
// that callback holds the spinner for the entire round trip. Measured in sandbox:
// `/models` returned in 92ms while the awaited `setAgentModel` took 9,972ms on a
// cold agent session — the tab appeared ~100x slower than the data it needed.
//
// `setAgentModel` here is a reconciliation write (the runtime keeps the selected
// model in memory and loses it on restart), so it belongs off the render path
// entirely. These tests pin that by making it never settle.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  fetchModelCatalog: vi.fn(),
  setAgentModel: vi.fn(),
}));

vi.mock('../../api', () => ({
  fetchModelCatalog: api.fetchModelCatalog,
  setAgentModel: api.setAgentModel,
}));

import ModelSettings from './ModelSettings';

// Order matters: `/models` is ordered by ListInferenceProfiles, which really does
// return Claude 3 Haiku first. Position 0 being the crippled model is the whole
// point of these tests.
const HAIKU3 = { id: 'us.anthropic.claude-3-haiku-20240307-v1:0', name: 'US Anthropic Claude 3 Haiku' };
const MODELS = [
  HAIKU3,
  { id: 'us.anthropic.claude-sonnet-5', name: 'US Anthropic Claude Sonnet 5' },
  { id: 'us.anthropic.claude-opus-5', name: 'US Anthropic Claude Opus 5' },
];
const DEFAULT_ID = 'us.anthropic.claude-sonnet-5';

beforeEach(() => {
  localStorage.clear();
  api.fetchModelCatalog.mockResolvedValue({ models: MODELS, defaultModelId: DEFAULT_ID });
  api.setAgentModel.mockResolvedValue({ model_id: DEFAULT_ID });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ModelSettings load path', () => {
  it('renders the list without waiting for the runtime write', async () => {
    // A saved-and-valid id is what triggers the re-apply.
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    // The runtime never answers — a cold session, or one that is simply down.
    api.setAgentModel.mockImplementation(() => new Promise(() => {}));

    render(<ModelSettings />);

    // The models must appear anyway. Before the fix this timed out, because the
    // spinner was gated on the pending write.
    expect(await screen.findByText('US Anthropic Claude Sonnet 5')).toBeTruthy();
    await waitFor(() => expect(screen.queryByText(/Loading available models/i)).toBeNull());

    // And the reconciliation was still attempted — it is fire-and-forget, not
    // removed.
    expect(api.setAgentModel).toHaveBeenCalledWith(DEFAULT_ID);
  });

  it('does not surface a failed reconciliation as a load error', async () => {
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    api.setAgentModel.mockRejectedValue(new Error('runtime unavailable'));

    render(<ModelSettings />);

    expect(await screen.findByText('US Anthropic Claude Sonnet 5')).toBeTruthy();
    // The user's selection lives in localStorage and is re-applied on every
    // change, so a failed re-apply self-heals and must not read as broken.
    expect(screen.queryByText(/runtime unavailable/i)).toBeNull();
  });

  it('still reports a genuine models-fetch failure', async () => {
    api.fetchModelCatalog.mockRejectedValue(new Error('Models request failed: 502'));
    render(<ModelSettings />);
    expect(await screen.findByText(/Models request failed: 502/)).toBeTruthy();
    expect(screen.queryByText('No models are available to this environment.')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '✕' }));
    expect(screen.queryByText('No models are available to this environment.')).toBeNull();
  });

  it('shows an empty catalog only after a successful response', async () => {
    api.fetchModelCatalog.mockResolvedValue({ models: [] });
    render(<ModelSettings />);
    expect(await screen.findByText('No models are available to this environment.')).toBeTruthy();
  });

  it('adopts the PLATFORM default, not list position, when nothing is saved', async () => {
    // The incident: `/models` returns Claude 3 Haiku first (4096 output). Taking
    // `models[0]` meant opening this tab on a fresh browser pinned the
    // orchestrator to it, and every chat turn then failed with
    // `ValidationException ... exceeds the model limit of 4096`.
    render(<ModelSettings />);
    await screen.findByText('US Anthropic Claude Sonnet 5');

    expect(localStorage.getItem('lab-orchestrator-model-id')).toBe(DEFAULT_ID);
    expect(localStorage.getItem('lab-orchestrator-model-id')).not.toBe(HAIKU3.id);
    // No saved id ⇒ no re-apply; the default is announced via the event instead.
    expect(api.setAgentModel).not.toHaveBeenCalled();
  });

  it('persists NOTHING when the proxy reports no default', async () => {
    // Older proxy. Showing a value is fine; writing a guess is what caused the
    // incident, so the runtime keeps its own default instead.
    api.fetchModelCatalog.mockResolvedValue({ models: MODELS });
    render(<ModelSettings />);
    await screen.findByText('US Anthropic Claude 3 Haiku');

    expect(localStorage.getItem('lab-orchestrator-model-id')).toBeNull();
    expect(api.setAgentModel).not.toHaveBeenCalled();
  });

  it('ignores a reported default that is not in the list', async () => {
    api.fetchModelCatalog.mockResolvedValue({ models: MODELS, defaultModelId: 'us.retired.model' });
    render(<ModelSettings />);
    await screen.findByText('US Anthropic Claude 3 Haiku');
    expect(localStorage.getItem('lab-orchestrator-model-id')).toBeNull();
  });

  it('awaits the write when the USER picks a model', async () => {
    // The opposite of the load path: here the user acted, so blocking and
    // surfacing failure is correct. Only the mount-time reconciliation is
    // fire-and-forget.
    //
    // Picks Opus 5 — NOT `MODELS[1]`, which is the saved/current model. The row list
    // offers no `Use` on the model already in use, where the old `<select>` would
    // happily "change" to the value it was already showing and fire a pointless
    // runtime write.
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    render(<ModelSettings />);
    await screen.findByText('US Anthropic Claude Opus 5');

    api.setAgentModel.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Use US Anthropic Claude Opus 5' }));

    await waitFor(() => expect(api.setAgentModel).toHaveBeenCalledWith('us.anthropic.claude-opus-5'));
  });

  it('offers no Use control on the model already in use', async () => {
    // The current row is identified by its badge, not by a button that would re-apply
    // what is already applied.
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    render(<ModelSettings />);
    await screen.findByText('US Anthropic Claude Sonnet 5');

    expect(screen.getByText('Current')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Use US Anthropic Claude Sonnet 5' })).toBeNull();
    // The others remain selectable.
    expect(screen.getByRole('button', { name: 'Use US Anthropic Claude Opus 5' })).toBeTruthy();
  });

  it('shows the model in use in its own card, never filtered out by the search', async () => {
    // 52 profiles ship in sandbox, so "what is running?" must not require scrolling —
    // and a search must not hide it (the "opus" query that left every row saying Use).
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    render(<ModelSettings />);
    const card = await screen.findByTestId('current-model');
    expect(within(card).getByText('US Anthropic Claude Sonnet 5')).toBeTruthy();
    expect(within(card).getByText(DEFAULT_ID)).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Search models'), { target: { value: 'opus' } });
    expect(within(screen.getByTestId('current-model')).getByText('US Anthropic Claude Sonnet 5')).toBeTruthy();
    expect(screen.getByText('US Anthropic Claude Opus 5')).toBeTruthy();
    expect(screen.queryByText('US Anthropic Claude 3 Haiku')).toBeNull();

    // By id, too — an operator debugging a model-specific error has the id, not the
    // display name.
    fireEvent.change(screen.getByLabelText('Search models'), { target: { value: 'claude-3-haiku' } });
    expect(screen.getByText('US Anthropic Claude 3 Haiku')).toBeTruthy();
    expect(screen.queryByText('US Anthropic Claude Opus 5')).toBeNull();
  });

  it('does not repeat the current model in the list, and counts only the filtered rows', async () => {
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    render(<ModelSettings />);
    await screen.findByTestId('current-model');
    expect(screen.getAllByText('US Anthropic Claude Sonnet 5')).toHaveLength(1);
    expect(screen.getByText(/Other models · 2/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Search models'), { target: { value: 'opus' } });
    expect(screen.getByText(/Other models · 1/)).toBeTruthy();
  });

  it('puts the effort control inside the current card, gated on supportsEffort', async () => {
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    api.fetchModelCatalog.mockResolvedValue({
      models: [HAIKU3, { ...MODELS[1], supportsEffort: true }, MODELS[2]], defaultModelId: DEFAULT_ID,
    });
    render(<ModelSettings />);
    const card = await screen.findByTestId('current-model');
    expect(within(card).getByRole('radiogroup', { name: 'Reasoning effort' })).toBeTruthy();
    expect(within(card).queryByTestId('effort-unsupported')).toBeNull();
  });

  it('says effort is not adjustable when the current model does not support it', async () => {
    localStorage.setItem('lab-orchestrator-model-id', DEFAULT_ID);
    render(<ModelSettings />);
    const card = await screen.findByTestId('current-model');
    expect(within(card).queryByRole('radiogroup')).toBeNull();
    expect(within(card).getByTestId('effort-unsupported')).toHaveTextContent(/isn't adjustable/);
  });
});
