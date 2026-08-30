import { describe, expect, it } from 'vitest';
import {
  channelSettingsPath,
  directMessagePath,
  guildApplicationDirectoryPath,
  guildChannelPath,
  guildIntegrationsPath,
  guildSettingsPath,
  resolveApplicationDirectoryPath
} from './routes';

const guild = { id: '100', origin_domain: 'chat.example' };
const channel = { id: '200', origin_domain: 'remote.example' };

describe('federated application routes', () => {
  it('resolves App Directory query and detail paths without losing their suffix', () => {
    expect(resolveApplicationDirectoryPath('/application-directory?q=weather')).toBe(
      '/application-directory?q=weather'
    );
    expect(resolveApplicationDirectoryPath('/application-directory/42%40apps.example')).toBe(
      '/application-directory/42%40apps.example'
    );
    expect(() => resolveApplicationDirectoryPath('/application-directory-evil')).toThrow();
  });

  it('resolves guild and channel parameters before returning the path', () => {
    expect(guildChannelPath(guild, channel)).toBe('/g/100%40chat.example/200%40remote.example');
  });

  it('resolves guild settings and direct-message paths', () => {
    expect(guildSettingsPath(guild)).toBe('/g/100%40chat.example/settings');
    expect(guildIntegrationsPath(guild)).toBe('/g/100%40chat.example/integrations');
    expect(guildApplicationDirectoryPath(guild)).toBe(
      '/application-directory?from=%2Fg%2F100%2540chat.example%2Fsettings'
    );
    expect(channelSettingsPath(guild, channel)).toBe(
      '/g/100%40chat.example/200%40remote.example/settings?panel=overview'
    );
    expect(channelSettingsPath(guild, channel, 'permissions')).toBe(
      '/g/100%40chat.example/200%40remote.example/settings?panel=permissions'
    );
    expect(directMessagePath(channel)).toBe('/home/200%40remote.example');
  });
});
