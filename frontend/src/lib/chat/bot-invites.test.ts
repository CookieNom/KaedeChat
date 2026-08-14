import { describe, expect, it } from 'vitest';
import { botInvitesInMessage, normalizeBotInvite } from './bot-invites';

describe('bot invite links', () => {
  it('accepts exact secure application-home install links', () => {
    expect(
      normalizeBotInvite('https://apps.example/applications/123@apps.example/install/community')
    ).toEqual({ applicationRef: '123@apps.example', templateSlug: 'community' });
  });

  it('rejects cross-origin identities, insecure links, credentials, ports, and suffix paths', () => {
    expect(
      normalizeBotInvite('https://apps.example/applications/1@evil.example/install/x')
    ).toBeNull();
    expect(
      normalizeBotInvite('http://apps.example/applications/1@apps.example/install/x')
    ).toBeNull();
    expect(
      normalizeBotInvite('https://u@apps.example/applications/1@apps.example/install/x')
    ).toBeNull();
    expect(
      normalizeBotInvite('https://apps.example:8443/applications/1@apps.example/install/x')
    ).toBeNull();
    expect(
      normalizeBotInvite('https://apps.example/applications/1@apps.example/install/x/more')
    ).toBeNull();
  });

  it('deduplicates embedded invitations', () => {
    const link = 'https://apps.example/applications/123@apps.example/install/community';
    expect(botInvitesInMessage(`${link} ${link}`)).toHaveLength(1);
  });
});
