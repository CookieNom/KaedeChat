import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  loadSoundboardMedia,
  SOUNDBOARD_MAX_BYTES,
  soundboardChannelSupported,
  soundboardPlaybackUnavailableReason,
  soundboardSourceAllowed,
  validateSoundboardMediaUrl
} from './soundboard';

const digestOfAbc = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad';

function request(overrides: Partial<Parameters<typeof loadSoundboardMedia>[0]> = {}) {
  return {
    downloadUrl: 'https://media.guild.example/sounds/one?signature=opaque',
    authorityDomain: 'guild.example',
    mediaOrigin: 'https://media.guild.example',
    expectedSha256: digestOfAbc,
    contentType: 'audio/ogg',
    ...overrides
  };
}

describe('soundboard media integrity', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('accepts only the exact authority-signed HTTPS media origin', () => {
    expect(
      validateSoundboardMediaUrl(
        'https://media.guild.example/object?signature=opaque',
        'Guild.Example',
        'https://media.guild.example'
      ).hostname
    ).toBe('media.guild.example');
    expect(
      validateSoundboardMediaUrl(
        'https://kaede-sounds.s3.example.com/object?signature=opaque',
        'guild.example',
        'https://kaede-sounds.s3.example.com'
      ).hostname
    ).toBe('kaede-sounds.s3.example.com');
    expect(() =>
      validateSoundboardMediaUrl(
        'https://media.guild.example.attacker.test/object',
        'guild.example',
        'https://media.guild.example'
      )
    ).toThrow('unexpected media origin');
    expect(() =>
      validateSoundboardMediaUrl(
        'https://media.guild.example:8443/object',
        'guild.example',
        'https://media.guild.example'
      )
    ).toThrow('unexpected media origin');
    expect(() =>
      validateSoundboardMediaUrl(
        'https://user@media.guild.example/object',
        'guild.example',
        'https://media.guild.example'
      )
    ).toThrow('unexpected media origin');
  });

  it('downloads without credentials or redirects and verifies SHA-256 before returning a blob', async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(new TextEncoder().encode('abc'), {
          status: 200,
          headers: { 'Content-Length': '3', 'Content-Type': 'audio/ogg' }
        })
    );
    vi.stubGlobal('fetch', fetcher);

    const blob = await loadSoundboardMedia(request());

    expect(blob.size).toBe(3);
    expect(blob.type).toBe('audio/ogg');
    expect(fetcher).toHaveBeenCalledWith(
      expect.objectContaining({ hostname: 'media.guild.example' }),
      expect.objectContaining({ credentials: 'omit', redirect: 'manual', cache: 'no-store' })
    );
  });

  it('rejects redirects before reading a replacement location', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(null, {
            status: 302,
            headers: { Location: 'https://attacker.test/sound' }
          })
      )
    );

    await expect(loadSoundboardMedia(request())).rejects.toThrow('redirected guild sound');
  });

  it('rejects both declared and streamed responses above the 512 KiB limit', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(new Uint8Array([1]), {
            headers: { 'Content-Length': String(SOUNDBOARD_MAX_BYTES + 1) }
          })
      )
    );
    await expect(loadSoundboardMedia(request())).rejects.toThrow('larger than Kaede');

    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(new Uint8Array(SOUNDBOARD_MAX_BYTES));
                controller.enqueue(new Uint8Array([1]));
                controller.close();
              }
            })
          )
      )
    );
    await expect(loadSoundboardMedia(request())).rejects.toThrow('larger than Kaede');
  });

  it('rejects bytes that do not match the signed sound digest', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(new TextEncoder().encode('changed')))
    );

    await expect(loadSoundboardMedia(request())).rejects.toThrow('failed its integrity check');
  });
});

describe('soundboard playback eligibility', () => {
  const ready = {
    connected: true,
    canSpeak: true,
    selfMuted: false,
    selfDeafened: false
  };

  it('is available only in guild voice channels, not Stages or DM calls', () => {
    expect(soundboardChannelSupported(2, false)).toBe(true);
    expect(soundboardChannelSupported(13, false)).toBe(false);
    expect(soundboardChannelSupported(2, true)).toBe(false);
  });

  it('requires an active, unsuppressed, unmuted speaker state', () => {
    expect(soundboardPlaybackUnavailableReason(ready)).toBeNull();
    expect(soundboardPlaybackUnavailableReason({ ...ready, connected: false })).toContain('Join');
    expect(soundboardPlaybackUnavailableReason({ ...ready, canSpeak: false })).toContain(
      'permission'
    );
    expect(soundboardPlaybackUnavailableReason({ ...ready, selfMuted: true })).toContain('Unmute');
    expect(soundboardPlaybackUnavailableReason({ ...ready, selfDeafened: true })).toContain(
      'Undeafen'
    );
    expect(soundboardPlaybackUnavailableReason({ ...ready, serverMuted: true })).toContain(
      'moderator'
    );
    expect(soundboardPlaybackUnavailableReason({ ...ready, serverDeafened: true })).toContain(
      'moderator'
    );
    expect(soundboardPlaybackUnavailableReason({ ...ready, suppressed: true })).toContain(
      'Stage speakers'
    );
  });

  it('uses full guild refs when enforcing external-sound permission', () => {
    expect(soundboardSourceAllowed('1@guild.example', null, false)).toBe(true);
    expect(soundboardSourceAllowed('1@guild.example', '1@guild.example', false)).toBe(true);
    expect(soundboardSourceAllowed('1@guild.example', '1@remote.example', false)).toBe(false);
    expect(soundboardSourceAllowed('1@guild.example', '1@remote.example', true)).toBe(true);
  });
});
