<script lang="ts" generics="T extends { key: string }">
  import { t } from '$lib/ui/locale';

  import { onMount, tick, untrack, type Snippet } from 'svelte';

  let {
    items,
    renderItem,
    empty,
    header,
    historyStart,
    hasEarlier = false,
    loadingEarlier = false,
    hasLater = false,
    loadingLater = false,
    onLoadEarlier,
    onLoadLater,
    onBottomChange,
    onRead,
    canJumpToRead = false,
    forceJumpToLatest = false,
    onJumpToRead,
    onJumpToLatest,
    jumpingHistory = false,
    targetKey,
    label = $t('ui_messages_04d7b483')
  }: {
    items: T[];
    renderItem: Snippet<[item: T, index: number]>;
    empty?: Snippet;
    /** Optional non-history content pinned above the currently loaded items. */
    header?: Snippet;
    /** Optional explanation shown at the oldest retained history boundary. */
    historyStart?: Snippet;
    hasEarlier?: boolean;
    loadingEarlier?: boolean;
    hasLater?: boolean;
    loadingLater?: boolean;
    onLoadEarlier?: () => Promise<void> | void;
    onLoadLater?: () => Promise<void> | void;
    onBottomChange?: (atBottom: boolean) => void;
    onRead?: (key: string) => void;
    canJumpToRead?: boolean;
    forceJumpToLatest?: boolean;
    onJumpToRead?: () => Promise<unknown> | unknown;
    onJumpToLatest?: () => Promise<unknown> | unknown;
    jumpingHistory?: boolean;
    targetKey?: string | null;
    label?: string;
  } = $props();

  let viewport = $state<HTMLDivElement | null>(null);
  let atBottom = $state(false);
  let unseen = $state(0);
  let previousLastKey = '';
  let initialized = $state(false);
  let contentElement = $state<HTMLDivElement | null>(null);
  let resizeFrame = 0;
  let readTimer: ReturnType<typeof setTimeout> | undefined;

  function scheduleRead() {
    clearTimeout(readTimer);
    readTimer = setTimeout(() => {
      if (
        !viewport ||
        !initialized ||
        document.visibilityState !== 'visible' ||
        !document.hasFocus()
      )
        return;
      const bounds = viewport.getBoundingClientRect();
      const visible = Array.from(
        viewport.querySelectorAll<HTMLElement>('[data-virtual-key^="message:"]')
      )
        .reverse()
        .find((element) => {
          const rect = element.getBoundingClientRect();
          return rect.bottom > bounds.top && rect.bottom <= bounds.bottom;
        });
      if (visible?.dataset.virtualKey) onRead?.(visible.dataset.virtualKey);
    }, 250);
  }

  async function jumpToLatest() {
    if ((hasLater || forceJumpToLatest) && onJumpToLatest) await onJumpToLatest();
    else await scrollToBottom();
  }

  function historyKeydown(event: KeyboardEvent) {
    if (event.shiftKey && event.key === 'PageUp' && canJumpToRead) {
      event.preventDefault();
      void onJumpToRead?.();
    } else if (event.ctrlKey && event.key === 'End') {
      event.preventDefault();
      void jumpToLatest();
    }
  }

  function prefersReducedMotion(): boolean {
    return (
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function updateBottom(next: boolean) {
    if (next === atBottom) return;
    atBottom = next;
    onBottomChange?.(next && !hasLater);
  }

  function pinViewportToBottom() {
    if (!viewport || !items.length) return;
    viewport.scrollTop = viewport.scrollHeight;
    updateBottom(!hasLater);
    unseen = 0;
  }

  function viewportScrolled() {
    if (!viewport) return;
    const nextAtBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 24;
    updateBottom(nextAtBottom && !hasLater);
    scheduleRead();
    if (nextAtBottom) unseen = 0;
    if (initialized && viewport.scrollTop <= 24 && hasEarlier && !loadingEarlier) {
      void loadEarlierAnchored();
    }
    if (initialized && nextAtBottom && hasLater && !loadingLater && onLoadLater) {
      void onLoadLater();
    }
  }

  async function loadEarlierAnchored() {
    if (!onLoadEarlier || !viewport || loadingEarlier || !items.length) return;
    const previousHeight = viewport.scrollHeight;
    const previousTop = viewport.scrollTop;
    await onLoadEarlier();
    await tick();
    if (viewport) viewport.scrollTop = previousTop + viewport.scrollHeight - previousHeight;
  }

  async function scrollToBottom(): Promise<boolean> {
    if (!viewport || !items.length) return false;
    const wasInitialized = initialized;
    await tick();
    if (!viewport) return false;
    viewport.scrollTo({
      top: viewport.scrollHeight,
      behavior: wasInitialized && !prefersReducedMotion() ? 'smooth' : 'auto'
    });
    if (!wasInitialized) {
      viewport.scrollTop = viewport.scrollHeight;
    }
    updateBottom(!hasLater);
    initialized = true;
    unseen = 0;
    scheduleRead();
    return true;
  }

  async function scrollToTarget(key: string) {
    const index = items.findIndex((item) => item.key === key);
    if (index < 0 || !viewport) return false;
    await tick();
    viewport.querySelector<HTMLElement>(`[data-virtual-key="${CSS.escape(key)}"]`)?.scrollIntoView({
      block: 'center'
    });
    initialized = true;
    updateBottom(false);
    scheduleRead();
    return true;
  }

  onMount(() => {
    if (!viewport) return;
    const currentViewport = viewport;
    const resizeObserver = new ResizeObserver(() => {
      if (!initialized || !atBottom || !viewport) return;
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(pinViewportToBottom);
    });
    if (contentElement) resizeObserver.observe(contentElement);
    currentViewport.addEventListener('scroll', viewportScrolled);
    window.addEventListener('focus', scheduleRead);
    document.addEventListener('visibilitychange', scheduleRead);
    if (items.length && !initialized) {
      if (targetKey) void tick().then(() => scrollToTarget(targetKey));
      else void tick().then(scrollToBottom);
    }
    return () => {
      clearTimeout(readTimer);
      window.removeEventListener('focus', scheduleRead);
      document.removeEventListener('visibilitychange', scheduleRead);
      resizeObserver.disconnect();
      window.cancelAnimationFrame(resizeFrame);
      currentViewport.removeEventListener('scroll', viewportScrolled);
    };
  });

  $effect(() => {
    const later = hasLater;
    untrack(() => {
      if (!later && initialized) void tick().then(viewportScrolled);
    });
  });

  $effect.pre(() => {
    const currentLastKey = items.at(-1)?.key ?? '';
    untrack(() => {
      if (!currentLastKey) {
        previousLastKey = '';
        initialized = false;
        unseen = 0;
        updateBottom(false);
        return;
      }
      if (!previousLastKey) {
        if (targetKey) void tick().then(() => scrollToTarget(targetKey));
        else void tick().then(scrollToBottom);
      } else if (currentLastKey !== previousLastKey) {
        if (atBottom) void tick().then(scrollToBottom);
        else unseen += 1;
      } else if (atBottom) {
        // History can load before a retained ephemeral response without changing the last key.
        void tick().then(pinViewportToBottom);
      }
      previousLastKey = currentLastKey;
    });
  });
</script>

<div class="virtual-message-shell">
  {#if canJumpToRead}
    <button class="read-position-banner" disabled={jumpingHistory} onclick={onJumpToRead}>
      {$t('chat_jump_to_read')}
    </button>
  {/if}
  <span id="message-history-keyboard-help" class="visually-hidden">
    {$t('ui_scroll_this_region_with_page_up_and_page_down_7fffc5aa')}
  </span>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions (the scrollable message region needs a keyboard entry point) -->
  <div
    bind:this={viewport}
    class="virtual-message-viewport"
    role="region"
    tabindex="0"
    aria-label={`${label} history`}
    aria-describedby="message-history-keyboard-help"
    onkeydown={historyKeydown}
    aria-busy={loadingEarlier || loadingLater || jumpingHistory}
  >
    <div bind:this={contentElement} class="virtual-message-content">
      {#if hasEarlier}
        <button class="history-button" disabled={loadingEarlier} onclick={loadEarlierAnchored}>
          {loadingEarlier ? $t('ui_loading_ba3bbbe1') : $t('ui_load_earlier_messages_33fd46b9')}
        </button>
      {:else if items.length && historyStart}
        {@render historyStart()}
      {/if}
      {#if header}{@render header()}{/if}
      {#if !items.length && empty}{@render empty()}{/if}
      {#each items as item, index (item.key)}
        <div class="virtual-message-item" data-virtual-key={item.key}>
          {@render renderItem(item, index)}
        </div>
      {/each}
      {#if hasLater}
        <button class="history-button" disabled={loadingLater} onclick={onLoadLater}>
          {loadingLater ? $t('ui_loading_ba3bbbe1') : $t('ui_load_newer_messages_dc294d6b')}
        </button>
      {/if}
    </div>
  </div>
  {#if forceJumpToLatest || unseen > 0 || hasLater || (initialized && !atBottom)}
    <button class="new-message-pill" disabled={jumpingHistory} onclick={jumpToLatest}>
      {$t('chat_jump_to_latest')}
    </button>
  {/if}
</div>
