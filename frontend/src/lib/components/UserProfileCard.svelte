<script lang="ts">
  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import { entityRef } from '$lib/chat/refs';
  import type { PresenceStatus, Relationship, Role, UserSummary } from '$lib/chat/types';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { assetUrl } from '$lib/media/assets';
  import { placeContextMenu } from '$lib/ui/context-menu';
  import { portal } from '$lib/ui/portal';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { onMount, tick } from 'svelte';
  import Icon from './Icon.svelte';

  let {
    user,
    presence = 'offline',
    x,
    y,
    isSelf = false,
    onClose,
    onMessage,
    moderationActions = [],
    onModerate,
    roles = [],
    roleIds = [],
    manageableRoles = [],
    onRoleChange
  }: {
    user: UserSummary;
    presence?: PresenceStatus;
    x: number;
    y: number;
    isSelf?: boolean;
    onClose: () => void;
    onMessage?: (user: UserSummary) => void;
    moderationActions?: Array<{ id: 'kick' | 'timeout' | 'ban'; label: string }>;
    onModerate?: (user: UserSummary, action: 'kick' | 'timeout' | 'ban') => void;
    roles?: Role[];
    roleIds?: string[];
    manageableRoles?: Role[];
    onRoleChange?: (user: UserSummary, role: Role, enabled: boolean) => Promise<void>;
  } = $props();

  let popover = $state<HTMLElement | null>(null);
  let feedback = $state('');
  let relationshipType = $state<Relationship['type'] | 'none' | 'loading' | 'unavailable'>(
    'loading'
  );
  let relationshipBusy = $state(false);
  let relationshipError = $state('');
  let roleBusy = $state<string | null>(null);
  let roleError = $state('');
  let rolePickerOpen = $state(false);
  let roleSearch = $state('');
  const assignedRoles = $derived(
    roles
      .filter((role) => roleIds.includes(role.id))
      .sort((left, right) => right.position - left.position)
  );
  const availableRoles = $derived(
    manageableRoles.filter(
      (role) =>
        !roleIds.includes(role.id) &&
        role.name.toLowerCase().includes(roleSearch.trim().toLowerCase())
    )
  );

  function roleIsManageable(role: Role): boolean {
    return manageableRoles.some(
      (candidate) => candidate.id === role.id && candidate.origin_domain === role.origin_domain
    );
  }
  const statusLabel = $derived(
    presence === 'dnd'
      ? 'Do not disturb'
      : presence === 'idle'
        ? 'Idle'
        : presence === 'online'
          ? 'Online'
          : 'Offline'
  );

  onMount(() => {
    const controller = new AbortController();
    void tick().then(() => {
      if (!popover) return;
      placeContextMenu(popover, x, y);
      popover.focus();
    });
    if (!isSelf && user.profile_resolved !== false) {
      void api<Relationship[]>('/users/@me/relationships', { signal: controller.signal })
        .then((relationships) => {
          const relationship = relationships.find(
            (candidate) =>
              candidate.user.id === user.id && candidate.user.origin_domain === user.origin_domain
          );
          relationshipType = relationship?.type ?? 'none';
        })
        .catch((caught: unknown) => {
          if (controller.signal.aborted) return;
          relationshipType = 'unavailable';
          relationshipError = userErrorMessage(
            caught,
            'Could not load friendship status. Try reopening this profile.'
          );
        });
    }
    return () => controller.abort();
  });

  async function updateFriendship() {
    if (
      isSelf ||
      user.profile_resolved === false ||
      relationshipBusy ||
      !['none', 'pending_in'].includes(relationshipType)
    ) {
      return;
    }
    relationshipBusy = true;
    relationshipError = '';
    try {
      const relationship = await api<Relationship>('/users/@me/relationships', {
        method: 'POST',
        body: JSON.stringify({ handle: `@${user.handle}` })
      });
      relationshipType = relationship.type;
    } catch (caught) {
      relationshipError = userErrorMessage(caught, 'Could not update friendship. Try again.');
    } finally {
      relationshipBusy = false;
    }
  }

  function friendshipLabel(): string {
    switch (relationshipType) {
      case 'friend':
        return 'Friends';
      case 'pending_in':
        return 'Accept friend request';
      case 'pending_out':
        return 'Request sent';
      case 'blocked':
        return 'Blocked';
      case 'loading':
        return 'Checking friendship…';
      case 'unavailable':
        return 'Friendship unavailable';
      default:
        return 'Add friend';
    }
  }

  async function copyValue(value: string, successMessage: string) {
    try {
      await navigator.clipboard.writeText(value);
      feedback = successMessage;
    } catch {
      feedback = 'Browser denied clipboard access. Allow clipboard permission and try again.';
    }
  }

  function actionLabel(successMessage: string, defaultLabel: string): string {
    return feedback === successMessage || feedback.startsWith('Browser denied clipboard access')
      ? feedback
      : defaultLabel;
  }

  function dismissRolePicker(event: PointerEvent) {
    if (!rolePickerOpen) return;
    const target = event.target;
    if (target instanceof Element && target.closest('.user-role-add, .user-role-picker')) return;
    rolePickerOpen = false;
    roleSearch = '';
  }

  async function changeRole(role: Role, enabled: boolean) {
    if (!onRoleChange || roleBusy) return false;
    roleBusy = role.id;
    roleError = '';
    try {
      await onRoleChange(user, role, enabled);
      return true;
    } catch (caught) {
      roleError = userErrorMessage(caught, 'Could not update this role. Try again.');
      return false;
    } finally {
      roleBusy = null;
    }
  }
</script>

<svelte:window
  onkeydown={(event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    }
  }}
/>

<div
  use:portal
  class="user-popover-layer"
  role="presentation"
  onpointerdown={(event) => {
    if (event.target === event.currentTarget) onClose();
  }}
  oncontextmenu={(event) => {
    event.preventDefault();
    event.stopPropagation();
  }}
  onkeydown={(event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    }
  }}
>
  <div
    bind:this={popover}
    class="user-popover"
    role="dialog"
    aria-modal="true"
    aria-label={`${userDisplayName(user)}'s profile`}
    tabindex="-1"
    oncontextmenu={(event) => {
      event.preventDefault();
      event.stopPropagation();
    }}
    onpointerdown={dismissRolePicker}
  >
    <div class="user-popover-banner">
      {#if user.banner_hash}
        <img src={assetUrl(user.banner_hash, 'thumbnail_512', user)} alt="" />
      {/if}
      <button
        class="user-popover-close"
        type="button"
        aria-label="Close profile"
        onclick={(event) => {
          event.stopPropagation();
          onClose();
        }}
      >
        <span aria-hidden="true">×</span>
      </button>
    </div>

    <div class="user-popover-content">
      <div class="user-popover-avatar-wrap">
        <span class="user-popover-avatar" aria-hidden="true">
          {#if user.avatar_hash}
            <img src={assetUrl(user.avatar_hash, 'thumbnail_128', user)} alt="" />
          {:else}
            {user.profile_resolved === false ? '•' : user.username.slice(0, 1).toUpperCase()}
          {/if}
        </span>
        <i class={`user-popover-presence presence-${presence}`} title={statusLabel}></i>
      </div>

      <div class="user-popover-identity">
        <h2>{userDisplayName(user)}</h2>
        {#if userPublicHandle(user)}
          <p>@{userPublicHandle(user)}</p>
        {:else}
          <p>Profile unavailable; Kaede will refresh it automatically.</p>
        {/if}
      </div>

      <div class="user-popover-status">
        <i class={`user-popover-status-dot presence-${presence}`}></i>
        <span>{user.custom_status?.trim() || statusLabel}</span>
      </div>

      {#if user.bio?.trim()}
        <section class="user-popover-about" aria-labelledby="user-popover-about-heading">
          <h3 id="user-popover-about-heading">About me</h3>
          <p>{user.bio.trim()}</p>
        </section>
      {/if}

      {#if assignedRoles.length || manageableRoles.length}
        <section class="user-popover-roles" aria-labelledby="user-popover-roles-heading">
          <div class="user-role-heading">
            <h3 id="user-popover-roles-heading">Roles</h3>
            {#if manageableRoles.length && onRoleChange}
              <button
                class="user-role-add"
                type="button"
                aria-label="Add role"
                aria-expanded={rolePickerOpen}
                title="Add role"
                onclick={(event) => {
                  event.stopPropagation();
                  rolePickerOpen = !rolePickerOpen;
                  roleSearch = '';
                }}>+</button
              >
            {/if}
          </div>
          <div class="user-role-tags">
            {#each assignedRoles as role (role.id + '@' + role.origin_domain)}
              <span>
                <i style={`--role-color: #${role.color.toString(16).padStart(6, '0')}`}></i>
                {role.name}
                {#if roleIsManageable(role) && onRoleChange}
                  <button
                    type="button"
                    aria-label={`Remove ${role.name}`}
                    title={`Remove ${role.name}`}
                    disabled={roleBusy !== null}
                    onclick={(event) => {
                      event.stopPropagation();
                      void changeRole(role, false);
                    }}>×</button
                  >
                {/if}
              </span>
            {/each}
            {#if !assignedRoles.length}<span class="user-role-empty">No assigned roles</span>{/if}
          </div>
          {#if rolePickerOpen && onRoleChange}
            <div
              class="user-role-picker"
              role="dialog"
              tabindex="-1"
              aria-label="Add a role"
              onpointerdown={(event) => event.stopPropagation()}
            >
              <input
                bind:value={roleSearch}
                type="search"
                placeholder="Search roles"
                aria-label="Search roles"
              />
              <div>
                {#each availableRoles as role (role.id + '@' + role.origin_domain)}
                  <button
                    type="button"
                    disabled={roleBusy !== null}
                    onclick={(event) => {
                      event.stopPropagation();
                      void changeRole(role, true).then((saved) => {
                        if (saved) {
                          rolePickerOpen = false;
                          roleSearch = '';
                        }
                      });
                    }}
                  >
                    <i style={`--role-color: #${role.color.toString(16).padStart(6, '0')}`}></i>
                    <span>{role.name}</span>
                    {#if roleBusy === role.id}<small>Saving…</small>{/if}
                  </button>
                {:else}
                  <p>No matching roles available.</p>
                {/each}
              </div>
            </div>
          {/if}
          {#if roleError}<p class="user-popover-error" role="alert">{roleError}</p>{/if}
        </section>
      {/if}

      <div class="user-popover-actions">
        {#if isSelf}
          <a class="user-popover-primary" href={resolve('/settings#profile')} onclick={onClose}>
            <Icon name="edit" size={17} />
            <span>Edit profile</span>
          </a>
        {:else if onMessage && user.profile_resolved !== false}
          <button type="button" class="user-popover-primary" onclick={() => onMessage?.(user)}>
            <Icon name="message" size={17} />
            <span>Message</span>
          </button>
        {/if}
        {#if !isSelf && user.profile_resolved !== false}
          <button
            type="button"
            class:relationship-friend={relationshipType === 'friend'}
            disabled={relationshipBusy || !['none', 'pending_in'].includes(relationshipType)}
            aria-label={friendshipLabel()}
            title={friendshipLabel()}
            onclick={updateFriendship}
          >
            <Icon name={relationshipType === 'friend' ? 'check' : 'users'} size={17} />
            <span>{relationshipBusy ? 'Updating…' : friendshipLabel()}</span>
          </button>
        {/if}
        {#if userPublicHandle(user)}
          <button
            type="button"
            onclick={() => copyValue(`@${userPublicHandle(user)}`, 'Username copied')}
          >
            <Icon name={feedback === 'Username copied' ? 'check' : 'copy'} size={17} />
            <span>{actionLabel('Username copied', 'Copy username')}</span>
          </button>
        {/if}
        {#if developerMode.enabled}
          <button type="button" onclick={() => copyValue(entityRef(user), 'User ID copied')}>
            <Icon name={feedback === 'User ID copied' ? 'check' : 'hash'} size={17} />
            <span>{actionLabel('User ID copied', 'Copy technical user ID')}</span>
          </button>
        {/if}
        {#if moderationActions.length && onModerate}
          <div class="user-popover-moderation" role="group" aria-label="Moderation actions">
            {#each moderationActions as action (action.id)}
              <button
                type="button"
                class:danger-action={action.id === 'kick' || action.id === 'ban'}
                onclick={() => {
                  onClose();
                  onModerate?.(user, action.id);
                }}
              >
                <Icon name={action.id === 'timeout' ? 'clock' : 'shield'} size={17} />
                <span>{action.label}</span>
              </button>
            {/each}
          </div>
        {/if}
      </div>
      {#if relationshipError}
        <p class="user-popover-error" role="alert">{relationshipError}</p>
      {/if}
    </div>
  </div>
</div>

<style>
  .user-popover-layer {
    position: fixed;
    z-index: 219;
    inset: 0;
  }

  .user-popover {
    position: fixed;
    z-index: 1;
    width: min(340px, calc(100vw - 20px));
    max-height: calc(100vh - 20px);
    overflow-x: hidden;
    overflow-y: auto;
    border: 1px solid var(--line);
    border-radius: 18px;
    color: var(--text);
    background: var(--surface-raised);
    box-shadow: 0 24px 72px rgb(0 0 0 / 48%);
  }

  .user-popover:focus {
    outline: none;
  }

  .user-popover-banner {
    position: relative;
    height: 106px;
    overflow: hidden;
    background:
      radial-gradient(circle at 82% 12%, rgb(255 255 255 / 12%), transparent 34%),
      linear-gradient(135deg, color-mix(in srgb, var(--accent) 76%, #31221f), var(--pine));
  }

  .user-popover-banner::after {
    position: absolute;
    inset: 0;
    background: linear-gradient(to bottom, transparent 45%, rgb(0 0 0 / 18%));
    content: '';
    pointer-events: none;
  }

  .user-popover-banner img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .user-popover-close {
    position: absolute;
    z-index: 2;
    top: 10px;
    right: 10px;
    display: grid;
    width: 30px;
    height: 30px;
    place-items: center;
    border: 1px solid rgb(255 255 255 / 14%);
    border-radius: 50%;
    color: white;
    background: rgb(14 13 12 / 68%);
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    backdrop-filter: blur(8px);
  }

  .user-popover-close:hover {
    background: rgb(14 13 12 / 88%);
  }

  .user-popover-content {
    position: relative;
    display: grid;
    gap: 0;
    padding: 50px 18px 18px;
  }

  .user-popover-avatar-wrap {
    position: absolute;
    top: -43px;
    left: 18px;
  }

  .user-popover-avatar {
    display: grid;
    width: 78px;
    height: 78px;
    place-items: center;
    overflow: hidden;
    border: 5px solid var(--surface-raised);
    border-radius: 50%;
    color: white;
    background: var(--pine);
    font-size: 1.45rem;
    font-weight: 850;
  }

  .user-popover-avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .user-popover-presence {
    position: absolute;
    right: 1px;
    bottom: 1px;
    width: 18px;
    height: 18px;
    border: 4px solid var(--surface-raised);
    border-radius: 50%;
  }

  .user-popover-identity {
    min-width: 0;
  }

  .user-popover-identity h2 {
    overflow: hidden;
    margin: 0;
    font-size: 1.2rem;
    line-height: 1.25;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .user-popover-identity p {
    overflow: hidden;
    margin: 3px 0 0;
    color: var(--text-muted);
    font-size: 0.74rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .user-popover-status {
    display: inline-flex;
    width: fit-content;
    align-items: center;
    gap: 7px;
    margin-top: 12px;
    border: 1px solid var(--line-soft);
    border-radius: 999px;
    padding: 5px 9px;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-size: 0.68rem;
    font-weight: 650;
  }

  .user-popover-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .presence-online {
    background: #3ba55d;
  }

  .presence-idle {
    background: #f0a61b;
  }

  .presence-dnd {
    background: #ed4245;
  }

  .presence-offline {
    background: #747f8d;
  }

  .user-popover-about {
    display: grid;
    gap: 6px;
    margin-top: 14px;
    border-top: 1px solid var(--line-soft);
    padding-top: 13px;
  }

  .user-popover-about h3 {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.63rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .user-popover-about p {
    margin: 0;
    color: var(--text-soft);
    font-size: 0.76rem;
    line-height: 1.5;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .user-popover-roles {
    display: grid;
    gap: 8px;
    margin-top: 14px;
    border-top: 1px solid var(--line-soft);
    padding-top: 13px;
  }

  .user-popover-roles h3 {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.63rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .user-role-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .user-role-add {
    display: grid;
    width: 24px;
    height: 24px;
    place-items: center;
    border: 0;
    border-radius: 7px;
    padding: 0;
    color: var(--text-muted);
    background: transparent;
    font-size: 1.15rem;
    cursor: pointer;
  }

  .user-role-add:hover,
  .user-role-add[aria-expanded='true'] {
    color: var(--text);
    background: var(--surface-hover);
  }

  .user-role-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .user-role-tags span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid var(--line-soft);
    border-radius: 999px;
    padding: 4px 8px;
    color: var(--text-soft);
    background: var(--surface-subtle);
    font-size: 0.68rem;
  }

  .user-role-tags span > button {
    display: grid;
    width: 16px;
    height: 16px;
    place-items: center;
    border: 0;
    border-radius: 50%;
    padding: 0;
    color: var(--text-muted);
    background: transparent;
    line-height: 1;
    cursor: pointer;
  }

  .user-role-tags span > button:hover {
    color: var(--text);
    background: var(--surface-hover);
  }

  .user-role-tags i,
  .user-role-picker button > i {
    width: 8px;
    height: 8px;
    flex: 0 0 auto;
    border-radius: 50%;
    background: var(--role-color);
  }

  .user-role-empty {
    border-style: dashed !important;
    margin: 0;
    color: var(--text-muted);
    font-size: 0.72rem;
  }

  .user-role-picker {
    display: grid;
    gap: 8px;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 8px;
    background: var(--surface-overlay, var(--surface-raised));
    box-shadow: var(--shadow-md);
  }

  .user-role-picker > input {
    width: 100%;
    min-height: 36px;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0 10px;
    color: var(--text);
    background: var(--surface-inset);
  }

  .user-role-picker > div {
    display: grid;
    max-height: 180px;
    overflow-y: auto;
  }

  .user-role-picker button {
    display: grid;
    min-height: 38px;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    border: 0;
    border-radius: 8px;
    padding: 7px 8px;
    color: var(--text-soft);
    background: transparent;
    font-size: 0.74rem;
    text-align: left;
    cursor: pointer;
  }

  .user-role-picker button:hover {
    color: var(--text);
    background: var(--surface-subtle);
  }

  .user-role-picker small,
  .user-role-picker p {
    margin: 0;
    color: var(--text-muted);
    font-size: 0.63rem;
  }

  .user-role-picker p {
    padding: 10px 8px;
  }

  .user-popover-actions {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 8px;
    margin-top: 16px;
  }

  .user-popover-moderation {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0.45rem;
    margin-top: 0.15rem;
    border-top: 1px solid var(--line-soft);
    padding-top: 0.65rem;
  }

  .user-popover-moderation .danger-action {
    color: var(--danger);
  }

  .user-popover-actions button,
  .user-popover-actions a {
    display: inline-flex;
    min-width: 0;
    min-height: 38px;
    align-items: center;
    justify-content: center;
    gap: 7px;
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 0 12px;
    color: var(--text);
    background: var(--surface-subtle);
    font-size: 0.7rem;
    font-weight: 720;
    white-space: nowrap;
    cursor: pointer;
    text-decoration: none;
  }

  .user-popover-actions button:not(:disabled):hover {
    border-color: color-mix(in srgb, var(--text-muted) 52%, var(--line));
    background: var(--surface-hover);
  }

  .user-popover-actions button:disabled {
    cursor: default;
    opacity: 0.72;
  }

  .user-popover-actions .relationship-friend {
    border-color: color-mix(in srgb, #3ba55d 50%, var(--line));
    color: color-mix(in srgb, #3ba55d 84%, var(--text));
  }

  .user-popover-actions .user-popover-primary {
    border-color: var(--accent);
    color: var(--on-accent);
    background: var(--accent);
  }

  .user-popover-actions .user-popover-primary:hover {
    border-color: var(--accent-hover);
    background: var(--accent-hover);
  }

  .user-popover-error {
    margin: 9px 2px 0;
    color: var(--danger);
    font-size: 0.68rem;
    line-height: 1.35;
  }

  @media (max-width: 420px) {
    .user-popover {
      width: calc(100vw - 16px);
    }
  }
</style>
