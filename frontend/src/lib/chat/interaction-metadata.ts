import type { Message } from './types';

/** Discord-style, content-independent application response attribution. */
export function interactionAttributionText(
  message: Pick<Message, 'deleted_at' | 'interaction_metadata'>
): string | null {
  const metadata = message.deleted_at ? null : message.interaction_metadata;
  if (!metadata) return null;
  const actor = metadata.user.display_name?.trim() || metadata.user.username.trim();
  if (!actor) return null;
  if (metadata.type === 'command') {
    const command = metadata.command_name?.trim();
    if (!command) return null;
    return metadata.command_type === 'chat_input'
      ? `${actor} used /${command}`
      : `${actor} used ${command}`;
  }
  if (metadata.type === 'component') return `${actor} used a message component`;
  if (metadata.type === 'modal_submit') return `${actor} submitted a form`;
  return null;
}
