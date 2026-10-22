/**
 * @vitest-environment jsdom
 *
 * Guards the localStorage hardening: sign-in must survive a FULL store (the reported
 * QuotaExceededError that bricked login), and session caches must stay bounded.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  safeSetItem,
  setSessionCache,
  quotaSafeLocalStorage,
  pruneLocalStorageForSpace,
  MAX_SESSION_CACHES,
  SESSION_CACHE_PREFIX,
} from './safeStorage';

/** A localStorage stub that enforces a byte budget (throws QuotaExceededError when over). */
function installQuotaStorage(budget: number) {
  const map = new Map<string, string>();
  const size = () => [...map].reduce((n, [k, v]) => n + k.length + v.length, 0);
  const store: Storage = {
    get length() { return map.size; },
    clear() { map.clear(); },
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    key: (i: number) => [...map.keys()][i] ?? null,
    removeItem: (k: string) => { map.delete(k); },
    setItem: (k: string, v: string) => {
      const existing = map.get(k)?.length ?? 0;
      if (size() - (map.has(k) ? existing + k.length : 0) + k.length + v.length > budget) {
        throw new DOMException('exceeded quota', 'QuotaExceededError');
      }
      map.set(k, v);
    },
  } as Storage;
  vi.stubGlobal('localStorage', store);
  return map;
}

beforeEach(() => { vi.unstubAllGlobals(); });

describe('setSessionCache — bounded LRU', () => {
  it('evicts the oldest session caches beyond MAX_SESSION_CACHES', () => {
    installQuotaStorage(10_000_000); // ample budget; testing eviction, not quota
    for (let i = 0; i < MAX_SESSION_CACHES + 5; i++) {
      setSessionCache(`s${i}`, `payload-${i}`);
    }
    // The 5 oldest (s0..s4) must have been evicted; the newest survive.
    for (let i = 0; i < 5; i++) {
      expect(localStorage.getItem(SESSION_CACHE_PREFIX + `s${i}`)).toBeNull();
    }
    expect(localStorage.getItem(SESSION_CACHE_PREFIX + `s${MAX_SESSION_CACHES + 4}`)).not.toBeNull();
  });
});

describe('safeSetItem — prune + retry on quota', () => {
  it('prunes stale oidc.* and old session caches, then stores the value', () => {
    installQuotaStorage(400);
    // Fill with abandoned oidc states + old session caches (via raw setItem to bypass the LRU).
    localStorage.setItem('oidc.stale1', 'x'.repeat(80));
    localStorage.setItem('oidc.stale2', 'x'.repeat(80));
    localStorage.setItem('lab-session-lru', JSON.stringify(['old1', 'old2']));
    localStorage.setItem('lab-session-old1', 'y'.repeat(60));
    localStorage.setItem('lab-session-old2', 'y'.repeat(60));
    // Now a new write would exceed 400 bytes — safeSetItem must prune + retry.
    const ok = safeSetItem('oidc.new-signin-state', 'z'.repeat(60));
    expect(ok).toBe(true);
    expect(localStorage.getItem('oidc.new-signin-state')).toBe('z'.repeat(60));
    expect(localStorage.getItem('oidc.stale1')).toBeNull(); // stale oidc pruned
  });
});

describe('quotaSafeLocalStorage — sign-in survives a full store', () => {
  it('writes the OIDC state after pruning instead of throwing (the reported bug)', () => {
    installQuotaStorage(360);
    // Simulate a full store: old session caches + a stale oidc state occupy it.
    localStorage.setItem('lab-session-lru', JSON.stringify(['a', 'b']));
    localStorage.setItem('lab-session-a', 'q'.repeat(90));
    localStorage.setItem('lab-session-b', 'q'.repeat(90));
    localStorage.setItem('oidc.abandoned', 'q'.repeat(60));
    // The OIDC client would do exactly this during createSigninRequest → it must NOT throw.
    expect(() =>
      quotaSafeLocalStorage.setItem('oidc.a5780cd7f9e14fa5b1ccaa8b4ba58ea3', 'w'.repeat(60)),
    ).not.toThrow();
    expect(localStorage.getItem('oidc.a5780cd7f9e14fa5b1ccaa8b4ba58ea3')).toBe('w'.repeat(60));
  });
});

describe('pruneLocalStorageForSpace', () => {
  it('never removes the exceptKey', () => {
    installQuotaStorage(10_000_000);
    localStorage.setItem('oidc.keepme', 'v');
    pruneLocalStorageForSpace('oidc.keepme');
    expect(localStorage.getItem('oidc.keepme')).toBe('v');
  });
});
