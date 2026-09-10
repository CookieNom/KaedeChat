import { describe, expect, it } from 'vitest';
import { botInvitesInMessage, normalizeBotInvite } from './bot-invites';

describe('bot invite links', () => {
  it('accepts exact secure application-home install links', () => {
    expect(
      normalizeBotInvite('https://apps.example/applications/123@apps.example/install/community')
    ).toEqual({ applicationRef: '123@apps.example', templateSlug: 'community' });
  });

  it('preserves the application authority in links shared from another instance', () => {
    expect(
      normalizeBotInvite('https://chat.example/applications/123%40apps.example/install/community')
    ).toEqual({ applicationRef: '123@apps.example', templateSlug: 'community' });
    expect(normalizeBotInvite('https://apps.example/applications/123/install/community')).toEqual({
      applicationRef: '123@apps.example',
      templateSlug: 'community'
    });
  });

  it('rejects insecure links, credentials, ports, and suffix paths', () => {
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

  it('validates whole message URLs instead of embedding valid-looking prefixes', () => {
    const link = 'https://apps.example/applications/123@apps.example/install/community';
    for (const suffix of ['/more', '?tracking=true', '#fragment']) {
      expect(botInvitesInMessage(`${link}${suffix}`)).toEqual([]);
    }
    expect(botInvitesInMessage(`(${link}/).`)).toEqual([
      { applicationRef: '123@apps.example', templateSlug: 'community' }
    ]);
    expect(
      botInvitesInMessage(
        `${link} https://chat.example/applications/123%40apps.example/install/community`
      )
    ).toHaveLength(1);
  });

  it('deduplicates embedded invitations', () => {
    const link = 'https://apps.example/applications/123@apps.example/install/community';
    expect(botInvitesInMessage(`${link} ${link}`)).toEqual([
      { applicationRef: '123@apps.example', templateSlug: 'community' }
    ]);
  });
});
