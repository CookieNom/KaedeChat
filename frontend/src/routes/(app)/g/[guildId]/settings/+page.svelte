<script lang="ts">
  import GuildOnboarding from '$lib/components/GuildOnboarding.svelte';
  import ForumEmojiField from '$lib/components/ForumEmojiField.svelte';
  import { customEmojiToken } from '$lib/chat/emojis';
  import ColorPicker from '$lib/components/ColorPicker.svelte';
  import { t } from '$lib/ui/locale';

  import { trapDialogFocus } from '$lib/ui/focus';
  import { cancelableDelay } from '$lib/ui/delay';
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import { api, ApiError, userErrorMessage } from '$lib/api/client';
  import { loadAuthConfiguration } from '$lib/auth/config';
  import { firstNavigableChannel, groupChannels } from '$lib/chat/channels';
  import { canReadAnnouncementChannel } from '$lib/chat/announcements';
  import {
    canAccessGuildExpressionSettings,
    canEditGuildExpression,
    guildOwnerRef,
    hasGuildPermissionOrOwnership,
    isQualifiedGuildOwner
  } from '$lib/chat/guild-admin';
  import {
    channelInviteListPath,
    guildInviteManagementPath,
    guildInviteUrl
  } from '$lib/chat/invites';
  import { hasAllPermissions, reconcileChannelPermissionProjection } from '$lib/chat/permissions';
  import { guildMemberOutranks, guildRoleOutranks } from '$lib/chat/moderation';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import {
    commitGuildWebhookAvatar,
    createGuildWebhook,
    createGuildWebhookAvatarTicket,
    deleteGuildWebhook,
    deleteGuildWebhookAvatar,
    canManageWebhookChannel,
    listChannelWebhooks,
    listGuildWebhooks,
    manageableWebhookChannels,
    rotateGuildWebhook,
    updateGuildWebhook,
    type WebhookSummary
  } from '$lib/chat/webhooks';
  import { forumDefaultReactionPayload } from '$lib/chat/threads';
  import {
    listScheduledEvents,
    scheduledEventRef,
    type ScheduledEvent
  } from '$lib/chat/scheduled-events';
  import type {
    Channel,
    CustomEmoji,
    GuildSticker,
    ForumTag,
    Guild,
    GuildMemberSummary,
    Role,
    UserSummary
  } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import Icon from '$lib/components/Icon.svelte';
  import GuildAuditLog from '$lib/components/GuildAuditLog.svelte';
  import GuildMemberPicker from '$lib/components/GuildMemberPicker.svelte';
  import GuildMemberManagement from '$lib/components/GuildMemberManagement.svelte';
  import GuildSafetyTools from '$lib/components/GuildSafetyTools.svelte';
  import AnnouncementFollowers from '$lib/components/AnnouncementFollowers.svelte';
  import ImageUploadField from '$lib/components/ImageUploadField.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import { initializeE2EE } from '$lib/e2ee/client';
  import { acknowledgeEncryptedRoom } from '$lib/e2ee/disclosures';
  import { PERMISSION_METADATA, Permission } from '$lib/generated/permissions';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import { assetUrl } from '$lib/media/assets';
  import { completeScannedMediaResource } from '$lib/media/scanned';
  import { moveCrop, resizeCrop, type CropCorner, type NormalizedCrop } from '$lib/media/crop';
  import { chatEntities as entities } from '$lib/stores/entities.svelte';
  import { TRACKER_CHANNEL_TYPE } from '$lib/task-tracker/types';
  import {
    browserNotifications,
    type GuildNotificationLevel,
    type GuildNotificationPreference
  } from '$lib/notifications/browser.svelte';
  import {
    guildChannelPath,
    guildApplicationDirectoryPath,
    guildIntegrationsPath,
    type ChannelSettingsPanel
  } from '$lib/navigation/routes';
  import { formatDateTime } from '$lib/ui/locale';
  import { portal } from '$lib/ui/portal';
  import { onDestroy, tick, untrack } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';

  interface GuildView extends Guild {
    banner_hash: string | null;
  }

  interface InviteSummary {
    code: string;
    channel_id: string | null;
    uses?: number;
    max_uses?: number | null;
    expires_at: string | null;
    created_at?: string;
    temporary?: boolean;
    reusable?: boolean;
    target_type: 'stream' | null;
    target_user_id: string | null;
    scheduled_event_id: string | null;
    role_ids: string[];
    target_user_count: number;
  }

  interface MemberSummary extends GuildMemberSummary {
    joined_at?: string;
    timeout_until?: string | null;
    timeout_indefinite?: boolean;
  }

  interface BanSummary {
    user: UserSummary;
    reason: string | null;
    created_at: string;
    expires_at: string | null;
  }

  interface InstanceBanSummary {
    instance_domain: string;
    reason: string | null;
    created_at: string;
    expires_at: string | null;
  }

  type MemberModerationAction = 'timeout' | 'untimeout' | 'kick' | 'ban';

  interface MemberModerationDialog {
    action: MemberModerationAction;
    member: MemberSummary;
  }

  interface ChannelOverwrite {
    target_id: string;
    target_domain: string;
    target_type: 'role' | 'member';
    allow: string;
    deny: string;
  }

  interface EditableForumTag extends Omit<ForumTag, 'id'> {
    id?: string;
  }

  interface VoiceRegion {
    id: string;
    name: string;
    optimal: boolean;
    deprecated: boolean;
    custom: boolean;
  }

  type DestructiveConfirmation =
    | {
        kind: 'channel';
        target: Channel;
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'role';
        target: Role;
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'invite';
        target: InviteSummary;
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'instance-ban';
        domain: string;
        reason: string;
        expiresAt: string | null;
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'guild-leave';
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'guild-transfer';
        target: UserSummary;
        title: string;
        description: string;
        confirmLabel: string;
      }
    | {
        kind: 'guild-delete';
        verificationText: string;
        title: string;
        description: string;
        confirmLabel: string;
      };

  type GuildAssetKind = 'icon' | 'banner';
  type GuildAssetStage = 'uploading' | 'scanning';

  const MEMBER_PAGE_SIZE = 25;
  const acceptedImageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  function validStickerName(value: string): boolean {
    const length = [...value.trim()].length;
    const hasControlCharacter = [...value].some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127;
    });
    return length >= 2 && length <= 30 && !hasControlCharacter;
  }

  function validStickerDescription(value: string): boolean {
    const length = [...value.trim()].length;
    return length === 0 || (length >= 2 && length <= 100);
  }

  const channelOnly = $derived(Boolean(page.params.channelId));
  const guildId = $derived(page.params.guildId ?? '');
  const channelId = $derived(channelOnly ? (page.params.channelId ?? '') : '');
  let localDomain = $state('');
  let currentUserRef = $state('');
  let signedInUser = $state<UserSummary | null>(null);
  let e2eeActivationEnabled = $state(false);
  let guild = $state<GuildView | null>(null);
  let members = $state<MemberSummary[]>([]);
  let membersHaveMore = $state(false);
  let membersLoadingMore = $state(false);
  let roleMemberSearch = $state('');
  let roleMemberSearchResults = $state<MemberSummary[]>([]);
  let roleMemberSearchBusy = $state(false);
  let roleMemberSearchError = $state('');
  let bans = $state<BanSummary[]>([]);
  let instanceBans = $state<InstanceBanSummary[]>([]);
  let moderationReason = $state('');
  let timeoutDuration = $state('3600');
  let banDuration = $state('permanent');
  let banDeleteSeconds = $state('0');
  let memberModerationDialog = $state<MemberModerationDialog | null>(null);
  let memberModerationBusy = $state(false);
  let memberModerationElement = $state<HTMLElement | null>(null);
  let memberModerationCancel = $state<HTMLButtonElement | null>(null);
  let memberModerationPreviousFocus: HTMLElement | null = null;
  let memberModerationController: AbortController | null = null;
  let memberModerationGeneration = 0;
  let instanceBanDomain = $state('');
  let instanceBanReason = $state('');
  let instanceBanDuration = $state('permanent');
  let invites = $state<InviteSummary[]>([]);
  let scheduledEvents = $state<ScheduledEvent[]>([]);
  let webhooks = $state<WebhookSummary[]>([]);
  let newWebhookName = $state('');
  let webhookNameDrafts = $state<Record<string, string>>({});
  let webhookChannelDrafts = $state<Record<string, string>>({});
  let revealedWebhookToken = $state('');
  let webhookProjectionReady = false;
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let loadGeneration = 0;
  let routeController: AbortController | null = null;
  let observedGuildProjectionRef = '';
  let guildAssetKind = $state<GuildAssetKind | null>(null);
  let guildAssetStage = $state<GuildAssetStage | null>(null);
  let guildAssetProgress = $state(0);
  let guildAssetError = $state('');
  let emojiName = $state('');
  let emojiFile = $state<File | null>(null);
  let emojiInput = $state<HTMLInputElement | null>(null);
  let emojiBusy = $state(false);
  let emojiDrafts = $state<Record<string, { name: string; roles: string[] }>>({});
  let stickerName = $state('');
  let stickerDescription = $state('');
  let stickerFile = $state<File | null>(null);
  let stickerInput = $state<HTMLInputElement | null>(null);
  let stickerBusy = $state(false);
  let stickerDrafts = $state<Record<string, { name: string; description: string; tags: string }>>(
    {}
  );
  let stickerPreviewUrl = $state('');
  let stickerCropX = $state(0);
  let stickerCropY = $state(0);
  let stickerCropWidth = $state(1);
  let stickerCropHeight = $state(1);
  let stickerImageAspect = $state(1);
  let stickerRemoveBackground = $state(false);
  let stickerCropGesture: {
    pointerId: number;
    mode: 'move' | CropCorner;
    clientX: number;
    clientY: number;
  } | null = null;

  onDestroy(() => {
    if (stickerPreviewUrl) URL.revokeObjectURL(stickerPreviewUrl);
  });

  let name = $state('');
  let description = $state('');
  let guildHistoryPolicy = $state<'disabled' | 'full_retained'>('disabled');
  let guildNotificationLevel = $state<GuildNotificationLevel>('mentions');

  let selectedChannel = $state<Channel | null>(null);
  let channelName = $state('');
  let channelTopic = $state('');
  let channelNsfw = $state(false);
  let channelParent = $state('');
  let channelSlowmode = $state(0);
  let channelBitrate = $state(64000);
  let channelUserLimit = $state(0);
  let channelRtcRegion = $state('');
  let channelVoiceRegions = $state<VoiceRegion[]>([]);
  let channelVoiceRegionsError = $state('');
  let channelHistoryPolicy = $state<'inherit' | 'disabled' | 'full_retained'>('inherit');
  let channelForumTags = $state<EditableForumTag[]>([]);
  let newForumTagName = $state('');
  let channelForumSort = $state<0 | 1>(0);
  let channelForumLayout = $state<0 | 1 | 2>(0);
  let channelForumArchive = $state<60 | 1440 | 4320 | 10080>(1440);
  let channelForumSlowmode = $state(0);
  const forumEmojis = $derived(
    (guild?.emojis ?? [])
      .filter((emoji) => emoji.media_hash && emoji.available !== false)
      .map((emoji) => ({
        ...emoji,
        guild_name: guild?.name,
        url: assetUrl(emoji.media_hash ?? '', 'thumbnail_128', emoji.origin_domain),
        value: customEmojiToken(emoji)
      }))
      .filter((emoji) => Boolean(emoji.url && emoji.value))
  );
  let channelForumReaction = $state('');
  let channelForumReactionId = $state<string | null>(null);
  let channelForumE2EE = $state(false);
  let channelForumRequireTag = $state(false);
  let newChannelName = $state('');
  let newChannelType = $state(0);
  let newChannelParent = $state('');
  let newChannelTrackerPrefix = $state('');
  let channelOverwrites = $state<ChannelOverwrite[]>([]);
  let overwriteTarget = $state('');
  let overwriteAllow = $state('0');
  let overwriteDeny = $state('0');
  let overwriteSearch = $state('');
  let permissionSearch = $state('');
  let channelEditorPanel = $state<ChannelSettingsPanel>('overview');
  let channelSafetyNumber = $state('');

  let selectedRole = $state<Role | null>(null);
  let roleName = $state('');
  let roleColor = $state('#7b7168');
  let rolePermissions = $state('0');
  let roleHoist = $state(false);
  let roleMentionable = $state(false);
  let roleIconFile = $state<File | null>(null);
  let roleIconBusy = $state(false);
  let roleIconError = $state('');
  let newRoleName = $state('');
  let roleEditorTab = $state<'display' | 'permissions' | 'members'>('display');
  let draggedRoleKey = $state<string | null>(null);
  let roleDropKey = $state<string | null>(null);
  let reorderingRoles = $state(false);

  let inviteChannel = $state('');
  let inviteMaxAge = $state('86400');
  let inviteMaxUses = $state('');
  let inviteTemporary = $state(false);
  let inviteUnique = $state(false);
  let inviteTargetType = $state<'' | 'stream'>('');
  let inviteTargetUser = $state('');
  let ownershipTargetUser = $state<UserSummary | null>(null);
  let inviteScheduledEvent = $state('');
  let inviteRoleIds = $state<string[]>([]);
  let createdInvite = $state<InviteSummary | null>(null);
  let destructiveConfirmation = $state<DestructiveConfirmation | null>(null);
  let confirmationDialog = $state<HTMLElement | null>(null);
  let confirmationCancelButton = $state<HTMLButtonElement | null>(null);
  let confirmationPreviousFocus: HTMLElement | null = null;
  let confirmationVerification = $state('');
  let ownershipTarget = $state('');

  const permissionGroups = [...new Set(PERMISSION_METADATA.map((item) => item.group))].map(
    (group) => ({
      name: group,
      permissions: PERMISSION_METADATA.filter((item) => item.group === group).map(
        (item) => [item.label, item.description, item.bit, item] as const
      )
    })
  );
  const permissionLabels = new Map<string, string>(
    PERMISSION_METADATA.map((item) => [item.permission, item.label])
  );

  function permissionDependencies(dependencies: readonly string[]): string {
    return dependencies.map((name) => permissionLabels.get(name) ?? name).join(', ');
  }
  const filteredPermissionGroups = $derived(
    permissionGroups
      .map((group) => ({
        ...group,
        permissions: group.permissions.filter((permission) => {
          const query = permissionSearch.trim().toLowerCase();
          return !query || `${permission[0]} ${permission[1]}`.toLowerCase().includes(query);
        })
      }))
      .filter((group) => group.permissions.length)
  );
  const channelPermissionGroups = $derived(
    filteredPermissionGroups
      .map((group) => ({
        ...group,
        permissions: group.permissions.filter((permission) => {
          const metadata = permission[3];
          return (
            (metadata.resourceScopes as readonly string[]).includes('channel') &&
            (!metadata.channelTypes.length ||
              (selectedChannel
                ? (metadata.channelTypes as readonly number[]).includes(selectedChannel.type)
                : false))
          );
        })
      }))
      .filter((group) => group.permissions.length)
  );
  const filteredRoles = $derived(
    (guild?.roles ?? []).filter((role) =>
      role.name.toLowerCase().includes(overwriteSearch.trim().toLowerCase())
    )
  );
  const filteredMembers = $derived(
    liveMemberRows(members, true).filter((member) =>
      `${member.nickname ?? ''} ${userDisplayName(member.user)} ${userPublicHandle(member.user) ?? ''}`
        .toLowerCase()
        .includes(overwriteSearch.trim().toLowerCase())
    )
  );

  const channelGroups = $derived(groupChannels(guild?.channels ?? []));
  const isLocalGuild = $derived(
    Boolean(guild && localDomain && guild.origin_domain.toLowerCase() === localDomain.toLowerCase())
  );
  const effectivePermissions = $derived.by(() => {
    try {
      return BigInt(guild?.permissions ?? '0');
    } catch {
      return 0n;
    }
  });
  const normalizedGuildPermissionProjection = $derived(
    guild ? (entities.guilds.get(entityKey(guild)) ?? null) : null
  );

  function revokeGuildSettingsAccess() {
    routeController?.abort();
    routeController = null;
    loadGeneration += 1;
    memberModerationGeneration += 1;
    memberModerationController?.abort();
    memberModerationController = null;
    guild = null;
    members = [];
    membersHaveMore = false;
    roleMemberSearch = '';
    roleMemberSearchResults = [];
    roleMemberSearchError = '';
    bans = [];
    instanceBans = [];
    invites = [];
    scheduledEvents = [];
    webhooks = [];
    webhookNameDrafts = {};
    webhookChannelDrafts = {};
    revealedWebhookToken = '';
    emojiDrafts = {};
    stickerDrafts = {};
    name = '';
    description = '';
    selectedChannel = null;
    channelName = '';
    channelTopic = '';
    channelParent = '';
    channelOverwrites = [];
    overwriteTarget = '';
    overwriteAllow = '0';
    overwriteDeny = '0';
    channelSafetyNumber = '';
    selectedRole = null;
    roleName = '';
    rolePermissions = '0';
    createdInvite = null;
    inviteTargetUser = '';
    ownershipTargetUser = null;
    inviteRoleIds = [];
    ownershipTarget = '';
    memberModerationDialog = null;
    memberModerationBusy = false;
    memberModerationElement = null;
    memberModerationCancel = null;
    memberModerationPreviousFocus = null;
    destructiveConfirmation = null;
    confirmationDialog = null;
    confirmationCancelButton = null;
    confirmationPreviousFocus = null;
    webhookProjectionReady = false;
    busy = false;
    loading = false;
    error = $t('ui_this_guild_is_unavailable_or_you_no_longer_ha_70ba0e65');
    notice = '';
  }

  $effect(() => {
    const current = guild;
    const projection = normalizedGuildPermissionProjection;
    if (!current) return;
    const currentRef = entityKey(current);
    if (!projection) {
      if (observedGuildProjectionRef === currentRef) {
        observedGuildProjectionRef = '';
        untrack(revokeGuildSettingsAccess);
      }
      return;
    }
    observedGuildProjectionRef = currentRef;
    const roles = projection.roles ?? current.roles;
    const projectionChannels = projection.channels;
    const channels = projectionChannels
      ? reconcileChannelPermissionProjection(current.channels, projectionChannels)
      : (current.channels ?? []);
    const changed =
      current.permissions !== projection.permissions ||
      current.actor_highest_role_id !== projection.actor_highest_role_id ||
      current.permission_generation !== projection.permission_generation ||
      roles?.length !== current.roles?.length ||
      roles?.some((item, index) => item !== current.roles?.[index]) ||
      channels.length !== (current.channels?.length ?? 0) ||
      channels.some((item, index) => item !== current.channels?.[index]);
    if (!changed) return;
    untrack(() => {
      guild = {
        ...current,
        permissions: projection.permissions,
        actor_highest_role_id: projection.actor_highest_role_id,
        permission_generation: projection.permission_generation,
        roles,
        channels
      };
      if (selectedChannel) {
        const projected = channels.find((item) => entityKey(item) === entityKey(selectedChannel!));
        selectedChannel = projected
          ? { ...selectedChannel, permissions: projected.permissions }
          : null;
      }
      if (selectedRole) {
        selectedRole = roles?.find((role) => entityKey(role) === entityKey(selectedRole!)) ?? null;
      }
    });
  });

  const isGuildOwner = $derived(isQualifiedGuildOwner(guild, currentUserRef));

  function hasPermission(permission: bigint): boolean {
    return hasGuildPermissionOrOwnership(
      effectivePermissions,
      permission,
      currentUserRef,
      guild ? guildOwnerRef(guild) : null
    );
  }

  const canManageGuild = $derived(hasPermission(Permission.MANAGE_GUILD));
  const canManageGuildAssets = $derived(hasPermission(Permission.MANAGE_GUILD));
  function latestMember(member: MemberSummary): MemberSummary {
    const user = entities.users.get(entityKey(member.user));
    return user ? { ...member, user: { ...member.user, ...user } } : member;
  }

  function cacheMemberRows(rows: MemberSummary[]): MemberSummary[] {
    entities.members.upsertMany(rows);
    return rows;
  }

  function removeCachedMember(member: MemberSummary) {
    entities.members.remove(`${member.guild_id}@${member.guild_domain}:${entityKey(member.user)}`);
  }

  function liveMemberRows(rows: MemberSummary[], includeUnlisted = false): MemberSummary[] {
    if (!guild) return [];
    const live = entities.members.values.filter(
      (member) => member.guild_id === guild?.id && member.guild_domain === guild?.origin_domain
    );
    const liveByUser = new Map(live.map((member) => [entityKey(member.user), member]));
    const rowKeys = new Set(rows.map((member) => entityKey(member.user)));
    return [
      ...rows.flatMap((member) => {
        const projected = liveByUser.get(entityKey(member.user));
        return projected ? [latestMember(projected)] : [];
      }),
      ...(includeUnlisted
        ? live.filter((member) => !rowKeys.has(entityKey(member.user))).map(latestMember)
        : [])
    ];
  }

  function initializeExpressionDrafts(target: Guild) {
    emojiDrafts = Object.fromEntries(
      (target.emojis ?? []).map((emoji) => [
        entityKey(emoji),
        {
          name: emoji.name,
          roles: [...(emoji.roles ?? [])]
        }
      ])
    );
    stickerDrafts = Object.fromEntries(
      (target.stickers ?? []).map((sticker) => [
        entityKey(sticker),
        {
          name: sticker.name,
          description: sticker.description ?? '',
          tags: (sticker.tags ?? []).join(', ')
        }
      ])
    );
  }
  const currentMembers = $derived(liveMemberRows(members, true));
  const currentRoleMemberSearchResults = $derived(liveMemberRows(roleMemberSearchResults));
  const ownershipCandidates = $derived(
    currentMembers.filter(
      (member) =>
        member.user.account_type !== 'bot' &&
        member.user.bot !== true &&
        entityRef(member.user) !== currentUserRef
    )
  );
  const canManageChannels = $derived(hasPermission(Permission.MANAGE_CHANNELS));
  const CHANNEL_MOVE_PERMISSIONS = Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS;
  const canManageRoles = $derived(hasPermission(Permission.MANAGE_ROLES));
  const canCreateExpressions = $derived(hasPermission(Permission.CREATE_GUILD_EXPRESSIONS));
  const canAccessExpressions = $derived(
    canAccessGuildExpressionSettings(effectivePermissions, isGuildOwner)
  );
  const actorHighestRole = $derived(
    guild?.roles?.find((role) => role.id === guild?.actor_highest_role_id) ?? null
  );

  function roleRank(role: Role): [number, bigint] {
    return [role.position, -BigInt(role.id)];
  }

  function compareRoleRank(left: Role, right: Role): number {
    const [leftPosition, leftId] = roleRank(left);
    const [rightPosition, rightId] = roleRank(right);
    return leftPosition - rightPosition || (leftId < rightId ? -1 : leftId > rightId ? 1 : 0);
  }

  function canManageRole(role: Role): boolean {
    if (!canManageRoles) return false;
    if (isGuildOwner) return true;
    return Boolean(actorHighestRole && compareRoleRank(actorHighestRole, role) > 0);
  }

  function canManageExpressionRole(role: Role): boolean {
    if (!canAccessExpressions) return false;
    if (isGuildOwner) return true;
    return Boolean(actorHighestRole && compareRoleRank(actorHighestRole, role) > 0);
  }

  function canEditExpression(creatorId?: string, creatorDomain?: string): boolean {
    return (
      isGuildOwner ||
      canEditGuildExpression(effectivePermissions, currentUserRef, creatorId, creatorDomain)
    );
  }

  function canEditEmoji(emoji: CustomEmoji): boolean {
    return canEditExpression(emoji.creator_id, emoji.creator_domain);
  }

  function canEditSticker(sticker: GuildSticker): boolean {
    return canEditExpression(sticker.creator_id, sticker.creator_domain);
  }

  function canEditEmojiRoleRestrictions(emoji: CustomEmoji): boolean {
    if (!canEditEmoji(emoji)) return false;
    return (emoji.roles ?? []).every((reference) => {
      const role = guild?.roles?.find((candidate) => entityRef(candidate) === reference);
      return Boolean(role && canManageExpressionRole(role));
    });
  }

  function canReorderRole(role: Role): boolean {
    return Boolean(guild && role.id !== guild.id && canManageRole(role));
  }

  function canManageMember(member: MemberSummary): boolean {
    if (!guild || !signedInUser || !canManageRoles) return false;
    if (entityRef(member.user) === currentUserRef) return true;
    return guildMemberOutranks(guild, signedInUser, member.user, currentMembers);
  }

  function canManageOverwriteRole(role: Role): boolean {
    return (
      canEditSelectedPermissions && guildRoleOutranks(guild, signedInUser, role, currentMembers)
    );
  }

  function canManageOverwriteMember(member: MemberSummary): boolean {
    return (
      canEditSelectedPermissions &&
      guildMemberOutranks(guild, signedInUser, member.user, currentMembers)
    );
  }

  function canManageOverwriteTarget(value = overwriteTarget): boolean {
    if (!guild || !value || !canEditSelectedPermissions) return false;
    const [targetType, ...refParts] = value.split(':');
    const targetRef = refParts.join(':');
    if (targetType === 'role') {
      const role = guild.roles?.find((candidate) => entityRef(candidate) === targetRef);
      return Boolean(role && canManageOverwriteRole(role));
    }
    if (targetType === 'member') {
      const member = currentMembers.find((candidate) => entityRef(candidate.user) === targetRef);
      return Boolean(member && canManageOverwriteMember(member));
    }
    return false;
  }

  const canManageSelectedRole = $derived(Boolean(selectedRole && canManageRole(selectedRole)));
  const canViewAuditLog = $derived(hasPermission(Permission.VIEW_AUDIT_LOG));
  const canViewMembers = $derived(hasPermission(Permission.VIEW_CHANNEL));
  const canKickMembers = $derived(hasPermission(Permission.KICK_MEMBERS));
  const canBanMembers = $derived(hasPermission(Permission.BAN_MEMBERS));
  const canTimeoutMembers = $derived(hasPermission(Permission.MODERATE_MEMBERS));
  const canBanInstances = $derived(hasPermission(Permission.BAN_INSTANCES));
  const canModerateMembers = $derived(canKickMembers || canBanMembers || canTimeoutMembers);
  const visibleRoleMembers = $derived(
    roleMemberSearch.trim() ? currentRoleMemberSearchResults : currentMembers
  );
  const canCreateInvites = $derived(hasPermission(Permission.CREATE_INVITE));
  const canAccessInvites = $derived(canManageGuild || canCreateInvites);
  const canManageWebhooks = $derived(hasPermission(Permission.MANAGE_WEBHOOKS));
  const canAccessGuildIntegrations = $derived.by(() => {
    const current = guild;
    return (
      canManageGuild ||
      canManageWebhooks ||
      Boolean(
        current && current.channels?.some((channel) => canReadAnnouncementChannel(channel, current))
      )
    );
  });
  const selectedEffectivePermissions = $derived.by(() => {
    try {
      return BigInt(selectedChannel?.permissions ?? guild?.permissions ?? '0');
    } catch {
      return 0n;
    }
  });

  function selectedHasPermission(permission: bigint): boolean {
    return hasAllPermissions(selectedEffectivePermissions, permission);
  }

  function channelHasPermission(channel: Channel, permission: bigint): boolean {
    try {
      return hasAllPermissions(
        BigInt(channel.permissions ?? guild?.permissions ?? '0'),
        permission
      );
    } catch {
      return false;
    }
  }

  function editableChannelParents(target: Channel | null): Channel[] {
    const currentParent =
      target?.parent_id && target.parent_domain
        ? `${target.parent_id}@${target.parent_domain}`
        : '';
    return (guild?.channels ?? []).filter(
      (channel) =>
        channel.type === 4 &&
        (entityKey(channel) === currentParent ||
          channelHasPermission(channel, CHANNEL_MOVE_PERMISSIONS))
    );
  }

  const canEditSelectedChannel = $derived(selectedHasPermission(Permission.MANAGE_CHANNELS));
  const canDeleteSelectedChannel = $derived(selectedHasPermission(Permission.MANAGE_CHANNELS));
  const canEditSelectedPermissions = $derived(selectedHasPermission(Permission.MANAGE_ROLES));
  const canCreateSelectedInvite = $derived(selectedHasPermission(Permission.CREATE_INVITE));
  const canManageSelectedWebhooks = $derived(
    Boolean(guild && selectedChannel && canManageWebhookChannel(selectedChannel, guild))
  );
  const manageableWebhookTargets = $derived(guild ? manageableWebhookChannels(guild) : []);
  const canReadSelectedAnnouncementFollows = $derived(
    Boolean(guild && selectedChannel && canReadAnnouncementChannel(selectedChannel, guild))
  );
  const canAccessSelectedIntegrations = $derived(
    canManageSelectedWebhooks || canReadSelectedAnnouncementFollows
  );
  const announcementGuilds = $derived.by(() => {
    const available = new SvelteMap<string, Guild>();
    for (const item of entities.guilds.values) available.set(entityRef(item), item);
    if (guild) available.set(entityRef(guild), guild);
    return [...available.values()];
  });
  const selectedChannelInvites = $derived(
    selectedChannel ? invites.filter((invite) => invite.channel_id === selectedChannel?.id) : []
  );
  const selectedChannelWebhooks = $derived(
    selectedChannel
      ? webhooks.filter(
          (webhook) =>
            webhook.channel_id === selectedChannel?.id &&
            webhook.channel_domain === selectedChannel?.origin_domain
        )
      : []
  );

  function canManageWebhook(webhook: WebhookSummary): boolean {
    if (!guild) return false;
    const target = guild.channels?.find(
      (channel) =>
        channel.id === webhook.channel_id && channel.origin_domain === webhook.channel_domain
    );
    return Boolean(target && canManageWebhookChannel(target, guild));
  }

  function initializeWebhookDrafts(value: WebhookSummary[]) {
    webhooks = value;
    webhookNameDrafts = Object.fromEntries(value.map((item) => [item.id, item.name]));
    webhookChannelDrafts = Object.fromEntries(
      value.map((item) => [item.id, `${item.channel_id}@${item.channel_domain}`])
    );
  }

  async function loadSelectedChannelWebhooks(channel: Channel) {
    if (!guild || !canManageWebhookChannel(channel, guild)) return;
    const generation = loadGeneration;
    const guildRef = entityRef(guild);
    try {
      const value = await listChannelWebhooks(
        guildRef,
        entityRef(channel),
        routeController?.signal
      );
      if (generation !== loadGeneration || !guild || entityRef(guild) !== guildRef) return;
      initializeWebhookDrafts([
        ...webhooks.filter(
          (item) => item.channel_id !== channel.id || item.channel_domain !== channel.origin_domain
        ),
        ...value
      ]);
    } catch (caught) {
      if (generation !== loadGeneration || routeController?.signal.aborted) return;
      error = userErrorMessage(
        caught,
        $t('ui_could_not_load_this_channel_s_webhooks_try_ag_d9d9ecd4')
      );
    }
  }

  function channelPath(channel: Channel): string {
    if (!guild) return resolve('/home');
    return guildChannelPath(guild, channel);
  }

  function donePath(): string {
    if (channelOnly && selectedChannel) return channelPath(selectedChannel);
    const channel = firstNavigableChannel(guild?.channels);
    return channel ? channelPath(channel) : resolve('/home');
  }

  function channelPanelTitle(panel: ChannelSettingsPanel): string {
    if (panel === 'permissions') return 'Channel Permissions';
    if (panel === 'invites') return 'Channel Invites';
    if (panel === 'integrations') return 'Channel Integrations';
    if (panel === 'delete') return `Delete ${selectedChannel?.type === 4 ? 'Category' : 'Channel'}`;
    return selectedChannel?.type === 4 ? 'Category Overview' : 'Channel Overview';
  }

  function channelPanelDescription(panel: ChannelSettingsPanel): string {
    if (panel === 'permissions') return 'Customize who can access and act in this channel.';
    if (panel === 'invites') return 'Create and manage invitation links for this channel.';
    if (panel === 'integrations')
      return 'Manage webhooks and announcement follower channels for this channel.';
    if (panel === 'delete') return 'Permanently remove this channel and its configuration.';
    return 'Update the channel name, topic, category, and behavior.';
  }

  function roleColorValue(color: number): string {
    return `#${color.toString(16).padStart(6, '0')}`;
  }

  const roleColorPalette = [
    '#1abc9c',
    '#2ecc71',
    '#3498db',
    '#9b59b6',
    '#e91e63',
    '#f1c40f',
    '#e67e22',
    '#e74c3c',
    '#11806a',
    '#1f8b4c',
    '#206694',
    '#71368a',
    '#ad1457',
    '#c27c0e',
    '#a84300',
    '#992d22',
    '#95a5a6',
    '#607d8b'
  ];

  function setRoleColor(value: string) {
    if (/^#[0-9a-f]{6}$/i.test(value)) roleColor = value.toLowerCase();
  }

  function normalizeRoleColorInput(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const candidate = input.value.trim();
    if (/^#?[0-9a-f]{6}$/i.test(candidate))
      setRoleColor(candidate.startsWith('#') ? candidate : `#${candidate}`);
    input.value = roleColor;
  }

  function roleContrastColor(color: string): '#111111' | '#ffffff' {
    const raw = Number.parseInt(color.replace('#', ''), 16);
    const channels = [raw >> 16, (raw >> 8) & 255, raw & 255].map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    });
    const luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    return luminance > 0.179 ? '#111111' : '#ffffff';
  }

  function mergeGuildState(current: GuildView, updated: GuildView): GuildView {
    return {
      ...current,
      ...updated,
      permissions: updated.permissions ?? current.permissions,
      channels: updated.channels ?? current.channels,
      roles: updated.roles ?? current.roles
    };
  }

  let savedChannel = $state('');
  const channelDraft = $derived(
    JSON.stringify([
      channelName,
      channelTopic.trim(),
      channelNsfw,
      channelParent,
      channelSlowmode,
      channelBitrate,
      channelUserLimit,
      channelRtcRegion.trim(),
      channelHistoryPolicy,
      channelForumTags,
      channelForumSort,
      channelForumLayout,
      channelForumArchive,
      channelForumSlowmode,
      channelForumReaction,
      channelForumReactionId,
      channelForumE2EE,
      channelForumRequireTag
    ])
  );
  const channelDirty = $derived(!!selectedChannel && channelDraft !== savedChannel);
  const guildDirty = $derived(
    !!guild &&
      (name !== guild.name ||
        description.trim() !== (guild.description ?? '') ||
        guildHistoryPolicy !== (guild.federated_history_policy ?? 'disabled'))
  );
  const roleDirty = $derived(
    !!selectedRole &&
      (roleName !== selectedRole.name ||
        roleColor !== roleColorValue(selectedRole.color) ||
        rolePermissions !== selectedRole.permissions ||
        roleHoist !== selectedRole.hoist ||
        roleMentionable !== selectedRole.mentionable)
  );
  const overwriteDirty = $derived.by(() => {
    const existing = channelOverwrites.find(
      (item) => `${item.target_type}:${item.target_id}@${item.target_domain}` === overwriteTarget
    );
    return (
      !!overwriteTarget &&
      (overwriteAllow !== (existing?.allow ?? '0') || overwriteDeny !== (existing?.deny ?? '0'))
    );
  });

  function selectChannel(channel: Channel, force = false) {
    if (busy && !force) return;
    selectedChannel = channel;
    if (channelEditorPanel === 'invites' && channel.type !== 4) {
      // Deep-linking directly to Channel Settings > Invites does not pass
      // through selectChannelPanel(). Keep creation anchored to the channel
      // whose permission grant made this surface visible.
      inviteChannel = entityKey(channel);
    }
    channelName = channel.name ?? '';
    channelTopic = channel.topic ?? '';
    channelNsfw = channel.nsfw ?? false;
    channelParent =
      channel.parent_id && channel.parent_domain
        ? `${channel.parent_id}@${channel.parent_domain}`
        : '';
    channelSlowmode = channel.rate_limit_per_user;
    channelBitrate = channel.bitrate ?? 64000;
    channelUserLimit = channel.user_limit ?? 0;
    channelRtcRegion = channel.rtc_region ?? '';
    channelHistoryPolicy = channel.federated_history_policy ?? 'inherit';
    channelForumTags = [...(channel.available_tags ?? [])];
    newForumTagName = '';
    channelForumSort = channel.default_sort_order === 1 ? 1 : 0;
    channelForumLayout =
      typeof channel.default_forum_layout === 'number' ? channel.default_forum_layout : 0;
    channelForumArchive = [60, 1440, 4320, 10080].includes(
      channel.default_auto_archive_duration ?? 1440
    )
      ? ((channel.default_auto_archive_duration ?? 1440) as 60 | 1440 | 4320 | 10080)
      : 1440;
    channelForumSlowmode = channel.default_thread_rate_limit_per_user ?? 0;
    channelForumReaction = channel.default_reaction_emoji?.emoji_name ?? '';
    channelForumReactionId = channel.default_reaction_emoji?.emoji_id ?? null;
    channelForumE2EE = channel.e2ee_required ?? false;
    channelForumRequireTag = Boolean(Number(channel.flags ?? 0) & (1 << 4));
    savedChannel = channelDraft;
    channelSafetyNumber = '';
    error = '';
    notice = '';
    channelOverwrites = [];
    overwriteTarget = '';
    overwriteAllow = '0';
    overwriteDeny = '0';
    if (guild && canEditSelectedPermissions) void loadChannelOverwrites(channel);
    if (channelOnly && webhookProjectionReady && channelEditorPanel === 'integrations') {
      void loadSelectedChannelWebhooks(channel);
    }
  }

  function addForumTag() {
    const tagName = newForumTagName.trim();
    if (
      !tagName ||
      channelForumTags.length >= 20 ||
      channelForumTags.some((tag) => tag.name.toLocaleLowerCase() === tagName.toLocaleLowerCase())
    )
      return;
    channelForumTags = [...channelForumTags, { name: tagName, moderated: false }];
    newForumTagName = '';
  }

  function updateForumTag(index: number, patch: Partial<EditableForumTag>) {
    channelForumTags = channelForumTags.map((tag, tagIndex) =>
      tagIndex === index ? { ...tag, ...patch } : tag
    );
  }

  function removeForumTag(index: number) {
    channelForumTags = channelForumTags.filter((_, tagIndex) => tagIndex !== index);
  }

  function changeForumEncryptionRequirement(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    if (!input.checked) {
      channelForumE2EE = false;
      return;
    }
    const confirmed = window.confirm(
      $t('ui_require_end_to_end_encryption_for_future_post_a1d64bc1')
    );
    channelForumE2EE = confirmed;
    if (!confirmed) input.checked = false;
  }

  function selectChannelPanel(panel: ChannelSettingsPanel) {
    if (panel === 'overview' && !canEditSelectedChannel) return;
    if (panel === 'permissions' && !canEditSelectedPermissions) return;
    if (panel === 'invites' && !(canCreateSelectedInvite || canEditSelectedChannel)) return;
    if (panel === 'integrations' && !canAccessSelectedIntegrations) return;
    if (panel === 'delete' && !canEditSelectedChannel) return;
    if (panel === 'invites' && selectedChannel) inviteChannel = entityKey(selectedChannel);
    channelEditorPanel = panel;
    if (panel === 'integrations' && channelOnly && selectedChannel && webhookProjectionReady) {
      void loadSelectedChannelWebhooks(selectedChannel);
    }
  }

  async function loadChannelOverwrites(channel: Channel) {
    if (!guild) return;
    const guildRef = entityRef(guild);
    const channelRef = entityRef(channel);
    try {
      const loaded = await api<ChannelOverwrite[]>(
        `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(channelRef)}/overwrites`
      );
      if (!selectedChannel || entityKey(selectedChannel) !== entityKey(channel)) return;
      channelOverwrites = loaded;
      if (overwriteTarget && !canManageOverwriteTarget(overwriteTarget)) {
        overwriteTarget = '';
        overwriteAllow = '0';
        overwriteDeny = '0';
      }
      const firstManageable = loaded.find((item) =>
        canManageOverwriteTarget(`${item.target_type}:${item.target_id}@${item.target_domain}`)
      );
      if (!overwriteTarget && firstManageable) {
        overwriteTarget = `${firstManageable.target_type}:${firstManageable.target_id}@${firstManageable.target_domain}`;
        selectOverwriteTarget(overwriteTarget);
      }
    } catch (caught) {
      if (selectedChannel && entityKey(selectedChannel) === entityKey(channel))
        error = userErrorMessage(
          caught,
          $t('ui_could_not_load_channel_permissions_try_again_cf8c7b32')
        );
    }
  }

  function selectOverwriteTarget(value: string) {
    if (!canManageOverwriteTarget(value)) return;
    overwriteTarget = value;
    const [targetType, ...refParts] = value.split(':');
    const targetRef = refParts.join(':');
    const existing = channelOverwrites.find(
      (item) =>
        item.target_type === targetType && `${item.target_id}@${item.target_domain}` === targetRef
    );
    overwriteAllow = existing?.allow ?? '0';
    overwriteDeny = existing?.deny ?? '0';
  }

  function overwriteTargetLabel(): string {
    if (!guild || !overwriteTarget) return 'Select a role or member';
    const [targetType, ...refParts] = overwriteTarget.split(':');
    const targetRef = refParts.join(':');
    if (targetType === 'role') {
      const role = guild.roles?.find((candidate) => entityRef(candidate) === targetRef);
      if (!role) return 'Role';
      return role.id === guild.id ? '@everyone' : role.name;
    }
    const member = members.find((candidate) => entityRef(candidate.user) === targetRef);
    return member?.nickname ?? userDisplayName(member?.user);
  }

  function overwritePermission(permission: bigint): 'inherit' | 'allow' | 'deny' {
    if (BigInt(overwriteAllow) & permission) return 'allow';
    if (BigInt(overwriteDeny) & permission) return 'deny';
    return 'inherit';
  }

  function setOverwritePermission(permission: bigint, value: string) {
    if (!canEditSelectedPermissions || !selectedHasPermission(permission)) return;
    let allow = BigInt(overwriteAllow);
    let deny = BigInt(overwriteDeny);
    allow &= ~permission;
    deny &= ~permission;
    if (value === 'allow') allow |= permission;
    if (value === 'deny') deny |= permission;
    overwriteAllow = allow.toString();
    overwriteDeny = deny.toString();
  }

  function saveChannelOverwrite() {
    if (!overwriteDirty) return;
    if (
      !guild ||
      !selectedChannel ||
      !overwriteTarget ||
      !canManageOverwriteTarget(overwriteTarget)
    )
      return;
    const [targetType, ...refParts] = overwriteTarget.split(':');
    const targetRef = refParts.join(':');
    const channel = selectedChannel;
    return run(async (guildRef, generation) => {
      await api(
        `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(entityRef(channel))}/overwrites`,
        {
          method: 'PUT',
          body: JSON.stringify({
            target_id: targetRef,
            target_type: targetType,
            allow: overwriteAllow,
            deny: overwriteDeny
          })
        }
      );
      if (generation !== loadGeneration) return;
      await loadChannelOverwrites(channel);
      notice = $t('ui_channel_permissions_saved_0f576307');
    });
  }

  function resetChannelOverwrite() {
    if (
      !guild ||
      !selectedChannel ||
      !overwriteTarget ||
      !canManageOverwriteTarget(overwriteTarget) ||
      !hasAllPermissions(
        selectedEffectivePermissions,
        BigInt(overwriteAllow) | BigInt(overwriteDeny)
      )
    )
      return;
    const [targetType, ...refParts] = overwriteTarget.split(':');
    const targetRef = refParts.join(':');
    const channel = selectedChannel;
    return run(async (guildRef, generation) => {
      await api(
        `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(entityRef(channel))}/overwrites/${encodeURIComponent(targetType)}/${encodeURIComponent(targetRef)}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration) return;
      await loadChannelOverwrites(channel);
      overwriteAllow = '0';
      overwriteDeny = '0';
      notice = $t('ui_channel_override_reset_to_inherited_permissio_faaea4fa');
    });
  }

  function syncChannelPermissions() {
    if (!guild || !selectedChannel || !selectedChannel.parent_id) return;
    const channel = selectedChannel;
    return run(async (guildRef, generation) => {
      const updated = await api<Channel>(
        `/guilds/${encodeURIComponent(guildRef)}/channels/${encodeURIComponent(entityRef(channel))}/permissions/sync`,
        { method: 'POST' }
      );
      if (generation !== loadGeneration || !guild) return;
      guild = {
        ...guild,
        channels: guild.channels?.map((item) =>
          entityKey(item) === entityKey(updated) ? updated : item
        )
      };
      selectChannel(updated, true);
      channelEditorPanel = 'permissions';
      notice = $t('ui_permissions_synced_with_the_category_f79f1c3f');
    });
  }

  function enableChannelEncryption() {
    if (!selectedChannel || !signedInUser) return;
    const channel = selectedChannel;
    const user = signedInUser;
    const rekey = channel.encryption_mode === 'e2ee' && channel.encryption_state === 'rekeying';
    if (!rekey && !e2eeActivationEnabled) return;
    if (channel.encryption_mode === 'e2ee' && !rekey) return;
    const activationWarning =
      channel.type === 2
        ? 'Turn on end-to-end encryption for this voice channel? This is permanent. Microphone, camera, screen video, and screen audio will be encrypted on participant devices. Recording, transcription, server media moderation, and unsupported clients will stop working. Participant, timing, track, and traffic metadata remains visible. Anyone can still record content on their own device. Participant identities remain unverified until members compare the safety number through a separate trusted channel; repeat that comparison after membership or identity changes to detect key substitution by an actively malicious instance.'
        : 'Turn on end-to-end encryption for this channel? This is permanent and protects only new content; existing history stays readable to the server. Server search, link and GIF previews, file previews, malware scanning, and PhotoDNA scanning will stop. Webhooks receive no access automatically; a verified webhook device can receive only future content after a server administrator grants it and the room establishes a rekey and history floor. Verified participant-mode apps follow the same future-only admission rule. Notifications become generic, while participants, timing, and message-size metadata remain visible. Participant identities remain unverified until members compare the safety number through a separate trusted channel; repeat that comparison after membership or identity changes to detect key substitution by an actively malicious instance. Losing the synchronized account vault, all trusted local state, and the recovery backup loses encrypted history. Removed members, apps, and webhooks keep content they already received.';
    if (
      !window.confirm(
        rekey ? $t('ui_create_fresh_encryption_keys_for_the_current__3f02ee10') : activationWarning
      )
    )
      return;
    return run(async (_guildRef, generation) => {
      const client = await initializeE2EE(user);
      const updated = rekey
        ? await client.rekeyRoom(entityRef(channel))
        : await client.activateRoom(entityRef(channel));
      if (!rekey) acknowledgeEncryptedRoom(entityRef(user), entityRef(updated));
      if (generation !== loadGeneration || !guild) return;
      guild = {
        ...guild,
        channels: guild.channels?.map((item) =>
          entityKey(item) === entityKey(updated) ? updated : item
        )
      };
      selectChannel(updated, true);
      notice = rekey
        ? 'Fresh encryption keys are active for the current channel members.'
        : 'End-to-end encryption is now on for this channel.';
    });
  }

  function verifyChannelSafetyNumber() {
    if (!selectedChannel || !signedInUser || selectedChannel.encryption_state !== 'active') return;
    const channel = selectedChannel;
    const user = signedInUser;
    return run(async (_guildRef, generation) => {
      const client = await initializeE2EE(user);
      const safetyNumber = await client.safetyNumber(channel);
      if (
        generation === loadGeneration &&
        selectedChannel &&
        entityKey(selectedChannel) === entityKey(channel)
      ) {
        channelSafetyNumber = safetyNumber;
        notice = $t('ui_compare_this_safety_number_with_members_throu_0247a03c');
      }
    });
  }

  function saveGuildNotificationLevel(level: GuildNotificationLevel) {
    if (!guild || level === guildNotificationLevel) return;
    return run(async (guildRef, generation) => {
      const updated = await api<GuildNotificationPreference>(
        `/guilds/${encodeURIComponent(guildRef)}/notification-settings`,
        {
          method: 'PUT',
          body: JSON.stringify({ level })
        }
      );
      if (generation !== loadGeneration || !guild) return;
      guildNotificationLevel = updated.level;
      browserNotifications.setGuildPreference(guild, updated.level);
      notice = $t('ui_notification_settings_saved_d4b4e3ea');
    });
  }

  function selectRole(role: Role, force = false) {
    if (busy && !force) return;
    selectedRole = role;
    roleName = role.name;
    roleColor = roleColorValue(role.color);
    rolePermissions = role.permissions;
    roleHoist = role.hoist;
    roleMentionable = role.mentionable;
    roleIconFile = null;
    roleIconError = '';
    roleEditorTab = 'display';
    error = '';
    notice = '';
  }

  async function load(
    targetGuild: string,
    targetChannel: string,
    requestedPanel: string | null,
    generation: number,
    signal: AbortSignal
  ) {
    loading = true;
    webhookProjectionReady = false;
    try {
      const [loaded, currentUser, notificationSettings, authConfiguration] = await Promise.all([
        api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}`, { signal }),
        api<UserSummary>('/users/@me', { signal }),
        api<GuildNotificationPreference>(
          `/guilds/${encodeURIComponent(targetGuild)}/notification-settings`,
          { signal }
        ),
        loadAuthConfiguration(signal)
      ]);
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      localDomain = currentUser.origin_domain;
      currentUserRef = entityRef(currentUser);
      signedInUser = currentUser;
      e2eeActivationEnabled = authConfiguration.e2ee_activation_enabled;
      void initializeE2EE(currentUser).catch(() => {
        // Guild settings remain available on clients without secure device storage.
      });
      entities.ingestGuilds([loaded]);
      guild = loaded;
      initializeExpressionDrafts(loaded);
      name = loaded.name;
      description = loaded.description ?? '';
      guildHistoryPolicy = loaded.federated_history_policy ?? 'disabled';
      guildNotificationLevel = notificationSettings.level;
      const requestedChannel = loaded.channels?.find(
        (channel) => entityRef(channel) === targetChannel
      );
      let requestedChannelPermissions = 0n;
      try {
        requestedChannelPermissions = BigInt(
          requestedChannel?.permissions ?? loaded.permissions ?? '0'
        );
      } catch {
        requestedChannelPermissions = 0n;
      }
      const requestedChannelAllows = (permission: bigint) =>
        Boolean(requestedChannel && hasAllPermissions(requestedChannelPermissions, permission));
      selectedChannel = channelOnly ? (requestedChannel ?? null) : (loaded.channels?.[0] ?? null);
      channelEditorPanel =
        requestedPanel === 'permissions' ||
        requestedPanel === 'invites' ||
        requestedPanel === 'integrations' ||
        requestedPanel === 'delete'
          ? requestedPanel
          : 'overview';
      if (selectedChannel) selectChannel(selectedChannel);
      else if (channelOnly) error = $t('ui_this_channel_is_unavailable_or_you_no_longer__7afa473d');
      selectedRole =
        loaded.roles?.find((role) => role.id !== loaded.id) ?? loaded.roles?.[0] ?? null;
      if (selectedRole) selectRole(selectedRole);
      const permissions = BigInt(loaded.permissions ?? '0');
      const ownerRef = `${loaded.owner_id}@${loaded.owner_domain ?? loaded.origin_domain}`;
      const allows = (permission: bigint) =>
        hasGuildPermissionOrOwnership(
          permissions,
          permission,
          `${currentUser.id}@${currentUser.origin_domain}`,
          ownerRef
        );
      const optional: Promise<unknown>[] = [];
      channelVoiceRegions = [];
      channelVoiceRegionsError = '';
      if (
        (allows(Permission.MANAGE_CHANNELS) &&
          loaded.channels?.some((channel) => channel.type === 2 || channel.type === 13)) ||
        (channelOnly &&
          (requestedChannel?.type === 2 || requestedChannel?.type === 13) &&
          requestedChannelAllows(Permission.MANAGE_CHANNELS))
      ) {
        optional.push(
          api<VoiceRegion[]>(`/voice/regions?guild_ref=${encodeURIComponent(targetGuild)}`, {
            signal
          })
            .then((value) => {
              if (generation === loadGeneration) channelVoiceRegions = value;
            })
            .catch((caught) => {
              if (signal.aborted || generation !== loadGeneration) return;
              channelVoiceRegionsError = userErrorMessage(
                caught,
                $t('ui_region_overrides_are_temporarily_unavailable_f02e100d')
              );
            })
        );
      }
      scheduledEvents = [];
      optional.push(
        listScheduledEvents(targetGuild, signal).then((value) => {
          if (generation === loadGeneration) scheduledEvents = value;
        })
      );
      if (
        allows(Permission.CREATE_GUILD_EXPRESSIONS | Permission.MANAGE_GUILD_EXPRESSIONS) ||
        (!channelOnly && allows(Permission.MANAGE_CHANNELS)) ||
        (requestedChannel?.type === 15 && requestedChannelAllows(Permission.MANAGE_CHANNELS))
      ) {
        optional.push(
          Promise.all([
            api<CustomEmoji[]>(`/guilds/${encodeURIComponent(targetGuild)}/emojis`, { signal }),
            api<GuildSticker[]>(`/guilds/${encodeURIComponent(targetGuild)}/stickers`, { signal })
          ]).then(([emojis, stickers]) => {
            if (generation === loadGeneration && guild) {
              guild = { ...guild, emojis, stickers };
              initializeExpressionDrafts(guild);
            }
          })
        );
      }
      if (allows(Permission.VIEW_CHANNEL)) {
        optional.push(
          api<MemberSummary[]>(
            `/guilds/${encodeURIComponent(targetGuild)}/members?limit=${MEMBER_PAGE_SIZE + 1}`,
            { signal }
          ).then((value) => {
            if (generation === loadGeneration) {
              members = cacheMemberRows(value.slice(0, MEMBER_PAGE_SIZE));
              membersHaveMore = value.length > MEMBER_PAGE_SIZE;
            }
          })
        );
      }
      if (allows(Permission.MANAGE_GUILD)) {
        optional.push(
          api<InviteSummary[]>(`/guilds/${encodeURIComponent(targetGuild)}/invites`, {
            signal
          }).then((value) => {
            if (generation === loadGeneration) invites = value;
          })
        );
      } else if (channelOnly && requestedChannel) {
        if (requestedChannelAllows(Permission.MANAGE_CHANNELS)) {
          optional.push(
            api<InviteSummary[]>(channelInviteListPath(entityRef(requestedChannel)), {
              signal
            }).then((value) => {
              if (generation === loadGeneration) invites = value;
            })
          );
        }
      }
      if (allows(Permission.MANAGE_WEBHOOKS)) {
        optional.push(
          listGuildWebhooks(targetGuild, signal).then((value) => {
            if (generation === loadGeneration) initializeWebhookDrafts(value);
          })
        );
      } else if (
        channelOnly &&
        requestedChannel &&
        canManageWebhookChannel(requestedChannel, loaded)
      ) {
        optional.push(
          listChannelWebhooks(targetGuild, entityRef(requestedChannel), signal).then((value) => {
            if (generation === loadGeneration) initializeWebhookDrafts(value);
          })
        );
      }
      if (allows(Permission.BAN_MEMBERS)) {
        optional.push(
          api<BanSummary[]>(`/guilds/${encodeURIComponent(targetGuild)}/bans?limit=1000`, {
            signal
          }).then((value) => {
            if (generation === loadGeneration) bans = value;
          })
        );
      }
      if (allows(Permission.BAN_INSTANCES)) {
        optional.push(
          api<InstanceBanSummary[]>(
            `/guilds/${encodeURIComponent(targetGuild)}/instance-bans?limit=1000`,
            { signal }
          ).then((value) => {
            if (generation === loadGeneration) instanceBans = value;
          })
        );
      }
      await Promise.all(optional);
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      webhookProjectionReady = true;
    } catch (caught) {
      if (signal.aborted || generation !== loadGeneration || targetGuild !== guildId) return;
      error = userErrorMessage(caught, $t('ui_could_not_load_guild_settings_try_again_72d3e788'));
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) loading = false;
    }
  }

  async function run(
    action: (targetGuild: string, generation: number) => Promise<void>
  ): Promise<boolean> {
    if (busy || loading) return false;
    const targetGuild = guildId;
    const generation = loadGeneration;
    busy = true;
    error = '';
    notice = '';
    guildAssetError = '';
    try {
      await action(targetGuild, generation);
      return generation === loadGeneration && targetGuild === guildId;
    } catch (caught) {
      if (generation !== loadGeneration || targetGuild !== guildId) return false;
      if (
        caught instanceof ApiError &&
        (caught.code === 'SETTINGS_VERSION_CONFLICT' || caught.code === 'SETTINGS_VERSION_REQUIRED')
      ) {
        const controller = new AbortController();
        routeController?.abort();
        routeController = controller;
        await load(
          targetGuild,
          channelOnly ? channelId : '',
          channelOnly ? channelEditorPanel : null,
          generation,
          controller.signal
        );
        if (generation === loadGeneration && targetGuild === guildId) {
          notice = $t('ui_these_settings_changed_elsewhere_the_latest_v_ee001ea9');
        }
        return false;
      }
      error = userErrorMessage(caught, $t('ui_the_change_could_not_be_saved_try_again_c2070387'));
      return false;
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) busy = false;
    }
  }

  async function openDestructiveConfirmation(confirmation: DestructiveConfirmation) {
    if (busy) return;
    confirmationPreviousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    destructiveConfirmation = confirmation;
    confirmationVerification = '';
    error = '';
    notice = '';
    await tick();
    confirmationCancelButton?.focus();
  }

  function closeDestructiveConfirmation() {
    if (busy) return;
    const previousFocus = confirmationPreviousFocus;
    destructiveConfirmation = null;
    confirmationVerification = '';
    confirmationPreviousFocus = null;
    void tick().then(() => {
      if (previousFocus?.isConnected) {
        previousFocus.focus();
        return;
      }
      document.querySelector<HTMLElement>('.settings-content a, .settings-content button')?.focus();
    });
  }

  function confirmationKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeDestructiveConfirmation();
      return;
    }
    if (event.key !== 'Tab' || !confirmationDialog) return;
    const focusable = Array.from(
      confirmationDialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const current = focusable.indexOf(document.activeElement as HTMLElement);
    const next = event.shiftKey
      ? current <= 0
        ? focusable.length - 1
        : current - 1
      : current < 0 || current === focusable.length - 1
        ? 0
        : current + 1;
    focusable[next].focus();
    event.preventDefault();
  }

  function saveGuild() {
    if (!canManageGuild || !guildDirty) return;
    return run(async (targetGuild, generation) => {
      const updated = await api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}`, {
        method: 'PATCH',
        headers: guild?.version ? { 'If-Match': `"${guild.version}"` } : undefined,
        body: JSON.stringify({
          name,
          description: description.trim() || null,
          federated_history_policy: guildHistoryPolicy
        })
      });
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) guild = mergeGuildState(guild, updated);
      notice = $t('ui_overview_saved_aaf90806');
    });
  }

  async function uploadGuildAsset(kind: GuildAssetKind, file: File) {
    const controller = routeController;
    if (busy || loading || !guild || !canManageGuildAssets || !controller) return;
    if (!acceptedImageTypes.has(file.type)) {
      guildAssetError = $t('ui_choose_a_png_jpeg_gif_or_webp_image_4cef2205');
      error = '';
      notice = '';
      return;
    }
    if (file.size < 1) {
      guildAssetError = $t('ui_choose_a_non_empty_image_file_b81ea05c');
      error = '';
      notice = '';
      return;
    }

    const targetGuild = guildId;
    const generation = loadGeneration;
    busy = true;
    error = '';
    notice = '';
    guildAssetError = '';
    guildAssetKind = kind;
    guildAssetStage = 'uploading';
    guildAssetProgress = 0;

    try {
      const ticket = await api<UploadTicket>(
        `/guilds/${encodeURIComponent(targetGuild)}/assets/${kind}`,
        {
          method: 'POST',
          signal: controller.signal,
          body: JSON.stringify({
            filename: file.name || `${kind}.image`,
            content_type: file.type,
            size: file.size
          })
        }
      );
      await uploadObject(
        ticket,
        file,
        (progress) => {
          if (
            !controller.signal.aborted &&
            generation === loadGeneration &&
            targetGuild === guildId
          ) {
            guildAssetProgress = progress;
          }
        },
        controller.signal
      );
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      guildAssetProgress = 100;
      guildAssetStage = 'scanning';
      await completeScannedMediaResource(
        () =>
          api<{ scan_status: string }>(
            `/guilds/${encodeURIComponent(targetGuild)}/assets/${kind}`,
            {
              method: 'PUT',
              signal: controller.signal,
              body: JSON.stringify({ attachment_id: ticket.id })
            }
          ),
        (attachment): attachment is { scan_status: string } => attachment.scan_status === 'clean',
        {
          signal: controller.signal,
          maxAttempts: 30,
          rejectedMessage: 'The image did not pass media processing.'
        }
      );
      const updated = await api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}`, {
        signal: controller.signal
      });
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      if (guild) guild = mergeGuildState(guild, updated);
      notice = `${kind === 'icon' ? 'Guild icon' : 'Guild banner'} updated.`;
      return;
    } catch (caught) {
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      guildAssetError = userErrorMessage(
        caught,
        $t('ui_could_not_update_the_guild_image_choose_the_f_4f8f9940')
      );
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) {
        busy = false;
        guildAssetKind = null;
        guildAssetStage = null;
        guildAssetProgress = 0;
      }
    }
  }

  async function removeGuildAsset(kind: GuildAssetKind) {
    const controller = routeController;
    if (busy || loading || !guild || !canManageGuildAssets || !controller) return;
    const label = kind === 'icon' ? 'guild icon' : 'guild banner';
    if (!window.confirm(`Remove the ${label}? You can upload a new one at any time.`)) return;
    const targetGuild = guildId;
    const generation = loadGeneration;
    busy = true;
    error = '';
    notice = '';
    try {
      const updated = await api<GuildView>(
        `/guilds/${encodeURIComponent(targetGuild)}/assets/${kind}`,
        { method: 'DELETE', signal: controller.signal }
      );
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      if (guild) guild = mergeGuildState(guild, updated);
      entities.guilds.upsert(updated);
      notice = `${kind === 'icon' ? 'Guild icon' : 'Guild banner'} removed.`;
    } catch (caught) {
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      error = userErrorMessage(caught, `Could not remove the ${label}. Try again.`);
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) busy = false;
    }
  }

  async function createEmoji(event: SubmitEvent) {
    event.preventDefault();
    const signal = routeController?.signal;
    if (!guild || !emojiFile || !canCreateExpressions || emojiBusy || !signal) return;
    const file = emojiFile;
    if (!acceptedImageTypes.has(file.type)) {
      error = $t('ui_choose_a_png_jpeg_gif_or_webp_image_4cef2205');
      return;
    }
    if (file.size > (guild.emoji_max_bytes ?? 262144)) {
      error = `Emoji images can be at most ${Math.ceil((guild.emoji_max_bytes ?? 262144) / 1024)} KiB.`;
      return;
    }
    emojiBusy = true;
    error = '';
    notice = '';
    try {
      const ticket = await api<UploadTicket>(
        `/guilds/${encodeURIComponent(guildId)}/emojis/tickets`,
        {
          method: 'POST',
          body: JSON.stringify({ filename: file.name, content_type: file.type, size: file.size })
        }
      );
      await uploadObject(ticket, file, () => undefined, signal);
      const commit = () =>
        api<CustomEmoji | { scan_status: string }>(
          `/guilds/${encodeURIComponent(guildId)}/emojis`,
          {
            method: 'POST',
            body: JSON.stringify({ attachment_id: ticket.id, name: emojiName.trim() })
          }
        );
      const created = await completeScannedMediaResource(
        commit,
        (value): value is CustomEmoji => 'media_hash' in value && Boolean(value.media_hash),
        { signal, maxAttempts: 30, rejectedMessage: 'The emoji did not pass media processing.' }
      );
      guild = {
        ...guild,
        emojis: [
          ...(guild.emojis ?? []).filter((item) => entityKey(item) !== entityKey(created)),
          created
        ]
      };
      patchEmojiDraft(created, {
        name: created.name,
        roles: created.roles ?? []
      });
      emojiName = '';
      emojiFile = null;
      if (emojiInput) emojiInput.value = '';
      notice = `:${created.name}: is ready to use.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_create_the_emoji_choose_the_file_ag_9425c308')
      );
    } finally {
      emojiBusy = false;
    }
  }

  async function deleteEmoji(emoji: CustomEmoji) {
    if (!guild || !canEditEmoji(emoji) || emojiBusy) return;
    if (!confirm(`Delete :${emoji.name}:? This cannot be undone.`)) return;
    emojiBusy = true;
    error = '';
    try {
      await api(`/guilds/${encodeURIComponent(guildId)}/emojis/${encodeURIComponent(emoji.id)}`, {
        method: 'DELETE'
      });
      guild = {
        ...guild,
        emojis: (guild.emojis ?? []).filter((item) => entityKey(item) !== entityKey(emoji))
      };
      notice = `:${emoji.name}: was deleted.`;
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_delete_the_emoji_try_again_8cec4c2e'));
    } finally {
      emojiBusy = false;
    }
  }

  function patchEmojiDraft(emoji: CustomEmoji, patch: Partial<{ name: string; roles: string[] }>) {
    const key = entityKey(emoji);
    emojiDrafts = {
      ...emojiDrafts,
      [key]: {
        ...(emojiDrafts[key] ?? {
          name: emoji.name,
          roles: emoji.roles ?? []
        }),
        ...patch
      }
    };
  }

  function emojiDirty(emoji: CustomEmoji) {
    const value = emojiDrafts[entityKey(emoji)];
    return (
      !!value &&
      (value.name.trim() !== emoji.name ||
        (canEditEmojiRoleRestrictions(emoji) &&
          JSON.stringify([...value.roles].sort()) !==
            JSON.stringify([...(emoji.roles ?? [])].sort())))
    );
  }

  async function updateEmoji(emoji: CustomEmoji) {
    if (!emojiDirty(emoji)) return;
    const draftValue = emojiDrafts[entityKey(emoji)];
    if (!guild || !draftValue || !canEditEmoji(emoji) || emojiBusy) return;
    if (!draftValue.name.trim()) {
      error = $t('ui_emoji_names_cannot_be_blank_090b47ad');
      return;
    }
    emojiBusy = true;
    error = '';
    try {
      const updated = await api<CustomEmoji>(
        `/guilds/${encodeURIComponent(guildId)}/emojis/${encodeURIComponent(emoji.id)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            name: draftValue.name.trim(),
            ...(canEditEmojiRoleRestrictions(emoji) ? { role_ids: draftValue.roles } : {})
          })
        }
      );
      guild = {
        ...guild,
        emojis: (guild.emojis ?? []).map((item) =>
          entityKey(item) === entityKey(updated) ? updated : item
        )
      };
      patchEmojiDraft(updated, {
        name: updated.name,
        roles: updated.roles ?? []
      });
      notice = `:${updated.name}: was updated.`;
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_update_the_emoji_try_again_d7fc3b29'));
    } finally {
      emojiBusy = false;
    }
  }

  function selectStickerFile(file: File | null, input: HTMLInputElement) {
    if (stickerPreviewUrl) URL.revokeObjectURL(stickerPreviewUrl);
    stickerFile = file;
    stickerInput = input;
    stickerPreviewUrl = file ? URL.createObjectURL(file) : '';
    stickerCropX = 0;
    stickerCropY = 0;
    stickerCropWidth = 1;
    stickerCropHeight = 1;
    stickerImageAspect = 1;
    stickerRemoveBackground = false;
  }

  function currentStickerCrop(): NormalizedCrop {
    return {
      x: stickerCropX,
      y: stickerCropY,
      width: stickerCropWidth,
      height: stickerCropHeight
    };
  }

  function applyStickerCrop(crop: NormalizedCrop) {
    stickerCropX = crop.x;
    stickerCropY = crop.y;
    stickerCropWidth = crop.width;
    stickerCropHeight = crop.height;
  }

  function beginStickerCropGesture(
    event: PointerEvent,
    mode: 'move' | CropCorner,
    stopPropagation = false
  ) {
    if (event.button !== 0) return;
    event.preventDefault();
    if (stopPropagation) event.stopPropagation();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    stickerCropGesture = {
      pointerId: event.pointerId,
      mode,
      clientX: event.clientX,
      clientY: event.clientY
    };
  }

  function moveStickerCropGesture(event: PointerEvent) {
    const gesture = stickerCropGesture;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    event.preventDefault();
    const dx = (event.clientX - gesture.clientX) / bounds.width;
    const dy = (event.clientY - gesture.clientY) / bounds.height;
    applyStickerCrop(
      gesture.mode === 'move'
        ? moveCrop(currentStickerCrop(), dx, dy)
        : resizeCrop(currentStickerCrop(), gesture.mode, dx, dy)
    );
    gesture.clientX = event.clientX;
    gesture.clientY = event.clientY;
  }

  function endStickerCropGesture(event: PointerEvent) {
    if (stickerCropGesture?.pointerId === event.pointerId) stickerCropGesture = null;
  }

  function moveStickerCropWithKeyboard(event: KeyboardEvent) {
    if (event.target !== event.currentTarget) return;
    const step = event.shiftKey ? 0.05 : 0.01;
    const delta = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step]
    }[event.key];
    if (!delta) return;
    event.preventDefault();
    applyStickerCrop(moveCrop(currentStickerCrop(), delta[0], delta[1]));
  }

  function resizeStickerCropWithKeyboard(event: KeyboardEvent, corner: CropCorner) {
    const step = event.shiftKey ? 0.05 : 0.01;
    const delta = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step]
    }[event.key];
    if (!delta) return;
    event.preventDefault();
    event.stopPropagation();
    applyStickerCrop(resizeCrop(currentStickerCrop(), corner, delta[0], delta[1]));
  }

  async function createSticker(event: SubmitEvent) {
    event.preventDefault();
    const signal = routeController?.signal;
    if (!guild || !stickerFile || !canCreateExpressions || stickerBusy || !signal) return;
    const file = stickerFile;
    if (!acceptedImageTypes.has(file.type)) {
      error = $t('ui_choose_a_png_jpeg_gif_or_webp_image_4cef2205');
      return;
    }
    if (file.size > (guild.sticker_max_bytes ?? 524288)) {
      error = `Sticker images can be at most ${Math.ceil((guild.sticker_max_bytes ?? 524288) / 1024)} KiB.`;
      return;
    }
    const cleanedName = stickerName.trim();
    const cleanedDescription = stickerDescription.trim();
    if (!validStickerName(cleanedName)) {
      error = $t('ui_sticker_names_must_contain_2_30_meaningful_ch_5d459eb3');
      return;
    }
    if (!validStickerDescription(cleanedDescription)) {
      error = $t('ui_sticker_descriptions_must_be_empty_or_contain_01ecbdf8');
      return;
    }
    stickerBusy = true;
    error = '';
    notice = '';
    try {
      const ticket = await api<UploadTicket>(
        `/guilds/${encodeURIComponent(guildId)}/stickers/tickets`,
        {
          method: 'POST',
          body: JSON.stringify({
            filename: file.name,
            content_type: file.type,
            size: file.size,
            crop: {
              x: stickerCropX,
              y: stickerCropY,
              width: stickerCropWidth,
              height: stickerCropHeight
            },
            remove_background: stickerRemoveBackground
          })
        }
      );
      await uploadObject(ticket, file, () => undefined, signal);
      const commit = () =>
        api<GuildSticker | { scan_status: string }>(
          `/guilds/${encodeURIComponent(guildId)}/stickers`,
          {
            method: 'POST',
            body: JSON.stringify({
              attachment_id: ticket.id,
              name: cleanedName,
              description: cleanedDescription || null
            })
          }
        );
      const created = await completeScannedMediaResource(
        commit,
        (value): value is GuildSticker => 'media_hash' in value && Boolean(value.media_hash),
        { signal, rejectedMessage: 'The sticker did not pass media processing.' }
      );
      guild = {
        ...guild,
        stickers: [
          ...(guild.stickers ?? []).filter((item) => entityKey(item) !== entityKey(created)),
          created
        ]
      };
      patchStickerDraft(created, {
        name: created.name,
        description: created.description ?? '',
        tags: (created.tags ?? []).join(', ')
      });
      stickerName = '';
      stickerDescription = '';
      if (stickerPreviewUrl) URL.revokeObjectURL(stickerPreviewUrl);
      stickerPreviewUrl = '';
      stickerFile = null;
      if (stickerInput) stickerInput.value = '';
      notice = `${created.name} is ready to use.`;
    } catch (caught) {
      error = userErrorMessage(
        caught,
        $t('ui_could_not_create_the_sticker_choose_the_file__ff4c7816')
      );
    } finally {
      stickerBusy = false;
    }
  }

  async function deleteSticker(sticker: GuildSticker) {
    if (!guild || !canEditSticker(sticker) || stickerBusy) return;
    if (!confirm(`Delete the sticker “${sticker.name}”? This cannot be undone.`)) return;
    stickerBusy = true;
    error = '';
    try {
      await api(
        `/guilds/${encodeURIComponent(guildId)}/stickers/${encodeURIComponent(sticker.id)}`,
        { method: 'DELETE' }
      );
      guild = {
        ...guild,
        stickers: (guild.stickers ?? []).filter((item) => entityKey(item) !== entityKey(sticker))
      };
      notice = `${sticker.name} was deleted.`;
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_delete_the_sticker_try_again_e13d8c4e'));
    } finally {
      stickerBusy = false;
    }
  }

  function patchStickerDraft(
    sticker: GuildSticker,
    patch: Partial<{ name: string; description: string; tags: string }>
  ) {
    const key = entityKey(sticker);
    stickerDrafts = {
      ...stickerDrafts,
      [key]: {
        ...(stickerDrafts[key] ?? {
          name: sticker.name,
          description: sticker.description ?? '',
          tags: (sticker.tags ?? []).join(', ')
        }),
        ...patch
      }
    };
  }

  function stickerDirty(sticker: GuildSticker) {
    const value = stickerDrafts[entityKey(sticker)];
    return (
      !!value &&
      (value.name.trim() !== sticker.name ||
        value.description.trim() !== (sticker.description ?? '') ||
        JSON.stringify([
          ...new Set(
            value.tags
              .split(',')
              .map((tag) => tag.trim())
              .filter(Boolean)
          )
        ]) !== JSON.stringify(sticker.tags ?? []))
    );
  }

  async function updateSticker(sticker: GuildSticker) {
    if (!stickerDirty(sticker)) return;
    const draftValue = stickerDrafts[entityKey(sticker)];
    if (!guild || !draftValue || !canEditSticker(sticker) || stickerBusy) return;
    const tags = [
      ...new Set(
        draftValue.tags
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean)
      )
    ];
    const cleanedName = draftValue.name.trim();
    const cleanedDescription = draftValue.description.trim();
    if (!validStickerName(cleanedName)) {
      error = $t('ui_sticker_names_must_contain_2_30_meaningful_ch_5d459eb3');
      return;
    }
    if (!validStickerDescription(cleanedDescription)) {
      error = $t('ui_sticker_descriptions_must_be_empty_or_contain_01ecbdf8');
      return;
    }
    if (
      !tags.length ||
      tags.length > 10 ||
      tags.some((tag) => tag.length > 100) ||
      tags.join(',').length > 200
    ) {
      error = $t('ui_sticker_tags_must_contain_1_10_unique_values__636809a5');
      return;
    }
    stickerBusy = true;
    error = '';
    try {
      const updated = await api<GuildSticker>(
        `/guilds/${encodeURIComponent(guildId)}/stickers/${encodeURIComponent(sticker.id)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            name: cleanedName,
            description: cleanedDescription || null,
            tags
          })
        }
      );
      guild = {
        ...guild,
        stickers: (guild.stickers ?? []).map((item) =>
          entityKey(item) === entityKey(updated) ? updated : item
        )
      };
      patchStickerDraft(updated, {
        name: updated.name,
        description: updated.description ?? '',
        tags: (updated.tags ?? []).join(', ')
      });
      notice = `${updated.name} was updated.`;
    } catch (caught) {
      error = userErrorMessage(caught, $t('ui_could_not_update_the_sticker_try_again_4ccf1a50'));
    } finally {
      stickerBusy = false;
    }
  }

  function createChannel() {
    if (!canManageChannels) return;
    return run(async (targetGuild, generation) => {
      const parent = guild?.channels?.find(
        (channel) => entityKey(channel) === newChannelParent && channel.type === 4
      );
      const channel = await api<Channel>(`/guilds/${encodeURIComponent(targetGuild)}/channels`, {
        method: 'POST',
        body: JSON.stringify({
          name: newChannelName,
          type: newChannelType,
          parent_id: newChannelType === 4 ? null : (parent?.id ?? null),
          ...(newChannelType === TRACKER_CHANNEL_TYPE && newChannelTrackerPrefix.trim()
            ? { tracker_key_prefix: newChannelTrackerPrefix.trim().toUpperCase() }
            : {})
        })
      });
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) guild = { ...guild, channels: [...(guild.channels ?? []), channel] };
      newChannelName = '';
      newChannelTrackerPrefix = '';
      selectChannel(channel, true);
      notice = `${channel.type === 4 ? 'Category' : 'Channel'} created.`;
    });
  }

  function saveChannel() {
    if (!canEditSelectedChannel || !selectedChannel || !channelDirty) return;
    const target = selectedChannel;
    return run(async (targetGuild, generation) => {
      const current = guild?.channels?.find((channel) => entityKey(channel) === entityKey(target));
      if (!current || !channelHasPermission(current, CHANNEL_MOVE_PERMISSIONS)) {
        error = $t('ui_you_no_longer_have_permission_to_edit_this_ch_27548c26');
        return;
      }
      const parent = guild?.channels?.find(
        (channel) => entityKey(channel) === channelParent && channel.type === 4
      );
      if (channelParent && !parent) {
        error = $t('ui_that_category_is_no_longer_available_3259687c');
        return;
      }
      const currentParent =
        current.parent_id && current.parent_domain
          ? `${current.parent_id}@${current.parent_domain}`
          : '';
      if (
        channelParent !== currentParent &&
        parent &&
        !channelHasPermission(parent, CHANNEL_MOVE_PERMISSIONS)
      ) {
        error = $t('ui_you_cannot_move_this_channel_to_that_category_c8b19993');
        return;
      }
      const updated = await api<Channel>(
        `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(current))}`,
        {
          method: 'PATCH',
          headers: current.version ? { 'If-Match': `"${current.version}"` } : undefined,
          body: JSON.stringify({
            name: channelName,
            topic: channelTopic.trim() || null,
            nsfw: channelNsfw,
            parent_id: current.type === 4 ? null : (parent?.id ?? null),
            rate_limit_per_user: current.type === 4 ? 0 : channelSlowmode,
            federated_history_policy:
              current.type === 0 || current.type === 5 ? channelHistoryPolicy : 'inherit',
            ...(current.type === 0 || current.type === 5
              ? {
                  default_auto_archive_duration: channelForumArchive,
                  ...(current.type === 0
                    ? { default_thread_rate_limit_per_user: channelForumSlowmode }
                    : {})
                }
              : {}),
            ...(current.type === 15
              ? {
                  available_tags: channelForumTags,
                  default_reaction_emoji: forumDefaultReactionPayload(
                    channelForumReaction,
                    channelForumReactionId
                  ),
                  default_auto_archive_duration: channelForumArchive,
                  default_thread_rate_limit_per_user: channelForumSlowmode,
                  default_sort_order: channelForumSort,
                  default_forum_layout: channelForumLayout,
                  e2ee_required: current.e2ee_required ? true : channelForumE2EE,
                  flags: channelForumRequireTag ? 1 << 4 : 0
                }
              : {}),
            ...(current.type === 2 || current.type === 13
              ? {
                  bitrate: channelBitrate,
                  user_limit: channelUserLimit,
                  // Region IDs are deliberately opaque. Empty keeps automatic
                  // selection and remains compatible with future providers.
                  rtc_region: channelRtcRegion.trim() || null
                }
              : {})
          })
        }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) {
        guild = {
          ...guild,
          channels: guild.channels?.map((channel) =>
            entityKey(channel) === entityKey(updated) ? updated : channel
          )
        };
      }
      selectChannel(updated, true);
      notice = $t('ui_channel_saved_2ad13379');
    });
  }

  function deleteChannel() {
    if (!canDeleteSelectedChannel || !selectedChannel || !guild) return;
    const target = selectedChannel;
    const label = target.type === 4 ? 'category' : 'channel';
    const name = target.name ?? 'Untitled';
    void openDestructiveConfirmation({
      kind: 'channel',
      target,
      title: `Delete ${label}?`,
      description:
        target.type === 4
          ? `“${name}” will be permanently removed. A category must be empty before it can be deleted.`
          : `“${name}” will be permanently removed. A channel containing messages cannot be deleted.`,
      confirmLabel: `Delete ${label}`
    });
  }

  function deleteConfirmedChannel(target: Channel) {
    const current = guild?.channels?.find((channel) => entityKey(channel) === entityKey(target));
    if (!current || !channelHasPermission(current, Permission.MANAGE_CHANNELS)) return false;
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(target))}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      const remaining = (guild.channels ?? []).filter(
        (channel) => entityKey(channel) !== entityKey(target)
      );
      guild = { ...guild, channels: remaining };
      selectedChannel = remaining[0] ?? null;
      if (selectedChannel) selectChannel(selectedChannel, true);
      notice = `${target.type === 4 ? 'Category' : 'Channel'} deleted.`;
    });
  }

  function permissionChecked(permission: bigint): boolean {
    try {
      return Boolean(BigInt(rolePermissions) & permission);
    } catch {
      return false;
    }
  }

  function togglePermission(permission: bigint, enabled: boolean) {
    let value = BigInt(rolePermissions || '0');
    value = enabled ? value | permission : value & ~permission;
    rolePermissions = value.toString();
  }

  function setGuildRoles(roles: Role[]) {
    if (!guild) return;
    const targetGuild = entityKey(guild);
    guild = { ...guild, roles };
    entities.guilds.update(targetGuild, (current) => ({ ...current, roles }));
  }

  function createRole() {
    if (!canManageRoles) return;
    return run(async (targetGuild, generation) => {
      const role = await api<Role>(`/guilds/${encodeURIComponent(targetGuild)}/roles`, {
        method: 'POST',
        body: JSON.stringify({ name: newRoleName, permissions: '0' })
      });
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) {
        setGuildRoles([
          ...(guild.roles ?? []).map((existing) =>
            existing.id === guild?.id ? existing : { ...existing, position: existing.position + 1 }
          ),
          role
        ]);
      }
      newRoleName = '';
      selectRole(role, true);
      notice = $t('ui_role_created_configure_its_permissions_before_9ad7259d');
    });
  }

  function saveRole() {
    if (!canManageSelectedRole || !selectedRole || !roleDirty) return;
    const target = selectedRole;
    return run(async (targetGuild, generation) => {
      const updated = await api<Role>(
        `/guilds/${encodeURIComponent(targetGuild)}/roles/${encodeURIComponent(entityRef(target))}`,
        {
          method: 'PATCH',
          headers: target.version ? { 'If-Match': `"${target.version}"` } : undefined,
          body: JSON.stringify({
            name: roleName,
            color: Number.parseInt(roleColor.slice(1), 16),
            permissions: rolePermissions,
            hoist: roleHoist,
            mentionable: roleMentionable
          })
        }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) {
        setGuildRoles(
          (guild.roles ?? []).map((role) =>
            entityKey(role) === entityKey(updated) ? updated : role
          )
        );
      }
      selectRole(updated, true);
      notice = $t('ui_role_saved_2ad7f1dd');
    });
  }

  async function uploadRoleIcon(file: File | null, input: HTMLInputElement) {
    const target = selectedRole;
    const signal = routeController?.signal;
    roleIconFile = file;
    roleIconError = '';
    if (!file || !target || !guild || !canManageSelectedRole || !signal) return;
    if (!acceptedImageTypes.has(file.type)) {
      roleIconError = $t('ui_choose_a_png_jpeg_gif_or_webp_image_4cef2205');
      return;
    }
    const maxBytes = guild.emoji_max_bytes ?? 262144;
    if (file.size > maxBytes) {
      roleIconError = `Role icons can be at most ${Math.ceil(maxBytes / 1024)} KiB.`;
      return;
    }
    roleIconBusy = true;
    error = '';
    notice = '';
    const path = `/guilds/${encodeURIComponent(guildId)}/roles/${encodeURIComponent(entityRef(target))}/icon`;
    try {
      const ticket = await api<UploadTicket>(path, {
        method: 'POST',
        body: JSON.stringify({ filename: file.name, content_type: file.type, size: file.size })
      });
      await uploadObject(ticket, file, () => undefined, signal);
      const updated = await completeScannedMediaResource(
        () =>
          api<Role | { scan_status: string }>(path, {
            method: 'PUT',
            body: JSON.stringify({ attachment_id: ticket.id })
          }),
        (value): value is Role => 'guild_id' in value,
        { signal, maxAttempts: 30, rejectedMessage: 'The role icon did not pass media processing.' }
      );
      if (guild) {
        setGuildRoles(
          (guild.roles ?? []).map((role) =>
            entityKey(role) === entityKey(updated) ? updated : role
          )
        );
      }
      selectRole(updated, true);
      notice = $t('ui_role_icon_updated_dfc1eff6');
    } catch (caught) {
      roleIconError = userErrorMessage(
        caught,
        $t('ui_could_not_update_the_role_icon_try_again_4f7c8eed')
      );
    } finally {
      roleIconBusy = false;
      roleIconFile = null;
      input.value = '';
    }
  }

  async function deleteRoleIcon() {
    const target = selectedRole;
    if (!target || !guild || !canManageSelectedRole || roleIconBusy) return;
    roleIconBusy = true;
    roleIconError = '';
    const path = `/guilds/${encodeURIComponent(guildId)}/roles/${encodeURIComponent(entityRef(target))}/icon`;
    try {
      const updated = await api<Role>(path, { method: 'DELETE' });
      setGuildRoles(
        (guild.roles ?? []).map((role) => (entityKey(role) === entityKey(updated) ? updated : role))
      );
      selectRole(updated, true);
      notice = $t('ui_role_icon_removed_755aaf54');
    } catch (caught) {
      roleIconError = userErrorMessage(
        caught,
        $t('ui_could_not_remove_the_role_icon_try_again_4a97e877')
      );
    } finally {
      roleIconBusy = false;
    }
  }

  function orderedRoles(): Role[] {
    return [...(guild?.roles ?? [])]
      .filter((role) => role.id !== guild?.id)
      .sort((left, right) => compareRoleRank(right, left));
  }

  function roleDragStart(event: DragEvent, role: Role) {
    if (!canReorderRole(role) || busy || reorderingRoles) {
      event.preventDefault();
      return;
    }
    draggedRoleKey = entityKey(role);
    event.dataTransfer?.setData('application/x-kaede-role', draggedRoleKey);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
  }

  function roleDragOver(event: DragEvent, role: Role) {
    if (!draggedRoleKey || busy || reorderingRoles || role.id === guild?.id) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
    roleDropKey = entityKey(role);
  }

  function roleDragEnd() {
    draggedRoleKey = null;
    roleDropKey = null;
  }

  async function roleDrop(event: DragEvent, target: Role) {
    if (!guild || !draggedRoleKey || busy || reorderingRoles || target.id === guild.id) return;
    event.preventDefault();
    const previous = guild.roles ?? [];
    const ordered = orderedRoles();
    const sourceIndex = ordered.findIndex((role) => entityKey(role) === draggedRoleKey);
    const targetIndex = ordered.findIndex((role) => entityKey(role) === entityKey(target));
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) {
      roleDragEnd();
      return;
    }
    const [moved] = ordered.splice(sourceIndex, 1);
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const after = event.clientY > bounds.top + bounds.height / 2;
    let insertion = targetIndex + (after ? 1 : 0);
    if (sourceIndex < targetIndex) insertion -= 1;
    ordered.splice(Math.max(0, insertion), 0, moved);
    const positioned = ordered.map((role, index) => ({
      ...role,
      position: ordered.length - index
    }));
    const previousByKey = new Map(previous.map((role) => [entityKey(role), role]));
    const changed = positioned.filter(
      (role) => previousByKey.get(entityKey(role))?.position !== role.position
    );
    if (!changed.length || changed.some((role) => !canReorderRole(role))) {
      roleDragEnd();
      error = $t('ui_you_can_only_reorder_roles_below_your_highest_7b1f277f');
      return;
    }
    roleDragEnd();
    await persistRoleOrder(previous, positioned);
  }

  async function persistRoleOrder(previous: Role[], ordered: Role[]) {
    if (!guild || busy || reorderingRoles) return;
    if (ordered.some((role) => !role.version)) {
      error = $t('ui_role_versions_are_unavailable_reload_settings_02ef4b23');
      return;
    }
    const targetGuild = guildId;
    const generation = loadGeneration;
    const defaultRole = previous.find((role) => role.id === guild?.id);
    setGuildRoles(defaultRole ? [defaultRole, ...ordered] : ordered);
    busy = true;
    reorderingRoles = true;
    error = '';
    notice = $t('ui_saving_role_order_926d7045');
    try {
      const updated = await api<Role[]>(`/guilds/${encodeURIComponent(targetGuild)}/roles`, {
        method: 'PATCH',
        body: JSON.stringify({
          roles: ordered.map((role) => ({
            id: role.id,
            position: role.position,
            version: role.version
          }))
        })
      });
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      const savedByKey = new Map(updated.map((role) => [entityKey(role), role]));
      setGuildRoles((guild.roles ?? []).map((role) => savedByKey.get(entityKey(role)) ?? role));
      if (selectedRole) {
        const selected = guild.roles?.find((role) => entityKey(role) === entityKey(selectedRole!));
        if (selected) selectRole(selected, true);
      }
      notice = $t('ui_role_order_saved_b13d4fbf');
    } catch (caught) {
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      setGuildRoles(previous);
      error = userErrorMessage(
        caught,
        $t('ui_could_not_save_the_role_order_reload_and_try__f5db06ed')
      );
      notice = '';
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) {
        busy = false;
        reorderingRoles = false;
      }
    }
  }

  function deleteRole() {
    if (!canManageSelectedRole || !selectedRole || !guild || selectedRole.id === guild.id) return;
    const target = selectedRole;
    void openDestructiveConfirmation({
      kind: 'role',
      target,
      title: $t('ui_delete_role_de10c0cf'),
      description: `“${target.name}” will be permanently removed. Members assigned to it will immediately lose its permissions.`,
      confirmLabel: 'Delete role'
    });
  }

  function deleteConfirmedRole(target: Role) {
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/roles/${encodeURIComponent(entityRef(target))}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      const remaining = (guild.roles ?? []).filter((role) => entityKey(role) !== entityKey(target));
      setGuildRoles(remaining);
      selectedRole = remaining.find((role) => role.id !== guild?.id) ?? remaining[0] ?? null;
      if (selectedRole) selectRole(selectedRole, true);
      members = cacheMemberRows(
        members.map((member) => ({
          ...member,
          role_ids: member.role_ids.filter((id) => id !== target.id)
        }))
      );
      notice = $t('ui_role_deleted_fb555aa3');
    });
  }

  function createInvite() {
    if (!(channelOnly ? canCreateSelectedInvite : canCreateInvites)) return;
    return run(async (targetGuild, generation) => {
      const channel =
        channelOnly && selectedChannel?.type !== 4
          ? selectedChannel
          : guild?.channels?.find(
              (item) =>
                entityKey(item) === inviteChannel &&
                item.type !== 4 &&
                channelHasPermission(item, Permission.CREATE_INVITE)
            );
      if ((channelOnly || inviteChannel) && !channel) {
        error = $t('ui_choose_a_channel_where_you_can_create_invites_7ee57c3e');
        return;
      }
      const invite = await api<InviteSummary>(
        `/guilds/${encodeURIComponent(targetGuild)}/invites`,
        {
          method: 'POST',
          body: JSON.stringify({
            channel_id: channel ? entityRef(channel) : null,
            max_age_seconds: inviteMaxAge ? Number(inviteMaxAge) : null,
            max_uses: inviteMaxUses ? Number(inviteMaxUses) : null,
            temporary: inviteTemporary,
            unique: inviteUnique,
            target_type: inviteTargetType || null,
            target_user_id: inviteTargetType === 'stream' ? inviteTargetUser || null : null,
            scheduled_event_id: inviteScheduledEvent || null,
            role_ids: inviteRoleIds
          })
        }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      invites = [invite, ...invites];
      createdInvite = invite;
      notice = $t('ui_invite_created_copy_the_link_below_before_lea_1fc85cfd');
    });
  }

  function createChannelWebhook() {
    if (!canManageSelectedWebhooks || !selectedChannel || selectedChannel.type === 4) return;
    const channel = selectedChannel;
    return run(async (targetGuild, generation) => {
      const created = await createGuildWebhook(targetGuild, entityRef(channel), newWebhookName);
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      webhooks = [...webhooks, created];
      webhookNameDrafts = { ...webhookNameDrafts, [created.id]: created.name };
      webhookChannelDrafts = {
        ...webhookChannelDrafts,
        [created.id]: `${created.channel_id}@${created.channel_domain}`
      };
      newWebhookName = '';
      revealedWebhookToken = created.execution_url ?? '';
      notice = $t('ui_webhook_created_its_url_remains_available_to__15821689');
    });
  }

  function rotateWebhook(webhook: WebhookSummary) {
    if (!canManageWebhook(webhook)) return;
    if (
      !confirm(
        `Rotate the token for “${webhook.name}”? The current token will stop working immediately.`
      )
    )
      return;
    return run(async (targetGuild, generation) => {
      const updated = await rotateGuildWebhook(targetGuild, webhook);
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === webhook.id ? updated : item));
      revealedWebhookToken = updated.execution_url ?? '';
      notice = $t('ui_webhook_token_rotated_the_previous_token_no_l_b93ec265');
    });
  }

  function webhookDirty(webhook: WebhookSummary) {
    return (
      (webhookNameDrafts[webhook.id] ?? webhook.name).trim() !== webhook.name ||
      (webhookChannelDrafts[webhook.id] ?? `${webhook.channel_id}@${webhook.channel_domain}`) !==
        `${webhook.channel_id}@${webhook.channel_domain}`
    );
  }

  function updateWebhook(webhook: WebhookSummary) {
    if (!webhookDirty(webhook)) return;
    if (!canManageWebhook(webhook)) return;
    const nextName = (webhookNameDrafts[webhook.id] ?? webhook.name).trim();
    if (!nextName) {
      error = $t('ui_webhook_names_cannot_be_blank_7a077d9b');
      return;
    }
    const nextChannel =
      webhookChannelDrafts[webhook.id] ?? `${webhook.channel_id}@${webhook.channel_domain}`;
    const targetChannel = manageableWebhookTargets.find(
      (channel) => entityRef(channel) === nextChannel
    );
    if (!targetChannel) {
      error = $t('ui_choose_a_text_announcement_or_forum_channel_f_9671552f');
      return;
    }
    return run(async (targetGuild, generation) => {
      const updated = await updateGuildWebhook(targetGuild, webhook, {
        name: nextName,
        channel_id: entityRef(targetChannel)
      });
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === webhook.id ? updated : item));
      webhookNameDrafts = { ...webhookNameDrafts, [updated.id]: updated.name };
      webhookChannelDrafts = {
        ...webhookChannelDrafts,
        [updated.id]: `${updated.channel_id}@${updated.channel_domain}`
      };
      notice = $t('ui_webhook_saved_11589616');
    });
  }

  function uploadWebhookAvatar(
    webhook: WebhookSummary,
    file: File | null,
    input: HTMLInputElement
  ) {
    if (!file || !canManageWebhook(webhook)) return;
    if (!acceptedImageTypes.has(file.type)) {
      error = $t('ui_choose_a_png_jpeg_gif_or_webp_image_4cef2205');
      input.value = '';
      return;
    }
    if (!file.size) {
      error = $t('ui_choose_a_non_empty_image_file_b81ea05c');
      input.value = '';
      return;
    }
    return run(async (targetGuild, generation) => {
      const signal = routeController?.signal;
      if (!signal) return;
      const ticket = await createGuildWebhookAvatarTicket(
        targetGuild,
        webhook,
        {
          filename: file.name || 'webhook-avatar',
          content_type: file.type,
          size: file.size
        },
        signal
      );
      await uploadObject(ticket, file, () => undefined, signal);
      let updated: WebhookSummary | null = null;
      for (let attempt = 0; attempt < 45; attempt += 1) {
        const result = await commitGuildWebhookAvatar(targetGuild, webhook, ticket.id, signal);
        if ('guild_id' in result) {
          updated = result;
          break;
        }
        const scanStatus = result.attachment?.scan_status ?? 'pending';
        if (['infected', 'rejected', 'failed'].includes(scanStatus)) {
          throw new Error($t('ui_the_webhook_avatar_did_not_pass_media_safety__f45f1a3f'));
        }
        await cancelableDelay(1000, signal);
      }
      if (!updated) {
        throw new Error($t('ui_webhook_avatar_processing_is_taking_longer_th_74fad3a7'));
      }
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === updated?.id ? updated : item));
      input.value = '';
      notice = $t('ui_webhook_avatar_updated_a4a0a1f5');
    });
  }

  function deleteWebhookAvatar(webhook: WebhookSummary) {
    if (!canManageWebhook(webhook) || !webhook.avatar_hash) return;
    if (!confirm(`Remove the avatar for “${webhook.name}”?`)) return;
    return run(async (targetGuild, generation) => {
      const updated = await deleteGuildWebhookAvatar(targetGuild, webhook);
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === updated.id ? updated : item));
      notice = $t('ui_webhook_avatar_removed_607e6eae');
    });
  }

  function deleteWebhook(webhook: WebhookSummary) {
    if (!canManageWebhook(webhook)) return;
    if (!confirm(`Delete the webhook “${webhook.name}”? This cannot be undone.`)) return;
    return run(async (targetGuild, generation) => {
      await deleteGuildWebhook(targetGuild, webhook);
      if (generation !== loadGeneration) return;
      webhooks = webhooks.filter((item) => item.id !== webhook.id);
      const remainingNames = { ...webhookNameDrafts };
      delete remainingNames[webhook.id];
      webhookNameDrafts = remainingNames;
      const remainingChannels = { ...webhookChannelDrafts };
      delete remainingChannels[webhook.id];
      webhookChannelDrafts = remainingChannels;
      revealedWebhookToken = '';
      notice = $t('ui_webhook_deleted_425f0b97');
    });
  }

  function revokeInvite(invite: InviteSummary) {
    if (!canRevokeInvite(invite)) return;
    void openDestructiveConfirmation({
      kind: 'invite',
      target: invite,
      title: $t('ui_revoke_invite_5bb1d75b'),
      description: `Invite ${invite.code} will stop working immediately. People who already joined the guild will not be affected.`,
      confirmLabel: 'Revoke invite'
    });
  }

  function canRevokeInvite(invite: InviteSummary): boolean {
    if (canManageGuild) return true;
    if (!invite.channel_id) return false;
    const inviteChannel = guild?.channels?.find((channel) => channel.id === invite.channel_id);
    if (!inviteChannel) return false;
    try {
      return hasAllPermissions(
        BigInt(inviteChannel.permissions ?? guild?.permissions ?? '0'),
        Permission.MANAGE_CHANNELS
      );
    } catch {
      return false;
    }
  }

  function revokeConfirmedInvite(invite: InviteSummary) {
    return run(async (targetGuild, generation) => {
      if (!guild) return;
      await api(guildInviteManagementPath(invite.code, guild.origin_domain, targetGuild), {
        method: 'DELETE'
      });
      if (generation !== loadGeneration) return;
      invites = invites.filter((item) => item.code !== invite.code);
      notice = $t('ui_invite_revoked_48fd740c');
    });
  }

  async function confirmDestructiveAction() {
    const confirmation = destructiveConfirmation;
    if (!confirmation || busy) return;
    let succeeded = false;
    if (confirmation.kind === 'channel') {
      succeeded = await deleteConfirmedChannel(confirmation.target);
    } else if (confirmation.kind === 'role') {
      succeeded = await deleteConfirmedRole(confirmation.target);
    } else if (confirmation.kind === 'invite') {
      succeeded = await revokeConfirmedInvite(confirmation.target);
    } else if (confirmation.kind === 'instance-ban') {
      succeeded = await banConfirmedFederatedInstance(
        confirmation.domain,
        confirmation.reason,
        confirmation.expiresAt
      );
    } else if (confirmation.kind === 'guild-leave') {
      succeeded = await leaveConfirmedGuild();
    } else if (confirmation.kind === 'guild-transfer') {
      succeeded = await transferConfirmedGuild(confirmation.target);
    } else {
      if (confirmationVerification !== confirmation.verificationText) return;
      succeeded = await deleteConfirmedGuild();
    }
    if (succeeded) closeDestructiveConfirmation();
  }

  function destructiveBusyLabel(confirmation: DestructiveConfirmation): string {
    if (confirmation.kind === 'invite') return 'Revoking…';
    if (confirmation.kind === 'instance-ban') return 'Banning…';
    if (confirmation.kind === 'guild-leave') return 'Leaving…';
    if (confirmation.kind === 'guild-transfer') return 'Transferring…';
    return 'Deleting…';
  }

  function requestLeaveGuild() {
    if (!guild || isGuildOwner) return;
    void openDestructiveConfirmation({
      kind: 'guild-leave',
      title: `Leave ${guild.name}?`,
      description: $t('ui_you_will_lose_access_to_this_guild_and_its_ca_7fbdc849'),
      confirmLabel: 'Leave guild'
    });
  }

  function requestOwnershipTransfer() {
    const user =
      ownershipTargetUser ??
      ownershipCandidates.find((candidate) => entityRef(candidate.user) === ownershipTarget)?.user;
    if (!guild || !isGuildOwner || !user) return;
    const targetName = userDisplayName(user);
    void openDestructiveConfirmation({
      kind: 'guild-transfer',
      target: user,
      title: `Transfer ownership to ${targetName}?`,
      description: $t('ui_they_will_become_the_guild_owner_immediately__e1478b2f'),
      confirmLabel: 'Transfer ownership'
    });
  }

  function requestDeleteGuild() {
    if (!guild || !isGuildOwner) return;
    void openDestructiveConfirmation({
      kind: 'guild-delete',
      verificationText: guild.name,
      title: `Delete ${guild.name}?`,
      description: $t('ui_this_permanently_removes_the_guild_its_channe_29a4bdc2'),
      confirmLabel: 'Delete guild'
    });
  }

  function leaveConfirmedGuild() {
    return run(async (targetGuild) => {
      await api(`/guilds/${encodeURIComponent(targetGuild)}/members/@me`, { method: 'DELETE' });
      window.location.assign(resolve('/home'));
    });
  }

  function transferConfirmedGuild(user: UserSummary) {
    return run(async (targetGuild, generation) => {
      if (!guild?.version) throw new Error($t('ui_guild_version_is_unavailable_3653be0a'));
      const updated = await api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}/owner`, {
        method: 'PUT',
        headers: { 'If-Match': guild.version },
        body: JSON.stringify({ owner_id: entityRef(user) })
      });
      if (generation !== loadGeneration) return;
      guild = { ...guild!, ...updated };
      ownershipTarget = '';
      ownershipTargetUser = null;
      notice = `Ownership transferred to ${userDisplayName(user)}.`;
    });
  }

  function deleteConfirmedGuild() {
    return run(async (targetGuild) => {
      if (!guild?.version) throw new Error($t('ui_guild_version_is_unavailable_3653be0a'));
      await api(`/guilds/${encodeURIComponent(targetGuild)}`, {
        method: 'DELETE',
        headers: { 'If-Match': guild.version }
      });
      window.location.assign(resolve('/home'));
    });
  }

  async function copyInvite(invite: InviteSummary) {
    error = '';
    try {
      await navigator.clipboard.writeText(inviteUrl(invite.code));
      notice = $t('ui_invite_link_copied_d65176d6');
    } catch {
      error = $t('ui_browser_denied_clipboard_access_allow_clipboa_1319db32');
    }
  }

  async function copyWebhookUrl(url = revealedWebhookToken) {
    if (!url) return;
    error = '';
    try {
      await navigator.clipboard.writeText(url);
      notice = $t('ui_webhook_url_copied_d08595e0');
    } catch {
      error = $t('ui_browser_denied_clipboard_access_select_the_we_9a5c5b57');
    }
  }

  function inviteUrl(code: string): string {
    if (!guild) return '';
    const currentOrigin = typeof window === 'undefined' ? undefined : window.location.origin;
    return guildInviteUrl(code, guild.origin_domain, currentOrigin);
  }

  function toggleMemberRole(member: MemberSummary, role: Role, enabled: boolean) {
    const currentRole = guild?.roles?.find((candidate) => entityKey(candidate) === entityKey(role));
    const currentMember = currentMembers.find(
      (candidate) => entityKey(candidate.user) === entityKey(member.user)
    );
    if (
      !currentRole ||
      !currentMember ||
      !canManageRole(currentRole) ||
      !canManageMember(currentMember)
    )
      return;
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/members/${encodeURIComponent(entityRef(currentMember.user))}/roles/${encodeURIComponent(entityRef(currentRole))}`,
        { method: enabled ? 'PUT' : 'DELETE' }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      const updatedMember = {
        ...currentMember,
        role_ids: enabled
          ? [...new Set([...currentMember.role_ids, currentRole.id])]
          : currentMember.role_ids.filter((id) => id !== currentRole.id)
      };
      entities.members.upsert(updatedMember);
      members = members.map((item) =>
        entityKey(item.user) === entityKey(currentMember.user) ? updatedMember : item
      );
      notice = `${currentRole.name} ${enabled ? 'assigned' : 'removed'}.`;
    });
  }

  function expiryFor(duration: string): string | null {
    if (duration === 'permanent') return null;
    return new Date(Date.now() + Number(duration) * 1000).toISOString();
  }

  function isModeratableMember(member: MemberSummary): boolean {
    return guildMemberOutranks(guild, signedInUser, member.user, currentMembers);
  }

  function memberModerationTitle(dialog: MemberModerationDialog): string {
    const name = dialog.member.nickname ?? userDisplayName(dialog.member.user);
    if (dialog.action === 'untimeout') return `Remove ${name}'s timeout?`;
    return `${dialog.action.slice(0, 1).toUpperCase()}${dialog.action.slice(1)} ${name}?`;
  }

  function memberModerationDescription(dialog: MemberModerationDialog): string {
    if (dialog.action === 'timeout')
      return 'They will be unable to send messages, react, speak, or use other interactive guild features until the timeout ends.';
    if (dialog.action === 'untimeout')
      return 'They will immediately regain the actions allowed by their roles and channel permissions.';
    if (dialog.action === 'kick')
      return 'They will be removed immediately, but may return using another valid invite.';
    return 'They will be removed and unable to rejoin until this ban expires or is removed.';
  }

  async function openMemberModeration(
    member: MemberSummary,
    action: MemberModerationAction,
    invoker: HTMLElement
  ) {
    if (!isModeratableMember(member) || memberModerationBusy) return;
    if (action === 'timeout' || action === 'untimeout') {
      if (!canTimeoutMembers) return;
    } else if (action === 'kick') {
      if (!canKickMembers) return;
    } else if (!canBanMembers) return;
    memberModerationPreviousFocus = invoker;
    moderationReason = '';
    timeoutDuration = '3600';
    banDuration = 'permanent';
    banDeleteSeconds = '0';
    error = '';
    notice = '';
    memberModerationDialog = { action, member };
    await tick();
    memberModerationCancel?.focus();
  }

  function closeMemberModeration() {
    const previousFocus = memberModerationPreviousFocus;
    memberModerationGeneration += 1;
    memberModerationController?.abort();
    memberModerationController = null;
    memberModerationBusy = false;
    memberModerationDialog = null;
    memberModerationElement = null;
    memberModerationCancel = null;
    moderationReason = '';
    error = '';
    memberModerationPreviousFocus = null;
    void tick().then(() => {
      if (previousFocus?.isConnected) previousFocus.focus();
    });
  }

  function cancelMemberModeration(event: Event) {
    event.preventDefault();
    event.stopPropagation();
    closeMemberModeration();
  }

  function memberModerationKeydown(event: KeyboardEvent) {
    if (!memberModerationDialog || !memberModerationElement) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMemberModeration();
      return;
    }
    if (event.key !== 'Tab') return;
    trapDialogFocus(
      event,
      memberModerationElement,
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
  }

  async function submitMemberModeration() {
    const dialog = memberModerationDialog;
    if (!dialog || memberModerationBusy) return;
    const targetGuild = guildId;
    const routeGeneration = loadGeneration;
    const requestGeneration = ++memberModerationGeneration;
    const controller = new AbortController();
    memberModerationController = controller;
    memberModerationBusy = true;
    error = '';
    notice = '';
    const reason = moderationReason.trim();
    const headers = reason ? { 'X-Audit-Log-Reason': reason } : undefined;
    const memberPath = `/guilds/${encodeURIComponent(targetGuild)}/members/${encodeURIComponent(entityRef(dialog.member.user))}`;
    const stillCurrent = () =>
      requestGeneration === memberModerationGeneration &&
      routeGeneration === loadGeneration &&
      targetGuild === guildId;
    try {
      if (dialog.action === 'timeout' || dialog.action === 'untimeout') {
        const indefinite = dialog.action === 'timeout' && timeoutDuration === 'permanent';
        const updated = await api<MemberSummary>(memberPath, {
          method: 'PATCH',
          headers,
          signal: controller.signal,
          body: JSON.stringify({
            timeout_until:
              dialog.action === 'timeout' && !indefinite ? expiryFor(timeoutDuration) : null,
            timeout_indefinite: indefinite
          })
        });
        if (!stillCurrent()) return;
        entities.members.upsert(updated);
        members = members.map((item) =>
          entityKey(item.user) === entityKey(dialog.member.user) ? updated : item
        );
        notice =
          dialog.action === 'timeout'
            ? `${userDisplayName(dialog.member.user)} was timed out${indefinite ? ' indefinitely' : ''}.`
            : `Timeout removed for ${userDisplayName(dialog.member.user)}.`;
      } else if (dialog.action === 'kick') {
        await api(memberPath, { method: 'DELETE', headers, signal: controller.signal });
        if (!stillCurrent()) return;
        removeCachedMember(dialog.member);
        members = members.filter((item) => entityKey(item.user) !== entityKey(dialog.member.user));
        notice = `${userDisplayName(dialog.member.user)} was kicked.`;
      } else {
        const expiresAt = expiryFor(banDuration);
        await api(
          `/guilds/${encodeURIComponent(targetGuild)}/bans/${encodeURIComponent(entityRef(dialog.member.user))}`,
          {
            method: 'PUT',
            headers,
            signal: controller.signal,
            body: JSON.stringify({
              reason: reason || null,
              expires_at: expiresAt,
              delete_message_seconds: Number(banDeleteSeconds)
            })
          }
        );
        if (!stillCurrent()) return;
        removeCachedMember(dialog.member);
        members = members.filter((item) => entityKey(item.user) !== entityKey(dialog.member.user));
        bans = [
          {
            user: dialog.member.user,
            reason: reason || null,
            created_at: new Date().toISOString(),
            expires_at: expiresAt
          },
          ...bans.filter((item) => entityKey(item.user) !== entityKey(dialog.member.user))
        ];
        notice = `${userDisplayName(dialog.member.user)} was banned.`;
      }
      if (stillCurrent()) closeMemberModeration();
    } catch (caught) {
      if (!stillCurrent() || controller.signal.aborted) return;
      error = userErrorMessage(
        caught,
        $t('ui_the_moderation_action_could_not_be_applied_tr_f65acfb2')
      );
    } finally {
      if (requestGeneration === memberModerationGeneration) {
        memberModerationController = null;
        memberModerationBusy = false;
      }
    }
  }

  function unbanUser(ban: BanSummary) {
    if (!canBanMembers) return;
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/bans/${encodeURIComponent(entityRef(ban.user))}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration) return;
      bans = bans.filter((item) => entityKey(item.user) !== entityKey(ban.user));
      notice = `${userDisplayName(ban.user)} was unbanned.`;
    });
  }

  function banFederatedInstance() {
    if (!canBanInstances || !instanceBanDomain.trim()) return;
    const domain = instanceBanDomain.trim().toLowerCase().replace(/\.$/, '');
    const expiresAt = expiryFor(instanceBanDuration);
    void openDestructiveConfirmation({
      kind: 'instance-ban',
      domain,
      reason: instanceBanReason,
      expiresAt,
      title: `Ban everyone from ${domain}?`,
      description: `Every current member homed on ${domain} will be removed and that instance cannot add members${expiresAt ? ` until ${formatDateTime(expiresAt)}` : $t('ui_until_this_ban_is_removed_7c73a1a3')}. Its server will be asked to erase cached guild data, but a malicious, offline, or modified server may retain copies.`,
      confirmLabel: 'Ban instance'
    });
  }

  function banConfirmedFederatedInstance(domain: string, reason: string, expiresAt: string | null) {
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/instance-bans/${encodeURIComponent(domain)}`,
        {
          method: 'PUT',
          headers: reason ? { 'X-Audit-Log-Reason': reason } : undefined,
          body: JSON.stringify({ reason: reason || null, expires_at: expiresAt })
        }
      );
      if (generation !== loadGeneration) return;
      for (const member of entities.members.values) {
        if (
          member.guild_id === guild?.id &&
          member.guild_domain === guild?.origin_domain &&
          member.user.origin_domain === domain
        ) {
          removeCachedMember(member);
        }
      }
      members = members.filter((member) => member.user.origin_domain !== domain);
      instanceBans = [
        {
          instance_domain: domain,
          reason: reason || null,
          created_at: new Date().toISOString(),
          expires_at: expiresAt
        },
        ...instanceBans.filter((item) => item.instance_domain !== domain)
      ];
      instanceBanDomain = '';
      instanceBanReason = '';
      notice = `${domain} was banned from this guild.`;
    });
  }

  function unbanFederatedInstance(ban: InstanceBanSummary) {
    if (!canBanInstances) return;
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/instance-bans/${encodeURIComponent(ban.instance_domain)}`,
        { method: 'DELETE' }
      );
      if (generation !== loadGeneration) return;
      instanceBans = instanceBans.filter((item) => item.instance_domain !== ban.instance_domain);
      notice = `${ban.instance_domain} may join this guild again.`;
    });
  }

  async function loadMoreMembers(): Promise<boolean> {
    if (membersLoadingMore || !membersHaveMore || !members.length) return false;
    const targetGuild = guildId;
    const generation = loadGeneration;
    const after = entityRef(members[members.length - 1].user);
    membersLoadingMore = true;
    try {
      const page = await api<MemberSummary[]>(
        `/guilds/${encodeURIComponent(targetGuild)}/members?limit=101&after=${encodeURIComponent(after)}`
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return false;
      const next = cacheMemberRows(page.slice(0, 100));
      const existing = new Set(members.map((member) => entityKey(member.user)));
      members = [...members, ...next.filter((member) => !existing.has(entityKey(member.user)))];
      membersHaveMore = page.length > 100;
      return next.length > 0;
    } catch (caught) {
      if (generation === loadGeneration && targetGuild === guildId) {
        error = userErrorMessage(caught, $t('ui_could_not_load_more_members_try_again_e2763fe0'));
      }
      return false;
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) membersLoadingMore = false;
    }
  }

  $effect(() => {
    const search = roleMemberSearch.trim();
    const targetGuild = guildId;
    if (!search) {
      roleMemberSearchResults = [];
      roleMemberSearchBusy = false;
      roleMemberSearchError = '';
      return;
    }
    const controller = new AbortController();
    roleMemberSearchBusy = true;
    roleMemberSearchError = '';
    const timeout = window.setTimeout(() => {
      void api<MemberSummary[]>(
        `/guilds/${encodeURIComponent(targetGuild)}/members?limit=100&query=${encodeURIComponent(search)}`,
        { signal: controller.signal }
      )
        .then((results) => {
          if (controller.signal.aborted || targetGuild !== guildId) return;
          roleMemberSearchResults = cacheMemberRows(results);
        })
        .catch((caught: unknown) => {
          if (controller.signal.aborted || targetGuild !== guildId) return;
          roleMemberSearchResults = [];
          roleMemberSearchError = userErrorMessage(
            caught,
            $t('ui_could_not_search_guild_members_try_again_0b092cee')
          );
        })
        .finally(() => {
          if (!controller.signal.aborted && targetGuild === guildId) roleMemberSearchBusy = false;
        });
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  });

  $effect(() => {
    const targetGuild = guildId;
    const targetChannel = channelOnly ? channelId : '';
    const requestedPanel = channelOnly ? page.url.searchParams.get('panel') : null;
    const generation = ++loadGeneration;
    const controller = new AbortController();
    routeController = controller;
    guild = null;
    localDomain = '';
    members = [];
    membersHaveMore = false;
    membersLoadingMore = false;
    roleMemberSearch = '';
    bans = [];
    instanceBans = [];
    memberModerationGeneration += 1;
    memberModerationController?.abort();
    memberModerationController = null;
    memberModerationDialog = null;
    memberModerationBusy = false;
    memberModerationElement = null;
    memberModerationCancel = null;
    memberModerationPreviousFocus = null;
    invites = [];
    createdInvite = null;
    selectedChannel = null;
    selectedRole = null;
    guildAssetKind = null;
    guildAssetStage = null;
    guildAssetProgress = 0;
    guildAssetError = '';
    destructiveConfirmation = null;
    confirmationPreviousFocus = null;
    error = '';
    notice = '';
    busy = false;
    void load(targetGuild, targetChannel, requestedPanel, generation, controller.signal);
    return () => {
      controller.abort();
      if (routeController === controller) routeController = null;
    };
  });
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- channelPath resolves the typed route before insertion -->

<svelte:head>
  <title
    >{$t('ui_value0_kaede_chat_4bf52868', {
      value0: String(
        channelOnly
          ? `${selectedChannel?.name ?? 'Channel'} settings`
          : `${guild?.name ?? 'Guild'} settings`
      )
    })}</title
  >
</svelte:head>

<main class="settings-page guild-settings-page" class:channel-settings-page={channelOnly}>
  <aside class="settings-nav">
    {#if channelOnly}
      <a class="settings-back" href={donePath()}>
        <Icon name="arrow-left" size={18} />
        <span>{$t('ui_back_to_channel_5f24d648')}</span>
      </a>
      <div class="channel-settings-identity">
        <span
          >{selectedChannel?.type === 4
            ? $t('ui_category_292c06f0')
            : $t('ui_channel_ce4683e7')}</span
        >
        <strong>
          {#if selectedChannel?.type !== 4}<Icon
              name={selectedChannel?.type === 2
                ? 'volume'
                : selectedChannel?.type === TRACKER_CHANNEL_TYPE
                  ? 'kanban'
                  : selectedChannel?.type === 15
                    ? 'forum'
                    : 'hash'}
              size={16}
            />{/if}
          {selectedChannel?.name ?? $t('ui_loading_ba3bbbe1')}
        </strong>
      </div>
      <nav aria-label={$t('ui_channel_settings_sections_e3fa6383')}>
        {#if canEditSelectedChannel}
          <button
            class:active={channelEditorPanel === 'overview'}
            type="button"
            onclick={() => selectChannelPanel('overview')}>{$t('ui_overview_d4b1ea57')}</button
          >
        {/if}
        {#if canEditSelectedPermissions}
          <button
            class:active={channelEditorPanel === 'permissions'}
            type="button"
            onclick={() => selectChannelPanel('permissions')}
            >{$t('ui_permissions_abccc78c')}</button
          >
        {/if}
        {#if (canCreateSelectedInvite || canEditSelectedChannel) && selectedChannel?.type !== 4}
          <button
            class:active={channelEditorPanel === 'invites'}
            type="button"
            onclick={() => selectChannelPanel('invites')}>{$t('ui_invites_f212a985')}</button
          >
        {/if}
        {#if canAccessSelectedIntegrations && [0, 5, 15].includes(selectedChannel?.type ?? -1)}
          <button
            class:active={channelEditorPanel === 'integrations'}
            type="button"
            onclick={() => selectChannelPanel('integrations')}
            >{$t('ui_integrations_090512d9')}</button
          >
        {/if}
        {#if canEditSelectedChannel}
          <span class="channel-settings-divider"></span>
          <button
            class="danger-nav-item"
            class:active={channelEditorPanel === 'delete'}
            type="button"
            onclick={() => selectChannelPanel('delete')}
            >{$t('ui_delete_value0_8782b778', {
              value0: String(selectedChannel?.type === 4 ? 'Category' : 'Channel')
            })}</button
          >
        {/if}
      </nav>
      <span class="settings-instance-label">
        {selectedChannel ? `${selectedChannel.id}@${selectedChannel.origin_domain}` : ''}
      </span>
    {:else}
      <a class="settings-back" href={donePath()}>
        <Icon name="arrow-left" size={18} />
        <span>{$t('ui_back_to_guild_676f45d1')}</span>
      </a>
      <div class="settings-account-mini">
        <span class="avatar avatar-small guild-avatar">
          {#if guild?.icon_hash}
            <img src={assetUrl(guild.icon_hash, 'thumbnail_128', guild)} alt="" />
          {:else}
            {guild?.name.slice(0, 2).toUpperCase() ?? '—'}
          {/if}
        </span>
        <span>
          <strong>{guild?.name ?? $t('ui_loading_ba3bbbe1')}</strong>
          <small>{guild?.origin_domain ?? $t('ui_guild_settings_81f6b9fc')}</small>
        </span>
      </div>
      <nav aria-label={$t('ui_guild_settings_sections_7d9ac3a7')}>
        <p>{$t('ui_personal_845f9286')}</p>
        <a href="#notifications"><Icon name="bell" size={18} />{$t('ui_notifications_78801183')}</a>
        <p>{$t('ui_guild_298ffc49')}</p>
        <a href="#overview"><Icon name="server" size={18} />{$t('ui_overview_d4b1ea57')}</a>
        {#if canAccessGuildIntegrations && guild}
          <a href={guildIntegrationsPath(guild)}
            ><Icon name="server" size={18} />{$t('ui_integrations_090512d9')}</a
          >
        {/if}
        {#if canManageGuild && !channelOnly && guild}
          <a href={guildApplicationDirectoryPath(guild)}
            ><Icon name="sparkles" size={18} />{$t('ui_app_directory_a9c3e633')}</a
          >
        {/if}
        {#if canManageGuild && canManageRoles}<a href="#onboarding"
            ><Icon name="users" size={18} />Rules &amp; onboarding</a
          >{/if}
        {#if canManageRoles}
          <a href="#roles"><Icon name="shield" size={18} />{$t('ui_roles_c2533705')}</a>
        {/if}
        {#if canAccessExpressions}
          <a href="#emoji"><span aria-hidden="true">☺</span>{$t('ui_emoji_61ad8976')}</a>
          <a href="#stickers"><span aria-hidden="true">▱</span>{$t('ui_stickers_dbf9cbbe')}</a>
        {/if}
        {#if hasPermission(Permission.MANAGE_AUTO_MODERATION)}
          <a href="#automod"><Icon name="shield" size={18} />{$t('ui_automod_a3ed9717')}</a>
        {/if}
        {#if canKickMembers || canBanMembers}
          <a href="#bulk-moderation"
            ><Icon name="users" size={18} />{$t('ui_bulk_moderation_7b53e29a')}</a
          >
        {/if}
        {#if canAccessExpressions}
          <a href="#soundboard"><span aria-hidden="true">♫</span>{$t('ui_soundboard_07ff885c')}</a>
        {/if}
        {#if canViewAuditLog}
          <a href="#audit-log"><Icon name="clock" size={18} />{$t('ui_audit_log_e4d36f9a')}</a>
        {/if}
        {#if canViewMembers}
          <p>{$t('ui_community_bb501d78')}</p>
          <a href="#members"><Icon name="users" size={18} />{$t('ui_members_1044a4c0')}</a>
        {/if}
        {#if canAccessInvites}
          <a href="#invites"><Icon name="globe" size={18} />{$t('ui_invites_f212a985')}</a>
        {/if}
        <p>{$t('ui_membership_9feceb93')}</p>
        <a href="#guild-lifecycle"
          ><Icon name="logout" size={18} />{$t('ui_guild_access_dad929fc')}</a
        >
      </nav>
      <span class="settings-instance-label">
        {isLocalGuild
          ? $t('ui_managed_on_this_instance_c5d29403')
          : $t('ui_managed_by_its_home_instance_4dbd2cc4')}
      </span>
    {/if}
  </aside>

  <section class="settings-content">
    <header class="settings-page-heading">
      <div>
        <p class="eyebrow">
          {channelOnly
            ? (selectedChannel?.name ?? $t('ui_channel_ce4683e7'))
            : $t('ui_guild_administration_8f490092')}
        </p>
        <h1>
          {channelOnly
            ? channelPanelTitle(channelEditorPanel)
            : (guild?.name ?? $t('ui_settings_74a883a0'))}
        </h1>
        <p>
          {channelOnly
            ? channelPanelDescription(channelEditorPanel)
            : $t('ui_shape_the_spaces_roles_and_invitations_that_h_752d92c7')}
        </p>
      </div>
      <a
        class="icon-button settings-close"
        href={donePath()}
        aria-label={channelOnly
          ? $t('ui_close_channel_settings_c4f98234')
          : $t('ui_close_guild_settings_ca407616')}>×</a
      >
    </header>

    {#if error && !destructiveConfirmation}
      <div class="notice-banner error-banner" role="alert">{error}</div>
    {/if}
    <Toast message={notice} onDismiss={() => (notice = '')} />

    {#if loading}
      <div class="settings-loading" aria-label={$t('ui_loading_guild_settings_c0d32e73')}>
        <span></span><span></span><span></span>
      </div>
    {:else if !guild}
      <section class="empty-state">
        <span><Icon name="server" size={28} /></span>
        <h2>{$t('ui_guild_settings_are_unavailable_a1a7e63e')}</h2>
        <p>{$t('ui_return_to_kaede_and_try_opening_this_guild_ag_14a2e38e')}</p>
        <a class="primary-button" href={resolve('/home')}>{$t('ui_return_home_bbcc935e')}</a>
      </section>
    {:else}
      {#if !channelOnly}
        <section id="notifications" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="bell" /></span>
            <div>
              <h2>{$t('ui_notifications_78801183')}</h2>
              <p>{$t('ui_choose_which_messages_from_this_guild_can_sen_b6a7ec94')}</p>
            </div>
          </div>
          <div class="settings-card">
            <div class="toggle-list notification-level-list">
              <label
                class:selected={guildNotificationLevel === 'all'}
                class="toggle-row notification-level-row"
              >
                <span>
                  <strong>{$t('ui_all_messages_5eb8f655')}</strong>
                  <small>{$t('ui_notify_you_whenever_a_message_is_sent_in_a_ch_7b46fbb3')}</small>
                </span>
                <input
                  type="radio"
                  name="guild-notification-level"
                  value="all"
                  checked={guildNotificationLevel === 'all'}
                  disabled={busy}
                  onchange={() => void saveGuildNotificationLevel('all')}
                />
              </label>
              <label
                class:selected={guildNotificationLevel === 'mentions'}
                class="toggle-row notification-level-row"
              >
                <span>
                  <strong>{$t('ui_mentions_only_78732e1d')}</strong>
                  <small>{$t('ui_notify_you_only_when_a_message_directly_menti_902bdf7e')}</small>
                </span>
                <input
                  type="radio"
                  name="guild-notification-level"
                  value="mentions"
                  checked={guildNotificationLevel === 'mentions'}
                  disabled={busy}
                  onchange={() => void saveGuildNotificationLevel('mentions')}
                />
              </label>
              <label
                class:selected={guildNotificationLevel === 'none'}
                class="toggle-row notification-level-row"
              >
                <span>
                  <strong>{$t('ui_nothing_67e1bca0')}</strong>
                  <small>{$t('ui_do_not_send_notifications_for_messages_in_thi_dd8dc386')}</small>
                </span>
                <input
                  type="radio"
                  name="guild-notification-level"
                  value="none"
                  checked={guildNotificationLevel === 'none'}
                  disabled={busy}
                  onchange={() => void saveGuildNotificationLevel('none')}
                />
              </label>
            </div>
            <p class="settings-helper">
              {$t('ui_notifications_must_also_be_enabled_in_ea8e841d')}
              <a href={resolve('/settings#notifications')}>{$t('ui_user_settings_818ed0c8')}</a>{$t(
                'ui_the_preference_syncs_across_the_web_desktop_a_30f07ec6'
              )}
            </p>
          </div>
        </section>

        {#if guild && canManageGuild && canManageRoles}<GuildOnboarding {guild} admin />{/if}
        <section id="overview" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="server" /></span>
            <div>
              <h2>{$t('ui_overview_d4b1ea57')}</h2>
              <p>{$t('ui_the_public_name_and_description_people_see_wh_01358240')}</p>
            </div>
          </div>

          {#if !isLocalGuild}
            <div class="notice-banner info-banner">
              <Icon name="globe" size={18} />
              {$t('ui_this_guild_is_hosted_by_0b1ebe89')} <strong>{guild.origin_domain}</strong>{$t(
                'ui_changes_are_forwarded_there_and_your_current__1021c4b9'
              )}
            </div>
          {/if}

          <div class="profile-card">
            <div class="profile-banner">
              {#if guild.banner_hash}
                <img src={assetUrl(guild.banner_hash, 'original', guild)} alt="" />
              {:else}
                <span aria-hidden="true"></span>
              {/if}
            </div>
            <div class="profile-card-body">
              <span class="avatar avatar-large guild-avatar">
                {#if guild.icon_hash}
                  <img src={assetUrl(guild.icon_hash, 'thumbnail_128', guild)} alt="" />
                {:else}
                  {guild.name.slice(0, 2).toUpperCase()}
                {/if}
              </span>
              <div class="profile-identity">
                <strong>{name || guild.name}</strong>
                <span>{guild.origin_domain}</span>
                <p>{description || $t('ui_no_description_yet_6d962a3d')}</p>
              </div>
            </div>
          </div>

          {#if canManageGuildAssets}
            <div class="settings-card">
              <div class="settings-card-row">
                <div>
                  <strong>{$t('ui_guild_images_5f5c4a12')}</strong>
                  <p>{$t('ui_png_jpeg_gif_or_webp_files_are_scanned_before_da72aea4')}</p>
                </div>
                <div class="profile-media-actions">
                  <label class="secondary-button">
                    <Icon name="server" size={16} />{$t('ui_change_icon_87f2c4e6')}
                    <input
                      class="visually-hidden"
                      type="file"
                      accept="image/png,image/jpeg,image/gif,image/webp"
                      disabled={busy}
                      onchange={(event) => {
                        const file = event.currentTarget.files?.[0];
                        if (file) void uploadGuildAsset('icon', file);
                        event.currentTarget.value = '';
                      }}
                    />
                  </label>
                  {#if guild.icon_hash}
                    <button
                      class="secondary-button"
                      type="button"
                      disabled={busy}
                      onclick={() => void removeGuildAsset('icon')}
                    >
                      <Icon name="trash" size={16} />{$t('ui_remove_icon_93d6de71')}
                    </button>
                  {/if}
                  <label class="secondary-button">
                    <Icon name="image" size={16} />{$t('ui_change_banner_6ca19843')}
                    <input
                      class="visually-hidden"
                      type="file"
                      accept="image/png,image/jpeg,image/gif,image/webp"
                      disabled={busy}
                      onchange={(event) => {
                        const file = event.currentTarget.files?.[0];
                        if (file) void uploadGuildAsset('banner', file);
                        event.currentTarget.value = '';
                      }}
                    />
                  </label>
                  {#if guild.banner_hash}
                    <button
                      class="secondary-button"
                      type="button"
                      disabled={busy}
                      onclick={() => void removeGuildAsset('banner')}
                    >
                      <Icon name="trash" size={16} />{$t('ui_remove_banner_0f667465')}
                    </button>
                  {/if}
                </div>
              </div>
              {#if guildAssetKind && guildAssetStage}
                <div class="upload-progress" aria-live="polite">
                  {#if guildAssetStage === 'uploading'}
                    <progress
                      max="100"
                      value={guildAssetProgress}
                      aria-label={`Guild ${guildAssetKind} upload: ${guildAssetProgress}%`}
                    ></progress>
                    <span>{guildAssetProgress}%</span>
                  {:else}
                    <progress aria-label={`Scanning guild ${guildAssetKind}`}></progress>
                    <span>{$t('ui_scanning_38d96da6')}</span>
                  {/if}
                </div>
              {/if}
              {#if guildAssetError}
                <p class="form-error" role="alert">{guildAssetError}</p>
              {/if}
            </div>
          {/if}

          <form
            class="settings-card settings-form"
            onsubmit={(event) => {
              event.preventDefault();
              void saveGuild();
            }}
          >
            <label class="form-field">
              <span>{$t('ui_guild_name_4bffa81b')}</span>
              <small>{$t('ui_2_100_characters_aaae248a')}</small>
              <input
                bind:value={name}
                minlength="2"
                maxlength="100"
                required
                disabled={!canManageGuild}
              />
            </label>
            <label class="form-field">
              <span>{$t('ui_description_526e0087')}</span>
              <small>{description.length}/500</small>
              <textarea
                bind:value={description}
                maxlength="500"
                rows="4"
                disabled={!canManageGuild}
                placeholder={$t('ui_what_brings_this_community_together_8fe5bcff')}
              ></textarea>
            </label>
            <div class="settings-card-row history-policy-row">
              <div>
                <strong>{$t('ui_federated_message_history_4171dae9')}</strong>
                <p>{$t('ui_controls_the_default_for_remote_members_a_mem_a722f6a0')}</p>
              </div>
              <label class="form-field compact-field policy-select">
                <span>{$t('ui_guild_default_1ef8576b')}</span>
                <select bind:value={guildHistoryPolicy} disabled={!canManageGuild || busy}>
                  <option value="disabled">{$t('ui_do_not_export_history_efd17f23')}</option>
                  <option value="full_retained">{$t('ui_export_retained_history_a4526eb4')}</option>
                </select>
              </label>
            </div>
            {#if guildHistoryPolicy === 'full_retained'}
              <div class="notice-banner warning-banner" role="note">
                <Icon name="globe" size={18} />
                <span> {$t('ui_remote_instances_receive_their_own_copy_of_pe_173d38d8')} </span>
              </div>
            {/if}
            {#if canManageGuild}
              <div class="form-actions">
                <button class="primary-button" disabled={busy || !guildDirty}
                  >{$t('ui_save_overview_1b6df9ef')}</button
                >
              </div>
            {/if}
          </form>
        </section>
      {/if}

      {#if channelOnly && (canEditSelectedChannel || canEditSelectedPermissions || canCreateSelectedInvite || canAccessSelectedIntegrations)}
        <section id="channels" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="hash" /></span>
            <div>
              <h2>{$t('ui_channels_4c8906cf')}</h2>
              <p>{$t('ui_edit_channel_details_here_reorder_and_reparen_c7d6a5b5')}</p>
            </div>
          </div>
          <div class="settings-split">
            <div class="settings-list-panel">
              <div class="settings-list-heading">
                <strong>{$t('ui_channel_list_cc41e005')}</strong>
                <a href={donePath()}>{$t('ui_reorder_in_guild_3217ca75')}</a>
              </div>
              {#each channelGroups as group (group.key)}
                {#if group.category}
                  <button
                    class:active={selectedChannel &&
                      entityKey(selectedChannel) === entityKey(group.category)}
                    class="settings-list-item category-item"
                    type="button"
                    disabled={busy}
                    onclick={() => selectChannel(group.category!)}
                  >
                    <Icon name="chevron-down" size={15} />
                    <span>{group.category.name}</span>
                  </button>
                {/if}
                {#each group.channels as channel (entityKey(channel))}
                  <button
                    class:active={selectedChannel &&
                      entityKey(selectedChannel) === entityKey(channel)}
                    class="settings-list-item channel-item"
                    type="button"
                    disabled={busy}
                    onclick={() => selectChannel(channel)}
                  >
                    <Icon
                      name={channel.type === 2
                        ? 'volume'
                        : channel.type === 5
                          ? 'bell'
                          : channel.type === TRACKER_CHANNEL_TYPE
                            ? 'kanban'
                            : channel.type === 15
                              ? 'forum'
                              : 'hash'}
                      size={16}
                    />
                    <span>{channel.name}</span>
                  </button>
                {/each}
              {/each}
            </div>

            <div class="settings-card editor-card">
              {#if selectedChannel}
                <div class="editor-heading">
                  <div>
                    <span
                      >{selectedChannel.type === 4
                        ? $t('ui_category_292c06f0')
                        : $t('ui_channel_ce4683e7')}</span
                    >
                    <h3>{selectedChannel.name}</h3>
                  </div>
                  <code>{selectedChannel.id}</code>
                </div>
                <div
                  class="editor-tabs"
                  role="tablist"
                  aria-label={$t('ui_channel_settings_afc219f2')}
                >
                  <button
                    class:active={channelEditorPanel === 'overview'}
                    type="button"
                    role="tab"
                    aria-selected={channelEditorPanel === 'overview'}
                    onclick={() => selectChannelPanel('overview')}
                    >{$t('ui_overview_d4b1ea57')}</button
                  >
                  {#if canEditSelectedPermissions}
                    <button
                      class:active={channelEditorPanel === 'permissions'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'permissions'}
                      onclick={() => selectChannelPanel('permissions')}
                      >{$t('ui_permissions_abccc78c')}</button
                    >
                  {/if}
                  {#if (canCreateSelectedInvite || canEditSelectedChannel) && selectedChannel.type !== 4}
                    <button
                      class:active={channelEditorPanel === 'invites'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'invites'}
                      onclick={() => selectChannelPanel('invites')}
                      >{$t('ui_invites_f212a985')}</button
                    >
                  {/if}
                  {#if canAccessSelectedIntegrations && [0, 5, 15].includes(selectedChannel.type)}
                    <button
                      class:active={channelEditorPanel === 'integrations'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'integrations'}
                      onclick={() => selectChannelPanel('integrations')}
                      >{$t('ui_integrations_090512d9')}</button
                    >
                  {/if}
                  {#if canDeleteSelectedChannel}
                    <button
                      class="danger-tab"
                      class:active={channelEditorPanel === 'delete'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'delete'}
                      onclick={() => selectChannelPanel('delete')}
                      >{$t('ui_delete_e2d0a549')}</button
                    >
                  {/if}
                </div>
                {#if channelEditorPanel === 'overview' && canEditSelectedChannel}
                  <form
                    class="settings-form"
                    onsubmit={(event) => {
                      event.preventDefault();
                      void saveChannel();
                    }}
                  >
                    <label class="form-field compact-field">
                      <span>{$t('ui_name_dcd1d522')}</span>
                      <input bind:value={channelName} maxlength="100" required disabled={busy} />
                    </label>
                    {#if selectedChannel.type !== 4}
                      <label class="form-field compact-field">
                        <span
                          >{selectedChannel.type === 15
                            ? $t('ui_post_guidelines_91bdbfbb')
                            : $t('ui_topic_7e61847d')}</span
                        >
                        <textarea
                          bind:value={channelTopic}
                          maxlength={selectedChannel.type === 15 ? 4096 : 1024}
                          rows="3"
                          placeholder={selectedChannel.type === 15
                            ? $t('ui_help_members_understand_what_to_post_here_1cdc0d9c')
                            : $t('ui_what_belongs_in_this_channel_99732b14')}
                          disabled={busy}
                        ></textarea>
                      </label>
                      {#if selectedChannel.type === 15}
                        <fieldset class="forum-settings-group">
                          <legend>{$t('ui_tags_1331275b')}</legend>
                          <small
                            >{$t('ui_members_can_apply_up_to_five_tags_to_a_post_6120dd04')}</small
                          >
                          {#each channelForumTags as tag, index (`${tag.id ?? 'new'}:${index}`)}
                            <div class="forum-tag-editor">
                              <ForumEmojiField
                                emojiId={tag.emoji_id}
                                emojiName={tag.emoji_name}
                                guildDomain={guild?.origin_domain ?? ''}
                                customEmojis={forumEmojis}
                                label={`Emoji for ${tag.name}`}
                                disabled={busy}
                                onChange={(emoji) => updateForumTag(index, emoji)}
                              />
                              <input
                                value={tag.name}
                                maxlength="20"
                                aria-label={$t('ui_tag_name_1ace926b')}
                                required
                                disabled={busy}
                                oninput={(event) =>
                                  updateForumTag(index, { name: event.currentTarget.value })}
                              />
                              <label>
                                <input
                                  type="checkbox"
                                  checked={tag.moderated ?? false}
                                  disabled={busy}
                                  onchange={(event) =>
                                    updateForumTag(index, {
                                      moderated: event.currentTarget.checked
                                    })}
                                />
                                {$t('ui_moderators_only_100d2002')}
                              </label>
                              <button
                                class="quiet-button"
                                type="button"
                                disabled={busy}
                                aria-label={`Remove ${tag.name}`}
                                onclick={() => removeForumTag(index)}
                                >{$t('ui_remove_c3812fc4')}</button
                              >
                            </div>
                          {/each}
                          {#if channelForumTags.length < 20}
                            <div class="forum-tag-add">
                              <input
                                bind:value={newForumTagName}
                                maxlength="20"
                                placeholder={$t('ui_new_tag_609117d5')}
                                disabled={busy}
                              />
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy || !newForumTagName.trim()}
                                onclick={addForumTag}>{$t('ui_add_tag_3e10b08c')}</button
                              >
                            </div>
                          {/if}
                        </fieldset>
                        <div class="form-field compact-field">
                          <span>{$t('ui_default_reaction_emoji_cfae845e')}</span>
                          <ForumEmojiField
                            emojiId={channelForumReactionId}
                            emojiName={channelForumReaction}
                            guildDomain={guild?.origin_domain ?? ''}
                            customEmojis={forumEmojis}
                            label={$t('ui_default_reaction_emoji_cfae845e')}
                            disabled={busy}
                            onChange={(emoji) => {
                              channelForumReactionId = emoji.emoji_id;
                              channelForumReaction = emoji.emoji_name ?? '';
                            }}
                          />
                        </div>
                        <label class="form-field compact-field">
                          <span>{$t('ui_default_sort_order_91ca6695')}</span>
                          <select bind:value={channelForumSort} disabled={busy}>
                            <option value={0}>{$t('ui_recently_active_9068c1c7')}</option>
                            <option value={1}>{$t('ui_date_posted_1772fef6')}</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>{$t('ui_default_layout_8c43d9f6')}</span>
                          <select bind:value={channelForumLayout} disabled={busy}>
                            <option value={0}>{$t('ui_not_set_4895f731')}</option>
                            <option value={1}>{$t('ui_list_view_48627bdf')}</option>
                            <option value={2}>{$t('ui_gallery_view_62306a16')}</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>{$t('ui_hide_posts_after_inactivity_dfe364b5')}</span>
                          <select bind:value={channelForumArchive} disabled={busy}>
                            <option value={60}>{$t('ui_1_hour_f8b8883f')}</option>
                            <option value={1440}>{$t('ui_24_hours_f0514e8d')}</option>
                            <option value={4320}>{$t('ui_3_days_36071944')}</option>
                            <option value={10080}>{$t('ui_1_week_c8cc5223')}</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>{$t('ui_default_post_slowmode_2474deff')}</span>
                          <select bind:value={channelForumSlowmode} disabled={busy}>
                            <option value={0}>{$t('ui_off_ca7981b4')}</option>
                            <option value={5}>{$t('ui_5_seconds_be553746')}</option>
                            <option value={10}>{$t('ui_10_seconds_f78b958d')}</option>
                            <option value={30}>{$t('ui_30_seconds_f3d19541')}</option>
                            <option value={60}>{$t('ui_1_minute_e67b6f61')}</option>
                            <option value={300}>{$t('ui_5_minutes_3170543c')}</option>
                            <option value={3600}>{$t('ui_1_hour_f8b8883f')}</option>
                          </select>
                        </label>
                        <label class="settings-toggle-row">
                          <span>
                            <strong
                              >{$t(
                                'ui_require_people_to_select_tags_before_posting_578b963e'
                              )}</strong
                            >
                            <small
                              >{$t('ui_new_posts_must_include_at_least_one_tag_ae1fb202')}</small
                            >
                          </span>
                          <input
                            type="checkbox"
                            bind:checked={channelForumRequireTag}
                            disabled={busy}
                          />
                        </label>
                        <label class="settings-toggle-row">
                          <span>
                            <strong
                              >{$t(
                                'ui_require_end_to_end_encryption_for_post_replie_a72e5f95'
                              )}</strong
                            >
                            <small>
                              {$t('ui_titles_starter_messages_title_search_and_star_9b8d695d')}
                            </small>
                          </span>
                          <input
                            type="checkbox"
                            checked={channelForumE2EE}
                            disabled={busy ||
                              Boolean(selectedChannel.e2ee_required) ||
                              !e2eeActivationEnabled}
                            onchange={changeForumEncryptionRequirement}
                          />
                        </label>
                      {/if}
                      {#if selectedChannel.type === 0 || selectedChannel.type === 5}
                        <label class="form-field compact-field">
                          <span>{$t('ui_default_auto_archive_duration_f79f2cf7')}</span>
                          <select bind:value={channelForumArchive} disabled={busy}>
                            <option value={60}>{$t('ui_1_hour_f8b8883f')}</option>
                            <option value={1440}>{$t('ui_24_hours_f0514e8d')}</option>
                            <option value={4320}>{$t('ui_3_days_36071944')}</option>
                            <option value={10080}>{$t('ui_1_week_c8cc5223')}</option>
                          </select>
                        </label>
                        {#if selectedChannel.type === 0}
                          <label class="form-field compact-field">
                            <span>{$t('ui_default_thread_slowmode_6151d674')}</span>
                            <select bind:value={channelForumSlowmode} disabled={busy}>
                              <option value={0}>{$t('ui_off_ca7981b4')}</option>
                              <option value={5}>{$t('ui_5_seconds_be553746')}</option>
                              <option value={10}>{$t('ui_10_seconds_f78b958d')}</option>
                              <option value={30}>{$t('ui_30_seconds_f3d19541')}</option>
                              <option value={60}>{$t('ui_1_minute_e67b6f61')}</option>
                              <option value={300}>{$t('ui_5_minutes_3170543c')}</option>
                              <option value={3600}>{$t('ui_1_hour_f8b8883f')}</option>
                            </select>
                          </label>
                        {/if}
                        <label class="form-field compact-field">
                          <span>{$t('ui_federated_history_5190aa44')}</span>
                          <small>
                            {$t('ui_applies_only_after_the_member_also_passes_thi_8e8d143c')}
                          </small>
                          <select bind:value={channelHistoryPolicy} disabled={busy}>
                            <option value="inherit">{$t('ui_use_guild_default_64957975')}</option>
                            <option value="disabled"
                              >{$t('ui_never_export_this_channel_b99f29eb')}</option
                            >
                            <option value="full_retained"
                              >{$t('ui_export_retained_history_a4526eb4')}</option
                            >
                          </select>
                        </label>
                        {#if channelHistoryPolicy === 'full_retained'}
                          <div class="notice-banner warning-banner compact-warning" role="note">
                            <Icon name="globe" size={17} />
                            {$t('ui_remote_deletion_is_requested_on_access_loss_b_0e264b13')}
                          </div>
                        {/if}
                      {/if}
                      {#if selectedChannel.type === 0 || selectedChannel.type === 5 || selectedChannel.type === 15}
                        <label class="settings-toggle-row">
                          <span>
                            <strong>{$t('ui_age_restricted_channel_d51f5d19')}</strong>
                            <small>
                              {$t('ui_only_age_assured_adult_members_can_use_age_re_7b778f17')}
                            </small>
                          </span>
                          <input type="checkbox" bind:checked={channelNsfw} disabled={busy} />
                        </label>
                      {/if}
                      {#if selectedChannel.type === 2 || selectedChannel.type === 13}
                        <fieldset class="forum-settings-group">
                          <legend>{$t('ui_voice_quality_and_capacity_75eeed61')}</legend>
                          <small>
                            {$t('ui_these_limits_apply_to_every_web_desktop_mobil_a25d5cf0')}
                          </small>
                          <div class="forum-tag-add">
                            <label class="form-field compact-field">
                              <span>{$t('ui_bitrate_0b2b7f69')}</span>
                              <select bind:value={channelBitrate} disabled={busy}>
                                <option value={8000}>{$t('ui_8_kbps_fa9b273b')}</option>
                                <option value={32000}>{$t('ui_32_kbps_bcf057be')}</option>
                                <option value={64000}>{$t('ui_64_kbps_e38d61b0')}</option>
                                {#if selectedChannel.type === 2}
                                  <option value={96000}>{$t('ui_96_kbps_45220020')}</option>
                                  <option value={128000}>{$t('ui_128_kbps_a6920d5b')}</option>
                                  <option value={256000}>{$t('ui_256_kbps_e4950395')}</option>
                                  <option value={384000}>{$t('ui_384_kbps_728550c6')}</option>
                                {/if}
                              </select>
                            </label>
                            <label class="form-field compact-field">
                              <span>{$t('ui_user_limit_93ca90db')}</span>
                              <input
                                type="number"
                                min="0"
                                max={selectedChannel.type === 13 ? 10000 : 99}
                                bind:value={channelUserLimit}
                                disabled={busy}
                              />
                              <small>{$t('ui_0_means_no_explicit_limit_25da0b40')}</small>
                            </label>
                          </div>
                          <label class="form-field compact-field">
                            <span>{$t('ui_region_override_556de8e1')}</span>
                            <select
                              bind:value={channelRtcRegion}
                              disabled={busy || !canEditSelectedChannel}
                            >
                              <option value="">{$t('ui_automatic_d461a493')}</option>
                              {#if channelRtcRegion && !channelVoiceRegions.some((region) => region.id === channelRtcRegion)}
                                <option value={channelRtcRegion}
                                  >{$t('ui_value0_unavailable_59cf7d87', {
                                    value0: String(channelRtcRegion)
                                  })}</option
                                >
                              {/if}
                              {#each channelVoiceRegions as region (region.id)}
                                <option
                                  value={region.id}
                                  disabled={region.deprecated && region.id !== channelRtcRegion}
                                >
                                  {region.name}{region.optimal
                                    ? $t('ui_recommended_d966cec0')
                                    : ''}{region.deprecated ? $t('ui_deprecated_db5b5b88') : ''}
                                </option>
                              {/each}
                            </select>
                            <small>
                              {$t('ui_automatic_chooses_the_lowest_latency_region_t_7a5d9278')}
                            </small>
                            {#if channelVoiceRegionsError}
                              <small class="field-error">{channelVoiceRegionsError}</small>
                            {/if}
                          </label>
                        </fieldset>
                      {/if}
                      {#if (selectedChannel.type === 0 || selectedChannel.type === 2 || selectedChannel.type === 5) && (selectedChannel.encryption_mode === 'e2ee' || e2eeActivationEnabled)}
                        <div class="notice-banner compact-warning" role="note">
                          <Icon name="lock" size={17} />
                          <div>
                            <strong>{$t('ui_end_to_end_encryption_1b3f0f02')}</strong>
                            {#if selectedChannel.encryption_mode === 'e2ee'}
                              <p>
                                {selectedChannel.encryption_state === 'rekeying'
                                  ? $t('ui_paused_after_a_membership_change_secure_the_c_9b39a5fe')
                                  : $t('ui_on_participant_identities_remain_unverified_u_04ef8c30')}
                              </p>
                              {#if channelSafetyNumber}
                                <code class="e2ee-safety-number">{channelSafetyNumber}</code>
                              {/if}
                              {#if selectedChannel.encryption_state === 'active'}
                                <button
                                  class="secondary-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={verifyChannelSafetyNumber}
                                  >{$t('ui_show_safety_number_e3744d1f')}</button
                                >
                              {/if}
                              {#if selectedChannel.encryption_state === 'rekeying'}
                                <button
                                  class="secondary-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={enableChannelEncryption}
                                  >{$t('ui_secure_current_members_e92c2946')}</button
                                >
                              {/if}
                            {:else if e2eeActivationEnabled}
                              <p>
                                {selectedChannel.type === 2
                                  ? $t('ui_optional_and_permanent_encrypts_microphone_ca_7d76cf1e')
                                  : $t('ui_optional_and_permanent_disables_server_search_cb244c02')}
                              </p>
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy}
                                onclick={enableChannelEncryption}
                                >{$t('ui_turn_on_encryption_9eff34df')}</button
                              >
                            {/if}
                          </div>
                        </div>
                      {/if}
                      <label class="form-field compact-field">
                        <span>{$t('ui_category_292c06f0')}</span>
                        <select bind:value={channelParent} disabled={busy}>
                          <option value="">{$t('ui_no_category_b91b9cac')}</option>
                          {#each editableChannelParents(selectedChannel) as category (entityKey(category))}
                            <option value={entityKey(category)}>{category.name}</option>
                          {/each}
                        </select>
                      </label>
                      <label class="form-field compact-field">
                        <span>{$t('ui_slowmode_b529fb9f')}</span>
                        <select bind:value={channelSlowmode} disabled={busy}>
                          <option value={0}>{$t('ui_off_ca7981b4')}</option>
                          <option value={5}>{$t('ui_5_seconds_be553746')}</option>
                          <option value={10}>{$t('ui_10_seconds_f78b958d')}</option>
                          <option value={30}>{$t('ui_30_seconds_f3d19541')}</option>
                          <option value={60}>{$t('ui_1_minute_e67b6f61')}</option>
                          <option value={300}>{$t('ui_5_minutes_3170543c')}</option>
                          <option value={3600}>{$t('ui_1_hour_f8b8883f')}</option>
                        </select>
                      </label>
                    {/if}
                    <div class="form-actions">
                      <button class="primary-button" disabled={busy || !channelDirty}
                        >{$t('ui_save_channel_694a13e9')}</button
                      >
                    </div>
                  </form>
                {/if}
                {#if channelEditorPanel === 'permissions' && canEditSelectedPermissions}
                  <section
                    class="channel-permissions-editor"
                    aria-labelledby="channel-permissions-title"
                  >
                    <div>
                      <span>{$t('ui_access_control_0bf1d245')}</span>
                      <h4 id="channel-permissions-title">
                        {$t('ui_channel_permissions_0f2de6e0')}
                      </h4>
                      <p>{$t('ui_override_a_role_or_member_for_this_channel_in_74c822aa')}</p>
                    </div>
                    {#if selectedChannel.parent_id}
                      <div
                        class:warning-banner={!selectedChannel.permissions_synced}
                        class="notice-banner sync-banner"
                        role="status"
                      >
                        <Icon
                          name={selectedChannel.permissions_synced ? 'check' : 'lock'}
                          size={18}
                        />
                        <span>
                          <strong>
                            {selectedChannel.permissions_synced
                              ? $t('ui_synced_with_category_9ebefac7')
                              : $t('ui_permissions_not_synced_with_category_19c4a32c')}
                          </strong>
                          <small>
                            {$t('ui_syncing_replaces_this_channel_s_overrides_wit_739df227')}
                          </small>
                        </span>
                        {#if !selectedChannel.permissions_synced}
                          <button
                            class="secondary-button"
                            type="button"
                            disabled={busy}
                            onclick={() => void syncChannelPermissions()}
                            >{$t('ui_sync_now_b5060fd1')}</button
                          >
                        {/if}
                      </div>
                    {/if}
                    <div class="permission-workspace">
                      <aside
                        class="permission-target-rail"
                        aria-label={$t('ui_permission_targets_db9a5274')}
                      >
                        <label class="form-field compact-field">
                          <span>{$t('ui_roles_and_members_47e46135')}</span>
                          <input
                            bind:value={overwriteSearch}
                            placeholder={$t('ui_search_49c266ba')}
                          />
                        </label>
                        <div class="permission-target-list">
                          <p>{$t('ui_roles_c2533705')}</p>
                          {#each filteredRoles as role (entityKey(role))}
                            <button
                              class:active={overwriteTarget === `role:${entityRef(role)}`}
                              type="button"
                              disabled={busy || !canManageOverwriteRole(role)}
                              aria-pressed={overwriteTarget === `role:${entityRef(role)}`}
                              onclick={() => selectOverwriteTarget(`role:${entityRef(role)}`)}
                            >
                              <span
                                class="role-color-dot"
                                style={`--role-color: ${roleColorValue(role.color)}`}
                              ></span>
                              <span>{role.id === guild.id ? '@everyone' : role.name}</span>
                            </button>
                          {/each}
                          {#if filteredMembers.length}
                            <p>{$t('ui_members_1044a4c0')}</p>
                            {#each filteredMembers as member (entityKey(member.user))}
                              <button
                                class:active={overwriteTarget ===
                                  `member:${entityRef(member.user)}`}
                                type="button"
                                disabled={busy || !canManageOverwriteMember(member)}
                                aria-pressed={overwriteTarget ===
                                  `member:${entityRef(member.user)}`}
                                onclick={() =>
                                  selectOverwriteTarget(`member:${entityRef(member.user)}`)}
                              >
                                <span class="permission-target-avatar">
                                  {#if member.user.avatar_hash}
                                    <img
                                      src={assetUrl(
                                        member.user.avatar_hash,
                                        'thumbnail_128',
                                        member.user
                                      )}
                                      alt=""
                                    />
                                  {:else}
                                    {(member.nickname ?? userDisplayName(member.user))
                                      .slice(0, 1)
                                      .toUpperCase()}
                                  {/if}
                                </span>
                                <span>
                                  {member.nickname ?? userDisplayName(member.user)}
                                </span>
                              </button>
                            {/each}
                          {/if}
                        </div>
                      </aside>

                      <div class="permission-detail">
                        {#if overwriteTarget}
                          <div class="permission-detail-heading">
                            <div>
                              <span>{$t('ui_permissions_for_e474cdc4')}</span>
                              <h5>{overwriteTargetLabel()}</h5>
                            </div>
                            <label class="form-field compact-field permission-search">
                              <span class="visually-hidden"
                                >{$t('ui_search_permissions_0e099113')}</span
                              >
                              <input
                                bind:value={permissionSearch}
                                placeholder={$t('ui_search_permissions_0e099113')}
                              />
                            </label>
                          </div>
                          <div class="overwrite-matrix">
                            {#each channelPermissionGroups as group (group.name)}
                              <fieldset>
                                <legend>{group.name}</legend>
                                {#each group.permissions as permission (permission[0])}
                                  <div class="overwrite-permission-row">
                                    <span>
                                      <strong>{permission[0]}</strong>
                                      <small>{permission[1]}</small>
                                      {#if permission[3].dependencies.length}
                                        <small class="permission-dependencies">
                                          {$t('ui_also_requires_value0_68b47b3a', {
                                            value0: String(
                                              permissionDependencies(permission[3].dependencies)
                                            )
                                          })}
                                        </small>
                                      {/if}
                                    </span>
                                    <div
                                      class="permission-tristate"
                                      role="group"
                                      aria-label={`${permission[0]} channel override`}
                                    >
                                      <button
                                        class="deny"
                                        class:active={overwritePermission(permission[2]) === 'deny'}
                                        type="button"
                                        disabled={busy || !selectedHasPermission(permission[2])}
                                        aria-label={$t('ui_deny_in_this_channel_be2b5114')}
                                        title={$t('ui_deny_05a2d733')}
                                        onclick={() =>
                                          setOverwritePermission(permission[2], 'deny')}>×</button
                                      >
                                      <button
                                        class:active={overwritePermission(permission[2]) ===
                                          'inherit'}
                                        type="button"
                                        disabled={busy || !selectedHasPermission(permission[2])}
                                        aria-label={$t('ui_inherit_guild_setting_e42820b1')}
                                        title={$t('ui_inherit_3f72f038')}
                                        onclick={() =>
                                          setOverwritePermission(permission[2], 'inherit')}
                                        >/</button
                                      >
                                      <button
                                        class="allow"
                                        class:active={overwritePermission(permission[2]) ===
                                          'allow'}
                                        type="button"
                                        disabled={busy || !selectedHasPermission(permission[2])}
                                        aria-label={$t('ui_allow_in_this_channel_fbf07560')}
                                        title={$t('ui_allow_e213c161')}
                                        onclick={() =>
                                          setOverwritePermission(permission[2], 'allow')}>✓</button
                                      >
                                    </div>
                                  </div>
                                {/each}
                              </fieldset>
                            {/each}
                          </div>
                          <div class="form-actions spread-actions">
                            <button
                              class="secondary-button"
                              type="button"
                              disabled={busy ||
                                !canManageOverwriteTarget(overwriteTarget) ||
                                !hasAllPermissions(
                                  selectedEffectivePermissions,
                                  BigInt(overwriteAllow) | BigInt(overwriteDeny)
                                )}
                              onclick={() => void resetChannelOverwrite()}
                              >{$t('ui_reset_override_82b7570a')}</button
                            >
                            <button
                              class="primary-button"
                              type="button"
                              disabled={busy ||
                                !overwriteDirty ||
                                !canManageOverwriteTarget(overwriteTarget)}
                              onclick={() => void saveChannelOverwrite()}
                              >{$t('ui_save_permissions_1eab372a')}</button
                            >
                          </div>
                        {:else}
                          <div class="empty-state compact-empty permission-target-empty">
                            <span><Icon name="shield" /></span>
                            <h3>{$t('ui_choose_a_role_or_member_54951033')}</h3>
                            <p>{$t('ui_select_a_target_to_review_its_channel_specifi_25fcd6a2')}</p>
                          </div>
                        {/if}
                      </div>
                    </div>
                  </section>
                {/if}
                {#if channelEditorPanel === 'invites' && (canCreateSelectedInvite || canEditSelectedChannel)}
                  <section
                    class="channel-permissions-editor"
                    aria-labelledby="channel-invites-title"
                  >
                    <div>
                      <span>{$t('ui_channel_access_34e631c3')}</span>
                      <h4 id="channel-invites-title">{$t('ui_invites_f212a985')}</h4>
                      <p>{$t('ui_create_links_that_open_this_channel_after_the_36f26c96')}</p>
                    </div>
                    {#if canCreateSelectedInvite}
                      <div class="settings-form">
                        <div class="two-column-fields">
                          <label class="form-field compact-field">
                            <span>{$t('ui_expires_after_a5e4b9f5')}</span>
                            <select bind:value={inviteMaxAge} disabled={busy}>
                              <option value="3600">{$t('ui_1_hour_f8b8883f')}</option>
                              <option value="86400">{$t('ui_1_day_fa665d95')}</option>
                              <option value="604800">{$t('ui_7_days_7f920bb6')}</option>
                              <option value="">{$t('ui_never_6300ef80')}</option>
                            </select>
                          </label>
                          <label class="form-field compact-field">
                            <span>{$t('ui_maximum_uses_3a104290')}</span>
                            <input
                              bind:value={inviteMaxUses}
                              type="number"
                              min="1"
                              placeholder={$t('ui_unlimited_11dde17d')}
                              disabled={busy}
                            />
                          </label>
                        </div>
                        <div class="form-actions">
                          <button
                            class="primary-button"
                            type="button"
                            disabled={busy}
                            onclick={() => void createInvite()}
                            >{$t('ui_create_invite_9f395b8f')}</button
                          >
                        </div>
                      </div>
                    {/if}
                    <div class="settings-list compact-list">
                      {#each selectedChannelInvites as invite (invite.code)}
                        <div class="settings-list-row">
                          <span>
                            <strong>{invite.code}</strong>
                            <small
                              >{$t('ui_value0_use_value1_value2_4740faeb', {
                                value0: String(invite.uses),
                                value1: String(invite.uses === 1 ? '' : 's'),
                                value2: String(
                                  invite.expires_at
                                    ? formatDateTime(invite.expires_at)
                                    : 'No expiry'
                                )
                              })}</small
                            >
                          </span>
                          {#if canRevokeInvite(invite)}
                            <button
                              class="danger-text-button"
                              type="button"
                              disabled={busy}
                              onclick={() => revokeInvite(invite)}
                              >{$t('ui_revoke_87e6d00b')}</button
                            >
                          {/if}
                        </div>
                      {:else}
                        <p class="muted-copy">
                          {$t('ui_no_active_invites_target_this_channel_11ad1ed1')}
                        </p>
                      {/each}
                    </div>
                  </section>
                {/if}
                {#if channelEditorPanel === 'integrations' && canAccessSelectedIntegrations}
                  {#if canManageSelectedWebhooks}
                    <section
                      class="channel-permissions-editor"
                      aria-labelledby="channel-integrations-title"
                    >
                      <div>
                        <span>{$t('ui_integrations_090512d9')}</span>
                        <h4 id="channel-integrations-title">{$t('ui_webhooks_45808d75')}</h4>
                        <p>{$t('ui_webhooks_can_post_into_this_channel_tokens_ar_fb5e1893')}</p>
                      </div>
                      <form
                        class="inline-settings-form"
                        onsubmit={(event) => {
                          event.preventDefault();
                          void createChannelWebhook();
                        }}
                      >
                        <label class="form-field compact-field">
                          <span>{$t('ui_webhook_name_d28bddc1')}</span>
                          <input
                            bind:value={newWebhookName}
                            minlength="1"
                            maxlength="80"
                            required
                            disabled={busy}
                          />
                        </label>
                        <button class="primary-button" disabled={busy}
                          >{$t('ui_create_webhook_4a2b33ad')}</button
                        >
                      </form>
                      {#if revealedWebhookToken}
                        <div class="notice-banner warning-banner" role="status">
                          <Icon name="lock" size={18} />
                          <span
                            ><strong>{$t('ui_webhook_url_84805a75')}</strong><code
                              >{revealedWebhookToken}</code
                            ></span
                          >
                          <button
                            class="secondary-button"
                            type="button"
                            onclick={() => void copyWebhookUrl()}
                            >{$t('ui_copy_webhook_url_69179edf')}</button
                          >
                        </div>
                      {/if}
                      <div class="settings-list compact-list">
                        {#each selectedChannelWebhooks as webhook (webhook.id)}
                          <div class="settings-list-row webhook-settings-row">
                            <div class="webhook-avatar-editor">
                              {#if webhook.avatar_hash}
                                <img
                                  src={assetUrl(
                                    webhook.avatar_hash,
                                    'thumbnail_128',
                                    webhook.guild_domain
                                  )}
                                  alt=""
                                />
                              {:else}
                                <span class="webhook-avatar-placeholder" aria-hidden="true">
                                  <Icon name="image" size={20} />
                                </span>
                              {/if}
                              <label class="secondary-button webhook-avatar-button">
                                <span
                                  >{webhook.avatar_hash
                                    ? $t('ui_replace_avatar_ec965b00')
                                    : $t('ui_add_avatar_97bc36ba')}</span
                                >
                                <input
                                  type="file"
                                  accept="image/png,image/jpeg,image/gif,image/webp"
                                  disabled={busy}
                                  onchange={(event) => {
                                    const input = event.currentTarget;
                                    void uploadWebhookAvatar(
                                      webhook,
                                      input.files?.[0] ?? null,
                                      input
                                    );
                                  }}
                                />
                              </label>
                              {#if webhook.avatar_hash}
                                <button
                                  class="danger-text-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={() => void deleteWebhookAvatar(webhook)}
                                  >{$t('ui_remove_avatar_5ae2a862')}</button
                                >
                              {/if}
                            </div>
                            <div class="webhook-fields">
                              <label class="form-field compact-field webhook-name-field">
                                <span
                                  >{$t('ui_name_dcd1d522')}
                                  <small
                                    >{$t('ui_id_value0_19b3c40a', {
                                      value0: String(webhook.id)
                                    })}</small
                                  ></span
                                >
                                <input
                                  value={webhookNameDrafts[webhook.id] ?? webhook.name}
                                  minlength="1"
                                  maxlength="80"
                                  disabled={busy}
                                  oninput={(event) =>
                                    (webhookNameDrafts = {
                                      ...webhookNameDrafts,
                                      [webhook.id]: event.currentTarget.value
                                    })}
                                />
                              </label>
                              <label class="form-field compact-field">
                                <span>{$t('ui_post_to_channel_6bcc7da9')}</span>
                                <select
                                  value={webhookChannelDrafts[webhook.id] ??
                                    `${webhook.channel_id}@${webhook.channel_domain}`}
                                  disabled={busy}
                                  onchange={(event) =>
                                    (webhookChannelDrafts = {
                                      ...webhookChannelDrafts,
                                      [webhook.id]: event.currentTarget.value
                                    })}
                                >
                                  {#each manageableWebhookTargets as channel (entityRef(channel))}
                                    <option value={entityRef(channel)}>#{channel.name}</option>
                                  {/each}
                                </select>
                              </label>
                            </div>
                            <div class="row-actions">
                              {#if webhook.execution_url}
                                <button
                                  class="secondary-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={() => void copyWebhookUrl(webhook.execution_url)}
                                  >{$t('ui_copy_webhook_url_69179edf')}</button
                                >
                              {/if}
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy ||
                                  !webhookDirty(webhook) ||
                                  !(webhookNameDrafts[webhook.id] ?? webhook.name).trim()}
                                onclick={() => void updateWebhook(webhook)}
                                >{$t('ui_save_1509f561')}</button
                              >
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy}
                                onclick={() => void rotateWebhook(webhook)}
                                >{$t('ui_rotate_token_4ade7882')}</button
                              >
                              <button
                                class="danger-text-button"
                                type="button"
                                disabled={busy}
                                onclick={() => void deleteWebhook(webhook)}
                                >{$t('ui_delete_e2d0a549')}</button
                              >
                            </div>
                          </div>
                        {:else}
                          <p class="muted-copy">
                            {$t('ui_no_webhooks_post_to_this_channel_6bb10927')}
                          </p>
                        {/each}
                      </div>
                    </section>
                  {/if}
                  {#if selectedChannel.type === 5}
                    <AnnouncementFollowers
                      sourceChannel={selectedChannel}
                      guilds={announcementGuilds}
                      canRead={canReadSelectedAnnouncementFollows}
                    />
                  {/if}
                {/if}
                {#if channelEditorPanel === 'delete' && canEditSelectedChannel}
                  <section
                    class="channel-permissions-editor danger-zone"
                    aria-labelledby="delete-channel-title"
                  >
                    <div>
                      <span>{$t('ui_danger_zone_fd8b8dae')}</span>
                      <h4 id="delete-channel-title">
                        {$t('ui_delete_value0_8782b778', {
                          value0: String(selectedChannel.type === 4 ? 'category' : 'channel')
                        })}
                      </h4>
                      <p>
                        {selectedChannel.type === TRACKER_CHANNEL_TYPE
                          ? $t('ui_this_is_permanent_the_board_its_statuses_and__530e8c9b')
                          : $t('ui_this_is_permanent_categories_must_be_empty_an_58060f5a')}
                      </p>
                    </div>
                    <button
                      class="danger-button"
                      type="button"
                      disabled={busy}
                      onclick={deleteChannel}
                    >
                      <Icon name="trash" size={16} />
                      {$t('ui_delete_value0_8782b778', {
                        value0: String(selectedChannel.type === 4 ? 'category' : 'channel')
                      })}
                    </button>
                  </section>
                {/if}
              {:else}
                <div class="empty-state compact-empty">
                  <span><Icon name="hash" /></span>
                  <h3>{$t('ui_select_a_channel_364ccb65')}</h3>
                  <p>{$t('ui_choose_an_item_from_the_list_to_edit_its_deta_9c8a3e35')}</p>
                </div>
              {/if}
            </div>
          </div>

          {#if canManageChannels}
            <form
              class="settings-card quick-create"
              onsubmit={(event) => {
                event.preventDefault();
                void createChannel();
              }}
            >
              <div>
                <strong>{$t('ui_create_a_channel_10ac504b')}</strong>
                <p>{$t('ui_add_a_text_voice_announcement_forum_task_trac_f400eb16')}</p>
              </div>
              <label class="form-field compact-field">
                <span>{$t('ui_name_dcd1d522')}</span>
                <input bind:value={newChannelName} maxlength="100" required disabled={busy} />
              </label>
              <label class="form-field compact-field">
                <span>{$t('ui_type_baaddf70')}</span>
                <select bind:value={newChannelType} disabled={busy}>
                  <option value={0}>{$t('ui_text_71988c4d')}</option>
                  <option value={2}>{$t('ui_voice_87bf2bc0')}</option>
                  <option value={4}>{$t('ui_category_292c06f0')}</option>
                  <option value={5}>{$t('ui_announcement_028cd1c8')}</option>
                  <option value={15}>{$t('ui_forum_4da7bd42')}</option>
                  <option value={TRACKER_CHANNEL_TYPE}>{$t('ui_task_tracker_b7794464')}</option>
                </select>
              </label>
              {#if newChannelType === TRACKER_CHANNEL_TYPE}
                <label class="form-field compact-field">
                  <span>{$t('ui_task_key_prefix_f14da226')}</span>
                  <small>{$t('ui_optional_defaults_from_the_channel_name_067263d7')}</small>
                  <input
                    bind:value={newChannelTrackerPrefix}
                    minlength="2"
                    maxlength="10"
                    pattern="[A-Za-z][A-Za-z0-9]*"
                    placeholder={$t('ui_e_g_raid_ec75a5bf')}
                    disabled={busy}
                  />
                </label>
              {/if}
              {#if newChannelType !== 4}
                <label class="form-field compact-field">
                  <span>{$t('ui_category_292c06f0')}</span>
                  <select bind:value={newChannelParent} disabled={busy}>
                    <option value="">{$t('ui_no_category_b91b9cac')}</option>
                    {#each (guild.channels ?? []).filter((channel) => channel.type === 4) as category (entityKey(category))}
                      <option value={entityKey(category)}>{category.name}</option>
                    {/each}
                  </select>
                </label>
              {/if}
              <button class="primary-button" disabled={busy}>
                <Icon name="plus" size={16} />{$t('ui_create_4759498a')}
              </button>
            </form>
          {/if}
        </section>
      {/if}

      {#if !channelOnly && canAccessExpressions}
        <section id="emoji" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon" aria-hidden="true">☺</span>
            <div>
              <h2>{$t('ui_custom_emoji_1596e05e')}</h2>
              <p>{$t('ui_upload_emoji_that_members_can_use_here_and_in_9271d84a')}</p>
            </div>
          </div>
          <div class="settings-card">
            <div class="settings-list-heading">
              <div>
                <strong>{$t('ui_guild_emoji_0b0a82c1')}</strong>
                <p>
                  {$t('ui_value0_of_value1_used_1e8de6e6', {
                    value0: String(guild?.emojis?.length ?? 0),
                    value1: String(guild?.emoji_limit ?? 100)
                  })}
                </p>
              </div>
            </div>
            {#if canCreateExpressions}
              <form class="inline-create-form" onsubmit={createEmoji}>
                <label class="form-field compact-field">
                  <span>{$t('ui_name_dcd1d522')}</span>
                  <input
                    bind:value={emojiName}
                    pattern={'[A-Za-z0-9_]{2,32}'}
                    minlength="2"
                    maxlength="32"
                    placeholder="party_blob"
                    required
                    disabled={emojiBusy}
                  />
                </label>
                <label class="form-field compact-field">
                  <span>{$t('ui_image_1aa4cb0b')}</span>
                  <ImageUploadField
                    id="emoji-image"
                    file={emojiFile}
                    required
                    disabled={emojiBusy}
                    onSelect={(file, input) => {
                      emojiFile = file;
                      emojiInput = input;
                    }}
                  />
                </label>
                <button
                  class="primary-button"
                  disabled={emojiBusy ||
                    (guild?.emojis?.length ?? 0) >= (guild?.emoji_limit ?? 100)}
                  >{emojiBusy
                    ? $t('ui_uploading_5ce44dd7')
                    : $t('ui_upload_emoji_84bc1823')}</button
                >
              </form>
            {/if}
            <div class="emoji-management-grid">
              {#each guild?.emojis ?? [] as emoji (entityKey(emoji))}
                {@const editable = canEditEmoji(emoji)}
                <article class="emoji-management-item expression-management-item">
                  {#if emoji.media_hash}
                    <img
                      src={assetUrl(emoji.media_hash, 'thumbnail_128', emoji.origin_domain)}
                      alt={`:${emoji.name}:`}
                    />
                  {/if}
                  {#if emojiDrafts[entityKey(emoji)]}
                    <div class="expression-fields">
                      <label class="form-field compact-field">
                        <span>{$t('ui_name_dcd1d522')}</span>
                        <input
                          value={emojiDrafts[entityKey(emoji)].name}
                          pattern={'[A-Za-z0-9_]{2,32}'}
                          minlength="2"
                          maxlength="32"
                          disabled={emojiBusy || !editable}
                          oninput={(event) =>
                            patchEmojiDraft(emoji, { name: event.currentTarget.value })}
                        />
                      </label>
                      <label class="form-field compact-field">
                        <span
                          >{$t('ui_role_restrictions_42263853')}
                          <small>{$t('ui_none_means_everyone_9b433987')}</small></span
                        >
                        <select
                          multiple
                          size="3"
                          value={emojiDrafts[entityKey(emoji)].roles}
                          disabled={emojiBusy || !canEditEmojiRoleRestrictions(emoji)}
                          onchange={(event) =>
                            patchEmojiDraft(emoji, {
                              roles: Array.from(
                                event.currentTarget.selectedOptions,
                                (option) => option.value
                              )
                            })}
                        >
                          {#each (guild.roles ?? []).filter((role) => role.id !== guild?.id && canManageExpressionRole(role)) as role (entityKey(role))}
                            <option value={entityRef(role)}>{role.name}</option>
                          {/each}
                        </select>
                        {#if editable && !canEditEmojiRoleRestrictions(emoji)}
                          <small>
                            {$t('ui_a_restriction_is_above_your_highest_role_you__dc49c1b4')}
                          </small>
                        {/if}
                      </label>
                      <p class="field-hint">
                        <strong
                          >{emoji.available === false
                            ? $t('ui_unavailable_ca184496')
                            : $t('ui_available_e6744473')}</strong
                        >
                        {$t('ui_availability_is_controlled_by_the_server_and__709ec772')}
                      </p>
                    </div>
                    {#if editable}
                      <div class="row-actions expression-actions">
                        <button
                          class="secondary-button"
                          type="button"
                          disabled={emojiBusy || !emojiDirty(emoji)}
                          onclick={() => void updateEmoji(emoji)}>{$t('ui_save_1509f561')}</button
                        >
                        <button
                          class="secondary-button danger-button"
                          type="button"
                          disabled={emojiBusy}
                          onclick={() => void deleteEmoji(emoji)}>{$t('ui_delete_e2d0a549')}</button
                        >
                      </div>
                    {:else}
                      <small class="field-hint"
                        >{$t('ui_only_its_creator_or_an_expression_manager_can_e0175c0c')}</small
                      >
                    {/if}
                  {/if}
                </article>
              {:else}
                <p class="empty-copy">{$t('ui_this_guild_has_no_custom_emoji_yet_bed2d0ca')}</p>
              {/each}
            </div>
          </div>
        </section>

        <section id="stickers" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon" aria-hidden="true">▱</span>
            <div>
              <h2>{$t('ui_guild_stickers_b9cc433f')}</h2>
              <p>{$t('ui_create_transparent_static_or_animated_sticker_7e61024d')}</p>
            </div>
          </div>
          <div class="settings-card">
            <div class="settings-list-heading">
              <div>
                <strong>{$t('ui_sticker_collection_76fcec4f')}</strong>
                <p>
                  {$t('ui_value0_of_value1_used_1e8de6e6', {
                    value0: String(guild?.stickers?.length ?? 0),
                    value1: String(guild?.sticker_limit ?? 60)
                  })}
                </p>
              </div>
            </div>
            {#if canCreateExpressions}
              <form class="sticker-create-form" onsubmit={createSticker}>
                <div class="sticker-fields">
                  <label class="form-field compact-field">
                    <span>{$t('ui_name_dcd1d522')}</span>
                    <input
                      bind:value={stickerName}
                      minlength="2"
                      maxlength="30"
                      placeholder={$t('ui_friendly_wave_706204c7')}
                      required
                      disabled={stickerBusy}
                    />
                  </label>
                  <label class="form-field compact-field">
                    <span
                      >{$t('ui_description_526e0087')}
                      <small>{$t('ui_optional_59be7133')}</small></span
                    >
                    <input
                      bind:value={stickerDescription}
                      minlength="2"
                      maxlength="100"
                      placeholder={$t('ui_a_friendly_wave_cd04be35')}
                      disabled={stickerBusy}
                    />
                  </label>
                  <label class="form-field compact-field">
                    <span>{$t('ui_image_1aa4cb0b')}</span>
                    <ImageUploadField
                      id="sticker-image"
                      file={stickerFile}
                      required
                      disabled={stickerBusy}
                      onSelect={selectStickerFile}
                    />
                  </label>
                </div>
                {#if stickerPreviewUrl}
                  <div class="sticker-editor">
                    <div class="sticker-crop-stage">
                      <div
                        class="sticker-crop-preview"
                        role="application"
                        aria-label={$t('ui_sticker_crop_editor_a4f4b5c7')}
                        style:--sticker-image-aspect={String(stickerImageAspect)}
                        onpointermove={moveStickerCropGesture}
                        onpointerup={endStickerCropGesture}
                        onpointercancel={endStickerCropGesture}
                      >
                        <img
                          src={stickerPreviewUrl}
                          alt={$t('ui_sticker_source_c47ae468')}
                          draggable="false"
                          onload={(event) => {
                            const image = event.currentTarget as HTMLImageElement;
                            stickerImageAspect = image.naturalWidth / image.naturalHeight || 1;
                          }}
                        />
                        <div
                          class="sticker-crop-selection"
                          role="button"
                          aria-label={$t(
                            'ui_crop_selection_drag_to_move_or_use_the_arrow__b25c1380'
                          )}
                          tabindex="0"
                          style:left={`${stickerCropX * 100}%`}
                          style:top={`${stickerCropY * 100}%`}
                          style:width={`${stickerCropWidth * 100}%`}
                          style:height={`${stickerCropHeight * 100}%`}
                          onpointerdown={(event) => beginStickerCropGesture(event, 'move')}
                          onkeydown={moveStickerCropWithKeyboard}
                        >
                          <span class="crop-grid-line crop-grid-line-v first"></span>
                          <span class="crop-grid-line crop-grid-line-v second"></span>
                          <span class="crop-grid-line crop-grid-line-h first"></span>
                          <span class="crop-grid-line crop-grid-line-h second"></span>
                          {#each ['nw', 'ne', 'sw', 'se'] as corner (corner)}
                            <button
                              class={`crop-handle ${corner}`}
                              type="button"
                              aria-label={`Resize crop from ${corner.toUpperCase()} corner`}
                              onpointerdown={(event) =>
                                beginStickerCropGesture(event, corner as CropCorner, true)}
                              onkeydown={(event) =>
                                resizeStickerCropWithKeyboard(event, corner as CropCorner)}
                            ></button>
                          {/each}
                        </div>
                      </div>
                    </div>
                    <div class="sticker-crop-controls">
                      <strong>{$t('ui_crop_your_sticker_41cf7615')}</strong>
                      <p>{$t('ui_drag_the_box_to_move_it_drag_any_corner_to_re_5f1d6ef4')}</p>
                      <small>
                        {$t('ui_selection_value0_value1_87e898ca', {
                          value0: String(Math.round(stickerCropWidth * 100)),
                          value1: String(Math.round(stickerCropHeight * 100))
                        })}
                      </small>
                      <button
                        class="secondary-button crop-reset-button"
                        type="button"
                        onclick={() => applyStickerCrop({ x: 0, y: 0, width: 1, height: 1 })}
                        >{$t('ui_reset_crop_994feef6')}</button
                      >
                      <label class="toggle-row">
                        <input
                          type="checkbox"
                          bind:checked={stickerRemoveBackground}
                          disabled={stickerBusy ||
                            stickerFile?.type === 'image/gif' ||
                            !guild?.sticker_background_removal_enabled}
                        />
                        <span
                          >{$t('ui_remove_background_a8d1ab54')}
                          <small
                            >{stickerFile?.type === 'image/gif'
                              ? $t('ui_static_images_only_09e32e0a')
                              : guild?.sticker_background_removal_enabled
                                ? $t('ui_powered_by_rembg_f7a3cce3')
                                : $t('ui_not_enabled_on_this_server_419eb451')}</small
                          ></span
                        >
                      </label>
                    </div>
                  </div>
                {/if}
                <button
                  class="primary-button"
                  disabled={stickerBusy ||
                    !stickerFile ||
                    (guild?.stickers?.length ?? 0) >= (guild?.sticker_limit ?? 60)}
                  >{stickerBusy
                    ? $t('ui_creating_sticker_89db4a32')
                    : $t('ui_create_sticker_1c59eed4')}</button
                >
              </form>
            {/if}
            <div class="sticker-management-grid">
              {#each guild?.stickers ?? [] as sticker (entityKey(sticker))}
                {@const editable = canEditSticker(sticker)}
                <article class="sticker-management-item expression-management-item">
                  {#if sticker.media_hash}<img
                      src={assetUrl(sticker.media_hash, 'thumbnail_512', sticker.origin_domain)}
                      alt={sticker.name}
                    />{/if}
                  {#if stickerDrafts[entityKey(sticker)]}
                    <div class="expression-fields">
                      <label class="form-field compact-field">
                        <span>{$t('ui_name_dcd1d522')}</span>
                        <input
                          value={stickerDrafts[entityKey(sticker)].name}
                          minlength="2"
                          maxlength="30"
                          disabled={stickerBusy || !editable}
                          oninput={(event) =>
                            patchStickerDraft(sticker, { name: event.currentTarget.value })}
                        />
                      </label>
                      <label class="form-field compact-field">
                        <span>{$t('ui_description_526e0087')}</span>
                        <input
                          value={stickerDrafts[entityKey(sticker)].description}
                          minlength="2"
                          maxlength="100"
                          disabled={stickerBusy || !editable}
                          oninput={(event) =>
                            patchStickerDraft(sticker, {
                              description: event.currentTarget.value
                            })}
                        />
                      </label>
                      <label class="form-field compact-field">
                        <span
                          >{$t('ui_tags_1331275b')}
                          <small>{$t('ui_comma_separated_f46a9874')}</small></span
                        >
                        <input
                          value={stickerDrafts[entityKey(sticker)].tags}
                          maxlength="200"
                          disabled={stickerBusy || !editable}
                          oninput={(event) =>
                            patchStickerDraft(sticker, { tags: event.currentTarget.value })}
                        />
                      </label>
                      <p class="field-hint">
                        <strong
                          >{sticker.available === false
                            ? $t('ui_unavailable_ca184496')
                            : $t('ui_available_e6744473')}</strong
                        >
                        {$t('ui_availability_is_controlled_by_the_server_and__709ec772')}
                      </p>
                    </div>
                    {#if editable}
                      <div class="row-actions expression-actions">
                        <button
                          class="secondary-button"
                          type="button"
                          disabled={stickerBusy || !stickerDirty(sticker)}
                          onclick={() => void updateSticker(sticker)}
                          >{$t('ui_save_1509f561')}</button
                        >
                        <button
                          class="secondary-button danger-button"
                          type="button"
                          disabled={stickerBusy}
                          onclick={() => void deleteSticker(sticker)}
                          >{$t('ui_delete_e2d0a549')}</button
                        >
                      </div>
                    {:else}
                      <small class="field-hint"
                        >{$t('ui_only_its_creator_or_an_expression_manager_can_db7c4796')}</small
                      >
                    {/if}
                  {/if}
                </article>
              {:else}
                <p class="empty-copy">{$t('ui_this_guild_has_no_stickers_yet_dd22d2d9')}</p>
              {/each}
            </div>
          </div>
        </section>
      {/if}

      {#if !channelOnly}
        <GuildSafetyTools {guild} {currentUserRef} />
      {/if}

      {#if !channelOnly && canManageRoles}
        <section id="roles" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="shield" /></span>
            <div>
              <h2>{$t('ui_roles_c2533705')}</h2>
              <p>{$t('ui_group_permissions_into_roles_then_assign_them_c4244074')}</p>
            </div>
          </div>
          <div class="settings-split role-settings-split">
            <div class="settings-list-panel">
              <div class="settings-list-heading"><strong>{$t('ui_roles_c2533705')}</strong></div>
              {#each [...(guild.roles ?? [])].sort((a, b) => b.position - a.position) as role (entityKey(role))}
                <button
                  class:active={selectedRole && entityKey(selectedRole) === entityKey(role)}
                  class:drag-over={roleDropKey === entityKey(role)}
                  class:role-locked={!canManageRole(role)}
                  class="settings-list-item role-item"
                  type="button"
                  disabled={busy}
                  draggable={canReorderRole(role) && !busy && !reorderingRoles}
                  ondragstart={(event) => roleDragStart(event, role)}
                  ondragover={(event) => roleDragOver(event, role)}
                  ondragleave={() => (roleDropKey = null)}
                  ondrop={(event) => void roleDrop(event, role)}
                  ondragend={roleDragEnd}
                  onclick={() => selectRole(role)}
                >
                  <span class="role-drag-handle" aria-hidden="true">⠿</span>
                  <svg class="role-color-dot" viewBox="0 0 10 10" aria-hidden="true">
                    <circle cx="5" cy="5" r="5" fill={roleColorValue(role.color)} />
                  </svg>
                  <span>{role.name}</span>
                  {#if role.id === guild.id}<small>{$t('ui_default_21b111cb')}</small>{/if}
                </button>
              {/each}
              <form
                class="list-create-form"
                onsubmit={(event) => {
                  event.preventDefault();
                  void createRole();
                }}
              >
                <input
                  bind:value={newRoleName}
                  maxlength="100"
                  placeholder={$t('ui_new_role_500f2f9d')}
                  aria-label={$t('ui_new_role_name_78e73250')}
                  required
                  disabled={busy}
                />
                <button
                  class="icon-button"
                  disabled={busy}
                  aria-label={$t('ui_create_role_221163d6')}
                >
                  <Icon name="plus" size={17} />
                </button>
              </form>
            </div>

            <div class="settings-card editor-card role-editor">
              {#if selectedRole}
                <div class="editor-heading">
                  <div>
                    <span>{$t('ui_role_14736a2e')}</span>
                    <h3>{selectedRole.name}</h3>
                  </div>
                  {#if selectedRole.icon_hash}
                    <img
                      class="role-preview role-icon-preview"
                      src={assetUrl(
                        selectedRole.icon_hash,
                        'thumbnail_128',
                        selectedRole.origin_domain
                      )}
                      alt=""
                    />
                  {:else}
                    <svg class="role-preview" viewBox="0 0 38 38" aria-hidden="true">
                      <rect width="38" height="38" rx="13" fill={roleColor} />
                      <text x="19" y="24" text-anchor="middle" fill={roleContrastColor(roleColor)}
                        >{roleName.slice(0, 1) || 'R'}</text
                      >
                    </svg>
                  {/if}
                </div>
                {#if selectedRole.managed}
                  <p class="field-hint" role="note">{$t('ui_managed_bot_role_notice')}</p>
                  <p class="field-hint">{$t('ui_bot_role_scope_notice')}</p>
                {/if}
                <div
                  class="editor-tabs role-editor-tabs"
                  role="tablist"
                  aria-label={$t('ui_role_settings_4826ab3e')}
                >
                  <button
                    class:active={roleEditorTab === 'display'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'display'}
                    onclick={() => (roleEditorTab = 'display')}>{$t('ui_display_34e108c0')}</button
                  >
                  <button
                    class:active={roleEditorTab === 'permissions'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'permissions'}
                    onclick={() => (roleEditorTab = 'permissions')}
                    >{$t('ui_permissions_abccc78c')}</button
                  >
                  <button
                    class:active={roleEditorTab === 'members'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'members'}
                    onclick={() => (roleEditorTab = 'members')}
                    >{$t('ui_manage_members_7ce24d68')}</button
                  >
                </div>
                <form
                  class="settings-form"
                  onsubmit={(event) => {
                    event.preventDefault();
                    void saveRole();
                  }}
                >
                  {#if roleEditorTab === 'display'}
                    <div class="role-order-controls">
                      <div>
                        <strong>{$t('ui_role_position_075ca0b7')}</strong>
                        <small
                          >{$t('ui_drag_roles_in_the_list_to_reorder_them_change_61c2cf88')}</small
                        >
                      </div>
                    </div>
                    <div class="two-column-fields role-name-fields">
                      <label class="form-field compact-field">
                        <span>{$t('ui_name_dcd1d522')}</span>
                        <input
                          bind:value={roleName}
                          maxlength="100"
                          required
                          disabled={busy || selectedRole.id === guild.id || !canManageSelectedRole}
                        />
                      </label>
                    </div>
                    <fieldset
                      class="role-icon-field"
                      disabled={busy || roleIconBusy || !canManageSelectedRole}
                    >
                      <legend>{$t('ui_role_icon_8007f7fb')}</legend>
                      <small>
                        {$t('ui_shown_beside_members_names_in_chat_when_a_mem_96db6526')}
                      </small>
                      <div class="role-icon-controls">
                        {#if selectedRole.icon_hash}
                          <img
                            src={assetUrl(
                              selectedRole.icon_hash,
                              'thumbnail_128',
                              selectedRole.origin_domain
                            )}
                            alt={`${selectedRole.name} role icon`}
                          />
                        {/if}
                        <ImageUploadField
                          id="role-icon-upload"
                          file={roleIconFile}
                          disabled={busy || roleIconBusy || !canManageSelectedRole}
                          onSelect={(file, input) => void uploadRoleIcon(file, input)}
                        />
                        {#if selectedRole.icon_hash}
                          <button
                            class="secondary-button"
                            type="button"
                            disabled={busy || roleIconBusy || !canManageSelectedRole}
                            onclick={() => void deleteRoleIcon()}
                            >{$t('ui_remove_icon_93d6de71')}</button
                          >
                        {/if}
                      </div>
                      {#if roleIconBusy}<small role="status"
                          >{$t('ui_uploading_and_scanning_role_icon_90052e27')}</small
                        >{/if}
                      {#if roleIconError}<p class="form-error" role="alert">{roleIconError}</p>{/if}
                    </fieldset>
                    <fieldset
                      class="role-color-field"
                      disabled={busy || selectedRole.id === guild.id || !canManageSelectedRole}
                    >
                      <legend>{$t('ui_role_color_d33a0827')}</legend>
                      <small
                        >{$t('ui_members_use_the_color_of_their_highest_displa_816247c7')}</small
                      >
                      <div class="role-color-controls">
                        <button
                          class="role-color-default"
                          class:selected={roleColor === '#000000'}
                          type="button"
                          aria-label={$t('ui_use_the_default_role_color_6c6aecaa')}
                          aria-pressed={roleColor === '#000000'}
                          onclick={() => setRoleColor('#000000')}
                          ><span>✓</span><small>{$t('ui_default_21b111cb')}</small></button
                        >
                        <ColorPicker
                          bind:value={roleColor}
                          label={$t('ui_choose_a_custom_role_color_fc7cd895')}
                          caption={$t('ui_custom_494ca78f')}
                          selected={!roleColorPalette.includes(roleColor) &&
                            roleColor !== '#000000'}
                        />
                        <div class="role-color-swatches">
                          {#each roleColorPalette as color (color)}
                            <button
                              class:selected={roleColor === color}
                              type="button"
                              style={`--swatch: ${color}`}
                              aria-label={`Use role color ${color}`}
                              aria-pressed={roleColor === color}
                              onclick={() => setRoleColor(color)}
                            ></button>
                          {/each}
                        </div>
                      </div>
                      <label class="role-color-hex">
                        <span>{$t('ui_hex_69493e6f')}</span>
                        <input
                          value={roleColor}
                          maxlength="7"
                          pattern={'#?[0-9a-fA-F]{6}'}
                          onblur={normalizeRoleColorInput}
                          onkeydown={(event) => {
                            if (event.key === 'Enter') {
                              event.preventDefault();
                              normalizeRoleColorInput(event);
                            }
                          }}
                        />
                      </label>
                    </fieldset>
                    <div class="toggle-list">
                      <label class="toggle-row">
                        <span
                          ><strong>{$t('ui_display_separately_2cf34ad9')}</strong><small
                            >{$t('ui_hoist_members_in_this_role_f6451c23')}</small
                          ></span
                        >
                        <input
                          type="checkbox"
                          bind:checked={roleHoist}
                          disabled={busy || selectedRole.id === guild.id || !canManageSelectedRole}
                        />
                      </label>
                      <label class="toggle-row">
                        <span
                          ><strong>{$t('ui_allow_mentions_4b52561f')}</strong><small
                            >{$t('ui_let_anyone_mention_this_role_c4e8a4d8')}</small
                          ></span
                        >
                        <input
                          type="checkbox"
                          bind:checked={roleMentionable}
                          disabled={busy || selectedRole.id === guild.id || !canManageSelectedRole}
                        />
                      </label>
                    </div>
                  {:else if roleEditorTab === 'permissions'}
                    <label class="form-field permission-search">
                      <span>{$t('ui_search_permissions_0e099113')}</span>
                      <input
                        bind:value={permissionSearch}
                        placeholder={$t('ui_search_permissions_0e099113')}
                      />
                    </label>
                    {#if permissionChecked(Permission.ADMINISTRATOR)}
                      <div class="notice-banner warning-banner" role="alert">
                        <Icon name="shield" size={18} />
                        <span>
                          <strong
                            >{$t(
                              'ui_administrator_bypasses_every_channel_restrict_bc8d8e3f'
                            )}</strong
                          >
                          {$t('ui_only_grant_it_to_people_who_should_have_unres_95a0e45f')}
                        </span>
                      </div>
                    {/if}
                    <div class="permission-matrix">
                      {#each filteredPermissionGroups as group (group.name)}
                        <fieldset>
                          <legend>{group.name}</legend>
                          {#each group.permissions as permission (permission[0])}
                            <label class="permission-row">
                              <span>
                                <strong>{permission[0]}</strong>
                                <small>{permission[1]}</small>
                                {#if permission[3].dependencies.length}
                                  <small class="permission-dependencies">
                                    {$t('ui_also_requires_value0_68b47b3a', {
                                      value0: String(
                                        permissionDependencies(permission[3].dependencies)
                                      )
                                    })}
                                  </small>
                                {/if}
                              </span>
                              <input
                                type="checkbox"
                                checked={permissionChecked(permission[2])}
                                disabled={busy ||
                                  !canManageSelectedRole ||
                                  !hasPermission(permission[2])}
                                onchange={(event) =>
                                  togglePermission(permission[2], event.currentTarget.checked)}
                              />
                            </label>
                          {/each}
                        </fieldset>
                      {/each}
                    </div>
                  {:else}
                    <div class="role-member-picker">
                      <p class="field-hint">
                        {$t('ui_assign_this_role_to_guild_members_search_incl_d188ebc2')}
                      </p>
                      <label class="form-field member-search-field">
                        <span>{$t('ui_search_members_6497fc6f')}</span>
                        <input
                          bind:value={roleMemberSearch}
                          type="search"
                          placeholder={$t('ui_search_by_name_username_or_instance_419b763f')}
                          autocomplete="off"
                        />
                      </label>
                      {#if roleMemberSearchBusy}
                        <p class="field-hint" role="status">
                          {$t('ui_searching_members_4fbc9742')}
                        </p>
                      {:else if roleMemberSearchError}
                        <p class="form-error" role="alert">{roleMemberSearchError}</p>
                      {/if}
                      {#each visibleRoleMembers as member (entityKey(member.user))}
                        <label class="permission-row role-member-row">
                          <span>
                            <strong>{member.nickname ?? userDisplayName(member.user)}</strong>
                            <small
                              >{userPublicHandle(member.user) ??
                                $t('ui_profile_unavailable_158e5a22')}</small
                            >
                          </span>
                          <input
                            type="checkbox"
                            checked={member.role_ids.includes(selectedRole.id)}
                            disabled={busy ||
                              selectedRole.id === guild.id ||
                              selectedRole.managed ||
                              !canManageSelectedRole ||
                              !canManageMember(member)}
                            onchange={(event) =>
                              void toggleMemberRole(
                                member,
                                selectedRole!,
                                event.currentTarget.checked
                              )}
                          />
                        </label>
                      {/each}
                      {#if roleMemberSearch.trim() && !roleMemberSearchBusy && !visibleRoleMembers.length}
                        <div class="empty-state compact-empty">
                          <span><Icon name="search" /></span>
                          <h3>{$t('ui_no_matching_members_a2d9d134')}</h3>
                          <p>{$t('ui_try_a_display_name_username_nickname_or_insta_0d7aa6eb')}</p>
                        </div>
                      {/if}
                      {#if membersHaveMore && !roleMemberSearch.trim()}
                        <button
                          class="secondary-button settings-load-more"
                          type="button"
                          disabled={membersLoadingMore}
                          onclick={loadMoreMembers}
                        >
                          {membersLoadingMore
                            ? $t('ui_loading_ba3bbbe1')
                            : $t('ui_load_more_members_01c037a8')}
                        </button>
                      {/if}
                    </div>
                  {/if}
                  <div class="form-actions spread-actions">
                    {#if selectedRole.id !== guild.id && !selectedRole.managed && canManageSelectedRole}
                      <button
                        class="danger-text-button"
                        type="button"
                        disabled={busy}
                        onclick={deleteRole}
                      >
                        <Icon name="trash" size={16} />{$t('ui_delete_role_ac18d11a')}
                      </button>
                    {:else if !selectedRole.managed}
                      <span class="field-hint"
                        >{$t('ui_the_default_role_cannot_be_deleted_f09efff5')}</span
                      >
                    {/if}
                    <button
                      class="primary-button"
                      disabled={busy || !roleDirty || !canManageSelectedRole}
                      >{$t('ui_save_role_c7523b15')}</button
                    >
                  </div>
                </form>
              {/if}
            </div>
          </div>
        </section>
      {/if}

      {#if !channelOnly && canViewAuditLog}
        <section id="audit-log" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="clock" /></span>
            <div>
              <h2>{$t('ui_audit_log_e4d36f9a')}</h2>
              <p>{$t('ui_review_administrative_actions_affected_resour_5b0684a3')}</p>
            </div>
          </div>
          <div class="settings-card">
            <GuildAuditLog {guild} members={currentMembers} />
          </div>
        </section>
      {/if}

      {#if !channelOnly && canViewMembers}
        <section id="members" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="users" /></span>
            <div>
              <h2>{$t('ui_members_1044a4c0')}</h2>
              <p>{$t('ui_review_your_guild_s_members_manage_roles_and__d0d3faf9')}</p>
            </div>
          </div>
          {#key guildId}
            <GuildMemberManagement
              members={currentMembers}
              roles={(guild.roles ?? []).filter((role) => role.id !== guild?.id)}
              hasMore={membersHaveMore}
              loading={membersLoadingMore}
              busy={busy || memberModerationBusy}
              loadMore={loadMoreMembers}
              {canManageMember}
              {canManageRole}
              toggleRole={toggleMemberRole}
              canPrune={canManageGuild && canKickMembers}
            >
              {#snippet actions(member: GuildMemberSummary, activeTimeout: boolean)}
                {#if canModerateMembers && isModeratableMember(member)}
                  {#if canTimeoutMembers}
                    <button
                      class="secondary-button small-button"
                      type="button"
                      disabled={busy || memberModerationBusy}
                      onclick={(event) =>
                        void openMemberModeration(
                          member,
                          activeTimeout ? 'untimeout' : 'timeout',
                          event.currentTarget
                        )}
                    >
                      {activeTimeout ? $t('ui_remove_timeout_f9104d86') : $t('ui_timeout_70594d93')}
                    </button>
                  {/if}
                  {#if canKickMembers}
                    <button
                      class="secondary-button small-button"
                      type="button"
                      disabled={busy || memberModerationBusy}
                      onclick={(event) =>
                        void openMemberModeration(member, 'kick', event.currentTarget)}
                      >{$t('ui_kick_37ca3cfb')}</button
                    >
                  {/if}
                  {#if canBanMembers}
                    <button
                      class="danger-text-button small-button"
                      type="button"
                      disabled={busy || memberModerationBusy}
                      onclick={(event) =>
                        void openMemberModeration(member, 'ban', event.currentTarget)}
                      >{$t('ui_ban_520ed297')}</button
                    >
                  {/if}
                {/if}
              {/snippet}
            </GuildMemberManagement>
          {/key}
          {#if canBanMembers}
            <div class="settings-card sanction-list">
              <div class="settings-list-heading">
                <div>
                  <strong>{$t('ui_active_user_bans_1c52c3ec')}</strong>
                  <p>{$t('ui_expired_bans_disappear_automatically_and_no_l_53e64095')}</p>
                </div>
                <span>{bans.length}</span>
              </div>
              {#each bans as ban (entityKey(ban.user))}
                <article class="sanction-row">
                  <span class="avatar avatar-medium">
                    {#if ban.user.avatar_hash}
                      <img src={assetUrl(ban.user.avatar_hash, 'thumbnail_128', ban.user)} alt="" />
                    {:else}
                      {ban.user.profile_resolved === false
                        ? '•'
                        : ban.user.username.slice(0, 1).toUpperCase()}
                    {/if}
                  </span>
                  <div>
                    <strong>{userDisplayName(ban.user)}</strong>
                    <small
                      >{userPublicHandle(ban.user) ?? $t('ui_profile_unavailable_158e5a22')}</small
                    >
                    <span>{ban.reason ?? $t('ui_no_reason_provided_d31817f1')}</span>
                  </div>
                  <span
                    >{ban.expires_at
                      ? `Until ${formatDateTime(ban.expires_at)}`
                      : $t('ui_permanent_455a9549')}</span
                  >
                  <button
                    class="secondary-button"
                    type="button"
                    disabled={busy}
                    onclick={() => void unbanUser(ban)}>{$t('ui_unban_25fb5989')}</button
                  >
                </article>
              {:else}
                <p class="field-hint">{$t('ui_no_active_user_bans_af948a57')}</p>
              {/each}
            </div>
          {/if}

          {#if canBanInstances}
            <div class="settings-card instance-ban-card">
              <div class="settings-list-heading">
                <div>
                  <strong>{$t('ui_federated_instance_bans_57da45aa')}</strong>
                  <p>{$t('ui_block_an_entire_instance_from_participating_i_83ad3283')}</p>
                </div>
              </div>
              <div class="federation-warning" role="note">
                <Icon name="shield" size={20} />
                <div>
                  <strong>{$t('ui_this_removes_every_current_member_from_that_i_b8646108')}</strong>
                  <p>{$t('ui_their_home_will_be_instructed_to_delete_cache_10b17cb9')}</p>
                </div>
              </div>
              <div class="instance-ban-form">
                <label class="form-field">
                  <span>{$t('ui_exact_instance_domain_7add23fa')}</span>
                  <input
                    bind:value={instanceBanDomain}
                    maxlength="253"
                    placeholder="chat.example.net"
                    disabled={busy}
                  />
                </label>
                <label class="form-field">
                  <span>{$t('ui_duration_4fc52a3c')}</span>
                  <select bind:value={instanceBanDuration} disabled={busy}>
                    <option value="3600">{$t('ui_1_hour_f8b8883f')}</option>
                    <option value="86400">{$t('ui_1_day_fa665d95')}</option>
                    <option value="604800">{$t('ui_7_days_7f920bb6')}</option>
                    <option value="2592000">{$t('ui_30_days_ffd72805')}</option>
                    <option value="permanent">{$t('ui_permanent_455a9549')}</option>
                  </select>
                </label>
                <label class="form-field instance-ban-reason">
                  <span>{$t('ui_reason_f81ab834')} <small>optional</small></span>
                  <input
                    bind:value={instanceBanReason}
                    maxlength="512"
                    placeholder={$t('ui_visible_in_the_audit_log_78684c70')}
                    disabled={busy}
                  />
                </label>
                <button
                  class="danger-button"
                  type="button"
                  disabled={busy || !instanceBanDomain.trim()}
                  onclick={() => void banFederatedInstance()}
                  >{$t('ui_ban_instance_4a261781')}</button
                >
              </div>
              <div class="sanction-list embedded-list">
                {#each instanceBans as ban (ban.instance_domain)}
                  <article class="sanction-row instance-sanction-row">
                    <span class="section-icon"><Icon name="globe" /></span>
                    <div>
                      <strong>{ban.instance_domain}</strong>
                      <span>{ban.reason ?? $t('ui_no_reason_provided_d31817f1')}</span>
                    </div>
                    <span
                      >{ban.expires_at
                        ? `Until ${formatDateTime(ban.expires_at)}`
                        : $t('ui_permanent_455a9549')}</span
                    >
                    <button
                      class="secondary-button"
                      type="button"
                      disabled={busy}
                      onclick={() => void unbanFederatedInstance(ban)}
                      >{$t('ui_remove_ban_a3fde3cd')}</button
                    >
                  </article>
                {:else}
                  <p class="field-hint">
                    {$t('ui_no_federated_instances_are_banned_from_this_g_c29d93d3')}
                  </p>
                {/each}
              </div>
            </div>
          {/if}
        </section>
      {/if}

      {#if !channelOnly && canAccessInvites}
        <section id="invites" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="globe" /></span>
            <div>
              <h2>{$t('ui_invites_f212a985')}</h2>
              <p>{$t('ui_create_bounded_links_and_revoke_them_whenever_8155f732')}</p>
            </div>
          </div>
          {#if canCreateInvites}
            <form
              class="settings-card quick-create invite-create"
              onsubmit={(event) => {
                event.preventDefault();
                void createInvite();
              }}
            >
              <div>
                <strong>{$t('ui_create_an_invite_33cb9bb7')}</strong>
                <p>{$t('ui_choose_an_optional_destination_lifetime_and_u_90b16eeb')}</p>
              </div>
              <label class="form-field compact-field">
                <span>{$t('ui_destination_293d404a')}</span>
                <select bind:value={inviteChannel} disabled={busy}>
                  <option value="">{$t('ui_guild_landing_channel_58aff412')}</option>
                  {#each (guild.channels ?? []).filter((channel) => channel.type !== 4 && channelHasPermission(channel, Permission.CREATE_INVITE)) as channel (entityKey(channel))}
                    <option value={entityKey(channel)}>{channel.name}</option>
                  {/each}
                </select>
              </label>
              <label class="form-field compact-field">
                <span>{$t('ui_expires_f6725f3a')}</span>
                <select bind:value={inviteMaxAge} disabled={busy}>
                  <option value="1800">{$t('ui_30_minutes_f01a042b')}</option>
                  <option value="3600">{$t('ui_1_hour_f8b8883f')}</option>
                  <option value="21600">{$t('ui_6_hours_4105ae3b')}</option>
                  <option value="86400">{$t('ui_1_day_fa665d95')}</option>
                  <option value="604800">{$t('ui_7_days_7f920bb6')}</option>
                  <option value="">{$t('ui_never_6300ef80')}</option>
                </select>
              </label>
              <label class="form-field compact-field">
                <span>{$t('ui_maximum_uses_3a104290')}</span>
                <input
                  bind:value={inviteMaxUses}
                  type="number"
                  min="1"
                  max="100"
                  placeholder={$t('ui_unlimited_11dde17d')}
                  disabled={busy}
                />
              </label>
              <details class="invite-advanced-options">
                <summary>{$t('ui_advanced_options_9443ff69')}</summary>
                <div class="invite-advanced-grid">
                  <label class="toggle-row">
                    <input type="checkbox" bind:checked={inviteTemporary} disabled={busy} />
                    <span
                      ><strong>{$t('ui_temporary_membership_baa8c83e')}</strong><small
                        >{$t('ui_remove_members_when_their_final_voice_connect_a99a95cc')}</small
                      ></span
                    >
                  </label>
                  <label class="toggle-row">
                    <input type="checkbox" bind:checked={inviteUnique} disabled={busy} />
                    <span
                      ><strong>{$t('ui_always_create_a_new_code_a6cbeb3e')}</strong><small
                        >{$t('ui_when_off_an_equivalent_reusable_invite_may_be_70e01529')}</small
                      ></span
                    >
                  </label>
                  <label class="form-field compact-field">
                    <span>{$t('ui_voice_invite_target_418b6c06')}</span>
                    <select bind:value={inviteTargetType} disabled={busy}>
                      <option value="">{$t('ui_none_dc937b59')}</option>
                      <option value="stream">{$t('ui_member_s_go_live_stream_ecf509d4')}</option>
                    </select>
                    <small>{$t('ui_voice_targets_require_a_voice_or_stage_destin_68719e6f')}</small>
                  </label>
                  {#if inviteTargetType === 'stream'}
                    <label class="form-field compact-field">
                      <span>{$t('ui_streaming_member_16b000f1')}</span>
                      <GuildMemberPicker
                        guildRef={entityRef(guild)}
                        fallbackUsers={currentMembers.map((member) => member.user)}
                        value={inviteTargetUser ? [inviteTargetUser] : []}
                        placeholder={$t('ui_choose_a_member_c6aaa720')}
                        disabled={busy}
                        onChange={(values) => (inviteTargetUser = values[0] ?? '')}
                      />
                      <small
                        >{$t('ui_the_member_must_currently_be_able_to_stream_i_c7e43a03')}</small
                      >
                    </label>
                  {/if}
                  <label class="form-field compact-field">
                    <span>{$t('ui_scheduled_event_e7b77e0c')}</span>
                    <select bind:value={inviteScheduledEvent} disabled={busy}>
                      <option value="">{$t('ui_no_event_association_9961e003')}</option>
                      {#each scheduledEvents as scheduledEvent (scheduledEventRef(scheduledEvent))}
                        <option value={scheduledEventRef(scheduledEvent)}
                          >{scheduledEvent.name} · {formatDateTime(
                            scheduledEvent.scheduled_start_time
                          )}</option
                        >
                      {/each}
                    </select>
                    <small>{$t('ui_event_details_are_included_independently_of_a_35587c7a')}</small>
                  </label>
                  {#if canManageRoles}
                    <label class="form-field compact-field">
                      <span>{$t('ui_roles_optional_4eff2966')}</span>
                      <select multiple bind:value={inviteRoleIds} size="5" disabled={busy}>
                        {#each (guild.roles ?? []).filter((role) => role.id !== guild?.id && canManageRole(role) && !role.managed) as role (entityKey(role))}
                          <option value={entityRef(role)}>{role.name}</option>
                        {/each}
                      </select>
                      <small
                        >{$t('ui_members_receive_these_roles_when_they_accept__21aeadd6')}</small
                      >
                    </label>
                  {/if}
                </div>
              </details>
              <button class="primary-button" disabled={busy}>
                <Icon name="plus" size={16} />{$t('ui_create_invite_9f395b8f')}
              </button>
            </form>
            {#if createdInvite}
              <div class="settings-card created-invite-card" role="status">
                <div>
                  <strong>{$t('ui_your_new_invite_is_ready_4d616ec7')}</strong>
                  <p>{$t('ui_anyone_with_this_link_can_use_it_until_its_li_1e9f9a48')}</p>
                </div>
                <code>{inviteUrl(createdInvite.code)}</code>
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy}
                  onclick={() => copyInvite(createdInvite!)}
                >
                  <Icon name="copy" size={17} />{$t('ui_copy_invite_link_5c58cd79')}
                </button>
              </div>
            {/if}
          {/if}

          {#if canManageGuild || canViewAuditLog}
            <div class="settings-card invite-list">
              <div class="settings-list-heading">
                <strong>{$t('ui_active_invites_f84513a5')}</strong>
                <span>{invites.length}</span>
              </div>
              {#each invites as invite (invite.code)}
                <article class="invite-row">
                  <div>
                    <code>{invite.code}</code>
                    <span>
                      {#if invite.uses !== undefined}
                        {$t('ui_value0_value1_uses_80200e00', {
                          value0: String(invite.uses),
                          value1: String(invite.max_uses ? ` / ${invite.max_uses}` : '')
                        })}{/if}{invite.expires_at
                        ? `expires ${formatDateTime(invite.expires_at)}`
                        : $t('ui_never_expires_fd791f1a')}{invite.temporary
                        ? $t('ui_temporary_11eb04d1')
                        : ''}{invite.target_type
                        ? ` · targets ${invite.target_type.replaceAll('_', ' ')}`
                        : ''}{invite.scheduled_event_id
                        ? $t('ui_includes_scheduled_event_16e4938d')
                        : ''}{invite.role_ids?.length
                        ? ` · grants ${invite.role_ids.length} role${invite.role_ids.length === 1 ? '' : 's'}`
                        : ''}{invite.target_user_count
                        ? ` · limited to ${invite.target_user_count} user${invite.target_user_count === 1 ? '' : 's'}`
                        : ''}
                    </span>
                  </div>
                  <button
                    class="icon-button"
                    type="button"
                    disabled={busy}
                    aria-label={`Copy invite ${invite.code}`}
                    onclick={() => copyInvite(invite)}
                  >
                    <Icon name="copy" size={17} />
                  </button>
                  {#if canRevokeInvite(invite)}
                    <button
                      class="icon-button danger-icon-button"
                      type="button"
                      disabled={busy}
                      aria-label={`Revoke invite ${invite.code}`}
                      onclick={() => revokeInvite(invite)}
                    >
                      <Icon name="trash" size={17} />
                    </button>
                  {/if}
                </article>
              {:else}
                <div class="empty-state compact-empty">
                  <span><Icon name="globe" /></span>
                  <h3>{$t('ui_no_active_invites_f07a398e')}</h3>
                  <p>{$t('ui_create_one_when_you_are_ready_to_welcome_some_f00fac06')}</p>
                </div>
              {/each}
            </div>
          {/if}
        </section>
      {/if}

      {#if !channelOnly}
        <section id="guild-lifecycle" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="logout" /></span>
            <div>
              <h2>{$t('ui_guild_access_dad929fc')}</h2>
              <p>{$t('ui_leave_this_community_or_manage_its_ownership__4b81f060')}</p>
            </div>
          </div>
          {#if isGuildOwner}
            <div class="settings-card guild-ownership-card">
              <div class="settings-list-heading">
                <div>
                  <strong>{$t('ui_transfer_ownership_3667f939')}</strong>
                  <p>{$t('ui_ownership_can_be_transferred_to_any_eligible__0e5a33d4')}</p>
                </div>
              </div>
              <div class="inline-settings-form">
                <label class="form-field">
                  <span>{$t('ui_new_owner_1cf8bc7f')}</span>
                  <GuildMemberPicker
                    guildRef={entityRef(guild)}
                    fallbackUsers={ownershipCandidates.map((member) => member.user)}
                    value={ownershipTarget ? [ownershipTarget] : []}
                    placeholder={$t('ui_choose_a_member_c6aaa720')}
                    disabled={busy}
                    filterUser={(user) =>
                      user.account_type !== 'bot' &&
                      user.bot !== true &&
                      entityRef(user) !== currentUserRef}
                    onChange={(values, users) => {
                      ownershipTarget = values[0] ?? '';
                      ownershipTargetUser = users[0] ?? null;
                    }}
                  />
                </label>
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy || !ownershipTarget}
                  onclick={requestOwnershipTransfer}>{$t('ui_transfer_ownership_3667f939')}</button
                >
              </div>
              {#if !ownershipCandidates.length}
                <p class="field-hint">
                  {$t('ui_no_other_eligible_human_member_can_receive_ow_f6df56c4')}
                </p>
              {/if}
            </div>
            <div class="settings-card danger-zone guild-delete-card">
              <div>
                <span>{$t('ui_permanent_action_583317a2')}</span>
                <h3>{$t('ui_delete_guild_48611aaa')}</h3>
                <p>{$t('ui_deletes_all_guild_data_at_its_home_and_sends__dd1c5201')}</p>
              </div>
              <button
                class="danger-button"
                type="button"
                disabled={busy}
                onclick={requestDeleteGuild}
              >
                <Icon name="trash" size={16} />{$t('ui_delete_guild_48611aaa')}
              </button>
            </div>
          {:else}
            <div class="settings-card danger-zone guild-leave-card">
              <div>
                <span>{$t('ui_membership_9feceb93')}</span>
                <h3>{$t('ui_leave_guild_92d9a021')}</h3>
                <p>{$t('ui_you_will_need_a_new_valid_invitation_before_y_efc60a8d')}</p>
              </div>
              <button
                class="danger-button"
                type="button"
                disabled={busy}
                onclick={requestLeaveGuild}
              >
                <Icon name="logout" size={16} />{$t('ui_leave_guild_92d9a021')}
              </button>
            </div>
          {/if}
        </section>
      {/if}
    {/if}

    {#if !channelOnly}
      <footer class="settings-footer">
        <span>{guild?.name ?? $t('ui_guild_298ffc49')}</span>
        <span>{guild ? `${guild.id}@${guild.origin_domain}` : ''}</span>
      </footer>
    {/if}
  </section>
</main>

{#if memberModerationDialog}
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
      aria-label={$t('ui_cancel_moderation_action_1747fc6b')}
      onclick={cancelMemberModeration}
    ></button>
    <div
      bind:this={memberModerationElement}
      class="channel-dialog confirmation-dialog member-moderation-dialog"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-labelledby="member-moderation-title"
      aria-describedby="member-moderation-description"
      aria-busy={memberModerationBusy}
      onkeydown={memberModerationKeydown}
    >
      <header>
        <div>
          <p>{$t('ui_member_moderation_0e542933')}</p>
          <h2 id="member-moderation-title">{memberModerationTitle(memberModerationDialog)}</h2>
        </div>
        <button type="button" aria-label={$t('ui_cancel_19766ed6')} onclick={cancelMemberModeration}
          >×</button
        >
      </header>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void submitMemberModeration();
        }}
      >
        <div class="moderation-dialog-member">
          <span class="avatar avatar-medium">
            {#if memberModerationDialog.member.user.avatar_hash}
              <img
                src={assetUrl(
                  memberModerationDialog.member.user.avatar_hash,
                  'thumbnail_128',
                  memberModerationDialog.member.user
                )}
                alt=""
              />
            {:else}
              {memberModerationDialog.member.user.profile_resolved === false
                ? '•'
                : memberModerationDialog.member.user.username.slice(0, 1).toUpperCase()}
            {/if}
          </span>
          <div>
            <strong
              >{memberModerationDialog.member.nickname ??
                userDisplayName(memberModerationDialog.member.user)}</strong
            >
            <small
              >{userPublicHandle(memberModerationDialog.member.user) ??
                $t('ui_profile_unavailable_158e5a22')}</small
            >
          </div>
        </div>
        <p id="member-moderation-description" class="confirmation-copy">
          {memberModerationDescription(memberModerationDialog)}
        </p>
        {#if memberModerationDialog.action === 'timeout'}
          <label class="channel-dialog-field">
            {$t('ui_duration_4fc52a3c')}
            <select bind:value={timeoutDuration} disabled={memberModerationBusy}>
              <option value="600">{$t('ui_10_minutes_b075b6e1')}</option>
              <option value="3600">{$t('ui_1_hour_f8b8883f')}</option>
              <option value="86400">{$t('ui_1_day_fa665d95')}</option>
              <option value="604800">{$t('ui_7_days_7f920bb6')}</option>
              <option value="2419200">{$t('ui_28_days_56a795b7')}</option>
              <option value="permanent">{$t('ui_indefinite_674087cf')}</option>
            </select>
          </label>
        {:else if memberModerationDialog.action === 'ban'}
          <div class="moderation-inline-selects">
            <label class="channel-dialog-field">
              {$t('ui_duration_4fc52a3c')}
              <select bind:value={banDuration} disabled={memberModerationBusy}>
                <option value="3600">{$t('ui_1_hour_f8b8883f')}</option>
                <option value="86400">{$t('ui_1_day_fa665d95')}</option>
                <option value="604800">{$t('ui_7_days_7f920bb6')}</option>
                <option value="2592000">{$t('ui_30_days_ffd72805')}</option>
                <option value="permanent">{$t('ui_permanent_455a9549')}</option>
              </select>
            </label>
            <label class="channel-dialog-field">
              {$t('ui_delete_messages_708b6094')}
              <select bind:value={banDeleteSeconds} disabled={memberModerationBusy}>
                <option value="0">{$t('ui_none_dc937b59')}</option>
                <option value="3600">{$t('ui_previous_hour_e680d771')}</option>
                <option value="86400">{$t('ui_previous_day_e4a1e89e')}</option>
                <option value="604800">{$t('ui_previous_7_days_c6327b7b')}</option>
              </select>
            </label>
          </div>
        {/if}
        <label class="channel-dialog-field">
          {$t('ui_reason_f81ab834')}
          <span class="field-optional">{$t('ui_optional_59be7133')}</span>
          <textarea
            bind:value={moderationReason}
            maxlength="512"
            rows="3"
            placeholder={$t('ui_visible_in_the_guild_audit_log_5958620b')}
            disabled={memberModerationBusy}
          ></textarea>
        </label>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <footer>
          <button
            bind:this={memberModerationCancel}
            class="secondary-button"
            type="button"
            onclick={cancelMemberModeration}>{$t('ui_cancel_19766ed6')}</button
          >
          <button
            class={memberModerationDialog.action === 'untimeout'
              ? 'primary-button'
              : 'danger-button'}
            type="submit"
            disabled={memberModerationBusy}
          >
            {memberModerationBusy
              ? $t('ui_applying_3329a9bb')
              : memberModerationDialog.action === 'untimeout'
                ? $t('ui_remove_timeout_f9104d86')
                : memberModerationDialog.action === 'timeout'
                  ? $t('ui_apply_timeout_e6df7c71')
                  : memberModerationDialog.action === 'kick'
                    ? $t('ui_kick_member_175f3c4c')
                    : $t('ui_ban_member_5af6225d')}
          </button>
        </footer>
      </form>
    </div>
  </div>
{/if}

{#if destructiveConfirmation}
  <div use:portal class="channel-dialog-layer">
    <button
      class="channel-dialog-backdrop"
      type="button"
      disabled={busy}
      aria-label={$t('ui_cancel_destructive_action_50a03457')}
      onclick={closeDestructiveConfirmation}
    ></button>
    <div
      bind:this={confirmationDialog}
      class="channel-dialog confirmation-dialog"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-labelledby="destructive-confirmation-title"
      aria-describedby="destructive-confirmation-description"
      aria-busy={busy}
      onkeydown={confirmationKeydown}
    >
      <header>
        <div>
          <p>{$t('ui_destructive_action_627d4cfc')}</p>
          <h2 id="destructive-confirmation-title">{destructiveConfirmation.title}</h2>
        </div>
        <button
          type="button"
          disabled={busy}
          aria-label={$t('ui_cancel_19766ed6')}
          onclick={closeDestructiveConfirmation}>×</button
        >
      </header>
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void confirmDestructiveAction();
        }}
      >
        <div class="confirmation-copy">
          <p id="destructive-confirmation-description">{destructiveConfirmation.description}</p>
        </div>
        {#if destructiveConfirmation.kind === 'guild-delete'}
          <label class="channel-dialog-field">
            {$t('ui_type_baaddf70')} <strong>{destructiveConfirmation.verificationText}</strong>
            {$t('ui_to_confirm_83918ed6')}
            <input
              bind:value={confirmationVerification}
              autocomplete="off"
              disabled={busy}
              required
            />
          </label>
        {/if}
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <footer>
          <button
            bind:this={confirmationCancelButton}
            class="secondary-button"
            type="button"
            disabled={busy}
            onclick={closeDestructiveConfirmation}>{$t('ui_cancel_19766ed6')}</button
          >
          <button
            class="danger-button"
            disabled={busy ||
              (destructiveConfirmation.kind === 'guild-delete' &&
                confirmationVerification !== destructiveConfirmation.verificationText)}
          >
            {busy
              ? destructiveBusyLabel(destructiveConfirmation)
              : destructiveConfirmation.confirmLabel}
          </button>
        </footer>
      </form>
    </div>
  </div>
{/if}
