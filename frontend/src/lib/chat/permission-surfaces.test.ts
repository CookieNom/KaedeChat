import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const guildRoute = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/[channelId]/+page.svelte', import.meta.url),
  'utf8'
);
const guildSettings = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/settings/+page.svelte', import.meta.url),
  'utf8'
);
const guildIntegrations = readFileSync(
  new URL('../../routes/(app)/g/[guildId]/integrations/+page.svelte', import.meta.url),
  'utf8'
);

describe('channel-scoped permission surfaces', () => {
  it('discovers invite and webhook-only channel settings with the correct initial panel', () => {
    expect(guildRoute).toContain('canManageChannelWebhooks');
    expect(guildRoute).toContain('canCreateChannelInvite');
    expect(guildRoute).toContain("? 'integrations'");
    expect(guildRoute).toContain('channelSettingsPath(guild, target, settingsPanel)');
    expect(guildSettings.match(/\[0, 5, 15\]\.includes/gu)).toHaveLength(2);
  });

  it('uses selected-channel permissions for overwrite changes and target management', () => {
    expect(guildSettings.match(/!selectedHasPermission\(permission\[2\]\)/gu)).toHaveLength(3);
    expect(guildSettings).toContain('disabled={busy || !canManageOverwriteRole(role)}');
    expect(guildSettings).toContain('disabled={busy || !canManageOverwriteMember(member)}');
    expect(
      (guildSettings.match(/!canManageOverwriteTarget\(overwriteTarget\)/gu) ?? []).length
    ).toBeGreaterThan(2);
    expect(guildRoute).toContain(
      '{@const canEditChannel = channelHasPermission(target, Permission.MANAGE_CHANNELS)}'
    );
  });

  it('requires MOVE_MEMBERS in both voice move channels', () => {
    expect(guildRoute).toContain(
      'channelHasPermission(source, Permission.MOVE_MEMBERS) && actorOutranks(user)'
    );
    expect(guildRoute).toContain('channelHasPermission(target, Permission.MOVE_MEMBERS)');
    expect(guildRoute).toContain('canMoveVoiceMemberTo(dragged.user, dragged.source, target)');
  });

  it('keeps history-backed controls behind READ_MESSAGE_HISTORY', () => {
    expect(guildRoute).toContain('const canReadMessageHistory = $derived(');
    expect(guildRoute).toContain('if (!canReadMessageHistory) return Promise.resolve();');
    expect(guildRoute).toContain('{#if canReadMessageHistory &&');
  });

  it('does not over-gate announcement follows on retained message history', () => {
    expect(guildSettings.match(/canReadAnnouncementChannel/gu)).toHaveLength(3);
  });

  it('purges and leaves a channel when live access is revoked', () => {
    expect(guildRoute).toContain('if (revokedCurrent) {');
    expect(guildRoute).toContain('loadedRouteChannel = null;');
    expect(guildRoute).toContain('entities.removeChannel({');
    expect(guildRoute).toContain('origin_domain: deletedDomain');
    expect(guildRoute).toContain(
      "window.location.assign(guild && next ? guildChannelPath(guild, next) : resolve('/home'))"
    );
  });

  it('clears guild settings data after a live guild projection is revoked', () => {
    expect(guildSettings).toContain("let observedGuildProjectionRef = '';");
    expect(guildSettings).toContain('if (observedGuildProjectionRef === currentRef)');
    expect(guildSettings).toContain('untrack(revokeGuildSettingsAccess);');
    expect(guildSettings).toContain('loadGeneration += 1;');
    expect(guildSettings).toContain("revealedWebhookToken = '';");
    expect(guildSettings).toContain(
      "error = 'This guild is unavailable or you no longer have access.';"
    );
  });

  it('purges and leaves active guild routes after normalized access loss', () => {
    expect(guildRoute).toContain("let observedGuildProjectionRef = '';");
    expect(guildRoute).toContain('untrack(() => revokeActiveGuildAccess(current));');
    expect(guildRoute).toContain('setMessages([]);');
    expect(guildRoute).toContain('setMembers([]);');
    expect(guildRoute).toContain('entities.removeGuild(removed);');
    expect(guildRoute).toContain('loadedRouteChannel = null;');
    expect(guildRoute).toContain('messageSearchOpen = false;');
    expect(guildRoute).toContain('hasEarlier = false;');
    expect(guildRoute).toContain('e2eeClient = null;');
    expect(guildRoute).toContain("window.location.assign(resolve('/home'));");
  });

  it('clears guild integrations after a live guild projection is revoked', () => {
    expect(guildIntegrations).toContain("let observedGuildProjectionRef = '';");
    expect(guildIntegrations).toContain(
      'untrack(() => revokeGuildIntegrationsAccess(targetGuildRef));'
    );
    expect(guildIntegrations).toContain('loadController.abort();');
    expect(guildIntegrations).toContain('installations = [];');
    expect(guildIntegrations).toContain("window.location.assign(resolve('/home'));");
  });

  it('does not show guild-scoped channel creation for a channel-only grant', () => {
    expect(guildSettings).toContain(
      '{#if canManageChannels}\n            <form\n              class="settings-card quick-create"'
    );
  });

  it('uses channel-effective permissions for category moves without gating creation parents', () => {
    expect(guildRoute).toContain('channelParentOptions(channelDialogTarget)');
    expect(guildRoute).toContain('channelOrderPermissionTargets(previous, next, movedKey)');
    expect(guildRoute).toContain('channelPositionRequest(previous, next, movedKey)');
    expect(guildSettings).toContain('editableChannelParents(selectedChannel)');
    expect(guildSettings).toContain('channelHasPermission(parent, CHANNEL_MOVE_PERMISSIONS)');
    expect(guildSettings).toContain('bind:value={newChannelParent}');
    expect(guildSettings).toContain(
      '{#each (guild.channels ?? []).filter((channel) => channel.type === 4) as category'
    );
  });

  it('fails role assignment controls closed on incomplete target hierarchy', () => {
    expect(guildSettings).not.toContain('function highestRoleFor');
    expect(guildSettings).toContain(
      'return guildMemberOutranks(guild, signedInUser, member.user, currentMembers);'
    );
    expect(guildSettings).toContain('const currentMember = currentMembers.find(');
  });

  it('keeps settings hierarchy controls on live role and member projections', () => {
    expect(guildSettings).toContain('const roles = projection.roles ?? current.roles;');
    expect(guildSettings).toContain('entities.members.upsertMany(rows);');
    expect(guildSettings).toContain(
      'const currentMembers = $derived(liveMemberRows(members, true));'
    );
    expect(guildSettings).toContain('removeCachedMember(dialog.member);');
    expect(guildSettings).toContain('entities.guilds.update(targetGuild,');
  });

  it('filters full-guild invite destinations by channel-effective permission', () => {
    expect(guildSettings).toContain('channelHasPermission(channel, Permission.CREATE_INVITE)');
    expect(guildSettings).toContain("error = 'Choose a channel where you can create invites.';");
  });
});
