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
const messageRow = readFileSync(new URL('./MessageRow.svelte', import.meta.url), 'utf8');
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

  it('puts forum reactions and an add-reaction picker in the starter message footer', () => {
    expect(threadHeader).not.toContain('starterMessage');
    expect(threadHeader).not.toContain('React to Post');
    expect(guildRoute.match(/showPostFooter=/gu)).toHaveLength(2);
    expect(messageRow).toContain('showPostFooter = false');
    expect(messageRow).toContain(
      'class:post-footer={showPostFooter} class="message-footer-actions"'
    );
    expect(messageRow).toContain(
      "aria-label={reactionEntries.length ? 'Add reaction' : 'React to Post'}"
    );
    expect(messageRow).toContain('{#if !reactionEntries.length}<span>React to Post</span>{/if}');
    expect(messageRow.match(/class="add-reaction"/gu)).toHaveLength(1);
    expect(messageRow).toContain('onclick={openInlineReactionPicker}');
    expect(messageRow).toContain('showMenu(bounds.left, bounds.bottom, trigger, true);');
    expect(globalStyles).toMatch(/\.message-footer-actions\.post-footer\s*\{/u);
    expect(globalStyles).toMatch(/\.message-reactions button\.add-reaction\s*\{/u);
    expect(globalStyles).toMatch(/\.message-reactions button\.add-reaction\.labeled\s*\{/u);
  });

  it('uses a detail-pane post header and a Discord-style starter footer', () => {
    expect(guildRoute).toContain('class:forum-thread-pane=');
    expect(guildRoute.indexOf('<ThreadHeader')).toBeGreaterThan(
      guildRoute.indexOf('class="thread-conversation"')
    );
    expect(guildRoute).toContain('class="forum-post-intro"');
    expect(guildRoute).toContain('<ForumTagEmoji');
    expect(messageRow).toContain('class="post-footer-controls"');
    expect(messageRow).toContain("<span>{postFollowing ? 'Following' : 'Follow'}</span>");
    expect(messageRow).toContain('aria-label="Copy post link"');
    expect(threadHeader).toContain('class="thread-close-action"');
    expect(globalStyles).toMatch(
      /\.message-footer-actions\.post-footer\s*\{[^}]*grid-column: 1 \/ -1;/u
    );
    expect(globalStyles).toMatch(
      /\.forum-thread-pane > \.conversation-layout\.forum-thread-split\s*\{[^}]*display: contents;/u
    );
    expect(globalStyles).toMatch(
      /\.forum-thread-pane \.thread-conversation\s*\{[^}]*grid-column: 2;[^}]*grid-row: 1 \/ 3;/u
    );
  });
});
