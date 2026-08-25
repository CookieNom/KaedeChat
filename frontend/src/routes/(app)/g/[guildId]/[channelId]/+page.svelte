<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { apiErrorMessage } from '$lib/api/errors';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import type { GifResult } from '$lib/chat/gifs';
  import {
    customEmojiToken,
    loadUnicodeEmojis,
    unicodeEmojiCompletions,
    type CustomEmojiOption,
    type EmojiOption
  } from '$lib/chat/emojis';
  import { stickerOptions, type StickerOption } from '$lib/chat/stickers';
  import { autosizeTextarea } from '$lib/ui/autosize';
  import {
    channelCompletions,
    completionAt,
    memberCompletions,
    roleCompletions,
    replaceCompletion
  } from '$lib/chat/completion';
  import { mentionsUser } from '$lib/chat/mentions';
  import {
    commandCompletions,
    commandInvocation,
    commandOptionsComplete,
    commandStringOptions,
    type ApplicationCommand
  } from '$lib/chat/application-commands';
  import { messageSearchUserCandidates } from '$lib/chat/message-search';
  import { applyReactionUpdate, type ReactionUpdate } from '$lib/chat/reaction-state';
  import { guildModerationActions } from '$lib/chat/moderation';
  import { guildHistorySyncGuidance, guildReplicaSyncGuidance } from '$lib/chat/guild-sync';
  import {
    selfModerationExpiryDelay,
    selfModerationGuidance,
    selfModerationRetryDelay,
    selfModerationTimerDelay,
    type SelfModerationStatus
  } from '$lib/chat/self-moderation';
  import {
    discardAttachments,
    pendingMessageSend,
    type PendingMessageSend,
    withoutSubmittedUploads
  } from '$lib/chat/outbox';
  import {
    firstNavigableChannel,
    groupChannels,
    moveChannel,
    type ChannelDropPlacement
  } from '$lib/chat/channels';
  import {
    activeThreadsForParent,
    createThread,
    createThreadFromMessage,
    fetchActiveGuildThreads,
    fetchChannel,
    fetchThreadMembers,
    fetchThreads,
    FORUM_POST_CONTENT_MAX_LENGTH,
    forumDefaultSort,
    isForumChannel,
    isThreadChannel,
    isThreadParentChannel,
    mergeThreadIntoChannels,
    ordinaryGuildChannels,
    parseNativeThreadCommand,
    parseCreatedThread,
    setThreadMember,
    setThreadMembership,
    threadParentAllowsChildCreation,
    threadMembersUpdateRemovesUser,
    threadRequiresE2EEActivation,
    updateThread
  } from '$lib/chat/threads';
  import {
    compareMessages,
    failPendingMessage,
    mergeMessageSnapshot,
    messageDeliveryFailure,
    messageReferenceTarget,
    reconcileMessage,
    resolvedReferencedMessage
  } from '$lib/chat/reconcile';
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
    GuildSticker,
    GuildMemberSummary,
    Message,
    ReadStateStatus,
    Role,
    ThreadMember,
    UserSummary
  } from '$lib/chat/types';
  import { userDisplayName } from '$lib/chat/users';
  import GuildRail from '$lib/components/GuildRail.svelte';
  import {
    GATEWAY_SESSION_RESET_EVENT,
    type Dispatch,
    type GatewayClient
  } from '$lib/gateway/client';
  import { authenticatedGateway } from '$lib/gateway/runtime.svelte';
  import { Permission } from '$lib/generated/permissions';
  import ComposerAutocomplete, {
    type Completion
  } from '$lib/components/ComposerAutocomplete.svelte';
  import CommandOptionComposer from '$lib/components/CommandOptionComposer.svelte';
  import CreateThreadDialog from '$lib/components/CreateThreadDialog.svelte';
  import ForumView from '$lib/components/ForumView.svelte';
  import GuildMemberRoster from '$lib/components/GuildMemberRoster.svelte';
  import { memberRoleColor } from '$lib/chat/members';
  import EmojiPicker from '$lib/components/EmojiPicker.svelte';
  import GifPicker from '$lib/components/GifPicker.svelte';
  import Icon from '$lib/components/Icon.svelte';
  import MessageRow from '$lib/components/MessageRow.svelte';
  import MessageSearch from '$lib/components/MessageSearch.svelte';
  import PinnedMessagesPanel from '$lib/components/PinnedMessagesPanel.svelte';
  import PresencePicker from '$lib/components/PresencePicker.svelte';
  import UploadPreviewTray from '$lib/components/UploadPreviewTray.svelte';
  import ThreadHeader from '$lib/components/ThreadHeader.svelte';
  import ThreadsPanel from '$lib/components/ThreadsPanel.svelte';
  import UserProfileCard from '$lib/components/UserProfileCard.svelte';
  import VirtualMessageList from '$lib/components/VirtualMessageList.svelte';
  import {
    decryptConversationMessages,
    initializeE2EE,
    type KaedeE2EEClient
  } from '$lib/e2ee/client';
  import { acknowledgeEncryptedRoom, confirmEncryptedRoomJoin } from '$lib/e2ee/disclosures';
  import { uploadEncryptedChannelFile } from '$lib/e2ee/media';
  import { uploadChannelFile, type PendingUpload } from '$lib/media/uploads';
  import { assetUrl } from '$lib/media/assets';
  import {
    channelUnreadPresentation,
    compactBadgeCount,
    directMessageUnreadCount,
    guildMentionCount
  } from '$lib/notifications/counts';
  import { applyReadStateDispatch, type ReadStateDispatch } from '$lib/notifications/read-state';
  import { ReadAcknowledgementQueue } from '$lib/notifications/read-ack';
  import {
    channelSettingsPath,
    directMessagePath,
    guildChannelPath,
    guildSettingsPath
  } from '$lib/navigation/routes';
  import { chatEntities as entities } from '$lib/stores/entities.svelte';
  import { placeContextMenu } from '$lib/ui/context-menu';
  import { DISMISS_FLOATING_LAYERS_EVENT, dismissFloatingLayers } from '$lib/ui/floating-layers';
  import { portal } from '$lib/ui/portal';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import VoiceDock from '$lib/voice/VoiceDock.svelte';
  import {
    applyVoiceStateUpdate,
    type VoiceOccupant,
    type VoiceStateUpdate
  } from '$lib/voice/occupancy';
  import { onMount, tick, untrack } from 'svelte';
  import { SvelteMap, SvelteSet } from 'svelte/reactivity';

  interface CreatedInvite {
    code: string;
  }

  const nativeThreadOptions = [
    {
      type: 'string' as const,
      name: 'name',
      description: 'The thread name',
      required: true,
      max_length: 100
    },
    {
      type: 'string' as const,
      name: 'message',
      description: 'Type the first message in your thread',
      required: true,
      max_length: 4000
    }
  ];

  const guildId = $derived(page.params.guildId ?? '');
  const channelId = $derived(page.params.channelId ?? '');
  const localDomain = typeof window === 'undefined' ? '' : window.location.hostname;
  let guild = $state<Guild | null>(null);
  let selfModeration = $state<SelfModerationStatus | null>(null);
  let selfModerationWarning = $state('');
  let selfModerationRequest = 0;
  let selfModerationExpiryTimer: number | null = null;
  let selfModerationRetryTimer: number | null = null;
  const guilds = $derived(entities.guilds.values);
  let loadedRouteChannel = $state<Channel | null>(null);
  const messages = $derived(
    entities.messages.values.filter((message) =>
      matchesEntityRef(
        channelId,
        { id: message.channel_id, origin_domain: message.channel_domain },
        localDomain
      )
    )
  );
  const readStates = $derived(entities.readStates.values);
  const members = $derived(
    entities.members.values.filter((member) =>
      matchesEntityRef(
        guildId,
        { id: member.guild_id, origin_domain: member.guild_domain },
        localDomain
      )
    )
  );
  const currentUser = $derived(entities.currentUser);
  const messageSearchUsers = $derived.by(() => {
    const guildChannelKeys = new Set((guild?.channels ?? []).map((item) => entityKey(item)));
    const loadedGuildAuthors = entities.messages.values.flatMap((message) =>
      message.author &&
      guildChannelKeys.has(
        entityKey({ id: message.channel_id, origin_domain: message.channel_domain })
      )
        ? [message.author]
        : []
    );
    return messageSearchUserCandidates([
      currentUser,
      ...members.map((member) => member.user),
      ...loadedGuildAuthors
    ]);
  });
  const homeUnreadCount = $derived(directMessageUnreadCount(readStates));
  let content = $state('');
  let applicationCommands = $state<ApplicationCommand[]>([]);
  let selectedApplicationCommand = $state<ApplicationCommand | null>(null);
  let nativeThreadComposer = $state(false);
  let commandOptionValues = $state<Record<string, string>>({});
  let commandNotice = $state('');
  let gifPickerEnabled = $state(false);
  let gifPickerOpen = $state(false);
  let messageSearchOpen = $state(false);
  let gifConfigurationError = $state('');
  let gifConfigurationLoading = $state(false);
  let e2eeActivationEnabled = $state(false);
  let featureController: AbortController | null = null;
  let emojiPickerOpen = $state(false);
  let availableEmojis = $state<CustomEmoji[]>([]);
  let availableStickers = $state<GuildSticker[]>([]);
  let unicodeEmojis = $state<EmojiOption[]>([]);
  let emojiCatalogLoading = false;
  let slowmodeRemaining = $state(0);
  let slowmodeTimer: number | null = null;
  let error = $state('');
  let busy = $state(false);
  let channelReady = $state(false);
  let forumPosts = $state<Channel[]>([]);
  let forumLoading = $state(false);
  let forumLoadingMore = $state(false);
  let forumError = $state('');
  let forumHasMore = $state(false);
  let forumCursor = $state('');
  let forumFilterState = $state<{
    query: string;
    selectedTagIds: string[];
    sort: 'recent_activity' | 'creation_date';
  }>({ query: '', selectedTagIds: [], sort: 'recent_activity' });
  let forumRequestSequence = 0;
  let forumRefreshTimer: number | null = null;
  let forumPostBusy = $state(false);
  let threadMembers = $state<ThreadMember[]>([]);
  let threadActionBusy = $state(false);
  let threadEncryptionBusy = $state(false);
  let threadEncryptionStatus = $state('');
  let threadCreateSource = $state<Message | null>(null);
  let threadCreateBusy = $state(false);
  let threadCreateError = $state('');
  let threadDirectoryActive = $state<Channel[]>([]);
  let threadDirectoryArchived = $state<Channel[]>([]);
  let threadDirectoryOpen = $state(false);
  let threadDirectoryLoading = $state(false);
  let threadDirectoryLoadingMore = $state(false);
  let threadDirectoryActiveHasMore = $state(false);
  let threadDirectoryArchivedHasMore = $state(false);
  let threadDirectoryActiveCursor = $state('');
  let threadDirectoryArchivedCursor = $state('');
  let threadDirectoryBusy = $state(false);
  let typing = $state('');
  let typingParticipants = $state<TypingParticipant[]>([]);
  let replyingMessage = $state<Message | null>(null);
  let replyNotify = $state(true);
  let pinnedMessages = $state<Message[]>([]);
  let pinsOpen = $state(false);
  let pinsLoading = $state(false);
  let pinsError = $state('');
  let hasEarlier = $state(true);
  let loadingEarlier = $state(false);
  let hasLater = $state(false);
  let loadingLater = $state(false);
  let lastTypingAt = 0;
  let loadGeneration = 0;
  let snapshotGeneration = 0;
  let typingTimer: number | null = null;
  let gateway: GatewayClient | null = null;
  let subscribedGuildRef = '';
  let lastMemberRefreshAt = 0;
  let dispatchBuffer: Dispatch[] | null = null;
  let uploads = $state<PendingUpload[]>([]);
  let e2eeClient = $state<KaedeE2EEClient | null>(null);
  let e2eeSafetyNumber = $state('');
  let fileInput = $state<HTMLInputElement | null>(null);
  let composerInput = $state<HTMLTextAreaElement | null>(null);
  let autocomplete = $state<{ handleKeydown(event: KeyboardEvent): boolean } | null>(null);
  let editingMessage = $state<Message | null>(null);
  let composerDraftBeforeEdit = $state<{ content: string; cursor: number } | null>(null);
  let composerCursor = $state(0);
  let completionActive = $state(0);
  let completionOpen = $state(false);
  let timelineAtBottom = $state(false);
  let channelMenu = $state<{ channel: Channel | null; x: number; y: number } | null>(null);
  let channelMenuElement = $state<HTMLElement | null>(null);
  let channelMenuReturnFocus: HTMLElement | null = null;
  let channelDialogOpen = $state(false);
  let channelDialogTarget = $state<Channel | null>(null);
  let channelDialogName = $state('');
  let channelDialogType = $state(0);
  let channelDialogParent = $state('');
  let channelDialogBusy = $state(false);
  let channelDialogError = $state('');
  let channelDialogInput = $state<HTMLInputElement | null>(null);
  let channelDialogElement = $state<HTMLElement | null>(null);
  let channelDialogReturnFocus: HTMLElement | null = null;
  let channelDeleteTarget = $state<Channel | null>(null);
  let channelDeleteBusy = $state(false);
  let channelDeleteDialog = $state<HTMLElement | null>(null);
  let channelDeleteCancel = $state<HTMLButtonElement | null>(null);
  let channelDeleteReturnFocus: HTMLElement | null = null;
  let channelDeleteGeneration = 0;
  let inviteDialogOpen = $state(false);
  let inviteDialogBusy = $state(false);
  let inviteDialogError = $state('');
  let inviteLink = $state('');
  let inviteDialogElement = $state<HTMLElement | null>(null);
  let inviteDialogClose = $state<HTMLButtonElement | null>(null);
  let inviteDialogReturnFocus: HTMLElement | null = null;
  let inviteDialogGeneration = 0;
  let draggedChannelKey = $state<string | null>(null);
  let dragOverChannelKey = $state<string | null>(null);
  let reorderingChannels = $state(false);
  let channelOrderStatus = $state('');
  let channelReorderGeneration = 0;
  let mobileNavigationOpen = $state(false);
  let mobileNavigationToggle = $state<HTMLButtonElement | null>(null);
  let mobileNavigationDrawer = $state<HTMLElement | null>(null);
  let mobileNavigationClose = $state<HTMLButtonElement | null>(null);
  let memberRosterOpen = $state(true);
  let profile = $state<{ user: UserSummary; x: number; y: number } | null>(null);
  let moderationDialog = $state<{
    user: UserSummary;
    action: 'kick' | 'timeout' | 'ban';
  } | null>(null);
  let moderationReason = $state('');
  let moderationDuration = $state('86400');
  let moderationBusy = $state(false);
  let moderationError = $state('');
  let moderationController: AbortController | null = null;
  let moderationGeneration = 0;
  let presencePreference = $state<'online' | 'idle' | 'dnd' | 'invisible'>('online');
  let readStateWarning = $state('');
  let voiceOccupancy = $state<Record<string, VoiceOccupant[]>>({});
  let voiceOccupancyErrors = $state<Record<string, string>>({});
  let voiceOccupancyLoading = $state<Record<string, boolean>>({});
  let voiceOccupancyVersion = 0;
  let voiceRefreshSequence = 0;
  let draggedVoiceMember = $state<{
    occupant: VoiceOccupant;
    user: UserSummary;
    source: Channel;
  } | null>(null);
  let voiceDropChannelKey = $state<string | null>(null);
  let voiceMemberMenu = $state<{
    occupant: VoiceOccupant;
    user: UserSummary;
    source: Channel;
    x: number;
    y: number;
  } | null>(null);
  let voiceMemberMenuElement = $state<HTMLElement | null>(null);
  let voiceModerationBusy = $state(false);
  const uploadControllers = new SvelteMap<string, AbortController>();
  const pendingSends = new SvelteMap<string, PendingMessageSend>();
  const collapsedCategories = new SvelteSet<string>();
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

  const channel = $derived(
    guild?.channels?.find((item) => matchesEntityRef(channelId, item, localDomain)) ??
      loadedRouteChannel
  );
  const parentChannel = $derived(
    channel?.parent_id && guild
      ? (guild.channels?.find(
          (item) => item.id === channel.parent_id && item.origin_domain === channel.parent_domain
        ) ?? null)
      : null
  );
  const forumParent = $derived(isForumChannel(parentChannel) ? parentChannel : null);
  const threadDirectoryParent = $derived(
    channel && (channel.type === 0 || channel.type === 5)
      ? channel
      : parentChannel && (parentChannel.type === 0 || parentChannel.type === 5)
        ? parentChannel
        : null
  );
  const forumDefaultReaction = $derived.by(() => {
    const reaction = forumParent?.default_reaction_emoji;
    if (reaction?.emoji_name) return reaction.emoji_name;
    if (!reaction?.emoji_id) return '👍';
    const emoji = availableEmojis.find(
      (item) =>
        item.id === reaction.emoji_id &&
        item.guild_id === forumParent?.guild_id &&
        item.guild_domain === forumParent?.guild_domain
    );
    return emoji ? customEmojiToken(emoji) : '';
  });
  const forumStarterMessage = $derived(
    channel && isThreadChannel(channel)
      ? (channel.starter_message ?? messages.find((message) => message.message_type === 0) ?? null)
      : null
  );
  const threadTimelineStarter = $derived.by(() => {
    if (!channel || !isThreadChannel(channel) || !channel.starter_message) return null;
    const starter = channel.starter_message;
    return messages.some((message) => entityKey(message) === entityKey(starter)) ? null : starter;
  });
  const currentThreadMember = $derived.by(() => {
    if (!channel || !isThreadChannel(channel) || !currentUser) return null;
    if (channel.member) return channel.member;
    return (
      threadMembers.find((member) => {
        if (member.user) return entityKey(member.user) === entityKey(currentUser);
        return (
          member.user_id === currentUser.id && member.user_domain === currentUser.origin_domain
        );
      }) ?? null
    );
  });
  const currentThreadJoined = $derived(Boolean(currentThreadMember));
  const currentThreadNotificationLevel = $derived(
    currentThreadMember?.notification_level ?? 'inherit'
  );
  const pickerEmojis = $derived.by((): CustomEmojiOption[] => {
    if (!guild || !channel) return [];
    const activeGuild = guild;
    const mayUseExternal = channelHasPermission(channel, Permission.USE_EXTERNAL_EMOJIS);
    return availableEmojis
      .filter(
        (emoji) =>
          emoji.media_hash &&
          ((emoji.guild_id === activeGuild.id &&
            emoji.guild_domain === activeGuild.origin_domain) ||
            mayUseExternal)
      )
      .map((emoji) => ({
        ...emoji,
        url: assetUrl(emoji.media_hash ?? '', 'thumbnail_128', emoji.origin_domain),
        value: customEmojiToken(emoji)
      }))
      .filter((emoji) => Boolean(emoji.url && emoji.value));
  });
  const pickerStickers = $derived.by((): StickerOption[] => {
    if (!guild || !channel) return [];
    const mayUseExternal = channelHasPermission(channel, Permission.USE_EXTERNAL_EMOJIS);
    return stickerOptions(
      availableStickers.filter(
        (sticker) =>
          (sticker.guild_id === guild?.id && sticker.guild_domain === guild?.origin_domain) ||
          mayUseExternal
      ),
      guild
    );
  });
  const currentReadState = $derived(channel ? unreadFor(channel) : undefined);
  const channelGroups = $derived(groupChannels(guild?.channels ?? []));
  const dynamicPermission = (name: string, fallback: bigint): bigint =>
    (Permission as Record<string, bigint>)[name] ?? fallback;
  const CREATE_PUBLIC_THREADS = dynamicPermission('CREATE_PUBLIC_THREADS', 1n << 35n);
  const CREATE_PRIVATE_THREADS = dynamicPermission('CREATE_PRIVATE_THREADS', 1n << 36n);
  const MANAGE_THREADS = dynamicPermission('MANAGE_THREADS', 1n << 34n);
  const SEND_MESSAGES_IN_THREADS = dynamicPermission('SEND_MESSAGES_IN_THREADS', 1n << 38n);
  const USE_APPLICATION_COMMANDS = dynamicPermission('USE_APPLICATION_COMMANDS', 1n << 32n);
  const PIN_MESSAGES = dynamicPermission('PIN_MESSAGES', Permission.MANAGE_MESSAGES);
  const BYPASS_SLOWMODE = dynamicPermission('BYPASS_SLOWMODE', 1n << 52n);
  const canManageChannels = $derived.by(() => {
    if (!guild || guild.origin_domain !== localDomain) return false;
    if (
      currentUser &&
      guild.owner_id === currentUser.id &&
      guild.origin_domain === currentUser.origin_domain
    )
      return true;
    try {
      const permissions = BigInt(guild.permissions ?? '0');
      return Boolean(permissions & (Permission.ADMINISTRATOR | Permission.MANAGE_CHANNELS));
    } catch {
      return false;
    }
  });
  const canManageRoles = $derived.by(() => {
    if (!guild || guild.origin_domain !== localDomain) return false;
    if (
      currentUser &&
      guild.owner_id === currentUser.id &&
      (guild.owner_domain ?? guild.origin_domain) === currentUser.origin_domain
    )
      return true;
    try {
      const permissions = BigInt(guild.permissions ?? '0');
      return Boolean(permissions & (Permission.ADMINISTRATOR | Permission.MANAGE_ROLES));
    } catch {
      return false;
    }
  });
  function channelHasPermission(target: Channel, permission: bigint): boolean {
    if (!guild) return false;
    if (
      currentUser &&
      guild.owner_id === currentUser.id &&
      (guild.owner_domain ?? guild.origin_domain) === currentUser.origin_domain
    )
      return true;
    try {
      const effective = BigInt(target.permissions ?? guild.permissions ?? '0');
      return Boolean(effective & (Permission.ADMINISTRATOR | permission));
    } catch {
      return false;
    }
  }

  const replicaSyncWarning = $derived(guild ? guildReplicaSyncGuidance(guild) : null);
  const historySyncWarning = $derived(guild ? guildHistorySyncGuidance(guild) : null);
  const timeoutGuidance = $derived(selfModerationGuidance(selfModeration));

  const canCreateCurrentChannelInvite = $derived(
    Boolean(
      channel &&
      guild?.origin_domain === localDomain &&
      channel.type !== 4 &&
      channelHasPermission(channel, Permission.CREATE_INVITE)
    )
  );
  const canSendMessages = $derived(
    Boolean(
      channel &&
      (isThreadChannel(channel)
        ? (!channel.locked || channelHasPermission(channel, MANAGE_THREADS)) &&
          channelHasPermission(channel, SEND_MESSAGES_IN_THREADS)
        : channelHasPermission(channel, Permission.SEND_MESSAGES)) &&
      !threadEncryptionBusy &&
      (channel.encryption_mode !== 'e2ee' || channel.encryption_state === 'active') &&
      !threadRequiresE2EEActivation(channel)
    )
  );
  const canManageThreads = $derived(
    Boolean(channel && channelHasPermission(channel, MANAGE_THREADS))
  );
  const canEditCurrentThread = $derived(
    Boolean(
      channel &&
      isThreadChannel(channel) &&
      (canManageThreads ||
        (!channel.locked &&
          currentUser &&
          channel.owner_id === currentUser.id &&
          channel.owner_domain === currentUser.origin_domain))
    )
  );
  const canInviteThreadMembers = $derived(
    Boolean(
      channel &&
      isThreadChannel(channel) &&
      !channel.archived &&
      channelHasPermission(channel, SEND_MESSAGES_IN_THREADS) &&
      (channel.type !== 12 || canManageThreads || (channel.invitable && currentThreadJoined))
    )
  );
  const canRemoveThreadMembers = $derived(
    Boolean(
      channel &&
      !channel.archived &&
      (canManageThreads ||
        (channel.type === 12 &&
          currentUser &&
          channel.owner_id === currentUser.id &&
          channel.owner_domain === currentUser.origin_domain))
    )
  );
  const canEnableThreadEncryption = $derived(
    Boolean(
      channel &&
      isThreadChannel(channel) &&
      !channel.archived &&
      canEditCurrentThread &&
      e2eeActivationEnabled &&
      channel.encryption_mode !== 'e2ee'
    )
  );
  const canRekeyThreadEncryption = $derived(
    Boolean(
      channel &&
      isThreadChannel(channel) &&
      !channel.archived &&
      canEditCurrentThread &&
      channel.encryption_mode === 'e2ee' &&
      (channel.encryption_state === 'rekeying' || channel.encryption_state === 'failed')
    )
  );
  const canCreatePublicThreads = $derived(
    Boolean(
      channel &&
      isThreadParentChannel(channel) &&
      channel.encryption_mode !== 'e2ee' &&
      channelHasPermission(channel, CREATE_PUBLIC_THREADS)
    )
  );
  const canCreateNativeThread = $derived(
    Boolean(
      channel &&
      (channel.type === 0 || channel.type === 5) &&
      canCreatePublicThreads &&
      channelHasPermission(channel, SEND_MESSAGES_IN_THREADS)
    )
  );
  const canCreateDirectoryPublicThread = $derived(
    Boolean(
      threadDirectoryParent &&
      threadParentAllowsChildCreation(threadDirectoryParent) &&
      channelHasPermission(threadDirectoryParent, CREATE_PUBLIC_THREADS)
    )
  );
  const canCreateDirectoryPrivateThread = $derived(
    Boolean(
      threadDirectoryParent?.type === 0 &&
      threadParentAllowsChildCreation(threadDirectoryParent) &&
      channelHasPermission(threadDirectoryParent, CREATE_PRIVATE_THREADS)
    )
  );
  const canSendDirectoryStarter = $derived(
    Boolean(
      threadDirectoryParent &&
      threadDirectoryParent.encryption_mode !== 'e2ee' &&
      channelHasPermission(threadDirectoryParent, SEND_MESSAGES_IN_THREADS)
    )
  );
  const canCreateForumPost = $derived(
    Boolean(
      channel && isForumChannel(channel) && channelHasPermission(channel, Permission.SEND_MESSAGES)
    )
  );
  const canAddReactions = $derived(
    Boolean(channel && !channel.archived && channelHasPermission(channel, Permission.ADD_REACTIONS))
  );
  const canAttachFiles = $derived(
    Boolean(canSendMessages && channel && channelHasPermission(channel, Permission.ATTACH_FILES))
  );
  const canPinMessages = $derived(
    Boolean(channel && !channel.archived && channelHasPermission(channel, PIN_MESSAGES))
  );
  const canUseApplicationCommands = $derived(
    Boolean(channel && channelHasPermission(channel, USE_APPLICATION_COMMANDS))
  );
  const canBypassSlowmode = $derived(
    Boolean(channel && channelHasPermission(channel, BYPASS_SLOWMODE))
  );
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
    return resolvedReferencedMessage(message, messages);
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
      members.find((member) => member.user.id === userId && member.user.origin_domain === domain)
        ?.user ??
      entities.users.values.find(
        (candidate) => candidate.id === userId && candidate.origin_domain === domain
      );
    typingParticipants = upsertTypingParticipant(typingParticipants, {
      ref: `${userId}@${domain}`,
      name: userDisplayName(user)
    });
    refreshTyping();
  }
  const completionQuery = $derived(completionAt(content, composerCursor));
  const completionOptions = $derived.by((): Completion[] => {
    if (!completionQuery) return [];
    if (completionQuery.marker === '@')
      return [
        ...memberCompletions(members, completionQuery.query),
        ...roleCompletions(guild?.roles ?? [], completionQuery.query, {
          canMentionUnmentionable: Boolean(
            channel && channelHasPermission(channel, Permission.MENTION_EVERYONE)
          )
        })
      ];
    if (completionQuery.marker === '#')
      return channelCompletions(guild?.channels ?? [], completionQuery.query);
    if (completionQuery.marker === '/') {
      if (channel?.archived) return [];
      const commands = canUseApplicationCommands
        ? commandCompletions(applicationCommands, completionQuery.query)
        : [];
      const needle = completionQuery.query.toLocaleLowerCase();
      return canCreateNativeThread && 'thread'.includes(needle)
        ? [
            {
              value: '/thread',
              label: '/thread',
              detail: 'Create a thread · name, message',
              kind: 'application-command' as const
            },
            ...commands
          ]
        : commands;
    }
    const needle = completionQuery.query.toLocaleLowerCase();
    const custom = pickerEmojis
      .filter((emoji) => emoji.name.toLocaleLowerCase().includes(needle))
      .map((emoji) => ({
        value: emoji.value,
        label: `:${emoji.name}:`,
        detail: emoji.guild_name ?? 'Custom emoji',
        imageUrl: emoji.url,
        kind: 'custom-emoji' as const
      }));
    return [...custom, ...unicodeEmojiCompletions(unicodeEmojis, needle)];
  });

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
  const setMembers = (items: GuildMemberSummary[]) => entities.ingestMembers(items);
  const preserveHistorySync = (item: Guild): Guild => {
    const current = entities.guilds.get(entityKey(item));
    if (!current?.history_sync_status || item.history_sync_status) return item;
    return {
      ...item,
      history_sync_status: current.history_sync_status,
      history_sync_error_code: current.history_sync_error_code,
      history_sync_retry_after_ms: current.history_sync_retry_after_ms,
      history_sync_resource: current.history_sync_resource
    };
  };
  const setGuilds = (items: Guild[]) => {
    entities.ingestGuilds(items.map(preserveHistorySync));
  };

  function setCurrentChannels(channels: Channel[]) {
    if (!guild) return;
    const reconciled = [
      ...new Map(channels.map((channel) => [entityKey(channel), channel])).values()
    ];
    guild = { ...guild, channels: reconciled };
    entities.guilds.upsert(guild);
    entities.channels.upsertMany(reconciled);
  }

  function withCurrentThreads(channels: Channel[]): Channel[] {
    const threads = (guild?.channels ?? []).filter(isThreadChannel);
    return [
      ...channels,
      ...threads.filter((thread) => !channels.some((item) => entityKey(item) === entityKey(thread)))
    ];
  }

  function closeChannelMenu(restoreFocus = false) {
    const returnFocus = channelMenuReturnFocus;
    channelMenu = null;
    channelMenuReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) {
      void tick().then(() => returnFocus.focus());
    }
  }

  function openChannelMenu(
    target: Channel | null,
    anchor: HTMLElement,
    pointer?: { x: number; y: number }
  ) {
    if (channelDialogOpen || (!target && !canManageChannels)) return;
    dismissFloatingLayers();
    closeChannelMenu(false);
    const bounds = anchor.getBoundingClientRect();
    const x = pointer?.x ?? Math.min(bounds.right, bounds.left + 28);
    const y = pointer?.y ?? Math.min(bounds.bottom, bounds.top + 28);
    channelMenuReturnFocus =
      anchor.tabIndex >= 0
        ? anchor
        : (anchor.querySelector<HTMLElement>(':scope > button:not([disabled]), :scope > a[href]') ??
          activeElement());
    channelMenu = { channel: target, x, y };
    void tick().then(() => {
      if (channelMenu && channelMenuElement) {
        placeContextMenu(channelMenuElement, channelMenu.x, channelMenu.y);
        channelMenuItems()[0]?.focus();
      }
    });
  }

  function showChannelMenu(event: MouseEvent, target: Channel | null) {
    event.preventDefault();
    event.stopPropagation();
    const anchor = event.currentTarget;
    if (!(anchor instanceof HTMLElement)) return;
    const pointer =
      event.clientX === 0 && event.clientY === 0
        ? undefined
        : { x: event.clientX, y: event.clientY };
    openChannelMenu(target, anchor, pointer);
  }

  function showChannelMenuFromKeyboard(event: KeyboardEvent, target: Channel | null) {
    if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
    if (event.target !== event.currentTarget) return;
    event.preventDefault();
    event.stopPropagation();
    const anchor = event.currentTarget;
    if (anchor instanceof HTMLElement) openChannelMenu(target, anchor);
  }

  function activeElement(): HTMLElement | null {
    return document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }

  function channelMenuItems(): HTMLElement[] {
    if (!channelMenuElement) return [];
    return Array.from(
      channelMenuElement.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])')
    );
  }

  function channelMenuKeydown(event: KeyboardEvent) {
    if (!channelMenu) return;
    if (event.key === 'Escape' || event.key === 'Tab') {
      event.preventDefault();
      closeChannelMenu(true);
      return;
    }
    const items = channelMenuItems();
    if (!items.length) return;
    const focused = activeElement();
    const currentIndex = focused ? items.indexOf(focused) : -1;
    let nextIndex: number | null = null;
    if (event.key === 'ArrowDown') {
      nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
    } else if (event.key === 'ArrowUp') {
      nextIndex =
        currentIndex < 0 ? items.length - 1 : (currentIndex - 1 + items.length) % items.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = items.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    items[nextIndex]?.focus();
  }

  async function copyChannelValue(value: string, event: MouseEvent) {
    event.stopPropagation();
    closeChannelMenu(true);
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      error = 'Browser denied clipboard access. Allow clipboard permission and try again.';
    }
  }

  function absoluteChannelLink(target: Channel): string {
    if (!guild) return window.location.href;
    return `${window.location.origin}${guildChannelPath(guild, target)}`;
  }

  function guildLandingPath(targetGuild: Guild): string {
    const target = firstNavigableChannel(targetGuild.channels);
    return target ? guildChannelPath(targetGuild, target) : resolve('/home');
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

  function openChannelDialog(
    type: number,
    parent: Channel | null = null,
    target: Channel | null = null,
    invoker: HTMLElement | null = null
  ) {
    const returnFocus = channelMenuReturnFocus ?? invoker ?? activeElement();
    channelDialogReturnFocus = mobileNavigationOpen ? mobileNavigationToggle : returnFocus;
    closeChannelMenu(false);
    if (mobileNavigationOpen) closeMobileNavigation(false);
    channelDialogTarget = target;
    channelDialogName = target?.name ?? '';
    channelDialogType = target?.type ?? type;
    channelDialogParent =
      target?.parent_id && target.parent_domain
        ? `${target.parent_id}@${target.parent_domain}`
        : parent
          ? entityKey(parent)
          : '';
    channelDialogError = '';
    channelDialogOpen = true;
    void tick().then(() => channelDialogInput?.focus());
  }

  function closeChannelDialog(restoreFocus = true) {
    if (channelDialogBusy) return;
    const returnFocus = channelDialogReturnFocus;
    channelDialogOpen = false;
    channelDialogTarget = null;
    channelDialogError = '';
    channelDialogReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) {
      void tick().then(() => returnFocus.focus());
    }
  }

  function channelDialogKeydown(event: KeyboardEvent) {
    if (!channelDialogOpen || !channelDialogElement) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeChannelDialog();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      channelDialogElement.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (!channelDialogElement.contains(document.activeElement)) {
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

  async function openQuickInvite(invoker: HTMLElement) {
    if (!guild || !channel || !canCreateCurrentChannelInvite || inviteDialogBusy) return;
    const targetGuild = entityRef(guild);
    const targetChannel = channel;
    const routeGeneration = loadGeneration;
    const dialogGeneration = ++inviteDialogGeneration;
    inviteDialogReturnFocus = mobileNavigationOpen ? mobileNavigationToggle : invoker;
    if (mobileNavigationOpen) closeMobileNavigation(false);
    inviteDialogOpen = true;
    inviteDialogBusy = true;
    inviteDialogError = '';
    inviteLink = '';
    await tick();
    inviteDialogClose?.focus();
    try {
      const created = await api<CreatedInvite>(
        `/guilds/${encodeURIComponent(targetGuild)}/invites`,
        {
          method: 'POST',
          body: JSON.stringify({ channel_id: targetChannel.id })
        }
      );
      if (
        dialogGeneration !== inviteDialogGeneration ||
        routeGeneration !== loadGeneration ||
        !inviteDialogOpen
      )
        return;
      inviteLink = `${window.location.origin}/invite/${created.code}`;
    } catch (caught) {
      if (dialogGeneration !== inviteDialogGeneration || !inviteDialogOpen) return;
      inviteDialogError = userErrorMessage(caught, 'Could not create an invite. Try again.');
    } finally {
      if (dialogGeneration === inviteDialogGeneration) inviteDialogBusy = false;
    }
  }

  function closeQuickInvite(restoreFocus = true) {
    const returnFocus = inviteDialogReturnFocus;
    inviteDialogGeneration += 1;
    inviteDialogOpen = false;
    inviteDialogBusy = false;
    inviteDialogError = '';
    inviteLink = '';
    inviteDialogElement = null;
    inviteDialogClose = null;
    inviteDialogReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) {
      void tick().then(() => returnFocus.focus());
    }
  }

  async function copyQuickInvite() {
    if (!inviteLink) return;
    try {
      await navigator.clipboard.writeText(inviteLink);
      closeQuickInvite();
    } catch {
      inviteDialogError = 'Clipboard access was denied. Select and copy the link manually.';
    }
  }

  function inviteDialogKeydown(event: KeyboardEvent) {
    if (!inviteDialogOpen || !inviteDialogElement) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeQuickInvite();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      inviteDialogElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (!inviteDialogElement.contains(document.activeElement)) {
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

  function requestChannelDeletion(target: Channel) {
    channelDeleteReturnFocus = channelMenuReturnFocus ?? activeElement();
    closeChannelMenu(false);
    channelDeleteTarget = target;
    error = '';
    void tick().then(() => channelDeleteCancel?.focus());
  }

  function closeChannelDeleteDialog(restoreFocus = true) {
    if (channelDeleteBusy) return;
    const returnFocus = channelDeleteReturnFocus;
    channelDeleteTarget = null;
    channelDeleteReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) {
      void tick().then(() => returnFocus.focus());
    }
  }

  function channelDeleteDialogKeydown(event: KeyboardEvent) {
    if (!channelDeleteTarget || !channelDeleteDialog) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeChannelDeleteDialog();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
      channelDeleteDialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (!channelDeleteDialog.contains(document.activeElement)) {
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

  async function saveChannelDialog() {
    if (!guild || channelDialogBusy || !channelDialogName.trim()) return;
    const targetGuild = entityRef(guild);
    const routeGeneration = loadGeneration;
    const target = channelDialogTarget;
    const stillCurrent = () =>
      routeGeneration === loadGeneration &&
      guild !== null &&
      entityRef(guild) === targetGuild &&
      channelDialogTarget === target;
    const parent = guild.channels?.find(
      (item) => entityKey(item) === channelDialogParent && item.type === 4
    );
    channelDialogBusy = true;
    channelDialogError = '';
    let saved = false;
    try {
      if (target) {
        const updated = await api<Channel>(
          `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(target))}`,
          {
            method: 'PATCH',
            body: JSON.stringify({
              name: channelDialogName.trim(),
              parent_id: target.type === 4 ? null : (parent?.id ?? null)
            })
          }
        );
        if (!stillCurrent()) return;
        setCurrentChannels(
          (guild.channels ?? []).map((item) =>
            entityKey(item) === entityKey(updated) ? updated : item
          )
        );
      } else {
        const created = await api<Channel>(`/guilds/${encodeURIComponent(targetGuild)}/channels`, {
          method: 'POST',
          body: JSON.stringify({
            name: channelDialogName.trim(),
            type: channelDialogType,
            parent_id: channelDialogType === 4 ? null : (parent?.id ?? null)
          })
        });
        if (!stillCurrent()) return;
        setCurrentChannels([...(guild.channels ?? []), created]);
      }
      saved = true;
    } catch (caught) {
      if (!stillCurrent()) return;
      channelDialogError = userErrorMessage(
        caught,
        'Could not save the channel. Check its details and try again.'
      );
    } finally {
      if (stillCurrent()) {
        channelDialogBusy = false;
        if (saved) closeChannelDialog();
      }
    }
  }

  async function removeChannel(target: Channel) {
    if (!guild || channelDeleteBusy || channelDeleteTarget !== target) return;
    const label = target.type === 4 ? 'category' : 'channel';
    const targetGuild = entityRef(guild);
    const routeGeneration = loadGeneration;
    const deletionGeneration = ++channelDeleteGeneration;
    const stillCurrent = () =>
      deletionGeneration === channelDeleteGeneration &&
      routeGeneration === loadGeneration &&
      guild !== null &&
      entityRef(guild) === targetGuild &&
      channelDeleteTarget === target;
    channelDeleteBusy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(target))}`,
        { method: 'DELETE' }
      );
      if (!stillCurrent()) return;
      const remaining = (guild?.channels ?? []).filter(
        (item) => entityKey(item) !== entityKey(target)
      );
      setCurrentChannels(remaining);
      channelDeleteBusy = false;
      closeChannelDeleteDialog();
      if (channel && entityKey(channel) === entityKey(target)) {
        const next = remaining.find((item) => item.type !== 4);
        window.location.assign(guild && next ? guildChannelPath(guild, next) : resolve('/home'));
      }
    } catch (caught) {
      if (!stillCurrent()) return;
      error = userErrorMessage(caught, `Could not delete the ${label}. Try again.`);
    } finally {
      if (deletionGeneration === channelDeleteGeneration) channelDeleteBusy = false;
    }
  }

  async function markChannelRead(target: Channel) {
    closeChannelMenu(true);
    if (!target.last_message_id || !target.last_message_domain) return;
    try {
      await api(`/channels/${encodeURIComponent(entityRef(target))}/ack`, {
        method: 'POST',
        body: JSON.stringify({
          message_id: `${target.last_message_id}@${target.last_message_domain}`
        })
      });
      setReadStates(
        readStates.map((state) =>
          state.channel_id === target.id && state.channel_domain === target.origin_domain
            ? { ...state, mention_count: 0, unread: false }
            : state
        )
      );
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not mark the channel as read. Try again.');
    }
  }

  function channelDragStart(event: DragEvent, target: Channel) {
    if (!canManageChannels || reorderingChannels) {
      event.preventDefault();
      return;
    }
    draggedChannelKey = entityKey(target);
    closeChannelMenu();
    event.dataTransfer?.setData('text/plain', draggedChannelKey);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function channelDragEnd() {
    draggedChannelKey = null;
    dragOverChannelKey = null;
  }

  function channelDragOver(event: DragEvent, target: Channel | null) {
    if (draggedVoiceMember) {
      voiceChannelDragOver(event, target);
      return;
    }
    if (!draggedChannelKey || !canManageChannels || reorderingChannels) return;
    const dragged = guild?.channels?.find((item) => entityKey(item) === draggedChannelKey);
    if (!dragged) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    dragOverChannelKey = target ? entityKey(target) : 'ungrouped';
  }

  async function channelDrop(event: DragEvent, target: Channel | null) {
    if (draggedVoiceMember) {
      await voiceChannelDrop(event, target);
      return;
    }
    if (!draggedChannelKey || !guild || !canManageChannels || reorderingChannels) return;
    event.preventDefault();
    const dragged = guild.channels?.find((item) => entityKey(item) === draggedChannelKey);
    if (!dragged) return;
    let placement: ChannelDropPlacement = target ? 'before' : 'ungrouped';
    if (target?.type === 4 && dragged.type !== 4) {
      placement = 'inside';
    } else if (target) {
      const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
      placement = event.clientY > bounds.top + bounds.height / 2 ? 'after' : 'before';
    }
    const previous = ordinaryGuildChannels(guild.channels ?? []);
    const next = moveChannel(
      previous,
      draggedChannelKey,
      target ? entityKey(target) : null,
      placement
    );
    channelDragEnd();
    await persistChannelOrder(previous, next, 'Channel order saved.');
  }

  function channelReorderSiblings(target: Channel): Channel[] {
    if (target.type === 4) {
      return channelGroups.flatMap((group) => (group.category ? [group.category] : []));
    }
    return (
      channelGroups.find((group) =>
        group.channels.some((item) => entityKey(item) === entityKey(target))
      )?.channels ?? []
    );
  }

  function canMoveChannel(target: Channel, direction: -1 | 1): boolean {
    if (!canManageChannels || reorderingChannels) return false;
    const siblings = channelReorderSiblings(target);
    const index = siblings.findIndex((item) => entityKey(item) === entityKey(target));
    return index >= 0 && index + direction >= 0 && index + direction < siblings.length;
  }

  async function moveChannelByStep(target: Channel, direction: -1 | 1) {
    if (!guild || !canMoveChannel(target, direction)) return;
    const siblings = channelReorderSiblings(target);
    const index = siblings.findIndex((item) => entityKey(item) === entityKey(target));
    const neighbor = siblings[index + direction];
    if (!neighbor) return;
    const previous = ordinaryGuildChannels(guild.channels ?? []);
    const next = moveChannel(
      previous,
      entityKey(target),
      entityKey(neighbor),
      direction < 0 ? 'before' : 'after'
    );
    const label = target.type === 4 ? 'Category' : 'Channel';
    const destination = index + direction + 1;
    closeChannelMenu(true);
    await persistChannelOrder(
      previous,
      next,
      `${label} “${target.name}” moved ${direction < 0 ? 'up' : 'down'} to position ${destination}.`
    );
  }

  function channelOrderChanged(previous: Channel[], next: Channel[]): boolean {
    return (
      previous.length !== next.length ||
      next.some(
        (item, index) =>
          entityKey(item) !== entityKey(previous[index]) ||
          item.position !== previous[index]?.position ||
          item.parent_id !== previous[index]?.parent_id
      )
    );
  }

  async function persistChannelOrder(previous: Channel[], next: Channel[], successMessage: string) {
    if (!guild || reorderingChannels || !channelOrderChanged(previous, next)) return;
    const reorderGeneration = ++channelReorderGeneration;
    const routeGeneration = loadGeneration;
    const targetGuild = entityRef(guild);
    const stillCurrent = () =>
      reorderGeneration === channelReorderGeneration &&
      routeGeneration === loadGeneration &&
      guild !== null &&
      entityRef(guild) === targetGuild;
    setCurrentChannels(withCurrentThreads(next));
    reorderingChannels = true;
    error = '';
    channelOrderStatus = 'Saving channel order…';
    try {
      const saved = await api<Channel[]>(`/guilds/${encodeURIComponent(targetGuild)}/channels`, {
        method: 'PATCH',
        body: JSON.stringify({
          channels: next.map((item) => ({
            id: item.id,
            position: item.position,
            parent_id: item.parent_id
          }))
        })
      });
      if (!stillCurrent()) return;
      setCurrentChannels(withCurrentThreads(saved));
      channelOrderStatus = successMessage;
    } catch (caught) {
      if (!stillCurrent()) return;
      setCurrentChannels(withCurrentThreads(previous));
      error = userErrorMessage(caught, 'Could not save the channel order. Reload and try again.');
      channelOrderStatus = 'Channel order was not saved. The previous order has been restored.';
    } finally {
      if (reorderGeneration === channelReorderGeneration) reorderingChannels = false;
    }
  }

  function toggleCategory(category: Channel) {
    const key = entityKey(category);
    if (collapsedCategories.has(key)) collapsedCategories.delete(key);
    else collapsedCategories.add(key);
  }

  function unreadFor(targetChannel: {
    id: string;
    origin_domain: string;
  }): ReadStateStatus | undefined {
    return readStates.find(
      (state) =>
        state.channel_id === targetChannel.id &&
        state.channel_domain === targetChannel.origin_domain
    );
  }

  function isCurrentChannel(targetId: string, targetDomain: string): boolean {
    return matchesEntityRef(channelId, { id: targetId, origin_domain: targetDomain }, localDomain);
  }

  function dispatchTargetsCurrentChannel(targetId: string, targetDomain?: string): boolean {
    return targetDomain ? isCurrentChannel(targetId, targetDomain) : channel?.id === targetId;
  }

  function isCurrentGuild(targetId: string, targetDomain: string): boolean {
    return matchesEntityRef(guildId, { id: targetId, origin_domain: targetDomain }, localDomain);
  }

  async function refreshSelfModeration(targetGuild = guildId) {
    if (!targetGuild) return;
    const request = ++selfModerationRequest;
    const generation = loadGeneration;
    try {
      const status = await api<SelfModerationStatus>(
        `/guilds/${encodeURIComponent(targetGuild)}/members/@me/moderation-status`
      );
      if (request !== selfModerationRequest || generation !== loadGeneration) return;
      selfModeration = status;
      selfModerationWarning = '';
      scheduleSelfModerationExpiry(status);
      scheduleSelfModerationRetry(status);
    } catch (caught) {
      if (request !== selfModerationRequest || generation !== loadGeneration) return;
      // Keep an already-known active status visible while its home instance is
      // unreachable. Normal (not timed-out) users get no noisy channel banner.
      selfModerationWarning = selfModerationGuidance(selfModeration)
        ? userErrorMessage(
            caught,
            'Your timeout details could not be refreshed. Sending is still checked by the guild home.'
          )
        : '';
      scheduleSelfModerationRetry(selfModeration);
    }
  }

  function scheduleSelfModerationRetry(status: SelfModerationStatus | null) {
    if (selfModerationRetryTimer !== null) {
      window.clearTimeout(selfModerationRetryTimer);
      selfModerationRetryTimer = null;
    }
    if (document.hidden) return;
    const delay = selfModerationRetryDelay(status);
    if (delay === null) return;
    selfModerationRetryTimer = window.setTimeout(() => {
      selfModerationRetryTimer = null;
      void refreshSelfModeration();
    }, delay);
  }

  function scheduleSelfModerationExpiry(status: SelfModerationStatus | null) {
    if (selfModerationExpiryTimer !== null) {
      window.clearTimeout(selfModerationExpiryTimer);
      selfModerationExpiryTimer = null;
    }
    if (!status?.timed_out || status.timeout_indefinite || !status.timeout_until) return;
    const delay = selfModerationExpiryDelay(status);
    if (delay === null) return;
    if (delay <= 0) {
      scheduleSelfModerationRetry(null);
      selfModeration = null;
      return;
    }
    const timerDelay = selfModerationTimerDelay(status);
    if (timerDelay === null) return;
    selfModerationExpiryTimer = window.setTimeout(() => {
      selfModerationExpiryTimer = null;
      // Browsers clamp timers to roughly 24.85 days. A longer timeout must
      // remain visible and be scheduled again instead of clearing early.
      const remaining = selfModerationExpiryDelay(selfModeration);
      if (remaining !== null && remaining > 0) {
        scheduleSelfModerationExpiry(selfModeration);
        return;
      }
      scheduleSelfModerationRetry(null);
      selfModeration = null;
      void refreshSelfModeration();
    }, timerDelay);
  }

  function presenceFor(user: UserSummary) {
    return entities.presenceFor(user);
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

  function toggleMemberRoster() {
    memberRosterOpen = !memberRosterOpen;
    try {
      localStorage.setItem('kaede.member-roster.visible', String(memberRosterOpen));
    } catch {
      // The roster can still be toggled for this page when storage is unavailable.
    }
  }

  function memberFor(userId: string, userDomain: string) {
    return members.find(
      (member) => member.user.id === userId && member.user.origin_domain === userDomain
    );
  }

  function roleRank(role: Role): [number, bigint] {
    return [role.position, -BigInt(role.id)];
  }

  function compareRoleRank(left: Role, right: Role): number {
    const [leftPosition, leftId] = roleRank(left);
    const [rightPosition, rightId] = roleRank(right);
    return leftPosition - rightPosition || (leftId < rightId ? -1 : leftId > rightId ? 1 : 0);
  }

  function highestRoleFor(member: GuildMemberSummary | undefined): Role | undefined {
    if (!guild || !member) return undefined;
    const assigned = (guild.roles ?? []).filter(
      (role) => role.id === guild?.id || member.role_ids.includes(role.id)
    );
    return assigned.sort(compareRoleRank).at(-1);
  }

  function actorOutranks(user: UserSummary): boolean {
    if (!guild || !currentUser || entityKey(user) === entityKey(currentUser)) return false;
    if (
      user.id === guild.owner_id &&
      user.origin_domain === (guild.owner_domain ?? guild.origin_domain)
    )
      return false;
    const actorIsOwner =
      currentUser.id === guild.owner_id &&
      currentUser.origin_domain === (guild.owner_domain ?? guild.origin_domain);
    if (actorIsOwner) return true;
    const targetMember = memberFor(user.id, user.origin_domain);
    const actorMember = memberFor(currentUser.id, currentUser.origin_domain);
    const actorHighest =
      highestRoleFor(actorMember) ??
      (guild.roles ?? []).find((role) => role.id === guild?.actor_highest_role_id);
    const targetHighest = highestRoleFor(targetMember);
    return Boolean(
      actorHighest && targetHighest && compareRoleRank(actorHighest, targetHighest) > 0
    );
  }

  function manageableRolesFor(user: UserSummary): Role[] {
    if (!guild || !currentUser || !canManageRoles) return [];
    const actorMember = memberFor(currentUser.id, currentUser.origin_domain);
    const editingSelf = entityKey(user) === entityKey(currentUser);
    if (!editingSelf && !actorOutranks(user)) return [];
    const actorIsOwner =
      currentUser.id === guild.owner_id &&
      currentUser.origin_domain === (guild.owner_domain ?? guild.origin_domain);
    const actorHighest =
      highestRoleFor(actorMember) ??
      (guild.roles ?? []).find((role) => role.id === guild?.actor_highest_role_id);
    return (guild.roles ?? [])
      .filter(
        (role) =>
          role.id !== guild?.id &&
          (actorIsOwner || Boolean(actorHighest && compareRoleRank(actorHighest, role) > 0))
      )
      .sort((left, right) => compareRoleRank(right, left));
  }

  async function changeMemberRole(user: UserSummary, role: Role, enabled: boolean) {
    if (
      !guild ||
      !manageableRolesFor(user).some((candidate) => entityKey(candidate) === entityKey(role))
    ) {
      return;
    }
    const guildRef = encodeURIComponent(entityRef(guild));
    const memberRef = encodeURIComponent(entityRef(user));
    const roleRef = encodeURIComponent(entityRef(role));
    await api(`/guilds/${guildRef}/members/${memberRef}/roles/${roleRef}`, {
      method: enabled ? 'PUT' : 'DELETE'
    });
    setMembers(
      members.map((member) =>
        entityKey(member.user) === entityKey(user)
          ? {
              ...member,
              role_ids: enabled
                ? [...new Set([...member.role_ids, role.id])]
                : member.role_ids.filter((id) => id !== role.id)
            }
          : member
      )
    );
  }

  function occupantsFor(target: Channel): VoiceOccupant[] {
    return voiceOccupancy[entityKey(target)] ?? [];
  }

  function canMoveVoiceMember(user: UserSummary, source: Channel): boolean {
    return Boolean(
      guild &&
      guild.origin_domain === localDomain &&
      channelHasPermission(source, Permission.MOVE_MEMBERS) &&
      actorOutranks(user)
    );
  }

  function canMoveVoiceMemberTo(
    user: UserSummary,
    source: Channel,
    target: Channel | null
  ): target is Channel {
    return Boolean(
      target &&
      target.type === 2 &&
      entityKey(target) !== entityKey(source) &&
      canMoveVoiceMember(user, source) &&
      channelHasPermission(target, Permission.MOVE_MEMBERS)
    );
  }

  function voiceMemberDragStart(
    event: DragEvent,
    occupant: VoiceOccupant,
    user: UserSummary,
    source: Channel
  ) {
    event.stopPropagation();
    if (!canMoveVoiceMember(user, source) || voiceModerationBusy) {
      event.preventDefault();
      return;
    }
    closeChannelMenu(false);
    closeVoiceMemberMenu();
    draggedVoiceMember = { occupant, user, source };
    event.dataTransfer?.setData('application/x-kaede-voice-member', occupant.identity);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function voiceMemberDragEnd() {
    draggedVoiceMember = null;
    voiceDropChannelKey = null;
  }

  function voiceChannelDragOver(event: DragEvent, target: Channel | null) {
    const dragged = draggedVoiceMember;
    if (!dragged || !canMoveVoiceMemberTo(dragged.user, dragged.source, target)) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    voiceDropChannelKey = entityKey(target);
  }

  async function voiceChannelDrop(event: DragEvent, target: Channel | null) {
    const dragged = draggedVoiceMember;
    if (!dragged || !guild || !canMoveVoiceMemberTo(dragged.user, dragged.source, target)) {
      voiceMemberDragEnd();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    voiceMemberDragEnd();
    voiceModerationBusy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(entityRef(guild))}/members/${encodeURIComponent(entityRef(dragged.user))}/voice/move`,
        {
          method: 'POST',
          body: JSON.stringify({ channel_id: entityRef(target) })
        }
      );
      const withoutMember = applyVoiceStateUpdate(voiceOccupancy, guild.channels ?? [], {
        user_id: dragged.user.id,
        user_domain: dragged.user.origin_domain,
        channel_id: dragged.source.id,
        channel_domain: dragged.source.origin_domain,
        connected: false
      });
      voiceOccupancy = applyVoiceStateUpdate(withoutMember, guild.channels ?? [], {
        user_id: dragged.user.id,
        user_domain: dragged.user.origin_domain,
        channel_id: target.id,
        channel_domain: target.origin_domain,
        connected: true,
        state: { ...dragged.occupant, channel_id: target.id }
      });
      voiceOccupancyVersion += 1;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not move this voice member. Try again.');
    } finally {
      voiceModerationBusy = false;
    }
  }

  function openVoiceMemberMenu(
    event: MouseEvent,
    occupant: VoiceOccupant,
    user: UserSummary,
    source: Channel
  ) {
    event.preventDefault();
    event.stopPropagation();
    dismissFloatingLayers();
    closeChannelMenu(false);
    profile = null;
    voiceMemberMenu = { occupant, user, source, x: event.clientX, y: event.clientY };
    void tick().then(() => {
      if (voiceMemberMenu && voiceMemberMenuElement) {
        placeContextMenu(voiceMemberMenuElement, voiceMemberMenu.x, voiceMemberMenu.y);
        voiceMemberMenuElement.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
      }
    });
  }

  function closeVoiceMemberMenu() {
    voiceMemberMenu = null;
  }

  function viewVoiceMemberProfile(menu: NonNullable<typeof voiceMemberMenu>) {
    const anchor = voiceMemberMenuElement;
    const bounds = anchor?.getBoundingClientRect();
    closeVoiceMemberMenu();
    profile = {
      user: menu.user,
      x: bounds?.right ?? window.innerWidth / 2,
      y: bounds?.top ?? window.innerHeight / 2
    };
  }

  async function disconnectVoiceMember(menu: NonNullable<typeof voiceMemberMenu>) {
    if (!guild || !canMoveVoiceMember(menu.user, menu.source) || voiceModerationBusy) return;
    closeVoiceMemberMenu();
    voiceModerationBusy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(entityRef(guild))}/members/${encodeURIComponent(entityRef(menu.user))}/voice`,
        { method: 'DELETE' }
      );
      voiceOccupancy = applyVoiceStateUpdate(voiceOccupancy, guild.channels ?? [], {
        user_id: menu.user.id,
        user_domain: menu.user.origin_domain,
        channel_id: menu.source.id,
        channel_domain: menu.source.origin_domain,
        connected: false
      });
      voiceOccupancyVersion += 1;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not disconnect this voice member. Try again.');
    } finally {
      voiceModerationBusy = false;
    }
  }

  function openProfile(user: UserSummary, event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    dismissFloatingLayers();
    const target = event.currentTarget as HTMLElement | null;
    const bounds = target?.getBoundingClientRect();
    profile = {
      user,
      x: event.clientX || (bounds?.right ?? window.innerWidth / 2),
      y: event.clientY || (bounds?.top ?? window.innerHeight / 2)
    };
  }

  function openMessageProfile(message: Message, event: MouseEvent) {
    if (message.author) openProfile(message.author, event);
  }

  function moderationActionsFor(user: UserSummary) {
    return guildModerationActions(guild, currentUser, user, members);
  }

  function formatTimeoutFailure(failure: ApiError): string {
    const reason = typeof failure.detail.reason === 'string' ? failure.detail.reason.trim() : '';
    const indefinite = failure.detail.timeout_indefinite === true;
    const until =
      typeof failure.detail.timeout_until === 'string'
        ? new Date(failure.detail.timeout_until)
        : null;
    const duration = indefinite
      ? 'indefinitely'
      : until && !Number.isNaN(until.valueOf())
        ? `until ${until.toLocaleString()}`
        : 'in this guild';
    return `You are timed out ${duration}.${reason ? ` Reason: ${reason}` : ''}`;
  }

  function requestModeration(user: UserSummary, action: 'kick' | 'timeout' | 'ban') {
    if (!moderationActionsFor(user).some((candidate) => candidate.id === action)) return;
    dismissFloatingLayers();
    moderationReason = '';
    moderationDuration = action === 'timeout' ? '86400' : 'permanent';
    moderationError = '';
    moderationDialog = { user, action };
  }

  function closeModerationDialog() {
    moderationGeneration += 1;
    moderationController?.abort();
    moderationController = null;
    moderationBusy = false;
    moderationDialog = null;
    moderationError = '';
  }

  function cancelModerationDialog(event: Event) {
    event.preventDefault();
    event.stopPropagation();
    closeModerationDialog();
  }

  function moderationDialogKeydown(event: KeyboardEvent) {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation();
    closeModerationDialog();
  }

  async function confirmModeration() {
    if (!guild || !moderationDialog || moderationBusy) return;
    const request = moderationDialog;
    const requestGeneration = ++moderationGeneration;
    const controller = new AbortController();
    moderationController = controller;
    moderationBusy = true;
    moderationError = '';
    const headers = moderationReason.trim()
      ? { 'X-Audit-Log-Reason': moderationReason.trim() }
      : undefined;
    const guildRef = encodeURIComponent(entityRef(guild));
    const userRef = encodeURIComponent(entityRef(request.user));
    try {
      if (request.action === 'kick') {
        await api(`/guilds/${guildRef}/members/${userRef}`, {
          method: 'DELETE',
          headers,
          signal: controller.signal
        });
        if (requestGeneration !== moderationGeneration) return;
        moderationDialog = null;
        setMembers(members.filter((member) => entityKey(member.user) !== entityKey(request.user)));
      } else if (request.action === 'ban') {
        const expiresAt =
          moderationDuration === 'permanent'
            ? null
            : new Date(Date.now() + Number(moderationDuration) * 1000).toISOString();
        await api(`/guilds/${guildRef}/bans/${userRef}`, {
          method: 'PUT',
          headers,
          signal: controller.signal,
          body: JSON.stringify({
            reason: moderationReason.trim() || null,
            expires_at: expiresAt,
            delete_message_seconds: 0
          })
        });
        if (requestGeneration !== moderationGeneration) return;
        moderationDialog = null;
        setMembers(members.filter((member) => entityKey(member.user) !== entityKey(request.user)));
      } else {
        const indefinite = moderationDuration === 'permanent';
        const updated = await api<GuildMemberSummary>(`/guilds/${guildRef}/members/${userRef}`, {
          method: 'PATCH',
          headers,
          signal: controller.signal,
          body: JSON.stringify({
            timeout_until: indefinite
              ? null
              : new Date(Date.now() + Number(moderationDuration) * 1000).toISOString(),
            timeout_indefinite: indefinite
          })
        });
        if (requestGeneration !== moderationGeneration) return;
        moderationDialog = null;
        setMembers(
          members.map((member) =>
            entityKey(member.user) === entityKey(updated.user) ? updated : member
          )
        );
      }
    } catch (caught) {
      if (requestGeneration !== moderationGeneration || controller.signal.aborted) return;
      moderationError = userErrorMessage(
        caught,
        'The moderation action could not be applied. Try again.'
      );
    } finally {
      if (requestGeneration === moderationGeneration) {
        moderationController = null;
        moderationBusy = false;
      }
    }
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

  async function loadVoiceOccupancy(channels: Channel[], generation: number) {
    const voiceChannels = channels.filter((item) => item.type === 2);
    if (voiceChannels.length === 0) return;
    const sequence = ++voiceRefreshSequence;
    const version = voiceOccupancyVersion;
    voiceOccupancyLoading = {
      ...voiceOccupancyLoading,
      ...Object.fromEntries(voiceChannels.map((item) => [entityKey(item), true]))
    };
    const snapshots = await Promise.all(
      voiceChannels.map(async (item) => {
        const key = entityKey(item);
        try {
          const snapshot = await api<{ participants: VoiceOccupant[] }>(
            `/channels/${encodeURIComponent(entityRef(item))}/voice/occupancy`
          );
          return { key, participants: snapshot.participants, error: '' };
        } catch (caught) {
          return {
            key,
            participants: null,
            error: userErrorMessage(
              caught,
              'Could not refresh this voice roster. Check your connection and try again.'
            )
          };
        }
      })
    );
    if (generation !== loadGeneration || sequence !== voiceRefreshSequence) return;
    if (version !== voiceOccupancyVersion) {
      const nextLoading = { ...voiceOccupancyLoading };
      for (const item of voiceChannels) nextLoading[entityKey(item)] = false;
      voiceOccupancyLoading = nextLoading;
      return;
    }
    const nextOccupancy = { ...voiceOccupancy };
    const nextErrors = { ...voiceOccupancyErrors };
    const nextLoading = { ...voiceOccupancyLoading };
    for (const snapshot of snapshots) {
      nextLoading[snapshot.key] = false;
      if (snapshot.participants) {
        nextOccupancy[snapshot.key] = snapshot.participants;
        delete nextErrors[snapshot.key];
      } else {
        nextErrors[snapshot.key] = snapshot.error;
      }
    }
    voiceOccupancy = nextOccupancy;
    voiceOccupancyErrors = nextErrors;
    voiceOccupancyLoading = nextLoading;
    voiceOccupancyVersion += 1;
  }

  function refreshVoiceOccupancy() {
    if (document.visibilityState !== 'visible' || !guild) return;
    const occupiedVoiceChannels = (guild.channels ?? []).filter(
      (item) =>
        item.type === 2 &&
        ((voiceOccupancy[entityKey(item)]?.length ?? 0) > 0 ||
          Boolean(voiceOccupancyErrors[entityKey(item)]))
    );
    if (occupiedVoiceChannels.length > 0) {
      void loadVoiceOccupancy(occupiedVoiceChannels, loadGeneration);
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
      if (channel?.starter_message && entityKey(channel.starter_message) === entityKey(update)) {
        rememberThread({
          ...channel,
          starter_message: { ...channel.starter_message, ...update }
        });
      }
      scheduleForumRefreshForChannel(update.channel_id, update.channel_domain);
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
      if (
        channel?.starter_message &&
        entityKey(channel.starter_message) ===
          entityKey({ id: deleted.id, origin_domain: deleted.origin_domain })
      ) {
        rememberThread({
          ...channel,
          starter_message: {
            ...channel.starter_message,
            content: null,
            attachments: [],
            deleted_at: new Date().toISOString(),
            content_unavailable: true
          }
        });
      }
      scheduleForumRefreshForChannel(deleted.channel_id, deleted.channel_domain);
    } else if (dispatch.t === 'MESSAGE_SEND_REJECTED') {
      const rejected = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        client_nonce: string;
        code: string;
        reason?: string | null;
        timeout_until?: string | null;
        timeout_indefinite?: boolean;
      };
      if (isCurrentChannel(rejected.channel_id, rejected.channel_domain)) {
        const failure = messageDeliveryFailure(rejected);
        setMessages(
          messages.map((item) =>
            item.client_nonce === rejected.client_nonce
              ? {
                  ...item,
                  queued: false,
                  pending: false,
                  failed: true,
                  failure_reason: failure.reason,
                  retryable: failure.retryable
                }
              : item
          )
        );
        error =
          failure.reason ??
          apiErrorMessage(rejected.code || 'REQUEST_FAILED', 400, {
            message: rejected.reason
          });
      }
    } else if (dispatch.t === 'THREAD_CREATE') {
      try {
        const created = parseCreatedThread(dispatch.d);
        rememberThread({
          ...created.channel,
          starter_message: created.starter_message ?? created.channel.starter_message
        });
        scheduleForumRefreshForThread(created.channel);
      } catch {
        // Ignore a malformed projection; the next guild/channel snapshot repairs it.
      }
    } else if (dispatch.t === 'THREAD_UPDATE') {
      const updated = dispatch.d as Channel;
      if (updated.id && updated.origin_domain && isThreadChannel(updated)) {
        rememberThread(updated);
        scheduleForumRefreshForThread(updated);
      }
    } else if (dispatch.t === 'THREAD_DELETE') {
      const deleted = dispatch.d as {
        id: string;
        origin_domain: string;
        parent_id?: string;
        parent_domain?: string;
      };
      const deletedKey = entityKey(deleted);
      scheduleForumRefreshForThread(deleted);
      forumPosts = forumPosts.filter((item) => entityKey(item) !== deletedKey);
      threadDirectoryActive = threadDirectoryActive.filter(
        (item) => entityKey(item) !== deletedKey
      );
      threadDirectoryArchived = threadDirectoryArchived.filter(
        (item) => entityKey(item) !== deletedKey
      );
      if (guild) {
        setCurrentChannels((guild.channels ?? []).filter((item) => entityKey(item) !== deletedKey));
        if (channel && entityKey(channel) === deletedKey) {
          const destination = (guild.channels ?? []).find(
            (item) => item.id === deleted.parent_id && item.origin_domain === deleted.parent_domain
          );
          if (destination) window.location.assign(guildChannelPath(guild, destination));
        }
      }
    } else if (dispatch.t === 'THREAD_LIST_SYNC') {
      const update = dispatch.d as { threads?: Channel[]; members?: ThreadMember[] };
      const syncedMembers = update.members ?? [];
      for (const thread of update.threads ?? []) {
        const member = syncedMembers.find(
          (item) =>
            item.id === thread.id &&
            (!item.thread_domain || item.thread_domain === thread.origin_domain)
        );
        rememberThread({
          ...thread,
          member: member ?? null
        });
      }
      if (channel && isThreadChannel(channel)) {
        threadMembers = syncedMembers.filter(
          (member) =>
            member.id === channel.id &&
            (!member.thread_domain || member.thread_domain === channel.origin_domain)
        );
      }
    } else if (dispatch.t === 'THREAD_MEMBER_UPDATE') {
      const member = dispatch.d as ThreadMember & { thread_domain?: string };
      if (
        channel &&
        isThreadChannel(channel) &&
        member.id === channel.id &&
        (!member.thread_domain || member.thread_domain === channel.origin_domain)
      ) {
        const memberRef = `${member.user_id}@${member.user_domain}`;
        threadMembers = [
          ...threadMembers.filter((item) => `${item.user_id}@${item.user_domain}` !== memberRef),
          member
        ];
        if (
          currentUser &&
          member.user_id === currentUser.id &&
          member.user_domain === currentUser.origin_domain
        ) {
          rememberThread({ ...channel, member });
        }
      }
    } else if (dispatch.t === 'THREAD_MEMBERS_UPDATE') {
      const update = dispatch.d as {
        id: string;
        thread_domain?: string;
        member_count?: number;
        removed_member_ids?: string[];
        removed_member_refs?: Array<{ id: string; origin_domain: string }>;
      };
      const threadDomain = update.thread_domain ?? guild?.origin_domain ?? localDomain;
      const target = (guild?.channels ?? []).find(
        (item) => item.id === update.id && item.origin_domain === threadDomain
      );
      if (target && isThreadChannel(target)) {
        const removesCurrentUser = threadMembersUpdateRemovesUser(update, currentUser);
        const targetIsCurrent = channel ? entityKey(channel) === entityKey(target) : false;
        if (removesCurrentUser && target.type === 12) {
          if (targetIsCurrent) {
            setMessages([]);
            threadMembers = [];
          }
          forumPosts = forumPosts.filter((item) => entityKey(item) !== entityKey(target));
          threadDirectoryActive = threadDirectoryActive.filter(
            (item) => entityKey(item) !== entityKey(target)
          );
          threadDirectoryArchived = threadDirectoryArchived.filter(
            (item) => entityKey(item) !== entityKey(target)
          );
          entities.channels.remove(entityKey(target));
          if (guild) {
            setCurrentChannels(
              (guild.channels ?? []).filter((item) => entityKey(item) !== entityKey(target))
            );
            if (targetIsCurrent) {
              const destination = (guild.channels ?? []).find(
                (item) =>
                  item.id === target.parent_id && item.origin_domain === target.parent_domain
              );
              window.location.assign(
                destination ? guildChannelPath(guild, destination) : resolve('/home')
              );
            }
          }
        } else {
          rememberThread({
            ...target,
            member_count: update.member_count ?? target.member_count,
            member: removesCurrentUser ? null : target.member
          });
          if (targetIsCurrent) {
            if (removesCurrentUser && currentUser) {
              threadMembers = threadMembers.filter((member) => {
                if (member.user) return entityKey(member.user) !== entityKey(currentUser);
                return !(
                  member.user_id === currentUser.id &&
                  member.user_domain === currentUser.origin_domain
                );
              });
            } else {
              void fetchThreadMembers(target).then((members) => {
                if (matchesEntityRef(channelId, target, localDomain)) threadMembers = members;
              });
            }
          }
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
    } else if (dispatch.t === 'GUILD_MEMBER_UPDATE') {
      const update = dispatch.d as {
        user_id?: string;
        user_domain?: string;
        user?: { id?: string; origin_domain?: string };
      };
      const userId = update.user_id ?? update.user?.id;
      const userDomain = update.user_domain ?? update.user?.origin_domain;
      if (
        currentUser &&
        userId === currentUser.id &&
        (!userDomain || userDomain === currentUser.origin_domain)
      ) {
        void refreshSelfModeration();
      }
    } else if (dispatch.t === 'TYPING_START') {
      const started = dispatch.d as {
        channel_id: string;
        channel_domain?: string;
        user_id: string;
        user_domain?: string;
      };
      const authoredByMe =
        currentUser?.id === started.user_id &&
        (!started.user_domain || currentUser.origin_domain === started.user_domain);
      if (
        !authoredByMe &&
        dispatchTargetsCurrentChannel(started.channel_id, started.channel_domain)
      ) {
        registerTyping(started.user_id, started.user_domain);
      }
    } else if (dispatch.t === 'READ_STATE_UPDATE') {
      setReadStates(applyReadStateDispatch(readStates, dispatch.d as ReadStateDispatch));
    } else if (dispatch.t === 'CHANNEL_CREATE' || dispatch.t === 'CHANNEL_ACCESS_GRANTED') {
      const created = dispatch.d as Channel;
      if (
        guild &&
        created.guild_id === guild.id &&
        created.guild_domain === guild.origin_domain &&
        !(guild.channels ?? []).some((item) => entityKey(item) === entityKey(created))
      ) {
        setCurrentChannels([...(guild.channels ?? []), created]);
      }
    } else if (dispatch.t === 'CHANNEL_UPDATE') {
      const updated = dispatch.d as Channel;
      if (guild && updated.guild_id === guild.id && updated.guild_domain === guild.origin_domain) {
        setCurrentChannels(
          (guild.channels ?? []).map((item) =>
            entityKey(item) === entityKey(updated) ? { ...item, ...updated } : item
          )
        );
      }
    } else if (dispatch.t === 'CHANNEL_PERMISSION_UPDATE') {
      const updated = dispatch.d as {
        channel_id: string;
        channel_domain: string;
        permissions: string;
      };
      if (guild) {
        setCurrentChannels(
          (guild.channels ?? []).map((item) =>
            item.id === updated.channel_id && item.origin_domain === updated.channel_domain
              ? { ...item, permissions: updated.permissions }
              : item
          )
        );
      }
    } else if (dispatch.t === 'CHANNEL_DELETE' || dispatch.t === 'CHANNEL_ACCESS_REVOKED') {
      const deleted = dispatch.d as {
        id?: string;
        origin_domain?: string;
        channel_id?: string;
        channel_domain?: string;
        guild_id: string;
        guild_domain: string;
      };
      if (guild && deleted.guild_id === guild.id && deleted.guild_domain === guild.origin_domain) {
        const deletedId = deleted.id ?? deleted.channel_id;
        const deletedDomain = deleted.origin_domain ?? deleted.channel_domain;
        setCurrentChannels(
          (guild.channels ?? []).filter(
            (item) => item.id !== deletedId || item.origin_domain !== deletedDomain
          )
        );
      }
    } else if (dispatch.t === 'GUILD_UPDATE') {
      const updated = dispatch.d as Guild;
      if (guild && entityKey(updated) === entityKey(guild)) {
        guild = { ...guild, ...updated };
        entities.guilds.upsert(guild);
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
    } else if (dispatch.t === 'GUILD_HISTORY_SYNC_UPDATE') {
      const update = dispatch.d as {
        guild_id: string;
        guild_domain: string;
        status: Guild['history_sync_status'];
        code?: string | null;
        retry_after_ms?: number | null;
        resource?: string | null;
      };
      if (guild && isCurrentGuild(update.guild_id, update.guild_domain)) {
        guild = {
          ...guild,
          history_sync_status: update.status,
          history_sync_error_code: update.status === 'ready' ? null : (update.code ?? null),
          history_sync_retry_after_ms:
            update.status === 'retrying' ? (update.retry_after_ms ?? null) : null,
          history_sync_resource: update.status === 'failed' ? (update.resource ?? null) : null
        };
        entities.guilds.upsert(guild);
      }
    } else if (dispatch.t === 'GUILD_ROLE_CREATE' || dispatch.t === 'GUILD_ROLE_UPDATE') {
      const role = dispatch.d as Role;
      if (guild && role.guild_id === guild.id && role.guild_domain === guild.origin_domain) {
        guild = {
          ...guild,
          roles: [
            ...(guild.roles ?? []).filter((candidate) => entityKey(candidate) !== entityKey(role)),
            role
          ]
        };
      }
    } else if (dispatch.t === 'GUILD_ROLE_DELETE') {
      const role = dispatch.d as {
        id: string;
        origin_domain: string;
        guild_id: string;
        guild_domain: string;
      };
      if (guild && role.guild_id === guild.id && role.guild_domain === guild.origin_domain) {
        guild = {
          ...guild,
          roles: (guild.roles ?? []).filter(
            (candidate) =>
              candidate.id !== role.id || candidate.origin_domain !== role.origin_domain
          )
        };
      }
    } else if (dispatch.t === 'GUILD_MEMBER_LIST_UPDATE') {
      const update = dispatch.d as {
        guild_id: string;
        guild_domain: string;
        ops: { op: string; items: GuildMemberSummary[] }[];
      };
      if (isCurrentGuild(update.guild_id, update.guild_domain))
        setMembers(update.ops.flatMap((operation) => operation.items ?? []));
    } else if (dispatch.t === 'GUILD_MEMBERS_CHUNK') {
      const chunk = dispatch.d as {
        guild_id: string;
        guild_domain: string;
        members: GuildMemberSummary[];
      };
      if (isCurrentGuild(chunk.guild_id, chunk.guild_domain)) setMembers(chunk.members);
    } else if (dispatch.t === 'GUILD_AVAILABILITY_UPDATE') {
      const update = dispatch.d as {
        guild_id: string;
        guild_domain: string;
        available: boolean;
      };
      if (isCurrentGuild(update.guild_id, update.guild_domain) && guild) {
        guild = { ...guild, unavailable: !update.available };
      }
      setGuilds(
        guilds.map((item) =>
          item.id === update.guild_id && item.origin_domain === update.guild_domain
            ? { ...item, unavailable: !update.available }
            : item
        )
      );
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
    } else if (dispatch.t === 'VOICE_STATE_UPDATE') {
      voiceOccupancy = applyVoiceStateUpdate(
        voiceOccupancy,
        guild?.channels ?? [],
        dispatch.d as VoiceStateUpdate
      );
      voiceOccupancyVersion += 1;
    } else if (dispatch.t === 'VOICE_TOKEN') {
      const update = dispatch.d as {
        move_session_id?: string;
        channel_id: string;
        channel_domain: string;
        grant: import('$lib/voice/session').VoiceToken;
      };
      if ((update.move_session_id ?? null) === (update.grant.move_session_id ?? null)) {
        window.dispatchEvent(
          new CustomEvent('kaede:voice-token', {
            detail: {
              grant: update.grant,
              channelRef: `${update.channel_id}@${update.channel_domain}`
            }
          })
        );
      }
    }
  }

  function ensureMemberSubscription(targetGuild: string, refresh = false) {
    if (!gateway || !targetGuild) return;
    if (!refresh && subscribedGuildRef === targetGuild) return;
    const now = Date.now();
    if (refresh && subscribedGuildRef === targetGuild && now - lastMemberRefreshAt < 5_000) return;
    subscribedGuildRef = targetGuild;
    lastMemberRefreshAt = now;
    gateway.subscribeMembers(targetGuild);
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
    const desktopViewport = window.matchMedia('(min-width: 741px)');
    const viewportChanged = () => {
      if (desktopViewport.matches) closeMobileNavigation(false);
    };
    const dismissChannelMenu = () => closeChannelMenu(false);
    const dismissChannelMenuOnContext = (event: MouseEvent) => {
      if (!channelMenuElement?.contains(event.target as Node)) closeChannelMenu(false);
    };
    const dismissTransientLayers = () => {
      closeChannelMenu(false);
      profile = null;
      gifPickerOpen = false;
      emojiPickerOpen = false;
    };
    gateway = client;
    featureController = new AbortController();
    void refreshGifConfiguration();
    try {
      memberRosterOpen = localStorage.getItem('kaede.member-roster.visible') !== 'false';
    } catch {
      memberRosterOpen = true;
    }
    const visibilityChanged = () => {
      acknowledgeLatestIfVisible();
      refreshVoiceOccupancy();
      if (document.hidden) {
        if (selfModerationRetryTimer !== null) window.clearTimeout(selfModerationRetryTimer);
        selfModerationRetryTimer = null;
      } else {
        ensureMemberSubscription(guildId, true);
        void refreshSelfModeration();
      }
    };
    const focused = () => {
      refreshVoiceOccupancy();
      ensureMemberSubscription(guildId, true);
      void refreshSelfModeration();
    };
    const voiceRefreshTimer = window.setInterval(refreshVoiceOccupancy, 30_000);
    const sessionReset = () => {
      ensureMemberSubscription(guildId, true);
      recoverCurrentRoute();
    };
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
    window.addEventListener('focus', focused);
    window.addEventListener('resize', dismissChannelMenu);
    window.addEventListener('scroll', dismissChannelMenu, true);
    window.addEventListener('contextmenu', dismissChannelMenuOnContext, true);
    window.addEventListener('kaede:open-user-profile', profileRequest);
    window.addEventListener(DISMISS_FLOATING_LAYERS_EVENT, dismissTransientLayers);
    desktopViewport.addEventListener('change', viewportChanged);
    viewportChanged();
    client.addEventListener('dispatch', receive);
    client.addEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
    ensureMemberSubscription(guildId);
    return () => {
      featureController?.abort();
      featureController = null;
      readAcknowledgements.reset();
      loadGeneration += 1;
      snapshotGeneration += 1;
      voiceRefreshSequence += 1;
      dispatchBuffer = null;
      document.removeEventListener('visibilitychange', visibilityChanged);
      window.removeEventListener('focus', focused);
      window.clearInterval(voiceRefreshTimer);
      window.removeEventListener('resize', dismissChannelMenu);
      window.removeEventListener('scroll', dismissChannelMenu, true);
      window.removeEventListener('contextmenu', dismissChannelMenuOnContext, true);
      window.removeEventListener('kaede:open-user-profile', profileRequest);
      window.removeEventListener(DISMISS_FLOATING_LAYERS_EVENT, dismissTransientLayers);
      desktopViewport.removeEventListener('change', viewportChanged);
      client.removeEventListener('dispatch', receive);
      client.removeEventListener(GATEWAY_SESSION_RESET_EVENT, sessionReset);
      client.releaseMembers();
      subscribedGuildRef = '';
      lastMemberRefreshAt = 0;
      if (gateway === client) gateway = null;
      resetTyping();
      if (slowmodeTimer) window.clearInterval(slowmodeTimer);
      if (selfModerationExpiryTimer !== null) window.clearTimeout(selfModerationExpiryTimer);
      selfModerationExpiryTimer = null;
      if (selfModerationRetryTimer !== null) window.clearTimeout(selfModerationRetryTimer);
      selfModerationRetryTimer = null;
      if (forumRefreshTimer !== null) window.clearTimeout(forumRefreshTimer);
      forumRefreshTimer = null;
      resetUploads();
    };
  });

  function chooseGif(gif: GifResult) {
    if (busy || !channelReady || !channel || !canSendMessages || editingMessage) return;
    gifPickerOpen = false;
    emojiPickerOpen = false;
    void send(pendingMessageSend(gif.url, [], []));
  }

  function chooseEmoji(value: string) {
    if (busy || !channelReady || !channel || !canSendMessages) return;
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

  function chooseSticker(value: string) {
    if (busy || !channelReady || !channel || !canSendMessages || editingMessage) return;
    emojiPickerOpen = false;
    gifPickerOpen = false;
    void send(pendingMessageSend(value, [], []));
  }

  function startSlowmode(milliseconds: number) {
    const deadline = Date.now() + Math.max(1000, milliseconds);
    try {
      localStorage.setItem(
        `kaede.slowmode.${currentUser ? entityRef(currentUser) : 'unknown'}.${channelId}`,
        String(deadline)
      );
    } catch {
      // Slow mode remains accurate in memory when browser storage is unavailable.
    }
    if (slowmodeTimer) window.clearInterval(slowmodeTimer);
    const update = () => {
      slowmodeRemaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      if (slowmodeRemaining === 0 && slowmodeTimer) {
        window.clearInterval(slowmodeTimer);
        slowmodeTimer = null;
        try {
          localStorage.removeItem(
            `kaede.slowmode.${currentUser ? entityRef(currentUser) : 'unknown'}.${channelId}`
          );
        } catch {
          // Nothing else is required when browser storage is unavailable.
        }
      }
    };
    update();
    slowmodeTimer = window.setInterval(update, 250);
  }

  function restoreSlowmode(targetChannel: string) {
    if (canBypassSlowmode) {
      slowmodeRemaining = 0;
      return;
    }
    try {
      const stored = Number(
        localStorage.getItem(
          `kaede.slowmode.${currentUser ? entityRef(currentUser) : 'unknown'}.${targetChannel}`
        )
      );
      if (Number.isFinite(stored) && stored > Date.now()) startSlowmode(stored - Date.now());
    } catch {
      // An unavailable local store only removes the refresh-time hint.
    }
  }

  $effect(() => {
    const targetGuild = guildId;
    const targetChannel = channelId;
    const targetAround = aroundMessage;
    untrack(() => {
      const routeGeneration = ++loadGeneration;
      const snapshot = ++snapshotGeneration;
      const buffered: Dispatch[] = [];
      dispatchBuffer = buffered;
      guild = null;
      loadedRouteChannel = null;
      selfModeration = null;
      selfModerationWarning = '';
      selfModerationRequest += 1;
      if (selfModerationExpiryTimer !== null) window.clearTimeout(selfModerationExpiryTimer);
      selfModerationExpiryTimer = null;
      if (selfModerationRetryTimer !== null) window.clearTimeout(selfModerationRetryTimer);
      selfModerationRetryTimer = null;
      setMessages([]);
      setMembers([]);
      resetUploads();
      content = '';
      applicationCommands = [];
      selectedApplicationCommand = null;
      nativeThreadComposer = false;
      commandOptionValues = {};
      commandNotice = '';
      composerCursor = 0;
      editingMessage = null;
      composerDraftBeforeEdit = null;
      channelMenu = null;
      channelMenuReturnFocus = null;
      if (slowmodeTimer) window.clearInterval(slowmodeTimer);
      slowmodeTimer = null;
      slowmodeRemaining = 0;
      channelDialogOpen = false;
      channelDialogTarget = null;
      channelDialogReturnFocus = null;
      channelDialogBusy = false;
      channelDeleteGeneration += 1;
      channelDeleteTarget = null;
      channelDeleteBusy = false;
      channelDeleteDialog = null;
      channelDeleteCancel = null;
      channelDeleteReturnFocus = null;
      inviteDialogGeneration += 1;
      inviteDialogOpen = false;
      inviteDialogBusy = false;
      inviteDialogError = '';
      inviteLink = '';
      inviteDialogElement = null;
      inviteDialogClose = null;
      inviteDialogReturnFocus = null;
      draggedChannelKey = null;
      dragOverChannelKey = null;
      channelReorderGeneration += 1;
      reorderingChannels = false;
      channelOrderStatus = '';
      mobileNavigationOpen = false;
      profile = null;
      moderationGeneration += 1;
      moderationController?.abort();
      moderationController = null;
      moderationDialog = null;
      moderationBusy = false;
      moderationError = '';
      voiceOccupancy = {};
      voiceOccupancyErrors = {};
      voiceOccupancyLoading = {};
      voiceOccupancyVersion += 1;
      voiceRefreshSequence += 1;
      resetTyping();
      replyingMessage = null;
      replyNotify = true;
      pinnedMessages = [];
      pinsOpen = false;
      pinsLoading = false;
      pinsError = '';
      error = '';
      busy = false;
      channelReady = false;
      forumPosts = [];
      forumLoading = false;
      forumLoadingMore = false;
      forumError = '';
      forumHasMore = false;
      forumCursor = '';
      forumFilterState = { query: '', selectedTagIds: [], sort: 'recent_activity' };
      forumRequestSequence += 1;
      if (forumRefreshTimer !== null) window.clearTimeout(forumRefreshTimer);
      forumRefreshTimer = null;
      forumPostBusy = false;
      threadMembers = [];
      threadActionBusy = false;
      threadEncryptionBusy = false;
      threadEncryptionStatus = '';
      threadCreateSource = null;
      threadCreateBusy = false;
      threadCreateError = '';
      threadDirectoryActive = [];
      threadDirectoryArchived = [];
      threadDirectoryOpen = false;
      threadDirectoryLoading = false;
      threadDirectoryLoadingMore = false;
      threadDirectoryActiveHasMore = false;
      threadDirectoryArchivedHasMore = false;
      threadDirectoryActiveCursor = '';
      threadDirectoryArchivedCursor = '';
      threadDirectoryBusy = false;
      timelineAtBottom = false;
      loadingEarlier = false;
      hasLater = false;
      loadingLater = false;
      lastTypingAt = 0;
      hasEarlier = true;
      pendingSends.clear();
      readAcknowledgements.reset();
      ensureMemberSubscription(targetGuild);
      void refreshSelfModeration(targetGuild);
      void load(
        targetGuild,
        targetChannel,
        routeGeneration,
        snapshot,
        buffered,
        false,
        targetAround
      );
    });
  });

  $effect(() => {
    if (canSendMessages && canAttachFiles) return;
    untrack(() => {
      gifPickerOpen = false;
      emojiPickerOpen = false;
      if (!canAttachFiles && uploads.length) resetUploads();
    });
  });

  function recoverCurrentRoute() {
    const targetGuild = guildId;
    const targetChannel = channelId;
    const targetAround = aroundMessage;
    const routeGeneration = loadGeneration;
    const snapshot = ++snapshotGeneration;
    const buffered: Dispatch[] = [];
    dispatchBuffer = buffered;
    void load(targetGuild, targetChannel, routeGeneration, snapshot, buffered, true, targetAround);
  }

  async function load(
    targetGuild: string,
    targetChannel: string,
    routeGeneration: number,
    snapshot: number,
    buffered: Dispatch[],
    preserveMessages: boolean,
    targetAround: string | null
  ) {
    try {
      const [
        loadedGuild,
        loadedGuilds,
        loadedMessages,
        loadedReadStates,
        loadedCurrentUser,
        loadedEmojis,
        loadedStickers,
        loadedPins,
        loadedCommands,
        routeChannel,
        activeGuildThreads
      ] = await Promise.all([
        api<Guild>(`/guilds/${encodeURIComponent(targetGuild)}`),
        api<Guild[]>('/users/@me/guilds'),
        api<Message[]>(
          `/channels/${encodeURIComponent(targetChannel)}/messages${targetAround ? `?around=${encodeURIComponent(targetAround)}` : ''}`
        ),
        api<ReadStateStatus[]>('/users/@me/read-states'),
        api<UserSummary>('/users/@me'),
        api<CustomEmoji[]>('/users/@me/emojis'),
        api<GuildSticker[]>('/users/@me/stickers'),
        api<Message[]>(`/channels/${encodeURIComponent(targetChannel)}/pins`).catch(() => []),
        api<ApplicationCommand[]>(
          `/guilds/${encodeURIComponent(targetGuild)}/application-commands`
        ).catch(() => []),
        fetchChannel(targetChannel).catch(() => null),
        fetchActiveGuildThreads(targetGuild).catch(() => ({
          threads: [],
          members: [],
          has_more: false,
          next_cursor: null
        }))
      ]);
      if (
        routeGeneration !== loadGeneration ||
        snapshot !== snapshotGeneration ||
        targetGuild !== guildId ||
        targetChannel !== channelId
      )
        return;
      for (const thread of activeGuildThreads.threads) {
        loadedGuild.channels = mergeThreadIntoChannels(loadedGuild.channels ?? [], thread);
      }
      let loadedChannel =
        loadedGuild.channels?.find((item) => matchesEntityRef(targetChannel, item, localDomain)) ??
        routeChannel;
      if (loadedChannel && isThreadChannel(loadedChannel)) {
        loadedGuild.channels = mergeThreadIntoChannels(loadedGuild.channels ?? [], loadedChannel);
      }
      guild = preserveHistorySync(loadedGuild);
      loadedRouteChannel = loadedChannel;
      availableEmojis = loadedEmojis;
      availableStickers = loadedStickers;
      pinnedMessages = loadedPins;
      applicationCommands = loadedCommands;
      setGuilds(loadedGuilds);
      setReadStates(loadedReadStates);
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
      void loadVoiceOccupancy(loadedGuild.channels ?? [], routeGeneration);
      hasEarlier = targetAround ? loadedMessages.length > 0 : loadedMessages.length === 50;
      hasLater = Boolean(targetAround && loadedMessages.length > 0);
      if (loadedChannel && isForumChannel(loadedChannel)) {
        forumLoading = true;
        try {
          const feed = await fetchThreads(loadedChannel, { includeArchived: true });
          if (routeGeneration !== loadGeneration || targetChannel !== channelId) return;
          forumPosts = feed.threads;
          const initialSort = forumDefaultSort(loadedChannel);
          forumFilterState = { query: '', selectedTagIds: [], sort: initialSort };
          forumHasMore = feed.has_more;
          forumCursor = feed.next_cursor ?? '';
          threadMembers = feed.members;
          for (const thread of forumPosts) {
            loadedGuild.channels = mergeThreadIntoChannels(loadedGuild.channels ?? [], thread);
          }
          guild = preserveHistorySync(loadedGuild);
        } catch (caught) {
          forumError = userErrorMessage(caught, 'Could not load forum posts. Try again.');
        } finally {
          forumLoading = false;
        }
      } else if (loadedChannel && isThreadChannel(loadedChannel)) {
        threadMembers = await fetchThreadMembers(loadedChannel).catch(() => []);
        const loadedParent = loadedGuild.channels?.find(
          (item) =>
            item.id === loadedChannel?.parent_id &&
            item.origin_domain === loadedChannel?.parent_domain
        );
        if (loadedParent && isForumChannel(loadedParent)) {
          forumLoading = true;
          try {
            const feed = await fetchThreads(loadedParent, { includeArchived: true });
            if (routeGeneration !== loadGeneration || targetChannel !== channelId) return;
            forumPosts = feed.threads;
            const initialSort = forumDefaultSort(loadedParent);
            forumFilterState = { query: '', selectedTagIds: [], sort: initialSort };
            forumHasMore = feed.has_more;
            forumCursor = feed.next_cursor ?? '';
            for (const thread of feed.threads) {
              loadedGuild.channels = mergeThreadIntoChannels(loadedGuild.channels ?? [], thread);
            }
            guild = preserveHistorySync(loadedGuild);
          } catch (caught) {
            forumError = userErrorMessage(caught, 'Could not load forum posts. Try again.');
          } finally {
            forumLoading = false;
          }
        }
        if (threadRequiresE2EEActivation(loadedChannel)) {
          threadEncryptionStatus = 'Securing replies with end-to-end encryption…';
          try {
            const client = await initializeE2EE(loadedCurrentUser);
            const updated = await client.activateRoom(entityRef(loadedChannel));
            if (routeGeneration !== loadGeneration || targetChannel !== channelId) return;
            loadedChannel = updated;
            loadedRouteChannel = updated;
            loadedGuild.channels = mergeThreadIntoChannels(loadedGuild.channels ?? [], updated);
            guild = preserveHistorySync(loadedGuild);
            e2eeClient = client;
            threadEncryptionStatus = 'End-to-end encryption is active for replies.';
          } catch {
            threadEncryptionStatus =
              'Encryption setup is required before anyone can reply. Retrying…';
            const retryThread = loadedChannel;
            window.setTimeout(() => {
              if (routeGeneration !== loadGeneration || targetChannel !== channelId) return;
              void activateRequiredThread(retryThread, loadedParent).catch(() => {
                if (routeGeneration === loadGeneration)
                  threadEncryptionStatus =
                    'Encryption activation is still required. Replies remain disabled.';
              });
            }, 1500);
          }
        }
      }
      if (loadedChannel?.encryption_mode !== 'e2ee') {
        void initializeE2EE(loadedCurrentUser)
          .then((client) => {
            if (routeGeneration === loadGeneration) e2eeClient = client;
          })
          .catch(() => {
            // Plaintext channels remain usable when secure device storage is unavailable.
          });
      }
      let orderedMessages = loadedMessages.reverse().sort(compareMessages);
      if (
        loadedChannel?.encryption_mode === 'e2ee' &&
        loadedChannel.encryption_state === 'active'
      ) {
        if (
          !confirmEncryptedRoomJoin(
            entityRef(loadedCurrentUser),
            entityRef(loadedChannel),
            loadedChannel.type === 2 ? 'media' : 'messages'
          )
        ) {
          window.location.assign(resolve('/home'));
          return;
        }
        const client = await initializeE2EE(loadedCurrentUser);
        if (routeGeneration !== loadGeneration || targetChannel !== channelId) return;
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
      restoreSlowmode(targetChannel);
      acknowledgeLatestIfVisible();
    } catch (caught) {
      if (
        routeGeneration !== loadGeneration ||
        snapshot !== snapshotGeneration ||
        targetGuild !== guildId ||
        targetChannel !== channelId
      )
        return;
      for (const dispatch of buffered) applyDispatch(dispatch);
      forgetConfirmedSends();
      if (dispatchBuffer === buffered) dispatchBuffer = null;
      if (!preserveMessages) {
        error = userErrorMessage(caught, 'Could not open this channel. Try again.');
      } else if (!error) {
        error = 'Live updates resumed, but channel state could not be refreshed.';
      }
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
      `End-to-end encryption is on, but participant identities remain unverified until this safety number is compared with the other members using a separate trusted channel. A match detects first-contact key substitution by an actively malicious instance. Compare it again after membership or identity changes:\n\n${e2eeSafetyNumber || 'Safety number unavailable on this device.'}`
    );
  }

  async function loadEarlier() {
    const generation = loadGeneration;
    const targetChannel = channelId;
    const oldest = messages[0];
    if (!oldest || loadingEarlier || !hasEarlier || messages.length >= 1_000) return;
    loadingEarlier = true;
    try {
      const older = await api<Message[]>(
        `/channels/${encodeURIComponent(targetChannel)}/messages?before=${encodeURIComponent(entityRef(oldest))}`
      );
      if (generation !== loadGeneration || targetChannel !== channelId) return;
      const available = Math.max(0, 1_000 - messages.length);
      let prepended = older.reverse().slice(-available);
      if (channel?.encryption_mode === 'e2ee' && e2eeClient)
        prepended = await decryptConversationMessages(e2eeClient, channel, prepended);
      const byKey = Object.create(null) as Record<string, Message>;
      for (const message of prepended) byKey[entityKey(message)] = message;
      for (const message of messages) byKey[entityKey(message)] = message;
      setMessages(Object.values(byKey).sort(compareMessages));
      hasEarlier = older.length === 50 && messages.length < 1_000;
    } catch (caught) {
      if (generation !== loadGeneration || targetChannel !== channelId) return;
      error = userErrorMessage(caught, 'Could not load earlier messages. Try again.');
    } finally {
      if (generation === loadGeneration && targetChannel === channelId) loadingEarlier = false;
    }
  }

  async function loadLater() {
    const generation = loadGeneration;
    const targetChannel = channelId;
    const newest = messages.at(-1);
    if (!newest || loadingLater || !hasLater) return;
    loadingLater = true;
    try {
      const newer = await api<Message[]>(
        `/channels/${encodeURIComponent(targetChannel)}/messages?after=${encodeURIComponent(entityRef(newest))}`
      );
      if (generation !== loadGeneration || targetChannel !== channelId) return;
      const byKey = Object.create(null) as Record<string, Message>;
      for (const message of messages) byKey[entityKey(message)] = message;
      let decryptedNewer = newer.reverse();
      if (channel?.encryption_mode === 'e2ee' && e2eeClient)
        decryptedNewer = await decryptConversationMessages(e2eeClient, channel, decryptedNewer);
      for (const message of decryptedNewer) byKey[entityKey(message)] = message;
      setMessages(Object.values(byKey).sort(compareMessages).slice(-1_000));
      hasLater = newer.length === 50;
    } catch (caught) {
      if (generation !== loadGeneration || targetChannel !== channelId) return;
      error = userErrorMessage(caught, 'Could not load newer messages. Try again.');
    } finally {
      if (generation === loadGeneration && targetChannel === channelId) loadingLater = false;
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

  function rememberThread(thread: Channel) {
    loadedRouteChannel = matchesEntityRef(channelId, thread, localDomain)
      ? { ...loadedRouteChannel, ...thread }
      : loadedRouteChannel;
    const threadParent = guild?.channels?.find(
      (item) => item.id === thread.parent_id && item.origin_domain === thread.parent_domain
    );
    if (isForumChannel(threadParent)) {
      forumPosts = forumPosts.some((item) => entityKey(item) === entityKey(thread))
        ? forumPosts.map((item) =>
            entityKey(item) === entityKey(thread) ? { ...item, ...thread } : item
          )
        : [...forumPosts, thread];
    }
    if (
      threadDirectoryParent &&
      thread.parent_id === threadDirectoryParent.id &&
      thread.parent_domain === threadDirectoryParent.origin_domain
    ) {
      const mergeDirectory = (items: Channel[]) =>
        items.some((item) => entityKey(item) === entityKey(thread))
          ? items.map((item) =>
              entityKey(item) === entityKey(thread) ? { ...item, ...thread } : item
            )
          : [...items, thread];
      if (thread.archived) {
        threadDirectoryActive = threadDirectoryActive.filter(
          (item) => entityKey(item) !== entityKey(thread)
        );
        threadDirectoryArchived = mergeDirectory(threadDirectoryArchived);
      } else {
        threadDirectoryArchived = threadDirectoryArchived.filter(
          (item) => entityKey(item) !== entityKey(thread)
        );
        threadDirectoryActive = mergeDirectory(threadDirectoryActive);
      }
    }
    if (guild) setCurrentChannels(mergeThreadIntoChannels(guild.channels ?? [], thread));
  }

  async function activateRequiredThread(
    thread: Channel,
    parent: Channel | null | undefined
  ): Promise<Channel> {
    if (!threadRequiresE2EEActivation(thread)) return thread;
    if (!currentUser) throw new Error('Sign in again before opening this encrypted post.');
    threadEncryptionStatus = 'Securing replies with end-to-end encryption…';
    try {
      const client = await initializeE2EE(currentUser);
      const updated = await client.activateRoom(entityRef(thread));
      e2eeClient = client;
      if (
        matchesEntityRef(channelId, updated, localDomain) &&
        !confirmEncryptedRoomJoin(entityRef(currentUser), entityRef(updated), 'messages')
      ) {
        threadEncryptionStatus =
          'Open the post again when you are ready to review its encryption disclosure.';
        if (guild && parent) window.location.assign(guildChannelPath(guild, parent));
        throw new Error('The encrypted room disclosure was declined.');
      }
      rememberThread(updated);
      threadEncryptionStatus = 'End-to-end encryption is active for replies.';
      return updated;
    } catch (caught) {
      threadEncryptionStatus =
        'Encryption setup is required before anyone can reply. Replies remain disabled.';
      throw caught;
    }
  }

  async function createForumPost(
    forum: Channel,
    draft: { name: string; content: string; appliedTagIds: string[] }
  ) {
    if (!guild || forumPostBusy) return;
    const generation = loadGeneration;
    forumError = '';
    const attachmentIds = uploads
      .filter((item) => item.status === 'ready' && item.attachmentId)
      .map((item) => item.attachmentId as string);
    if (!draft.name.trim() || (!draft.content.trim() && !attachmentIds.length)) return;
    if (draft.content.length > FORUM_POST_CONTENT_MAX_LENGTH) {
      forumError = `Post messages can be at most ${FORUM_POST_CONTENT_MAX_LENGTH} characters.`;
      return;
    }
    forumPostBusy = true;
    try {
      const created = await createThread(forum, {
        ...draft,
        attachmentIds,
        autoArchiveDuration: forum.default_auto_archive_duration ?? 1440
      });
      if (generation !== loadGeneration) return;
      let createdChannel: Channel = {
        ...created.channel,
        starter_message: created.starter_message ?? created.channel.starter_message
      };
      rememberThread(createdChannel);
      clearSubmittedUploads(attachmentIds);
      if (threadRequiresE2EEActivation(createdChannel)) {
        try {
          createdChannel = await activateRequiredThread(createdChannel, forum);
        } catch {
          // The detail view keeps replies disabled and retries activation on reload.
        }
      }
      if (generation === loadGeneration) {
        window.location.assign(guildChannelPath(guild, createdChannel));
      }
    } catch (caught) {
      if (generation === loadGeneration)
        forumError = userErrorMessage(caught, 'Could not create the post. Try again.');
    } finally {
      if (generation === loadGeneration) forumPostBusy = false;
    }
  }

  async function reloadForumPosts(
    forum: Channel,
    filters: { query: string; selectedTagIds: string[]; sort: 'recent_activity' | 'creation_date' },
    silent = false
  ) {
    const generation = loadGeneration;
    const requestSequence = ++forumRequestSequence;
    const forumKey = entityKey(forum);
    forumFilterState = filters;
    if (!silent) forumLoading = true;
    forumLoadingMore = false;
    if (!silent) forumError = '';
    try {
      const options = {
        query: filters.query,
        tagIds: filters.selectedTagIds,
        sort: filters.sort
      } as const;
      const feed = await fetchThreads(forum, { ...options, includeArchived: true });
      const activeForum = isForumChannel(channel) ? channel : forumParent;
      if (
        generation !== loadGeneration ||
        requestSequence !== forumRequestSequence ||
        !activeForum ||
        entityKey(activeForum) !== forumKey
      )
        return;
      forumPosts = feed.threads;
      forumHasMore = feed.has_more;
      forumCursor = feed.next_cursor ?? '';
      for (const thread of forumPosts) rememberThread(thread);
    } catch (caught) {
      if (!silent && generation === loadGeneration && requestSequence === forumRequestSequence)
        forumError = userErrorMessage(caught, 'Could not search forum posts. Try again.');
    } finally {
      if (!silent && generation === loadGeneration && requestSequence === forumRequestSequence)
        forumLoading = false;
    }
  }

  function scheduleForumRefreshForChannel(channelId: string, channelDomain: string) {
    const thread = (guild?.channels ?? []).find(
      (item) => item.id === channelId && item.origin_domain === channelDomain
    );
    if (thread && isThreadChannel(thread)) scheduleForumRefreshForThread(thread);
  }

  function scheduleForumRefreshForThread(thread: {
    parent_id?: string | null;
    parent_domain?: string | null;
  }) {
    const forum = isForumChannel(channel) ? channel : forumParent;
    if (!forum || thread.parent_id !== forum.id || thread.parent_domain !== forum.origin_domain)
      return;
    if (forumRefreshTimer !== null) window.clearTimeout(forumRefreshTimer);
    const forumKey = entityKey(forum);
    forumRefreshTimer = window.setTimeout(() => {
      forumRefreshTimer = null;
      const activeForum = isForumChannel(channel) ? channel : forumParent;
      if (!activeForum || entityKey(activeForum) !== forumKey) return;
      void reloadForumPosts(activeForum, forumFilterState, true);
    }, 180);
  }

  async function loadMoreForumPosts(forum: Channel) {
    if (forumLoading || forumLoadingMore || !forumHasMore) return;
    const generation = loadGeneration;
    const requestSequence = forumRequestSequence;
    const forumKey = entityKey(forum);
    forumLoadingMore = true;
    forumError = '';
    const options = {
      query: forumFilterState.query,
      tagIds: forumFilterState.selectedTagIds,
      sort: forumFilterState.sort,
      limit: 100
    } as const;
    try {
      const page = await fetchThreads(forum, {
        ...options,
        includeArchived: true,
        cursor: forumCursor
      });
      const activeForum = isForumChannel(channel) ? channel : forumParent;
      if (
        generation !== loadGeneration ||
        requestSequence !== forumRequestSequence ||
        !activeForum ||
        entityKey(activeForum) !== forumKey
      )
        return;
      const appended = page.threads;
      forumPosts = [
        ...new Map(
          [...forumPosts, ...appended].map((thread) => [entityKey(thread), thread])
        ).values()
      ];
      forumHasMore = page.has_more;
      forumCursor = page.next_cursor ?? '';
      for (const thread of appended) rememberThread(thread);
    } catch (caught) {
      if (generation === loadGeneration && requestSequence === forumRequestSequence)
        forumError = userErrorMessage(caught, 'Could not load more forum posts. Try again.');
    } finally {
      if (generation === loadGeneration && requestSequence === forumRequestSequence)
        forumLoadingMore = false;
    }
  }

  function requestThreadForMessage(message: Message) {
    if (!canCreatePublicThreads || !channel || channel.encryption_mode === 'e2ee') return;
    threadCreateError = '';
    threadCreateSource = message;
  }

  async function createMessageThread(name: string) {
    if (!threadCreateSource || !channel || !guild || threadCreateBusy) return;
    const source = threadCreateSource;
    const parent = channel;
    const generation = loadGeneration;
    threadCreateBusy = true;
    threadCreateError = '';
    try {
      const created = await createThreadFromMessage(parent, source, name);
      if (generation !== loadGeneration) return;
      const createdChannel = {
        ...created.channel,
        starter_message: created.starter_message ?? created.channel.starter_message ?? source
      };
      rememberThread(createdChannel);
      threadCreateSource = null;
      window.location.assign(guildChannelPath(guild, createdChannel));
    } catch (caught) {
      if (generation === loadGeneration)
        threadCreateError = userErrorMessage(caught, 'Could not create the thread. Try again.');
    } finally {
      if (generation === loadGeneration) threadCreateBusy = false;
    }
  }

  async function openThreadDirectory() {
    const parent = threadDirectoryParent;
    if (!parent || threadDirectoryLoading) return;
    const generation = loadGeneration;
    const parentKey = entityKey(parent);
    threadDirectoryLoading = true;
    error = '';
    try {
      const [active, archived] = await Promise.all([
        fetchThreads(parent, { archived: false, limit: 100 }),
        fetchThreads(parent, { archived: true, limit: 100 })
      ]);
      if (
        generation !== loadGeneration ||
        !threadDirectoryParent ||
        entityKey(threadDirectoryParent) !== parentKey
      )
        return;
      threadDirectoryActive = active.threads;
      threadDirectoryArchived = archived.threads;
      threadDirectoryActiveHasMore = active.has_more;
      threadDirectoryArchivedHasMore = archived.has_more;
      threadDirectoryActiveCursor = active.next_cursor ?? '';
      threadDirectoryArchivedCursor = archived.next_cursor ?? '';
      for (const thread of [...active.threads, ...archived.threads]) rememberThread(thread);
    } catch (caught) {
      if (generation === loadGeneration)
        error = userErrorMessage(caught, 'Could not load the thread directory. Try again.');
    } finally {
      if (generation === loadGeneration) threadDirectoryLoading = false;
    }
  }

  async function loadMoreThreadDirectory(archived: boolean) {
    const parent = threadDirectoryParent;
    const hasMore = archived ? threadDirectoryArchivedHasMore : threadDirectoryActiveHasMore;
    if (!parent || !hasMore || threadDirectoryLoading || threadDirectoryLoadingMore) return;
    const generation = loadGeneration;
    const parentKey = entityKey(parent);
    threadDirectoryLoadingMore = true;
    try {
      const page = await fetchThreads(parent, {
        archived,
        limit: 100,
        cursor: archived ? threadDirectoryArchivedCursor : threadDirectoryActiveCursor
      });
      if (
        generation !== loadGeneration ||
        !threadDirectoryParent ||
        entityKey(threadDirectoryParent) !== parentKey
      )
        return;
      for (const thread of page.threads) rememberThread(thread);
      if (archived) {
        threadDirectoryArchivedHasMore = page.has_more;
        threadDirectoryArchivedCursor = page.next_cursor ?? '';
      } else {
        threadDirectoryActiveHasMore = page.has_more;
        threadDirectoryActiveCursor = page.next_cursor ?? '';
      }
    } catch (caught) {
      if (generation === loadGeneration)
        error = userErrorMessage(caught, 'Could not load more threads. Try again.');
    } finally {
      if (generation === loadGeneration) threadDirectoryLoadingMore = false;
    }
  }

  function showThreadDirectory() {
    if (!threadDirectoryParent) return;
    threadDirectoryOpen = true;
    void openThreadDirectory();
  }

  function openProjectedThread(thread: Channel) {
    if (!guild) return;
    rememberThread(thread);
    window.location.assign(guildChannelPath(guild, thread));
  }

  async function createDirectoryThread(draft: { name: string; message: string; private: boolean }) {
    const parent = threadDirectoryParent;
    if (
      !parent ||
      !guild ||
      threadDirectoryBusy ||
      (draft.private ? !canCreateDirectoryPrivateThread : !canCreateDirectoryPublicThread)
    )
      return;
    const generation = loadGeneration;
    threadDirectoryBusy = true;
    error = '';
    try {
      const created = await createThread(parent, {
        name: draft.name,
        content: parent.encryption_mode === 'e2ee' ? undefined : draft.message || undefined,
        type: draft.private ? 12 : parent.type === 5 ? 10 : 11,
        invitable: draft.private ? true : undefined
      });
      if (generation !== loadGeneration) return;
      let createdChannel: Channel = {
        ...created.channel,
        starter_message: created.starter_message ?? created.channel.starter_message
      };
      rememberThread(createdChannel);
      if (threadRequiresE2EEActivation(createdChannel)) {
        try {
          createdChannel = await activateRequiredThread(createdChannel, parent);
        } catch {
          // The detail view keeps replies disabled and retries activation on reload.
        }
      }
      window.location.assign(guildChannelPath(guild, createdChannel));
    } catch (caught) {
      if (generation === loadGeneration)
        error = userErrorMessage(caught, 'Could not create the thread. Try again.');
    } finally {
      if (generation === loadGeneration) threadDirectoryBusy = false;
    }
  }

  async function createNativeThread(name: string, message: string) {
    if (!channel || !guild || !canCreateNativeThread || busy) return;
    if (channel.encryption_mode === 'e2ee') {
      error = 'Threads cannot be created from an end-to-end encrypted parent channel.';
      return;
    }
    const generation = loadGeneration;
    busy = true;
    error = '';
    try {
      const created = await createThread(channel, {
        name,
        content: message,
        type: channel.type === 5 ? 10 : 11
      });
      if (generation !== loadGeneration) return;
      const createdChannel = {
        ...created.channel,
        starter_message: created.starter_message ?? created.channel.starter_message
      };
      rememberThread(createdChannel);
      content = '';
      composerCursor = 0;
      nativeThreadComposer = false;
      commandOptionValues = {};
      window.location.assign(guildChannelPath(guild, createdChannel));
    } catch (caught) {
      if (generation === loadGeneration)
        error = userErrorMessage(caught, 'Could not create the thread. Try again.');
    } finally {
      if (generation === loadGeneration) busy = false;
    }
  }

  async function changeThreadMembership(joined: boolean) {
    if (!channel || !isThreadChannel(channel) || threadActionBusy) return;
    threadActionBusy = true;
    error = '';
    try {
      const notificationLevel = currentThreadNotificationLevel;
      await setThreadMembership(channel, joined, notificationLevel);
      rememberThread({
        ...channel,
        member: joined
          ? { ...(currentThreadMember ?? {}), notification_level: notificationLevel }
          : null
      });
      threadMembers = await fetchThreadMembers(channel).catch(() => threadMembers);
    } catch (caught) {
      error = userErrorMessage(
        caught,
        joined ? 'Could not join this thread.' : 'Could not leave this thread.'
      );
    } finally {
      threadActionBusy = false;
    }
  }

  async function changeThreadNotifications(
    notificationLevel: NonNullable<ThreadMember['notification_level']>
  ) {
    if (
      !channel ||
      !isThreadChannel(channel) ||
      !currentThreadJoined ||
      channel.archived ||
      threadActionBusy
    )
      return;
    threadActionBusy = true;
    error = '';
    try {
      await setThreadMembership(channel, true, notificationLevel);
      rememberThread({
        ...channel,
        member: { ...(currentThreadMember ?? {}), notification_level: notificationLevel }
      });
      threadMembers = await fetchThreadMembers(channel).catch(() => threadMembers);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update thread notifications.');
    } finally {
      threadActionBusy = false;
    }
  }

  async function updateCurrentThreadEncryption() {
    if (
      !channel ||
      !isThreadChannel(channel) ||
      !currentUser ||
      threadActionBusy ||
      (!canEnableThreadEncryption && !canRekeyThreadEncryption)
    )
      return;
    const target = channel;
    const user = currentUser;
    const rekey = canRekeyThreadEncryption;
    const warning =
      'Turn on end-to-end encryption for this thread? This is permanent and protects only new content; existing history stays readable to the server. Server search, link and GIF previews, bots, webhooks, file previews, malware scanning, and PhotoDNA scanning will stop. Notifications become generic, while participants, timing, and message-size metadata remain visible. Participant identities remain unverified until everyone compares the safety number through a separate trusted channel; repeat that comparison after membership or identity changes to detect key substitution by an actively malicious instance. Losing the synchronized account vault, all trusted local state, and the recovery backup loses encrypted history. Removed members keep content they already received.';
    if (
      !window.confirm(
        rekey
          ? 'Create fresh encryption keys for the current thread members? Removed members and revoked devices will not receive the new keys.'
          : warning
      )
    )
      return;
    threadActionBusy = true;
    threadEncryptionBusy = true;
    threadEncryptionStatus = rekey
      ? 'Securing the current thread members…'
      : 'Turning on end-to-end encryption for new replies…';
    error = '';
    try {
      const client = e2eeClient ?? (await initializeE2EE(user));
      const updated = rekey
        ? await client.rekeyRoom(entityRef(target))
        : await client.activateRoom(entityRef(target));
      if (!rekey) acknowledgeEncryptedRoom(entityRef(user), entityRef(updated));
      e2eeClient = client;
      rememberThread(updated);
      e2eeSafetyNumber = await client.safetyNumber(updated).catch(() => '');
      threadEncryptionStatus = rekey
        ? 'Fresh encryption keys are active for the current thread members.'
        : 'End-to-end encryption is active for new replies.';
    } catch (caught) {
      const refreshed = await fetchChannel(entityRef(target)).catch(() => null);
      if (refreshed) rememberThread(refreshed);
      threadEncryptionStatus = rekey
        ? 'Encryption remains paused until the current members are secured.'
        : 'End-to-end encryption could not be activated.';
      error = userErrorMessage(caught, 'Could not update end-to-end encryption.');
    } finally {
      threadEncryptionBusy = false;
      threadActionBusy = false;
    }
  }

  async function patchCurrentThread(patch: {
    name?: string;
    archived?: boolean;
    locked?: boolean;
    invitable?: boolean;
    pinned?: boolean;
    applied_tag_ids?: string[];
  }) {
    if (!channel || !isThreadChannel(channel) || threadActionBusy) return false;
    const moderatorOnly = patch.locked !== undefined || patch.pinned !== undefined;
    if (moderatorOnly ? !canManageThreads : !canEditCurrentThread) return false;
    threadActionBusy = true;
    error = '';
    try {
      rememberThread(await updateThread(channel, patch));
      return true;
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the thread. Try again.');
      return false;
    } finally {
      threadActionBusy = false;
    }
  }

  async function changeThreadMember(userRef: string, joined: boolean) {
    if (
      !channel ||
      !isThreadChannel(channel) ||
      (joined ? !canInviteThreadMembers : !canRemoveThreadMembers) ||
      threadActionBusy
    )
      return;
    threadActionBusy = true;
    error = '';
    try {
      await setThreadMember(channel, userRef, joined);
      threadMembers = await fetchThreadMembers(channel);
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not update the thread member.');
    } finally {
      threadActionBusy = false;
    }
  }

  async function deleteCurrentThread() {
    if (
      !channel ||
      !isThreadChannel(channel) ||
      !parentChannel ||
      !guild ||
      !canManageThreads ||
      threadActionBusy
    )
      return;
    const label = isForumChannel(parentChannel) ? 'post' : 'thread';
    if (!window.confirm(`Delete “${channel.name ?? label}”? This cannot be undone.`)) return;
    threadActionBusy = true;
    error = '';
    try {
      await api(`/channels/${encodeURIComponent(entityRef(channel))}`, { method: 'DELETE' });
      window.location.assign(guildChannelPath(guild, parentChannel));
    } catch (caught) {
      error = userErrorMessage(caught, `Could not delete the ${label}. Try again.`);
      threadActionBusy = false;
    }
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
      if (channel?.archived) {
        error = 'Archived threads cannot be edited.';
        return;
      }
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
          `/channels/${encodeURIComponent(channelId)}/messages/${encodeURIComponent(entityRef(editing))}`,
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
    if (nativeThreadComposer && !retry) {
      const name = commandOptionValues.name?.trim() ?? '';
      const message = commandOptionValues.message?.trim() ?? '';
      if (!name || !message) return;
      await createNativeThread(name, message);
      return;
    }
    if (selectedApplicationCommand && !retry) {
      const selected = selectedApplicationCommand;
      if (!canUseApplicationCommands) {
        error = 'You do not have permission to use application commands in this channel.';
        return;
      }
      if (
        !commandOptionsComplete(selected, commandOptionValues) ||
        !channelReady ||
        !channel ||
        busy
      )
        return;
      if (channel.encryption_mode === 'e2ee') {
        error =
          'Bot commands are disabled in this E2EE channel until encrypted interactions are enabled in this client.';
        return;
      }
      if (channel.archived) {
        error = 'Commands are unavailable in archived threads.';
        return;
      }
      const generation = loadGeneration;
      busy = true;
      error = '';
      commandNotice = '';
      try {
        await api(`/channels/${encodeURIComponent(entityRef(channel))}/interactions`, {
          method: 'POST',
          body: JSON.stringify({
            application_ref: selected.application_ref,
            command_name: selected.name,
            command_type: selected.type,
            options: Object.fromEntries(
              Object.entries(commandOptionValues).filter(([, value]) => value.trim())
            )
          })
        });
        if (generation !== loadGeneration) return;
        selectedApplicationCommand = null;
        commandOptionValues = {};
        content = '';
        commandNotice = `/${selected.name} sent to ${selected.application_name}.`;
      } catch (caught) {
        if (generation === loadGeneration)
          error = userErrorMessage(caught, 'The bot command could not be delivered.');
      } finally {
        if (generation === loadGeneration) busy = false;
      }
      return;
    }
    if (!retry && /^\/thread(?:\s|$)/i.test(text)) {
      if (!canCreateNativeThread) {
        error =
          channel?.encryption_mode === 'e2ee'
            ? 'Threads cannot be created from an end-to-end encrypted parent channel.'
            : 'You do not have permission to create threads in this channel.';
        return;
      }
      const nativeThread = parseNativeThreadCommand(text);
      if (!nativeThread) {
        error = 'Use /thread with both name: and message: options.';
        return;
      }
      await createNativeThread(nativeThread.name, nativeThread.message);
      return;
    }
    const invocation = !retry ? commandInvocation(text, applicationCommands) : null;
    if (invocation) {
      if (!channelReady || !channel || busy) return;
      if (!canUseApplicationCommands) {
        error = 'You do not have permission to use application commands in this channel.';
        return;
      }
      if (channel.encryption_mode === 'e2ee') {
        error =
          'Bot commands are disabled in this E2EE channel until encrypted interactions are enabled in this client.';
        return;
      }
      if (channel.archived) {
        error = 'Commands are unavailable in archived threads.';
        return;
      }
      const generation = loadGeneration;
      busy = true;
      error = '';
      commandNotice = '';
      try {
        await api(`/channels/${encodeURIComponent(entityRef(channel))}/interactions`, {
          method: 'POST',
          body: JSON.stringify({
            application_ref: invocation.command.application_ref,
            command_name: invocation.command.name,
            command_type: invocation.command.type,
            options: invocation.options
          })
        });
        if (generation !== loadGeneration) return;
        content = '';
        composerCursor = 0;
        commandNotice = `/${invocation.command.name} sent to ${invocation.command.application_name}.`;
        window.setTimeout(() => {
          if (generation === loadGeneration) commandNotice = '';
        }, 5000);
      } catch (caught) {
        if (generation === loadGeneration)
          error = userErrorMessage(caught, 'The bot command could not be delivered.');
      } finally {
        if (generation === loadGeneration) busy = false;
      }
      return;
    }
    if (!canSendMessages) {
      error = 'You do not have permission to send messages in this channel.';
      return;
    }
    if (!editingMessage && slowmodeRemaining > 0) return;
    if (busy || !channelReady || !channel) return;
    const attachmentIds = retry
      ? retry.attachmentIds
      : uploads
          .filter((item) => item.status === 'ready' && item.attachmentId)
          .map((item) => item.attachmentId as string);
    if (!retry && uploads.some((item) => item.status === 'uploading')) return;
    const mentionUserIds = retry
      ? retry.mentionUserIds
      : [
          ...members
            .filter((member) => mentionsUser(text, member.user, localDomain))
            .map((member) => entityRef(member.user)),
          ...(replyingMessage?.author &&
          replyNotify &&
          (replyingMessage.author.id !== currentUser?.id ||
            replyingMessage.author.origin_domain !== currentUser?.origin_domain)
            ? [entityRef(replyingMessage.author)]
            : [])
        ].filter((value, index, values) => values.indexOf(value) === index);
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
    const routeChannel = channelId;
    const targetChannel = channel ? entityRef(channel) : channelId;
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
      replyNotify = true;
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
      const saved = await api<Message | { status: 'queued'; client_nonce: string }>(
        `/channels/${encodeURIComponent(targetChannel)}/messages`,
        {
          method: 'POST',
          body: JSON.stringify({
            content: encrypted ? null : draft.content,
            e2ee: encrypted,
            client_nonce: nonce,
            attachment_ids: draft.attachmentIds,
            mention_user_ids: draft.mentionUserIds,
            referenced_message_id: draft.referencedMessageId
          })
        }
      );
      if (generation !== loadGeneration || routeChannel !== channelId) return;
      if ('status' in saved) {
        setMessages(
          messages.map((item) =>
            item.client_nonce === saved.client_nonce && item.pending
              ? { ...item, pending: false, queued: true }
              : item
          )
        );
        clearSubmittedUploads(draft.attachmentIds);
        if (channel.rate_limit_per_user > 0 && !canBypassSlowmode)
          startSlowmode(channel.rate_limit_per_user * 1000);
        return;
      }
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
      if (channel.rate_limit_per_user > 0 && !canBypassSlowmode)
        startSlowmode(channel.rate_limit_per_user * 1000);
      await acknowledge(saved);
    } catch (caught) {
      if (generation !== loadGeneration || routeChannel !== channelId) return;
      const stillPending = messages.some((item) => item.client_nonce === nonce && item.pending);
      const timeoutFailure = caught instanceof ApiError && caught.code === 'MEMBER_TIMED_OUT';
      const slowmodeFailure = caught instanceof ApiError && caught.code === 'SLOWMODE_RATE_LIMITED';
      if (slowmodeFailure) {
        const retryAfter = Number(caught.detail.retry_after_ms);
        startSlowmode(
          Number.isFinite(retryAfter) ? retryAfter : channel.rate_limit_per_user * 1000
        );
        pendingSends.delete(nonce);
        setMessages(
          existing
            ? messages.map((item) =>
                item.client_nonce === nonce ? { ...item, pending: false } : item
              )
            : messages.filter((item) => item.client_nonce !== nonce)
        );
        if (!retry && draft.content) {
          content = draft.content;
          composerCursor = content.length;
          replyingMessage = draft.referencedMessageId
            ? (messages.find((item) =>
                matchesEntityRef(draft.referencedMessageId ?? '', item, localDomain)
              ) ?? null)
            : null;
        }
        error = '';
        return;
      }
      const timeoutReason = timeoutFailure ? formatTimeoutFailure(caught) : undefined;
      setMessages(
        failPendingMessage(messages, nonce, {
          reason: timeoutReason,
          retryable: !timeoutFailure
        })
      );
      if (stillPending) {
        if (caught instanceof ApiError && caught.code === 'ATTACHMENT_ALREADY_USED') {
          pendingSends.set(nonce, discardAttachments(draft));
          clearSubmittedUploads(draft.attachmentIds);
          error =
            'Those files were already used by another message. Reattach them before retrying.';
        } else {
          error =
            timeoutReason ?? userErrorMessage(caught, 'Could not send the message. Try again.');
        }
      }
    } finally {
      if (generation === loadGeneration && routeChannel === channelId) busy = false;
    }
  }

  async function queueFiles(files: FileList | File[]) {
    if (!channel || !canAttachFiles || busy || uploads.length >= 10) return;
    const target = entityRef(channel);
    const generation = loadGeneration;
    const routeChannel = channelId;
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
          if (
            controller.signal.aborted ||
            generation !== loadGeneration ||
            routeChannel !== channelId
          )
            return;
          uploads = uploads.map((item) => (item.key === key ? { ...item, progress } : item));
        },
        controller.signal
      )
        .then((ticket) => {
          uploadControllers.delete(key);
          if (generation !== loadGeneration || routeChannel !== channelId) return;
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
          if (
            controller.signal.aborted ||
            generation !== loadGeneration ||
            routeChannel !== channelId
          )
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
    const targetChannel = channel ? entityRef(channel) : channelId;
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
    replyNotify = Boolean(
      message.author &&
      (message.author.id !== currentUser?.id ||
        message.author.origin_domain !== currentUser?.origin_domain)
    );
    void tick().then(() => composerInput?.focus());
  }

  function cancelReply() {
    replyingMessage = null;
    replyNotify = true;
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
    if (!channel || !canPinMessages) return;
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
    const emojiExists = Number(message.reaction_counts?.[emoji] ?? 0) > 0;
    if (channel?.archived || !channel || (!remove && !canAddReactions && !emojiExists)) return;
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
    const target = messageReferenceTarget(message);
    if (!target) return;
    const reference = entityRef(target);
    if (
      guild &&
      target.channel_id &&
      target.channel_domain &&
      !matchesEntityRef(
        channelId,
        {
          id: target.channel_id,
          origin_domain: target.channel_domain
        },
        localDomain
      )
    ) {
      const destination = guildChannelPath(guild, {
        id: target.channel_id,
        origin_domain: target.channel_domain
      });
      window.location.assign(`${destination}?${new URLSearchParams({ around: reference })}`);
      return;
    }
    jumpToMessageReference(reference);
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
    const routeChannel = channelId;
    try {
      await api(
        `/channels/${encodeURIComponent(routeChannel)}/messages/${encodeURIComponent(entityRef(message))}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration || routeChannel !== channelId) return;
      setMessages(
        messages.map((item) =>
          entityKey(item) === entityKey(message)
            ? { ...item, content: null, deleted_at: new Date().toISOString() }
            : item
        )
      );
      if (editingMessage && entityKey(editingMessage) === entityKey(message)) finishEditing();
    } catch (caught) {
      if (generation !== loadGeneration || routeChannel !== channelId) return;
      error = userErrorMessage(caught, 'Could not delete the message. Try again.');
    }
  }

  async function messageUser(user: UserSummary) {
    if (currentUser && entityKey(user) === entityKey(currentUser)) return;
    if (user.profile_resolved === false) return;
    const generation = loadGeneration;
    const routeGuild = guildId;
    const routeChannel = channelId;
    const stillCurrent = () =>
      generation === loadGeneration && routeGuild === guildId && routeChannel === channelId;
    try {
      const opened = await api<
        import('$lib/chat/types').Channel | { status: 'queued'; operation_id: string }
      >('/users/@me/channels', {
        method: 'POST',
        body: JSON.stringify({ handle: user.handle })
      });
      if (!stillCurrent()) return;
      if ('status' in opened) {
        error = 'The direct-message request is queued with the recipient’s instance.';
        return;
      }
      window.location.assign(directMessagePath(opened));
    } catch (caught) {
      if (!stillCurrent()) return;
      error = userErrorMessage(caught, 'Could not open a direct message. Try again.');
    }
  }

  async function messageAuthor(message: Message) {
    if (message.author) await messageUser(message.author);
  }

  function retryMessage(message: Message) {
    editingMessage = null;
    composerDraftBeforeEdit = null;
    if (message.delivery_status === 'failed') {
      content = message.content ?? '';
      composerCursor = content.length;
      if (!content) error = 'Reattach this message’s files before sending it again.';
      void tick().then(() => composerInput?.focus());
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
    if (completion.kind === 'application-command') {
      const commandName = completion.value.replace(/^\//, '');
      if (commandName === 'thread' && canCreateNativeThread) {
        nativeThreadComposer = true;
        selectedApplicationCommand = null;
        commandOptionValues = {};
        content = '';
        composerCursor = 0;
        return;
      }
      const selected = applicationCommands.find(
        (command) => command.type === 'chat_input' && command.name === commandName
      );
      if (selected && commandStringOptions(selected).length) {
        selectedApplicationCommand = selected;
        nativeThreadComposer = false;
        commandOptionValues = {};
        content = '';
        composerCursor = 0;
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

  function cancelCommandComposer() {
    nativeThreadComposer = false;
    selectedApplicationCommand = null;
    commandOptionValues = {};
    content = '';
    composerCursor = 0;
    void tick().then(() => composerInput?.focus());
  }

  function timelineBottomChanged(value: boolean) {
    timelineAtBottom = value;
    if (value) acknowledgeLatestIfVisible();
  }
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- route helpers resolve the typed template before substituting encoded parameters -->

<svelte:head><title>{channel?.name ?? 'Channel'} · Kaede Chat</title></svelte:head>
<svelte:window
  onclick={() => {
    closeChannelMenu(false);
    closeVoiceMemberMenu();
  }}
  onkeydown={(event) => {
    if (moderationDialog && event.key === 'Escape') {
      event.preventDefault();
      closeModerationDialog();
      return;
    }
    if (inviteDialogOpen) {
      inviteDialogKeydown(event);
      return;
    }
    if (channelDeleteTarget) {
      channelDeleteDialogKeydown(event);
      return;
    }
    if (channelDialogOpen) {
      channelDialogKeydown(event);
      return;
    }
    if (channelMenu) {
      channelMenuKeydown(event);
      return;
    }
    if (mobileNavigationOpen) {
      mobileNavigationKeydown(event);
      return;
    }
  }}
/>

{#if mobileNavigationOpen}
  <button
    class="mobile-sidebar-backdrop"
    type="button"
    aria-label="Close guild navigation"
    onclick={() => closeMobileNavigation()}
  ></button>
{/if}

{#snippet voiceMembers(target: Channel)}
  {#if target.type === 2}
    {@const voiceKey = entityKey(target)}
    {#if occupantsFor(target).length}
      <div class="voice-channel-members" aria-label={`People in ${target.name}`}>
        {#each occupantsFor(target) as occupant (occupant.identity)}
          {@const voiceMember = memberFor(occupant.user_id, occupant.user_domain)}
          {#if voiceMember}
            <button
              type="button"
              draggable={canMoveVoiceMember(voiceMember.user, target) && !voiceModerationBusy}
              title={`${userDisplayName(voiceMember.user)}${occupant.self_mute || occupant.server_mute ? ' · muted' : ''}`}
              ondragstart={(event) =>
                voiceMemberDragStart(event, occupant, voiceMember.user, target)}
              ondragend={voiceMemberDragEnd}
              oncontextmenu={(event) =>
                openVoiceMemberMenu(event, occupant, voiceMember.user, target)}
              onclick={(event) => openProfile(voiceMember.user, event)}
            >
              <span class="voice-member-avatar">
                {#if voiceMember.user.avatar_hash}
                  <img
                    src={assetUrl(voiceMember.user.avatar_hash, 'thumbnail_128', voiceMember.user)}
                    alt=""
                  />
                {:else}
                  {(voiceMember.nickname ?? userDisplayName(voiceMember.user))
                    .slice(0, 1)
                    .toUpperCase()}
                {/if}
                <i class={`presence-dot presence-${presenceFor(voiceMember.user)}`}></i>
              </span>
              <span>{voiceMember.nickname ?? userDisplayName(voiceMember.user)}</span>
              {#if occupant.self_mute || occupant.server_mute}<Icon
                  name="microphone-off"
                  size={13}
                />{/if}
              {#if occupant.self_deaf || occupant.server_deaf}<span
                  class="voice-state-icon"
                  aria-label="Deafened"
                  title="Deafened"><Icon name="headphones-off" size={13} /></span
                >{/if}
            </button>
          {/if}
        {/each}
      </div>
    {/if}
    {#if voiceOccupancyErrors[voiceKey]}
      <div class="voice-roster-health" role="status">
        <span title={voiceOccupancyErrors[voiceKey]}>{voiceOccupancyErrors[voiceKey]}</span>
        <button
          type="button"
          disabled={voiceOccupancyLoading[voiceKey]}
          onclick={() => void loadVoiceOccupancy([target], loadGeneration)}
        >
          {voiceOccupancyLoading[voiceKey] ? 'Retrying…' : 'Retry'}
        </button>
      </div>
    {/if}
  {/if}
{/snippet}

<main class:member-roster-visible={memberRosterOpen} class="chat-app">
  <GuildRail
    {guilds}
    homeHref={resolve('/home')}
    {homeUnreadCount}
    guildHref={guildLandingPath}
    mentionCount={(item) => guildMentionCount(readStates, item)}
    activeGuildKey={guild ? entityKey(guild) : null}
  />
  <aside
    bind:this={mobileNavigationDrawer}
    class:mobile-open={mobileNavigationOpen}
    class="channel-sidebar"
    id="guild-channel-navigation"
    role={mobileNavigationOpen ? 'dialog' : undefined}
    aria-modal={mobileNavigationOpen ? 'true' : undefined}
    aria-label="Guild navigation"
  >
    <header>
      <div class="sidebar-heading">
        <div>
          <p>Guild</p>
          <h2>{guild?.name ?? 'Loading…'}</h2>
          {#if canCreateCurrentChannelInvite}
            <button
              class="guild-quick-invite"
              type="button"
              onclick={(event) => void openQuickInvite(event.currentTarget)}
            >
              <Icon name="plus" size={13} strokeWidth={2.2} />Invite people
            </button>
          {/if}
        </div>
        <div class="mobile-sidebar-tools">
          {#if guild}
            <a
              class="sidebar-settings"
              href={guildSettingsPath(guild)}
              aria-label="Guild settings"
              title="Guild settings"
              onclick={() => closeMobileNavigation(false)}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path
                  d="M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Zm8 3.5-.1-1.2 2-1.5-2-3.4-2.4 1a8.8 8.8 0 0 0-2.1-1.2L15 3h-4l-.4 2.7c-.8.3-1.5.7-2.1 1.2l-2.4-1-2 3.4 2 1.5A9.7 9.7 0 0 0 6 12l.1 1.2-2 1.5 2 3.4 2.4-1c.6.5 1.3.9 2.1 1.2L11 21h4l.4-2.7c.8-.3 1.5-.7 2.1-1.2l2.4 1 2-3.4-2-1.5.1-1.2Z"
                />
              </svg>
            </a>
          {/if}
          <button
            bind:this={mobileNavigationClose}
            class="mobile-sidebar-close"
            type="button"
            aria-label="Close guild navigation"
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
      <div class="channel-header-actions">
        {#if channel?.type !== 2 && channel?.type !== 4}
          <button
            class:active={pinsOpen}
            class="icon-button"
            type="button"
            aria-label={pinsOpen ? 'Hide pinned messages' : 'Show pinned messages'}
            aria-pressed={pinsOpen}
            title="Pinned messages"
            onclick={togglePins}>📌</button
          >
        {/if}
      </div>
    </header>
    <div class="sidebar-section-heading">
      <p class="sidebar-section-label">Channels</p>
      {#if canManageChannels}
        <button
          type="button"
          aria-label="Create channel"
          title="Create channel"
          onclick={(event) => {
            event.stopPropagation();
            openChannelDialog(0, null, null, event.currentTarget);
          }}>+</button
        >
      {/if}
    </div>
    <p class="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
      {channelOrderStatus}
    </p>
    <nav
      class="channel-tree"
      aria-label="Channels"
      aria-busy={reorderingChannels}
      oncontextmenu={(event) => showChannelMenu(event, null)}
    >
      {#each channelGroups as group (group.key)}
        {#if group.category}
          <section class="channel-category">
            <div
              class:drag-over={dragOverChannelKey === entityKey(group.category)}
              class="channel-category-heading"
              role="group"
              draggable={canManageChannels && !reorderingChannels}
              ondragstart={(event) => channelDragStart(event, group.category!)}
              ondragend={channelDragEnd}
              ondragover={(event) => channelDragOver(event, group.category)}
              ondragleave={() => (dragOverChannelKey = null)}
              ondrop={(event) => channelDrop(event, group.category)}
              oncontextmenu={(event) => showChannelMenu(event, group.category)}
            >
              <button
                class="category-toggle"
                type="button"
                aria-expanded={!collapsedCategories.has(entityKey(group.category))}
                aria-haspopup="menu"
                aria-label={`${collapsedCategories.has(entityKey(group.category)) ? 'Expand' : 'Collapse'} ${group.category.name}`}
                onkeydown={(event) => showChannelMenuFromKeyboard(event, group.category)}
                onclick={(event) => {
                  event.stopPropagation();
                  toggleCategory(group.category!);
                }}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m5 3 5 5-5 5" /></svg>
              </button>
              <span>{group.category.name}</span>
              {#if canManageChannels}
                <button
                  class="category-add"
                  type="button"
                  aria-label={`Create a channel in ${group.category.name}`}
                  title="Create channel"
                  onclick={(event) => {
                    event.stopPropagation();
                    openChannelDialog(0, group.category, null, event.currentTarget);
                  }}>+</button
                >
              {/if}
              <button
                class="channel-row-actions"
                type="button"
                aria-label={`Actions for ${group.category.name}`}
                aria-haspopup="menu"
                aria-controls="channel-context-menu"
                aria-expanded={channelMenu?.channel
                  ? entityKey(channelMenu.channel) === entityKey(group.category)
                  : false}
                onclick={(event) => {
                  event.stopPropagation();
                  openChannelMenu(group.category, event.currentTarget);
                }}
              >
                <Icon name="more" size={18} />
              </button>
            </div>
            {#if !collapsedCategories.has(entityKey(group.category))}
              <div class="category-channels">
                {#each group.channels as item (entityKey(item))}
                  {@const unread = channelUnreadPresentation(unreadFor(item))}
                  <div class="channel-row">
                    <a
                      class:active={matchesEntityRef(channelId, item, localDomain)}
                      class:channel-unread={unread.unread}
                      class:channel-unread-dot={unread.showUnreadDot}
                      class:drag-over={dragOverChannelKey === entityKey(item) ||
                        voiceDropChannelKey === entityKey(item)}
                      draggable={canManageChannels && !reorderingChannels}
                      href={guild ? guildChannelPath(guild, item) : resolve('/home')}
                      aria-haspopup="menu"
                      aria-current={matchesEntityRef(channelId, item, localDomain)
                        ? 'page'
                        : undefined}
                      ondragstart={(event) => channelDragStart(event, item)}
                      ondragend={channelDragEnd}
                      ondragover={(event) => channelDragOver(event, item)}
                      ondragleave={() => (dragOverChannelKey = null)}
                      ondrop={(event) => channelDrop(event, item)}
                      oncontextmenu={(event) => showChannelMenu(event, item)}
                      onkeydown={(event) => showChannelMenuFromKeyboard(event, item)}
                      onclick={() => closeMobileNavigation(false)}
                    >
                      <span>
                        <Icon
                          name={item.type === 2
                            ? 'volume'
                            : item.type === 5
                              ? 'bell'
                              : item.type === 15
                                ? 'forum'
                                : 'hash'}
                          size={16}
                        />
                        {item.name}
                      </span>
                      {#if unread.mentionCount > 0}
                        <small class="unread-badge">{compactBadgeCount(unread.mentionCount)}</small>
                      {/if}
                    </a>
                    <button
                      class="channel-row-actions"
                      type="button"
                      aria-label={`Actions for ${item.name}`}
                      aria-haspopup="menu"
                      aria-controls="channel-context-menu"
                      aria-expanded={channelMenu?.channel
                        ? entityKey(channelMenu.channel) === entityKey(item)
                        : false}
                      onclick={(event) => {
                        event.stopPropagation();
                        openChannelMenu(item, event.currentTarget);
                      }}
                    >
                      <Icon name="more" size={18} />
                    </button>
                  </div>
                  {@render voiceMembers(item)}
                  {#if guild && isThreadParentChannel(item)}
                    <div class="active-thread-list">
                      {#each activeThreadsForParent(guild.channels ?? [], item) as activeThread (entityKey(activeThread))}
                        <a
                          class:active={matchesEntityRef(channelId, activeThread, localDomain)}
                          href={guildChannelPath(guild, activeThread)}
                          aria-current={matchesEntityRef(channelId, activeThread, localDomain)
                            ? 'page'
                            : undefined}
                          onclick={() => closeMobileNavigation(false)}
                          ><span aria-hidden="true">└</span><Icon
                            name="message"
                            size={14}
                          />{activeThread.name}</a
                        >
                      {/each}
                    </div>
                  {/if}
                {/each}
              </div>
            {/if}
          </section>
        {:else}
          <div
            class:drag-over={dragOverChannelKey === 'ungrouped'}
            class="uncategorized-channels"
            role="group"
            ondragover={(event) => channelDragOver(event, null)}
            ondragleave={() => (dragOverChannelKey = null)}
            ondrop={(event) => channelDrop(event, null)}
          >
            {#each group.channels as item (entityKey(item))}
              {@const unread = channelUnreadPresentation(unreadFor(item))}
              <div class="channel-row">
                <a
                  class:active={matchesEntityRef(channelId, item, localDomain)}
                  class:channel-unread={unread.unread}
                  class:channel-unread-dot={unread.showUnreadDot}
                  class:drag-over={dragOverChannelKey === entityKey(item) ||
                    voiceDropChannelKey === entityKey(item)}
                  draggable={canManageChannels && !reorderingChannels}
                  href={guild ? guildChannelPath(guild, item) : resolve('/home')}
                  aria-haspopup="menu"
                  aria-current={matchesEntityRef(channelId, item, localDomain) ? 'page' : undefined}
                  ondragstart={(event) => channelDragStart(event, item)}
                  ondragend={channelDragEnd}
                  ondragover={(event) => channelDragOver(event, item)}
                  ondragleave={() => (dragOverChannelKey = null)}
                  ondrop={(event) => channelDrop(event, item)}
                  oncontextmenu={(event) => showChannelMenu(event, item)}
                  onkeydown={(event) => showChannelMenuFromKeyboard(event, item)}
                  onclick={() => closeMobileNavigation(false)}
                >
                  <span>
                    <Icon
                      name={item.type === 2
                        ? 'volume'
                        : item.type === 5
                          ? 'bell'
                          : item.type === 15
                            ? 'forum'
                            : 'hash'}
                      size={16}
                    />
                    {item.name}
                  </span>
                  {#if unread.mentionCount > 0}
                    <small class="unread-badge">{compactBadgeCount(unread.mentionCount)}</small>
                  {/if}
                </a>
                <button
                  class="channel-row-actions"
                  type="button"
                  aria-label={`Actions for ${item.name}`}
                  aria-haspopup="menu"
                  aria-controls="channel-context-menu"
                  aria-expanded={channelMenu?.channel
                    ? entityKey(channelMenu.channel) === entityKey(item)
                    : false}
                  onclick={(event) => {
                    event.stopPropagation();
                    openChannelMenu(item, event.currentTarget);
                  }}
                >
                  <Icon name="more" size={18} />
                </button>
              </div>
              {@render voiceMembers(item)}
              {#if guild && isThreadParentChannel(item)}
                <div class="active-thread-list">
                  {#each activeThreadsForParent(guild.channels ?? [], item) as activeThread (entityKey(activeThread))}
                    <a
                      class:active={matchesEntityRef(channelId, activeThread, localDomain)}
                      href={guildChannelPath(guild, activeThread)}
                      aria-current={matchesEntityRef(channelId, activeThread, localDomain)
                        ? 'page'
                        : undefined}
                      onclick={() => closeMobileNavigation(false)}
                      ><span aria-hidden="true">└</span><Icon
                        name="message"
                        size={14}
                      />{activeThread.name}</a
                    >
                  {/each}
                </div>
              {/if}
            {/each}
          </div>
        {/if}
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
        {#if currentUser?.custom_status?.trim()}
          <small title={currentUser.custom_status}>{currentUser.custom_status}</small>
        {/if}
        <PresencePicker value={presencePreference} onChange={setMyPresence} />
      </div>
      <a class="icon-button" href={resolve('/settings')} aria-label="User settings">
        <Icon name="settings" size={18} />
      </a>
    </div>
  </aside>
  <section
    class:guild-voice-pane={channel?.type === 2}
    class:sync-paused={guild?.sync_status === 'quota_paused'}
    class:has-status-warning={Boolean(replicaSyncWarning || historySyncWarning || timeoutGuidance)}
    class="message-pane"
  >
    <header class:guild-voice-header={channel?.type === 2} class="channel-header">
      <div class="channel-header-primary">
        <button
          bind:this={mobileNavigationToggle}
          class="mobile-sidebar-toggle"
          type="button"
          aria-label={mobileNavigationOpen ? 'Close guild navigation' : 'Open guild navigation'}
          aria-controls="guild-channel-navigation"
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
          <span class="channel-mark" aria-hidden="true">
            {#if channel?.type === 2}
              <Icon name="volume" size={18} />
            {:else if channel?.type === 5}
              <Icon name="bell" size={18} />
            {:else if isForumChannel(channel)}
              <Icon name="forum" size={18} />
            {:else if isThreadChannel(channel)}
              <Icon name="message" size={18} />
            {:else}
              #
            {/if}
          </span>
          <div>
            <strong>{channel?.name ?? 'Channel'}</strong>
            {#if channel?.topic && !isThreadChannel(channel)}<span>{channel.topic}</span>{/if}
          </div>
        </div>
      </div>
      <div class="channel-header-actions">
        {#if guild && threadDirectoryParent}
          <ThreadsPanel
            bind:open={threadDirectoryOpen}
            {guild}
            parent={threadDirectoryParent}
            activeThreads={threadDirectoryActive}
            archivedThreads={threadDirectoryArchived}
            loading={threadDirectoryLoading}
            loadingMore={threadDirectoryLoadingMore}
            activeHasMore={threadDirectoryActiveHasMore}
            archivedHasMore={threadDirectoryArchivedHasMore}
            busy={threadDirectoryBusy}
            canCreatePublic={canCreateDirectoryPublicThread}
            canCreatePrivate={canCreateDirectoryPrivateThread}
            canSendStarter={canSendDirectoryStarter}
            onOpen={openThreadDirectory}
            onLoadMore={loadMoreThreadDirectory}
            onCreate={createDirectoryThread}
          />
        {/if}
        {#if channel?.encryption_mode === 'e2ee'}
          <button
            class="icon-button active e2ee-status-button"
            type="button"
            aria-label="End-to-end encryption is on; view safety number"
            title="End-to-end encrypted · identities unverified until the safety number is compared"
            onclick={showEncryptionInfo}
          >
            <Icon name="lock" size={18} />
            <span>{channel.encryption_state === 'active' ? 'Encrypted' : 'Rekey needed'}</span>
          </button>
        {/if}
        {#if !isForumChannel(channel)}
          <MessageSearch
            bind:open={messageSearchOpen}
            scope="guild"
            scopeRef={guild ? entityRef(guild) : guildId}
            accountRef={currentUser ? entityRef(currentUser) : null}
            {channel}
            users={messageSearchUsers}
            placement="header"
          />
        {/if}
        <button
          class:active={memberRosterOpen}
          class="icon-button member-roster-toggle"
          type="button"
          aria-label={memberRosterOpen ? 'Hide member list' : 'Show member list'}
          aria-pressed={memberRosterOpen}
          title={memberRosterOpen ? 'Hide member list' : 'Show member list'}
          onclick={toggleMemberRoster}
        >
          <Icon name="users" size={19} />
        </button>
      </div>
    </header>
    {#if guild && channel && isThreadChannel(channel)}
      <ThreadHeader
        {guild}
        thread={channel}
        parent={parentChannel}
        joined={currentThreadJoined}
        notificationLevel={currentThreadNotificationLevel}
        starterMessage={forumStarterMessage}
        reactionEmoji={forumDefaultReaction}
        canReact={canAddReactions ||
          Number(forumStarterMessage?.reaction_counts?.[forumDefaultReaction] ?? 0) > 0 ||
          Boolean(forumStarterMessage?.reacted_emoji?.includes(forumDefaultReaction))}
        canEdit={canEditCurrentThread}
        canManage={canManageThreads}
        canInviteMembers={canInviteThreadMembers}
        canRemoveMembers={canRemoveThreadMembers}
        canEnableEncryption={canEnableThreadEncryption}
        canRekeyEncryption={canRekeyThreadEncryption}
        busy={threadActionBusy}
        encryptionStatus={threadEncryptionStatus}
        {threadMembers}
        guildMembers={members}
        availableTags={forumParent?.available_tags ?? []}
        customEmojis={pickerEmojis}
        onMembership={changeThreadMembership}
        onNotifications={changeThreadNotifications}
        onReaction={toggleMessageReaction}
        onRename={(name) => patchCurrentThread({ name })}
        onEncryption={updateCurrentThreadEncryption}
        onInvitable={(invitable) => void patchCurrentThread({ invitable })}
        onArchive={(archived) =>
          void patchCurrentThread(
            !archived && channel.locked && canManageThreads
              ? { archived, locked: false }
              : { archived }
          )}
        onLock={(locked) =>
          void patchCurrentThread(
            !locked && channel.archived ? { archived: false, locked } : { locked }
          )}
        onMemberChange={changeThreadMember}
        onPin={(pinned) => void patchCurrentThread({ pinned })}
        onTagsChange={(applied_tag_ids) => void patchCurrentThread({ applied_tag_ids })}
        onDelete={deleteCurrentThread}
      />
    {/if}
    {#if readStateWarning}
      <div class="read-state-warning" role="status">
        <span>{readStateWarning}</span>
        <button type="button" onclick={() => void readAcknowledgements.retryNow()}>Retry now</button
        >
      </div>
    {/if}
    {#if replicaSyncWarning || historySyncWarning || timeoutGuidance}
      <div class="status-warnings">
        {#if replicaSyncWarning}
          <div class="sync-warning" role={replicaSyncWarning.severity}>
            <strong>{replicaSyncWarning.title}</strong>
            <span>{replicaSyncWarning.message}</span>
          </div>
        {:else if historySyncWarning}
          <div class="sync-warning" role={historySyncWarning.severity}>
            <strong>{historySyncWarning.title}</strong>
            <span>{historySyncWarning.message}</span>
          </div>
        {/if}
        {#if timeoutGuidance}
          <div class="timeout-warning" role="status">
            <strong>{timeoutGuidance.title}</strong>
            <span>{timeoutGuidance.message}</span>
            {#if selfModerationWarning}<small>{selfModerationWarning}</small>{/if}
          </div>
        {/if}
      </div>
    {/if}
    {#if channel?.type === 2}
      <div class="guild-voice-content">
        {#key entityRef(channel)}
          <VoiceDock channelRef={entityRef(channel)} permissions={channel.permissions ?? '0'} />
        {/key}
      </div>
    {:else if channel && guild && isForumChannel(channel)}
      <ForumView
        {guild}
        forum={channel}
        posts={forumPosts}
        loading={forumLoading}
        loadingMore={forumLoadingMore}
        hasMore={forumHasMore}
        error={forumError}
        canCreate={canCreateForumPost}
        canManageTags={channelHasPermission(channel, MANAGE_THREADS)}
        customEmojis={pickerEmojis}
        busy={forumPostBusy}
        {uploads}
        onCreate={(draft) => createForumPost(channel, draft)}
        onFiltersChange={(filters) => reloadForumPosts(channel, filters)}
        onLoadMore={() => loadMoreForumPosts(channel)}
        onFiles={canAttachFiles ? queueFiles : undefined}
        onRemoveUpload={removeUpload}
      />
    {:else}
      <div
        class:forum-thread-split={Boolean(
          guild && forumParent && channel && isThreadChannel(channel)
        )}
        class="conversation-layout"
      >
        {#if guild && forumParent && channel && isThreadChannel(channel)}
          <ForumView
            {guild}
            forum={forumParent}
            posts={forumPosts}
            loading={forumLoading}
            loadingMore={forumLoadingMore}
            hasMore={forumHasMore}
            error={forumError}
            canCreate={channelHasPermission(forumParent, Permission.SEND_MESSAGES)}
            canManageTags={channelHasPermission(forumParent, MANAGE_THREADS)}
            customEmojis={pickerEmojis}
            busy={forumPostBusy}
            compact
            onCreate={(draft) => createForumPost(forumParent, draft)}
            onFiltersChange={(filters) => reloadForumPosts(forumParent, filters)}
            onLoadMore={() => loadMoreForumPosts(forumParent)}
          />
        {/if}
        <div class="thread-conversation">
          <div
            class="message-list"
            aria-live="polite"
            role="log"
            aria-label={`Messages in ${channel?.name ?? 'channel'}`}
          >
            {#if error}<p class="form-error message-error" role="alert">{error}</p>{/if}
            {#snippet emptyTimeline()}
              {#if channelReady && channel && !threadTimelineStarter}
                <section class="channel-welcome">
                  <span class="welcome-mark" aria-hidden="true">#</span>
                  <h2>Welcome to #{channel.name}</h2>
                  <p>This is the beginning of the conversation.</p>
                </section>
              {/if}
            {/snippet}
            {#snippet threadStarterHeader()}
              {#if threadTimelineStarter}
                <div
                  class="thread-starter-reference"
                  aria-label="Original message that started this thread"
                >
                  <MessageRow
                    message={threadTimelineStarter}
                    authorColor={threadTimelineStarter.author
                      ? memberRoleColor(
                          memberFor(
                            threadTimelineStarter.author_id,
                            threadTimelineStarter.author_domain
                          ),
                          guild?.roles ?? []
                        )
                      : undefined}
                    mentionUsers={entities.users.values}
                    mentionRoles={guild?.roles ?? []}
                    presence={threadTimelineStarter.author
                      ? presenceFor(threadTimelineStarter.author)
                      : 'offline'}
                    actionsEnabled={false}
                    timestampFormat="date-time"
                    domIdPrefix="thread-starter"
                    onViewProfile={threadTimelineStarter.author ? openMessageProfile : undefined}
                  />
                </div>
              {/if}
            {/snippet}
            {#key channelId}
              <VirtualMessageList
                items={timeline}
                empty={emptyTimeline}
                header={threadStarterHeader}
                {hasEarlier}
                {loadingEarlier}
                {hasLater}
                {loadingLater}
                onLoadEarlier={loadEarlier}
                onLoadLater={loadLater}
                targetKey={targetTimelineKey}
                onBottomChange={timelineBottomChanged}
                label={`Messages in ${channel?.name ?? 'channel'}`}
              >
                {#snippet renderItem(item)}
                  {#if item.kind === 'day'}
                    <div class="timeline-divider" role="separator"><span>{item.label}</span></div>
                  {:else if item.kind === 'new'}
                    <div class="timeline-divider new" role="separator">
                      <span>{item.label}</span>
                    </div>
                  {:else}
                    <MessageRow
                      message={item.message}
                      compact={item.compact}
                      authorColor={item.message.author
                        ? memberRoleColor(
                            memberFor(item.message.author_id, item.message.author_domain),
                            guild?.roles ?? []
                          )
                        : undefined}
                      mentionUsers={entities.users.values}
                      mentionRoles={guild?.roles ?? []}
                      referencedMessage={referencedMessage(item.message)}
                      pinned={pinnedMessages.some(
                        (pinned) => entityKey(pinned) === entityKey(item.message)
                      )}
                      presence={item.message.author ? presenceFor(item.message.author) : 'offline'}
                      canEdit={!channel?.archived &&
                        (!channel?.locked || canManageThreads) &&
                        item.message.author_id === currentUser?.id &&
                        item.message.author_domain === currentUser?.origin_domain}
                      canDelete={((item.message.author_id === currentUser?.id &&
                        item.message.author_domain === currentUser?.origin_domain) ||
                        Boolean(
                          channel && channelHasPermission(channel, Permission.MANAGE_MESSAGES)
                        )) &&
                        (!channel?.archived || !channel?.locked || canManageThreads)}
                      onEdit={startEditing}
                      onDelete={deleteMessage}
                      onMessageAuthor={item.message.author &&
                      item.message.author.profile_resolved !== false &&
                      (item.message.author_id !== currentUser?.id ||
                        item.message.author_domain !== currentUser?.origin_domain)
                        ? messageAuthor
                        : undefined}
                      onRetry={retryMessage}
                      onViewProfile={openMessageProfile}
                      onReply={startReply}
                      onCreateThread={canCreatePublicThreads &&
                      item.message.message_type === 0 &&
                      !item.message.id.startsWith('pending-')
                        ? requestThreadForMessage
                        : undefined}
                      onOpenThread={openProjectedThread}
                      onOpenThreads={showThreadDirectory}
                      canReact={canAddReactions}
                      canReactToExisting={Boolean(channel && !channel.archived)}
                      customEmojis={pickerEmojis}
                      reactionUserKey={currentUser ? entityKey(currentUser) : ''}
                      onToggleReaction={!channel?.archived ? toggleMessageReaction : undefined}
                      onJumpToReference={jumpToReply}
                      onTogglePin={canPinMessages ? togglePinnedMessage : undefined}
                      moderationActions={item.message.author
                        ? moderationActionsFor(item.message.author)
                        : []}
                      onModerate={requestModeration}
                    />
                  {/if}
                {/snippet}
              </VirtualMessageList>
            {/key}
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
                  {#if replyingMessage.author && (replyingMessage.author.id !== currentUser?.id || replyingMessage.author.origin_domain !== currentUser?.origin_domain)}
                    <label class="reply-notify-toggle">
                      <input type="checkbox" bind:checked={replyNotify} />
                      Notify author
                    </label>
                  {/if}
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
              listboxId="guild-message-suggestions"
              onActiveIndexChange={(index) => (completionActive = index)}
              onOpenChange={(open) => (completionOpen = open)}
              onSelect={chooseCompletion}
            />
            {#if canSendMessages || canCreateNativeThread || editingMessage}
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
                  disabled={busy ||
                    !channelReady ||
                    !channel ||
                    !canAttachFiles ||
                    Boolean(editingMessage || nativeThreadComposer || selectedApplicationCommand)}
                  onclick={() => fileInput?.click()}
                  aria-label={canAttachFiles
                    ? 'Attach files'
                    : 'You cannot attach files in this channel'}
                  title={canAttachFiles
                    ? 'Attach files'
                    : 'You cannot attach files in this channel'}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="m9.5 12.5 5.8-5.8a3 3 0 1 1 4.2 4.3l-8.2 8.1a5 5 0 0 1-7.1-7L12 4.3" />
                  </svg>
                </button>
                {#if nativeThreadComposer}
                  <CommandOptionComposer
                    commandName="thread"
                    options={nativeThreadOptions}
                    values={commandOptionValues}
                    disabled={busy}
                    onValueChange={(name, value) =>
                      (commandOptionValues = { ...commandOptionValues, [name]: value })}
                    onCancel={cancelCommandComposer}
                  />
                {:else if selectedApplicationCommand}
                  <CommandOptionComposer
                    commandName={selectedApplicationCommand.name}
                    applicationName={selectedApplicationCommand.application_name}
                    options={commandStringOptions(selectedApplicationCommand)}
                    values={commandOptionValues}
                    disabled={busy}
                    onValueChange={(name, value) =>
                      (commandOptionValues = { ...commandOptionValues, [name]: value })}
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
                    aria-controls={completionOpen ? 'guild-message-suggestions' : undefined}
                    aria-activedescendant={completionOpen
                      ? `guild-message-suggestions-option-${completionActive}`
                      : undefined}
                    aria-label={`Message ${channel?.name ?? 'channel'}`}
                    placeholder={canSendMessages
                      ? `Message #${channel?.name ?? 'channel'}`
                      : 'Use /thread to create a thread'}
                    rows="1"
                    maxlength="4000"
                  ></textarea>
                {/if}
                {#if (gifPickerEnabled || gifConfigurationError) && !editingMessage && !nativeThreadComposer && !selectedApplicationCommand}
                  <button
                    class="gif-button"
                    class:active={gifPickerOpen}
                    type="button"
                    disabled={busy ||
                      !channelReady ||
                      !channel ||
                      !canSendMessages ||
                      !gifPickerEnabled}
                    aria-label={gifPickerEnabled
                      ? 'Choose a GIF'
                      : 'GIF availability could not be checked'}
                    title={gifPickerEnabled ? 'Choose a GIF' : gifConfigurationError}
                    aria-expanded={gifPickerOpen}
                    onclick={() => {
                      gifPickerOpen = !gifPickerOpen;
                      emojiPickerOpen = false;
                    }}>GIF</button
                  >
                {/if}
                {#if !editingMessage && !nativeThreadComposer && !selectedApplicationCommand}
                  <button
                    class="emoji-button"
                    class:active={emojiPickerOpen}
                    type="button"
                    disabled={busy || !channelReady || !channel || !canSendMessages}
                    aria-label="Choose an emoji or sticker"
                    title="Emoji and stickers"
                    aria-expanded={emojiPickerOpen}
                    onclick={() => {
                      emojiPickerOpen = !emojiPickerOpen;
                      gifPickerOpen = false;
                    }}>☺</button
                  >
                {/if}
                {#if nativeThreadComposer || selectedApplicationCommand}
                  <small class="composer-count"></small>
                {:else if slowmodeRemaining > 0}
                  <small class="slowmode-indicator" role="status" title="Slow mode is active"
                    >⏱ {slowmodeRemaining}s</small
                  >
                {:else}
                  <small class="composer-count">{content.length}/4000</small>
                {/if}
                <button
                  class="send-button"
                  disabled={busy ||
                    (slowmodeRemaining > 0 &&
                      !nativeThreadComposer &&
                      !selectedApplicationCommand &&
                      !/^\/thread(?:\s|$)/i.test(content.trim())) ||
                    !channelReady ||
                    !channel ||
                    (!canSendMessages &&
                      !nativeThreadComposer &&
                      !selectedApplicationCommand &&
                      !/^\/thread(?:\s|$)/i.test(content.trim())) ||
                    uploads.some((item) => item.status === 'uploading') ||
                    (nativeThreadComposer
                      ? !commandOptionValues.name?.trim() || !commandOptionValues.message?.trim()
                      : selectedApplicationCommand
                        ? !commandOptionsComplete(selectedApplicationCommand, commandOptionValues)
                        : editingMessage
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
              {#if commandNotice}<p class="composer-feature-warning" role="status">
                  <span>{commandNotice}</span>
                </p>{/if}
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
                  stickers={pickerStickers}
                  onSelect={chooseEmoji}
                  onStickerSelect={chooseSticker}
                  onClose={() => (emojiPickerOpen = false)}
                />
              {/if}
              {#if uploads.length && !editingMessage}
                <UploadPreviewTray {uploads} onRemove={removeUpload} />
              {/if}
            {:else}
              <div class="composer composer-disabled" role="note">
                <Icon name="lock" size={18} />
                <span
                  >{threadRequiresE2EEActivation(channel)
                    ? 'End-to-end encryption must finish activating before replies can be sent.'
                    : channel?.archived
                      ? 'This thread is archived.'
                      : channel?.locked
                        ? 'This post has been locked. Only moderators can send messages.'
                        : 'You do not have permission to send messages in this channel.'}</span
                >
              </div>
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
        </div>
      </div>
    {/if}
  </section>
  {#if memberRosterOpen}
    <GuildMemberRoster
      {members}
      roles={guild?.roles ?? []}
      {presenceFor}
      onProfile={openProfile}
      onClose={toggleMemberRoster}
    />
  {/if}
</main>

{#if threadCreateSource}
  <CreateThreadDialog
    message={threadCreateSource}
    busy={threadCreateBusy}
    error={threadCreateError}
    onCreate={createMessageThread}
    onClose={() => {
      if (!threadCreateBusy) threadCreateSource = null;
    }}
  />
{/if}

{#if profile}
  <UserProfileCard
    user={profile.user}
    presence={presenceFor(profile.user)}
    x={profile.x}
    y={profile.y}
    isSelf={Boolean(currentUser && entityKey(currentUser) === entityKey(profile.user))}
    onClose={() => (profile = null)}
    onMessage={profile.user.profile_resolved === false ? undefined : messageUser}
    moderationActions={moderationActionsFor(profile.user)}
    onModerate={requestModeration}
    roles={guild?.roles ?? []}
    roleIds={memberFor(profile.user.id, profile.user.origin_domain)?.role_ids ?? []}
    manageableRoles={manageableRolesFor(profile.user)}
    onRoleChange={changeMemberRole}
  />
{/if}

{#if moderationDialog}
  <div
    use:portal
    class="channel-dialog-layer"
    role="presentation"
    oncontextmenu={(event) => {
      event.preventDefault();
      event.stopPropagation();
    }}
  >
    <button
      class="channel-dialog-backdrop"
      type="button"
      aria-label="Cancel moderation action"
      onclick={cancelModerationDialog}
    ></button>
    <div
      class="channel-dialog confirmation-dialog"
      role="alertdialog"
      tabindex="-1"
      aria-modal="true"
      aria-labelledby="moderation-dialog-title"
      aria-busy={moderationBusy}
      onkeydown={moderationDialogKeydown}
    >
      <header>
        <div>
          <p>Member moderation</p>
          <h2 id="moderation-dialog-title">
            {moderationDialog.action === 'timeout'
              ? 'Timeout'
              : moderationDialog.action === 'kick'
                ? 'Kick'
                : 'Ban'}
            {userDisplayName(moderationDialog.user)}?
          </h2>
        </div>
      </header>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void confirmModeration();
        }}
      >
        {#if moderationDialog.action !== 'kick'}
          <label class="channel-dialog-field">
            Duration
            <select bind:value={moderationDuration} disabled={moderationBusy}>
              {#if moderationDialog.action === 'timeout'}
                <option value="3600">1 hour</option>
                <option value="86400">1 day</option>
                <option value="604800">7 days</option>
                <option value="2419200">28 days</option>
              {:else}
                <option value="86400">1 day</option>
                <option value="604800">7 days</option>
                <option value="2592000">30 days</option>
              {/if}
              <option value="permanent">Permanent</option>
            </select>
          </label>
        {/if}
        <label class="channel-dialog-field">
          Reason <span class="field-optional">Optional</span>
          <textarea bind:value={moderationReason} maxlength="512" rows="3" disabled={moderationBusy}
          ></textarea>
        </label>
        {#if moderationError}<p class="form-error" role="alert">{moderationError}</p>{/if}
        <footer>
          <button class="secondary-button" type="button" onclick={cancelModerationDialog}
            >Cancel</button
          >
          <button class="danger-button" type="submit" disabled={moderationBusy}>
            {moderationBusy ? 'Applying…' : 'Confirm'}
          </button>
        </footer>
      </form>
    </div>
  </div>
{/if}

{#if voiceMemberMenu}
  <div
    use:portal
    bind:this={voiceMemberMenuElement}
    class="channel-context-menu voice-member-context-menu"
    role="menu"
    tabindex="-1"
    aria-label={`Voice actions for ${userDisplayName(voiceMemberMenu.user)}`}
    oncontextmenu={(event) => {
      event.preventDefault();
      event.stopPropagation();
    }}
  >
    <button
      type="button"
      role="menuitem"
      tabindex="-1"
      onclick={() => viewVoiceMemberProfile(voiceMemberMenu!)}
    >
      <span>View profile</span>
    </button>
    {#if canMoveVoiceMember(voiceMemberMenu.user, voiceMemberMenu.source)}
      <button
        class="danger-item menu-separator"
        type="button"
        role="menuitem"
        tabindex="-1"
        disabled={voiceModerationBusy}
        onclick={() => void disconnectVoiceMember(voiceMemberMenu!)}
      >
        <span>Disconnect from voice</span>
      </button>
    {/if}
  </div>
{/if}

{#if channelMenu}
  <div
    use:portal
    bind:this={channelMenuElement}
    id="channel-context-menu"
    class="channel-context-menu"
    role="menu"
    tabindex="-1"
    aria-label={channelMenu.channel ? 'Channel actions' : 'Channel list actions'}
    oncontextmenu={(event) => {
      event.preventDefault();
      event.stopPropagation();
    }}
    aria-busy={reorderingChannels}
  >
    {#if channelMenu.channel}
      {@const target = channelMenu.channel}
      {@const canEditChannel =
        canManageChannels || channelHasPermission(target, Permission.MANAGE_CHANNELS)}
      {@const canEditPermissions = channelHasPermission(target, Permission.MANAGE_ROLES)}
      {#if target.type !== 4 && guild}
        <a
          role="menuitem"
          tabindex="-1"
          href={guildChannelPath(guild, target)}
          onclick={() => closeChannelMenu(false)}
        >
          <span>Open channel</span>
        </a>
      {/if}
      {#if target.type !== 4 && target.last_message_id}
        <button type="button" role="menuitem" tabindex="-1" onclick={() => markChannelRead(target)}>
          <span>Mark as read</span>
        </button>
      {/if}
      {#if canEditChannel && target.type === 4}
        <button
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => openChannelDialog(0, target, null, event.currentTarget)}
        >
          <span>Create channel</span>
        </button>
      {/if}
      {#if guild && (canEditChannel || canEditPermissions)}
        <a
          class="menu-separator"
          role="menuitem"
          tabindex="-1"
          href={channelSettingsPath(guild, target, canEditChannel ? 'overview' : 'permissions')}
          onclick={() => closeChannelMenu(false)}
        >
          <span>Edit {target.type === 4 ? 'category' : 'channel'}</span>
        </a>
      {/if}
      {#if canEditChannel}
        <button
          type="button"
          role="menuitem"
          tabindex="-1"
          disabled={!canMoveChannel(target, -1)}
          onclick={() => moveChannelByStep(target, -1)}
        >
          <span>Move up</span>
        </button>
        <button
          type="button"
          role="menuitem"
          tabindex="-1"
          disabled={!canMoveChannel(target, 1)}
          onclick={() => moveChannelByStep(target, 1)}
        >
          <span>Move down</span>
        </button>
        <button
          class="danger-item"
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={() => requestChannelDeletion(target)}
        >
          <span>Delete {target.type === 4 ? 'category' : 'channel'}</span>
        </button>
      {/if}
      {#if target.type !== 4 && guild}
        <button
          class="menu-separator"
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => copyChannelValue(absoluteChannelLink(target), event)}
        >
          <span>Copy channel link</span>
        </button>
      {/if}
      {#if developerMode.enabled}
        <button
          class:menu-separator={target.type === 4}
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => copyChannelValue(entityRef(target), event)}
        >
          <span>Copy {target.type === 4 ? 'category' : 'channel'} ID</span>
        </button>
      {/if}
    {:else if canManageChannels}
      <button
        type="button"
        role="menuitem"
        tabindex="-1"
        onclick={(event) => openChannelDialog(0, null, null, event.currentTarget)}
      >
        <span>Create channel</span>
      </button>
      <button
        type="button"
        role="menuitem"
        tabindex="-1"
        onclick={(event) => openChannelDialog(4, null, null, event.currentTarget)}
      >
        <span>Create category</span>
      </button>
    {/if}
  </div>
{/if}

{#if inviteDialogOpen}
  <div class="channel-dialog-layer">
    <button
      class="channel-dialog-backdrop"
      type="button"
      aria-label="Close invite dialog"
      onclick={() => closeQuickInvite()}
    ></button>
    <div
      bind:this={inviteDialogElement}
      class="channel-dialog quick-invite-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="quick-invite-title"
      aria-busy={inviteDialogBusy}
    >
      <header>
        <div>
          <p>Invite people</p>
          <h2 id="quick-invite-title">Invite people to {guild?.name}</h2>
        </div>
        <button
          bind:this={inviteDialogClose}
          type="button"
          aria-label="Close"
          onclick={() => closeQuickInvite()}>×</button
        >
      </header>
      <div class="quick-invite-content">
        <p>
          This link opens <strong>#{channel?.name}</strong> after the person joins. It expires in 24 hours.
        </p>
        {#if inviteDialogBusy}
          <p class="quick-invite-status" role="status">Creating a secure invite…</p>
        {:else if inviteLink}
          <label class="channel-dialog-field">
            Invite link
            <div class="quick-invite-link">
              <input
                value={inviteLink}
                readonly
                onclick={(event) => event.currentTarget.select()}
              />
              <button class="primary-button" type="button" onclick={() => void copyQuickInvite()}>
                <Icon name="copy" size={16} />Copy
              </button>
            </div>
          </label>
        {/if}
        {#if inviteDialogError}<p class="form-error" role="alert">{inviteDialogError}</p>{/if}
      </div>
    </div>
  </div>
{/if}

{#if channelDeleteTarget}
  <div class="channel-dialog-layer">
    <button
      class="channel-dialog-backdrop"
      type="button"
      aria-label="Cancel channel deletion"
      disabled={channelDeleteBusy}
      onclick={() => closeChannelDeleteDialog()}
    ></button>
    <div
      bind:this={channelDeleteDialog}
      class="channel-dialog confirmation-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="channel-delete-title"
      aria-describedby="channel-delete-description"
      aria-busy={channelDeleteBusy}
    >
      <header>
        <div>
          <p>Permanent action</p>
          <h2 id="channel-delete-title">
            Delete {channelDeleteTarget.type === 4 ? 'category' : 'channel'}?
          </h2>
        </div>
      </header>
      <div class="confirmation-copy">
        <p id="channel-delete-description">
          <strong>“{channelDeleteTarget.name ?? 'Untitled'}”</strong> will be permanently removed.
          {channelDeleteTarget.type === 4
            ? ' The category must be empty before it can be deleted.'
            : ' A channel containing messages cannot be deleted.'}
        </p>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
      </div>
      <footer>
        <button
          bind:this={channelDeleteCancel}
          class="secondary-button"
          type="button"
          disabled={channelDeleteBusy}
          onclick={() => closeChannelDeleteDialog()}>Cancel</button
        >
        <button
          class="danger-button"
          type="button"
          disabled={channelDeleteBusy}
          onclick={() => void removeChannel(channelDeleteTarget!)}
        >
          {channelDeleteBusy
            ? 'Deleting…'
            : `Delete ${channelDeleteTarget.type === 4 ? 'category' : 'channel'}`}
        </button>
      </footer>
    </div>
  </div>
{/if}

{#if channelDialogOpen}
  <div class="channel-dialog-layer">
    <button
      class="channel-dialog-backdrop"
      type="button"
      aria-label="Close channel dialog"
      onclick={() => closeChannelDialog()}
    ></button>
    <div
      bind:this={channelDialogElement}
      class="channel-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="channel-dialog-title"
      aria-busy={channelDialogBusy}
    >
      <header>
        <div>
          <p>{channelDialogTarget ? 'Edit' : 'Create'}</p>
          <h2 id="channel-dialog-title">
            {channelDialogTarget
              ? channelDialogTarget.type === 4
                ? 'Edit category'
                : 'Edit channel'
              : channelDialogType === 4
                ? 'Create category'
                : 'Create channel'}
          </h2>
        </div>
        <button
          type="button"
          aria-label="Close"
          disabled={channelDialogBusy}
          onclick={() => closeChannelDialog()}>×</button
        >
      </header>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void saveChannelDialog();
        }}
      >
        {#if !channelDialogTarget}
          <fieldset>
            <legend>Channel type</legend>
            <label>
              <input type="radio" bind:group={channelDialogType} value={0} />
              <span><strong>Text</strong><small>Messages, images, and files</small></span>
            </label>
            <label>
              <input type="radio" bind:group={channelDialogType} value={2} />
              <span><strong>Voice</strong><small>Voice and video conversations</small></span>
            </label>
            <label>
              <input type="radio" bind:group={channelDialogType} value={4} />
              <span><strong>Category</strong><small>Organize related channels</small></span>
            </label>
            <label>
              <input type="radio" bind:group={channelDialogType} value={5} />
              <span><strong>Announcement</strong><small>Broadcast important updates</small></span>
            </label>
            <label>
              <input type="radio" bind:group={channelDialogType} value={15} />
              <span><strong>Forum</strong><small>Organized posts and discussions</small></span>
            </label>
          </fieldset>
        {/if}
        <label class="channel-dialog-field">
          {channelDialogType === 4 ? 'Category name' : 'Channel name'}
          <input
            bind:this={channelDialogInput}
            bind:value={channelDialogName}
            minlength="1"
            maxlength="100"
            autocomplete="off"
            required
          />
        </label>
        {#if channelDialogType !== 4}
          <label class="channel-dialog-field">
            Category
            <select bind:value={channelDialogParent}>
              <option value="">No category</option>
              {#each (guild?.channels ?? []).filter((item) => item.type === 4) as category (entityKey(category))}
                <option value={entityKey(category)}>{category.name}</option>
              {/each}
            </select>
          </label>
        {/if}
        {#if channelDialogError}
          <p class="form-error" role="alert">{channelDialogError}</p>
        {/if}
        <footer>
          <button
            class="quiet-button"
            type="button"
            disabled={channelDialogBusy}
            onclick={() => closeChannelDialog()}>Cancel</button
          >
          <button class="primary-button" disabled={channelDialogBusy || !channelDialogName.trim()}>
            {channelDialogBusy
              ? 'Saving…'
              : channelDialogTarget
                ? 'Save changes'
                : channelDialogType === 4
                  ? 'Create category'
                  : 'Create channel'}
          </button>
        </footer>
      </form>
    </div>
  </div>
{/if}
