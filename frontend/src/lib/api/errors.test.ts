import { describe, expect, it } from 'vitest';
import {
  apiErrorMessage,
  ApiError,
  normalizeErrorDetail,
  trustedClientErrorMessage,
  userErrorMessage
} from './errors';

describe('user-facing API errors', () => {
  it('maps bare status phrases to an actionable explanation', () => {
    const forbidden = apiErrorMessage('HTTP_403', 403, { message: 'Forbidden' });
    expect(forbidden).toMatch(/permission/i);
    expect(forbidden).not.toBe('Forbidden');
    const conflict = apiErrorMessage('REQUEST_FAILED', 409, { message: 'Conflict' });
    expect(conflict).toMatch(/reload|refresh|retry|try again/i);
    expect(conflict).not.toBe('Conflict');
  });

  it('includes retry timing returned by the server', () => {
    expect(apiErrorMessage('RATE_LIMITED', 429, { retry_after_ms: 2_100 })).toContain('3 seconds');
  });

  it('shows a safe support reference for server failures without exposing technical detail', () => {
    const message = apiErrorMessage('INTERNAL_SERVER_ERROR', 500, {
      message: 'SQLAlchemy MissingGreenlet at /home/service/media.py',
      trace_id: 'aabbccddeeff00112233445566778899'
    });
    expect(message).toContain('aabbccddeeff00112233445566778899');
    expect(message).not.toMatch(/SQLAlchemy|MissingGreenlet|\/home\/service|media\.py/);
    expect(message).toMatch(/server|request/i);
  });

  it('accepts the backend trace-id alphabet, including short and dotted references', () => {
    expect(apiErrorMessage('INTERNAL_SERVER_ERROR', 500, { trace_id: 'edge.7-a' })).toContain(
      'edge.7-a'
    );
  });

  it('formats upload limits supplied by the server', () => {
    expect(apiErrorMessage('ATTACHMENT_TOO_LARGE', 413, { max_bytes: 5 * 1024 * 1024 })).toContain(
      '5 MiB'
    );
  });

  it('explains federation cache limits without blaming the user or suggesting message deletion', () => {
    for (const [code, status, expected] of [
      ['KAED_FED_REPLICA_QUOTA_EXCEEDED', 507, /replica|cache/i],
      ['FEDERATED_DM_STORAGE_QUOTA_EXCEEDED', 507, /direct.message/i],
      ['FEDERATED_DM_HISTORY_UNAVAILABLE', 503, /older|history/i],
      ['KAED_FED_HISTORY_CAPACITY', 429, /history/i],
      ['FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED', 507, /remote account/i],
      ['FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED', 507, /remote server/i],
      ['FEDERATION_OUTBOX_CAPACITY_EXCEEDED', 507, /not.*sav|nothing.*sav/i],
      ['KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED', 507, /not delivered/i],
      ['FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE', 503, /automatic/i],
      ['FEDERATED_GUILD_HISTORY_LIMIT_REACHED', 507, /recent|new activity/i],
      ['FEDERATED_GUILD_HISTORY_REJECTED', 409, /safe/i]
    ] as const) {
      const message = apiErrorMessage(code, status, {});
      expect(message).toMatch(expected);
      expect(message).not.toMatch(/delete your|you (?:caused|exceeded)/i);
    }
    expect(apiErrorMessage('KAED_FED_HISTORY_CAPACITY', 429, { retry_after_ms: 60_000 })).toContain(
      '1 minute'
    );
  });

  it('validation field rendering omits raw response', () => {
    expect(
      apiErrorMessage('VALIDATION_ERROR', 422, {
        errors: [{ location: ['body', 'display_name'], message: 'Field required' }]
      })
    ).toBe('Check Display Name: This field is required.');
    expect(
      apiErrorMessage('VALIDATION_ERROR', 422, {
        errors: [{ loc: ['body', 'display_name'], msg: 'Field required' }]
      })
    ).toBe('Check Display Name: This field is required.');
  });

  it('does not invent a “Value” field when validation has no useful location', () => {
    const message = apiErrorMessage('VALIDATION_ERROR', 422, {
      errors: [{ location: ['body'], message: 'Field required' }]
    });
    expect(message).not.toMatch(/\bValue\b/);
    expect(message).toMatch(/missing|invalid|required/i);
  });

  it('supports both current and legacy API error envelopes', () => {
    expect(normalizeErrorDetail({ code: 'USER_NOT_FOUND', message: 'No user' })).toEqual({
      code: 'USER_NOT_FOUND',
      message: 'No user'
    });
    expect(
      normalizeErrorDetail({ detail: { code: 'USER_NOT_FOUND', message: 'No user' } })
    ).toEqual({ code: 'USER_NOT_FOUND', message: 'No user' });
  });

  it('preserves trusted native recovery wording without trusting remote 5xx text', () => {
    expect(
      trustedClientErrorMessage(
        'Secure credential storage is locked. Unlock your keyring and try again.',
        'NATIVE_CREDENTIALS_LOCKED',
        503,
        { trace_id: 'native.4' }
      )
    ).toBe(
      'Secure credential storage is locked. Unlock your keyring and try again. Error reference: native.4.'
    );
    expect(
      apiErrorMessage('NATIVE_CREDENTIALS_LOCKED', 503, {
        message: 'Secure credential storage is locked. Unlock your keyring and try again.'
      })
    ).toBe('The service is temporarily unavailable. Try again shortly.');
  });
});

describe('non-API errors', () => {
  it('turns browser transport failures into useful connection guidance', () => {
    const message = userErrorMessage(new TypeError('Failed to fetch'), 'Could not save.');
    expect(message).toMatch(/connection|network/i);
    expect(message).not.toContain('Failed to fetch');
  });

  it('does not show technical runtime details', () => {
    expect(
      userErrorMessage(
        new Error('SQLAlchemy exception while reading /var/lib/kaede/secret'),
        'Could not save your changes.'
      )
    ).toBe('Could not save your changes.');
    expect(
      userErrorMessage(
        new TypeError("Cannot read properties of undefined (reading 'profile')"),
        'Could not save your changes.'
      )
    ).toBe('Could not save your changes.');
    expect(userErrorMessage(new Error('GATEWAY_NOT_CONNECTED'), 'Could not reconnect.')).toBe(
      'Could not reconnect.'
    );
  });

  it.each([
    'AxiosError: Request failed with status code 500',
    'Request failed with status code 503',
    'Upload rejected: token=do-not-show',
    'Could not call https://alice:password@example.test/private',
    'Could not call https://example.test/path?secret=do-not-show'
  ])('does not expose generic transport or credential-bearing text: %s', (message) => {
    expect(userErrorMessage(new Error(message), 'Could not complete the action. Try again.')).toBe(
      'Could not complete the action. Try again.'
    );
  });

  it('retains safe locally-authored explanations', () => {
    expect(
      userErrorMessage(new Error('The image did not pass media processing.'), 'Upload failed.')
    ).toBe('The image did not pass media processing.');
  });

  it('exposes structured metadata to callers without putting it in arbitrary messages', () => {
    const error = new ApiError('RATE_LIMITED', 'Wait.', 429, {
      retry_after_ms: 1_500,
      trace_id: 'trace-reference-1'
    });
    expect(error.retryAfterMs).toBe(1_500);
    expect(error.traceId).toBe('trace-reference-1');
  });
});
