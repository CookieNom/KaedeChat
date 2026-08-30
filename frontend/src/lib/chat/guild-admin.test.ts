import { describe, expect, it } from 'vitest';
import {
  autoModPayload,
  boundedVolume,
  canAccessGuildExpressionSettings,
  canCreateGuildExpression,
  canEditGuildExpression,
  guildOwnerRef,
  hasGuildPermissionOrOwnership,
  isQualifiedGuildOwner,
  pruneEstimateQuery,
  soundboardEmojiPayload,
  uniqueNonemptyLines,
  webhookManagementPath
} from './guild-admin';
import { Permission } from '$lib/generated/permissions';

describe('guild administration payloads', () => {
  it('normalizes line-based AutoMod lists without duplicate hidden values', () => {
    expect(uniqueNonemptyLines('  alpha\n\nbeta\nalpha\n')).toEqual(['alpha', 'beta']);
  });

  it('builds a complete mention-spam rule and omits unrelated metadata', () => {
    const payload = autoModPayload({
      name: ' Mentions ',
      enabled: true,
      triggerType: 'mention_spam',
      keywords: 'ignored',
      regexPatterns: '',
      presets: [],
      allowList: '',
      mentionLimit: 8,
      mentionRaidProtection: true,
      blockMessage: true,
      blockMessageText: 'Please slow down',
      alertMessage: true,
      alertChannelRef: '12@example.test',
      timeout: true,
      timeoutSeconds: 300,
      blockMemberInteraction: false,
      exemptRoles: ['3@example.test', '3@example.test'],
      exemptChannels: []
    });
    expect(payload).toMatchObject({
      name: 'Mentions',
      event_type: 'message_send',
      trigger_type: 'mention_spam',
      trigger_metadata: { mention_total_limit: 8, mention_raid_protection_enabled: true },
      exempt_roles: ['3@example.test']
    });
    expect(payload.actions).toEqual([
      { type: 'block_message', custom_message: 'Please slow down' },
      { type: 'send_alert_message', channel_id: '12@example.test' },
      { type: 'timeout', duration_seconds: 300 }
    ]);
  });

  it('keeps repeated prune-role query parameters and clamps playback volume', () => {
    expect(pruneEstimateQuery(14, ['1@chat.test', '2@chat.test', '1@chat.test'])).toBe(
      'days=14&include_roles=1%40chat.test&include_roles=2%40chat.test'
    );
    expect(boundedVolume(-1)).toBe(0);
    expect(boundedVolume(1.4)).toBe(1);
    expect(boundedVolume(Number.NaN)).toBe(1);
  });

  it('uses create permission for creator-owned expressions and manage for other creators', () => {
    const create = Permission.CREATE_GUILD_EXPRESSIONS;
    const manage = Permission.MANAGE_GUILD_EXPRESSIONS;
    const user = '7@chat.test';

    expect(canCreateGuildExpression(create)).toBe(true);
    expect(canEditGuildExpression(create, user, '7', 'chat.test')).toBe(true);
    expect(canEditGuildExpression(create, user, '8', 'chat.test')).toBe(false);
    expect(canEditGuildExpression(manage, user, '8', 'remote.test')).toBe(true);
    expect(canEditGuildExpression(Permission.ADMINISTRATOR, user, undefined, undefined)).toBe(true);
  });

  it('keeps Soundboard settings for expression creators and managers, not playback-only users', () => {
    expect(canAccessGuildExpressionSettings(Permission.USE_SOUNDBOARD)).toBe(false);
    expect(canAccessGuildExpressionSettings(Permission.CREATE_GUILD_EXPRESSIONS)).toBe(true);
    expect(canAccessGuildExpressionSettings(Permission.MANAGE_GUILD_EXPRESSIONS)).toBe(true);
    expect(canAccessGuildExpressionSettings(0n, true)).toBe(true);
  });

  it('recognizes a qualified remote owner without relying on replicated permission bits', () => {
    const guild = {
      owner_id: '7',
      owner_domain: 'remote.test',
      origin_domain: 'guilds.test'
    };
    expect(guildOwnerRef(guild)).toBe('7@remote.test');
    expect(isQualifiedGuildOwner(guild, '7@remote.test')).toBe(true);
    expect(isQualifiedGuildOwner(guild, '7@guilds.test')).toBe(false);
    expect(
      hasGuildPermissionOrOwnership(0n, Permission.MANAGE_GUILD, '7@remote.test', '7@remote.test')
    ).toBe(true);
    expect(
      hasGuildPermissionOrOwnership(
        Permission.MANAGE_CHANNELS,
        Permission.MANAGE_CHANNELS | Permission.MANAGE_ROLES,
        '7@home.test',
        '8@remote.test'
      )
    ).toBe(true);
    expect(
      hasGuildPermissionOrOwnership(0n, Permission.MANAGE_GUILD, '7@home.test', '7@remote.test')
    ).toBe(false);
  });

  it('preserves custom sound emoji IDs independently from Unicode emoji names', () => {
    expect(soundboardEmojiPayload({ mode: 'custom', emojiId: '55', emojiName: '' })).toEqual({
      emoji_id: '55',
      emoji_name: null
    });
    expect(soundboardEmojiPayload({ mode: 'unicode', emojiId: '', emojiName: ' 🔔 ' })).toEqual({
      emoji_id: null,
      emoji_name: '🔔'
    });
    expect(soundboardEmojiPayload({ mode: 'none', emojiId: '', emojiName: '' })).toEqual({
      emoji_id: null,
      emoji_name: null
    });
  });

  it('qualifies authenticated webhook management at the guild authority', () => {
    expect(
      webhookManagementPath(
        { id: '80', ref: '80@remote.test', guild_domain: 'remote.test' },
        '1@remote.test',
        '/rotate'
      )
    ).toBe('/webhooks/80%40remote.test/rotate?guild_ref=1%40remote.test');
    expect(
      webhookManagementPath({ id: '80', guild_domain: 'remote.test' }, '1@remote.test', '/avatar')
    ).toBe('/webhooks/80%40remote.test/avatar?guild_ref=1%40remote.test');
  });
});
