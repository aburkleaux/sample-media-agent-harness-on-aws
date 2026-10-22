/**
 * The one `GET /config` load for the whole page.
 *
 * Sign-in (`auth/authManager.ts`), branding, the artifacts domain, upload limits, and the Projects flag all
 * read the same unauthenticated `/config`. They used to fetch it separately (four requests per load), and
 * `lab-api.getConfig` cached a FAILED load as `{artifactsDomain: ''}` for the life of the page, which hid
 * Projects and broke artifact links until a reload.
 *
 * Here: concurrent callers share one in-flight request; a success is cached; a failure is NOT cached, so
 * the next caller retries. Sign-in awaits this before the app renders, so by the time the workspace mounts
 * `peekRuntimeConfig()` already has the answer and env-gated UI (the Projects tab) renders in the same
 * frame as everything else instead of popping in after a second round trip.
 */
export type RuntimeConfig = Record<string, unknown>;

let cached: RuntimeConfig | null = null;
let inflight: Promise<RuntimeConfig> | null = null;

export function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (cached) return Promise.resolve(cached);
  if (!inflight) {
    inflight = (async () => {
      try {
        const res = await fetch('/config');
        if (!res.ok) throw new Error(`Failed to load /config (status ${res.status})`);
        const body = (await res.json()) as unknown;
        cached = body && typeof body === 'object' ? (body as RuntimeConfig) : {};
        return cached;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/** The loaded config, or `null` before the first successful load. Never fetches. */
export function peekRuntimeConfig(): RuntimeConfig | null {
  return cached;
}

/** Test seam: forget the cached config. */
export function __resetRuntimeConfigForTests(): void {
  cached = null;
  inflight = null;
}

/**
 * The release this env runs, from `/config` (`0.9.0` for a release, `0.9.0-rc.870+abc1234` for a staging
 * candidate, `dev` locally). `short` drops the build metadata after `+`. Null until the config has loaded.
 */
export function appVersionOf(cfg: RuntimeConfig | null): { full: string; short: string } | null {
  const v = cfg && typeof cfg.appVersion === 'string' ? cfg.appVersion.trim() : '';
  if (!v) return null;
  return { full: v, short: v.split('+')[0] };
}
