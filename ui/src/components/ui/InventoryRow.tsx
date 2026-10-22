import React from 'react';
import { theme } from '../../styles/tokens';
import Badge, { type BadgeVariant } from './Badge';
import WarningIcon from '../icons/WarningIcon';
import ClampedText from './ClampedText';

/**
 * One row in a capability inventory — an agent, skill, tool collection or hook.
 *
 * Written because the four inventories used TWO different row idioms under
 * headers that had been deliberately made to match each other 1:1:
 *
 *   Agents            bordered cards (`caRowStyle`) — border, panel radius,
 *                     `12px 16px`, own surface
 *   Skills/Tools/     hairline rows (`listRowStyle`) — `padding: 11px 0` and a
 *   Hooks             bottom border, no card
 *
 * So sibling surfaces in the same nav, reached the same way, listed the same
 * kind of thing in visibly different furniture. Cards won: a row reads as a
 * discrete object, the action cluster gets room to sit at the far right instead
 * of wrapping inside a narrow column, and a card tolerates its meta line
 * wrapping at the Large font setting where a hairline row just collapses.
 *
 * This owns the chrome, the layout, the badge and the row-error slot. It does
 * NOT own the actions: each host passes its own cluster, because publish/test/
 * purge availability differs per category and folding that in here would couple
 * four lifecycles into one component.
 */
export interface InventoryRowProps {
  name: string;
  /**
   * The author's own words for what this is FOR, spanning the full row under the
   * name. Optional and `undefined`-when-unknown on purpose: several sources
   * (Gateway tools, built-ins, Registry records) carry no description, and
   * rendering `''` for those would claim the author left it blank.
   */
  description?: string;
  /** Lines of `description` shown before it clamps. */
  descriptionLines?: number;
  /**
   * Small muted qualifier on the NAME line, after the name and before the badge —
   * a version, a count, a scope.
   *
   * Exists so a one-token fact does not cost a whole row. `v1` sat alone on the meta
   * line under a three-line description, which read as a second paragraph and pushed
   * every subsequent row down for no information gain. Keep it to a few characters:
   * this line already carries the name, the badge and the action cluster.
   */
  nameSuffix?: React.ReactNode;
  /** Secondary line — "Updated 7/27/2026", "3 tools". */
  meta?: React.ReactNode;
  /** State chip shown inline with the name. */
  badge?: { label: string; variant?: BadgeVariant };
  /** The row's action cluster. Pinned to the right of the NAME line. */
  actions?: React.ReactNode;
  /**
   * Row-scoped failure (a publish rejection, a delete that failed). Spans the
   * full row width so it reads as belonging to THIS row rather than to the list.
   */
  error?: string | null;
  onDismissError?: () => void;
  /** Fades the row during an optimistic removal. */
  dimmed?: boolean;
  /**
   * Makes the whole row open the item. Action buttons inside `actions` must call
   * `stopPropagation`, or clicking Delete also opens the thing being deleted.
   */
  onClick?: () => void;
  'data-testid'?: string;
}

export default function InventoryRow({
  name,
  nameSuffix,
  description,
  descriptionLines = 3,
  meta,
  badge,
  actions,
  error,
  onDismissError,
  dimmed = false,
  onClick,
  'data-testid': testId,
}: InventoryRowProps) {
  return (
    <div
      data-testid={testId}
      onClick={onClick}
      style={{
        ...rowStyle,
        ...(onClick ? { cursor: 'pointer' } : null),
        ...(dimmed ? { opacity: theme.opacity.disabled } : null),
      }}
    >
      <div style={mainStyle}>
        {/* The actions sit on THIS line, beside the name — not centred against the
            whole card. That is what lets the description below run the full width
            instead of wrapping in the gap left over beside five buttons, and it puts
            every row's action cluster at the same height whatever the text does. */}
        <div style={nameRowStyle}>
          <span style={nameStyle} title={name}>{name}</span>
          {nameSuffix !== undefined && nameSuffix !== null && (
            <span style={nameSuffixStyle}>{nameSuffix}</span>
          )}
          {badge && <Badge variant={badge.variant ?? 'neutral'}>{badge.label}</Badge>}
          {actions && <div style={actionsStyle}>{actions}</div>}
        </div>
        {description && (
          <div style={descriptionStyle}>
            <ClampedText lines={descriptionLines}>{description}</ClampedText>
          </div>
        )}
        {meta !== undefined && meta !== null && <div style={metaStyle}>{meta}</div>}
      </div>

      {error && (
        <div style={errorStyle} role="alert">
          {/* The row owns the warning mark so no caller has to embed a glyph in
              its message string — which is how a stray `⚠ ` prefix survived the
              emoji cleanup. */}
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <WarningIcon />
            {error}
          </span>
          {onDismissError && (
            <button
              type="button"
              onClick={onDismissError}
              style={errorDismissStyle}
              aria-label={`Dismiss error for ${name}`}
            >
              ✕
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Stacks rows with a consistent gutter. Use around a list of `InventoryRow`. */
export function InventoryRowList({ children }: { children: React.ReactNode }) {
  return <div style={listStyle}>{children}</div>;
}

const listStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
};

const rowStyle: React.CSSProperties = {
  display: 'flex',
  // `flex-start`, not `center`: with a description the card is 3-5 lines tall and the
  // single child should fill it from the top rather than float in the middle.
  alignItems: 'flex-start',
  gap: 12,
  // Lets the error span a full row of its own beneath the content.
  flexWrap: 'wrap',
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  padding: '12px 16px',
  background: theme.colors.surface,
  transition: `opacity ${theme.transitions.fade}`,
};

// `minWidth: 0` is load-bearing: without it the name's ellipsis never engages and
// a long id pushes the action cluster off the row instead of truncating.
const mainStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
};

const nameRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  minWidth: 0,
};

// Muted and unshrinkable: it qualifies the name, so it must not be the thing that
// truncates when the name is long — the name has the ellipsis for that.
const nameSuffixStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
  flexShrink: 0,
  whiteSpace: 'nowrap',
};

// Full row width, because nothing shares the line with it.
const descriptionStyle: React.CSSProperties = {
  marginTop: 3,
};

const nameStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.body,
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};

const metaStyle: React.CSSProperties = {
  marginTop: 2,
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.textSecondary,
};

// `marginLeft: auto` pins the cluster to the right end of the NAME line.
const actionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  alignItems: 'center',
  marginLeft: 'auto',
  // Never squeezed by a long name: the name truncates (it has `minWidth: 0` and an
  // ellipsis), the buttons do not.
  flexShrink: 0,
  // Buttons carry their own labels, so a narrow pane wraps the cluster rather than
  // clipping it.
  flexWrap: 'wrap',
  justifyContent: 'flex-end',
};

const errorStyle: React.CSSProperties = {
  flexBasis: '100%',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 8,
  marginTop: 6,
  fontSize: theme.typography.sizes.meta,
  color: theme.colors.error,
};

const errorDismissStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  color: theme.colors.error,
  fontSize: theme.typography.sizes.meta,
  padding: '0 2px',
  flexShrink: 0,
};
