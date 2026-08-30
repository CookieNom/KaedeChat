import { Permission } from '$lib/generated/permissions';
import { hasAllPermissions, hasAnyPermission } from './permissions';

export type AutoModTrigger =
  'keyword' | 'spam' | 'keyword_preset' | 'mention_spam' | 'member_profile';

export interface AutoModDraft {
  name: string;
  enabled: boolean;
  triggerType: AutoModTrigger;
  keywords: string;
  regexPatterns: string;
  presets: Array<'profanity' | 'sexual_content' | 'slurs'>;
  allowList: string;
  mentionLimit: number;
  mentionRaidProtection: boolean;
  blockMessage: boolean;
  blockMessageText: string;
  alertMessage: boolean;
  alertChannelRef: string;
  timeout: boolean;
  timeoutSeconds: number;
  blockMemberInteraction: boolean;
  exemptRoles: string[];
  exemptChannels: string[];
}

export interface SoundboardEmojiDraft {
  mode: 'none' | 'unicode' | 'custom';
  emojiId: string;
  emojiName: string;
}

export interface ManagedWebhookRef {
  id: string;
  ref?: string;
  guild_domain: string;
}

export interface GuildOwnerIdentity {
  owner_id: string;
  owner_domain?: string | null;
  origin_domain: string;
}

/** Build the owner's composite identity without assuming the guild and owner share an origin. */
export function guildOwnerRef(guild: GuildOwnerIdentity): string {
  return `${guild.owner_id}@${guild.owner_domain ?? guild.origin_domain}`;
}

export function isQualifiedGuildOwner(
  guild: GuildOwnerIdentity | null | undefined,
  actorRef: string | null | undefined
): boolean {
  return Boolean(guild && actorRef && guildOwnerRef(guild) === actorRef);
}

/** Guild owners retain every settings surface even when a replica has stale bits. */
export function hasGuildPermissionOrOwnership(
  effectivePermissions: bigint,
  requestedPermissions: bigint,
  actorRef: string | null | undefined,
  ownerRef: string | null | undefined
): boolean {
  return (
    Boolean(actorRef && ownerRef && actorRef === ownerRef) ||
    hasAnyPermission(effectivePermissions, requestedPermissions)
  );
}

/** Build an authenticated management route at the webhook's authority. */
export function webhookManagementPath(
  webhook: ManagedWebhookRef,
  guildRef: string,
  suffix = ''
): string {
  const webhookRef = webhook.ref?.trim() || `${webhook.id}@${webhook.guild_domain}`;
  const query = new URLSearchParams({ guild_ref: guildRef });
  return `/webhooks/${encodeURIComponent(webhookRef)}${suffix}?${query}`;
}

export function soundboardEmojiPayload(draft: SoundboardEmojiDraft): {
  emoji_id: string | null;
  emoji_name: string | null;
} {
  if (draft.mode === 'custom') return { emoji_id: draft.emojiId, emoji_name: null };
  if (draft.mode === 'unicode') {
    return { emoji_id: null, emoji_name: draft.emojiName.trim() || null };
  }
  return { emoji_id: null, emoji_name: null };
}

export function uniqueNonemptyLines(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean)
    )
  ];
}

export function autoModPayload(draft: AutoModDraft): Record<string, unknown> {
  const actions: Array<Record<string, unknown>> = [];
  if (draft.blockMessage) {
    actions.push({
      type: 'block_message',
      ...(draft.blockMessageText.trim() ? { custom_message: draft.blockMessageText.trim() } : {})
    });
  }
  if (draft.alertMessage) {
    actions.push({ type: 'send_alert_message', channel_id: draft.alertChannelRef });
  }
  if (draft.timeout) {
    actions.push({ type: 'timeout', duration_seconds: draft.timeoutSeconds });
  }
  if (draft.blockMemberInteraction) actions.push({ type: 'block_member_interaction' });

  const metadata: Record<string, unknown> = {};
  if (draft.triggerType === 'keyword' || draft.triggerType === 'member_profile') {
    metadata.keyword_filter = uniqueNonemptyLines(draft.keywords);
    metadata.regex_patterns = uniqueNonemptyLines(draft.regexPatterns);
    metadata.allow_list = uniqueNonemptyLines(draft.allowList);
  } else if (draft.triggerType === 'keyword_preset') {
    metadata.presets = [...new Set(draft.presets)];
    metadata.allow_list = uniqueNonemptyLines(draft.allowList);
  } else if (draft.triggerType === 'mention_spam') {
    metadata.mention_total_limit = draft.mentionLimit;
    metadata.mention_raid_protection_enabled = draft.mentionRaidProtection;
  }

  return {
    name: draft.name.trim(),
    event_type: draft.triggerType === 'member_profile' ? 'member_update' : 'message_send',
    trigger_type: draft.triggerType,
    trigger_metadata: metadata,
    actions,
    enabled: draft.enabled,
    exempt_roles: [...new Set(draft.exemptRoles)],
    exempt_channels: [...new Set(draft.exemptChannels)]
  };
}

export function pruneEstimateQuery(days: number, roleRefs: string[]): string {
  const query = new URLSearchParams({ days: String(days) });
  for (const roleRef of new Set(roleRefs)) query.append('include_roles', roleRef);
  return query.toString();
}

export function boundedVolume(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.min(1, Math.max(0, value));
}

export function canCreateGuildExpression(permissionBits: bigint): boolean {
  return hasAllPermissions(permissionBits, Permission.CREATE_GUILD_EXPRESSIONS);
}

/** Settings are for creating or managing expressions, not merely playing Soundboard sounds. */
export function canAccessGuildExpressionSettings(
  permissionBits: bigint,
  isGuildOwner = false
): boolean {
  return (
    isGuildOwner ||
    hasAnyPermission(
      permissionBits,
      Permission.CREATE_GUILD_EXPRESSIONS | Permission.MANAGE_GUILD_EXPRESSIONS
    )
  );
}

export function canEditGuildExpression(
  permissionBits: bigint,
  currentUserRef: string,
  creatorId: string | null | undefined,
  creatorDomain: string | null | undefined
): boolean {
  if (hasAllPermissions(permissionBits, Permission.MANAGE_GUILD_EXPRESSIONS)) return true;
  return Boolean(
    currentUserRef &&
    creatorId &&
    creatorDomain &&
    `${creatorId}@${creatorDomain}` === currentUserRef &&
    canCreateGuildExpression(permissionBits)
  );
}
