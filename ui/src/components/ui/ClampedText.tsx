import React, { useLayoutEffect, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';

/**
 * Body text clamped to `lines`, with a more/less toggle when it is actually longer.
 *
 * The toggle appears from a MEASUREMENT (`scrollHeight > clientHeight`), not from a
 * character-count guess. A guess is wrong in both directions at the widths this ships
 * at: a 300-character description fits three lines in a wide pane and needs five in a
 * narrow one, so a threshold either hides the toggle on text that is genuinely cut off
 * or offers "more" that reveals nothing.
 *
 * `-webkit-line-clamp` is used deliberately over a fixed `maxHeight`. It clamps on the
 * LINE, so the last visible line ends in an ellipsis instead of being sliced through
 * the middle of its glyphs, and it needs no knowledge of the line-height. It is
 * supported in every browser this app targets (it is not, despite the prefix, WebKit-only).
 */
export interface ClampedTextProps {
  children: string;
  /** Visible lines before clamping. */
  lines?: number;
  /** Overrides the default `meta`-sized secondary text. */
  style?: React.CSSProperties;
}

export default function ClampedText({ children, lines = 3, style }: ClampedTextProps) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const textRef = useRef<HTMLDivElement | null>(null);

  // Layout effect, not effect: this reads geometry, and doing it after paint shows a
  // frame with no toggle on text that has one.
  useLayoutEffect(() => {
    const node = textRef.current;
    if (!node) return;
    const measure = () => {
      // Only meaningful while clamped — expanded, scrollHeight === clientHeight and the
      // toggle would disappear the moment it was used.
      if (expanded) return;
      setOverflowing(node.scrollHeight > node.clientHeight + 1);
    };
    measure();
    // Re-measure on resize: the sidebar and the chat pane are both resizable, so the
    // same text is clamped at one width and complete at another.
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [children, lines, expanded]);

  return (
    <div>
      <div
        ref={textRef}
        style={{
          ...textStyle,
          ...style,
          ...(expanded ? null : clampStyle(lines)),
        }}
      >
        {children}
      </div>
      {(overflowing || expanded) && (
        <button type="button" onClick={() => setExpanded(value => !value)} style={toggleStyle}>
          {expanded ? 'less' : 'more'}
        </button>
      )}
    </div>
  );
}

const textStyle: React.CSSProperties = {
  fontSize: theme.typography.sizes.meta,
  lineHeight: 1.45,
  color: theme.colors.textSecondary,
  // Long ids and URLs inside a description must not push the card wider.
  overflowWrap: 'anywhere',
};

const clampStyle = (lines: number): React.CSSProperties => ({
  display: '-webkit-box',
  WebkitLineClamp: lines,
  WebkitBoxOrient: 'vertical',
  overflow: 'hidden',
});

const toggleStyle: React.CSSProperties = {
  marginTop: 2,
  padding: 0,
  border: 'none',
  background: 'none',
  color: theme.colors.primary,
  fontSize: theme.typography.sizes.meta,
  cursor: 'pointer',
};
