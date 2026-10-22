import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));
import { authFetch } from '../auth/authFetch';
import { listUsers, addUser, setUserAdmin, resetUserPassword, disableUser, enableUser, deleteUser, AdminApiError } from './adminApi';

const mockAuthFetch = authFetch as unknown as ReturnType<typeof vi.fn>;
function res(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  return { ok: init.ok ?? true, status: init.status ?? 200, json: async () => body } as unknown as Response;
}

beforeEach(() => mockAuthFetch.mockReset());

describe('adminApi service', () => {
  it('listUsers returns the users array via authFetch', async () => {
    mockAuthFetch.mockResolvedValue(res({ users: [{ username: 'a', admin: true }] }));
    const users = await listUsers();
    expect(users).toEqual([{ username: 'a', admin: true }]);
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users', expect.objectContaining({ headers: expect.any(Object) }));
  });

  it('listUsers throws AdminApiError carrying the status on failure', async () => {
    mockAuthFetch.mockResolvedValue(res({}, { ok: false, status: 403 }));
    await expect(listUsers()).rejects.toBeInstanceOf(AdminApiError);
    mockAuthFetch.mockResolvedValue(res({}, { ok: false, status: 403 }));
    await expect(listUsers()).rejects.toMatchObject({ status: 403 });
  });

  it('setUserAdmin POSTs to grant', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await setUserAdmin('joe', true);
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/joe/admin', expect.objectContaining({ method: 'POST' }));
  });

  it('setUserAdmin DELETEs to revoke and URL-encodes the username', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await setUserAdmin('a@b.com', false);
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/a%40b.com/admin', expect.objectContaining({ method: 'DELETE' }));
  });

  it('setUserAdmin throws AdminApiError on failure', async () => {
    mockAuthFetch.mockResolvedValue(res({}, { ok: false, status: 500 }));
    await expect(setUserAdmin('joe', true)).rejects.toBeInstanceOf(AdminApiError);
  });

  it('resetUserPassword POSTs to /reset-password', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await resetUserPassword('joe');
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/joe/reset-password', expect.objectContaining({ method: 'POST' }));
  });

  it('deleteUser DELETEs the bare user path (URL-encoded)', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await deleteUser('a@b.com');
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/a%40b.com', expect.objectContaining({ method: 'DELETE' }));
  });

  it('deleteUser throws AdminApiError on failure', async () => {
    mockAuthFetch.mockResolvedValue(res({}, { ok: false, status: 500 }));
    await expect(deleteUser('joe')).rejects.toBeInstanceOf(AdminApiError);
  });

  it('addUser POSTs /admin/users with email + optional names', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await addUser({ email: 'new@x.com', firstName: 'New', lastName: 'User' });
    const calls = mockAuthFetch.mock.calls;
    const call = calls[calls.length - 1] as [string, RequestInit];
    expect(call[0]).toBe('/admin/users');
    expect((call[1].method ?? '').toUpperCase()).toBe('POST');
    expect(JSON.parse(String(call[1].body))).toMatchObject({ email: 'new@x.com', firstName: 'New', lastName: 'User' });
  });

  it('disableUser POSTs /disable, enableUser POSTs /enable', async () => {
    mockAuthFetch.mockResolvedValue(res({ ok: true }));
    await disableUser('joe');
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/joe/disable', expect.objectContaining({ method: 'POST' }));
    await enableUser('joe');
    expect(mockAuthFetch).toHaveBeenCalledWith('/admin/users/joe/enable', expect.objectContaining({ method: 'POST' }));
  });
});
