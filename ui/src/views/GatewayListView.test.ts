// GatewayListView.test.ts — Unit tests for the gateway-list view's pure
// helpers and module export shape.
//
// The component itself depends on `useIsGatewayAdmin`, which subscribes to
// the OIDC `UserManager`. Driving that under jsdom is overkill for what
// this task ships (a static read-only table). We exercise:
//   1. The pure auth-type formatter — every branch of the discriminator.
//   2. That the default export is a function component.
//
// The interactive admin/non-admin gating, fetch, and table rendering are
// covered by the planned component integration tests in task 8.6.

import { describe, it, expect } from 'vitest';
import GatewayListView, {
  describeTestConnectionError,
  embeddedHeaderActionsStyle,
  formatAuthType,
  tableStyle,
  tableWrapperStyle,
} from './GatewayListView';
import {
  SETTINGS_CLOSE_BUTTON_GUTTER,
  SETTINGS_CLOSE_BUTTON_INSET,
  SETTINGS_CLOSE_BUTTON_SIZE,
} from '../components/settings/panelChrome';

describe('formatAuthType', () => {
  it('formats jwt as "OAuth (JWT)"', () => {
    expect(formatAuthType('jwt')).toBe('OAuth (JWT)');
  });

  it('formats sigv4 as "AWS SigV4"', () => {
    expect(formatAuthType('sigv4')).toBe('AWS SigV4');
  });

  it('formats none as "None"', () => {
    expect(formatAuthType('none')).toBe('None');
  });

  it('falls back to a stringified label for unknown auth types', () => {
    // The runtime currently restricts auth_type to the three values above,
    // but a future server-side addition should not crash the table — the
    // exhaustiveness fallback should produce something readable.
    const future = 'mtls' as unknown as Parameters<typeof formatAuthType>[0];
    expect(formatAuthType(future)).toBe('mtls');
  });
});

describe('describeTestConnectionError', () => {
  it('renders a clear banner for admin_required', () => {
    expect(describeTestConnectionError('admin_required', undefined, 'Audio'))
      .toMatch(/gateway-admins/);
  });

  it('renders a clear banner for not_found with the gateway name', () => {
    expect(describeTestConnectionError('not_found', undefined, 'Audio'))
      .toMatch(/Audio/);
  });

  it('includes the runtime message for invalid_payload when provided', () => {
    expect(
      describeTestConnectionError('invalid_payload', 'missing id', 'Audio'),
    ).toMatch(/missing id/);
  });

  it('falls back to a generic banner for invalid_payload without a message', () => {
    expect(describeTestConnectionError('invalid_payload', undefined, 'Audio'))
      .toMatch(/invalid/i);
  });

  it('includes the probe error message for probe_failed', () => {
    expect(
      describeTestConnectionError('probe_failed', '401 Unauthorized', 'Audio'),
    ).toMatch(/401 Unauthorized/);
  });

  it('uses one wording for every connection failure', () => {
    // A connection test is one thing everywhere now (initialize + tools/list),
    // so probe and registration failures read the same rather than implying
    // different diagnoses.
    for (const code of ['probe_failed', 'registration_failed']) {
      expect(describeTestConnectionError(code, undefined, 'Audio'))
        .toMatch(/Couldn't connect/i);
    }
  });

  it('carries the runtime message when there is one', () => {
    expect(
      describeTestConnectionError('probe_failed', 'HTTP 401', 'Audio'),
    ).toMatch(/Couldn't connect: HTTP 401/);
  });

  it('keeps non-connection failures distinct, since each has a different fix', () => {
    const admin = describeTestConnectionError('admin_required', undefined, 'Audio');
    const missing = describeTestConnectionError('not_found', undefined, 'Audio');
    expect(admin).toMatch(/gateway-admins/);
    expect(missing).toMatch(/no longer exists/);
    expect(admin).not.toEqual(missing);
  });

  it('falls back to the runtime message for unknown codes', () => {
    expect(describeTestConnectionError('unknown', 'rpc timeout', 'Audio'))
      .toBe('rpc timeout');
  });
});

describe('GatewayListView', () => {
  it('exports a function component', () => {
    expect(typeof GatewayListView).toBe('function');
  });
});

// The six-column table is wider than the Settings modal's content pane (~864px
// against a ~904px min-content table). It used to be CLIPPED rather than
// scrollable, so the Actions column could not be reached at all — a silent,
// type-check-invisible layout failure, which is why these are asserted directly.
describe('gateway table overflow', () => {
  it('scrolls horizontally instead of clipping', () => {
    expect(tableWrapperStyle.overflowX).toBe('auto');
  });

  it('lets the wrapper shrink below the table it contains', () => {
    // The wrapper is a flex item, and `min-width: auto` floors a flex item at its
    // min-content width — so without this it refused to shrink to the pane and
    // pushed past it, where the pane's own `overflow-x: hidden` cut it off.
    // Omitting this makes `overflowX: 'auto'` above do nothing.
    expect(tableWrapperStyle.minWidth).toBe(0);
  });

  it('still clips the other axis, so the rounded corners survive', () => {
    expect(tableWrapperStyle.overflowY).toBe('hidden');
    expect(tableWrapperStyle.overflow).toBeUndefined();
  });

  it('gives the table a width floor so columns stay legible', () => {
    // `width: 100%` alone lets the browser crush URL and Actions into slivers
    // before it ever overflows. Past this floor the wrapper scrolls instead.
    expect(tableStyle.width).toBe('100%');
    expect(typeof tableStyle.minWidth).toBe('number');
    expect(tableStyle.minWidth as number).toBeGreaterThan(700);
  });
});

// The Settings modal's ✕ is absolutely positioned at the modal's top-right and
// floats OVER the content pane, so this panel's right-aligned Refresh /
// "Register new gateway" row rendered underneath it.
describe('embedded header clears the modal close button', () => {
  it('reserves the close button footprint', () => {
    expect(embeddedHeaderActionsStyle.paddingRight).toBe(SETTINGS_CLOSE_BUTTON_GUTTER);
  });

  it('derives the gutter from the button metrics so the two cannot drift', () => {
    expect(SETTINGS_CLOSE_BUTTON_GUTTER).toBe(
      SETTINGS_CLOSE_BUTTON_SIZE + SETTINGS_CLOSE_BUTTON_INSET,
    );
  });
});
