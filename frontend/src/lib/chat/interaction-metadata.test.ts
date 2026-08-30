import { describe, expect, it } from 'vitest';
import { interactionAttributionText } from './interaction-metadata';
import type { MessageInteractionMetadata } from './types';

const metadata = (
  overrides: Partial<MessageInteractionMetadata> = {}
): MessageInteractionMetadata => ({
  id: '1',
  origin_domain: 'one.example',
  interaction_ref: '1@one.example',
  type: 'command',
  user: {
    id: '2',
    origin_domain: 'two.example',
    username: 'alice',
    display_name: 'Alice',
    avatar_hash: null,
    bot: false
  },
  user_ref: '2@two.example',
  application_ref: '3@three.example',
  integration_type: 'guild_install',
  authorizing_integration_owners: { guild_install: '4@one.example' },
  command_name: 'ship',
  command_type: 'chat_input',
  ...overrides
});

describe('interaction metadata attribution', () => {
  it('uses slash syntax only for chat-input commands', () => {
    expect(interactionAttributionText({ deleted_at: null, interaction_metadata: metadata() })).toBe(
      'Alice used /ship'
    );
    expect(
      interactionAttributionText({
        deleted_at: null,
        interaction_metadata: metadata({ command_type: 'message' })
      })
    ).toBe('Alice used ship');
  });

  it('labels component/modal activity without response content', () => {
    expect(
      interactionAttributionText({
        deleted_at: null,
        interaction_metadata: metadata({ type: 'component', command_name: undefined })
      })
    ).toBe('Alice used a message component');
    expect(
      interactionAttributionText({
        deleted_at: null,
        interaction_metadata: metadata({ type: 'modal_submit', command_name: undefined })
      })
    ).toBe('Alice submitted a form');
  });

  it('fails closed for deleted or malformed command attribution', () => {
    expect(
      interactionAttributionText({
        deleted_at: '2026-08-28T00:00:00Z',
        interaction_metadata: metadata()
      })
    ).toBeNull();
    expect(
      interactionAttributionText({
        deleted_at: null,
        interaction_metadata: metadata({ command_name: ' ' })
      })
    ).toBeNull();
  });
});
