import { describe, expect, it } from 'vitest';
import {
  finalizePollPath,
  forwardedMessagePath,
  interactionPollVotersPath,
  interactionPollVotePath,
  pollVotersPath,
  pollVotePath
} from './interactions';

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
