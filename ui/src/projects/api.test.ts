import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../auth/authFetch', () => ({ authFetch: vi.fn() }));
vi.mock('../lab-api', () => ({ getConfig: vi.fn() }));

import { authFetch } from '../auth/authFetch';
import { getConfig } from '../lab-api';
import {
  createProject,
  deleteProject,
  getProject,
  inviteMember,
  leaveProject,
  listProjects,
  parseProjectAccessLost,
  patchSessionProject,
  projectsEnabled,
  removeMember,
  renameProject,
} from './api';

const mockFetch = authFetch as unknown as ReturnType<typeof vi.fn>;
const mockConfig = getConfig as unknown as ReturnType<typeof vi.fn>;

function res(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as unknown as Response;
}

const NOT_FOUND = { error: { code: 'project_not_found', message: 'Project not found' } };
const PID = 'p0123456789abcdef0123456789abcdef';

beforeEach(() => {
  mockFetch.mockReset();
  mockConfig.mockReset();
  mockConfig.mockResolvedValue({ artifactsDomain: '', projectsEnabled: true });
});

describe('projects api: enablement', () => {
  it('is off unless /config.projectsEnabled is exactly true', async () => {
    mockConfig.mockResolvedValueOnce({ artifactsDomain: '' });
    expect(await projectsEnabled()).toBe(false);
    mockConfig.mockResolvedValueOnce({ artifactsDomain: '', projectsEnabled: 'true' });
    expect(await projectsEnabled()).toBe(false);
    mockConfig.mockRejectedValueOnce(new Error('boom'));
    expect(await projectsEnabled()).toBe(false);
    mockConfig.mockResolvedValueOnce({ artifactsDomain: '', projectsEnabled: true });
    expect(await projectsEnabled()).toBe(true);
  });

  it('makes no request from any route when disabled', async () => {
    mockConfig.mockResolvedValue({ artifactsDomain: '', projectsEnabled: false });
    const results = await Promise.all([
      listProjects(),
      createProject('A'),
      getProject(PID),
      renameProject(PID, 'B'),
      deleteProject(PID, 'B'),
      inviteMember(PID, 'a@b.com'),
      removeMember(PID, 'sub-1'),
      leaveProject(PID),
      patchSessionProject('s1', { activeProjectId: PID }),
    ]);
    for (const r of results) expect(r).toEqual({ ok: false, kind: 'disabled' });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('projects api: routes', () => {
  it('GET /projects unwraps the list', async () => {
    const projects = [{ projectId: PID, name: 'Personal', role: 'owner', personal: true }];
    mockFetch.mockResolvedValue(res(200, { projects }));
    expect(await listProjects()).toEqual({ ok: true, value: projects });
    expect(mockFetch).toHaveBeenCalledWith('/projects', expect.objectContaining({ method: 'GET' }));
  });

  it('sends each route its method, path, and allowlisted body', async () => {
    mockFetch.mockResolvedValue(res(200, { project: { projectId: PID }, member: {}, removed: {}, left: {} }));
    await createProject('Trailer cuts');
    await renameProject(PID, 'New');
    await deleteProject(PID, 'New');
    await inviteMember(PID, 'a@b.com');
    await removeMember(PID, 'sub/1');
    await leaveProject(PID);
    await getProject(PID);
    const calls = mockFetch.mock.calls.map(([path, init]) => [path, init.method, init.body]);
    expect(calls).toEqual([
      ['/projects', 'POST', JSON.stringify({ name: 'Trailer cuts' })],
      [`/projects/${PID}`, 'PATCH', JSON.stringify({ name: 'New' })],
      [`/projects/${PID}`, 'DELETE', JSON.stringify({ confirmName: 'New' })],
      [`/projects/${PID}/members`, 'POST', JSON.stringify({ invite: 'a@b.com' })],
      [`/projects/${PID}/members/sub%2F1`, 'DELETE', undefined],
      [`/projects/${PID}/leave`, 'POST', undefined],
      [`/projects/${PID}`, 'GET', undefined],
    ]);
  });

  it('PATCH /sessions/:id sends only the provided Projects keys', async () => {
    mockFetch.mockResolvedValue(res(200, { sessionId: 's 1' }));
    expect(await patchSessionProject('s 1', { activeProjectId: PID })).toEqual({ ok: true, value: undefined });
    await patchSessionProject('s 1', { webSearch: false });
    expect(mockFetch.mock.calls[0]).toEqual([
      '/sessions/s%201',
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ activeProjectId: PID }) }),
    ]);
    expect(mockFetch.mock.calls[1][1].body).toBe(JSON.stringify({ webSearch: false }));
  });
});

describe('projects api: typed failures', () => {
  it('maps the uniform 404 project_not_found to not_found on registry and session routes', async () => {
    mockFetch.mockResolvedValue(res(404, NOT_FOUND));
    expect(await getProject(PID)).toEqual({ ok: false, kind: 'not_found' });
    expect(await patchSessionProject('s1', { activeProjectId: PID })).toEqual({ ok: false, kind: 'not_found' });
  });

  it('maps 404 projects_not_enabled to disabled', async () => {
    mockFetch.mockResolvedValue(res(404, { error: { code: 'projects_not_enabled', message: 'x' } }));
    expect(await listProjects()).toEqual({ ok: false, kind: 'disabled' });
  });

  it('keeps code, message, and extra fields for other registry errors', async () => {
    const error = { code: 'owned_project_limit', limit: 50, message: 'You can own at most 50 Projects' };
    mockFetch.mockResolvedValue(res(400, { error }));
    expect(await createProject('A')).toEqual({
      ok: false, kind: 'error', status: 400, code: 'owned_project_limit', message: error.message, error,
    });
  });

  it('reads the string error of /sessions (423 locked)', async () => {
    mockFetch.mockResolvedValue(res(423, { error: 'Session is locked', locked: true }));
    expect(await patchSessionProject('s1', { activeProjectId: PID })).toEqual({
      ok: false, kind: 'error', status: 423, code: 'session_locked', message: 'Session is locked',
    });
  });

  it('reports a network failure as status 0', async () => {
    mockFetch.mockRejectedValue(new Error('offline'));
    expect(await listProjects()).toMatchObject({ ok: false, kind: 'error', status: 0, code: 'network_error' });
  });

  it('parses the turn gate 409 project_access_lost', () => {
    const body = { error: { code: 'project_access_lost', personalProject: { id: PID, name: 'Personal' } } };
    expect(parseProjectAccessLost(409, body)).toEqual({ id: PID, name: 'Personal' });
    expect(parseProjectAccessLost(400, body)).toBeNull();
    expect(parseProjectAccessLost(409, { error: { code: 'other' } })).toBeNull();
    expect(parseProjectAccessLost(409, { error: { code: 'project_access_lost' } })).toBeNull();
    expect(parseProjectAccessLost(409, null)).toBeNull();
  });

  it('surfaces a 409 project_access_lost as access_lost', async () => {
    const body = { error: { code: 'project_access_lost', personalProject: { id: PID, name: 'Personal' } } };
    mockFetch.mockResolvedValue(res(409, body));
    expect(await patchSessionProject('s1', { webSearch: true })).toEqual({
      ok: false, kind: 'access_lost', personalProject: { id: PID, name: 'Personal' },
    });
  });
});

describe('parseSessionProjectFixed (asset-lineage R2.1)', () => {
  it('reads the structured and the bare 409 body, and nothing else', async () => {
    const { parseSessionProjectFixed } = await import('./api');
    expect(parseSessionProjectFixed(409, { error: { code: 'session_project_fixed', projectId: 'p1' } })).toBe('p1');
    expect(parseSessionProjectFixed(409, { error: { code: 'session_project_fixed' } })).toBe('');
    expect(parseSessionProjectFixed(409, { error: 'session_project_fixed' })).toBe('');
    expect(parseSessionProjectFixed(409, { error: { code: 'project_access_lost' } })).toBeNull();
    expect(parseSessionProjectFixed(400, { error: { code: 'session_project_fixed' } })).toBeNull();
    expect(parseSessionProjectFixed(409, null)).toBeNull();
  });
});

describe('projects api: moves update where every view says a file lives', () => {
  it('a move and a folder rename are recorded in the folder store at once', async () => {
    const { moveAssets, renameFolder } = await import('./api');
    const { peekAssetFolder, noteAssetFolders, _resetAssetFolders } = await import('./assetFolders');
    _resetAssetFolders();
    mockFetch.mockResolvedValueOnce(res(200, { folder: 'Selects', moved: ['video:1'], failed: [] }));
    expect((await moveAssets(PID, ['video:1', 'video:2'], 'Selects')).ok).toBe(true);
    expect(peekAssetFolder(PID, 'video:1')).toBe('Selects');
    expect(peekAssetFolder(PID, 'video:2')).toBeUndefined(); // it did not move
    noteAssetFolders(PID, { 'video:3': 'Selects/Wide' });
    mockFetch.mockResolvedValueOnce(res(200, { moved: 2, remaining: 0 }));
    expect((await renameFolder(PID, 'Selects', 'Picks')).ok).toBe(true);
    expect(peekAssetFolder(PID, 'video:1')).toBe('Picks');
    expect(peekAssetFolder(PID, 'video:3')).toBe('Picks/Wide');
  });
});
