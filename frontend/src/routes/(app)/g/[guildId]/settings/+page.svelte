<script lang="ts">
  import { page } from '$app/state';
  import { resolve } from '$app/paths';
  import { api, ApiError } from '$lib/api/client';
  import { firstNavigableChannel, groupChannels } from '$lib/chat/channels';
  import { entityKey, entityRef } from '$lib/chat/refs';
  import type { Channel, Guild, GuildMemberSummary, Role, UserSummary } from '$lib/chat/types';
  import Icon from '$lib/components/Icon.svelte';
  import Toast from '$lib/components/Toast.svelte';
  import { PERMISSION_METADATA, Permission } from '$lib/generated/permissions';
  import { uploadObject, type UploadTicket } from '$lib/media/uploads';
  import { guildChannelPath, type ChannelSettingsPanel } from '$lib/navigation/routes';
  import { formatDateTime } from '$lib/ui/locale';
  import { tick } from 'svelte';

  interface GuildView extends Guild {
    banner_hash: string | null;
  }

  interface InviteSummary {
    code: string;
    channel_id: string | null;
    uses: number;
    max_uses: number | null;
    expires_at: string | null;
    created_at: string;
  }

  interface WebhookSummary {
    id: string;
    guild_id: string;
    guild_domain: string;
    channel_id: string;
    channel_domain: string;
    name: string;
    avatar_hash: string | null;
    revoked: boolean;
    token?: string;
  }

  interface MemberSummary extends GuildMemberSummary {
    joined_at?: string;
    timeout_until?: string | null;
  }

  interface ChannelOverwrite {
    target_id: string;
    target_domain: string;
    target_type: 'role' | 'member';
    allow: string;
    deny: string;
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
      };

  type GuildAssetKind = 'icon' | 'banner';
  type GuildAssetStage = 'uploading' | 'scanning';

  const acceptedImageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
  const channelOnly = $derived(Boolean(page.params.channelId));
  const guildId = $derived(page.params.guildId ?? '');
  const channelId = $derived(channelOnly ? (page.params.channelId ?? '') : '');
  let localDomain = $state('');
  let guild = $state<GuildView | null>(null);
  let members = $state<MemberSummary[]>([]);
  let membersHaveMore = $state(false);
  let membersLoadingMore = $state(false);
  let invites = $state<InviteSummary[]>([]);
  let webhooks = $state<WebhookSummary[]>([]);
  let newWebhookName = $state('');
  let revealedWebhookToken = $state('');
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let loadGeneration = 0;
  let routeController: AbortController | null = null;
  let guildAssetKind = $state<GuildAssetKind | null>(null);
  let guildAssetStage = $state<GuildAssetStage | null>(null);
  let guildAssetProgress = $state(0);
  let guildAssetError = $state('');

  let name = $state('');
  let description = $state('');
  let guildHistoryPolicy = $state<'disabled' | 'full_retained'>('disabled');

  let selectedChannel = $state<Channel | null>(null);
  let channelName = $state('');
  let channelTopic = $state('');
  let channelParent = $state('');
  let channelSlowmode = $state(0);
  let channelHistoryPolicy = $state<'inherit' | 'disabled' | 'full_retained'>('inherit');
  let newChannelName = $state('');
  let newChannelType = $state(0);
  let newChannelParent = $state('');
  let channelOverwrites = $state<ChannelOverwrite[]>([]);
  let overwriteTarget = $state('');
  let overwriteAllow = $state('0');
  let overwriteDeny = $state('0');
  let overwriteSearch = $state('');
  let permissionSearch = $state('');
  let channelEditorPanel = $state<ChannelSettingsPanel>('overview');

  let selectedRole = $state<Role | null>(null);
  let roleName = $state('');
  let roleColor = $state('#7b7168');
  let rolePermissions = $state('0');
  let roleHoist = $state(false);
  let roleMentionable = $state(false);
  let newRoleName = $state('');
  let roleEditorTab = $state<'display' | 'permissions' | 'members'>('display');

  let inviteChannel = $state('');
  let inviteMaxAge = $state('86400');
  let inviteMaxUses = $state('');
  let createdInvite = $state<InviteSummary | null>(null);
  let destructiveConfirmation = $state<DestructiveConfirmation | null>(null);
  let confirmationDialog = $state<HTMLElement | null>(null);
  let confirmationCancelButton = $state<HTMLButtonElement | null>(null);
  let confirmationPreviousFocus: HTMLElement | null = null;

  const permissionGroups = [...new Set(PERMISSION_METADATA.map((item) => item.group))].map(
    (group) => ({
      name: group,
      permissions: PERMISSION_METADATA.filter((item) => item.group === group).map(
        (item) => [item.label, item.description, item.bit, item] as const
      )
    })
  );
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
    members.filter((member) =>
      `${member.nickname ?? ''} ${member.user.display_name ?? ''} ${member.user.username}`
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

  function hasPermission(permission: bigint): boolean {
    return Boolean(effectivePermissions & (permission | Permission.ADMINISTRATOR));
  }

  const canManageGuild = $derived(isLocalGuild && hasPermission(Permission.MANAGE_GUILD));
  const canManageChannels = $derived(isLocalGuild && hasPermission(Permission.MANAGE_CHANNELS));
  const canManageRoles = $derived(isLocalGuild && hasPermission(Permission.MANAGE_ROLES));
  const canViewMembers = $derived(isLocalGuild && hasPermission(Permission.VIEW_CHANNEL));
  const canCreateInvites = $derived(isLocalGuild && hasPermission(Permission.CREATE_INVITE));
  const canAccessInvites = $derived(canManageGuild || canCreateInvites);
  const canManageWebhooks = $derived(isLocalGuild && hasPermission(Permission.MANAGE_WEBHOOKS));
  const selectedEffectivePermissions = $derived.by(() => {
    try {
      return BigInt(selectedChannel?.permissions ?? guild?.permissions ?? '0');
    } catch {
      return 0n;
    }
  });

  function selectedHasPermission(permission: bigint): boolean {
    return Boolean(selectedEffectivePermissions & (permission | Permission.ADMINISTRATOR));
  }

  const canEditSelectedChannel = $derived(
    isLocalGuild && selectedHasPermission(Permission.MANAGE_CHANNELS)
  );
  const canEditSelectedPermissions = $derived(
    isLocalGuild && selectedHasPermission(Permission.MANAGE_ROLES)
  );
  const canCreateSelectedInvite = $derived(
    isLocalGuild && selectedHasPermission(Permission.CREATE_INVITE)
  );
  const canManageSelectedWebhooks = $derived(
    isLocalGuild && selectedHasPermission(Permission.MANAGE_WEBHOOKS)
  );
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
    if (panel === 'integrations') return 'Manage webhooks that can post in this channel.';
    if (panel === 'delete') return 'Permanently remove this channel and its configuration.';
    return 'Update the channel name, topic, category, and behavior.';
  }

  function roleColorValue(color: number): string {
    return `#${color.toString(16).padStart(6, '0')}`;
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
    channelName = channel.name ?? '';
    channelTopic = channel.topic ?? '';
    channelParent =
      channel.parent_id && channel.parent_domain
        ? `${channel.parent_id}@${channel.parent_domain}`
        : '';
    channelSlowmode = channel.rate_limit_per_user;
    channelHistoryPolicy = channel.federated_history_policy ?? 'inherit';
    error = '';
    notice = '';
    channelOverwrites = [];
    overwriteTarget = '';
    overwriteAllow = '0';
    overwriteDeny = '0';
    if (guild && canEditSelectedPermissions) void loadChannelOverwrites(channel);
  }

  function selectChannelPanel(panel: ChannelSettingsPanel) {
    if (panel === 'overview' && !canEditSelectedChannel) return;
    if (panel === 'permissions' && !canEditSelectedPermissions) return;
    if (panel === 'invites' && !(canAccessInvites && canCreateSelectedInvite)) return;
    if (panel === 'integrations' && !canManageSelectedWebhooks) return;
    if (panel === 'delete' && !canEditSelectedChannel) return;
    if (panel === 'invites' && selectedChannel) inviteChannel = entityKey(selectedChannel);
    channelEditorPanel = panel;
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
      if (!overwriteTarget && loaded[0]) {
        overwriteTarget = `${loaded[0].target_type}:${loaded[0].target_id}@${loaded[0].target_domain}`;
        selectOverwriteTarget(overwriteTarget);
      }
    } catch (caught) {
      if (selectedChannel && entityKey(selectedChannel) === entityKey(channel))
        error = caught instanceof ApiError ? caught.message : 'Could not load channel permissions.';
    }
  }

  function selectOverwriteTarget(value: string) {
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
    return member?.nickname ?? member?.user.display_name ?? member?.user.username ?? 'Member';
  }

  function overwritePermission(permission: bigint): 'inherit' | 'allow' | 'deny' {
    if (BigInt(overwriteAllow) & permission) return 'allow';
    if (BigInt(overwriteDeny) & permission) return 'deny';
    return 'inherit';
  }

  function setOverwritePermission(permission: bigint, value: string) {
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
    if (!guild || !selectedChannel || !overwriteTarget) return;
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
    if (!guild || !selectedChannel || !overwriteTarget) return;
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

  function selectRole(role: Role, force = false) {
    if (busy && !force) return;
    selectedRole = role;
    roleName = role.name;
    roleColor = roleColorValue(role.color);
    rolePermissions = role.permissions;
    roleHoist = role.hoist;
    roleMentionable = role.mentionable;
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
    try {
      const [loaded, currentUser] = await Promise.all([
        api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}`, { signal }),
        api<UserSummary>('/users/@me', { signal })
      ]);
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      localDomain = currentUser.origin_domain;
      guild = loaded;
      name = loaded.name;
      description = loaded.description ?? '';
      guildHistoryPolicy = loaded.federated_history_policy ?? 'disabled';
      const requestedChannel = loaded.channels?.find(
        (channel) => entityRef(channel) === targetChannel
      );
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
      const local = loaded.origin_domain.toLowerCase() === currentUser.origin_domain.toLowerCase();
      const permissions = BigInt(loaded.permissions ?? '0');
      const administrator = Boolean(permissions & Permission.ADMINISTRATOR);
      const optional: Promise<unknown>[] = [];
      if (local && (administrator || Boolean(permissions & Permission.VIEW_CHANNEL))) {
        optional.push(
          api<MemberSummary[]>(`/guilds/${encodeURIComponent(targetGuild)}/members?limit=101`, {
            signal
          }).then((value) => {
            if (generation === loadGeneration) {
              members = value.slice(0, 100);
              membersHaveMore = value.length > 100;
            }
          })
        );
      }
      if (local && (administrator || Boolean(permissions & Permission.MANAGE_GUILD))) {
        optional.push(
          api<InviteSummary[]>(`/guilds/${encodeURIComponent(targetGuild)}/invites`, {
            signal
          }).then((value) => {
            if (generation === loadGeneration) invites = value;
          })
        );
      }
      if (local && (administrator || Boolean(permissions & Permission.MANAGE_WEBHOOKS))) {
        optional.push(
          api<WebhookSummary[]>(`/guilds/${encodeURIComponent(targetGuild)}/webhooks`, {
            signal
          }).then((value) => {
            if (generation === loadGeneration) webhooks = value;
          })
        );
      }
      await Promise.all(optional);
      if (generation !== loadGeneration || targetGuild !== guildId) return;
    } catch (caught) {
      if (signal.aborted || generation !== loadGeneration || targetGuild !== guildId) return;
      error = caught instanceof ApiError ? caught.message : 'Could not load guild settings.';
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
      error = caught instanceof ApiError ? caught.message : 'The change could not be saved.';
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
    error = '';
    notice = '';
    await tick();
    confirmationCancelButton?.focus();
  }

  function closeDestructiveConfirmation() {
    if (busy) return;
    const previousFocus = confirmationPreviousFocus;
    destructiveConfirmation = null;
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
    if (busy || loading || !guild || !canManageGuild || !controller) return;
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
      await api(`/guilds/${encodeURIComponent(targetGuild)}/assets/${kind}`, {
        method: 'PUT',
        signal: controller.signal,
        body: JSON.stringify({ attachment_id: ticket.id })
      });

      for (let attempt = 0; attempt < 30; attempt += 1) {
        const attachment = await api<{ scan_status: string }>(`/attachments/${ticket.id}`, {
          signal: controller.signal
        });
        if (attachment.scan_status === 'clean') {
          await api(`/guilds/${encodeURIComponent(targetGuild)}/assets/${kind}`, {
            method: 'PUT',
            signal: controller.signal,
            body: JSON.stringify({ attachment_id: ticket.id })
          });
          const updated = await api<GuildView>(`/guilds/${encodeURIComponent(targetGuild)}`, {
            signal: controller.signal
          });
          if (
            controller.signal.aborted ||
            generation !== loadGeneration ||
            targetGuild !== guildId
          ) {
            return;
          }
          if (guild) guild = mergeGuildState(guild, updated);
          notice = `${kind === 'icon' ? 'Guild icon' : 'Guild banner'} updated.`;
          return;
        }
        if (attachment.scan_status === 'infected' || attachment.scan_status === 'failed') {
          throw new Error('The image did not pass media processing.');
        }
        await cancelableDelay(1000, controller.signal);
      }
      throw new Error('Media processing is taking longer than expected. Try again shortly.');
    } catch (caught) {
      if (controller.signal.aborted || generation !== loadGeneration || targetGuild !== guildId) {
        return;
      }
      guildAssetError =
        caught instanceof ApiError
          ? caught.message
          : caught instanceof Error
            ? caught.message
            : 'Could not update the guild image.';
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) {
        busy = false;
        guildAssetKind = null;
        guildAssetStage = null;
        guildAssetProgress = 0;
      }
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
          parent_id: newChannelType === 4 ? null : (parent?.id ?? null)
        })
      });
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) guild = { ...guild, channels: [...(guild.channels ?? []), channel] };
      newChannelName = '';
      selectChannel(channel, true);
      notice = `${channel.type === 4 ? 'Category' : 'Channel'} created.`;
    });
  }

  function saveChannel() {
    if (!canManageChannels || !selectedChannel) return;
    const target = selectedChannel;
    return run(async (targetGuild, generation) => {
      const parent = guild?.channels?.find(
        (channel) => entityKey(channel) === channelParent && channel.type === 4
      );
      const updated = await api<Channel>(
        `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(target))}`,
        {
          method: 'PATCH',
          headers: target.version ? { 'If-Match': `"${target.version}"` } : undefined,
          body: JSON.stringify({
            name: channelName,
            topic: channelTopic.trim() || null,
            parent_id: target.type === 4 ? null : (parent?.id ?? null),
            rate_limit_per_user: target.type === 4 ? 0 : channelSlowmode,
            federated_history_policy:
              target.type === 0 || target.type === 5 ? channelHistoryPolicy : 'inherit'
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
    if (!canManageChannels || !selectedChannel || !guild) return;
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

  function createRole() {
    if (!canManageRoles) return;
    return run(async (targetGuild, generation) => {
      const role = await api<Role>(`/guilds/${encodeURIComponent(targetGuild)}/roles`, {
        method: 'POST',
        body: JSON.stringify({ name: newRoleName, permissions: '0' })
      });
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      if (guild) guild = { ...guild, roles: [...(guild.roles ?? []), role] };
      newRoleName = '';
      selectRole(role, true);
      notice = 'Role created. Configure its permissions before assigning it.';
    });
  }

  function saveRole() {
    if (!canManageRoles || !selectedRole) return;
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
        guild = {
          ...guild,
          roles: guild.roles?.map((role) =>
            entityKey(role) === entityKey(updated) ? updated : role
          )
        };
      }
      selectRole(updated, true);
      notice = 'Role saved.';
    });
  }

  function moveSelectedRole(direction: -1 | 1) {
    if (!guild || !selectedRole || selectedRole.id === guild.id || busy) return;
    const ordered = [...(guild.roles ?? [])]
      .filter((role) => role.id !== guild?.id)
      .sort((left, right) => right.position - left.position || left.id.localeCompare(right.id));
    const index = ordered.findIndex((role) => entityKey(role) === entityKey(selectedRole!));
    const swapIndex = index + direction;
    if (index < 0 || swapIndex < 0 || swapIndex >= ordered.length) return;
    const current = ordered[index];
    const adjacent = ordered[swapIndex];
    if (!current.version || !adjacent.version) {
      error = 'Role versions are unavailable. Reload settings before reordering roles.';
      return;
    }
    return run(async (targetGuild, generation) => {
      const updated = await api<Role[]>(`/guilds/${encodeURIComponent(targetGuild)}/roles`, {
        method: 'PATCH',
        body: JSON.stringify({
          roles: [
            { id: current.id, position: adjacent.position, version: current.version },
            { id: adjacent.id, position: current.position, version: adjacent.version }
          ]
        })
      });
      if (generation !== loadGeneration || targetGuild !== guildId || !guild) return;
      const byKey = new Map(updated.map((role) => [entityKey(role), role]));
      guild = {
        ...guild,
        roles: guild.roles?.map((role) => byKey.get(entityKey(role)) ?? role)
      };
      const selected = byKey.get(entityKey(current)) ?? current;
      selectRole(selected, true);
      notice = 'Role order updated.';
    });
  }

  function deleteRole() {
    if (!canManageRoles || !selectedRole || !guild || selectedRole.id === guild.id) return;
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
      guild = { ...guild, roles: remaining };
      selectedRole = remaining.find((role) => role.id !== guild?.id) ?? remaining[0] ?? null;
      if (selectedRole) selectRole(selectedRole, true);
      members = members.map((member) => ({
        ...member,
        role_ids: member.role_ids.filter((id) => id !== target.id)
      }));
      notice = 'Role deleted.';
    });
  }

  function createInvite() {
    if (!canCreateInvites) return;
    return run(async (targetGuild, generation) => {
      const channel = guild?.channels?.find(
        (item) => entityKey(item) === inviteChannel && item.type !== 4
      );
      const invite = await api<InviteSummary>(
        `/guilds/${encodeURIComponent(targetGuild)}/invites`,
        {
          method: 'POST',
          body: JSON.stringify({
            channel_id: channel?.id ?? null,
            max_age_seconds: inviteMaxAge ? Number(inviteMaxAge) : null,
            max_uses: inviteMaxUses ? Number(inviteMaxUses) : null
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
    if (!canManageWebhooks || !selectedChannel || selectedChannel.type === 4) return;
    const channel = selectedChannel;
    return run(async (targetGuild, generation) => {
      const created = await api<WebhookSummary>(
        `/guilds/${encodeURIComponent(targetGuild)}/channels/${encodeURIComponent(entityRef(channel))}/webhooks`,
        {
          method: 'POST',
          body: JSON.stringify({ name: newWebhookName })
        }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      webhooks = [...webhooks, created];
      newWebhookName = '';
      revealedWebhookToken = created.token ?? '';
      notice = 'Webhook created. Copy its token now; it will not be shown again.';
    });
  }

  function rotateWebhook(webhook: WebhookSummary) {
    if (!canManageWebhooks) return;
    return run(async (_targetGuild, generation) => {
      const updated = await api<WebhookSummary>(`/webhooks/${webhook.id}/rotate`, {
        method: 'POST'
      });
      if (generation !== loadGeneration) return;
      webhooks = webhooks.map((item) => (item.id === webhook.id ? updated : item));
      revealedWebhookToken = updated.token ?? '';
      notice = 'Webhook token rotated. The previous token no longer works.';
    });
  }

  function deleteWebhook(webhook: WebhookSummary) {
    if (!canManageWebhooks) return;
    return run(async (_targetGuild, generation) => {
      await api(`/webhooks/${webhook.id}`, { method: 'DELETE' });
      if (generation !== loadGeneration) return;
      webhooks = webhooks.filter((item) => item.id !== webhook.id);
      revealedWebhookToken = '';
      notice = 'Webhook deleted.';
    });
  }

  function revokeInvite(invite: InviteSummary) {
    if (!canManageGuild) return;
    void openDestructiveConfirmation({
      kind: 'invite',
      target: invite,
      title: 'Revoke invite?',
      description: `Invite ${invite.code} will stop working immediately. People who already joined the guild will not be affected.`,
      confirmLabel: 'Revoke invite'
    });
  }

  function revokeConfirmedInvite(invite: InviteSummary) {
    return run(async (_targetGuild, generation) => {
      await api(`/invites/${encodeURIComponent(invite.code)}`, { method: 'DELETE' });
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
    } else {
      succeeded = await revokeConfirmedInvite(confirmation.target);
    }
    if (succeeded) closeDestructiveConfirmation();
  }

  async function copyInvite(invite: InviteSummary) {
    error = '';
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}/invite/${encodeURIComponent(invite.code)}`
      );
      notice = 'Invite link copied.';
    } catch {
      error = 'Clipboard access was denied by the browser.';
    }
  }

  function toggleMemberRole(member: MemberSummary, role: Role, enabled: boolean) {
    if (!canManageRoles) return;
    return run(async (targetGuild, generation) => {
      await api(
        `/guilds/${encodeURIComponent(targetGuild)}/members/${encodeURIComponent(entityRef(member.user))}/roles/${encodeURIComponent(entityRef(role))}`,
        { method: enabled ? 'PUT' : 'DELETE' }
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      members = members.map((item) => {
        if (entityKey(item.user) !== entityKey(member.user)) return item;
        return {
          ...item,
          role_ids: enabled
            ? [...new Set([...item.role_ids, role.id])]
            : item.role_ids.filter((id) => id !== role.id)
        };
      });
      notice = `${role.name} ${enabled ? 'assigned' : 'removed'}.`;
    });
  }

  async function loadMoreMembers() {
    if (membersLoadingMore || !membersHaveMore || !members.length) return;
    const targetGuild = guildId;
    const generation = loadGeneration;
    const after = entityRef(members[members.length - 1].user);
    membersLoadingMore = true;
    try {
      const page = await api<MemberSummary[]>(
        `/guilds/${encodeURIComponent(targetGuild)}/members?limit=101&after=${encodeURIComponent(after)}`
      );
      if (generation !== loadGeneration || targetGuild !== guildId) return;
      const next = page.slice(0, 100);
      const existing = new Set(members.map((member) => entityKey(member.user)));
      members = [...members, ...next.filter((member) => !existing.has(entityKey(member.user)))];
      membersHaveMore = page.length > 100;
    } catch (caught) {
      if (generation === loadGeneration && targetGuild === guildId) {
        error = caught instanceof ApiError ? caught.message : 'Could not load more members.';
      }
    } finally {
      if (generation === loadGeneration && targetGuild === guildId) membersLoadingMore = false;
    }
  }

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
              name={selectedChannel?.type === 2 ? 'volume' : 'hash'}
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
        {#if canAccessInvites && canCreateSelectedInvite && selectedChannel?.type !== 4}
          <button
            class:active={channelEditorPanel === 'invites'}
            type="button"
            onclick={() => selectChannelPanel('invites')}>Invites</button
          >
        {/if}
        {#if canManageSelectedWebhooks && (selectedChannel?.type === 0 || selectedChannel?.type === 5)}
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
            <img src={`/media/assets/${guild.icon_hash}/thumbnail_128`} alt="" />
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
        <p>Guild</p>
        <a href="#overview"><Icon name="server" size={18} />Overview</a>
        {#if canManageRoles}
          <a href="#roles"><Icon name="shield" size={18} />Roles</a>
        {/if}
        {#if canViewMembers}
          <p>Community</p>
          <a href="#members"><Icon name="users" size={18} />Members</a>
        {/if}
        {#if canAccessInvites}
          <a href="#invites"><Icon name="globe" size={18} />Invites</a>
        {/if}
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
              This guild is hosted by <strong>{guild.origin_domain}</strong>. Administrative changes
              must be made on its home instance.
            </div>
          {/if}

          <div class="profile-card">
            <div class="profile-banner">
              {#if guild.banner_hash}
                <img src={`/media/assets/${guild.banner_hash}/original`} alt="" />
              {:else}
                <span aria-hidden="true"></span>
              {/if}
            </div>
            <div class="profile-card-body">
              <span class="avatar avatar-large guild-avatar">
                {#if guild.icon_hash}
                  <img src={`/media/assets/${guild.icon_hash}/thumbnail_128`} alt="" />
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

          {#if canManageGuild}
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

      {#if channelOnly && (canEditSelectedChannel || canEditSelectedPermissions || canCreateSelectedInvite || canManageSelectedWebhooks)}
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
              {#each channelGroups as group (group.category ? entityKey(group.category) : 'ungrouped')}
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
                      name={channel.type === 2 ? 'volume' : channel.type === 5 ? 'bell' : 'hash'}
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
                  {#if canAccessInvites && canCreateSelectedInvite && selectedChannel.type !== 4}
                    <button
                      class:active={channelEditorPanel === 'invites'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'invites'}
                      onclick={() => selectChannelPanel('invites')}>Invites</button
                    >
                  {/if}
                  {#if canManageSelectedWebhooks && (selectedChannel.type === 0 || selectedChannel.type === 5)}
                    <button
                      class:active={channelEditorPanel === 'integrations'}
                      type="button"
                      role="tab"
                      aria-selected={channelEditorPanel === 'integrations'}
                      onclick={() => selectChannelPanel('integrations')}>Integrations</button
                    >
                  {/if}
                  {#if canEditSelectedChannel}
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
                        <span>Topic</span>
                        <textarea
                          bind:value={channelTopic}
                          maxlength="1024"
                          rows="3"
                          placeholder="What belongs in this channel?"
                          disabled={busy}
                        ></textarea>
                      </label>
                      {#if selectedChannel.type === 0 || selectedChannel.type === 5}
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
                      <label class="form-field compact-field">
                        <span>Category</span>
                        <select bind:value={channelParent} disabled={busy}>
                          <option value="">No category</option>
                          {#each (guild.channels ?? []).filter((channel) => channel.type === 4) as category (entityKey(category))}
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
                              disabled={busy}
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
                                disabled={busy}
                                aria-pressed={overwriteTarget ===
                                  `member:${entityRef(member.user)}`}
                                onclick={() =>
                                  selectOverwriteTarget(`member:${entityRef(member.user)}`)}
                              >
                                <span class="permission-target-avatar">
                                  {#if member.user.avatar_hash}
                                    <img
                                      src={`/media/assets/${member.user.avatar_hash}/thumbnail_128`}
                                      alt=""
                                    />
                                  {:else}
                                    {(member.nickname ?? member.user.username)
                                      .slice(0, 1)
                                      .toUpperCase()}
                                  {/if}
                                </span>
                                <span>
                                  {member.nickname ??
                                    member.user.display_name ??
                                    member.user.username}
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
                                    <span
                                      ><strong>{permission[0]}</strong><small>{permission[1]}</small
                                      ></span
                                    >
                                    <div
                                      class="permission-tristate"
                                      role="group"
                                      aria-label={`${permission[0]} channel override`}
                                    >
                                      <button
                                        class="deny"
                                        class:active={overwritePermission(permission[2]) === 'deny'}
                                        type="button"
                                        disabled={busy || !hasPermission(permission[2])}
                                        aria-label="Deny in this channel"
                                        title="Deny"
                                        onclick={() =>
                                          setOverwritePermission(permission[2], 'deny')}>×</button
                                      >
                                      <button
                                        class:active={overwritePermission(permission[2]) ===
                                          'inherit'}
                                        type="button"
                                        disabled={busy || !hasPermission(permission[2])}
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
                                        disabled={busy || !hasPermission(permission[2])}
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
                              disabled={busy}
                              onclick={() => void resetChannelOverwrite()}>Reset override</button
                            >
                            <button
                              class="primary-button"
                              type="button"
                              disabled={busy}
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
                {#if channelEditorPanel === 'invites' && canAccessInvites && canCreateSelectedInvite}
                  <section
                    class="channel-permissions-editor"
                    aria-labelledby="channel-invites-title"
                  >
                    <div>
                      <span>Channel access</span>
                      <h4 id="channel-invites-title">Invites</h4>
                      <p>Create links that open this channel after the person joins the guild.</p>
                    </div>
                    {#if canCreateInvites}
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
                          {#if canManageGuild}
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
                {#if channelEditorPanel === 'integrations' && canManageSelectedWebhooks}
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
                          ><strong>Save this token now</strong><code>{revealedWebhookToken}</code
                          ></span
                        >
                      </div>
                    {/if}
                    <div class="settings-list compact-list">
                      {#each selectedChannelWebhooks as webhook (webhook.id)}
                        <div class="settings-list-row">
                          <span><strong>{webhook.name}</strong><small>ID {webhook.id}</small></span>
                          <div class="row-actions">
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
                        This is permanent. Categories must be empty and channels containing retained
                        messages cannot be deleted.
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

          <form
            class="settings-card quick-create"
            onsubmit={(event) => {
              event.preventDefault();
              void createChannel();
            }}
          >
            <div>
              <strong>Create a channel</strong>
              <p>Add a text, voice, announcement channel, or a category.</p>
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
              </select>
            </label>
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
        </section>
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
                  class="settings-list-item role-item"
                  type="button"
                  disabled={busy}
                  onclick={() => selectRole(role)}
                >
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
                  <svg class="role-preview" viewBox="0 0 38 38" aria-hidden="true">
                    <rect width="38" height="38" rx="13" fill={roleColor} />
                    <text x="19" y="24" text-anchor="middle" fill={roleContrastColor(roleColor)}
                      >{roleName.slice(0, 1) || 'R'}</text
                    >
                  </svg>
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
                        <small>Higher roles can manage lower roles and members.</small>
                      </div>
                      <button
                        class="secondary-button"
                        type="button"
                        disabled={busy || selectedRole.id === guild.id}
                        aria-label="Move role higher"
                        onclick={() => void moveSelectedRole(-1)}>Move up</button
                      >
                      <button
                        class="secondary-button"
                        type="button"
                        disabled={busy || selectedRole.id === guild.id}
                        aria-label="Move role lower"
                        onclick={() => void moveSelectedRole(1)}>Move down</button
                      >
                    </div>
                    <div class="two-column-fields">
                      <label class="form-field compact-field">
                        <span>Name</span>
                        <input
                          bind:value={roleName}
                          maxlength="100"
                          required
                          disabled={busy || selectedRole.id === guild.id}
                        />
                      </label>
                      <label class="form-field compact-field">
                        <span>Color</span>
                        <input
                          class="color-input"
                          bind:value={roleColor}
                          type="color"
                          disabled={busy || selectedRole.id === guild.id}
                        />
                      </label>
                    </div>
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
                          disabled={busy || selectedRole.id === guild.id}
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
                          disabled={busy || selectedRole.id === guild.id}
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
                              <span
                                ><strong>{permission[0]}</strong><small>{permission[1]}</small
                                ></span
                              >
                              <input
                                type="checkbox"
                                checked={permissionChecked(permission[2])}
                                disabled={busy || !hasPermission(permission[2])}
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
                        Assign this role to loaded members. Load additional members below if this
                        guild is larger than the current page.
                      </p>
                      {#each members as member (entityKey(member.user))}
                        <label class="permission-row role-member-row">
                          <span>
                            <strong
                              >{member.nickname ??
                                member.user.display_name ??
                                member.user.username}</strong
                            >
                            <small>{member.user.handle}</small>
                          </span>
                          <input
                            type="checkbox"
                            checked={member.role_ids.includes(selectedRole.id)}
                            disabled={busy || selectedRole.id === guild.id}
                            onchange={(event) =>
                              void toggleMemberRole(
                                member,
                                selectedRole!,
                                event.currentTarget.checked
                              )}
                          />
                        </label>
                      {/each}
                      {#if membersHaveMore}
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
                    {#if selectedRole.id !== guild.id}
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
                    <button class="primary-button" disabled={busy}>Save role</button>
                  </div>
                </form>
              {/if}
            </div>
          </div>
        </section>
      {/if}

      {#if !channelOnly && canViewMembers}
        <section id="members" class="settings-section">
          <div class="settings-section-heading">
            <span class="section-icon"><Icon name="users" /></span>
            <div>
              <h2>Members</h2>
              <p>{members.length} loaded member{members.length === 1 ? '' : 's'} in this guild.</p>
            </div>
          </div>
          <div class="settings-card member-management-list">
            {#each members as member (entityKey(member.user))}
              <article class="member-management-row">
                <span class="avatar avatar-medium">
                  {#if member.user.avatar_hash}
                    <img src={`/media/assets/${member.user.avatar_hash}/thumbnail_128`} alt="" />
                  {:else}
                    {member.user.username.slice(0, 1).toUpperCase()}
                  {/if}
                </span>
                <div class="member-management-identity">
                  <strong
                    >{member.nickname ?? member.user.display_name ?? member.user.username}</strong
                  >
                  <small>{member.user.handle}</small>
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
              </article>
            {:else}
              <div class="empty-state compact-empty">
                <span><Icon name="users" /></span>
                <h3>No members loaded</h3>
                <p>Member information may be temporarily unavailable.</p>
              </div>
            {/each}
            {#if membersHaveMore}
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
                  {#each (guild.channels ?? []).filter((channel) => channel.type !== 4) as channel (entityKey(channel))}
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
                  max="1000"
                  placeholder="Unlimited"
                  disabled={busy}
                />
              </label>
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
                <code>{window.location.origin}/invite/{createdInvite.code}</code>
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

          {#if canManageGuild}
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
                      {invite.uses}{invite.max_uses ? ` / ${invite.max_uses}` : ''} uses ·
                      {invite.expires_at
                        ? `expires ${formatDateTime(invite.expires_at)}`
                        : 'never expires'}
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
                  <button
                    class="icon-button danger-icon-button"
                    type="button"
                    disabled={busy}
                    aria-label={`Revoke invite ${invite.code}`}
                    onclick={() => revokeInvite(invite)}
                  >
                    <Icon name="trash" size={17} />
                  </button>
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
    {/if}

    {#if !channelOnly}
      <footer class="settings-footer">
        <span>{guild?.name ?? 'Guild'}</span>
        <span>{guild ? `${guild.id}@${guild.origin_domain}` : ''}</span>
      </footer>
    {/if}
  </section>
</main>

{#if destructiveConfirmation}
  <div class="channel-dialog-layer">
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
        {#if error}<p class="form-error" role="alert">{error}</p>{/if}
        <footer>
          <button
            bind:this={confirmationCancelButton}
            class="secondary-button"
            type="button"
            disabled={busy}
            onclick={closeDestructiveConfirmation}>Cancel</button
          >
          <button class="danger-button" disabled={busy}>
            {busy
              ? destructiveConfirmation.kind === 'invite'
                ? 'Revoking…'
                : 'Deleting…'
              : destructiveConfirmation.confirmLabel}
          </button>
        </footer>
      </form>
    </div>
  </div>
{/if}
