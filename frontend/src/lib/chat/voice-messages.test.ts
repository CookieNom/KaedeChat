// @vitest-environment happy-dom
import { mount, unmount, flushSync, tick } from 'svelte';
import type { Message } from './types';
import ForwardedMessage from '$lib/components/ForwardedMessage.svelte';
const media = vi.hoisted(() => ({ load: vi.fn() }));
vi.mock('$lib/api/client', async (original) => ({
  ...(await original<typeof import('$lib/api/client')>()),
  api: vi.fn(async () => ({
    source_channel_ref: '5@chat.example',
    source_message_ref: '6@chat.example'
  }))
}));
vi.mock('$lib/media/authenticated', async (original) => ({
  ...(await original<typeof import('$lib/media/authenticated')>()),
  authenticatedMedia: media.load
}));
let component: ReturnType<typeof mount> | undefined;
afterEach(async () => {
  if (component) await unmount(component);
  component = undefined;
  document.body.replaceChildren();
});
import { afterEach, describe, expect, it, vi } from 'vitest';
import { isVoiceMessage, voiceDurationLabel, voiceWaveformSamples } from './voice-messages';

describe('voice messages', () => {
  it('requires the stable flag and exactly one audio attachment', () => {
    const attachment = { content_type: 'audio/ogg' };
    expect(isVoiceMessage({ flags: 1 << 13, attachments: [attachment] as never[] })).toBe(true);
    expect(isVoiceMessage({ flags: 0, attachments: [attachment] as never[] })).toBe(false);
    expect(isVoiceMessage({ flags: 1 << 13, attachments: [] })).toBe(false);
    expect(
      isVoiceMessage({ flags: 1 << 13, attachments: [attachment, attachment] as never[] })
    ).toBe(false);
    expect(
      isVoiceMessage({ flags: 1 << 13, attachments: [{ content_type: 'image/png' }] as never[] })
    ).toBe(false);
  });
  it('decodes bounded waveform samples and formats the duration', () => {
    const samples = voiceWaveformSamples('AP+A');
    expect(samples).toHaveLength(3);
    expect(samples[0]).toBeGreaterThan(0);
    expect(samples[0]).toBeLessThan(samples[2]);
    expect(samples.slice(1)).toEqual([1, 128 / 255]);
    expect(voiceWaveformSamples('not base64')).toEqual([]);
    expect(voiceDurationLabel(65.4)).toBe('1:05');
    expect(voiceDurationLabel(null)).toBe('Audio');
  });

  it('renders flagged forwarded voice snapshots with the voice player', async () => {
    component = mount(ForwardedMessage, {
      target: document.body,
      props: {
        message: {
          id: '3',
          origin_domain: 'chat.example',
          channel_id: '2',
          channel_domain: 'chat.example',
          message_snapshots: [
            {
              message: {
                content: '',
                flags: 1 << 13,
                created_at: '2026-01-01T00:00:00Z',
                attachments: [
                  {
                    id: '4',
                    origin_domain: 'chat.example',
                    content_type: 'audio/ogg',
                    filename: 'voice.ogg',
                    duration_secs: 65,
                    waveform: 'AP+A'
                  }
                ],
                embeds: [],
                components: [],
                sticker_items: []
              }
            }
          ]
        } as unknown as Message
      }
    });
    flushSync();
    await tick();
    const audio = document.querySelector(
      'audio[aria-label="Play voice message voice.ogg"]'
    ) as HTMLAudioElement;
    expect(audio).not.toBeNull();
    expect(audio.controls).toBe(true);
    expect(media.load).toHaveBeenCalledWith(
      audio,
      expect.objectContaining({
        path: expect.stringContaining('/4/original'),
        contentType: 'audio/ogg'
      })
    );
  });
});
