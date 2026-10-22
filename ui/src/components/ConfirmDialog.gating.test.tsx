/**
 * @vitest-environment jsdom
 *
 * The typed-phrase gate on ConfirmDialog.
 *
 * `ConfirmDialog.test.ts` covers the module's shape only. This file covers the
 * behaviour that exists specifically to stop an accident: the confirmation
 * required before an irreversible action, and the focus/reset details that make
 * it a real gate rather than a decoration.
 *
 * Every existing call site passes no `requiredPhrase`, so the first test here is
 * the regression guard for them.
 */
import '@testing-library/jest-dom';
import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import ConfirmDialog from './ConfirmDialog';

const base = {
  open: true,
  title: 'Delete thing',
  message: 'Are you sure?',
  onConfirm: vi.fn(),
  onCancel: vi.fn(),
};

describe('ConfirmDialog without a required phrase', () => {
  it('confirms immediately and focuses Confirm, as it always has', () => {
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...base} onConfirm={onConfirm} confirmLabel="Delete" />);

    const confirm = screen.getByRole('button', { name: 'Delete' });
    expect(confirm).toBeEnabled();
    expect(confirm).toHaveFocus();
    expect(screen.queryByRole('textbox')).toBeNull();

    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalled();
  });
});

describe('ConfirmDialog with a required phrase', () => {
  it('blocks Confirm until the phrase matches exactly', () => {
    render(<ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" />);
    const confirm = screen.getByRole('button', { name: 'Delete' });
    const input = screen.getByLabelText('Type my-agent to confirm');

    expect(confirm).toBeDisabled();

    fireEvent.change(input, { target: { value: 'my-age' } });
    expect(confirm).toBeDisabled();

    // Case matters: a lineage named `My-Agent` is a different lineage.
    fireEvent.change(input, { target: { value: 'MY-AGENT' } });
    expect(confirm).toBeDisabled();

    fireEvent.change(input, { target: { value: 'my-agent' } });
    expect(confirm).toBeEnabled();
  });

  it('tolerates surrounding whitespace', () => {
    // Copying the name out of the row picks up a trailing space; that is a paste
    // artifact, not a different intent.
    render(<ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" />);
    fireEvent.change(screen.getByLabelText('Type my-agent to confirm'), {
      target: { value: '  my-agent ' },
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('focuses the input, not Confirm', () => {
    // Focusing Confirm would put an irreversible action one Enter away.
    render(<ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" />);
    expect(screen.getByLabelText('Type my-agent to confirm')).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Delete' })).not.toHaveFocus();
  });

  it('clears the typed phrase when reopened', () => {
    // Otherwise a dialog that was cancelled comes back already armed, and the
    // next Delete click is one further click from destroying something.
    const { rerender } = render(
      <ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" />,
    );
    fireEvent.change(screen.getByLabelText('Type my-agent to confirm'), {
      target: { value: 'my-agent' },
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();

    rerender(<ConfirmDialog {...base} open={false} confirmLabel="Delete" requiredPhrase="my-agent" />);
    rerender(<ConfirmDialog {...base} open confirmLabel="Delete" requiredPhrase="my-agent" />);

    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
    expect(screen.getByLabelText('Type my-agent to confirm')).toHaveValue('');
  });

  it('honours confirmDisabled even when the phrase matches', () => {
    // The caller's own gate — e.g. a preview that has not loaded yet.
    render(
      <ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" confirmDisabled />,
    );
    fireEvent.change(screen.getByLabelText('Type my-agent to confirm'), {
      target: { value: 'my-agent' },
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();
  });

  it('renders an error without closing, and keeps the typed phrase', () => {
    const { rerender } = render(
      <ConfirmDialog {...base} confirmLabel="Delete" requiredPhrase="my-agent" />,
    );
    fireEvent.change(screen.getByLabelText('Type my-agent to confirm'), {
      target: { value: 'my-agent' },
    });
    rerender(
      <ConfirmDialog
        {...base}
        confirmLabel="Delete"
        requiredPhrase="my-agent"
        error="cancelled after removing [2]"
      />,
    );

    expect(screen.getByText('cancelled after removing [2]')).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();
    // A retry must not require retyping the name.
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('renders caller-supplied children', () => {
    render(
      <ConfirmDialog {...base} requiredPhrase="my-agent">
        <p>2 published versions</p>
      </ConfirmDialog>,
    );
    expect(screen.getByText('2 published versions')).toBeTruthy();
  });

  it('still cancels on Escape', () => {
    const onCancel = vi.fn();
    render(<ConfirmDialog {...base} onCancel={onCancel} requiredPhrase="my-agent" />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalled();
  });

  it('disables the input while the action is in flight', () => {
    render(<ConfirmDialog {...base} requiredPhrase="my-agent" loading />);
    expect(screen.getByLabelText('Type my-agent to confirm')).toBeDisabled();
  });
});
