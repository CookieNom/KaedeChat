import { describe, expect, it } from 'vitest';
import {
  channelInviteListPath,
  guildInviteManagementPath,
  guildInviteUrl,
  federatedInviteHomeUrl,
  inviteReferencesInMessage,
  normalizeInviteReference
} from './invites';

describe('normalizeInviteReference', () => {
  it('accepts local and federated invite codes', () => {
    expect(normalizeInviteReference('Ab12Cd34')).toBe('Ab12Cd34');
    expect(normalizeInviteReference('Ab12Cd34@chat.example')).toBe('Ab12Cd34@chat.example');
  });

  it('turns invite URLs into federated references', () => {
    expect(normalizeInviteReference('https://chat.example/invite/Ab12Cd34')).toBe(
      'Ab12Cd34@chat.example'
    );
    expect(normalizeInviteReference('chat.example/invite/Ab12Cd34')).toBe('Ab12Cd34@chat.example');
  });

  it('rejects ambiguous or unsafe URLs', () => {
    expect(normalizeInviteReference('https://user@chat.example/invite/Ab12Cd34')).toBeNull();
    expect(normalizeInviteReference('https://chat.example:8443/invite/Ab12Cd34')).toBeNull();
    expect(
      normalizeInviteReference('https://chat.example/invite/Ab12Cd34@other.example')
    ).toBeNull();
    expect(normalizeInviteReference('https://chat.example/other/Ab12Cd34')).toBeNull();
  });
});

describe('inviteReferencesInMessage', () => {
  it('extracts unique invite links and ignores ordinary URLs', () => {
    expect(
      inviteReferencesInMessage(
        'Join https://chat.example/invite/Ab12Cd34 or https://chat.example/invite/Ab12Cd34. See https://example.org.'
      )
    ).toEqual(['Ab12Cd34@chat.example']);
  });
});

describe('guildInviteUrl', () => {
  it('keeps local development origins and routes remote codes to their authority', () => {
    expect(guildInviteUrl('Ab12Cd34', 'chat.example', 'http://chat.example:5173')).toBe(
      'http://chat.example:5173/invite/Ab12Cd34'
    );
    expect(guildInviteUrl('Ab12Cd34', 'remote.example', 'https://chat.example')).toBe(
      'https://remote.example/invite/Ab12Cd34'
    );
  });

  it('routes a remote invite through a recipient-owned home without losing authority', () => {
    expect(federatedInviteHomeUrl('Ab12Cd34', 'guild.example', 'home.example')).toBe(
      'https://home.example/invite/Ab12Cd34%40guild.example'
    );
    expect(federatedInviteHomeUrl('Ab12Cd34', 'guild.example', 'guild.example')).toBe(
      'https://guild.example/invite/Ab12Cd34'
    );
    expect(federatedInviteHomeUrl('Ab12Cd34@guild.example', 'guild.example', 'other.example')).toBe(
      'https://other.example/invite/Ab12Cd34%40guild.example'
    );
    expect(federatedInviteHomeUrl('Ab12Cd34', 'guild.example', 'https://evil.example')).toBeNull();
  });

  it('binds remote revocation to the exact qualified guild', () => {
    expect(guildInviteManagementPath('Ab12Cd34', 'remote.example', '1@remote.example')).toBe(
      '/invites/Ab12Cd34%40remote.example?guild_ref=1%40remote.example'
    );
  });

  it('binds channel invite listing to the qualified channel authority', () => {
    expect(channelInviteListPath('20@remote.example')).toBe(
      '/channels/20%40remote.example/invites'
    );
  });
});
