import { describe, expect, it, vi } from 'vitest';
import { loadMessageHistory } from './message-history';
import type { Message } from './types';

const message = (id: string) => ({ id, origin_domain: 'chat.example' }) as Message;

describe('message history newer boundary', () => {
  it.each([1, 50])(
    'does not offer newer messages when all %i loaded messages reach the end',
    async (size) => {
      const messages = Array.from({ length: size }, (_, index) => message(String(100 - index)));
      const request = vi.fn().mockResolvedValueOnce(messages).mockResolvedValueOnce([]);

      await expect(
        loadMessageHistory('5@chat.example', '75@chat.example', request)
      ).resolves.toEqual({
        messages,
        hasLater: false
      });
      expect(request.mock.calls).toEqual([
        ['/channels/5%40chat.example/messages?around=75%40chat.example'],
        ['/channels/5%40chat.example/messages?after=100%40chat.example&limit=1']
      ]);
    }
  );

  it('offers pagination when messages exist beyond the loaded window', async () => {
    const messages = [message('100'), message('99')];
    const request = vi
      .fn()
      .mockResolvedValueOnce(messages)
      .mockResolvedValueOnce([message('101')]);

    await expect(loadMessageHistory('5@chat.example', '99@chat.example', request)).resolves.toEqual(
      {
        messages,
        hasLater: true
      }
    );
  });

  it.each([
    { around: null, messages: [message('100')] },
    { around: '99@chat.example', messages: [] }
  ])(
    'skips the boundary check for latest or empty history: $around',
    async ({ around, messages }) => {
      const request = vi.fn().mockResolvedValueOnce(messages);

      await expect(loadMessageHistory('5@chat.example', around, request)).resolves.toEqual({
        messages,
        hasLater: false
      });
      expect(request).toHaveBeenCalledTimes(1);
    }
  );

  it('propagates a failed boundary check instead of claiming history is complete', async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce([message('100')])
      .mockRejectedValueOnce(new Error('offline'));

    await expect(loadMessageHistory('5@chat.example', '99@chat.example', request)).rejects.toThrow(
      'offline'
    );
  });
});
