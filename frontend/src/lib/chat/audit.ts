import { entityRef } from './refs';
import type { Guild, GuildMemberSummary } from './types';
import { userDisplayName } from './users';

export interface AuditLogChange {
  key: string;
  old_value?: unknown;
  new_value?: unknown;
  added?: unknown;
  removed?: unknown;
}

export interface AuditLogEntry {
  id: string;
  guild_id: string;
  guild_domain: string;
  actor_id: string;
  actor_domain: string;
  action_type: number;
  target_type: string | null;
  target_ref: Record<string, unknown> | null;
  reason: string | null;
  changes: AuditLogChange[];
  created_at: string;
}

export interface AuditActionOption {
  action_type: number;
  target_type: string | null;
  label: string;
  verb: string;
}

export interface AuditLogQueryOptions {
  limit?: number;
  before?: string;
  userId?: string;
  actionType?: number;
  targetType?: string | null;
}

const MAX_AUDIT_ACTOR_ID = 9_223_372_036_854_775_807n;
const AUDIT_ACTOR_DOMAIN_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/**
 * Normalize an exact federated actor reference accepted by the audit API.
 *
 * Audit entries can outlive guild membership, so a moderator must be able to
 * filter by a copied reference even when member search can no longer resolve
 * that account. Keeping this validation at the UI boundary also avoids
 * turning a mistyped search term into a failing audit-log request.
 */
export function canonicalAuditActorRef(value: string): string | null {
  const candidate = value.trim();
  const separator = candidate.indexOf('@');
  if (separator <= 0 || separator !== candidate.lastIndexOf('@')) return null;

  const identifier = candidate.slice(0, separator);
  const domain = candidate.slice(separator + 1).toLowerCase();
  if (!/^(?:0|[1-9][0-9]{0,18})$/.test(identifier)) return null;
  if (BigInt(identifier) > MAX_AUDIT_ACTOR_ID) return null;
  if (!domain.includes('.') || domain.length > 253) return null;
  if (domain.split('.').some((label) => !AUDIT_ACTOR_DOMAIN_LABEL.test(label))) return null;
  return `${identifier}@${domain}`;
}

/**
 * Actions Kaede currently emits or has a Discord-compatible implementation for.
 *
 * A few Kaede actions intentionally refine Discord's meanings. In particular,
 * channel permission operations use 15-17, role reordering uses 33, and codes
 * 25-27 also cover federated-instance moderation and ownership transfer. Keep
 * the target type in the key so those meanings remain unambiguous.
 */
export const AUDIT_ACTION_OPTIONS: readonly AuditActionOption[] = [
  { action_type: 1, target_type: 'guild', label: 'Guild updated', verb: 'updated' },
  { action_type: 10, target_type: 'channel', label: 'Channel created', verb: 'created' },
  { action_type: 11, target_type: 'channel', label: 'Channel updated', verb: 'updated' },
  {
    action_type: 11,
    target_type: 'channel_order',
    label: 'Channel order updated',
    verb: 'reordered'
  },
  { action_type: 12, target_type: 'channel', label: 'Channel deleted', verb: 'deleted' },
  {
    action_type: 13,
    target_type: 'channel_overwrite',
    label: 'Channel permission override created',
    verb: 'created'
  },
  {
    action_type: 14,
    target_type: 'channel_overwrite',
    label: 'Channel permission override updated',
    verb: 'updated'
  },
  {
    action_type: 15,
    target_type: 'channel_overwrite',
    label: 'Channel permission override deleted',
    verb: 'deleted'
  },
  {
    action_type: 15,
    target_type: 'channel',
    label: 'Channel permissions updated',
    verb: 'updated permissions for'
  },
  {
    action_type: 16,
    target_type: 'channel',
    label: 'Channel permissions removed',
    verb: 'removed a permission override from'
  },
  {
    action_type: 17,
    target_type: 'channel',
    label: 'Channel permissions synced',
    verb: 'synced permissions for'
  },
  { action_type: 20, target_type: 'member', label: 'Member kicked', verb: 'kicked' },
  { action_type: 21, target_type: 'guild', label: 'Members pruned', verb: 'pruned' },
  { action_type: 22, target_type: 'user', label: 'Member banned', verb: 'banned' },
  { action_type: 23, target_type: 'user', label: 'Member unbanned', verb: 'unbanned' },
  { action_type: 24, target_type: 'member', label: 'Member updated', verb: 'updated' },
  {
    action_type: 25,
    target_type: 'member',
    label: 'Member roles updated',
    verb: 'updated roles for'
  },
  { action_type: 25, target_type: 'instance', label: 'Instance banned', verb: 'banned' },
  { action_type: 26, target_type: 'member', label: 'Member moved', verb: 'moved' },
  { action_type: 26, target_type: 'instance', label: 'Instance unbanned', verb: 'unbanned' },
  {
    action_type: 27,
    target_type: 'member',
    label: 'Member disconnected',
    verb: 'disconnected'
  },
  {
    action_type: 27,
    target_type: 'user',
    label: 'Ownership transferred',
    verb: 'transferred ownership to'
  },
  { action_type: 28, target_type: 'user', label: 'Bot added', verb: 'added' },
  { action_type: 30, target_type: 'role', label: 'Role created', verb: 'created' },
  { action_type: 31, target_type: 'role', label: 'Role updated', verb: 'updated' },
  { action_type: 32, target_type: 'role', label: 'Role deleted', verb: 'deleted' },
  { action_type: 33, target_type: 'role', label: 'Roles reordered', verb: 'reordered' },
  { action_type: 40, target_type: 'invite', label: 'Invite created', verb: 'created' },
  { action_type: 41, target_type: 'invite', label: 'Invite updated', verb: 'updated' },
  { action_type: 42, target_type: 'invite', label: 'Invite deleted', verb: 'deleted' },
  { action_type: 50, target_type: 'webhook', label: 'Webhook created', verb: 'created' },
  { action_type: 51, target_type: 'webhook', label: 'Webhook updated', verb: 'updated' },
  { action_type: 52, target_type: 'webhook', label: 'Webhook deleted', verb: 'deleted' },
  { action_type: 60, target_type: 'emoji', label: 'Emoji created', verb: 'created' },
  { action_type: 61, target_type: 'emoji', label: 'Emoji updated', verb: 'updated' },
  { action_type: 62, target_type: 'emoji', label: 'Emoji deleted', verb: 'deleted' },
  { action_type: 72, target_type: 'message', label: 'Message deleted', verb: 'deleted' },
  {
    action_type: 73,
    target_type: 'message',
    label: 'Messages bulk deleted',
    verb: 'bulk deleted'
  },
  { action_type: 74, target_type: 'message', label: 'Message pinned', verb: 'pinned' },
  { action_type: 75, target_type: 'message', label: 'Message unpinned', verb: 'unpinned' },
  {
    action_type: 80,
    target_type: 'integration',
    label: 'Integration created',
    verb: 'created'
  },
  {
    action_type: 81,
    target_type: 'integration',
    label: 'Integration updated',
    verb: 'updated'
  },
  {
    action_type: 82,
    target_type: 'integration',
    label: 'Integration deleted',
    verb: 'deleted'
  },
  { action_type: 90, target_type: 'sticker', label: 'Sticker created', verb: 'created' },
  { action_type: 91, target_type: 'sticker', label: 'Sticker updated', verb: 'updated' },
  { action_type: 92, target_type: 'sticker', label: 'Sticker deleted', verb: 'deleted' },
  {
    action_type: 100,
    target_type: 'scheduled_event',
    label: 'Scheduled event created',
    verb: 'created'
  },
  {
    action_type: 101,
    target_type: 'scheduled_event',
    label: 'Scheduled event updated',
    verb: 'updated'
  },
  {
    action_type: 102,
    target_type: 'scheduled_event',
    label: 'Scheduled event deleted',
    verb: 'deleted'
  },
  { action_type: 110, target_type: 'thread', label: 'Thread created', verb: 'created' },
  { action_type: 111, target_type: 'thread', label: 'Thread updated', verb: 'updated' },
  { action_type: 112, target_type: 'thread', label: 'Thread deleted', verb: 'deleted' },
  {
    action_type: 121,
    target_type: 'application_command',
    label: 'Application command permissions updated',
    verb: 'updated permissions for'
  },
  {
    action_type: 130,
    target_type: 'soundboard_sound',
    label: 'Soundboard sound created',
    verb: 'created'
  },
  {
    action_type: 131,
    target_type: 'soundboard_sound',
    label: 'Soundboard sound updated',
    verb: 'updated'
  },
  {
    action_type: 132,
    target_type: 'soundboard_sound',
    label: 'Soundboard sound deleted',
    verb: 'deleted'
  },
  {
    action_type: 140,
    target_type: 'auto_mod_rule',
    label: 'AutoMod rule created',
    verb: 'created'
  },
  {
    action_type: 141,
    target_type: 'auto_mod_rule',
    label: 'AutoMod rule updated',
    verb: 'updated'
  },
  {
    action_type: 142,
    target_type: 'auto_mod_rule',
    label: 'AutoMod rule deleted',
    verb: 'deleted'
  },
  {
    action_type: 143,
    target_type: 'user',
    label: 'AutoMod blocked an action',
    verb: 'blocked an action by'
  },
  {
    action_type: 144,
    target_type: 'message',
    label: 'AutoMod flagged a message',
    verb: 'flagged a message from'
  },
  {
    action_type: 145,
    target_type: 'user',
    label: 'AutoMod timed out a member',
    verb: 'timed out'
  },
  {
    action_type: 146,
    target_type: 'user',
    label: 'AutoMod quarantined a member',
    verb: 'quarantined'
  },
  {
    action_type: 192,
    target_type: 'voice_channel',
    label: 'Voice channel status set',
    verb: 'set the status for'
  },
  {
    action_type: 193,
    target_type: 'voice_channel',
    label: 'Voice channel status cleared',
    verb: 'cleared the status for'
  }
];

function actionKey(entry: Pick<AuditLogEntry, 'action_type' | 'target_type'>): string {
  return `${entry.action_type}|${entry.target_type ?? ''}`;
}

const actionOptionsByKey = new Map(
  AUDIT_ACTION_OPTIONS.map((option) => [actionKey(option), option] as const)
);

function auditActionOption(
  entry: Pick<AuditLogEntry, 'action_type' | 'target_type'>
): AuditActionOption | undefined {
  return (
    actionOptionsByKey.get(actionKey(entry)) ??
    AUDIT_ACTION_OPTIONS.find((option) => option.action_type === entry.action_type)
  );
}

export function auditActionFilterValue(
  entry: Pick<AuditLogEntry, 'action_type' | 'target_type'>
): string {
  return actionKey(entry);
}

export function parseAuditActionFilter(
  value: string
): Pick<AuditActionOption, 'action_type' | 'target_type'> | null {
  const separator = value.indexOf('|');
  if (separator < 1) return null;
  const actionType = Number(value.slice(0, separator));
  if (!Number.isSafeInteger(actionType) || actionType < 0) return null;
  return {
    action_type: actionType,
    target_type: value.slice(separator + 1) || null
  };
}

export function auditLogQueryString(options: AuditLogQueryOptions = {}): string {
  const query = new URLSearchParams();
  query.set('limit', String(options.limit ?? 50));
  if (options.before) query.set('before', options.before);
  if (options.userId) query.set('user_id', options.userId);
  if (options.actionType !== undefined) query.set('action_type', String(options.actionType));
  if (options.targetType) query.set('target_type', options.targetType);
  return query.toString();
}

export function auditActorRef(entry: AuditLogEntry): string {
  return `${entry.actor_id}@${entry.actor_domain}`;
}

export function auditActorName(
  entry: AuditLogEntry,
  members: readonly GuildMemberSummary[]
): string {
  const member = members.find((candidate) => entityRef(candidate.user) === auditActorRef(entry));
  return member
    ? member.nickname?.trim() || userDisplayName(member.user)
    : `@${entry.actor_id}@${entry.actor_domain}`;
}

export function auditActionLabel(
  entry: Pick<AuditLogEntry, 'action_type' | 'target_type'>
): string {
  return auditActionOption(entry)?.label ?? `Unknown action (${entry.action_type})`;
}

function targetIdentity(entry: AuditLogEntry, defaultDomain: string): string | null {
  const id = entry.target_ref?.id;
  if (typeof id !== 'string' && typeof id !== 'number') return null;
  const domain = entry.target_ref?.origin_domain ?? entry.target_ref?.domain ?? defaultDomain;
  return typeof domain === 'string' ? `${id}@${domain}` : null;
}

function targetName(entry: AuditLogEntry): string | null {
  const name = entry.target_ref?.name;
  if (typeof name === 'string' && name.trim()) return name.trim();
  const nameChange = entry.changes.find((change) => change.key === 'name');
  const changedName = nameChange?.new_value ?? nameChange?.old_value;
  return typeof changedName === 'string' && changedName.trim() ? changedName.trim() : null;
}

function targetId(entry: AuditLogEntry): string | null {
  const id = entry.target_ref?.id;
  return typeof id === 'string' || typeof id === 'number' ? String(id) : null;
}

export function auditTargetName(
  entry: AuditLogEntry,
  guild: Guild,
  members: readonly GuildMemberSummary[]
): string {
  const ref = targetIdentity(entry, guild.origin_domain);
  if (entry.action_type === 21) return 'inactive members';
  if (entry.target_type === 'guild') return guild.name;
  if (entry.target_type === 'channel_order') return 'the channel list';
  if (entry.target_type === 'channel' || entry.target_type === 'voice_channel') {
    const channel = guild.channels?.find((candidate) => entityRef(candidate) === ref);
    const name = channel?.name ?? targetName(entry);
    return name
      ? `#${name}`
      : entry.target_type === 'voice_channel'
        ? 'a voice channel'
        : 'a channel';
  }
  if (entry.target_type === 'thread') {
    const thread = guild.channels?.find((candidate) => entityRef(candidate) === ref);
    const name = thread?.name ?? targetName(entry);
    return name ? `thread #${name}` : 'a thread';
  }
  if (entry.target_type === 'channel_overwrite') {
    const channelId = entry.target_ref?.channel_id;
    const channel = guild.channels?.find(
      (candidate) =>
        candidate.id === String(channelId) && candidate.origin_domain === guild.origin_domain
    );
    return channel?.name ? `permissions for #${channel.name}` : 'a channel permission override';
  }
  if (entry.target_type === 'role') {
    const role = guild.roles?.find((candidate) => entityRef(candidate) === ref);
    const name = role?.name ?? targetName(entry);
    if (name) return `@${name}`;
    const ids = entry.target_ref?.ids;
    return Array.isArray(ids) ? `${ids.length} roles` : 'a role';
  }
  if (
    entry.target_type === 'member' ||
    entry.target_type === 'user' ||
    entry.target_type === 'bot'
  ) {
    const member = members.find((candidate) => entityRef(candidate.user) === ref);
    if (member) return member.nickname?.trim() || userDisplayName(member.user);
    const username = entry.target_ref?.username ?? entry.target_ref?.display_name;
    if (typeof username === 'string' && username.trim()) return username.trim();
    return ref ? `@${ref}` : '@member';
  }
  if (entry.target_type === 'instance') {
    return typeof entry.target_ref?.domain === 'string' ? entry.target_ref.domain : 'an instance';
  }
  if (entry.target_type === 'invite') {
    return typeof entry.target_ref?.code === 'string'
      ? `invite ${entry.target_ref.code}`
      : 'an invite';
  }
  if (entry.target_type === 'emoji') {
    const emoji = guild.emojis?.find((candidate) => entityRef(candidate) === ref);
    const name = emoji?.name ?? targetName(entry);
    return name ? `:${name}:` : 'an emoji';
  }
  if (entry.target_type === 'sticker') {
    const sticker = guild.stickers?.find((candidate) => entityRef(candidate) === ref);
    const name = sticker?.name ?? targetName(entry);
    return name ? `sticker ${name}` : 'a sticker';
  }
  if (entry.target_type === 'scheduled_event') {
    const name = targetName(entry);
    return name ? `event ${name}` : 'a scheduled event';
  }
  if (entry.target_type === 'webhook') {
    const name = targetName(entry);
    return name ? `webhook ${name}` : 'a webhook';
  }
  if (entry.target_type === 'soundboard_sound') {
    const name = targetName(entry);
    return name ? `sound ${name}` : 'a soundboard sound';
  }
  if (entry.target_type === 'auto_mod_rule') {
    const name = targetName(entry);
    return name ? `AutoMod rule ${name}` : 'an AutoMod rule';
  }
  if (entry.target_type === 'application_command') {
    const name = targetName(entry);
    return name ? `command /${name}` : 'an application command';
  }
  if (entry.target_type === 'integration') {
    return targetName(entry) ?? 'an integration';
  }
  if (entry.target_type === 'message') {
    const id = targetId(entry) ?? entry.target_ref?.message_id;
    return id != null ? `message ${id}` : 'one or more messages';
  }
  const name = targetName(entry);
  if (name) return name;
  const readableType = entry.target_type?.replace(/[_-]+/g, ' ');
  return readableType ? `a ${readableType}` : 'the guild';
}

export function auditSummary(actor: string, entry: AuditLogEntry, target: string): string {
  const verb = auditActionOption(entry)?.verb ?? 'performed an action on';
  return `${actor} ${verb} ${target}`;
}

export function auditFieldLabel(key: string): string {
  const normalized = key.trim().toLowerCase();
  const labels: Record<string, string> = {
    $add: 'Roles added',
    $remove: 'Roles removed',
    afk_timeout: 'AFK timeout',
    auto_archive_duration: 'Auto-archive duration',
    channel_id: 'Channel',
    delete_member_days: 'Inactive days',
    default_auto_archive_duration: 'Default auto-archive duration',
    entity_type: 'Event type',
    exempt_channels: 'Exempt channels',
    exempt_roles: 'Exempt roles',
    members_removed: 'Members removed',
    permission_overwrites: 'Permission overrides',
    rate_limit_per_user: 'Slowmode',
    rtc_region: 'RTC region',
    scheduled_end_time: 'Scheduled end',
    scheduled_start_time: 'Scheduled start',
    trigger_type: 'Trigger type',
    user_limit: 'User limit',
    voice_flags: 'Voice settings'
  };
  if (labels[normalized]) return labels[normalized];
  const label = normalized.replace(/[_\-\s]+/g, ' ');
  return label ? label[0].toUpperCase() + label.slice(1) : 'Value';
}

function displayAuditValue(value: unknown): string {
  if (value === null || value === undefined || value === 'null') return 'None';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => displayAuditValue(item)).join(', ') : 'None';
  }
  if (typeof value === 'object') {
    const item = value as Record<string, unknown>;
    if (item.name != null) return String(item.name);
    if (item.id != null && item.origin_domain != null) return `${item.id}@${item.origin_domain}`;
    if (item.id != null) return String(item.id);
    return JSON.stringify(value);
  }
  return String(value);
}

export function auditChangeDescription(change: AuditLogChange): string {
  if ('added' in change || 'removed' in change) {
    return [
      change.added != null ? `Added ${displayAuditValue(change.added)}` : '',
      change.removed != null ? `Removed ${displayAuditValue(change.removed)}` : ''
    ]
      .filter(Boolean)
      .join(' • ');
  }
  return `${displayAuditValue(change.old_value)} → ${displayAuditValue(change.new_value)}`;
}

export function auditRelativeTime(value: string | Date, now = new Date()): string {
  const date = value instanceof Date ? value : new Date(value);
  const seconds = Math.floor((now.getTime() - date.getTime()) / 1000);
  if (!Number.isFinite(seconds) || seconds < 45) return 'Just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minute${seconds < 120 ? '' : 's'} ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hour${seconds < 7200 ? '' : 's'} ago`;
  if (seconds < 604800)
    return `${Math.floor(seconds / 86400)} day${seconds < 172800 ? '' : 's'} ago`;
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(date);
}
