import { resolve } from '$app/paths';
import { entityRef, type FederatedIdentity } from '$lib/chat/refs';

export function guildChannelPath(guild: FederatedIdentity, channel: FederatedIdentity): string {
  const guildId = encodeURIComponent(entityRef(guild));
  const channelId = encodeURIComponent(entityRef(channel));
  return resolve(`/g/${guildId}/${channelId}`);
}

export function guildSettingsPath(guild: FederatedIdentity): string {
  return resolve(`/g/${encodeURIComponent(entityRef(guild))}/settings`);
}

export type ChannelSettingsPanel =
  'overview' | 'permissions' | 'invites' | 'integrations' | 'delete';

export function channelSettingsPath(
  guild: FederatedIdentity,
  channel: FederatedIdentity,
  panel: ChannelSettingsPanel = 'overview'
): string {
  const path = resolve(
    `/g/${encodeURIComponent(entityRef(guild))}/${encodeURIComponent(entityRef(channel))}/settings`
  );
  return `${path}?${new URLSearchParams({ panel }).toString()}`;
}

export function directMessagePath(channel: FederatedIdentity): string {
  return resolve(`/home/${encodeURIComponent(entityRef(channel))}`);
}
