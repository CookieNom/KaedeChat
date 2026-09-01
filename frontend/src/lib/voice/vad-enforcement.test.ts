import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const voiceDock = readFileSync(new URL('./VoiceDock.svelte', import.meta.url), 'utf8');

describe('browser voice-activity permission UI', () => {
  it('renders an enforced pointer-and-keyboard hold-to-talk control for no-VAD grants', () => {
    expect(voiceDock).toContain('{#if view.pushToTalkRequired}');
    expect(voiceDock).toContain('aria-label="Hold to talk"');
    expect(voiceDock).toContain('onpointerdown={pushToTalkPointerDown}');
    expect(voiceDock).toContain('onpointerup={pushToTalkPointerUp}');
    expect(voiceDock).toContain('onpointercancel={pushToTalkPointerUp}');
    expect(voiceDock).toContain('onkeydown={pushToTalkKeyDown}');
    expect(voiceDock).toContain('onkeyup={pushToTalkKeyUp}');
  });

  it('closes push-to-talk capture when the tab loses focus or becomes hidden', () => {
    expect(voiceDock).toContain("window.addEventListener('blur', releasePushToTalk)");
    expect(voiceDock).toContain(
      "document.addEventListener('visibilitychange', releasePushToTalkWhenHidden)"
    );
  });

  it('reconciles live browser grants when effective channel permissions change', () => {
    expect(voiceDock).toContain('voice.reconcileBrowserPermissions(next)');
    expect(voiceDock).toContain('void requireFreshBrowserVoiceGrant();');
    expect(voiceDock).toContain('Permission.ADMINISTRATOR | Permission.USE_VAD');
  });

  it('uses the authoritative Stage speaker grant for live promotion and VAD', () => {
    expect(voiceDock).toContain('if (isStageChannel) {');
    expect(voiceDock).toContain('canSpeak: view.canSpeak');
    expect(voiceDock).toContain('.reconcileParticipantPermissions({');
    expect(voiceDock).toContain('canUseVad: canSpeak');
  });

  it('gates Stage moderation by the target member hierarchy', () => {
    expect(voiceDock).toContain('function canModerateStageOccupant(occupant: VoiceOccupant)');
    expect(voiceDock).toContain(
      'return guildMemberOutranks(guild, currentUser, target, guildMembers);'
    );
    expect(voiceDock).toContain('{#if !self && canModerateStageOccupant(occupant)}');
    expect(voiceDock).toContain('if (!self && !canModerateStageOccupant(occupant)) return;');
  });
});
