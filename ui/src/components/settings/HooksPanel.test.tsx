// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const hooksResult = {
  userHooks: [{
    assetId: 'user-1', name: 'notify-errors', event: 'after_tool_result', condition: 'result.error',
    action: { type: 'notify' as const, params: {} }, priority: 100, enabled: false, builtin: false,
  }],
  builtinHooks: [{
    assetId: 'builtin-render', name: 'render-artifacts', event: 'after_tool_result', condition: 'result.urls',
    action: { type: 'render' as const, params: {} }, priority: 10, enabled: true, builtin: true,
  }],
};

const api = vi.hoisted(() => ({
  listHooks: vi.fn(),
  peekHooks: vi.fn(),
  createHook: vi.fn(),
  updateHook: vi.fn(),
  deleteHook: vi.fn(),
  validateHookCondition: vi.fn(),
}));

vi.mock('../../lab-api', () => api);

import HooksPanel from './HooksPanel';

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  api.peekHooks.mockReturnValue(hooksResult);
  api.listHooks.mockResolvedValue(hooksResult);
  api.updateHook.mockResolvedValue({ updated: true });
  api.createHook.mockResolvedValue({ assetId: 'override-1', s3Prefix: '' });
  api.deleteHook.mockResolvedValue({ deleted: true });
});

describe('HooksPanel', () => {
  it('renders capability-aligned controls immediately and keeps built-ins off by default', async () => {
    render(<HooksPanel />);

    expect(screen.getByLabelText('Search hooks')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Create hook' })).toBeTruthy();
    expect(screen.getByRole('switch', { name: 'Built-in hooks' }).getAttribute('aria-checked')).toBe('false');
    expect(screen.queryByText('render-artifacts')).toBeNull();

    fireEvent.click(screen.getByRole('switch', { name: 'Built-in hooks' }));
    expect(await screen.findByText('render-artifacts')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Search hooks'), { target: { value: 'missing' } });
    expect(screen.queryByText('notify-errors')).toBeNull();
    expect(screen.getByText('No custom hooks match your search.')).toBeTruthy();
  });

  it('optimistically enables a disabled custom hook and persists the change', async () => {
    render(<HooksPanel />);
    const enable = screen.getByRole('switch', { name: 'Enable notify-errors' });
    expect(enable.getAttribute('aria-checked')).toBe('false');

    fireEvent.click(enable);
    expect(screen.getByRole('switch', { name: 'Disable notify-errors' }).getAttribute('aria-checked')).toBe('true');
    await waitFor(() => expect(api.updateHook).toHaveBeenCalledWith('user-1', { enabled: true }));
  });

  it('can disable and then re-enable a built-in hook through its override', async () => {
    render(<HooksPanel />);
    fireEvent.click(screen.getByRole('switch', { name: 'Built-in hooks' }));
    fireEvent.click(await screen.findByRole('switch', { name: 'Disable render-artifacts' }));

    await waitFor(() => expect(api.createHook).toHaveBeenCalledWith(expect.objectContaining({
      name: 'render-artifacts', enabled: false,
    })));
    const reenable = await screen.findByRole('switch', { name: 'Enable render-artifacts' });
    fireEvent.click(reenable);
    await waitFor(() => expect(api.deleteHook).toHaveBeenCalledWith('override-1'));
    expect(screen.getByRole('switch', { name: 'Disable render-artifacts' }).getAttribute('aria-checked')).toBe('true');
  });

  it('does not let a slow inventory response overwrite a local enable', async () => {
    let resolveInventory!: (value: typeof hooksResult) => void;
    api.listHooks.mockReturnValue(new Promise(resolve => { resolveInventory = resolve; }));
    render(<HooksPanel />);

    fireEvent.click(screen.getByRole('switch', { name: 'Enable notify-errors' }));
    expect(screen.getByRole('switch', { name: 'Disable notify-errors' }).getAttribute('aria-checked')).toBe('true');

    await act(async () => { resolveInventory(hooksResult); });
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Disable notify-errors' }).getAttribute('aria-checked')).toBe('true'));
  });
});
