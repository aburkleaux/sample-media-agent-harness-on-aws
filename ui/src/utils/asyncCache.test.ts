import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAsyncCache } from './asyncCache';

afterEach(() => vi.useRealTimers());

describe('createAsyncCache', () => {
  it('coalesces concurrent requests and reuses the cached value', async () => {
    const cache = createAsyncCache<number>(1_000);
    let calls = 0;
    let resolve!: (value: number) => void;
    const loader = () => {
      calls += 1;
      return new Promise<number>(done => { resolve = done; });
    };

    const first = cache.get(loader);
    const second = cache.get(loader);
    expect(calls).toBe(1);
    resolve(42);
    await expect(first).resolves.toBe(42);
    await expect(second).resolves.toBe(42);
    await expect(cache.get(loader)).resolves.toBe(42);
    expect(calls).toBe(1);
  });

  it('refreshes on force, invalidation, and expiry', async () => {
    vi.useFakeTimers();
    const cache = createAsyncCache<number>(100);
    let calls = 0;
    const loader = async () => ++calls;

    await expect(cache.get(loader)).resolves.toBe(1);
    await expect(cache.get(loader, true)).resolves.toBe(2);
    cache.invalidate();
    await expect(cache.get(loader)).resolves.toBe(3);
    vi.advanceTimersByTime(101);
    await expect(cache.get(loader)).resolves.toBe(4);
    vi.useRealTimers();
  });

  it('does not cache failures', async () => {
    const cache = createAsyncCache<number>(1_000);
    let calls = 0;
    const loader = async () => {
      calls += 1;
      if (calls === 1) throw new Error('temporary');
      return 7;
    };

    await expect(cache.get(loader)).rejects.toThrow('temporary');
    await expect(cache.get(loader)).resolves.toBe(7);
    expect(calls).toBe(2);
  });

  it('exposes the last successful value after expiry for stale-while-refresh rendering', async () => {
    vi.useFakeTimers();
    const cache = createAsyncCache<number>(100);
    await cache.get(async () => 9);
    vi.advanceTimersByTime(101);
    expect(cache.peek()).toBe(9);
    vi.useRealTimers();
  });


  it('does not let an older in-flight load overwrite a newer primed mutation', async () => {
    const cache = createAsyncCache<number>(1_000);
    let resolve!: (value: number) => void;
    const pending = cache.get(() => new Promise<number>(done => { resolve = done; }));

    cache.prime(99);
    resolve(1);

    await expect(pending).resolves.toBe(1);
    expect(cache.peek()).toBe(99);
    await expect(cache.get(async () => 2)).resolves.toBe(99);
  });
});
