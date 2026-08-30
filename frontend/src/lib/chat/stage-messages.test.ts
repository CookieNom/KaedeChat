import { describe, expect, it } from 'vitest';
import { stageSystemMessageText } from './stage-messages';

describe('Stage lifecycle messages', () => {
  it('renders lifecycle events at their normal timeline location', () => {
    expect(stageSystemMessageText(27, 'Mina', 'Town Hall')).toBe('Mina started a Stage: Town Hall');
    expect(stageSystemMessageText(28, 'Mina', 'Town Hall')).toBe('Mina ended the Stage: Town Hall');
    expect(stageSystemMessageText(29, 'Mina', null)).toBe('Mina became a speaker.');
    expect(stageSystemMessageText(31, 'Mina', 'Questions')).toBe(
      'Mina changed the Stage topic: Questions'
    );
  });
});
