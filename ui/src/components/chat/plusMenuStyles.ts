/** Styles shared by the Plus_Menu and its sections (`RecentAssets`, `ProjectPicker`). Theme tokens only. */
import type React from 'react';
import { theme } from '../../styles/tokens';

export const menuItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  padding: '6px 8px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
  fontSize: 'inherit',
};

export const highlightStyle: React.CSSProperties = { background: theme.colors.activeBg };
export const disabledStyle: React.CSSProperties = { opacity: theme.opacity.disabled, cursor: 'not-allowed' };

/** The row style for `key`: highlighted while it holds focus, dimmed while `aria-disabled`. */
export function rowStyle(highlight: string | null, key: string, disabled = false): React.CSSProperties {
  return {
    ...menuItemStyle,
    ...(highlight === key ? highlightStyle : null),
    ...(disabled ? disabledStyle : null),
  };
}

export const sectionHeadingStyle: React.CSSProperties = {
  padding: '6px 8px 2px',
  fontSize: theme.typography.sizes.meta,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

export const noteStyle: React.CSSProperties = {
  padding: '6px 8px',
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

export const separatorStyle: React.CSSProperties = {
  height: 1,
  background: theme.colors.border,
  margin: '6px 0',
};

export const iconColumnStyle: React.CSSProperties = {
  display: 'inline-flex',
  flexShrink: 0,
  color: theme.colors.textSecondary,
};

export const checkColumnStyle: React.CSSProperties = {
  display: 'inline-flex',
  width: 14,
  flexShrink: 0,
  color: theme.colors.primary,
};

export const itemNameStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

export const metaStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
  flexShrink: 0,
};
