// RegisterGatewayModal.test.ts — Unit tests for the modal's pure helpers.
//
// The interactive form (focus, submit, error banner, secret-clearing) is
// covered by the planned component integration tests in task 8.6 — the
// host's `useIsGatewayAdmin` hook subscribes to `UserManager` and driving
// that under jsdom is overkill for what this task ships. We exercise:
//
//   1. `buildRegisterPayload` — every branch of the auth-type
//      discriminator, including that `client_secret` only lands on the
//      `jwt` payload and never leaks into `sigv4` or `none`.
//   2. `formatRegisterError` — every documented error code resolves to a
//      stable, human-readable banner.
//   3. The default export is a function component.

import { describe, it, expect } from 'vitest';
import RegisterGatewayModal, {
  buildRegisterPayload,
  formatRegisterError,
} from './RegisterGatewayModal';

const baseForm = {
  id: 'audio',
  name: 'Audio Gateway',
  description: 'Speech tools',
  url: 'https://example.com/mcp',
  auth_type: 'jwt' as const,
  token_endpoint: 'https://auth.example.com/token',
  client_id: 'client-123',
  client_secret: 'shhh',
  scope: 'gateway/invoke',
  region: '',
  service: '',
};

describe('buildRegisterPayload', () => {
  it('builds a jwt payload with the secret on auth_config', () => {
    const payload = buildRegisterPayload({ ...baseForm, auth_type: 'jwt' });
    expect(payload.auth_type).toBe('jwt');
    expect(payload.auth_config).toEqual({
      auth_type: 'jwt',
      token_endpoint: 'https://auth.example.com/token',
      client_id: 'client-123',
      client_secret: 'shhh',
      scope: 'gateway/invoke',
    });
    expect(payload.enabled).toBe(true);
  });

  it('builds a sigv4 payload without any secret fields', () => {
    const payload = buildRegisterPayload({
      ...baseForm,
      auth_type: 'sigv4',
      region: 'us-east-1',
      service: 'bedrock-agentcore',
    });
    expect(payload.auth_type).toBe('sigv4');
    expect(payload.auth_config).toEqual({
      auth_type: 'sigv4',
      region: 'us-east-1',
      service: 'bedrock-agentcore',
    });
    // Defensive: no secret should appear on the sigv4 branch even when
    // the form still carries jwt-form values from a prior auth-type
    // selection.
    expect(JSON.stringify(payload)).not.toContain('shhh');
  });

  it('builds a none payload with an empty auth_config', () => {
    const payload = buildRegisterPayload({ ...baseForm, auth_type: 'none' });
    expect(payload.auth_type).toBe('none');
    expect(payload.auth_config).toEqual({ auth_type: 'none' });
    expect(JSON.stringify(payload)).not.toContain('shhh');
  });

  it('trims whitespace on string fields', () => {
    const payload = buildRegisterPayload({
      ...baseForm,
      id: '  audio  ',
      name: '  Audio  ',
      url: '  https://example.com/mcp  ',
      auth_type: 'jwt',
      token_endpoint: '  https://auth.example.com/token  ',
      client_id: '  client-123  ',
      scope: '  gateway/invoke  ',
    });
    expect(payload.id).toBe('audio');
    expect(payload.name).toBe('Audio');
    expect(payload.url).toBe('https://example.com/mcp');
    if (payload.auth_config.auth_type === 'jwt') {
      expect(payload.auth_config.token_endpoint).toBe(
        'https://auth.example.com/token',
      );
      expect(payload.auth_config.client_id).toBe('client-123');
      expect(payload.auth_config.scope).toBe('gateway/invoke');
      // The secret itself is intentionally not trimmed — leading/trailing
      // whitespace can be significant in a credential, and the runtime is
      // the source of truth on whether it's valid.
      expect(payload.auth_config.client_secret).toBe('shhh');
    } else {
      throw new Error('expected jwt auth_config');
    }
  });
});

describe('formatRegisterError', () => {
  it('renders a clear banner for admin_required', () => {
    expect(formatRegisterError('admin_required')).toMatch(/gateway-admins/);
  });

  it('renders a clear banner for duplicate_id', () => {
    expect(formatRegisterError('duplicate_id')).toMatch(/already exists/i);
  });

  it('includes the message for invalid_payload when provided', () => {
    expect(formatRegisterError('invalid_payload', 'url is required'))
      .toMatch(/url is required/);
  });

  it('falls back to a generic invalid_payload banner without a message', () => {
    expect(formatRegisterError('invalid_payload')).toMatch(/invalid/i);
  });

  it('renders a clear banner for actor_unresolved', () => {
    expect(formatRegisterError('actor_unresolved')).toMatch(/sign/i);
  });

  it('renders a clear banner for gateway_registry_unavailable', () => {
    expect(formatRegisterError('gateway_registry_unavailable'))
      .toMatch(/registry/i);
  });

  it('falls back to the message for unknown errors', () => {
    expect(formatRegisterError('unknown', 'rpc timeout')).toBe('rpc timeout');
  });

  it('falls back to a generic banner for unknown errors with no message', () => {
    expect(formatRegisterError('unknown')).toMatch(/unknown reason/i);
  });
});

describe('RegisterGatewayModal', () => {
  it('exports a function component', () => {
    expect(typeof RegisterGatewayModal).toBe('function');
  });
});
