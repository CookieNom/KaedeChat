import { describe, expect, it } from 'vitest';
import { shouldPlayTts, ttsCommand, ttsPreferencesFromSettings } from './tts';

describe('text to speech', () => {
  it('recognizes only the built-in /tts command and strips the command token', () => {
    expect(ttsCommand('/tts hello')).toEqual({ matched: true, content: 'hello' });
    expect(ttsCommand('/TTS   hello there')).toEqual({ matched: true, content: 'hello there' });
    expect(ttsCommand('/tts')).toEqual({ matched: true, content: '' });
    expect(ttsCommand('/ttsx hello')).toEqual({ matched: false, content: '' });
  });

  it('defaults to silent playback and honors current-channel selection', () => {
    const defaults = ttsPreferencesFromSettings({});
    expect(defaults).toEqual({ enabled: false, playback: 'never', rate: 1 });
    const message = {
      tts: true,
      content: 'hello',
      channel_id: '10',
      channel_domain: 'chat.example'
    };
    expect(
      shouldPlayTts(message, '10@chat.example', {
        enabled: true,
        playback: 'current',
        rate: 1
      })
    ).toBe(true);
    expect(
      shouldPlayTts(message, '11@chat.example', {
        enabled: true,
        playback: 'current',
        rate: 1
      })
    ).toBe(false);
  });

  it('speaks encrypted text only after authenticated decryption', () => {
    const encrypted = {
      tts: true,
      content: 'untrusted outer text',
      decrypted_content: 'authenticated secret',
      e2ee: { ciphertext: 'opaque' },
      e2ee_verified: false,
      channel_id: '10',
      channel_domain: 'chat.example'
    };
    const preferences = { enabled: true, playback: 'all' as const, rate: 1 };

    expect(shouldPlayTts(encrypted, null, preferences)).toBe(false);
    expect(shouldPlayTts({ ...encrypted, e2ee_verified: true }, null, preferences)).toBe(true);
  });
});
