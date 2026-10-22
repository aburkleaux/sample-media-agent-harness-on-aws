/**
 * The "?" beside the notification bell — reopens the harness tour for anyone who skipped or missed it.
 * Styled as the bell's twin so the two read as one toolbar group in every theme.
 */
import { theme } from '../../styles/tokens';
import { startHarnessTour } from './startTour';

export default function HelpButton() {
  return (
    <button type="button" onClick={startHarnessTour} style={helpBtnStyle} aria-label="Take the tour" title="Take the tour" data-testid="help-tour">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M9.5 9.2a2.6 2.6 0 0 1 5 .9c0 1.7-2.5 2.3-2.5 3.9" />
        <path d="M12 17h.01" />
      </svg>
    </button>
  );
}

const helpBtnStyle: React.CSSProperties = {
  width: 30, height: 30, borderRadius: theme.radius.button,
  border: `1px solid ${theme.colors.border}`, background: theme.colors.surface,
  color: theme.colors.textSecondary, cursor: 'pointer', display: 'flex',
  alignItems: 'center', justifyContent: 'center',
};
