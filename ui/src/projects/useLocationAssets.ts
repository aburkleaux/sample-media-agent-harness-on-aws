/**
 * Which project assets a list of shown URLs are (a Renderer image gallery): one `resolve-locations` call per set
 * of URLs, cached per project + URL, so a gallery can say where its images are filed. `undefined` = still asking;
 * a URL that is no asset of the project maps to `null`.
 */
import { useEffect, useMemo, useState } from 'react';
import { resolveLocations } from './api';

const cache = new Map<string, string | null>();
const key = (projectId: string, url: string) => `${projectId}\u0000${url}`;

export function useLocationAssets(projectId: string | null | undefined, urls: readonly string[]): Record<string, string | null> | undefined {
  const sig = urls.join('\n');
  const [, bump] = useState(0);
  useEffect(() => {
    if (!projectId || urls.length === 0) return;
    const missing = urls.filter((u) => !cache.has(key(projectId, u))).slice(0, 100);
    if (missing.length === 0) return;
    let live = true;
    void resolveLocations(projectId, missing).then((r) => {
      if (!r.ok) return; // not cached: asked again next time the gallery shows
      for (const u of missing) cache.set(key(projectId, u), r.value[u] ?? null);
      if (live) bump((n) => n + 1);
    });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, sig]);
  return useMemo(() => {
    if (!projectId) return undefined;
    const out: Record<string, string | null> = {};
    for (const u of urls) {
      const k = key(projectId, u);
      if (!cache.has(k)) return undefined;
      out[u] = cache.get(k)!;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, sig, cache.size]);
}

/** Test seam. */
export function _resetLocationAssets(): void { cache.clear(); }
