import { entityKey } from '$lib/chat/refs';
import type {
  Channel,
  Guild,
  GuildMemberSummary,
  Message,
  PresenceStatus,
  ReadStateStatus,
  Role,
  UserSummary
} from '$lib/chat/types';
import { chatEntities } from '$lib/stores/entities.svelte';
import { GatewayClient, type Dispatch } from './client';

type ReadyPayload = {
  user: UserSummary;
  guilds: Guild[];
  dm_channels: Channel[];
  read_states: ReadStateStatus[];
};

function applyEntityDispatch(dispatch: Dispatch): void {
  switch (dispatch.t) {
    case 'READY': {
      const ready = dispatch.d as ReadyPayload;
      chatEntities.clearSession();
      chatEntities.ingestCurrentUser(ready.user);
      chatEntities.ingestGuilds(ready.guilds);
      chatEntities.channels.upsertMany(ready.dm_channels);
      chatEntities.readStates.upsertMany(ready.read_states);
      return;
    }
    case 'MESSAGE_CREATE':
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
    case 'CHANNEL_ACCESS_GRANTED':
      chatEntities.channels.upsert(dispatch.d as Channel);
      return;
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
    case 'GUILD_DELETE': {
      const target = dispatch.d as { id: string; origin_domain: string };
      chatEntities.guilds.remove(entityKey(target));
      return;
    }
    case 'GUILD_MEMBER_ADD':
    case 'GUILD_MEMBER_UPDATE':
      chatEntities.members.upsert(dispatch.d as GuildMemberSummary);
      return;
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
      chatEntities.readStates.upsert(dispatch.d as ReadStateStatus);
      return;
    case 'PRESENCE_UPDATE': {
      const presence = dispatch.d as {
        user_id: string;
        user_domain: string;
        status: PresenceStatus;
      };
      chatEntities.setPresence(
        { id: presence.user_id, origin_domain: presence.user_domain },
        presence.status
      );
      return;
    }
    case 'USER_UPDATE': {
      const user = dispatch.d as UserSummary;
      if (user.id && user.origin_domain) chatEntities.users.upsert(user);
      if (
        chatEntities.currentUser &&
        user.id === chatEntities.currentUser.id &&
        user.origin_domain === chatEntities.currentUser.origin_domain
      ) {
        chatEntities.ingestCurrentUser({ ...chatEntities.currentUser, ...user });
      }
      return;
    }
    default:
      return;
  }
}

class AuthenticatedGatewayRuntime {
  readonly client = new GatewayClient();
  #started = false;
  #readStateSync: BroadcastChannel | null = null;
  #reduce = (event: Event) => {
    const dispatch = (event as CustomEvent<Dispatch>).detail;
    applyEntityDispatch(dispatch);
    if (dispatch.t === 'READ_STATE_UPDATE') this.#readStateSync?.postMessage(dispatch.d);
  };
  #receiveReadState = (event: MessageEvent<unknown>) => {
    const value = event.data as Partial<ReadStateStatus> | null;
    if (value && typeof value.channel_id === 'string' && typeof value.channel_domain === 'string') {
      chatEntities.readStates.upsert(value as ReadStateStatus);
    }
  };

  start(): void {
    if (this.#started) return;
    this.#started = true;
    this.client.addEventListener('dispatch', this.#reduce);
    if (typeof BroadcastChannel !== 'undefined') {
      this.#readStateSync = new BroadcastChannel('kaede-read-states');
      this.#readStateSync.addEventListener('message', this.#receiveReadState);
    }
    this.client.connect();
  }

  stop(): void {
    if (!this.#started) return;
    this.#started = false;
    this.client.removeEventListener('dispatch', this.#reduce);
    this.client.close();
    this.#readStateSync?.removeEventListener('message', this.#receiveReadState);
    this.#readStateSync?.close();
    this.#readStateSync = null;
    chatEntities.clearSession();
  }
}

export const authenticatedGateway = new AuthenticatedGatewayRuntime();
