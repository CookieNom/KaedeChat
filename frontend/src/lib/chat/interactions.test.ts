import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '$lib/api/client';
import { applyLocale, setSystemLanguages } from '$lib/ui/locale';
import {
  createInteraction,
  finalizePollPath,
  forwardedMessagePath,
  interactionPollVotersPath,
  interactionPollVotePath,
  pollVotersPath,
  pollVotePath
} from './interactions';

vi.mock('$lib/api/client', () => ({ api: vi.fn() }));
vi.mock('./interaction-responses.svelte', () => ({
  interactionResponses: { register: vi.fn() }
}));

afterEach(() => {
  applyLocale('en-US');
  setSystemLanguages();
  vi.clearAllMocks();
});

it('sends the current device language for every interaction type', async () => {
  vi.mocked(api).mockResolvedValue({ interaction_ref: '9@chat.example' });
  applyLocale('system');
  for (const [deviceLocale, locale] of [
    ['ja-JP', 'ja-JP'],
    ['en-GB', 'en-GB'],
    ['en_US', 'en-US'],
    ['en-US-u-ca-gregory', 'en-US']
  ]) {
    setSystemLanguages([deviceLocale]);
    for (const type of ['command', 'autocomplete', 'component', 'modal_submit']) {
      await createInteraction(
        { channelRef: '1@chat.example', applicationRef: '2@chat.example' },
        { interaction_type: type }
      );
      const options = vi.mocked(api).mock.lastCall![1]!;
      expect(JSON.parse(options.body as string)).toMatchObject({
        interaction_type: type,
        locale
      });
    }
  }
});

describe('rich-message API paths', () => {
  it('keeps composite references encoded as one path segment', () => {
    expect(pollVotePath('1@chat.example', '2@chat.example', 3)).toBe(
      '/channels/1%40chat.example/messages/2%40chat.example/polls/answers/3/@me'
    );
    expect(forwardedMessagePath('1@chat.example', '2@chat.example')).toBe(
      '/channels/1%40chat.example/messages/2%40chat.example/forwarded'
    );
    expect(pollVotersPath('1@chat.example', '2@chat.example', 3, '9@remote.example')).toBe(
      '/channels/1%40chat.example/messages/2%40chat.example/polls/answers/3?after=9%40remote.example'
    );
    expect(finalizePollPath('1@chat.example', '2@chat.example')).toBe(
      '/channels/1%40chat.example/messages/2%40chat.example/polls/expire'
    );
    expect(interactionPollVotePath('10', '20', 3)).toBe(
      '/interactions/10/responses/20/polls/answers/3/@me'
    );
    expect(interactionPollVotersPath('10', '20', 3, '9@remote.example')).toBe(
      '/interactions/10/responses/20/polls/answers/3?after=9%40remote.example'
    );
  });
});
