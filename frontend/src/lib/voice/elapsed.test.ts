import { describe, expect, it } from 'vitest';

import { formatVoiceElapsed, voiceStartTimeFromDispatch } from './elapsed';

const channel = { id: '7', origin_domain: 'guild.example' };

describe('voice channel elapsed state', () => {
  it('matches federated start-time updates and preserves an explicit clear', () => {
    expect(
      voiceStartTimeFromDispatch(
        'VOICE_CHANNEL_START_TIME_UPDATE',
        {
          id: '7',
          origin_domain: 'guild.example',
          guild_domain: 'guild.example',
          voice_start_time: 1_777_777_777
        },
        channel
      )
    ).toBe(1_777_777_777);
    expect(
      voiceStartTimeFromDispatch(
        'VOICE_CHANNEL_START_TIME_UPDATE',
        {
          id: '7',
          origin_domain: 'guild.example',
          voice_start_time: null
        },
        channel
      )
    ).toBeNull();
  });

  it('reads opcode-43 snapshots without crossing authority domains', () => {
    const payload = {
      guild_domain: 'guild.example',
      channels: [
        { id: '7', origin_domain: 'other.example', voice_start_time: 1 },
        { id: '7', voice_start_time: 2 }
      ]
    };
    expect(voiceStartTimeFromDispatch('CHANNEL_INFO', payload, channel)).toBe(2);
    expect(
      voiceStartTimeFromDispatch('CHANNEL_INFO', payload, {
        id: '7',
        origin_domain: 'missing.example'
      })
    ).toBeUndefined();
  });

  it('formats minute and hour durations and clamps clock skew', () => {
    expect(formatVoiceElapsed(1_000, 1_065_000)).toBe('1:05');
    expect(formatVoiceElapsed(1_000, 4_661_000)).toBe('1:01:01');
    expect(formatVoiceElapsed(1_000, 999_000)).toBe('0:00');
    expect(formatVoiceElapsed(null, 1_000_000)).toBeNull();
  });
});
