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
  import { userErrorMessage } from '$lib/api/client';
  import type {
    Attachment,
    Channel,
    Message,
    PresenceStatus,
    Role,
    UserSummary
  } from '$lib/chat/types';
  import type { CustomEmojiOption } from '$lib/chat/emojis';
  import { userDisplayName, userPublicHandle } from '$lib/chat/users';
  import { entityRef } from '$lib/chat/refs';
  import { inviteReferencesInMessage } from '$lib/chat/invites';
  import { botInvitesInMessage } from '$lib/chat/bot-invites';
  import { gifFavoriteForUrl, isGifFavorite, klipyGifUrl, toggleGifFavorite } from '$lib/chat/gifs';
  import { previewableLink } from '$lib/chat/links';
  import { recentReactions, rememberReaction } from '$lib/chat/reactions';
  import { stickerFromToken, stickerUrl } from '$lib/chat/stickers';
  import { placeContextMenu } from '$lib/ui/context-menu';
  import { DISMISS_FLOATING_LAYERS_EVENT, dismissFloatingLayers } from '$lib/ui/floating-layers';
  import { portal } from '$lib/ui/portal';
  import { developerMode } from '$lib/ui/developer-mode.svelte';
  import { preferredLocale } from '$lib/ui/locale';
  import { assetUrl } from '$lib/media/assets';
  import { downloadEncryptedFile, type EncryptedFileManifest } from '$lib/e2ee/media';
  import {
    attachmentMediaPath,
    authenticatedMedia,
    downloadAuthenticatedMedia
  } from '$lib/media/authenticated';
  import { onDestroy, tick } from 'svelte';
  import Markdown from './Markdown.svelte';
  import InviteEmbed from './InviteEmbed.svelte';
  import BotInviteEmbed from './BotInviteEmbed.svelte';
  import LinkPreview from './LinkPreview.svelte';
  import MediaViewer from './MediaViewer.svelte';
  import ReactionEmoji from './ReactionEmoji.svelte';
  import ReactionPicker from './ReactionPicker.svelte';
  import ReactionViewer from './ReactionViewer.svelte';
  import ReportMessageDialog from './ReportMessageDialog.svelte';

  let {
    message,
    compact = false,
    canEdit = false,
    canDelete = false,
    presence = 'offline',
    authorColor,
    mentionUsers = [],
    mentionRoles = [],
    referencedMessage = null,
    pinned = false,
    onEdit,
    canReact = false,
    canReactToExisting = false,
    customEmojis = [],
    reactionUserKey = '',
    onToggleReaction,
    onDelete,
    onMessageAuthor,
    onRetry,
    onViewProfile,
    onReply,
    onCreateThread,
    onOpenThread,
    onOpenThreads,
    onJumpToReference,
    onTogglePin,
    moderationActions = [],
    onModerate,
    domIdPrefix = 'message',
    actionsEnabled = true,
    timestampFormat = 'time'
  }: {
    message: Message;
    compact?: boolean;
    canEdit?: boolean;
    canDelete?: boolean;
    presence?: PresenceStatus;
    authorColor?: string;
    mentionUsers?: UserSummary[];
    mentionRoles?: Role[];
    referencedMessage?: Message | null;
    pinned?: boolean;
    onEdit?: (message: Message) => void;
    onDelete?: (message: Message) => void;
    canReact?: boolean;
    canReactToExisting?: boolean;
    customEmojis?: CustomEmojiOption[];
    reactionUserKey?: string;
    onToggleReaction?: (message: Message, emoji: string, remove: boolean) => void;
    onMessageAuthor?: (message: Message) => void;
    onRetry?: (message: Message) => void;
    onViewProfile?: (message: Message, event: MouseEvent) => void;
    onReply?: (message: Message) => void;
    onCreateThread?: (message: Message) => void;
    onOpenThread?: (thread: Channel) => void;
    onOpenThreads?: () => void;
    onJumpToReference?: (message: Message) => void;
    onTogglePin?: (message: Message, pinned: boolean) => void;
    moderationActions?: Array<{ id: 'kick' | 'timeout' | 'ban'; label: string }>;
    onModerate?: (user: UserSummary, action: 'kick' | 'timeout' | 'ban') => void;
    domIdPrefix?: string;
    actionsEnabled?: boolean;
    timestampFormat?: 'time' | 'date-time';
  } = $props();

  let menuOpen = $state(false);
  let menuElement = $state<HTMLElement | null>(null);
  let rowElement = $state<HTMLElement | null>(null);
  let menuTrigger: HTMLElement | null = null;
  let menuAnchorX = 0;
  let menuAnchorY = 0;
  let confirmingDelete = $state(false);
  let deleteConfirmationButton = $state<HTMLButtonElement | null>(null);
  let feedback = $state('');
  let mediaViewer = $state<Attachment | null>(null);
  let reactionPickerOpen = $state(false);
  let reactionViewerOpen = $state(false);
  let reportDialogOpen = $state(false);
  let attachmentReport = $state<{
    message: Message;
    attachment: Attachment;
    label: string;
    manifest?: EncryptedFileManifest;
  } | null>(null);
  let reactionViewerInitialEmoji = $state<string | undefined>(undefined);
  let reactionViewerReturnFocus: HTMLElement | null = null;
  let recentReactionValues = $state<string[]>([]);
  let reactionBusy = $state(false);
  let mediaFailures = $state<Record<string, string>>({});
  let mediaAttempts = $state<Record<string, number>>({});
  let attachmentActionError = $state('');
  let menuListenersActive = false;
  const closeExclusiveMenu = (restoreFocus: boolean) => closeMenu(restoreFocus);
  const groupSystemNotice = $derived([3, 4, 5, 18].includes(message.message_type));
  const threadCreatedNotice = $derived(message.message_type === 18);
  const resolvedReference = $derived(referencedMessage ?? message.referenced_message ?? null);
  const presentedMessage = $derived(
    message.message_type === 21 && resolvedReference ? resolvedReference : message
  );
  const renderedContent = $derived(
    presentedMessage.e2ee ? (presentedMessage.decrypted_content ?? null) : presentedMessage.content
  );
  const hasMessageReference = $derived(
    message.message_type !== 21 &&
      Boolean(
        message.referenced_message_id ||
        message.message_reference?.message_id ||
        message.referenced_message
      )
  );

  const editAvailable = $derived(
    !groupSystemNotice && canEdit && !message.deleted_at && !message.pending && !message.queued
  );
  const deleteAvailable = $derived(
    !groupSystemNotice &&
      Boolean(onDelete) &&
      (editAvailable || canDelete) &&
      !message.deleted_at &&
      !message.pending &&
      !message.queued
  );
  const menuAvailable = $derived(
    !groupSystemNotice &&
      !presentedMessage.content_unavailable &&
      actionsEnabled &&
      !message.pending &&
      !message.queued
  );
  // Content-derived network requests are intentionally disabled for E2EE.
  // A manual, disclosed preview flow may be added later, but decrypted URLs
  // must never be submitted to the server implicitly.
  const previewableContent = $derived(presentedMessage.e2ee ? null : renderedContent);
  const inviteReferences = $derived(
    previewableContent ? inviteReferencesInMessage(previewableContent) : []
  );
  const botInviteReferences = $derived(
    previewableContent ? botInvitesInMessage(previewableContent) : []
  );
  const gifUrl = $derived(klipyGifUrl(previewableContent));
  const sticker = $derived(renderedContent ? stickerFromToken(renderedContent) : null);
  let gifFavorited = $derived(gifUrl ? isGifFavorite(gifUrl) : false);
  const linkPreviewUrl = $derived(previewableLink(previewableContent));

  function attachmentKey(attachment: Attachment): string {
    return `${attachment.id}@${attachment.origin_domain}`;
  }

  async function downloadDecryptedAttachment(manifest: EncryptedFileManifest) {
    attachmentActionError = '';
    const matching = message.attachments?.find(
      (item) =>
        item.id === manifest.attachment_id && item.origin_domain === manifest.attachment_domain
    );
    try {
      await downloadEncryptedFile(manifest, matching?.history_media_url);
    } catch (caught) {
      attachmentActionError = userErrorMessage(
        caught,
        'Could not decrypt this file on this device.'
      );
    }
  }

  function markMediaFailed(attachment: Attachment, event: Event) {
    const target = event.currentTarget as HTMLImageElement | HTMLVideoElement;
    mediaFailures = {
      ...mediaFailures,
      [attachmentKey(attachment)]:
        target.dataset.mediaErrorMessage ??
        `Could not load ${attachment.filename}. Check your connection and try again.`
    };
  }

  function retryMedia(attachment: Attachment) {
    const key = attachmentKey(attachment);
    const nextFailures = { ...mediaFailures };
    delete nextFailures[key];
    mediaFailures = nextFailures;
    mediaAttempts = { ...mediaAttempts, [key]: (mediaAttempts[key] ?? 0) + 1 };
  }

  async function downloadAttachment(attachment: Attachment) {
    attachmentActionError = '';
    try {
      await downloadAuthenticatedMedia(
        {
          path: attachmentMediaPath(
            attachment.origin_domain,
            attachment.id,
            'original',
            attachment.history_media_url
          ),
          contentType: attachment.content_type
        },
        attachment.filename
      );
    } catch (caught) {
      attachmentActionError = userErrorMessage(
        caught,
        `Could not download ${attachment.filename}. Try again.`
      );
    }
  }

  function authorName(): string {
    if (message.content_unavailable) return 'Original message';
    return (
      message.webhook?.name ??
      (message.author ? userDisplayName(message.author) : null) ??
      'Unknown author'
    );
  }

  function openProjectedThread(event: MouseEvent) {
    event.stopPropagation();
    if (message.thread) onOpenThread?.(message.thread);
  }

  function openThreadDirectory(event: MouseEvent) {
    event.stopPropagation();
    onOpenThreads?.();
  }

  function visibleTime(): string {
    const createdAt = new Date(message.created_at ?? '');
    if (Number.isNaN(createdAt.getTime())) return '';
    if (timestampFormat === 'date-time') {
      return createdAt.toLocaleString(preferredLocale(), {
        dateStyle: 'medium',
        timeStyle: 'short'
      });
    }
    return createdAt.toLocaleTimeString(preferredLocale(), {
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  function accessibleTime(): string {
    const createdAt = new Date(message.created_at ?? '');
    if (Number.isNaN(createdAt.getTime())) return 'Time unavailable';
    return createdAt.toLocaleString(preferredLocale(), {
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
    menuAnchorX = pointerX;
    menuAnchorY = pointerY;
    menuTrigger = trigger;
    menuOpen = true;
    addMenuListeners();
    void tick().then(() => {
      reactionPickerOpen = false;
      recentReactionValues = recentReactions(reactionUserKey);
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

  function replyToMessage(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(false);
    onReply?.(message);
  }

  function createThread(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(false);
    onCreateThread?.(message);
  }

  function jumpToReference(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    onJumpToReference?.(message);
  }

  function togglePin(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(false);
    onTogglePin?.(message, !pinned);
  }

  function favoriteGif(event: MouseEvent) {
    event.stopPropagation();
    if (!gifUrl) return;
    const result = toggleGifFavorite(gifFavoriteForUrl(gifUrl));
    gifFavorited = result.favorite;
    feedback = result.favorite ? 'GIF added to favorites.' : 'GIF removed from favorites.';
    closeMenu(true);
  }

  async function toggleReaction(value: string, event?: MouseEvent) {
    event?.stopPropagation();
    if (!onToggleReaction || reactionBusy) return;
    const remove = message.reacted_emoji?.includes(value) ?? false;
    const exists = Number(message.reaction_counts?.[value] ?? 0) > 0;
    if (!remove && !canReact && !(canReactToExisting && exists)) return;
    reactionBusy = true;
    if (!remove) rememberReaction(reactionUserKey, value);
    closeMenu(false);
    try {
      await onToggleReaction(message, value, remove);
    } finally {
      reactionBusy = false;
    }
  }

  function openReactionPicker(event: MouseEvent) {
    event.stopPropagation();
    reactionPickerOpen = true;
    void tick().then(() => {
      if (menuOpen && menuElement) {
        placeContextMenu(menuElement, menuAnchorX, menuAnchorY);
      }
    });
  }

  function openReactionViewer(event: MouseEvent) {
    event.stopPropagation();
    reactionViewerReturnFocus = menuTrigger ?? rowElement;
    reactionViewerInitialEmoji = Object.keys(message.reaction_counts ?? {})[0];
    closeMenu(false);
    reactionViewerOpen = true;
  }

  function closeReactionViewer() {
    reactionViewerOpen = false;
    const returnFocus = reactionViewerReturnFocus;
    reactionViewerReturnFocus = null;
    if (returnFocus?.isConnected) {
      void tick().then(() => returnFocus.focus());
    }
  }

  function openReportDialog(event: MouseEvent) {
    event.stopPropagation();
    closeMenu(false);
    reportDialogOpen = true;
  }

  function openAttachmentReport(
    attachment: Attachment,
    label: string,
    event?: MouseEvent,
    manifest?: EncryptedFileManifest
  ) {
    event?.stopPropagation();
    attachmentReport = { message: presentedMessage, attachment, label, manifest };
  }

  function attachmentForManifest(manifest: EncryptedFileManifest): Attachment | null {
    if (!manifest.attachment_id || !manifest.attachment_domain) return null;
    return (
      presentedMessage.attachments?.find(
        (attachment) =>
          attachment.id === manifest.attachment_id &&
          attachment.origin_domain === manifest.attachment_domain
      ) ?? null
    );
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
      feedback = 'Browser denied clipboard access. Allow clipboard permission and try again.';
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
    reactionPickerOpen = false;
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

  function windowScroll(event: Event) {
    if (menuElement && event.composedPath().includes(menuElement)) return;
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
  class:sending={message.pending ||
    message.delivery_status === 'pending' ||
    message.delivery_status === 'retrying'}
  class:failed={message.failed || message.delivery_status === 'failed'}
  class:compact
  class:group-system-notice={groupSystemNotice}
  class:menu-open={menuOpen}
  class="message-row"
  id={`${domIdPrefix}-${entityRef(message)}`}
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
      aria-controls={menuOpen ? `${domIdPrefix}-actions-${entityRef(message)}` : undefined}
      onclick={openKeyboardMenu}
    >
      Message actions
    </button>
  {/if}
  <button
    class="message-avatar"
    class:profile-trigger={Boolean(
      message.author?.profile_resolved !== false &&
      message.author &&
      !message.webhook &&
      onViewProfile
    )}
    type="button"
    disabled={message.author?.profile_resolved === false ||
      !message.author ||
      Boolean(message.webhook) ||
      !onViewProfile}
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
      {compact
        ? ''
        : message.author?.profile_resolved === false
          ? '•'
          : (message.author?.username.slice(0, 1).toUpperCase() ?? '•')}
    {/if}
    {#if !compact && message.author && !message.webhook}
      <i class={`presence-dot presence-${presence}`} aria-hidden="true"></i>
    {/if}
  </button>
  <div class="message-body">
    {#if hasMessageReference}
      <button
        class="message-reply-reference"
        type="button"
        aria-label="Jump to replied message"
        disabled={!onJumpToReference}
        onclick={jumpToReference}
      >
        <span aria-hidden="true">↪</span>
        {#if resolvedReference}
          <strong
            >{resolvedReference.author
              ? userDisplayName(resolvedReference.author)
              : 'Unknown author'}</strong
          >
          <span
            >{resolvedReference.deleted_at
              ? 'Message removed'
              : resolvedReference.content_unavailable
                ? 'Original message unavailable'
                : resolvedReference.decrypted_content ||
                  resolvedReference.content ||
                  'Attachment'}</span
          >
        {:else}
          <span>Referenced message</span>
        {/if}
      </button>
    {/if}
    {#if !compact}
      <header>
        {#if message.author && message.author.profile_resolved !== false && !message.webhook && onViewProfile}
          <button
            class="message-author"
            style:color={authorColor}
            type="button"
            onclick={viewProfile}>{authorName()}</button
          >
        {:else}
          <strong style:color={authorColor}>{authorName()}</strong>
        {/if}
        {#if message.webhook}<small class="webhook-badge">WEBHOOK</small>{/if}
        {#if message.author?.bot}<small class="bot-badge">BOT</small>{/if}
        {#if !presentedMessage.content_unavailable}<time
            datetime={message.created_at}
            title={accessibleTime()}>{visibleTime()}</time
          >{/if}
      </header>
    {:else}
      <span class="visually-hidden">{authorName()}, {accessibleTime()}</span>
    {/if}
    {#if presentedMessage.content_unavailable}
      <p class="message-removed">Original message is no longer available.</p>
    {:else if threadCreatedNotice}
      <div class="group-system-message thread-created-message">
        <span class="group-system-icon" aria-hidden="true">🧵</span>
        <span>
          <strong>{authorName()}</strong> started a thread:
          {#if message.thread && onOpenThread}
            <button type="button" onclick={openProjectedThread}
              >{message.thread.name ?? renderedContent ?? 'Thread'}</button
            >
          {:else}
            <strong>{message.thread?.name ?? renderedContent ?? 'Thread'}</strong>
          {/if}.
          {#if onOpenThreads}
            <button type="button" onclick={openThreadDirectory}>See all threads.</button>
          {/if}
        </span>
        <time datetime={message.created_at} title={accessibleTime()}>{visibleTime()}</time>
      </div>
    {:else if groupSystemNotice}
      <div class="group-system-message">
        <span class="group-system-icon" aria-hidden="true">✦</span>
        <span>{renderedContent}</span>
        <time datetime={message.created_at} title={accessibleTime()}>{visibleTime()}</time>
      </div>
    {:else if presentedMessage.deleted_at}
      <p class="message-removed">Message removed</p>
    {:else if presentedMessage.e2ee && !renderedContent}
      <div class="encrypted-message-unavailable" role="status">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <rect x="5" y="10" width="14" height="10" rx="2" />
          <path d="M8 10V7a4 4 0 0 1 8 0v3" />
        </svg>
        <span>
          <strong>Can’t decrypt this message on this device.</strong>
          Verify or recover this device’s encryption keys, or update Kaede if this device does not support
          the room’s encryption version.
        </span>
      </div>
    {:else if sticker}
      <a
        class="message-sticker"
        href={stickerUrl(sticker.id, sticker.domain)}
        aria-label={`Sticker: ${sticker.name}`}
      >
        <img src={stickerUrl(sticker.id, sticker.domain)} alt={sticker.name} loading="lazy" />
      </a>
    {:else if gifUrl}
      <div class="klipy-gif-wrap">
        <a class="klipy-gif" href={gifUrl} target="_blank" rel="noopener noreferrer">
          <img src={gifUrl} alt="GIF shared from KLIPY" loading="lazy" />
          <small>Powered by KLIPY</small>
        </a>
        <button
          class:active={gifFavorited}
          class="sent-gif-favorite"
          type="button"
          aria-label={gifFavorited ? 'Remove GIF from favorites' : 'Add GIF to favorites'}
          aria-pressed={gifFavorited}
          onclick={favoriteGif}>★</button
        >
      </div>
    {:else if renderedContent}
      <Markdown content={renderedContent} {mentionUsers} {mentionRoles} />
      {#each inviteReferences as reference (reference)}
        <InviteEmbed {reference} />
      {/each}
      {#each botInviteReferences as reference (`${reference.applicationRef}/${reference.templateSlug}`)}
        <BotInviteEmbed {reference} />
      {/each}
      {#if linkPreviewUrl && (presentedMessage.flags & 4) === 0}
        <LinkPreview url={linkPreviewUrl} />
      {/if}
    {/if}
    {#if message.thread && !threadCreatedNotice}
      <button class="thread-preview-card" type="button" onclick={openProjectedThread}>
        <span>
          <strong>{message.thread.name ?? 'Thread'}</strong>
          <b>
            {message.thread.message_count ?? 0}
            {(message.thread.message_count ?? 0) === 1 ? 'Message' : 'Messages'} ›
          </b>
        </span>
        <small>
          {#if message.thread.last_message?.author}
            <strong>{userDisplayName(message.thread.last_message.author)}</strong>
          {/if}
          {message.thread.last_message?.e2ee
            ? 'Encrypted message'
            : (message.thread.last_message?.content ?? 'Open thread')}
        </small>
      </button>
    {/if}
    {#if !presentedMessage.deleted_at && presentedMessage.attachments?.length}
      <div class="message-attachments">
        {#each presentedMessage.attachments as attachment (`${attachment.id}@${attachment.origin_domain}`)}
          {#if attachment.encryption_mode !== 'e2ee'}
            <div
              class="attachment-reportable"
              class:media-reportable={attachment.content_type.startsWith('image/') ||
                attachment.content_type.startsWith('video/')}
            >
              {#if attachment.scan_status === 'pending'}
                <span class="attachment-file">Scanning {attachment.filename}…</span>
              {:else if attachment.scan_status === 'rejected' || attachment.scan_status === 'infected'}
                <span class="attachment-file">Attachment rejected during server processing</span>
              {:else if attachment.scan_status === 'failed'}
                <span class="attachment-file">Attachment processing unavailable</span>
              {:else if attachment.content_type.startsWith('image/')}
                {#if mediaFailures[attachmentKey(attachment)]}
                  <div class="attachment-load-error" role="alert">
                    <span>{mediaFailures[attachmentKey(attachment)]}</span>
                    <button type="button" onclick={() => retryMedia(attachment)}>Try again</button>
                  </div>
                {:else}
                  {#key `${attachmentKey(attachment)}:${mediaAttempts[attachmentKey(attachment)] ?? 0}`}
                    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- authenticated media is served by the API, not a Svelte route -->
                    <button
                      class="attachment-preview-button"
                      type="button"
                      aria-label={`Open ${attachment.filename}`}
                      onclick={() => (mediaViewer = attachment)}
                    >
                      <img
                        use:authenticatedMedia={{
                          path: attachmentMediaPath(
                            attachment.origin_domain,
                            attachment.id,
                            'thumbnail_512',
                            attachment.history_media_url
                          ),
                          contentType: attachment.content_type
                        }}
                        onerror={(event) => markMediaFailed(attachment, event)}
                        alt={attachment.filename}
                        width={attachment.width ?? 512}
                        height={attachment.height ?? 320}
                      />
                    </button>
                  {/key}
                {/if}
              {:else if attachment.content_type.startsWith('video/')}
                {#if mediaFailures[attachmentKey(attachment)]}
                  <div class="attachment-load-error" role="alert">
                    <span>{mediaFailures[attachmentKey(attachment)]}</span>
                    <button type="button" onclick={() => retryMedia(attachment)}>Try again</button>
                  </div>
                {:else}
                  {#key `${attachmentKey(attachment)}:${mediaAttempts[attachmentKey(attachment)] ?? 0}`}
                    <div class="attachment-video">
                      <video
                        use:authenticatedMedia={{
                          path: attachmentMediaPath(
                            attachment.origin_domain,
                            attachment.id,
                            'original',
                            attachment.history_media_url
                          ),
                          contentType: attachment.content_type
                        }}
                        onerror={(event) => markMediaFailed(attachment, event)}
                        controls
                        playsinline
                        preload="metadata"
                      >
                        <track kind="captions" />
                      </video>
                      <button type="button" onclick={() => (mediaViewer = attachment)}
                        >Open viewer</button
                      >
                    </div>
                  {/key}
                {/if}
              {:else}
                <button
                  type="button"
                  class="attachment-file"
                  onclick={() => void downloadAttachment(attachment)}
                >
                  📎 {attachment.filename}
                </button>
              {/if}
              {#if actionsEnabled && !message.pending && !message.queued}
                <button
                  type="button"
                  class="attachment-report-action"
                  aria-label={`Report ${attachment.filename}`}
                  onclick={(event) => openAttachmentReport(attachment, attachment.filename, event)}
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M5 21V4m0 0h11l-2 4 2 4H5" />
                  </svg>
                  Report
                </button>
              {/if}
            </div>
          {/if}
        {/each}
      </div>
      {#if attachmentActionError}
        <p class="form-error" role="alert">{attachmentActionError}</p>
      {/if}
    {/if}
    {#if !presentedMessage.deleted_at && presentedMessage.decrypted_attachments?.length}
      <div class="message-attachments encrypted-attachments">
        {#each presentedMessage.decrypted_attachments as manifest (manifest.file_id)}
          {@const encryptedAttachment = attachmentForManifest(manifest)}
          <div class="attachment-reportable">
            <button
              type="button"
              class="attachment-file"
              onclick={() => void downloadDecryptedAttachment(manifest)}
            >
              🔒 {manifest.filename} · {Math.max(1, Math.ceil(manifest.plaintext_size / 1024))} KB
            </button>
            {#if encryptedAttachment && actionsEnabled && !message.pending && !message.queued}
              <button
                type="button"
                class="attachment-report-action"
                aria-label={`Report ${manifest.filename}`}
                onclick={(event) =>
                  openAttachmentReport(encryptedAttachment, manifest.filename, event, manifest)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M5 21V4m0 0h11l-2 4 2 4H5" />
                </svg>
                Report
              </button>
            {/if}
          </div>
        {/each}
      </div>
      {#if attachmentActionError}
        <p class="form-error" role="alert">{attachmentActionError}</p>
      {/if}
    {/if}
    {#if !message.deleted_at && Object.keys(message.reaction_counts ?? {}).length}
      <div class="message-reactions" aria-label="Message reactions">
        {#each Object.entries(message.reaction_counts ?? {}) as [emoji, count] (emoji)}
          <button
            class:active={message.reacted_emoji?.includes(emoji)}
            type="button"
            disabled={!onToggleReaction ||
              reactionBusy ||
              (!canReact && !canReactToExisting && !message.reacted_emoji?.includes(emoji))}
            aria-label={`${message.reacted_emoji?.includes(emoji) ? 'Remove' : 'Add'} ${emoji} reaction, ${count}`}
            onclick={(event) => void toggleReaction(emoji, event)}
          >
            <ReactionEmoji value={emoji} /><span>{count}</span>
          </button>
        {/each}
      </div>
    {/if}
    {#if message.edited_at || message.failed || message.delivery_status === 'failed' || message.delivery_status === 'retrying' || message.queued}
      <div class="message-meta-actions">
        {#if message.edited_at}<small>(edited)</small>{/if}
        {#if message.failed || message.delivery_status === 'failed'}
          <small class="delivery-failed" role="status">
            {message.failure_reason ?? 'Message not delivered.'}
          </small>
        {/if}
        {#if message.delivery_status === 'retrying'}
          <small role="status">
            {message.failure_reason ??
              'The receiving instance is temporarily at capacity. Kaede is retrying automatically.'}
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
      id={`${domIdPrefix}-actions-${entityRef(message)}`}
      class="message-context-menu"
      role="menu"
      tabindex="-1"
      aria-label="Message actions"
      onkeydown={menuKeydown}
    >
      {#if reactionPickerOpen}
        <ReactionPicker
          {customEmojis}
          onSelect={(value) => void toggleReaction(value)}
          onClose={() => (reactionPickerOpen = false)}
        />
      {:else}
        {#if canReact && onToggleReaction && !message.deleted_at}
          <div class="quick-reactions" aria-label="Recent reactions">
            {#each recentReactionValues as emoji (emoji)}
              <button
                class:active={message.reacted_emoji?.includes(emoji)}
                type="button"
                role="menuitem"
                tabindex="-1"
                title={`React with ${emoji}`}
                onclick={(event) => void toggleReaction(emoji, event)}
                ><ReactionEmoji value={emoji} /></button
              >
            {/each}
          </div>
          <button type="button" role="menuitem" tabindex="-1" onclick={openReactionPicker}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01" />
            </svg>
            <span>Add reaction</span>
          </button>
        {/if}
        {#if Object.keys(message.reaction_counts ?? {}).length > 0}
          <button type="button" role="menuitem" tabindex="-1" onclick={openReactionViewer}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <circle cx="9" cy="9" r="3" />
              <circle cx="17" cy="10" r="2.5" />
              <path d="M3.5 19c.6-3.2 2.4-5 5.5-5s4.9 1.8 5.5 5M14 15c2.9-.7 5.1.6 6 3" />
            </svg>
            <span>View reactions</span>
          </button>
        {/if}
        {#if onMessageAuthor && message.author && !message.deleted_at}
          <button type="button" role="menuitem" tabindex="-1" onclick={messageAuthor}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M20 15a4 4 0 0 1-4 4H8l-4 2V7a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v8Z" />
            </svg>
            <span>Message {userDisplayName(message.author)}</span>
          </button>
        {/if}
        {#if onReply && !message.deleted_at}
          <button type="button" role="menuitem" tabindex="-1" onclick={replyToMessage}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 8-5 4 5 4v-3h4c3.5 0 5.8 1.4 7 4-.2-5.8-3.3-9-9-9H9Z" />
            </svg>
            <span>Reply</span>
          </button>
        {/if}
        {#if onCreateThread && !message.deleted_at}
          <button type="button" role="menuitem" tabindex="-1" onclick={createThread}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M7 3v12a4 4 0 0 0 4 4h8M3 7h8M15 15l4 4-4 4" />
            </svg>
            <span>Create Thread</span>
          </button>
        {/if}
        {#if onTogglePin && !message.deleted_at}
          <button type="button" role="menuitem" tabindex="-1" onclick={togglePin}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m9 3 6 6-2 2 4 4-2 2-4-4-2 2-6-6 6-6Z" />
              <path d="m9 15-5 5" />
            </svg>
            <span>{pinned ? 'Unpin message' : 'Pin message'}</span>
          </button>
        {/if}
        {#if gifUrl}
          <button type="button" role="menuitem" tabindex="-1" onclick={favoriteGif}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z"
              />
            </svg>
            <span>{gifFavorited ? 'Remove from GIF favorites' : 'Add to GIF favorites'}</span>
          </button>
        {/if}
        {#if message.author && message.author.profile_resolved !== false && !message.webhook && onViewProfile}
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
            onclick={(event) => {
              const handle = message.author ? userPublicHandle(message.author) : null;
              if (handle) void copy(`@${handle}`, event);
            }}
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
              <span>{action.label} {userDisplayName(message.author)}</span>
            </button>
          {/each}
        {/if}
        {#if renderedContent && !message.deleted_at}
          <button type="button" role="menuitem" tabindex="-1" onclick={openReportDialog}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M5 21V4m0 1h11l-2 4 2 4H5" />
            </svg>
            <span>Report message</span>
          </button>
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
        {/if}
        {#if deleteAvailable}
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
        {#if renderedContent && !message.deleted_at}
          <button
            class:menu-separator={editAvailable || Boolean(onMessageAuthor)}
            type="button"
            role="menuitem"
            tabindex="-1"
            onclick={(event) => copy(renderedContent ?? '', event)}
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
      {/if}
    </div>
  {/if}
</article>
{#if reactionViewerOpen}
  <ReactionViewer
    {message}
    initialEmoji={reactionViewerInitialEmoji}
    onClose={closeReactionViewer}
  />
{/if}
{#if reportDialogOpen}
  <ReportMessageDialog
    {message}
    onClose={() => (reportDialogOpen = false)}
    onSubmitted={() => (feedback = 'Report submitted to Trust & Safety.')}
  />
{/if}
{#if attachmentReport}
  <ReportMessageDialog
    message={attachmentReport.message}
    attachment={attachmentReport.attachment}
    attachmentLabel={attachmentReport.label}
    attachmentManifest={attachmentReport.manifest}
    onClose={() => (attachmentReport = null)}
    onSubmitted={() => (feedback = 'Attachment report submitted to Trust & Safety.')}
  />
{/if}

{#if mediaViewer}
  <MediaViewer
    attachment={mediaViewer}
    onClose={() => (mediaViewer = null)}
    onReport={() => {
      const attachment = mediaViewer;
      if (!attachment) return;
      mediaViewer = null;
      openAttachmentReport(attachment, attachment.filename);
    }}
  />
{/if}
