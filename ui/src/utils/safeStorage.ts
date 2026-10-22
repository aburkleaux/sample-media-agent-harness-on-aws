/**
 * localStorage hardening.
 *
 * Two failure modes this guards against, both observed on staging:
 *  1. A FULL localStorage makes the OIDC client's tiny `oidc.<state>` write throw
 *     `QuotaExceededError`, which bricked sign-in entirely (the app can't even start the
 *     redirect). `quotaSafeLocalStorage` wraps `setItem` so a quota error prunes stale
 *     `oidc.*` states + the oldest session caches and retries, instead of propagating.
 *  2. Per-session transcript caches (`lab-session-<id>`) accumulate one-per-session with no
 *     global bound, so localStorage grows until it's full. `setSessionCache` keeps them within
 *     a bounded LRU and evicts the oldest.
 */

export const SESSION_CACHE_PREFIX = 'lab-session-';
const LRU_KEY = 'lab-session-lru';
/** Per-session render-history cache (`lab-render-<id>`) — same one-per-session growth
 *  as the transcript cache, so it gets the same bounded LRU + prune treatment. */
export const RENDER_CACHE_PREFIX = 'lab-render-';
const RENDER_LRU_KEY = 'lab-render-lru';
/** Keep at most this many session transcript caches; evict the oldest beyond it. */
export const MAX_SESSION_CACHES = 40;

function isQuotaError(e: unknown): boolean {
  return (
    e instanceof DOMException &&
    // name across browsers; code 22 (most) / 1014 (Firefox legacy)
    (e.name === 'QuotaExceededError' ||
      e.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
      e.code === 22 ||
      e.code === 1014)
  );
}

function readLru(lruKey: string = LRU_KEY): string[] {
  try {
    const raw = localStorage.getItem(lruKey);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? (arr as string[]) : [];
  } catch {
    return [];
  }
}

function writeLru(ids: string[], lruKey: string = LRU_KEY): void {
  try {
    localStorage.setItem(lruKey, JSON.stringify(ids));
  } catch {
    /* ignore — the LRU index is best-effort */
  }
}

/**
 * Free space when the store is full: drop abandoned OIDC signin states (transient, they
 * accumulate on abandoned redirects) and the oldest half of the per-session caches — both the
 * transcript (`lab-session-<id>`) and render-history (`lab-render-<id>`) families, the biggest
 * consumers. `exceptKey` is never removed (it's the write we're trying to make room for).
 */
export function pruneLocalStorageForSpace(exceptKey?: string): void {
  try {
    const oidcKeys: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.startsWith('oidc.') && k !== exceptKey) oidcKeys.push(k);
    }
    for (const k of oidcKeys) localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
  for (const [lruKey, prefix] of [
    [LRU_KEY, SESSION_CACHE_PREFIX],
    [RENDER_LRU_KEY, RENDER_CACHE_PREFIX],
  ] as const) {
    try {
      const lru = readLru(lruKey);
      const keep = Math.floor(lru.length / 2);
      for (const id of lru.slice(keep)) {
        if (prefix + id !== exceptKey) localStorage.removeItem(prefix + id);
      }
      writeLru(lru.slice(0, keep), lruKey);
    } catch {
      /* ignore */
    }
  }
}

/** setItem that, on a quota error, prunes and retries once. Returns whether the value is stored. */
export function safeSetItem(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch (e) {
    if (!isQuotaError(e)) return false;
    pruneLocalStorageForSpace(key);
    try {
      localStorage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  }
}

/**
 * Persist a session transcript cache, bounded to an LRU of MAX_SESSION_CACHES. Touching a session
 * moves it to most-recent; sessions beyond the budget have their caches evicted. Quota-safe.
 */
export function setSessionCache(sessionId: string, value: string): boolean {
  let lru = readLru().filter((id) => id !== sessionId);
  lru.unshift(sessionId);
  const evicted = lru.slice(MAX_SESSION_CACHES);
  for (const id of evicted) {
    try {
      localStorage.removeItem(SESSION_CACHE_PREFIX + id);
    } catch {
      /* ignore */
    }
  }
  lru = lru.slice(0, MAX_SESSION_CACHES);
  writeLru(lru);
  return safeSetItem(SESSION_CACHE_PREFIX + sessionId, value);
}

/**
 * Persist a per-session render-history cache (`lab-render-<id>`), bounded to its own LRU of
 * MAX_SESSION_CACHES. Same shape as `setSessionCache` but a separate index, because render caches
 * are the heaviest per-session consumer (clip galleries, artifact URLs) and were previously
 * written one-per-session with NO bound and NO prune — the exact unbounded-growth pattern that
 * filled the quota. renderHistory is an S3-authoritative cache, so evicting the oldest is safe.
 */
export function setRenderCache(sessionId: string, value: string): boolean {
  let lru = readLru(RENDER_LRU_KEY).filter((id) => id !== sessionId);
  lru.unshift(sessionId);
  const evicted = lru.slice(MAX_SESSION_CACHES);
  for (const id of evicted) {
    try {
      localStorage.removeItem(RENDER_CACHE_PREFIX + id);
    } catch {
      /* ignore */
    }
  }
  lru = lru.slice(0, MAX_SESSION_CACHES);
  writeLru(lru, RENDER_LRU_KEY);
  return safeSetItem(RENDER_CACHE_PREFIX + sessionId, value);
}

/**
 * A `Storage`-compatible object whose `setItem` prunes+retries on quota — for the OIDC state
 * store, so a full localStorage can never block sign-in. Kept on localStorage (not sessionStorage)
 * so the silent-renew iframe can still read the state.
 */
export const quotaSafeLocalStorage: Storage = {
  get length() {
    return localStorage.length;
  },
  clear() {
    localStorage.clear();
  },
  getItem(key: string) {
    return localStorage.getItem(key);
  },
  key(index: number) {
    return localStorage.key(index);
  },
  removeItem(key: string) {
    localStorage.removeItem(key);
  },
  setItem(key: string, value: string) {
    if (!safeSetItem(key, value)) {
      // Prune failed to make room; surface the original error to the caller.
      localStorage.setItem(key, value);
    }
  },
};
