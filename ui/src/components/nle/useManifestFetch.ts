/**
 * useManifestFetch — React hook for fetching and parsing Render Manifests
 * from CloudFront/proxy URLs.
 *
 * Features:
 * - 10-second fetch timeout via AbortController
 * - Debounced URL changes (300ms) to handle rapid re-renders
 * - Manual retry via returned `retry` function
 * - Optional `initialManifest` to skip fetching when manifest data is passed directly
 *
 * Fetched through `authFetch`: a `/api/artifact` manifest URL needs the bearer token when the
 * environment's artifacts are private; an absolute CDN URL passes through unchanged.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { parseManifest, type RenderManifest } from './manifestParser';
import { authFetch } from '../../auth/authFetch';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UseManifestFetchOptions {
  /** CloudFront URL to fetch manifest JSON from */
  manifestUrl?: string | null;
  /** Pre-loaded manifest — skips fetching when provided */
  initialManifest?: RenderManifest | null;
}

export interface UseManifestFetchResult {
  manifest: RenderManifest | null;
  loading: boolean;
  error: string | null;
  retry: () => void;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Fetch timeout in milliseconds */
const FETCH_TIMEOUT_MS = 10_000;

/** Debounce delay for URL changes in milliseconds */
const DEBOUNCE_MS = 300;

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useManifestFetch(options: UseManifestFetchOptions): UseManifestFetchResult {
  const { manifestUrl, initialManifest } = options;

  const [manifest, setManifest] = useState<RenderManifest | null>(initialManifest ?? null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Track the current fetch so we can abort on unmount or URL change
  const abortControllerRef = useRef<AbortController | null>(null);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Monotonically increasing ID to discard stale fetch results
  const fetchIdRef = useRef(0);

  /**
   * Core fetch logic — extracted so `retry` can re-invoke it.
   */
  const doFetch = useCallback(async (url: string, id: number) => {
    // Abort any in-flight request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }

    const controller = new AbortController();
    abortControllerRef.current = controller;

    // Set up timeout
    const timeoutId = setTimeout(() => {
      controller.abort();
    }, FETCH_TIMEOUT_MS);

    setLoading(true);
    setError(null);

    try {
      const response = await authFetch(url, { signal: controller.signal });

      // Check if this fetch is still the most recent one
      if (id !== fetchIdRef.current) return;

      if (!response.ok) {
        setError(`Failed to load manifest: HTTP ${response.status}`);
        setManifest(null);
        return;
      }

      const text = await response.text();

      // Check again after reading body
      if (id !== fetchIdRef.current) return;

      const result = parseManifest(text);
      if (result.ok) {
        setManifest(result.manifest);
        setError(null);
      } else {
        const messages = result.errors.map((e) => `${e.path || '(root)'}: ${e.message}`);
        setError(`Invalid manifest: ${messages.join('; ')}`);
        setManifest(null);
      }
    } catch (err: unknown) {
      // Only handle if this is still the active fetch
      if (id !== fetchIdRef.current) return;

      if (err instanceof DOMException && err.name === 'AbortError') {
        setError('Manifest loading timed out');
        setManifest(null);
      } else {
        const message = err instanceof Error ? err.message : 'Unknown fetch error';
        setError(`Failed to load manifest: ${message}`);
        setManifest(null);
      }
    } finally {
      clearTimeout(timeoutId);
      if (id === fetchIdRef.current) {
        setLoading(false);
      }
    }
  }, []);

  /**
   * Trigger a fetch with debouncing for URL changes.
   */
  const triggerFetch = useCallback((url: string) => {
    // Clear any pending debounce
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    debounceTimerRef.current = setTimeout(() => {
      const id = ++fetchIdRef.current;
      doFetch(url, id);
    }, DEBOUNCE_MS);
  }, [doFetch]);

  /**
   * Retry: re-fetch the current URL immediately (no debounce).
   */
  const retry = useCallback(() => {
    if (!manifestUrl) return;

    // Clear any pending debounce
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    const id = ++fetchIdRef.current;
    doFetch(manifestUrl, id);
  }, [manifestUrl, doFetch]);

  // ---------------------------------------------------------------------------
  // Effect: react to URL / initialManifest changes
  // ---------------------------------------------------------------------------

  useEffect(() => {
    // If an initial manifest is provided, use it directly — no fetch needed
    if (initialManifest) {
      setManifest(initialManifest);
      setLoading(false);
      setError(null);
      return;
    }

    // No URL means nothing to fetch
    if (!manifestUrl) {
      setManifest(null);
      setLoading(false);
      setError(null);
      return;
    }

    // Trigger debounced fetch
    triggerFetch(manifestUrl);

    // Cleanup on URL change or unmount
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
  }, [manifestUrl, initialManifest, triggerFetch]);

  return { manifest, loading, error, retry };
}
