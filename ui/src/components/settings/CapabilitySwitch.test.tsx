// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import CapabilitySwitch from './CapabilitySwitch';

afterEach(cleanup);

describe('CapabilitySwitch', () => {
  it('uses the full labeled control as a clickable semantic switch', () => {
    const onChange = vi.fn();
    render(<CapabilitySwitch checked onChange={onChange} label="Built-in skills" />);

    const control = screen.getByRole('switch', { name: 'Built-in skills' });
    expect(control.getAttribute('aria-checked')).toBe('true');
    fireEvent.click(control);
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it('disables mutations and reports busy state while a toggle is pending', () => {
    const onChange = vi.fn();
    render(<CapabilitySwitch checked={false} onChange={onChange} label="Disabled" ariaLabel="Enable audit hook" busy />);

    const control = screen.getByRole('switch', { name: 'Enable audit hook' });
    expect(control.getAttribute('aria-busy')).toBe('true');
    expect(control.getAttribute('aria-disabled')).toBe('true');
    expect((control as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(control);
    expect(onChange).not.toHaveBeenCalled();
  });
});
