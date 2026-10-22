import React from 'react';
import { theme, tint } from '../../styles/tokens';

/**
 * The app's button.
 *
 * Written because there were 84 separate `…BtnStyle` / `…ButtonStyle` constants
 * across the UI and no shared component, which had two consequences: buttons
 * drifted apart visually, and the styles stopped meaning anything — a View
 * action was being rendered with `deleteBtnStyle` simply because that was the
 * neutral outline style that happened to exist.
 *
 * Four variants, chosen by what the action DOES rather than where it sits:
 *
 *   primary    the one affirmative action in a view (Save, Publish, Create)
 *   secondary  a real but non-primary action (View, Edit, Test) — outlined
 *   ghost      navigation and dismissal (Back, Cancel, Close) — no border
 *   danger     destructive and irreversible (Delete, Purge, Confirm delete)
 *
 * Exactly one `primary` per view. If two actions both look primary, one of them
 * is `secondary`.
 */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'style'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Replaces the label with `busyLabel` and disables. Keeps width stable. */
  busy?: boolean;
  busyLabel?: string;
  /** Escape hatch for layout only (margin, alignSelf, flex) — not colour. */
  style?: React.CSSProperties;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = 'secondary',
  size = 'sm',
  busy = false,
  busyLabel,
  disabled,
  children,
  style,
  ...rest
}, ref) {
  const isDisabled = disabled || busy;
  return (
    <button
      {...rest}
      ref={ref}
      disabled={isDisabled}
      style={{
        ...baseStyle,
        ...sizeStyles[size],
        ...variantStyles[variant],
        ...(isDisabled ? disabledStyle : null),
        ...style,
      }}
    >
      {busy ? (busyLabel ?? children) : children}
    </button>
  );
});

export default Button;

const baseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
  fontWeight: theme.typography.weights.medium,
  lineHeight: 1,
  whiteSpace: 'nowrap',
  cursor: 'pointer',
  transition: `background ${theme.transitions.hover}, border-color ${theme.transitions.hover}, color ${theme.transitions.hover}`,
};

/*
 * Family B metrics — see the note on `theme.radius.button` in styles/tokens.ts.
 *
 * The app had two button families and this component shipped Family A (radius 6,
 * 13px, 7–8px padding). Family B won, so these are its two tiers as they were
 * actually used:
 *
 *   sm  the compact action that sits INSIDE a row — Edit / Test / View, next to a
 *       name. Was `4px 9px` @ 11px in the Agents rows.
 *   md  the form-level action — Save / Publish / Cancel at the foot of an editor.
 *       Was `5–6px 12px` @ 12px across the Agents, Tools and Workflow editors.
 *
 * Radius comes from the token, so it tracks any future change to that decision.
 */
const sizeStyles: Record<ButtonSize, React.CSSProperties> = {
  sm: { padding: '4px 10px', fontSize: 'var(--fs-2xs)' },
  md: { padding: '6px 12px', fontSize: 'var(--fs-xs)' },
};

const variantStyles: Record<ButtonVariant, React.CSSProperties> = {
  primary: {
    background: theme.colors.primary,
    // Contrast against the fill, so it tracks the fill rather than the page:
    // white on the blue primaries, near-black on dark-kirocrew's mint (where
    // white would be ~1.5:1).
    color: theme.colors.onPrimary,
    border: '1px solid transparent',
  },
  secondary: {
    background: theme.colors.surface,
    color: theme.colors.textPrimary,
    border: `1px solid ${theme.colors.border}`,
  },
  ghost: {
    background: 'transparent',
    color: theme.colors.textSecondary,
    border: '1px solid transparent',
  },
  danger: {
    background: tint(theme.colors.error, 10),
    color: theme.colors.error,
    border: `1px solid ${tint(theme.colors.error, 40)}`,
  },
};

const disabledStyle: React.CSSProperties = {
  opacity: theme.opacity.disabled,
  cursor: 'not-allowed',
};
