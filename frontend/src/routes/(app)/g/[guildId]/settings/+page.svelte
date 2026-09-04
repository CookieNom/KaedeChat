<script lang="ts">
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
        target: MemberSummary;
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
  let memberPage = $state(0);
  let memberSearch = $state('');
  let memberSearchResults = $state<MemberSummary[]>([]);
  let memberSearchBusy = $state(false);
  let memberSearchError = $state('');
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
    memberSearch = '';
    memberSearchResults = [];
    memberSearchError = '';
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
    error = 'This guild is unavailable or you no longer have access.';
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
  const currentMemberSearchResults = $derived(liveMemberRows(memberSearchResults));
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
  const visibleMembers = $derived(
    memberSearch.trim()
      ? currentMemberSearchResults
      : currentMembers.slice(memberPage * MEMBER_PAGE_SIZE, (memberPage + 1) * MEMBER_PAGE_SIZE)
  );
  const visibleRoleMembers = $derived(
    roleMemberSearch.trim() ? currentRoleMemberSearchResults : currentMembers
  );
  const memberPageCount = $derived(
    Math.max(1, Math.ceil(currentMembers.length / MEMBER_PAGE_SIZE) + (membersHaveMore ? 1 : 0))
  );
  const memberHasPreviousPage = $derived(!memberSearch.trim() && memberPage > 0);
  const memberHasNextPage = $derived(
    !memberSearch.trim() &&
      ((memberPage + 1) * MEMBER_PAGE_SIZE < currentMembers.length || membersHaveMore)
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
      error = userErrorMessage(caught, 'Could not load this channel’s webhooks. Try again.');
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

  function cancelableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolveDelay, rejectDelay) => {
      if (signal.aborted) {
        rejectDelay(new DOMException('Operation cancelled', 'AbortError'));
        return;
      }
      const timeout = window.setTimeout(finish, milliseconds);
      function finish() {
        signal.removeEventListener('abort', cancel);
        resolveDelay();
      }
      function cancel() {
        window.clearTimeout(timeout);
        rejectDelay(new DOMException('Operation cancelled', 'AbortError'));
      }
      signal.addEventListener('abort', cancel, { once: true });
    });
  }

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

  function editForumDefaultReaction(event: Event) {
    channelForumReaction = (event.currentTarget as HTMLInputElement).value;
    channelForumReactionId = null;
  }

  function changeForumEncryptionRequirement(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    if (!input.checked) {
      channelForumE2EE = false;
      return;
    }
    const confirmed = window.confirm(
      'Require end-to-end encryption for future posts? This policy is permanent once saved and affects only posts created afterward; existing posts do not change. Titles and title search remain readable to the server, but each future post establishes its encryption keys before its starter body, files, or replies are sent. Server starter previews, message search, link and GIF previews, file previews, malware scanning, and PhotoDNA scanning will stop. Webhooks receive no access automatically; a verified webhook device can receive only future post content after a server administrator grants it and the post establishes a rekey and history floor. Verified participant-mode apps follow the same future-only admission rule. Notifications become generic, while participants, timing, and message-size metadata remain visible. Participant identities remain unverified until everyone compares the safety number through a separate trusted channel. Losing the synchronized account vault, all trusted local state, and the recovery backup loses encrypted post history. Removed members, apps, and webhooks keep content they already received.'
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
        error = userErrorMessage(caught, 'Could not load channel permissions. Try again.');
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
      notice = 'Channel permissions saved.';
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
      notice = 'Channel override reset to inherited permissions.';
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
      notice = 'Permissions synced with the category.';
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
        rekey
          ? 'Create fresh encryption keys for the current channel members? Removed members and revoked devices will not receive the new keys.'
          : activationWarning
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
        notice = 'Compare this safety number with members through a separate trusted channel.';
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
      notice = 'Notification settings saved.';
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
      else if (channelOnly) error = 'This channel is unavailable or you no longer have access.';
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
                'Region overrides are temporarily unavailable.'
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
      if (allows(Permission.CREATE_GUILD_EXPRESSIONS | Permission.MANAGE_GUILD_EXPRESSIONS)) {
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
              memberPage = 0;
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
      error = userErrorMessage(caught, 'Could not load guild settings. Try again.');
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
          notice = 'These settings changed elsewhere. The latest version has been loaded.';
        }
        return false;
      }
      error = userErrorMessage(caught, 'The change could not be saved. Try again.');
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
    if (!canManageGuild) return;
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
      notice = 'Overview saved.';
    });
  }

  async function uploadGuildAsset(kind: GuildAssetKind, file: File) {
    const controller = routeController;
    if (busy || loading || !guild || !canManageGuildAssets || !controller) return;
    if (!acceptedImageTypes.has(file.type)) {
      guildAssetError = 'Choose a PNG, JPEG, GIF, or WebP image.';
      error = '';
      notice = '';
      return;
    }
    if (file.size < 1) {
      guildAssetError = 'Choose a non-empty image file.';
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
        'Could not update the guild image. Choose the file again and retry.'
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
      error = 'Choose a PNG, JPEG, GIF, or WebP image.';
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
        'Could not create the emoji. Choose the file again and retry.'
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
      error = userErrorMessage(caught, 'Could not delete the emoji. Try again.');
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

  async function updateEmoji(emoji: CustomEmoji) {
    const draftValue = emojiDrafts[entityKey(emoji)];
    if (!guild || !draftValue || !canEditEmoji(emoji) || emojiBusy) return;
    if (!draftValue.name.trim()) {
      error = 'Emoji names cannot be blank.';
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
      error = userErrorMessage(caught, 'Could not update the emoji. Try again.');
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
      error = 'Choose a PNG, JPEG, GIF, or WebP image.';
      return;
    }
    if (file.size > (guild.sticker_max_bytes ?? 524288)) {
      error = `Sticker images can be at most ${Math.ceil((guild.sticker_max_bytes ?? 524288) / 1024)} KiB.`;
      return;
    }
    const cleanedName = stickerName.trim();
    const cleanedDescription = stickerDescription.trim();
    if (!validStickerName(cleanedName)) {
      error = 'Sticker names must contain 2–30 meaningful characters.';
      return;
    }
    if (!validStickerDescription(cleanedDescription)) {
      error = 'Sticker descriptions must be empty or contain 2–100 characters.';
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
        'Could not create the sticker. Choose the file again and retry.'
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
      error = userErrorMessage(caught, 'Could not delete the sticker. Try again.');
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

  async function updateSticker(sticker: GuildSticker) {
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
      error = 'Sticker names must contain 2–30 meaningful characters.';
      return;
    }
    if (!validStickerDescription(cleanedDescription)) {
      error = 'Sticker descriptions must be empty or contain 2–100 characters.';
      return;
    }
    if (
      !tags.length ||
      tags.length > 10 ||
      tags.some((tag) => tag.length > 100) ||
      tags.join(',').length > 200
    ) {
      error = 'Sticker tags must contain 1–10 unique values and use at most 200 characters.';
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
      error = userErrorMessage(caught, 'Could not update the sticker. Try again.');
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
    if (!canEditSelectedChannel || !selectedChannel) return;
    const target = selectedChannel;
    return run(async (targetGuild, generation) => {
      const current = guild?.channels?.find((channel) => entityKey(channel) === entityKey(target));
      if (!current || !channelHasPermission(current, CHANNEL_MOVE_PERMISSIONS)) {
        error = 'You no longer have permission to edit this channel.';
        return;
      }
      const parent = guild?.channels?.find(
        (channel) => entityKey(channel) === channelParent && channel.type === 4
      );
      if (channelParent && !parent) {
        error = 'That category is no longer available.';
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
        error = 'You cannot move this channel to that category.';
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
      notice = 'Channel saved.';
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
      notice = 'Role created. Configure its permissions before assigning it.';
    });
  }

  function saveRole() {
    if (!canManageSelectedRole || !selectedRole) return;
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
      notice = 'Role saved.';
    });
  }

  async function uploadRoleIcon(file: File | null, input: HTMLInputElement) {
    const target = selectedRole;
    const signal = routeController?.signal;
    roleIconFile = file;
    roleIconError = '';
    if (!file || !target || !guild || !canManageSelectedRole || !signal) return;
    if (!acceptedImageTypes.has(file.type)) {
      roleIconError = 'Choose a PNG, JPEG, GIF, or WebP image.';
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
      notice = 'Role icon updated.';
    } catch (caught) {
      roleIconError = userErrorMessage(caught, 'Could not update the role icon. Try again.');
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
      notice = 'Role icon removed.';
    } catch (caught) {
      roleIconError = userErrorMessage(caught, 'Could not remove the role icon. Try again.');
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
      error = 'You can only reorder roles below your highest role.';
      return;
    }
    roleDragEnd();
    await persistRoleOrder(previous, positioned);
  }

  async function persistRoleOrder(previous: Role[], ordered: Role[]) {
    if (!guild || busy || reorderingRoles) return;
    if (ordered.some((role) => !role.version)) {
      error = 'Role versions are unavailable. Reload settings before reordering roles.';
      return;
    }
    const targetGuild = guildId;
    const generation = loadGeneration;
    const defaultRole = previous.find((role) => role.id === guild?.id);
    setGuildRoles(defaultRole ? [defaultRole, ...ordered] : ordered);
    busy = true;
    reorderingRoles = true;
    error = '';
    notice = 'Saving role order…';
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
      notice = 'Role order saved.';
    } catch (caught) {
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      setGuildRoles(previous);
      error = userErrorMessage(caught, 'Could not save the role order. Reload and try again.');
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
      title: 'Delete role?',
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
      notice = 'Role deleted.';
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
        error = 'Choose a channel where you can create invites.';
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
      notice = 'Invite created. Copy the link below before leaving this page.';
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
      notice = 'Webhook created. Its URL remains available to server managers.';
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
      notice = 'Webhook token rotated. The previous token no longer works.';
    });
  }

  function updateWebhook(webhook: WebhookSummary) {
    if (!canManageWebhook(webhook)) return;
    const nextName = (webhookNameDrafts[webhook.id] ?? webhook.name).trim();
    if (!nextName) {
      error = 'Webhook names cannot be blank.';
      return;
    }
    const nextChannel =
      webhookChannelDrafts[webhook.id] ?? `${webhook.channel_id}@${webhook.channel_domain}`;
    const targetChannel = manageableWebhookTargets.find(
      (channel) => entityRef(channel) === nextChannel
    );
    if (!targetChannel) {
      error = 'Choose a text, announcement, or forum channel for this webhook.';
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
      notice = 'Webhook saved.';
    });
  }

  function uploadWebhookAvatar(
    webhook: WebhookSummary,
    file: File | null,
    input: HTMLInputElement
  ) {
    if (!file || !canManageWebhook(webhook)) return;
    if (!acceptedImageTypes.has(file.type)) {
      error = 'Choose a PNG, JPEG, GIF, or WebP image.';
      input.value = '';
      return;
    }
    if (!file.size) {
      error = 'Choose a non-empty image file.';
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
          throw new Error('The webhook avatar did not pass media safety processing.');
        }
        await cancelableDelay(1000, signal);
      }
      if (!updated) {
        throw new Error('Webhook avatar processing is taking longer than expected. Try again.');
      }
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === updated?.id ? updated : item));
      input.value = '';
      notice = 'Webhook avatar updated.';
    });
  }

  function deleteWebhookAvatar(webhook: WebhookSummary) {
    if (!canManageWebhook(webhook) || !webhook.avatar_hash) return;
    if (!confirm(`Remove the avatar for “${webhook.name}”?`)) return;
    return run(async (targetGuild, generation) => {
      const updated = await deleteGuildWebhookAvatar(targetGuild, webhook);
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === updated.id ? updated : item));
      notice = 'Webhook avatar removed.';
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
      notice = 'Webhook deleted.';
    });
  }

  function revokeInvite(invite: InviteSummary) {
    if (!canRevokeInvite(invite)) return;
    void openDestructiveConfirmation({
      kind: 'invite',
      target: invite,
      title: 'Revoke invite?',
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
      notice = 'Invite revoked.';
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
      description:
        'You will lose access to this guild and its cached remote history. You need another valid invite to return.',
      confirmLabel: 'Leave guild'
    });
  }

  function requestOwnershipTransfer() {
    const member = ownershipCandidates.find(
      (candidate) => entityRef(candidate.user) === ownershipTarget
    );
    if (!guild || !isGuildOwner || !member) return;
    const targetName = userDisplayName(member.user);
    void openDestructiveConfirmation({
      kind: 'guild-transfer',
      target: member,
      title: `Transfer ownership to ${targetName}?`,
      description:
        'They will become the guild owner immediately. You will remain a member, but only the new owner can transfer or delete the guild.',
      confirmLabel: 'Transfer ownership'
    });
  }

  function requestDeleteGuild() {
    if (!guild || !isGuildOwner) return;
    void openDestructiveConfirmation({
      kind: 'guild-delete',
      verificationText: guild.name,
      title: `Delete ${guild.name}?`,
      description:
        'This permanently removes the guild, its channels, messages, roles, invites, and moderation records. Remote instances will receive durable access-revocation events.',
      confirmLabel: 'Delete guild'
    });
  }

  function leaveConfirmedGuild() {
    return run(async (targetGuild) => {
      await api(`/guilds/${encodeURIComponent(targetGuild)}/members/@me`, { method: 'DELETE' });
      window.location.assign(resolve('/home'));
    });
  }

  function transferConfirmedGuild(member: MemberSummary) {
    return run(async (targetGuild, generation) => {
      if (!guild?.version) throw new Error('Guild version is unavailable.');
      const updated = await api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}/owner`, {
        method: 'PUT',
        headers: { 'If-Match': guild.version },
        body: JSON.stringify({ owner_id: entityRef(member.user) })
      });
      if (generation !== loadGeneration) return;
      guild = { ...guild!, ...updated };
      ownershipTarget = '';
      notice = `Ownership transferred to ${userDisplayName(member.user)}.`;
    });
  }

  function deleteConfirmedGuild() {
    return run(async (targetGuild) => {
      if (!guild?.version) throw new Error('Guild version is unavailable.');
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
      notice = 'Invite link copied.';
    } catch {
      error = 'Browser denied clipboard access. Allow clipboard permission and try again.';
    }
  }

  async function copyWebhookUrl(url = revealedWebhookToken) {
    if (!url) return;
    error = '';
    try {
      await navigator.clipboard.writeText(url);
      notice = 'Webhook URL copied.';
    } catch {
      error = 'Browser denied clipboard access. Select the webhook URL and copy it manually.';
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
    const focusable = Array.from(
      memberModerationElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    );
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1) ?? first;
    if (!memberModerationElement.contains(document.activeElement)) {
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

  function clampMemberPage() {
    memberPage = Math.min(
      memberPage,
      Math.max(0, Math.ceil(currentMembers.length / MEMBER_PAGE_SIZE) - 1)
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
        clampMemberPage();
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
        clampMemberPage();
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
      error = userErrorMessage(caught, 'The moderation action could not be applied. Try again.');
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
      description: `Every current member homed on ${domain} will be removed and that instance cannot add members${expiresAt ? ` until ${formatDateTime(expiresAt)}` : ' until this ban is removed'}. Its server will be asked to erase cached guild data, but a malicious, offline, or modified server may retain copies.`,
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
        `/guilds/${encodeURIComponent(targetGuild)}/members?limit=${MEMBER_PAGE_SIZE + 1}&after=${encodeURIComponent(after)}`
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return false;
      const next = cacheMemberRows(page.slice(0, MEMBER_PAGE_SIZE));
      const existing = new Set(members.map((member) => entityKey(member.user)));
      members = [...members, ...next.filter((member) => !existing.has(entityKey(member.user)))];
      membersHaveMore = page.length > MEMBER_PAGE_SIZE;
      return next.length > 0;
    } catch (caught) {
      if (generation === loadGeneration && targetGuild === guildId) {
        error = userErrorMessage(caught, 'Could not load more members. Try again.');
      }
      return false;
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) membersLoadingMore = false;
    }
  }

  function showPreviousMemberPage() {
    if (memberPage > 0 && !membersLoadingMore) memberPage -= 1;
  }

  async function showNextMemberPage() {
    if (!memberHasNextPage || membersLoadingMore) return;
    const nextStart = (memberPage + 1) * MEMBER_PAGE_SIZE;
    if (nextStart >= currentMembers.length && !(await loadMoreMembers())) return;
    if (nextStart < currentMembers.length) memberPage += 1;
  }

  $effect(() => {
    const search = memberSearch.trim();
    const targetGuild = guildId;
    if (!search) {
      memberSearchResults = [];
      memberSearchBusy = false;
      memberSearchError = '';
      return;
    }
    const controller = new AbortController();
    memberSearchBusy = true;
    memberSearchError = '';
    const timeout = window.setTimeout(() => {
      void api<MemberSummary[]>(
        `/guilds/${encodeURIComponent(targetGuild)}/members?limit=100&query=${encodeURIComponent(search)}`,
        { signal: controller.signal }
      )
        .then((results) => {
          if (controller.signal.aborted || targetGuild !== guildId) return;
          memberSearchResults = cacheMemberRows(results);
        })
        .catch((caught: unknown) => {
          if (controller.signal.aborted || targetGuild !== guildId) return;
          memberSearchResults = [];
          memberSearchError = userErrorMessage(
            caught,
            'Could not search guild members. Try again.'
          );
        })
        .finally(() => {
          if (!controller.signal.aborted && targetGuild === guildId) memberSearchBusy = false;
        });
    }, 250);
    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  });

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
            'Could not search guild members. Try again.'
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
    memberPage = 0;
    memberSearch = '';
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
    >{channelOnly
      ? `${selectedChannel?.name ?? 'Channel'} settings`
      : `${guild?.name ?? 'Guild'} settings`} · Kaede Chat</title
  >
</svelte:head>

<main class="settings-page guild-settings-page" class:channel-settings-page={channelOnly}>
  <aside class="settings-nav">
    {#if channelOnly}
      <a class="settings-back" href={donePath()}>
        <Icon name="arrow-left" size={18} />
        <span>Back to channel</span>
      </a>
      <div class="channel-settings-identity">
        <span>{selectedChannel?.type === 4 ? 'Category' : 'Channel'}</span>
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
          {selectedChannel?.name ?? 'Loading…'}
        </strong>
      </div>
      <nav aria-label="Channel settings sections">
        {#if canEditSelectedChannel}
          <button
            class:active={channelEditorPanel === 'overview'}
            type="button"
            onclick={() => selectChannelPanel('overview')}>Overview</button
          >
        {/if}
        {#if canEditSelectedPermissions}
          <button
            class:active={channelEditorPanel === 'permissions'}
            type="button"
            onclick={() => selectChannelPanel('permissions')}>Permissions</button
          >
        {/if}
        {#if (canCreateSelectedInvite || canEditSelectedChannel) && selectedChannel?.type !== 4}
          <button
            class:active={channelEditorPanel === 'invites'}
            type="button"
            onclick={() => selectChannelPanel('invites')}>Invites</button
          >
        {/if}
        {#if canAccessSelectedIntegrations && [0, 5, 15].includes(selectedChannel?.type ?? -1)}
          <button
            class:active={channelEditorPanel === 'integrations'}
            type="button"
            onclick={() => selectChannelPanel('integrations')}>Integrations</button
          >
        {/if}
        {#if canEditSelectedChannel}
          <span class="channel-settings-divider"></span>
          <button
            class="danger-nav-item"
            class:active={channelEditorPanel === 'delete'}
            type="button"
            onclick={() => selectChannelPanel('delete')}
            >Delete {selectedChannel?.type === 4 ? 'Category' : 'Channel'}</button
          >
        {/if}
      </nav>
      <span class="settings-instance-label">
        {selectedChannel ? `${selectedChannel.id}@${selectedChannel.origin_domain}` : ''}
      </span>
    {:else}
      <a class="settings-back" href={donePath()}>
        <Icon name="arrow-left" size={18} />
        <span>Back to guild</span>
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
          <strong>{guild?.name ?? 'Loading…'}</strong>
          <small>{guild?.origin_domain ?? 'Guild settings'}</small>
        </span>
      </div>
      <nav aria-label="Guild settings sections">
        <p>Personal</p>
        <a href="#notifications"><Icon name="bell" size={18} />Notifications</a>
        <p>Guild</p>
        <a href="#overview"><Icon name="server" size={18} />Overview</a>
        {#if canAccessGuildIntegrations && guild}
          <a href={guildIntegrationsPath(guild)}><Icon name="server" size={18} />Integrations</a>
        {/if}
        {#if canManageGuild && !channelOnly && guild}
          <a href={guildApplicationDirectoryPath(guild)}
            ><Icon name="sparkles" size={18} />App Directory</a
          >
        {/if}
        {#if canManageRoles}
          <a href="#roles"><Icon name="shield" size={18} />Roles</a>
        {/if}
        {#if canAccessExpressions}
          <a href="#emoji"><span aria-hidden="true">☺</span>Emoji</a>
          <a href="#stickers"><span aria-hidden="true">▱</span>Stickers</a>
        {/if}
        {#if hasPermission(Permission.MANAGE_AUTO_MODERATION)}
          <a href="#automod"><Icon name="shield" size={18} />AutoMod</a>
        {/if}
        {#if canKickMembers || canBanMembers}
          <a href="#bulk-moderation"><Icon name="users" size={18} />Bulk moderation</a>
        {/if}
        {#if canAccessExpressions}
          <a href="#soundboard"><span aria-hidden="true">♫</span>Soundboard</a>
        {/if}
        {#if canViewAuditLog}
          <a href="#audit-log"><Icon name="clock" size={18} />Audit log</a>
        {/if}
        {#if canViewMembers}
          <p>Community</p>
          <a href="#members"><Icon name="users" size={18} />Members</a>
        {/if}
        {#if canAccessInvites}
          <a href="#invites"><Icon name="globe" size={18} />Invites</a>
        {/if}
        <p>Membership</p>
        <a href="#guild-lifecycle"><Icon name="logout" size={18} />Guild access</a>
      </nav>
      <span class="settings-instance-label">
        {isLocalGuild ? 'Managed on this instance' : 'Managed by its home instance'}
      </span>
    {/if}
  </aside>

  <section class="settings-content">
    <header class="settings-page-heading">
      <div>
        <p class="eyebrow">
          {channelOnly ? (selectedChannel?.name ?? 'Channel') : 'Guild administration'}
        </p>
        <h1>{channelOnly ? channelPanelTitle(channelEditorPanel) : (guild?.name ?? 'Settings')}</h1>
        <p>
          {channelOnly
            ? channelPanelDescription(channelEditorPanel)
            : 'Shape the spaces, roles, and invitations that hold this community together.'}
        </p>
      </div>
      <a
        class="icon-button settings-close"
        href={donePath()}
        aria-label={channelOnly ? 'Close channel settings' : 'Close guild settings'}>×</a
      >
    </header>

    {#if error && !destructiveConfirmation}
      <div class="notice-banner error-banner" role="alert">{error}</div>
    {/if}
    <Toast message={notice} onDismiss={() => (notice = '')} />

    {#if loading}
      <div class="settings-loading" aria-label="Loading guild settings">
        <span></span><span></span><span></span>
      </div>
    {:else if !guild}
      <section class="empty-state">
        <span><Icon name="server" size={28} /></span>
        <h2>Guild settings are unavailable</h2>
        <p>Return to Kaede and try opening this guild again.</p>
        <a class="primary-button" href={resolve('/home')}>Return home</a>
      </section>
    {:else}
      {#if !channelOnly}
        <section id="notifications" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="bell" /></span>
            <div>
              <h2>Notifications</h2>
              <p>Choose which messages from this guild can send you a notification.</p>
            </div>
          </div>
          <div class="settings-card">
            <div class="toggle-list notification-level-list">
              <label
                class:selected={guildNotificationLevel === 'all'}
                class="toggle-row notification-level-row"
              >
                <span>
                  <strong>All messages</strong>
                  <small>Notify you whenever a message is sent in a channel you can see.</small>
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
                  <strong>Mentions only</strong>
                  <small>Notify you only when a message directly mentions you.</small>
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
                  <strong>Nothing</strong>
                  <small>Do not send notifications for messages in this guild.</small>
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
              Notifications must also be enabled in <a href={resolve('/settings#notifications')}
                >User Settings</a
              >. The preference syncs across the web, desktop, and mobile clients signed in to your
              account.
            </p>
          </div>
        </section>

        <section id="overview" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="server" /></span>
            <div>
              <h2>Overview</h2>
              <p>The public name and description people see when they discover this guild.</p>
            </div>
          </div>

          {#if !isLocalGuild}
            <div class="notice-banner info-banner">
              <Icon name="globe" size={18} />
              This guild is hosted by <strong>{guild.origin_domain}</strong>. Changes are forwarded
              there and your current membership, permissions, and resource versions are
              re-authorized before they are applied.
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
                <p>{description || 'No description yet.'}</p>
              </div>
            </div>
          </div>

          {#if canManageGuildAssets}
            <div class="settings-card">
              <div class="settings-card-row">
                <div>
                  <strong>Guild images</strong>
                  <p>PNG, JPEG, GIF, or WebP. Files are scanned before they become public.</p>
                </div>
                <div class="profile-media-actions">
                  <label class="secondary-button">
                    <Icon name="server" size={16} />Change icon
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
                      <Icon name="trash" size={16} />Remove icon
                    </button>
                  {/if}
                  <label class="secondary-button">
                    <Icon name="image" size={16} />Change banner
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
                      <Icon name="trash" size={16} />Remove banner
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
                    <span>Scanning…</span>
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
              <span>Guild name</span>
              <small>2–100 characters</small>
              <input
                bind:value={name}
                minlength="2"
                maxlength="100"
                required
                disabled={!canManageGuild}
              />
            </label>
            <label class="form-field">
              <span>Description</span>
              <small>{description.length}/500</small>
              <textarea
                bind:value={description}
                maxlength="500"
                rows="4"
                disabled={!canManageGuild}
                placeholder="What brings this community together?"
              ></textarea>
            </label>
            <div class="settings-card-row history-policy-row">
              <div>
                <strong>Federated message history</strong>
                <p>
                  Controls the default for remote members. A member must also have View channel and
                  Read message history permission. Channels can override this default.
                </p>
              </div>
              <label class="form-field compact-field policy-select">
                <span>Guild default</span>
                <select bind:value={guildHistoryPolicy} disabled={!canManageGuild || busy}>
                  <option value="disabled">Do not export history</option>
                  <option value="full_retained">Export retained history</option>
                </select>
              </label>
            </div>
            {#if guildHistoryPolicy === 'full_retained'}
              <div class="notice-banner warning-banner" role="note">
                <Icon name="globe" size={18} />
                <span>
                  Remote instances receive their own copy of permitted messages and attachments.
                  Kaede asks them to purge that copy when access is revoked, but deletion cannot be
                  guaranteed if a remote operator is malicious, offline, or has modified their
                  server.
                </span>
              </div>
            {/if}
            {#if canManageGuild}
              <div class="form-actions">
                <button class="primary-button" disabled={busy}>Save overview</button>
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
              <h2>Channels</h2>
              <p>
                Edit channel details here. Reorder and reparent channels from the guild sidebar.
              </p>
            </div>
          </div>
          <div class="settings-split">
            <div class="settings-list-panel">
              <div class="settings-list-heading">
                <strong>Channel list</strong>
                <a href={donePath()}>Reorder in guild</a>
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
                    <span>{selectedChannel.type === 4 ? 'Category' : 'Channel'}</span>
                    <h3>{selectedChannel.name}</h3>
                  </div>
                  <code>{selectedChannel.id}</code>
                </div>
                <div class="editor-tabs" role="tablist" aria-label="Channel settings">
                  <button
                    class:active={channelEditorPanel === 'overview'}
                    type="button"
                    role="tab"
                    aria-selected={channelEditorPanel === 'overview'}
                    onclick={() => selectChannelPanel('overview')}>Overview</button
                  >
                  {#if canEditSelectedPermissions}
                    <button
                      class:active={channelEditorPanel === 'permissions'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'permissions'}
                      onclick={() => selectChannelPanel('permissions')}>Permissions</button
                    >
                  {/if}
                  {#if (canCreateSelectedInvite || canEditSelectedChannel) && selectedChannel.type !== 4}
                    <button
                      class:active={channelEditorPanel === 'invites'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'invites'}
                      onclick={() => selectChannelPanel('invites')}>Invites</button
                    >
                  {/if}
                  {#if canAccessSelectedIntegrations && [0, 5, 15].includes(selectedChannel.type)}
                    <button
                      class:active={channelEditorPanel === 'integrations'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'integrations'}
                      onclick={() => selectChannelPanel('integrations')}>Integrations</button
                    >
                  {/if}
                  {#if canDeleteSelectedChannel}
                    <button
                      class="danger-tab"
                      class:active={channelEditorPanel === 'delete'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'delete'}
                      onclick={() => selectChannelPanel('delete')}>Delete</button
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
                      <span>Name</span>
                      <input bind:value={channelName} maxlength="100" required disabled={busy} />
                    </label>
                    {#if selectedChannel.type !== 4}
                      <label class="form-field compact-field">
                        <span>{selectedChannel.type === 15 ? 'Post Guidelines' : 'Topic'}</span>
                        <textarea
                          bind:value={channelTopic}
                          maxlength={selectedChannel.type === 15 ? 4096 : 1024}
                          rows="3"
                          placeholder={selectedChannel.type === 15
                            ? 'Help members understand what to post here.'
                            : 'What belongs in this channel?'}
                          disabled={busy}
                        ></textarea>
                      </label>
                      {#if selectedChannel.type === 15}
                        <fieldset class="forum-settings-group">
                          <legend>Tags</legend>
                          <small>Members can apply up to five tags to a post.</small>
                          {#each channelForumTags as tag, index (`${tag.id ?? 'new'}:${index}`)}
                            <div class="forum-tag-editor">
                              <input
                                value={tag.emoji_name ?? ''}
                                maxlength="64"
                                aria-label={`Emoji for ${tag.name}`}
                                placeholder="Emoji"
                                disabled={busy}
                                oninput={(event) =>
                                  updateForumTag(index, {
                                    emoji_id: null,
                                    emoji_name: event.currentTarget.value.trim() || null
                                  })}
                              />
                              <input
                                value={tag.name}
                                maxlength="20"
                                aria-label="Tag name"
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
                                Moderators only
                              </label>
                              <button
                                class="quiet-button"
                                type="button"
                                disabled={busy}
                                aria-label={`Remove ${tag.name}`}
                                onclick={() => removeForumTag(index)}>Remove</button
                              >
                            </div>
                          {/each}
                          {#if channelForumTags.length < 20}
                            <div class="forum-tag-add">
                              <input
                                bind:value={newForumTagName}
                                maxlength="20"
                                placeholder="New tag"
                                disabled={busy}
                              />
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy || !newForumTagName.trim()}
                                onclick={addForumTag}>Add tag</button
                              >
                            </div>
                          {/if}
                        </fieldset>
                        <label class="form-field compact-field">
                          <span>Default reaction emoji</span>
                          <input
                            value={channelForumReaction}
                            maxlength="64"
                            placeholder="None"
                            disabled={busy}
                            oninput={editForumDefaultReaction}
                          />
                        </label>
                        <label class="form-field compact-field">
                          <span>Default sort order</span>
                          <select bind:value={channelForumSort} disabled={busy}>
                            <option value={0}>Recently Active</option>
                            <option value={1}>Date Posted</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>Default layout</span>
                          <select bind:value={channelForumLayout} disabled={busy}>
                            <option value={0}>Not set</option>
                            <option value={1}>List View</option>
                            <option value={2}>Gallery View</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>Hide posts after inactivity</span>
                          <select bind:value={channelForumArchive} disabled={busy}>
                            <option value={60}>1 hour</option>
                            <option value={1440}>24 hours</option>
                            <option value={4320}>3 days</option>
                            <option value={10080}>1 week</option>
                          </select>
                        </label>
                        <label class="form-field compact-field">
                          <span>Default post slowmode</span>
                          <select bind:value={channelForumSlowmode} disabled={busy}>
                            <option value={0}>Off</option>
                            <option value={5}>5 seconds</option>
                            <option value={10}>10 seconds</option>
                            <option value={30}>30 seconds</option>
                            <option value={60}>1 minute</option>
                            <option value={300}>5 minutes</option>
                            <option value={3600}>1 hour</option>
                          </select>
                        </label>
                        <label class="settings-toggle-row">
                          <span>
                            <strong>Require people to select tags before posting</strong>
                            <small>New posts must include at least one tag.</small>
                          </span>
                          <input
                            type="checkbox"
                            bind:checked={channelForumRequireTag}
                            disabled={busy}
                          />
                        </label>
                        <label class="settings-toggle-row">
                          <span>
                            <strong>Require end-to-end encryption for post replies</strong>
                            <small>
                              Titles, starter messages, title search, and starter previews remain
                              plaintext. This permanent policy applies only to posts created after
                              it is enabled. Each future post secures its own subsequent replies;
                              webhooks, reply search and previews, and server scanning are
                              unavailable there. Verified participant-mode apps require an explicit
                              per-post grant.
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
                          <span>Default Auto-Archive Duration</span>
                          <select bind:value={channelForumArchive} disabled={busy}>
                            <option value={60}>1 hour</option>
                            <option value={1440}>24 hours</option>
                            <option value={4320}>3 days</option>
                            <option value={10080}>1 week</option>
                          </select>
                        </label>
                        {#if selectedChannel.type === 0}
                          <label class="form-field compact-field">
                            <span>Default Thread Slowmode</span>
                            <select bind:value={channelForumSlowmode} disabled={busy}>
                              <option value={0}>Off</option>
                              <option value={5}>5 seconds</option>
                              <option value={10}>10 seconds</option>
                              <option value={30}>30 seconds</option>
                              <option value={60}>1 minute</option>
                              <option value={300}>5 minutes</option>
                              <option value={3600}>1 hour</option>
                            </select>
                          </label>
                        {/if}
                        <label class="form-field compact-field">
                          <span>Federated history</span>
                          <small>
                            Applies only after the member also passes this channel’s View channel
                            and Read message history permissions.
                          </small>
                          <select bind:value={channelHistoryPolicy} disabled={busy}>
                            <option value="inherit">Use guild default</option>
                            <option value="disabled">Never export this channel</option>
                            <option value="full_retained">Export retained history</option>
                          </select>
                        </label>
                        {#if channelHistoryPolicy === 'full_retained'}
                          <div class="notice-banner warning-banner compact-warning" role="note">
                            <Icon name="globe" size={17} />
                            Remote deletion is requested on access loss, but cannot be guaranteed.
                          </div>
                        {/if}
                      {/if}
                      {#if selectedChannel.type === 0 || selectedChannel.type === 5 || selectedChannel.type === 15}
                        <label class="settings-toggle-row">
                          <span>
                            <strong>Age-restricted channel</strong>
                            <small>
                              Only age-assured adult members can use age-restricted application
                              commands here. Threads inherit this setting from their parent.
                            </small>
                          </span>
                          <input type="checkbox" bind:checked={channelNsfw} disabled={busy} />
                        </label>
                      {/if}
                      {#if selectedChannel.type === 2 || selectedChannel.type === 13}
                        <fieldset class="forum-settings-group">
                          <legend>Voice quality and capacity</legend>
                          <small>
                            These limits apply to every Web, Desktop, Mobile, and bot connection.
                            Stage channels use the same authority-advertised media regions.
                          </small>
                          <div class="forum-tag-add">
                            <label class="form-field compact-field">
                              <span>Bitrate</span>
                              <select bind:value={channelBitrate} disabled={busy}>
                                <option value={8000}>8 kbps</option>
                                <option value={32000}>32 kbps</option>
                                <option value={64000}>64 kbps</option>
                                {#if selectedChannel.type === 2}
                                  <option value={96000}>96 kbps</option>
                                  <option value={128000}>128 kbps</option>
                                  <option value={256000}>256 kbps</option>
                                  <option value={384000}>384 kbps</option>
                                {/if}
                              </select>
                            </label>
                            <label class="form-field compact-field">
                              <span>User limit</span>
                              <input
                                type="number"
                                min="0"
                                max={selectedChannel.type === 13 ? 10000 : 99}
                                bind:value={channelUserLimit}
                                disabled={busy}
                              />
                              <small>0 means no explicit limit.</small>
                            </label>
                          </div>
                          <label class="form-field compact-field">
                            <span>Region Override</span>
                            <select
                              bind:value={channelRtcRegion}
                              disabled={busy || !canEditSelectedChannel}
                            >
                              <option value="">Automatic</option>
                              {#if channelRtcRegion && !channelVoiceRegions.some((region) => region.id === channelRtcRegion)}
                                <option value={channelRtcRegion}
                                  >{channelRtcRegion} (unavailable)</option
                                >
                              {/if}
                              {#each channelVoiceRegions as region (region.id)}
                                <option
                                  value={region.id}
                                  disabled={region.deprecated && region.id !== channelRtcRegion}
                                >
                                  {region.name}{region.optimal
                                    ? ' — Recommended'
                                    : ''}{region.deprecated ? ' — Deprecated' : ''}
                                </option>
                              {/each}
                            </select>
                            <small>
                              Automatic chooses the lowest-latency region. The catalog comes from
                              this server's authority, including for federated servers.
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
                            <strong>End-to-end encryption</strong>
                            {#if selectedChannel.encryption_mode === 'e2ee'}
                              <p>
                                {selectedChannel.encryption_state === 'rekeying'
                                  ? 'Paused after a membership change. Secure the current member list before messaging resumes.'
                                  : 'On. Participant identities remain unverified until members compare the safety number.'}
                              </p>
                              {#if channelSafetyNumber}
                                <code class="e2ee-safety-number">{channelSafetyNumber}</code>
                              {/if}
                              {#if selectedChannel.encryption_state === 'active'}
                                <button
                                  class="secondary-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={verifyChannelSafetyNumber}>Show safety number</button
                                >
                              {/if}
                              {#if selectedChannel.encryption_state === 'rekeying'}
                                <button
                                  class="secondary-button"
                                  type="button"
                                  disabled={busy}
                                  onclick={enableChannelEncryption}>Secure current members</button
                                >
                              {/if}
                            {:else if e2eeActivationEnabled}
                              <p>
                                {selectedChannel.type === 2
                                  ? 'Optional and permanent. Encrypts microphone, camera, and screen-share frames. Unsupported devices cannot join, and key loss prevents access.'
                                  : 'Optional and permanent. Disables server search, previews, and file scanning. Verified app and webhook devices receive no access automatically and require an explicit future-only grant and rekey. Key loss can make history unrecoverable.'}
                              </p>
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy}
                                onclick={enableChannelEncryption}>Turn on encryption</button
                              >
                            {/if}
                          </div>
                        </div>
                      {/if}
                      <label class="form-field compact-field">
                        <span>Category</span>
                        <select bind:value={channelParent} disabled={busy}>
                          <option value="">No category</option>
                          {#each editableChannelParents(selectedChannel) as category (entityKey(category))}
                            <option value={entityKey(category)}>{category.name}</option>
                          {/each}
                        </select>
                      </label>
                      <label class="form-field compact-field">
                        <span>Slowmode</span>
                        <select bind:value={channelSlowmode} disabled={busy}>
                          <option value={0}>Off</option>
                          <option value={5}>5 seconds</option>
                          <option value={10}>10 seconds</option>
                          <option value={30}>30 seconds</option>
                          <option value={60}>1 minute</option>
                          <option value={300}>5 minutes</option>
                          <option value={3600}>1 hour</option>
                        </select>
                      </label>
                    {/if}
                    <div class="form-actions">
                      <button class="primary-button" disabled={busy}>Save channel</button>
                    </div>
                  </form>
                {/if}
                {#if channelEditorPanel === 'permissions' && canEditSelectedPermissions}
                  <section
                    class="channel-permissions-editor"
                    aria-labelledby="channel-permissions-title"
                  >
                    <div>
                      <span>Access control</span>
                      <h4 id="channel-permissions-title">Channel permissions</h4>
                      <p>
                        Override a role or member for this channel. Inherit uses the guild role
                        value.
                      </p>
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
                              ? 'Synced with category'
                              : 'Permissions not synced with category'}
                          </strong>
                          <small>
                            Syncing replaces this channel’s overrides with the category’s current
                            rules.
                          </small>
                        </span>
                        {#if !selectedChannel.permissions_synced}
                          <button
                            class="secondary-button"
                            type="button"
                            disabled={busy}
                            onclick={() => void syncChannelPermissions()}>Sync Now</button
                          >
                        {/if}
                      </div>
                    {/if}
                    <div class="permission-workspace">
                      <aside class="permission-target-rail" aria-label="Permission targets">
                        <label class="form-field compact-field">
                          <span>Roles and members</span>
                          <input bind:value={overwriteSearch} placeholder="Search" />
                        </label>
                        <div class="permission-target-list">
                          <p>Roles</p>
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
                            <p>Members</p>
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
                              <span>Permissions for</span>
                              <h5>{overwriteTargetLabel()}</h5>
                            </div>
                            <label class="form-field compact-field permission-search">
                              <span class="visually-hidden">Search permissions</span>
                              <input
                                bind:value={permissionSearch}
                                placeholder="Search permissions"
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
                                          Also requires: {permissionDependencies(
                                            permission[3].dependencies
                                          )}
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
                                        aria-label="Deny in this channel"
                                        title="Deny"
                                        onclick={() =>
                                          setOverwritePermission(permission[2], 'deny')}>×</button
                                      >
                                      <button
                                        class:active={overwritePermission(permission[2]) ===
                                          'inherit'}
                                        type="button"
                                        disabled={busy || !selectedHasPermission(permission[2])}
                                        aria-label="Inherit guild setting"
                                        title="Inherit"
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
                                        aria-label="Allow in this channel"
                                        title="Allow"
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
                              onclick={() => void resetChannelOverwrite()}>Reset override</button
                            >
                            <button
                              class="primary-button"
                              type="button"
                              disabled={busy || !canManageOverwriteTarget(overwriteTarget)}
                              onclick={() => void saveChannelOverwrite()}>Save permissions</button
                            >
                          </div>
                        {:else}
                          <div class="empty-state compact-empty permission-target-empty">
                            <span><Icon name="shield" /></span>
                            <h3>Choose a role or member</h3>
                            <p>Select a target to review its channel-specific permissions.</p>
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
                      <span>Channel access</span>
                      <h4 id="channel-invites-title">Invites</h4>
                      <p>Create links that open this channel after the person joins the guild.</p>
                    </div>
                    {#if canCreateSelectedInvite}
                      <div class="settings-form">
                        <div class="two-column-fields">
                          <label class="form-field compact-field">
                            <span>Expires after</span>
                            <select bind:value={inviteMaxAge} disabled={busy}>
                              <option value="3600">1 hour</option>
                              <option value="86400">1 day</option>
                              <option value="604800">7 days</option>
                              <option value="">Never</option>
                            </select>
                          </label>
                          <label class="form-field compact-field">
                            <span>Maximum uses</span>
                            <input
                              bind:value={inviteMaxUses}
                              type="number"
                              min="1"
                              placeholder="Unlimited"
                              disabled={busy}
                            />
                          </label>
                        </div>
                        <div class="form-actions">
                          <button
                            class="primary-button"
                            type="button"
                            disabled={busy}
                            onclick={() => void createInvite()}>Create invite</button
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
                              >{invite.uses} use{invite.uses === 1 ? '' : 's'} · {invite.expires_at
                                ? formatDateTime(invite.expires_at)
                                : 'No expiry'}</small
                            >
                          </span>
                          {#if canRevokeInvite(invite)}
                            <button
                              class="danger-text-button"
                              type="button"
                              disabled={busy}
                              onclick={() => revokeInvite(invite)}>Revoke</button
                            >
                          {/if}
                        </div>
                      {:else}
                        <p class="muted-copy">No active invites target this channel.</p>
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
                        <span>Integrations</span>
                        <h4 id="channel-integrations-title">Webhooks</h4>
                        <p>
                          Webhooks can post into this channel. Tokens are shown only when created or
                          rotated.
                        </p>
                      </div>
                      <form
                        class="inline-settings-form"
                        onsubmit={(event) => {
                          event.preventDefault();
                          void createChannelWebhook();
                        }}
                      >
                        <label class="form-field compact-field">
                          <span>Webhook name</span>
                          <input
                            bind:value={newWebhookName}
                            minlength="1"
                            maxlength="80"
                            required
                            disabled={busy}
                          />
                        </label>
                        <button class="primary-button" disabled={busy}>Create webhook</button>
                      </form>
                      {#if revealedWebhookToken}
                        <div class="notice-banner warning-banner" role="status">
                          <Icon name="lock" size={18} />
                          <span
                            ><strong>Webhook URL</strong><code>{revealedWebhookToken}</code></span
                          >
                          <button
                            class="secondary-button"
                            type="button"
                            onclick={() => void copyWebhookUrl()}>Copy webhook URL</button
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
                                <span>{webhook.avatar_hash ? 'Replace avatar' : 'Add avatar'}</span>
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
                                  >Remove avatar</button
                                >
                              {/if}
                            </div>
                            <div class="webhook-fields">
                              <label class="form-field compact-field webhook-name-field">
                                <span>Name <small>ID {webhook.id}</small></span>
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
                                <span>Post to channel</span>
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
                                  >Copy webhook URL</button
                                >
                              {/if}
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy ||
                                  !(webhookNameDrafts[webhook.id] ?? webhook.name).trim()}
                                onclick={() => void updateWebhook(webhook)}>Save</button
                              >
                              <button
                                class="secondary-button"
                                type="button"
                                disabled={busy}
                                onclick={() => void rotateWebhook(webhook)}>Rotate token</button
                              >
                              <button
                                class="danger-text-button"
                                type="button"
                                disabled={busy}
                                onclick={() => void deleteWebhook(webhook)}>Delete</button
                              >
                            </div>
                          </div>
                        {:else}
                          <p class="muted-copy">No webhooks post to this channel.</p>
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
                      <span>Danger zone</span>
                      <h4 id="delete-channel-title">
                        Delete {selectedChannel.type === 4 ? 'category' : 'channel'}
                      </h4>
                      <p>
                        {selectedChannel.type === TRACKER_CHANNEL_TYPE
                          ? 'This is permanent. The board, its statuses, and every task will be deleted.'
                          : 'This is permanent. Categories must be empty and channels containing retained messages cannot be deleted.'}
                      </p>
                    </div>
                    <button
                      class="danger-button"
                      type="button"
                      disabled={busy}
                      onclick={deleteChannel}
                    >
                      <Icon name="trash" size={16} /> Delete {selectedChannel.type === 4
                        ? 'category'
                        : 'channel'}
                    </button>
                  </section>
                {/if}
              {:else}
                <div class="empty-state compact-empty">
                  <span><Icon name="hash" /></span>
                  <h3>Select a channel</h3>
                  <p>Choose an item from the list to edit its details.</p>
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
                <strong>Create a channel</strong>
                <p>Add a text, voice, announcement, forum, task tracker, or category.</p>
              </div>
              <label class="form-field compact-field">
                <span>Name</span>
                <input bind:value={newChannelName} maxlength="100" required disabled={busy} />
              </label>
              <label class="form-field compact-field">
                <span>Type</span>
                <select bind:value={newChannelType} disabled={busy}>
                  <option value={0}>Text</option>
                  <option value={2}>Voice</option>
                  <option value={4}>Category</option>
                  <option value={5}>Announcement</option>
                  <option value={15}>Forum</option>
                  <option value={TRACKER_CHANNEL_TYPE}>Task tracker</option>
                </select>
              </label>
              {#if newChannelType === TRACKER_CHANNEL_TYPE}
                <label class="form-field compact-field">
                  <span>Task key prefix</span>
                  <small>Optional; defaults from the channel name</small>
                  <input
                    bind:value={newChannelTrackerPrefix}
                    minlength="2"
                    maxlength="10"
                    pattern="[A-Za-z][A-Za-z0-9]*"
                    placeholder="e.g. RAID"
                    disabled={busy}
                  />
                </label>
              {/if}
              {#if newChannelType !== 4}
                <label class="form-field compact-field">
                  <span>Category</span>
                  <select bind:value={newChannelParent} disabled={busy}>
                    <option value="">No category</option>
                    {#each (guild.channels ?? []).filter((channel) => channel.type === 4) as category (entityKey(category))}
                      <option value={entityKey(category)}>{category.name}</option>
                    {/each}
                  </select>
                </label>
              {/if}
              <button class="primary-button" disabled={busy}>
                <Icon name="plus" size={16} />Create
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
              <h2>Custom emoji</h2>
              <p>Upload emoji that members can use here and in their other guilds.</p>
            </div>
          </div>
          <div class="settings-card">
            <div class="settings-list-heading">
              <div>
                <strong>Guild emoji</strong>
                <p>{guild?.emojis?.length ?? 0} of {guild?.emoji_limit ?? 100} used</p>
              </div>
            </div>
            {#if canCreateExpressions}
              <form class="inline-create-form" onsubmit={createEmoji}>
                <label class="form-field compact-field">
                  <span>Name</span>
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
                  <span>Image</span>
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
                  >{emojiBusy ? 'Uploading…' : 'Upload emoji'}</button
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
                        <span>Name</span>
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
                        <span>Role restrictions <small>None means everyone</small></span>
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
                            A restriction is above your highest role. You can edit the emoji, but
                            not its role access.
                          </small>
                        {/if}
                      </label>
                      <p class="field-hint">
                        <strong>{emoji.available === false ? 'Unavailable' : 'Available'}</strong>
                        · Availability is controlled by the server and cannot be edited.
                      </p>
                    </div>
                    {#if editable}
                      <div class="row-actions expression-actions">
                        <button
                          class="secondary-button"
                          type="button"
                          disabled={emojiBusy}
                          onclick={() => void updateEmoji(emoji)}>Save</button
                        >
                        <button
                          class="secondary-button danger-button"
                          type="button"
                          disabled={emojiBusy}
                          onclick={() => void deleteEmoji(emoji)}>Delete</button
                        >
                      </div>
                    {:else}
                      <small class="field-hint"
                        >Only its creator or an expression manager can edit this emoji.</small
                      >
                    {/if}
                  {/if}
                </article>
              {:else}
                <p class="empty-copy">This guild has no custom emoji yet.</p>
              {/each}
            </div>
          </div>
        </section>

        <section id="stickers" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon" aria-hidden="true">▱</span>
            <div>
              <h2>Guild stickers</h2>
              <p>
                Create transparent static or animated stickers members can send from the sticker
                menu.
              </p>
            </div>
          </div>
          <div class="settings-card">
            <div class="settings-list-heading">
              <div>
                <strong>Sticker collection</strong>
                <p>{guild?.stickers?.length ?? 0} of {guild?.sticker_limit ?? 60} used</p>
              </div>
            </div>
            {#if canCreateExpressions}
              <form class="sticker-create-form" onsubmit={createSticker}>
                <div class="sticker-fields">
                  <label class="form-field compact-field">
                    <span>Name</span>
                    <input
                      bind:value={stickerName}
                      minlength="2"
                      maxlength="30"
                      placeholder="Friendly wave"
                      required
                      disabled={stickerBusy}
                    />
                  </label>
                  <label class="form-field compact-field">
                    <span>Description <small>Optional</small></span>
                    <input
                      bind:value={stickerDescription}
                      minlength="2"
                      maxlength="100"
                      placeholder="A friendly wave"
                      disabled={stickerBusy}
                    />
                  </label>
                  <label class="form-field compact-field">
                    <span>Image</span>
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
                        aria-label="Sticker crop editor"
                        style:--sticker-image-aspect={String(stickerImageAspect)}
                        onpointermove={moveStickerCropGesture}
                        onpointerup={endStickerCropGesture}
                        onpointercancel={endStickerCropGesture}
                      >
                        <img
                          src={stickerPreviewUrl}
                          alt="Sticker source"
                          draggable="false"
                          onload={(event) => {
                            const image = event.currentTarget as HTMLImageElement;
                            stickerImageAspect = image.naturalWidth / image.naturalHeight || 1;
                          }}
                        />
                        <div
                          class="sticker-crop-selection"
                          role="button"
                          aria-label="Crop selection. Drag to move, or use the arrow keys."
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
                      <strong>Crop your sticker</strong>
                      <p>Drag the box to move it. Drag any corner to resize it.</p>
                      <small>
                        Selection: {Math.round(stickerCropWidth * 100)}% × {Math.round(
                          stickerCropHeight * 100
                        )}%
                      </small>
                      <button
                        class="secondary-button crop-reset-button"
                        type="button"
                        onclick={() => applyStickerCrop({ x: 0, y: 0, width: 1, height: 1 })}
                        >Reset crop</button
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
                          >Remove background <small
                            >{stickerFile?.type === 'image/gif'
                              ? 'Static images only'
                              : guild?.sticker_background_removal_enabled
                                ? 'Powered by rembg'
                                : 'Not enabled on this server'}</small
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
                  >{stickerBusy ? 'Creating sticker…' : 'Create sticker'}</button
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
                        <span>Name</span>
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
                        <span>Description</span>
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
                        <span>Tags <small>Comma separated</small></span>
                        <input
                          value={stickerDrafts[entityKey(sticker)].tags}
                          maxlength="200"
                          disabled={stickerBusy || !editable}
                          oninput={(event) =>
                            patchStickerDraft(sticker, { tags: event.currentTarget.value })}
                        />
                      </label>
                      <p class="field-hint">
                        <strong>{sticker.available === false ? 'Unavailable' : 'Available'}</strong>
                        · Availability is controlled by the server and cannot be edited.
                      </p>
                    </div>
                    {#if editable}
                      <div class="row-actions expression-actions">
                        <button
                          class="secondary-button"
                          type="button"
                          disabled={stickerBusy}
                          onclick={() => void updateSticker(sticker)}>Save</button
                        >
                        <button
                          class="secondary-button danger-button"
                          type="button"
                          disabled={stickerBusy}
                          onclick={() => void deleteSticker(sticker)}>Delete</button
                        >
                      </div>
                    {:else}
                      <small class="field-hint"
                        >Only its creator or an expression manager can edit this sticker.</small
                      >
                    {/if}
                  {/if}
                </article>
              {:else}
                <p class="empty-copy">This guild has no stickers yet.</p>
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
              <h2>Roles</h2>
              <p>Group permissions into roles, then assign them to members.</p>
            </div>
          </div>
          <div class="settings-split role-settings-split">
            <div class="settings-list-panel">
              <div class="settings-list-heading"><strong>Roles</strong></div>
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
                  {#if role.id === guild.id}<small>Default</small>{/if}
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
                  placeholder="New role"
                  aria-label="New role name"
                  required
                  disabled={busy}
                />
                <button class="icon-button" disabled={busy} aria-label="Create role">
                  <Icon name="plus" size={17} />
                </button>
              </form>
            </div>

            <div class="settings-card editor-card role-editor">
              {#if selectedRole}
                <div class="editor-heading">
                  <div>
                    <span>Role</span>
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
                <div class="editor-tabs role-editor-tabs" role="tablist" aria-label="Role settings">
                  <button
                    class:active={roleEditorTab === 'display'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'display'}
                    onclick={() => (roleEditorTab = 'display')}>Display</button
                  >
                  <button
                    class:active={roleEditorTab === 'permissions'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'permissions'}
                    onclick={() => (roleEditorTab = 'permissions')}>Permissions</button
                  >
                  <button
                    class:active={roleEditorTab === 'members'}
                    type="button"
                    role="tab"
                    aria-selected={roleEditorTab === 'members'}
                    onclick={() => (roleEditorTab = 'members')}>Manage members</button
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
                        <strong>Role position</strong>
                        <small
                          >Drag roles in the list to reorder them. Changes save automatically, and
                          you can only move roles below your highest role.</small
                        >
                      </div>
                    </div>
                    <div class="two-column-fields role-name-fields">
                      <label class="form-field compact-field">
                        <span>Name</span>
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
                      <legend>Role icon</legend>
                      <small>
                        Shown beside members' names in chat. When a member has several icons, their
                        highest role icon is used.
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
                            onclick={() => void deleteRoleIcon()}>Remove icon</button
                          >
                        {/if}
                      </div>
                      {#if roleIconBusy}<small role="status"
                          >Uploading and scanning role icon…</small
                        >{/if}
                      {#if roleIconError}<p class="form-error" role="alert">{roleIconError}</p>{/if}
                    </fieldset>
                    <fieldset
                      class="role-color-field"
                      disabled={busy || selectedRole.id === guild.id || !canManageSelectedRole}
                    >
                      <legend>Role color</legend>
                      <small>Members use the color of their highest displayed role.</small>
                      <div class="role-color-controls">
                        <button
                          class="role-color-default"
                          class:selected={roleColor === '#000000'}
                          type="button"
                          aria-label="Use the default role color"
                          aria-pressed={roleColor === '#000000'}
                          onclick={() => setRoleColor('#000000')}
                          ><span>✓</span><small>Default</small></button
                        >
                        <label
                          class="role-color-custom"
                          class:selected={!roleColorPalette.includes(roleColor) &&
                            roleColor !== '#000000'}
                        >
                          <input
                            bind:value={roleColor}
                            type="color"
                            aria-label="Choose a custom role color"
                          />
                          <span style={`--selected-role-color: ${roleColor}`}></span>
                          <small>Custom</small>
                        </label>
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
                        <span>Hex</span>
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
                          ><strong>Display separately</strong><small
                            >Hoist members in this role.</small
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
                          ><strong>Allow mentions</strong><small
                            >Let anyone mention this role.</small
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
                      <span>Search permissions</span>
                      <input bind:value={permissionSearch} placeholder="Search permissions" />
                    </label>
                    {#if permissionChecked(Permission.ADMINISTRATOR)}
                      <div class="notice-banner warning-banner" role="alert">
                        <Icon name="shield" size={18} />
                        <span>
                          <strong>Administrator bypasses every channel restriction.</strong>
                          Only grant it to people who should have unrestricted control of this guild.
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
                                    Also requires: {permissionDependencies(
                                      permission[3].dependencies
                                    )}
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
                        Assign this role to guild members. Search includes members beyond the
                        currently loaded page.
                      </p>
                      <label class="form-field member-search-field">
                        <span>Search members</span>
                        <input
                          bind:value={roleMemberSearch}
                          type="search"
                          placeholder="Search by name, username, or instance"
                          autocomplete="off"
                        />
                      </label>
                      {#if roleMemberSearchBusy}
                        <p class="field-hint" role="status">Searching members…</p>
                      {:else if roleMemberSearchError}
                        <p class="form-error" role="alert">{roleMemberSearchError}</p>
                      {/if}
                      {#each visibleRoleMembers as member (entityKey(member.user))}
                        <label class="permission-row role-member-row">
                          <span>
                            <strong>{member.nickname ?? userDisplayName(member.user)}</strong>
                            <small>{userPublicHandle(member.user) ?? 'Profile unavailable'}</small>
                          </span>
                          <input
                            type="checkbox"
                            checked={member.role_ids.includes(selectedRole.id)}
                            disabled={busy ||
                              selectedRole.id === guild.id ||
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
                          <h3>No matching members</h3>
                          <p>Try a display name, username, nickname, or instance domain.</p>
                        </div>
                      {/if}
                      {#if membersHaveMore && !roleMemberSearch.trim()}
                        <button
                          class="secondary-button settings-load-more"
                          type="button"
                          disabled={membersLoadingMore}
                          onclick={loadMoreMembers}
                        >
                          {membersLoadingMore ? 'Loading…' : 'Load more members'}
                        </button>
                      {/if}
                    </div>
                  {/if}
                  <div class="form-actions spread-actions">
                    {#if selectedRole.id !== guild.id && canManageSelectedRole}
                      <button
                        class="danger-text-button"
                        type="button"
                        disabled={busy}
                        onclick={deleteRole}
                      >
                        <Icon name="trash" size={16} />Delete role
                      </button>
                    {:else}
                      <span class="field-hint">The default role cannot be deleted.</span>
                    {/if}
                    <button class="primary-button" disabled={busy || !canManageSelectedRole}
                      >Save role</button
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
              <h2>Audit log</h2>
              <p>Review administrative actions, affected resources, reasons, and field changes.</p>
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
              <h2>Members</h2>
              <p>
                {currentMembers.length} loaded member{currentMembers.length === 1 ? '' : 's'} in this
                guild.
              </p>
            </div>
          </div>
          <label class="form-field member-search-field settings-card">
            <span>Search members</span>
            <input
              bind:value={memberSearch}
              type="search"
              placeholder="Search by name, username, nickname, or instance"
              autocomplete="off"
            />
            {#if memberSearchBusy}
              <small role="status">Searching members…</small>
            {:else if memberSearch.trim()}
              <small
                >{visibleMembers.length} matching member{visibleMembers.length === 1
                  ? ''
                  : 's'}</small
              >
            {/if}
          </label>
          {#if memberSearchError}<p class="form-error" role="alert">{memberSearchError}</p>{/if}
          <div class="settings-card member-management-list">
            {#each visibleMembers as member (entityKey(member.user))}
              <article class="member-management-row">
                <span class="avatar avatar-medium">
                  {#if member.user.avatar_hash}
                    <img
                      src={assetUrl(member.user.avatar_hash, 'thumbnail_128', member.user)}
                      alt=""
                    />
                  {:else}
                    {member.user.profile_resolved === false
                      ? '•'
                      : member.user.username.slice(0, 1).toUpperCase()}
                  {/if}
                </span>
                <div class="member-management-identity">
                  <strong>{member.nickname ?? userDisplayName(member.user)}</strong>
                  <small>{userPublicHandle(member.user) ?? 'Profile unavailable'}</small>
                </div>
                <div class="member-role-tags">
                  {#each (guild.roles ?? []).filter((role) => role.id !== guild?.id) as role (entityKey(role))}
                    <label>
                      <input
                        type="checkbox"
                        checked={member.role_ids.includes(role.id)}
                        disabled={!canManageRoles || busy}
                        onchange={(event) =>
                          void toggleMemberRole(member, role, event.currentTarget.checked)}
                      />
                      <span>{role.name}</span>
                    </label>
                  {/each}
                </div>
                <div class="member-management-actions">
                  {#if member.timeout_indefinite}
                    <span class="sanction-badge">Timed out indefinitely</span>
                  {:else if member.timeout_until}
                    <span class="sanction-badge"
                      >Timed out until {formatDateTime(member.timeout_until)}</span
                    >
                  {/if}
                  {#if canModerateMembers && isModeratableMember(member)}
                    {#if canTimeoutMembers}
                      <button
                        class="secondary-button small-button"
                        type="button"
                        disabled={busy || memberModerationBusy}
                        onclick={(event) =>
                          void openMemberModeration(
                            member,
                            member.timeout_indefinite || member.timeout_until
                              ? 'untimeout'
                              : 'timeout',
                            event.currentTarget
                          )}
                      >
                        {member.timeout_indefinite || member.timeout_until
                          ? 'Remove timeout'
                          : 'Timeout'}
                      </button>
                    {/if}
                    {#if canKickMembers}
                      <button
                        class="secondary-button small-button"
                        type="button"
                        disabled={busy || memberModerationBusy}
                        onclick={(event) =>
                          void openMemberModeration(member, 'kick', event.currentTarget)}
                        >Kick</button
                      >
                    {/if}
                    {#if canBanMembers}
                      <button
                        class="danger-text-button small-button"
                        type="button"
                        disabled={busy || memberModerationBusy}
                        onclick={(event) =>
                          void openMemberModeration(member, 'ban', event.currentTarget)}>Ban</button
                      >
                    {/if}
                  {/if}
                </div>
              </article>
            {:else}
              <div class="empty-state compact-empty">
                <span><Icon name={memberSearch.trim() ? 'search' : 'users'} /></span>
                <h3>{memberSearch.trim() ? 'No matching members' : 'No members loaded'}</h3>
                <p>
                  {memberSearch.trim()
                    ? 'Try a display name, username, nickname, or instance domain.'
                    : 'Member information may be temporarily unavailable.'}
                </p>
              </div>
            {/each}
            {#if currentMembers.length && !memberSearch.trim()}
              <nav class="member-pagination" aria-label="Guild member pages">
                <button
                  class="secondary-button small-button"
                  type="button"
                  disabled={!memberHasPreviousPage || membersLoadingMore}
                  onclick={showPreviousMemberPage}>Previous</button
                >
                <span>
                  Page {memberPage + 1}{membersHaveMore
                    ? ` of at least ${memberPageCount}`
                    : ` of ${memberPageCount}`}
                </span>
                <button
                  class="secondary-button small-button"
                  type="button"
                  disabled={!memberHasNextPage || membersLoadingMore}
                  onclick={() => void showNextMemberPage()}
                >
                  {membersLoadingMore ? 'Loading…' : 'Next'}
                </button>
              </nav>
            {/if}
          </div>
          {#if canBanMembers}
            <div class="settings-card sanction-list">
              <div class="settings-list-heading">
                <div>
                  <strong>Active user bans</strong>
                  <p>Expired bans disappear automatically and no longer block joining.</p>
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
                    <small>{userPublicHandle(ban.user) ?? 'Profile unavailable'}</small>
                    <span>{ban.reason ?? 'No reason provided'}</span>
                  </div>
                  <span
                    >{ban.expires_at
                      ? `Until ${formatDateTime(ban.expires_at)}`
                      : 'Permanent'}</span
                  >
                  <button
                    class="secondary-button"
                    type="button"
                    disabled={busy}
                    onclick={() => void unbanUser(ban)}>Unban</button
                  >
                </article>
              {:else}
                <p class="field-hint">No active user bans.</p>
              {/each}
            </div>
          {/if}

          {#if canBanInstances}
            <div class="settings-card instance-ban-card">
              <div class="settings-list-heading">
                <div>
                  <strong>Federated instance bans</strong>
                  <p>Block an entire instance from participating in this guild.</p>
                </div>
              </div>
              <div class="federation-warning" role="note">
                <Icon name="shield" size={20} />
                <div>
                  <strong>This removes every current member from that instance.</strong>
                  <p>
                    Their home will be instructed to delete cached guild data. That deletion is best
                    effort: a malicious or modified instance can retain data it already received.
                    Existing messages authored by those members remain in this guild.
                  </p>
                </div>
              </div>
              <div class="instance-ban-form">
                <label class="form-field">
                  <span>Exact instance domain</span>
                  <input
                    bind:value={instanceBanDomain}
                    maxlength="253"
                    placeholder="chat.example.net"
                    disabled={busy}
                  />
                </label>
                <label class="form-field">
                  <span>Duration</span>
                  <select bind:value={instanceBanDuration} disabled={busy}>
                    <option value="3600">1 hour</option>
                    <option value="86400">1 day</option>
                    <option value="604800">7 days</option>
                    <option value="2592000">30 days</option>
                    <option value="permanent">Permanent</option>
                  </select>
                </label>
                <label class="form-field instance-ban-reason">
                  <span>Reason <small>optional</small></span>
                  <input
                    bind:value={instanceBanReason}
                    maxlength="512"
                    placeholder="Visible in the audit log"
                    disabled={busy}
                  />
                </label>
                <button
                  class="danger-button"
                  type="button"
                  disabled={busy || !instanceBanDomain.trim()}
                  onclick={() => void banFederatedInstance()}>Ban instance</button
                >
              </div>
              <div class="sanction-list embedded-list">
                {#each instanceBans as ban (ban.instance_domain)}
                  <article class="sanction-row instance-sanction-row">
                    <span class="section-icon"><Icon name="globe" /></span>
                    <div>
                      <strong>{ban.instance_domain}</strong>
                      <span>{ban.reason ?? 'No reason provided'}</span>
                    </div>
                    <span
                      >{ban.expires_at
                        ? `Until ${formatDateTime(ban.expires_at)}`
                        : 'Permanent'}</span
                    >
                    <button
                      class="secondary-button"
                      type="button"
                      disabled={busy}
                      onclick={() => void unbanFederatedInstance(ban)}>Remove ban</button
                    >
                  </article>
                {:else}
                  <p class="field-hint">No federated instances are banned from this guild.</p>
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
              <h2>Invites</h2>
              <p>Create bounded links and revoke them whenever you need to.</p>
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
                <strong>Create an invite</strong>
                <p>Choose an optional destination, lifetime, and use limit.</p>
              </div>
              <label class="form-field compact-field">
                <span>Destination</span>
                <select bind:value={inviteChannel} disabled={busy}>
                  <option value="">Guild landing channel</option>
                  {#each (guild.channels ?? []).filter((channel) => channel.type !== 4 && channelHasPermission(channel, Permission.CREATE_INVITE)) as channel (entityKey(channel))}
                    <option value={entityKey(channel)}>{channel.name}</option>
                  {/each}
                </select>
              </label>
              <label class="form-field compact-field">
                <span>Expires</span>
                <select bind:value={inviteMaxAge} disabled={busy}>
                  <option value="1800">30 minutes</option>
                  <option value="3600">1 hour</option>
                  <option value="21600">6 hours</option>
                  <option value="86400">1 day</option>
                  <option value="604800">7 days</option>
                  <option value="">Never</option>
                </select>
              </label>
              <label class="form-field compact-field">
                <span>Maximum uses</span>
                <input
                  bind:value={inviteMaxUses}
                  type="number"
                  min="1"
                  max="100"
                  placeholder="Unlimited"
                  disabled={busy}
                />
              </label>
              <details class="invite-advanced-options">
                <summary>Advanced options</summary>
                <div class="invite-advanced-grid">
                  <label class="toggle-row">
                    <input type="checkbox" bind:checked={inviteTemporary} disabled={busy} />
                    <span
                      ><strong>Temporary membership</strong><small
                        >Remove members when their final voice connection ends unless a role is
                        assigned.</small
                      ></span
                    >
                  </label>
                  <label class="toggle-row">
                    <input type="checkbox" bind:checked={inviteUnique} disabled={busy} />
                    <span
                      ><strong>Always create a new code</strong><small
                        >When off, an equivalent reusable invite may be returned.</small
                      ></span
                    >
                  </label>
                  <label class="form-field compact-field">
                    <span>Voice invite target</span>
                    <select bind:value={inviteTargetType} disabled={busy}>
                      <option value="">None</option>
                      <option value="stream">Member's Go Live stream</option>
                    </select>
                    <small>Voice targets require a voice or Stage destination.</small>
                  </label>
                  {#if inviteTargetType === 'stream'}
                    <label class="form-field compact-field">
                      <span>Streaming member</span>
                      <select bind:value={inviteTargetUser} required disabled={busy}>
                        <option value="">Choose a member</option>
                        {#each members as member (entityKey(member.user))}
                          <option value={entityRef(member.user)}
                            >{member.user.display_name ?? member.user.username} · {member.user
                              .origin_domain}</option
                          >
                        {/each}
                      </select>
                      <small>The member must currently be able to stream in the destination.</small>
                    </label>
                  {/if}
                  <label class="form-field compact-field">
                    <span>Scheduled event</span>
                    <select bind:value={inviteScheduledEvent} disabled={busy}>
                      <option value="">No event association</option>
                      {#each scheduledEvents as scheduledEvent (scheduledEventRef(scheduledEvent))}
                        <option value={scheduledEventRef(scheduledEvent)}
                          >{scheduledEvent.name} · {formatDateTime(
                            scheduledEvent.scheduled_start_time
                          )}</option
                        >
                      {/each}
                    </select>
                    <small>Event details are included independently of a voice target.</small>
                  </label>
                  {#if canManageRoles}
                    <label class="form-field compact-field">
                      <span>Roles (optional)</span>
                      <select multiple bind:value={inviteRoleIds} size="5" disabled={busy}>
                        {#each (guild.roles ?? []).filter((role) => role.id !== guild?.id && canManageRole(role)) as role (entityKey(role))}
                          <option value={entityRef(role)}>{role.name}</option>
                        {/each}
                      </select>
                      <small
                        >Members receive these roles when they accept—even if they already joined.
                        Only roles below your highest role are available.</small
                      >
                    </label>
                  {/if}
                </div>
              </details>
              <button class="primary-button" disabled={busy}>
                <Icon name="plus" size={16} />Create invite
              </button>
            </form>
            {#if createdInvite}
              <div class="settings-card created-invite-card" role="status">
                <div>
                  <strong>Your new invite is ready</strong>
                  <p>
                    Anyone with this link can use it until its limits are reached. Keep it private
                    when the guild is private.
                  </p>
                </div>
                <code>{inviteUrl(createdInvite.code)}</code>
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy}
                  onclick={() => copyInvite(createdInvite!)}
                >
                  <Icon name="copy" size={17} />Copy invite link
                </button>
              </div>
            {/if}
          {/if}

          {#if canManageGuild || canViewAuditLog}
            <div class="settings-card invite-list">
              <div class="settings-list-heading">
                <strong>Active invites</strong>
                <span>{invites.length}</span>
              </div>
              {#each invites as invite (invite.code)}
                <article class="invite-row">
                  <div>
                    <code>{invite.code}</code>
                    <span>
                      {#if invite.uses !== undefined}
                        {invite.uses}{invite.max_uses ? ` / ${invite.max_uses}` : ''} uses ·
                      {/if}{invite.expires_at
                        ? `expires ${formatDateTime(invite.expires_at)}`
                        : 'never expires'}{invite.temporary
                        ? ' · temporary'
                        : ''}{invite.target_type
                        ? ` · targets ${invite.target_type.replaceAll('_', ' ')}`
                        : ''}{invite.scheduled_event_id ? ' · includes scheduled event' : ''}{invite
                        .role_ids?.length
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
                  <h3>No active invites</h3>
                  <p>Create one when you are ready to welcome someone new.</p>
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
              <h2>Guild access</h2>
              <p>Leave this community or manage its ownership and permanent deletion.</p>
            </div>
          </div>
          {#if isGuildOwner}
            <div class="settings-card guild-ownership-card">
              <div class="settings-list-heading">
                <div>
                  <strong>Transfer ownership</strong>
                  <p>Ownership can be transferred to any eligible human member of this guild.</p>
                </div>
              </div>
              <div class="inline-settings-form">
                <label class="form-field">
                  <span>New owner</span>
                  <select bind:value={ownershipTarget} disabled={busy}>
                    <option value="">Choose a member</option>
                    {#each ownershipCandidates as member (entityKey(member.user))}
                      <option value={entityRef(member.user)}>
                        {member.nickname ?? userDisplayName(member.user)} · {userPublicHandle(
                          member.user
                        ) ?? 'Profile unavailable'}
                      </option>
                    {/each}
                  </select>
                </label>
                <button
                  class="secondary-button"
                  type="button"
                  disabled={busy || !ownershipTarget}
                  onclick={requestOwnershipTransfer}>Transfer ownership</button
                >
              </div>
              {#if !ownershipCandidates.length}
                <p class="field-hint">No other eligible human member can receive ownership.</p>
              {/if}
            </div>
            <div class="settings-card danger-zone guild-delete-card">
              <div>
                <span>Permanent action</span>
                <h3>Delete guild</h3>
                <p>
                  Deletes all guild data at its home and sends durable access revocations to remote
                  member instances.
                </p>
              </div>
              <button
                class="danger-button"
                type="button"
                disabled={busy}
                onclick={requestDeleteGuild}
              >
                <Icon name="trash" size={16} />Delete guild
              </button>
            </div>
          {:else}
            <div class="settings-card danger-zone guild-leave-card">
              <div>
                <span>Membership</span>
                <h3>Leave guild</h3>
                <p>You will need a new valid invitation before you can return.</p>
              </div>
              <button
                class="danger-button"
                type="button"
                disabled={busy}
                onclick={requestLeaveGuild}
              >
                <Icon name="logout" size={16} />Leave guild
              </button>
            </div>
          {/if}
        </section>
      {/if}
    {/if}

    {#if !channelOnly}
      <footer class="settings-footer">
        <span>{guild?.name ?? 'Guild'}</span>
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
      aria-label="Cancel moderation action"
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
          <p>Member moderation</p>
          <h2 id="member-moderation-title">{memberModerationTitle(memberModerationDialog)}</h2>
        </div>
        <button type="button" aria-label="Cancel" onclick={cancelMemberModeration}>×</button>
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
                'Profile unavailable'}</small
            >
          </div>
        </div>
        <p id="member-moderation-description" class="confirmation-copy">
          {memberModerationDescription(memberModerationDialog)}
        </p>
        {#if memberModerationDialog.action === 'timeout'}
          <label class="channel-dialog-field">
            Duration
            <select bind:value={timeoutDuration} disabled={memberModerationBusy}>
              <option value="600">10 minutes</option>
              <option value="3600">1 hour</option>
              <option value="86400">1 day</option>
              <option value="604800">7 days</option>
              <option value="2419200">28 days</option>
              <option value="permanent">Indefinite</option>
            </select>
          </label>
        {:else if memberModerationDialog.action === 'ban'}
          <div class="moderation-inline-selects">
            <label class="channel-dialog-field">
              Duration
              <select bind:value={banDuration} disabled={memberModerationBusy}>
                <option value="3600">1 hour</option>
                <option value="86400">1 day</option>
                <option value="604800">7 days</option>
                <option value="2592000">30 days</option>
                <option value="permanent">Permanent</option>
              </select>
            </label>
            <label class="channel-dialog-field">
              Delete messages
              <select bind:value={banDeleteSeconds} disabled={memberModerationBusy}>
                <option value="0">None</option>
                <option value="3600">Previous hour</option>
                <option value="86400">Previous day</option>
                <option value="604800">Previous 7 days</option>
              </select>
            </label>
          </div>
        {/if}
        <label class="channel-dialog-field">
          Reason <span class="field-optional">Optional</span>
          <textarea
            bind:value={moderationReason}
            maxlength="512"
            rows="3"
            placeholder="Visible in the guild audit log"
            disabled={memberModerationBusy}
          ></textarea>
        </label>
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <footer>
          <button
            bind:this={memberModerationCancel}
            class="secondary-button"
            type="button"
            onclick={cancelMemberModeration}>Cancel</button
          >
          <button
            class={memberModerationDialog.action === 'untimeout'
              ? 'primary-button'
              : 'danger-button'}
            type="submit"
            disabled={memberModerationBusy}
          >
            {memberModerationBusy
              ? 'Applying…'
              : memberModerationDialog.action === 'untimeout'
                ? 'Remove timeout'
                : memberModerationDialog.action === 'timeout'
                  ? 'Apply timeout'
                  : memberModerationDialog.action === 'kick'
                    ? 'Kick member'
                    : 'Ban member'}
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
      aria-label="Cancel destructive action"
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
          <p>Destructive action</p>
          <h2 id="destructive-confirmation-title">{destructiveConfirmation.title}</h2>
        </div>
        <button
          type="button"
          disabled={busy}
          aria-label="Cancel"
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
            Type <strong>{destructiveConfirmation.verificationText}</strong> to confirm
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
            onclick={closeDestructiveConfirmation}>Cancel</button
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
