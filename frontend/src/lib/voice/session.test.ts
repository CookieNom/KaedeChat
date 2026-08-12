import { describe, expect, it } from 'vitest';

import { isUsableVoiceToken, withVoiceConnectTimeout, type VoiceToken } from './session';

function grant(overrides: Partial<VoiceToken> = {}): VoiceToken {
  return {
    token: 'a'.repeat(64),
    url: 'wss://chat.example/livekit',
    room: 'g.1.2',
    generation: 0,
    expires_at: '2026-07-19T12:15:00Z',
    can_speak: true,
    can_stream: true,
    can_use_vad: true,
    ...overrides
  };
}

describe('voice grant validation', () => {
  it('accepts a future, scoped LiveKit grant', () => {
    expect(isUsableVoiceToken(grant(), Date.parse('2026-07-19T12:00:00Z'))).toBe(true);
  });

  it('rejects expired, non-WebSocket, and malformed room grants', () => {
    expect(isUsableVoiceToken(grant(), Date.parse('2026-07-19T12:15:00Z'))).toBe(false);
    expect(isUsableVoiceToken(grant({ url: 'https://chat.example/livekit' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ room: '../g.1.2' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ move_session_id: 'too-short' }), 0)).toBe(false);
    expect(isUsableVoiceToken(grant({ move_session_id: '!'.repeat(32) }), 0)).toBe(false);
    expect(
      isUsableVoiceToken(grant({ move_session_id: ['a'.repeat(32)] as unknown as string }), 0)
    ).toBe(false);
  });
});

describe('voice connection timeout', () => {
  it('rejects a signaling attempt that never settles', async () => {
    await expect(withVoiceConnectTimeout(new Promise(() => undefined), 1)).rejects.toThrow(
      'Voice connection timed out'
    );
  });
});
