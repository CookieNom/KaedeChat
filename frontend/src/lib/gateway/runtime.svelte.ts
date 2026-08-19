import { entityKey } from '$lib/chat/refs';
import type {
  Channel,
  CustomEmoji,
  Guild,
  GuildMemberSummary,
  Message,
  PresenceStatus,
  ReadStateStatus,
  Role,
  UserSummary
} from '$lib/chat/types';
import { browserNotifications } from '$lib/notifications/browser.svelte';
import {
  applyIncomingMessage,
  applyReadStateDispatch,
  type ReadStateDispatch
} from '$lib/notifications/read-state';
import { chatEntities } from '$lib/stores/entities.svelte';
import { guildNavigation } from '$lib/stores/guild-navigation.svelte';
import { GATEWAY_STATUS_EVENT, GatewayClient, type Dispatch, type GatewayStatus } from './client';

type ReadyPayload = {
  user: UserSummary;
  guilds: Guild[];
  dm_channels: Channel[];
  read_states: ReadStateStatus[];
  presence_preference?: 'online' | 'idle' | 'dnd' | 'invisible';
};

function applyOwnPresencePreference(
  preference: 'online' | 'idle' | 'dnd' | 'invisible' | undefined
): void {
  if (!preference) return;
  authenticatedGateway.client.rememberPresence(preference);
  if (chatEntities.currentUser) {
    chatEntities.setPresence(
      chatEntities.currentUser,
      preference === 'invisible' ? 'offline' : preference
    );
  }
  try {
    globalThis.localStorage?.setItem('kaede.presence', preference);
  } catch {
    // Live state still updates when persistent storage is unavailable.
  }
}

function applyEntityDispatch(dispatch: Dispatch): void {
  switch (dispatch.t) {
    case 'READY': {
      const ready = dispatch.d as ReadyPayload;
      // A full gateway re-identify may happen after a tab has slept or a resume
      // cursor expires. The route reload restores messages over HTTP, while the
      // roster is a separate gateway request. Keep the same account's last
      // known roster until that request arrives instead of flashing—or
      // indefinitely leaving—an empty member list. Account changes still clear
      // all member data before the new READY is applied.
      chatEntities.beginGatewaySession(ready.user);
      chatEntities.ingestGuilds(ready.guilds);
      chatEntities.ingestDirectMessages(ready.dm_channels);
      chatEntities.readStates.upsertMany(ready.read_states);
      applyOwnPresencePreference(ready.presence_preference);
      return;
    }
    case 'RESUMED': {
      const resumed = dispatch.d as {
        presence_preference?: 'online' | 'idle' | 'dnd' | 'invisible';
      };
      applyOwnPresencePreference(resumed.presence_preference);
      return;
    }
    case 'MESSAGE_CREATE': {
      const message = dispatch.d as Message;
      chatEntities.messages.upsert(message);
      const channel = chatEntities.channels.get(`${message.channel_id}@${message.channel_domain}`);
      if (channel?.guild_id === null) chatEntities.channels.upsert(channel, { append: false });
      chatEntities.readStates.replace(
        applyIncomingMessage(
          chatEntities.readStates.values,
          message,
          chatEntities.currentUser,
          channel ?? null
        )
      );
      return;
    }
    case 'MESSAGE_UPDATE':
      chatEntities.messages.upsert(dispatch.d as Message);
      return;
    case 'MESSAGE_DELETE': {
      const message = dispatch.d as Partial<Message> & { id: string; origin_domain: string };
      chatEntities.messages.update(entityKey(message), (current) => ({
        ...current,
        ...message,
        content: null,
        deleted_at: message.deleted_at ?? new Date().toISOString()
      }));
      return;
    }
    case 'CHANNEL_CREATE':
    case 'CHANNEL_UPDATE':
    case 'CHANNEL_ACCESS_GRANTED': {
      const channel = dispatch.d as Channel;
      chatEntities.channels.upsert(channel, { append: channel.guild_id !== null });
      return;
    }
    case 'CHANNEL_PERMISSION_UPDATE': {
      const update = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        permissions: string;
      };
      chatEntities.channels.update(`${update.channel_id}@${update.channel_domain}`, (channel) => ({
        ...channel,
        permissions: update.permissions
      }));
      return;
    }
    case 'CHANNEL_ACCESS_REVOKED': {
      const revoked = dispatch.d as { channel_id: string; channel_domain: string };
      chatEntities.channels.remove(`${revoked.channel_id}@${revoked.channel_domain}`);
      return;
    }
    case 'CHANNEL_DELETE': {
      const channel = dispatch.d as { id: string; origin_domain: string };
      chatEntities.channels.remove(entityKey(channel));
      return;
    }
    case 'GUILD_CREATE':
    case 'GUILD_UPDATE':
      chatEntities.ingestGuilds([dispatch.d as Guild]);
      return;
    case 'GUILD_HISTORY_SYNC_UPDATE': {
      const update = dispatch.d as {
        guild_id: string;
        guild_domain: string;
        status: Guild['history_sync_status'];
        code?: string | null;
        retry_after_ms?: number | null;
        resource?: string | null;
      };
      chatEntities.guilds.update(`${update.guild_id}@${update.guild_domain}`, (guild) => ({
        ...guild,
        history_sync_status: update.status,
        history_sync_error_code: update.status === 'ready' ? null : (update.code ?? null),
        history_sync_retry_after_ms:
          update.status === 'retrying' ? (update.retry_after_ms ?? null) : null,
        history_sync_resource: update.status === 'failed' ? (update.resource ?? null) : null
      }));
      return;
    }
    case 'GUILD_ROLE_CREATE':
    case 'GUILD_ROLE_UPDATE': {
      const role = dispatch.d as Role;
      chatEntities.guilds.update(`${role.guild_id}@${role.guild_domain}`, (guild) => ({
        ...guild,
        roles: [
          ...(guild.roles ?? []).filter((candidate) => entityKey(candidate) !== entityKey(role)),
          role
        ]
      }));
      return;
    }
    case 'GUILD_ROLE_DELETE': {
      const role = dispatch.d as {
        id: string;
        origin_domain: string;
        guild_id: string;
        guild_domain: string;
      };
      chatEntities.guilds.update(`${role.guild_id}@${role.guild_domain}`, (guild) => ({
        ...guild,
        roles: (guild.roles ?? []).filter((candidate) => entityKey(candidate) !== entityKey(role))
      }));
      return;
    }
    case 'GUILD_EMOJI_CREATE': {
      const emoji = dispatch.d as CustomEmoji;
      chatEntities.guilds.update(`${emoji.guild_id}@${emoji.guild_domain}`, (guild) => ({
        ...guild,
        emojis: [
          ...(guild.emojis ?? []).filter((candidate) => entityKey(candidate) !== entityKey(emoji)),
          emoji
        ]
      }));
      return;
    }
    case 'GUILD_EMOJI_DELETE': {
      const emoji = dispatch.d as CustomEmoji;
      chatEntities.guilds.update(`${emoji.guild_id}@${emoji.guild_domain}`, (guild) => ({
        ...guild,
        emojis: (guild.emojis ?? []).filter(
          (candidate) => entityKey(candidate) !== entityKey(emoji)
        )
      }));
      return;
    }
    case 'GUILD_DELETE': {
      const target = dispatch.d as { id: string; origin_domain: string };
      chatEntities.guilds.remove(entityKey(target));
      return;
    }
    case 'GUILD_NAVIGATION_UPDATE':
      guildNavigation.apply(dispatch.d);
      return;
    case 'GUILD_MEMBER_ADD':
    case 'GUILD_MEMBER_UPDATE': {
      const member = dispatch.d as Partial<GuildMemberSummary>;
      // Older replicas emitted only user_id/role_id for role changes. Ignore
      // those incomplete projections instead of corrupting the normalized
      // member store; the next member chunk repairs the stale row.
      if (member.user && member.guild_id && member.guild_domain) {
        chatEntities.members.upsert(member as GuildMemberSummary);
      }
      return;
    }
    case 'GUILD_MEMBER_REMOVE': {
      const member = dispatch.d as GuildMemberSummary;
      chatEntities.members.remove(
        `${member.guild_id}@${member.guild_domain}:${entityKey(member.user)}`
      );
      return;
    }
    case 'GUILD_MEMBERS_CHUNK': {
      const chunk = dispatch.d as { members?: GuildMemberSummary[] };
      chatEntities.members.upsertMany(chunk.members ?? []);
      return;
    }
    case 'READ_STATE_UPDATE':
      chatEntities.readStates.replace(
        applyReadStateDispatch(chatEntities.readStates.values, dispatch.d as ReadStateDispatch)
      );
      return;
    case 'PRESENCE_UPDATE': {
      const presence = dispatch.d as {
        user_id: string;
        user_domain: string;
        status: PresenceStatus;
        preference?: 'online' | 'idle' | 'dnd' | 'invisible';
      };
      chatEntities.setPresence(
        { id: presence.user_id, origin_domain: presence.user_domain },
        presence.status
      );
      if (
        presence.preference &&
        chatEntities.currentUser?.id === presence.user_id &&
        chatEntities.currentUser.origin_domain === presence.user_domain
      ) {
        applyOwnPresencePreference(presence.preference);
      }
      return;
    }
    case 'USER_UPDATE': {
      const user = dispatch.d as UserSummary;
      if (user.id && user.origin_domain) chatEntities.applyUserProfile(user);
      return;
    }
    default:
      return;
  }
}

class AuthenticatedGatewayRuntime {
  readonly client = new GatewayClient();
  status = $state<GatewayStatus>({ state: 'connecting', message: '' });
  #started = false;
  #readStateSync: BroadcastChannel | null = null;
  #reduce = (event: Event) => {
    const dispatch = (event as CustomEvent<Dispatch>).detail;
    applyEntityDispatch(dispatch);
    if (dispatch.t === 'MESSAGE_CREATE') {
      browserNotifications.notifyMessage(dispatch.d as Message);
    }
    if (dispatch.t === 'READ_STATE_UPDATE') this.#readStateSync?.postMessage(dispatch.d);
  };
  #receiveReadState = (event: MessageEvent<unknown>) => {
    const value = event.data as Partial<ReadStateStatus> | null;
    if (value && typeof value.channel_id === 'string' && typeof value.channel_domain === 'string') {
      chatEntities.readStates.upsert(value as ReadStateStatus);
    }
  };
  #updateStatus = (event: Event) => {
    this.status = (event as CustomEvent<GatewayStatus>).detail;
  };

  start(): void {
    if (this.#started) return;
    this.#started = true;
    this.client.addEventListener('dispatch', this.#reduce);
    this.client.addEventListener(GATEWAY_STATUS_EVENT, this.#updateStatus);
    if (typeof BroadcastChannel !== 'undefined') {
      this.#readStateSync = new BroadcastChannel('kaede-read-states');
      this.#readStateSync.addEventListener('message', this.#receiveReadState);
    }
    this.client.connect();
  }

  reportStartupFailure(message: string): void {
    this.status = { state: 'offline', message };
  }

  stop(): void {
    if (!this.#started) return;
    this.#started = false;
    this.client.removeEventListener('dispatch', this.#reduce);
    this.client.removeEventListener(GATEWAY_STATUS_EVENT, this.#updateStatus);
    this.client.close();
    this.#readStateSync?.removeEventListener('message', this.#receiveReadState);
    this.#readStateSync?.close();
    this.#readStateSync = null;
    this.status = { state: 'connecting', message: '' };
    chatEntities.clearSession();
  }
}

export const authenticatedGateway = new AuthenticatedGatewayRuntime();
