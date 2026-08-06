import { describe, expect, it } from 'vitest';

import { mentionsUser } from './mentions';

const ann = {
  id: '75512661369970688',
  origin_domain: 'chat.example',
  username: 'ann',
  handle: 'ann@chat.example'
};

describe('message mention matching', () => {
  it('matches complete usernames and federated handles case-insensitively', () => {
    expect(mentionsUser('Hello @ANN!', ann)).toBe(true);
    expect(mentionsUser('Hello @ann.', ann)).toBe(true);
    expect(mentionsUser('Hello @ann@chat.example.', ann)).toBe(true);
    expect(mentionsUser('Hello <@75512661369970688@chat.example>', ann)).toBe(true);
    expect(mentionsUser('Hello <@75512661369970688>', ann, 'chat.example')).toBe(true);
  });

  it('does not notify prefix matches, email fragments, or other handles', () => {
    expect(mentionsUser('Hello @anna', ann)).toBe(false);
    expect(mentionsUser('Email me at friend@ann.test', ann)).toBe(false);
    expect(mentionsUser('Hello @ann@other.example', ann)).toBe(false);
    expect(mentionsUser('Hello <@75512661369970689@chat.example>', ann)).toBe(false);
    expect(mentionsUser('Hello <@75512661369970688>', ann, 'other.example')).toBe(false);
  });
});
