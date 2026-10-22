/**
 * Two-pane and unified renderers for a diff the SERVER computed.
 *
 * This component deliberately contains no diff algorithm. `diffText` arrives as a
 * unified diff produced by Python's `difflib` next to the proposal, so the diff a
 * reviewer reads is byte-identical to the one that will be published, and no diff
 * library needs adding to the browser bundle (there is none installed, by choice).
 *
 * `CodeArtifact.tsx` renders a supplied diff string the same way, but it is a
 * manifest-driven `RendererProps` artifact template registered in `RendererRegistry`, and
 * it hardcodes `background: '#1e1e2e'` instead of reading theme tokens, so it would not
 * follow light/dark here. Hence a separate, themed component rather than a reuse.
 */
import React from 'react';
import { theme, tint, numericStyle } from '../../styles/tokens';

type LineKind = 'add' | 'remove' | 'context' | 'meta';

function classify(line: string): LineKind {
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) return 'meta';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'remove';
  return 'context';
}

const LINE_STYLE: Record<LineKind, React.CSSProperties> = {
  add: { background: tint(theme.colors.success, 16), color: theme.colors.textPrimary },
  remove: { background: tint(theme.colors.error, 16), color: theme.colors.textPrimary },
  context: { color: theme.colors.textSecondary },
  meta: { color: theme.colors.textTertiary },
};

const preStyle: React.CSSProperties = {
  ...numericStyle,
  fontSize: theme.typography.sizes.code,
  margin: 0,
  padding: 0,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  lineHeight: 1.55,
};

/** Unified diff, coloured per line. */
export function UnifiedDiff({ diffText }: { diffText: string }) {
  if (!diffText) {
    return (
      <div
        data-testid="diff-empty"
        style={{
          fontSize: theme.typography.sizes.meta, color: theme.colors.textTertiary,
          border: `1px dashed ${theme.colors.borderStrong}`,
          borderRadius: theme.radius.button, padding: '8px 10px',
        }}
      >
        No text diff. This suggestion changes behaviour that does not live in an editable
        document.
      </div>
    );
  }
  return (
    <div
      data-testid="diff-unified"
      style={{
        border: `1px solid ${theme.colors.border}`,
        borderRadius: theme.radius.button,
        background: theme.colors.surfaceSubtle,
        padding: '8px 10px',
        overflowX: 'auto',
      }}
    >
      <pre style={preStyle}>
        {diffText.split('\n').map((line, index) => (
          <div key={index} style={{ ...LINE_STYLE[classify(line)], padding: '0 4px' }}>
            {line || ' '}
          </div>
        ))}
      </pre>
    </div>
  );
}

/**
 * Side-by-side current vs proposed. Added lines are marked on the right by comparing against
 * the `before` line set, a presentation aid only; the authoritative change is `diffText`.
 *
 * KNOWN LIMITATION, deliberately left in place for now: this cannot render a REMOVAL. Every
 * before-line is hard-coded `added: false`, so nothing is ever marked as deleted, and an
 * `operation: 'remove'` suggestion (a value `Suggestion.operation` admits) displays two panes
 * that look identical, "nothing happened". Membership is also set-based rather than
 * positional, so a line that already occurs anywhere in `before` is not marked, which hides
 * duplicates and moves.
 *
 * Both are reasons the unified renderer is the more trustworthy view, and why `diffText` is
 * the authority. Do not add a removal affordance here without also deciding whether two panes
 * survive at all, the pane/candidate labelling collision is still open.
 */
export function TwoPaneDiff({
  before, after, currentLabel, proposedLabel,
}: {
  before: string;
  after: string;
  currentLabel: string;
  proposedLabel: string;
}) {
  const beforeLines = before.split('\n');
  const afterLines = after.split('\n');
  const beforeSet = new Set(beforeLines);

  return (
    <div
      data-testid="diff-two-pane"
      style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}
    >
      <Pane label={currentLabel} lines={beforeLines.map((l) => ({ text: l, added: false }))} />
      <Pane
        label={proposedLabel}
        lines={afterLines.map((l) => ({ text: l, added: l.trim() !== '' && !beforeSet.has(l) }))}
      />
    </div>
  );
}

function Pane({
  label, lines,
}: { label: string; lines: { text: string; added: boolean }[] }) {
  return (
    <div style={{
      border: `1px solid ${theme.colors.border}`,
      borderRadius: theme.radius.button,
      background: theme.colors.surfaceSubtle,
      overflow: 'hidden',
    }}>
      <div style={{
        fontSize: theme.typography.sizes.badge,
        color: theme.colors.textSecondary,
        padding: '4px 8px',
        borderBottom: `1px solid ${theme.colors.border}`,
        background: theme.colors.surface,
      }}>
        {label}
      </div>
      <pre style={{ ...preStyle, padding: '6px 8px' }}>
        {lines.map((line, index) => (
          <div
            key={index}
            style={{
              padding: '0 4px',
              background: line.added ? tint(theme.colors.success, 18) : undefined,
            }}
          >
            {line.added && (
              <span style={{ color: theme.colors.success, userSelect: 'none' }}>+ </span>
            )}
            {line.text || ' '}
          </div>
        ))}
      </pre>
    </div>
  );
}

export default UnifiedDiff;
