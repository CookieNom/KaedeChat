import { describe, expect, it } from 'vitest';
import { inviteReferencesInMessage, normalizeInviteReference } from './invites';

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
