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
    expect(apiErrorMessage('HTTP_403', 403, { message: 'Forbidden' })).toBe(
      "You don't have permission to do that."
    );
    expect(apiErrorMessage('REQUEST_FAILED', 409, { message: 'Conflict' })).toBe(
      'That information changed somewhere else. Reload and try again.'
    );
  });

  it('includes retry timing returned by the server', () => {
    expect(apiErrorMessage('RATE_LIMITED', 429, { retry_after_ms: 2_100 })).toBe(
      'You are doing that too quickly. Try again in 3 seconds.'
    );
  });

  it('shows a safe support reference for server failures without exposing technical detail', () => {
    expect(
      apiErrorMessage('INTERNAL_SERVER_ERROR', 500, {
        message: 'SQLAlchemy MissingGreenlet at /home/service/media.py',
        trace_id: 'aabbccddeeff00112233445566778899'
      })
    ).toBe(
      'The server encountered an unexpected problem and could not complete this request. Try again shortly. Error reference: aabbccddeeff00112233445566778899.'
    );
  });

  it('accepts the backend trace-id alphabet, including short and dotted references', () => {
    expect(
      apiErrorMessage('INTERNAL_SERVER_ERROR', 500, {
        trace_id: 'edge.7-a'
      })
    ).toBe(
      'The server encountered an unexpected problem and could not complete this request. Try again shortly. Error reference: edge.7-a.'
    );
  });

  it('formats upload limits supplied by the server', () => {
    expect(
      apiErrorMessage('ATTACHMENT_TOO_LARGE', 413, {
        max_bytes: 5 * 1024 * 1024
      })
    ).toBe(
      'That attachment exceeds this instance’s size limit. The maximum allowed size is 5 MiB.'
    );
  });

  it('explains federation cache limits without blaming the user or suggesting message deletion', () => {
    expect(apiErrorMessage('KAED_FED_REPLICA_QUOTA_EXCEEDED', 507, {})).toBe(
      'This guild’s local replica reached its cache limit. New messages and changes may be missing until your instance frees space.'
    );
    expect(apiErrorMessage('FEDERATED_DM_STORAGE_QUOTA_EXCEEDED', 507, {})).toBe(
      'This instance could not retain more direct-message data. Recent remote messages are normally kept by removing the oldest cached copies; if this persists, contact your instance administrator.'
    );
    expect(apiErrorMessage('FEDERATED_DM_HISTORY_UNAVAILABLE', 503, {})).toBe(
      'Older messages could not be loaded from their home instance right now. Your recent messages are still available; try again in a moment.'
    );
    expect(apiErrorMessage('KAED_FED_HISTORY_CAPACITY', 429, { retry_after_ms: 60_000 })).toBe(
      'This instance is already importing the maximum amount of remote message history. The import will be retried automatically. Try again in 1 minute.'
    );
    expect(apiErrorMessage('FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED', 507, {})).toBe(
      'This instance cannot cache another remote account right now. Contact your instance administrator if this continues.'
    );
    expect(apiErrorMessage('FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED', 507, {})).toBe(
      'This instance cannot cache another remote server right now. Contact your instance administrator if this continues.'
    );
    expect(apiErrorMessage('FEDERATION_OUTBOX_CAPACITY_EXCEEDED', 507, {})).toContain(
      'Nothing was saved'
    );
    expect(apiErrorMessage('KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED', 507, {})).toBe(
      'The receiving instance cannot accept another pending friend request right now. Your request was not delivered.'
    );
    expect(apiErrorMessage('FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE', 503, {})).toContain(
      'retry automatically'
    );
    expect(apiErrorMessage('FEDERATED_GUILD_HISTORY_LIMIT_REACHED', 507, {})).toContain(
      'Recent messages and new activity remain available'
    );
    expect(apiErrorMessage('FEDERATED_GUILD_HISTORY_REJECTED', 409, {})).toContain(
      'could not be safely imported'
    );
  });

  it('identifies the first invalid field without showing the raw response object', () => {
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
    expect(
      apiErrorMessage('VALIDATION_ERROR', 422, {
        errors: [{ location: ['body'], message: 'Field required' }]
      })
    ).toBe('Some information is missing or invalid. Check it and try again.');
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
    expect(userErrorMessage(new TypeError('Failed to fetch'), 'Could not save.')).toBe(
      'Could not reach the server. Check your connection and try again.'
    );
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
