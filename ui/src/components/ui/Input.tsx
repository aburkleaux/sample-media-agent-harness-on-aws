import React from 'react';
import { theme } from '../../styles/tokens';

/**
 * The app's text input, textarea and select.
 *
 * Written because there were SIX different input geometries and no shared
 * component, so a form's fields were a different height depending on which file
 * they lived in:
 *
 *   6px          SkillsPanel            13px   radius 4   borderStrong
 *   6px 8px      Workflow / Tools /     13px   radius 4   borderStrong
 *                Agents editors, the
 *                three browsers
 *   5px 8px      Tools filter           12px   radius 4   borderStrong
 *   7px 9px      MCP modal              13px   radius 6   borderStrong
 *   7px 10px     Skills / Agents search 13px   radius 6   border
 *   8px 10px     SkillEditor, Hooks     13px   radius 6   border
 *   8px 12px     Hooks search           13px   radius 6   border
 *
 * They also disagreed on which border token to use, which is the visible half:
 * `border` is the hairline for dividers and `borderStrong` is the one meant to
 * read as an editable edge, so half the inputs in the app looked inert.
 * `borderStrong` is correct for a control and is what this uses.
 *
 * Two sizes, matching Button's tiers so a control row lines up:
 *   sm  inline filters and search boxes
 *   md  form fields
 */
export type InputSize = 'sm' | 'md';

const sizeStyles: Record<InputSize, React.CSSProperties> = {
  sm: { padding: '5px 8px', fontSize: 'var(--fs-xs)' },
  md: { padding: '6px 8px', fontSize: 'var(--fs-sm)' },
};

const baseStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  fontFamily: 'var(--font-body)',
  color: theme.colors.textPrimary,
  background: theme.colors.surface,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  outline: 'none',
};

/** Applied when `invalid` — a red edge, so the message is not the only signal. */
const invalidStyle: React.CSSProperties = { borderColor: theme.colors.error };

const disabledStyle: React.CSSProperties = {
  opacity: theme.opacity.disabled,
  cursor: 'not-allowed',
};

interface Common {
  inputSize?: InputSize;
  invalid?: boolean;
  /** Escape hatch for layout only (flex, margin, width) — not colour. */
  style?: React.CSSProperties;
}

export type InputProps = Common & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'style' | 'size'>;

export function Input({ inputSize = 'md', invalid, disabled, style, ...rest }: InputProps) {
  return (
    <input
      {...rest}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      style={{
        ...baseStyle,
        ...sizeStyles[inputSize],
        ...(invalid ? invalidStyle : null),
        ...(disabled ? disabledStyle : null),
        ...style,
      }}
    />
  );
}

export type TextareaProps = Common & Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'style'> & {
  /** `true` for code, prompts and JSON — anything the user reads character by character. */
  mono?: boolean;
};

export function Textarea({ inputSize = 'md', invalid, disabled, mono, style, ...rest }: TextareaProps) {
  return (
    <textarea
      {...rest}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      style={{
        ...baseStyle,
        ...sizeStyles[inputSize],
        resize: 'vertical',
        lineHeight: theme.typography.lineHeight,
        ...(mono ? { fontFamily: 'var(--font-mono)' } : null),
        ...(invalid ? invalidStyle : null),
        ...(disabled ? disabledStyle : null),
        ...style,
      }}
    />
  );
}

export type SelectProps = Common & Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'style'>;

export function Select({ inputSize = 'md', invalid, disabled, style, children, ...rest }: SelectProps) {
  return (
    <select
      {...rest}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      style={{
        ...baseStyle,
        ...sizeStyles[inputSize],
        cursor: disabled ? 'not-allowed' : 'pointer',
        ...(invalid ? invalidStyle : null),
        ...(disabled ? disabledStyle : null),
        ...style,
      }}
    >
      {children}
    </select>
  );
}

export default Input;
