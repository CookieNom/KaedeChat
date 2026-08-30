import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { isVoiceMessage, voiceDurationLabel, voiceWaveformSamples } from './voice-messages';

const forwardedMessage = readFileSync(
  new URL('../components/ForwardedMessage.svelte', import.meta.url),
  'utf8'
);

describe('voice messages', () => {
  it('requires the stable flag and exactly one audio attachment', () => {
    const attachment = { content_type: 'audio/ogg' };
    expect(isVoiceMessage({ flags: 1 << 13, attachments: [attachment] as never[] })).toBe(true);
    expect(isVoiceMessage({ flags: 0, attachments: [attachment] as never[] })).toBe(false);
    expect(isVoiceMessage({ flags: 1 << 13, attachments: [] })).toBe(false);
  });

  it('decodes bounded waveform samples and formats the duration', () => {
    expect(voiceWaveformSamples('AP+A')).toEqual([0.12, 1, 128 / 255]);
    expect(voiceWaveformSamples('not base64')).toEqual([]);
    expect(voiceDurationLabel(65.4)).toBe('1:05');
    expect(voiceDurationLabel(null)).toBe('Audio');
  });

  it('renders flagged forwarded voice snapshots with the voice player', () => {
    expect(forwardedMessage).toContain('snapshot && isVoiceMessage(snapshot)');
    expect(forwardedMessage).toContain('<VoiceMessagePlayer {attachment} />');
  });
});
