// @vitest-environment jsdom
import '@testing-library/jest-dom';
import React from 'react';
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../auth/authFetch', () => ({ authFetch: vi.fn() }));
import { authFetch } from '../../auth/authFetch';
import UsersPanel from './UsersPanel';

const mockAuthFetch = authFetch as unknown as ReturnType<typeof vi.fn>;
function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return { ok: init.ok ?? true, status: init.status ?? 200, json: async () => body } as unknown as Response;
}

const DIRECTORY = {
  users: [
    { username: 'joe', email: 'joe@example.com', status: 'CONFIRMED', enabled: true, admin: true },
    { username: 'ann', email: 'ann@example.com', status: 'CONFIRMED', enabled: true, admin: false },
  ],
};

const noop = () => {};

beforeEach(() => {
  mockAuthFetch.mockReset();
  mockAuthFetch.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url === '/admin/users' && method === 'GET') return jsonResponse(DIRECTORY);
    if (/\/admin\/users\/[^/]+\/admin$/.test(url)) return jsonResponse({ ok: true });
    if (/\/admin\/users\/[^/]+\/reset-password$/.test(url) && method === 'POST') return jsonResponse({ ok: true });
    if (/\/admin\/users\/[^/]+\/(disable|enable)$/.test(url) && method === 'POST') return jsonResponse({ ok: true });
    return jsonResponse({}, { ok: false, status: 404 });
  });
});
afterEach(() => cleanup());

describe('UsersPanel', () => {
  it('renders the directory with per-user admin state', async () => {
    render(<UsersPanel onAddUser={noop} />);
    expect(await screen.findByText('joe@example.com')).toBeInTheDocument();
    expect(screen.getByText('ann@example.com')).toBeInTheDocument();
    expect(screen.getByText('Admin ✓')).toBeInTheDocument();   // joe
    expect(screen.getByText('Grant admin')).toBeInTheDocument(); // ann
  });

  it('+ Add user delegates to onAddUser (opens the page)', async () => {
    const onAddUser = vi.fn();
    render(<UsersPanel onAddUser={onAddUser} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getByRole('button', { name: '+ Add user' }));
    expect(onAddUser).toHaveBeenCalledTimes(1);
  });

  it('grants admin (POST /admin/users/:u/admin) for a non-admin', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getByText('Grant admin'));
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith(
      '/admin/users/ann/admin', expect.objectContaining({ method: 'POST' })));
  });

  it('revokes admin (DELETE /admin/users/:u/admin) for an admin', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getByText('Admin ✓'));
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith(
      '/admin/users/joe/admin', expect.objectContaining({ method: 'DELETE' })));
  });

  it('filters the directory by the search box', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.change(screen.getByLabelText('Search users'), { target: { value: 'ann' } });
    expect(screen.queryByText('joe@example.com')).not.toBeInTheDocument();
    expect(screen.getByText('ann@example.com')).toBeInTheDocument();
  });

  it('resets a password (POST /reset-password) and shows a notice', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getAllByText('Reset password')[0]);
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith(
      '/admin/users/joe/reset-password', expect.objectContaining({ method: 'POST' })));
    expect(await screen.findByText(/reset initiated/i)).toBeInTheDocument();
  });

  it('soft-disables (POST /disable) and surfaces an Undo affordance', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getAllByText('Disable')[0]); // joe
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith(
      '/admin/users/joe/disable', expect.objectContaining({ method: 'POST' })));
    expect(await screen.findByText('Undo')).toBeInTheDocument();
  });

  it('Undo re-enables (POST /enable)', async () => {
    render(<UsersPanel onAddUser={noop} />);
    await screen.findByText('joe@example.com');
    fireEvent.click(screen.getAllByText('Disable')[0]);
    fireEvent.click(await screen.findByText('Undo'));
    await waitFor(() => expect(mockAuthFetch).toHaveBeenCalledWith(
      '/admin/users/joe/enable', expect.objectContaining({ method: 'POST' })));
  });

  it('surfaces a clear error when the directory load is forbidden (403)', async () => {
    mockAuthFetch.mockImplementation(async () => jsonResponse({}, { ok: false, status: 403 }));
    render(<UsersPanel onAddUser={noop} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/not an administrator/i);
  });
});
