import { describe, expect, it } from 'vitest';
import { splitSpoilers, tokenizeText, tokenKind } from './markdown';

describe('message markdown tokenization', () => {
  it('classifies immutable handles, channels, and emoji without dropping text', () => {
    expect(tokenizeText('Hi @alice@chat.example in #general :kaede:')).toEqual([
      { text: 'Hi ', kind: 'text' },
      { text: '@alice@chat.example', kind: 'mention' },
      { text: ' in ', kind: 'text' },
      { text: '#general', kind: 'channel' },
      { text: ' ', kind: 'text' },
      { text: ':kaede:', kind: 'emoji' }
    ]);
    expect(tokenKind(':wave:')).toBe('emoji');
    expect(tokenizeText('Hi <@75512661369970688@chat.example>!')).toEqual([
      { text: 'Hi ', kind: 'text' },
      { text: '<@75512661369970688@chat.example>', kind: 'mention' },
      { text: '!', kind: 'text' }
    ]);
    expect(tokenKind('<@75512661369970688>')).toBe('mention');
  });

  it('separates multiple spoilers without consuming surrounding markdown text', () => {
    expect(splitSpoilers('before ||secret|| middle ||second line\ncontinues|| after')).toEqual([
      'before ',
      '||secret||',
      ' middle ',
      '||second line\ncontinues||',
      ' after'
    ]);
  });
});
