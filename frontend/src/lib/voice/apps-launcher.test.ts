import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const voiceDock = readFileSync(new URL('./VoiceDock.svelte', import.meta.url), 'utf8');
const launcher = readFileSync(
  new URL('../components/ApplicationCommandLauncher.svelte', import.meta.url),
  'utf8'
);
const guildChannelRoute = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/[channelId]/+page.svelte', import.meta.url),
  'utf8'
);

describe('voice Apps launcher', () => {
  it('keeps Apps between screen sharing and mute like Discord desktop', () => {
    expect(voiceDock).toContain('onApps?: () => void;');
    expect(voiceDock).toContain('aria-label="Open Apps"');
    expect(voiceDock).toContain('aria-haspopup="dialog"');
    expect(voiceDock).toContain('onclick={onApps}');
    expect(voiceDock).toContain('<Icon name="sparkles" size={20} />');
    const screenShare = voiceDock.indexOf('aria-label={view.screen ?');
    const apps = voiceDock.indexOf('{#if onApps}');
    const microphone = voiceDock.indexOf("aria-label={view.microphone ? 'Mute microphone'");
    expect(screenShare).toBeGreaterThan(-1);
    expect(apps).toBeGreaterThan(screenShare);
    expect(microphone).toBeGreaterThan(apps);
  });

  it('opens the route-owned launcher and preserves command execution in voice', () => {
    expect(launcher).toContain('open = $bindable(false)');
    expect(launcher).toContain('showTrigger = true');
    expect(launcher).toContain('{#if showTrigger}');
    expect(guildChannelRoute).toContain('let applicationLauncherOpen = $state(false);');
    expect(guildChannelRoute).toContain(
      'onApps={channelReady ? () => (applicationLauncherOpen = true) : undefined}'
    );
    expect(guildChannelRoute.match(/bind:open=\{applicationLauncherOpen\}/gu)).toHaveLength(1);
    expect(guildChannelRoute).toContain('showTrigger={false}');
    expect(guildChannelRoute).toContain('compact');
    expect(guildChannelRoute).toContain('class="channel-dialog voice-command-dialog"');
    expect(guildChannelRoute).toContain(
      '{@render applicationCommandFields(selectedApplicationCommand)}'
    );
    expect(guildChannelRoute).toContain('void send();');
  });

  it('uses the shared base64url encoder for the voice connection nonce', () => {
    expect(voiceDock).toContain("import { base64url, randomBytes } from '$lib/e2ee/encoding';");
    expect(voiceDock).toContain('let connectionId = base64url(randomBytes(32));');
    expect(voiceDock).not.toContain('btoa(');
  });
});
