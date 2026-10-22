import React, { useCallback, useRef, useEffect } from 'react';
import { theme } from '../styles/tokens';

export interface ResizeDividerProps {
  onResize: (delta: number) => void;
  orientation: 'vertical';
}

/**
 * Reusable drag-handle component for resizing panels.
 * On mousedown, captures mouse movement and reports pixel deltas to the parent.
 * The parent applies clamping logic (min/max constraints) before updating width state.
 */
export default function ResizeDivider({ onResize, orientation: _orientation }: ResizeDividerProps) {
  const dragging = useRef(false);
  const lastX = useRef(0);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    lastX.current = e.clientX;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const delta = e.clientX - lastX.current;
      lastX.current = e.clientX;
      if (delta !== 0) {
        onResize(delta);
      }
    };

    const handleMouseUp = () => {
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [onResize]);

  return (
    <div
      onMouseDown={handleMouseDown}
      style={dividerStyle}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize panel"
      tabIndex={0}
    >
      <div style={dotsContainerStyle}>
        <div style={dotStyle} />
        <div style={dotStyle} />
        <div style={dotStyle} />
      </div>
    </div>
  );
}

const dividerStyle: React.CSSProperties = {
  width: 6,
  cursor: 'col-resize',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  flexShrink: 0,
  background: 'transparent',
  transition: 'background 0.15s',
};

const dotsContainerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 3,
  alignItems: 'center',
};

const dotStyle: React.CSSProperties = {
  width: 3,
  height: 3,
  borderRadius: '50%',
  background: theme.colors.borderStrong,
};
