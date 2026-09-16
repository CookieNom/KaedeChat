// @vitest-environment happy-dom
import { expect, it, vi } from 'vitest';
import { Track, RoomEvent, type Room } from 'livekit-client';
import { VoiceSession } from './session';

it('separates voice from stream audio, including new publications, without affecting another person', async () => {
  const media = () => document.createElement('audio');
  const mic = media(),
    stream = media(),
    bob = media();
  const publication = (element: HTMLAudioElement, source: Track.Source) => ({
    source,
    track: { kind: Track.Kind.Audio, attach: () => element }
  });
  const listeners = new Map<string, (...args: unknown[]) => void>();
  const room = {
    remoteParticipants: new Map([
      [
        'alice',
        {
          identity: 'alice',
          audioTrackPublications: new Map([
            ['mic', publication(mic, Track.Source.Microphone)],
            ['stream', publication(stream, Track.Source.ScreenShareAudio)]
          ])
        }
      ],
      [
        'bob',
        {
          identity: 'bob',
          audioTrackPublications: new Map([['mic', publication(bob, Track.Source.Microphone)]])
        }
      ]
    ]),
    on: vi.fn((event, fn) => listeners.set(event, fn)),
    off: vi.fn()
  };
  const voice = new VoiceSession(() => room as unknown as Room);
  await voice.setListeningVolume('alice', false, 0);
  const detach = voice.attachAudio(document.body);
  expect(mic.volume).toBe(0);
  expect(stream.volume).toBe(1);
  expect(bob.volume).toBe(1);
  await voice.setListeningVolume('alice', true, 0.3);
  expect(stream.volume).toBe(0.3);
  expect(mic.volume).toBe(0);
  const republished = media();
  listeners.get(RoomEvent.TrackSubscribed)!(
    null,
    publication(republished, Track.Source.ScreenShareAudio),
    { identity: 'alice' }
  );
  expect(republished.volume).toBe(0.3);
  await voice.setListeningVolume('alice', true, Number.NaN);
  expect(republished.volume).toBe(0.3);
  detach();
  expect(document.body.children).toHaveLength(0);
});
