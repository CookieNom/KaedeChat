import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const guildRoute = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/[channelId]/+page.svelte', import.meta.url),
  'utf8'
);
const dmRoute = readFileSync(
  new URL('../../routes/(app)/home/[dmId]/+page.svelte', import.meta.url),
  'utf8'
);
const guildSettings = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/settings/+page.svelte', import.meta.url),
  'utf8'
);
const threadsPanel = readFileSync(new URL('./ThreadsPanel.svelte', import.meta.url), 'utf8');

describe('Discord parity cleanup', () => {
  it('uses slash autocomplete instead of an Apps button in message composers', () => {
    expect(guildRoute).toContain("if (completionQuery.marker === '/')");
    expect(dmRoute).toContain("completionQuery?.marker === '/'");
    expect(dmRoute).not.toContain('ApplicationCommandLauncher');
  });

  it('removes voice status editing and channel video quality controls', () => {
    expect(guildRoute).not.toContain('Set voice channel status');
    expect(guildRoute).not.toContain('/voice-status');
    expect(guildSettings).not.toContain('Video quality');
    expect(guildSettings).not.toContain('video_quality_mode:');
  });

  it('keeps header controls icon-sized and the Follow dialog above route layouts', () => {
    expect(threadsPanel).not.toContain('<span>Threads</span>');
    expect(guildRoute).toContain(
      '{#if announcementFollowOpen && channel && canFollowAnnouncements}\n  <div use:portal class="channel-dialog-layer">'
    );
  });
});
