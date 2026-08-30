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
const threadHeader = readFileSync(new URL('./ThreadHeader.svelte', import.meta.url), 'utf8');
const globalStyles = readFileSync(new URL('../../styles.css', import.meta.url), 'utf8');

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

  it('keeps Threads icon-sized and gives Follow its own labeled control', () => {
    expect(threadsPanel).not.toContain('<span>Threads</span>');
    expect(globalStyles).toMatch(/\.announcement-follow-button\s*\{[^}]*width: auto;/u);
    expect(guildRoute).toContain('aria-label="Follow announcement channel"');
    expect(guildRoute).toContain(
      '{#if announcementFollowOpen && channel && canFollowAnnouncements}\n  <div use:portal class="channel-dialog-layer">'
    );
  });

  it('dismisses the Threads panel on outside pointer presses and Escape', () => {
    expect(threadsPanel).toContain(
      '<svelte:window onpointerdown={dismissOnOutsidePointer} onkeydown={dismissOnEscape} />'
    );
    expect(threadsPanel).toContain('!panel.contains(target)');
    expect(threadsPanel).toContain("if (!open || event.key !== 'Escape') return;");
    expect(guildRoute).toContain('bind:this={guildNameMenu}');
    expect(guildRoute).toContain('!guildNameMenu.contains(event.target as Node)');
  });

  it('closes forum posts and dismisses the post actions menu', () => {
    expect(guildRoute).toMatch(/locked && forumParent\s+\? \{ archived: true, locked \}/u);
    expect(threadHeader).toContain(
      '<svelte:window onpointerdown={dismissThreadActions} onkeydown={dismissThreadActionsOnEscape} />'
    );
    expect(threadHeader).toContain('bind:this={threadActionsRoot}');
    expect(threadHeader).toContain('querySelectorAll<HTMLDetailsElement>');
    expect(threadHeader).toContain("'details[open]'");
    expect(threadHeader).toContain('onclick={() => runThreadAction(() => onLock(!thread.locked))}');
    expect(threadHeader).toContain('threadActionsMenu.open = false;');
  });

  it('uses a compact generic smile action for forum post reactions', () => {
    expect(threadHeader).toContain('<span>React to Post</span>');
    expect(threadHeader).toContain('<circle cx="12" cy="12" r="9" />');
    expect(threadHeader).not.toContain('<ReactionEmoji value={selectedReaction} />');
    expect(globalStyles).toMatch(/\.post-reaction-action\s*\{[^}]*min-height: 30px;/u);
  });
});
