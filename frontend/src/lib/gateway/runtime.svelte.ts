import { entityKey } from '$lib/chat/refs';
import { mergeMessageUpdate } from '$lib/chat/reconcile';
import {
  applyContentSafetyUpdate,
  contentSafety,
  invalidateContentSafety,
  protectMessage,
  syncContentSafety
} from '$lib/chat/content-safety.svelte';
import { api } from '$lib/api/client';
import {
  applyBulkMessageDelete,
  tombstoneMessage,
  type MessageBulkDeleteUpdate
} from '$lib/chat/message-deletions';
import { reconcileChannelPinsUpdate, type ChannelPinsUpdate } from '$lib/chat/pins';
import { applyPollVoteDispatch, type PollVoteDispatchName } from '$lib/chat/poll-state';
import {
  applyReactionClear,
  applyReactionUpdate,
  reactionClearEmoji,
  reactionUpdateFromDispatch,
  type ReactionClearUpdate
} from '$lib/chat/reaction-state';
import { isThreadChannel, threadMembersUpdateRemovesUser } from '$lib/chat/threads';
import type {
  Channel,
  CustomEmoji,
  Guild,
  GuildSticker,
  GuildMemberSummary,
  Message,
  PresenceStatus,
  ReadStateStatus,
  Relationship,
  Role,
  ThreadMember,
  UserSummary
} from '$lib/chat/types';
import { browserNotifications } from '$lib/notifications/browser.svelte';
import {
  applyIncomingMessage,
  applyReadStateDispatch,
  type ReadStateDispatch
} from '$lib/notifications/read-state';
import { chatEntities } from '$lib/stores/entities.svelte';
import {
  isPresenceStatePayload,
  presenceControl,
  type PresenceStatePayload
} from '$lib/stores/presence.svelte';
import { guildNavigation } from '$lib/stores/guild-navigation.svelte';
import {
  interactionResponses,
  type InteractionResponseEventName
} from '$lib/chat/interaction-responses.svelte';
import { GATEWAY_STATUS_EVENT, GatewayClient, type Dispatch, type GatewayStatus } from './client';
import { SvelteMap } from 'svelte/reactivity';

type ReadyPayload = {
  user: UserSummary;
  guilds: Guild[];
  dm_channels: Channel[];
  relationships?: Relationship[];
  presences?: Array<{
    user_id: string;
    user_domain: string;
    status: PresenceStatus;
  }>;
  read_states: ReadStateStatus[];
  presence_preference?: 'online' | 'idle' | 'dnd' | 'invisible';
  presence_state?: PresenceStatePayload;
};

const guildProjectionRefreshGenerations = new SvelteMap<string, number>();
let guildProjectionSession = 0;
let relationshipSnapshotUpdates: Map<string, Relationship | null> | null = null;

function refreshRelationships(): Promise<void> {
  const session = guildProjectionSession;
  const updates = new SvelteMap<string, Relationship | null>();
  relationshipSnapshotUpdates = updates;
  return api<Relationship[]>('/users/@me/relationships')
    .then((relationships) => {
      if (session !== guildProjectionSession || relationshipSnapshotUpdates !== updates) return;
      chatEntities.relationships.replace(relationships);
      for (const [key, relationship] of updates) {
        if (relationship) chatEntities.relationships.upsert(relationship);
        else chatEntities.relationships.remove(key);
      }
    })
    .finally(() => {
      if (relationshipSnapshotUpdates === updates) relationshipSnapshotUpdates = null;
    });
}

function refreshGuildPermissionProjection(guildId: string, guildDomain: string): void {
  if (!guildId || !guildDomain) return;
  const guildRef = `${guildId}@${guildDomain}`;
  const generation = (guildProjectionRefreshGenerations.get(guildRef) ?? 0) + 1;
  const session = guildProjectionSession;
  guildProjectionRefreshGenerations.set(guildRef, generation);
  chatEntities.invalidateGuildPermissionProjection(guildRef);
  void api<Guild>(`/guilds/${encodeURIComponent(guildRef)}`)
    .then((guild) => {
      if (
        guildProjectionSession === session &&
        guildProjectionRefreshGenerations.get(guildRef) === generation
      ) {
        chatEntities.ingestGuilds([guild]);
      }
    })
    .catch(() => {
      // Keep the projection fail-closed until the route or a later dispatch refreshes it.
    });
}

function cancelGuildProjectionRefresh(guildId?: string, guildDomain?: string): void {
  if (!guildId || !guildDomain) return;
  const guildRef = `${guildId}@${guildDomain}`;
  guildProjectionRefreshGenerations.set(
    guildRef,
    (guildProjectionRefreshGenerations.get(guildRef) ?? 0) + 1
  );
}

function applyOwnPresencePreference(
  preference: 'online' | 'idle' | 'dnd' | 'invisible' | undefined,
  state?: PresenceStatePayload | null
): void {
  const effective = state?.effective_status ?? preference;
  if (!effective) return;
  authenticatedGateway.client.rememberPresence(effective);
  if (chatEntities.currentUser) {
    chatEntities.setPresence(
      chatEntities.currentUser,
      effective === 'invisible' ? 'offline' : effective
    );
  }
  if (state && isPresenceStatePayload(state)) {
    presenceControl.apply(state);
  } else {
    presenceControl.applyFallbackPreference(effective);
  }
}

function storeMessageDispatch(message: Message): void {
  const current = chatEntities.messages.get(entityKey(message));
  chatEntities.messages.upsert(mergeMessageUpdate(current ?? message, message));
}

function applyEntityDispatch(dispatch: Dispatch): void {
  const eventName = dispatch.t as string;
  if (eventName === 'DM_CONSENT_UPDATE') {
    const update = dispatch.d as Channel;
    chatEntities.channels.update(entityKey(update), (room) => ({
      ...room,
      continuation_requester: update.continuation_requester
    }));
    return;
  }
  if (eventName === 'CONTENT_SAFETY_UPDATE') {
    try {
      applyContentSafetyUpdate(dispatch.d);
      chatEntities.messages.replace(chatEntities.messages.values.map(protectMessage));
      authenticatedGateway.resyncAfterUpdate();
    } catch {
      contentSafety.ready = false;
      contentSafety.error = 'Could not apply a content safety update. Reconnect and try again.';
    }
    return;
  }
  if (
    eventName === 'INTERACTION_RESPONSE_CREATE' ||
    eventName === 'INTERACTION_RESPONSE_UPDATE' ||
    eventName === 'INTERACTION_RESPONSE_DELETE'
  ) {
    interactionResponses.apply(eventName as InteractionResponseEventName, dispatch.d);
    return;
  }
  switch (dispatch.t) {
    case 'READY': {
      const ready = dispatch.d as ReadyPayload;
      guildProjectionSession += 1;
      guildProjectionRefreshGenerations.clear();
      // A full gateway re-identify may happen after a tab has slept or a resume
      // cursor expires. The route reload restores messages over HTTP, while the
      // roster is a separate gateway request. Keep the same account's last
      // known roster until that request arrives instead of flashing—or
      // indefinitely leaving—an empty member list. Account changes still clear
      // all member data before the new READY is applied. Likewise, preserve
      // history that HTTP may have loaded before the initial READY arrives.
      if (
        chatEntities.currentUser &&
        entityKey(chatEntities.currentUser) !== entityKey(ready.user)
      ) {
        authenticatedGateway.joinedDmCall = null;
        presenceControl.reset();
      }
      chatEntities.beginGatewaySession(ready.user);
      authenticatedGateway.applyRelationships(ready.relationships);
      void authenticatedGateway.retryContentSafety();
      chatEntities.ingestGuilds(ready.guilds);
      chatEntities.ingestDirectMessages(ready.dm_channels);
      chatEntities.ingestPresences(ready.presences ?? []);
      chatEntities.readStates.upsertMany(ready.read_states);
      applyOwnPresencePreference(ready.presence_preference, ready.presence_state);
      return;
    }
    case 'RESUMED': {
      authenticatedGateway.applyRelationships((dispatch.d as ReadyPayload).relationships);
      void authenticatedGateway.retryContentSafety();
      const resumed = dispatch.d as {
        presence_preference?: 'online' | 'idle' | 'dnd' | 'invisible';
        presence_state?: PresenceStatePayload;
        presences?: Array<{
          user_id: string;
          user_domain: string;
          status: PresenceStatus;
        }>;
      };
      chatEntities.ingestPresences(resumed.presences ?? []);
      applyOwnPresencePreference(resumed.presence_preference, resumed.presence_state);
      return;
    }
    case 'MESSAGE_CREATE': {
      const message = dispatch.d as Message;
      // The channel publishes encrypted messages after decryption. Inserting the
      // raw echo here briefly displays a failure beside the readable local draft.
      if (!message.e2ee) storeMessageDispatch(message);
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
    case 'MESSAGE_REACTION_ADD':
    case 'MESSAGE_REACTION_REMOVE': {
      const update = reactionUpdateFromDispatch(dispatch.t, dispatch.d);
      if (update) {
        chatEntities.messages.update(entityKey(update), (message) =>
          applyReactionUpdate(message, update, chatEntities.currentUser)
        );
      }
      return;
    }
    case 'MESSAGE_REACTION_REMOVE_ALL':
    case 'MESSAGE_REACTION_REMOVE_EMOJI': {
      const update = dispatch.d as ReactionClearUpdate;
      if (typeof update.message_id === 'string' && typeof update.message_domain === 'string') {
        chatEntities.messages.update(`${update.message_id}@${update.message_domain}`, (message) =>
          applyReactionClear(message, reactionClearEmoji(update))
        );
      }
      return;
    }
    case 'MESSAGE_UPDATE': {
      const message = dispatch.d as Message;
      if ('reaction' in message) {
        const update = reactionUpdateFromDispatch('MESSAGE_UPDATE', dispatch.d);
        if (update) {
          chatEntities.messages.update(entityKey(update), (current) =>
            applyReactionUpdate(current, update, chatEntities.currentUser)
          );
        }
      } else {
        storeMessageDispatch(message);
      }
      return;
    }
    case 'MESSAGE_POLL_VOTE_ADD':
    case 'MESSAGE_POLL_VOTE_REMOVE': {
      const update = dispatch.d as { message_id?: string; message_domain?: string };
      if (typeof update.message_id === 'string' && typeof update.message_domain === 'string') {
        chatEntities.messages.update(`${update.message_id}@${update.message_domain}`, (message) =>
          applyPollVoteDispatch(
            message,
            dispatch.t as PollVoteDispatchName,
            dispatch.d,
            chatEntities.currentUser
          )
        );
      }
      return;
    }
    case 'CHANNEL_PINS_UPDATE': {
      const update = dispatch.d as ChannelPinsUpdate;
      chatEntities.messages.upsertMany(
        reconcileChannelPinsUpdate(chatEntities.messages.values, update)
      );
      return;
    }
    case 'MESSAGE_DELETE': {
      const message = dispatch.d as Partial<Message> & { id: string; origin_domain: string };
      chatEntities.messages.update(entityKey(message), (current) =>
        tombstoneMessage({ ...current, ...message }, message.deleted_at ?? new Date().toISOString())
      );
      return;
    }
    case 'MESSAGE_DELETE_BULK': {
      const update = dispatch.d as MessageBulkDeleteUpdate;
      chatEntities.messages.replace(applyBulkMessageDelete(chatEntities.messages.values, update));
      return;
    }
    case 'CHANNEL_CREATE':
    case 'CHANNEL_UPDATE':
    case 'CHANNEL_ACCESS_GRANTED': {
      const channel = dispatch.d as Channel;
      if (channel.guild_id === null && channel.dm_previous_id) {
        const previous = entityKey({
          id: channel.dm_previous_id,
          origin_domain: channel.origin_domain
        });
        chatEntities.channels.remove(previous);
        chatEntities.readStates.remove(previous);
      }
      chatEntities.upsertGuildChannel(channel);
      return;
    }
    case 'CHANNEL_PERMISSION_UPDATE': {
      const update = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        permissions: string;
      };
      chatEntities.updateChannelPermissions(
        { id: update.channel_id, origin_domain: update.channel_domain },
        update.permissions
      );
      return;
    }
    case 'CHANNEL_ACCESS_REVOKED': {
      const revoked = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        guild_id?: string;
        guild_domain?: string;
      };
      const current = chatEntities.channels.get(`${revoked.channel_id}@${revoked.channel_domain}`);
      cancelGuildProjectionRefresh(
        revoked.guild_id ?? current?.guild_id ?? undefined,
        revoked.guild_domain ?? current?.guild_domain ?? undefined
      );
      chatEntities.removeChannel({
        id: revoked.channel_id,
        origin_domain: revoked.channel_domain
      });
      return;
    }
    case 'CHANNEL_DELETE': {
      const channel = dispatch.d as {
        id: string;
        origin_domain: string;
        guild_id?: string;
        guild_domain?: string;
      };
      if ('dm_successor_id' in channel && typeof channel.dm_successor_id === 'string') {
        // Retirement keeps authorized history in the timeline. A membership
        // revocation still takes the normal deletion path below.
        chatEntities.channels.remove(entityKey(channel));
        chatEntities.readStates.remove(entityKey(channel));
        return;
      }
      const current = chatEntities.channels.get(entityKey(channel));
      cancelGuildProjectionRefresh(
        channel.guild_id ?? current?.guild_id ?? undefined,
        channel.guild_domain ?? current?.guild_domain ?? undefined
      );
      chatEntities.removeChannel(channel);
      return;
    }
    case 'THREAD_CREATE':
    case 'THREAD_UPDATE': {
      const data = dispatch.d as {
        channel?: Channel;
        thread?: Channel;
        starter_message?: Message | null;
      } & Partial<Channel>;
      const thread = (data.channel ?? data.thread ?? data) as Channel;
      if (thread.id && thread.origin_domain && isThreadChannel(thread)) {
        chatEntities.upsertGuildChannel({
          ...thread,
          starter_message: data.starter_message ?? thread.starter_message
        });
      }
      return;
    }
    case 'THREAD_DELETE': {
      const data = dispatch.d as {
        channel?: Channel;
        thread?: Channel;
        id?: string;
        origin_domain?: string;
        thread_domain?: string;
      };
      const thread = data.channel ?? data.thread ?? data;
      if (thread.id && (thread.origin_domain || data.thread_domain)) {
        chatEntities.removeChannel({
          id: thread.id,
          origin_domain: thread.origin_domain ?? data.thread_domain!
        });
      }
      return;
    }
    case 'THREAD_LIST_SYNC': {
      const update = dispatch.d as { threads?: Channel[]; members?: ThreadMember[] };
      const members = update.members ?? [];
      for (const thread of update.threads ?? []) {
        const member = members.find(
          (item) =>
            item.id === thread.id &&
            (!item.thread_domain || item.thread_domain === thread.origin_domain)
        );
        chatEntities.upsertGuildChannel({ ...thread, member: member ?? null });
      }
      return;
    }
    case 'THREAD_MEMBER_UPDATE': {
      const member = dispatch.d as ThreadMember & { removed?: boolean };
      if (!member.id) return;
      const threadDomain = member.thread_domain;
      const thread = threadDomain
        ? chatEntities.channels.get(`${member.id}@${threadDomain}`)
        : chatEntities.channels.values.find(
            (item) => item.id === member.id && isThreadChannel(item)
          );
      const currentUser = chatEntities.currentUser;
      if (
        thread &&
        currentUser &&
        member.user_id === currentUser.id &&
        member.user_domain === currentUser.origin_domain
      ) {
        chatEntities.upsertGuildChannel({
          ...thread,
          member: member.removed ? null : member
        });
      }
      return;
    }
    case 'THREAD_MEMBERS_UPDATE': {
      const update = dispatch.d as {
        id: string;
        thread_domain?: string;
        guild_domain?: string;
        member_count?: number;
        removed_member_ids?: string[];
        removed_member_refs?: Array<{ id: string; origin_domain: string }>;
      };
      const threadDomain = update.thread_domain ?? update.guild_domain;
      const thread = threadDomain
        ? chatEntities.channels.get(`${update.id}@${threadDomain}`)
        : undefined;
      if (!thread) return;
      const removesCurrentUser = threadMembersUpdateRemovesUser(update, chatEntities.currentUser);
      if (removesCurrentUser && thread.type === 12) {
        chatEntities.removeChannel(thread);
      } else {
        chatEntities.upsertGuildChannel({
          ...thread,
          member_count: update.member_count ?? thread.member_count,
          member: removesCurrentUser ? null : thread.member
        });
      }
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
      refreshGuildPermissionProjection(role.guild_id, role.guild_domain);
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
      refreshGuildPermissionProjection(role.guild_id, role.guild_domain);
      return;
    }
    case 'GUILD_EMOJI_CREATE':
    case 'GUILD_EMOJI_UPDATE': {
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
    case 'GUILD_STICKER_CREATE':
    case 'GUILD_STICKER_UPDATE': {
      const sticker = dispatch.d as GuildSticker;
      chatEntities.guilds.update(`${sticker.guild_id}@${sticker.guild_domain}`, (guild) => ({
        ...guild,
        stickers: [
          ...(guild.stickers ?? []).filter(
            (candidate) => entityKey(candidate) !== entityKey(sticker)
          ),
          sticker
        ]
      }));
      return;
    }
    case 'GUILD_STICKER_DELETE': {
      const sticker = dispatch.d as GuildSticker;
      chatEntities.guilds.update(`${sticker.guild_id}@${sticker.guild_domain}`, (guild) => ({
        ...guild,
        stickers: (guild.stickers ?? []).filter(
          (candidate) => entityKey(candidate) !== entityKey(sticker)
        )
      }));
      return;
    }
    case 'GUILD_DELETE': {
      const target = dispatch.d as { id: string; origin_domain: string };
      cancelGuildProjectionRefresh(target.id, target.origin_domain);
      chatEntities.removeGuild(target);
      return;
    }
    case 'GUILD_NAVIGATION_UPDATE':
      guildNavigation.apply(dispatch.d);
      return;
    case 'GUILD_MEMBER_ADD':
    case 'GUILD_MEMBER_UPDATE': {
      const member = dispatch.d as Partial<GuildMemberSummary> & {
        user_id?: string;
        user_domain?: string;
      };
      // Older replicas emitted only user_id/role_id for role changes. Ignore
      // those incomplete projections instead of corrupting the normalized
      // member store; the next member chunk repairs the stale row.
      if (member.user && member.guild_id && member.guild_domain) {
        chatEntities.members.upsert(member as GuildMemberSummary);
      }
      const updatedUser =
        member.user ??
        (member.user_id && member.user_domain
          ? { id: member.user_id, origin_domain: member.user_domain }
          : null);
      if (
        member.guild_id &&
        member.guild_domain &&
        updatedUser &&
        chatEntities.currentUser &&
        entityKey(updatedUser) === entityKey(chatEntities.currentUser)
      ) {
        refreshGuildPermissionProjection(member.guild_id, member.guild_domain);
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
        presence_state?: PresenceStatePayload;
      };
      chatEntities.setPresence(
        { id: presence.user_id, origin_domain: presence.user_domain },
        presence.status
      );
      if (
        (presence.preference || presence.presence_state) &&
        chatEntities.currentUser?.id === presence.user_id &&
        chatEntities.currentUser.origin_domain === presence.user_domain
      ) {
        applyOwnPresencePreference(presence.preference, presence.presence_state);
      }
      return;
    }
    case 'USER_UPDATE': {
      const update = dispatch.d as {
        relationship?: { type: Relationship['type'] | 'none'; user: UserSummary };
      };
      if (update.relationship) {
        const { type, user } = update.relationship;
        const key = entityKey(user);
        const relationship =
          type === 'none'
            ? null
            : {
                type,
                user,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString()
              };
        relationshipSnapshotUpdates?.set(key, relationship);
        if (relationship) chatEntities.relationships.upsert(relationship);
        else chatEntities.relationships.remove(key);
      }
      const user = dispatch.d as UserSummary;
      if (user.id && user.origin_domain) chatEntities.applyUserProfile(user);
      return;
    }
    case 'VOICE_CHANNEL_EFFECT_SEND': {
      // Voice docks are route-scoped and own their audio lifecycle. Forward
      // the authorized, short-lived playback capability without persisting it
      // in the normalized guild store.
      globalThis.window?.dispatchEvent(
        new CustomEvent('kaede:voice-soundboard', { detail: dispatch.d })
      );
      return;
    }
    default:
      return;
  }
}

class AuthenticatedGatewayRuntime {
  readonly client = new GatewayClient();
  // Joining is an explicit choice in this tab and login, independent of server call membership.
  joinedDmCall: { owner: string; id: string; authority: string } | null = null;
  status = $state<GatewayStatus>({ state: 'connecting', message: '' });
  #started = false;
  #safetyBootstrap: Promise<void> | null = null;
  #relationshipsReady = false;
  #readStateSync: BroadcastChannel | null = null;
  #reduce = (event: Event) => {
    const dispatch = (event as CustomEvent<Dispatch>).detail;
    if (dispatch.t === 'READY') {
      this.#safetyBootstrap = null;
      this.#relationshipsReady = false;
      invalidateContentSafety();
    }
    applyEntityDispatch(dispatch);
    if (dispatch.t === 'MESSAGE_CREATE') {
      if (!chatEntities.isMessageBlocked(dispatch.d as Message)) {
        browserNotifications.notifyMessage(dispatch.d as Message);
      }
    }
    if (dispatch.t === 'READ_STATE_UPDATE' && chatEntities.currentUser) {
      this.#readStateSync?.postMessage({
        account: entityKey(chatEntities.currentUser),
        update: dispatch.d
      });
    }
  };
  #receiveReadState = (event: MessageEvent<unknown>) => {
    const message = event.data as { account?: string; update?: ReadStateDispatch } | null;
    const user = chatEntities.currentUser;
    const update = message?.update;
    if (
      !user ||
      message?.account !== entityKey(user) ||
      !update ||
      typeof update.channel_id !== 'string' ||
      typeof update.channel_domain !== 'string' ||
      typeof update.mention_count !== 'number'
    )
      return;
    chatEntities.readStates.replace(applyReadStateDispatch(chatEntities.readStates.values, update));
  };
  #updateStatus = (event: Event) => {
    this.status = (event as CustomEvent<GatewayStatus>).detail;
    if (['connecting', 'reconnecting', 'offline'].includes(this.status.state)) {
      invalidateContentSafety();
      if (this.#safetyBootstrap) {
        this.#safetyBootstrap = null;
        relationshipSnapshotUpdates = null;
      }
    }
  };

  applyRelationships(snapshot?: Relationship[]): void {
    if (!Array.isArray(snapshot)) return;
    relationshipSnapshotUpdates = null;
    chatEntities.relationships.replace(snapshot);
    this.#relationshipsReady = true;
  }

  resyncAfterUpdate(): void {
    const pending = this.#safetyBootstrap;
    if (this.#relationshipsReady && chatEntities.currentUser) {
      void this.retryContentSafety();
    } else if (pending) {
      const session = guildProjectionSession;
      void pending.then(() => {
        if (session === guildProjectionSession && contentSafety.ready)
          void this.retryContentSafety();
      });
    } else if (chatEntities.currentUser) void this.retryContentSafety();
  }

  retryContentSafety(): Promise<void> {
    if (!this.#started) return Promise.resolve();
    if (this.#relationshipsReady && chatEntities.currentUser)
      return syncContentSafety(chatEntities.currentUser).catch(() => undefined);
    if (this.#safetyBootstrap) return this.#safetyBootstrap;

    // A reloaded tab can resume its socket without receiving READY's account snapshot.
    const session = guildProjectionSession;
    contentSafety.ready = false;
    contentSafety.error = '';
    const identity = chatEntities.currentUser ?? api<UserSummary>('/users/@me');
    const current = () => this.#safetyBootstrap === pending && session === guildProjectionSession;
    const pending: Promise<void> = Promise.all([
      identity,
      this.#relationshipsReady ? undefined : refreshRelationships()
    ])
      .then(async ([user]) => {
        if (!current()) return;
        if (!chatEntities.currentUser) {
          chatEntities.ingestCurrentUser({ ...user, ...chatEntities.users.get(entityKey(user)) });
        }
        this.#relationshipsReady = true;
        await syncContentSafety(user);
      })
      .catch(() => {
        if (current() && !contentSafety.error)
          contentSafety.error = 'Could not check message availability. Try again.';
      })
      .finally(() => {
        if (this.#safetyBootstrap === pending) this.#safetyBootstrap = null;
      });
    this.#safetyBootstrap = pending;
    return pending;
  }

  start(): void {
    if (this.#started) return;
    this.#started = true;
    invalidateContentSafety();
    this.client.addEventListener('dispatch', this.#reduce);
    this.client.addEventListener(GATEWAY_STATUS_EVENT, this.#updateStatus);
    if (typeof BroadcastChannel !== 'undefined') {
      this.#readStateSync = new BroadcastChannel('kaede-read-states-v2');
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
    this.#safetyBootstrap = null;
    this.#relationshipsReady = false;
    invalidateContentSafety();
    this.client.removeEventListener('dispatch', this.#reduce);
    this.client.removeEventListener(GATEWAY_STATUS_EVENT, this.#updateStatus);
    this.client.close();
    this.#readStateSync?.removeEventListener('message', this.#receiveReadState);
    this.#readStateSync?.close();
    this.#readStateSync = null;
    this.status = { state: 'connecting', message: '' };
    guildProjectionSession += 1;
    guildProjectionRefreshGenerations.clear();
    this.joinedDmCall = null;
    presenceControl.reset();
    chatEntities.clearSession();
    interactionResponses.reset();
  }
}

export const authenticatedGateway = new AuthenticatedGatewayRuntime();
