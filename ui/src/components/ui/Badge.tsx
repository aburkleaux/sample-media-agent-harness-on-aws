import React from 'react';
import { theme, tint } from '../../styles/tokens';

/**
 * The app's status badge and count pill.
 *
 * Written because there were 26 separate badge style constants, in four
 * incompatible idioms that disagreed on radius (3 / 4 / 999), tint strength
 * (12% / 14% / 16% / a solid `border` fill) and size — so the same "Draft" chip
 * looked different on the Agents rows, the Skills rows and the Hooks rows.
 *
 * Variants name the STATE, not the colour, so a theme can change what a state
 * looks like without every call site being rewritten:
 *
 *   neutral    draft, inactive, "not yet anything" — the resting state
 *   success    published, active, passed
 *   warning    needs attention, unresolved, deprecated
 *   error      failed, invalid, orphaned
 *   info       informational tags — provenance, model, category
 *   count      a number, not a state (pill-shaped)
 *
 * WHY `neutral` RATHER THAN AMBER FOR DRAFT
 *
 * Draft used to be `accent` and published `success`. In `dark-kirocrew` those are
 * the SAME colour (`--color-accent` and `--color-success` are both #22E39A,
 * because that theme is deliberately monochrome plus one mint), so Draft and
 * Published rendered identically and the colour carried no information at all.
 *
 * Fixed by treatment rather than by giving that theme a second hue — which its
 * own design note in global.css explicitly rejects. `neutral` is unfilled and
 * grey, `success` is tinted and coloured, so the two differ by weight in every
 * theme. That also means the distinction survives being colour-blind or
 * greyscale-printed, which a hue pair never did.
 */
export type BadgeVariant = 'neutral' | 'success' | 'warning' | 'error' | 'info' | 'count';

export interface BadgeProps {
  variant?: BadgeVariant;
  /** Renders in the uppercase micro-label treatment (provenance, category tags). */
  caps?: boolean;
  children: React.ReactNode;
  /** Escape hatch for layout only (margin, alignSelf) — not colour. */
  style?: React.CSSProperties;
  title?: string;
}

export default function Badge({ variant = 'neutral', caps = false, children, style, title }: BadgeProps) {
  return (
    <span
      title={title}
      style={{
        ...baseStyle,
        ...variantStyles[variant],
        ...(caps ? capsStyle : null),
        ...style,
      }}
    >
      {children}
    </span>
  );
}

const baseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  fontSize: theme.typography.sizes.badge,
  fontWeight: theme.typography.weights.semibold,
  padding: '2px 7px',
  borderRadius: theme.radius.badge,
  border: '1px solid transparent',
  lineHeight: 1.4,
  // A long name next to a badge must not squeeze it.
  flexShrink: 0,
  whiteSpace: 'nowrap',
};

const capsStyle: React.CSSProperties = {
  textTransform: 'uppercase',
  letterSpacing: 0.5,
};

const variantStyles: Record<BadgeVariant, React.CSSProperties> = {
  // Unfilled and grey on purpose — see the note above on dark-kirocrew.
  neutral: {
    background: theme.colors.surfaceSubtle,
    color: theme.colors.textSecondary,
    borderColor: theme.colors.border,
  },
  success: {
    background: tint(theme.colors.success, 16),
    color: theme.colors.success,
  },
  warning: {
    background: tint(theme.colors.warning, 16),
    color: theme.colors.warning,
  },
  error: {
    background: tint(theme.colors.error, 16),
    color: theme.colors.error,
  },
  info: {
    background: tint(theme.colors.primary, 16),
    color: theme.colors.primary,
  },
  count: {
    background: `color-mix(in srgb, ${theme.colors.textSecondary} 12%, transparent)`,
    color: theme.colors.textSecondary,
    borderRadius: theme.radius.pill,
    // Keeps single and double digits the same width so a column of counts aligns.
    minWidth: 20,
    justifyContent: 'center',
    fontVariantNumeric: 'tabular-nums',
  },
};
