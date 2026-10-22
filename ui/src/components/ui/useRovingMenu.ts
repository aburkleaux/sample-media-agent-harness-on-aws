/**
 * Roving-focus keyboard behaviour shared by small popover menus: the composer's Plus_Menu (one focus order
 * across its Attach, Recent, web-search, and Project sections) and the Projects_Surface overflow menu.
 *
 * One item holds `tabIndex=0`, the rest `-1`. Up/Down move across every item (wrapping), Home/End jump
 * to the ends, Escape closes and asks the caller to refocus its trigger, Tab closes without refocusing.
 * Focus lands on the first item when the menu opens, and again when `resetKey` changes (a late list
 * arriving) unless the user has already moved.
 *
 * Items are found with `itemSelector` inside `menuRef`; disabled form controls are skipped, while
 * `aria-disabled` items stay focusable so their reason can be read.
 */
import React, { useCallback, useLayoutEffect, useRef, useState } from 'react';

export interface RovingMenuOptions {
  open: boolean;
  itemSelector: string;
  /** Close the menu. `refocus` is true for Escape, false for Tab. */
  onClose: (refocus: boolean) => void;
  /** Changing this re-focuses the first item, unless the user has already moved focus. */
  resetKey?: unknown;
}

export function useRovingMenu({ open, itemSelector, onClose, resetKey }: RovingMenuOptions) {
  const menuRef = useRef<HTMLDivElement>(null);
  /** The item that holds `tabIndex=0`. */
  const currentRef = useRef<HTMLElement | null>(null);
  /** Set once the user moves focus, so a late list does not yank it back to the first item. */
  const userMovedRef = useRef(false);
  const [highlight, setHighlight] = useState<string | null>(null);

  const items = useCallback((): HTMLElement[] => {
    const root = menuRef.current;
    if (!root) return [];
    return Array.from(root.querySelectorAll<HTMLElement>(itemSelector)).filter(
      (el) => !(el as HTMLButtonElement | HTMLInputElement).disabled,
    );
  }, [itemSelector]);

  const focusItem = useCallback((el: HTMLElement | undefined) => {
    if (!el) return;
    currentRef.current = el;
    el.focus();
  }, []);

  // Reset per open, so a reopened menu starts at its first item.
  useLayoutEffect(() => {
    if (open) return;
    currentRef.current = null;
    userMovedRef.current = false;
    setHighlight(null);
  }, [open]);

  // Roving tabindex, re-applied after every render so slot items and late-arriving rows take part.
  useLayoutEffect(() => {
    if (!open) return;
    const all = items();
    if (!currentRef.current || !all.includes(currentRef.current)) currentRef.current = all[0] ?? null;
    for (const el of all) el.tabIndex = el === currentRef.current ? 0 : -1;
  });

  // Focus the first item on open, and again when `resetKey` changes if the user has not moved yet.
  useLayoutEffect(() => {
    if (!open || userMovedRef.current) return;
    focusItem(items()[0]);
  }, [open, resetKey, items, focusItem]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onClose(true);
      return;
    }
    if (e.key === 'Tab') {
      onClose(false);
      return;
    }
    const all = items();
    if (all.length === 0) return;
    const at = all.indexOf(document.activeElement as HTMLElement);
    let next: number | null = null;
    if (e.key === 'ArrowDown') next = at < 0 ? 0 : (at + 1) % all.length;
    else if (e.key === 'ArrowUp') next = at < 0 ? all.length - 1 : (at - 1 + all.length) % all.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = all.length - 1;
    if (next === null) return;
    e.preventDefault();
    userMovedRef.current = true;
    focusItem(all[next]);
  };

  const onFocusCapture = (e: React.FocusEvent<HTMLElement>) => {
    const target = e.target as HTMLElement;
    if (items().includes(target)) {
      currentRef.current = target;
      setHighlight(target.dataset.menuKey ?? null);
    }
  };

  const hoverFocus = (e: React.MouseEvent<HTMLElement>) => {
    userMovedRef.current = true;
    focusItem(e.currentTarget);
  };

  return { menuRef, highlight, onKeyDown, onFocusCapture, hoverFocus };
}
