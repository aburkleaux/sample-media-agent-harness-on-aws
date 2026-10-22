export interface AsyncCache<T> {
  get(loader: () => Promise<T>, force?: boolean): Promise<T>;
  invalidate(): void;
  prime(value: T): void;
  peek(): T | undefined;
}

/**
 * Small in-memory cache for read-mostly UI inventories.
 *
 * - Coalesces concurrent callers onto one promise (the common settings-header
 *   + browser-child duplication case).
 * - Serves values only for the configured TTL.
 * - Never caches failures.
 * - Explicit refresh still shares an already-running request; this avoids a
 *   second backend call when a user clicks Refresh while the initial load is
 *   still pending.
 */
export function createAsyncCache<T>(ttlMs: number): AsyncCache<T> {
  let value: T | undefined;
  let hasValue = false;
  let expiresAt = 0;
  let inFlight: Promise<T> | null = null;
  let generation = 0;

  return {
    get(loader, force = false) {
      const now = Date.now();
      if (!force && hasValue && now < expiresAt) {
        return Promise.resolve(value as T);
      }
      if (inFlight) return inFlight;

      const requestGeneration = generation;
      inFlight = loader()
        .then(result => {
          if (generation === requestGeneration) {
            value = result;
            hasValue = true;
            expiresAt = Date.now() + ttlMs;
          }
          return result;
        })
        .finally(() => {
          inFlight = null;
        });
      return inFlight;
    },
    invalidate() {
      generation += 1;
      value = undefined;
      hasValue = false;
      expiresAt = 0;
    },
    prime(nextValue) {
      generation += 1;
      value = nextValue;
      hasValue = true;
      expiresAt = Date.now() + ttlMs;
    },
    peek() {
      // Deliberately return the last successful value even after TTL expiry.
      // Callers use this only to keep stale content visible while `get()`
      // performs the normal freshness check and refreshes in the background.
      return hasValue ? value : undefined;
    },
  };
}
