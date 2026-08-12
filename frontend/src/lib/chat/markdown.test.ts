import { describe, expect, it } from 'vitest';
import { mentionPresentation, splitSpoilers, tokenizeText, tokenKind } from './markdown';

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
    expect(tokenizeText('party <:party_blob:75512661369970689@chat.example> now')).toEqual([
      { text: 'party ', kind: 'text' },
      {
        text: '<:party_blob:75512661369970689@chat.example>',
        kind: 'emoji'
      },
      { text: ' now', kind: 'text' }
    ]);
    expect(tokenKind('<a:dance:75512661369970690@chat.example>')).toBe('emoji');
    expect(tokenizeText('hello <@&75512661369970691@chat.example>')).toEqual([
      { text: 'hello ', kind: 'text' },
      { text: '<@&75512661369970691@chat.example>', kind: 'mention' }
    ]);
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

  it('keeps an unresolved explicit mention actionable by ref without exposing its fake handle', () => {
    const mention = mentionPresentation(
      '<@42@remote.example>',
      [
        {
          id: '42',
          origin_domain: 'remote.example',
          username: 'history_deadbeef',
          display_name: null,
          avatar_hash: null,
          handle: 'history_deadbeef@remote.example',
          profile_resolved: false
        }
      ],
      'local.example'
    );

    expect(mention.text).toBe('@Remote user · remote.example');
    expect(mention.userRef).toBe('42@remote.example');
    expect(mention.userHandle).toBeUndefined();
    expect(mention.title).toBe('Remote user · remote.example');
    expect(JSON.stringify(mention)).not.toContain('history_deadbeef');
  });
});
