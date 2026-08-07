<script lang="ts" generics="T extends { key: string }">
  import { bottomVirtualWindow } from '$lib/chat/virtualization';
  import { onMount, tick, untrack, type Snippet } from 'svelte';

  let {
    items,
    renderItem,
    empty,
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
  let windowStart = $state(0);
  let windowEnd = $state(100);
  let estimatedHeight = $state(76);
  let contentElement = $state<HTMLDivElement | null>(null);
  let topSpacerElement = $state<HTMLDivElement | null>(null);
  let bottomSpacerElement = $state<HTMLDivElement | null>(null);
  let initialPinTimers: number[] = [];
  let initialPinActive = false;
  let resizeFrame = 0;
  const overscan = 24;
  const visibleItems = $derived(items.slice(windowStart, windowEnd));
  const topSpacer = $derived(windowStart * estimatedHeight);
  const bottomSpacer = $derived(Math.max(0, items.length - windowEnd) * estimatedHeight);

  function updateWindow(itemCount = items.length) {
    if (!viewport) return;
    const visible = Math.ceil(viewport.clientHeight / estimatedHeight);
    const start = Math.max(0, Math.floor(viewport.scrollTop / estimatedHeight) - overscan);
    windowStart = start;
    windowEnd = Math.min(itemCount, start + visible + overscan * 2);
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
    onBottomChange?.(next);
  }

  function cancelInitialBottomPin() {
    initialPinActive = false;
    for (const timer of initialPinTimers) window.clearTimeout(timer);
    initialPinTimers = [];
  }

  function pinViewportToBottom() {
    if (!viewport || !items.length) return;
    viewport.scrollTop = viewport.scrollHeight;
    updateWindow();
    updateBottom(true);
    unseen = 0;
  }

  function scheduleInitialBottomPin() {
    cancelInitialBottomPin();
    initialPinActive = true;
    const delays = [0, 50, 150, 300, 600, 1000];
    initialPinTimers = delays.map((delay, index) =>
      window.setTimeout(() => {
        if (!initialPinActive) return;
        pinViewportToBottom();
        if (index === delays.length - 1) {
          initialPinActive = false;
          initialPinTimers = [];
        }
      }, delay)
    );
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
    updateWindow();
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
    if (!wasInitialized) {
      const initialWindow = bottomVirtualWindow(
        items.length,
        viewport.clientHeight,
        estimatedHeight,
        overscan
      );
      windowStart = initialWindow.start;
      windowEnd = initialWindow.end;
      await tick();
      if (!viewport) return false;
    }
    viewport.scrollTo({
      top: viewport.scrollHeight,
      behavior: wasInitialized && !prefersReducedMotion() ? 'smooth' : 'auto'
    });
    if (!wasInitialized) {
      await tick();
      if (!viewport) return false;
      viewport.scrollTop = viewport.scrollHeight;
      updateWindow();
    }
    updateBottom(true);
    initialized = true;
    unseen = 0;
    if (!wasInitialized) scheduleInitialBottomPin();
    return true;
  }

  async function scrollToTarget(key: string) {
    const index = items.findIndex((item) => item.key === key);
    if (index < 0 || !viewport) return false;
    windowStart = Math.max(0, index - overscan);
    windowEnd = Math.min(items.length, index + overscan);
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
    currentViewport.addEventListener('wheel', cancelInitialBottomPin, { passive: true });
    currentViewport.addEventListener('pointerdown', cancelInitialBottomPin, { passive: true });
    updateWindow();
    if (items.length && !initialized) {
      if (targetKey) void tick().then(() => scrollToTarget(targetKey));
      else void tick().then(scrollToBottom);
    }
    return () => {
      resizeObserver.disconnect();
      window.cancelAnimationFrame(resizeFrame);
      cancelInitialBottomPin();
      currentViewport.removeEventListener('scroll', viewportScrolled);
      currentViewport.removeEventListener('wheel', cancelInitialBottomPin);
      currentViewport.removeEventListener('pointerdown', cancelInitialBottomPin);
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

  $effect(() => {
    const itemCount = items.length;
    untrack(() => updateWindow(itemCount));
  });

  $effect(() => {
    if (topSpacerElement) topSpacerElement.style.setProperty('height', `${topSpacer}px`);
    if (bottomSpacerElement) bottomSpacerElement.style.setProperty('height', `${bottomSpacer}px`);
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
      {/if}
      {#if !items.length && empty}{@render empty()}{/if}
      <div bind:this={topSpacerElement} class="virtual-message-spacer"></div>
      {#each visibleItems as item, index (item.key)}
        <div class="virtual-message-item" data-virtual-key={item.key}>
          {@render renderItem(item, windowStart + index)}
        </div>
      {/each}
      <div bind:this={bottomSpacerElement} class="virtual-message-spacer"></div>
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
