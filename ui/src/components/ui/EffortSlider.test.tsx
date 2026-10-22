// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import '@testing-library/jest-dom';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import EffortSlider, { EFFORT_STORAGE_KEY } from './EffortSlider';

const radio = (name: string) => screen.getByRole('radio', { name });

describe('EffortSlider (segmented reasoning-effort control)', () => {
  beforeEach(() => {
    cleanup();
    localStorage.clear();
  });

  it('shows a one-line note instead of the control when the model does not support effort', () => {
    render(<EffortSlider supported={false} />);
    expect(screen.queryByRole('radiogroup')).toBeNull();
    expect(screen.getByTestId('effort-unsupported')).toHaveTextContent(/isn't adjustable/);
  });

  it('renders four labelled segments in a radiogroup', () => {
    render(<EffortSlider supported />);
    expect(screen.getByRole('radiogroup', { name: 'Reasoning effort' })).toBeInTheDocument();
    expect(screen.getAllByRole('radio').map(r => r.textContent)).toEqual(['Default', 'Low', 'Medium', 'High']);
  });

  it('starts on Default with its helper copy when nothing is stored', () => {
    render(<EffortSlider supported />);
    expect(radio('Default')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('effort-hint')).toHaveTextContent(/model's own reasoning default/);
  });

  it('persists the enum, fires model-changed and updates the hint on selection', () => {
    const changed = vi.fn();
    window.addEventListener('model-changed', changed);
    const onChange = vi.fn();
    render(<EffortSlider supported onChange={onChange} />);

    fireEvent.click(radio('Low'));
    expect(localStorage.getItem(EFFORT_STORAGE_KEY)).toBe('low');
    expect(onChange).toHaveBeenCalledWith('low');
    expect(changed).toHaveBeenCalledTimes(1);
    expect(radio('Low')).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByTestId('effort-hint')).toHaveTextContent(/Fastest/);

    fireEvent.click(radio('High'));
    expect(localStorage.getItem(EFFORT_STORAGE_KEY)).toBe('high');

    // Back to Default stores the empty string (= send nothing)
    fireEvent.click(radio('Default'));
    expect(localStorage.getItem(EFFORT_STORAGE_KEY)).toBe('');
    window.removeEventListener('model-changed', changed);
  });

  it('does not re-fire when the selected segment is clicked again', () => {
    const changed = vi.fn();
    window.addEventListener('model-changed', changed);
    render(<EffortSlider supported />);
    fireEvent.click(radio('Default'));
    expect(changed).not.toHaveBeenCalled();
    window.removeEventListener('model-changed', changed);
  });

  it('moves the selection with arrow keys and Home/End (roving tabindex)', () => {
    render(<EffortSlider supported />);
    const group = screen.getByRole('radiogroup');
    expect(radio('Default')).toHaveAttribute('tabindex', '0');
    expect(radio('Low')).toHaveAttribute('tabindex', '-1');

    fireEvent.keyDown(group, { key: 'ArrowRight' });
    expect(radio('Low')).toHaveAttribute('aria-checked', 'true');
    expect(radio('Low')).toHaveAttribute('tabindex', '0');
    fireEvent.keyDown(group, { key: 'End' });
    expect(localStorage.getItem(EFFORT_STORAGE_KEY)).toBe('high');
    fireEvent.keyDown(group, { key: 'ArrowRight' }); // clamps at the end
    expect(radio('High')).toHaveAttribute('aria-checked', 'true');
    fireEvent.keyDown(group, { key: 'Home' });
    expect(localStorage.getItem(EFFORT_STORAGE_KEY)).toBe('');
  });

  it('restores the stored value on mount', () => {
    localStorage.setItem(EFFORT_STORAGE_KEY, 'medium');
    render(<EffortSlider supported />);
    expect(radio('Medium')).toHaveAttribute('aria-checked', 'true');
  });
});
