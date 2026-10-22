/**
 * @vitest-environment jsdom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { act, cleanup, renderHook } from '@testing-library/react';

const mocks = vi.hoisted(() => ({ listProjects: vi.fn() }));
vi.mock('./api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api')>()),
  listProjects: mocks.listProjects,
}));

import { __resetProjectsListForTests, refreshProjectsList, useProjectsList } from './useProjectsList';

const A = { projectId: 'a', name: 'A', role: 'owner' as const, personal: true };
const B = { projectId: 'b', name: 'B', role: 'member' as const, personal: false };

async function flush() {
  await act(async () => { for (let i = 0; i < 4; i++) await Promise.resolve(); });
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetProjectsListForTests();
  mocks.listProjects.mockResolvedValue({ ok: true, value: [A] });
});
afterEach(cleanup);

describe('useProjectsList', () => {
  it('stays inert while disabled, then loads once for every subscriber', async () => {
    const off = renderHook(() => useProjectsList({ enabled: false }));
    await flush();
    expect(mocks.listProjects).not.toHaveBeenCalled();
    expect(off.result.current.projects).toBeNull();

    const one = renderHook(() => useProjectsList());
    const two = renderHook(() => useProjectsList());
    await flush();
    expect(mocks.listProjects).toHaveBeenCalledTimes(1);
    expect(one.result.current.projects).toEqual([A]);
    expect(two.result.current.status).toBe('ready');
    // The disabled subscriber sees the shared snapshot too.
    expect(off.result.current.projects).toEqual([A]);
  });

  it('refresh() shares one in-flight request and updates every subscriber', async () => {
    const { result } = renderHook(() => useProjectsList());
    await flush();
    mocks.listProjects.mockResolvedValue({ ok: true, value: [A, B] });
    await act(async () => {
      await Promise.all([result.current.refresh(), refreshProjectsList()]);
    });
    expect(mocks.listProjects).toHaveBeenCalledTimes(2);
    expect(result.current.projects).toEqual([A, B]);
  });

  it('keeps the last good list on an error and stays null when Projects are disabled', async () => {
    const { result } = renderHook(() => useProjectsList());
    await flush();
    mocks.listProjects.mockResolvedValue({ ok: false, kind: 'error', status: 500, code: 'x', message: 'boom' });
    await act(async () => { await result.current.refresh(); });
    expect(result.current.status).toBe('error');
    expect(result.current.projects).toEqual([A]);

    __resetProjectsListForTests();
    mocks.listProjects.mockResolvedValue({ ok: false, kind: 'disabled' });
    await act(async () => { await refreshProjectsList(); });
    expect(result.current.status).toBe('disabled');
    expect(result.current.projects).toBeNull();
  });
});
