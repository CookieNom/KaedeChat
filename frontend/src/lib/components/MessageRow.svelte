<script module lang="ts">
  let activeMessageMenu: ((restoreFocus: boolean) => void) | null = null;

  function claimMessageMenu(close: (restoreFocus: boolean) => void) {
    if (activeMessageMenu !== close) activeMessageMenu?.(false);
    activeMessageMenu = close;
  }

  function releaseMessageMenu(close: (restoreFocus: boolean) => void) {
    if (activeMessageMenu === close) activeMessageMenu = null;
  }
</script>

<script lang="ts">
  import type { Message, PresenceStatus, UserSummary } from '$lib/chat/types';
  import { entityRef } from '$lib/chat/refs';
  import { inviteReferencesInMessage } from '$lib/chat/invites';
  import { klipyGifUrl } from '$lib/chat/gifs';
  import { placeContextMenu } from '$lib/ui/context-menu';
  import { DISMISS_FLOATING_LAYERS_EVENT, dismissFloatingLayers } from '$lib/ui/floating-layers';
  import { portal } from '$lib/ui/portal';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { preferredLocale } from '$lib/ui/locale';
  import { assetUrl } from '$lib/media/assets';
  import { onDestroy, tick } from 'svelte';
  import Markdown from './Markdown.svelte';
  import InviteEmbed from './InviteEmbed.svelte';

  let {
    message,
    compact = false,
    canEdit = false,
    presence = 'offline',
    mentionUsers = [],
    onEdit,
    onDelete,
    onMessageAuthor,
    onRetry,
    onViewProfile,
    moderationActions = [],
    onModerate
  }: {
    message: Message;
    compact?: boolean;
    canEdit?: boolean;
    presence?: PresenceStatus;
    mentionUsers?: UserSummary[];
    onEdit?: (message: Message) => void;
    onDelete?: (message: Message) => void;
    onMessageAuthor?: (message: Message) => void;
    onRetry?: (message: Message) => void;
    onViewProfile?: (message: Message, event: MouseEvent) => void;
    moderationActions?: Array<{ id: 'kick' | 'timeout' | 'ban'; label: string }>;
    onModerate?: (user: UserSummary, action: 'kick' | 'timeout' | 'ban') => void;
  } = $props();

  let menuOpen = $state(false);
  let menuElement = $state<HTMLElement | null>(null);
  let rowElement = $state<HTMLElement | null>(null);
  let menuTrigger: HTMLElement | null = null;
  let confirmingDelete = $state(false);
  let deleteConfirmationButton = $state<HTMLButtonElement | null>(null);
  let feedback = $state('');
  let menuListenersActive = false;
  const closeExclusiveMenu = (restoreFocus: boolean) => closeMenu(restoreFocus);

  const editAvailable = $derived(
    canEdit && !message.deleted_at && !message.pending && !message.queued
  );
  const menuAvailable = $derived(!message.pending && !message.queued);
  const inviteReferences = $derived(
    message.content ? inviteReferencesInMessage(message.content) : []
  );
  const gifUrl = $derived(klipyGifUrl(message.content));

  function authorName(): string {
    return (
      message.webhook?.name ??
      message.author?.display_name ??
      message.author?.username ??
      'Unknown author'
    );
  }

  function visibleTime(): string {
    return new Date(message.created_at).toLocaleTimeString(preferredLocale(), {
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  function accessibleTime(): string {
    return new Date(message.created_at).toLocaleString(preferredLocale(), {
      dateStyle: 'long',
      timeStyle: 'short'
    });
  }

  function menuItems(): HTMLElement[] {
    return Array.from(
      menuElement?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? []
    );
  }

  function showMenu(pointerX: number, pointerY: number, trigger: HTMLElement | null) {
    dismissFloatingLayers();
    claimMessageMenu(closeExclusiveMenu);
    menuTrigger = trigger;
    menuOpen = true;
    addMenuListeners();
    void tick().then(() => {
      if (!menuOpen || !menuElement) return;
      placeContextMenu(menuElement, pointerX, pointerY);
      menuItems()[0]?.focus();
    });
  }

  function openContextMenu(event: MouseEvent) {
    if (!menuAvailable) return;
    event.preventDefault();
    event.stopPropagation();
    const target = event.currentTarget as HTMLElement;
    const bounds = target.getBoundingClientRect();
    const pointerX = event.clientX || bounds.left + Math.min(bounds.width / 2, 24);
    const pointerY = event.clientY || bounds.top + Math.min(bounds.height / 2, 24);
    const focused = document.activeElement;
    showMenu(
      pointerX,
      pointerY,
      focused instanceof HTMLElement && rowElement?.contains(focused) ? focused : rowElement
    );
  }

  function openKeyboardMenu(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    const trigger = event.currentTarget as HTMLButtonElement;
    const bounds = rowElement?.getBoundingClientRect() ?? trigger.getBoundingClientRect();
    showMenu(bounds.left + Math.min(bounds.width / 2, 32), bounds.top + 24, trigger);
  }

  function editMessage(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(true);
    onEdit?.(message);
  }

  function requestDelete(event: MouseEvent) {
    event.stopPropagation();
    confirmingDelete = true;
    void tick().then(() => deleteConfirmationButton?.focus());
  }

  function cancelDelete(event: MouseEvent) {
    event.stopPropagation();
    confirmingDelete = false;
    void tick().then(() =>
      menuItems()
        .find((item) => item.classList.contains('danger-item'))
        ?.focus()
    );
  }

  function deleteMessage(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(true);
    onDelete?.(message);
  }

  function messageAuthor(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(true);
    onMessageAuthor?.(message);
  }

  function viewProfile(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(false);
    onViewProfile?.(message, event);
  }

  function moderateAuthor(action: 'kick' | 'timeout' | 'ban', event: MouseEvent) {
    event.stopPropagation();
    const author = message.author;
    closeMenu(false);
    if (author) onModerate?.(author, action);
  }

  async function copy(value: string, event: MouseEvent) {
    event.stopPropagation();
    closeMenu(true);
    feedback = '';
    await tick();
    try {
      await navigator.clipboard.writeText(value);
      feedback = 'Copied to clipboard.';
    } catch {
      feedback = 'Clipboard access was denied.';
    }
  }

  function messageLink(): string {
    return `${window.location.origin}${window.location.pathname}${window.location.search}#message-${entityRef(message)}`;
  }

  function closeMenu(restoreFocus = true) {
    if (!menuOpen) return;
    menuOpen = false;
    confirmingDelete = false;
    removeMenuListeners();
    releaseMessageMenu(closeExclusiveMenu);
    const trigger = menuTrigger;
    menuTrigger = null;
    if (restoreFocus && trigger?.isConnected) void tick().then(() => trigger.focus());
  }

  function menuKeydown(event: KeyboardEvent) {
    const items = menuItems();
    if (!items.length) return;
    const current = items.findIndex((item) => item === document.activeElement);
    let next = current;
    if (event.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % items.length;
    else if (event.key === 'ArrowUp')
      next = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
      return;
    } else if (event.key === 'Tab') {
      closeMenu(false);
      return;
    } else return;
    event.preventDefault();
    event.stopPropagation();
    items[next]?.focus();
  }

  function windowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') closeMenu(true);
  }

  function windowClick(event: MouseEvent) {
    if (!menuElement?.contains(event.target as Node)) closeMenu(false);
  }

  function addMenuListeners() {
    if (menuListenersActive) return;
    menuListenersActive = true;
    window.addEventListener('click', windowClick);
    window.addEventListener('keydown', windowKeydown);
    window.addEventListener('resize', windowResize);
    window.addEventListener('scroll', windowScroll, true);
    window.addEventListener('contextmenu', windowContextMenu);
    window.addEventListener(DISMISS_FLOATING_LAYERS_EVENT, windowDismissFloatingLayers);
  }

  function removeMenuListeners() {
    if (!menuListenersActive) return;
    menuListenersActive = false;
    window.removeEventListener('click', windowClick);
    window.removeEventListener('keydown', windowKeydown);
    window.removeEventListener('resize', windowResize);
    window.removeEventListener('scroll', windowScroll, true);
    window.removeEventListener('contextmenu', windowContextMenu);
    window.removeEventListener(DISMISS_FLOATING_LAYERS_EVENT, windowDismissFloatingLayers);
  }

  function windowResize() {
    closeMenu(true);
  }

  function windowScroll() {
    closeMenu(true);
  }

  function windowContextMenu(event: MouseEvent) {
    if (
      !menuElement?.contains(event.target as Node) &&
      !rowElement?.contains(event.target as Node)
    ) {
      closeMenu(false);
    }
  }

  function windowDismissFloatingLayers() {
    closeMenu(false);
  }

  onDestroy(() => {
    removeMenuListeners();
    releaseMessageMenu(closeExclusiveMenu);
  });
</script>

<!-- eslint-disable svelte/no-navigation-without-resolve -- authenticated media URLs are API resources, not Svelte routes -->

<article
  bind:this={rowElement}
  class:sending={message.pending || message.delivery_status === 'pending'}
  class:failed={message.failed || message.delivery_status === 'failed'}
  class:compact
  class:menu-open={menuOpen}
  class="message-row"
  id={`message-${entityRef(message)}`}
  oncontextmenu={openContextMenu}
>
  <span class="visually-hidden" role="status" aria-live="polite">{feedback}</span>
  {#if menuAvailable}
    <button
      class="visually-hidden"
      type="button"
      aria-label={`Open actions for message from ${authorName()} at ${accessibleTime()}`}
      aria-haspopup="menu"
      aria-expanded={menuOpen}
      aria-controls={menuOpen ? `message-actions-${entityRef(message)}` : undefined}
      onclick={openKeyboardMenu}
    >
      Message actions
    </button>
  {/if}
  <button
    class="message-avatar"
    class:profile-trigger={Boolean(message.author && !message.webhook && onViewProfile)}
    type="button"
    disabled={!message.author || Boolean(message.webhook) || !onViewProfile}
    aria-label={message.author ? `View ${authorName()}'s profile` : 'Unknown author'}
    onclick={viewProfile}
  >
    {#if !compact && (message.webhook?.avatar_hash || message.author?.avatar_hash)}
      <img
        src={assetUrl(
          message.webhook?.avatar_hash ?? message.author?.avatar_hash ?? '',
          'thumbnail_128',
          message.author?.origin_domain ?? message.origin_domain
        )}
        alt=""
      />
    {:else}
      {compact ? '' : (message.author?.username.slice(0, 1).toUpperCase() ?? '•')}
    {/if}
    {#if !compact && message.author && !message.webhook}
      <i class={`presence-dot presence-${presence}`} aria-hidden="true"></i>
    {/if}
  </button>
  <div class="message-body">
    {#if !compact}
      <header>
        {#if message.author && !message.webhook && onViewProfile}
          <button class="message-author" type="button" onclick={viewProfile}>{authorName()}</button>
        {:else}
          <strong>{authorName()}</strong>
        {/if}
        {#if message.webhook}<small class="webhook-badge">WEBHOOK</small>{/if}
        <time datetime={message.created_at} title={accessibleTime()}>{visibleTime()}</time>
      </header>
    {:else}
      <span class="visually-hidden">{authorName()}, {accessibleTime()}</span>
    {/if}
    {#if message.deleted_at}
      <p class="message-removed">Message removed</p>
    {:else if gifUrl}
      <a class="klipy-gif" href={gifUrl} target="_blank" rel="noopener noreferrer">
        <img src={gifUrl} alt="GIF shared from KLIPY" loading="lazy" />
        <small>Powered by KLIPY</small>
      </a>
    {:else if message.content}
      <Markdown content={message.content} {mentionUsers} />
      {#each inviteReferences as reference (reference)}
        <InviteEmbed {reference} />
      {/each}
    {/if}
    {#if !message.deleted_at && message.attachments?.length}
      <div class="message-attachments">
        {#each message.attachments as attachment (`${attachment.id}@${attachment.origin_domain}`)}
          {#if attachment.scan_status === 'pending'}
            <span class="attachment-file">Scanning {attachment.filename}…</span>
          {:else if attachment.scan_status === 'infected'}
            <span class="attachment-file">Removed unsafe attachment</span>
          {:else if attachment.scan_status === 'failed'}
            <span class="attachment-file">Attachment processing unavailable</span>
          {:else if attachment.content_type.startsWith('image/')}
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- authenticated media is served by the API, not a Svelte route -->
            <a href={`/media/${attachment.origin_domain}/${attachment.id}/original`}>
              <img
                src={`/media/${attachment.origin_domain}/${attachment.id}/thumbnail_512`}
                alt={attachment.filename}
                width={attachment.width ?? 512}
                height={attachment.height ?? 320}
              />
            </a>
          {:else}
            <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- authenticated media is served by the API, not a Svelte route -->
            <a
              class="attachment-file"
              href={`/media/${attachment.origin_domain}/${attachment.id}/original`}
            >
              📎 {attachment.filename}
            </a>
          {/if}
        {/each}
      </div>
    {/if}
    {#if message.edited_at || message.failed || message.delivery_status === 'failed' || message.queued}
      <div class="message-meta-actions">
        {#if message.edited_at}<small>(edited)</small>{/if}
        {#if message.failed || message.delivery_status === 'failed'}
          <small class="delivery-failed" role="status">
            {message.failure_reason ?? 'Message not delivered.'}
          </small>
        {/if}
        {#if (message.failed || message.delivery_status === 'failed') && onRetry && message.retryable !== false}
          <button type="button" onclick={() => onRetry?.(message)}>Retry</button>
        {:else if message.queued}
          <small>Queued for the guild home ⏱</small>
        {/if}
      </div>
    {/if}
  </div>
  {#if menuOpen}
    <div
      use:portal
      bind:this={menuElement}
      id={`message-actions-${entityRef(message)}`}
      class="message-context-menu"
      role="menu"
      tabindex="-1"
      aria-label="Message actions"
      onkeydown={menuKeydown}
    >
      {#if onMessageAuthor && message.author && !message.deleted_at}
        <button type="button" role="menuitem" tabindex="-1" onclick={messageAuthor}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M20 15a4 4 0 0 1-4 4H8l-4 2V7a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v8Z" />
          </svg>
          <span>Message {message.author.display_name ?? message.author.username}</span>
        </button>
      {/if}
      {#if message.author && !message.webhook && onViewProfile}
        <button type="button" role="menuitem" tabindex="-1" onclick={viewProfile}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="8" r="4" />
            <path d="M4 21a8 8 0 0 1 16 0" />
          </svg>
          <span>View profile</span>
        </button>
        <button
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => copy(`@${message.author?.handle}`, event)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M16 8a4 4 0 1 1-4-4c4 0 7 3 7 7v1a3 3 0 0 1-6 0V8" />
            <path d="M19 19a9 9 0 1 1 2-4" />
          </svg>
          <span>Copy username</span>
        </button>
        {#if developerMode.enabled}
          <button
            type="button"
            role="menuitem"
            tabindex="-1"
            onclick={(event) => copy(`${message.author_id}@${message.author_domain}`, event)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M9 3 7 21m10-18-2 18M3 9h18M2 15h18" />
            </svg>
            <span>Copy technical user ID</span>
          </button>
        {/if}
      {/if}
      {#if message.author && moderationActions.length && onModerate}
        {#each moderationActions as action, index (action.id)}
          <button
            class:menu-separator={index === 0}
            class:danger-item={action.id === 'kick' || action.id === 'ban'}
            type="button"
            role="menuitem"
            tabindex="-1"
            onclick={(event) => moderateAuthor(action.id, event)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              {#if action.id === 'timeout'}
                <circle cx="12" cy="12" r="9" />
                <path d="M12 7v5l3 2" />
              {:else}
                <path d="M12 3 4 6v5c0 5 3.4 8.2 8 10 4.6-1.8 8-5 8-10V6l-8-3Z" />
              {/if}
            </svg>
            <span>{action.label} {message.author.display_name ?? message.author.username}</span>
          </button>
        {/each}
      {/if}
      {#if editAvailable}
        <button
          class:menu-separator={Boolean(onMessageAuthor && message.author)}
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={editMessage}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m4 16-.8 4.8L8 20l11-11-4-4L4 16Z" />
            <path d="m13.5 6.5 4 4" />
          </svg>
          <span>Edit message</span>
          <kbd>↑</kbd>
        </button>
        {#if onDelete}
          {#if confirmingDelete}
            <div class="message-delete-confirmation" role="group" aria-label="Confirm deletion">
              <p>Delete this message?</p>
              <div>
                <button type="button" role="menuitem" tabindex="-1" onclick={cancelDelete}>
                  Cancel
                </button>
                <button
                  bind:this={deleteConfirmationButton}
                  class="danger-item"
                  type="button"
                  role="menuitem"
                  tabindex="-1"
                  onclick={deleteMessage}
                >
                  Delete
                </button>
              </div>
            </div>
          {:else}
            <button
              class="danger-item"
              type="button"
              role="menuitem"
              tabindex="-1"
              onclick={requestDelete}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6" />
              </svg>
              <span>Delete message</span>
            </button>
          {/if}
        {/if}
      {/if}
      {#if message.content && !message.deleted_at}
        <button
          class:menu-separator={editAvailable || Boolean(onMessageAuthor)}
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => copy(message.content ?? '', event)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M8 8h11v12H8z" />
            <path d="M16 8V4H4v12h4" />
          </svg>
          <span>Copy text</span>
        </button>
      {/if}
      <button
        type="button"
        role="menuitem"
        tabindex="-1"
        onclick={(event) => copy(messageLink(), event)}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.2 1.2" />
          <path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.2-1.2" />
        </svg>
        <span>Copy message link</span>
      </button>
      {#if developerMode.enabled}
        <button
          type="button"
          role="menuitem"
          tabindex="-1"
          onclick={(event) => copy(entityRef(message), event)}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M9 3 7 21m10-18-2 18M3 9h18M2 15h18" />
          </svg>
          <span>Copy message ID</span>
        </button>
      {/if}
    </div>
  {/if}
</article>
