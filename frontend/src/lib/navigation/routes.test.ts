import { describe, expect, it } from 'vitest';
import {
  channelSettingsPath,
  directMessagePath,
  guildChannelPath,
  guildSettingsPath
} from './routes';

const guild = { id: '100', origin_domain: 'chat.example' };
const channel = { id: '200', origin_domain: 'remote.example' };

describe('federated application routes', () => {
  it('resolves guild and channel parameters before returning the path', () => {
    expect(guildChannelPath(guild, channel)).toBe('/g/100%40chat.example/200%40remote.example');
  });

  it('resolves guild settings and direct-message paths', () => {
    expect(guildSettingsPath(guild)).toBe('/g/100%40chat.example/settings');
    expect(channelSettingsPath(guild, channel)).toBe(
      '/g/100%40chat.example/200%40remote.example/settings?panel=overview'
    );
    expect(channelSettingsPath(guild, channel, 'permissions')).toBe(
      '/g/100%40chat.example/200%40remote.example/settings?panel=permissions'
    );
    expect(directMessagePath(channel)).toBe('/home/200%40remote.example');
  });
});
