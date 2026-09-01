import { api } from '$lib/api/client';
import { Permission } from '$lib/generated/permissions';
import type { UploadTicket } from '$lib/media/uploads';
import { webhookManagementPath } from './guild-admin';
import { hasAllPermissions } from './permissions';
import { entityRef } from './refs';
import type { Channel, Guild } from './types';

export interface WebhookSummary {
  id: string;
  ref?: string;
  type?: number;
  guild_id: string;
  guild_domain: string;
  channel_id: string;
  channel_domain: string;
  name: string;
  avatar_hash: string | null;
  revoked: boolean;
  token?: string;
  execution_url?: string;
  token_recovery_required?: boolean;
  federated?: boolean;
  source_guild?: {
    id: string;
    origin_domain: string;
    name: string;
    icon_hash: string | null;
  };
  source_channel?: {
    id: string;
    origin_domain: string;
    name: string | null;
  };
}

export type WebhookAvatarCommit =
  WebhookSummary | { status?: string; attachment: { scan_status: string } };

export const CHANNEL_FOLLOWER_WEBHOOK_TYPE = 2;

function effectivePermissions(channel: Channel, guild: Guild): bigint {
  try {
    return BigInt(channel.permissions ?? guild.permissions ?? '0');
  } catch {
    return 0n;
  }
}

export function canManageWebhookChannel(channel: Channel, guild: Guild): boolean {
  return (
    [0, 5, 15].includes(channel.type) &&
    channel.encryption_mode !== 'e2ee' &&
    hasAllPermissions(effectivePermissions(channel, guild), Permission.MANAGE_WEBHOOKS)
  );
}

export function manageableWebhookChannels(guild: Guild): Channel[] {
  return (guild.channels ?? [])
    .filter((channel) => canManageWebhookChannel(channel, guild))
    .sort(
      (left, right) =>
        left.position - right.position || entityRef(left).localeCompare(entityRef(right))
    );
}

export function isChannelFollowerWebhook(webhook: WebhookSummary): boolean {
  return webhook.type === CHANNEL_FOLLOWER_WEBHOOK_TYPE;
}

export function listGuildWebhooks(
  guildRef: string,
  signal?: AbortSignal
): Promise<WebhookSummary[]> {
  return api<WebhookSummary[]>(`/guilds/${encodeURIComponent(guildRef)}/webhooks`, { signal });
}

export function listChannelWebhooks(
  guildRef: string,
  channelRef: string,
  signal?: AbortSignal
): Promise<WebhookSummary[]> {
  return api<WebhookSummary[]>(
    `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(channelRef)}/webhooks`,
    { signal }
  );
}

export function createGuildWebhook(
  guildRef: string,
  channelRef: string,
  name: string
): Promise<WebhookSummary> {
  return api<WebhookSummary>(
    `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(channelRef)}/webhooks`,
    {
      method: 'POST',
      body: JSON.stringify({ name })
    }
  );
}

export function updateGuildWebhook(
  guildRef: string,
  webhook: WebhookSummary,
  changes: { name?: string; channel_id?: string }
): Promise<WebhookSummary> {
  return api<WebhookSummary>(webhookManagementPath(webhook, guildRef), {
    method: 'PATCH',
    body: JSON.stringify(changes)
  });
}

export function rotateGuildWebhook(
  guildRef: string,
  webhook: WebhookSummary
): Promise<WebhookSummary> {
  return api<WebhookSummary>(webhookManagementPath(webhook, guildRef, '/rotate'), {
    method: 'POST'
  });
}

export function deleteGuildWebhook(guildRef: string, webhook: WebhookSummary): Promise<void> {
  return api<void>(webhookManagementPath(webhook, guildRef), { method: 'DELETE' });
}

export function createGuildWebhookAvatarTicket(
  guildRef: string,
  webhook: WebhookSummary,
  payload: { filename: string; content_type: string; size: number },
  signal?: AbortSignal
): Promise<UploadTicket> {
  return api<UploadTicket>(webhookManagementPath(webhook, guildRef, '/avatar/tickets'), {
    method: 'POST',
    body: JSON.stringify(payload),
    signal
  });
}

export function commitGuildWebhookAvatar(
  guildRef: string,
  webhook: WebhookSummary,
  attachmentId: string,
  signal?: AbortSignal
): Promise<WebhookAvatarCommit> {
  return api<WebhookAvatarCommit>(webhookManagementPath(webhook, guildRef, '/avatar'), {
    method: 'PUT',
    body: JSON.stringify({ attachment_id: attachmentId }),
    signal
  });
}

export function deleteGuildWebhookAvatar(
  guildRef: string,
  webhook: WebhookSummary
): Promise<WebhookSummary> {
  return api<WebhookSummary>(webhookManagementPath(webhook, guildRef, '/avatar'), {
    method: 'DELETE'
  });
}
