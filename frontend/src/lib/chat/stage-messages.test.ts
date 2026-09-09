import { describe, expect, it } from 'vitest';
import { stageSystemMessageText } from './stage-messages';

describe('Stage lifecycle messages', () => {
  it('stage lifecycle text mapping', () => {
    for (const [type, topic, action] of [
      [27, 'Town Hall', /start/i],
      [28, 'Town Hall', /end/i],
      [29, null, /speaker/i],
      [31, 'Questions', /topic/i]
    ] as const) {
      const text = stageSystemMessageText(type, 'Mina', topic);
      expect(text).toContain('Mina');
      expect(text).toMatch(action);
      if (topic) expect(text).toContain(topic);
    }
  });
});
