/**
 * @vitest-environment jsdom
 *
 * The Skills and Agents surface headers are meant to line up 1:1 — same metrics,
 * same rule, same two-tier arrangement when you go one level in. That used to be
 * an invariant maintained by nothing but a comment on each of two style objects
 * ("Matches caHeaderStyle (Agents)" / "Matches SkillsCenter headerStyle"), and the
 * surfaces had already drifted apart in the way that matters most: Skills kept its
 * header on every branch, Agents lost it the moment you opened an agent.
 *
 * These assert the shared DEFINITION rather than two copies that happen to agree,
 * so a future edit to one surface's chrome cannot silently desync the other.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import {
  SurfaceHeader,
  surfaceNestedHeaderStyle,
  surfaceNestedStyle,
  surfaceStyle,
} from './SurfaceHeader';
import { SkillsSurfaceHeader, skillsNestedStyle, skillsSurfaceStyle } from './SkillsSurfaceHeader';
import { AgentsSurfaceHeader, agentsNestedStyle, agentsSurfaceStyle } from './AgentsSurfaceHeader';
import { BELL_SAFE_AREA } from '../utils/layout';

describe('SurfaceHeader', () => {
  it('renders the title, the description, and the rule under them', () => {
    render(<SurfaceHeader title="Widgets" subtitle="Things you can widget." />);

    const heading = screen.getByRole('heading', { name: 'Widgets', level: 2 });
    expect(screen.getByText('Things you can widget.')).toBeTruthy();
    // The rule, not just the text: its absence is what made a nested branch's
    // content look like it had run off the top of the pane.
    expect(heading.closest('div')!.parentElement!.style.borderBottom)
      .toBe('1px solid var(--color-border)');
  });

  it('gives a nested branch the same metrics but NO second rule', () => {
    render(<SurfaceHeader title="Widgets" subtitle="Things you can widget." />);
    const header = screen.getByRole('heading', { name: 'Widgets', level: 2 })
      .closest('div')!.parentElement!;

    // A nested header (the item's name + its Back/Edit) sits directly under the
    // surface header, so the two must stack as one block…
    expect(surfaceNestedHeaderStyle.padding).toBe(header.style.padding);
    // …and only the outer one draws a rule. Two hairlines a heading apart read as
    // a broken table.
    expect(surfaceNestedHeaderStyle.borderBottom).toBeUndefined();
    // Right inset reserves the floating notification bell's safe area.
    expect(surfaceNestedHeaderStyle.padding).toContain(`${BELL_SAFE_AREA}px`);
  });

  it('can shrink below its content so the header is never pushed out', () => {
    // `minHeight: 0` is the non-obvious one: a flex child will not shrink below
    // its content height without it, so the child's `overflowY: auto` is inert and
    // a long body pushes the header off the pane instead of scrolling.
    expect(surfaceNestedStyle.minHeight).toBe(0);
    expect(surfaceNestedStyle.flex).toBe(1);
  });
});

describe('the Skills and Agents surfaces share one definition', () => {
  it('uses the SAME style objects, not two that agree today', () => {
    expect(skillsSurfaceStyle).toBe(surfaceStyle);
    expect(agentsSurfaceStyle).toBe(surfaceStyle);
    expect(skillsNestedStyle).toBe(surfaceNestedStyle);
    expect(agentsNestedStyle).toBe(surfaceNestedStyle);
  });

  it('differs only in the copy', () => {
    const { unmount } = render(<SkillsSurfaceHeader />);
    expect(screen.getByRole('heading', { name: 'Skills', level: 2 })).toBeTruthy();
    expect(screen.getByText(/Capabilities an agent can load/)).toBeTruthy();
    unmount();

    render(<AgentsSurfaceHeader />);
    expect(screen.getByRole('heading', { name: 'Agents', level: 2 })).toBeTruthy();
    expect(screen.getByText(/Create and configure custom agents/)).toBeTruthy();
  });
});
