/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import Badge from './Badge';

describe('Badge', () => {
  it('renders its label', () => {
    render(<Badge>Draft</Badge>);
    expect(screen.getByText('Draft')).toBeInTheDocument();
  });

  /**
   * The regression this component exists to prevent.
   *
   * Draft used to be `--color-accent` and Published `--color-success`. In
   * `dark-kirocrew` both of those resolve to the SAME mint (#22E39A), because
   * that theme is deliberately monochrome plus one accent — so the two badges
   * were pixel-identical and the colour carried no information.
   *
   * Asserting on the resolved colour is not possible here (jsdom does not load
   * global.css, so every `var()` computes to empty), so this asserts the
   * structural property that actually fixes it: the two variants must not draw
   * from the same token. `neutral` is a grey surface fill, `success` is a tint of
   * the success hue.
   */
  it('distinguishes neutral (draft) from success (published) without relying on hue', () => {
    const { container: neutral } = render(<Badge variant="neutral">Draft</Badge>);
    const { container: success } = render(<Badge variant="success">Published</Badge>);

    const draftStyle = neutral.firstElementChild!.getAttribute('style')!;
    const pubStyle = success.firstElementChild!.getAttribute('style')!;

    expect(draftStyle).not.toEqual(pubStyle);
    // Draft draws from the neutral furniture tokens...
    expect(draftStyle).toContain('--color-surface-subtle');
    expect(draftStyle).toContain('--color-text-secondary');
    // ...and must NOT borrow the accent, which is what collided with success.
    expect(draftStyle).not.toContain('--color-accent');
    expect(draftStyle).not.toContain('--color-success');
    // Published is the only one that speaks in the success hue.
    expect(pubStyle).toContain('--color-success');
  });

  it('gives draft a visible border so the two differ by weight, not only colour', () => {
    // Belt and braces for greyscale and colour-blind users: `neutral` is
    // outlined and `success` is a filled tint, so they are still telling apart
    // with no colour perception at all.
    const { container } = render(<Badge variant="neutral">Draft</Badge>);
    expect(container.firstElementChild!.getAttribute('style')).toContain('--color-border');
  });

  it('renders count as a pill with a stable width', () => {
    const { container } = render(<Badge variant="count">7</Badge>);
    const style = container.firstElementChild!.getAttribute('style')!;
    // 999px keeps it fully rounded at any height; min-width stops a column of
    // counts jittering between one and two digits.
    expect(style).toContain('border-radius: 999px');
    expect(style).toContain('min-width: 20px');
    expect(style).toContain('tabular-nums');
  });

  it('applies the uppercase micro-label treatment only when asked', () => {
    const { container: plain } = render(<Badge>Draft</Badge>);
    const { container: caps } = render(<Badge caps>Gateway</Badge>);
    expect(plain.firstElementChild!.getAttribute('style')).not.toContain('uppercase');
    expect(caps.firstElementChild!.getAttribute('style')).toContain('uppercase');
  });
});
