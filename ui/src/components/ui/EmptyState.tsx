import React from 'react';
import { theme } from '../../styles/tokens';

/**
 * The dashed "nothing here yet" card.
 *
 * Written because this existed FIVE times: `ActivityPanel`, `CapabilitiesSettings`,
 * `nle/InspectorPanel`, plus copies in `HooksPanel` and `WorkspaceView` whose own
 * comments said "identical to the Skills/Tools EmptyState" and "matching Tools'
 * EmptyState" — i.e. copy-paste with a note admitting it. Which is exactly how
 * they drifted anyway.
 *
 * `cta` names the create affordance instead of rendering one. That is deliberate
 * and load-bearing: on these surfaces the sidebar button is the single create
 * path, and a second identical button one glance away read as a duplicate. It
 * must be passed the HOST's real label — the generic fallback used to say
 * `Click "+ Create"` on a surface whose button says `+ New skill`, and only a
 * browser click ever caught that.
 */
export interface EmptyStateProps {
  /** Plural noun for the thing that is missing, e.g. `skills`, `tool collections`. */
  label?: string;
  /** The host's exact create-button label, e.g. `+ New skill`. */
  cta?: string;
  /** Free-form body, for states with nothing to create toward. */
  children?: React.ReactNode;
}

export default function EmptyState({ label, cta, children }: EmptyStateProps) {
  return (
    <div style={cardStyle}>
      {children ?? (
        cta
          ? `No ${label} yet. Click "${cta}" to add one.`
          : `No ${label} yet.`
      )}
    </div>
  );
}

/**
 * Carries the text styling itself so the copy needs no inner wrapper — two nested
 * elements with identical text make `getByText` ambiguous in tests.
 */
const cardStyle: React.CSSProperties = {
  padding: 24,
  border: `1px dashed ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  textAlign: 'center',
  color: theme.colors.textSecondary,
  fontSize: theme.typography.sizes.body,
};
