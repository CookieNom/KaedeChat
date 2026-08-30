import { resolve } from '$app/paths';
import { directoryEntryPath } from '$lib/chat/application-directory';
import { entityRef, type FederatedIdentity } from '$lib/chat/refs';

const APPLICATION_DIRECTORY_ROOT = '/application-directory';

export function resolveApplicationDirectoryPath(path = APPLICATION_DIRECTORY_ROOT): string {
  const suffix = path.slice(APPLICATION_DIRECTORY_ROOT.length);
  if (
    !path.startsWith(APPLICATION_DIRECTORY_ROOT) ||
    (suffix && suffix[0] !== '/' && suffix[0] !== '?' && suffix[0] !== '#')
  ) {
    throw new Error('Invalid App Directory path.');
  }
  return `${resolve('/application-directory')}${suffix}`;
}

export function guildChannelPath(guild: FederatedIdentity, channel: FederatedIdentity): string {
  const guildId = encodeURIComponent(entityRef(guild));
  const channelId = encodeURIComponent(entityRef(channel));
  return resolve(`/g/${guildId}/${channelId}`);
}

export function guildSettingsPath(guild: FederatedIdentity): string {
  return resolve(`/g/${encodeURIComponent(entityRef(guild))}/settings`);
}

export function guildIntegrationsPath(guild: FederatedIdentity): string {
  return resolve(`/g/${encodeURIComponent(entityRef(guild))}/integrations`);
}

export function guildApplicationDirectoryPath(guild: FederatedIdentity): string {
  const sourcePath = `/g/${encodeURIComponent(entityRef(guild))}/settings`;
  return resolveApplicationDirectoryPath(directoryEntryPath(sourcePath));
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
