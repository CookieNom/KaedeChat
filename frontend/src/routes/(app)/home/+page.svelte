<script lang="ts">
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
      readStates = applyIncomingMessage(readStates, message, currentUser);
      const channel = directMessages.find(
        (item) => item.id === message.channel_id && item.origin_domain === message.channel_domain
      );
      if (channel) directMessages = promoteDirectMessage(directMessages, channel);
    } else if (dispatch.t === 'CHANNEL_CREATE') {
      const channel = dispatch.d as Channel;
      if (channel.type === 1 && !directMessages.some((item) => sameEntity(item, channel))) {
        directMessages = promoteDirectMessage(directMessages, channel);
        notice = 'Your direct-message request is ready.';
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
        error = userErrorMessage(caught, 'Could not load your conversations. Try again.');
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
      error = userErrorMessage(caught, 'Could not update this relationship. Try again.');
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
      notice = 'Friend request sent.';
    } catch (caught) {
      if (generation !== loadGeneration) return;
      error = userErrorMessage(caught, 'Could not send the friend request. Try again.');
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
        'Could not create the guild. Check its details and try again.'
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
        notice = 'The recipient’s instance is unavailable. Your request is safely queued.';
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
          : userErrorMessage(caught, 'Could not open the conversation. Try again.');
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
        'Could not create the group conversation. Check the selected friends and try again.'
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
      error = 'Enter an invite code or a complete Kaede invite link.';
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
        'Could not join this guild. Check the invite and try again.'
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
          'Profile unavailable'}</small
      >
    </span>
    {#if relationship.type === 'pending_in'}
      <button
        class="primary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'accept')}>Accept</button
      >
      <button
        class="secondary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'remove')}>Ignore</button
      >
    {:else if relationship.type === 'friend'}
      <button
        class="secondary-button small-button"
        onclick={(event) => showProfile(relationship.user, event)}
      >
        <Icon name="user" size={15} />View profile
      </button>
      <button
        class="secondary-button small-button"
        disabled={busy || !userPublicHandle(relationship.user)}
        onclick={() => {
          const publicHandle = userPublicHandle(relationship.user);
          if (publicHandle) void openDirectMessage(publicHandle);
        }}
      >
        <Icon name="message" size={15} />Message
      </button>
      <button
        class="secondary-button small-button"
        disabled={relationshipBusy}
        onclick={() => updateRelationship(relationship.user, 'remove')}>Remove</button
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
        {relationship.type === 'blocked' ? 'Unblock' : 'Cancel request'}
      </button>
    {/if}
  </article>
{/snippet}

<svelte:head><title>{friendsView ? 'Friends' : 'Home'} · Kaede Chat</title></svelte:head>
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
      aria-label="Close navigation"
      onclick={() => closeNavigation()}
    ></button>
  {/if}
  <aside
    bind:this={navigationDrawer}
    class:open={navigationOpen}
    class="home-sidebar"
    role={navigationOpen ? 'dialog' : undefined}
    aria-modal={navigationOpen ? 'true' : undefined}
    aria-label="Home navigation"
  >
    <header class="home-brand">
      <span class="brand-mark">K</span>
      <span
        ><strong>Kaede</strong><small>{currentUser?.origin_domain ?? 'Your instance'}</small></span
      >
      <button
        bind:this={navigationClose}
        class="mobile-sidebar-close"
        type="button"
        aria-label="Close navigation"
        onclick={() => closeNavigation()}
      >
        ×
      </button>
    </header>
    <nav class="home-nav" aria-label="Home">
      <a
        class:active={!friendsView}
        href={resolve('/home')}
        aria-current={!friendsView ? 'page' : undefined}><Icon name="home" size={18} />Overview</a
      >
      <a
        class:active={friendsView}
        href={resolve('/home/friends')}
        aria-current={friendsView ? 'page' : undefined}
        ><Icon name="users" size={18} />Friends & requests</a
      >
    </nav>
    <div class="home-sidebar-heading">
      <span>Direct messages</span>
      <button
        type="button"
        aria-label="New message"
        title="New message"
        onclick={() => (newMessageOpen = true)}><Icon name="plus" size={17} /></button
      >
    </div>
    <nav class="home-dm-list" aria-label="Direct messages">
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
        <p>No conversations yet.</p>
      {/each}
    </nav>
    <div class="sidebar-user-dock">
      <span class="avatar avatar-small">
        {#if currentUser?.avatar_hash}
          <img src={assetUrl(currentUser.avatar_hash, 'thumbnail_128', currentUser)} alt="" />
        {:else}
          {currentUser?.username.slice(0, 1).toUpperCase() ?? 'K'}
        {/if}
      </span>
      <span>
        <strong>{currentUser?.display_name ?? currentUser?.username ?? 'Your account'}</strong>
        <small>{currentUser?.handle ?? 'Loading…'}</small>
      </span>
      <a class="icon-button" href={resolve('/settings')} aria-label="User settings">
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
        aria-label="Open navigation"
        aria-expanded={navigationOpen}
        onclick={openNavigation}
      >
        <span></span><span></span><span></span>
      </button>
      <div>
        <strong>{friendsView ? 'Friends' : 'Home'}</strong>
        <span
          >{friendsView
            ? 'Friends and pending requests'
            : 'Your conversations and communities'}</span
        >
      </div>
      <div class="home-topbar-actions">
        <button
          class="icon-button"
          type="button"
          aria-label="Search direct messages"
          title="Search direct messages"
          onclick={() => (messageSearchOpen = true)}
        >
          <Icon name="message" size={19} />
        </button>
        <button
          class="icon-button"
          type="button"
          aria-label="Jump to a channel"
          title="Jump to a channel (Ctrl+K)"
          onclick={openCommandSwitcher}
        >
          <Icon name="search" size={19} />
        </button>
        <a class="icon-button" href={resolve('/settings')} aria-label="Settings">
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
            <p class="eyebrow">Your people</p>
            <h1>Friends</h1>
            <p>Manage connections and requests across this instance and the fediverse.</p>
          </div>
          <div class="friend-summary" aria-label="Relationship summary">
            <span><strong>{friends.length}</strong> friends</span>
            <span><strong>{incomingRequests.length}</strong> incoming</span>
            <span><strong>{outgoingRequests.length}</strong> sent</span>
          </div>
        </section>
      {:else}
        <section class="home-hero">
          <div>
            <p class="eyebrow">Welcome back</p>
            <h1>
              {currentUser?.display_name ?? currentUser?.username ?? 'Your place'}, all in one
              place.
            </h1>
            <p>Pick up a conversation, return to a guild, or start something new.</p>
          </div>
          {#if lastVisited}
            <a class="primary-button" href={lastVisited}>
              Continue where you left off <Icon name="chevron-right" size={17} />
            </a>
          {/if}
        </section>
      {/if}

      {#if error}
        <div class="notice-banner error-banner home-error-banner" role="alert">
          <span>{error}</span>
          <button class="secondary-button small-button" type="button" onclick={retryOverview}>
            Try again
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
              <p>Guilds</p>
              <h2>Your communities</h2>
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
                    <small class="status-chip">Unavailable</small>
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
              <h3>No guilds yet</h3>
              <p>Create a home for your community or join one with an invite.</p>
            </div>
          {/if}
        </section>

        <section class="home-section">
          <div class="home-section-heading">
            <div>
              <p>Quick actions</p>
              <h2>Start something</h2>
            </div>
          </div>
          <div class="quick-action-grid">
            <details id="new-message" class="quick-action-card">
              <summary>
                <span class="quick-action-icon green"><Icon name="message" /></span>
                <span
                  ><strong>New message</strong><small>Reach someone by federated handle</small
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
                  <span>User handle</span>
                  <input bind:value={handle} placeholder="friend@example.net" required />
                </label>
                <button class="primary-button" disabled={busy}>Start conversation</button>
              </form>
            </details>

            <details class="quick-action-card">
              <summary>
                <span class="quick-action-icon orange"><Icon name="globe" /></span>
                <span
                  ><strong>Join a guild</strong><small>Use a local or federated invite</small></span
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
                  <span>Invite code</span>
                  <input bind:value={invite} placeholder="Ab12Cd34@example.net" required />
                </label>
                <button class="primary-button" disabled={busy}>Join guild</button>
              </form>
            </details>

            <details class="quick-action-card">
              <summary>
                <span class="quick-action-icon purple"><Icon name="plus" /></span>
                <span
                  ><strong>Create a guild</strong><small
                    >Make a new community on this instance</small
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
                  <span>Guild name</span>
                  <input bind:value={name} minlength="2" maxlength="100" required />
                </label>
                <button class="primary-button" disabled={busy}>
                  {busy ? 'Creating…' : 'Create guild'}
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
            <p class="eyebrow">Add a friend</p>
            <h2>Connect by federated username</h2>
            <p>Use a full username such as <code>@friend@example.net</code>.</p>
          </div>
          <form
            class="friend-add-form"
            onsubmit={(event) => {
              event.preventDefault();
              void requestFriendship();
            }}
          >
            <label class="form-field compact-field">
              <span>Federated username</span>
              <input
                bind:value={friendHandle}
                placeholder={`@friend@${currentUser?.origin_domain ?? 'example.net'}`}
                autocomplete="off"
                required
              />
            </label>
            <button class="primary-button" disabled={busy}>
              {busy ? 'Sending…' : 'Send request'}
            </button>
          </form>
        </section>

        {#if incomingRequests.length}
          <section class="home-section relationship-section">
            <div class="home-section-heading">
              <div>
                <p>Pending</p>
                <h2>Incoming requests</h2>
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
                <p>Pending</p>
                <h2>Sent requests</h2>
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
              <p>People</p>
              <h2>All friends</h2>
            </div>
            <span>{friends.length}</span>
          </div>
          <div class="relationship-list">
            {#each friends as relationship (entityKey(relationship.user))}
              {@render relationshipRow(relationship)}
            {:else}
              <div class="empty-state compact-empty">
                <span><Icon name="users" /></span>
                <h3>No friends yet</h3>
                <p>Send a request using someone’s full federated username.</p>
              </div>
            {/each}
          </div>
        </section>

        {#if blockedUsers.length}
          <details class="blocked-relationships">
            <summary>
              <span>Blocked users</span><small>{blockedUsers.length}</small><Icon
                name="chevron-down"
                size={16}
              />
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
        <p class="eyebrow">New conversation</p>
        <h2>{messageMode === 'group' ? 'Create a group DM' : 'Start a conversation'}</h2>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label="Close"
        onclick={() => messageDialog?.close()}>×</button
      >
    </header>
    <div class="dm-mode-tabs" role="tablist" aria-label="Conversation type">
      <button
        type="button"
        role="tab"
        aria-selected={messageMode === 'direct'}
        class:active={messageMode === 'direct'}
        onclick={() => {
          messageMode = 'direct';
          groupMemberKeys = [];
          messageDialogError = '';
        }}>Direct message</button
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
        }}>Group DM</button
      >
    </div>
    {#if messageMode === 'group'}
      <label class="form-field">
        <span>Group name <small>Optional</small></span>
        <input bind:value={groupName} maxlength="100" placeholder="Weekend plans" />
      </label>
    {/if}
    <label class="form-field">
      <span>{messageMode === 'group' ? 'Find friends' : 'Username or friend'}</span>
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
          : 'Enter a complete federated username, or select a friend below.'}
      </small>
    </label>
    <section class="dm-friend-picker" aria-labelledby="dm-friend-picker-heading">
      <div class="dm-friend-picker-heading">
        <strong id="dm-friend-picker-heading">
          {messageMode === 'group' ? 'Friends' : 'Friends without a visible conversation'}
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
                  : 'Profile unavailable'}</small
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
              <strong>No friends match that search.</strong>
              <small>Only existing friends can be added automatically.</small>
            {:else if handle.trim()}
              <strong>No friends match that search.</strong>
              <small>You can still message the complete federated username above.</small>
            {:else if newDmFriends.length === 0}
              <strong>Every friend already has a visible conversation.</strong>
              <small>You can still enter any federated username above.</small>
            {:else}
              <strong>No friends available.</strong>
            {/if}
          </div>
        {/each}
      </div>
    </section>
    {#if messageDialogError}<p class="form-error" role="alert">{messageDialogError}</p>{/if}
    <footer>
      <button class="secondary-button" type="button" onclick={() => messageDialog?.close()}
        >Cancel</button
      >
      <button
        class="primary-button"
        disabled={busy || (messageMode === 'group' && groupMemberKeys.length < 2)}
      >
        {busy ? 'Opening…' : messageMode === 'group' ? 'Create group' : 'Message'}
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
