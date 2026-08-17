<script lang="ts">
  import { resolve } from '$app/paths';
  import { page } from '$app/state';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import type { GifResult } from '$lib/chat/gifs';
  import {
    customEmojiToken,
    loadUnicodeEmojis,
    unicodeEmojiCompletions,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import { autosizeTextarea } from '$lib/ui/autosize';
  import { firstNavigableChannel } from '$lib/chat/channels';
  import { dmTitle, groupDmSubtitle, isGroupDm, ownsGroupDm } from '$lib/chat/direct-messages';
  import { completionAt, replaceCompletion } from '$lib/chat/completion';
  import { mentionsUser } from '$lib/chat/mentions';
  import {
    applyMessageDeliveryUpdate,
    compareMessages,
    failPendingMessage,
    mergeMessageSnapshot,
    reconcileMessage,
    type MessageDeliveryUpdate
  } from '$lib/chat/reconcile';
  import { applyReactionUpdate, type ReactionUpdate } from '$lib/chat/reaction-state';
  import {
    discardAttachments,
    pendingMessageSend,
    type PendingMessageSend,
    withoutSubmittedUploads
  } from '$lib/chat/outbox';
  import { compareEntityRefs, entityKey, entityRef, matchesEntityRef } from '$lib/chat/refs';
  import { buildTimeline } from '$lib/chat/timeline';
  import {
    activeTypingParticipants,
    typingLabel,
    upsertTypingParticipant,
    type TypingParticipant
  } from '$lib/chat/typing';
  import type {
    Attachment,
    Channel,
    CustomEmoji,
    Guild,
    Message,
    ReadStateStatus,
    UserSummary
  } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import {
    GATEWAY_SESSION_RESET_EVENT,
    type Dispatch,
    type GatewayClient
  } from '$lib/gateway/client';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import ComposerAutocomplete, {
    type Completion
  } from '$lib/components/ComposerAutocomplete.svelte';
  import GuildRail from '$lib/components/GuildRail.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import EmojiPicker from '$lib/components/EmojiPicker.svelte';
  import GifPicker from '$lib/components/GifPicker.svelte';
  import MessageRow from '$lib/components/MessageRow.svelte';
  import MessageSearch from '$lib/components/MessageSearch.svelte';
  import NewMessageDialog from '$lib/components/NewMessageDialog.svelte';
  import PinnedMessagesPanel from '$lib/components/PinnedMessagesPanel.svelte';
  import PresencePicker from '$lib/components/PresencePicker.svelte';
  import UploadPreviewTray from '$lib/components/UploadPreviewTray.svelte';
  import UserProfileCard from '$lib/components/UserProfileCard.svelte';
  import VirtualMessageList from '$lib/components/VirtualMessageList.svelte';
  import {
    decryptConversationMessages,
    initializeE2EE,
    type KaedeE2EEClient
  } from '$lib/e2ee/client';
  import { uploadEncryptedChannelFile } from '$lib/e2ee/media';
  import { uploadChannelFile, type PendingUpload } from '$lib/media/uploads';
  import { assetUrl } from '$lib/media/assets';
  import { directMessageUnreadCount, guildMentionCount } from '$lib/notifications/counts';
  import { applyReadStateDispatch, type ReadStateDispatch } from '$lib/notifications/read-state';
  import { ReadAcknowledgementQueue } from '$lib/notifications/read-ack';
  import { directMessagePath, guildChannelPath } from '$lib/navigation/routes';
  import { chatEntities as entities } from '$lib/stores/entities.svelte';
  import VoiceDock from '$lib/voice/VoiceDock.svelte';
  import { onMount, tick, untrack } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';

  const dmId = $derived(page.params.dmId ?? '');
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname;
  const directMessages = $derived(
    entities.channels.values.filter((item) => item.guild_id === null)
  );
  const guilds = $derived(entities.guilds.values);
  const messages = $derived(
    entities.messages.values.filter((message) =>
      matchesEntityRef(
        dmId,
        { id: message.channel_id, origin_domain: message.channel_domain },
        localDomain
      )
    )
  );
  const readStates = $derived(entities.readStates.values);
  const currentUser = $derived(entities.currentUser);
  const homeUnreadCount = $derived(directMessageUnreadCount(readStates));
  let content = $state('');
  let gifPickerEnabled = $state(false);
  let e2eeActivationEnabled = $state(false);
  let gifPickerOpen = $state(false);
  let messageSearchOpen = $state(false);
  let newMessageOpen = $state(false);
  let gifConfigurationError = $state('');
  let gifConfigurationLoading = $state(false);
  let featureController: AbortController | null = null;
  let emojiPickerOpen = $state(false);
  let availableEmojis = $state<CustomEmoji[]>([]);
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let emojiCatalogLoading = false;
  const pickerEmojis = $derived(
    availableEmojis
      .filter((emoji) => emoji.media_hash)
      .map((emoji): CustomEmojiOption => ({
        ...emoji,
        url: assetUrl(emoji.media_hash ?? '', 'thumbnail_128', emoji.origin_domain),
        value: customEmojiToken(emoji)
      }))
      .filter((emoji) => Boolean(emoji.url && emoji.value))
  );
  let error = $state('');
  let busy = $state(false);
  let channelReady = $state(false);
  let typing = $state('');
  let typingParticipants = $state<TypingParticipant[]>([]);
  let replyingMessage = $state<Message | null>(null);
  let pinnedMessages = $state<Message[]>([]);
  let pinsOpen = $state(false);
  let pinsLoading = $state(false);
  let pinsError = $state('');
  let hasEarlier = $state(true);
  let authorityHistoryComplete = $state(false);
  let loadingEarlier = $state(false);
  let hasLater = $state(false);
  let loadingLater = $state(false);
  let lastTypingAt = 0;
  let loadGeneration = 0;
  let snapshotGeneration = 0;
  let typingTimer: number | null = null;
  let gateway: GatewayClient | null = null;
  let dispatchBuffer: Dispatch[] | null = null;
  let uploads = $state<PendingUpload[]>([]);
  let fileInput = $state<HTMLInputElement | null>(null);
  let activeCall = $state<CallState | null>(null);
  let callJoined = $state(false);
  let composerInput = $state<HTMLTextAreaElement | null>(null);
  let autocomplete = $state<{ handleKeydown(event: KeyboardEvent): boolean } | null>(null);
  let editingMessage = $state<Message | null>(null);
  let composerDraftBeforeEdit = $state<{ content: string; cursor: number } | null>(null);
  let composerCursor = $state(0);
  let completionActive = $state(0);
  let completionOpen = $state(false);
  let timelineAtBottom = $state(false);
  let callBusy = $state(false);
  let callRevision = 0;
  let mobileNavigationOpen = $state(false);
  let mobileNavigationToggle = $state<HTMLButtonElement | null>(null);
  let mobileNavigationDrawer = $state<HTMLElement | null>(null);
  let mobileNavigationClose = $state<HTMLButtonElement | null>(null);
  let profile = $state<{ user: UserSummary; x: number; y: number } | null>(null);
  let presencePreference = $state<'online' | 'idle' | 'dnd' | 'invisible'>('online');
  let readStateWarning = $state('');
  let groupDialog = $state<HTMLDialogElement | null>(null);
  let groupName = $state('');
  let groupInviteHandle = $state('');
  let groupError = $state('');
  let groupBusy = $state(false);
  let e2eeClient = $state<KaedeE2EEClient | null>(null);
  let e2eeSafetyNumber = $state('');
  const uploadControllers = new SvelteMap<string, AbortController>();
  const pendingSends = new SvelteMap<string, PendingMessageSend>();
  const deliveryRecoveries = new SvelteSet<string>();
  const readAcknowledgements = new ReadAcknowledgementQueue<Message>({
    send: (message) =>
      api(
        `/channels/${encodeURIComponent(entityRef({ id: message.channel_id, origin_domain: message.channel_domain }))}/ack`,
        {
          method: 'POST',
          body: JSON.stringify({ message_id: entityRef(message) })
        }
      ),
    acknowledged: markMessageAcknowledged,
    warningChanged: (message) => (readStateWarning = message)
  });

  function guildLandingPath(guild: Guild): string {
    const target = firstNavigableChannel(guild.channels);
    return target ? guildChannelPath(guild, target) : resolve('/home');
  }

  async function openMobileNavigation() {
    mobileNavigationOpen = true;
    await tick();
    mobileNavigationClose?.focus();
  }

  function closeMobileNavigation(restoreFocus = true) {
    if (!mobileNavigationOpen) return;
    mobileNavigationOpen = false;
    if (restoreFocus) void tick().then(() => mobileNavigationToggle?.focus());
  }

  function toggleMobileNavigation() {
    if (mobileNavigationOpen) closeMobileNavigation();
    else void openMobileNavigation();
  }

  function mobileNavigationKeydown(event: KeyboardEvent) {
    if (!mobileNavigationOpen) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMobileNavigation();
      return;
    }
    if (event.key !== 'Tab' || !mobileNavigationDrawer) return;
    const focusable = Array.from(
      mobileNavigationDrawer.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (!mobileNavigationDrawer.contains(document.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  interface CallState {
    id: string;
    channel_id: string;
    channel_domain: string;
    authority_domain: string;
    room: string;
    state: 'ringing' | 'active' | 'ended';
    created_at: number;
    ended_at: number | null;
    caller: string;
    participants: string[];
  }

  interface ActiveCallState {
    call: CallState | null;
    joined: boolean;
  }

  const activeCallRef = $derived(
    activeCall ? entityRef({ id: activeCall.id, origin_domain: activeCall.authority_domain }) : ''
  );

  const channel = $derived(
    directMessages.find((item) => matchesEntityRef(dmId, item, localDomain)) ?? null
  );
  const recipient = $derived(channel?.recipients?.[0] ?? null);
  const conversationTitle = $derived(dmTitle(channel));
  const groupConversation = $derived(isGroupDm(channel));
  const groupOwner = $derived(Boolean(channel && ownsGroupDm(channel, currentUser)));
  const currentReadState = $derived(channel ? unreadFor(channel) : undefined);
  const timeline = $derived(
    buildTimeline(
      messages,
      currentReadState?.read_message_id && currentReadState.read_message_domain
        ? {
            id: currentReadState.read_message_id,
            origin_domain: currentReadState.read_message_domain
          }
        : null
    )
  );
  const aroundMessage = $derived(page.url.searchParams.get('around'));
  const targetTimelineKey = $derived.by(() => {
    if (!aroundMessage) return null;
    const target = messages.find((message) =>
      matchesEntityRef(aroundMessage, message, localDomain)
    );
    return target ? `message:${entityKey(target)}` : null;
  });

  function referencedMessage(message: Message): Message | null {
    if (!message.referenced_message_id) return null;
    return (
      messages.find(
        (candidate) =>
          candidate.id === message.referenced_message_id &&
          candidate.origin_domain === message.referenced_message_domain
      ) ?? null
    );
  }

  function reachesRetainedHistoryStart(
    target: Channel | null,
    oldest: Message | undefined
  ): boolean {
    const retained = target?.oldest_available_message_ref;
    return Boolean(
      target?.history_truncated &&
      !target.history_remote_available &&
      retained &&
      oldest &&
      oldest.id === retained.id &&
      oldest.origin_domain === retained.origin_domain
    );
  }

  function resetTyping() {
    typingParticipants = [];
    typing = '';
    if (typingTimer) window.clearTimeout(typingTimer);
    typingTimer = null;
  }

  function refreshTyping() {
    typingParticipants = activeTypingParticipants(typingParticipants);
    typing = typingLabel(typingParticipants);
    if (typingTimer) window.clearTimeout(typingTimer);
    typingTimer = null;
    if (!typingParticipants.length) return;
    const nextExpiry = Math.min(...typingParticipants.map((item) => item.expiresAt));
    typingTimer = window.setTimeout(refreshTyping, Math.max(50, nextExpiry - Date.now() + 5));
  }

  function registerTyping(userId: string, userDomain?: string) {
    const domain = userDomain ?? localDomain;
    if (currentUser?.id === userId && currentUser.origin_domain === domain) return;
    const user =
      entities.users.values.find(
        (candidate) => candidate.id === userId && candidate.origin_domain === domain
      ) ?? recipient;
    typingParticipants = upsertTypingParticipant(typingParticipants, {
      ref: `${userId}@${domain}`,
      name: user ? userDisplayName(user) : 'Someone'
    });
    refreshTyping();
  }
  const completionQuery = $derived(completionAt(content, composerCursor));
  const completionOptions = $derived(
    completionQuery?.marker === ':'
      ? [
          ...pickerEmojis
            .filter((emoji) =>
              emoji.name.toLocaleLowerCase().includes(completionQuery.query.toLocaleLowerCase())
            )
            .map((emoji) => ({
              value: emoji.value,
              label: `:${emoji.name}:`,
              detail: emoji.guild_name ?? 'Custom emoji',
              imageUrl: emoji.url,
              kind: 'custom-emoji' as const
            })),
          ...unicodeEmojiCompletions(unicodeEmojis, completionQuery.query)
        ]
      : completionQuery?.marker === '@' &&
          recipient &&
          userPublicHandle(recipient)
            ?.toLocaleLowerCase()
            .includes(completionQuery.query.toLocaleLowerCase())
        ? [
            {
              value: `<@${entityRef(recipient)}>`,
              label: userDisplayName(recipient),
              detail: `@${userPublicHandle(recipient)}`
            }
          ]
        : []
  );

  $effect(() => {
    if (completionQuery?.marker !== ':' || unicodeEmojis.length || emojiCatalogLoading) return;
    emojiCatalogLoading = true;
    void loadUnicodeEmojis()
      .then((items) => (unicodeEmojis = items))
      .finally(() => (emojiCatalogLoading = false));
  });

  const setMessages = (items: Message[]) => {
    const target = channel;
    if (!target) {
      entities.messages.upsertMany(items);
      return;
    }
    entities.messages.replaceWhere(
      items,
      (message) =>
        message.channel_id === target.id && message.channel_domain === target.origin_domain
    );
  };
  const setReadStates = (items: ReadStateStatus[]) => entities.readStates.replace(items);
  const setGuilds = (items: Guild[]) => entities.ingestGuilds(items);
  const setDirectMessages = (items: Channel[]) => entities.ingestDirectMessages(items);

  function unreadFor(target: Channel): ReadStateStatus | undefined {
    return readStates.find(
      (state) => state.channel_id === target.id && state.channel_domain === target.origin_domain
    );
  }

  function isCurrentChannel(channelId: string, channelDomain: string): boolean {
    return matchesEntityRef(dmId, { id: channelId, origin_domain: channelDomain }, localDomain);
  }

  function dispatchTargetsCurrentChannel(channelId: string, channelDomain?: string): boolean {
    return channelDomain ? isCurrentChannel(channelId, channelDomain) : channel?.id === channelId;
  }

  function isCurrentCall(call: CallState): boolean {
    return (
      activeCall?.id === call.id &&
      activeCall.authority_domain === call.authority_domain &&
      isCurrentChannel(call.channel_id, call.channel_domain)
    );
  }

  function callWasStartedByMe(call: CallState): boolean {
    return currentUser !== null && call.caller === entityRef(currentUser);
  }

  function openProfile(user: UserSummary, event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    const bounds = (event.currentTarget as HTMLElement | null)?.getBoundingClientRect();
    profile = {
      user,
      x: event.clientX || (bounds?.right ?? window.innerWidth / 2),
      y: event.clientY || (bounds?.top ?? window.innerHeight / 2)
    };
  }

  function setMyPresence(status: 'online' | 'idle' | 'dnd' | 'invisible') {
    presencePreference = status;
    try {
      localStorage.setItem('kaede.presence', status);
    } catch {
      // Presence still applies to this connection when persistent storage is unavailable.
    }
    gateway?.setPresence(status);
    void api('/users/@me/settings', {
      method: 'PATCH',
      body: JSON.stringify({ presence_preference: status })
    }).catch((caught) => {
      if (presencePreference !== status) return;
      error = `Presence changed for this session, but it could not sync to your other devices. ${userErrorMessage(
        caught,
        'The server could not save the presence setting. Try again.'
      )}`;
    });
    if (currentUser) entities.setPresence(currentUser, status === 'invisible' ? 'offline' : status);
  }

  function myPresencePreference(): 'online' | 'idle' | 'dnd' | 'invisible' {
    try {
      const preferred = localStorage.getItem('kaede.presence');
      if (preferred === 'idle' || preferred === 'dnd' || preferred === 'invisible')
        return preferred;
    } catch {
      // Use online when browser storage is unavailable.
    }
    return 'online';
  }

  function openMessageProfile(message: Message, event: MouseEvent) {
    if (message.author) openProfile(message.author, event);
  }

  async function openHandleProfile(event: Event) {
    const detail = (event as CustomEvent<{ handle?: string; reference?: string }>).detail;
    const handle = detail?.handle;
    if (detail?.reference) {
      const reference = detail.reference.includes('@')
        ? detail.reference
        : `${detail.reference}@${localDomain}`;
      const knownUser = entities.users.get(reference);
      if (knownUser) {
        profile = { user: knownUser, x: window.innerWidth / 2, y: window.innerHeight / 2 };
        return;
      }
    }
    if (!handle) return;
    try {
      const user = await api<UserSummary>(`/users/lookup?handle=${encodeURIComponent(handle)}`);
      profile = { user, x: window.innerWidth / 2, y: window.innerHeight / 2 };
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not load that profile. Try again.');
    }
  }

  function applyDispatch(dispatch: Dispatch) {
    if (dispatch.t === 'MESSAGE_CREATE') {
      const message = dispatch.d as Message;
      if (isCurrentChannel(message.channel_id, message.channel_domain)) {
        if (message.e2ee && channel && e2eeClient) {
          void decryptConversationMessages(e2eeClient, channel, [message]).then(([decrypted]) =>
            reconcile(decrypted)
          );
        } else reconcile(message);
        if (document.visibilityState === 'visible' && timelineAtBottom) void acknowledge(message);
      } else {
        setReadStates(
          readStates.map((state) =>
            state.channel_id === message.channel_id &&
            state.channel_domain === message.channel_domain
              ? {
                  ...state,
                  last_message_id: message.id,
                  last_message_domain: message.origin_domain,
                  unread: true
                }
              : state
          )
        );
      }
    } else if (dispatch.t === 'MESSAGE_UPDATE') {
      const update = dispatch.d as Message;
      if (update.e2ee && channel && e2eeClient) {
        void decryptConversationMessages(e2eeClient, channel, [update]).then(([decrypted]) =>
          applyDispatch({ ...dispatch, d: { ...decrypted, e2ee: null } })
        );
        return;
      }
      setMessages(
        messages.map((item) =>
          entityKey(item) === entityKey(update)
            ? 'reaction' in update
              ? applyReactionUpdate(item, update as unknown as ReactionUpdate, currentUser)
              : { ...item, ...update }
            : item
        )
      );
    } else if (dispatch.t === 'ATTACHMENT_UPDATE') {
      const update = dispatch.d as {
        message_id: string;
        message_domain: string;
        attachment: Attachment;
      };
      setMessages(
        messages.map((item) =>
          item.id === update.message_id && item.origin_domain === update.message_domain
            ? {
                ...item,
                attachments: item.attachments?.map((attachment) =>
                  attachment.id === update.attachment.id &&
                  attachment.origin_domain === update.attachment.origin_domain
                    ? update.attachment
                    : attachment
                )
              }
            : item
        )
      );
    } else if (dispatch.t === 'MESSAGE_DELETE') {
      const deleted = dispatch.d as {
        id: string;
        origin_domain: string;
        channel_id: string;
        channel_domain: string;
      };
      if (isCurrentChannel(deleted.channel_id, deleted.channel_domain)) {
        setMessages(
          messages.map((item) =>
            item.id === deleted.id && item.origin_domain === deleted.origin_domain
              ? { ...item, content: null, deleted_at: new Date().toISOString() }
              : item
          )
        );
      }
    } else if (dispatch.t === 'MESSAGE_DELIVERY_UPDATE') {
      const update = dispatch.d as MessageDeliveryUpdate;
      if (isCurrentChannel(update.channel_id, update.channel_domain)) {
        const applied = applyMessageDeliveryUpdate(messages, update);
        setMessages(applied.messages);
        if (!applied.matched) void recoverDeliveryUpdate(update);
      }
    } else if (dispatch.t === 'USER_UPDATE') {
      const user = dispatch.d as UserSummary;
      if (user.id && user.origin_domain) {
        entities.applyUserProfile(user);
        const patch = (message: Message): Message =>
          message.author_id === user.id && message.author_domain === user.origin_domain
            ? { ...message, author: { ...(message.author ?? user), ...user } }
            : message;
        pinnedMessages = pinnedMessages.map(patch);
        if (replyingMessage) replyingMessage = patch(replyingMessage);
        if (profile && entityKey(profile.user) === entityKey(user)) {
          profile = { ...profile, user: { ...profile.user, ...user } };
        }
      }
    } else if (dispatch.t === 'GUILD_EMOJI_CREATE') {
      const emoji = dispatch.d as CustomEmoji;
      availableEmojis = [
        ...availableEmojis.filter((item) => entityKey(item) !== entityKey(emoji)),
        emoji
      ];
    } else if (dispatch.t === 'GUILD_EMOJI_DELETE') {
      const emoji = dispatch.d as CustomEmoji;
      availableEmojis = availableEmojis.filter((item) => entityKey(item) !== entityKey(emoji));
    } else if (dispatch.t === 'GUILD_DELETE') {
      const removed = dispatch.d as { id: string; origin_domain: string };
      availableEmojis = availableEmojis.filter(
        (item) => item.guild_id !== removed.id || item.guild_domain !== removed.origin_domain
      );
    } else if (dispatch.t === 'TYPING_START') {
      const started = dispatch.d as {
        channel_id: string;
        channel_domain?: string;
        user_id?: string;
        user_domain?: string;
      };
      const authoredByMe =
        currentUser !== null &&
        started.user_id === currentUser.id &&
        (!started.user_domain || started.user_domain === currentUser.origin_domain);
      if (
        !authoredByMe &&
        dispatchTargetsCurrentChannel(started.channel_id, started.channel_domain)
      ) {
        registerTyping(started.user_id ?? recipient?.id ?? 'unknown', started.user_domain);
      }
    } else if (dispatch.t === 'READ_STATE_UPDATE') {
      setReadStates(applyReadStateDispatch(readStates, dispatch.d as ReadStateDispatch));
    } else if (dispatch.t === 'CALL_CREATE' || dispatch.t === 'CALL_RING') {
      const call = dispatch.d as CallState;
      if (isCurrentChannel(call.channel_id, call.channel_domain)) {
        callRevision += 1;
        activeCall = call;
        callJoined = callWasStartedByMe(call);
      }
    } else if (dispatch.t === 'CALL_ACCEPT') {
      const call = dispatch.d as CallState;
      if (
        isCurrentChannel(call.channel_id, call.channel_domain) &&
        (!activeCall ||
          (activeCall.id === call.id && activeCall.authority_domain === call.authority_domain))
      ) {
        callRevision += 1;
        activeCall = call;
        callJoined = true;
      }
    } else if (dispatch.t === 'CALL_DECLINE' || dispatch.t === 'CALL_END') {
      const call = dispatch.d as CallState;
      if (isCurrentCall(call)) {
        callRevision += 1;
        activeCall = null;
        callJoined = false;
      }
    } else if (dispatch.t === 'PRESENCE_UPDATE') {
      const update = dispatch.d as {
        user_id: string;
        user_domain: string;
        status: import('$lib/chat/types').PresenceStatus;
        preference?: 'online' | 'idle' | 'dnd' | 'invisible';
      };
      entities.setPresence(
        { id: update.user_id, origin_domain: update.user_domain },
        update.status
      );
      if (
        update.preference &&
        currentUser?.id === update.user_id &&
        currentUser.origin_domain === update.user_domain
      ) {
        presencePreference = update.preference;
      }
    }
  }

  async function refreshGifConfiguration() {
    const controller = featureController;
    if (!controller || gifConfigurationLoading) return;
    gifConfigurationLoading = true;
    gifConfigurationError = '';
    try {
      const configuration = await loadAuthConfiguration(controller.signal);
      gifPickerEnabled = configuration.gif_picker_enabled;
      e2eeActivationEnabled = configuration.e2ee_activation_enabled;
    } catch (caught) {
      if (controller.signal.aborted) return;
      gifPickerEnabled = false;
      e2eeActivationEnabled = false;
      gifConfigurationError = userErrorMessage(
        caught,
        'Could not check whether GIF search is available. Try again.'
      );
    } finally {
      if (featureController === controller) gifConfigurationLoading = false;
    }
  }

  onMount(() => {
    const client = authenticatedGateway.client;
    gateway = client;
    featureController = new AbortController();
    void refreshGifConfiguration();
    const desktopViewport = window.matchMedia('(min-width: 741px)');
    const viewportChanged = () => {
      if (desktopViewport.matches) closeMobileNavigation(false);
    };
    const visibilityChanged = () => acknowledgeLatestIfVisible();
    const sessionReset = () => recoverCurrentRoute();
    const profileRequest = (event: Event) => void openHandleProfile(event);
    const receive = (event: Event) => {
      const dispatch = (event as CustomEvent<Dispatch>).detail;
      if (dispatchBuffer && dispatch.t !== 'READY' && dispatch.t !== 'RESUMED') {
        dispatchBuffer.push(dispatch);
        return;
      }
      applyDispatch(dispatch);
    };
    document.addEventListener('visibilitychange', visibilityChanged);
    window.addEventListener('kaede:open-user-profile', profileRequest);
    client.addEventListener('dispatch', receive);
    client.addEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
    desktopViewport.addEventListener('change', viewportChanged);
    viewportChanged();
    return () => {
      featureController?.abort();
      featureController = null;
      readAcknowledgements.reset();
      loadGeneration += 1;
      snapshotGeneration += 1;
      dispatchBuffer = null;
      document.removeEventListener('visibilitychange', visibilityChanged);
      window.removeEventListener('kaede:open-user-profile', profileRequest);
      client.removeEventListener('dispatch', receive);
      client.removeEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
      desktopViewport.removeEventListener('change', viewportChanged);
      if (gateway === client) gateway = null;
      resetTyping();
      resetUploads();
    };
  });

  function chooseGif(gif: GifResult) {
    if (busy || !channelReady || !channel || editingMessage) return;
    gifPickerOpen = false;
    emojiPickerOpen = false;
    void send(pendingMessageSend(gif.url, [], []));
  }

  function chooseEmoji(value: string) {
    if (busy || !channelReady || !channel) return;
    const start = composerInput?.selectionStart ?? composerCursor;
    const end = composerInput?.selectionEnd ?? start;
    const next = `${content.slice(0, start)}${value}${content.slice(end)}`;
    if (next.length > 4000) return;
    content = next;
    composerCursor = start + value.length;
    emojiPickerOpen = false;
    void tick().then(() => {
      composerInput?.focus();
      composerInput?.setSelectionRange(composerCursor, composerCursor);
    });
  }

  $effect(() => {
    const targetRef = dmId;
    const targetAround = aroundMessage;
    untrack(() => {
      const routeGeneration = ++loadGeneration;
      const snapshot = ++snapshotGeneration;
      const buffered: Dispatch[] = [];
      dispatchBuffer = buffered;
      setMessages([]);
      resetUploads();
      editingMessage = null;
      composerDraftBeforeEdit = null;
      content = '';
      composerCursor = 0;
      resetTyping();
      replyingMessage = null;
      pinnedMessages = [];
      pinsOpen = false;
      pinsLoading = false;
      pinsError = '';
      error = '';
      busy = false;
      channelReady = false;
      timelineAtBottom = false;
      callRevision += 1;
      activeCall = null;
      callJoined = false;
      profile = null;
      callBusy = false;
      mobileNavigationOpen = false;
      loadingEarlier = false;
      hasLater = false;
      loadingLater = false;
      lastTypingAt = 0;
      hasEarlier = true;
      authorityHistoryComplete = false;
      pendingSends.clear();
      deliveryRecoveries.clear();
      readAcknowledgements.reset();
      void load(targetRef, routeGeneration, snapshot, buffered, false, callRevision, targetAround);
    });
  });

  function recoverCurrentRoute() {
    const targetRef = dmId;
    const targetAround = aroundMessage;
    const routeGeneration = loadGeneration;
    const snapshot = ++snapshotGeneration;
    const buffered: Dispatch[] = [];
    const startingCallRevision = callRevision;
    dispatchBuffer = buffered;
    void load(
      targetRef,
      routeGeneration,
      snapshot,
      buffered,
      true,
      startingCallRevision,
      targetAround
    );
  }

  async function load(
    targetRef: string,
    routeGeneration: number,
    snapshot: number,
    buffered: Dispatch[],
    preserveMessages: boolean,
    startingCallRevision: number,
    targetAround: string | null
  ) {
    try {
      const [
        loadedDms,
        loadedGuilds,
        loadedMessages,
        loadedReadStates,
        loadedCurrentUser,
        loadedCall,
        loadedEmojis,
        loadedPins
      ] = await Promise.all([
        api<Channel[]>('/users/@me/channels'),
        api<Guild[]>('/users/@me/guilds'),
        api<Message[]>(
          `/channels/${encodeURIComponent(targetRef)}/messages${targetAround ? `?around=${encodeURIComponent(targetAround)}` : ''}`
        ),
        api<ReadStateStatus[]>('/users/@me/read-states'),
        api<UserSummary>('/users/@me'),
        api<ActiveCallState>(`/channels/${encodeURIComponent(targetRef)}/calls/active`).catch(
          () => ({
            call: null,
            joined: false
          })
        ),
        api<CustomEmoji[]>('/users/@me/emojis'),
        api<Message[]>(`/channels/${encodeURIComponent(targetRef)}/pins`).catch(() => [])
      ]);
      if (
        routeGeneration !== loadGeneration ||
        snapshot !== snapshotGeneration ||
        targetRef !== dmId
      )
        return;
      setDirectMessages(loadedDms);
      setGuilds(loadedGuilds);
      setReadStates(loadedReadStates);
      availableEmojis = loadedEmojis;
      pinnedMessages = loadedPins;
      entities.ingestCurrentUser(loadedCurrentUser);
      const preferredPresence = myPresencePreference();
      presencePreference = preferredPresence;
      entities.setPresence(
        loadedCurrentUser,
        preferredPresence === 'idle' || preferredPresence === 'dnd'
          ? preferredPresence
          : preferredPresence === 'invisible'
            ? 'offline'
            : 'online'
      );
      const nextCall =
        loadedCall.call &&
        matchesEntityRef(
          targetRef,
          { id: loadedCall.call.channel_id, origin_domain: loadedCall.call.channel_domain },
          localDomain
        )
          ? loadedCall.call
          : null;
      if (callRevision === startingCallRevision) {
        const callChanged =
          activeCall?.id !== nextCall?.id ||
          activeCall?.authority_domain !== nextCall?.authority_domain ||
          activeCall?.state !== nextCall?.state ||
          callJoined !== (nextCall ? loadedCall.joined : false);
        if (callChanged) callRevision += 1;
        activeCall = nextCall;
        callJoined = nextCall ? loadedCall.joined : false;
      }
      const loadedChannel =
        loadedDms.find((item) => matchesEntityRef(targetRef, item, localDomain)) ?? null;
      if (loadedChannel?.encryption_mode !== 'e2ee') {
        void initializeE2EE(loadedCurrentUser)
          .then((client) => {
            if (routeGeneration === loadGeneration) e2eeClient = client;
          })
          .catch(() => {
            // Plaintext conversations remain usable when secure device storage is unavailable.
          });
      }
      const oldestLoaded = loadedMessages.at(-1);
      authorityHistoryComplete =
        (preserveMessages && authorityHistoryComplete) ||
        oldestLoaded?.history_page_complete === true ||
        (loadedMessages.length === 0 &&
          loadedChannel?.history_truncated === true &&
          loadedChannel.history_remote_available === true);
      if (oldestLoaded?.history_page_error_code === 'FEDERATED_DM_HISTORY_UNAVAILABLE') {
        error =
          'Older messages are temporarily unavailable from this conversation’s home instance. Recent cached messages are still shown; retry in a moment.';
      }
      hasEarlier =
        (targetAround
          ? loadedMessages.length > 0
          : loadedMessages.length === 50 ||
            oldestLoaded?.history_page_error_code === 'FEDERATED_DM_HISTORY_UNAVAILABLE') &&
        !oldestLoaded?.history_page_complete &&
        !reachesRetainedHistoryStart(loadedChannel, oldestLoaded);
      hasLater = Boolean(targetAround && loadedMessages.length > 0);
      let orderedMessages = loadedMessages.reverse().sort(compareMessages);
      if (loadedChannel?.encryption_mode === 'e2ee') {
        const client = await initializeE2EE(loadedCurrentUser);
        if (routeGeneration !== loadGeneration || targetRef !== dmId) return;
        e2eeClient = client;
        orderedMessages = await decryptConversationMessages(client, loadedChannel, orderedMessages);
        pinnedMessages = await decryptConversationMessages(client, loadedChannel, loadedPins);
        e2eeSafetyNumber = await client.safetyNumber(loadedChannel).catch(() => '');
      } else {
        e2eeClient = null;
        e2eeSafetyNumber = '';
      }
      setMessages(
        preserveMessages
          ? mergeMessageSnapshot(messages, orderedMessages, {
              authoritative: true,
              complete: loadedMessages.length < 50,
              preserveNonces: new Set(pendingSends.keys())
            })
          : orderedMessages
      );
      for (const dispatch of buffered) applyDispatch(dispatch);
      forgetConfirmedSends();
      if (dispatchBuffer === buffered) dispatchBuffer = null;
      channelReady = true;
      acknowledgeLatestIfVisible();
    } catch (caught) {
      if (
        routeGeneration !== loadGeneration ||
        snapshot !== snapshotGeneration ||
        targetRef !== dmId
      )
        return;
      for (const dispatch of buffered) applyDispatch(dispatch);
      forgetConfirmedSends();
      if (dispatchBuffer === buffered) dispatchBuffer = null;
      if (!preserveMessages) {
        error = userErrorMessage(caught, 'Could not open this conversation. Try again.');
      } else if (!error) {
        error = 'Live updates resumed, but conversation state could not be refreshed.';
      }
    }
  }

  async function loadEarlier() {
    const generation = loadGeneration;
    const targetRef = dmId;
    const oldest = messages[0];
    if (!oldest || loadingEarlier || !hasEarlier || messages.length >= 1_000) return;
    loadingEarlier = true;
    try {
      const older = await api<Message[]>(
        `/channels/${encodeURIComponent(targetRef)}/messages?before=${encodeURIComponent(entityRef(oldest))}`
      );
      if (generation !== loadGeneration || targetRef !== dmId) return;
      // A successful empty page is the unambiguous end of both sources: the
      // backend would have returned any remaining durable local rows and only
      // suppresses failures by returning a marked, non-empty cached page.
      const pageCompletesAuthorityHistory =
        older.length === 0 || older.at(-1)?.history_page_complete === true;
      const authorityHistoryError = older.at(-1)?.history_page_error_code;
      const available = Math.max(0, 1_000 - messages.length);
      let prepended = older.reverse().slice(-available);
      if (channel?.encryption_mode === 'e2ee' && e2eeClient)
        prepended = await decryptConversationMessages(e2eeClient, channel, prepended);
      const byKey = Object.create(null) as Record<string, Message>;
      for (const message of prepended) byKey[entityKey(message)] = message;
      for (const message of messages) byKey[entityKey(message)] = message;
      const combined = Object.values(byKey).sort(compareMessages);
      setMessages(combined);
      authorityHistoryComplete ||= pageCompletesAuthorityHistory;
      if (authorityHistoryError === 'FEDERATED_DM_HISTORY_UNAVAILABLE') {
        error =
          'Older messages are temporarily unavailable from this conversation’s home instance. Recent cached messages remain visible; retry in a moment.';
      } else if (error.startsWith('Older messages are temporarily unavailable')) {
        error = '';
      }
      hasEarlier =
        (older.length === 50 || authorityHistoryError === 'FEDERATED_DM_HISTORY_UNAVAILABLE') &&
        combined.length < 1_000 &&
        !pageCompletesAuthorityHistory &&
        !reachesRetainedHistoryStart(channel, combined[0]);
    } catch (caught) {
      if (generation !== loadGeneration || targetRef !== dmId) return;
      error = userErrorMessage(caught, 'Could not load earlier messages. Try again.');
    } finally {
      if (generation === loadGeneration && targetRef === dmId) loadingEarlier = false;
    }
  }

  async function loadLater() {
    const generation = loadGeneration;
    const targetRef = dmId;
    const newest = messages.at(-1);
    if (!newest || loadingLater || !hasLater) return;
    loadingLater = true;
    try {
      const newer = await api<Message[]>(
        `/channels/${encodeURIComponent(targetRef)}/messages?after=${encodeURIComponent(entityRef(newest))}`
      );
      if (generation !== loadGeneration || targetRef !== dmId) return;
      const byKey = Object.create(null) as Record<string, Message>;
      for (const message of messages) byKey[entityKey(message)] = message;
      let decryptedNewer = newer.reverse();
      if (channel?.encryption_mode === 'e2ee' && e2eeClient)
        decryptedNewer = await decryptConversationMessages(e2eeClient, channel, decryptedNewer);
      for (const message of decryptedNewer) byKey[entityKey(message)] = message;
      setMessages(Object.values(byKey).sort(compareMessages).slice(-1_000));
      hasLater = newer.length === 50;
    } catch (caught) {
      if (generation !== loadGeneration || targetRef !== dmId) return;
      error = userErrorMessage(caught, 'Could not load newer messages. Try again.');
    } finally {
      if (generation === loadGeneration && targetRef === dmId) loadingLater = false;
    }
  }

  function markMessageAcknowledged(message: Message) {
    const targetChannel = {
      id: message.channel_id,
      origin_domain: message.channel_domain
    };
    setReadStates(
      readStates.map((state) =>
        state.channel_id === targetChannel.id &&
        state.channel_domain === targetChannel.origin_domain
          ? state.read_message_id !== null &&
            state.read_message_domain !== null &&
            compareEntityRefs(message, {
              id: state.read_message_id,
              origin_domain: state.read_message_domain
            }) < 0
            ? state
            : {
                ...state,
                read_message_id: message.id,
                read_message_domain: message.origin_domain,
                mention_count: 0,
                unread: false
              }
          : state
      )
    );
  }

  function acknowledge(message: Message): Promise<void> {
    return readAcknowledgements.acknowledge(message);
  }

  function reconcile(message: Message) {
    if (message.client_nonce && !dispatchBuffer) pendingSends.delete(message.client_nonce);
    setMessages(reconcileMessage(messages, message));
  }

  function forgetConfirmedSends() {
    for (const message of messages) {
      if (message.client_nonce && !message.id.startsWith('pending-')) {
        pendingSends.delete(message.client_nonce);
      }
    }
  }

  function clearSubmittedUploads(attachmentIds: readonly string[]) {
    uploads = withoutSubmittedUploads(uploads, attachmentIds);
  }

  async function recoverDeliveryUpdate(update: MessageDeliveryUpdate) {
    const routeGeneration = loadGeneration;
    const routeRef = dmId;
    const recoveryKey = `${routeGeneration}:${update.message_domain}:${update.message_id}`;
    if (deliveryRecoveries.has(recoveryKey)) return;
    deliveryRecoveries.add(recoveryKey);
    try {
      const messageRef = entityRef({
        id: update.message_id,
        origin_domain: update.message_domain
      });
      const channelRef = entityRef({ id: update.channel_id, origin_domain: update.channel_domain });
      const recovered = await api<Message[]>(
        `/channels/${encodeURIComponent(channelRef)}/messages?around=${encodeURIComponent(messageRef)}&limit=5`
      );
      if (routeGeneration !== loadGeneration || routeRef !== dmId) return;
      for (const message of recovered) {
        if (message.client_nonce) pendingSends.delete(message.client_nonce);
      }
      const merged = mergeMessageSnapshot(messages, recovered);
      setMessages(applyMessageDeliveryUpdate(merged, update).messages);
    } catch {
      // A later history refresh reconstructs delivery state from the durable outbox.
    } finally {
      deliveryRecoveries.delete(recoveryKey);
    }
  }

  function acknowledgeLatestIfVisible() {
    if (document.visibilityState !== 'visible' || !timelineAtBottom) return;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (!message.id.startsWith('pending-')) {
        void acknowledge(message);
        return;
      }
    }
  }

  async function send(retry?: PendingMessageSend) {
    const text = content.trim();
    if (editingMessage && !retry) {
      if (!text || busy) return;
      const editing = editingMessage;
      const generation = loadGeneration;
      busy = true;
      try {
        const encrypted =
          channel?.encryption_mode === 'e2ee'
            ? await (
                e2eeClient ?? (currentUser ? await initializeE2EE(currentUser) : null)
              )?.encryptMessage(channel, text, {
                operation: 'edit',
                targetMessage: entityRef(editing),
                attachments: editing.decrypted_attachments ?? []
              })
            : null;
        if (channel?.encryption_mode === 'e2ee' && !encrypted)
          throw new Error('Encryption is unavailable on this device.');
        const saved = await api<Message>(
          `/channels/${encodeURIComponent(dmId)}/messages/${encodeURIComponent(entityRef(editing))}`,
          {
            method: 'PATCH',
            body: JSON.stringify(encrypted ? { e2ee: encrypted } : { content: text })
          }
        );
        if (generation !== loadGeneration) return;
        reconcile(
          encrypted
            ? {
                ...saved,
                decrypted_content: text,
                decrypted_attachments: editing.decrypted_attachments ?? []
              }
            : saved
        );
        finishEditing();
      } catch (caught) {
        if (generation === loadGeneration)
          error = userErrorMessage(caught, 'Could not edit the message. Try again.');
      } finally {
        if (generation === loadGeneration) busy = false;
      }
      return;
    }
    if (busy || !channelReady || !channel) return;
    const attachmentIds = retry
      ? retry.attachmentIds
      : uploads
          .filter((item) => item.status === 'ready' && item.attachmentId)
          .map((item) => item.attachmentId as string);
    if (!retry && uploads.some((item) => item.status === 'uploading')) return;
    const mentionUserIds = retry
      ? retry.mentionUserIds
      : recipient && mentionsUser(text, recipient, localDomain)
        ? [entityRef(recipient)]
        : [];
    if (!retry && !text && !attachmentIds.length) return;
    const draft =
      retry ??
      pendingMessageSend(
        text || null,
        attachmentIds,
        mentionUserIds,
        crypto.randomUUID(),
        replyingMessage ? entityRef(replyingMessage) : null,
        uploads.flatMap((item) => (item.encryptedManifest ? [item.encryptedManifest] : []))
      );
    if (!draft.content && !draft.attachmentIds.length) {
      error = 'Reattach this message’s files before retrying.';
      return;
    }
    const generation = loadGeneration;
    const routeRef = dmId;
    const targetRef = channel ? entityRef(channel) : dmId;
    const nonce = draft.clientNonce;
    error = '';
    pendingSends.set(nonce, draft);
    const existing = messages.find((message) => message.client_nonce === nonce);
    const optimistic: Message = existing
      ? { ...existing, pending: true, queued: false, failed: false }
      : {
          id: `pending-${nonce}`,
          origin_domain: '',
          channel_id: channel.id,
          channel_domain: channel.origin_domain,
          author_id: 'me',
          author_domain: currentUser?.origin_domain ?? localDomain,
          author: currentUser,
          content: draft.content,
          decrypted_content: channel.encryption_mode === 'e2ee' ? draft.content : undefined,
          decrypted_attachments:
            channel.encryption_mode === 'e2ee' ? draft.encryptedAttachments : undefined,
          message_type: 0,
          flags: 0,
          client_nonce: nonce,
          referenced_message_id:
            messages.find((item) =>
              draft.referencedMessageId
                ? matchesEntityRef(draft.referencedMessageId, item, localDomain)
                : false
            )?.id ?? null,
          referenced_message_domain:
            messages.find((item) =>
              draft.referencedMessageId
                ? matchesEntityRef(draft.referencedMessageId, item, localDomain)
                : false
            )?.origin_domain ?? null,
          mention_user_refs: [],
          edited_at: null,
          deleted_at: null,
          created_at: new Date().toISOString(),
          pending: true
        };
    setMessages(
      existing
        ? messages.map((message) => (message.client_nonce === nonce ? optimistic : message))
        : [...messages, optimistic].slice(-250)
    );
    if (!retry) {
      content = '';
      composerCursor = 0;
      replyingMessage = null;
    }
    busy = true;
    try {
      const encrypted =
        channel.encryption_mode === 'e2ee'
          ? await (
              e2eeClient ?? (currentUser ? await initializeE2EE(currentUser) : null)
            )?.encryptMessage(channel, draft.content ?? '', {
              attachments: draft.encryptedAttachments
            })
          : null;
      if (channel.encryption_mode === 'e2ee' && !encrypted)
        throw new Error('Encryption is unavailable on this device.');
      const saved = await api<Message>(`/channels/${encodeURIComponent(targetRef)}/messages`, {
        method: 'POST',
        body: JSON.stringify({
          content: encrypted ? null : draft.content,
          e2ee: encrypted,
          client_nonce: nonce,
          attachment_ids: draft.attachmentIds,
          mention_user_ids: draft.mentionUserIds,
          referenced_message_id: draft.referencedMessageId
        })
      });
      if (generation !== loadGeneration || routeRef !== dmId) return;
      reconcile(
        encrypted
          ? {
              ...saved,
              decrypted_content: draft.content,
              decrypted_attachments: draft.encryptedAttachments
            }
          : saved
      );
      clearSubmittedUploads(draft.attachmentIds);
      await acknowledge(saved);
    } catch (caught) {
      if (generation !== loadGeneration || routeRef !== dmId) return;
      const stillPending = messages.some((item) => item.client_nonce === nonce && item.pending);
      setMessages(failPendingMessage(messages, nonce));
      if (stillPending) {
        if (caught instanceof ApiError && caught.code === 'ATTACHMENT_ALREADY_USED') {
          pendingSends.set(nonce, discardAttachments(draft));
          clearSubmittedUploads(draft.attachmentIds);
          error =
            'Those files were already used by another message. Reattach them before retrying.';
        } else {
          error = userErrorMessage(caught, 'Could not send the message. Try again.');
        }
      }
    } finally {
      if (generation === loadGeneration && routeRef === dmId) busy = false;
    }
  }

  async function queueFiles(files: FileList | File[]) {
    if (!channel || busy || uploads.length >= 10) return;
    const target = entityRef(channel);
    const generation = loadGeneration;
    const routeRef = dmId;
    for (const file of Array.from(files).slice(0, 10 - uploads.length)) {
      const key = crypto.randomUUID();
      const controller = new AbortController();
      uploadControllers.set(key, controller);
      uploads = [...uploads, { key, file, progress: 0, status: 'uploading' }];
      const upload =
        channel.encryption_mode === 'e2ee' ? uploadEncryptedChannelFile : uploadChannelFile;
      void upload(
        target,
        file,
        (progress) => {
          if (controller.signal.aborted || generation !== loadGeneration || routeRef !== dmId)
            return;
          uploads = uploads.map((item) => (item.key === key ? { ...item, progress } : item));
        },
        controller.signal
      )
        .then((ticket) => {
          uploadControllers.delete(key);
          if (generation !== loadGeneration || routeRef !== dmId) return;
          uploads = uploads.map((item) =>
            item.key === key
              ? {
                  ...item,
                  progress: 100,
                  status: 'ready',
                  attachmentId: 'ticket' in ticket ? ticket.ticket.id : ticket.id,
                  encryptedManifest: 'manifest' in ticket ? ticket.manifest : undefined
                }
              : item
          );
        })
        .catch((caught: unknown) => {
          uploadControllers.delete(key);
          if (controller.signal.aborted || generation !== loadGeneration || routeRef !== dmId)
            return;
          uploads = uploads.map((item) =>
            item.key === key
              ? {
                  ...item,
                  status: 'failed',
                  error: userErrorMessage(caught, 'Upload failed. Remove the file and try again.')
                }
              : item
          );
        });
    }
  }

  function removeUpload(key: string) {
    uploadControllers.get(key)?.abort();
    uploadControllers.delete(key);
    uploads = uploads.filter((item) => item.key !== key);
  }

  function resetUploads() {
    for (const controller of uploadControllers.values()) controller.abort();
    uploadControllers.clear();
    uploads = [];
  }

  async function startCall() {
    if (!channel || activeCall || callBusy) return;
    const generation = loadGeneration;
    const routeRef = dmId;
    const targetChannel = entityRef(channel);
    const revision = callRevision;
    callBusy = true;
    error = '';
    try {
      const created = await api<CallState>(`/channels/${encodeURIComponent(targetChannel)}/calls`, {
        method: 'POST',
        body: JSON.stringify({ ring: true })
      });
      if (
        generation !== loadGeneration ||
        routeRef !== dmId ||
        revision !== callRevision ||
        !isCurrentChannel(created.channel_id, created.channel_domain)
      )
        return;
      activeCall = created;
      callJoined = true;
      callRevision += 1;
    } catch (caught) {
      if (
        generation === loadGeneration &&
        routeRef === dmId &&
        revision === callRevision &&
        !activeCall
      )
        error = userErrorMessage(caught, 'Could not start the call. Try again.');
    } finally {
      if (generation === loadGeneration && routeRef === dmId) callBusy = false;
    }
  }

  async function callAction(action: 'accept' | 'decline' | 'end') {
    if (!activeCall || callBusy) return;
    const generation = loadGeneration;
    const routeRef = dmId;
    const selected = activeCall;
    const selectedRef = entityRef({ id: selected.id, origin_domain: selected.authority_domain });
    const revision = callRevision;
    callBusy = true;
    error = '';
    try {
      const updated = await api<CallState>(`/calls/${encodeURIComponent(selectedRef)}`, {
        method: 'POST',
        body: JSON.stringify({ action })
      });
      if (
        generation !== loadGeneration ||
        routeRef !== dmId ||
        revision !== callRevision ||
        activeCall?.id !== selected.id ||
        activeCall.authority_domain !== selected.authority_domain
      )
        return;
      if (action === 'accept') {
        activeCall = updated;
        callJoined = true;
      } else {
        activeCall = null;
        callJoined = false;
      }
      callRevision += 1;
    } catch (caught) {
      if (generation === loadGeneration && routeRef === dmId && revision === callRevision)
        error = userErrorMessage(caught, 'Could not update the call. Try again.');
    } finally {
      if (generation === loadGeneration && routeRef === dmId) callBusy = false;
    }
  }

  function openGroupSettings() {
    if (!channel || !groupConversation) return;
    groupName = channel.name ?? '';
    groupInviteHandle = '';
    groupError = '';
    groupDialog?.showModal();
  }

  async function updateGroupName() {
    if (!channel || groupBusy) return;
    groupBusy = true;
    groupError = '';
    try {
      const updated = await api<Channel>(
        `/users/@me/channels/${encodeURIComponent(entityRef(channel))}/group`,
        {
          method: 'PATCH',
          body: JSON.stringify({ name: groupName.trim() || null })
        }
      );
      entities.channels.upsert(updated);
    } catch (caught) {
      groupError = userErrorMessage(caught, 'Could not rename this group. Try again.');
    } finally {
      groupBusy = false;
    }
  }

  async function addGroupMember() {
    if (!channel || groupBusy || !groupInviteHandle.trim()) return;
    groupBusy = true;
    groupError = '';
    try {
      const updated = await api<Channel>(
        `/users/@me/channels/${encodeURIComponent(entityRef(channel))}/group/recipients`,
        {
          method: 'POST',
          body: JSON.stringify({ handle: groupInviteHandle.trim() })
        }
      );
      entities.channels.upsert(updated);
      if (updated.encryption_state === 'rekeying' && currentUser) {
        const client = e2eeClient ?? (await initializeE2EE(currentUser));
        const secured = await client.rekeyRoom(entityRef(updated));
        entities.channels.upsert(secured);
      }
      groupInviteHandle = '';
    } catch (caught) {
      groupError = userErrorMessage(
        caught,
        'Could not add that friend. They must be friends with the inviter and able to join.'
      );
    } finally {
      groupBusy = false;
    }
  }

  async function removeGroupMember(user: UserSummary) {
    if (!channel || groupBusy) return;
    groupBusy = true;
    groupError = '';
    try {
      const updated = await api<Channel>(
        `/users/@me/channels/${encodeURIComponent(entityRef(channel))}/group/recipients/${encodeURIComponent(entityRef(user))}`,
        { method: 'DELETE' }
      );
      entities.channels.upsert(updated);
      if (updated.encryption_state === 'rekeying' && currentUser) {
        const client = e2eeClient ?? (await initializeE2EE(currentUser));
        const secured = await client.rekeyRoom(entityRef(updated));
        entities.channels.upsert(secured);
      }
    } catch (caught) {
      groupError = userErrorMessage(caught, 'Could not remove that member. Try again.');
    } finally {
      groupBusy = false;
    }
  }

  async function leaveGroup() {
    if (!channel || groupBusy) return;
    groupBusy = true;
    groupError = '';
    try {
      await api(`/users/@me/channels/${encodeURIComponent(entityRef(channel))}/group/leave`, {
        method: 'POST'
      });
      groupDialog?.close();
      window.location.assign(resolve('/home'));
    } catch (caught) {
      groupError = userErrorMessage(caught, 'Could not leave this group. Try again.');
      groupBusy = false;
    }
  }

  async function enableEncryption() {
    if (
      !channel ||
      !currentUser ||
      groupBusy ||
      channel.encryption_mode === 'e2ee' ||
      !e2eeActivationEnabled
    )
      return;
    const confirmed = window.confirm(
      'Turn on end-to-end encryption for this conversation? This cannot be turned off and protects only new content; existing history stays readable to the server. New messages, files, and supported calls will be encrypted. Search, link and GIF previews, bots, webhooks, file previews, malware scanning, call recording, and transcription will stop; unsupported clients cannot participate. Notifications become generic, while participants, timing, message-size, track, and traffic metadata remain visible. Anyone can still record content on their own device. Losing every enrolled device and recovery backup loses encrypted history. Removed members keep content they already received.'
    );
    if (!confirmed) return;
    groupBusy = true;
    groupError = '';
    try {
      const client = await initializeE2EE(currentUser);
      const proposal = await client.createRoomProposal(entityRef(channel));
      const updated = await client.activateRoom(entityRef(channel), proposal);
      e2eeClient = client;
      entities.channels.upsert(updated);
      e2eeSafetyNumber = await client.safetyNumber(updated);
    } catch (caught) {
      const message = userErrorMessage(
        caught,
        'Could not enable end-to-end encryption. Try again.'
      );
      if (groupConversation) groupError = message;
      else error = message;
    } finally {
      groupBusy = false;
    }
  }

  async function rekeyEncryption() {
    if (!channel || !currentUser || groupBusy || channel.encryption_state !== 'rekeying') return;
    groupBusy = true;
    groupError = '';
    try {
      const client = e2eeClient ?? (await initializeE2EE(currentUser));
      const updated = await client.rekeyRoom(entityRef(channel));
      e2eeClient = client;
      entities.channels.upsert(updated);
      e2eeSafetyNumber = await client.safetyNumber(updated);
    } catch (caught) {
      groupError = userErrorMessage(caught, 'Could not secure the updated member list. Try again.');
    } finally {
      groupBusy = false;
    }
  }

  function showEncryptionInfo() {
    if (!channel || channel.encryption_mode !== 'e2ee') return;
    window.alert(
      `End-to-end encryption is on. Compare this safety number with the other members using a separate trusted channel:\n\n${e2eeSafetyNumber || 'Safety number unavailable on this device.'}`
    );
  }

  function composerPaste(event: ClipboardEvent) {
    if (editingMessage) return;
    if (event.clipboardData?.files.length) void queueFiles(event.clipboardData.files);
  }

  function composerDrop(event: DragEvent) {
    event.preventDefault();
    if (editingMessage) return;
    if (event.dataTransfer?.files.length) void queueFiles(event.dataTransfer.files);
  }

  function announceTyping() {
    if (Date.now() - lastTypingAt < 8000) return;
    lastTypingAt = Date.now();
    const targetChannel = channel ? entityRef(channel) : dmId;
    void api(`/channels/${encodeURIComponent(targetChannel)}/typing`, { method: 'POST' }).catch(
      () => undefined
    );
  }

  function syncComposerCursor() {
    composerCursor = composerInput?.selectionStart ?? content.length;
  }

  function composerChanged() {
    syncComposerCursor();
    announceTyping();
  }

  function composerKeydown(event: KeyboardEvent) {
    if (autocomplete?.handleKeydown(event)) return;
    if (event.key === 'Escape' && editingMessage) {
      event.preventDefault();
      finishEditing();
      return;
    }
    if (event.key === 'ArrowUp' && !content && !event.shiftKey) {
      const own = messages.findLast(
        (message) =>
          !message.deleted_at &&
          !message.pending &&
          message.author_id === currentUser?.id &&
          message.author_domain === currentUser.origin_domain
      );
      if (own) {
        event.preventDefault();
        startEditing(own);
      }
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void send();
    }
  }

  function startEditing(message: Message) {
    replyingMessage = null;
    if (!editingMessage) {
      composerDraftBeforeEdit = { content, cursor: composerCursor };
    }
    editingMessage = message;
    content = message.decrypted_content ?? message.content ?? '';
    composerCursor = content.length;
    void tick().then(() => {
      composerInput?.focus();
      composerInput?.setSelectionRange(composerCursor, composerCursor);
    });
  }

  function startReply(message: Message) {
    if (editingMessage) finishEditing();
    replyingMessage = message;
    void tick().then(() => composerInput?.focus());
  }

  function cancelReply() {
    replyingMessage = null;
    void tick().then(() => composerInput?.focus());
  }

  async function loadPins() {
    if (!channel) return;
    pinsLoading = true;
    pinsError = '';
    try {
      pinnedMessages = await api<Message[]>(
        `/channels/${encodeURIComponent(entityRef(channel))}/pins`
      );
    } catch (caught) {
      pinsError = userErrorMessage(
        caught,
        'Could not load pinned messages. Close this panel and try again.'
      );
    } finally {
      pinsLoading = false;
    }
  }

  function togglePins() {
    pinsOpen = !pinsOpen;
    if (pinsOpen) void loadPins();
  }

  async function togglePinnedMessage(message: Message, shouldPin: boolean) {
    if (!channel) return;
    try {
      await api(
        `/channels/${encodeURIComponent(entityRef(channel))}/pins/${encodeURIComponent(entityRef(message))}`,
        { method: shouldPin ? 'PUT' : 'DELETE' }
      );
      pinnedMessages = shouldPin
        ? [message, ...pinnedMessages.filter((item) => entityKey(item) !== entityKey(message))]
        : pinnedMessages.filter((item) => entityKey(item) !== entityKey(message));
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the pinned message. Try again.');
    }
  }

  async function toggleMessageReaction(message: Message, emoji: string, remove: boolean) {
    if (!channel) return;
    const channelRef = encodeURIComponent(entityRef(channel));
    const messageRef = encodeURIComponent(entityRef(message));
    try {
      await api(
        `/channels/${channelRef}/messages/${messageRef}/reactions${remove ? `/${encodeURIComponent(emoji)}` : ''}`,
        remove ? { method: 'DELETE' } : { method: 'POST', body: JSON.stringify({ emoji }) }
      );
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update that reaction. Try again.');
    }
  }

  function jumpToPinnedMessage(message: Message) {
    pinsOpen = false;
    jumpToMessageReference(entityRef(message));
  }

  function jumpToMessageReference(reference: string | Message) {
    const target = typeof reference === 'string' ? reference : entityRef(reference);
    const element = document.getElementById(`message-${target}`);
    if (element) {
      element.scrollIntoView({ block: 'center', behavior: 'smooth' });
      return;
    }
    const url = new URL(window.location.href);
    url.searchParams.set('around', target);
    window.location.assign(url);
  }

  function jumpToReply(message: Message) {
    if (!message.referenced_message_id || !message.referenced_message_domain) return;
    jumpToMessageReference(`${message.referenced_message_id}@${message.referenced_message_domain}`);
  }

  function finishEditing() {
    const draft = composerDraftBeforeEdit;
    editingMessage = null;
    composerDraftBeforeEdit = null;
    content = draft?.content ?? '';
    composerCursor = Math.min(draft?.cursor ?? content.length, content.length);
    void tick().then(() => {
      composerInput?.focus();
      composerInput?.setSelectionRange(composerCursor, composerCursor);
    });
  }

  async function deleteMessage(message: Message) {
    const generation = loadGeneration;
    const routeChannel = dmId;
    try {
      await api(
        `/channels/${encodeURIComponent(routeChannel)}/messages/${encodeURIComponent(entityRef(message))}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration || routeChannel !== dmId) return;
      setMessages(
        messages.map((item) =>
          entityKey(item) === entityKey(message)
            ? { ...item, content: null, deleted_at: new Date().toISOString() }
            : item
        )
      );
      if (editingMessage && entityKey(editingMessage) === entityKey(message)) finishEditing();
    } catch (caught) {
      if (generation !== loadGeneration || routeChannel !== dmId) return;
      error = userErrorMessage(caught, 'Could not delete the message. Try again.');
    }
  }

  function retryMessage(message: Message) {
    editingMessage = null;
    composerDraftBeforeEdit = null;
    if (message.delivery_status === 'failed') {
      if (message.attachments?.length || !message.content) {
        content = message.content ?? '';
        composerCursor = content.length;
        error = 'Reattach this message’s files before retrying.';
        void tick().then(() => composerInput?.focus());
        return;
      }
      void send(
        pendingMessageSend(
          message.content,
          [],
          message.mention_user_refs.map((reference) => entityRef(reference)),
          crypto.randomUUID(),
          message.referenced_message_id && message.referenced_message_domain
            ? `${message.referenced_message_id}@${message.referenced_message_domain}`
            : null
        )
      );
      return;
    }
    let draft = message.client_nonce ? pendingSends.get(message.client_nonce) : undefined;
    if (draft && !draft.attachmentIds.length) {
      const replacements = uploads
        .filter((upload) => upload.status === 'ready' && upload.attachmentId)
        .map((upload) => upload.attachmentId as string);
      if (replacements.length) {
        draft = pendingMessageSend(
          draft.content,
          replacements,
          draft.mentionUserIds,
          draft.clientNonce,
          draft.referencedMessageId
        );
        pendingSends.set(draft.clientNonce, draft);
      }
    }
    if (!draft) {
      content = message.content ?? '';
      composerCursor = content.length;
      if (!content) error = 'Reattach this message’s files before retrying.';
      void tick().then(() => composerInput?.focus());
      return;
    }
    void send(draft);
  }

  function chooseCompletion(completion: Completion) {
    if (!completionQuery) return;
    const cursor = completionQuery.start + completion.value.length + 1;
    content = replaceCompletion(content, completionQuery, completion.value);
    composerCursor = cursor;
    void tick().then(() => {
      composerInput?.focus();
      composerInput?.setSelectionRange(cursor, cursor);
    });
  }

  function timelineBottomChanged(value: boolean) {
    timelineAtBottom = value;
    if (value) acknowledgeLatestIfVisible();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- authenticated media URLs are API resources, not Svelte routes -->

<svelte:head><title>{conversationTitle} · Kaede Chat</title></svelte:head>
<svelte:window
  onkeydown={(event) => {
    if (mobileNavigationOpen) mobileNavigationKeydown(event);
  }}
/>

{#if mobileNavigationOpen}
  <button
    class="mobile-sidebar-backdrop"
    type="button"
    aria-label="Close direct-message navigation"
    onclick={() => closeMobileNavigation()}
  ></button>
{/if}

<main class="chat-app">
  <GuildRail
    {guilds}
    homeHref={resolve('/home')}
    homeActive
    {homeUnreadCount}
    guildHref={guildLandingPath}
    mentionCount={(item) => guildMentionCount(readStates, item)}
  />
  <aside
    bind:this={mobileNavigationDrawer}
    class:mobile-open={mobileNavigationOpen}
    class="channel-sidebar"
    id="direct-message-navigation"
    role={mobileNavigationOpen ? 'dialog' : undefined}
    aria-modal={mobileNavigationOpen ? 'true' : undefined}
    aria-label="Direct-message navigation"
  >
    <header>
      <div class="sidebar-heading">
        <div>
          <p>Messages</p>
          <h2>Direct threads</h2>
        </div>
        <div class="mobile-sidebar-tools">
          <a
            class="sidebar-settings"
            href={resolve('/settings')}
            aria-label="User settings"
            title="User settings"
            onclick={() => closeMobileNavigation(false)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Zm8 3.5-.1-1.2 2-1.5-2-3.4-2.4 1a8.8 8.8 0 0 0-2.1-1.2L15 3h-4l-.4 2.7c-.8.3-1.5.7-2.1 1.2l-2.4-1-2 3.4 2 1.5A9.7 9.7 0 0 0 6 12l.1 1.2-2 1.5 2 3.4 2.4-1c.6.5 1.3.9 2.1 1.2L11 21h4l.4-2.7c.8-.3 1.5-.7 2.1-1.2l2.4 1 2-3.4-2-1.5.1-1.2Z"
              />
            </svg>
          </a>
          <button
            bind:this={mobileNavigationClose}
            class="mobile-sidebar-close"
            type="button"
            aria-label="Close direct-message navigation"
            onclick={() => closeMobileNavigation()}
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              aria-hidden="true"
            >
              <path d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
      </div>
    </header>
    <div class="sidebar-section-heading">
      <p class="sidebar-section-label">Conversations</p>
      <button
        class="sidebar-create-link"
        type="button"
        aria-label="New message"
        title="New message"
        onclick={() => {
          closeMobileNavigation(false);
          newMessageOpen = true;
        }}>+</button
      >
    </div>
    <nav aria-label="Direct messages">
      {#each directMessages as item (entityKey(item))}
        {@const itemRecipient = item.recipients?.[0]}
        <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- dmPath calls resolve before substituting the typed parameter -->
        <a
          class:active={matchesEntityRef(dmId, item, localDomain)}
          href={directMessagePath(item)}
          aria-current={matchesEntityRef(dmId, item, localDomain) ? 'page' : undefined}
          onclick={() => closeMobileNavigation(false)}
        >
          <span class="avatar avatar-small">
            {#if isGroupDm(item)}
              <Icon name="users" size={16} />
            {:else if itemRecipient?.avatar_hash}
              <img
                src={assetUrl(itemRecipient.avatar_hash, 'thumbnail_128', itemRecipient)}
                alt=""
              />
            {:else}
              {itemRecipient?.profile_resolved === false
                ? '•'
                : (itemRecipient?.username.slice(0, 1).toUpperCase() ?? '?')}
            {/if}
          </span>
          <span>
            {dmTitle(item)}
          </span>
          {#if unreadFor(item)?.unread}<small class="unread-badge"
              >{Math.max(1, unreadFor(item)?.mention_count ?? 0)}</small
            >{/if}
        </a>
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
      <div class="sidebar-user-identity">
        <strong>{currentUser?.display_name ?? currentUser?.username ?? 'Your account'}</strong>
        <PresencePicker value={presencePreference} onChange={setMyPresence} />
      </div>
      <a class="icon-button" href={resolve('/settings')} aria-label="User settings">
        <Icon name="settings" size={18} />
      </a>
    </div>
  </aside>
  <section class="message-pane dm-message-pane">
    <header class="channel-header">
      <div class="channel-header-primary">
        <button
          bind:this={mobileNavigationToggle}
          class="mobile-sidebar-toggle"
          type="button"
          aria-label={mobileNavigationOpen
            ? 'Close direct-message navigation'
            : 'Open direct-message navigation'}
          aria-controls="direct-message-navigation"
          aria-expanded={mobileNavigationOpen}
          onclick={toggleMobileNavigation}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <path d="M4 7h16M4 12h16M4 17h16" />
          </svg>
        </button>
        <div class="channel-title">
          <span class="channel-mark direct" aria-hidden="true">
            {groupConversation ? '✦' : '@'}
          </span>
          <div>
            {#if groupConversation}
              <strong>{conversationTitle}</strong>
            {:else if recipient}
              <button
                class="profile-title-button"
                type="button"
                onclick={(event) => openProfile(recipient, event)}
                >{userDisplayName(recipient)}</button
              >
            {:else}
              <strong>Conversation</strong>
            {/if}
            {#if groupConversation && channel}
              <span>{groupDmSubtitle(channel)}</span>
            {:else if recipient}
              <span
                >{recipient.profile_resolved === false
                  ? 'Profile unavailable'
                  : recipient.handle}</span
              >
            {/if}
          </div>
        </div>
      </div>
      <div class="channel-header-actions">
        {#if !groupConversation && channel && (channel.encryption_mode === 'e2ee' || e2eeActivationEnabled)}
          <button
            class:active={channel.encryption_mode === 'e2ee'}
            class="icon-button"
            type="button"
            disabled={groupBusy}
            aria-label={channel.encryption_mode === 'e2ee'
              ? 'End-to-end encryption is on'
              : 'Turn on end-to-end encryption'}
            title={channel.encryption_mode === 'e2ee'
              ? 'End-to-end encrypted'
              : 'Turn on end-to-end encryption'}
            onclick={channel.encryption_mode === 'e2ee' ? showEncryptionInfo : enableEncryption}
          >
            <Icon name="lock" size={18} />
          </button>
        {/if}
        {#if groupConversation}
          <button
            class="icon-button"
            type="button"
            aria-label="Group settings"
            title="Group settings"
            onclick={openGroupSettings}
          >
            <Icon name="users" size={19} />
          </button>
        {/if}
        <button
          class:active={pinsOpen}
          class="icon-button"
          type="button"
          aria-label={pinsOpen ? 'Hide pinned messages' : 'Show pinned messages'}
          aria-pressed={pinsOpen}
          title="Pinned messages"
          onclick={togglePins}>📌</button
        >
        <MessageSearch
          bind:open={messageSearchOpen}
          scope="channel"
          scopeRef={channel ? entityRef(channel) : dmId}
          accountRef={currentUser ? entityRef(currentUser) : null}
          {channel}
          users={[...(currentUser ? [currentUser] : []), ...(channel?.recipients ?? [])]}
          placement="header"
        />
        {#if !activeCall}
          <button class="call-button" onclick={startCall} disabled={!channelReady || callBusy}
            >Start call</button
          >
        {/if}
      </div>
    </header>
    {#if readStateWarning}
      <div class="read-state-warning" role="status">
        <span>{readStateWarning}</span>
        <button type="button" onclick={() => void readAcknowledgements.retryNow()}>Retry now</button
        >
      </div>
    {/if}
    <div
      class:has-active-call={Boolean(activeCall && callJoined)}
      class:has-ringing-call={Boolean(activeCall && !callJoined)}
      class="dm-conversation-layout"
    >
      {#if activeCall && !callJoined}
        <div class="call-ringing dm-call-region" role="status">
          <strong
            >{groupConversation ? 'Group call incoming' : `${conversationTitle} is calling`}</strong
          >
          <button disabled={callBusy} onclick={() => callAction('accept')}>Accept</button>
          <button disabled={callBusy} class="decline" onclick={() => callAction('decline')}
            >Decline</button
          >
        </div>
      {:else if activeCall && callJoined}
        <div class="dm-call-stage dm-call-region dm-call-active">
          {#key activeCallRef}
            <VoiceDock
              callRef={activeCallRef}
              channelRef={channel ? entityRef(channel) : undefined}
            />
          {/key}
          <button class="end-call" disabled={callBusy} onclick={() => callAction('end')}
            >End call for everyone</button
          >
        </div>
      {/if}
      <div class="message-list" aria-live="polite" role="log" aria-label="Direct messages">
        {#if error}<p class="form-error message-error" role="alert">{error}</p>{/if}
        {#snippet emptyTimeline()}
          {#if channelReady && channel}
            {#if channel.history_truncated}
              <aside class="history-boundary" role="status">
                <strong>No recent messages are cached</strong>
                <span>
                  This instance keeps a rolling cache of this remote conversation. Older messages
                  are loaded securely from its home instance when available.
                </span>
              </aside>
            {:else}
              <section class="channel-welcome">
                <span class="welcome-mark direct" aria-hidden="true">@</span>
                <h2>{conversationTitle}</h2>
                <p>
                  This is the beginning of your {groupConversation ? 'group' : 'direct'} conversation.
                </p>
              </section>
            {/if}
          {/if}
        {/snippet}
        {#key dmId}
          <VirtualMessageList
            items={timeline}
            empty={emptyTimeline}
            {hasEarlier}
            {loadingEarlier}
            {hasLater}
            {loadingLater}
            onLoadEarlier={loadEarlier}
            onLoadLater={loadLater}
            targetKey={targetTimelineKey}
            onBottomChange={timelineBottomChanged}
            label="Direct messages"
          >
            {#snippet historyStart()}
              {#if channel?.history_truncated && !authorityHistoryComplete}
                <aside class="history-boundary" role="status">
                  <strong>Recent history starts here</strong>
                  <span>
                    This instance keeps a rolling cache of this remote conversation. Older messages
                    load on demand from its home instance; retry if that instance is temporarily
                    unavailable.
                  </span>
                </aside>
              {:else if authorityHistoryComplete}
                <aside class="history-boundary" role="status">
                  <strong>This is the beginning of your direct conversation.</strong>
                </aside>
              {/if}
            {/snippet}
            {#snippet renderItem(item)}
              {#if item.kind === 'day'}
                <div class="timeline-divider" role="separator"><span>{item.label}</span></div>
              {:else if item.kind === 'new'}
                <div class="timeline-divider new" role="separator"><span>{item.label}</span></div>
              {:else}
                <MessageRow
                  message={item.message}
                  compact={item.compact}
                  mentionUsers={entities.users.values}
                  referencedMessage={referencedMessage(item.message)}
                  pinned={pinnedMessages.some(
                    (pinned) => entityKey(pinned) === entityKey(item.message)
                  )}
                  presence={item.message.author
                    ? entities.presenceFor(item.message.author)
                    : 'offline'}
                  canEdit={item.message.author_id === currentUser?.id &&
                    item.message.author_domain === currentUser?.origin_domain}
                  onEdit={startEditing}
                  onDelete={deleteMessage}
                  onRetry={retryMessage}
                  onViewProfile={openMessageProfile}
                  onReply={startReply}
                  onJumpToReference={jumpToReply}
                  onTogglePin={togglePinnedMessage}
                  canReact
                  customEmojis={pickerEmojis}
                  reactionUserKey={currentUser ? entityKey(currentUser) : ''}
                  onToggleReaction={toggleMessageReaction}
                />
              {/if}
            {/snippet}
          </VirtualMessageList>
        {/key}
      </div>
    </div>
    <footer class="composer-wrap">
      <span class="typing-line">{typing}</span>
      {#if replyingMessage}
        <div class="reply-banner">
          <span>
            Replying to
            <strong>{userDisplayName(replyingMessage.author)}</strong>
          </span>
          <div class="reply-banner-actions">
            <button type="button" onclick={cancelReply} aria-label="Cancel reply">×</button>
          </div>
        </div>
      {/if}
      {#if editingMessage}
        <div class="editing-banner">
          <span>Editing message <small>Your draft and attachments are saved.</small></span>
          <button type="button" onclick={finishEditing}>Cancel</button>
        </div>
      {/if}
      <ComposerAutocomplete
        bind:this={autocomplete}
        query={completionQuery?.query ?? ''}
        options={completionOptions}
        listboxId="dm-message-suggestions"
        onActiveIndexChange={(index) => (completionActive = index)}
        onOpenChange={(open) => (completionOpen = open)}
        onSelect={chooseCompletion}
      />
      <form
        class="composer"
        ondragover={(event) => event.preventDefault()}
        ondrop={composerDrop}
        onsubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <input
          class="visually-hidden"
          bind:this={fileInput}
          type="file"
          multiple
          onchange={(event) => {
            const target = event.currentTarget;
            if (target.files) void queueFiles(target.files);
            target.value = '';
          }}
        />
        <button
          class="attach-button"
          type="button"
          disabled={busy || !channelReady || !channel || Boolean(editingMessage)}
          onclick={() => fileInput?.click()}
          aria-label="Attach files"
          title="Attach files"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m9.5 12.5 5.8-5.8a3 3 0 1 1 4.2 4.3l-8.2 8.1a5 5 0 0 1-7.1-7L12 4.3" />
          </svg>
        </button>
        <textarea
          use:autosizeTextarea={{ value: content, maxHeight: 180 }}
          bind:this={composerInput}
          bind:value={content}
          oninput={composerChanged}
          onselect={syncComposerCursor}
          onclick={syncComposerCursor}
          onkeyup={syncComposerCursor}
          onkeydown={composerKeydown}
          onpaste={composerPaste}
          disabled={!channelReady || !channel}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={completionOpen}
          aria-controls={completionOpen ? 'dm-message-suggestions' : undefined}
          aria-activedescendant={completionOpen
            ? `dm-message-suggestions-option-${completionActive}`
            : undefined}
          aria-label="Direct message"
          placeholder={`Message ${conversationTitle}`}
          rows="1"
          maxlength="4000"
        ></textarea>
        {#if (gifPickerEnabled || gifConfigurationError) && !editingMessage}
          <button
            class="gif-button"
            class:active={gifPickerOpen}
            type="button"
            disabled={busy || !channelReady || !channel || !gifPickerEnabled}
            aria-label={gifPickerEnabled ? 'Choose a GIF' : 'GIF availability could not be checked'}
            title={gifPickerEnabled ? 'Choose a GIF' : gifConfigurationError}
            aria-expanded={gifPickerOpen}
            onclick={() => {
              gifPickerOpen = !gifPickerOpen;
              emojiPickerOpen = false;
            }}>GIF</button
          >
        {/if}
        {#if !editingMessage}
          <button
            class="emoji-button"
            class:active={emojiPickerOpen}
            type="button"
            disabled={busy || !channelReady || !channel}
            aria-label="Choose an emoji"
            aria-expanded={emojiPickerOpen}
            onclick={() => {
              emojiPickerOpen = !emojiPickerOpen;
              gifPickerOpen = false;
            }}>☺</button
          >
        {/if}
        <small class="composer-count">{content.length}/4000</small>
        <button
          class="send-button"
          disabled={busy ||
            !channelReady ||
            !channel ||
            uploads.some((item) => item.status === 'uploading') ||
            (editingMessage
              ? !content.trim()
              : !content.trim() && !uploads.some((item) => item.status === 'ready'))}
          aria-label="Send message"
          title="Send message"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m4 4 17 8-17 8 3-7 8-1-8-1z" />
          </svg>
        </button>
      </form>
      {#if gifConfigurationError}
        <p class="composer-feature-warning" role="status">
          <span>{gifConfigurationError}</span>
          <button
            type="button"
            disabled={gifConfigurationLoading}
            onclick={() => void refreshGifConfiguration()}
          >
            {gifConfigurationLoading ? 'Checking…' : 'Retry GIF check'}
          </button>
        </p>
      {/if}
      {#if gifPickerOpen}
        <GifPicker onSelect={chooseGif} onClose={() => (gifPickerOpen = false)} />
      {/if}
      {#if emojiPickerOpen}
        <EmojiPicker
          customEmojis={pickerEmojis}
          onSelect={chooseEmoji}
          onClose={() => (emojiPickerOpen = false)}
        />
      {/if}
      {#if uploads.length && !editingMessage}
        <UploadPreviewTray {uploads} onRemove={removeUpload} />
      {/if}
    </footer>
    {#if pinsOpen}
      <PinnedMessagesPanel
        messages={pinnedMessages}
        loading={pinsLoading}
        error={pinsError}
        onClose={() => (pinsOpen = false)}
        onJump={jumpToPinnedMessage}
        onRetry={() => void loadPins()}
      />
    {/if}
  </section>
</main>

<NewMessageDialog bind:open={newMessageOpen} />

{#if profile}
  <UserProfileCard
    user={profile.user}
    presence={entities.presenceFor(profile.user)}
    x={profile.x}
    y={profile.y}
    isSelf={Boolean(currentUser && entityKey(currentUser) === entityKey(profile.user))}
    onClose={() => (profile = null)}
  />
{/if}

<dialog bind:this={groupDialog} class="action-dialog group-dm-dialog">
  <form method="dialog" onsubmit={(event) => event.preventDefault()}>
    <header>
      <div>
        <p class="eyebrow">Group conversation</p>
        <h2>{conversationTitle}</h2>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label="Close"
        onclick={() => groupDialog?.close()}>×</button
      >
    </header>

    <section class="group-dm-setting">
      <label class="form-field">
        <span>Group name</span>
        <input bind:value={groupName} maxlength="100" placeholder="Optional group name" />
      </label>
      <button class="secondary-button" type="button" disabled={groupBusy} onclick={updateGroupName}>
        Save name
      </button>
    </section>

    <section class="group-dm-setting">
      <label class="form-field">
        <span>Add a friend</span>
        <input
          bind:value={groupInviteHandle}
          placeholder="@friend@example.net"
          autocomplete="off"
        />
        <small>Any member can invite one of their existing friends.</small>
      </label>
      <button
        class="secondary-button"
        type="button"
        disabled={groupBusy || !groupInviteHandle.trim()}
        onclick={addGroupMember}>Add</button
      >
    </section>

    <section class="group-dm-setting e2ee-setting">
      <div>
        <strong>End-to-end encryption</strong>
        {#if channel?.encryption_mode === 'e2ee'}
          <small>
            {channel.encryption_state === 'rekeying'
              ? 'Paused · The member list changed and requires fresh device keys.'
              : 'On · Messages, files, and supported calls are readable only on members’ enrolled devices.'}
          </small>
          {#if e2eeSafetyNumber}<code class="e2ee-safety-number">{e2eeSafetyNumber}</code>{/if}
        {:else}
          <small>
            Optional and permanent. Disables server message search, previews, bots, webhooks, and
            file scanning. Losing every device key and recovery backup loses message access.
          </small>
        {/if}
      </div>
      {#if channel?.encryption_mode !== 'e2ee' && groupOwner && e2eeActivationEnabled}
        <button
          class="secondary-button"
          type="button"
          disabled={groupBusy}
          onclick={enableEncryption}>Turn on</button
        >
      {:else if channel?.encryption_state === 'rekeying' && groupOwner}
        <button
          class="secondary-button"
          type="button"
          disabled={groupBusy}
          onclick={rekeyEncryption}>Secure changes</button
        >
      {/if}
    </section>

    <section class="group-dm-members" aria-labelledby="group-members-heading">
      <div class="dm-friend-picker-heading">
        <strong id="group-members-heading">Members</strong>
        <small>{channel ? (channel.recipients?.length ?? 0) + 1 : 0}</small>
      </div>
      {#if currentUser}
        <div class="group-dm-member">
          <span class="avatar avatar-small">
            {#if currentUser.avatar_hash}
              <img src={assetUrl(currentUser.avatar_hash, 'thumbnail_128', currentUser)} alt="" />
            {:else}{currentUser.username.slice(0, 1).toUpperCase()}{/if}
          </span>
          <span><strong>{userDisplayName(currentUser)}</strong><small>You</small></span>
          {#if channel?.owner_id === currentUser.id && channel.owner_domain === currentUser.origin_domain}
            <small class="owner-badge">Owner</small>
          {/if}
        </div>
      {/if}
      {#each channel?.recipients ?? [] as member (entityKey(member))}
        <div class="group-dm-member">
          <span class="avatar avatar-small">
            {#if member.avatar_hash}
              <img src={assetUrl(member.avatar_hash, 'thumbnail_128', member)} alt="" />
            {:else}{member.username.slice(0, 1).toUpperCase()}{/if}
          </span>
          <span><strong>{userDisplayName(member)}</strong><small>{member.handle}</small></span>
          {#if channel?.owner_id === member.id && channel.owner_domain === member.origin_domain}
            <small class="owner-badge">Owner</small>
          {:else if groupOwner}
            <button
              class="group-member-action"
              type="button"
              disabled={groupBusy}
              aria-label={`Remove ${userDisplayName(member)} from the group`}
              onclick={() => removeGroupMember(member)}
              ><Icon name="trash" size={16} />Remove</button
            >
          {/if}
        </div>
      {/each}
    </section>
    {#if groupError}<p class="form-error" role="alert">{groupError}</p>{/if}
    <footer>
      <button class="group-leave-button" type="button" disabled={groupBusy} onclick={leaveGroup}>
        <Icon name="logout" size={17} />Leave group
      </button>
      <button class="secondary-button" type="button" onclick={() => groupDialog?.close()}
        >Done</button
      >
    </footer>
  </form>
</dialog>
