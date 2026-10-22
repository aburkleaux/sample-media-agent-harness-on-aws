// @vitest-environment jsdom
//
// The harness toolbar, and the absence of run history.
//
// History was removed because it could not work as written: `HarnessRunRecord`
// declared `draftRef`, `subject`, `outputs`, `logs` and `durationMs`, while the
// server's `list_runs` returns only `runId`, `targetAssetId`, `status`, `startedAt`
// and `endedAt`. `labAction` casts without validating, so TypeScript asserted a shape
// that never arrived and the rows crashed the app on `run.draftRef.category` — a
// render-time TypeError with no error boundary above it, which unmounts the tree and
// leaves a blank pane. It was invisible until the first run record existed, because
// until then the list was empty.
//
// So this asserts the absence, not just the current layout: re-adding the control
// without fixing that contract reintroduces a crash, not a missing feature.
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  runHarness: vi.fn(),
  cancelHarnessRun: vi.fn(),
}));
vi.mock('../lab-api', () => api);

const ctx = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../LabContext', () => ({ useLabContext: () => ctx.value }));

import TestHarnessPanel from './TestHarnessPanel';

beforeEach(() => {
  vi.clearAllMocks();
  ctx.value = {
    harnessTarget: { category: 'skill', assetId: 'skill-1' },
    specialtyAgents: [],
    skills: [{ assetId: 'skill-1', name: 'wip-skill' }],
    toolCollections: [],
    publishedSkills: [],
    publishedSpecialtyAgents: [],
    ensureCategoryLoaded: vi.fn().mockResolvedValue(undefined),
    setRightPanelState: vi.fn(),
  };
});

afterEach(cleanup);

describe('TestHarnessPanel toolbar', () => {
  it('offers no run history', () => {
    render(<TestHarnessPanel onClose={() => {}} />);
    expect(screen.queryByRole('button', { name: 'History' })).toBeNull();
  });

  it('puts the way out and Run on one row, with no second exit', () => {
    render(<TestHarnessPanel onClose={() => {}} />);

    const back = screen.getByRole('button', { name: '← Back' });
    const run = screen.getByRole('button', { name: 'Run' });
    expect(back.parentElement).toBe(run.parentElement);
    // The panel used to carry a ✕ as well, a few pixels from a host-supplied
    // "← Back to Skills" — two exits from one form.
    expect(screen.queryByRole('button', { name: 'Close panel' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Back to/ })).toBeNull();
  });

  it('lets a host name where Back goes', () => {
    // Settings hosts this panel too, where a bare "← Back" would not say where it
    // lands.
    render(<TestHarnessPanel onClose={() => {}} backLabel="← Back to Tools" />);
    expect(screen.getByRole('button', { name: '← Back to Tools' })).toBeTruthy();
  });

  /** The Target Draft field, scoped so the binding dropdowns' options — which list
   *  the same names — cannot satisfy an assertion about the target. */
  const targetField = () =>
    screen.getByText('Target Draft').parentElement as HTMLElement;

  it('names the target by name, not by id', () => {
    render(<TestHarnessPanel onClose={() => {}} />);
    expect(targetField().textContent).toContain('wip-skill');
    expect(targetField().textContent).not.toContain('skill-1');
  });

  it('names a published target too', () => {
    // Publishing consumes the working copy, so for a published skill the drafts pool
    // cannot contain it — the field showed a bare UUID for every such run.
    ctx.value.skills = [];
    ctx.value.publishedSkills = [
      { assetId: 'skill-1', name: 'shipped-skill', version: 2, current: true },
    ];
    render(<TestHarnessPanel onClose={() => {}} />);
    expect(targetField().textContent).toContain('shipped-skill');
    expect(targetField().textContent).not.toContain('skill-1');
  });

  it('will not run without sample input', () => {
    render(<TestHarnessPanel onClose={() => {}} />);
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled();
    expect(screen.getByText(/Sample input is required/)).toBeTruthy();
  });
});
