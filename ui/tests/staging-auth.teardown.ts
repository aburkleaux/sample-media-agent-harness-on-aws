import { expect, test as teardown } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { stagingLogin, CREATED_SESSIONS_PATH } from './capability-audit.helpers';

// Delete ONLY the sessions this capability-audit run actually created, recorded by
// `attachSessionRecorder` (intercepting POST /sessions) and reset at suite start by the setup.
//
// The previous version deleted every session with `createdAt >= suiteStartedAt` — a time window
// that also swept up REAL user sessions created (under the same Cognito identity) during the run,
// which is why users saw their chat sessions disappear without deleting them. Scoping deletion to
// the exact recorded sessionIds makes that impossible: anything not created by this run is left
// untouched, no matter when it was created.
teardown('remove only the sessions this capability audit run created', async ({ page }) => {
  if (!existsSync(CREATED_SESSIONS_PATH)) return;

  const createdIds = Array.from(new Set(
    readFileSync(CREATED_SESSIONS_PATH, 'utf8')
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
  ));
  if (createdIds.length === 0) return;

  await stagingLogin(page);

  const result = await page.evaluate(async (ids) => {
    let token = '';
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith('oidc.user:')) continue;
      const raw = sessionStorage.getItem(key);
      if (!raw) continue;
      try { token = JSON.parse(raw).access_token ?? ''; } catch { /* continue */ }
      if (token) break;
    }
    if (!token) throw new Error('No Cognito access token found for session cleanup');
    const headers = { Authorization: `Bearer ${token}` };
    const idset = new Set(ids);

    const list = async () => {
      const response = await fetch('/sessions', { headers, cache: 'no-store' });
      if (!response.ok) throw new Error(`Session list failed: ${response.status}`);
      return (await response.json()).sessions as Array<{ sessionId: string }>;
    };

    // Only sessions this run created AND that still exist. Real user sessions (not in idset)
    // are never deleted, regardless of when they were created.
    const toDelete = (await list()).filter((s) => idset.has(s.sessionId));
    await Promise.all(toDelete.map(async (s) => {
      const response = await fetch(`/sessions/${encodeURIComponent(s.sessionId)}`, {
        method: 'DELETE',
        headers,
      });
      if (!response.ok) throw new Error(`Session cleanup failed: ${response.status}`);
    }));

    const remaining = (await list()).filter((s) => idset.has(s.sessionId));
    return { requested: ids.length, deleted: toDelete.length, remaining: remaining.map((s) => s.sessionId) };
  }, createdIds);

  expect(
    result.remaining,
    `audit cleanup left its OWN sessions behind after deleting ${result.deleted} of ${result.requested}`,
  ).toEqual([]);
});
