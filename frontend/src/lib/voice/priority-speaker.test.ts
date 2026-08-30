import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { nativePrioritySpeakerIdentities } from '$lib/platform/native';

const voiceDock = readFileSync(new URL('./VoiceDock.svelte', import.meta.url), 'utf8');
const voiceSession = readFileSync(new URL('./session.ts', import.meta.url), 'utf8');
const settings = readFileSync(
  new URL('../components/NativeVoiceSettings.svelte', import.meta.url),
  'utf8'
);

describe('native Priority Speaker UI', () => {
  it('accepts only concrete native participant identities and deduplicates them', () => {
    expect([
      ...nativePrioritySpeakerIdentities(['42@chat.example', '42@chat.example', '', 42, null])
    ]).toEqual(['42@chat.example']);
    expect(nativePrioritySpeakerIdentities(null).size).toBe(0);
    expect(nativePrioritySpeakerIdentities(['not an identity']).size).toBe(0);
    expect(voiceSession).toContain(
      'this.#nativePrioritySpeakers = nativePrioritySpeakerIdentities(status.priority_speakers);'
    );
    expect(voiceSession).toContain('this.#nativePrioritySpeakers.clear();');
  });

  it('keeps the cue visible in participant and video layouts', () => {
    expect(voiceDock).toContain('class:priority-speaker={participant.priority}');
    expect(voiceDock).toContain('class="priority-speaker-roster" role="status"');
    expect(voiceDock).toContain('<Icon name="megaphone" size={17} />');
  });

  it('offers a separate priority keybind only in the push-to-talk settings branch', () => {
    const pushToTalkBranch = settings.indexOf('{:else}');
    const priorityKeybind = settings.indexOf(
      'bind:value={preferences.priority_push_to_talk_hotkey}'
    );
    expect(pushToTalkBranch).toBeGreaterThan(-1);
    expect(priorityKeybind).toBeGreaterThan(pushToTalkBranch);
    expect(settings).toContain('when your role has Priority Speaker permission');
  });
});
