/** @vitest-environment jsdom */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import { MediaCard } from './MediaCard';

describe('MediaCard menu', () => {
  it('lifts the card above its neighbours while the menu is open', () => {
    render(<ul><MediaCard kind="video" title="a.mp4" onPreview={() => {}} menu={[{ label: 'Save to folder', onClick: () => {} }]} /></ul>);
    const card = screen.getByTestId('media-card');
    expect(card.style.zIndex).toBe('');
    fireEvent.click(screen.getByLabelText('More for a.mp4'));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(Number(card.style.zIndex)).toBeGreaterThan(0);
  });
});
