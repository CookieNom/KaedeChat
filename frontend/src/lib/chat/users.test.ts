import { describe, expect, it } from 'vitest';
import type { UserSummary } from './types';
import { isApplicationUser, isSystemUser } from './users';

const human: UserSummary = {
  id: '1',
  origin_domain: 'chat.example',
  username: 'maple',
  display_name: 'Definitely APP',
  avatar_hash: null,
  handle: 'maple@chat.example'
};

describe('trusted application identity', () => {
  it('accepts only server-projected account discriminators', () => {
    expect(isApplicationUser({ ...human, account_type: 'bot' })).toBe(true);
    expect(isApplicationUser({ ...human, bot: true })).toBe(true);
    expect(isApplicationUser(human)).toBe(false);
    expect(isApplicationUser({ ...human, display_name: 'APP' })).toBe(false);
  });
});

it('does not infer system authority from a name or bot flag', () => {
  expect(isSystemUser({ ...human, display_name: 'Kaede System', username: 'system' })).toBe(false);
  expect(isSystemUser({ ...human, account_type: 'bot', bot: true })).toBe(false);
  expect(isSystemUser({ ...human, account_type: 'system' })).toBe(true);
  expect(isApplicationUser({ ...human, account_type: 'system' })).toBe(false);
});
