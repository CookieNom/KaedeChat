import { api } from '$lib/api/client';
import type { ReadStateStatus } from '$lib/chat/types';
import { chatEntities } from '$lib/stores/entities.svelte';

export async function markConversationsRead(filter: { guild?: string; channel?: string } = {}) {
  const user = chatEntities.currentUser;
  const snapshot = await api<ReadStateStatus[]>('/users/@me/read-states');
  // A fixed snapshot ensures messages arriving during the operation remain unread.
  for (const state of snapshot) {
    if (chatEntities.currentUser !== user) return;
    const channel = `${state.channel_id}@${state.channel_domain}`;
    if (filter.channel && filter.channel !== channel) continue;
    if (filter.guild && filter.guild !== `${state.guild_id}@${state.guild_domain}`) continue;
    if (state.can_read_history === false) continue;
    if (!state.last_message_id || !state.last_message_domain) continue;
    await api(`/channels/${encodeURIComponent(channel)}/ack`, {
      method: 'POST',
      body: JSON.stringify({
        message_id: `${state.last_message_id}@${state.last_message_domain}`,
        read_version: state.read_version ?? 0
      })
    });
  }
  if (chatEntities.currentUser !== user) return;
  const refreshed = await api<ReadStateStatus[]>('/users/@me/read-states');
  if (chatEntities.currentUser === user) chatEntities.readStates.replace(refreshed);
}
