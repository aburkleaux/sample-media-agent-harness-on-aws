/**
 * `/open?key=artifacts/...` — a stable link to one artifact (for Slack and other external links, where a
 * presigned URL would expire). `main.tsx` renders it under `AuthGate`, so a signed-out visitor signs in and comes
 * back to this same URL (`signinRedirect(returnTo)` / `safeReturnTo`). Once signed in it resolves the key to a
 * fresh presigned URL and replaces the location with it, so Back does not return to this page.
 *
 *   * A missing or malformed key (not under `artifacts/`, `..`, a backslash) shows a themed error and makes no
 *     request.
 *   * A key the caller cannot read, or that no longer exists, shows the no-access message.
 */
import React, { useEffect, useState } from 'react';
import { theme } from '../styles/tokens';
import WarningIcon from '../components/icons/WarningIcon';
import { isArtifactKey, resolveArtifactUrls } from '../artifacts/artifactUrls';

export const OPEN_INVALID_MESSAGE = 'This link is not valid. It must name a file under artifacts/.';
export const OPEN_NO_ACCESS_MESSAGE = 'You do not have access to this file or it no longer exists';

export interface OpenArtifactRouteProps {
  /** Test seam: where to go. Defaults to `window.location.replace`. */
  replace?: (url: string) => void;
  /** Test seam: the query string. Defaults to `window.location.search`. */
  search?: string;
}

type Phase = 'resolving' | 'invalid' | 'no_access' | 'redirecting';

/** The `key` query parameter when it names one artifact, else null. */
export function artifactKeyFromSearch(search: string): string | null {
  const key = new URLSearchParams(search).get('key');
  return key && isArtifactKey(key) ? key : null;
}

export default function OpenArtifactRoute({ replace, search }: OpenArtifactRouteProps) {
  const query = search ?? window.location.search;
  const key = artifactKeyFromSearch(query);
  const [phase, setPhase] = useState<Phase>(key ? 'resolving' : 'invalid');

  useEffect(() => {
    if (!key) return;
    let live = true;
    const go = replace ?? ((url: string) => window.location.replace(url));
    resolveArtifactUrls([key])
      .then((map) => {
        if (!live) return;
        const url = map.get(key)?.url;
        if (!url) {
          setPhase('no_access');
          return;
        }
        setPhase('redirecting');
        go(url);
      })
      .catch(() => {
        if (live) setPhase('no_access');
      });
    return () => {
      live = false;
    };
  }, [key, replace]);

  const fileName = key ? key.split('/').pop() : '';

  return (
    <main style={pageStyle} data-testid="open-artifact">
      {phase === 'resolving' || phase === 'redirecting' ? (
        <p style={bodyStyle} role="status">{`Opening ${fileName}…`}</p>
      ) : (
        <div style={cardStyle} role="alert">
          <span style={iconStyle}><WarningIcon size={20} /></span>
          <p style={titleStyle}>Can't open this file</p>
          <p style={bodyStyle}>{phase === 'invalid' ? OPEN_INVALID_MESSAGE : OPEN_NO_ACCESS_MESSAGE}</p>
          <a href="/" style={linkStyle}>Go to the app</a>
        </div>
      )}
    </main>
  );
}

const pageStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 24,
  background: theme.colors.background,
  color: theme.colors.textPrimary,
  fontFamily: theme.typography.fontFamily,
};
const cardStyle: React.CSSProperties = {
  maxWidth: 440,
  padding: '24px 28px',
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  textAlign: 'center',
  gap: 8,
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.subtle,
};
const iconStyle: React.CSSProperties = { display: 'inline-flex', color: theme.colors.warning };
const titleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-md)',
  fontWeight: theme.typography.weights.semibold,
};
const bodyStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary,
  lineHeight: 1.5,
};
const linkStyle: React.CSSProperties = { fontSize: 'var(--fs-sm)', color: theme.colors.primary };
