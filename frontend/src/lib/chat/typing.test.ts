import { describe, expect, it } from 'vitest';

import { activeTypingParticipants, typingLabel, upsertTypingParticipant } from './typing';

describe('typing participants', () => {
  it('updates one participant without duplicating it', () => {
    const first = upsertTypingParticipant([], { ref: '1@a', name: 'A' }, 100, 10);
    expect(upsertTypingParticipant(first, { ref: '1@a', name: 'Alice' }, 105, 10)).toEqual([
      { ref: '1@a', name: 'Alice', expiresAt: 115 }
    ]);
  });

  it('expires stale participants and formats bounded names', () => {
    const participants = ['A', 'B', 'C', 'D'].map((name, index) => ({
      ref: `${index}@a`,
      name,
      expiresAt: index === 0 ? 9 : 20
    }));
    const active = activeTypingParticipants(participants, 10);
    expect(typingLabel(active)).toBe('B, C, and D are typing…');
    expect(typingLabel([...active, { ref: '4@a', name: 'E', expiresAt: 20 }])).toBe(
      'B, C, and 2 more are typing…'
    );
  });
});
