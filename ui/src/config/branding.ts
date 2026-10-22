/**
 * Branding configuration — dynamically swap logo and brand name across
 * the app (splash page, nav bar, sidebar) via a single config.
 *
 * Logo selection is determined at runtime from GET /config (`logoKey`),
 * falling back to VITE_LOGO_KEY (dev only), then 'default'.
 *
 * To add a new logo variant:
 *   1. Place your logo in `ui/public/` (e.g., `logo-alt.png`)
 *   2. Add an entry to LOGO_OPTIONS with a unique key
 *   3. Set LOGO_KEY=<key> on the ECS task (deploy) or VITE_LOGO_KEY=<key> locally
 */
import { loadRuntimeConfig } from './runtimeConfig';

export interface LogoOption {
  key: string;
  src: string;           // Path relative to public/ root
  alt: string;
  borderRadius?: number; // px — 0 for square, 6 for rounded, 9999 for circle
}

/**
 * All available logo options. Add new variants here.
 */
export const LOGO_OPTIONS: Record<string, LogoOption> = {
  default: {
    key: 'default',
    src: '/logo.jpg',
    alt: 'Media Agent Harness',
    borderRadius: 6,
  },
  network: {
    key: 'network',
    src: '/logo-network.jpg',
    alt: 'Media Agent Harness',
    borderRadius: 8,
  },
};

/**
 * Which logo is active. Resolved at runtime:
 *   1. From GET /config `logoKey` field (set via LOGO_KEY env on ECS task)
 *   2. From VITE_LOGO_KEY (dev-only, baked at build time)
 *   3. Fallback: 'default'
 *
 * 'random' picks a random option on each page load.
 */
const envLogoKey = typeof import.meta !== 'undefined'
  ? (import.meta as any).env?.VITE_LOGO_KEY
  : undefined;

function pickLogoKey(key?: string): string {
  const resolved = key || envLogoKey || 'default';
  if (resolved === 'random') {
    const keys = Object.keys(LOGO_OPTIONS);
    return keys[Math.floor(Math.random() * keys.length)];
  }
  return resolved;
}

export let ACTIVE_LOGO_KEY: string = pickLogoKey();

let _brandName: string = (typeof import.meta !== 'undefined'
  ? (import.meta as any).env?.VITE_BRAND_NAME
  : undefined) || 'Media Agent Harness';

/**
 * Initialize branding from the /config endpoint (runtime).
 * Called once at app startup. If /config provides logoKey or brandName,
 * they override the build-time defaults.
 */
export async function initBrandingFromConfig(): Promise<void> {
  try {
    const cfg = (await loadRuntimeConfig()) as { logoKey?: string; brandName?: string };
    if (cfg.logoKey) {
      ACTIVE_LOGO_KEY = pickLogoKey(cfg.logoKey);
    }
    if (cfg.brandName) {
      _brandName = cfg.brandName;
    }
    // index.html's static <title> carries the default name; keep the tab in
    // step with a deployment's brand override.
    if (typeof document !== 'undefined') document.title = _brandName;
  } catch { /* fall through — use defaults */ }
}

/**
 * Switch the active logo at runtime (e.g., from a settings panel).
 */
export function setActiveLogo(key: string): void {
  if (LOGO_OPTIONS[key]) {
    ACTIVE_LOGO_KEY = key;
  }
}

/**
 * Get the active logo config.
 */
export function getActiveLogo(): LogoOption {
  return LOGO_OPTIONS[ACTIVE_LOGO_KEY] ?? LOGO_OPTIONS.default;
}

/**
 * Brand name displayed next to the logo.
 */
export const BRAND_NAME: string = _brandName;

export function getBrandName(): string {
  return _brandName;
}
