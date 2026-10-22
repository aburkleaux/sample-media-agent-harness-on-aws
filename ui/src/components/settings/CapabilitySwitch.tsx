import React from 'react';
import { theme } from '../../styles/tokens';

export interface CapabilitySwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  ariaLabel?: string;
  disabled?: boolean;
  busy?: boolean;
}

export default function CapabilitySwitch({
  checked,
  onChange,
  label,
  ariaLabel = label,
  disabled = false,
  busy = false,
}: CapabilitySwitchProps) {
  const inactive = disabled || busy;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      aria-busy={busy || undefined}
      aria-disabled={inactive || undefined}
      onClick={() => { if (!inactive) onChange(!checked); }}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 8,
        minHeight: 32, padding: '5px 8px 5px 10px',
        border: `1px solid ${theme.colors.border}`,
        borderRadius: theme.radius.button,
        background: theme.colors.surface,
        color: theme.colors.textPrimary,
        cursor: busy ? 'wait' : disabled ? 'not-allowed' : 'pointer',
        opacity: inactive ? 0.65 : 1,
        fontSize: 'var(--fs-xs)', fontWeight: 500,
      }}
    >
      <span>{busy ? `${label}…` : label}</span>
      <span
        aria-hidden="true"
        style={{
          width: 34, height: 20, borderRadius: theme.radius.pill,
          background: checked ? theme.colors.primary : theme.colors.border,
          position: 'relative', flexShrink: 0,
          transition: 'background 0.2s',
        }}
      >
        <span style={{
          position: 'absolute', top: 2, left: checked ? 16 : 2,
          width: 16, height: 16, borderRadius: '50%',
          background: theme.colors.surface,
          boxShadow: theme.elevation.subtle,
          transition: 'left 0.2s',
        }} />
      </span>
    </button>
  );
}
