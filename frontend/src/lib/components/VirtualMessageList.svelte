<script lang="ts" generics="T extends { key: string }">
  import { onMount, tick, untrack, type Snippet } from 'svelte';

  let {
    items,
    renderItem,
    empty,
    historyStart,
    hasEarlier = false,
    loadingEarlier = false,
    hasLater = false,
    loadingLater = false,
    onLoadEarlier,
    onLoadLater,
    onBottomChange,
    targetKey,
    label = 'Messages'
  }: {
    items: T[];
    renderItem: Snippet<[item: T, index: number]>;
    empty?: Snippet;
    /** Optional explanation shown at the oldest retained history boundary. */
    historyStart?: Snippet;
    hasEarlier?: boolean;
    loadingEarlier?: boolean;
    hasLater?: boolean;
    loadingLater?: boolean;
    onLoadEarlier?: () => Promise<void> | void;
    onLoadLater?: () => Promise<void> | void;
    onBottomChange?: (atBottom: boolean) => void;
    targetKey?: string | null;
    label?: string;
  } = $props();

  let viewport = $state<HTMLDivElement | null>(null);
  let atBottom = $state(false);
  let unseen = $state(0);
  let previousLastKey = '';
  let initialized = false;
  let contentElement = $state<HTMLDivElement | null>(null);
  let resizeFrame = 0;

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
    onBottomChange?.(next);
  }

  function pinViewportToBottom() {
    if (!viewport || !items.length) return;
    viewport.scrollTop = viewport.scrollHeight;
    updateBottom(true);
    unseen = 0;
  }

  function viewportScrolled() {
    if (!viewport) return;
    const nextAtBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 24;
    updateBottom(nextAtBottom);
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
    updateBottom(true);
    initialized = true;
    unseen = 0;
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
    if (items.length && !initialized) {
      if (targetKey) void tick().then(() => scrollToTarget(targetKey));
      else void tick().then(scrollToBottom);
    }
    return () => {
      resizeObserver.disconnect();
      window.cancelAnimationFrame(resizeFrame);
      currentViewport.removeEventListener('scroll', viewportScrolled);
    };
  });

  $effect(() => {
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
      }
      previousLastKey = currentLastKey;
    });
  });
</script>

<div class="virtual-message-shell">
  <span id="message-history-keyboard-help" class="visually-hidden">
    Scroll this region with Page Up and Page Down. Each message has a keyboard-accessible actions
    menu.
  </span>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (the scrollable message region needs a keyboard entry point) -->
  <div
    bind:this={viewport}
    class="virtual-message-viewport"
    role="region"
    tabindex="0"
    aria-label={`${label} history`}
    aria-describedby="message-history-keyboard-help"
    aria-busy={loadingEarlier}
  >
    <div bind:this={contentElement} class="virtual-message-content">
      {#if hasEarlier}
        <button class="history-button" disabled={loadingEarlier} onclick={loadEarlierAnchored}>
          {loadingEarlier ? 'Loading…' : 'Load earlier messages'}
        </button>
      {:else if items.length && historyStart}
        {@render historyStart()}
      {/if}
      {#if !items.length && empty}{@render empty()}{/if}
      {#each items as item, index (item.key)}
        <div class="virtual-message-item" data-virtual-key={item.key}>
          {@render renderItem(item, index)}
        </div>
      {/each}
      {#if hasLater}
        <button class="history-button" disabled={loadingLater} onclick={onLoadLater}>
          {loadingLater ? 'Loading…' : 'Load newer messages'}
        </button>
      {/if}
    </div>
  </div>
  {#if unseen > 0}
    <button class="new-message-pill" onclick={scrollToBottom} aria-label={`${unseen} new messages`}>
      {unseen} new {unseen === 1 ? 'message' : 'messages'} ↓
    </button>
  {/if}
</div>
