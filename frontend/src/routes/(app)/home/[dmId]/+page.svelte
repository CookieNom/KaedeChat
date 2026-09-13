<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { createUploadQueue } from '$lib/media/upload-queue';
  import { resolve } from '$app/paths';
  import { page } from '$app/state';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import EphemeralInteractionTray from '$lib/components/EphemeralInteractionTray.svelte';
  import type { GifResult } from '$lib/chat/gifs';
  import {
    customEmojiToken,
    loadUnicodeEmojis,
    unicodeEmojiCompletions,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import { stickerItem, stickerOptions, type StickerOption } from '$lib/chat/stickers';
  import { autosizeTextarea } from '$lib/ui/autosize';
  import { firstNavigableChannel } from '$lib/chat/channels';
  import { dmTitle, groupDmSubtitle, isGroupDm, ownsGroupDm } from '$lib/chat/direct-messages';
  import { completionAt, replaceCompletion } from '$lib/chat/completion';
  import {
    commandAttachmentOptionIds,
    applicationCommandRequestIdentity,
    applicationCommandByIdentity,
    commandCompletions,
    resolveCommandInvocation,
    localizedCommandName,
    commandOptionPayload,
    commandOptionsComplete,
    parseApplicationCommands,
    type ApplicationCommand,
    type ApplicationCommandOption,
    type CommandComposerValues
  } from '$lib/chat/application-commands';
  import { rememberAppContextCommand } from '$lib/chat/context-commands';
  import {
    commandInteractionRequestContext,
    createInteraction,
    interactionFileEncryptionIntent,
    requestCommandAutocomplete
  } from '$lib/chat/interactions';
  import { forwardingDestinations, forwardUnavailableReason } from '$lib/chat/forwarding';
  import { executePreparedForward } from '$lib/chat/prepared-forwarding';
  import {
    channelSupportsMessagePins,
    loadPinnedMessages,
    messagePinPath,
    reconcileChannelPinsUpdate,
    type ChannelPinsUpdate
  } from '$lib/chat/pins';
  import { fileUploadMatches, type PollCreatePayload } from '$lib/chat/rich-content';
  import { mentionsUser } from '$lib/chat/mentions';
  import {
    applyMessageDeliveryUpdate,
    compareMessages,
    failPendingMessage,
    mergeMessageSnapshot,
    messageReferenceTarget,
    reconcileMessage,
    resolvedReferencedMessage,
    type MessageDeliveryUpdate
  } from '$lib/chat/reconcile';
  import { canonicalReactionEmoji } from '$lib/chat/reactions';
  import {
    applyReactionDispatch,
    messageReactionsPath,
    ownReactionPath,
    reactionUpdateFromDispatch,
    type ReactionDispatchName
  } from '$lib/chat/reaction-state';
  import { currentTtsPreferences, speakTtsMessage, ttsCommand } from '$lib/chat/tts';
  import {
    applyBulkMessageDelete,
    tombstoneMessage,
    type MessageBulkDeleteUpdate
  } from '$lib/chat/message-deletions';
  import {
    applyPollVoteDispatch,
    pollVoteUpdateFromDispatch,
    type PollVoteDispatchName
  } from '$lib/chat/poll-state';
  import {
    discardAttachments,
    pendingMessageSend,
    type PendingMessageSend,
    withoutSubmittedUploads
  } from '$lib/chat/outbox';
  import { compareEntityRefs, entityKey, entityRef, matchesEntityRef } from '$lib/chat/refs';
  import { interactionResponses } from '$lib/chat/interaction-responses.svelte';
  import { buildTimeline, withInteractionResponses } from '$lib/chat/timeline';
  import { createTypingState } from '$lib/chat/typing';
  import type {
    Attachment,
    Channel,
    CustomEmoji,
    Guild,
    GuildSticker,
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
  import CommandOptionComposer from '$lib/components/CommandOptionComposer.svelte';
  import ComposerActionMenu from '$lib/components/ComposerActionMenu.svelte';
  import CreatePollDialog from '$lib/components/CreatePollDialog.svelte';
  import DmBotE2eeParticipation from '$lib/components/DmBotE2eeParticipation.svelte';
  import ForwardMessageDialog from '$lib/components/ForwardMessageDialog.svelte';
  import GuildRail from '$lib/components/GuildRail.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import EmojiPicker from '$lib/components/EmojiPicker.svelte';
  import GifPicker from '$lib/components/GifPicker.svelte';
  import { markConversationsRead } from '$lib/notifications/read-actions';
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
    encryptedMessageEditBindings,
    initializeE2EE,
    richMessageMentionIntent,
    type EncryptedAllowedMentions,
    type KaedeE2EEClient
  } from '$lib/e2ee/client';
  import { acknowledgeEncryptedRoom, confirmEncryptedRoomJoin } from '$lib/e2ee/disclosures';
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
  let availableStickers = $state<GuildSticker[]>([]);
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
  const pickerStickers = $derived<StickerOption[]>(stickerOptions(availableStickers));
  let error = $state('');
  let busy = $state(false);
  let channelReady = $state(false);
  let typing = $state('');
  const typingState = createTypingState((label) => {
    typing = label;
  });
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
  // Keep the visit's starting point even as acknowledgements advance on other devices.
  let visitReadInclusive = $state(false);
  let visitReadRef = $state<{ id: string; origin_domain: string } | null>(null);
  let historyTarget = $state<string | null>(null);
  let historyRevision = $state(0);
  let jumpingHistory = $state(false);
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
  let encryptedAppsOpen = $state(false);
  let e2eeClient = $state<KaedeE2EEClient | null>(null);
  let e2eeSafetyNumber = $state('');
  let applicationCommands = $state<ApplicationCommand[]>([]);
  let selectedApplicationCommand = $state<ApplicationCommand | null>(null);
  let commandOptionValues = $state<CommandComposerValues>({});
  let commandNotice = $state('');
  let pollDialogOpen = $state(false);
  let forwardingMessage = $state<Message | null>(null);
  const uploadQueue = createUploadQueue(
    () => uploads,
    (next) => {
      uploads = next;
    }
  );
  const pendingSends = new SvelteMap<string, PendingMessageSend>();
  const deliveryRecoveries = new SvelteSet<string>();
  let manualUnreadPaused = $state(false);
  const readAcknowledgements = new ReadAcknowledgementQueue<Message & { read_version?: number }>({
    send: async (message) => {
      try {
        await api(
          `/channels/${encodeURIComponent(`${message.channel_id}@${message.channel_domain}`)}/ack`,
          {
            method: 'POST',
            body: JSON.stringify({
              message_id: entityRef(message),
              read_version: message.read_version ?? 0
            })
          }
        );
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          readAcknowledgements.reset();
          manualUnreadPaused = true;
          setReadStates(await api<ReadStateStatus[]>('/users/@me/read-states'));
          return;
        }
        throw caught;
      }
    },
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
  const canCreatePoll = $derived(Boolean(channelReady && channel));
  const canPinMessages = $derived(Boolean(channel && channelSupportsMessagePins(channel)));
  const forwardDestinations = $derived(
    channel ? forwardingDestinations(channel, entities.channels.values) : []
  );
  const recipient = $derived(channel?.recipients?.[0] ?? null);
  const conversationTitle = $derived(dmTitle(channel));
  const groupConversation = $derived(isGroupDm(channel));
  const groupOwner = $derived(Boolean(channel && ownsGroupDm(channel, currentUser)));
  const currentReadState = $derived(channel ? unreadFor(channel) : undefined);
  const timeline = $derived(
    withInteractionResponses(
      buildTimeline(messages, visitReadRef, visitReadInclusive),
      Object.values(interactionResponses.byResponse),
      channel ? entityRef(channel) : ''
    )
  );
  const aroundMessage = $derived(page.url.searchParams.get('around'));
  const targetTimelineKey = $derived.by(() => {
    const reference = historyTarget;
    if (!reference) return null;
    const target = messages.find((message) => matchesEntityRef(reference, message, localDomain));
    // Deleted/expired anchors still have a valid ordered history window.
    return target
      ? `message:${entityKey(target)}`
      : (timeline.find((item) => item.kind === 'new')?.key ?? timeline.at(-1)?.key ?? null);
  });

  function referencedMessage(message: Message): Message | null {
    return resolvedReferencedMessage(message, messages);
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
    typingState.reset();
  }

  function registerTyping(userId: string, userDomain?: string) {
    const domain = userDomain ?? localDomain;
    if (currentUser?.id === userId && currentUser.origin_domain === domain) return;
    const user =
      entities.users.values.find(
        (candidate) => candidate.id === userId && candidate.origin_domain === domain
      ) ?? recipient;
    typingState.register({
      ref: `${userId}@${domain}`,
      name: user ? userDisplayName(user) : 'Someone'
    });
  }
  const completionQuery = $derived(completionAt(content, composerCursor));
  const completionOptions = $derived(
    completionQuery?.marker === '/'
      ? commandCompletions(applicationCommands, completionQuery.query)
      : completionQuery?.marker === ':'
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
        $t('ui_the_server_could_not_save_the_presence_settin_0b553172')
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
      error = userErrorMessage(caught, $t('ui_could_not_load_that_profile_try_again_829ce57b'));
    }
  }

  function reconcileReactionMutation(eventName: ReactionDispatchName, payload: unknown) {
    if (!reactionUpdateFromDispatch(eventName, payload)) return;
    const patch = (message: Message) =>
      applyReactionDispatch(message, eventName, payload, currentUser);
    setMessages(messages.map(patch));
    pinnedMessages = pinnedMessages.map(patch);
  }

  function reconcilePollVote(eventName: PollVoteDispatchName, payload: unknown) {
    if (!pollVoteUpdateFromDispatch(payload)) return;
    const patch = (message: Message) =>
      applyPollVoteDispatch(message, eventName, payload, currentUser);
    setMessages(messages.map(patch));
    pinnedMessages = pinnedMessages.map(patch);
  }

  function applyDispatch(dispatch: Dispatch) {
    if (dispatch.t === 'MESSAGE_CREATE') {
      const message = dispatch.d as Message;
      if (isCurrentChannel(message.channel_id, message.channel_domain)) {
        if (message.e2ee && channel && e2eeClient) {
          void decryptConversationMessages(e2eeClient, channel, [message]).then(([decrypted]) => {
            speakTtsMessage(decrypted, entityRef(channel));
            if (!hasLater && !jumpingHistory) reconcile(decrypted);
          });
        } else {
          speakTtsMessage(message, channel ? entityRef(channel) : null);
          if (!hasLater && !jumpingHistory) reconcile(message);
        }
        if (
          document.visibilityState === 'visible' &&
          document.hasFocus() &&
          timelineAtBottom &&
          !hasLater &&
          !jumpingHistory
        )
          void acknowledge(message);
      } else {
        const target = entities.channels.values.find(
          (candidate) =>
            candidate.id === message.channel_id &&
            candidate.origin_domain === message.channel_domain
        );
        if (message.e2ee && target?.encryption_mode === 'e2ee' && e2eeClient) {
          void decryptConversationMessages(e2eeClient, target, [message]).then(([decrypted]) =>
            speakTtsMessage(decrypted, channel ? entityRef(channel) : null)
          );
        } else if (!message.e2ee) {
          speakTtsMessage(message, channel ? entityRef(channel) : null);
        }
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
    } else if (dispatch.t === 'MESSAGE_REACTION_ADD' || dispatch.t === 'MESSAGE_REACTION_REMOVE') {
      reconcileReactionMutation(dispatch.t, dispatch.d);
    } else if (
      dispatch.t === 'MESSAGE_POLL_VOTE_ADD' ||
      dispatch.t === 'MESSAGE_POLL_VOTE_REMOVE'
    ) {
      reconcilePollVote(dispatch.t, dispatch.d);
    } else if (dispatch.t === 'CHANNEL_PINS_UPDATE') {
      const update = dispatch.d as ChannelPinsUpdate;
      if (
        channel &&
        update.channel_id === channel.id &&
        update.channel_domain === channel.origin_domain
      ) {
        setMessages(reconcileChannelPinsUpdate(messages, update));
        pinnedMessages = reconcileChannelPinsUpdate(pinnedMessages, update).filter(
          (message) => message.pinned !== false
        );
        if (pinsOpen) void loadPins();
      }
    } else if (dispatch.t === 'MESSAGE_UPDATE') {
      const update = dispatch.d as Message;
      if ('reaction' in update) {
        reconcileReactionMutation('MESSAGE_UPDATE', dispatch.d);
        return;
      }
      if (update.e2ee && channel && e2eeClient) {
        void decryptConversationMessages(e2eeClient, channel, [update]).then(([decrypted]) =>
          applyDispatch({ ...dispatch, d: { ...decrypted, e2ee: null } })
        );
        return;
      }
      setMessages(
        messages.map((item) =>
          entityKey(item) === entityKey(update) ? { ...item, ...update } : item
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
        const deletedAt = new Date().toISOString();
        setMessages(
          messages.map((item) =>
            item.id === deleted.id && item.origin_domain === deleted.origin_domain
              ? tombstoneMessage(item, deletedAt)
              : item
          )
        );
        pinnedMessages = pinnedMessages.filter(
          (item) => item.id !== deleted.id || item.origin_domain !== deleted.origin_domain
        );
      }
    } else if (dispatch.t === 'MESSAGE_DELETE_BULK') {
      const update = dispatch.d as MessageBulkDeleteUpdate;
      if (
        channel &&
        update.channel_id === channel.id &&
        update.channel_domain === channel.origin_domain
      ) {
        setMessages(applyBulkMessageDelete(messages, update));
        pinnedMessages = applyBulkMessageDelete(pinnedMessages, update).filter(
          (message) => message.deleted_at === null
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
    } else if (dispatch.t === 'GUILD_STICKER_CREATE') {
      const sticker = dispatch.d as GuildSticker;
      availableStickers = [
        ...availableStickers.filter((item) => entityKey(item) !== entityKey(sticker)),
        sticker
      ];
    } else if (dispatch.t === 'GUILD_STICKER_DELETE') {
      const sticker = dispatch.d as GuildSticker;
      availableStickers = availableStickers.filter(
        (item) => entityKey(item) !== entityKey(sticker)
      );
    } else if (dispatch.t === 'GUILD_DELETE') {
      const removed = dispatch.d as { id: string; origin_domain: string };
      availableEmojis = availableEmojis.filter(
        (item) => item.guild_id !== removed.id || item.guild_domain !== removed.origin_domain
      );
      availableStickers = availableStickers.filter(
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
      const update = dispatch.d as ReadStateDispatch;
      if (
        (update.read_version ?? 0) >
          (readStates.find(
            (s) => s.channel_id === update.channel_id && s.channel_domain === update.channel_domain
          )?.read_version ?? 0) &&
        update.manual_unread &&
        channel &&
        update.channel_id === channel.id &&
        update.channel_domain === channel.origin_domain
      ) {
        readAcknowledgements.reset();
        manualUnreadPaused = true;
        visitReadRef =
          update.last_message_id && update.last_message_domain
            ? { id: update.last_message_id, origin_domain: update.last_message_domain }
            : null;
        visitReadInclusive = !visitReadRef;
        if (!visitReadRef && update.unread_message_id && update.unread_message_domain)
          visitReadRef = {
            id: update.unread_message_id,
            origin_domain: update.unread_message_domain
          };
      }
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
        $t('ui_could_not_check_whether_gif_search_is_availab_48239bb9')
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

  function chooseSticker(sticker: StickerOption) {
    if (busy || !channelReady || !channel || editingMessage) return;
    emojiPickerOpen = false;
    gifPickerOpen = false;
    void send(
      pendingMessageSend(
        null,
        [],
        [],
        crypto.randomUUID(),
        null,
        [],
        false,
        [sticker.value],
        [stickerItem(sticker)]
      )
    );
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
      applicationCommands = [];
      selectedApplicationCommand = null;
      commandOptionValues = {};
      commandNotice = '';
      resetTyping();
      replyingMessage = null;
      pinnedMessages = [];
      pinsOpen = false;
      pinsLoading = false;
      pinsError = '';
      error = '';
      busy = false;
      channelReady = false;
      jumpingHistory = false;
      visitReadRef = null;
      historyTarget = null;
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
    const targetAround = historyTarget;
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
        loadedStickers,
        loadedPins,
        loadedCommands
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
        api<GuildSticker[]>('/users/@me/stickers'),
        loadPinnedMessages(targetRef).catch(() => []),
        api<unknown>(`/channels/${encodeURIComponent(targetRef)}/application-commands`)
          .then(parseApplicationCommands)
          .catch(() => [])
      ]);
      if (
        routeGeneration !== loadGeneration ||
        snapshot !== snapshotGeneration ||
        targetRef !== dmId
      )
        return;
      setDirectMessages(loadedDms);
      setGuilds(loadedGuilds);
      if (!preserveMessages) {
        const state = loadedReadStates.find((item) =>
          matchesEntityRef(
            targetRef,
            {
              id: item.channel_id,
              origin_domain: item.channel_domain
            },
            localDomain
          )
        );
        visitReadRef =
          state?.read_message_id && state.read_message_domain
            ? { id: state.read_message_id, origin_domain: state.read_message_domain }
            : null;
        visitReadInclusive = !visitReadRef && Boolean(state?.first_unread_message_id);
        if (
          visitReadInclusive &&
          state?.first_unread_message_id &&
          state.first_unread_message_domain
        )
          visitReadRef = {
            id: state.first_unread_message_id,
            origin_domain: state.first_unread_message_domain
          };
        manualUnreadPaused = false;
        historyTarget = targetAround;
        if (!targetAround && state?.unread && visitReadRef) {
          targetAround = entityRef(visitReadRef);
          const history = await api<Message[]>(
            `/channels/${encodeURIComponent(targetRef)}/messages?around=${encodeURIComponent(targetAround)}`
          );
          if (
            routeGeneration !== loadGeneration ||
            snapshot !== snapshotGeneration ||
            targetRef !== dmId
          )
            return;
          loadedMessages.splice(0, loadedMessages.length, ...history);
          historyTarget = targetAround;
        }
      }
      setReadStates(loadedReadStates);
      availableEmojis = loadedEmojis;
      availableStickers = loadedStickers;
      pinnedMessages = loadedPins;
      applicationCommands = loadedCommands;
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
        error = $t('ui_older_messages_are_temporarily_unavailable_fr_57247e24');
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
        if (
          !confirmEncryptedRoomJoin(
            entityRef(loadedCurrentUser),
            entityRef(loadedChannel),
            'conversation'
          )
        ) {
          window.location.assign(resolve('/home'));
          return;
        }
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
        error = userErrorMessage(
          caught,
          $t('ui_could_not_open_this_conversation_try_again_426d4f45')
        );
      } else if (!error) {
        error = $t('ui_live_updates_resumed_but_conversation_state_c_439252f1');
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
        error = $t('ui_older_messages_are_temporarily_unavailable_fr_1936e561');
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
      error = userErrorMessage(caught, $t('ui_could_not_load_earlier_messages_try_again_7d0ea620'));
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
      error = userErrorMessage(caught, $t('ui_could_not_load_newer_messages_try_again_e1bc8d7b'));
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
                mention_count:
                  state.last_message_id === message.id &&
                  state.last_message_domain === message.origin_domain
                    ? 0
                    : state.mention_count,
                unread: Boolean(
                  state.last_message_id &&
                  state.last_message_domain &&
                  compareEntityRefs(
                    { id: state.last_message_id, origin_domain: state.last_message_domain },
                    message
                  ) > 0
                )
              }
          : state
      )
    );
  }

  async function markMessageUnread(message: Message) {
    const generation = loadGeneration;
    manualUnreadPaused = true;
    readAcknowledgements.reset();
    try {
      const states = await api<ReadStateStatus[]>('/users/@me/read-states');
      if (generation !== loadGeneration) return;
      const current = states.find(
        (s) => s.channel_id === message.channel_id && s.channel_domain === message.channel_domain
      );
      await api(
        `/channels/${encodeURIComponent(`${message.channel_id}@${message.channel_domain}`)}/ack`,
        {
          method: 'POST',
          body: JSON.stringify({
            message_id: entityRef(message),
            mark_unread: true,
            read_version: current?.read_version ?? 0
          })
        }
      );
      if (generation !== loadGeneration) return;
      const refreshed = await api<ReadStateStatus[]>('/users/@me/read-states');
      if (generation !== loadGeneration) return;
      setReadStates(refreshed);
      const saved = refreshed.find(
        (s) => s.channel_id === message.channel_id && s.channel_domain === message.channel_domain
      );
      visitReadRef =
        saved?.read_message_id && saved.read_message_domain
          ? { id: saved.read_message_id, origin_domain: saved.read_message_domain }
          : null;
      visitReadInclusive = !visitReadRef;
      if (!visitReadRef) visitReadRef = { id: message.id, origin_domain: message.origin_domain };
    } catch (caught) {
      manualUnreadPaused = false;
      readStateWarning = userErrorMessage(caught, 'Could not mark the message unread. Try again.');
    }
  }

  function acknowledge(message: Message): Promise<void> {
    if (manualUnreadPaused || document.querySelector('dialog[open]')) return Promise.resolve();
    return readAcknowledgements.acknowledge({
      ...message,
      read_version:
        readStates.find(
          (s) => s.channel_id === message.channel_id && s.channel_domain === message.channel_domain
        )?.read_version ?? 0
    });
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
    if (document.querySelector('dialog[open]')) return;
    if (manualUnreadPaused) return;
    if (
      document.visibilityState !== 'visible' ||
      !document.hasFocus() ||
      !timelineAtBottom ||
      hasLater ||
      jumpingHistory
    )
      return;
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
        const editBindings =
          channel?.encryption_mode === 'e2ee' ? encryptedMessageEditBindings(editing) : null;
        const encrypted =
          channel?.encryption_mode === 'e2ee'
            ? await (
                e2eeClient ?? (currentUser ? await initializeE2EE(currentUser) : null)
              )?.encryptMessage(channel, text, {
                operation: 'edit',
                targetMessage: entityRef(editing),
                attachments: editing.decrypted_attachments ?? [],
                ...(editBindings ?? {})
              })
            : null;
        if (channel?.encryption_mode === 'e2ee' && !encrypted)
          throw new Error($t('ui_encryption_is_unavailable_on_this_device_cc08d0c6'));
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
                e2ee_verified: true,
                decrypted_content: text,
                decrypted_attachments: editing.decrypted_attachments ?? [],
                decrypted_allowed_mentions: editing.decrypted_allowed_mentions
              }
            : saved
        );
        finishEditing();
      } catch (caught) {
        if (generation === loadGeneration)
          error = userErrorMessage(caught, $t('ui_could_not_edit_the_message_try_again_ba5399d9'));
      } finally {
        if (generation === loadGeneration) busy = false;
      }
      return;
    }
    if (busy || !channelReady || !channel) return;
    if (selectedApplicationCommand && !retry) {
      const selected = selectedApplicationCommand;
      if (!commandOptionsComplete(selected, commandOptionValues)) return;
      const commandAttachmentIds = commandAttachmentOptionIds(selected, commandOptionValues);
      busy = true;
      error = '';
      commandNotice = '';
      try {
        await createInteraction(
          await commandInteractionRequestContext(channel, selected, currentUser, e2eeClient),
          {
            ...applicationCommandRequestIdentity(selected),
            options: commandOptionPayload(selected, commandOptionValues)
          },
          channel.encryption_mode === 'e2ee'
            ? interactionFileEncryptionIntent(commandAttachmentIds, uploads)
            : {}
        );
        clearSubmittedUploads(commandAttachmentIds);
        selectedApplicationCommand = null;
        commandOptionValues = {};
        content = '';
        composerCursor = 0;
        commandNotice = `/${selected.name} sent to ${selected.application_name}.`;
      } catch (caught) {
        error = userErrorMessage(caught, $t('ui_the_bot_command_could_not_be_delivered_f00c1081'));
      } finally {
        busy = false;
      }
      return;
    }
    const ttsInvocation = retry ? { matched: false, content: '' } : ttsCommand(text);
    const tts = retry?.tts ?? ttsInvocation.matched;
    if (!retry && ttsInvocation.matched && !ttsInvocation.content) {
      error = $t('ui_enter_a_message_after_tts_f057a42d');
      return;
    }
    if (tts) {
      if (!currentTtsPreferences().enabled) {
        error = $t('ui_enable_allow_playback_and_usage_of_tts_comman_204eb385');
        return;
      }
    }
    const outgoingText = tts ? (retry?.content ?? ttsInvocation.content) : text;
    const commandResolution =
      !retry && !tts
        ? resolveCommandInvocation(text, applicationCommands)
        : { kind: 'none' as const };
    if (commandResolution.kind === 'ambiguous') {
      error = `More than one app provides /${commandResolution.commands[0]?.name ?? 'command'}. Choose the app from the command list or Apps launcher.`;
      return;
    }
    const invocation = commandResolution.kind === 'resolved' ? commandResolution : null;
    if (invocation) {
      if (invocation.command.options?.length) {
        selectedApplicationCommand = invocation.command;
        commandOptionValues = {};
        content = '';
        composerCursor = 0;
        error = invocation.options.raw
          ? 'Choose the command options in the typed fields. Free-form command arguments are not sent.'
          : '';
        return;
      }
      busy = true;
      error = '';
      commandNotice = '';
      try {
        await createInteraction(
          await commandInteractionRequestContext(
            channel,
            invocation.command,
            currentUser,
            e2eeClient
          ),
          {
            ...applicationCommandRequestIdentity(invocation.command),
            options: invocation.options
          }
        );
        content = '';
        composerCursor = 0;
        commandNotice = `/${invocation.command.name} sent to ${invocation.command.application_name}.`;
      } catch (caught) {
        error = userErrorMessage(caught, $t('ui_the_bot_command_could_not_be_delivered_f00c1081'));
      } finally {
        busy = false;
      }
      return;
    }
    const attachmentIds = retry
      ? retry.attachmentIds
      : uploads
          .filter((item) => item.status === 'ready' && item.attachmentId)
          .map((item) => item.attachmentId as string);
    if (!retry && uploads.some((item) => item.status === 'uploading')) return;
    let mentionUserIds: string[];
    let encryptedAllowedMentions: EncryptedAllowedMentions | null;
    let repliedUserRef: string | null;
    if (retry) {
      mentionUserIds = retry.mentionUserIds;
      repliedUserRef = retry.repliedUserRef;
      encryptedAllowedMentions =
        retry.encryptedAllowedMentions ??
        (channel.encryption_mode === 'e2ee'
          ? {
              parse: ['users'],
              users: [],
              roles: [],
              replied_user: repliedUserRef !== null
            }
          : null);
    } else {
      repliedUserRef =
        replyingMessage &&
        (replyingMessage.author_id !== currentUser?.id ||
          replyingMessage.author_domain !== currentUser?.origin_domain)
          ? `${replyingMessage.author_id}@${replyingMessage.author_domain}`
          : null;
      if (channel.encryption_mode === 'e2ee') {
        encryptedAllowedMentions = {
          parse: ['users'],
          users: [],
          roles: [],
          replied_user: repliedUserRef !== null
        };
        try {
          const intent = richMessageMentionIntent({
            content: outgoingText || null,
            components: [],
            allowed_mentions: encryptedAllowedMentions
          });
          mentionUserIds = [
            ...new Set([...intent.userRefs, ...(repliedUserRef ? [repliedUserRef] : [])])
          ].sort();
        } catch (caught) {
          error = userErrorMessage(
            caught,
            $t('ui_encrypted_mentions_must_use_a_fully_qualified_33c5c88a')
          );
          return;
        }
      } else {
        encryptedAllowedMentions = null;
        mentionUserIds = [
          ...(channel.recipients ?? [])
            .filter((user) => mentionsUser(outgoingText, user, localDomain))
            .map(entityRef),
          ...(repliedUserRef ? [repliedUserRef] : [])
        ].filter((value, index, values) => values.indexOf(value) === index);
      }
    }
    if (!retry && !outgoingText && !attachmentIds.length) return;
    const draft = retry
      ? { ...retry, encryptedAllowedMentions, repliedUserRef }
      : pendingMessageSend(
          outgoingText || null,
          attachmentIds,
          mentionUserIds,
          crypto.randomUUID(),
          replyingMessage ? entityRef(replyingMessage) : null,
          uploads.flatMap((item) => (item.encryptedManifest ? [item.encryptedManifest] : [])),
          tts,
          [],
          [],
          encryptedAllowedMentions,
          repliedUserRef
        );
    if (!draft.content && !draft.attachmentIds.length && !draft.stickerIds.length) {
      error = $t('ui_reattach_this_message_s_files_before_retrying_08c69cee');
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
          sticker_items: draft.stickerItems,
          tts: draft.tts,
          e2ee_verified: channel.encryption_mode === 'e2ee' ? true : undefined,
          decrypted_content: channel.encryption_mode === 'e2ee' ? draft.content : undefined,
          decrypted_attachments:
            channel.encryption_mode === 'e2ee' ? draft.encryptedAttachments : undefined,
          decrypted_allowed_mentions:
            channel.encryption_mode === 'e2ee'
              ? (draft.encryptedAllowedMentions ?? undefined)
              : undefined,
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
              attachments: draft.encryptedAttachments,
              mentionUserRefs: draft.mentionUserIds,
              repliedUserRef: draft.repliedUserRef,
              referencedMessageRef: draft.referencedMessageId,
              rich: {
                stickerItems: draft.stickerItems,
                tts: draft.tts,
                allowedMentions: draft.encryptedAllowedMentions ?? undefined
              }
            })
          : null;
      if (channel.encryption_mode === 'e2ee' && !encrypted)
        throw new Error($t('ui_encryption_is_unavailable_on_this_device_cc08d0c6'));
      const saved = await api<Message>(`/channels/${encodeURIComponent(targetRef)}/messages`, {
        method: 'POST',
        body: JSON.stringify({
          content: encrypted ? null : draft.content,
          e2ee: encrypted,
          client_nonce: nonce,
          attachment_ids: draft.attachmentIds,
          mention_user_ids: draft.mentionUserIds,
          referenced_message_id: draft.referencedMessageId,
          sticker_ids: encrypted ? [] : draft.stickerIds,
          tts: draft.tts
        })
      });
      if (generation !== loadGeneration || routeRef !== dmId) return;
      reconcile(
        encrypted
          ? {
              ...saved,
              e2ee_verified: true,
              decrypted_content: draft.content,
              decrypted_attachments: draft.encryptedAttachments,
              decrypted_allowed_mentions: draft.encryptedAllowedMentions ?? undefined,
              sticker_items: draft.stickerItems,
              tts: draft.tts
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
          error = $t('ui_those_files_were_already_used_by_another_mess_12a34083');
        } else {
          error = userErrorMessage(caught, $t('ui_could_not_send_the_message_try_again_86b6cd6a'));
        }
      }
    } finally {
      if (generation === loadGeneration && routeRef === dmId) busy = false;
    }
  }

  async function queueFiles(
    files: FileList | File[],
    commandTarget?: { path: string; fileTypes?: string[]; command: ApplicationCommand }
  ) {
    if (!channel || busy || uploads.length >= 10) return;
    const target = entityRef(channel);
    const generation = loadGeneration;
    const routeRef = dmId;
    for (const file of Array.from(files).slice(0, 10 - uploads.length)) {
      if (commandTarget && !fileUploadMatches(commandTarget.fileTypes, file.name, file.type)) {
        error = `“${file.name}” is not an accepted file type for this command option.`;
        continue;
      }
      const upload =
        channel.encryption_mode === 'e2ee' ? uploadEncryptedChannelFile : uploadChannelFile;
      uploadQueue.add(
        file,
        (progress, signal) => upload(target, file, progress, signal),
        () => generation === loadGeneration && routeRef === dmId,
        (attachmentId) => {
          if (commandTarget && selectedApplicationCommand === commandTarget.command) {
            commandOptionValues = { ...commandOptionValues, [commandTarget.path]: attachmentId };
          }
        }
      );
    }
  }

  function removeUpload(key: string) {
    uploadQueue.remove(key);
  }

  function resetUploads() {
    uploadQueue.reset();
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
        error = userErrorMessage(caught, $t('ui_could_not_start_the_call_try_again_cc9f3f64'));
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
        error = userErrorMessage(caught, $t('ui_could_not_update_the_call_try_again_0c2733c8'));
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
      groupError = userErrorMessage(
        caught,
        $t('ui_could_not_rename_this_group_try_again_38a87d9c')
      );
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
        $t('ui_could_not_add_that_friend_they_must_be_friend_c3ab7967')
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
      groupError = userErrorMessage(
        caught,
        $t('ui_could_not_remove_that_member_try_again_084ad93a')
      );
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
      groupError = userErrorMessage(caught, $t('ui_could_not_leave_this_group_try_again_0845f089'));
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
      $t('ui_turn_on_end_to_end_encryption_for_this_conver_0f55c4d1')
    );
    if (!confirmed) return;
    groupBusy = true;
    groupError = '';
    try {
      const client = await initializeE2EE(currentUser);
      const updated = await client.activateRoom(entityRef(channel));
      e2eeClient = client;
      entities.channels.upsert(updated);
      acknowledgeEncryptedRoom(entityRef(currentUser), entityRef(updated));
      e2eeSafetyNumber = await client.safetyNumber(updated);
    } catch (caught) {
      const message = userErrorMessage(
        caught,
        $t('ui_could_not_enable_end_to_end_encryption_try_ag_c4200375')
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
      groupError = userErrorMessage(
        caught,
        $t('ui_could_not_secure_the_updated_member_list_try__e6844fa4')
      );
    } finally {
      groupBusy = false;
    }
  }

  async function showEncryptionInfo() {
    if (!channel || channel.encryption_mode !== 'e2ee') return;
    if (currentUser) {
      try {
        const client = e2eeClient ?? (await initializeE2EE(currentUser));
        e2eeClient = client;
        e2eeSafetyNumber = await client.safetyNumber(channel);
      } catch {
        // The cached number or an explicit unavailable state remains useful.
      }
    }
    window.alert(
      `End-to-end encryption is on, but participant identities remain unverified until this safety number is compared with the other members using a separate trusted channel. A match detects first-contact key substitution by an actively malicious instance. Compare it again after membership or identity changes:\n\n${e2eeSafetyNumber || $t('ui_safety_number_unavailable_on_this_device_52363a69')}`
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
    if (message.e2ee && message.e2ee_verified !== true) {
      error = $t('ui_this_encrypted_message_is_unavailable_on_this_019dc894');
      return;
    }
    replyingMessage = null;
    if (!editingMessage) {
      composerDraftBeforeEdit = { content, cursor: composerCursor };
    }
    editingMessage = message;
    content = message.e2ee ? (message.decrypted_content ?? '') : (message.content ?? '');
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
      pinnedMessages = await loadPinnedMessages(entityRef(channel));
    } catch (caught) {
      pinsError = userErrorMessage(
        caught,
        $t('ui_could_not_load_pinned_messages_close_this_pan_7f16cd85')
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
    if (!channel || !canPinMessages) return;
    if (
      !window.confirm(
        shouldPin ? $t('ui_pin_this_message_befe11f0') : $t('ui_unpin_this_message_27c1a290')
      )
    )
      return;
    try {
      await api(messagePinPath(entityRef(channel), entityRef(message)), {
        method: shouldPin ? 'PUT' : 'DELETE'
      });
      pinnedMessages = shouldPin
        ? [message, ...pinnedMessages.filter((item) => entityKey(item) !== entityKey(message))]
        : pinnedMessages.filter((item) => entityKey(item) !== entityKey(message));
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_update_the_pinned_message_try_again_657d6df4')
      );
    }
  }

  async function toggleMessageReaction(message: Message, emoji: string, remove: boolean) {
    if (!channel) return;
    const canonical = canonicalReactionEmoji(emoji);
    if (!canonical) return;
    const channelRef = entityRef(channel);
    const messageRef = entityRef(message);
    try {
      await api(
        remove
          ? ownReactionPath(channelRef, messageRef, canonical)
          : messageReactionsPath(channelRef, messageRef),
        remove
          ? { method: 'DELETE' }
          : { method: 'POST', body: JSON.stringify({ emoji: canonical }) }
      );
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_update_that_reaction_try_again_9ff32cc9'));
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
    const target = messageReferenceTarget(message);
    if (!target) return;
    jumpToMessageReference(entityRef(target));
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
      error = userErrorMessage(caught, $t('ui_could_not_delete_the_message_try_again_82b44f9f'));
    }
  }

  function retryMessage(message: Message) {
    editingMessage = null;
    composerDraftBeforeEdit = null;
    if (message.delivery_status === 'failed') {
      if (message.sticker_items?.length) {
        void send(
          pendingMessageSend(
            null,
            [],
            [],
            crypto.randomUUID(),
            null,
            [],
            false,
            message.sticker_items.map((item) => `${item.id}@${item.origin_domain}`),
            message.sticker_items
          )
        );
        return;
      }
      if (message.attachments?.length || !message.content) {
        content =
          message.tts && message.content ? `/tts ${message.content}` : (message.content ?? '');
        composerCursor = content.length;
        error = $t('ui_reattach_this_message_s_files_before_retrying_08c69cee');
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
            : null,
          [],
          message.tts === true
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
          draft.referencedMessageId,
          draft.encryptedAttachments,
          draft.tts,
          draft.stickerIds,
          draft.stickerItems,
          draft.encryptedAllowedMentions,
          draft.repliedUserRef
        );
        pendingSends.set(draft.clientNonce, draft);
      }
    }
    if (!draft) {
      content =
        message.tts && message.content ? `/tts ${message.content}` : (message.content ?? '');
      composerCursor = content.length;
      if (!content) error = $t('ui_reattach_this_message_s_files_before_retrying_08c69cee');
      void tick().then(() => composerInput?.focus());
      return;
    }
    void send(draft);
  }

  function chooseCompletion(completion: Completion) {
    if (!completionQuery) return;
    if (completion.kind === 'application-command') {
      const selected = applicationCommandByIdentity(
        applicationCommands,
        completion.applicationCommand
      );
      if (selected) {
        selectApplicationCommand(selected);
        return;
      }
    }
    const cursor = completionQuery.start + completion.value.length + 1;
    content = replaceCompletion(content, completionQuery, completion.value);
    composerCursor = cursor;
    void tick().then(() => {
      composerInput?.focus();
      composerInput?.setSelectionRange(cursor, cursor);
    });
  }

  function selectApplicationCommand(command: ApplicationCommand) {
    selectedApplicationCommand = command;
    commandOptionValues = {};
    content = '';
    composerCursor = 0;
    gifPickerOpen = false;
    emojiPickerOpen = false;
  }

  function cancelCommandComposer() {
    selectedApplicationCommand = null;
    commandOptionValues = {};
    content = '';
    composerCursor = 0;
    void tick().then(() => composerInput?.focus());
  }

  async function createPollMessage(poll: PollCreatePayload) {
    const target = channel;
    if (!target || !canCreatePoll) {
      throw new Error($t('ui_polls_are_unavailable_in_this_conversation_0476fd2e'));
    }
    const generation = loadGeneration;
    const client =
      target.encryption_mode === 'e2ee'
        ? (e2eeClient ?? (currentUser ? await initializeE2EE(currentUser) : null))
        : null;
    const encrypted = client
      ? await client.encryptMessage(target, '', { rich: { poll: { ...poll } } })
      : null;
    if (target.encryption_mode === 'e2ee' && !encrypted) {
      throw new Error($t('ui_encryption_is_unavailable_on_this_device_cc08d0c6'));
    }
    const saved = await api<Message>(
      `/channels/${encodeURIComponent(entityRef(target))}/messages`,
      {
        method: 'POST',
        body: JSON.stringify(encrypted ? { e2ee: encrypted } : { poll })
      }
    );
    if (generation !== loadGeneration) return;
    const verified = client
      ? (await decryptConversationMessages(client, target, [saved]))[0]
      : saved;
    if (!verified || (client && !verified.poll)) {
      throw new Error($t('ui_the_encrypted_poll_could_not_be_verified_loca_d68383d9'));
    }
    reconcile(verified);
    pollDialogOpen = false;
    await acknowledge(verified);
  }

  function requestForward(message: Message) {
    const unavailable = forwardUnavailableReason(message);
    if (unavailable) {
      error = unavailable;
      return;
    }
    forwardingMessage = message;
  }

  async function submitForward(targets: Channel[], note: string) {
    const source = forwardingMessage;
    if (!source) return;
    const sourceChannel = channel;
    if (!sourceChannel) return;
    if (!currentUser) throw new Error($t('ui_your_forwarding_identity_is_unavailable_8571701f'));
    const requiresEncryption = targets.some((target) => target.encryption_mode === 'e2ee');
    const client = requiresEncryption
      ? (e2eeClient ?? (currentUser ? await initializeE2EE(currentUser) : null))
      : e2eeClient;
    if (requiresEncryption && !client) {
      throw new Error($t('ui_encryption_is_unavailable_for_a_selected_dest_e5fed7d6'));
    }
    if (client && !e2eeClient) e2eeClient = client;
    const result = await executePreparedForward({
      source,
      sourceChannel,
      destinations: targets,
      requesterRef: entityRef(currentUser),
      note,
      e2eeClient: client
    });
    if (!result.forwards.length) {
      throw new Error($t('ui_the_message_could_not_be_forwarded_to_any_sel_93c4be17'));
    }
    const target = channel;
    if (target) {
      for (const forwarded of result.forwards) {
        if (forwarded.destination_channel_ref !== entityRef(target)) continue;
        reconcile(forwarded.message);
        await acknowledge(forwarded.message);
      }
    }
    forwardingMessage = null;
    commandNotice = result.failures.length
      ? `Forwarded to ${result.forwards.length} destination${result.forwards.length === 1 ? '' : 's'}; ${result.failures.length} failed.`
      : `Message forwarded to ${result.forwards.length} destination${result.forwards.length === 1 ? '' : 's'}.`;
  }

  async function executeContextCommand(command: ApplicationCommand, target: Message | UserSummary) {
    const targetChannel = channel;
    if (!targetChannel) return;
    error = '';
    try {
      await createInteraction(
        await commandInteractionRequestContext(targetChannel, command, currentUser, e2eeClient),
        {
          ...applicationCommandRequestIdentity(command),
          target_ref: entityRef(target)
        }
      );
      if (currentUser) rememberAppContextCommand(entityRef(currentUser), command);
      commandNotice = `${command.name} sent to ${command.application_name}.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_the_context_command_could_not_be_delivered_453eca35')
      );
    }
  }

  async function componentInteractionRequest(applicationRef: string) {
    const targetChannel = channel;
    if (!targetChannel) throw new Error($t('ui_this_conversation_is_no_longer_available_07f8c47b'));
    if (targetChannel.encryption_mode !== 'e2ee') {
      return { channelRef: entityRef(targetChannel), applicationRef };
    }
    const authority = applicationCommands.find(
      (command) => command.application_ref === applicationRef
    );
    if (!authority) {
      throw new Error($t('ui_refresh_this_conversation_before_using_this_e_dbe0d5e6'));
    }
    return commandInteractionRequestContext(targetChannel, authority, currentUser, e2eeClient);
  }

  async function autocompleteCommandOption(
    option: ApplicationCommandOption,
    value: string,
    generation: number,
    path: string
  ) {
    const selected = selectedApplicationCommand;
    const targetChannel = channel;
    if (!selected || !targetChannel || !option.autocomplete) return [];
    const draftValues = {
      ...commandOptionValues,
      [path]: value
    };
    const options = commandOptionPayload(selected, draftValues);
    const attachmentIds = commandAttachmentOptionIds(selected, draftValues);
    return requestCommandAutocomplete(
      await commandInteractionRequestContext(targetChannel, selected, currentUser, e2eeClient),
      {
        ...applicationCommandRequestIdentity(selected),
        interaction_type: 'autocomplete',
        options,
        focused_option: path,
        autocomplete_generation: generation
      },
      targetChannel.encryption_mode === 'e2ee'
        ? interactionFileEncryptionIntent(attachmentIds, uploads)
        : {}
    );
  }

  async function jumpHistory(reference: string | null) {
    manualUnreadPaused = false;
    if (!channel || jumpingHistory || loadingEarlier || loadingLater) return;
    const targetChannel = channel;
    const generation = loadGeneration;
    jumpingHistory = true;
    try {
      let history = await api<Message[]>(
        `/channels/${encodeURIComponent(entityRef(targetChannel))}/messages${reference ? `?around=${encodeURIComponent(reference)}` : ''}`
      );
      if (generation !== loadGeneration) return;
      if (targetChannel.encryption_mode === 'e2ee' && e2eeClient)
        history = await decryptConversationMessages(e2eeClient, targetChannel, history);
      if (generation !== loadGeneration) return;
      historyTarget = reference;
      timelineAtBottom = false;
      hasEarlier = reference ? history.length > 0 : history.length === 50;
      const oldest = history.at(-1);
      authorityHistoryComplete = oldest?.history_page_complete === true;
      hasEarlier &&=
        !authorityHistoryComplete && !reachesRetainedHistoryStart(targetChannel, oldest);
      if (oldest?.history_page_error_code === 'FEDERATED_DM_HISTORY_UNAVAILABLE')
        error = $t('ui_older_messages_are_temporarily_unavailable_fr_1936e561');
      hasLater = Boolean(reference && history.length);
      setMessages(history.sort(compareMessages));
      historyRevision += 1;
    } catch (caught) {
      if (generation === loadGeneration)
        error = userErrorMessage(caught, 'Could not load message history. Try again.');
    } finally {
      if (generation === loadGeneration) jumpingHistory = false;
    }
  }

  function acknowledgeVisible(key: string) {
    if (document.querySelector('dialog[open]')) return;
    if (manualUnreadPaused) return;
    if (
      !channelReady ||
      jumpingHistory ||
      document.visibilityState !== 'visible' ||
      !document.hasFocus()
    )
      return;
    const message = messages.find((item) => `message:${entityKey(item)}` === key);
    if (!message || message.id.startsWith('pending-')) return;
    const state = currentReadState;
    if (
      state?.read_message_id &&
      state.read_message_domain &&
      compareEntityRefs(message, {
        id: state.read_message_id,
        origin_domain: state.read_message_domain
      }) <= 0
    )
      return;
    void acknowledge(message);
  }

  function timelineBottomChanged(value: boolean) {
    timelineAtBottom = value;
    if (value) acknowledgeLatestIfVisible();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- authenticated media URLs are API resources, not Svelte routes -->

<svelte:head
  ><title>{$t('ui_value0_kaede_chat_4bf52868', { value0: String(conversationTitle) })}</title
  ></svelte:head
>
<svelte:window
  onkeydown={(event) => {
    if (mobileNavigationOpen) mobileNavigationKeydown(event);
  }}
/>

{#if mobileNavigationOpen}
  <button
    class="mobile-sidebar-backdrop"
    type="button"
    aria-label={$t('ui_close_direct_message_navigation_d4dda6dd')}
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
    class:open={mobileNavigationOpen}
    class="home-sidebar"
    id="direct-message-navigation"
    role={mobileNavigationOpen ? 'dialog' : undefined}
    aria-modal={mobileNavigationOpen ? 'true' : undefined}
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
        bind:this={mobileNavigationClose}
        class="mobile-sidebar-close"
        type="button"
        aria-label={$t('ui_close_home_navigation_fe078c65')}
        onclick={() => closeMobileNavigation()}
      >
        ×
      </button>
    </header>
    <nav class="home-nav" aria-label={$t('ui_home_3a786953')}>
      <a href={resolve('/home')} onclick={() => closeMobileNavigation(false)}
        ><Icon name="home" size={18} />{$t('ui_overview_d4b1ea57')}</a
      >
      <a href={resolve('/home/friends')} onclick={() => closeMobileNavigation(false)}
        ><Icon name="users" size={18} />{$t('ui_friends_requests_2cb22f8b')}</a
      >
    </nav>
    <div class="home-sidebar-heading">
      <span>{$t('ui_direct_messages_95e66705')}</span>
      <button
        type="button"
        aria-label={$t('ui_new_message_78f5975a')}
        title={$t('ui_new_message_78f5975a')}
        onclick={() => {
          closeMobileNavigation(false);
          newMessageOpen = true;
        }}><Icon name="plus" size={17} /></button
      >
    </div>
    <nav class="home-dm-list" aria-label={$t('ui_direct_messages_95e66705')}>
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
          <strong>{dmTitle(item)}</strong>
          {#if unreadFor(item)?.unread}<small class="unread-badge"
              >{Math.max(1, unreadFor(item)?.mention_count ?? 0)}</small
            >{/if}
        </a>
      {:else}
        <p>{$t('ui_no_conversations_yet_52a87373')}</p>
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
        <strong
          >{currentUser?.display_name ??
            currentUser?.username ??
            $t('ui_your_account_dbb5f637')}</strong
        >
        <PresencePicker value={presencePreference} onChange={setMyPresence} />
      </div>
      <a
        class="icon-button"
        href={resolve('/settings')}
        aria-label={$t('ui_user_settings_2b363e87')}
      >
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
            ? $t('ui_close_direct_message_navigation_d4dda6dd')
            : $t('ui_open_direct_message_navigation_c52e5b0d')}
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
              <strong>{$t('ui_conversation_ccca1817')}</strong>
            {/if}
            {#if groupConversation && channel}
              <span>{groupDmSubtitle(channel)}</span>
            {:else if recipient}
              <span
                >{recipient.profile_resolved === false
                  ? $t('ui_profile_unavailable_158e5a22')
                  : recipient.handle}</span
              >
            {/if}
          </div>
        </div>
      </div>
      <div class="channel-header-actions">
        <button
          class="icon-button"
          title={$t('chat_mark_read')}
          aria-label={$t('chat_mark_read')}
          onclick={async () => {
            if (!channel) return;
            try {
              await markConversationsRead({ channel: entityRef(channel) });
              manualUnreadPaused = false;
            } catch (caught) {
              readStateWarning = userErrorMessage(
                caught,
                'Could not mark conversation read. Try again.'
              );
            }
          }}>✓</button
        >
        {#if channel && (channel.encryption_mode === 'e2ee' || (!groupConversation && e2eeActivationEnabled))}
          <button
            class:active={channel.encryption_mode === 'e2ee'}
            class:e2ee-status-button={channel.encryption_mode === 'e2ee'}
            class="icon-button"
            type="button"
            disabled={groupBusy}
            aria-label={channel.encryption_mode === 'e2ee'
              ? $t('ui_end_to_end_encryption_is_on_3e72f343')
              : $t('ui_turn_on_end_to_end_encryption_b2438d75')}
            title={channel.encryption_mode === 'e2ee'
              ? $t('ui_end_to_end_encrypted_f01afb7a')
              : $t('ui_turn_on_end_to_end_encryption_b2438d75')}
            onclick={channel.encryption_mode === 'e2ee' ? showEncryptionInfo : enableEncryption}
          >
            <Icon name="lock" size={18} />
            {#if channel.encryption_mode === 'e2ee'}
              <span
                >{channel.encryption_state === 'active'
                  ? $t('ui_encrypted_f45aef6e')
                  : $t('ui_rekey_needed_74d26182')}</span
              >
            {/if}
          </button>
        {/if}
        {#if channel?.encryption_mode === 'e2ee'}
          <button
            class="icon-button"
            type="button"
            aria-label={$t('ui_manage_apps_in_this_encrypted_conversation_f0f5ed22')}
            title={$t('ui_encrypted_apps_e70c2756')}
            onclick={() => (encryptedAppsOpen = true)}
          >
            <Icon name="shield" size={18} />
          </button>
        {/if}
        {#if groupConversation}
          <button
            class="icon-button"
            type="button"
            aria-label={$t('ui_group_settings_ba4062f8')}
            title={$t('ui_group_settings_ba4062f8')}
            onclick={openGroupSettings}
          >
            <Icon name="users" size={19} />
          </button>
        {/if}
        <button
          class:active={pinsOpen}
          class="icon-button"
          type="button"
          aria-label={pinsOpen
            ? $t('ui_hide_pinned_messages_9ee082b6')
            : $t('ui_show_pinned_messages_26b577c8')}
          aria-pressed={pinsOpen}
          title={$t('ui_pinned_messages_4c0dbc8c')}
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
            >{$t('ui_start_call_99e5d804')}</button
          >
        {/if}
      </div>
    </header>
    {#if readStateWarning}
      <div class="read-state-warning" role="status">
        <span>{readStateWarning}</span>
        <button type="button" onclick={() => void readAcknowledgements.retryNow()}
          >{$t('ui_retry_now_5148c3e2')}</button
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
            >{groupConversation
              ? $t('ui_group_call_incoming_0c883124')
              : `${conversationTitle} is calling`}</strong
          >
          <button disabled={callBusy} onclick={() => callAction('accept')}
            >{$t('ui_accept_89713b9c')}</button
          >
          <button disabled={callBusy} class="decline" onclick={() => callAction('decline')}
            >{$t('ui_decline_a2d285b3')}</button
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
            >{$t('ui_end_call_for_everyone_1dc035bc')}</button
          >
        </div>
      {/if}
      <div
        class="message-list"
        aria-live="polite"
        role="log"
        aria-label={$t('ui_direct_messages_95e66705')}
      >
        {#if error}<p class="form-error message-error" role="alert">{error}</p>{/if}
        {#snippet emptyTimeline()}
          {#if channelReady && channel}
            {#if channel.history_truncated}
              <aside class="history-boundary" role="status">
                <strong>{$t('ui_no_recent_messages_are_cached_04baa15b')}</strong>
                <span> {$t('ui_this_instance_keeps_a_rolling_cache_of_this_r_bafb903c')} </span>
              </aside>
            {:else}
              <section class="channel-welcome">
                <span class="welcome-mark direct" aria-hidden="true">@</span>
                <h2>{conversationTitle}</h2>
                <p>
                  {$t('ui_this_is_the_beginning_of_your_value0_conversa_6cfa1b37', {
                    value0: String(groupConversation ? 'group' : 'direct')
                  })}
                </p>
              </section>
            {/if}
          {/if}
        {/snippet}
        {#key `${dmId}:${historyRevision}`}
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
            onRead={acknowledgeVisible}
            canJumpToRead={Boolean(visitReadRef)}
            forceJumpToLatest={manualUnreadPaused}
            onJumpToRead={() => visitReadRef && jumpHistory(entityRef(visitReadRef))}
            onJumpToLatest={() => jumpHistory(null)}
            {jumpingHistory}
            onBottomChange={timelineBottomChanged}
            label={$t('ui_direct_messages_95e66705')}
          >
            {#snippet historyStart()}
              {#if channel?.history_truncated && !authorityHistoryComplete}
                <aside class="history-boundary" role="status">
                  <strong>{$t('ui_recent_history_starts_here_ae1d1241')}</strong>
                  <span> {$t('ui_this_instance_keeps_a_rolling_cache_of_this_r_fea82511')} </span>
                </aside>
              {:else if authorityHistoryComplete}
                <aside class="history-boundary" role="status">
                  <strong>{$t('ui_this_is_the_beginning_of_your_direct_conversa_293f3a61')}</strong>
                </aside>
              {/if}
            {/snippet}
            {#snippet renderItem(item)}
              {#if item.kind === 'ephemeral'}
                <EphemeralInteractionTray
                  channelRef={channel ? entityRef(channel) : ''}
                  responseRef={item.responseRef}
                />
              {:else if item.kind === 'day'}
                <div class="timeline-divider" role="separator"><span>{item.label}</span></div>
              {:else if item.kind === 'new'}
                <div class="timeline-divider new" role="separator"><span>{item.label}</span></div>
              {:else}
                <MessageRow
                  onMarkUnread={markMessageUnread}
                  message={item.message}
                  compact={item.compact}
                  mentionUsers={entities.users.values}
                  componentChannels={directMessages}
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
                  onForward={forwardDestinations.length &&
                  forwardUnavailableReason(item.message) === null
                    ? requestForward
                    : undefined}
                  forwardUnavailableReason={forwardUnavailableReason(item.message)}
                  {applicationCommands}
                  contextCommandAccountRef={currentUser ? entityRef(currentUser) : null}
                  onApplicationCommand={executeContextCommand}
                  resolveInteractionRequest={componentInteractionRequest}
                  onJumpToReference={jumpToReply}
                  onTogglePin={canPinMessages ? togglePinnedMessage : undefined}
                  canClosePoll={Boolean(
                    item.message.poll &&
                    item.message.author_id === currentUser?.id &&
                    item.message.author_domain === currentUser?.origin_domain
                  )}
                  onMessageUpdate={reconcile}
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
      {#if commandNotice}<span class="typing-line" role="status">{commandNotice}</span>{/if}
      {#if replyingMessage}
        <div class="reply-banner">
          <span>
            {$t('ui_replying_to_2e89f01d')}
            <strong>{userDisplayName(replyingMessage.author)}</strong>
          </span>
          <div class="reply-banner-actions">
            <button type="button" onclick={cancelReply} aria-label={$t('ui_cancel_reply_2355f731')}
              >×</button
            >
          </div>
        </div>
      {/if}
      {#if editingMessage}
        <div class="editing-banner">
          <span
            >{$t('ui_editing_message_7dbfd44a')}
            <small>{$t('ui_your_draft_and_attachments_are_saved_88ba9911')}</small></span
          >
          <button type="button" onclick={finishEditing}>{$t('ui_cancel_19766ed6')}</button>
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
            composerInput?.focus();
          }}
        />
        <ComposerActionMenu
          canAttach={!editingMessage && !selectedApplicationCommand}
          canPoll={canCreatePoll && !editingMessage && !selectedApplicationCommand}
          disabled={busy ||
            !channelReady ||
            !channel ||
            Boolean(editingMessage || selectedApplicationCommand)}
          onAttach={() => fileInput?.click()}
          onPoll={() => {
            gifPickerOpen = false;
            emojiPickerOpen = false;
            pollDialogOpen = true;
          }}
        />
        {#if selectedApplicationCommand}
          <CommandOptionComposer
            commandName={selectedApplicationCommand.name}
            commandDisplayName={localizedCommandName(selectedApplicationCommand)}
            applicationName={selectedApplicationCommand.application_name}
            options={selectedApplicationCommand.options ?? []}
            values={commandOptionValues}
            users={[currentUser, ...(channel?.recipients ?? [])].filter(
              (user): user is UserSummary => Boolean(user)
            )}
            channels={directMessages}
            attachments={uploads.flatMap((upload) =>
              upload.status === 'ready' && upload.attachmentId
                ? [
                    {
                      id: upload.attachmentId,
                      label: upload.file.name,
                      filename: upload.file.name,
                      contentType: upload.file.type
                    }
                  ]
                : []
            )}
            disabled={busy}
            onValueChange={(name, value) =>
              (commandOptionValues = { ...commandOptionValues, [name]: value })}
            onAttachmentFiles={(option, path, files) =>
              void queueFiles(files, {
                path,
                fileTypes: option.file_types,
                command: selectedApplicationCommand!
              })}
            onAutocomplete={autocompleteCommandOption}
            onSubmit={() => void send()}
            onCancel={cancelCommandComposer}
          />
        {:else}
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
            aria-label={$t('ui_direct_message_cd3e1605')}
            placeholder={`Message ${conversationTitle}`}
            rows="1"
            maxlength="4000"
          ></textarea>
        {/if}
        {#if (gifPickerEnabled || gifConfigurationError) && !editingMessage && !selectedApplicationCommand}
          <button
            class="gif-button"
            class:active={gifPickerOpen}
            type="button"
            disabled={busy || !channelReady || !channel || !gifPickerEnabled}
            aria-label={gifPickerEnabled
              ? $t('ui_choose_a_gif_261f5249')
              : $t('ui_gif_availability_could_not_be_checked_5290ae44')}
            title={gifPickerEnabled ? $t('ui_choose_a_gif_261f5249') : gifConfigurationError}
            aria-expanded={gifPickerOpen}
            onclick={() => {
              gifPickerOpen = !gifPickerOpen;
              emojiPickerOpen = false;
            }}>{$t('ui_gif_76c664ef')}</button
          >
        {/if}
        {#if !editingMessage && !selectedApplicationCommand}
          <button
            class="emoji-button"
            class:active={emojiPickerOpen}
            type="button"
            disabled={busy || !channelReady || !channel}
            aria-label={$t('ui_choose_an_emoji_or_sticker_b4c5df44')}
            title={$t('ui_emoji_and_stickers_d4c7b8e2')}
            aria-expanded={emojiPickerOpen}
            onclick={() => {
              emojiPickerOpen = !emojiPickerOpen;
              gifPickerOpen = false;
            }}>☺</button
          >
        {/if}
        <small class="composer-count"
          >{selectedApplicationCommand ? '' : `${content.length}/4000`}</small
        >
        <button
          class="send-button"
          disabled={busy ||
            !channelReady ||
            !channel ||
            uploads.some((item) => item.status === 'uploading') ||
            (selectedApplicationCommand
              ? !commandOptionsComplete(selectedApplicationCommand, commandOptionValues)
              : editingMessage
                ? !content.trim()
                : !content.trim() && !uploads.some((item) => item.status === 'ready'))}
          aria-label={$t('ui_send_message_93a26b1e')}
          title={$t('ui_send_message_93a26b1e')}
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
            {gifConfigurationLoading
              ? $t('ui_checking_ec963ffc')
              : $t('ui_retry_gif_check_b1062c5e')}
          </button>
        </p>
      {/if}
      {#if gifPickerOpen}
        <GifPicker onSelect={chooseGif} onClose={() => (gifPickerOpen = false)} />
      {/if}
      {#if emojiPickerOpen}
        <EmojiPicker
          customEmojis={pickerEmojis}
          stickers={pickerStickers}
          onSelect={chooseEmoji}
          onStickerSelect={chooseSticker}
          onClose={() => (emojiPickerOpen = false)}
        />
      {/if}
      {#if uploads.length && !editingMessage}
        <UploadPreviewTray
          {uploads}
          onRemove={removeUpload}
          onSpoiler={uploadQueue.setSpoiler}
          disabled={busy}
        />
      {/if}
    </footer>
    {#if pinsOpen}
      <PinnedMessagesPanel
        messages={pinnedMessages}
        loading={pinsLoading}
        error={pinsError}
        onClose={() => (pinsOpen = false)}
        onJump={jumpToPinnedMessage}
        onUnpin={canPinMessages ? (message) => void togglePinnedMessage(message, false) : undefined}
        onRetry={() => void loadPins()}
      />
    {/if}
  </section>
</main>

<NewMessageDialog bind:open={newMessageOpen} />

{#if pollDialogOpen}
  <CreatePollDialog
    customEmojis={pickerEmojis}
    onCreate={createPollMessage}
    onClose={() => (pollDialogOpen = false)}
  />
{/if}

{#if forwardingMessage}
  <ForwardMessageDialog
    message={forwardingMessage}
    channels={forwardDestinations}
    onForward={submitForward}
    onClose={() => (forwardingMessage = null)}
  />
{/if}

{#if profile}
  <UserProfileCard
    user={profile.user}
    presence={entities.presenceFor(profile.user)}
    x={profile.x}
    y={profile.y}
    isSelf={Boolean(currentUser && entityKey(currentUser) === entityKey(profile.user))}
    onClose={() => (profile = null)}
    {applicationCommands}
    contextCommandAccountRef={currentUser ? entityRef(currentUser) : null}
    onApplicationCommand={executeContextCommand}
  />
{/if}

<dialog bind:this={groupDialog} class="action-dialog group-dm-dialog">
  <form method="dialog" onsubmit={(event) => event.preventDefault()}>
    <header>
      <div>
        <p class="eyebrow">{$t('ui_group_conversation_b6aebd28')}</p>
        <h2>{conversationTitle}</h2>
      </div>
      <button
        class="icon-button"
        type="button"
        aria-label={$t('ui_close_7d9eb7ac')}
        onclick={() => groupDialog?.close()}>×</button
      >
    </header>

    <section class="group-dm-setting">
      <label class="form-field">
        <span>{$t('ui_group_name_762ebb70')}</span>
        <input
          bind:value={groupName}
          maxlength="100"
          placeholder={$t('ui_optional_group_name_ed1f40c9')}
        />
      </label>
      <button class="secondary-button" type="button" disabled={groupBusy} onclick={updateGroupName}>
        {$t('ui_save_name_b7297226')}
      </button>
    </section>

    <section class="group-dm-setting">
      <label class="form-field">
        <span>{$t('ui_add_a_friend_33275c37')}</span>
        <input
          bind:value={groupInviteHandle}
          placeholder="@friend@example.net"
          autocomplete="off"
        />
        <small>{$t('ui_any_member_can_invite_one_of_their_existing_f_112079d9')}</small>
      </label>
      <button
        class="secondary-button"
        type="button"
        disabled={groupBusy || !groupInviteHandle.trim()}
        onclick={addGroupMember}>{$t('ui_add_9fd728c6')}</button
      >
    </section>

    <section class="group-dm-setting e2ee-setting">
      <div>
        <strong>{$t('ui_end_to_end_encryption_1b3f0f02')}</strong>
        {#if channel?.encryption_mode === 'e2ee'}
          <small>
            {channel.encryption_state === 'rekeying'
              ? $t('ui_paused_the_member_list_changed_and_requires_f_53f1fd53')
              : $t('ui_on_encrypted_with_participant_identities_unve_ff54b617')}
          </small>
          {#if e2eeSafetyNumber}<code class="e2ee-safety-number">{e2eeSafetyNumber}</code>{/if}
        {:else}
          <small> {$t('ui_optional_and_permanent_disables_server_messag_7a6492df')} </small>
        {/if}
      </div>
      {#if channel?.encryption_mode !== 'e2ee' && groupOwner && e2eeActivationEnabled}
        <button
          class="secondary-button"
          type="button"
          disabled={groupBusy}
          onclick={enableEncryption}>{$t('ui_turn_on_5a1f096a')}</button
        >
      {:else if channel?.encryption_state === 'rekeying' && groupOwner}
        <button
          class="secondary-button"
          type="button"
          disabled={groupBusy}
          onclick={rekeyEncryption}>{$t('ui_secure_changes_41e8dec7')}</button
        >
      {/if}
      {#if channel?.encryption_mode === 'e2ee'}
        <button
          class="secondary-button"
          type="button"
          onclick={() => {
            groupDialog?.close();
            encryptedAppsOpen = true;
          }}>{$t('ui_manage_apps_75f86663')}</button
        >
      {/if}
    </section>

    <section class="group-dm-members" aria-labelledby="group-members-heading">
      <div class="dm-friend-picker-heading">
        <strong id="group-members-heading">{$t('ui_members_1044a4c0')}</strong>
        <small>{channel ? (channel.recipients?.length ?? 0) + 1 : 0}</small>
      </div>
      {#if currentUser}
        <div class="group-dm-member">
          <span class="avatar avatar-small">
            {#if currentUser.avatar_hash}
              <img src={assetUrl(currentUser.avatar_hash, 'thumbnail_128', currentUser)} alt="" />
            {:else}{currentUser.username.slice(0, 1).toUpperCase()}{/if}
          </span>
          <span
            ><strong>{userDisplayName(currentUser)}</strong><small>{$t('ui_you_08b04193')}</small
            ></span
          >
          {#if channel?.owner_id === currentUser.id && channel.owner_domain === currentUser.origin_domain}
            <small class="owner-badge">{$t('ui_owner_4b1b8aa3')}</small>
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
            <small class="owner-badge">{$t('ui_owner_4b1b8aa3')}</small>
          {:else if groupOwner}
            <button
              class="group-member-action"
              type="button"
              disabled={groupBusy}
              aria-label={`Remove ${userDisplayName(member)} from the group`}
              onclick={() => removeGroupMember(member)}
              ><Icon name="trash" size={16} />{$t('ui_remove_c3812fc4')}</button
            >
          {/if}
        </div>
      {/each}
    </section>
    {#if groupError}<p class="form-error" role="alert">{groupError}</p>{/if}
    <footer>
      <button class="group-leave-button" type="button" disabled={groupBusy} onclick={leaveGroup}>
        <Icon name="logout" size={17} />{$t('ui_leave_group_3475393d')}
      </button>
      <button class="secondary-button" type="button" onclick={() => groupDialog?.close()}
        >{$t('ui_done_11a6767d')}</button
      >
    </footer>
  </form>
</dialog>

{#if channel}
  <DmBotE2eeParticipation
    open={encryptedAppsOpen}
    channelRef={entityRef(channel)}
    channelName={conversationTitle}
    onClose={() => (encryptedAppsOpen = false)}
  />
{/if}
