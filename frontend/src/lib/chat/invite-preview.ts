import { api } from '$lib/api/client';
import { sameEntity } from './refs';
import { firstNavigableChannel } from './channels';
import type { Channel, Guild } from './types';

export interface InvitePreview {
  code: string;
  guild: Guild;
  channel_id: string | null;
  expires_at: string | null;
  uses?: number;
  max_uses?: number | null;
  temporary?: boolean;
  target_type?: 'stream' | null;
  role_ids?: string[];
  target_user_count?: number;
  guild_scheduled_event?: {
    name: string;
    scheduled_start_time?: string;
  } | null;
}

export function invitePreviewDetails(preview: InvitePreview): string[] {
  const details: string[] = [];
  if (preview.max_uses != null) {
    details.push(`${Math.max(0, preview.max_uses - (preview.uses ?? 0))} uses remain`);
  }
  if (preview.role_ids?.length) {
    details.push(
      `Grants ${preview.role_ids.length} role${preview.role_ids.length === 1 ? '' : 's'}`
    );
  }
  if ((preview.target_user_count ?? 0) > 0) details.push('Limited invitation');
  if (preview.target_type === 'stream') details.push('Opens a Go Live stream');
  if (preview.guild_scheduled_event?.name) {
    details.push(`Event: ${preview.guild_scheduled_event.name}`);
  }
  return details;
}

export function invitedChannel(guild: Guild, channelId: string | null): Channel | null {
  const channels = guild.channels ?? [];
  if (channelId) {
    const exact = channels.find(
      (channel) => channel.id === channelId && channel.origin_domain === guild.origin_domain
    );
    if (exact && exact.type !== 4) return exact;
  }
  return firstNavigableChannel(channels);
}

export function hasJoinedGuild(guilds: Guild[], invited: Guild): boolean {
  return guilds.some((guild) => sameEntity(guild, invited));
}

interface CacheEntry {
  expiresAt: number;
  result: Promise<InvitePreview>;
}

const previewCache = new Map<string, CacheEntry>();
const SUCCESS_TTL_MS = 5 * 60_000;
const FAILURE_TTL_MS = 15_000;

export function loadInvitePreview(reference: string): Promise<InvitePreview> {
  const now = Date.now();
  const cached = previewCache.get(reference);
  if (cached && cached.expiresAt > now) return cached.result;
  if (previewCache.size >= 256) {
    const oldest = previewCache.keys().next().value;
    if (oldest) previewCache.delete(oldest);
  }

  const entry: CacheEntry = {
    expiresAt: now + SUCCESS_TTL_MS,
    result: api<InvitePreview>(`/invites/${encodeURIComponent(reference)}`).catch((error) => {
      entry.expiresAt = Date.now() + FAILURE_TTL_MS;
      throw error;
    })
  };
  previewCache.set(reference, entry);
  return entry.result;
}
