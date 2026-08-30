import { describe, expect, it } from 'vitest';

import {
  compareEntityRefs,
  entityKey,
  entityRef,
  isCanonicalFederationDomain,
  matchesEntityRef,
  parseCanonicalEntityRef,
  sameEntity
} from './refs';

const remote = { id: '42', origin_domain: 'chat.example' };

describe('federated entity references', () => {
  it('qualifies instance-scoped snowflakes', () => {
    expect(entityRef(remote)).toBe('42@chat.example');
    expect(entityKey(remote)).toBe('42@chat.example');
  });

  it('matches canonical refs and only local legacy shorthand', () => {
    expect(matchesEntityRef('42@chat.example', remote, 'local.example')).toBe(true);
    expect(matchesEntityRef('42', remote, 'local.example')).toBe(false);
    expect(matchesEntityRef('42', remote, 'chat.example')).toBe(true);
    expect(matchesEntityRef('42@other.example', remote, 'local.example')).toBe(false);
  });

  it('rejects malformed identities received at runtime without throwing', () => {
    const malformed = { id: '42', origin_domain: undefined } as unknown as typeof remote;
    expect(matchesEntityRef('42', malformed, 'chat.example')).toBe(false);
  });

  it('never treats colliding snowflakes from different homes as equal', () => {
    expect(sameEntity(remote, { id: '42', origin_domain: 'other.example' })).toBe(false);
    expect(compareEntityRefs(remote, { id: '42', origin_domain: 'other.example' })).toBeLessThan(0);
    expect(compareEntityRefs({ id: '100', origin_domain: 'a.example' }, remote)).toBeGreaterThan(0);
  });

  it('strictly parses canonical gateway references', () => {
    expect(isCanonicalFederationDomain('chat.example')).toBe(true);
    expect(isCanonicalFederationDomain('Chat.example')).toBe(false);
    expect(isCanonicalFederationDomain('chat..example')).toBe(false);
    expect(parseCanonicalEntityRef('42@chat.example', 'chat.example')).toEqual(remote);
    expect(parseCanonicalEntityRef('42@other.example', 'chat.example')).toBeNull();
    expect(parseCanonicalEntityRef('0@chat.example')).toBeNull();
    expect(parseCanonicalEntityRef('9223372036854775808@chat.example')).toBeNull();
  });
});
