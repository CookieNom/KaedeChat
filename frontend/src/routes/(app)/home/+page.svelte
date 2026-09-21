<script lang="ts">
  import AccountPanel from '$lib/components/AccountPanel.svelte';
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { page } from '$app/state';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { apiErrorMessage } from '$lib/api/errors';
  import { firstNavigableChannel } from '$lib/chat/channels';
  import { invitedChannel, loadInvitePreview } from '$lib/chat/invite-preview';
  import { filterDmFriends, friendsWithoutVisibleDm } from '$lib/chat/dm-picker';
  import { dmTitle, isGroupDm, promoteDirectMessage } from '$lib/chat/direct-messages';
  import { normalizeInviteReference } from '$lib/chat/invites';
  import { entityKey, entityRef, sameEntity } from '$lib/chat/refs';
  import type {
    Channel,
    Guild,
    Message,
    MessageSearchResult,
    ReadStateStatus,
    Relationship,
    UserSummary
  } from '$lib/chat/types';
  import {
    applyUserProfileToHomeProjections,
    userDisplayName,
    userPublicHandle
  } from '$lib/chat/users';
  import { GATEWAY_SESSION_RESET_EVENT, type Dispatch } from '$lib/gateway/client';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { DispatchReplayBuffer, type DispatchBatch } from '$lib/gateway/recovery';
  import { lastVisitedChannel } from '$lib/navigation/history';
  import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
  import { assetUrl } from '$lib/media/assets';
  import { directMessageUnreadCount, guildMentionCount } from '$lib/notifications/counts';
  import { applyIncomingMessage, applyReadStateDispatch } from '$lib/notifications/read-state';
  import UserProfileCard from '$lib/components/UserProfileCard.svelte';
  import GuildRail from '$lib/components/GuildRail.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import MessageSearch from '$lib/components/MessageSearch.svelte';
  import NewMessageDialog from '$lib/components/NewMessageDialog.svelte';
  import { onMount, tick } from 'svelte';

  let guilds = $state<Guild[]>([]);
  let directMessages = $state<Channel[]>([]);
  let readStates = $state<ReadStateStatus[]>([]);
  let relationships = $state<Relationship[]>([]);
  let currentUser = $state<UserSummary | null>(null);
  let name = $state('');
  let handle = $state('');
  let friendHandle = $state('');
  let invite = $state('');
  let error = $state('');
  let notice = $state('');
  let busy = $state(false);
  let relationshipBusy = $state(false);
  let loading = $state(true);
  let navigationOpen = $state(false);
  let navigationDrawer: HTMLElement | null = null;
  let navigationToggle: HTMLButtonElement | null = null;
  let navigationClose: HTMLButtonElement | null = null;
  let messageDialog = $state<HTMLDialogElement | null>(null);
  let messageSearchOpen = $state(false);
  let newMessageOpen = $state(false);
  let messageDialogError = $state('');
  let messageMode = $state<'direct' | 'group'>('direct');
  let groupName = $state('');
  let groupMemberKeys = $state<string[]>([]);
  let profilePopover = $state<{ user: UserSummary; x: number; y: number } | null>(null);
  let loadGeneration = 0;
  let snapshotGeneration = 0;
  let guildRevision = 0;
  let relationshipRevision = 0;
  let lastVisited = $state<string | null>(null);
  const dispatches = new DispatchReplayBuffer<Dispatch>();
  const friendsView = $derived(page.url.pathname.replace(/\/+$/, '').endsWith('/home/friends'));
  const friends = $derived(relationships.filter((item) => item.type === 'friend'));
  const incomingRequests = $derived(relationships.filter((item) => item.type === 'pending_in'));
  const outgoingRequests = $derived(relationships.filter((item) => item.type === 'pending_out'));
  const blockedUsers = $derived(relationships.filter((item) => item.type === 'blocked'));
  const newDmFriends = $derived(friendsWithoutVisibleDm(relationships, directMessages));
  const filteredNewDmFriends = $derived(filterDmFriends(newDmFriends, handle));
  const groupFriends = $derived(friends.map((item) => item.user));
  const filteredGroupFriends = $derived(filterDmFriends(groupFriends, handle));
  const homeUnreadCount = $derived(directMessageUnreadCount(readStates));

  function openNavigation() {
    navigationOpen = true;
    void tick().then(() => navigationClose?.focus());
  }

  function closeNavigation(restoreFocus = true) {
    if (!navigationOpen) return;
    navigationOpen = false;
    if (restoreFocus) void tick().then(() => navigationToggle?.focus());
  }

  function navigationKeydown(event: KeyboardEvent) {
    if (!navigationOpen || !navigationDrawer) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeNavigation();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      navigationDrawer.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((element) => !element.hasAttribute('hidden'));
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1)!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openCommandSwitcher() {
    window.dispatchEvent(new Event('kaede:open-command-switcher'));
  }

  function jumpToSearchResult(result: MessageSearchResult) {
    window.location.assign(
      `${directMessagePath(result.channel)}?${new URLSearchParams({ around: entityRef(result.message) })}`
    );
  }

  function guildLandingPath(guild: Guild): string {
    const channel = firstNavigableChannel(guild.channels);
    return channel ? guildChannelPath(guild, channel) : resolve('/home');
  }

  function applyDispatch(dispatch: Dispatch) {
    if (dispatch.t === 'READ_STATE_UPDATE') {
      const update = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        last_message_id: string | null;
        last_message_domain: string | null;
        mention_count: number;
      };
      readStates = applyReadStateDispatch(readStates, update);
    } else if (dispatch.t === 'MESSAGE_CREATE') {
      const message = dispatch.d as Message;
      const directMessage = directMessages.find(
        (item) => item.id === message.channel_id && item.origin_domain === message.channel_domain
      );
      const channel =
        directMessage ??
        guilds
          .flatMap((guild) => guild.channels ?? [])
          .find(
            (item) =>
              item.id === message.channel_id && item.origin_domain === message.channel_domain
          ) ??
        null;
      readStates = applyIncomingMessage(readStates, message, currentUser, channel);
      if (directMessage) directMessages = promoteDirectMessage(directMessages, directMessage);
    } else if (dispatch.t === 'CHANNEL_CREATE') {
      const channel = dispatch.d as Channel;
      if (channel.type === 1 && !directMessages.some((item) => sameEntity(item, channel))) {
        directMessages = promoteDirectMessage(directMessages, channel);
        notice = $t('ui_your_direct_message_request_is_ready_6e7d0a8c');
      }
    } else if (dispatch.t === 'CHANNEL_UPDATE') {
      const channel = dispatch.d as Channel;
      if (channel.type === 1) {
        directMessages = promoteDirectMessage(directMessages, channel);
      }
    } else if (dispatch.t === 'CHANNEL_DELETE') {
      const channel = dispatch.d as Pick<Channel, 'id' | 'origin_domain'>;
      directMessages = directMessages.filter((item) => !sameEntity(item, channel));
    } else if (dispatch.t === 'DM_OPEN_REJECTED') {
      const rejected = dispatch.d as { code?: string };
      notice = '';
      error = apiErrorMessage(rejected.code ?? 'CANNOT_DM_USER', 403, {});
    } else if (dispatch.t === 'USER_UPDATE') {
      const update = dispatch.d as {
        relationship?: {
          type: Relationship['type'] | 'none';
          user: UserSummary;
          error_code?: string;
        };
      } & Partial<UserSummary>;
      if (update.relationship) {
        const key = entityKey(update.relationship.user);
        if (update.relationship.type === 'none') {
          relationships = relationships.filter((item) => entityKey(item.user) !== key);
        } else {
          const next: Relationship = {
            type: update.relationship.type,
            user: update.relationship.user,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString()
          };
          const index = relationships.findIndex((item) => entityKey(item.user) === key);
          relationships =
            index === -1
              ? [next, ...relationships]
              : relationships.map((item, itemIndex) => (itemIndex === index ? next : item));
        }
        relationshipRevision += 1;
        if (update.relationship.error_code) {
          notice = '';
          error = apiErrorMessage(update.relationship.error_code, 507, {});
        }
      }
      const profileUser =
        update.relationship?.user ??
        (update.id && update.origin_domain ? (update as UserSummary) : null);
      if (profileUser) {
        const projected = applyUserProfileToHomeProjections(
          directMessages,
          relationships,
          profilePopover?.user ?? null,
          profileUser
        );
        directMessages = projected.directMessages;
        relationships = projected.relationships;
        if (profilePopover && projected.selectedUser) {
          profilePopover = { ...profilePopover, user: projected.selectedUser };
        }
        if (currentUser && entityKey(currentUser) === entityKey(profileUser)) {
          currentUser = { ...currentUser, ...profileUser };
        }
      }
    }
  }

  function unreadFor(channel: Channel): ReadStateStatus | undefined {
    return readStates.find(
      (state) => state.channel_id === channel.id && state.channel_domain === channel.origin_domain
    );
  }

  function guildUnread(guild: Guild): number {
    return guildMentionCount(readStates, guild);
  }

  onMount(() => {
    lastVisited = lastVisitedChannel(localStorage);
    const gateway = authenticatedGateway.client;
    const desktopViewport = window.matchMedia('(min-width: 741px)');
    const viewportChanged = () => {
      if (desktopViewport.matches) closeNavigation(false);
    };
    const routeGeneration = ++loadGeneration;
    const receive = (event: Event) => {
      const dispatch = (event as CustomEvent<Dispatch>).detail;
      if (!dispatches.push(dispatch)) applyDispatch(dispatch);
    };
    const sessionReset = () => refreshOverview(routeGeneration, true);
    gateway.addEventListener('dispatch', receive);
    gateway.addEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
    desktopViewport.addEventListener('change', viewportChanged);
    viewportChanged();
    refreshOverview(routeGeneration, false);
    return () => {
      loadGeneration += 1;
      snapshotGeneration += 1;
      dispatches.clear();
      gateway.removeEventListener('dispatch', receive);
      gateway.removeEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
      desktopViewport.removeEventListener('change', viewportChanged);
    };
  });

  function refreshOverview(routeGeneration: number, recovering: boolean) {
    const snapshot = ++snapshotGeneration;
    const batch = dispatches.begin();
    const startingGuildRevision = guildRevision;
    const startingRelationshipRevision = relationshipRevision;
    void load(
      routeGeneration,
      snapshot,
      batch,
      recovering,
      startingGuildRevision,
      startingRelationshipRevision
    );
  }

  function retryOverview() {
    error = '';
    loading = true;
    refreshOverview(loadGeneration, false);
  }

  function replay(batch: DispatchBatch<Dispatch>): boolean {
    const buffered = dispatches.finish(batch);
    if (buffered === null) return false;
    for (const dispatch of buffered) applyDispatch(dispatch);
    return true;
  }

  async function load(
    routeGeneration: number,
    snapshot: number,
    batch: DispatchBatch<Dispatch>,
    recovering: boolean,
    startingGuildRevision: number,
    startingRelationshipRevision: number
  ) {
    try {
      const [loadedGuilds, loadedDms, loadedReadStates, loadedRelationships, loadedUser] =
        await Promise.all([
          api<Guild[]>('/users/@me/guilds'),
          api<Channel[]>('/users/@me/channels'),
          api<ReadStateStatus[]>('/users/@me/read-states'),
          api<Relationship[]>('/users/@me/relationships'),
          api<UserSummary>('/users/@me')
        ]);
      if (routeGeneration !== loadGeneration || snapshot !== snapshotGeneration) return;
      if (guildRevision === startingGuildRevision) guilds = loadedGuilds;
      directMessages = loadedDms;
      readStates = loadedReadStates;
      if (relationshipRevision === startingRelationshipRevision) {
        relationships = loadedRelationships;
      }
      currentUser = loadedUser;
      replay(batch);
      error = '';
      loading = false;
    } catch (caught) {
      if (routeGeneration !== loadGeneration || snapshot !== snapshotGeneration) return;
      replay(batch);
      if (caught instanceof ApiError && caught.status === 401) {
        window.location.replace(resolve('/login'));
      } else if (!recovering || !error) {
        error = userErrorMessage(
          caught,
          $t('ui_could_not_load_your_conversations_try_again_72f219e2')
        );
      }
      loading = false;
    }
  }

  async function updateRelationship(
    user: Relationship['user'],
    action: 'accept' | 'remove' | 'unblock'
  ) {
    if (relationshipBusy) return;
    const generation = loadGeneration;
    relationshipBusy = true;
    error = '';
    notice = '';
    try {
      const userPath = encodeURIComponent(entityRef(user));
      await api(`/users/@me/relationships/${userPath}${action === 'unblock' ? '/block' : ''}`, {
        method: action === 'accept' ? 'PUT' : 'DELETE'
      });
      if (generation !== loadGeneration) return;
      const loadedRelationships = await api<Relationship[]>('/users/@me/relationships');
      if (generation !== loadGeneration) return;
      relationships = loadedRelationships;
      relationshipRevision += 1;
      notice =
        action === 'accept'
          ? 'Friend request accepted.'
          : action === 'unblock'
            ? 'User unblocked.'
            : 'Relationship updated.';
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_update_this_relationship_try_again_6b69ab67')
      );
    } finally {
      if (generation === loadGeneration) relationshipBusy = false;
    }
  }

  async function requestFriendship() {
    if (busy || !friendHandle.trim()) return;
    const generation = loadGeneration;
    busy = true;
    error = '';
    notice = '';
    try {
      await api<Relationship>('/users/@me/relationships', {
        method: 'POST',
        body: JSON.stringify({ handle: friendHandle.trim() })
      });
      if (generation !== loadGeneration) return;
      relationships = await api<Relationship[]>('/users/@me/relationships');
      if (generation !== loadGeneration) return;
      relationshipRevision += 1;
      friendHandle = '';
      notice = $t('ui_friend_request_sent_e05e1bac');
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_send_the_friend_request_try_again_15d79fcb')
      );
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }

  async function createGuild() {
    if (busy) return;
    const generation = loadGeneration;
    busy = true;
    error = '';
    try {
      const guild = await api<Guild>('/guilds', {
        method: 'POST',
        body: JSON.stringify({ name })
      });
      if (generation !== loadGeneration) return;
      const channel = firstNavigableChannel(guild.channels);
      if (channel) {
        window.location.assign(guildChannelPath(guild, channel));
      }
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_create_the_guild_check_its_details__bfed72cf')
      );
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }

  async function openDirectMessage(targetHandle = handle) {
    if (busy || !targetHandle.trim()) return;
    const generation = loadGeneration;
    busy = true;
    error = '';
    messageDialogError = '';
    notice = '';
    try {
      const result = await api<
        Channel | { status: 'queued'; operation_id: string; pair_key: string }
      >('/users/@me/channels', {
        method: 'POST',
        body: JSON.stringify({ handle: targetHandle.trim() })
      });
      if (generation !== loadGeneration) return;
      if ('status' in result) {
        notice = $t('ui_the_recipient_s_instance_is_unavailable_your__f9ccf77b');
        messageDialog?.close();
        return;
      }
      messageDialog?.close();
      window.location.assign(directMessagePath(result));
    } catch (caught) {
      if (generation !== loadGeneration) return;
      const message =
        caught instanceof ApiError &&
        (caught.code === 'CANNOT_DM_USER' || caught.code === 'DM_PRIVACY_REJECTED')
          ? 'This person’s privacy settings do not allow a direct message.'
          : userErrorMessage(caught, $t('ui_could_not_open_the_conversation_try_again_6fdfb40a'));
      if (messageDialog?.open) messageDialogError = message;
      else error = message;
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }

  async function createGroupMessage() {
    if (busy || groupMemberKeys.length < 2) return;
    const generation = loadGeneration;
    busy = true;
    error = '';
    messageDialogError = '';
    notice = '';
    try {
      const handles = groupFriends
        .filter((friend) => groupMemberKeys.includes(entityKey(friend)))
        .map(userPublicHandle)
        .filter((value): value is string => Boolean(value));
      const result = await api<Channel>('/users/@me/channels/group', {
        method: 'POST',
        body: JSON.stringify({ handles, name: groupName.trim() || null })
      });
      if (generation !== loadGeneration) return;
      messageDialog?.close();
      window.location.assign(directMessagePath(result));
    } catch (caught) {
      if (generation !== loadGeneration) return;
      messageDialogError = userErrorMessage(
        caught,
        $t('ui_could_not_create_the_group_conversation_check_86f631d0')
      );
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }

  function toggleGroupFriend(user: UserSummary) {
    const key = entityKey(user);
    groupMemberKeys = groupMemberKeys.includes(key)
      ? groupMemberKeys.filter((item) => item !== key)
      : [...groupMemberKeys, key];
    messageDialogError = '';
  }

  function selectMessageFriend(user: UserSummary) {
    const publicHandle = userPublicHandle(user);
    if (!publicHandle) return;
    handle = publicHandle;
    messageDialogError = '';
  }

  function showProfile(user: UserSummary, event: MouseEvent) {
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    profilePopover = { user, x: bounds.right + 8, y: bounds.top };
  }

  async function joinGuild() {
    if (busy) return;
    const inviteReference = normalizeInviteReference(invite);
    if (!inviteReference) {
      error = $t('ui_enter_an_invite_code_or_a_complete_kaede_invi_7a08e98d');
      return;
    }
    const generation = loadGeneration;
    busy = true;
    error = '';
    try {
      const preview = await loadInvitePreview(inviteReference);
      if (generation !== loadGeneration) return;
      const guild = await api<Guild>(`/invites/${encodeURIComponent(inviteReference)}`, {
        method: 'POST'
      });
      if (generation !== loadGeneration) return;
      const loadedGuilds = await api<Guild[]>('/users/@me/guilds');
      if (generation !== loadGeneration) return;
      guilds = loadedGuilds;
      guildRevision += 1;
      const joined = guilds.find((item) => sameEntity(item, guild));
      const channel = joined ? invitedChannel(joined, preview.channel_id) : null;
      if (joined && channel) window.location.assign(guildChannelPath(joined, channel));
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_join_this_guild_check_the_invite_an_bea468bc')
      );
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- route helpers resolve the typed template before substituting encoded parameters -->

{#snippet relationshipRow(relationship: Relationship)}
  <article>
    <span class="avatar avatar-medium">
      {#if relationship.type === 'friend' && relationship.user.avatar_hash}
        <img
          src={assetUrl(relationship.user.avatar_hash, 'thumbnail_128', relationship.user)}
          alt=""
          referrerpolicy="no-referrer"
        />
      {:else}
        {relationship.user.profile_resolved === false
          ? '•'
          : relationship.user.username.slice(0, 1).toUpperCase()}
      {/if}
    </span>
    <span class="relationship-copy">
      <strong>{userDisplayName(relationship.user)}</strong>
      <small
        >{relationship.user.custom_status?.trim() ||
          userPublicHandle(relationship.user) ||
          $t('ui_profile_unavailable_158e5a22')}</small
      >
    </span>
    {#if relationship.type === 'pending_in'}
      <button
        class="primary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'accept')}
        >{$t('ui_accept_89713b9c')}</button
      >
      <button
        class="secondary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'remove')}
        >{$t('ui_ignore_fce77c34')}</button
      >
    {:else if relationship.type === 'friend'}
      <button
        class="secondary-button small-button"
        onclick={(event) => showProfile(relationship.user, event)}
      >
        <Icon name="user" size={15} />{$t('ui_view_profile_d4788f25')}
      </button>
      <button
        class="secondary-button small-button"
        disabled={busy || !userPublicHandle(relationship.user)}
        onclick={() => {
          const publicHandle = userPublicHandle(relationship.user);
          if (publicHandle) void openDirectMessage(publicHandle);
        }}
      >
        <Icon name="message" size={15} />{$t('ui_message_2f77668a')}
      </button>
      <button
        class="secondary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'remove')}
        >{$t('ui_remove_c3812fc4')}</button
      >
    {:else}
      <button
        class="secondary-button small-button"
        disabled={relationshipBusy}
        onclick={() =>
          updateRelationship(
            relationship.user,
            relationship.type === 'blocked' ? 'unblock' : 'remove'
          )}
      >
        {relationship.type === 'blocked'
          ? $t('ui_unblock_712da631')
          : $t('ui_cancel_request_56196683')}
      </button>
    {/if}
  </article>
{/snippet}

<svelte:head
  ><title
    >{$t('ui_value0_kaede_chat_4bf52868', {
      value0: String(friendsView ? 'Friends' : 'Home')
    })}</title
  ></svelte:head
>
<svelte:window onkeydown={navigationKeydown} />

<main class="home-app">
  <GuildRail
    {guilds}
    homeHref={resolve('/home')}
    homeActive
    {homeUnreadCount}
    guildHref={guildLandingPath}
    mentionCount={guildUnread}
  />

  {#if navigationOpen}
    <button
      class="mobile-drawer-backdrop"
      aria-label={$t('ui_close_navigation_99904db3')}
      onclick={() => closeNavigation()}
    ></button>
  {/if}
  <aside
    bind:this={navigationDrawer}
    class:open={navigationOpen}
    class="home-sidebar"
    role={navigationOpen ? 'dialog' : undefined}
    aria-modal={navigationOpen ? 'true' : undefined}
    aria-label={$t('ui_home_navigation_199e23a5')}
  >
    <header class="home-brand">
      <span class="brand-mark">K</span>
      <span
        ><strong>{$t('ui_kaede_7d8eeb35')}</strong><small
          >{currentUser?.origin_domain ?? $t('ui_your_instance_2400b87f')}</small
        ></span
      >
      <button
        bind:this={navigationClose}
        class="mobile-sidebar-close"
        type="button"
        aria-label={$t('ui_close_navigation_99904db3')}
        onclick={() => closeNavigation()}
      >
        ×
      </button>
    </header>
    <nav class="home-nav" aria-label={$t('ui_home_3a786953')}>
      <a
        class:active={!friendsView}
        href={resolve('/home')}
        aria-current={!friendsView ? 'page' : undefined}
        ><Icon name="home" size={18} />{$t('ui_overview_d4b1ea57')}</a
      >
      <a
        class:active={friendsView}
        href={resolve('/home/friends')}
        aria-current={friendsView ? 'page' : undefined}
        ><Icon name="users" size={18} />{$t('ui_friends_requests_2cb22f8b')}
        {#if incomingRequests.length > 0}
          <small class="unread-badge"
            >{incomingRequests.length > 99 ? '99+' : incomingRequests.length}</small
          >
        {/if}</a
      >
    </nav>
    <div class="home-sidebar-heading">
      <span>{$t('ui_direct_messages_95e66705')}</span>
      <button
        type="button"
        aria-label={$t('ui_new_message_78f5975a')}
        title={$t('ui_new_message_78f5975a')}
        onclick={() => (newMessageOpen = true)}><Icon name="plus" size={17} /></button
      >
    </div>
    <nav class="home-dm-list" aria-label={$t('ui_direct_messages_95e66705')}>
      {#each directMessages as channel (entityKey(channel))}
        {@const recipient = channel.recipients?.[0]}
        <a href={directMessagePath(channel)} onclick={() => closeNavigation(false)}>
          <span class="avatar avatar-small">
            {#if isGroupDm(channel)}
              <Icon name="users" size={16} />
            {:else if recipient?.avatar_hash}
              <img src={assetUrl(recipient.avatar_hash, 'thumbnail_128', recipient)} alt="" />
            {:else}
              {recipient?.profile_resolved === false
                ? '•'
                : (recipient?.username.slice(0, 1).toUpperCase() ?? '?')}
            {/if}
          </span>
          <strong>{dmTitle(channel)}</strong>
          {#if unreadFor(channel)?.unread}
            <small class="unread-badge">{Math.max(1, unreadFor(channel)?.mention_count ?? 0)}</small
            >
          {/if}
        </a>
      {:else}
        <p>{$t('ui_no_conversations_yet_52a87373')}</p>
      {/each}
    </nav>
    <div class="sidebar-user-dock">
      <AccountPanel user={currentUser} />
      <a
        class="icon-button"
        href={resolve('/settings')}
        aria-label={$t('ui_user_settings_2b363e87')}
      >
        <Icon name="settings" size={18} />
      </a>
    </div>
  </aside>

  <section class="home-main">
    <header class="home-topbar">
      <button
        bind:this={navigationToggle}
        class="mobile-nav-button"
        type="button"
        aria-label={$t('ui_open_navigation_0ed77fd2')}
        aria-expanded={navigationOpen}
        onclick={openNavigation}
      >
        <span></span><span></span><span></span>
      </button>
      <div>
        <strong>{friendsView ? $t('ui_friends_bd104d1b') : $t('ui_home_3a786953')}</strong>
        <span
          >{friendsView
            ? $t('ui_friends_and_pending_requests_3962f04a')
            : $t('ui_your_conversations_and_communities_617b0ac5')}</span
        >
      </div>
      <div class="home-topbar-actions">
        <button
          class="icon-button"
          type="button"
          aria-label={$t('ui_search_direct_messages_e0cc8e00')}
          title={$t('ui_search_direct_messages_e0cc8e00')}
          onclick={() => (messageSearchOpen = true)}
        >
          <Icon name="message" size={19} />
        </button>
        <button
          class="icon-button"
          type="button"
          aria-label={$t('ui_jump_to_a_channel_56b8a4d6')}
          title={$t('ui_jump_to_a_channel_ctrl_k_1e57f483')}
          onclick={openCommandSwitcher}
        >
          <Icon name="search" size={19} />
        </button>
        <a class="icon-button" href={resolve('/settings')} aria-label={$t('ui_settings_74a883a0')}>
          <Icon name="settings" size={19} />
        </a>
      </div>
    </header>
    <MessageSearch
      bind:open={messageSearchOpen}
      scope="dms"
      scopeRef={null}
      accountRef={currentUser ? entityRef(currentUser) : null}
      users={[
        ...(currentUser ? [currentUser] : []),
        ...directMessages.flatMap((channel) => channel.recipients ?? [])
      ]}
      onJump={jumpToSearchResult}
    />

    <div class="home-scroll" aria-busy={loading}>
      {#if friendsView}
        <section class="friends-hero">
          <div>
            <p class="eyebrow">{$t('ui_your_people_e252b32e')}</p>
            <h1>{$t('ui_friends_bd104d1b')}</h1>
            <p>{$t('ui_manage_connections_and_requests_across_this_i_6dc754ae')}</p>
          </div>
          <div class="friend-summary" aria-label={$t('ui_relationship_summary_a80223eb')}>
            <span><strong>{friends.length}</strong> friends</span>
            <span><strong>{incomingRequests.length}</strong> incoming</span>
            <span><strong>{outgoingRequests.length}</strong> sent</span>
          </div>
        </section>
      {:else}
        <section class="home-hero">
          <div>
            <p class="eyebrow">{$t('ui_welcome_back_66212495')}</p>
            <h1>
              {$t('ui_value0_all_in_one_place_db7aee78', {
                value0: String(currentUser?.display_name ?? currentUser?.username ?? 'Your place')
              })}
            </h1>
            <p>{$t('ui_pick_up_a_conversation_return_to_a_guild_or_s_f46b9c51')}</p>
          </div>
          {#if lastVisited}
            <a class="primary-button" href={lastVisited}>
              {$t('ui_continue_where_you_left_off_dfbc51e0')}
              <Icon name="chevron-right" size={17} />
            </a>
          {/if}
        </section>
      {/if}

      {#if error}
        <div class="notice-banner error-banner home-error-banner" role="alert">
          <span>{error}</span>
          <button class="secondary-button small-button" type="button" onclick={retryOverview}>
            {$t('ui_try_again_d8b8392e')}
          </button>
        </div>
      {/if}
      {#if notice}
        <div class="notice-banner success-banner" aria-live="polite">
          <Icon name="check" size={17} />{notice}
        </div>
      {/if}

      {#if !friendsView}
        <section class="home-section">
          <div class="home-section-heading">
            <div>
              <p>{$t('ui_guilds_25df5134')}</p>
              <h2>{$t('ui_your_communities_605352da')}</h2>
            </div>
            <span>{guilds.length}</span>
          </div>
          {#if loading}
            <div class="guild-grid skeleton-grid" aria-hidden="true">
              <span></span><span></span><span></span>
            </div>
          {:else if guilds.length}
            <div class="guild-grid">
              {#each guilds as guild (entityKey(guild))}
                <a class="guild-card" href={guildLandingPath(guild)}>
                  <span class="guild-card-icon">
                    {#if guild.icon_hash}
                      <img src={assetUrl(guild.icon_hash, 'thumbnail_128', guild)} alt="" />
                    {:else}
                      {guild.name.slice(0, 2).toUpperCase()}
                    {/if}
                  </span>
                  <span class="guild-card-copy">
                    <strong>{guild.name}</strong>
                    <small>{guild.origin_domain}</small>
                    {#if guild.description}<p>{guild.description}</p>{/if}
                  </span>
                  {#if guild.unavailable}
                    <small class="status-chip">{$t('ui_unavailable_ca184496')}</small>
                  {:else if guildUnread(guild)}
                    <small class="unread-badge">{guildUnread(guild)}</small>
                  {:else}
                    <Icon name="chevron-right" size={18} />
                  {/if}
                </a>
              {/each}
            </div>
          {:else}
            <div class="empty-state">
              <span><Icon name="server" size={26} /></span>
              <h3>{$t('ui_no_guilds_yet_3fb1f0e5')}</h3>
              <p>{$t('ui_create_a_home_for_your_community_or_join_one__22148a75')}</p>
            </div>
          {/if}
        </section>

        <section class="home-section">
          <div class="home-section-heading">
            <div>
              <p>{$t('ui_quick_actions_1810407f')}</p>
              <h2>{$t('ui_start_something_0c4b50ca')}</h2>
            </div>
          </div>
          <div class="quick-action-grid">
            <details id="new-message" class="quick-action-card">
              <summary>
                <span class="quick-action-icon green"><Icon name="message" /></span>
                <span
                  ><strong>{$t('ui_new_message_78f5975a')}</strong><small
                    >{$t('ui_reach_someone_by_federated_handle_9a68d35b')}</small
                  ></span
                >
                <Icon name="chevron-down" size={17} />
              </summary>
              <form
                onsubmit={(event) => {
                  event.preventDefault();
                  void openDirectMessage();
                }}
              >
                <label class="form-field compact-field">
                  <span>{$t('ui_user_handle_bbedfb48')}</span>
                  <input bind:value={handle} placeholder="friend@example.net" required />
                </label>
                <button class="primary-button" disabled={busy}
                  >{$t('ui_start_conversation_b61625d0')}</button
                >
              </form>
            </details>

            <details class="quick-action-card">
              <summary>
                <span class="quick-action-icon orange"><Icon name="globe" /></span>
                <span
                  ><strong>{$t('ui_join_a_guild_5c9f5333')}</strong><small
                    >{$t('ui_use_a_local_or_federated_invite_b41d08b4')}</small
                  ></span
                >
                <Icon name="chevron-down" size={17} />
              </summary>
              <form
                onsubmit={(event) => {
                  event.preventDefault();
                  void joinGuild();
                }}
              >
                <label class="form-field compact-field">
                  <span>{$t('ui_invite_code_c6a8991e')}</span>
                  <input
                    bind:value={invite}
                    placeholder={$t('ui_ab12cd34_example_net_9982aaa1')}
                    required
                  />
                </label>
                <button class="primary-button" disabled={busy}
                  >{$t('ui_join_guild_6562aa44')}</button
                >
              </form>
            </details>

            <details class="quick-action-card">
              <summary>
                <span class="quick-action-icon purple"><Icon name="plus" /></span>
                <span
                  ><strong>{$t('ui_create_a_guild_be194799')}</strong><small
                    >{$t('ui_make_a_new_community_on_this_instance_597db0de')}</small
                  ></span
                >
                <Icon name="chevron-down" size={17} />
              </summary>
              <form
                onsubmit={(event) => {
                  event.preventDefault();
                  void createGuild();
                }}
              >
                <label class="form-field compact-field">
                  <span>{$t('ui_guild_name_4bffa81b')}</span>
                  <input bind:value={name} minlength="2" maxlength="100" required />
                </label>
                <button class="primary-button" disabled={busy}>
                  {busy ? $t('ui_creating_c79ed949') : $t('ui_create_guild_7e664b35')}
                </button>
              </form>
            </details>
          </div>
        </section>
      {/if}

      {#if friendsView}
        <section class="friend-add-panel">
          <span class="friend-add-icon"><Icon name="users" size={22} /></span>
          <div>
            <p class="eyebrow">{$t('ui_add_a_friend_33275c37')}</p>
            <h2>{$t('ui_connect_by_federated_username_40da2a8f')}</h2>
            <p>{$t('ui_use_a_full_username_such_as_19fc14e6')} <code>@friend@example.net</code>.</p>
          </div>
          <form
            class="friend-add-form"
            onsubmit={(event) => {
              event.preventDefault();
              void requestFriendship();
            }}
          >
            <label class="form-field compact-field">
              <span>{$t('ui_federated_username_bbdea5c4')}</span>
              <input
                bind:value={friendHandle}
                placeholder={`@friend@${currentUser?.origin_domain ?? 'example.net'}`}
                autocomplete="off"
                required
              />
            </label>
            <button class="primary-button" disabled={busy}>
              {busy ? $t('ui_sending_b8ed5279') : $t('ui_send_request_3a69f897')}
            </button>
          </form>
        </section>

        {#if incomingRequests.length}
          <section class="home-section relationship-section">
            <div class="home-section-heading">
              <div>
                <p>{$t('ui_pending_331551b0')}</p>
                <h2>{$t('ui_incoming_requests_b9bbd089')}</h2>
              </div>
              <span>{incomingRequests.length}</span>
            </div>
            <div class="relationship-list">
              {#each incomingRequests as relationship (entityKey(relationship.user))}
                {@render relationshipRow(relationship)}
              {/each}
            </div>
          </section>
        {/if}

        {#if outgoingRequests.length}
          <section class="home-section relationship-section">
            <div class="home-section-heading">
              <div>
                <p>{$t('ui_pending_331551b0')}</p>
                <h2>{$t('ui_sent_requests_874d69d6')}</h2>
              </div>
              <span>{outgoingRequests.length}</span>
            </div>
            <div class="relationship-list">
              {#each outgoingRequests as relationship (entityKey(relationship.user))}
                {@render relationshipRow(relationship)}
              {/each}
            </div>
          </section>
        {/if}

        <section class="home-section relationship-section">
          <div class="home-section-heading">
            <div>
              <p>{$t('ui_people_7db20897')}</p>
              <h2>{$t('ui_all_friends_c0cb9e20')}</h2>
            </div>
            <span>{friends.length}</span>
          </div>
          <div class="relationship-list">
            {#each friends as relationship (entityKey(relationship.user))}
              {@render relationshipRow(relationship)}
            {:else}
              <div class="empty-state compact-empty">
                <span><Icon name="users" /></span>
                <h3>{$t('ui_no_friends_yet_ebf0ef17')}</h3>
                <p>{$t('ui_send_a_request_using_someone_s_full_federated_1b7089ee')}</p>
              </div>
            {/each}
          </div>
        </section>

        {#if blockedUsers.length}
          <details class="blocked-relationships">
            <summary>
              <span>{$t('ui_blocked_users_f8173cd6')}</span><small>{blockedUsers.length}</small
              ><Icon name="chevron-down" size={16} />
            </summary>
            <div class="relationship-list">
              {#each blockedUsers as relationship (entityKey(relationship.user))}
                {@render relationshipRow(relationship)}
              {/each}
            </div>
          </details>
        {/if}
      {/if}
    </div>
  </section>
</main>

<NewMessageDialog bind:open={newMessageOpen} />

<dialog
  bind:this={messageDialog}
  class="action-dialog dm-picker-dialog"
  onclose={() => {
    handle = '';
    groupName = '';
    groupMemberKeys = [];
    messageMode = 'direct';
    messageDialogError = '';
  }}
>
  <form
    method="dialog"
    onsubmit={(event) => {
      event.preventDefault();
      if (messageMode === 'group') void createGroupMessage();
      else void openDirectMessage();
    }}
  >
    <header>
      <div>
        <p class="eyebrow">{$t('ui_new_conversation_396c946f')}</p>
        <h2>
          {messageMode === 'group'
            ? $t('ui_create_a_group_dm_acda28ac')
            : $t('ui_start_a_conversation_258150cb')}
        </h2>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label={$t('ui_close_7d9eb7ac')}
        onclick={() => messageDialog?.close()}>×</button
      >
    </header>
    <div class="dm-mode-tabs" role="tablist" aria-label={$t('ui_conversation_type_9ca49801')}>
      <button
        type="button"
        role="tab"
        aria-selected={messageMode === 'direct'}
        class:active={messageMode === 'direct'}
        onclick={() => {
          messageMode = 'direct';
          groupMemberKeys = [];
          messageDialogError = '';
        }}>{$t('ui_direct_message_cd3e1605')}</button
      >
      <button
        type="button"
        role="tab"
        aria-selected={messageMode === 'group'}
        class:active={messageMode === 'group'}
        onclick={() => {
          messageMode = 'group';
          handle = '';
          messageDialogError = '';
        }}>{$t('ui_group_dm_cbe7c5d4')}</button
      >
    </div>
    {#if messageMode === 'group'}
      <label class="form-field">
        <span>{$t('ui_group_name_762ebb70')} <small>{$t('ui_optional_59be7133')}</small></span>
        <input
          bind:value={groupName}
          maxlength="100"
          placeholder={$t('ui_weekend_plans_7285cd64')}
        />
      </label>
    {/if}
    <label class="form-field">
      <span
        >{messageMode === 'group'
          ? $t('ui_find_friends_feb7bfd1')
          : $t('ui_username_or_friend_fa66c0a7')}</span
      >
      <input
        bind:value={handle}
        placeholder="@friend@example.net"
        autocomplete="off"
        aria-controls="new-dm-friends"
        oninput={() => (messageDialogError = '')}
        required={messageMode === 'direct'}
      />
      <small>
        {messageMode === 'group'
          ? `Select 2–9 friends. ${groupMemberKeys.length} selected.`
          : $t('ui_enter_a_complete_federated_username_or_select_c604fb23')}
      </small>
    </label>
    <section class="dm-friend-picker" aria-labelledby="dm-friend-picker-heading">
      <div class="dm-friend-picker-heading">
        <strong id="dm-friend-picker-heading">
          {messageMode === 'group'
            ? $t('ui_friends_bd104d1b')
            : $t('ui_friends_without_a_visible_conversation_701b04f1')}
        </strong>
        <small
          >{messageMode === 'group'
            ? filteredGroupFriends.length
            : filteredNewDmFriends.length}</small
        >
      </div>
      <div id="new-dm-friends" class="dm-friend-results">
        {#each messageMode === 'group' ? filteredGroupFriends : filteredNewDmFriends as friend (entityKey(friend))}
          <button
            type="button"
            class:selected={messageMode === 'group'
              ? groupMemberKeys.includes(entityKey(friend))
              : handle.trim().replace(/^@/, '').toLocaleLowerCase() ===
                userPublicHandle(friend)?.replace(/^@/, '').toLocaleLowerCase()}
            aria-pressed={messageMode === 'group'
              ? groupMemberKeys.includes(entityKey(friend))
              : handle.trim().replace(/^@/, '').toLocaleLowerCase() ===
                userPublicHandle(friend)?.replace(/^@/, '').toLocaleLowerCase()}
            disabled={!userPublicHandle(friend) ||
              (messageMode === 'group' &&
                groupMemberKeys.length >= 9 &&
                !groupMemberKeys.includes(entityKey(friend)))}
            onclick={() =>
              messageMode === 'group' ? toggleGroupFriend(friend) : selectMessageFriend(friend)}
          >
            <span class="avatar avatar-small">
              {#if friend.avatar_hash}
                <img
                  src={assetUrl(friend.avatar_hash, 'thumbnail_128', friend)}
                  alt=""
                  referrerpolicy="no-referrer"
                />
              {:else}
                {friend.profile_resolved === false
                  ? '•'
                  : friend.username.slice(0, 1).toUpperCase()}
              {/if}
            </span>
            <span>
              <strong>{userDisplayName(friend)}</strong>
              <small
                >{userPublicHandle(friend)
                  ? `@${userPublicHandle(friend)?.replace(/^@/, '')}`
                  : $t('ui_profile_unavailable_158e5a22')}</small
              >
            </span>
            <Icon
              name={messageMode === 'group' && groupMemberKeys.includes(entityKey(friend))
                ? 'check'
                : 'message'}
              size={17}
            />
          </button>
        {:else}
          <div class="dm-friend-empty">
            {#if messageMode === 'group'}
              <strong>{$t('ui_no_friends_match_that_search_c359d5b1')}</strong>
              <small>{$t('ui_only_existing_friends_can_be_added_automatica_86c917ba')}</small>
            {:else if handle.trim()}
              <strong>{$t('ui_no_friends_match_that_search_c359d5b1')}</strong>
              <small>{$t('ui_you_can_still_message_the_complete_federated__9bfcac7d')}</small>
            {:else if newDmFriends.length === 0}
              <strong>{$t('ui_every_friend_already_has_a_visible_conversati_f23a59c9')}</strong>
              <small>{$t('ui_you_can_still_enter_any_federated_username_ab_32086c17')}</small>
            {:else}
              <strong>{$t('ui_no_friends_available_7af4e539')}</strong>
            {/if}
          </div>
        {/each}
      </div>
    </section>
    {#if messageDialogError}<p class="form-error" role="alert">{messageDialogError}</p>{/if}
    <footer>
      <button class="secondary-button" type="button" onclick={() => messageDialog?.close()}
        >{$t('ui_cancel_19766ed6')}</button
      >
      <button
        class="primary-button"
        disabled={busy || (messageMode === 'group' && groupMemberKeys.length < 2)}
      >
        {busy
          ? $t('ui_opening_c926c2c5')
          : messageMode === 'group'
            ? $t('ui_create_group_35be9c54')
            : $t('ui_message_2f77668a')}
      </button>
    </footer>
  </form>
</dialog>

{#if profilePopover}
  <UserProfileCard
    user={profilePopover.user}
    x={profilePopover.x}
    y={profilePopover.y}
    isSelf={Boolean(currentUser && entityKey(currentUser) === entityKey(profilePopover.user))}
    onClose={() => (profilePopover = null)}
    onMessage={(user) => {
      profilePopover = null;
      const publicHandle = userPublicHandle(user);
      if (publicHandle) void openDirectMessage(publicHandle);
    }}
  />
{/if}
